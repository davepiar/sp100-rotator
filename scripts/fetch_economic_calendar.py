"""Fetch upcoming economic events into state/economic_calendar.json.

Pulls FMP `/stable/economic-calendar` for the next ECON_LOOKAHEAD_DAYS days,
filters to high-impact US releases, and writes the deterministic schema
documented in docs/state-contract.md.

Read by `pre-open` Phase 2 (macro kill-switch) and `monthly-recalibration`
(regime-mismatch attribution).

FMP free tier: 1 call per session. The skill `economic-calendar-fetcher`
is a richer LLM-driven version of this; this script is the unattended
fallback that runs from the runbook.

Timezone: FMP /stable/economic-calendar returns timestamps in UTC despite
historically being labeled "ET". We convert to America/New_York so the
pre-open's macro kill-switch can correctly compare against the 09:30 ET
open.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

PROJECT = Path(__file__).resolve().parents[1]
OUT_PATH = PROJECT / "state" / "economic_calendar.json"
ECON_LOOKAHEAD_DAYS = 7

# Map FMP `event` strings to our normalized kinds + impact tier.
# Anything not matched is filtered out (we only care about market-movers).
HIGH_IMPACT_PATTERNS = {
    "FOMC":      ["FOMC", "Federal Funds", "Fed Interest", "Fed Chair", "FOMC Statement", "FOMC Minutes"],
    "CPI":       ["CPI", "Consumer Price"],
    "PPI":       ["PPI", "Producer Price"],
    "PCE":       ["PCE", "Personal Consumption"],
    "NFP":       ["Nonfarm Payrolls", "Non Farm Payrolls", "Non-Farm Payrolls"],
    "PAYROLLS":  ["ADP Employment", "Initial Jobless"],
    "GDP":       ["GDP "],
    "RETAIL":    ["Retail Sales"],
    "ISM":       ["ISM Manufacturing", "ISM Services", "ISM Non-Manufacturing"],
    "CONSUMER":  ["Consumer Confidence", "Michigan Consumer"],
    "HOUSING":   ["Housing Starts", "New Home Sales", "Existing Home Sales"],
}


def normalize(event_name: str) -> tuple[str | None, str]:
    """Return (kind, impact). kind=None → filter out."""
    n = (event_name or "").strip()
    for kind, patterns in HIGH_IMPACT_PATTERNS.items():
        for pat in patterns:
            if pat.lower() in n.lower():
                # FOMC, CPI, NFP, GDP are always high impact; others medium.
                impact = "high" if kind in ("FOMC", "CPI", "NFP", "GDP", "PCE") else "medium"
                return kind, impact
    return None, "low"


def main() -> int:
    fmp_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP")
    if not fmp_key:
        print("ERROR: FMP_API_KEY not set", file=sys.stderr)
        return 2

    today = date.today()
    end = today + timedelta(days=ECON_LOOKAHEAD_DAYS)
    url = "https://financialmodelingprep.com/stable/economic-calendar"
    try:
        r = requests.get(
            url,
            params={"from": today.isoformat(), "to": end.isoformat(), "apikey": fmp_key},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            print(f"ERROR: unexpected FMP shape: {type(rows)}", file=sys.stderr)
            return 2
    except Exception as e:
        print(f"ERROR: FMP fetch failed: {e}", file=sys.stderr)
        return 2

    events: list[dict] = []
    for row in rows:
        country = (row.get("country") or "").upper()
        if country not in ("US", "USA", "UNITED STATES", ""):
            continue  # we only care about US releases for an SP100 rotator
        ev_name = row.get("event") or ""
        kind, impact = normalize(ev_name)
        if kind is None:
            continue
        # FMP returns date strings like "2026-04-29 12:30:00" in **UTC**, despite
        # the field name. Convert to ET (America/New_York, DST-aware) so the
        # pre-open kill-switch can compare against the 09:30 ET open.
        d_str = row.get("date") or ""
        date_part = ""
        time_part = ""
        if d_str:
            try:
                dt_utc = datetime.strptime(d_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                dt_et = dt_utc.astimezone(ET)
                date_part = dt_et.date().isoformat()
                time_part = dt_et.strftime("%H:%M:%S")
            except ValueError:
                # Fallback: treat as date-only or pre-formatted; surface raw string
                date_part, _, time_part = d_str.partition(" ")
        actual = row.get("actual")
        consensus = row.get("estimate") or row.get("consensus")
        previous = row.get("previous")
        surprise = None
        if actual is not None and consensus is not None:
            try:
                a, c = float(actual), float(consensus)
                if c != 0:
                    surprise = (a - c) / abs(c)
            except (TypeError, ValueError):
                pass
        events.append({
            "date": date_part,
            "time_et": time_part[:5] if time_part else "",
            "kind": kind,
            "event": ev_name,
            "impact": impact,
            "actual": actual,
            "consensus": consensus,
            "previous": previous,
            "surprise": surprise,
        })

    # Sort by date+time
    events.sort(key=lambda e: (e["date"], e["time_et"]))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=ECON_LOOKAHEAD_DAYS)).isoformat(),
        "lookahead_days": ECON_LOOKAHEAD_DAYS,
        "events": events,
        "produced_by": "scripts/fetch_economic_calendar.py",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {OUT_PATH} with {len(events)} US events:")
    for e in events:
        time_str = e['time_et'] or '--:--'
        impact_emoji = "🔴" if e["impact"] == "high" else "🟡"
        print(f"  {e['date']} {time_str} ET  {impact_emoji} {e['kind']:8s} {e['event']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
