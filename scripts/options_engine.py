"""
Nifty 50 / Bank Nifty options strategy engine - v1 (design + logic complete,
data source pending). See docs/options_strategy.md for the full strategy
write-up and reasoning behind every choice below.

Everything in this file except fetch_option_chain() is pure logic, and is
covered by synthetic-data tests - it doesn't depend on any live data feed
and isn't just untested code sitting around waiting for one.

fetch_option_chain() is the one piece intentionally left unimplemented: it
needs a real, reliable NSE option-chain feed (strikes, open interest, IV,
Greeks). That's what Zerodha Kite Connect access will provide. Until then,
run_options_cycle() (the entrypoint a scheduled workflow would call) logs
that it's waiting and does nothing else - it never fabricates or estimates
option prices in order to pretend to paper-trade with data that doesn't
actually exist.
"""
from pathlib import Path

import pandas as pd

from paper_engine import rsi, adx

BASE = Path(__file__).parent.parent
OPTIONS_STATE_PATH = BASE / "data" / "options_state.json"

# --- configuration - verify against current NSE contract specs before going
# live. NSE revises lot sizes periodically (most recently Jan 2026: Nifty
# 75->65, Bank Nifty 35->30 shares per lot). ---
INDEX_CONFIG = {
    "NIFTY": {"yf_ticker": "^NSEI", "lot_size": 65, "strike_step": 50, "spread_width_points": 200},
    "BANKNIFTY": {"yf_ticker": "^NSEBANK", "lot_size": 30, "strike_step": 100, "spread_width_points": 500},
}

OPTIONS_PAPER_CAPITAL = 800_000          # separate notional pool from the equity book's CAPITAL (Rs 100,000) -
# deliberately NOT a % of it. Index-option lot economics need this: one lot of even a
# modest 200-point-wide Nifty spread (65 shares/lot) has a max loss of roughly
# Rs 8,000-13,000 depending on credit received, and a properly capped 1.5%-per-trade
# risk rule needs a pool comfortably above that or it will correctly, but uselessly,
# size every trade to zero lots. This is paper money either way - the number is set to
# make the simulation behave like a realistically-capitalised options trader would, not
# an arbitrary slice of the equity swing book. Adjust freely; just keep it sized to
# clear a typical spread's max loss at RISK_PER_SPREAD_PCT below.
RISK_PER_SPREAD_PCT = 0.015              # of OPTIONS_PAPER_CAPITAL, per spread
MAX_CONCURRENT_OPTION_POSITIONS = 2     # across NIFTY + BANKNIFTY combined - correlated, one bucket
DAILY_LOSS_CIRCUIT_BREAKER_PCT = 0.03   # of options allocation - stop new entries for the day if breached
MIN_DAYS_TO_EXPIRY = 3                  # skip entering with less runway than this
PROFIT_TARGET_FRACTION = 0.6            # close at 60% of max credit captured
STOP_LOSS_CREDIT_MULTIPLE = 1.75        # close if loss reaches 1.75x credit received
DIRECTIONAL_BUY_MAX_CAPITAL_PCT = 0.005 # of total capital, for the rare directional CE/PE exception
DIRECTIONAL_STOP_LOSS_PCT = 0.38        # close a directional buy if premium down this much
DIRECTIONAL_PROFIT_TARGET_PCT = 0.80    # close at this much premium gain


# ---------------- index-level signal read ----------------
def compute_breadth(all_analysis):
    """% of Nifty 50 constituents trading above their own 20-day SMA. Needs
    only the daily bars the equity engine already fetches - no options data."""
    if not all_analysis:
        return None
    above = sum(1 for a in all_analysis.values() if a.get("sma20") and a["ltp"] > a["sma20"])
    return round(100 * above / len(all_analysis), 1)


