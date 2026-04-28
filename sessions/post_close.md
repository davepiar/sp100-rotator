# `post-close` session — research only

Runs **22:00 Europe/Madrid** (post US close, ~16:00 ET +30 min).
Purpose: gather signals, build market posture, scout a draft candidate list.
**Does not propose orders, does not request approval, does not execute.**

The decision and execution happen in the `pre-open` session at 13:00 Madrid.
This session's only output that affects tomorrow is `state/research_bundle.json`.

---

## Inputs

| Path | Producer | Required |
|---|---|---|
| `state/portfolio.json` | prior session (or seed) | yes |
| `data/universe/sp100.csv` | manual | yes |
| `state/strategy_params.json` | `monthly-recalibration` | yes (defaults if missing) |
| `state/regime_context.json` | `weekly-context` | optional — falls back to live skill calls if missing/stale |
| `state/weekly_screens.json` | `weekly-context` | optional — without it, conviction skips the vcp/canslim bonus |
| Alpaca + FMP credentials | `~/.alpaca/credentials` | yes |

If anything required is missing, abort and surface the missing producer.

---

## Phases

### 1. Preflight (~30 s)

```bash
set -a; . /c/Users/dpineda/.alpaca/credentials; set +a
```

- Verify US market is CLOSED (current ET ≥ 16:00). If open, abort.
- `GET /v2/account` → record `equity`, `cash`, `last_equity`. Compute intraday DD;
  if < −2 %, **HARD STOP** with red-flag report.
- `GET /v2/positions` → reconcile against `state/portfolio.json`. Log any drift loudly.
- `GET /v2/orders?status=open` → **list** any stale day orders. Do **not** cancel
  here. Cancellation decisions happen tomorrow morning in `pre-open`. Just record
  them in the research report so the operator sees them at 22:00 and again at 13:00.

### 2. Refresh data feeds (~2 min)

```bash
python scripts/fetch_earnings_window.py
```

This refreshes `state/upcoming_earnings.json` from FMP `/stable/earnings-calendar`
+ `data/earnings/manual.json` overrides. The pre-earnings hard gate in
`screen_candidates.py` reads this file. If `FMP_API_KEY` is missing, only
manual entries are used and the gate becomes permissive — log a warning.

Then invoke the **`economic-calendar-fetcher`** skill for the next 7 days.
Persist the structured output to `state/economic_calendar.json` (schema:
`{schema_version, generated_at, valid_until=+7d, events:[{date,time_et,kind,impact,actual,consensus,surprise}]}`).
Pre-open reads this file to kill-switch marginal buys when a major release
(FOMC / CPI / NFP / GDP / payrolls) lands within ±2h of the open.

### 3. Signal layer — daily-cadence skills only (~3-5 min)

Call in parallel where possible. Save raw outputs to `data/snapshots/<YYYY-MM-DD>/<skill>/`.

Daily macro/breadth signals:
- `market-breadth-analyzer`
- `uptrend-analyzer`
- `market-top-detector`
- `ftd-detector`

Narrative + post-print signals (Phase 5b additions):
- **`market-news-analyst`** — 10-day market news summary, impact-ranked.
  Persist to `state/news_summary.json` (schema:
  `{schema_version, generated_at, valid_until=+24h, top_events:[{date,headline,impact,affected_tickers}]}`).
  Each `top_event` should also include `direction` (`risk-on` / `risk-off-margin` /
  `binary` / `neutral`) and a stable lowercase `topic_slug` (e.g. `iran_hormuz`,
  `fed_rate_hike`) so the scenario-analyzer trigger below can dedupe.
- **`earnings-trade-analyzer`** — 5-factor scoring of recent post-earnings
  reactions (default lookback 2 days, top 20). Persist JSON output to
  `data/snapshots/<YYYY-MM-DD>/earnings-trade-analyzer/`. Used in Phase 5
  to surface PEAD candidates and as a sanity-check for any held position
  that just printed.

**Scenario trigger (after `market-news-analyst` writes `news_summary.json`):**
Scan `state/news_summary.json.top_events`. Fire the **`scenario-analyzer`**
skill on the first event matching ALL of:
- `impact == "high"`
- `direction in ("risk-off-margin", "binary")`
- `event_date` within the last 3 calendar days
- `topic_slug` NOT in `state/scenario_analyses_index.json` for the last 7 days

