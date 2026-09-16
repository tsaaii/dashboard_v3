"""
Reports — Records Explorer over the weighbridge API.

    GET /reports              the screen (filters + first page of rows)
    GET /reports/rows         HTMX fragment: one page of the table
    GET /reports/export.pdf   summary PDF of everything matching the filters

Everything lives in the query string, so any view can be bookmarked or shared:

    start_date end_date agency_name site_name cluster material
    transfer_party_name vehicle_no ticket_no site_incharge user_name
    record_status min_net_weight max_net_weight
    cols=date&cols=time&...     chosen columns, in order
    sort=<column> dir=asc|desc  sort of the current page
    page limit preset

Rows come from data/records_api.py (cached, with diagnostics); column
definitions from data/pdf_export.py, so the table and the PDF agree.
"""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, Response, render_template, request

import config
from data import phases, records_api
from data.pdf_export import (COLUMN_SCHEMA, DEFAULT_COLUMNS, build_pdf_filename,
                             build_summary_pdf, resolve_columns)
from views.login import login_required

bp = Blueprint("reports", __name__, url_prefix="/reports")

# Transfer party moved out of the main bar into Advanced (rarely used).
SELECT_FILTERS = ["agency_name", "site_name", "cluster", "material"]
ADV_SELECTS = ["transfer_party_name", "site_incharge", "user_name", "record_status"]
TEXT_FILTERS = ["vehicle_no", "ticket_no"]
NUM_FILTERS = ["min_net_weight", "max_net_weight"]
FILTER_KEYS = ["start_date", "end_date"] + SELECT_FILTERS + ADV_SELECTS + TEXT_FILTERS + NUM_FILTERS
PAGE_SIZES = [25, 50, 100, 250]
PRESETS = {"24h": 1, "7d": 7, "30d": 30}


def _filters() -> dict:
    a = request.args
    f = {k: (a.get(k) or "").strip() for k in FILTER_KEYS}
    preset = a.get("preset", "")
    today = config.today_ist()
    # Explicit dates always win: a date box only submits with the old preset
    # radio still checked, and honouring it would wipe the date just picked.
    if preset in PRESETS and not (f["start_date"] or f["end_date"]):
        f["start_date"] = (today - timedelta(days=PRESETS[preset] - 1)).isoformat()
        f["end_date"] = today.isoformat()
    elif preset == "all" and not (f["start_date"] or f["end_date"]):
        f["start_date"] = f["end_date"] = ""
    elif not f["start_date"] and not f["end_date"] and "start_date" not in a:
        f["start_date"] = (today - timedelta(days=6)).isoformat()
        f["end_date"] = today.isoformat()
    return f


def _active_preset(f: dict) -> str:
    today = config.today_ist()
    if not f["start_date"] and not f["end_date"]:
        return "all"
    for key, days in PRESETS.items():
        if (f["start_date"] == (today - timedelta(days=days - 1)).isoformat()
                and f["end_date"] == today.isoformat()):
            return key
    return ""


def _columns() -> list[str]:
    return resolve_columns(request.args.getlist("cols"))


def _phase_options(f: dict) -> dict:
    """Agency / Site / Cluster choices, cross-filtered against each other.

    Source is sites_phases.csv, not the API: it is the list of ULBs actually
    awarded work, and it is what the tiles below are computed from. Each
    dropdown is narrowed by the OTHER two selections, so picking an agency
    leaves only that agency's sites and clusters, and picking a site leaves
    only the agencies that worked there.
    """
    rows = phases.all_rows()
    if not rows:
        return {}
    ph = [x.strip() for x in request.args.getlist("ph") if x.strip()]
    ag, site, cl = f.get("agency_name", ""), f.get("site_name", ""), f.get("cluster", "")

    def keep(r, skip):
        if ph and r["phase"] not in ph:
            return False
        if skip != "agency" and ag and r["agency_name"] != ag:
            return False
        if skip != "site" and site and r["site_name"] != site:
            return False
        if skip != "cluster" and cl and r["cluster"] != cl:
            return False
        return True

    return {
        "agencies": sorted({r["agency_name"] for r in rows if keep(r, "agency") and r["agency_name"]}, key=str.lower),
        "sites": sorted({r["site_name"] for r in rows if keep(r, "site") and r["site_name"]}, key=str.lower),
        "clusters": sorted({r["cluster"] for r in rows if keep(r, "cluster") and r["cluster"]}, key=str.lower),
        # selections that no longer exist after narrowing, so the template can flag them
        "stale": [v for v in (ag, site, cl) if v and not any(
            v in (r["agency_name"], r["site_name"], r["cluster"]) for r in rows if keep(r, ""))],
    }


def _options() -> dict:
    empty = {k: [] for k in ("agencies", "sites", "clusters", "materials",
                             "transfer_parties", "site_incharges", "users")}
    empty.update({"date_min": "", "date_max": "", "weight_min": 0, "weight_max": 0})
    try:
        return records_api.normalize_filter_options(records_api.get_filter_values())
    except Exception:                                    # noqa: BLE001
        return empty


def _valid_weight(r: dict) -> bool:
    """Weighbridge tickets are recorded in 5 kg steps. Anything else — a zero
    or a value like 1982.33 — is a bad read, so it never reaches the table."""
    try:
        net = float(r.get("net_weight") or 0)
    except (TypeError, ValueError):
        return False
    return net > 0 and abs(round(net / 5) * 5 - net) < 0.001


# ---------------------------------------------------------------------------
# Phase panel — four tiles fed by sites_phases.csv, cross-filtered by the
# same agency/site/date filters as the table.
# ---------------------------------------------------------------------------
PROJECT_END = date(2026, 10, 2)


