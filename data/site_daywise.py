"""
Day-wise summary aggregation for the per-site dashboard.

Pure functions only — no I/O. Feed it the normalized records that
`site_api.query_records()` already returns (which now carry
`transfer_party_name`, see site_api._normalize), and it produces a
per-material-type, per-day rollup of trip counts + net weight + the
first/last activity time of each day.

Shape returned by build_daywise():

    {
      "transfer_party": "All" | "<party>",
      "materials": [
        {
          "material_type": "Soil",
          "days": [
            {"date": "2026-06-02",       # raw YYYY-MM-DD (sortable)
             "start_time": "10:56 am",   # earliest record time that day
             "end_time":   "11:52 pm",   # latest record time that day
             "trips": 55,                # row count
             "net_mt": 1020.16},         # summed net_weight_mt
            ...
          ],
          "total": {"trips": 684, "net_mt": 11909.99}
        },
        ...
      ],
      "grand_total": {"trips": ..., "net_mt": ...}
    }

Transfer party is a PURE FILTER: pick one party and only its trips count;
pick "All" (or pass None) and every party's trips are pooled into the same
per-material-type tables.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

_UNSPECIFIED_MATERIAL = "(unspecified)"
_UNKNOWN_DATE = "Unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _party_of(rec: dict) -> str:
    return (rec.get("transfer_party_name") or "").strip()


def _material_of(rec: dict) -> str:
    return (rec.get("material_type") or "").strip() or _UNSPECIFIED_MATERIAL


def _date_of(rec: dict) -> str:
    return (rec.get("date") or "").strip() or _UNKNOWN_DATE


def _net_mt_of(rec: dict) -> float:
    try:
        return float(rec.get("net_weight_mt") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _time_key(raw) -> Optional[int]:
    """Parse a time string into seconds-since-midnight for ordering.

    Handles both 24-hour ('14:46:55', '14:46') and 12-hour
    ('10:56 am', '1:27 am', '12:03 pm') forms. Returns None for blank or
    unparseable values so they're ignored when picking first/last.
    """
    s = str(raw or "").strip().lower()
    if not s:
        return None

    ampm = None
    if s.endswith("am") or s.endswith("pm"):
        ampm = s[-2:]
        s = s[:-2].strip()

    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        sec = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None

    if ampm == "am" and h == 12:
        h = 0
    elif ampm == "pm" and h != 12:
        h += 12

    return h * 3600 + m * 60 + sec


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_transfer_parties(records: Iterable[dict]) -> list[str]:
    """Distinct, non-blank transfer parties present in `records`, sorted.

    Used to populate the dropdown from whatever appears in the pulled date
    range — no extra API call needed.
    """
    return sorted({p for p in (_party_of(r) for r in records) if p})


def build_daywise(records: Iterable[dict],
                  transfer_party: Optional[str] = None) -> dict:
    """Group records into per-material-type, per-day summary rows.

    `transfer_party` is a pure filter. None / "" / "All" => keep everything.
    """
    records = list(records)

    party = (transfer_party or "").strip()
    if party and party.lower() != "all":
        records = [r for r in records if _party_of(r) == party]

    # material -> date -> running cell
    groups: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {
            "trips": 0, "net_mt": 0.0,
            "_min": None, "_max": None,
            "start_time": "", "end_time": "",
        }))

    for r in records:
        mat = _material_of(r)
        d = _date_of(r)
        cell = groups[mat][d]
        cell["trips"] += 1
        cell["net_mt"] += _net_mt_of(r)

        tkey = _time_key(r.get("time"))
        disp = (r.get("time") or "").strip()
        if tkey is not None:
            if cell["_min"] is None or tkey < cell["_min"]:
                cell["_min"] = tkey
                cell["start_time"] = disp
            if cell["_max"] is None or tkey > cell["_max"]:
                cell["_max"] = tkey
                cell["end_time"] = disp

    materials: list[dict] = []
    for mat in sorted(groups.keys()):
        days: list[dict] = []
        tot_trips = 0
        tot_mt = 0.0
        for d in sorted(groups[mat].keys()):
            c = groups[mat][d]
            days.append({
                "date": d,
                "start_time": c["start_time"],
                "end_time": c["end_time"],
                "trips": c["trips"],
                "net_mt": round(c["net_mt"], 3),
            })
            tot_trips += c["trips"]
            tot_mt += c["net_mt"]
        materials.append({
            "material_type": mat,
            "days": days,
            "total": {"trips": tot_trips, "net_mt": round(tot_mt, 3)},
        })

    return {
        "transfer_party": party or "All",
        "materials": materials,
        "grand_total": {
            "trips": sum(m["total"]["trips"] for m in materials),
            "net_mt": round(sum(m["total"]["net_mt"] for m in materials), 3),
        },
    }