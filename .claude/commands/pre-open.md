---
description: Pre-open trading session — re-screen with fresh data, build plan, get APPROVAL, place MOO orders.
allowed-tools: Bash, Read, Write, Edit, Skill, TodoWrite
---

Pre-open SP100 trading session — !`date +%Y-%m-%d`. Paper account only.

Read `sessions/pre_open.md` and execute it phase by phase. **Hard cutoff:
15:25 Europe/Madrid (5 min before US open). If `APPROVE` hasn't arrived by
then, abort and skip the day.**

The `.claude/hooks/morning_window.py` PreToolUse hook will refuse any write
to `state/target_weights.json` outside 13:00-15:25 Madrid.

Quick recap of phases:

1. **Preflight-lite** — load creds, freshness-check `state/evening_research.json`
   (abort if missing or `valid_until < now`; tell operator to run `/post-close`
   first), snapshot account, list open orders. Ask operator before cancelling
   any open order. Do NOT auto-cancel.
2. **Pre-market data + macro kill-switch** — `python scripts/premarket_check.py`
   writes `state/premarket_data.json`. Then read `state/economic_calendar.json`:
   for any high-impact event within ±2h of today's 09:30 ET open, set the
   appropriate kill-switch (`kill_switch_marginal_buys` if released with
   surprise > ±0.5σ; `defer_marginal_buys` if pre-release). Both modes drop
   `conviction < 0.55` BUYs from the plan but keep SELLs. Document the
   decision in the report.
3. **Final candidate screen** — `python scripts/screen_candidates.py --mode=final`.
   Re-fetches today's bars, applies all hard gates (RS_3M, RS_20D, trend,
   earnings window, RS percentile, premarket-flag fold-in), correlation prune,
   sector + sub-sector caps. Outputs `data/snapshots/<date>/candidates.json`.
4. **Build plan** — `python scripts/build_diff_plan.py`. Writes
   `state/target_weights.json` (`signed_off=False`) and
   `data/snapshots/<date>/trade_plan.json`. The write to `target_weights.json`
   is gated by `morning_window.py` to the 13:00-15:25 Madrid window.
5. **Present plan** — Markdown table (action, ticker, sector, qty, limit $,
   notional, weight, conviction, thesis) plus post-trade cash %, sector
   breakdown, total notional. Write `data/reports/<date>.md`.
6. **STOP for `APPROVE`.** Wait for one of:
   - `APPROVE` → flip `target_weights.json.signed_off=True`, proceed.
   - `APPROVE with edits: …` → apply, re-write `target_weights.json`, proceed.
   - `REJECT` → close session, no orders.
   - No reply by 15:25 Madrid → abort.
7. **Execute** — `python scripts/execute_plan.py`. Bracket buys (stop-loss at
   entry × 0.92, take-profit at entry × 1.25), no-chase guard, TIF=`gtc`
   (Alpaca constrains bracket parents to `day`/`gtc`; `gtc` queues pre-open
   cleanly and persists if unfilled — next pre-open's preflight will cancel
   stale ones). SELLs are simple limits, TIF=`gtc`. Every request/response
   appended to `state/trade_log.jsonl`.
8. **Persist + report + validate** — update `state/portfolio.json`, append
   execution-summary section to `data/reports/<date>.md` (fills, slippage,
   remaining open orders, P/L delta vs prior session). Then invoke
   `data-quality-checker` on the report; warnings get a "Data quality"
   section appended (advisory, not a block).

Hard rules: paper only, never echo `ALPACA_SECRET_KEY`, never place an order
without `APPROVE`, hooks are defense-in-depth (`pre_order_check.py`,
`market_hours_check.py`, `drawdown_guard.py`, `morning_window.py`), SP100
universe only, no shorts/options/crypto/leveraged ETFs.

Circuit breakers: --max-turns 120, --max-budget-usd 2.00, intraday DD < −2 %
HARD STOP, 30-trade turnover cap, 10-position cap, 5 % notional per position.

If `state/evening_research.json` is missing or stale: abort, tell the operator
to run `/post-close` first. Don't try to limp along with a fresh full screen
unless the operator explicitly confirms post-close was skipped for a known
reason (holiday, error).
