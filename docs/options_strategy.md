# Nifty 50 / Bank Nifty options strategy (design doc)

Status: **designed and code-scaffolded, not yet automated.** Live options
paper-trading needs a real option-chain feed (strikes, open interest, IV,
Greeks) which free sources can't reliably provide - see "Why this waits for
Kite Connect" below. Nothing here places a real order, ever; even once
automated this stays a paper-trading system until you explicitly decide
otherwise.

## Why spreads, not naked option buying

You chose "spreads primary, occasional directional buys on the strongest
signals only" - that's the right default for a systematic approach. The
blunt version of why: buying options outright means time decay (theta)
fights you every single day you're wrong about direction, magnitude, *and*
timing all at once. Most retail options losses come from being right on
direction but wrong on timing/magnitude and watching premium bleed to zero.
Selling a defined-risk spread instead means time decay works *for* you, and
the max loss is capped and known before you ever enter - no gap-risk
surprise like naked option selling has. The trade-off is a capped profit
too, which is the right trade-off for a system meant to compound steadily
rather than swing for home runs.

## The three spread structures, chosen by regime

1. **Bull put spread** (sell a put, buy a further OTM put beneath it) -
   used when the index-level confluence check (below) is bullish. Collects
   premium; max profit if the index stays above the short strike through
   expiry; max loss is capped at (strike width - net credit).
2. **Bear call spread** (sell a call, buy a further OTM call above it) -
   used when the index-level check is bearish. Mirror image of the above.
3. **Iron condor** (bull put spread + bear call spread simultaneously) -
   used when the index-level check is mixed/range-bound: no clear directional
   edge, so collect premium on both sides and profit from the index doing
   nothing. This is usually the single best risk-adjusted structure for
   Indian index options historically, precisely because Nifty/Bank Nifty
   spend a large fraction of time in a range between trend legs.

If India VIX is elevated (see the equity engine's own `REGIME_MAX_VIX` gate,
shared here) - stand aside. Higher IV means bigger premium, but also bigger
unexpected moves; a spike is not the moment to add fresh option-selling risk
into a system that hasn't been live long enough to have proven its edge.

## Index-level confluence check (mirrors the v3 equity engine)

Before choosing a structure, the same "multiple logics must agree" principle
from the equity engine applies to the index itself:

- **Trend**: index above its own 50-day SMA.
- **Momentum**: RSI 45-68 zone and MACD histogram positive/rising, on the
  index itself.
- **Breadth**: percentage of the Nifty 50 constituents trading above their
  own 20-day SMA (a market-breadth proxy - this needs no options data at
  all, just the same daily bars the equity engine already fetches). Breadth
  above 60% supports a bullish structure, below 40% supports bearish,
  in between is neutral/mixed.
- **Sentiment**: the market-wide Gemini sentiment read already produced by
  `sentiment_scan.py` - not bearish for a bull spread, not bullish for a
  bear spread.

Bias rule: 3 or 4 of these agreeing bullish -> bull put spread. 3 or 4
agreeing bearish -> bear call spread. Anything mixed (roughly even split,
or fewer than 3 agreeing either way) -> iron condor. This reuses
infrastructure the equity engine already has (daily bars for all 50 stocks,
the sentiment file) rather than needing anything new to compute breadth and
momentum.

## Strike and expiry selection

- **Expiry**: nearest weekly expiry with at least 3 trading days left (skip
  entering anything with <3 days to expiry - gamma risk gets extreme in the
  final days and a small move can blow through a "safe" short strike fast).
  NSE's weekly-expiry weekday has changed more than once historically, so
  the code queries available expiries from the data source rather than
  hardcoding a day of the week - that's a deliberate design choice, not an
  oversight.
- **Short strike distance (until real IV/delta data is available)**: an
  ATR-based proxy for expected move - short strike placed roughly
  `1.2 x weekly ATR` out of the money. Once Kite Connect is live, this
  upgrades to proper delta-based selection (short strike at ~0.15-0.20
  delta), which is the industry-standard way to do this and more precise
  than an ATR proxy.