Pass the headline as the skill input. The skill writes
`reports/scenario_analysis_<topic_slug>_YYYYMMDD.md`. After it completes,
append an entry to `state/scenario_analyses_index.json`:
```jsonc
{"schema_version":"1.0","analyses":[
  {"topic_slug":"...", "trigger_event_date":"YYYY-MM-DD",
   "analyzed_at":"...Z", "report_path":"reports/scenario_analysis_..._YYYYMMDD.md"}
]}
```
If no event matches, skip silently. **At most one scenario fired per session.**

**Skipped at this cadence** (read from `state/regime_context.json` when fresh):
- `macro-regime-detector` (1-2 y horizon, weekly)
- `sector-analyst` (1-2 w horizon, weekly)
- `theme-detector` (1-2 w horizon, weekly)
- `us-market-bubble-detector` (weekly)

If `state/regime_context.json` is missing or stale (>8 days), call those skills
live this session and emit a warning. Tell the operator to run `/weekly-context`
this Sunday.

### 4. Synthesis — `exposure-coach`

Pass the four daily signal outputs **plus** the regime context (file or live).
Capture the Market Posture:

```jsonc
{
  "exposure_ceiling_pct": 60,
  "growth_value_tilt": "balanced",
  "new_entries_allowed": true,
  "cash_priority": false,
  "rationale": "breadth healthy, no top signal, ftd intact",
  "source": "exposure-coach"
}
```

If `exposure-coach` errors on schema mismatch, synthesize manually from raw
scores and set `source: "manual"`.

### 4b. Druckenmiller second opinion (~10 s)

```bash
python scripts/run_druckenmiller.py
```

Wraps the **`stanley-druckenmiller-investment`** skill's scoring + allocation
engine. Reads the 4 daily signal JSONs already produced this session
(`market-breadth-analyzer`, `uptrend-analyzer`, `market-top-detector`,
`ftd-detector`) plus the weekly `macro-regime-detector` output (from
`state/regime_context.json` if fresh, else the latest snapshot under
`data/snapshots/*/macro-regime-detector/`). Optional inputs (`vcp-screener`,
`theme-detector`, `canslim-screener`) are folded in when present.

Produces:
- `state/druckenmiller_view.json` — conviction score (0-100), pattern
  (Policy Pivot / Unsustainable Distortion / Extreme Sentiment Contrarian /
  Wait & Observe), target asset allocation, divergence vs exposure-coach.
- `data/snapshots/<DATE>/druckenmiller/druckenmiller_strategy_<ts>.{json,md}`.

**Divergence rule:** if
`|druckenmiller.target_allocation.equity_pct − posture.exposure_ceiling_pct| ≥ 20pp`,
the wrapper appends a `divergence_flag` to `posture.overlays[]` and bumps
`posture.conviction_floor` by +0.05 (capped 0.80). Exposure-coach remains
canonical for the ceiling — Druckenmiller is a macro-conviction sanity
check that tightens the bar for new entries when the two views disagree.

**On error / missing inputs:** wrapper warns and exits 0; post-close never
hard-fails on this phase. If `state/regime_context.json` is missing
(weekly never run), the wrapper falls back to today's macro snapshot with
`degraded:true`.

### 5. Draft candidate scout — three parallel paths (~2-3 min)

The momentum-rotator path is the canonical scout. PEAD is a complementary
post-earnings-momentum path. Weekly screens (vcp / canslim) feed a per-name
quality bonus to the conviction score.

**5a. Momentum rotator (canonical):**
```bash
python scripts/screen_candidates.py --mode=draft
```
Applies the 4 hard gates (RS_3M, RS_20D, trend, earnings window) plus
conviction scoring, **but skips** the percentile gate, the correlation prune,
and the sector caps. Output: top-30 superset at
`data/snapshots/<YYYY-MM-DD>/candidates_draft.json`.