def _phase_panel(f: dict) -> dict | None:
    rows = phases.all_rows()
    if not rows:
        return None
    phase_sel = [x.strip() for x in request.args.getlist("ph") if x.strip()]
    site, agency, cluster = f.get("site_name", ""), f.get("agency_name", ""), f.get("cluster", "")

    def match(r):
        if phase_sel and r["phase"] not in phase_sel:
            return False
        if site and site.lower() not in (r["site_name"].lower(), r["location"].lower()):
            return False
        if agency and r["agency_name"].lower() != agency.lower():
            return False
        if cluster and r["cluster"].lower() != cluster.lower():
            return False
        return True

    sel = [r for r in rows if match(r)]
    awarded = sum(r["target_mt"] for r in sel)
    remediated = sum(r["remediated_mt"] for r in sel)
    remaining = max(0.0, awarded - remediated)

    # Tile 3: Legacy/MSW tonnage recorded on the end date of the range.
    day = f.get("end_date") or config.today_ist().isoformat()
    day_mt, day_trips, day_err = 0.0, 0, None
    try:
        q = {k: v for k, v in f.items() if v and k not in ("start_date", "end_date", "material")}
        q.update({"start_date": day, "end_date": day, "material": "Legacy/MSW"})
        bundle = records_api.fetch_all_records(q, hard_cap=records_api.EXPORT_HARD_CAP)
        good = [r for r in bundle["records"] if _valid_weight(r)]
        day_trips = len(good)
        day_mt = sum(float(r.get("net_weight") or 0) for r in good) / 1000
        day_err = bundle.get("error")
    except Exception as exc:                                  # noqa: BLE001
        day_err = str(exc)

    days_needed = (remaining / day_mt) if day_mt > 0 else None
    days_left = (PROJECT_END - config.today_ist()).days
    return {
        "phases": phases.phases(), "selected": phase_sel, "entries": len(sel),
        "scope": [x for x in (agency, site, cluster) if x],
        "ulbs": len({r["site_name"] for r in sel}),
        "awarded": awarded, "remediated": remediated, "remaining": remaining,
        "pct": min(100.0, remediated / awarded * 100) if awarded else 0.0,
        "day": day, "day_mt": day_mt, "day_trips": day_trips, "day_err": day_err,
        "days_needed": days_needed, "days_left": days_left,
        "on_track": days_needed is not None and days_needed <= days_left,
        "deadline": PROJECT_END.strftime("%d %b %Y"),
    }


def _summary(records: list[dict]) -> dict:
    """Totals for the rows on this page — the API returns no aggregate."""
    net = sum(float(r.get("net_weight") or 0) for r in records)
    return {"net_kg": net, "net_mt": net / 1000,
            "vehicles": len({r.get("vehicle_no") for r in records if r.get("vehicle_no")}),
            "sites": len({r.get("site_name") for r in records if r.get("site_name")})}


@bp.route("")
@login_required
def reports_view():
    f = _filters()
    return render_template(
        "reports.html", nav="reports", f=f, o=_options(),
        cols=_columns(), schema=COLUMN_SCHEMA, default_cols=DEFAULT_COLUMNS,
        preset=_active_preset(f), page_sizes=PAGE_SIZES,
        limit=int(request.args.get("limit") or 50),
        advanced=any(f[k] for k in ADV_SELECTS + NUM_FILTERS),
        panel=_phase_panel(f), ph=request.args.getlist("ph"), po=_phase_options(f),
        active=[(k, v) for k, v in f.items() if v and k not in ("start_date", "end_date")],
    )


@bp.route("/rows")
@login_required
def rows():
    f = _filters()
    cols = _columns()
    page = max(1, int(request.args.get("page") or 1))
    limit = int(request.args.get("limit") or 50)
    limit = limit if limit in PAGE_SIZES else 50

    payload = records_api.fetch_records({k: v for k, v in f.items() if v}, page=page, limit=limit)
    raw = payload.get("records") or []
    records = [r for r in raw if _valid_weight(r)]
    dropped = len(raw) - len(records)

    sort = request.args.get("sort", "")
    if sort in COLUMN_SCHEMA:
        numeric = COLUMN_SCHEMA[sort]["numeric"]

        def key(r):
            v = r.get(sort)
            if numeric:
                try:
                    return float(v or 0)
                except (TypeError, ValueError):
                    return 0.0
            return str(v or "").lower()
        records = sorted(records, key=key, reverse=request.args.get("dir") == "desc")

    pg = payload.get("pagination") or {}
    total = pg.get("total") or pg.get("total_records") or 0
    pages = pg.get("total_pages") or pg.get("pages") or (max(1, -(-total // limit)) if total else 1)
    return render_template("partials/report_rows.html", records=records, cols=cols, schema=COLUMN_SCHEMA,
                           page=page, pages=pages, total=total, limit=limit,
                           first=(page - 1) * limit + 1 if records else 0,
                           last=(page - 1) * limit + len(records),
                           summary=_summary(records), meta=payload.get("_meta") or {},
                           dropped=dropped, sort=sort, desc=request.args.get("dir") == "desc")


@bp.route("/export.pdf")
@login_required
def export_pdf():
    f = {k: v for k, v in _filters().items() if v}
    bundle = records_api.fetch_all_records(f, hard_cap=records_api.EXPORT_HARD_CAP)
    pdf = build_summary_pdf(bundle["records"], filters=f, capped=bundle["capped"],
                            total_in_db=bundle["total_in_db"], columns=_columns())
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{build_pdf_filename(f)}"',
                             "Cache-Control": "no-store"})
