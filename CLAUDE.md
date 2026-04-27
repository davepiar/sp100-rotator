# Trading Agent — Project Guide

Daily automated portfolio-rotation bot over the S&P 100. One session per day after US close (22:00 Europe/Madrid). Claude synthesizes signals → picks 5–10 names → rebalances via Alpaca **paper** trading.

## Non-negotiables

- **Paper account only.** Live trading is explicitly out of scope. Never switch `ALPACA_BASE_URL` to `https://api.alpaca.markets` without an explicit, written instruction from the user in-session. Even then, ask a second time.
- **Never echo `ALPACA_SECRET_KEY`** or any credential in tool output, logs, commit messages, or notebook cells.
- **Confirm every order-placing / order-canceling action in chat before executing**, even in automated runs — the orchestrator must present a trade plan, and the user (or a hook) must approve it.
- **S&P 100 universe only.** No penny stocks, no OTC, no leveraged ETFs, no options, no crypto.
- **Risk limits are enforced by hooks, not prompts.** If a hook blocks, do not try to work around it — fix the underlying plan.

## Risk limits (enforced by PreToolUse hooks)

| Rule | Limit |
|---|---|
| Max position size | 5% of equity |
| Max single-day drawdown | 2% of equity |
| Max positions | 10 |
| Min positions when deployed | 5 |
| Max turnover per session | 30 trades |
| Allowed order types | market, limit, stop (stop is sell-only — defensive) (day/gtc) |
| Allowed sides | buy, sell (no shorts in v1) |
| Trading window | Only place orders when US market is open OR as MOO/LOO for next open |

Hooks live in `.claude/hooks/` and are registered in `.claude/settings.local.json`. They run before every `Bash` call matching the Alpaca order endpoints and before every `mcp__alpaca__*` call. A hook failure is a hard stop.

## Credentials

Loaded from `C:\Users\dpineda\.alpaca\credentials` (user-only ACL, outside repo):

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
```

Bash pattern:
```bash
set -a; . /c/Users/dpineda/.alpaca/credentials; set +a
```

Other API keys (FMP, news, etc.) go in the same file. Never commit.

## Project layout

```
trading-agent/
├── CLAUDE.md                    # this file
├── .mcp.json                    # MCP server registrations (no secrets)
├── .claude/
│   ├── settings.local.json      # hooks, permissions
│   ├── hooks/                   # risk-limit enforcement
│   │   ├── pre_order_check.py
│   │   ├── market_hours_check.py
│   │   └── drawdown_guard.py
│   ├── agents/                  # subagent definitions
│   │   ├── market-researcher.md
│   │   ├── risk-manager.md
│   │   ├── trade-executor.md
│   │   └── portfolio-reporter.md
│   └── skills/
│       ├── alpaca-trading/      # DONE — REST wrapper
│       ├── universe-manager/    # S&P 100 list + metadata
│       ├── rotation-synthesizer/# fork of Exposure Coach
│       ├── correlation-optimizer/
│       └── execution-engine/    # order planner + Alpaca calls
├── data/
│   ├── universe/sp100.csv       # ticker + sector + market cap
│   ├── snapshots/YYYY-MM-DD/    # daily price/fundamental pulls
│   └── reports/YYYY-MM-DD.md    # session report
├── state/
│   ├── portfolio.json           # last-known positions
│   ├── target_weights.json      # today's target allocation
│   └── trade_log.jsonl          # append-only audit log
└── scripts/
    └── daily_session.sh         # orchestrator entrypoint