**5b. PEAD candidates (parallel):**
Invoke the **`pead-screener`** skill in Mode B (consume the
`earnings-trade-analyzer` JSON from Phase 3). Output JSON at
`data/snapshots/<YYYY-MM-DD>/pead/`. PEAD candidates that are *also* in our
SP100 universe AND pass our trend gate are added to `candidates_draft.json`
under a new `pead_picks` key (alongside `draft`). Pre-open's final screen
treats them as candidates with a small conviction bonus (~0.05).

**5c. Weekly screens (read, not run):**
Read `state/weekly_screens.json` (produced by `/weekly-context` on Sunday).
For each name in `candidates_draft.draft`, look up whether it appears in
`weekly_screens.vcp_picks` or `weekly_screens.canslim_picks` and tag it.
The pre-open screen folds those tags into a small conviction bonus (~0.03
per tag, capped at 0.05 total). If the file is missing or stale, skip with
a warning — the rotator still works, just without the quality bonus.

The point: front-load the expensive bar pulls and signal computation tonight,
so the morning session refines a small superset against fresh quotes instead
of starting from scratch.

### 6. Persist research bundle (~5 s)

```bash
python scripts/write_research_bundle.py --posture-json='<posture from step 4>'
```

Writes `state/research_bundle.json` with `posture`, `draft_tickers`,
`signal_hashes`, and `valid_until = next-open ET`. The pre-open session's
preflight checks this file's freshness and aborts if missing or stale.

### 7. Write report + validate

Build `data/reports/<YYYY-MM-DD>-research.md` with:
- Account snapshot (equity, cash, position count)
- Signal layer summary (one line per skill)
- News headlines (from `news_summary.json`, top 5)
- Economic-calendar lookahead (next 7 days, high-impact only)
- Market Posture block (with any `posture.overlays[]` from breadth-veto and
  Druckenmiller divergence — show the conviction_floor evolution)
- **Druckenmiller second opinion**: from `state/druckenmiller_view.json`
  one-liner with `conviction_score`, `pattern`, `target_allocation.equity_pct`,
  divergence delta vs exposure-coach. Note `degraded:true` if applicable.
- **Active scenarios**: from `state/scenario_analyses_index.json`, list any
  analyses produced in the last 30 days with `topic_slug`, `trigger_event_date`,
  link to `report_path`. If any was fired this session, surface that prominently
  at the top of the section.
- Draft candidate table (top 10-20 by conviction; tags for pead/vcp/canslim hits)
- Open orders flagged from step 1
- "Tomorrow's pre-open should consider: …" notes

Then invoke **`data-quality-checker`** on the report file. It surfaces
warnings for price-scale mismatches, day-of-week errors, allocation totals
that don't sum, instrument notation. Advisory only — do not block on
warnings, but include the warning list in a "Data quality" section at the
bottom of the report so the operator sees them at 22:00 and again at 13:00.

### 8. STOP

**Do not place orders. Do not write `state/target_weights.json`. Do not
ask for `APPROVE`.** Those happen at 13:00 Madrid in `pre-open`.

---

## Hard rules

- Read-only towards the broker. No `POST /v2/orders`. No `DELETE /v2/orders`.
- Never write `state/target_weights.json`.
- Never write `data/snapshots/<date>/trade_plan.json`.
- Hooks are defense-in-depth — if anything Bash-style POST tries to fire,
  `market_hours_check.py` will block it (US is closed at 22:00 Madrid).

## Failure modes

| Scenario | Action |
|---|---|
| Skill errors mid-signal-layer | Continue with remaining signals; flag the gap in the research report. |
| `state/regime_context.json` stale (>8 d) | Warn; fall back to live macro/sector/theme calls. Note in report. |
| `exposure-coach` errors | Synthesize posture manually; mark `source: "manual"`. |
| `state/upcoming_earnings.json` missing | The pre-earnings hard gate becomes permissive; warn but continue. |
| Open orders found from a prior session | List them in the report. Do not cancel — that's pre-open's call. |

## Why this split

The legacy single-session `/preflight` ran post-close *and* placed MOO orders for
the next open. That meant the plan was built against 16-hour-stale prices and
acted on without seeing overnight earnings, gaps, or macro releases. The post-mortem
on the DE position made clear the plan needed fresh data. This session does
all the expensive analysis post-close so the morning is fast and decisive.
