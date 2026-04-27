# Phase 0.3 — State file contract

Every JSON file under `state/` carries the same envelope:

```jsonc
{
  "schema_version": "1.0",
  "generated_at":   "2026-04-26T20:00:00Z",   // UTC ISO 8601
  "valid_until":    "2026-05-03T20:00:00Z",   // when the file becomes stale
  // ... payload ...
}
```

Sessions reading a state file MUST check `valid_until`; if stale, abort and surface
"run `<producer>` first". Sessions writing a file MUST include the envelope.

## Files

### `state/portfolio.json`
- **Producer:** `scripts/execute_plan.py`, ad-hoc seed; `post-close` reconciles
- **Consumer:** both sessions, anything that needs current holdings without hitting Alpaca
- **Refresh:** every session (read in post-close, written post-execute in pre-open)
- **Validity:** until next session
- Source of truth is Alpaca; this file is a snapshot.

### `state/target_weights.json`
- **Producer:** `scripts/build_diff_plan.py` (called only from `pre-open`)
- **Consumer:** `scripts/execute_plan.py` (refuses if `signed_off=False`)
- **Refresh:** every `pre-open` session
- **Validity:** until next session
- **Hook-protected:** `.claude/hooks/morning_window.py` refuses any Write/Edit
  to this filename outside 13:00–15:25 Madrid.
- Carries `signed_off` flag — flipped to `true` only by user reply "APPROVE".

### `state/trade_log.jsonl` (append-only)
- **Producer:** `scripts/execute_plan.py`
- **Consumer:** auditing, monthly-recalibration
- **Refresh:** append per order
- **Validity:** infinite
- One JSON object per line: `{ts, action, payload?, response?, …}`

### `state/session_turnover.json`
- **Producer:** `.claude/hooks/pre_order_check.py`
- **Consumer:** same hook
- **Refresh:** per order POST
- Tracks `MAX_SESSION_TURNOVER` (30/day).

### `state/regime_context.json`
- **Producer:** `weekly-context` session (Phase 3 runbook in
  `sessions/weekly_context.md`)
- **Consumer:** `post-close` (`exposure-coach` synthesis); `pre-open` reads
  for posture cross-reference
- **Refresh:** Sunday 20:00 Madrid
- **Validity:** 8 days
- Contents: `{macro_regime, sector_rotation, themes, bubble, breadth}` — see
  `sessions/weekly_context.md` for the schema.

### `state/research_bundle.json` *(Phase 5 — produced by `post-close`)*
- **Producer:** `scripts/write_research_bundle.py`
- **Consumer:** `pre-open` preflight (freshness check; abort if missing or stale)
- **Refresh:** every `post-close` session
- **Validity:** ~17 hours (set to next-open 13:30 UTC, conservative)
- Contents: `{posture, draft_tickers, draft_count, signal_hashes, produced_by, consumed_by}`
- Schema decisions:
  - `posture` keys: `{exposure_ceiling_pct, growth_value_tilt, new_entries_allowed, cash_priority, source, rationale?}`
  - `signal_hashes.candidates_draft` is a sha256 of `candidates_draft.json` —
    pre-open can detect if the draft was tampered with after post-close wrote
    the bundle.

### `state/strategy_params.json` *(Phase 4 — seeded, read by all sessions)*
- **Producer:** `monthly-recalibration` session (initial seed by hand)
- **Consumer:** all sessions
- **Refresh:** first Saturday of the month
- **Validity:** 35 days
- `tunable.*` parameters override the defaults baked into scripts. `fixed.*`
  parameters are non-tunable; changes require a CLAUDE.md edit.

### `state/upcoming_earnings.json` *(Phase 1)*
- **Producer:** `scripts/fetch_earnings_window.py`
- **Consumer:** `scripts/screen_candidates.py` (pre-earnings hard gate),
  `morning-validation` (overnight cross-check)
- **Refresh:** every `evening-planning` session
- **Validity:** 7 days (auto-stale warning at consume time)
- Contents: `{by_symbol: {symbol: {date, source, eps_est?, revenue_est?}}}`
  merged from FMP /stable/earnings-calendar + manual override at
  `data/earnings/manual.json`.

### `state/premarket_data.json`
- **Producer:** `scripts/premarket_check.py`
- **Consumer:** `pre-open` runbook + `screen_candidates.py --mode=final`
  (premarket-flag fold-in: drops names with overnight earnings + adverse gap)
