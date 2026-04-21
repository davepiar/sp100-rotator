"""Build target_weights.json + trade plan for 2026-04-20 session.

Takes top-N candidates from candidates.json, applies position sizing
(5% cap * conviction * (1 - vol_20d)), computes share counts + mid+5bp
limit prices from latest Alpaca quote, and emits:
  state/target_weights.json     <- today's target allocation (audit trail)
  data/snapshots/2026-04-20/trade_plan.json  <- buy/sell list for report

Market closed, so limit price = last trade close + 5bp (buy offset).
Execution will re-quote mid at open.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parents[1]
SNAP = PROJECT / "data" / "snapshots" / "2026-04-20"
STATE = PROJECT / "state"

FIRST_SESSION_N = 6  # conservative for first live session
BASE_POSITION_CAP = 0.05
OFFSET_BPS = 5 / 10000  # 5bp


def fetch_latest_trade(symbols: list[str]) -> dict[str, dict]:
    """Use Alpaca latest-bar endpoint (market closed -> no fresh trades)."""
    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    r = requests.get(
        f"{data_url}/v2/stocks/bars/latest",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
        params={"symbols": ",".join(symbols), "feed": "iex"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("bars", {})


def fetch_account() -> dict:
    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    base = os.environ["ALPACA_BASE_URL"]
    r = requests.get(
        f"{base}/v2/account",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    cands = json.loads((SNAP / "candidates.json").read_text())
    picks = cands["final"][:FIRST_SESSION_N]
    if not picks:
        print("No candidates.", file=sys.stderr)
        return 1

    tickers = [p["ticker"] for p in picks]
    latest = fetch_latest_trade(tickers)
    account = fetch_account()
    equity = float(account["equity"])

    plan_rows = []
    total_weight = 0.0
    sector_weights: dict[str, float] = {}

    for p in picks:
        t = p["ticker"]
        last = latest.get(t)
        if not last:
            print(f"WARN: no latest bar for {t}", file=sys.stderr)
            continue
        last_close = float(last["c"])
        weight = BASE_POSITION_CAP * p["conviction"] * (1.0 - min(0.5, p["vol_20d"]))
        dollar_target = equity * weight
        limit_px = round(last_close * (1.0 + OFFSET_BPS), 2)
        qty = int(dollar_target // limit_px)
        if qty <= 0:
            continue
        notional = qty * limit_px
        actual_weight = notional / equity

        plan_rows.append({
            "ticker": t,
            "sector": p["analyst_sector"],
            "action": "BUY",
            "qty": qty,
            "limit_price": limit_px,
            "last_close": last_close,
            "target_weight_pct": round(actual_weight * 100, 3),
            "notional_usd": round(notional, 2),
            "conviction": round(p["conviction"], 3),
            "rs_3m": round(p["rs_3m"], 3),
            "trend": p["trend"],
            "accum": round(p["accum"], 3),
            "vol_20d": round(p["vol_20d"], 4),
            "thesis_tags": [
                f"top-4 sector ({p['analyst_sector']})",
                f"3M RS vs SPY +{p['rs_3m']*100:.1f}%",
                "above 50DMA & 200DMA (trend=1.0)" if p["trend"] == 1.0 else "partial trend",
                f"accumulation proxy {p['accum']:.2f}",
            ],
        })
        total_weight += actual_weight
        sector_weights[p["analyst_sector"]] = sector_weights.get(p["analyst_sector"], 0.0) + actual_weight

    target_weights = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "session_date": "2026-04-20",
        "equity_at_plan": equity,
        "total_deployed_pct": round(total_weight * 100, 2),
        "cash_after_pct": round((1 - total_weight) * 100, 2),
        "sector_concentration_pct": {k: round(v * 100, 2) for k, v in sector_weights.items()},
        "positions": {r["ticker"]: r["target_weight_pct"] for r in plan_rows},
        "signed_off": False,  # must be flipped to True by user APPROVE
    }
    (STATE / "target_weights.json").write_text(json.dumps(target_weights, indent=2))

    trade_plan = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "equity": equity,
        "orders": plan_rows,
        "sector_weights_pct": {k: round(v * 100, 2) for k, v in sector_weights.items()},
        "total_deployed_pct": round(total_weight * 100, 2),
        "cash_after_pct": round((1 - total_weight) * 100, 2),
        "order_type_plan": "limit, TIF=day, mid + 5bp offset at execution",
    }
    (SNAP / "trade_plan.json").write_text(json.dumps(trade_plan, indent=2))

    print(f"equity=${equity:,.2f}  total_deployed={total_weight*100:.2f}%  "
          f"cash_after={(1-total_weight)*100:.2f}%")
    for r in plan_rows:
        print(f"  {r['ticker']:5s} {r['sector']:16s} qty={r['qty']:5d} @ "
              f"${r['limit_price']:8.2f}  notional=${r['notional_usd']:9.2f}  "
              f"wt={r['target_weight_pct']:.2f}%  conv={r['conviction']:.2f}")
    print("Sector concentration:")
    for s, w in sector_weights.items():
        print(f"  {s:16s}: {w*100:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
