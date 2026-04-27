# `weekly-context` session

Runs **Sunday 20:00 Europe/Madrid** (before Asian futures open Monday).
Purpose: refresh long-horizon signals once per week so `evening-planning`
doesn't re-spend tokens on them every day.

Output: `state/regime_context.json`, valid 7 days. The evening session reads
this file; if it's missing or stale (>8 days old), evening-planning falls back
to live skill calls and emits a warning.

---

## What "long-horizon" means here

| Skill | Cadence | Why moving out of daily |
|---|---|---|
| `macro-regime-detector`  | weekly | Cross-asset ratios shift on a 1-2 year horizon; daily moves are noise. |
| `sector-analyst`         | weekly | Sector rotation moves on 1-2 week timescales. |
| `theme-detector`         | weekly | Narrative themes shift slowly; daily reruns waste tokens. |
| `us-market-bubble-detector` | weekly | Minsky/Kindleberger framework — quarterly-at-best signal, weekly check is generous. |
| `market-breadth-analyzer` (weekly view) | weekly + daily | Run weekly for context; daily already runs in evening-planning for tactical view. |

Daily-cadence skills (breadth, uptrend, top-detector, ftd-detector,
exposure-coach, position-sizer) stay in `evening-planning` and are NOT called
from this session.

---

## Inputs

| Path                              | Required |
|---|---|
| FMP / Alpaca / TraderMonty CSV access (no paid keys) | yes |
| `state/regime_context.json` (prior week, for diff) | optional |

No state-file inputs from prior `evening-planning` sessions — this is the
upstream feed.

---

## Phases

### 1. Macro layer (~3 min)

```
Skill: macro-regime-detector
```
Output: classification across {Concentration, Broadening, Contraction,
Inflationary, Transitional}. Save raw JSON to `data/weekly/<YYYY-WXX>/macro/`.

### 2. Sector + theme layer (~3 min)

```
Skill: sector-analyst        (weekly view, full output)
Skill: theme-detector        (weekly narrative scan)
```
If `theme-detector` requires FINVIZ Elite (paid) and that's not available,
skip it and note the gap. Other signals carry the load.

### 3. Bubble layer (~2 min)

```
Skill: us-market-bubble-detector
```
Use the `--quick` mode if available — full Minsky/Kindleberger walkthrough
is overkill at weekly cadence; we want the score and zone, not the essay.
Save the score (0-12 typically) and the zone (`Normal` / `Elevated` /
`Speculative` / `Bubble`) into `regime_context.json`.

### 4. Breadth (weekly view) (~1 min)

```
Skill: market-breadth-analyzer
```
Same skill as in `post-close`, but here we capture the 6-component score
with historical context (last N weeks) for the report. Daily reruns reuse
the already-fetched CSVs.

### 4b. Weekly entry-quality screens (~3-5 min, FMP-heavy)

These run once a week because each consumes ~250-300 FMP calls per run.
Outputs feed `state/weekly_screens.json`, which `post-close` reads as
per-name conviction bonuses.

```
Skill: vcp-screener
```
Default invocation (top-100 candidates). Tight Stage-2 bases with
contracting volatility near pivot points. Persist tickers + grades to
`weekly_screens.vcp_picks`.

```
Skill: canslim-screener --max-candidates 35
```
**Important:** the `--max-candidates 35` flag is mandatory on FMP free
tier (skill default is 40+ which exceeds 250 calls/day). Persist tickers
+ grades to `weekly_screens.canslim_picks`.

If both run successfully, write `state/weekly_screens.json`:

```jsonc
{
  "schema_version": "1.0",
  "generated_at": "...",
  "valid_until": "+7d",
  "vcp_picks":     [{"ticker": "...", "grade": "A", "score": 0-100, "stage": 2, "base_quality": "..."}, ...],
  "canslim_picks": [{"ticker": "...", "grade": "A", "score": 0-100, "rs_rating": ..., "eps_growth": ...}, ...],
  "produced_by": "weekly-context"
}
```

If either screen fails (FMP rate limit, transient error), write the
partial file with the working list and an `errors` array. `post-close`
treats missing keys as "no bonus available" — the rotator still works.

### 5. Synthesis — write `state/regime_context.json`

```jsonc
{
  "schema_version": "1.0",
  "generated_at":   "2026-04-26T18:00:00Z",
  "valid_until":    "2026-05-03T18:00:00Z",
  "macro_regime": {
    "primary": "Broadening",          // one of the five
    "confidence": "medium",
    "transition_risk": 0.30
  },
  "sector_rotation": {
    "leaders": ["Industrials", "Materials"],
    "laggards": ["Communication Services"],
    "rotation_score": 0.42
  },
  "themes": {
    "bullish": ["AI infrastructure", "industrial reshoring"],
    "bearish": ["consumer discretionary", "long-duration treasuries"],
    "lifecycle": "early-mid"
  },
  "bubble": {
    "score": 5,
    "zone": "Elevated",
    "drivers": ["margin debt up", "VIX compressed"]
  },
  "breadth": {
    "composite_0_100": 64,
    "trend_4w": "improving"
  }
}
```

### 6. Report + validate

Write `data/weekly/<YYYY-WXX>.md` with:
- Each layer's headline number + 2-line summary
- Diff vs prior week's `regime_context.json` (which signals moved, by how much)
- Top vcp + canslim picks (5 each, with grades)
- Implications for `exposure-coach` (whether to nudge ceiling up/down)

This is a *report for the operator*, not a decision artifact. The
machine-readable `regime_context.json` and `weekly_screens.json` are.

Then invoke **`data-quality-checker`** on the weekly report file. Same
advisory contract as the daily reports — warnings get a "Data quality"
section appended.

---

## How `evening-planning` uses this

`exposure-coach` reads `state/regime_context.json` as additional input. The
Druckenmiller tilt: **macro mans over tactic**. If `macro_regime` is
`Contraction` or `Transitional`, drop the exposure ceiling one notch below
what the tactical signals alone would suggest. If `bubble.zone` is
`Speculative` or `Bubble`, force `cash_priority=true` regardless of breadth.

If `state/regime_context.json` is older than 8 days, evening-planning runs
the macro/sector/theme skills live (with a warning) and tells the operator
to run `weekly-context` ASAP.

---

## Hard rules

- Read-only — never places orders, never proposes a plan.
- Never overrides a tactical buy/sell decision; this session shapes the
  *envelope* (exposure ceiling, cash priority), not the picks.
- One run per week. If it's run twice, the second run overwrites without
  conflict — `valid_until` extends.

## Future hardening

- `scenario-analyzer` — optional 18-month projections for the
  `monthly-recalibration` session. Punt unless we observe value in the
  monthly review.
- Cross-asset feed (DXY, oil, 10y, copper) — currently the macro skill
  fetches what it needs from Yahoo. If TraderMonty CSVs go stale, switch
  to a paid feed.
- `vcp-screener` `--full-sp500` mode requires FMP paid tier; we run on
  SP100 so default is sufficient. If the universe ever expands beyond SP100,
  upgrade FMP to Starter ($30/mo, 750 calls/day).
