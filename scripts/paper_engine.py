"""
Nifty50 swing paper-trading engine — core logic.

This module is shared by two entrypoints:
  - main() below: a long-running loop, for running manually in a terminal
    (e.g. `python3 paper_engine.py`). Not used by the GitHub Actions setup.
  - run_once.py: a single-cycle entrypoint used by the GitHub Actions
    workflow (.github/workflows/paper_engine.yml), which fires on a
    schedule instead of looping.

No real orders are ever placed - everything here is simulated.
All internal timestamps are UTC-aware (timezone.utc) for consistency
regardless of which machine/server runs this. Market-hours checks use
IST (Asia/Kolkata) explicitly, since that's what NSE trading hours are
defined in - independent of the host machine's own local timezone.
"""
import json
import warnings
from pathlib import Path
from datetime import datetime, timedelta, time as dtime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
from cost_model import buy_side_cost, sell_side_cost, apply_slippage

warnings.filterwarnings("ignore")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

BASE = Path(__file__).parent.parent
STATE_PATH = BASE / "data" / "paper_state.json"
SENTIMENT_PATH = BASE / "data" / "sentiment.json"
WATCHLIST_PATH = BASE / "data" / "watchlist.json"
METRICS_PATH = BASE / "output" / "performance_metrics.json"

CAPITAL = 100_000.0          # fixed capital base you set
RISK_PER_TRADE_PCT = 0.01    # 1% of capital max risk per trade
MAX_OPEN_POSITIONS = 8       # simple diversification cap
CYCLE_SECONDS = 5 * 60       # only used by the manual/local loop in main()
SENTIMENT_STALE_HOURS = 2.5  # ignore sentiment file older than this

NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE",
    "BAJAJFINSV","BEL","BHARTIARTL","CIPLA","COALINDIA","DRREDDY","EICHERMOT","ETERNAL",
    "GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HINDALCO","HINDUNILVR","ICICIBANK","INDIGO",
    "INFY","ITC","JIOFIN","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","MAXHEALTH","NESTLEIND",
    "NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SHRIRAMFIN","SBIN","SUNPHARMA","TCS",
    "TATACONSUM","TMPV","TATASTEEL","TECHM","TITAN","TRENT","ULTRACEMCO","WIPRO",
]


# ---------------- state ----------------
def load_state():
    if STATE_PATH.exists():
        return json.load(open(STATE_PATH))
    return {"capital": CAPITAL, "open_positions": {}, "closed_trades": [], "last_watchlist_update": None}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    json.dump(state, open(STATE_PATH, "w"), indent=2, default=str)


# ---------------- indicators ----------------
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100/(1+rs))


def atr(df, period=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period).mean()


def analyze_symbol(df):
    """df: daily bars with today's latest price appended as the last (partial) row."""
    if df is None or len(df) < 30:
        return None
    close = df["Close"]
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean() if len(df) >= 50 else pd.Series([np.nan]*len(df))
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal
    rsi14 = rsi(close, 14)
    atr14 = atr(df, 14)
    vol_avg20 = df["Volume"].rolling(20).mean()

    ltp = float(close.iloc[-1])
    donchian_high20 = float(df["High"].iloc[-21:-1].max()) if len(df) > 21 else None  # prior 20d high, excl today
    donchian_low20 = float(df["Low"].iloc[-21:-1].min()) if len(df) > 21 else None

    breakout = donchian_high20 is not None and ltp > donchian_high20
    breakdown = donchian_low20 is not None and ltp < donchian_low20

    return {
        "ltp": ltp, "sma20": _f(sma20.iloc[-1]), "sma50": _f(sma50.iloc[-1]),
        "rsi14": _f(rsi14.iloc[-1]), "macd_hist": _f(macd_hist.iloc[-1]),
        "macd_hist_prev": _f(macd_hist.iloc[-2]) if len(df) > 1 else None,
        "atr14": _f(atr14.iloc[-1]),
        "volume": float(df["Volume"].iloc[-1]), "vol_avg20": _f(vol_avg20.iloc[-1]),
        "donchian_high20": donchian_high20, "donchian_low20": donchian_low20,
        "breakout": breakout, "breakdown": breakdown,
    }


def _f(x):
    return None if pd.isna(x) else round(float(x), 4)


