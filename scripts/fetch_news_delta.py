"""Fetch overnight + early-morning news for tickers we care about right now.

Runs in `pre-open` Phase 2.5 between premarket_check and screen_candidates.

Inputs (read-only):
  state/news_summary.json     — produced by post-close; we extend it
  state/evening_research.json — draft_tickers list
  state/portfolio.json        — current holdings (so news on what we hold counts too)
  state/premarket_data.json   — we mutate this to add news_flags

Outputs:
  state/news_summary.json (rewritten with merged top_events + bumped generated_at)
  state/premarket_data.json (extends `flags` array with `kind: "news_headline"` entries)

Source: FMP /stable/news/stock — deterministic ticker-tagged news with publish times.
We do NOT use WebSearch from a script — too brittle, hard to budget. Conversational
news synthesis stays in post-close (full 10-day, broader sources).

Behavior on errors: warn-and-continue. Pre-open should never hard-fail because of
a news fetch problem.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

PROJECT = Path(__file__).resolve().parents[1]
NEWS_PATH = PROJECT / "state" / "news_summary.json"
PREMARKET_PATH = PROJECT / "state" / "premarket_data.json"
RESEARCH_PATH = PROJECT / "state" / "evening_research.json"
PORTFOLIO_PATH = PROJECT / "state" / "portfolio.json"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# Per-ticker call cap (FMP free tier 250/day; pre-open's other consumers use ~50)
PER_TICKER_LIMIT = 8
# Hard wall-clock budget — emit warning if the whole run exceeds this
WALL_BUDGET_SEC = 90

# Impact + direction heuristics. Conservative: when in doubt, "medium" + "neutral".
# These run on the headline (title) only, lower-cased.
HIGH_IMPACT_BULL = (
    "beat", "tops", "raises guidance", "upgrade", "fda approval", "approval ",
    "buyback", "dividend hike", "record high", "all-time high", "surges",
    "soars", "rally", "breakout", "wins contract", "secures",
)
HIGH_IMPACT_BEAR = (
    "miss", "downgrade", "cuts guidance", "guides lower", "warning",
    "lawsuit", "investigation", "subpoena", "fraud", "recall",
    "halts", "halted", "delisting", "going concern", "bankruptcy",
    "plunges", "crashes", "tumbles",
)
MEDIUM_BULL = ("upgrade", "raises", "beats", "outperform", "buy rating", "partnership")
MEDIUM_BEAR = ("downgrade", "underperform", "sell rating", "concerns", "pressure")


def classify(title: str) -> tuple[str, str]:
    """Return (impact, direction) from title heuristics. Defaults to ('medium','neutral')."""
    t = title.lower()
    if any(k in t for k in HIGH_IMPACT_BULL):
        return "high", "risk-on"
    if any(k in t for k in HIGH_IMPACT_BEAR):
        return "high", "risk-off-margin"
    if any(k in t for k in MEDIUM_BULL):
        return "medium", "risk-on"
    if any(k in t for k in MEDIUM_BEAR):
        return "medium", "risk-off-margin"
    return "medium", "neutral"


def fmp_stock_news(api_key: str, symbol: str, since_utc: datetime) -> list[dict]:
    """Return news items for a symbol, filtered to publish time > since_utc.

    FMP `publishedDate` is a string "YYYY-MM-DD HH:MM:SS" interpreted as UTC.
    """
    url = "https://financialmodelingprep.com/stable/news/stock"
    params = {"symbols": symbol, "limit": PER_TICKER_LIMIT, "apikey": api_key}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            print(f"  WARN {symbol}: HTTP {r.status_code} — {r.text[:120]}", file=sys.stderr)
            return []
        rows = r.json()
        if not isinstance(rows, list):
            return []
    except requests.RequestException as e:
        print(f"  WARN {symbol}: {e}", file=sys.stderr)
        return []

    fresh: list[dict] = []
    for row in rows:
        pd_str = row.get("publishedDate") or ""
        try:
            pub = datetime.strptime(pd_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if pub <= since_utc:
            continue
        fresh.append({
            "symbol": symbol,
            "published_at": pub.isoformat(),
            "title": row.get("title") or "",
            "site": row.get("site") or "",
            "url": row.get("url") or "",
        })
    return fresh


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        print("ERROR: FMP_API_KEY not set — pre-open continues without news delta", file=sys.stderr)
        return 0  # advisory degrade — exit clean so runbook continues

    summary = load_json(NEWS_PATH)
    if not summary:
        print("WARN: state/news_summary.json missing or unreadable — pre-open continues without news delta",
              file=sys.stderr)
        return 0

    research = load_json(RESEARCH_PATH)
    portfolio = load_json(PORTFOLIO_PATH)
    premarket = load_json(PREMARKET_PATH)

    draft_tickers = research.get("draft_tickers", [])
    held_tickers = [p["symbol"] for p in portfolio.get("positions", [])]
    universe = sorted(set(draft_tickers) | set(held_tickers))
    if not universe:
        print("WARN: no draft tickers or holdings — nothing to fetch", file=sys.stderr)
        return 0

    try:
        since_utc = datetime.fromisoformat(summary["generated_at"])
        if since_utc.tzinfo is None:
            since_utc = since_utc.replace(tzinfo=UTC)
    except (KeyError, ValueError):
        since_utc = datetime.now(UTC) - timedelta(hours=12)

    started = datetime.now(UTC)
    print(f"News delta: since={since_utc.isoformat()}  tickers={len(universe)} ({','.join(universe)})")

    new_items: list[dict] = []
    for ticker in universe:
        items = fmp_stock_news(api_key, ticker, since_utc)
        if items:
            print(f"  {ticker}: {len(items)} fresh items since post-close")
        new_items.extend(items)

    elapsed = (datetime.now(UTC) - started).total_seconds()
    if elapsed > WALL_BUDGET_SEC:
        print(f"WARN: news delta took {elapsed:.0f}s (budget {WALL_BUDGET_SEC}s)", file=sys.stderr)

    if not new_items:
        print("No fresh news items for any tracked ticker.")
        # Still bump generated_at so pre-open knows news was checked
        summary["generated_at"] = datetime.now(UTC).isoformat()
        summary["delta_run_at"] = datetime.now(UTC).isoformat()
        summary["delta_items_added"] = 0
        NEWS_PATH.write_text(json.dumps(summary, indent=2))
        return 0

    # Build top_events entries from new items, one per item
    new_events: list[dict] = []
    for item in new_items:
        impact, direction = classify(item["title"])
        new_events.append({
            "date": item["published_at"][:10],
            "headline": f"{item['symbol']}: {item['title']}",
            "impact": impact,
            "direction": direction,
            "affected_tickers": [item["symbol"]],
            "summary": f"Source: {item['site']} ({item['published_at']}).",
            "source_urls": [item["url"]] if item["url"] else [],
            "source": "fmp-stable-news-delta",
        })

    # Merge into existing top_events (append; don't dedupe — post-close events are
    # broader/macro and shouldn't be confused with these per-ticker drips).
    summary.setdefault("top_events", []).extend(new_events)
    summary["generated_at"] = datetime.now(UTC).isoformat()
    summary["delta_run_at"] = datetime.now(UTC).isoformat()
    summary["delta_items_added"] = len(new_events)
    # Bump valid_until to next-open ET (~13:30 UTC tomorrow)
    today_et = datetime.now(ET).date()
    next_open = datetime.combine(today_et + timedelta(days=1), datetime.min.time(), tzinfo=ET).replace(hour=9, minute=30)
    summary["valid_until"] = next_open.astimezone(UTC).isoformat()

    NEWS_PATH.write_text(json.dumps(summary, indent=2))
    print(f"Wrote {NEWS_PATH} (+{len(new_events)} delta events)")

    # Emit news_flags into premarket_data.json — same shape as gap_flags
    if not premarket:
        print(f"WARN: {PREMARKET_PATH} missing — skipping news_flags emit", file=sys.stderr)
        return 0

    premarket.setdefault("flags", [])
    for item, ev in zip(new_items, new_events):
        flag = {
            "kind": "news_headline",
            "symbol": item["symbol"],
            "headline": item["title"],
            "impact": ev["impact"],
            "direction": ev["direction"],
            "published_at": item["published_at"],
            "site": item["site"],
            "url": item["url"],
        }
        premarket["flags"].append(flag)
    PREMARKET_PATH.write_text(json.dumps(premarket, indent=2))
    print(f"Wrote {PREMARKET_PATH} (+{len(new_items)} news_flags)")
    print(f"Wall time: {elapsed:.1f}s")

    # Brief breakdown for the runbook
    by_dir: dict[str, int] = {}
    for ev in new_events:
        by_dir[ev["direction"]] = by_dir.get(ev["direction"], 0) + 1
    print(f"Direction breakdown: {by_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
