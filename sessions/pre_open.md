# `pre-open` session — propose, approve, execute

Runs **13:00 Europe/Madrid** (~2 h 30 min before US open).
Purpose: re-screen with fresh pre-market data, build the plan, get APPROVAL,
place MOO orders.

**Hard cutoff: 15:25 Madrid (5 min before open).** If `APPROVE` hasn't arrived
by then, abort and skip the day. Do not place orders against an unapproved plan.

The `.claude/hooks/morning_window.py` PreToolUse hook enforces the time window
on writes to `state/target_weights.json` (the canonical "I'm about to trade"
artifact).

---

## Inputs (must exist and be fresh)

| Path | Producer | Required |
|---|---|---|
| `state/research_bundle.json` | `post-close` (last night) | yes — abort if missing or `valid_until` < now |
| `state/portfolio.json` | `post-close` reconcile | yes |
| `state/strategy_params.json` | `monthly-recalibration` | yes (defaults if missing) |
| `state/upcoming_earnings.json` | `post-close` | yes — read by the screen's earnings gate |
| `state/economic_calendar.json` | `post-close` | yes — read for the macro kill-switch in Phase 2 |
| `state/druckenmiller_view.json` | `post-close` Phase 4b | optional — surfaced in the pre-open report context band |
| `state/scenario_analyses_index.json` | `post-close` Phase 3 (when triggered) | optional — surfaced in the report appendix |
| Alpaca + FMP credentials | `~/.alpaca/credentials` | yes |

If `research_bundle.json` is missing or stale, **abort** with
"run `/post-close` first to refresh the handoff bundle, or run
`scripts/screen_candidates.py --mode=final` directly to bypass the gate
(advanced — only when the operator knows the post-close was skipped for a
known reason such as a holiday).

---

## Phases

### 1. Preflight-lite (~30 s)

```bash
set -a; . /c/Users/dpineda/.alpaca/credentials; set +a
```

- Freshness check on `state/research_bundle.json`. Abort if missing or stale.
- `GET /v2/account` → fresh equity, cash, buying_power, drawdown vs last_equity.
  If intraday DD < −2 %, **HARD STOP**.
- `GET /v2/orders?status=open` → list any open orders (e.g. unfilled GTCs from
  prior sessions). **Ask the operator** before cancelling. Do not auto-cancel.

### 2. Pre-market data fetch + macro kill-switch (~1 min)

```bash
python scripts/premarket_check.py
```

Writes `state/premarket_data.json` with SPY/QQQ pre-market gaps, UVXY proxy,
overnight earnings prints, per-symbol gaps for every ticker in the prior
post-close's draft list, plus a `flags` array of threshold-breaches.

**Macro kill-switch (Phase 5b addition):**
Read `state/economic_calendar.json`. For any event with `impact == "high"`
and `time_et` within ±2h of today's 09:30 ET open:

| Trigger | Decision |
|---|---|
| Event has actual + consensus, surprise > ±0.5σ (or no consensus published) | Set `kill_switch_marginal_buys=True` for the next phases. |
| Event has not released yet (pre-open) | Set `defer_marginal_buys=True` (proceed with high-conviction names only, drop `conviction < 0.55`). |

Both modes drop low-conviction BUYs from the plan but keep SELLs intact.
Document the decision in the report. If no high-impact events match, no-op.

If `state/economic_calendar.json` is missing or stale, log a warning and
proceed without the kill-switch — but flag this as a hole in the report.

### 2.5. News delta refresh (~10-90 s)

```bash
python scripts/fetch_news_delta.py
```

Pulls FMP `/stable/news/stock` for every draft ticker plus every currently-held
position, filtering to publish times **after** `state/news_summary.json.generated_at`
(set by post-close last night). Merges new items into `top_events[]`, bumps
`generated_at`, and emits per-ticker `news_headline` flags into
`state/premarket_data.json.flags`.

