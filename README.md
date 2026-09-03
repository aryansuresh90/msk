# Nifty Paper Trading Engine — GitHub Actions edition

Runs the same paper-trading logic that was on your Mac, but on GitHub's
servers instead — no laptop needs to stay on. It fires every 15 minutes
during NSE market hours, checks all 50 Nifty stocks, opens/closes simulated
positions per the same rules, and commits the updated state back to this
repo. Nothing here places a real order.

## One-time setup (about 5 minutes)

1. **Create a GitHub account** if you don't have one already — free, at
   github.com.
2. **Create a new repository.** Name it whatever you like (e.g.
   `nifty-paper-trading`). Set it to **Private** — your simulated trade
   journal doesn't need to be public. Do NOT initialize it with a README
   (you're uploading one).
3. **Upload these files, keeping the folder structure exactly as given**
   (the `.github/workflows/` folder especially — that's what makes it
   run automatically). The easiest way: on the new repo's page, click
   "uploading an existing file", then drag this whole unzipped folder in.
   Or, if you're comfortable with git:
   ```
   cd nifty-paper-trading-deploy
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```
4. **Turn on write access for the workflow.** In your repo:
   Settings -> Actions -> General -> scroll to "Workflow permissions" ->
   select **"Read and write permissions"** -> Save. (Without this, the
   workflow can run cycles but can't commit the results back.)
5. **Do a manual test run.** Go to the "Actions" tab -> click "Nifty
   Paper Trading Engine" on the left -> "Run workflow" button -> Run
   workflow. Wait ~1-2 minutes, then check it went green (a checkmark,
   not a red X). If the market's open when you test, you should see a
   commit appear a minute later updating `data/paper_state.json`. If the
   market's closed, the run will just log "Market closed" and exit
   cleanly — that's expected, not a failure.

That's it — from here it runs on its own, every 15 minutes, Mon-Fri,
during market hours, with no dependency on your computer being on.

## Files

- `scripts/paper_engine.py` — all the trading logic (indicators, signal
  scoring, position sizing, cost/slippage model, performance metrics).
- `scripts/run_once.py` — the entrypoint the workflow calls: runs one
  cycle and exits (as opposed to looping forever like the old Mac
  version did).
- `scripts/cost_model.py` — realistic Indian equity delivery trade costs
  (STT, exchange charges, stamp duty, GST, DP charge) and slippage.
- `.github/workflows/paper_engine.yml` — the schedule and the commit step.
- `data/paper_state.json` — current capital, open positions, closed
  trades. This is the file that persists your paper-trading history.
- `output/performance_metrics.json` — win rate, expectancy, profit
  factor, drawdown, and the go-live gate checklist, recomputed every
  cycle.

## What's different from the original Mac version

- Cycle frequency: every ~15 minutes instead of every 5 (a schedule
  fired by GitHub, not a continuous loop — GitHub doesn't allow
  more-frequent schedules). Fine for a multi-day swing strategy.
- Sentiment: automated via `scripts/sentiment_scan.py` and
  `.github/workflows/sentiment_scan.yml` — real headlines (yfinance
  per-stock news + a few macro RSS feeds) read by the Gemini API, not
  keyword matching. Needs a free `GEMINI_API_KEY` repo secret.

## v2 additions (strategy, exits, alerts)

- **Trend/quality filters**: ADX (trend strength) and relative strength
  vs the Nifty index are now part of the entry score, and a weekly
  10-week-SMA check gates entries — a stock breaking out against its
  own weekly downtrend is skipped.
- **Sector concentration cap**: at most 2 open positions per sector at
  once, so the book can't end up all banks or all IT.
- **Trailing stop**: once a trade is up 1 ATR, the stop moves to
  breakeven; once up 2 ATR, the stop trails 1 ATR behind price.
- **Partial profit-taking**: half the position is booked at +1.5 ATR,
  the rest keeps running under the trailing stop.
- **Max holding period**: any trade still open after 10 days is closed
  regardless of price — this is a swing system, not a buy-and-hold one.
- **Push alerts**: every open/close/partial and the go-live gate being
  fully met sends a free push notification via ntfy.sh — install the
  free ntfy app (iOS/Android) and subscribe to the topic named in
  `paper_engine.py` (`NTFY_TOPIC`) to get them on your phone. No
  signup, no key. Change that topic string to anything else you like
  before your first run if you want a private name only you know.
- Cost model, 1% risk sizing, and the 50-trade/4-week gate are
  unchanged.

## v3 additions (multi-logic confirmation, market regime gate, options design)

- **Confluence entry engine**: entries are no longer a single additive
  score. Six independent categories are each scored pass/fail — trend
  alignment and a volume-confirmed breakout are structural requirements
  (both must pass), and at least 3 of the 4 remaining categories (momentum,
  relative strength vs Nifty, ADX trend strength, news sentiment) must also
  agree. One strong signal can no longer single-handedly outvote the
  others — see `confluence_check()` in `scripts/paper_engine.py`.
- **Market regime gate**: before considering any new entry, the system
  checks Nifty's own trend (above its 50-day SMA) and India VIX (below 22).
  If either fails, no new positions are opened that cycle, system-wide —
  open positions still get managed normally (stops, trailing, exits). See
  `fetch_market_regime()`.
- **Stock recommendation snapshot**: every cycle now writes
  `output/stock_candidates.json` — every Nifty 50 stock's full confluence
  breakdown, ranked, including symbols that scored well but weren't traded
  (sector cap, position cap, or regime gate closed) and why. This is where
  to look for "what does the system currently like" beyond just executed
  trades.
- **Options strategy — designed, not yet live**: a full systematic options
  strategy for Nifty 50 / Bank Nifty (credit spreads primary, occasional
  directional buys on the strongest signals only) is documented in
  `docs/options_strategy.md` and implemented as pure, synthetic-tested
  logic in `scripts/options_engine.py`. It deliberately doesn't place any
  option trades yet — live automation needs a real option-chain feed
  (strikes/OI/IV/Greeks), which free NSE scraping can't reliably provide
  from a cloud runner. It's built to switch on with no redesign once
  Zerodha Kite Connect access is available — see the doc for the full
  reasoning.

## Once this is confirmed running

Tell me and I'll point the two dashboards (Nifty Swing Desk, Paper
Trading Desk) at this repo's data instead of your Mac, and you can stop
the old launchd job there (`launchctl unload
~/Library/LaunchAgents/com.niftydashboard.paperengine.plist`) so you
don't end up with two diverging paper-trading histories running in
parallel.
