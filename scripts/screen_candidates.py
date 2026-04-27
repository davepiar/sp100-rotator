"""Screen SP100 candidates for the rotation session.

Two modes (Phase-5 split):
  * --mode=draft  → run by `post-close` (after-market). Applies all hard gates +
                    conviction scoring, but SKIPS correlation prune and sector
                    caps. Outputs top-30 superset to `candidates_draft.json`.
                    The point: front-load expensive analysis post-close, leave
                    the morning to refine against fresh data.
  * --mode=final  → run by `pre-open` (before-market). Same hard gates, then
                    correlation prune + sector + sub-sector caps. Outputs
                    `candidates.json`, which `build_diff_plan.py` consumes.

Hard gates (run in both modes — must pass ALL):
  1. 3M RS vs SPY > 0
  2. 20D RS vs SPY > RS_20D_HARD_FLOOR
  3. price >= SMA50 AND SMA20 >= SMA50 × TREND_BEAR_CROSS_TOLERANCE
  4. NOT in pre-earnings window  (reads state/upcoming_earnings.json)
  5. 3M RS percentile >= RS_3M_PCT_FLOOR  (final mode only — informational by default at floor=0.0)

Conviction score (0-1) blends:
  * 3M relative strength vs SPY  (40%)
  * Accumulation proxy (20/50d vol + close-location + up/down-day vol) (25%)
  * Trend score (above 50DMA + above 200DMA + 50>200) (20%)
  * Sector rank bonus (Basic Materials > Industrials > Healthcare > Tech) (15%)

Caps (final mode only):
  * Per GICS sector:   ≤ 5 names
  * Per sub-sector:    ≤ 4 semis names
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

PROJECT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = PROJECT / "data" / "universe" / "sp100.csv"
_TODAY = os.environ.get("SESSION_DATE", date.today().isoformat())
SECTOR_JSON_DIR = PROJECT / "data" / "snapshots" / _TODAY
OUT_DIR = PROJECT / "data" / "snapshots" / _TODAY

# Map sector-analyst sector names -> sp100.csv sector names
SECTOR_ALIAS = {
    "Technology": "Information Technology",
    "Healthcare": "Health Care",
    "Basic Materials": "Materials",
    "Financial": "Financials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
}

# From sector-analyst output for 2026-04-20 (uptrend ratio, trend, status)
SECTOR_RANK = {
    "Basic Materials": (1, 61.4, "Up", "Overbought"),
    "Industrials":     (2, 52.0, "Up", "Overbought"),
    "Healthcare":      (3, 43.7, "Up", "Overbought"),
    "Technology":      (4, 39.9, "Up", "Overbought"),
    "Financial":       (5, 35.1, "Up", "Normal"),
    "Consumer Cyclical": (6, 33.8, "Up", "Normal"),
}
TOP_SECTORS = ["Basic Materials", "Industrials", "Healthcare", "Technology"]
SECTOR_WEIGHT = {s: (len(TOP_SECTORS) - i) / len(TOP_SECTORS)
                 for i, s in enumerate(TOP_SECTORS)}  # 1.0, 0.75, 0.5, 0.25

# --- Hard-gate parameters (tunable via monthly-recalibration) ---
# Tuned 2026-04-26 against last week's buys (see docs/replay-2026-04-w17.md):
# the goal is to block clear breakdowns (DE-shape) without rejecting names with
# mild weakness that often resolve up (TXN, LIN-shape). Floors loose enough that
# normal pullbacks pass; tight enough that genuine breakdowns fail.
RS_20D_HARD_FLOOR = -0.10                 # block names underperforming SPY by >10pp over 20D
RS_3M_HARD_FLOOR  = 0.0                   # block names underperforming SPY over 3M (existing)
RS_3M_PCT_FLOOR   = 0.0                   # informational only by default — see monthly-recalibration
# Why 0.0: a fixed percentile floor is brittle in small candidate pools and the
# conviction score already weights RS percentile (40%). Raise (e.g. to 0.30) only
# after a proper backtest. The gate code stays wired so the floor is a one-line tune.
TREND_REQUIRE_PRICE_GE_SMA50   = True     # hard: don't buy below the 50DMA
TREND_BEAR_CROSS_TOLERANCE     = 0.99     # allow SMA20 down to 1% below SMA50; tighter = block

# Pre-earnings blocker (Phase 1) — block names whose next earnings date is within
# this many calendar days. Fed by state/upcoming_earnings.json (FMP + manual override).
EARNINGS_BLOCKER_DAYS = 14

# --- Sub-sector taxonomy (override / refine GICS where needed) ---
# Pure-semi names within the SP100 IT bucket. Networking (CSCO), software (MSFT/ORCL/CRM/ADBE),
# and IT services (ACN, IBM, etc.) are excluded.
SEMIS_TICKERS = {"AMD", "AVGO", "INTC", "NVDA", "QCOM", "TXN", "MU", "AMAT",
                 "LRCX", "KLAC", "ADI", "MCHP", "MRVL"}
SUB_SECTOR_NAME_CAPS = {"Semis": 4}  # max names per sub-sector in final list


def load_universe() -> dict[str, str]:
    """Return {ticker: sp100_sector}."""
    out = {}
    with open(UNIVERSE_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            out[row["ticker"]] = row["sector"]
    return out


def candidate_tickers(universe: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return [(ticker, analyst_sector, sp100_sector)] for top-4 sectors."""
    results = []
    sp100_to_analyst = {v: k for k, v in SECTOR_ALIAS.items()}
    for ticker, sp100_sector in universe.items():
        analyst = sp100_to_analyst.get(sp100_sector)
        if analyst in TOP_SECTORS:
            results.append((ticker, analyst, sp100_sector))
    return results