# ---------------- signal fusion ----------------
def technical_score(a):
    score = 0
    reasons = []
    if a["breakout"]:
        score += 3; reasons.append(f"Breaking above 20-day range high ({a['donchian_high20']:.1f})")
    if a["breakdown"]:
        score -= 3; reasons.append(f"Breaking below 20-day range low ({a['donchian_low20']:.1f})")
    if a["sma20"] and a["ltp"] > a["sma20"]:
        score += 1
    if a["sma50"] and a["ltp"] > a["sma50"]:
        score += 1
    if a["rsi14"] is not None:
        if 45 <= a["rsi14"] <= 68: score += 1
        elif a["rsi14"] > 75: score -= 2; reasons.append(f"RSI {a['rsi14']} overbought")
        elif a["rsi14"] < 30: score -= 1; reasons.append(f"RSI {a['rsi14']} weak")
    if a["macd_hist"] is not None and a["macd_hist_prev"] is not None:
        if a["macd_hist"] > 0 and a["macd_hist"] > a["macd_hist_prev"]: score += 1
        elif a["macd_hist"] < 0: score -= 1
    if a["volume"] and a["vol_avg20"] and a["volume"] > a["vol_avg20"]*1.3:
        score += 1; reasons.append("Volume surge confirming the move")
    return score, reasons


def sentiment_score(symbol, sentiment_data):
    if not sentiment_data:
        return 0, "no sentiment data"
    stocks = sentiment_data.get("stocks", {})
    s = stocks.get(symbol)
    market = sentiment_data.get("market", {})
    score = 0
    label = "neutral"
    if s and s.get("confidence", 0) > 0:
        label = s["sentiment"]
        mult = {"bullish": 1, "neutral": 0, "bearish": -1}.get(label, 0)
        score += mult * round(s["confidence"] / 3)  # 0-10 conf -> ~0-3 points
    m_label = market.get("sentiment", "neutral")
    m_mult = {"bullish": 1, "neutral": 0, "bearish": -1}.get(m_label, 0)
    score += m_mult  # market tailwind/headwind, +-1
    return score, label


def load_sentiment():
    if not SENTIMENT_PATH.exists():
        return None
    d = json.load(open(SENTIMENT_PATH))
    try:
        raw = d["generated_at"].replace("Z", "+00:00")
        generated = datetime.fromisoformat(raw)
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=UTC)
        age = datetime.now(UTC) - generated
        if age > timedelta(hours=SENTIMENT_STALE_HOURS):
            return None
    except Exception:
        pass
    return d


