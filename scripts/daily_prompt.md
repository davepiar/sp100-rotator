# Daily SP100 Rotation Session — Trigger Prompt

Fire this at **22:00 Europe/Madrid** (16:00 ET, 30 min after US close) on any trading day.

The assistant will run the full signal chain, present a plan, and **stop for your explicit approval** before placing any order. Hooks enforce: 5%/position, 10-position max, 30-trade turnover, 2% DD hard stop, SP100-only, and non-RTH → opg/cls conversion.

---

## The prompt

```
Daily SP100 rotation session — $(date +%Y-%m-%d). Paper account only.

== PHASE 1: PREFLIGHT ==
1. Load creds: `set -a; . /c/Users/thepi/.alpaca/credentials; set +a`.
2. Verify US market is closed (current ET should be >= 16:00). If open, abort.
3. GET /v2/account → record equity, cash, buying_power, last_equity.
   Compute intraday_dd = (equity - last_equity) / last_equity.
   If intraday_dd < -0.02, HARD STOP — write a red-flag report and exit.
4. GET /v2/positions → diff against state/portfolio.json.
   If any discrepancy in symbols or qty, LOG loudly and reconcile state/portfolio.json from Alpaca (source of truth = Alpaca).
5. GET /v2/orders?status=open → cancel any stale day orders from prior session
   (DELETE /v2/orders/{id}), note cancels in the report.

== PHASE 2: SIGNAL LAYER (save every output to data/snapshots/YYYY-MM-DD/<skill>/) ==
6. Invoke these skills in order and capture output:
   a. macro-regime-detector
   b. market-breadth-analyzer
   c. uptrend-analyzer
   d. market-top-detector
   e. ftd-detector
   f. sector-analyst
   g. theme-detector
   (institutional-flow-tracker dropped — FMP free-tier 402 on 13F endpoints,
    see CLAUDE.md "Out of scope")

== PHASE 3: SYNTHESIS ==
7. Invoke exposure-coach with the five regime signals (a, b, c, d, e) →
   produce Market Posture: net exposure % ceiling, growth/value tilt,
   new-entries-allowed (yes/no), cash-priority (yes/no).
8. Build the candidate universe:
   - Start: all 101 tickers in data/universe/sp100.csv
   - Filter 1: in one of the top-3 sectors from sector-analyst
   - Filter 2: 3M relative strength vs SPY > 0
     (inline calc: fetch 63 daily closes from Alpaca /v2/stocks/{sym}/bars
      vs SPY; RS_3M = (sym_return_63d - spy_return_63d))
   - Filter 3: current price > 50-day SMA (trend confirmation)
   - Filter 4 (earnings risk): skip any name with earnings in next 5 trading days.
     Cross-check via Alpaca news/calendar or a free earnings source; flag
     uncertain cases in the report. (earnings-trade-analyzer skill not yet imported.)
   - Rank by composite conviction (0-1):
     0.5 * RS_3M_zscore + 0.3 * sector_rank_score + 0.2 * theme_fit
   - Keep top 10.
9. Sector cap: no sector > 25% of target exposure. Trim to enforce.
10. Correlation prune: fetch 60 daily closes for the top-10 via Alpaca data API,
    compute pairwise correlation. For any pair > 0.85, drop the lower conviction.
    Final: 5 to 8 names.

== PHASE 4: POSITION SIZING ==
11. Invoke position-sizer per surviving name:
    - cap_pct = 0.05 (5% of equity, hard)
    - weight = conviction * (1 - vol_20d/vol_20d_median) clipped to [0.02, 0.05]
    - target_notional = weight * equity
    - qty = floor(target_notional / current_price)
12. Build state/target_weights.json with {symbol, target_weight, target_qty, current_qty, delta_qty, side, limit_price_mid_plus_5bp}.

== PHASE 5: PLAN + APPROVAL GATE ==
13. Diff target vs current positions → order list.
14. Print a Markdown table:
    | Ticker | Sector | Action | Current qty | Target qty | Δ qty | Limit px | Notional | Weight | Thesis (cite signals) |
    Plus a footer with total notional, post-trade cash %, exposure %, sector allocation breakdown.
15. Write data/reports/YYYY-MM-DD.md with: Market Posture, all signal outputs,
    full candidate list with rankings, pruned list, final plan table.
16. ⚠️ DO NOT PLACE ANY ORDERS. Print:
    "APPROVAL REQUIRED. Reply 'APPROVE' to execute exactly this plan, or
    'APPROVE with edits: <edits>' to modify, or 'REJECT' to cancel session."
    Wait for user reply.

== PHASE 6: EXECUTE (only after explicit APPROVE) ==
17. For each order in the approved plan:
    - Use limit orders at mid_price + 5bp (buys) or mid_price - 5bp (sells), TIF=opg
      (since we're running after close — orders will queue for next open).
    - POST /v2/orders with symbol, qty, side, type=limit, limit_price, time_in_force=opg.
    - The pre_order_check + market_hours_check + drawdown_guard hooks WILL fire.
      If a hook blocks, report which and why — do not retry the same order blind.
    - Append every request + response to state/trade_log.jsonl (one JSON per line).
18. Poll /v2/orders?status=all&limit=30 after session close the next day (or
    include a note in the report telling the user to check fills at tomorrow's close).

== PHASE 7: PERSIST + REPORT ==
19. Update state/portfolio.json from Alpaca /v2/positions + /v2/account.
20. Append to data/reports/YYYY-MM-DD.md:
    - Section "Execution": every order placed with response, fill summary.
    - Section "P/L": realized + unrealized vs prior session.
21. Print final summary: equity, cash %, position count, names added/trimmed,
    slippage vs limit, next session date.

== RULES (non-negotiable) ==
- Paper account only. Never change ALPACA_BASE_URL.
- Never echo ALPACA_SECRET_KEY or any credential in any output.
- Never place an order without explicit "APPROVE" reply.
- If any hook blocks an order: stop, report, do not loop-retry.
- Stay within the 101-ticker universe.
- Never short, never options, never crypto, never leveraged ETFs.
```

---

## Operational notes

- **Why TIF=opg (MOO)?** Session runs after close. Day orders would get rejected by market_hours_check. Opening auction avoids overnight gap risk of GTC.
- **Why limit at mid+5bp?** Balances fill probability vs paying the spread. Paper market fills are optimistic — real data will need monitoring.
- **Skill failure handling:** If any skill errors, fall back to manual synthesis using the other signals, and note the gap in the report. Don't skip the approval gate.
- **Budget:** Session is capped at 120 turns / $2 (see CLAUDE.md circuit breakers). If you're approaching either, stop and report.
- **Manual override:** You can interrupt at any phase. The approval gate (step 16) is the only *designed* stop; everything before it is diagnostic and can be aborted without state damage.

---

## How to fire it

Paste the entire fenced prompt above into a fresh Claude Code session in this repo. Or (future) wire it to `scripts/daily_session.sh` + a scheduler. For the first run, **always do it manually in an interactive session** — the approval gate is load-bearing.
