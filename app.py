"""
dashboard_v3 — Swachh Andhra legacy-waste remediation dashboard.

Routes
    GET  /                 public overview (4 project tiles + rotating agency slide)
    GET  /?mode=tv         same page, TV-wall layout
    GET  /style/<name>     pick one of four UI styles (cookie), redirect back
    GET  /sites            sites list            (login)      views/sites.py
    GET  /sites/<slug>     site detail           (login)      views/sites.py
    GET  /reports          records explorer      (login)      views/reports.py
    GET/POST /login, GET /logout                              views/login.py
    POST /admin/refresh-cache                    (login)
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
from data import master
from views.login import bp as login_bp, login_required
from views.overview import bp as overview_bp
from views.reports import bp as reports_bp
from views.sites import bp as sites_bp

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger(__name__)

# The four looks. Key = value of <html data-style>, see static/css/theme.css.
STYLES = {
    "ledger":    "Modern · neutral greys, one green accent",
    "govt":      "Government formal · AP green masthead, serif headings",
    "control":   "Control room · dark, mono numerals, glowing bars",
    "editorial": "Editorial · warm paper, hairline rules, serif numerals",
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

    app.register_blueprint(overview_bp)
    app.register_blueprint(login_bp)
    app.register_blueprint(sites_bp)
    app.register_blueprint(reports_bp)

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
            "ui_styles": STYLES,
            "updated_at": datetime.now(config.IST).strftime("%d %b %Y, %H:%M IST"),
            "deadline_str": deadline.strftime("%d %b %Y"),
            "days_left": max(0, (deadline - config.today_ist()).days),
        }

    @app.route("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.route("/admin/refresh-cache", methods=["POST"])
    @login_required
    def refresh_cache():
        master.invalidate_cache()
        return jsonify(status="cache cleared")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
