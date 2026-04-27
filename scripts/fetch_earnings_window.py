"""Fetch upcoming SP100 earnings into state/upcoming_earnings.json for the gate.

Source priority (manual wins):
  1. FMP /stable/earnings-calendar over the next EARNINGS_LOOKAHEAD_DAYS days
     (cap: free tier is sparse — many SP100 names are missing).
  2. data/earnings/manual.json (user-curated; same schema as the FMP rows).

Output schema (state/upcoming_earnings.json):
  {
    "schema_version": "1.0",
    "generated_at":   "2026-04-26T22:00:00Z",
    "valid_until":    "2026-05-03T22:00:00Z",
    "lookahead_days": 30,
    "by_symbol": {
        "DE":   {"date": "2026-05-14", "source": "manual"},
        "AMD":  {"date": "2026-05-05", "source": "fmp"},
        ...
    }
  }

Read by screen_candidates.py — if a ticker is in `by_symbol` and the date is
within EARNINGS_BLOCKER_DAYS, the ticker is blocked from new entries.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parents[1]
UNIVERSE_CSV = PROJECT / "data" / "universe" / "sp100.csv"
MANUAL_PATH  = PROJECT / "data" / "earnings" / "manual.json"
OUT_PATH     = PROJECT / "state" / "upcoming_earnings.json"

EARNINGS_LOOKAHEAD_DAYS = 30  # how far forward to fetch
VALID_FOR_DAYS = 7            # state file freshness


def load_universe() -> set[str]:
    with open(UNIVERSE_CSV) as f:
        return {row["ticker"].upper() for row in csv.DictReader(f)}


def fetch_fmp_window(api_key: str, start: date, end: date) -> list[dict]:
    """Fetch FMP earnings-calendar in 7-day chunks (free tier returns ~25 rows/wk max).

    Returns deduplicated list of {symbol, date, epsEstimated, ...}.
    """
    base = "https://financialmodelingprep.com/stable/earnings-calendar"
    out: dict[tuple[str, str], dict] = {}
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=6), end)
        try:
            r = requests.get(
                base,
                params={"from": cur.isoformat(), "to": chunk_end.isoformat(),
                        "apikey": api_key},
                timeout=15,
            )
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                data = r.json()
                if isinstance(data, list):
                    for row in data:
                        sym = (row.get("symbol") or "").upper()
                        d = row.get("date")
                        if sym and d:
                            out[(sym, d)] = row
        except requests.RequestException as e:
            print(f"  fmp chunk {cur}..{chunk_end} failed: {e}", file=sys.stderr)
        cur = chunk_end + timedelta(days=1)
    return list(out.values())


def load_manual() -> list[dict]:
    if not MANUAL_PATH.exists():
        return []
    try:
        data = json.loads(MANUAL_PATH.read_text())
        return data.get("entries", []) if isinstance(data, dict) else data
    except Exception as e:
        print(f"  manual override unreadable: {e}", file=sys.stderr)
        return []


def main() -> int:
    universe = load_universe()
    today = date.today()
    end = today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)

    fmp_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP")
    fmp_rows: list[dict] = []
    if fmp_key:
        fmp_rows = fetch_fmp_window(fmp_key, today, end)
        print(f"FMP: fetched {len(fmp_rows)} earnings rows over {EARNINGS_LOOKAHEAD_DAYS}d")
    else:
        print("FMP_API_KEY not set; skipping FMP fetch.")

    manual_rows = load_manual()
    print(f"Manual overrides: {len(manual_rows)} entries")

    by_symbol: dict[str, dict] = {}

    # Apply FMP first (lower priority)
    for row in fmp_rows:
        sym = (row.get("symbol") or "").upper()
        if sym not in universe:
            continue
        d = row.get("date")
        if not d:
            continue
        # Keep the earliest upcoming date per symbol
        if sym in by_symbol and by_symbol[sym]["date"] <= d:
            continue
        by_symbol[sym] = {
            "date": d,
            "source": "fmp",
            "eps_est": row.get("epsEstimated"),
            "revenue_est": row.get("revenueEstimated"),
        }

    # Apply manual (higher priority — overrides FMP)
    for row in manual_rows:
        sym = (row.get("symbol") or "").upper()
        if sym not in universe:
            continue
        d = row.get("date")
        if not d:
            continue
        by_symbol[sym] = {
            "date": d,
            "source": "manual",
            "eps_est": row.get("eps_est"),
            "revenue_est": row.get("revenue_est"),
            "note": row.get("note"),
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=VALID_FOR_DAYS)).isoformat(),
        "lookahead_days": EARNINGS_LOOKAHEAD_DAYS,
        "by_symbol": dict(sorted(by_symbol.items())),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {OUT_PATH} with {len(by_symbol)} SP100 names having known upcoming earnings:")
    for sym, info in sorted(by_symbol.items(), key=lambda kv: kv[1]["date"]):
        print(f"  {sym:5s}  {info['date']}  ({info['source']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
