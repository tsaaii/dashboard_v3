"""
Reports: cross-site records explorer over the weighbridge API.

    GET /reports              page (filters + first page via HTMX)
    GET /reports/rows         HTMX fragment: one page of records
    GET /reports/export.pdf   PDF of everything matching the filters (capped)
"""
from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, Response, render_template, request

import config
from data import records_api
from data.pdf_export import build_pdf_filename, build_summary_pdf, resolve_columns
from views.login import login_required

bp = Blueprint("reports", __name__, url_prefix="/reports")

FILTER_KEYS = ["agency_name", "site_name", "material_type", "transfer_party_name",
               "start_date", "end_date", "vehicle_no", "ticket_no"]


def _filters() -> dict:
    today = config.today_ist()
    f = {k: (request.args.get(k) or "").strip() for k in FILTER_KEYS}
    f["start_date"] = f["start_date"] or (today - timedelta(days=6)).isoformat()
    f["end_date"] = f["end_date"] or today.isoformat()
    return f


@bp.route("")
@login_required
def reports_view():
    try:
        options = records_api.normalize_filter_options(records_api.get_filter_values())
    except Exception:                                    # noqa: BLE001
        options = {"agencies": [], "sites": [], "materials": [], "transfer_parties": []}
    return render_template("reports.html", nav="reports", f=_filters(), o=options)


@bp.route("/rows")
@login_required
def rows():
    f = _filters()
    page = max(1, int(request.args.get("page", 1) or 1))
    payload = records_api.fetch_records({k: v for k, v in f.items() if v}, page=page, limit=50)
    pg = payload.get("pagination") or {}
    total = pg.get("total") or pg.get("total_records") or 0
    pages = pg.get("total_pages") or pg.get("pages") or max(1, -(-total // 50))
    return render_template("partials/report_rows.html", records=payload.get("records") or [],
                           page=page, pages=pages, total=total, meta=payload.get("_meta") or {}, f=f)


@bp.route("/export.pdf")
@login_required
def export_pdf():
    f = {k: v for k, v in _filters().items() if v}
    bundle = records_api.fetch_all_records(f, hard_cap=records_api.EXPORT_HARD_CAP)
    pdf = build_summary_pdf(bundle["records"], filters=f, capped=bundle["capped"],
                            total_in_db=bundle["total_in_db"], columns=resolve_columns([]))
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{build_pdf_filename(f)}"',
                             "Cache-Control": "no-store"})
