"""Public overview: 4 project tiles + rotating per-agency slide."""
from __future__ import annotations

from flask import Blueprint, render_template

import config
from data import master
from data.aggregate import agency_metrics, main_cards, overview_cards, project_overview

bp = Blueprint("overview", __name__)


@bp.route("/")
def overview():
    sites = master.get_sites()
    today = config.today_ist()
    ov = project_overview(sites, today)

    agencies = []
    for name in master.get_agencies():
        am = agency_metrics(name, sites, today)
        agencies.append({
            "name": name,
            "display_name": am.display_name,
            "site_count": am.total_sites,
            "cluster_count": len(am.clusters),
            "main_cards": main_cards(am),
        })

    return render_template(
        "overview.html",
        overview=ov,
        overview_cards=overview_cards(ov),
        overview_date_str=today.strftime("%B %d, %Y"),
        agencies=agencies,
        rotation_interval_ms=config.ROTATION_INTERVAL_MS,
    )
