#!/usr/bin/env python3
"""PreToolUse: block order placement if intraday DD < -2% vs prior close equity."""
import json
import re
import sys
import urllib.request
from pathlib import Path

CREDS = Path.home() / ".alpaca" / "credentials"
MAX_DD = 0.02


def fail(msg):
    print(f"drawdown_guard BLOCKED: {msg}", file=sys.stderr)
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

    if not CREDS.exists():
        fail("credentials file missing")
    creds = {}
    for line in CREDS.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()

    try:
        req = urllib.request.Request(
            creds["ALPACA_BASE_URL"] + "/v2/account",
            headers={
                "APCA-API-KEY-ID": creds["ALPACA_API_KEY"],
                "APCA-API-SECRET-KEY": creds["ALPACA_SECRET_KEY"],
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            account = json.loads(r.read())
    except Exception as e:
        fail(f"could not fetch account: {e}")

    equity = float(account["equity"])
    last_equity = float(account["last_equity"])
    if last_equity <= 0:
        sys.exit(0)
    dd = (equity - last_equity) / last_equity
    if dd < -MAX_DD:
        fail(
            f"intraday DD {dd * 100:+.2f}% breaches -{MAX_DD * 100:.0f}% hard stop "
            f"(equity ${equity:,.2f} vs last ${last_equity:,.2f})"
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