# ---------------- trade simulation ----------------
def open_paper_trade(state, symbol, a, tech_reasons, sent_label, regime="unknown"):
    entry_raw = a["ltp"]
    entry = apply_slippage(entry_raw, "buy")
    atr_val = a["atr14"] or (entry * 0.02)
    stop = round(entry - 1.5*atr_val, 2)
    target = round(entry + 2.5*atr_val, 2)
    risk_amount = state["capital"] * RISK_PER_TRADE_PCT
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        return
    qty = int(risk_amount // per_share_risk)
    if qty < 1:
        return
    buy_value = qty * entry
    if buy_value > state["capital"] * 0.25:  # don't let one position eat >25% of capital
        qty = int((state["capital"]*0.25)//entry)
    if qty < 1:
        return
    buy_value = round(qty*entry, 2)
    costs = buy_side_cost(buy_value)
    trade = {
        "symbol": symbol, "entry_time": datetime.now(UTC).isoformat(), "entry_price": entry,
        "qty": qty, "stop": stop, "target": target, "buy_value": buy_value,
        "buy_costs": costs, "reasons": tech_reasons, "sentiment": sent_label, "regime": regime,
    }
    state["open_positions"][symbol] = trade
    print(f"  OPEN  {symbol}  qty={qty}  entry={entry}  stop={stop}  target={target}  risk=Rs{risk_amount:.0f}")


def check_and_close(state, symbol, ltp, reason):
    pos = state["open_positions"].get(symbol)
    if not pos:
        return
    exit_price = apply_slippage(ltp, "sell")
    sell_value = round(pos["qty"]*exit_price, 2)
    sell_costs = sell_side_cost(sell_value)
    gross_pnl = round(sell_value - pos["buy_value"], 2)
    net_pnl = round(gross_pnl - pos["buy_costs"] - sell_costs, 2)
    closed = {
        **pos, "exit_time": datetime.now(UTC).isoformat(), "exit_price": exit_price,
        "sell_value": sell_value, "sell_costs": sell_costs, "gross_pnl": gross_pnl,
        "net_pnl": net_pnl, "exit_reason": reason,
    }
    state["closed_trades"].append(closed)
    state["capital"] = round(state["capital"] + net_pnl, 2)
    del state["open_positions"][symbol]
    print(f"  CLOSE {symbol}  exit={exit_price}  reason={reason}  net_pnl=Rs{net_pnl:+.2f}  capital=Rs{state['capital']:.0f}")


# ---------------- performance metrics ----------------
def compute_metrics(state):
    trades = state["closed_trades"]
    n = len(trades)
    if n == 0:
        return {"trade_count": 0, "note": "No closed trades yet."}
    pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins)/n
    avg_win = (gross_win/len(wins)) if wins else 0
    avg_loss = (gross_loss/len(losses)) if losses else 0
    expectancy = win_rate*avg_win - (1-win_rate)*avg_loss
    profit_factor = (gross_win/gross_loss) if gross_loss > 0 else float("inf")

    equity = [CAPITAL]
    for p in pnls:
        equity.append(equity[-1]+p)
    peak = equity[0]
    max_dd = 0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak-e)/peak if peak else 0)

    first_trade_date = datetime.fromisoformat(trades[0]["entry_time"])
    if first_trade_date.tzinfo is None:
        first_trade_date = first_trade_date.replace(tzinfo=UTC)
    weeks_elapsed = round((datetime.now(UTC)-first_trade_date).days/7, 1)

    # data integrity: every closed trade should have a complete, sane record
    required_fields = ["symbol","entry_time","exit_time","entry_price","exit_price","qty","stop","target","net_pnl"]
    integrity_issues = []
    seen_open_symbols = set()
    for t in trades:
        missing = [f for f in required_fields if t.get(f) is None]
        if missing:
            integrity_issues.append(f"{t.get('symbol','?')} missing fields: {missing}")
        if t.get("qty", 0) <= 0:
            integrity_issues.append(f"{t.get('symbol','?')} non-positive qty")
    open_symbols = list(state["open_positions"].keys())
    duplicate_open = len(open_symbols) != len(set(open_symbols))
    if duplicate_open:
        integrity_issues.append("duplicate symbol in open_positions")

    # stop-loss reliability: of trades that lost money, what fraction actually exited
    # at/near the intended stop (vs slipping through on a signal_reversal or gap)?
    stop_losses = [t for t in trades if t["net_pnl"] < 0]
    clean_stops = [t for t in stop_losses if t.get("exit_reason") == "stop_hit"]
    stop_reliability_pct = round(100*len(clean_stops)/len(stop_losses), 1) if stop_losses else None

    # regime stability: net pnl and win rate broken down by market regime at entry
    regimes = {}
    for t in trades:
        r = t.get("regime", "unknown")
        regimes.setdefault(r, {"count": 0, "net_pnl": 0.0, "wins": 0})
        regimes[r]["count"] += 1
        regimes[r]["net_pnl"] += t["net_pnl"]
        if t["net_pnl"] > 0:
            regimes[r]["wins"] += 1
    for r, d in regimes.items():
        d["net_pnl"] = round(d["net_pnl"], 2)
        d["win_rate_pct"] = round(100*d["wins"]/d["count"], 1) if d["count"] else None

    return {
        "trade_count": n, "win_rate_pct": round(win_rate*100,1),
        "net_expectancy_rs": round(expectancy,2), "profit_factor": round(profit_factor,2) if profit_factor!=float("inf") else None,
        "max_drawdown_pct": round(max_dd*100,2), "current_capital": state["capital"],
        "total_net_pnl": round(state["capital"]-CAPITAL,2),
        "weeks_elapsed": weeks_elapsed,
        "data_integrity_issues": integrity_issues,
        "stop_loss_reliability_pct": stop_reliability_pct,
        "regime_breakdown": regimes,
        "gate_status": {
            "trade_count_ok": n >= 50, "weeks_ok": weeks_elapsed >= 4,
            "expectancy_ok": expectancy > 0, "profit_factor_ok": (profit_factor >= 1.2) if profit_factor!=float("inf") else True,
            "drawdown_acceptable": max_dd < 0.15,
            "no_integrity_issues": len(integrity_issues) == 0,
            "stop_loss_reliable": (stop_reliability_pct is None) or (stop_reliability_pct >= 80),
            "regime_stable": all(d["net_pnl"] >= 0 for d in regimes.values()) if len(regimes) > 1 else None,
        },
    }


