"""Build diff-aware plan: compare target weights vs current positions, emit BUY/SELL/HOLD.

Reads candidates.json produced by screen_candidates.py. Computes target weight per
position-sizer rule (5% × conv × (1-vol_20d)), then diffs against current Alpaca
positions. A drift threshold of 0.5% of equity suppresses tiny rebalances.

Writes state/target_weights.json and data/snapshots/{date}/trade_plan.json.
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

BASE_POSITION_CAP = 0.05
OFFSET_BPS = 5 / 10000
DRIFT_THRESHOLD = 0.005  # 0.5% of equity


def alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }


def fetch_account() -> dict:
    r = requests.get(f"{os.environ['ALPACA_BASE_URL']}/v2/account",
                     headers=alpaca_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_positions() -> dict[str, dict]:
    r = requests.get(f"{os.environ['ALPACA_BASE_URL']}/v2/positions",
                     headers=alpaca_headers(), timeout=30)
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}


def fetch_latest_close(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    r = requests.get(f"{data_url}/v2/stocks/bars/latest",
                     headers=alpaca_headers(),
                     params={"symbols": ",".join(symbols), "feed": "iex"},
                     timeout=30)
    r.raise_for_status()
    return r.json().get("bars", {})


def main() -> int:
    cands = json.loads((SNAP / "candidates.json").read_text())
    picks = cands["final"]
    if not picks:
        print("No candidates.", file=sys.stderr)
        return 1

    account = fetch_account()
    equity = float(account["equity"])
    positions = fetch_positions()
    tickers = list({p["ticker"] for p in picks} | set(positions.keys()))
    latest = fetch_latest_close(tickers)

    # Compute target weights per sizer rule
    targets: dict[str, dict] = {}
    for p in picks:
        t = p["ticker"]
        raw = BASE_POSITION_CAP * p["conviction"] * (1.0 - min(0.5, p["vol_20d"]))
        target_weight = min(BASE_POSITION_CAP, raw)
        targets[t] = {
            "sector": p["analyst_sector"],
            "conviction": p["conviction"],
            "rs_3m": p["rs_3m"],
            "trend": p["trend"],
            "accum": p["accum"],
            "vol_20d": p["vol_20d"],
            "target_weight": target_weight,
        }

    orders = []
    sector_weights: dict[str, float] = {}

    # Pass 1: held positions
    for t, pos in positions.items():
        cur_mv = float(pos["market_value"])
        cur_weight = cur_mv / equity
        target = targets.get(t)
        bar = latest.get(t)
        last_close = float(bar["c"]) if bar else float(pos["current_price"])

        if target is None:
            # Not in top list → SELL
            qty = int(pos["qty"])
            limit_px = round(last_close * (1.0 - OFFSET_BPS), 2)
            orders.append({
                "ticker": t, "sector": "?", "action": "SELL", "qty": qty,
                "limit_price": limit_px, "last_close": last_close,
                "current_weight_pct": round(cur_weight * 100, 3),
                "target_weight_pct": 0.0,
                "notional_usd": round(qty * limit_px, 2),
                "conviction": 0.0,
                "thesis_tags": ["not in top-10 candidate list"],
            })
            continue

        tgt = target["target_weight"]
        drift = tgt - cur_weight
        if abs(drift) < DRIFT_THRESHOLD:
            orders.append({
                "ticker": t, "sector": target["sector"], "action": "HOLD",
                "qty": 0, "limit_price": last_close, "last_close": last_close,
                "current_weight_pct": round(cur_weight * 100, 3),
                "target_weight_pct": round(tgt * 100, 3),
                "drift_pct": round(drift * 100, 3),
                "notional_usd": 0.0,
                "conviction": round(target["conviction"], 3),
                "rs_3m": round(target["rs_3m"], 3),
                "vol_20d": round(target["vol_20d"], 4),
                "thesis_tags": [
                    f"top-10 {target['sector']}",
                    f"3M RS +{target['rs_3m']*100:.1f}%",
                    f"drift {drift*100:+.2f}% < threshold → HOLD",
                ],
            })
            sector_weights[target["sector"]] = sector_weights.get(target["sector"], 0.0) + cur_weight
        elif drift > 0:
            # BUY top-up
            dollar_add = equity * drift
            limit_px = round(last_close * (1.0 + OFFSET_BPS), 2)
            qty = int(dollar_add // limit_px)
            if qty <= 0:
                continue
            notional = qty * limit_px
            final_weight = (cur_mv + notional) / equity
            orders.append({
                "ticker": t, "sector": target["sector"], "action": "BUY (top-up)",
                "qty": qty, "limit_price": limit_px, "last_close": last_close,
                "current_weight_pct": round(cur_weight * 100, 3),
                "target_weight_pct": round(final_weight * 100, 3),
                "notional_usd": round(notional, 2),
                "conviction": round(target["conviction"], 3),
                "rs_3m": round(target["rs_3m"], 3),
                "vol_20d": round(target["vol_20d"], 4),
                "thesis_tags": [
                    f"top-10 {target['sector']}",
                    f"3M RS +{target['rs_3m']*100:.1f}%",
                    f"size up {drift*100:+.2f}%",
                ],
            })
            sector_weights[target["sector"]] = sector_weights.get(target["sector"], 0.0) + final_weight
        else:
            # SELL trim
            dollar_trim = equity * (-drift)
            limit_px = round(last_close * (1.0 - OFFSET_BPS), 2)
            qty = int(dollar_trim // limit_px)
            if qty <= 0:
                continue
            orders.append({
                "ticker": t, "sector": target["sector"], "action": "SELL (trim)",
                "qty": qty, "limit_price": limit_px, "last_close": last_close,
                "current_weight_pct": round(cur_weight * 100, 3),
                "target_weight_pct": round((cur_mv - qty * limit_px) / equity * 100, 3),
                "notional_usd": round(qty * limit_px, 2),
                "conviction": round(target["conviction"], 3),
                "thesis_tags": [
                    f"trim to target {tgt*100:.2f}%",
                ],
            })
            sector_weights[target["sector"]] = sector_weights.get(target["sector"], 0.0) + (cur_mv - qty * limit_px) / equity

    # Pass 2: new entries (in target, not in positions)
    for t, target in targets.items():
        if t in positions:
            continue
        bar = latest.get(t)
        if not bar:
            continue
        last_close = float(bar["c"])
        dollar_target = equity * target["target_weight"]
        limit_px = round(last_close * (1.0 + OFFSET_BPS), 2)
        qty = int(dollar_target // limit_px)
        if qty <= 0:
            continue
        notional = qty * limit_px
        actual_weight = notional / equity
        orders.append({
            "ticker": t, "sector": target["sector"], "action": "BUY (new)",
            "qty": qty, "limit_price": limit_px, "last_close": last_close,
            "current_weight_pct": 0.0,
            "target_weight_pct": round(actual_weight * 100, 3),
            "notional_usd": round(notional, 2),
            "conviction": round(target["conviction"], 3),
            "rs_3m": round(target["rs_3m"], 3),
            "vol_20d": round(target["vol_20d"], 4),
            "thesis_tags": [
                f"new entry in {target['sector']}",
                f"3M RS +{target['rs_3m']*100:.1f}%",
                f"trend score {target['trend']:.2f} / accum {target['accum']:.2f}",
            ],
        })
        sector_weights[target["sector"]] = sector_weights.get(target["sector"], 0.0) + actual_weight

    # Sort orders: SELL first, then BUY (new), then BUY (top-up), then HOLD
    order_rank = {"SELL": 0, "SELL (trim)": 1, "BUY (new)": 2, "BUY (top-up)": 3, "HOLD": 4}
    orders.sort(key=lambda o: (order_rank.get(o["action"], 99), -o.get("conviction", 0)))

    total_deployed = sum(
        sector_weights.values()
    )
    new_deploy_notional = sum(
        o["notional_usd"] for o in orders if o["action"].startswith("BUY")
    )
    net_sells_notional = sum(
        o["notional_usd"] for o in orders if o["action"].startswith("SELL")
    )

    trade_plan = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "session_date": TODAY,
        "equity": equity,
        "current_deployed_pct": round(sum(float(p["market_value"]) / equity for p in positions.values()) * 100, 2),
        "post_trade_deployed_pct": round(total_deployed * 100, 2),
        "cash_after_pct": round((1 - total_deployed) * 100, 2),
        "new_buy_notional_usd": round(new_deploy_notional, 2),
        "sell_notional_usd": round(net_sells_notional, 2),
        "sector_weights_pct": {k: round(v * 100, 2) for k, v in sector_weights.items()},
        "orders": orders,
        "order_type_plan": "limit orders, mid ± 5bp, TIF per execute phase (gtc after close, opg post-19:00 ET, day during RTH)",
    }
    (SNAP / "trade_plan.json").write_text(json.dumps(trade_plan, indent=2))

    # Emit target_weights.json for execute phase
    target_weights_file = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "session_date": TODAY,
        "equity_at_plan": equity,
        "total_deployed_pct": round(total_deployed * 100, 2),
        "cash_after_pct": round((1 - total_deployed) * 100, 2),
        "sector_concentration_pct": {k: round(v * 100, 2) for k, v in sector_weights.items()},
        "positions": {
            o["ticker"]: o["target_weight_pct"]
            for o in orders
            if o["action"] != "SELL"
        },
        "signed_off": False,
    }
    (STATE / "target_weights.json").write_text(json.dumps(target_weights_file, indent=2))

    # Print
    print(f"Equity: ${equity:,.2f}")
    print(f"Current deployed: {trade_plan['current_deployed_pct']:.2f}%  "
          f"→ Post-trade: {trade_plan['post_trade_deployed_pct']:.2f}%  "
          f"Cash after: {trade_plan['cash_after_pct']:.2f}%")
    print(f"New buys: ${new_deploy_notional:,.2f}  |  Sells: ${net_sells_notional:,.2f}")
    print("Sector weights:", {k: f"{v*100:.1f}%" for k, v in sector_weights.items()})
    print()
    print(f"{'ACTION':<14}{'TICKER':<7}{'SECTOR':<18}{'QTY':>6}{'LIM $':>10}{'NOTIONAL':>12}{'CUR %':>8}{'TGT %':>8}{'CONV':>7}")
    for o in orders:
        print(f"{o['action']:<14}{o['ticker']:<7}{o['sector'][:17]:<18}"
              f"{o['qty']:>6}{o['limit_price']:>10.2f}"
              f"{o['notional_usd']:>12,.2f}"
              f"{o['current_weight_pct']:>8.2f}{o['target_weight_pct']:>8.2f}"
              f"{o.get('conviction', 0):>7.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
