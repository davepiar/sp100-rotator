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
| `state/evening_research.json` | `post-close` (last night) | yes — abort if missing or `valid_until` < now |
| `state/portfolio.json` | `post-close` reconcile | yes |
| `state/strategy_params.json` | `monthly-recalibration` | yes (defaults if missing) |
| `state/upcoming_earnings.json` | `post-close` | yes — read by the screen's earnings gate |
| `state/economic_calendar.json` | `post-close` | yes — read for the macro kill-switch in Phase 2 |
| Alpaca + FMP credentials | `~/.alpaca/credentials` | yes |

If `evening_research.json` is missing or stale, **abort** with
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

- Freshness check on `state/evening_research.json`. Abort if missing or stale.
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

### 3. Final candidate screen (~1-2 min)

```bash
python scripts/screen_candidates.py --mode=final
```

This applies all 5 hard gates against today's bars (re-fetched), runs the
correlation prune + sector + sub-sector caps, and folds in the premarket-flag
gate from `state/premarket_data.json` (any candidate with overnight earnings
+ adverse gap → drop; |gap| > 5 % → soft-flag for the report). Output:
`data/snapshots/<YYYY-MM-DD>/candidates.json`.

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
| `state/evening_research.json` missing | Abort. Tell operator to run `/post-close` first. |
| `state/evening_research.json` stale (`valid_until < now`) | Abort with same message. |
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