```

## Daily session flow — two sessions

The work is split across **two sessions** so the actual decision happens
against fresh pre-market data, not 16-hour-stale post-close prices. The
post-mortem on the DE position made clear a same-night plan is insufficient.

### `/post-close` — research-only (22:00 Madrid, post US close)

Runbook: [`sessions/post_close.md`](sessions/post_close.md).

1. **Preflight** — verify market closed, load creds, snapshot account, reconcile
   `state/portfolio.json` against Alpaca. **Flag** stale orders; do not cancel
   (that's pre-open's call).
2. **Refresh data feeds** — `python scripts/fetch_earnings_window.py` rewrites
   `state/upcoming_earnings.json`.
3. **Signal layer** (daily-cadence skills only):
   - `market-breadth-analyzer` → regime
   - `uptrend-analyzer` → participation
   - `market-top-detector` → defensive override
   - `ftd-detector` → bottom-confirmation state
   Long-horizon skills (`macro-regime-detector`, `sector-analyst`,
   `theme-detector`, `us-market-bubble-detector`) run weekly via
   `/weekly-context` and are read from `state/regime_context.json`.
4. **Synthesis** — `exposure-coach` → Market Posture (ceiling %, growth/value
   tilt, new-entries-allowed, cash-priority).
5. **Draft scout** — `python scripts/screen_candidates.py --mode=draft`. Top-30
   superset to `data/snapshots/<date>/candidates_draft.json`. No correlation
   prune, no sector caps.
6. **Persist research bundle** —
   `python scripts/write_research_bundle.py --posture-json='…'`. Writes
   `state/evening_research.json` with posture + draft tickers + signal hashes
   + `valid_until = next-open ET`.
7. **Report** — `data/reports/<date>-research.md`.

**STOP. No trade plan. No `APPROVE`. No execution.**

### `/pre-open` — propose, approve, execute (13:00 Madrid, ~2h30 before US open)

Runbook: [`sessions/pre_open.md`](sessions/pre_open.md). Hard cutoff
**15:25 Madrid** — if no `APPROVE` by then, skip the day.

1. **Preflight-lite** — load creds, freshness-check `state/evening_research.json`
   (abort if missing or stale), snapshot account, list open orders, ask before
   cancelling.
2. **Pre-market** — `python scripts/premarket_check.py` writes
   `state/premarket_data.json` (SPY/QQQ pre-market, UVXY proxy, overnight
   earnings, per-symbol gaps).
3. **Final screen** — `python scripts/screen_candidates.py --mode=final`. All
   5 hard gates against today's bars, premarket-flag fold-in, correlation
   prune, sector + sub-sector caps.
4. **Plan** — `python scripts/build_diff_plan.py` writes
   `state/target_weights.json` (`signed_off=False`) and
   `data/snapshots/<date>/trade_plan.json`. The write is gated by
   `.claude/hooks/morning_window.py` to 13:00–15:25 Madrid.
5. **Present + STOP for `APPROVE`.**
6. **Execute** — `python scripts/execute_plan.py`. Bracket buys (SL at entry × 0.92,
   TP at entry × 1.25), no-chase guard, TIF=`gtc` (Alpaca limits bracket parents
   to `day`/`gtc`; `gtc` lets the order queue pre-open and survive intraday).
7. **Persist + report** — `state/portfolio.json` refresh, `state/trade_log.jsonl`
   append, execution-summary section appended to `data/reports/<date>.md`.

### Why the split

A plan built at 22:00 Madrid acts on prices 16-17 hours stale. Overnight
earnings, futures moves, gaps and macro releases routinely invalidate names
that screened well the night before. The post-close session front-loads the
expensive analysis (signal calls, draft screen) and produces a research
bundle. The pre-open session refines that bundle against fresh quotes,
applies premarket-flag adjustments, and only then asks for approval and
queues MOO orders. Same execution latency, much fresher data.

## Subagents

- **market-researcher** — read-only data puller and signal computer. Uses skills, never places orders. Tools: Bash, Read, Write, Grep.
- **risk-manager** — reads a draft plan, checks against `CLAUDE.md` limits and current state, returns approved plan or blocks with reason. Read-only except for `state/target_weights.json`.
- **trade-executor** — only agent allowed to call order-placing endpoints. Requires a signed-off `target_weights.json`. Tools: Bash (scoped to Alpaca order endpoints), Read.
- **portfolio-reporter** — generates the daily Markdown report. Tools: Read, Write.

## Skills in this repo

### Foundation (already built)
- `alpaca-trading` — REST wrapper for account / orders / data. See `.claude/skills/alpaca-trading/SKILL.md`.

### To build (custom)
- `universe-manager` — maintains `data/universe/sp100.csv`, refreshes monthly, returns sector-tagged ticker lists.
- `rotation-synthesizer` — combines screener outputs into per-ticker conviction scores. Template: Druckenmiller Synthesizer from tradermonty.
- `correlation-optimizer` — 60-day correlation matrix on final candidates; prunes pairs above 0.85.
- `execution-engine` — translates `target_weights.json` into concrete order list (entry price, qty, type), calls `alpaca-trading`.

### Ported skills

Tier 1 — pipeline core (already wired into post-close / pre-open):

| Skill | Role | Cadence |
|---|---|---|
| `market-breadth-analyzer` | Regime detection (risk-on/off) | daily (`post-close`) |
| `uptrend-analyzer` | Participation breadth | daily (`post-close`) |
| `market-top-detector` | Defensive override | daily (`post-close`) |
| `ftd-detector` | Bottom-confirmation state | daily (`post-close`) |
| `exposure-coach` | Market Posture synthesis | daily (`post-close`) |
| `position-sizer` | Per-trade sizing | daily (`pre-open`) |
| `earnings-calendar` | Upcoming-earnings dates | daily (`post-close`, via `fetch_earnings_window.py`) |
| `macro-regime-detector` | Cross-asset regime | weekly (`weekly-context`) |
| `sector-analyst` | Sector rotation | weekly (`weekly-context`) |
| `theme-detector` | Narrative themes | weekly (`weekly-context`) |
| `us-market-bubble-detector` | Structural risk | weekly (`weekly-context`) |
| `backtest-expert` | Robustness methodology | monthly (`monthly-recalibration`) |

Tier A — additional name-quality + measurement layer (ported but not yet
wired into the runbooks; invoke ad-hoc until integrated):

| Skill | Role | Notes |
|---|---|---|
| `signal-postmortem` | Track FP/FN per signal, feed `monthly-recalibration` | No external API. Closes the measurement loop. |
| `data-quality-checker` | Validate daily report before publication | Advisory only, no API. Run end of every session. |
| `market-news-analyst` | 10-day market-news summary | WebSearch/WebFetch only — no API key. |
| `economic-calendar-fetcher` | FOMC/CPI/NFP releases | FMP free tier (1 call/day). |
| `pead-screener` | Post-earnings-drift candidates | FMP free tier OK with default lookback. |
| `vcp-screener` | Volatility-contraction-pattern entries | FMP free tier OK at top-100; **paid** for full SP500 (we run on SP100 so free tier fits). |
| `canslim-screener` | O'Neil growth screen | **EXCEEDS 250-call free tier** at default settings (~283 calls/run). Cap with `--max-candidates 35` to stay under, or upgrade FMP. |
| `earnings-trade-analyzer` | Post-earnings 5-factor scoring | FMP free tier OK at default 2-day lookback. |

### FMP free-tier budget

The free tier is **250 calls/day**. Today's daily-cadence skills consume:

- `fetch_earnings_window.py`: 4-5 calls (chunked weekly window)
- `economic-calendar-fetcher`: 1 call
- `earnings-trade-analyzer`: ~140 calls (default 2-day, top-20)
- `pead-screener`: 50-200 calls (depends on universe)
- `vcp-screener`: ~300 calls at default 100-candidate run — **risk of exceeding budget**
- `canslim-screener`: ~283 calls — **always exceeds budget** without `--max-candidates 35`

**Mitigation strategy:**
- `vcp-screener` and `canslim-screener` should NOT both run daily. Pick one
  for the daily flow; run the other weekly or on-demand.
- Cap `canslim-screener` at `--max-candidates 35` if invoked daily.
- `monthly-recalibration` decides which screen runs daily based on
  observed performance.
- If we observe FMP rate-limit errors more than once a week, upgrade to
  FMP Starter ($30/mo, 750 calls/day).

### Skip list (paid endpoints or out of scope)

| Skill | Why skipped |
|---|---|
| `institutional-flow-tracker` | FMP `institutional-ownership/*` is paid-tier post-2025-08-31. |
| `finviz-screener` | Requires FINVIZ Elite (paid). |
| `options-strategy-advisor` | Out of scope (no options in v1). |
| `pair-trade-screener` | Different paradigm (market-neutral). |
| `kanchi-dividend-review-monitor`, `kanchi-dividend-sop`, `kanchi-dividend-us-tax-accounting` | Dividend strategy, ortogonal to momentum. |
| `value-dividend-screener`, `dividend-growth-pullback-screener` | Dividend strategy. |
| `downtrend-duration-analyzer` | Geared at shorting/exits; we don't short. |

Tier B/C/D (analysis depth, meta-tooling, edge framework) intentionally not
ported. Reconsider if a specific need surfaces during monthly-recalibration.

## Fork process (one-time, historical)

The first batch of skills was imported with:

```bash
cd /c/Work/sp100-rotator
git clone --depth 1 https://github.com/tradermonty/claude-trading-skills.git /tmp/tmc
mkdir -p .claude/skills
cp -r /tmp/tmc/skills/{market-breadth-analyzer,sector-analyst,market-top-detector,position-sizer,exposure-coach,uptrend-analyzer,ftd-detector,macro-regime-detector,theme-detector,earnings-calendar,us-market-bubble-detector,backtest-expert} .claude/skills/
# Tier A (Phase 5b):
cp -r /tmp/tmc/skills/{signal-postmortem,data-quality-checker,market-news-analyst,economic-calendar-fetcher,pead-screener,vcp-screener,canslim-screener,earnings-trade-analyzer} .claude/skills/
rm -rf /tmp/tmc
git add . && git commit -m "Import P0/P1 skills from tradermonty/claude-trading-skills"
```

After import, review each `SKILL.md` for hard-coded data sources and swap to the stack we standardize on (Alpaca data API for prices, FMP free tier for fundamentals, Yahoo as fallback).

**FMP free tier caveats (as of Apr 2026):** The `/api/v3/*` endpoints were deprecated Aug 31, 2025 — use `/stable/*` instead. Most price/quote/historical/fundamentals endpoints still work on the free 250/day tier, but `institutional-ownership/*`, some screeners, and certain analyst-data endpoints now return HTTP 402 (paid-tier only). If a skill throws 402, either migrate to a free alternative (Yahoo, Alpaca bars, SEC EDGAR) or scope it out. Cache aggressively — 250 req/day is tight for a 100-ticker universe.

## Logging & audit

- Every order request + response is appended to `state/trade_log.jsonl`.
- Every session writes a report to `data/reports/YYYY-MM-DD.md`.
- `state/portfolio.json` is the single source of truth for "what we think we own" — always reconcile against Alpaca `/positions` at session start; log discrepancies loudly.

## Circuit breakers

- `--max-turns 120`
- `--max-budget-usd 2.00` (per session)
- Hook: hard-block all order endpoints if portfolio equity drops >2% intraday vs yesterday's close
- Hook: hard-block order endpoints outside US market hours unless the order is explicitly MOO/LOO

## Out of scope (v1)

- Shorting, options, futures, crypto
- Intraday trading or multiple sessions per day
- Live (non-paper) trading
- Discretionary overrides mid-session
- Tax-lot optimization
- Institutional flow / 13F tracking — the `institutional-flow-tracker` skill was dropped because FMP moved `institutional-ownership/*` endpoints behind a paid tier after the Aug 31, 2025 v3 deprecation. The 45-day 13F reporting lag also doesn't fit a daily rotator. Revisit only if we upgrade FMP or wire up SEC EDGAR directly.

---

Ask the user before: changing the universe, relaxing a risk limit, adding a new data source that needs a paid key, or touching any file under `state/` manually.
