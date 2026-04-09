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
| Allowed order types | market, limit (day/gtc) |
| Allowed sides | buy, sell (no shorts in v1) |
| Trading window | Only place orders when US market is open OR as MOO/LOO for next open |

Hooks live in `.claude/hooks/` and are registered in `.claude/settings.local.json`. They run before every `Bash` call matching the Alpaca order endpoints and before every `mcp__alpaca__*` call. A hook failure is a hard stop.

## Credentials

Loaded from `C:\Users\thepi\.alpaca\credentials` (user-only ACL, outside repo):

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
```

Bash pattern:
```bash
set -a; . /c/Users/thepi/.alpaca/credentials; set +a
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

## Daily session flow

1. **Preflight** — verify market closed, load credentials, fetch account + positions, snapshot current equity, check drawdown guard.
2. **Data pull** — S&P 100 quotes, daily bars (6 months), fundamentals (FMP free tier), sector ETF bars, VIX, breadth (advance/decline).
3. **Signal layer** (skills, run in parallel via subagents):
   - Market Breadth Analyzer → regime (risk-on / risk-off / neutral)
   - Sector Analyst → sector rank
   - Market Top Detector → defensive override
   - CANSLIM / VCP Screeners → candidate list with scores
   - Earnings Trade Analyzer → avoid pre-earnings names or size-down
4. **Synthesis** — Rotation Synthesizer combines signal scores into per-ticker conviction (0–1). Keep top 5–10 by conviction, apply sector caps (≤25% per sector).
5. **Optimization** — Correlation Optimizer downweights highly-correlated picks. Position Sizer applies 5% cap, scales by conviction × (1 − recent vol).
6. **Plan** — build diff vs current portfolio → list of buy/sell orders. Present as a Markdown table in the session report.
7. **Risk gate** — Risk Manager subagent reviews plan against hooks' limits. If violated, shrink or skip.
8. **Execute** — Trade Executor places orders via `alpaca-trading` skill as limit orders at mid + small offset, TIF = day. Waits for fills or MOC fallback.
9. **Report** — Portfolio Reporter writes `data/reports/YYYY-MM-DD.md` with: before/after positions, P/L vs yesterday, realized/unrealized, signal explanations for each pick, trades executed, fills, slippage.
10. **Persist** — update `state/portfolio.json`, append to `state/trade_log.jsonl`.

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

### To fork from `tradermonty/claude-trading-skills`
Copy these into `.claude/skills/` and keep the upstream SKILL.md names so future merges are clean. All of these work with zero paid API keys (Yahoo/FMP free tier):

| Skill | Role in pipeline | Priority |
|---|---|---|
| `market-breadth-analyzer` | Regime detection (risk-on/off) | **P0** |
| `sector-analyst` | Sector ranking | **P0** |
| `market-top-detector` | Defensive override | **P0** |
| `position-sizer` | Per-trade sizing with vol adjust | **P0** |
| `exposure-coach` | Portfolio-level exposure check; template for Rotation Synthesizer | **P0** |
| `canslim-screener` | Growth candidate list | **P1** |
| `vcp-screener` | Momentum/breakout candidates | **P1** |
| `earnings-trade-analyzer` | Pre/post-earnings risk filter | **P1** |
| `druckenmiller-synthesizer` | Reference template for combining signals | **P1** |
| `trend-following-diagnostics` | Sanity check individual name trend | **P2** |
| `relative-strength-ranker` | RS vs SPY for tiebreaks | **P2** |
| `risk-parity-allocator` | Optional alt sizing model | **P2** |
| `drawdown-monitor` | Feeds the `drawdown_guard` hook | **P2** |

Skip: anything options-, crypto-, or futures-related; day-trading-scalp skills; anything that requires a paid Bloomberg/Polygon/Quandl key.

## Fork process (one-time)

```bash
cd /c/Users/thepi/Desktop/trading-agent
git init
git clone --depth 1 https://github.com/tradermonty/claude-trading-skills.git /tmp/tmc
mkdir -p .claude/skills
cp -r /tmp/tmc/skills/{market-breadth-analyzer,sector-analyst,market-top-detector,position-sizer,exposure-coach,canslim-screener,vcp-screener,earnings-trade-analyzer,druckenmiller-synthesizer} .claude/skills/
rm -rf /tmp/tmc
git add . && git commit -m "Import P0/P1 skills from tradermonty/claude-trading-skills"
```

After import, review each `SKILL.md` for hard-coded data sources and swap to the stack we standardize on (Alpaca data API for prices, FMP free tier for fundamentals, Yahoo as fallback).

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

---

Ask the user before: changing the universe, relaxing a risk limit, adding a new data source that needs a paid key, or touching any file under `state/` manually.