def index_bias(df, breadth_pct, market_sentiment_label):
    """The 4-category directional vote that picks bull put spread / bear
    call spread / iron condor, per docs/options_strategy.md."""
    close = df["Close"]
    sma50 = close.rolling(50).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    macd_hist = macd_line - macd_line.ewm(span=9, adjust=False).mean()
    rsi14 = rsi(close, 14)

    ltp = float(close.iloc[-1])
    sma50_val = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else None
    rsi_val = float(rsi14.iloc[-1]) if not pd.isna(rsi14.iloc[-1]) else None
    macd_val = float(macd_hist.iloc[-1]) if not pd.isna(macd_hist.iloc[-1]) else None

    def trend_vote():
        if sma50_val is None:
            return "neutral"
        if ltp > sma50_val * 1.001:
            return "bullish"
        if ltp < sma50_val * 0.999:
            return "bearish"
        return "neutral"

    def momentum_vote():
        if rsi_val is None or macd_val is None:
            return "neutral"
        if rsi_val > 55 and macd_val > 0:
            return "bullish"
        if rsi_val < 45 and macd_val < 0:
            return "bearish"
        return "neutral"

    def breadth_vote():
        if breadth_pct is None:
            return "neutral"
        if breadth_pct >= 60:
            return "bullish"
        if breadth_pct <= 40:
            return "bearish"
        return "neutral"

    def sentiment_vote():
        if market_sentiment_label == "bullish":
            return "bullish"
        if market_sentiment_label == "bearish":
            return "bearish"
        return "neutral"

    votes = {
        "trend": trend_vote(), "momentum": momentum_vote(),
        "breadth": breadth_vote(), "sentiment": sentiment_vote(),
    }
    bullish_votes = sum(1 for v in votes.values() if v == "bullish")
    bearish_votes = sum(1 for v in votes.values() if v == "bearish")

    if bullish_votes >= 3:
        bias = "bullish"
    elif bearish_votes >= 3:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "votes": votes, "bullish_votes": bullish_votes, "bearish_votes": bearish_votes,
        "bias": bias, "ltp": ltp, "rsi14": rsi_val, "macd_hist": macd_val,
    }


def select_structure(bias_result):
    """Bull put spread / bear call spread / iron condor, from index_bias()."""
    if bias_result["bias"] == "bullish":
        return "bull_put_spread"
    if bias_result["bias"] == "bearish":
        return "bear_call_spread"
    return "iron_condor"


def strong_directional_signal(df, breadth_pct, market_sentiment_label, adx_min=20):
    """The stricter check for the rare directional-buy exception: needs the
    same signals as index_bias() PLUS trend strength (ADX) agreeing.
    Returns (is_strong, direction, bias_detail, adx_value)."""
    b = index_bias(df, breadth_pct, market_sentiment_label)
    adx14 = adx(df, 14)
    adx_val = float(adx14.iloc[-1]) if not pd.isna(adx14.iloc[-1]) else None
    trending = adx_val is not None and adx_val >= adx_min
    if b["bullish_votes"] >= 3 and trending and b["votes"]["trend"] == "bullish":
        return True, "bullish", b, adx_val
    if b["bearish_votes"] >= 3 and trending and b["votes"]["trend"] == "bearish":
        return True, "bearish", b, adx_val
    return False, "neutral", b, adx_val


# ---------------- strike / spread construction ----------------
def round_to_step(price, step):
    return int(round(price / step) * step)