def fetch_alpaca_bars(symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
    """Pull daily bars for a batch of symbols. Returns {symbol: [bars...]}, oldest first."""
    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    url = f"{data_url}/v2/stocks/bars"
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}

    out: dict[str, list[dict]] = {}
    # Chunk symbols to stay well under URL limits (Alpaca accepts comma-separated list).
    chunk_size = 50
    for i in range(0, len(symbols), chunk_size):
        batch = symbols[i:i + chunk_size]
        params = {
            "symbols": ",".join(batch),
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
            "adjustment": "all",
            "feed": "iex",
        }
        next_token = None
        while True:
            if next_token:
                params["page_token"] = next_token
            r = requests.get(url, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            js = r.json()
            for sym, bars in js.get("bars", {}).items():
                out.setdefault(sym, []).extend(bars)
            next_token = js.get("next_page_token")
            if not next_token:
                params.pop("page_token", None)
                break
    for sym in out:
        out[sym].sort(key=lambda b: b["t"])
    return out


def pct_return(bars: list[dict], lookback: int) -> Optional[float]:
    if len(bars) < lookback + 1:
        return None
    start_close = bars[-lookback - 1]["c"]
    end_close = bars[-1]["c"]
    if start_close <= 0:
        return None
    return (end_close / start_close) - 1.0


def accumulation_proxy(bars: list[dict]) -> float:
    """Composite 0-1 accumulation score from price-volume behavior on last 20d."""
    if len(bars) < 50:
        return 0.0
    recent = bars[-20:]
    prior = bars[-50:-20]
    recent_vol = statistics.mean(b["v"] for b in recent)
    prior_vol = statistics.mean(b["v"] for b in prior)
    vol_ratio = recent_vol / prior_vol if prior_vol else 1.0

    # up-day vs down-day volume on last 20d
    up_vol = sum(b["v"] for b in recent if b["c"] >= b["o"])
    down_vol = sum(b["v"] for b in recent if b["c"] < b["o"]) or 1
    ud_ratio = up_vol / (up_vol + down_vol)  # 0-1, 1 = all up-day volume

    # close-location value: where does close sit in the day's range?
    clv = []
    for b in recent:
        rng = b["h"] - b["l"]
        if rng > 0:
            clv.append((2 * b["c"] - b["l"] - b["h"]) / rng)  # -1..+1
    clv_score = (statistics.mean(clv) + 1) / 2 if clv else 0.5  # 0-1

    # Compress vol_ratio into 0-1 band: 0.7->0, 1.3+->1
    vol_score = max(0.0, min(1.0, (vol_ratio - 0.7) / 0.6))

    # Blend: 45% up/down vol, 35% close location, 20% volume ratio
    return 0.45 * ud_ratio + 0.35 * clv_score + 0.20 * vol_score


def sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def trend_score(bars: list[dict]) -> float:
    if len(bars) < 200:
        return 0.0
    closes = [b["c"] for b in bars]
    price = closes[-1]
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    if sma50 is None or sma200 is None:
        return 0.0
    score = 0.0
    if price > sma50:
        score += 0.4
    if price > sma200:
        score += 0.3
    if sma50 > sma200:
        score += 0.3
    return score


def relative_strength(stock_bars: list[dict], spy_bars: list[dict], lookback_days: int = 63) -> Optional[float]:
    """Relative return over `lookback_days`: stock_return - spy_return. >0 means outperforming."""
    s = pct_return(stock_bars, lookback_days)
    m = pct_return(spy_bars, lookback_days)
    if s is None or m is None:
        return None
    return s - m


def trend_health(bars: list[dict]) -> dict:
    """Hard-gate trend-health check. Returns dict with 'pass', 'reasons', and the moving averages.

    Pass requires:
      * price >= SMA50      (catches DE -3.7% below SMA50 on 2026-04-22)
      * SMA20 >= SMA50      (catches DE bearish cross)
    SMA200 stays a soft factor in trend_score (not a hard gate).
    """
    if len(bars) < 50:
        return {"pass": False, "reasons": ["insufficient bars (<50)"]}
    closes = [b["c"] for b in bars]
    price = closes[-1]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200) if len(bars) >= 200 else None
    reasons: list[str] = []
    if sma50 is None:
        return {"pass": False, "reasons": ["no SMA50"]}
    if TREND_REQUIRE_PRICE_GE_SMA50 and price < sma50:
        reasons.append(f"price {price:.2f} < SMA50 {sma50:.2f}")
    if sma20 is not None and sma20 < sma50 * TREND_BEAR_CROSS_TOLERANCE:
        gap_pct = (sma20 / sma50 - 1) * 100
        reasons.append(f"SMA20 {sma20:.2f} < SMA50 {sma50:.2f} ({gap_pct:.2f}% bear cross)")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "price": price,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
    }