- **Spread width (long strike distance beyond the short strike)**: a fixed
  width sized to keep max loss per lot reasonable - default 200 points for
  Nifty, 500 points for Bank Nifty (both roughly 1-1.5% of typical index
  levels; adjust as the index level changes over time).

## Position sizing and risk controls

- Options run on their own separate notional paper-capital pool (default
  Rs 800,000), not a slice of the equity book's Rs 100,000. This isn't
  arbitrary - it's a real constraint the synthetic tests caught: one lot of
  even a modest 200-point-wide Nifty spread (65 shares/lot, the current NSE
  lot size as of early 2026) has a max loss of roughly Rs 8,000-13,000
  depending on credit received. A 1.5%-per-trade risk cap needs a pool
  comfortably above that number, or the sizing math will correctly - but
  uselessly - round every trade down to zero lots. Both numbers are still
  paper money; this just makes the simulation behave like a realistically
  capitalised options trader instead of an arbitrary carve-out of the
  equity swing capital. Worth remembering when you eventually compare
  results between the two books - they're not on the same capital base by
  design.
- Max risk per spread: 1.5% of that options pool (defined loss = spread
  width - net credit received, known exactly at entry).
- Nifty and Bank Nifty are highly correlated (both are large-cap Indian
  equity indices) - they're treated as **one exposure bucket**, not two
  independent ones. Max 2 concurrent option positions total across both
  indices, not 2 each.
- Daily circuit breaker: if realized + unrealized options P&L for the day
  drops below -3% of the options sub-allocation, no new option entries for
  the rest of that day (existing positions still get managed/closed
  normally). This is a standard systematic-desk safeguard against a single
  bad day compounding into a series of revenge trades.

## Exit rules

- **Profit target**: close at 50-70% of the maximum credit received.
  Letting a winning spread run to expiry for the last 30-50% of profit adds
  disproportionate gamma risk for very little extra reward - closing early
  is the higher-Sharpe choice, not a lack of conviction.
- **Stop-loss**: close if the position's loss reaches 1.5-2x the credit
  received (well before the hard max loss at full spread width - this
  keeps losers smaller and more consistent, and again the max loss is
  already capped by construction so this is a discipline rule, not a
  safety-critical one).
- **Time stop**: close by 1 day-to-expiry regardless of P&L - pin risk and
  end-of-day gamma near expiry aren't worth holding through for a
  system this new.

## Occasional directional buys (CE/PE) - the exception, not the rule

Only when the index-level confluence check scores at the very top (5-6 of 6
possible signals agreeing, using the same category structure as the equity
engine, applied to the index) does the system consider an outright
directional buy instead of a spread:

- Strike: near-the-money (ATM or one strike ITM) for a better delta/theta
  trade-off than a far-OTM lottery ticket.
- Size: small - 0.5% of total capital, reflecting that this is the
  higher-variance exception case, not the core strategy.
- Stop-loss: close if premium is down 35-40% from entry.
- Profit target: close at +60-100% of entry premium.
- These trades are expected to be rare by design - if they're firing every
  week, the confluence threshold needs revisiting, not the strategy.

## Why this waits for Kite Connect

Getting this live today would mean scraping NSE's public (unofficial)
option-chain JSON endpoint. It works, but it's an undocumented API that NSE
actively rate-limits and sometimes blocks entirely from data-center/cloud
IP ranges (which is exactly what a GitHub Actions runner is) - so it can go
dark for stretches with no warning, right when you'd want it working. Kite
Connect's data API doesn't have that problem and also gives real Greeks
(delta, IV) instead of the ATR-based proxy used above, which meaningfully
improves the strike-selection step. Building the automation on flaky
scraped data now, then having to redo it once Kite access lands, isn't a
good use of either of our time - so the strategy and code are fully ready,
and the data source is the one piece that plugs in later with no redesign.

## What's already testable today

Everything above the option-chain fetch itself is pure logic - the index
confluence scoring, spread-vs-condor selection, strike/width math given a
hypothetical chain, position sizing, and exit-rule math - and all of it is
implemented in `scripts/options_engine.py` with synthetic-data tests, so
it's verified correct now rather than being untested code waiting for data.
