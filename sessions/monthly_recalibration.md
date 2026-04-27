# `monthly-recalibration` session

Runs on the **first Saturday of each month**, manually.
Purpose: review the prior month with fresh eyes, validate the strategy still
works, and tune the parameters in `state/strategy_params.json`.

**This session is intentionally not automated.** Tuning is a judgment call
informed by the data, not a deterministic rule. Automation would just
overfit.

---

## Inputs

| Path                                    | Required |
|---|---|
| `state/strategy_params.json` (prior version) | yes |
| `state/trade_log.jsonl` (last 30+ days)      | yes |
| `state/portfolio.json` history              | yes |
| `data/reports/<YYYY-MM-DD>.md` × 20+        | yes — daily reports for the month |
| `data/weekly/<YYYY-WXX>.md` × 4              | yes — weekly contexts |
| Alpaca account/portfolio history            | yes |

If any required input is sparse (e.g. <15 daily reports), abort and surface
the gap. Tuning on incomplete data is worse than not tuning.

---

## Phases

### 1. Performance summary (~10 min)

For the prior month, compute:

| Metric | How |
|---|---|
| Total return | `(equity_end / equity_start) - 1` |
| Benchmark spread | vs SPY total return same window |
| Sharpe (daily) | sqrt(252) × mean(daily_ret) / stdev(daily_ret) |
| Max drawdown | peak-to-trough on daily equity series |
| Hit rate | wins / (wins + losses) on closed positions |
| Avg win / avg loss | $ basis from `trade_log.jsonl` |
| Turnover | trades / month |
| Sector concentration over time | weighted avg of per-snapshot dispersion |

Save to `data/monthly/<YYYY-MM>/performance.json`.

### 2. Replay-style audit + signal postmortem (~15-20 min)

Re-run `scripts/replay_gates.py` against the month's actual buys (extending
the `BUYS` list in that script, or factoring it into a CLI flag — Phase 4
work). For each buy, ask:

- Did all gates pass at trade-time?
- Did any gate fire but get overridden by manual intervention? (look at
  `data/snapshots/<date>/candidates.json` for the day's `blocked_*` fields,
  and at `data/reports/<date>.md` for any APPROVE-with-edits the operator typed)
- For closed positions, did the stop fire at the configured 8%? Was 8% the
  right number for that name's vol?

Names that *passed all gates* and still lost money are the highest-value
data points. Cluster them. If there's a pattern (always-Friday entries,
always after a 3-day rally, etc.), it's a candidate for a new gate.

**Mechanical postmortem (Phase 5b — `signal-postmortem` skill):**

Invoke the **`signal-postmortem`** skill, feeding it:
- `state/trade_log.jsonl` for the month's positions
- `state/postmortem_outcomes.jsonl` (append-only; closed positions get a
  row with which signals fired at entry, MAE/MFE, exit reason, hold days)
- The month's daily `candidates.json` snapshots so the skill can attribute
  each name's entry to which signals triggered it

The skill produces per-signal grades — false-positive rate (signal fired,
trade lost), false-negative rate (no signal, would've won), regime
mismatches (signal optimized for one regime, this month was a different
regime). Persist to `data/monthly/<YYYY-MM>/postmortem.json`.

Use the grades as input to Phase 3's parameter review — if `rs_3m_floor`
has been firing 18 times this month and 14 of those still lost money,
that's a signal to either tighten the floor or remove the gate. The skill
doesn't decide; it surfaces evidence.

### 3. Parameter review (~20 min)

Walk through `state/strategy_params.json.tunable` and ask:

- **Did this parameter fire at all?** (Count blocks attributable to it.)
- **Did it have false positives?** (Names blocked that turned out to win.)
- **Did it have false negatives?** (Losers that passed.)
- **Sensitivity analysis:** for each tunable, run the month's screen with
  the value perturbed +1 / +5 / +10% in each direction. Use `backtest-expert`
  skill for the mechanics ("beat the idea to death" methodology).

Tunables and their typical adjustment direction:

| Parameter | Loosen if... | Tighten if... |
|---|---|---|
| `rs_3m_hard_floor` | too few candidates surviving | more chronic underperformers slipping through |
| `rs_20d_hard_floor` | blocking eventual winners with mild weakness | DE-shaped names slipping through |
| `rs_3m_pct_floor` | blocking too many | bottom-quartile names slipping through |
| `trend_bear_cross_tolerance` | blocking TXN-style names with tiny crosses | DE-style real breakdowns slipping through |
| `earnings_blocker_days` | blocking too many viable PEAD setups | post-earnings gap-downs slipping through |
| `stop_loss_pct` | stopping out winners on noise | letting losers run too far |
| `take_profit_pct` | leaving too much on the table | (rarely a problem) |

Each adjustment must come with a one-sentence rationale tied to data, not
intuition.

### 4. Universe audit (~5 min)

`data/universe/sp100.csv` was last refreshed when? If >3 months ago, refresh.
S&P 100 composition changes ~quarterly. Names that left the index should be
removed; new entrants added. The `_doc` field in the manual.json could help
track manual additions but the canonical refresh is from S&P / Wikipedia.

### 5. Write the new `state/strategy_params.json`

Bump `generated_at`, extend `valid_until` by 35 days, add a record to
`monthly_review_history`:

```jsonc
{
  "review_date": "2026-05-02",
  "review_window": "2026-04-01..2026-04-30",
  "performance_summary": { "total_return": 0.0223, ... },
  "changes": [
    {"path": "tunable.screen_candidates.rs_20d_hard_floor",
     "from": -0.10, "to": -0.12, "rationale": "..."},
    ...
  ]
}
```

Untouched parameters need no entry. The history is append-only — you can
trace every tune back to its month and rationale.

### 6. Write the report + validate

`data/monthly/<YYYY-MM>.md` with:
- Performance table
- "What worked" / "What didn't"
- Per-signal grades from `signal-postmortem` (top 5 worst FP-rate signals)
- Parameter changes (table: before → after, rationale)
- Open questions for next month

Then invoke **`data-quality-checker`** on the monthly report. The monthly
is the most-scrutinized artifact — DQC catches the kind of arithmetic and
date errors that would otherwise propagate into next month's parameter
decisions.

---

## Hard rules

- Never changes `fixed` parameters here. Those require an architectural
  decision and a CLAUDE.md update.
- Never deletes history. `monthly_review_history` only grows.
- Never tunes more than ~3 parameters per session. If you find yourself
  wanting to change 8 things, the strategy is broken — back to the
  drawing board.
- Always uses the same look-back window length month over month
  (default: prior calendar month). Mixing windows compounds noise.

## Tools

- `backtest-expert` skill — "beating ideas to death" methodology, slippage
  modeling, robustness testing, bias prevention. Phase 3 starting point.
- `signal-postmortem` skill — Phase 2 mechanical evidence (FP/FN per signal,
  regime mismatches). Reads `state/postmortem_outcomes.jsonl`.
- `data-quality-checker` — Phase 6 report validation (advisory).

## Failure modes

| Scenario | Action |
|---|---|
| <15 daily reports for the month | Abort. Note the gap in next month's monthly. |
| Parameter sensitivity says current value is *clearly* wrong | Update; add 2-line rationale. |
| Sensitivity says any value within ±20% works equally well | Don't tune. Note "noise floor" in history. |
| One bad day dominates the month | Re-run excluding it. Decide whether the day was structural or one-off. |
