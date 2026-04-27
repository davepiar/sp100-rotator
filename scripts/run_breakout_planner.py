"""Pre-open Phase 3.5 — Minervini-style breakout entry refinement.

Wraps `breakout-trade-planner` from the upstream tradermonty skills repo.
Consumes the most recent VCP-screener JSON (typically produced by Phase 2.6)
and emits Alpaca-compatible order templates with worst-case-risk sizing.

Behavior is gated by `state/strategy_params.json.tunable.execute_plan.
breakout_planner_active`. When `false` (the default during the dry-run period
specified in the plan), this script runs the planner and saves output but
does NOT mutate `state/target_weights.json` or `data/snapshots/<date>/
trade_plan.json`. The intent is ≥3 sessions of dry-run review before any
behavioral change to limit pricing.

Outputs:
  data/snapshots/<DATE>/breakout/  — full planner output (JSON + markdown)

Failure: warn-and-continue.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SKILL = PROJECT / ".claude" / "skills" / "breakout-trade-planner" / "scripts" / "plan_breakout_trades.py"
SESSION_DATE = os.environ.get("SESSION_DATE", date.today().isoformat())
VCP_DIR = PROJECT / "data" / "snapshots" / SESSION_DATE / "vcp"
OUT_DIR = PROJECT / "data" / "snapshots" / SESSION_DATE / "breakout"
PARAMS_PATH = PROJECT / "state" / "strategy_params.json"


def latest_vcp_json() -> Path | None:
    """Find the most recent VCP screener JSON for this session date."""
    if not VCP_DIR.exists():
        return None
    cands = sorted(VCP_DIR.glob("vcp_screener_*.json"))
    return cands[-1] if cands else None


def alpaca_equity() -> float | None:
    base = os.environ.get("ALPACA_BASE_URL", "")
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not (base and key and sec):
        return None
    try:
        r = urllib.request.Request(
            base + "/v2/account",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
        )
        d = json.loads(urllib.request.urlopen(r, timeout=10).read())
        return float(d.get("equity"))
    except Exception as e:
        print(f"WARN: Alpaca account fetch failed: {e}", file=sys.stderr)
        return None


def is_active() -> bool:
    if not PARAMS_PATH.exists():
        return False
    try:
        d = json.loads(PARAMS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    flag = d.get("tunable", {}).get("execute_plan", {}).get("breakout_planner_active")
    if isinstance(flag, dict):  # {"value": false, "unit": "bool"} pattern
        return bool(flag.get("value"))
    return bool(flag)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    active = is_active()
    mode = "ACTIVE (overrides build_diff_plan limits)" if active else "DRY-RUN (output only, no plan mutation)"
    print(f"Breakout planner: {mode}")

    if not SKILL.exists():
        print(f"WARN: {SKILL} not found — skill not imported?", file=sys.stderr)
        return 0

    vcp_json = latest_vcp_json()
    if vcp_json is None:
        print(f"No VCP screener JSON found in {VCP_DIR} — Phase 2.6 must run first.")
        # Save a stub so the runbook + report can show "ran but had no input"
        stub = {
            "schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "active": active,
            "skipped_reason": "no_vcp_input",
        }
        (OUT_DIR / "no_input.json").write_text(json.dumps(stub, indent=2))
        return 0

    equity = alpaca_equity()
    if equity is None:
        # Fall back to a reasonable default; planner will still produce shapes.
        equity = 100000.0
        print(f"WARN: Alpaca equity unavailable; using fallback ${equity:,.0f}", file=sys.stderr)
    else:
        print(f"Using Alpaca equity ${equity:,.2f}")

    # Default risk + heat from the plan; can later be parameterized via strategy_params.
    cmd = [
        "python", str(SKILL),
        "--input", str(vcp_json),
        "--account-size", f"{equity:.2f}",
        "--risk-pct", "0.5",
        "--output-dir", str(OUT_DIR),
    ]
    print("Running:", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("WARN: breakout planner timed out (>120s); skipping", file=sys.stderr)
        return 0

    if r.returncode != 0:
        print(f"WARN: planner exited {r.returncode}: {r.stderr[:400]}", file=sys.stderr)
        return 0

    # Tail of stdout shows the planner's summary
    print(r.stdout[-800:] if r.stdout else "(no stdout)")

    if active:
        # NOTE: when active=True, build_diff_plan should consume this JSON and
        # override its standard mid+5bp limits with the planner's worst-case-risk
        # entries for any ticker present in both the rotator's picks AND the
        # planner's actionable list. Wiring that override is intentionally
        # deferred until the dry-run review window closes (≥3 sessions).
        print("(active=true; build_diff_plan integration to be wired in a follow-up commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