- **Refresh:** ~13:00 Madrid (start of pre-open)
- **Validity:** 2h
- Contents: SPY/QQQ pre-market, UVXY proxy for VIX, per-symbol gap % for
  every ticker in `research_bundle.json.draft_tickers`, overnight earnings
  rows, threshold-driven `flags`.

### `state/morning_decisions.json` *(retired in Phase 5)*
- The defensive validator pattern was replaced by folding pre-market flags
  directly into the pre-open's screen + plan. There is no separate decisions
  artifact. Removed from the contract.

### `state/economic_calendar.json` *(Phase 5b — Tier A)*
- **Producer:** `post-close` (`economic-calendar-fetcher` skill, Phase 2)
- **Consumer:** `pre-open` (Phase 2 macro kill-switch),
  `monthly-recalibration` (regime-mismatch attribution)
- **Refresh:** every `post-close`
- **Validity:** 7 days
- Schema: `{schema_version, generated_at, valid_until, events: [{date,
  time_et, kind: "FOMC"|"CPI"|"NFP"|"GDP"|"PAYROLLS"|other, impact:
  "low"|"medium"|"high", actual?, consensus?, surprise?}]}`
- Pre-open's kill-switch fires when any `impact == "high"` event lands
  within ±2h of today's 09:30 ET open and either has surprise > ±0.5σ or
  hasn't released yet (defer mode).

### `state/news_summary.json` *(Phase 5b — Tier A)*
- **Producer:** `post-close` (`market-news-analyst` skill, Phase 3)
- **Consumer:** `post-close` report, `monthly-recalibration` (narrative
  attribution for losers)
- **Refresh:** every `post-close`
- **Validity:** 24 hours
- Schema: `{schema_version, generated_at, valid_until, top_events:
  [{date, headline, impact: "low"|"medium"|"high", category, affected_tickers}]}`

### `state/weekly_screens.json` *(Phase 5b — Tier A)*
- **Producer:** `weekly-context` (`vcp-screener` + `canslim-screener`
  skills, Phase 4b). Sunday 20:00 Madrid.
- **Consumer:** `post-close` Phase 5c — names appearing in
  `vcp_picks` or `canslim_picks` get a small conviction bonus (~0.03/tag,
  capped at 0.05) in tomorrow's pre-open final screen.
- **Refresh:** weekly (Sunday)
- **Validity:** 7 days
- Schema: `{schema_version, generated_at, valid_until, vcp_picks:
  [{ticker, grade, score, stage, base_quality}], canslim_picks:
  [{ticker, grade, score, rs_rating, eps_growth}], errors?: [...]}`
- Both screens are FMP-heavy (~250-300 calls each). Run weekly only.
  `canslim-screener` must use `--max-candidates 35` to fit free tier.

### `state/postmortem_outcomes.jsonl` *(Phase 5b — Tier A)*
- **Producer:** `execute_plan.py` (on every closed position; not yet wired
  — follow-up). Append-only, one JSON per line.
- **Consumer:** `monthly-recalibration` (`signal-postmortem` skill, Phase 2)
- **Refresh:** continuous as positions close
- **Validity:** never expires; this is the audit trail of which signals
  fired for each closed trade
- Schema (per line): `{date, ticker, side: "buy"|"sell", entry_date,
  exit_date, entry_px, exit_px, stop_px, pnl_pct, hold_days, mae_pct,
  mfe_pct, signals_at_entry: ["rs_3m", "trend", ...], signals_blocked:
  [...], exit_reason: "stop"|"tp"|"manual"|"rebalance",
  conviction_at_entry: 0.0-1.0}`
- The skill rebuilds missing rows from `state/trade_log.jsonl` + Alpaca
  history on first run, so an empty starter file is fine.

### `state/institutional_signals.json` *(future — Phase 5, optional)*
- Open question (see `docs/skill-cadence-audit.md`): whether to keep institutional-flow-tracker at all. If dropped, this file does not exist.

## Conventions

- Timestamps: UTC ISO 8601, `Z` suffix.
- Money: float with up to 2 decimals.
- Weights: float in `[0, 1]`, not percentage.
- Schema versioning: bump `schema_version` on any breaking field change. Older readers must error out, not silently misread.
