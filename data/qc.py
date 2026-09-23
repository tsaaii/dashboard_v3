"""
QC mode for /reports — how long each trip took, and how soon the same
vehicle came back to the same site on the same day.

    trip delta     second_timestamp - first_timestamp   time between the two
                   weighments of one ticket (vehicle was on site)
    vehicle delta  first_timestamp - previous trip's second_timestamp for the
                   SAME vehicle at the SAME site on the SAME day (turnaround:
                   left the bridge loaded, came back empty). First trip of the
                   day has none.

Both are in minutes. Negative values are clock errors, not physics — they
are flagged as 'invalid' rather than hidden. A trip with only one
weighment (record_status incomplete) has no trip delta.

The upstream API already returns trip_seconds / vehicle_delta_seconds /
previous_ticket_no on each record; those are used as-is. The timestamp
arithmetic below is only the fallback for records that lack them.

Thresholds are caller-supplied so the UI can tune them; defaults are a
guess at what an implausibly fast legacy-waste trip looks like.
"""
from __future__ import annotations

from datetime import datetime
from statistics import median

_FORMATS = ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")

DEFAULT_MIN_TRIP = 10      # minutes; below this the trip looks too fast
DEFAULT_MIN_TURN = 15      # minutes; below this the vehicle came back too soon


def _ts(v) -> datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _hm(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else ""


def _mins(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 60, 1) if a and b else None


def _secs_to_min(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return round(float(v) / 60, 1)
    except (TypeError, ValueError):
        return None


def build(records: list[dict], min_trip: float = DEFAULT_MIN_TRIP, min_turn: float = DEFAULT_MIN_TURN,
          flagged_only: bool = False) -> dict:
    """Group -> site -> day -> vehicle, each trip annotated with deltas and flags."""
    trips = []
    for r in records:
        f, s = _ts(r.get("first_timestamp")), _ts(r.get("second_timestamp"))
        day = r.get("date") or (f.strftime("%d-%m-%Y") if f else "")
        trips.append({
            "site": r.get("site_name") or "", "day": day, "vehicle": (r.get("vehicle_no") or "").strip().upper(),
            "ticket": r.get("ticket_no") or "", "material": r.get("material") or r.get("material_type") or "",
            "party": r.get("transfer_party_name") or "", "net": float(r.get("net_weight") or 0),
            "first": f, "second": s, "in": _hm(f), "out": _hm(s),
            "trip": _secs_to_min(r.get("trip_seconds")) if "trip_seconds" in r else _mins(f, s),
            "turn": _secs_to_min(r.get("vehicle_delta_seconds")) if "vehicle_delta_seconds" in r else None,
            "prev": r.get("previous_ticket_no") or "", "flags": [],
            "images": bool(r.get("image_slots") or r.get("images") or r.get("first_front_image")),
            "date": r.get("date") or "", "site_name": r.get("site_name") or "", "ticket_no": r.get("ticket_no") or "",
        })

    # sort inside each vehicle-day by first weighment, then chain turnarounds
    keyed: dict[tuple, list] = {}
    for t in trips:
        keyed.setdefault((t["site"], t["day"], t["vehicle"]), []).append(t)
    for group in keyed.values():
        group.sort(key=lambda t: (t["first"] or datetime.max, t["ticket"]))
        prev = None
        for t in group:
            if t["turn"] is None and prev and prev["second"] and t["first"]:
                t["turn"] = _mins(prev["second"], t["first"])      # fallback only
                t["prev"] = t["prev"] or prev["ticket"]
            if t["trip"] is None:
                t["flags"].append("one weighment")
            elif t["trip"] < 0:
                t["flags"].append("invalid trip")
            elif t["trip"] < min_trip:
                t["flags"].append("fast trip")
            if t["turn"] is not None:
                if t["turn"] < 0:
                    t["flags"].append("overlap")          # came back before it left
                elif t["turn"] < min_turn:
                    t["flags"].append("fast return")
            prev = t

    if flagged_only:
        trips = [t for t in trips if t["flags"]]

    # nest for the template: site -> day -> vehicle -> trips
    sites: dict[str, dict] = {}
    for t in trips:
        s = sites.setdefault(t["site"], {"name": t["site"], "days": {}})
        d = s["days"].setdefault(t["day"], {"day": t["day"], "vehicles": {}})
        v = d["vehicles"].setdefault(t["vehicle"], {"vehicle": t["vehicle"], "trips": []})
        v["trips"].append(t)

    def _day_key(d):
        return _ts(d + " 00:00:00") or datetime.min

    out_sites = []
    for s in sorted(sites.values(), key=lambda x: x["name"].lower()):
        days = []
        for d in sorted(s["days"].values(), key=lambda x: _day_key(x["day"]), reverse=True):
            vehicles = []
            for v in sorted(d["vehicles"].values(), key=lambda x: x["vehicle"]):
                v["trips"].sort(key=lambda t: (t["first"] or datetime.max, t["ticket"]))
                v["n"] = len(v["trips"]); v["net"] = sum(t["net"] for t in v["trips"])
                v["flagged"] = sum(1 for t in v["trips"] if t["flags"])
                v["med_trip"] = median([t["trip"] for t in v["trips"] if t["trip"] is not None] or [0])
                turns = [t["turn"] for t in v["trips"] if t["turn"] is not None]
                v["med_turn"] = median(turns) if turns else None
                vehicles.append(v)
            d["vehicles"] = vehicles
            d["n"] = sum(v["n"] for v in vehicles); d["flagged"] = sum(v["flagged"] for v in vehicles)
            days.append(d)
        s["days"] = days
        s["n"] = sum(d["n"] for d in days); s["flagged"] = sum(d["flagged"] for d in days)
        out_sites.append(s)

    all_trips = [t["trip"] for t in trips if t["trip"] is not None and t["trip"] >= 0]
    all_turns = [t["turn"] for t in trips if t["turn"] is not None and t["turn"] >= 0]
    return {
        "sites": out_sites, "trips": len(trips), "vehicles": len(keyed),
        "flagged": sum(1 for t in trips if t["flags"]),
        "med_trip": median(all_trips) if all_trips else None,
        "med_turn": median(all_turns) if all_turns else None,
        "min_trip": min_trip, "min_turn": min_turn,
    }
