"""
Sites list and per-site detail.

    GET  /sites                    list (login)
    GET/POST /sites/<slug>         detail; per-site login (site creds) or admin
    GET  /sites/<slug>/records     HTMX fragment: records explorer
    GET  /sites/<slug>/daywise     HTMX fragment: day-wise table
    GET  /sites/<slug>/report.pdf  records PDF
    GET  /sites/<slug>/daywise.pdf day-wise PDF

Per-site access: the admin login unlocks everything; a site's own login_id /
login_pwd (from sites_master.csv) unlocks that one slug, stored in the session.
"""
from __future__ import annotations

from datetime import date, timedelta

from flask import (Blueprint, Response, abort, current_app, redirect,
                   render_template, request, session, url_for)

import captcha
import config
from auth import USERNAME, verify_credentials
from data import master, phases, site_api, site_daywise, site_daywise_pdf, site_pdf, site_totals, users
from views.login import SESSION_SCOPE_KEY, SESSION_USER_KEY, login_required

bp = Blueprint("sites", __name__, url_prefix="/sites")
SESSION_SITE_KEY = "site_auth"


# ---- access -------------------------------------------------------------
def _has_access(slug: str) -> bool:
    return bool(session.get(SESSION_USER_KEY)) or slug in (session.get(SESSION_SITE_KEY) or [])


def _grant(slug: str) -> None:
    g = set(session.get(SESSION_SITE_KEY) or []); g.add(slug)
    session[SESSION_SITE_KEY] = sorted(g); session.permanent = True


def _site_or_404(slug: str):
    site = master.get_site_by_slug(slug)
    if site is None:
        abort(404)
    return site


def _gate(slug: str):
    site = _site_or_404(slug)
    if not _has_access(site.slug):
        abort(403)
    return site


# ---- date range helper ----------------------------------------------------
def _range(default_days: int) -> tuple[str, str]:
    today = config.today_ist()
    start = (request.args.get("start") or "").strip() or (today - timedelta(days=default_days - 1)).isoformat()
    end = (request.args.get("end") or "").strip() or today.isoformat()
    return start, end


# ---- list: superseded by /ulb (views/ulb.py) ----------------------------
@bp.route("")
def sites_list():
    return redirect(url_for("ulb.ulb_view"))


# ---- detail ---------------------------------------------------------------
def _tiles(site) -> dict:
    """Headline numbers. Remediated/disposal come from real records when the
    processed master.csv is reachable; otherwise the spine values."""
    totals = site_totals.site_totals(site.api_site_names)
    # Use record-derived totals only when they actually exist; a reachable but
    # empty master.csv (e.g. local dev) must not zero out the tiles.
    if totals["source"] != "unavailable" and totals["remediated_mt"] > 0:
        remediated, split, disposed = totals["remediated_mt"], totals["disposal_mt"], totals["total_disposed_mt"]
    else:
        remediated = site.remediated_mt
        split = {"Soil": site.soil_disposed_mt, "RDF": site.rdf_disposed_mt,
                 "CnD": site.cnd_disposed_mt, "Inert": site.inert_disposed_mt}
        disposed = sum(split.values())
    target = site.target_mt
    remaining = max(0.0, target - remediated)
    today = config.today_ist()
    days_left = (site.deadline_date - today).days if site.deadline_date else 0
    required = remaining / days_left if days_left > 0 else 0.0
    elapsed = max(1, (today - site.start_date).days) if site.start_date else 1
    return {
        "target": target, "remediated": remediated, "remaining": remaining,
        "pct": min(100.0, remediated / target * 100) if target else 0.0,
        "days_left": max(0, days_left), "required": required,
        "avg_per_day": remediated / elapsed,
        "disposed": disposed, "disposal_pct": (disposed / remediated * 100) if remediated else 0.0,
        "split": [("Soil", split.get("Soil", 0), "warn"), ("Inert", split.get("Inert", 0), "muted"),
                  ("C&D", split.get("CnD", 0), "info"), ("RDF", split.get("RDF", 0), "ok")],
        "source": totals["source"],
    }


