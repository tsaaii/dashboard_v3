"""Public overview: 4 project tiles, disposal split, rotating per-agency slide."""
from __future__ import annotations

from flask import Blueprint, render_template

import config
from data import master
from data.aggregate import agency_metrics, project_overview

bp = Blueprint("overview", __name__)


def _pace(today_mt: float, required: float) -> dict:
    """Is today's tonnage keeping up with the hour of the day?"""
    if required <= 0:
        return {"ok": True, "label": "No target", "share": 0.0}
    expected_by_now = required * min(1.0, config.hours_since_midnight_ist() / 24)
    ok = today_mt >= expected_by_now * 0.8
    return {"ok": ok, "label": "On track" if ok else "Behind",
            "share": min(100.0, today_mt / required * 100)}


@bp.route("/")
def overview():
    sites = master.get_sites()
    today = config.today_ist()
    ov = project_overview(sites, today)

    # Disposal split across every row (soil / rdf / c&d / inert)
    split = [
        ("Soil",  sum(s.soil_disposed_mt  for s in sites), "warn"),
        ("RDF",   sum(s.rdf_disposed_mt   for s in sites), "ok"),
        ("C&D",   sum(s.cnd_disposed_mt   for s in sites), "info"),
        ("Inert", sum(s.inert_disposed_mt for s in sites), "muted"),
    ]

    agencies = []
    for name in master.get_agencies():
        am = agency_metrics(name, sites, today)
        lagging = sorted(am.site_rankings, key=lambda r: r["completion_pct"])[:5]
        for x in am.no_outward_sites:            # link each pending site to its page
            x["slug"] = master.slugify(x["site_name"])
        agencies.append({
            "m": am,
            "pace": _pace(am.today_mt, am.daily_rate_required),
            "lagging": lagging,
            "split": [
                ("Soil",  am.disposal_soil_mt,  "warn"),
                ("RDF",   am.disposal_rdf_mt,   "ok"),
                ("C&D",   am.disposal_cnd_mt,   "info"),
                ("Inert", am.disposal_inert_mt, "muted"),
            ],
        })

    return render_template(
        "overview.html",
        nav="overview",
        ov=ov,
        pace=_pace(ov.today_mt, ov.required_today_mt),
        split=split,
        split_total=sum(v for _, v, _ in split),
        agencies=agencies,
        today_str=today.strftime("%d %b %Y"),
        clock=config.now_ist().strftime("%H:%M"),
        rotation_ms=config.ROTATION_INTERVAL_MS,
    )
