"""
Single-cycle entrypoint used by the GitHub Actions workflow
(.github/workflows/paper_engine.yml). Fires on a schedule, runs exactly
one cycle if the market is currently open (IST), then exits. The workflow
commits data/paper_state.json and output/performance_metrics.json back to
the repo after this script finishes, so state carries over between runs
without needing any always-on machine.
"""
from paper_engine import load_state, run_cycle, load_sentiment, is_market_open_now


def main():
    if not is_market_open_now():
        print("Market closed (IST) - nothing to do this run.")
        return
    state = load_state()
    sentiment_data = load_sentiment()
    run_cycle(state, sentiment_data)


if __name__ == "__main__":
    main()
