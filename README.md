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

## What's different from the Mac version

- Cycle frequency: every ~15 minutes instead of every 5 (a schedule
  fired by GitHub, not a continuous loop — GitHub doesn't allow
  more-frequent schedules). Fine for a multi-day swing strategy.
- Sentiment input: `data/sentiment.json` isn't produced here yet — that
  piece is still being sorted out separately. Until it exists, trades
  are decided on technicals alone (the sentiment score just contributes
  0), same as it's been doing on the Mac these last few days.
- Everything else — signal rules, position sizing (1% risk, ATR-based
  stop/target), cost model, performance metrics, the 50-trade/4-week
  gate — is identical.

## Once this is confirmed running

Tell me and I'll point the two dashboards (Nifty Swing Desk, Paper
Trading Desk) at this repo's data instead of your Mac, and you can stop
the old launchd job there (`launchctl unload
~/Library/LaunchAgents/com.niftydashboard.paperengine.plist`) so you
don't end up with two diverging paper-trading histories running in
parallel.
