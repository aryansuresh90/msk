"""
Nifty50 swing paper-trading engine — core logic. v2: trend/relative-strength
filters, sector concentration cap, trailing stop + partial profit-taking,
max holding period, and ntfy.sh push alerts on trade events.

Shared by:
  - main() below: a long-running loop, for manual/local use only.
  - run_once.py: the single-cycle entrypoint GitHub Actions calls on a
    schedule (.github/workflows/paper_engine.yml).

No real orders are ever placed - everything here is simulated.
All internal timestamps are UTC-aware. Market-hours checks use IST
(Asia/Kolkata) explicitly, independent of the host machine's timezone.
"""
import json
import warnings
from pathlib import Path
from datetime import datetime, timedelta, time as dtime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
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
MAX_OPEN_POSITIONS = 8       # overall diversification cap
SECTOR_MAX_POSITIONS = 2     # max simultaneous open positions per sector
MAX_HOLD_DAYS = 10           # forced exit if a trade runs longer than this (this is a swing system, not a hold-forever one)
BREAKEVEN_AT_ATR = 1.0       # move stop to breakeven once unrealised gain reaches this many ATRs
TRAIL_START_ATR = 2.0        # start trailing the stop once gain reaches this many ATRs
TRAIL_DISTANCE_ATR = 1.0     # trailing stop sits this many ATRs behind the current price
PARTIAL_TAKE_ATR = 1.5       # book partial profit once gain reaches this many ATRs
PARTIAL_TAKE_FRACTION = 0.5  # fraction of the position closed at the partial-profit point
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

# Rough sector groupings, used only to cap concentration - not exact GICS.
SECTOR_MAP = {
    "ADANIENT":"Conglomerate","ADANIPORTS":"Infra","APOLLOHOSP":"Healthcare","ASIANPAINT":"Consumer",
    "AXISBANK":"Banking","BAJAJ-AUTO":"Auto","BAJFINANCE":"Financials","BAJAJFINSV":"Financials",
    "BEL":"Industrials","BHARTIARTL":"Telecom","CIPLA":"Pharma","COALINDIA":"Energy","DRREDDY":"Pharma",
    "EICHERMOT":"Auto","ETERNAL":"Consumer","GRASIM":"Cement","HCLTECH":"IT","HDFCBANK":"Banking",
    "HDFCLIFE":"Insurance","HINDALCO":"Metals","HINDUNILVR":"FMCG","ICICIBANK":"Banking","INDIGO":"Aviation",
    "INFY":"IT","ITC":"FMCG","JIOFIN":"Financials","JSWSTEEL":"Metals","KOTAKBANK":"Banking","LT":"Infra",
    "M&M":"Auto","MARUTI":"Auto","MAXHEALTH":"Healthcare","NESTLEIND":"FMCG","NTPC":"Energy","ONGC":"Energy",
    "POWERGRID":"Energy","RELIANCE":"Energy","SBILIFE":"Insurance","SHRIRAMFIN":"Financials","SBIN":"Banking",
    "SUNPHARMA":"Pharma","TCS":"IT","TATACONSUM":"FMCG","TMPV":"Auto","TATASTEEL":"Metals","TECHM":"IT",
    "TITAN":"Consumer","TRENT":"Retail","ULTRACEMCO":"Cement","WIPRO":"IT",
}

# ntfy.sh push alerts - free, no signup, no key. Pick a hard-to-guess topic
# name (this one is fine to keep, or swap it) and subscribe to it in the
# free ntfy app (iOS/Android) to get a push notification for every trade
# event. Set to None to disable alerts entirely.
NTFY_TOPIC = "nifty-paper-desk-suresh-8f2k91"


def send_alert(title, message, priority="default", tags=""):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except Exception as e:
        print("  alert failed:", e)


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


def true_range(df):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    return pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)


def atr(df, period=14):
    tr = true_range(df)
    return tr.ewm(alpha=1/period, min_periods=period).mean()


def adx(df, period=14):
    """Average Directional Index - trend strength, 0-100. >20ish = trending, <15ish = choppy."""
    high, low = df["High"], df["Low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(df)
    atr_ = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, min_periods=period).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, min_periods=period).mean() / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/period, min_periods=period).mean()


