#!/usr/bin/env python3
"""PreToolUse: only allow writes to state/target_weights.json inside the
13:00 -> 15:25 Europe/Madrid window (07:00 -> 09:25 ET, ~2h30 before to 5min
before US open).

This is the canonical "I'm about to trade" artifact in the new two-session
model: it's written by `pre-open`'s `build_diff_plan.py` and consumed by
`execute_plan.py`. Gating its write window prevents drift — the post-close
session can't accidentally produce a plan, and a pre-open run that misses the
window can't queue MOO orders against an unapproved plan.

History: this hook used to gate `morning_decisions.json` (Phase 2 design,
defensive validator only). After the post-close/pre-open redistribution the
decisions file was retired; the same time-window enforcement now applies to
the new authoritative artifact.
"""
import datetime
import json
import re
import sys
from zoneinfo import ZoneInfo

WINDOW_TZ = ZoneInfo("Europe/Madrid")
WINDOW_START = (13, 0)
WINDOW_END = (15, 25)
TARGET_FILENAME = "target_weights.json"


def fail(msg: str) -> None:
    print(f"morning_window BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = event.get("tool_name")
    if tool not in ("Write", "Edit"):
        sys.exit(0)
    path = (event.get("tool_input") or {}).get("file_path", "")
    if TARGET_FILENAME not in path:
        sys.exit(0)

    now = datetime.datetime.now(WINDOW_TZ)
    if now.weekday() >= 5:
        fail(f"weekend ({now:%a}) — pre-open does not run on non-trading days")
    start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = now.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    if not (start <= now <= end):
        fail(
            f"current Madrid time {now:%H:%M %a} outside window "
            f"{WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}-{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d}"
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