def build_spread(symbol, spot, weekly_atr, structure):
    """Builds the strike legs for a spread given a spot price and a
    weekly-ATR proxy for expected move (used until real IV/delta data is
    available - see docs/options_strategy.md). Returns strikes only;
    premium/credit comes from the live option chain once that's wired up."""
    cfg = INDEX_CONFIG[symbol]
    step = cfg["strike_step"]
    width = cfg["spread_width_points"]
    short_distance = round_to_step(1.2 * weekly_atr, step)
    short_distance = max(short_distance, step)  # never zero-distance

    if structure == "bull_put_spread":
        short_strike = round_to_step(spot - short_distance, step)
        long_strike = short_strike - width
        return {"structure": structure, "short_strike": short_strike, "long_strike": long_strike,
                "option_type": "PE", "width": width}
    if structure == "bear_call_spread":
        short_strike = round_to_step(spot + short_distance, step)
        long_strike = short_strike + width
        return {"structure": structure, "short_strike": short_strike, "long_strike": long_strike,
                "option_type": "CE", "width": width}
    if structure == "iron_condor":
        put_short = round_to_step(spot - short_distance, step)
        call_short = round_to_step(spot + short_distance, step)
        return {
            "structure": structure, "width": width,
            "put_leg": {"short_strike": put_short, "long_strike": put_short - width, "option_type": "PE"},
            "call_leg": {"short_strike": call_short, "long_strike": call_short + width, "option_type": "CE"},
        }
    raise ValueError(f"unknown structure {structure}")


# ---------------- sizing and exits ----------------
def spread_lots(options_capital, net_credit_per_share, width_points, lot_size,
                 risk_pct=RISK_PER_SPREAD_PCT):
    """How many lots to trade, given the credit received and the capped max
    loss per lot. net_credit_per_share and width_points are both in index
    points (i.e. per share, before multiplying by lot size)."""
    max_loss_per_share = max(width_points - net_credit_per_share, 0.01)
    max_loss_per_lot = max_loss_per_share * lot_size
    risk_budget = options_capital * risk_pct
    lots = int(risk_budget // max_loss_per_lot)
    return max(lots, 0)


def should_take_profit(entry_credit, current_price, fraction=PROFIT_TARGET_FRACTION):
    """current_price is what it would cost to close (buy back) the spread
    now. Profit realised so far = entry_credit - current_price."""
    if entry_credit <= 0:
        return False
    captured = (entry_credit - current_price) / entry_credit
    return captured >= fraction


def should_stop_loss(entry_credit, current_price, multiple=STOP_LOSS_CREDIT_MULTIPLE):
    if entry_credit <= 0:
        return False
    loss = current_price - entry_credit
    return loss >= multiple * entry_credit


def should_time_stop(days_to_expiry, min_days=1):
    return days_to_expiry <= min_days


def directional_should_stop(entry_premium, current_premium, pct=DIRECTIONAL_STOP_LOSS_PCT):
    if entry_premium <= 0:
        return False
    return (entry_premium - current_premium) / entry_premium >= pct


def directional_should_take_profit(entry_premium, current_premium, pct=DIRECTIONAL_PROFIT_TARGET_PCT):
    if entry_premium <= 0:
        return False
    return (current_premium - entry_premium) / entry_premium >= pct


# ---------------- data source (pending Kite Connect) ----------------
def fetch_option_chain(symbol, expiry=None):
    """Live option-chain fetch - NOT YET WIRED UP. See docs/options_strategy.md
    for why this waits for Zerodha Kite Connect rather than scraping NSE's
    unofficial endpoint from a cloud runner. Expected return shape once
    implemented: a list of dicts, one per strike/type:
        {"strike": int, "option_type": "CE"|"PE", "expiry": "YYYY-MM-DD",
         "ltp": float, "oi": int, "iv": float, "delta": float}
    """
    raise NotImplementedError(
        "fetch_option_chain() is intentionally unimplemented - awaiting Zerodha "
        "Kite Connect access. See docs/options_strategy.md."
    )


def run_options_cycle(state, all_analysis, sentiment_data):
    """Entry point a scheduled workflow would call, mirroring run_cycle() in
    paper_engine.py. Right now this only logs status - it does not fabricate
    trades from missing data. Once fetch_option_chain() is implemented, the
    logic above (index_bias -> select_structure -> build_spread -> sizing ->
    exits) plugs straight in with no redesign."""
    print("  [options] fetch_option_chain() not yet wired up - waiting on Kite Connect access. "
          "Strategy logic is ready (see docs/options_strategy.md); no options trades this cycle.")
    return state
