#!/usr/bin/env python3
"""PreToolUse: validate order format, universe membership, position count, 5% notional cap, session turnover."""
import csv
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SP100_CSV = PROJECT / "data" / "universe" / "sp100.csv"
TURNOVER_FILE = PROJECT / "state" / "session_turnover.json"
CREDS = Path.home() / ".alpaca" / "credentials"

MAX_POSITIONS = 10
MAX_POSITION_PCT = 0.05
MAX_SESSION_TURNOVER = 30
ALLOWED_TYPES = {"market", "limit"}
ALLOWED_SIDES = {"buy", "sell"}
ALLOWED_TIF = {"day", "gtc", "opg", "cls", "ioc", "fok"}


def fail(msg):
    print(f"pre_order_check BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


def load_creds():
    if not CREDS.exists():
        fail("credentials file missing")
    out = {}
    for line in CREDS.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def alpaca_get(url, creds):
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": creds["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": creds["ALPACA_SECRET_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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
        fail("could not parse order JSON body")
    try:
        body = json.loads(m.group(1))
    except Exception as e:
        fail(f"invalid JSON body: {e}")

    symbol = str(body.get("symbol", "")).upper()
    side = str(body.get("side", "")).lower()
    otype = str(body.get("type", "")).lower()
    tif = str(body.get("time_in_force", "")).lower()
    try:
        qty = float(body.get("qty", 0))
    except Exception:
        fail(f"bad qty: {body.get('qty')}")
    limit_price = body.get("limit_price")

    if not SP100_CSV.exists():
        fail(f"sp100.csv missing at {SP100_CSV}")
    with SP100_CSV.open() as f:
        universe = {row["ticker"].upper() for row in csv.DictReader(f)}
    if symbol not in universe:
        fail(f"{symbol} not in S&P 100 universe")

    if side not in ALLOWED_SIDES:
        fail(f"side '{side}' not in {sorted(ALLOWED_SIDES)}")
    if otype not in ALLOWED_TYPES:
        fail(f"type '{otype}' not in {sorted(ALLOWED_TYPES)}")
    if tif and tif not in ALLOWED_TIF:
        fail(f"time_in_force '{tif}' not allowed")
    if qty <= 0:
        fail(f"qty must be > 0, got {qty}")

    creds = load_creds()

    try:
        positions = alpaca_get(creds["ALPACA_BASE_URL"] + "/v2/positions", creds)
    except Exception as e:
        fail(f"positions fetch failed: {e}")
    current_symbols = {p["symbol"].upper() for p in positions}
    if side == "buy" and symbol not in current_symbols and len(positions) >= MAX_POSITIONS:
        fail(f"would exceed max {MAX_POSITIONS} positions (holding {len(positions)})")

    try:
        account = alpaca_get(creds["ALPACA_BASE_URL"] + "/v2/account", creds)
    except Exception as e:
        fail(f"account fetch failed: {e}")
    equity = float(account["equity"])

    if otype == "limit":
        if not limit_price:
            fail("limit order missing limit_price")
        ref_price = float(limit_price)
    else:
        try:
            trade = alpaca_get(
                creds["ALPACA_DATA_URL"] + f"/v2/stocks/{symbol}/trades/latest",
                creds,
            )
            ref_price = float(trade["trade"]["p"])
        except Exception as e:
            fail(f"market-price fetch failed for notional check: {e}")

    notional = qty * ref_price
    if side == "buy" and notional > MAX_POSITION_PCT * equity:
        fail(
            f"notional ${notional:,.0f} > 5% equity "
            f"(${MAX_POSITION_PCT * equity:,.0f}) for {symbol}"
        )

    TURNOVER_FILE.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    state = {}
    if TURNOVER_FILE.exists():
        try:
            state = json.loads(TURNOVER_FILE.read_text())
        except Exception:
            pass
    if state.get("date") != today:
        state = {"date": today, "count": 0}
    if state["count"] >= MAX_SESSION_TURNOVER:
        fail(f"session turnover limit {MAX_SESSION_TURNOVER} reached today")
    state["count"] += 1
    TURNOVER_FILE.write_text(json.dumps(state))

    print(
        f"pre_order_check OK: {side} {qty:g} {symbol} @ ~{ref_price:.2f} "
        f"(notional ${notional:,.0f}, turnover {state['count']}/{MAX_SESSION_TURNOVER})",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
