"""Pre-open Phase 2.8 — global market-environment posture overlay.

This is a *post-processor* for the `market-environment-analysis` skill. The
skill itself is invoked conversationally by the runbook, which asks Claude to
WebSearch global indices / FX / commodities / yields / VIX, write the human
report, and persist a structured summary to `state/market_environment.json`
(schema below).

This wrapper then:
  - Validates freshness (`valid_until`) and degraded flag.
  - Applies a conviction-floor bump when global risk is off AND VIX is elevated,
    same overlay pattern as `breadth_chart_veto.py`.
  - Records the overlay to `state/research_bundle.json.posture.overlays` for
    audit and downstream `screen_candidates --mode=final` consumption.

Failure modes:
  - File missing/unreadable     -> warn, no overlay, exit 0 (advisory).
  - File stale (past valid_until) -> warn, no overlay, exit 0.
  - degraded=true (skill ran with timeout/partial data) -> no overlay, exit 0.
  - Research bundle missing     -> log + exit 0 (pre-open's preflight aborts elsewhere).

Pre-open never hard-fails on this phase.

Schema for `state/market_environment.json`:
  {
    "schema_version": "1.0",
    "generated_at": "...Z",
    "valid_until": "...Z",
    "session_date": "YYYY-MM-DD",
    "regime": "risk-on" | "risk-off" | "neutral",
    "vix": {"level": 16.2, "class": "calm|normal|elevated|stressed"},
    "indices_overnight": {...},   # nikkei, hsi, stoxx, sp500_futures, ndx_futures
    "fx": {...},                  # dxy, eurusd, usdjpy
    "commodities": {...},         # wti, gold
    "yields": {...},              # us2y, us10y, curve_2s10s
    "narrative": "...",
    "degraded": false,
    "source": "market-environment-analysis (WebSearch)"
  }
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SESSION_DATE = os.environ.get("SESSION_DATE", date.today().isoformat())
ENV_PATH = PROJECT / "state" / "market_environment.json"
RESEARCH_PATH = PROJECT / "state" / "research_bundle.json"

CONVICTION_FLOOR_BUMP = 0.05
CONVICTION_FLOOR_CAP = 0.80
ELEVATED_VIX_CLASSES = {"elevated", "stressed"}


def load_market_env() -> dict | None:
    if not ENV_PATH.exists():
        print(f"WARN: {ENV_PATH} missing — invoke market-environment-analysis first.",
              file=sys.stderr)
        return None
    try:
        env = json.loads(ENV_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: cannot read market_environment.json: {e}", file=sys.stderr)
        return None
    valid_until_s = env.get("valid_until")
    if valid_until_s:
        try:
            vu = datetime.fromisoformat(valid_until_s.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > vu:
                print(f"WARN: market_environment.json stale (valid_until={valid_until_s})",
                      file=sys.stderr)
                return None
        except ValueError:
            print(f"WARN: invalid valid_until: {valid_until_s}", file=sys.stderr)
    if env.get("degraded"):
        print("WARN: market_environment.json marked degraded — skipping overlay.",
              file=sys.stderr)
        return None
    return env


def should_tighten(env: dict) -> tuple[bool, list[str]]:
    """Return (apply_bump, reasons[])."""
    reasons: list[str] = []
    regime = (env.get("regime") or "").lower()
    vix_class = ((env.get("vix") or {}).get("class") or "").lower()
    if regime != "risk-off":
        return False, []
    reasons.append(f"global regime risk-off")
    if vix_class not in ELEVATED_VIX_CLASSES:
        # Risk-off alone is informational — only escalate when VIX confirms.
        reasons.append(f"vix class '{vix_class or 'unknown'}' not elevated — overlay is informational only")
        return False, reasons
    reasons.append(f"vix class '{vix_class}' confirms stress")
    return True, reasons


def apply_overlay(env: dict, apply_bump: bool, reasons: list[str]) -> dict | None:
    if not RESEARCH_PATH.exists():
        print(f"WARN: {RESEARCH_PATH} missing — cannot record overlay", file=sys.stderr)
        return None
    try:
        bundle = json.loads(RESEARCH_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: cannot read research_bundle.json: {e}", file=sys.stderr)
        return None
    posture = bundle.setdefault("posture", {})
    overlay_record = {
        "source": "market-environment",
        "regime": env.get("regime"),
        "vix_class": (env.get("vix") or {}).get("class"),
        "vix_level": (env.get("vix") or {}).get("level"),
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "reasons": reasons,
        "bump_applied": apply_bump,
    }
    if apply_bump:
        old = float(posture.get("conviction_floor", 0.55))
        new = min(CONVICTION_FLOOR_CAP, old + CONVICTION_FLOOR_BUMP)
        if new > old:
            posture["conviction_floor"] = round(new, 2)
            overlay_record["floor_before"] = old
            overlay_record["floor_after"] = round(new, 2)
            overlay_record["bump"] = CONVICTION_FLOOR_BUMP
        else:
            overlay_record["floor_unchanged"] = True
            overlay_record["floor_at_cap"] = old
    posture.setdefault("overlays", []).append(overlay_record)
    RESEARCH_PATH.write_text(json.dumps(bundle, indent=2))
    return overlay_record


def main() -> int:
    env = load_market_env()
    if env is None:
        print("Market-environment overlay: SKIPPED (no valid input). Pre-open continues.")
        return 0

    apply_bump, reasons = should_tighten(env)
    overlay = apply_overlay(env, apply_bump, reasons)

    regime = env.get("regime", "?")
    vix = env.get("vix") or {}
    icon = {"risk-on": "🟢", "risk-off": "🔴", "neutral": "🟡"}.get(regime, "")
    print(f"Market environment: {icon} {regime} | VIX {vix.get('level', '?')} ({vix.get('class', '?')})")
    if env.get("narrative"):
        print(f"  narrative: {env['narrative'][:200]}")
    for r in reasons:
        print(f"  - {r}")
    if overlay and overlay.get("bump_applied") and overlay.get("floor_after"):
        print(f"  → posture.conviction_floor: {overlay['floor_before']:.2f} → {overlay['floor_after']:.2f} (+{overlay['bump']:.2f})")
    elif overlay and overlay.get("bump_applied") and overlay.get("floor_at_cap"):
        print(f"  → conviction_floor unchanged (already at cap {overlay['floor_at_cap']:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