# ---------------- market hours ----------------
def fetch_nifty_regime():
    """Classify the current market regime off the Nifty 50 index's own 20-day trend."""
    try:
        df = yf.Ticker("^NSEI").history(period="3mo", interval="1d")
        if len(df) < 21:
            return "unknown"
        chg20 = df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1
        if chg20 > 0.02:
            return "bull"
        if chg20 < -0.02:
            return "bear"
        return "sideways"
    except Exception:
        return "unknown"


def is_market_open_now():
    """NSE trading hours (9:15-15:30 IST), checked in IST regardless of host timezone."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9,15) <= t <= dtime(15,30)


def fetch_daily_with_today(symbol):
    try:
        df = yf.Ticker(symbol+".NS").history(period="9mo", interval="1d")
        if df.empty:
            return None
        intraday = yf.Ticker(symbol+".NS").history(period="1d", interval="5m")
        if not intraday.empty:
            last = intraday.iloc[-1]
            today_ist_date = datetime.now(IST).date()
            today = pd.DataFrame([{
                "Open": df["Open"].iloc[-1] if df.index[-1].date()==today_ist_date else last["Open"],
                "High": max(df["High"].iloc[-1], last["High"]) if df.index[-1].date()==today_ist_date else last["High"],
                "Low": min(df["Low"].iloc[-1], last["Low"]) if df.index[-1].date()==today_ist_date else last["Low"],
                "Close": last["Close"], "Volume": last["Volume"],
            }], index=[pd.Timestamp(today_ist_date)])
            if df.index[-1].date() == today_ist_date:
                df = df.iloc[:-1]
            df = pd.concat([df, today])
        return df
    except Exception as e:
        print(f"  fetch failed {symbol}: {e}")
        return None


def run_cycle(state, sentiment_data):
    print(f"\n=== cycle {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')} | open={len(state['open_positions'])} | capital=Rs{state['capital']:.0f} ===")
    regime = fetch_nifty_regime()
    all_analysis = {}
    for sym in NIFTY50:
        df = fetch_daily_with_today(sym)
        a = analyze_symbol(df)
        if a:
            all_analysis[sym] = a

    # manage open positions first
    for sym in list(state["open_positions"].keys()):
        a = all_analysis.get(sym)
        if not a:
            continue
        ltp = a["ltp"]
        pos = state["open_positions"][sym]
        if ltp <= pos["stop"]:
            check_and_close(state, sym, pos["stop"], "stop_hit")
        elif ltp >= pos["target"]:
            check_and_close(state, sym, pos["target"], "target_hit")
        elif a["breakdown"] or (a["rsi14"] and a["rsi14"] > 78):
            check_and_close(state, sym, ltp, "signal_reversal")

    # look for new entries
    if len(state["open_positions"]) < MAX_OPEN_POSITIONS:
        ranked = []
        for sym, a in all_analysis.items():
            if sym in state["open_positions"]:
                continue
            t_score, reasons = technical_score(a)
            s_score, s_label = sentiment_score(sym, sentiment_data)
            total = t_score + s_score
            ranked.append((total, sym, a, reasons, s_label))
        ranked.sort(key=lambda x: -x[0])
        for total, sym, a, reasons, s_label in ranked:
            if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
                break
            if total >= 5 and a["breakout"]:  # qualifying signal threshold
                open_paper_trade(state, sym, a, reasons, s_label, regime)

    state["last_cycle_at"] = datetime.now(UTC).isoformat()  # heartbeat so every run leaves a diff to commit
    save_state(state)
    metrics = compute_metrics(state)
    METRICS_PATH.parent.mkdir(exist_ok=True)
    json.dump(metrics, open(METRICS_PATH, "w"), indent=2)
    print(f"metrics: trades={metrics['trade_count']} pnl=Rs{metrics.get('total_net_pnl',0)}")


def main():
    """Long-running loop for manual/local use only. GitHub Actions uses run_once.py instead."""
    import time
    state = load_state()
    print(f"Paper engine starting. Capital=Rs{state['capital']:.0f}  open={len(state['open_positions'])}  closed={len(state['closed_trades'])}")
    while True:
        if is_market_open_now():
            sentiment_data = load_sentiment()
            try:
                run_cycle(state, sentiment_data)
            except Exception as e:
                print("cycle error:", e)
            time.sleep(CYCLE_SECONDS)
        else:
            print(f"Market closed ({datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}). Sleeping 10 min...")
            time.sleep(600)


if __name__ == "__main__":
    main()
