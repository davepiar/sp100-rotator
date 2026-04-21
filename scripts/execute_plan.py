"""Execute today's approved trade plan: place limit GTC orders per trade_plan.json.

Preconditions:
  - state/target_weights.json must have signed_off=True
  - data/snapshots/{date}/trade_plan.json must exist
Appends every request/response to state/trade_log.jsonl (one record per order).
Refuses to execute SELL in v1 (CLAUDE.md: no shorts — SELL only if we already hold).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parents[1]
TODAY = os.environ.get("SESSION_DATE", date.today().isoformat())
SNAP = PROJECT / "data" / "snapshots" / TODAY
STATE = PROJECT / "state"
TRADE_LOG = STATE / "trade_log.jsonl"


def headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        "Content-Type": "application/json",
    }


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open(TRADE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def main() -> int:
    tw = json.loads((STATE / "target_weights.json").read_text())
    if not tw.get("signed_off"):
        print("ERROR: target_weights.json not signed off. Refusing to execute.", file=sys.stderr)
        return 2

    plan = json.loads((SNAP / "trade_plan.json").read_text())
    buy_orders = [o for o in plan["orders"] if o["action"].startswith("BUY")]
    sell_orders = [o for o in plan["orders"] if o["action"].startswith("SELL")]
    print(f"Placing {len(buy_orders)} BUY + {len(sell_orders)} SELL orders")

    base = os.environ["ALPACA_BASE_URL"]
    results = []

    for o in sell_orders + buy_orders:
        side = "sell" if o["action"].startswith("SELL") else "buy"
        payload = {
            "symbol": o["ticker"],
            "qty": str(o["qty"]),
            "side": side,
            "type": "limit",
            "time_in_force": "gtc",
            "limit_price": str(o["limit_price"]),
        }
        log({"action": "submit_request", "payload": payload, "source_plan": o})
        try:
            r = requests.post(f"{base}/v2/orders", headers=headers(),
                              json=payload, timeout=30)
            resp_json = r.json() if r.content else {}
            log({"action": "submit_response", "status": r.status_code,
                 "symbol": o["ticker"], "response": resp_json})
            if r.status_code in (200, 201):
                oid = resp_json.get("id", "?")[:8]
                status = resp_json.get("status")
                print(f"  [{r.status_code}] {side.upper():4s} {o['ticker']:5s} "
                      f"qty={o['qty']:3d} @ ${o['limit_price']:.2f}  "
                      f"id={oid}  status={status}")
                results.append({"ticker": o["ticker"], "side": side, "qty": o["qty"],
                                "limit_price": o["limit_price"], "order_id": resp_json.get("id"),
                                "status": status, "ok": True})
            else:
                msg = resp_json.get("message") or r.text[:200]
                print(f"  [{r.status_code}] FAIL {o['ticker']}: {msg}")
                results.append({"ticker": o["ticker"], "side": side, "qty": o["qty"],
                                "limit_price": o["limit_price"], "error": msg, "ok": False})
        except Exception as e:
            log({"action": "submit_exception", "symbol": o["ticker"], "error": str(e)})
            print(f"  EXC {o['ticker']}: {e}")
            results.append({"ticker": o["ticker"], "ok": False, "error": str(e)})

    summary = {
        "session_date": TODAY,
        "submitted": len(results),
        "accepted": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "orders": results,
    }
    (SNAP / "execution_summary.json").write_text(json.dumps(summary, indent=2))
    print()
    print(f"Submitted: {summary['submitted']}  Accepted: {summary['accepted']}  "
          f"Failed: {summary['failed']}")
    print(f"Execution summary: {SNAP / 'execution_summary.json'}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