def sub_sector_for(ticker: str, sp100_sector: str) -> str:
    """Map a ticker to a sub-sector for concentration caps. Defaults to GICS sector."""
    if ticker in SEMIS_TICKERS:
        return "Semis"
    return sp100_sector


def volatility_20d(bars: list[dict]) -> Optional[float]:
    if len(bars) < 21:
        return None
    rets = []
    closes = [b["c"] for b in bars[-21:]]
    for i in range(1, len(closes)):
        rets.append(closes[i] / closes[i - 1] - 1.0)
    if not rets:
        return None
    return statistics.pstdev(rets)


def correlation_60d(a: list[dict], b: list[dict]) -> Optional[float]:
    if len(a) < 61 or len(b) < 61:
        return None
    a_r = [a[i]["c"] / a[i - 1]["c"] - 1 for i in range(-60, 0)]
    b_r = [b[i]["c"] / b[i - 1]["c"] - 1 for i in range(-60, 0)]
    ma, mb = statistics.mean(a_r), statistics.mean(b_r)
    num = sum((x - ma) * (y - mb) for x, y in zip(a_r, b_r))
    da = sum((x - ma) ** 2 for x in a_r) ** 0.5
    db = sum((y - mb) ** 2 for y in b_r) ** 0.5
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def percentile_rank(value: float, distribution: list[float]) -> float:
    if not distribution:
        return 0.5
    sorted_d = sorted(distribution)
    count_below = sum(1 for x in sorted_d if x < value)
    return count_below / len(sorted_d)