@bp.route("/<slug>", methods=["GET", "POST"])
def site_view(slug):
    site = _site_or_404(slug)
    if request.method == "GET" and slug != site.slug:
        return redirect(url_for("sites.site_view", slug=site.slug))

    if _has_access(site.slug):
        start, end = _range(30)
        try:
            pace = site_api.daily_pace(site.api_site_names)
        except Exception:                                   # API down: show —
            pace = None
        return render_template("site.html", nav="sites", site=site, t=_tiles(site),
                               pace=pace, start=start, end=end)

    # ---- per-site login (same order as views/login.py) ----
    def form(error=None, status=200):
        return render_template("login.html", error=error, next_url="",
                               captcha_question=captcha.current_question(),
                               captcha_nonce=captcha.current_nonce(),
                               form_action=url_for("sites.site_view", slug=site.slug),
                               site_name=site.site_name), status
    if request.method == "GET":
        return form()
    ip = captcha.client_ip()
    blocked, retry = captcha.is_blocked(ip)
    if blocked:
        return form(f"Too many failed attempts. Try again in {max(1, retry // 60)} minute(s).", 429)
    if not captcha.verify(request.form.get("captcha_answer", ""), nonce=request.form.get("captcha_nonce", "")):
        return form("Captcha was incorrect. Please try again.", 400)
    user = (request.form.get("username") or "").strip(); pwd = request.form.get("password") or ""
    if verify_credentials(user, pwd):
        captcha.clear_failures(ip); session[SESSION_USER_KEY] = USERNAME; session[SESSION_SCOPE_KEY] = "admin"; session.permanent = True
        return redirect(url_for("sites.site_view", slug=site.slug))
    urow = users.authenticate(user, pwd)
    if urow and urow["scope"] in ("all", "admin"):
        captcha.clear_failures(ip); session[SESSION_USER_KEY] = urow["username"]; session[SESSION_SCOPE_KEY] = urow["scope"]; session.permanent = True
        return redirect(url_for("sites.site_view", slug=site.slug))
    if urow and urow["scope"] == site.slug:
        captcha.clear_failures(ip); _grant(site.slug)
        return redirect(url_for("sites.site_view", slug=site.slug))
    if user and pwd and user == (site.login_id or "").strip() and pwd == (site.login_pwd or ""):
        captcha.clear_failures(ip); _grant(site.slug)
        return redirect(url_for("sites.site_view", slug=site.slug))
    count, just_blocked = captcha.register_failure(ip)
    current_app.logger.info("Failed site login slug=%s user=%r ip=%s", site.slug, user, ip)
    if just_blocked:
        return form(f"Too many failed attempts. Try again in {captcha.BLOCK_SECONDS // 60} minute(s).", 401)
    left = max(0, captcha.MAX_FAILURES - count)
    return form("Incorrect username or password." + (f" {left} attempt(s) remaining." if 0 < left <= 2 else ""), 401)


# ---- HTMX fragments -------------------------------------------------------
@bp.route("/<slug>/records")
def site_records(slug):
    site = _gate(slug)
    start, end = _range(30)
    material = (request.args.get("material") or "").strip() or None
    vehicle = (request.args.get("vehicle") or "").strip() or None
    error = None
    try:
        records = site_api.query_records(site.api_site_names, start_date=start, end_date=end,
                                         material=material, vehicle=vehicle)
    except Exception as exc:                                # noqa: BLE001
        current_app.logger.exception("records failed for %s", slug)
        records, error = [], str(exc)
    summary = site_api.summarize(records)
    # per-day totals for the trend strip
    by_day: dict[str, float] = {}
    for r in records:
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + r["net_weight_mt"]
    days = []
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    d = d0
    while d <= d1 and len(days) < 120:
        key = d.strftime("%d-%m-%Y")               # API dates are dd-mm-yyyy
        days.append((key, by_day.get(key, by_day.get(d.isoformat(), 0.0))))
        d += timedelta(days=1)
    peak = max([v for _, v in days] + [1.0])
    return render_template("partials/site_records.html", site=site, records=records[:500],
                           n=len(records), summary=summary, days=days, peak=peak,
                           start=start, end=end, material=material, vehicle=vehicle or "", error=error)


@bp.route("/<slug>/daywise")
def site_daywise_view(slug):
    site = _gate(slug)
    start, end = _range(30)
    party = (request.args.get("party") or "").strip() or None
    error = None
    try:
        records = site_api.query_records(site.api_site_names, start_date=start, end_date=end)
    except Exception as exc:                                # noqa: BLE001
        records, error = [], str(exc)
    return render_template("partials/site_daywise.html", site=site,
                           parties=site_daywise.list_transfer_parties(records),
                           summary=site_daywise.build_daywise(records, transfer_party=party),
                           start=start, end=end, party=party or "", error=error)


# ---- PDFs -----------------------------------------------------------------
def _pdf(data: bytes, name: str) -> Response:
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@bp.route("/<slug>/report.pdf")
def site_report_pdf(slug):
    site = _gate(slug)
    start, end = _range(30)
    filters = {"start": start, "end": end,
               "material": (request.args.get("material") or "").strip() or None,
               "vehicle": (request.args.get("vehicle") or "").strip() or None}
    records = site_api.query_records(site.api_site_names, start_date=start, end_date=end,
                                     material=filters["material"], vehicle=filters["vehicle"])
    return _pdf(site_pdf.build_report(site.site_name, site.agency_name, filters, records, site_api.summarize(records)),
                f"{site.slug}_{start}_{end}.pdf")


@bp.route("/<slug>/daywise.pdf")
def site_daywise_pdf_route(slug):
    site = _gate(slug)
    start, end = _range(30)
    party = (request.args.get("party") or "").strip() or None
    records = site_api.query_records(site.api_site_names, start_date=start, end_date=end)
    summary = site_daywise.build_daywise(records, transfer_party=party)
    return _pdf(site_daywise_pdf.build_daywise_pdf(site.site_name, site.agency_name, start, end, party, summary),
                f"{site.slug}_daywise_{start}_{end}.pdf")
