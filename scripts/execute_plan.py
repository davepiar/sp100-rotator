"""Execute today's approved trade plan: place bracket buy orders + simple sell limits.

Preconditions:
  - state/target_weights.json must have signed_off=True
  - data/snapshots/{date}/trade_plan.json must exist
Appends every request/response to state/trade_log.jsonl (one record per order).

Each BUY is placed as a bracket order:
  * Parent: limit BUY at trade_plan.limit_price (TIF=gtc)
  * Stop-loss child: stop at limit_price * (1 - STOP_LOSS_PCT)
  * Take-profit child: limit at limit_price * (1 + TAKE_PROFIT_PCT) — wide so it rarely fires
This guarantees every long position has a stop attached, addressing the DE-style
"floating loser with no plan" pattern from the 2026-04-26 post-mortem.

No-chase guard (added 2026-04-26): refuse to place a new BUY for any symbol that
already has an open order (any side) at Alpaca. Forces operator to cancel/replace
rather than silently pile on at a worse price (DOW +4%, AVGO +4.6% chases last week).

Target-weights filter (added 2026-04-30): BUY orders are cross-referenced against
state/target_weights.json.positions. Any BUY ticker NOT present there is treated as
"dropped by operator/kill-switch" and skipped. Closes the 2026-04-29 gap where the
macro `defer_marginal_buys` kill-switch edited target_weights but the executor still
placed the dropped BUYs from trade_plan.json. SELLs are NOT filtered — kill-switch
runbook is explicit that only marginal BUYs are deferred, never SELLs.
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

STOP_LOSS_PCT   = 0.08    # bracket stop at entry * (1 - 0.08)
TAKE_PROFIT_PCT = 0.25    # bracket TP at entry * (1 + 0.25); wide on purpose


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


def fetch_open_orders(base: str) -> dict[str, list[dict]]:
    """Return {symbol: [open orders...]} from Alpaca. Used for the no-chase guard."""
    r = requests.get(
        f"{base}/v2/orders",
        headers={k: v for k, v in headers().items() if k != "Content-Type"},
        params={"status": "open", "limit": 200},
        timeout=30,
    )
    r.raise_for_status()
    out: dict[str, list[dict]] = {}
    for o in r.json():
        out.setdefault(o["symbol"].upper(), []).append(o)
    return out


def build_buy_payload(o: dict) -> dict:
    """Bracket buy: limit parent + stop-loss child + take-profit child."""
    limit = float(o["limit_price"])
    stop_px = round(limit * (1 - STOP_LOSS_PCT), 2)
    tp_px   = round(limit * (1 + TAKE_PROFIT_PCT), 2)
    return {
        "symbol": o["ticker"],
        "qty": str(o["qty"]),
        "side": "buy",
        "type": "limit",
        "time_in_force": "gtc",
        "limit_price": str(limit),
        "order_class": "bracket",
        "stop_loss":   {"stop_price":  str(stop_px)},
        "take_profit": {"limit_price": str(tp_px)},
    }


def main() -> int:
    tw = json.loads((STATE / "target_weights.json").read_text())
    if not tw.get("signed_off"):
        print("ERROR: target_weights.json not signed off. Refusing to execute.", file=sys.stderr)
        return 2

    plan = json.loads((SNAP / "trade_plan.json").read_text())
    buy_orders = [o for o in plan["orders"] if o["action"].startswith("BUY")]
    sell_orders = [o for o in plan["orders"] if o["action"].startswith("SELL")]

    # Target-weights filter: BUYs not present in target_weights.positions were dropped
    # by operator edit or macro kill-switch. SELLs are NOT filtered.
    target_positions = {t.upper() for t in tw.get("positions", {})}
    tw_filtered: list[dict] = []
    safe_after_tw: list[dict] = []
    for o in buy_orders:
        sym = o["ticker"].upper()
        if sym not in target_positions:
            tw_filtered.append({
                "ticker": sym,
                "reason": "absent from target_weights.positions (kill-switch or operator drop)",
                "proposed_limit": o["limit_price"],
                "qty": o["qty"],
            })
            log({"action": "target_weights_filter_drop", "ticker": sym,
                 "proposed_limit": o["limit_price"], "qty": o["qty"]})
            continue
        safe_after_tw.append(o)
    if tw_filtered:
        print(f"  target-weights filter dropped {len(tw_filtered)} BUY(s):")
        for d in tw_filtered:
            print(f"    {d['ticker']:5s} qty={d['qty']:>3d} limit={d['proposed_limit']:.2f}  "
                  f"({d['reason']})")
    buy_orders = safe_after_tw

    print(f"Placing {len(buy_orders)} BUY + {len(sell_orders)} SELL orders")

    base = os.environ["ALPACA_BASE_URL"]
    results = []

    # No-chase guard: any symbol with a currently open order is off-limits for new BUYs.
    open_orders = fetch_open_orders(base)
    chase_blocked: list[dict] = []
    safe_buy_orders: list[dict] = []
    for o in buy_orders:
        sym = o["ticker"].upper()
        if sym in open_orders:
            existing = open_orders[sym][0]
            chase_blocked.append({
                "ticker": sym,
                "proposed_limit": o["limit_price"],
                "existing_order_id": existing.get("id"),
                "existing_side": existing.get("side"),
                "existing_limit": existing.get("limit_price"),
                "existing_status": existing.get("status"),
            })
            log({"action": "no_chase_block", "ticker": sym, "proposed_limit": o["limit_price"],
                 "existing": existing})
            continue
        safe_buy_orders.append(o)
    if chase_blocked:
        print(f"  no-chase guard blocked {len(chase_blocked)} BUY(s):")
        for c in chase_blocked:
            print(f"    {c['ticker']:5s} proposed {c['proposed_limit']} — existing "
                  f"{c['existing_side']} {c['existing_limit']} ({c['existing_status']}, id {c['existing_order_id'][:8] if c['existing_order_id'] else '?'})")
    buy_orders = safe_buy_orders

    for o in sell_orders + buy_orders:
        side = "sell" if o["action"].startswith("SELL") else "buy"
        if side == "buy":
            payload = build_buy_payload(o)
        else:
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
        "no_chase_blocked": chase_blocked,
        "target_weights_filtered": tw_filtered,
        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
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
