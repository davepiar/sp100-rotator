# Skill cadence audit

**Last reviewed:** 2026-04-27 (post-Phase-5 redistribution)

For each skill currently in `.claude/skills/`, three columns:

- **Cadence** — how often it should run.
- **Session** — which session of the new two-session model owns it.

| Skill                       | Cadence   | Session             | API budget | Notes |
|---|---|---|---|---|
| `alpaca-trading`            | as-needed | both                | Alpaca paper | Execution wrapper. |
| `exposure-coach`            | daily     | `post-close`        | none | Reads `regime_context.json`; outputs Market Posture. |
| `ftd-detector`              | daily     | `post-close`        | none | Bottom-confirmation. |
| `macro-regime-detector`     | weekly    | `weekly-context`    | none | 1–2 yr horizon. |
| `market-breadth-analyzer`   | daily     | `post-close`        | none | Tactical breadth (5–20d). |
| `market-top-detector`       | daily     | `post-close`        | none | Distribution-day count. |
| `position-sizer`            | daily     | `pre-open`          | none | Used inside `build_diff_plan.py`. |
| `sector-analyst`            | weekly    | `weekly-context`    | none | Sector rotation. |
| `theme-detector`            | weekly    | `weekly-context`    | none | Narrative themes. |
| `uptrend-analyzer`          | daily     | `post-close`        | none | Breadth dashboard. |
| `us-market-bubble-detector` | weekly    | `weekly-context`    | none | Minsky/Kindleberger v2.1. |
| `earnings-calendar`         | daily     | `post-close`        | FMP ~5/day | Via `fetch_earnings_window.py`. |
| `backtest-expert`           | monthly   | `monthly-recalibration` | none | Sensitivity methodology. |
| **Tier A (ported, not yet wired into runbooks)** | | | | |
| `signal-postmortem`         | monthly   | `monthly-recalibration` | none | Per-signal FP/FN tracking. Closes the measurement loop. |
| `data-quality-checker`      | daily     | end of every session | none | Validates the daily report before publication. Advisory only. |
| `market-news-analyst`       | daily     | `post-close`        | WebSearch/Fetch | 10-day news summary. No API key. |
| `economic-calendar-fetcher` | daily     | `post-close` + `pre-open` | FMP 1/day | FOMC/CPI/NFP dates → kill-switch on macro miss. |
| `pead-screener`             | daily/weekly | `post-close` (parallel) | FMP 50-200/day | Post-earnings-drift candidates. |
| `vcp-screener`              | weekly    | `post-close` (parallel) | FMP ~300/run | Tight-base breakout candidates. **Run weekly to fit budget.** |
| `canslim-screener`          | weekly    | `post-close` (parallel) | FMP ~283/run | O'Neil growth. **Cap `--max-candidates 35` or upgrade FMP.** |
| `earnings-trade-analyzer`   | daily     | `post-close`        | FMP ~140/run | Post-earnings 5-factor scoring. |

### FMP daily budget tracker (free tier = 250)

Worst-case daily totals depending on which optional skills run:

| Mode | Skills running daily | Daily FMP calls |
|---|---|---|
| **Today (minimal)** | earnings-calendar | 5 |
| **Tier A daily-only** | + economic-calendar, market-news, pead, earnings-trade-analyzer | ~196 |
| **+ vcp daily** | adds vcp-screener | ~496 ❌ over budget |
| **+ canslim daily** | adds canslim-screener (capped at 35) | ~478 ❌ over budget |
| **Recommended** | Tier A daily-only + vcp/canslim weekly | ~196 daily, peak ~500 weekly |

`monthly-recalibration` decides whether the weekly bump is worth it or
whether to upgrade FMP to Starter ($30/mo, 750 calls/day).

## To-port skills (from tradermonty/claude-trading-skills)

Promotions vs. `CLAUDE.md` priorities, based on the DE post-mortem (see `docs/replay-2026-04-w17.md`):

| Skill                          | CLAUDE.md tier | Master-plan tier | Phase   | Justification |
|---|---|---|---|---|
| `relative-strength-ranker`     | P2             | **P0**           | 1       | Per-name RS gate. Daily prompt's 3M filter is too long-window. |
| `trend-following-diagnostics`  | P2             | **P0**           | 1       | Already partially implemented inline as `trend_health()` in `screen_candidates.py` (Phase 0.5 done). Port skill to consolidate. |
| `earnings-trade-analyzer`      | P1             | **P0**           | 1       | Pre-earnings risk window. Would have flagged DE (~21d to print) and INTC (2d to print). |
| `earnings-calendar`            | new            | P0               | 1       | Data plumbing for the above. YAML-first, port skill if maintenance hurts. |
| `market-news-analyst`          | new            | P1               | 1       | Narrative confirmation; soft signal. |
| `economic-calendar-fetcher`    | new            | P1               | 1       | YAML-first for FOMC/CPI/NFP. |
| `vcp-screener`                 | P1             | P1               | 1 or 4  | Tight-base entries. Would have excluded DE. |
| `canslim-screener`             | P1             | P1               | 1 or 4  | Quality floor. Doesn't catch DE specifically (DE quality is fine). |
| `us-market-bubble-detector`    | new            | P1               | **3 — ported** | Structural risk layer; weekly cadence in `weekly-context`. |
| `pead-screener`                | new            | P2               | 4       | Post-earnings-drift; complements momentum. |
| `backtest-expert`              | new            | P2               | **4 — ported** | Monthly recalibration mechanics. |
| `scenario-analyzer`            | new            | P3               | 4 (opt) | 18-month projections; nice-to-have. |
| `data-quality-checker`         | new            | P2               | 6       | Guardrail before sending reports. |
| `druckenmiller-synthesizer`    | P1             | P3               | —       | Overlaps `exposure-coach`. Optional. |
| `risk-parity-allocator`        | P2             | drop             | —       | Different paradigm; not aligned with momentum rotator. |
| `drawdown-monitor`             | P2             | drop             | —       | Already implemented as a hook (`drawdown_guard.py`). |
| `institutional-flow-tracker`   | OUT            | **decide**       | 5       | FMP 13F endpoints behind paid tier post-2025-08-31. Options: drop / EDGAR direct / FMP upgrade. Default: **drop**, revisit only if monthly-recalibration shows we'd benefit. |

## Dependency map

| Skill                       | External deps                              |
|---|---|
| `alpaca-trading`            | Alpaca REST (paper)                        |
| `exposure-coach`            | Outputs of breadth/regime/top/uptrend/ftd  |
| `ftd-detector`              | Yahoo Finance (free)                       |
| `macro-regime-detector`     | Yahoo Finance (free)                       |
| `market-breadth-analyzer`   | TraderMonty public CSV                     |
| `market-top-detector`       | Yahoo Finance + sector ETF data            |
| `position-sizer`            | Pure compute                               |
| `sector-analyst`            | Public CSV (TraderMonty)                   |
| `theme-detector`            | FINVIZ Elite (paid) — currently skipped    |
| `uptrend-analyzer`          | TraderMonty public CSV                     |

No paid keys required for the core daily loop. `theme-detector` is the only blocker — defer its weekly-context role until we either pay for FINVIZ or implement an alternative.
