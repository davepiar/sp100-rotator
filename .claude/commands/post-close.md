---
description: Post-close research session — gather signals, build market posture, scout draft candidates. No orders, no approval, no execution.
allowed-tools: Bash, Read, Write, Edit, Skill, TodoWrite
---

Post-close SP100 research session — !`date +%Y-%m-%d`. Paper account only.

Read `sessions/post_close.md` and execute it phase by phase. **Stop after writing
`state/evening_research.json` and `data/reports/<date>-research.md`.** Do NOT
build a trade plan. Do NOT request `APPROVE`. Do NOT call `scripts/build_diff_plan.py`
or `scripts/execute_plan.py`.

Tomorrow's pre-open session at 13:00 Madrid will pick up `evening_research.json`,
re-screen with fresh data, and place the actual orders.

Quick recap of phases:

1. **Preflight** — load creds, verify market closed, snapshot account, reconcile
   positions, list open orders (do NOT cancel here).
2. **Refresh data feeds** — `python scripts/fetch_earnings_window.py`. Then
   invoke `economic-calendar-fetcher` skill → `state/economic_calendar.json`
   (FOMC/CPI/NFP/payrolls for the next 7 days).
3. **Signal layer** — daily skills in parallel:
   - market-breadth-analyzer, uptrend-analyzer, market-top-detector, ftd-detector
   - `market-news-analyst` → `state/news_summary.json` (10-day market news, impact-ranked)
   - `earnings-trade-analyzer` → `data/snapshots/<date>/earnings-trade-analyzer/` (post-earnings 5-factor scoring, default lookback 2d)
   Save all raw outputs to `data/snapshots/<date>/<skill>/`.
4. **Synthesis** — call `exposure-coach` with the daily signals + regime
   context (`state/regime_context.json` if fresh, else live macro/sector/theme
   calls). Capture posture: `{exposure_ceiling_pct, growth_value_tilt,
   new_entries_allowed, cash_priority, rationale, source}`.
5. **Draft scout — three parallel paths:**
   - 5a. `python scripts/screen_candidates.py --mode=draft` → momentum rotator (canonical).
   - 5b. `pead-screener` skill (Mode B: consume earnings-trade-analyzer JSON) → `pead_picks` merged into `candidates_draft.json`.
   - 5c. Read `state/weekly_screens.json` (from `/weekly-context`); tag draft names that hit vcp / canslim picks for a small conviction bonus in tomorrow's pre-open.
6. **Persist research bundle** — `python scripts/write_research_bundle.py
   --posture-json='<posture from step 4>'`. Writes `state/evening_research.json`.
7. **Write report + validate** — `data/reports/<date>-research.md` with
   signals, news headlines (top 5), economic-calendar lookahead,
   posture, draft table, open orders. Then invoke `data-quality-checker`
   on the report; warnings get a "Data quality" section appended.

**STOP** after step 7.

Hard rules: paper only, never echo `ALPACA_SECRET_KEY`, no `POST /v2/orders`,
no `DELETE /v2/orders`, never write `state/target_weights.json`, never write
`data/snapshots/<date>/trade_plan.json`. The order-placing scripts and the
pre-open runbook are off-limits in this session.

If anything goes sideways (skill error, FMP unavailable, regime context stale):
note the gap in the report, continue if possible, never silently degrade. The
operator and tomorrow's pre-open need to know what was missing.
