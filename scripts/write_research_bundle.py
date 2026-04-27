"""Write state/research_bundle.json — handoff bundle from post-close to pre-open.

Reads:
  data/snapshots/<SESSION_DATE>/candidates_draft.json (must exist; produced
                                                       by screen_candidates.py --mode=draft)
  --posture-file <path>  OR  --posture-json '{...}'   (required; from exposure-coach output)

Writes:
  state/research_bundle.json with:
    * Market posture (ceiling, tilt, new-entries-allowed, cash-priority)
    * Draft ticker list (top-N from the draft screen)
    * Signal hashes (sha256 of the draft file — pre-open checks freshness)
    * valid_until = next-open ET (~13:30 UTC, conservatively next-day 13:30 UTC)

The pre-open runbook reads this file at session start. If it's missing or
stale, pre-open aborts and tells the operator to run /post-close first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SESSION_DATE = os.environ.get("SESSION_DATE", date.today().isoformat())
SNAP_DIR = PROJECT / "data" / "snapshots" / SESSION_DATE
OUT_PATH = PROJECT / "state" / "research_bundle.json"

# Required posture keys (validated to surface schema drift early)
POSTURE_KEYS = {"exposure_ceiling_pct", "new_entries_allowed", "cash_priority"}
OPTIONAL_POSTURE_KEYS = {"growth_value_tilt", "rationale", "source"}


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def load_posture(args: argparse.Namespace) -> dict:
    if args.posture_file:
        return json.loads(Path(args.posture_file).read_text())
    if args.posture_json:
        return json.loads(args.posture_json)
    raise SystemExit("ERROR: must pass --posture-file or --posture-json")


def next_open_utc(today: date) -> datetime:
    """Roughly the next US RTH open in UTC (13:30 UTC = 09:30 ET during DST,
    14:30 UTC otherwise). For simplicity, use 13:30 UTC and document the
    half-hour wobble around DST transitions in the runbook."""
    next_day = today + timedelta(days=1)
    # If next_day is Saturday, push to Monday
    while next_day.weekday() >= 5:
        next_day = next_day + timedelta(days=1)
    return datetime.combine(next_day, datetime.min.time(),
                            tzinfo=timezone.utc).replace(hour=13, minute=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--posture-file", help="JSON file with the posture payload")
    parser.add_argument("--posture-json", help="Inline JSON string with the posture payload")
    args = parser.parse_args()

    draft_path = SNAP_DIR / "candidates_draft.json"
    if not draft_path.exists():
        print(f"ERROR: {draft_path} not found — run screen_candidates.py --mode=draft first",
              file=sys.stderr)
        return 2

    posture = load_posture(args)
    missing = POSTURE_KEYS - set(posture.keys())
    if missing:
        print(f"ERROR: posture missing required keys: {sorted(missing)}", file=sys.stderr)
        return 2

    draft = json.loads(draft_path.read_text())
    draft_tickers = [r["ticker"] for r in draft.get("draft", [])]

    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_date": SESSION_DATE,
        "valid_until": next_open_utc(date.fromisoformat(SESSION_DATE)).isoformat(),
        "posture": posture,
        "draft_tickers": draft_tickers,
        "draft_count": len(draft_tickers),
        "signal_hashes": {
            "candidates_draft": "sha256:" + sha256_of_file(draft_path),
        },
        "produced_by": "post-close session",
        "consumed_by": "pre-open session",
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {OUT_PATH}")
    print(f"  draft tickers: {len(draft_tickers)}")
    print(f"  posture: ceiling={posture['exposure_ceiling_pct']}% "
          f"new_entries={posture['new_entries_allowed']} "
          f"cash_priority={posture['cash_priority']}")
    print(f"  valid_until: {payload['valid_until']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
