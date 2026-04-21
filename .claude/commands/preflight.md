---
description: Run the daily SP100 rotation session — preflight, signals, plan, approval gate, execute.
allowed-tools: Bash, Read, Write, Edit, Skill, TodoWrite
---

Daily SP100 rotation session — !`date +%Y-%m-%d`. Paper account only.

PREFLIGHT
1. Load creds: `set -a; . /c/Users/thepi/.alpaca/credentials; set +a`
2. Verify US market is CLOSED (reject if open).
3. Fetch /v2/account → record equity, cash, buying_power, last_equity.
   Compute intraday DD = (equity - last_equity)/last_equity. If < -2%, HARD STOP.
4. Fetch /v2/positions → reconcile against state/portfolio.json, log any drift loudly.
5. Fetch /v2/orders?status=open → if any orders are still in the book from a prior
   session, list them. Ask me whether to cancel before placing new orders (stacking
   exposure is a hard no-no).

SIGNAL LAYER (run in parallel where possible, save every output to data/snapshots/YYYY-MM-DD/)
6. Invoke skills in this order and save the JSON/summary each returns:
   - macro-regime-detector
   - market-breadth-analyzer
   - uptrend-analyzer
   - market-top-detector
   - ftd-detector
   - sector-analyst
   - theme-detector  (skip if FINVIZ Elite / pandas not available — noted in CLAUDE.md)
   (institutional-flow-tracker is DROPPED per CLAUDE.md — FMP paywall on 13F endpoints.
    Substitute: price-volume accumulation proxy inline in scripts/screen_candidates.py.)

SYNTHESIS
7. Call exposure-coach with the five regime signals → produce a Market Posture
   block: net exposure ceiling %, growth/value tilt, new-entries allowed?, cash priority?
   If exposure-coach fails on schema mismatch, synthesize manually from the raw scores.
8. Build candidate list: names must be in sp100.csv AND in top-3 sectors AND show
   positive 3M RS vs SPY. Rank by composite conviction (0-1). Keep top 5-10.
   Apply sector cap (≤25%/sector). Compute 60-day correlations;
   drop the weaker of any pair >0.85.
9. Call position-sizer per name: 5% max, scale by conviction × (1 - 20d vol).
   Build target_weights.json (signed_off: false).
10. Diff vs current positions → buy/sell list.

PRESENT PLAN + STOP
11. Print a Markdown table: ticker | sector | action | qty | limit px | weight |
    one-line thesis citing which signals drove the pick. Show cash % post-trade.
    Write data/reports/YYYY-MM-DD.md with full skill outputs + plan.
    ⚠️ DO NOT PLACE ANY ORDERS. Wait for me to reply "APPROVE" or "APPROVE with edits: ...".

EXECUTE (only after my APPROVE)
12. TIF picker: if running during RTH use `day` + poll fills for 10 min, unfilled → MOC.
    If running after 19:00 ET use `opg` (LOO). Otherwise (16:00-19:00 ET window) use `gtc`.
    Limit = mid ± 5bp. Append every request/response to state/trade_log.jsonl.
13. Update state/portfolio.json from Alpaca /v2/positions + /v2/account + /v2/orders?status=open.
    Print final submission (or fill) summary with any remaining open orders flagged.