def load_upcoming_earnings() -> dict[str, dict]:
    """Read state/upcoming_earnings.json (produced by scripts/fetch_earnings_window.py).

    Returns {ticker: {date, source, ...}} or {} if missing/stale. Does NOT raise on
    absence — caller treats missing as "no earnings info available, gate is advisory".
    """
    path = PROJECT / "state" / "upcoming_earnings.json"
    if not path.exists():
        print(f"  upcoming_earnings.json missing → pre-earnings gate is a no-op", file=sys.stderr)
        return {}
    try:
        data = json.loads(path.read_text())
        valid_until = data.get("valid_until")
        if valid_until:
            from datetime import datetime as _dt
            try:
                vu = _dt.fromisoformat(valid_until.replace("Z", "+00:00"))
                if vu < _dt.now(vu.tzinfo):
                    print(f"  upcoming_earnings.json STALE (valid_until {valid_until}) "
                          f"— rerun fetch_earnings_window.py", file=sys.stderr)
            except Exception:
                pass
        return data.get("by_symbol", {})
    except Exception as e:
        print(f"  upcoming_earnings.json unreadable: {e}", file=sys.stderr)
        return {}


def days_to_earnings(ticker: str, upcoming: dict[str, dict], today: date) -> Optional[int]:
    """Return calendar days until next earnings, or None if unknown."""
    info = upcoming.get(ticker.upper())
    if not info:
        return None
    try:
        d = date.fromisoformat(info["date"])
    except Exception:
        return None
    delta = (d - today).days
    return delta if delta >= 0 else None  # already-passed earnings → no block


