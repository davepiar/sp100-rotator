"""Post-close Phase 4b — Druckenmiller second-opinion synthesis.

Wraps the `stanley-druckenmiller-investment` skill's scoring + allocation
engine to produce a macro-conviction view that complements (not replaces)
exposure-coach.

The skill ships a CLI orchestrator (`strategy_synthesizer.py`) but discovers
its inputs by filename prefix in one flat directory. The rotator persists
per-skill JSON to `data/snapshots/<DATE>/<skill>/`, so we bypass the loader
and call the in-process API directly:

    extract_signal(skill_name, raw_json) → normalized signals dict
    calculate_composite_conviction(signals) → conviction score + zone
    classify_pattern(signals, components, score) → 4 pattern types
    generate_allocation(...) + calculate_position_sizing(...) → asset mix
    generate_json_report / generate_markdown_report → snapshot artifacts

Outputs:
    state/druckenmiller_view.json
    data/snapshots/<DATE>/druckenmiller/druckenmiller_strategy_<ts>.{json,md}

If macro-regime is missing (weekly cadence; rotator runs it Sunday) we degrade
to a 4-of-5 input run and flag `degraded:true` rather than aborting post-close.

After scoring, we compute divergence between
    druckenmiller.target_allocation.equity_pct
and
    posture.exposure_ceiling_pct
If `|delta| >= 20pp`, append a `divergence_flag` to `posture.overlays[]` and
bump `posture.conviction_floor` by +0.05 (cap 0.80) — same overlay pattern
as `breadth_chart_veto.py`.

Failure modes: any unrecoverable error → warn, exit 0. Post-close never
hard-fails on this phase.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = PROJECT / ".claude" / "skills" / "stanley-druckenmiller-investment" / "scripts"
SESSION_DATE = os.environ.get("SESSION_DATE", date.today().isoformat())
SNAP_BASE = PROJECT / "data" / "snapshots" / SESSION_DATE
OUT_DIR = SNAP_BASE / "druckenmiller"
VIEW_PATH = PROJECT / "state" / "druckenmiller_view.json"
RESEARCH_PATH = PROJECT / "state" / "research_bundle.json"
REGIME_CTX_PATH = PROJECT / "state" / "regime_context.json"

DIVERGENCE_PP_THRESHOLD = 20.0
CONVICTION_FLOOR_BUMP = 0.05
CONVICTION_FLOOR_CAP = 0.80

REQUIRED_INPUTS = {
    "market_breadth": "market-breadth-analyzer",
    "uptrend_analysis": "uptrend-analyzer",
    "market_top": "market-top-detector",
    "ftd_detector": "ftd-detector",
}
OPTIONAL_INPUTS = {
    "vcp_screener": "vcp-screener",
    "theme_detector": "theme-detector",
    "canslim_screener": "canslim-screener",
}

# Map snapshot subdir name to the JSON filename prefix the skill emits
PREFIX_FOR = {
    "market-breadth-analyzer": "market_breadth_",
    "uptrend-analyzer": "uptrend_analysis_",
    "market-top-detector": "market_top_",
    "ftd-detector": "ftd_detector_",
    "macro-regime-detector": "macro_regime_",
    "vcp-screener": "vcp_screener_",
    "theme-detector": "theme_detector_",
    "canslim-screener": "canslim_screener_",
}


def _import_skill_modules():
    """Add the skill's scripts dir to sys.path and import the engine modules."""
    if not SKILL_SCRIPTS.exists():
        print(f"WARN: {SKILL_SCRIPTS} missing — skill not installed?", file=sys.stderr)
        return None
    sys.path.insert(0, str(SKILL_SCRIPTS))
    try:
        from allocation_engine import calculate_position_sizing, generate_allocation
        from report_generator import generate_json_report, generate_markdown_report
        from report_loader import extract_signal
        from scorer import calculate_composite_conviction, classify_pattern
    except ImportError as e:
        print(f"WARN: cannot import druckenmiller modules: {e}", file=sys.stderr)
        return None
    return {
        "extract_signal": extract_signal,
        "calculate_composite_conviction": calculate_composite_conviction,
        "classify_pattern": classify_pattern,
        "generate_allocation": generate_allocation,
        "calculate_position_sizing": calculate_position_sizing,
        "generate_json_report": generate_json_report,
        "generate_markdown_report": generate_markdown_report,
    }


