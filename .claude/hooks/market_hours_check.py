#!/usr/bin/env python3
"""PreToolUse: block day/ioc/fok orders outside US regular hours. gtc/opg/cls allowed anytime."""
import datetime
import json
import re
import sys
from zoneinfo import ZoneInfo


def fail(msg):
    print(f"market_hours_check BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if event.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = event.get("tool_input", {}).get("command", "")
    if not re.search(r"-X\s+POST.*?/v2/orders(?!/)", cmd):
        sys.exit(0)

    m = re.search(r"-d\s+'(\{[^']*\})'", cmd) or re.search(r'-d\s+"(\{[^"]*\})"', cmd)
    if not m:
        sys.exit(0)
    try:
        body = json.loads(m.group(1))
    except Exception:
        sys.exit(0)

    tif = str(body.get("time_in_force", "day")).lower()
    if tif in {"gtc", "opg", "cls"}:
        sys.exit(0)

    now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        fail(f"weekend ({now_et:%a}) — use opg/cls instead of {tif}")
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if not (open_t <= now_et <= close_t):
        fail(
            f"US market closed (ET {now_et:%H:%M} {now_et:%a}) — "
            f"use opg/cls for next open instead of {tif}"
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
