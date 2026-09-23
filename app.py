"""
dashboard_v3 — Swachh Andhra legacy-waste remediation dashboard.

Routes
    GET  /                 public overview (4 project tiles + rotating agency slide)
    GET  /?mode=tv         same page, TV-wall layout
    GET  /style/<name>     pick one of four UI styles (cookie), redirect back
    GET  /ulb              Data by ULB (public)               views/ulb.py
    GET  /sites            redirects to /ulb
    GET  /sites/<slug>     site detail           (login)      views/sites.py
    GET  /reports          records explorer      (login)      views/reports.py
    GET/POST /login, GET /logout                              views/login.py
    GET  /admin ...        data files, users, cache          views/admin.py
    GET  /healthz

Data: sites_master.csv (local path or gs://) via data/master.py; weighbridge
API via data/site_api.py and data/records_api.py. See RUNBOOK.md.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request

import config
from data.aggregate import fmt_int_indian, fmt_k, fmt_pct
from views.admin import bp as admin_bp
from views.login import bp as login_bp, is_admin, login_required
from views.record_images import bp as record_images_bp
from views.overview import bp as overview_bp
from views.reports import bp as reports_bp
from views.sites import bp as sites_bp
from views.ulb import bp as ulb_bp

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger(__name__)

# The four looks. Key = value of <html data-style>, see static/css/theme.css.
STYLES = {
    # key -> the two dominant colours of the theme (background, accent).
    # That pair IS the button in the masthead; there is no text label.
    "ledger":    {"desc": "Neutral grey with green accent",       "colors": ("#f5f5f2", "#2f7d52")},
    "govt":      {"desc": "AP green masthead with gold accent",   "colors": ("#0f4d2e", "#f4c542")},
    "control":   {"desc": "Near-black with mint accent",          "colors": ("#090c0f", "#5ee3a5")},
    "editorial": {"desc": "Warm paper with rust accent",          "colors": ("#f5f1e8", "#9a4a2a")},
}
DEFAULT_STYLE = "ledger"
STYLE_COOKIE = "dashboard_style"


def _resolve_secret_key() -> str:
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if os.environ.get("K_SERVICE") or os.environ.get("GAE_ENV"):   # Cloud Run / App Engine
        raise RuntimeError("SECRET_KEY env var is required in production.")
    logger.warning("SECRET_KEY not set; using a random one (local dev only).")
    return secrets.token_hex(32)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _resolve_secret_key()
    app.permanent_session_lifetime = timedelta(hours=8)
    is_prod = bool(os.environ.get("K_SERVICE") or os.environ.get("GAE_ENV"))
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=is_prod)

    # Number formatting for templates: {{ x|inr }} 1,23,45,678 · {{ x|pct }} 91.4% · {{ x|k }} 12.5K
    app.add_template_filter(fmt_int_indian, "inr")
    app.add_template_filter(fmt_pct, "pct")
    app.add_template_filter(fmt_k, "k")
    def _dmy(v: str) -> str:
        """ISO date -> '01 Apr 2026'; anything else passes through."""
        try:
            return datetime.strptime(v, "%Y-%m-%d").strftime("%d %b %Y")
        except (TypeError, ValueError):
            return v or "—"
    app.add_template_filter(_dmy, "dmy")
    def _without(args: dict, key: str, value: str | None = None) -> dict:
        """Copy of request.args minus one key, or minus one value of a repeated
        key — used by the 'remove this filter' chips."""
        out = {k: list(v) for k, v in args.items()}
        if value is None:
            out.pop(key, None)
        elif key in out:
            out[key] = [x for x in out[key] if x != value]
            if not out[key]:
                out.pop(key)
        return out

    app.add_template_filter(_without, "without")
    app.add_template_filter(lambda a, b: min(100.0, max(0.0, (a / b * 100) if b else 0.0)), "share")

    app.register_blueprint(overview_bp)
    app.register_blueprint(login_bp)
    app.register_blueprint(sites_bp)
    app.register_blueprint(ulb_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(record_images_bp)

    @app.route("/style/<name>")
    def set_style(name):
        """Store the chosen UI style in a cookie and go back where we came from."""
        if name not in STYLES:
            name = DEFAULT_STYLE
        back = request.referrer or "/"
        if not back.startswith(request.host_url):
            back = "/"
        resp = redirect(back)
        resp.set_cookie(STYLE_COOKIE, name, max_age=365 * 24 * 3600, samesite="Lax")
        return resp

    @app.context_processor
    def inject_globals():
        """Available in every template."""
        style = request.cookies.get(STYLE_COOKIE, DEFAULT_STYLE)
        deadline = config.project_deadline_date()
        return {
            "ui_style": style if style in STYLES else DEFAULT_STYLE,
            "is_admin": is_admin(),
            "ui_styles": STYLES,
            "updated_at": datetime.now(config.IST).strftime("%d %b %Y, %H:%M IST"),
            "deadline_str": deadline.strftime("%d %b %Y"),
            "days_left": max(0, (deadline - config.today_ist()).days),
        }

    @app.route("/healthz")
    def healthz():
        return jsonify(status="ok")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