Per-item classification (deterministic keyword heuristic, no LLM):
- High impact + bullish keywords (`beat`, `tops`, `upgrade`, `breakout`, …) → `high / risk-on`.
- High impact + bearish keywords (`miss`, `downgrade`, `lawsuit`, `recall`, `bankruptcy`, …) → `high / risk-off-margin`.
- Anything else → `medium / neutral` (default).

Phase 3 reads these flags. Behavior:
| flag | screen action |
|---|---|
| `impact == "high" AND direction in ("risk-off-margin","binary")` | **soft-block**, drop from candidates with reason `news_flag: …` |
| any other news flag | keep, attach `news_thesis_tags` for `build_diff_plan` to surface in the order's thesis |

Soft-block = visible in the report; operator can `APPROVE with edits: include <ticker>`.

**Budget:** 1 FMP call per unique ticker (draft ∪ held), wall-clock target ≤ 90 s.
Free-tier `/stable/news/stock` is generous; this consumes ~12-15 calls per session.

**On error or timeout:** the script emits warnings, returns 0, and pre-open continues
without news flags. Same advisory-degrade pattern as the missing economic_calendar fallback.

### 2.6. VCP screener — pre-breakout candidates (~1-2 min)

```bash
python .claude/skills/vcp-screener/scripts/screen_vcp.py \
  --output-dir data/snapshots/$(date +%F)/vcp \
  --max-candidates 60 --top 15
```

Pulls FMP price history for the SP100 universe and applies Mark Minervini's
Volatility Contraction Pattern (VCP) detection. Output is consumed by Phase 3.5
(breakout-trade-planner). FMP free-tier budget: ~80 calls per run on SP100 with
the cap above; combined with `fetch_news_delta` (~15 calls) this still leaves
margin for `screen_candidates` (~50) and `premarket_check` (~30).

If FMP is rate-limited or VCP fails: warn-and-continue. Phase 3.5 will see no
input and skip.

### 2.7. Breadth-chart pre-market regime veto (~5 s)

```bash
python scripts/breadth_chart_veto.py
```

Wraps the `breadth-chart-analyst` skill's CSV fetcher. Reads public breadth data
(no API key) and produces a verdict:

| Verdict | Trigger | Effect on `posture.conviction_floor` |
|---|---|---|
| 🟢 GREEN  | breadth_200ma ≥ 60 AND no dead_cross AND uptrend ≠ RED | unchanged |
| 🟡 YELLOW | dead_cross OR breadth_200ma 50–60 OR uptrend RED      | +0.05 (capped 0.80) |
| 🔴 RED    | breadth_200ma < 50 OR (dead_cross AND uptrend RED)    | +0.10 (capped 0.80) |

Verdict + overlay are persisted to:
- `data/snapshots/<DATE>/breadth-chart-analyst/breadth_verdict_<ts>.json`
- `state/research_bundle.json.posture.conviction_floor` (only on YELLOW/RED)
  + `state/research_bundle.json.posture.overlays[]` (audit trail)

**On error or timeout:** warn-and-continue, no posture mutation. Pre-open never hard-fails.

### 2.8. Global market-environment overlay (~2-3 min, soft-budgeted)

Purpose: capture overnight Asia close, Europe pre-market, FX/commodities
moves, and VIX level so the final screen acts on truly fresh global context
— not just yesterday's US-close synthesis.

**Step A — Invoke the skill.** Call **`market-environment-analysis`** with
WebSearch budget capped at ~3 minutes. Write the human-readable Markdown to
`data/snapshots/<DATE>/market-environment-analysis/` AND a structured
summary to `state/market_environment.json` matching this schema:
```jsonc
{
  "schema_version": "1.0",
  "generated_at": "...Z",
  "valid_until": "...Z (+8h)",
  "session_date": "YYYY-MM-DD",
  "regime": "risk-on" | "risk-off" | "neutral",
  "vix": {"level": 16.2, "class": "calm|normal|elevated|stressed"},
  "indices_overnight": {"nikkei":{...}, "hsi":{...}, "stoxx":{...},
                         "sp500_futures":{...}, "ndx_futures":{...}},
  "fx": {"dxy":..., "eurusd":..., "usdjpy":...},
  "commodities": {"wti":..., "gold":...},
  "yields": {"us2y":..., "us10y":..., "curve_2s10s":...},
  "narrative": "≤300 chars",
  "degraded": false,
  "source": "market-environment-analysis (WebSearch)"
}
```
If WebSearch errors out, hits the soft 3-min budget, or returns partial data:
write the file with `degraded:true` and best-effort fields. Do NOT block.

