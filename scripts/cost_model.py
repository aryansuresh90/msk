"""
Realistic Indian equity delivery trade cost model (Zerodha-style, as of 2026).
All rates approximate published statutory + broker rates; review periodically.
"""

STT_RATE = 0.001          # 0.1% on both buy and sell (delivery)
EXCHANGE_RATE = 0.0000322  # 0.00322% NSE transaction charge, both sides
SEBI_RATE = 0.000001       # 0.0001% (₹10 per crore), both sides
STAMP_DUTY_RATE = 0.00015  # 0.015% on buy side only
GST_RATE = 0.18            # on (brokerage + exchange charges)
BROKERAGE = 0.0            # ₹0 for delivery on Zerodha
DP_CHARGE = 15.93          # flat, per scrip, only charged when you SELL a delivery holding

DEFAULT_SLIPPAGE_BPS = 8   # 0.08% assumed adverse slippage per side, tune with real fills later


def buy_side_cost(value):
    exch = value * EXCHANGE_RATE
    stt = value * STT_RATE
    sebi = value * SEBI_RATE
    stamp = value * STAMP_DUTY_RATE
    gst = (BROKERAGE + exch) * GST_RATE
    return round(exch + stt + sebi + stamp + gst, 2)


def sell_side_cost(value):
    exch = value * EXCHANGE_RATE
    stt = value * STT_RATE
    sebi = value * SEBI_RATE
    gst = (BROKERAGE + exch) * GST_RATE
    return round(exch + stt + sebi + gst + DP_CHARGE, 2)


def round_trip_cost(buy_value, sell_value):
    return round(buy_side_cost(buy_value) + sell_side_cost(sell_value), 2)


def apply_slippage(price, side, bps=DEFAULT_SLIPPAGE_BPS):
    """side: 'buy' (pay more) or 'sell' (receive less)"""
    adj = price * (bps / 10000.0)
    return round(price + adj, 2) if side == "buy" else round(price - adj, 2)
