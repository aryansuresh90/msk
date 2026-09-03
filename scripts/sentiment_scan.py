"""
Automated news sentiment scan for the Nifty paper-trading system.
Runs on a schedule via .github/workflows/sentiment_scan.yml - no laptop,
no Cowork session, no device approval needed.

What it does each run:
  1. Reads data/watchlist.json for the list of stocks to check (falls back
     to a small default list if that file is missing).
  2. Pulls recent headlines for the Nifty index and each watchlist stock
     via yfinance's free .news lookup (same library the trading engine
     already uses - no separate news API/key needed for this part), plus
     broader macro headlines from a few free financial-news RSS feeds
     (Economic Times, Moneycontrol, Business Standard) for the market-wide
     read - crude oil, Fed/RBI policy, FII/DII flows, geopolitics etc.
  3. Sends the headlines to the Gemini API (free tier) and asks for a
     structured bullish/neutral/bearish read on the market and each stock,
     with a confidence score and a one-line rationale.
  4. Writes data/sentiment.json in the exact shape paper_engine.py already
     expects - no changes needed on the trading-engine side.

Requires one secret in the repo: GEMINI_API_KEY (Settings -> Secrets and
variables -> Actions -> New repository secret). Get a free key with no
credit card at https://aistudio.google.com/apikey.
"""
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.utils import parsedate_to_datetime

import requests
import yfinance as yf

BASE = Path(__file__).parent.parent
WATCHLIST_PATH = BASE / "data" / "watchlist.json"
SENTIMENT_PATH = BASE / "data" / "sentiment.json"

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_SYMBOLS = 20
MAX_HEADLINES_PER_SYMBOL = 4
NEWS_MAX_AGE_HOURS = 30  # ignore stale headlines
MAX_MACRO_HEADLINES_PER_FEED = 8

DEFAULT_WATCHLIST = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN",
    "BHARTIARTL", "ITC", "LT", "AXISBANK",
]

# Free financial-news RSS feeds, used only for the broad market-wide read
# (crude oil, Fed/RBI policy, FII/DII flows, geopolitics etc.) - no key,
# no signup. If any of these move or go offline, drop/replace the URL.
MACRO_RSS_FEEDS = [
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol Markets", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Business Standard Markets", "https://www.business-standard.com/rss/markets-106.rss"),
]


def load_watchlist():
    if WATCHLIST_PATH.exists():
        try:
            d = json.load(open(WATCHLIST_PATH))
            syms = [w["symbol"] for w in d.get("watchlist", [])]
            if syms:
                return syms[:MAX_SYMBOLS]
        except Exception as e:
            print("watchlist read failed, using default:", e)
    return DEFAULT_WATCHLIST


def recent_headlines(ticker_symbol):
    """Returns a list of {title, publisher, age_hours} for one yfinance ticker."""
    try:
        news = yf.Ticker(ticker_symbol).news or []
    except Exception as e:
        print(f"  news fetch failed {ticker_symbol}: {e}")
        return []
    out = []
    now = datetime.now(timezone.utc)
    for item in news[:10]:
        content = item.get("content", item)  # yfinance news shape has shifted before; handle both
        title = content.get("title") or item.get("title")
        pub_time = content.get("pubDate") or item.get("providerPublishTime")
        publisher = (content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else item.get("publisher")
        age_hours = None
        try:
            if isinstance(pub_time, (int, float)):
                pub_dt = datetime.fromtimestamp(pub_time, tz=timezone.utc)
            else:
                pub_dt = datetime.fromisoformat(str(pub_time).replace("Z", "+00:00"))
            age_hours = round((now - pub_dt).total_seconds() / 3600, 1)
        except Exception:
            pass
        if title and (age_hours is None or age_hours <= NEWS_MAX_AGE_HOURS):
            out.append({"title": title, "publisher": publisher or "unknown", "age_hours": age_hours})
        if len(out) >= MAX_HEADLINES_PER_SYMBOL:
            break
    return out


def fetch_rss_headlines(name, url):
    """Returns a list of {title, publisher, age_hours} from a standard RSS feed."""
    out = []
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        now = datetime.now(timezone.utc)
        for item in root.findall(".//item")[:MAX_MACRO_HEADLINES_PER_FEED]:
            title_el = item.find("title")
            pubdate_el = item.find("pubDate")
            title = title_el.text.strip() if title_el is not None and title_el.text else None
            if not title:
                continue
            age_hours = None
            if pubdate_el is not None and pubdate_el.text:
                try:
                    pub_dt = parsedate_to_datetime(pubdate_el.text)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    age_hours = round((now - pub_dt).total_seconds() / 3600, 1)
                except Exception:
                    pass
            if age_hours is None or age_hours <= NEWS_MAX_AGE_HOURS:
                out.append({"title": title, "publisher": name, "age_hours": age_hours})
    except Exception as e:
        print(f"  RSS fetch failed ({name}): {e}")
    return out


def build_prompt(market_headlines, stock_headlines):
    lines = [
        "You are a financial news sentiment analyst for Indian equities (NSE/Nifty50).",
        "Given recent headlines, classify sentiment for the overall market and for each",
        "listed stock. Use only the headlines given - do not invent news. If a stock has",
        "no headlines below, or none look market-moving, mark it neutral with confidence 0.",
        "",
        "Respond with ONLY a JSON object, no markdown fences, in exactly this shape:",
        '{"market": {"sentiment": "bullish|neutral|bearish", "confidence": 0-10, "rationale": "..."},',
        ' "stocks": {"SYMBOL": {"sentiment": "bullish|neutral|bearish", "confidence": 0-10, "rationale": "..."}, ...}}',
        "",
        "=== Market-wide headlines (Nifty 50 index) ===",
    ]
    if market_headlines:
        for h in market_headlines:
            lines.append(f"- ({h['age_hours']}h ago, {h['publisher']}) {h['title']}")
    else:
        lines.append("(none available this run)")

    for sym, heads in stock_headlines.items():
        lines.append(f"\n=== {sym} headlines ===")
        if heads:
            for h in heads:
                lines.append(f"- ({h['age_hours']}h ago, {h['publisher']}) {h['title']}")
        else:
            lines.append("(none available this run)")
    return "\n".join(lines)


def call_gemini(prompt, api_key):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    resp = requests.post(GEMINI_URL, params={"key": api_key}, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY not set - skipping this run.")
        return

    symbols = load_watchlist()
    print(f"Scanning sentiment for market + {len(symbols)} symbols: {symbols}")

    market_headlines = recent_headlines("^NSEI")
    for name, url in MACRO_RSS_FEEDS:
        market_headlines.extend(fetch_rss_headlines(name, url))
    stock_headlines = {}
    for sym in symbols:
        stock_headlines[sym] = recent_headlines(sym + ".NS")
        time.sleep(0.3)  # be polite to the free endpoint

    prompt = build_prompt(market_headlines, stock_headlines)

    try:
        result = call_gemini(prompt, api_key)
    except Exception as e:
        print("Gemini call failed:", e)
        print("FATAL: exiting non-zero so this shows as a clear failure, not a silent skip.")
        sys.exit(1)

    market = result.get("market", {"sentiment": "neutral", "confidence": 0, "rationale": "parse error"})
    stocks = result.get("stocks", {})

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "stocks": stocks,
    }
    SENTIMENT_PATH.parent.mkdir(exist_ok=True)
    json.dump(output, open(SENTIMENT_PATH, "w"), indent=2)
    print(f"wrote sentiment.json - market={market.get('sentiment')} conf={market.get('confidence')}, {len(stocks)} stocks scored")


if __name__ == "__main__":
    main()