**Step B — Apply the posture overlay.**
```bash
python scripts/run_market_environment.py
```
Reads `state/market_environment.json`. If `regime == "risk-off"` AND
`vix.class in ("elevated", "stressed")`, bumps
`state/research_bundle.json.posture.conviction_floor` by +0.05 (capped 0.80)
and appends a record to `posture.overlays[]`. Otherwise informational only.

If the JSON is missing, stale (past `valid_until`), or `degraded:true`, the
wrapper warns and exits 0 with no overlay. Pre-open never hard-fails on this
phase. Same advisory-degrade pattern as `breadth_chart_veto.py`.

### 3. Final candidate screen (~1-2 min)

```bash
python scripts/screen_candidates.py --mode=final
```

This applies all 5 hard gates against today's bars (re-fetched), runs the
correlation prune + sector + sub-sector caps, and folds in the premarket-flag
gate from `state/premarket_data.json`:

- **Gap flag:** overnight earnings + adverse gap < −3 % → drop; |gap| > 5 % → soft-flag.
- **News flag (Phase 2.5 fold-in):** high-impact + risk-off-margin/binary news → drop with reason `news_flag: …`; other news headlines flow through as `news_thesis_tags` on the order.

Output: `data/snapshots/<YYYY-MM-DD>/candidates.json` with new keys
`blocked_by_news` and `news_soft_flagged` alongside the existing gap-blocked lists.

### 3.5. Breakout entry refinement (~30 s, dry-run by default)

```bash
python scripts/run_breakout_planner.py
```

Reads the latest VCP screener JSON (Phase 2.6) and runs `breakout-trade-planner`
to compute Minervini-style worst-case-risk entries for VCP candidates. Output
saved to `data/snapshots/<DATE>/breakout/`.

**Dry-run default.** Behavior gated by `state/strategy_params.json.tunable.
execute_plan.breakout_planner_active` (default `false`). When `false`, the
planner's output is informational only — `build_diff_plan` still uses the
standard `mid + 5bp` offset for limits.

To activate after ≥3 sessions of dry-run review: flip the flag to `true` and
extend `build_diff_plan.py` to override limit prices for tickers that appear in
both the rotator's picks AND the breakout-planner's actionable list. (That
wiring is intentionally deferred — see plan doc for rationale.)

### 4. Build the plan (~10 s)

```bash
python scripts/build_diff_plan.py
```

Writes `state/target_weights.json` (with `signed_off=False`) and
`data/snapshots/<YYYY-MM-DD>/trade_plan.json`.

The write to `target_weights.json` is gated by `.claude/hooks/morning_window.py`
— inside 13:00-15:25 Madrid only.

### 5. Present plan + STOP for `APPROVE`

Print a Markdown table:

| Action | Ticker | Sector | Qty | Limit $ | Notional | Weight | Conviction | Thesis |

Plus: post-trade cash %, sector breakdown, total notional, sector caps headroom.

Append a **Context band** above the trade table summarising what the operator
is acting against:
- `state/market_environment.json` → regime + VIX level (note staleness if any).
- `state/druckenmiller_view.json` → conviction zone, pattern, target equity %,
  divergence delta vs exposure-coach (from the `divergence_vs_exposure_coach`
  field). One short line — full report is in `data/snapshots/<DATE>/druckenmiller/`.
- `state/scenario_analyses_index.json` → list of scenarios from the last 30
  days (topic_slug + report path). Read-only context; surfaces the
  long-horizon framing the operator may want to skim before approving.

Write `data/reports/<YYYY-MM-DD>.md` so the operator sees it both in chat and
on disk.