def load_premarket_flags() -> dict[str, dict]:
    """Read state/premarket_data.json (Phase 5 — pre-open final mode only).

    Returns {ticker: flag_dict}. flag_dict has at minimum {kind, gap_pct,
    earnings_today, advice}. Missing file → empty dict (gate becomes a no-op
    and is logged).
    """
    path = PROJECT / "state" / "premarket_data.json"
    if not path.exists():
        print("  premarket_data.json missing → premarket gate is a no-op", file=sys.stderr)
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        print(f"  premarket_data.json unreadable: {e}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    for f in data.get("flags", []):
        if f.get("kind") == "symbol_gap" and f.get("symbol"):
            out[f["symbol"].upper()] = f
    return out


def main(mode: str = "final") -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    universe = load_universe()
    cands = candidate_tickers(universe)
    tickers = [t for t, _, _ in cands] + ["SPY"]

    today = date.today()
    start = today - timedelta(days=365)
    bars = fetch_alpaca_bars(tickers, start, today)
    spy_bars = bars.get("SPY", [])
    if not spy_bars:
        print("ERROR: SPY bars unavailable", file=sys.stderr)
        return 1

    upcoming = load_upcoming_earnings()

    # Compute raw metrics
    rows = []
    rs_values = []
    for ticker, analyst_sector, sp100_sector in cands:
        b = bars.get(ticker, [])
        if len(b) < 200:
            print(f"skip {ticker}: only {len(b)} bars", file=sys.stderr)
            continue
        rs_3m = relative_strength(b, spy_bars, 63)
        rs_20d = relative_strength(b, spy_bars, 20)
        if rs_3m is None or rs_20d is None:
            continue
        th = trend_health(b)
        dte = days_to_earnings(ticker, upcoming, today)
        rs_values.append(rs_3m)
        rows.append({
            "ticker": ticker,
            "analyst_sector": analyst_sector,
            "sector": sp100_sector,
            "sub_sector": sub_sector_for(ticker, sp100_sector),
            "bars": b,
            "rs_3m": rs_3m,
            "rs_20d": rs_20d,
            "trend_health_pass": th["pass"],
            "trend_health_reasons": th["reasons"],
            "days_to_earnings": dte,
            "accum": accumulation_proxy(b),
            "trend": trend_score(b),
            "vol_20d": volatility_20d(b) or 0.02,
            "ret_1m": pct_return(b, 21) or 0.0,
            "price": b[-1]["c"],
            "sector_weight": SECTOR_WEIGHT[analyst_sector],
            "sector_rank": SECTOR_RANK[analyst_sector][0],
        })

    # --- Hard gates (drop with reason logging) ---
    blocked: list[dict] = []
    survivors: list[dict] = []
    for r in rows:
        reasons = []
        if r["rs_3m"] <= RS_3M_HARD_FLOOR:
            reasons.append(f"rs_3m {r['rs_3m']:+.3f} <= floor {RS_3M_HARD_FLOOR:+.3f}")
        if r["rs_20d"] <= RS_20D_HARD_FLOOR:
            reasons.append(f"rs_20d {r['rs_20d']:+.3f} <= floor {RS_20D_HARD_FLOOR:+.3f}")
        if not r["trend_health_pass"]:
            reasons.append("trend: " + "; ".join(r["trend_health_reasons"]))
        dte = r["days_to_earnings"]
        if dte is not None and dte <= EARNINGS_BLOCKER_DAYS:
            reasons.append(f"earnings in {dte}d (window={EARNINGS_BLOCKER_DAYS}d)")
        if reasons:
            blocked.append({"ticker": r["ticker"], "sub_sector": r["sub_sector"], "reasons": reasons})
            continue
        survivors.append(r)
    if blocked:
        print(f"Hard-gate blocked {len(blocked)} of {len(rows)} names:", file=sys.stderr)
        for b_ in blocked:
            print(f"  - {b_['ticker']:5s} ({b_['sub_sector']}): {' | '.join(b_['reasons'])}", file=sys.stderr)
    positive = survivors

    # Conviction score (with RS percentile within surviving pool)
    rs_dist = [r["rs_3m"] for r in positive]
    for r in positive:
        rs_p = percentile_rank(r["rs_3m"], rs_dist)
        r["rs_percentile"] = rs_p
        r["conviction"] = (
            0.40 * rs_p
            + 0.25 * r["accum"]
            + 0.20 * r["trend"]
            + 0.15 * r["sector_weight"]
        )

    positive.sort(key=lambda x: x["conviction"], reverse=True)

    common_meta = {
        "as_of": today.isoformat(),
        "mode": mode,
        "universe_size": len(cands),
        "passed_rs_gate": len(positive),
        "hard_gates": {
            "rs_3m_floor": RS_3M_HARD_FLOOR,
            "rs_20d_floor": RS_20D_HARD_FLOOR,
            "rs_3m_percentile_floor": RS_3M_PCT_FLOOR,
            "trend_require_price_ge_sma50": TREND_REQUIRE_PRICE_GE_SMA50,
            "trend_bear_cross_tolerance": TREND_BEAR_CROSS_TOLERANCE,
            "earnings_blocker_days": EARNINGS_BLOCKER_DAYS,
        },
        "blocked_by_hard_gates": blocked,
    }

    if mode == "draft":
        # Top-30 superset, no correlation prune, no sector cap, no percentile gate.
        # The morning's `final` mode refines this against fresh bars.
        draft_count = min(len(positive), 30)
        draft = positive[:draft_count]
        for r in draft:
            r.pop("bars", None)
        out = {**common_meta, "draft": draft, "draft_count": draft_count}
        out_path = OUT_DIR / "candidates_draft.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"Draft candidates written: {out_path}")
        print(f"  Universe: {len(cands)}  passed RS gate: {len(positive)}  draft: {draft_count}")
        for r in draft[:10]:
            print(f"  {r['ticker']:6s} sec={r['analyst_sector']:16s} conv={r['conviction']:.3f}  "
                  f"RS3M={r['rs_3m']:+.3f}  RS20D={r['rs_20d']:+.3f}")
        return 0

    # --- final mode: premarket gate + percentile gate + correlation prune + sector caps ---
    premarket_flags = load_premarket_flags()
    premarket_blocked: list[dict] = []
    premarket_soft_flagged: list[dict] = []
    pre_survivors: list[dict] = []
    for r in positive:
        flag = premarket_flags.get(r["ticker"].upper())
        if flag:
            gap = flag.get("gap_pct")
            if flag.get("earnings_today") and gap is not None and gap < -3.0:
                # gap_pct in premarket_data.json is in percent (already × 100)
                premarket_blocked.append({
                    "ticker": r["ticker"],
                    "gap_pct": gap,
                    "earnings_today": True,
                })
                continue
            if gap is not None and abs(gap) > 5.0:
                premarket_soft_flagged.append({
                    "ticker": r["ticker"],
                    "gap_pct": gap,
                    "advice": flag.get("advice", "review at runbook level"),
                })
        pre_survivors.append(r)
    if premarket_blocked:
        print(f"Premarket-gate blocked {len(premarket_blocked)}:", file=sys.stderr)
        for b_ in premarket_blocked:
            print(f"  - {b_['ticker']:5s} gap={b_['gap_pct']:+.2f}% earnings_today=True",
                  file=sys.stderr)
    positive = pre_survivors

    pct_blocked: list[dict] = []
    pct_survivors: list[dict] = []
    for r in positive:
        if r["rs_percentile"] < RS_3M_PCT_FLOOR:
            pct_blocked.append({
                "ticker": r["ticker"],
                "sub_sector": r["sub_sector"],
                "rs_3m": r["rs_3m"],
                "rs_percentile": r["rs_percentile"],
            })
            continue
        pct_survivors.append(r)
    if pct_blocked:
        print(f"RS-percentile blocked {len(pct_blocked)} (floor={RS_3M_PCT_FLOOR:.0%}):", file=sys.stderr)
        for b_ in pct_blocked:
            print(f"  - {b_['ticker']:5s} rs_3m={b_['rs_3m']:+.3f} pct={b_['rs_percentile']:.2f}",
                  file=sys.stderr)
    positive = pct_survivors

    # Take top 15, then correlation prune (track which names dropped to which keeper).
    shortlist = positive[:15]
    keep: list[dict] = []
    correlation_pruned: list[dict] = []
    for cand in shortlist:
        drop_against = None
        for k in keep:
            corr = correlation_60d(cand["bars"], k["bars"])
            if corr is not None and corr > 0.85:
                drop_against = {"keeper": k["ticker"], "corr": round(corr, 3)}
                break
        if drop_against is None:
            keep.append(cand)
        else:
            correlation_pruned.append({"ticker": cand["ticker"], **drop_against})
        if len(keep) >= 10:
            break

    # Sector cap: ≤25% per sector means max 5 names per sector (since 5% cap each).
    # Sub-sector cap: tighter limit for known concentrated buckets (e.g., Semis).
    by_sector: dict[str, int] = {}
    by_sub: dict[str, int] = {}
    capped: list[dict] = []
    sector_dropped: list[dict] = []
    for cand in keep:
        s = cand["analyst_sector"]
        sub = cand["sub_sector"]
        sub_cap = SUB_SECTOR_NAME_CAPS.get(sub)
        if by_sector.get(s, 0) >= 5:
            sector_dropped.append({"ticker": cand["ticker"], "reason": f"sector {s} full"})
            continue
        if sub_cap is not None and by_sub.get(sub, 0) >= sub_cap:
            sector_dropped.append({"ticker": cand["ticker"], "reason": f"sub-sector {sub} cap {sub_cap}"})
            continue
        capped.append(cand)
        by_sector[s] = by_sector.get(s, 0) + 1
        by_sub[sub] = by_sub.get(sub, 0) + 1

    # Strip bars before saving
    for r in capped:
        r.pop("bars", None)

    final_count = min(len(capped), 10)
    final = capped[:final_count]

    out = {
        **common_meta,
        "blocked_by_premarket_gap": premarket_blocked,
        "premarket_soft_flagged": premarket_soft_flagged,
        "blocked_by_rs_percentile": pct_blocked,
        "correlation_pruned": correlation_pruned,
        "blocked_by_sector_caps": sector_dropped,
        "shortlist_after_corr_sector": len(capped),
        "final": final,
    }
    out_path = OUT_DIR / "candidates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"Candidates written: {out_path}")
    print(f"  Universe: {len(cands)}  passed RS gate: {len(positive)}  final: {len(final)}")
    for r in final:
        print(f"  {r['ticker']:6s} sec={r['analyst_sector']:16s} conv={r['conviction']:.3f}  "
              f"RS3M={r['rs_3m']:+.3f}  trend={r['trend']:.2f}  accum={r['accum']:.2f}  "
              f"vol20d={r['vol_20d']:.3f}  price={r['price']:.2f}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--mode",
        choices=["draft", "final"],
        default="final",
        help="draft: top-30 superset, no prune/caps. final: full screen for build_diff_plan.",
    )
    args = parser.parse_args()
    sys.exit(main(args.mode))
