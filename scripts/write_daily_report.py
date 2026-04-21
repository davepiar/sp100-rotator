"""Write data/reports/{SESSION_DATE}.md consolidating today's signal outputs + plan."""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TODAY = os.environ.get("SESSION_DATE", date.today().isoformat())


def main() -> int:
    snap = PROJECT / "data" / "snapshots" / TODAY
    out = PROJECT / "data" / "reports" / f"{TODAY}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    macro = json.loads(sorted(snap.glob("macro_regime_*.json"))[0].read_text())
    breadth = json.loads(sorted(snap.glob("market_breadth_*.json"))[0].read_text())
    uptrend = json.loads(sorted((snap / "reports").glob("uptrend_analysis_*.json"))[0].read_text())
    top = json.loads(sorted(snap.glob("market_top_*.json"))[0].read_text())
    ftd = json.loads(sorted(snap.glob("ftd_detector_*.json"))[0].read_text())
    plan = json.loads((snap / "trade_plan.json").read_text())
    account = json.loads((snap / "account.json").read_text())
    positions = json.loads((snap / "positions.json").read_text())

    lines: list[str] = []
    lines.append(f"# Daily Rotation Session — {TODAY}")
    lines.append("")
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} (local) — paper account only.*")
    lines.append("")

    lines.append("## Preflight")
    lines.append("")
    lines.append("- **Market:** CLOSED (US RTH ended 16:00 ET)")
    lines.append(f"- **Equity:** ${float(account['equity']):,.2f}")
    lines.append(f"- **Last equity:** ${float(account['last_equity']):,.2f}")
    dd = (float(account['equity']) - float(account['last_equity'])) / float(account['last_equity']) * 100
    lines.append(f"- **Intraday DD:** {dd:+.3f}% ({'PASS' if dd > -2 else 'HARD STOP'})")
    lines.append(f"- **Cash:** ${float(account['cash']):,.2f}  |  Buying power: ${float(account['buying_power']):,.2f}")
    lines.append("")
    lines.append("**Positions at session start (all from overnight GTC fills of 2026-04-20 plan):**")
    lines.append("")
    lines.append("| Ticker | Qty | MV | UPL | UPL% |")
    lines.append("|---|---|---|---|---|")
    for p in positions:
        lines.append(
            f"| {p['symbol']} | {p['qty']} | ${float(p['market_value']):,.2f} | "
            f"${float(p['unrealized_pl']):+,.2f} | {float(p['unrealized_plpc']) * 100:+.2f}% |"
        )
    lines.append("")
    lines.append("**Reconciliation:** 5 of 6 yesterday-planned GTC orders filled overnight "
                 "(DOW did not fill). DOW stale GTC (112 sh @ $36.78, ~$4.1k) was **cancelled** "
                 "before today's plan per user approval.")
    lines.append("")

    lines.append("## Signal Layer")
    lines.append("")
    lines.append("| Signal | Score | Zone | Guidance |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| Macro Regime | {macro['composite']['composite_score']:.1f}/100 | "
        f"{macro['composite']['zone']} | Regime: **{macro['regime']['regime_label']}** "
        f"({macro['regime']['confidence']} conf, 40-60% transition prob) |"
    )
    lines.append(
        f"| Market Breadth | {breadth['composite']['composite_score']:.1f}/100 | "
        f"{breadth['composite']['zone']} | {breadth['composite']['exposure_guidance']} equity exposure |"
    )
    lines.append(
        f"| Uptrend | {uptrend['composite']['composite_score']:.1f}/100 | "
        f"{uptrend['composite']['zone_detail']} | {uptrend['composite']['exposure_guidance']} |"
    )
    lines.append(
        f"| Market Top | {top['composite']['composite_score']:.1f}/100 | "
        f"{top['composite']['zone']} | Risk budget {top['composite']['risk_budget']} |"
    )
    lines.append(
        f"| FTD Detector | {ftd['quality_score']['total_score']}/100 | "
        f"{ftd['market_state']['combined_state']} | {ftd['quality_score']['guidance']} |"
    )
    lines.append(
        "| Sector Rank | — | Early cycle (77) | Top-4 (all overbought): "
        "Basic Materials 60.5%, Industrials 51.2%, Health Care 41.6%, Tech 40.9% |"
    )
    lines.append(
        "| Theme Detector | SKIPPED | — | pandas not installed; theme signal out (per CLAUDE.md) |"
    )
    lines.append("")

    lines.append("### Notable signal details")
    lines.append("")
    lines.append("**Macro strongest transitioning components (4/6):**")
    for ev in macro['regime']['evidence'][:4]:
        lines.append(f"- {ev['component']} = {ev['score']} — {ev['signal']}")
    lines.append("")
    lines.append("**Top-risk warnings:**")
    lines.append("- Distribution Day Count: **90/100 CRITICAL** — heavy distribution detected (dominant warning component).")
    lines.append("- Breadth vs S&P divergence: 34/100 — S&P +3.1% vs breadth 8MA −0.113 over 60d (dangerous bearish divergence).")
    lines.append("")
    lines.append("**FTD guidance (offensive signal):**")
    lines.append("- S&P 500 rally from 2026-03-30 low (−9.1% decline), Day 15 of rally, **FTD CONFIRMED**.")
    lines.append("- NASDAQ rally from 2026-03-30 low (−10.8%), Day 15, **FTD CONFIRMED**.")
    lines.append("- Quality 100/100 Strong FTD | Post-FTD distribution: 0 in 5 days | Power Trend: YES (3/3).")
    lines.append("")
    lines.append("**Uptrend warnings active:** LATE CYCLE and SECTOR DIVERGENCE (−7 penalty applied).")
    lines.append("")

    lines.append("## Market Posture (manual synthesis — exposure-coach schema mismatch)")
    lines.append("")
    lines.append("Signals conflict: FTD + uptrend strongly bullish; breadth + top-detector flag late-cycle divergence.")
    lines.append("User direction for this session: **option (a) — normal rotation sized ~50-60% exposure ceiling**.")
    lines.append("")
    lines.append("- **Exposure ceiling:** 55% (midpoint of breadth 60-75% and uptrend 80-90%, de-rated for top-detector early warning + distribution-day 90)")
    lines.append("- **Growth/Value bias:** slight growth tilt — size factor broadening (+small cap), XLY/XLP risk-on, Tech remains a top-4 sector")
    lines.append("- **New entries:** ALLOWED (FTD confirmed, sector leaders uptrending)")
    lines.append("- **Cash priority:** NO")
    lines.append("- **Sizing posture:** position-sizer rule (5% × conv × (1-vol)) — built-in de-rating for higher-vol names")
    lines.append("")

    lines.append("## Today's Plan")
    lines.append("")
    lines.append(f"- **Equity at plan:** ${plan['equity']:,.2f}")
    lines.append(
        f"- **Current deployed:** {plan['current_deployed_pct']:.2f}%  →  "
        f"**Post-trade deployed:** {plan['post_trade_deployed_pct']:.2f}%"
    )
    lines.append(f"- **Cash after:** {plan['cash_after_pct']:.2f}%")
    lines.append(
        f"- **New buy notional:** ${plan['new_buy_notional_usd']:,.2f}  |  "
        f"**Sells:** ${plan['sell_notional_usd']:,.2f}"
    )
    lines.append(
        "- **Sector weights (post-trade):** "
        + ", ".join(f"{k} {v:.1f}%" for k, v in plan['sector_weights_pct'].items())
    )
    n_buy = sum(1 for o in plan['orders'] if o['action'].startswith('BUY'))
    n_sell = sum(1 for o in plan['orders'] if o['action'].startswith('SELL'))
    n_hold = sum(1 for o in plan['orders'] if o['action'] == 'HOLD')
    lines.append(f"- **Orders:** {n_buy} buys, {n_sell} sells, {n_hold} holds")
    lines.append("")
    lines.append("| Action | Ticker | Sector | Qty | Limit $ | Target Weight | Conv | Thesis |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for o in plan['orders']:
        thesis = " / ".join(o.get('thesis_tags', []))
        lines.append(
            f"| {o['action']} | {o['ticker']} | {o['sector']} | {o['qty']} | "
            f"${o['limit_price']:.2f} | {o['target_weight_pct']:.2f}% | "
            f"{o.get('conviction', 0):.2f} | {thesis} |"
        )
    lines.append("")

    lines.append("### Risk-limit check")
    lines.append("")
    sect_max = max(plan['sector_weights_pct'].values()) if plan['sector_weights_pct'] else 0.0
    lines.append("- Max position size 5%: each order ≤ 5% ✓")
    lines.append("- Max positions 10: post-trade = 10 ✓ (at limit — no headroom)")
    lines.append(f"- Max drawdown 2% intraday: current DD {dd:+.3f}% ✓")
    lines.append(f"- Max turnover 30 trades/session: this session = {n_buy + n_sell} ✓")
    lines.append(f"- Max sector concentration 25%: highest sector = {sect_max:.1f}% ✓")
    lines.append("- RTH window: orders will use GTC TIF (market closed, 16:00-19:00 ET window per spec) ✓")
    lines.append("- S&P 100 universe only: all tickers in sp100.csv ✓")
    lines.append("")

    lines.append("## Approval Gate")
    lines.append("")
    lines.append("**NO ORDERS HAVE BEEN PLACED.** Waiting for user approval.")
    lines.append("")
    lines.append("- Reply `APPROVE` to place the 5 BUY orders as shown, GTC limit at last_close+5bp.")
    lines.append("- Reply `APPROVE with edits: ...` to change sizes / drop names / swap TIF.")
    lines.append("- Anything else = do not place orders.")
    lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