def _find_latest_in_snapshot(skill_dir_name: str) -> dict | list | None:
    """Find the most recent JSON for a skill in today's snapshot dir."""
    skill_dir = SNAP_BASE / skill_dir_name
    if not skill_dir.exists():
        return None
    prefix = PREFIX_FOR.get(skill_dir_name, "")
    pattern = str(skill_dir / f"{prefix}*.json")
    matches = sorted(glob.glob(pattern))
    if not matches:
        # Fall back to any JSON if the prefix-based search missed
        matches = sorted(glob.glob(str(skill_dir / "*.json")))
    if not matches:
        return None
    try:
        return json.loads(Path(matches[-1]).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_for_extractor(sig_name: str, raw):
    """Adapt the rotator's per-skill JSON shape to what the druckenmiller
    extract_signal() expects.

    The rotator's market-breadth-analyzer persists a list of historical
    snapshots like `[{data_date, composite_score, component_scores, recorded_at}, ...]`
    while the upstream skill expects `{composite: {composite_score, zone, ...}}`.
    Other skills already match the expected shape — no-op for those.
    """
    if raw is None:
        return None
    if sig_name == "market_breadth" and isinstance(raw, list):
        if not raw:
            return None
        latest = raw[-1] if isinstance(raw[-1], dict) else None
        if latest is None:
            return None
        return {
            "composite": {
                "composite_score": latest.get("composite_score", 50),
                "zone": latest.get("zone", "Unknown"),
                "zone_color": latest.get("zone_color", ""),
                "exposure_guidance": latest.get("exposure_guidance", ""),
            }
        }
    return raw


def _load_macro_regime() -> dict | None:
    """Prefer state/regime_context.json (weekly), fall back to today's snapshot."""
    if REGIME_CTX_PATH.exists():
        try:
            ctx = json.loads(REGIME_CTX_PATH.read_text())
            if isinstance(ctx, dict) and "macro_regime" in ctx:
                return ctx["macro_regime"]
        except (OSError, json.JSONDecodeError):
            pass
    snap = _find_latest_in_snapshot("macro-regime-detector")
    if snap is not None:
        return snap
    # As a last resort, search the last 7 daily snapshots for a fresh-enough macro regime
    snaps_root = PROJECT / "data" / "snapshots"
    if snaps_root.exists():
        for d in sorted(snaps_root.iterdir(), reverse=True)[:7]:
            cand = d / "macro-regime-detector"
            if cand.exists():
                files = sorted(glob.glob(str(cand / "macro_regime_*.json")))
                if files:
                    try:
                        return json.loads(Path(files[-1]).read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
    return None


def _load_inputs(extract_signal) -> tuple[dict, dict]:
    """Returns (signals_dict, presence_map)."""
    signals: dict = {}
    presence: dict = {k: False for k in list(REQUIRED_INPUTS) + list(OPTIONAL_INPUTS) + ["macro_regime"]}

    for sig_name, dir_name in REQUIRED_INPUTS.items():
        raw = _normalize_for_extractor(sig_name, _find_latest_in_snapshot(dir_name))
        if raw is not None:
            try:
                signals[sig_name] = extract_signal(sig_name, raw)
                presence[sig_name] = True
            except Exception as e:
                print(f"WARN: extract_signal({sig_name}) failed: {e}", file=sys.stderr)

    macro_raw = _normalize_for_extractor("macro_regime", _load_macro_regime())
    if macro_raw is not None:
        try:
            signals["macro_regime"] = extract_signal("macro_regime", macro_raw)
            presence["macro_regime"] = True
        except Exception as e:
            print(f"WARN: extract_signal(macro_regime) failed: {e}", file=sys.stderr)

    for sig_name, dir_name in OPTIONAL_INPUTS.items():
        raw = _normalize_for_extractor(sig_name, _find_latest_in_snapshot(dir_name))
        if raw is None:
            continue
        try:
            signals[sig_name] = extract_signal(sig_name, raw)
            presence[sig_name] = True
        except Exception as e:
            print(f"WARN: extract_signal({sig_name}) failed: {e}", file=sys.stderr)

    return signals, presence


def _compute_divergence(equity_target_pct: float | None, posture: dict) -> dict:
    """Compute |druckenmiller.equity_pct - posture.exposure_ceiling_pct|."""
    ceiling = posture.get("exposure_ceiling_pct")
    if equity_target_pct is None or ceiling is None:
        return {"delta_pp": None, "flag": False, "reason": "incomplete inputs"}
    delta = abs(float(equity_target_pct) - float(ceiling))
    flag = delta >= DIVERGENCE_PP_THRESHOLD
    return {
        "delta_pp": round(delta, 1),
        "flag": flag,
        "druckenmiller_equity_pct": equity_target_pct,
        "exposure_ceiling_pct": ceiling,
    }


def _maybe_apply_overlay(divergence: dict, conviction_score: float, zone: str, pattern: str) -> dict | None:
    if not divergence.get("flag"):
        return None
    if not RESEARCH_PATH.exists():
        print(f"WARN: {RESEARCH_PATH} missing — cannot record overlay", file=sys.stderr)
        return None
    try:
        bundle = json.loads(RESEARCH_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: cannot read research_bundle.json: {e}", file=sys.stderr)
        return None
    posture = bundle.setdefault("posture", {})
    old = float(posture.get("conviction_floor", 0.55))
    new = min(CONVICTION_FLOOR_CAP, old + CONVICTION_FLOOR_BUMP)
    record = {
        "source": "druckenmiller-divergence",
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "delta_pp": divergence["delta_pp"],
        "druckenmiller_equity_pct": divergence.get("druckenmiller_equity_pct"),
        "exposure_ceiling_pct": divergence.get("exposure_ceiling_pct"),
        "conviction_score": conviction_score,
        "zone": zone,
        "pattern": pattern,
    }
    if new > old:
        posture["conviction_floor"] = round(new, 2)
        record["floor_before"] = old
        record["floor_after"] = round(new, 2)
        record["bump"] = CONVICTION_FLOOR_BUMP
    else:
        record["floor_unchanged"] = True
        record["floor_at_cap"] = old
    posture.setdefault("overlays", []).append(record)
    RESEARCH_PATH.write_text(json.dumps(bundle, indent=2))
    return record


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mods = _import_skill_modules()
    if mods is None:
        print("Druckenmiller: SKIPPED (skill modules not loadable). Post-close continues.")
        return 0

    signals, presence = _load_inputs(mods["extract_signal"])
    required_present = sum(1 for k in REQUIRED_INPUTS if presence.get(k))
    macro_present = presence.get("macro_regime", False)
    degraded = required_present < len(REQUIRED_INPUTS) or not macro_present

    if required_present < 3:
        print(f"Druckenmiller: SKIPPED — only {required_present}/{len(REQUIRED_INPUTS)} required signals present.",
              file=sys.stderr)
        return 0

    conviction = mods["calculate_composite_conviction"](signals)
    score = conviction["conviction_score"]
    zone = conviction["zone"]

    pattern = mods["classify_pattern"](signals, conviction["component_scores"], score)

    regime = (signals.get("macro_regime", {}) or {}).get("regime", "transitional")
    target_alloc = mods["generate_allocation"](
        conviction_score=score,
        zone=zone,
        pattern=pattern["pattern"],
        regime=regime,
    )
    sizing = mods["calculate_position_sizing"](conviction_score=score, zone=zone)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    analysis = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "session_date": SESSION_DATE,
            "skills_loaded": sum(1 for v in presence.values() if v),
            "required_count": required_present,
            "optional_count": sum(1 for k in OPTIONAL_INPUTS if presence.get(k)),
            "skills_list": [k for k, v in presence.items() if v],
            "degraded": degraded,
        },
        "conviction": conviction,
        "pattern": pattern,
        "allocation": {
            "target": target_alloc,
            "regime": regime,
            "pattern": pattern["pattern"],
            "zone": zone,
        },
        "position_sizing": sizing,
        "input_summary": signals,
    }

    json_path = OUT_DIR / f"druckenmiller_strategy_{timestamp}.json"
    md_path = OUT_DIR / f"druckenmiller_strategy_{timestamp}.md"
    mods["generate_json_report"](analysis, str(json_path))
    mods["generate_markdown_report"](analysis, str(md_path))

    posture = {}
    if RESEARCH_PATH.exists():
        try:
            posture = (json.loads(RESEARCH_PATH.read_text()) or {}).get("posture", {})
        except (OSError, json.JSONDecodeError):
            posture = {}

    divergence = _compute_divergence(target_alloc.get("equity"), posture)
    overlay = _maybe_apply_overlay(divergence, score, zone, pattern["pattern"])

    view = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_date": SESSION_DATE,
        "conviction_score": score,
        "conviction_zone": zone,
        "pattern": pattern["pattern"],
        "pattern_label": pattern["label"],
        "pattern_match_strength": pattern["match_strength"],
        "target_allocation": {
            "equity_pct": target_alloc.get("equity"),
            "bonds_pct": target_alloc.get("bonds"),
            "alternatives_pct": target_alloc.get("alternatives"),
            "cash_pct": target_alloc.get("cash"),
        },
        "position_sizing": sizing,
        "component_scores": {
            k: v.get("score") for k, v in (conviction.get("component_scores") or {}).items()
        },
        "divergence_vs_exposure_coach": divergence,
        "inputs_present": presence,
        "degraded": degraded,
        "snapshot_paths": {"json": str(json_path), "md": str(md_path)},
        "source": "stanley-druckenmiller-investment",
    }
    VIEW_PATH.write_text(json.dumps(view, indent=2))

    color_for_zone = {
        "Maximum Conviction": "🟢",
        "High Conviction": "🟢",
        "Moderate Conviction": "🟡",
        "Low Conviction": "🟠",
        "Capital Preservation": "🔴",
    }
    icon = color_for_zone.get(zone, "")
    print(f"Druckenmiller: {icon} {zone} ({score}/100) | pattern: {pattern['label']}")
    print(f"  target equity: {target_alloc.get('equity')}% | bonds: {target_alloc.get('bonds')}% "
          f"| alts: {target_alloc.get('alternatives')}% | cash: {target_alloc.get('cash')}%")
    if degraded:
        missing = [k for k in list(REQUIRED_INPUTS) + ["macro_regime"] if not presence.get(k)]
        print(f"  ⚠ degraded — missing: {', '.join(missing)}")
    if divergence.get("delta_pp") is not None:
        flag_txt = "FLAG" if divergence["flag"] else "ok"
        print(f"  divergence vs exposure-coach: Δ={divergence['delta_pp']}pp ({flag_txt}) "
              f"[druck={divergence.get('druckenmiller_equity_pct')}% vs ceiling={divergence.get('exposure_ceiling_pct')}%]")
    if overlay and overlay.get("floor_after"):
        print(f"  → posture.conviction_floor: {overlay['floor_before']:.2f} → {overlay['floor_after']:.2f} "
              f"(+{overlay['bump']:.2f})")
    elif overlay and overlay.get("floor_at_cap") is not None:
        print(f"  → conviction_floor unchanged (already at cap {overlay['floor_at_cap']:.2f})")
    print(f"Wrote {VIEW_PATH}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