Wait for one of:
- `APPROVE` → flip `target_weights.json.signed_off=True`, proceed to step 6.
- `APPROVE with edits: …` → apply edits, re-write `target_weights.json` (still
  inside the morning-window hook), then proceed.
- `REJECT` → close session, no orders.
- No reply by 15:25 Madrid → abort with "no APPROVE before cutoff".

### 6. Execute (only after APPROVE)

```bash
python scripts/execute_plan.py
```

- Refuses to run if `target_weights.json.signed_off=False`.
- No-chase guard runs first: any symbol with an existing open order at Alpaca
  is skipped and logged.
- Each BUY is a `bracket` order at limit price (mid + 5 bp) with stop-loss at
  entry × `(1 − 0.08)` and take-profit at entry × `(1 + 0.25)`. TIF = `gtc`
  (Alpaca constrains bracket parents to `day` or `gtc`; `gtc` lets the order
  queue pre-open and survive intraday if unfilled, which we want for these
  near-quote limits). The runbook should cancel any still-open bracket BUYs
  in the next pre-open's preflight if they failed to fill — see Phase 1.
- SELLs are simple limits, TIF = `gtc`. Same cleanup expectation.
- Every request and response is appended to `state/trade_log.jsonl`.

### 7. Persist + report + validate

- Update `state/portfolio.json` from Alpaca `/v2/positions` + `/v2/account`.
- Append execution-summary section to `data/reports/<YYYY-MM-DD>.md`:
  fills, slippage, remaining open orders, P/L delta vs prior session.
- Invoke **`data-quality-checker`** on the final report. Same advisory
  contract as post-close: warnings get a "Data quality" section appended,
  not a block. Particularly important here because the report is the only
  place we record what we executed against what we proposed.

---

## Hard rules

- **Paper account only.** Never change `ALPACA_BASE_URL`.
- **Never echo `ALPACA_SECRET_KEY`.**
- **Never place an order without explicit `APPROVE`.**
- Hooks are defense-in-depth — if blocked, stop and report. Do not loop-retry.
- Stay within the SP100 universe.
- No shorts, no options, no crypto, no leveraged ETFs.

## Circuit breakers

- `--max-turns 120`
- `--max-budget-usd 2.00`
- `pre_order_check.py`: 5 % notional cap, 30-trade turnover, 10-position cap.
- `market_hours_check.py`: blocks `day`/`ioc`/`fok` outside RTH (we use `opg`).
- `drawdown_guard.py`: HARD-BLOCK orders if intraday DD < −2 %.
- `morning_window.py`: blocks `state/target_weights.json` writes outside 13:00-15:25 Madrid.

## Failure handling

| Scenario | Action |
|---|---|
| `state/research_bundle.json` missing | Abort. Tell operator to run `/post-close` first. |
| `state/research_bundle.json` stale (`valid_until < now`) | Abort with same message. |
| `state/economic_calendar.json` missing or stale | Warn, skip the kill-switch phase, flag in the report. |
| `state/premarket_data.json` write fails | Continue without premarket-flag gate; warn loudly in report. |
| Skill / API errors during screen | If recoverable, retry once. Otherwise abort. |
| Hook blocks an order | Stop, report which hook + reason. Don't retry blind. |
| No `APPROVE` before 15:25 Madrid | Abort. Skip the day. Notify operator. |
| Alpaca 4xx/5xx | Surface JSON `message`, suggest a fix. Don't retry without operator. |

## How this differs from the legacy `/preflight`

- `/preflight` placed orders post-close (16:30 ET) using GTC limits at then-stale
  prices. Open the next morning could differ substantially; we observed this
  with DE losing 5 % overnight on idiosyncratic news. The price we paid was
  effectively the open, not our 16:30 limit.
- `pre-open` places MOO orders at 13:00 Madrid (08:00 ET) — orders fill at
  the opening auction with full pre-market context. Same execution latency,
  much fresher data.
- The decision artifact is identical (`state/target_weights.json`). Only the
  authoring time changes.
