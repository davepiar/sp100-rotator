"""Screen SP100 candidates for the 2026-04-20 rotation session.

Pulls 6 months of daily bars from Alpaca for every SP100 ticker in the top-4
sectors (ranked by sector-analyst uptrend ratio), scores each candidate, applies
a 60-day correlation prune, and writes the shortlist to a JSON file for the
position sizer + plan builder to consume.

Conviction score (0-1) blends:
  * 3M relative strength vs SPY  (40%)   -- core requirement from daily prompt
  * Accumulation proxy (20/50d vol + close-location + up/down-day vol) (25%)
  * Trend score (above 50DMA + above 200DMA + 50>200) (20%)
  * Sector rank bonus (Basic Materials > Industrials > Healthcare > Tech) (15%)
"""
from __future__ import annotations

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
SECTOR_JSON_DIR = PROJECT / "data" / "snapshots" / "2026-04-20"
OUT_DIR = PROJECT / "data" / "snapshots" / "2026-04-20"

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
    """Relative 3M return: stock_return - spy_return. >0 means outperforming."""
    s = pct_return(stock_bars, lookback_days)
    m = pct_return(spy_bars, lookback_days)
    if s is None or m is None:
        return None
    return s - m


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


def main() -> int:
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

    # Compute raw metrics
    rows = []
    rs_values = []
    for ticker, analyst_sector, sp100_sector in cands:
        b = bars.get(ticker, [])
        if len(b) < 200:
            print(f"skip {ticker}: only {len(b)} bars", file=sys.stderr)
            continue
        rs = relative_strength(b, spy_bars, 63)
        if rs is None:
            continue
        rs_values.append(rs)
        rows.append({
            "ticker": ticker,
            "analyst_sector": analyst_sector,
            "sector": sp100_sector,
            "bars": b,
            "rs_3m": rs,
            "accum": accumulation_proxy(b),
            "trend": trend_score(b),
            "vol_20d": volatility_20d(b) or 0.02,
            "ret_1m": pct_return(b, 21) or 0.0,
            "price": b[-1]["c"],
            "sector_weight": SECTOR_WEIGHT[analyst_sector],
            "sector_rank": SECTOR_RANK[analyst_sector][0],
        })

    # Gate: require RS > 0 (positive 3M RS vs SPY) per daily prompt step 7
    positive = [r for r in rows if r["rs_3m"] > 0]

    # Conviction score
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

    # Take top 15, then correlation prune
    shortlist = positive[:15]
    keep: list[dict] = []
    for cand in shortlist:
        drop = False
        for k in keep:
            corr = correlation_60d(cand["bars"], k["bars"])
            if corr is not None and corr > 0.85:
                drop = True
                break
        if not drop:
            keep.append(cand)
        if len(keep) >= 10:
            break

    # Sector cap: ≤25% per sector means max 5 names per sector (since 5% cap each)
    # Enforce by dropping later-ranked names in any sector that's full.
    by_sector: dict[str, int] = {}
    capped: list[dict] = []
    for cand in keep:
        s = cand["analyst_sector"]
        if by_sector.get(s, 0) >= 5:
            continue
        capped.append(cand)
        by_sector[s] = by_sector.get(s, 0) + 1

    # Strip bars before saving
    for r in capped:
        r.pop("bars", None)

    final_count = min(len(capped), 10)
    final = capped[:final_count]

    out = {
        "as_of": today.isoformat(),
        "universe_size": len(cands),
        "passed_rs_gate": len(positive),
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
    sys.exit(main())
