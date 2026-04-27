"""Pre-market data fetcher for the morning-validation session.

Pure-Python data collector — does NOT decide anything. Reads:
  * state/target_weights.json   (last evening's signed-off plan)
  * state/portfolio.json        (last evening's positions snapshot)
  * Alpaca data API             (SPY/QQQ pre-market quotes, VIX, per-symbol gaps)
  * FMP /stable/earnings-calendar (overnight earnings prints since prior close)

Writes state/premarket_data.json. The morning-validation runbook applies
deterministic rules to that file and decides cancel / reduce / no-op.

Designed to run unsupervised at ~14:25 Europe/Madrid (08:25 ET, 1h before open).
Exits 0 on success even if some sources are unavailable — the runbook handles
partial data. Only exits non-zero on hard input errors (no creds, no plan).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

PROJECT = Path(__file__).resolve().parents[1]
STATE = PROJECT / "state"
OUT_PATH = STATE / "premarket_data.json"

PRE_OPEN_GAP_FLAG_PCT = 0.03      # |gap| > 3% → flag
SPY_PRE_OPEN_BIG_DOWN = -0.015    # < -1.5% → flag
VIX_OVERNIGHT_BIG_UP  = 0.15      # +15% → flag


def alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":  os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }


def latest_trade(symbol: str) -> Optional[dict]:
    """Latest trade tick (works pre-/post-market on IEX feed)."""
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    try:
        r = requests.get(
            f"{data_url}/v2/stocks/{symbol}/trades/latest",
            headers=alpaca_headers(),
            params={"feed": "iex"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("trade")
    except requests.RequestException:
        pass
    return None


def prior_day_bar(symbol: str) -> Optional[dict]:
    """Most recent daily bar (yesterday's close)."""
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    try:
        r = requests.get(
            f"{data_url}/v2/stocks/{symbol}/bars/latest",
            headers=alpaca_headers(),
            params={"feed": "iex"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("bar")
    except requests.RequestException:
        pass
    return None


def gap_pct(symbol: str) -> Optional[dict]:
    """Compute pre-market gap % vs prior close. Returns None if either side missing."""
    prior = prior_day_bar(symbol)
    last = latest_trade(symbol)
    if not prior or not last:
        return None
    prev_close = float(prior["c"])
    pre_px = float(last["p"])
    if prev_close <= 0:
        return None
    return {
        "prev_close": prev_close,
        "pre_market_price": pre_px,
        "gap_pct": (pre_px / prev_close) - 1.0,
        "trade_ts": last.get("t"),
    }


def overnight_earnings(fmp_key: Optional[str], session_date: date) -> list[dict]:
    """Earnings reported since prior session close (i.e. between session_date-1 and session_date)."""
    if not fmp_key:
        return []
    base = "https://financialmodelingprep.com/stable/earnings-calendar"
    rows: list[dict] = []
    try:
        r = requests.get(
            base,
            params={"from": (session_date - timedelta(days=1)).isoformat(),
                    "to": session_date.isoformat(),
                    "apikey": fmp_key},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                rows = data
    except requests.RequestException:
        pass
    return rows


def main() -> int:
    if "ALPACA_API_KEY" not in os.environ:
        print("ERROR: alpaca creds not loaded", file=sys.stderr)
        return 2

    # Source of truth for "what to look at" depends on which session is running:
    #   * pre-open (new flow): state/evening_research.json.draft_tickers exists,
    #     state/target_weights.json does NOT exist yet (built later in pre-open).
    #   * legacy / re-runs: state/target_weights.json exists, draft tickers may
    #     not — fall back to it.
    research_path = STATE / "evening_research.json"
    target_path = STATE / "target_weights.json"
    pending_buy_symbols: list[str] = []
    source_label = "none"
    if research_path.exists():
        try:
            research = json.loads(research_path.read_text())
            pending_buy_symbols = sorted(set(research.get("draft_tickers") or []))
            source_label = f"evening_research.json (draft, n={len(pending_buy_symbols)})"
        except Exception as e:
            print(f"WARN: evening_research.json unreadable: {e}", file=sys.stderr)
    if not pending_buy_symbols and target_path.exists():
        try:
            target = json.loads(target_path.read_text())
            pending_buy_symbols = sorted({
                s for s, w in (target.get("positions") or {}).items() if (w or 0) > 0
            })
            source_label = f"target_weights.json (legacy, n={len(pending_buy_symbols)})"
        except Exception as e:
            print(f"WARN: target_weights.json unreadable: {e}", file=sys.stderr)
    if not pending_buy_symbols:
        print("ERROR: no draft tickers to validate — run /post-close first "
              "(produces state/evening_research.json) or place a signed-off "
              "state/target_weights.json", file=sys.stderr)
        return 2
    print(f"  symbols source: {source_label}", file=sys.stderr)

    # Index-level pre-market signals
    indices = {}
    for sym in ("SPY", "QQQ"):
        g = gap_pct(sym)
        indices[sym] = g

    # VIX — Alpaca doesn't carry it directly. Try CBOE proxy via FMP later;
    # for now, surface latest UVXY trade as a *very* rough proxy. The runbook
    # treats this as advisory only.
    vix_proxy = latest_trade("UVXY")  # ETF leveraged-vol proxy

    # Per-symbol pre-market state for any pending BUY
    per_symbol = {}
    for sym in pending_buy_symbols:
        per_symbol[sym] = gap_pct(sym)

    # Overnight earnings
    fmp_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP")
    earnings_rows = overnight_earnings(fmp_key, date.today())
    earnings_by_sym = {row.get("symbol", "").upper(): row for row in earnings_rows}

    # Compute flags (deterministic — runbook still has final say)
    flags: list[dict] = []
    if indices.get("SPY") and indices["SPY"]["gap_pct"] < SPY_PRE_OPEN_BIG_DOWN:
        flags.append({
            "kind": "spy_pre_open_down",
            "value_pct": indices["SPY"]["gap_pct"] * 100,
            "threshold_pct": SPY_PRE_OPEN_BIG_DOWN * 100,
            "advice": "consider lowering exposure ceiling one notch",
        })
    for sym, g in per_symbol.items():
        if not g:
            continue
        if abs(g["gap_pct"]) > PRE_OPEN_GAP_FLAG_PCT:
            earn = earnings_by_sym.get(sym)
            flags.append({
                "kind": "symbol_gap",
                "symbol": sym,
                "gap_pct": g["gap_pct"] * 100,
                "earnings_today": bool(earn),
                "advice": (
                    "cancel BUY (overnight earnings + adverse gap)"
                    if earn and g["gap_pct"] < -PRE_OPEN_GAP_FLAG_PCT
                    else "review BUY before placing"
                ),
            })

    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_date": date.today().isoformat(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "thresholds": {
            "pre_open_gap_flag_pct": PRE_OPEN_GAP_FLAG_PCT,
            "spy_pre_open_big_down": SPY_PRE_OPEN_BIG_DOWN,
            "vix_overnight_big_up": VIX_OVERNIGHT_BIG_UP,
        },
        "indices": indices,
        "vix_proxy": vix_proxy,
        "pending_buy_symbols": pending_buy_symbols,
        "per_symbol": per_symbol,
        "overnight_earnings": [
            {"symbol": k, "date": v.get("date"), "eps_actual": v.get("epsActual"),
             "eps_estimated": v.get("epsEstimated"),
             "revenue_actual": v.get("revenueActual"),
             "revenue_estimated": v.get("revenueEstimated")}
            for k, v in earnings_by_sym.items()
        ],
        "flags": flags,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {OUT_PATH}")
    print(f"  pending BUY symbols: {len(pending_buy_symbols)}  flags raised: {len(flags)}")
    for f in flags:
        if f["kind"] == "spy_pre_open_down":
            print(f"  - SPY {f['value_pct']:+.2f}%: {f['advice']}")
        else:
            print(f"  - {f['symbol']:5s} gap={f['gap_pct']:+.2f}%  earnings={f['earnings_today']}: {f['advice']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