def weekly_trend_ok(df):
    """Require price above its own 10-week SMA - a coarse multi-timeframe filter.
    Returns True (pass) if there isn't enough weekly history yet, so early data
    doesn't block every trade."""
    try:
        weekly = df["Close"].resample("W").last().dropna()
        if len(weekly) < 10:
            return True
        wsma10 = weekly.rolling(10).mean()
        return bool(weekly.iloc[-1] > wsma10.iloc[-1])
    except Exception:
        return True


def analyze_symbol(df, nifty_chg20=None):
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
    adx14 = adx(df, 14)
    vol_avg20 = df["Volume"].rolling(20).mean()

    ltp = float(close.iloc[-1])
    donchian_high20 = float(df["High"].iloc[-21:-1].max()) if len(df) > 21 else None
    donchian_low20 = float(df["Low"].iloc[-21:-1].min()) if len(df) > 21 else None

    breakout = donchian_high20 is not None and ltp > donchian_high20
    breakdown = donchian_low20 is not None and ltp < donchian_low20

    chg20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else None
    rel_strength = (chg20 - nifty_chg20) if (chg20 is not None and nifty_chg20 is not None) else None

    return {
        "ltp": ltp, "sma20": _f(sma20.iloc[-1]), "sma50": _f(sma50.iloc[-1]),
        "rsi14": _f(rsi14.iloc[-1]), "macd_hist": _f(macd_hist.iloc[-1]),
        "macd_hist_prev": _f(macd_hist.iloc[-2]) if len(df) > 1 else None,
        "atr14": _f(atr14.iloc[-1]), "adx14": _f(adx14.iloc[-1]),
        "volume": float(df["Volume"].iloc[-1]), "vol_avg20": _f(vol_avg20.iloc[-1]),
        "donchian_high20": donchian_high20, "donchian_low20": donchian_low20,
        "breakout": breakout, "breakdown": breakdown,
        "rel_strength": _f(rel_strength) if rel_strength is not None else None,
        "weekly_trend_ok": weekly_trend_ok(df),
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
    if a["adx14"] is not None:
        if a["adx14"] >= 20: score += 1; reasons.append(f"ADX {a['adx14']:.0f} - trending tape")
        elif a["adx14"] < 15: score -= 1; reasons.append(f"ADX {a['adx14']:.0f} - choppy, breakout less reliable")
    if a["rel_strength"] is not None:
        if a["rel_strength"] > 0.02: score += 1; reasons.append("Outperforming Nifty over 20 days")
        elif a["rel_strength"] < -0.02: score -= 1; reasons.append("Lagging Nifty over 20 days")
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
        score += mult * round(s["confidence"] / 3)
    m_label = market.get("sentiment", "neutral")
    m_mult = {"bullish": 1, "neutral": 0, "bearish": -1}.get(m_label, 0)
    score += m_mult
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
    if buy_value > state["capital"] * 0.25:
        qty = int((state["capital"]*0.25)//entry)
    if qty < 1:
        return
    buy_value = round(qty*entry, 2)
    costs = buy_side_cost(buy_value)
    trade = {
        "symbol": symbol, "entry_time": datetime.now(UTC).isoformat(), "entry_price": entry,
        "qty": qty, "original_qty": qty, "stop": stop, "target": target, "buy_value": buy_value,
        "buy_costs": costs, "reasons": tech_reasons, "sentiment": sent_label, "regime": regime,
        "sector": SECTOR_MAP.get(symbol, "Other"), "atr_entry": round(atr_val, 4),
        "trail_active": False, "partial_taken": False,
    }
    state["open_positions"][symbol] = trade
    print(f"  OPEN  {symbol}  qty={qty}  entry={entry}  stop={stop}  target={target}  risk=Rs{risk_amount:.0f}")
    send_alert(f"Opened {symbol}", f"qty={qty} entry=Rs{entry} stop=Rs{stop} target=Rs{target}\n{'; '.join(tech_reasons)}", tags="chart_with_upwards_trend")


def check_and_close(state, symbol, ltp, reason, qty_override=None):
    pos = state["open_positions"].get(symbol)
    if not pos:
        return
    close_qty = qty_override if qty_override is not None else pos["qty"]
    exit_price = apply_slippage(ltp, "sell")
    sell_value = round(close_qty*exit_price, 2)
    sell_costs = sell_side_cost(sell_value)
    # allocate buy-side value/costs proportionally to the quantity being closed
    frac = close_qty / pos["original_qty"]
    buy_value_frac = round(pos["buy_value"] * (close_qty / pos["qty"]) if pos["qty"] else 0, 2) if qty_override else pos["buy_value"]
    buy_costs_frac = round(pos["buy_costs"] * frac, 2) if qty_override else pos["buy_costs"]
    gross_pnl = round(sell_value - buy_value_frac, 2)
    net_pnl = round(gross_pnl - buy_costs_frac - sell_costs, 2)
    closed = {
        **pos, "qty": close_qty, "exit_time": datetime.now(UTC).isoformat(), "exit_price": exit_price,
        "sell_value": sell_value, "sell_costs": sell_costs, "gross_pnl": gross_pnl,
        "net_pnl": net_pnl, "exit_reason": reason,
    }
    state["closed_trades"].append(closed)
    state["capital"] = round(state["capital"] + net_pnl, 2)

    remaining = pos["qty"] - close_qty
    if remaining <= 0:
        del state["open_positions"][symbol]
        win = net_pnl > 0
        send_alert(
            f"{'Closed' if win else 'Stopped out'}: {symbol}",
            f"exit=Rs{exit_price} reason={reason} net_pnl=Rs{net_pnl:+.2f} capital=Rs{state['capital']:.0f}",
            priority="high" if not win else "default",
            tags="moneybag" if win else "chart_with_downwards_trend",
        )
    else:
        pos["qty"] = remaining
        pos["buy_value"] = round(pos["buy_value"] - buy_value_frac, 2)
        pos["buy_costs"] = round(pos["buy_costs"] - buy_costs_frac, 2)
        send_alert(
            f"Partial profit: {symbol}",
            f"sold {close_qty} of {pos['original_qty']} at Rs{exit_price}, net_pnl=Rs{net_pnl:+.2f}. {remaining} left running.",
            tags="moneybag",
        )
    print(f"  CLOSE {symbol}  qty={close_qty}  exit={exit_price}  reason={reason}  net_pnl=Rs{net_pnl:+.2f}  capital=Rs{state['capital']:.0f}")


def manage_open_position(state, symbol, a):
    """Trailing stop, breakeven-move, partial profit-taking, and max-hold-period
    checks for one open position. Returns True if the position was fully closed."""
    pos = state["open_positions"][symbol]
    ltp = a["ltp"]
    atr_entry = pos.get("atr_entry") or (pos["entry_price"]*0.02)
    gain_atr = (ltp - pos["entry_price"]) / atr_entry if atr_entry else 0

    # max holding period - force exit regardless of price action
    entry_dt = datetime.fromisoformat(pos["entry_time"])
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=UTC)
    held_days = (datetime.now(UTC) - entry_dt).days
    if held_days >= MAX_HOLD_DAYS:
        check_and_close(state, symbol, ltp, "max_hold_period")
        return True

    # stop / target / signal-reversal checks first
    if ltp <= pos["stop"]:
        check_and_close(state, symbol, pos["stop"], "stop_hit")
        return True
    if ltp >= pos["target"] and not pos.get("trail_active"):
        check_and_close(state, symbol, pos["target"], "target_hit")
        return True
    if a["breakdown"] or (a["rsi14"] and a["rsi14"] > 78):
        check_and_close(state, symbol, ltp, "signal_reversal")
        return True

    # partial profit-taking, once
    if not pos.get("partial_taken") and gain_atr >= PARTIAL_TAKE_ATR:
        take_qty = max(1, int(round(pos["original_qty"] * PARTIAL_TAKE_FRACTION)))
        take_qty = min(take_qty, pos["qty"] - 1) if pos["qty"] > 1 else 0
        if take_qty > 0:
            pos["partial_taken"] = True
            check_and_close(state, symbol, ltp, "partial_profit", qty_override=take_qty)
            pos = state["open_positions"].get(symbol)
            if not pos:
                return True

    # trailing stop management (ratchets up only, never down)
    if gain_atr >= TRAIL_START_ATR:
        pos["trail_active"] = True
        new_stop = round(ltp - TRAIL_DISTANCE_ATR*atr_entry, 2)
        if new_stop > pos["stop"]:
            pos["stop"] = new_stop
    elif gain_atr >= BREAKEVEN_AT_ATR and pos["stop"] < pos["entry_price"]:
        pos["stop"] = pos["entry_price"]

    return False


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

    required_fields = ["symbol","entry_time","exit_time","entry_price","exit_price","qty","stop","target","net_pnl"]
    integrity_issues = []
    for t in trades:
        missing = [f for f in required_fields if t.get(f) is None]
        if missing:
            integrity_issues.append(f"{t.get('symbol','?')} missing fields: {missing}")
        if t.get("qty", 0) <= 0:
            integrity_issues.append(f"{t.get('symbol','?')} non-positive qty")
    open_symbols = list(state["open_positions"].keys())
    if len(open_symbols) != len(set(open_symbols)):
        integrity_issues.append("duplicate symbol in open_positions")

    # only count full stop-outs (not partial-profit legs) for stop reliability
    stop_losses = [t for t in trades if t["net_pnl"] < 0 and t["exit_reason"] != "partial_profit"]
    clean_stops = [t for t in stop_losses if t.get("exit_reason") == "stop_hit"]
    stop_reliability_pct = round(100*len(clean_stops)/len(stop_losses), 1) if stop_losses else None

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
def fetch_nifty_regime_and_chg20():
    """Classify the current market regime, and return Nifty's own 20-day % change
    (used for relative-strength scoring on individual stocks)."""
    try:
        df = yf.Ticker("^NSEI").history(period="3mo", interval="1d")
        if len(df) < 21:
            return "unknown", None
        chg20 = float(df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1)
        if chg20 > 0.02:
            regime = "bull"
        elif chg20 < -0.02:
            regime = "bear"
        else:
            regime = "sideways"
        return regime, chg20
    except Exception:
        return "unknown", None


def is_market_open_now():
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
    regime, nifty_chg20 = fetch_nifty_regime_and_chg20()
    all_analysis = {}
    for sym in NIFTY50:
        df = fetch_daily_with_today(sym)
        a = analyze_symbol(df, nifty_chg20)
        if a:
            all_analysis[sym] = a

    gate_before = compute_metrics(state).get("gate_status", {})

    # manage open positions (stop/target/reversal, trailing, partials, max-hold)
    for sym in list(state["open_positions"].keys()):
        a = all_analysis.get(sym)
        if not a:
            continue
        manage_open_position(state, sym, a)

    # look for new entries
    if len(state["open_positions"]) < MAX_OPEN_POSITIONS:
        sector_counts = {}
        for p in state["open_positions"].values():
            sec = p.get("sector", "Other")
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        ranked = []
        for sym, a in all_analysis.items():
            if sym in state["open_positions"]:
                continue
            if not a.get("weekly_trend_ok", True):
                continue  # multi-timeframe filter: skip if weekly trend disagrees
            t_score, reasons = technical_score(a)
            s_score, s_label = sentiment_score(sym, sentiment_data)
            total = t_score + s_score
            ranked.append((total, sym, a, reasons, s_label))
        ranked.sort(key=lambda x: -x[0])

        for total, sym, a, reasons, s_label in ranked:
            if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
                break
            sec = SECTOR_MAP.get(sym, "Other")
            if sector_counts.get(sec, 0) >= SECTOR_MAX_POSITIONS:
                continue
            if total >= 5 and a["breakout"]:
                open_paper_trade(state, sym, a, reasons, s_label, regime)
                sector_counts[sec] = sector_counts.get(sec, 0) + 1

    state["last_cycle_at"] = datetime.now(UTC).isoformat()
    save_state(state)
    metrics = compute_metrics(state)
    METRICS_PATH.parent.mkdir(exist_ok=True)
    json.dump(metrics, open(METRICS_PATH, "w"), indent=2)
    print(f"metrics: trades={metrics['trade_count']} pnl=Rs{metrics.get('total_net_pnl',0)}")

    # milestone alert: gate flips from not-fully-passing to fully-passing
    gate_after = metrics.get("gate_status", {})
    if gate_after and all(v is True or v is None for v in gate_after.values()) and not all(v is True or v is None for v in gate_before.values()):
        send_alert("Go-live gate passed", "All paper-trading validation criteria are now met. Live automation can be considered (still needs your explicit go-ahead).", priority="urgent", tags="rotating_light")


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
