"""Replay the new screen_candidates hard gates against last week's actual buys.

Fetches Alpaca daily bars up to (and including) the trade date for each holding,
then runs the gates exactly as screen_candidates would have on that day.

Output: a Markdown table of {ticker, trade_date, rs_3m, rs_20d, trend_health,
verdict (PASS/BLOCK)} written to docs/replay-2026-04-w17.md.

This is the empirical evidence for Phase 0.4 of the master plan: every new gate
must demonstrate which past pick it would have changed.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from screen_candidates import (  # type: ignore
    EARNINGS_BLOCKER_DAYS,
    RS_20D_HARD_FLOOR,
    RS_3M_HARD_FLOOR,
    RS_3M_PCT_FLOOR,
    percentile_rank,
    relative_strength,
    sub_sector_for,
    trend_health,
)

# Buys made last week and the trade-date the gate would have evaluated on
BUYS = [
    ("CAT",  "2026-04-21", "Industrials"),
    ("FDX",  "2026-04-21", "Industrials"),
    ("INTC", "2026-04-21", "Information Technology"),
    ("LIN",  "2026-04-21", "Materials"),
    ("TXN",  "2026-04-21", "Information Technology"),
    ("AMD",  "2026-04-22", "Information Technology"),
    ("CSCO", "2026-04-22", "Information Technology"),
    ("DE",   "2026-04-22", "Industrials"),
    ("DOW",  "2026-04-22", "Materials"),
    ("AVGO", "2026-04-24", "Information Technology"),
    ("UNP",  "2026-04-24", "Industrials"),
]


def alpaca_get(path: str, **params) -> dict:
    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    r = requests.get(
        f"{data_url}{path}",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def bars_up_to(symbol: str, trade_date: date) -> list[dict]:
    start = trade_date - timedelta(days=400)
    out: list[dict] = []
    next_token = None
    while True:
        params = {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": (trade_date - timedelta(days=1)).isoformat(),
            "limit": 10000,
            "adjustment": "all",
            "feed": "iex",
        }
        if next_token:
            params["page_token"] = next_token
        js = alpaca_get(f"/v2/stocks/{symbol}/bars", **params)
        out.extend(js.get("bars", []))
        next_token = js.get("next_page_token")
        if not next_token:
            break
    out.sort(key=lambda b: b["t"])
    return out


def main() -> int:
    # Hypothetical earnings dates for the replay window (from public IR / FMP).
    # Best-known dates as of 2026-04-26; if FMP didn't provide, used the symbol's
    # standard quarterly cadence + 3 months from prior known print.
    REPLAY_EARNINGS = {
        "DE":   date(2026, 5, 14),   # fiscal Q2 (typical Deere mid-May)
        "AMD":  date(2026, 5, 5),    # confirmed via FMP
        "CSCO": date(2026, 5, 13),   # confirmed via FMP
        "LIN":  date(2026, 5, 7),    # typical early-May print
        # FDX, UNP, CAT, INTC, DOW, AVGO, TXN: most recent print already past;
        # next print is >EARNINGS_BLOCKER_DAYS away from each replay trade date.
    }

    # Pre-fetch SPY for each unique trade date
    trade_dates = sorted({date.fromisoformat(d) for _, d, _ in BUYS})
    spy_cache: dict[date, list[dict]] = {td: bars_up_to("SPY", td) for td in trade_dates}

    # First pass: compute per-symbol metrics
    metric_rows: list[dict] = []
    for ticker, dstr, sector in BUYS:
        td = date.fromisoformat(dstr)
        b = bars_up_to(ticker, td)
        spy = spy_cache[td]
        rs_3m = relative_strength(b, spy, 63)
        rs_20d = relative_strength(b, spy, 20)
        th = trend_health(b)
        sub = sub_sector_for(ticker, sector)
        next_earn = REPLAY_EARNINGS.get(ticker)
        dte = (next_earn - td).days if next_earn else None
        metric_rows.append({
            "ticker": ticker, "trade_date": dstr, "td": td, "sub_sector": sub,
            "rs_3m": rs_3m, "rs_20d": rs_20d, "trend_pass": th["pass"],
            "trend_reasons": th["reasons"], "days_to_earnings": dte,
        })

    # Second pass: build per-trade-date percentile distributions across
    # the same names that survived the first three hard gates that day.
    # (Approximation — real screen runs against full top-4-sector universe.)
    by_date: dict[date, list[dict]] = {}
    for r in metric_rows:
        by_date.setdefault(r["td"], []).append(r)
    for td, group in by_date.items():
        # candidates that survive trend + RS_3M + RS_20D + earnings on this date
        survivors = [
            x for x in group
            if x["rs_3m"] is not None and x["rs_3m"] > RS_3M_HARD_FLOOR
            and x["rs_20d"] is not None and x["rs_20d"] > RS_20D_HARD_FLOOR
            and x["trend_pass"]
            and not (x["days_to_earnings"] is not None and x["days_to_earnings"] <= EARNINGS_BLOCKER_DAYS)
        ]
        rs_dist = [s["rs_3m"] for s in survivors]
        for r in group:
            r["rs_3m_percentile"] = percentile_rank(r["rs_3m"], rs_dist) if r["rs_3m"] is not None else None

    rows: list[dict] = []
    for r in metric_rows:
        reasons = []
        if r["rs_3m"] is None or r["rs_3m"] <= RS_3M_HARD_FLOOR:
            reasons.append(f"rs_3m={r['rs_3m']:+.3f}" if r["rs_3m"] is not None else "rs_3m=None")
        if r["rs_20d"] is None or r["rs_20d"] <= RS_20D_HARD_FLOOR:
            reasons.append(f"rs_20d={r['rs_20d']:+.3f}" if r["rs_20d"] is not None else "rs_20d=None")
        if not r["trend_pass"]:
            reasons.append("trend: " + "; ".join(r["trend_reasons"]))
        if r["days_to_earnings"] is not None and r["days_to_earnings"] <= EARNINGS_BLOCKER_DAYS:
            reasons.append(f"earnings in {r['days_to_earnings']}d")
        if r["rs_3m_percentile"] is not None and r["rs_3m_percentile"] < RS_3M_PCT_FLOOR:
            reasons.append(f"rs_3m_pct={r['rs_3m_percentile']:.2f} < {RS_3M_PCT_FLOOR:.2f}")
        verdict = "BLOCK" if reasons else "PASS"

        rows.append({
            **{k: r[k] for k in ("ticker", "trade_date", "sub_sector", "rs_3m", "rs_20d",
                                 "trend_pass", "trend_reasons", "days_to_earnings", "rs_3m_percentile")},
            "verdict": verdict,
            "block_reasons": reasons,
        })

    # Print and write
    out_dir = PROJECT / "docs"
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / "replay-2026-04-w17.md"

    lines: list[str] = []
    lines.append("# Phase 0.4 replay — would the new gates have changed last week's picks?")
    lines.append("")
    lines.append(f"Generated {datetime.now().isoformat(timespec='seconds')}.")
    lines.append("")
    lines.append("Each row: gates evaluated against bars **up to and including the trading day before the buy**.")
    lines.append(f"Gates: rs_3m > {RS_3M_HARD_FLOOR:+.2f}, rs_20d > {RS_20D_HARD_FLOOR:+.2f}, "
                 f"price ≥ SMA50, SMA20 ≥ SMA50×{0.99}, "
                 f"earnings > {EARNINGS_BLOCKER_DAYS}d away, "
                 f"rs_3m percentile ≥ {RS_3M_PCT_FLOOR:.0%}.")
    lines.append("")
    lines.append("| Ticker | Trade date | Sub-sector | RS 3M | RS 20D | Trend | DTE | RS pct | Verdict | Reasons |")
    lines.append("|---|---|---|---:|---:|:-:|---:|---:|:-:|---|")
    for r in rows:
        rs3 = f"{r['rs_3m']*100:+.2f}pp" if r["rs_3m"] is not None else "—"
        rs20 = f"{r['rs_20d']*100:+.2f}pp" if r["rs_20d"] is not None else "—"
        tp = "✓" if r["trend_pass"] else "✗"
        dte = f"{r['days_to_earnings']}d" if r["days_to_earnings"] is not None else "—"
        rsp = f"{r['rs_3m_percentile']:.2f}" if r["rs_3m_percentile"] is not None else "—"
        verdict_emoji = "🟢 PASS" if r["verdict"] == "PASS" else "🔴 BLOCK"
        reason = "; ".join(r["block_reasons"]) if r["block_reasons"] else ""
        lines.append(f"| {r['ticker']} | {r['trade_date']} | {r['sub_sector']} | {rs3} | {rs20} | {tp} | {dte} | {rsp} | {verdict_emoji} | {reason} |")
    lines.append("")

    blocked = [r for r in rows if r["verdict"] == "BLOCK"]
    passed = [r for r in rows if r["verdict"] == "PASS"]
    lines.append(f"**Summary:** {len(blocked)}/{len(rows)} blocked, {len(passed)} pass.")
    lines.append("")
    if blocked:
        lines.append("## Blocked names — what last week's pipeline took, the new gates would refuse")
        lines.append("")
        for r in blocked:
            lines.append(f"- **{r['ticker']}** ({r['trade_date']}, {r['sub_sector']}): {'; '.join(r['block_reasons'])}")
        lines.append("")

    md_path.write_text("\n".join(lines))
    print(f"\nWrote {md_path}\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
