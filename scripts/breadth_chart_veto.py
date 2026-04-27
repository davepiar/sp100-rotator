"""Pre-open Phase 2.7 — pre-market breadth-chart regime veto.

Wraps `breadth-chart-analyst`'s `fetch_breadth_csv.py --json` and translates the
output into a verdict that pre-open's operator + build_diff_plan can act on.

Verdict logic (deterministic, no LLM):
  - RED  (hard veto):   breadth_200ma < 50  OR  (dead_cross AND uptrend RED)
  - YELLOW (soft veto): dead_cross OR (50 <= breadth_200ma < 60) OR uptrend RED
  - GREEN (no veto):    everything else

On YELLOW or RED, tighten `state/research_bundle.json.posture.conviction_floor`
by +0.05 (YELLOW) or +0.10 (RED), capped at 0.80. This is a posture overlay —
build_diff_plan reads it via the existing kill-switch path. Today's plan flow
already inherits the floor through the kill-switch logic; future code may also
read it directly.

Outputs:
  data/snapshots/<DATE>/breadth-chart-analyst/breadth_verdict_<timestamp>.json
  state/research_bundle.json (only mutated on YELLOW/RED)

Failure: warn-and-continue. Pre-open never hard-fails on this phase.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SKILL_FETCHER = PROJECT / ".claude" / "skills" / "breadth-chart-analyst" / "scripts" / "fetch_breadth_csv.py"
SESSION_DATE = os.environ.get("SESSION_DATE", date.today().isoformat())
OUT_DIR = PROJECT / "data" / "snapshots" / SESSION_DATE / "breadth-chart-analyst"
RESEARCH_PATH = PROJECT / "state" / "research_bundle.json"

YELLOW_BUMP = 0.05
RED_BUMP = 0.10
CONVICTION_FLOOR_CAP = 0.80


def fetch_breadth() -> dict | None:
    """Run the upstream skill's CSV fetcher in JSON mode."""
    if not SKILL_FETCHER.exists():
        print(f"WARN: {SKILL_FETCHER} not found — skill not imported?", file=sys.stderr)
        return None
    try:
        r = subprocess.run(
            ["python", str(SKILL_FETCHER), "--json", "--days", "1"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            print(f"WARN: fetch_breadth_csv exited {r.returncode}: {r.stderr[:300]}", file=sys.stderr)
            return None
        return json.loads(r.stdout)
    except Exception as e:
        print(f"WARN: breadth-chart fetcher failed: {e}", file=sys.stderr)
        return None


def classify(b: dict) -> tuple[str, list[str], float]:
    """Return (verdict, reasons[], floor_bump)."""
    reasons: list[str] = []
    floor_bump = 0.0
    breadth_200 = float(b.get("breadth_200ma") or 0.0)
    dead_cross = bool(b.get("dead_cross"))
    uptrend_color = (b.get("uptrend_color") or "").upper()

    # RED: severe deterioration
    if breadth_200 < 50.0:
        reasons.append(f"breadth_200ma {breadth_200:.1f}% below 50 — severe weakness")
        floor_bump = RED_BUMP
        verdict = "RED"
    elif dead_cross and uptrend_color == "RED":
        reasons.append("dead_cross AND uptrend RED — twin-warning")
        floor_bump = RED_BUMP
        verdict = "RED"
    # YELLOW: any single warning
    elif dead_cross:
        reasons.append(f"dead_cross (8MA −{abs(b.get('cross_diff', 0)):.2f}pt below 200MA)")
        floor_bump = YELLOW_BUMP
        verdict = "YELLOW"
    elif breadth_200 < 60.0:
        reasons.append(f"breadth_200ma {breadth_200:.1f}% below 60 (healthy threshold)")
        floor_bump = YELLOW_BUMP
        verdict = "YELLOW"
    elif uptrend_color == "RED":
        reasons.append("uptrend_color RED — short-term weakness")
        floor_bump = YELLOW_BUMP
        verdict = "YELLOW"
    else:
        verdict = "GREEN"
    return verdict, reasons, floor_bump


def maybe_tighten_floor(bump: float, verdict: str) -> dict | None:
    """Increase posture.conviction_floor by `bump`. Returns the change record or None."""
    if bump <= 0.0:
        return None
    if not RESEARCH_PATH.exists():
        print(f"WARN: {RESEARCH_PATH} missing — cannot tighten conviction floor", file=sys.stderr)
        return None
    try:
        bundle = json.loads(RESEARCH_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: cannot read research_bundle.json: {e}", file=sys.stderr)
        return None
    posture = bundle.setdefault("posture", {})
    old = float(posture.get("conviction_floor", 0.55))
    new = min(CONVICTION_FLOOR_CAP, old + bump)
    if new <= old:
        return None
    posture["conviction_floor"] = round(new, 2)
    posture.setdefault("overlays", []).append({
        "source": "breadth-chart-veto",
        "verdict": verdict,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "floor_before": old,
        "floor_after": round(new, 2),
        "bump": bump,
    })
    RESEARCH_PATH.write_text(json.dumps(bundle, indent=2))
    return {"old": old, "new": round(new, 2), "bump": bump}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    breadth = fetch_breadth()
    if breadth is None:
        print("Breadth-chart veto: SKIPPED (fetch failed). Pre-open continues unchanged.")
        return 0

    verdict, reasons, bump = classify(breadth)
    floor_change = maybe_tighten_floor(bump, verdict) if verdict != "GREEN" else None

    record = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_date": SESSION_DATE,
        "verdict": verdict,
        "reasons": reasons,
        "floor_bump": bump,
        "floor_change": floor_change,
        "raw": {
            "breadth_date": breadth.get("breadth_date"),
            "breadth_200ma": breadth.get("breadth_200ma"),
            "breadth_200ma_class": breadth.get("breadth_200ma_class"),
            "breadth_8ma": breadth.get("breadth_8ma"),
            "breadth_8ma_class": breadth.get("breadth_8ma_class"),
            "dead_cross": breadth.get("dead_cross"),
            "cross_diff": breadth.get("cross_diff"),
            "uptrend_ratio": breadth.get("uptrend_ratio"),
            "uptrend_color": breadth.get("uptrend_color"),
            "uptrend_class": breadth.get("uptrend_class"),
            "uptrend_trend": breadth.get("uptrend_trend"),
        },
    }
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    out_path = OUT_DIR / f"breadth_verdict_{ts}.json"
    out_path.write_text(json.dumps(record, indent=2))

    color = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(verdict, "")
    print(f"Breadth-chart verdict: {color} {verdict}")
    if reasons:
        for r in reasons:
            print(f"  - {r}")
    print(f"  raw: 200MA={breadth.get('breadth_200ma')}% ({breadth.get('breadth_200ma_class')}), "
          f"8MA={breadth.get('breadth_8ma')}% ({breadth.get('breadth_8ma_class')}), "
          f"uptrend={breadth.get('uptrend_color')} ({breadth.get('uptrend_class')})")
    if floor_change:
        print(f"  → posture.conviction_floor: {floor_change['old']:.2f} → {floor_change['new']:.2f} (+{floor_change['bump']:.2f})")
    elif bump > 0:
        print(f"  → conviction_floor unchanged (already at cap or research_bundle missing)")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
