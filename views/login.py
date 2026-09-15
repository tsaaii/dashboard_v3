"""
Login / logout routes.

Routes:
    GET  /login   - render the login form (with fresh captcha)
    POST /login   - verify captcha + credentials; redirect on success
    GET  /logout  - clear session, redirect to /login

Order of checks on POST:
    1. IP rate-limit  → 429 if blocked
    2. Captcha        → fail with new captcha if wrong
    3. Credentials    → fail / register failure if wrong
    4. All good       → clear failures, set session, redirect

Why captcha BEFORE credentials? Two reasons:
    - Credential check is the expensive operation (scrypt, ~100ms).
      Captcha gates that work behind a cheap arithmetic check, so
      bots can't trivially DoS the password hasher.
    - It denies bots even the *signal* of which usernames exist.

Also exposes a `login_required` decorator for protecting other routes.
"""
from __future__ import annotations

from functools import wraps
from urllib.parse import urlparse

from flask import (
    Blueprint, current_app, redirect, render_template,
    request, session, url_for,
)

import captcha
import mailer
from auth import USERNAME, verify_credentials
from data import users


bp = Blueprint("login", __name__)

SESSION_USER_KEY = "auth_user"
SESSION_SCOPE_KEY = "auth_scope"      # "admin" | "all" | <site slug>


def current_scope() -> str:
    return session.get(SESSION_SCOPE_KEY) or ""


def is_admin() -> bool:
    return current_scope() == "admin"


# --------------------------------------------------------------------------
# Decorator
# --------------------------------------------------------------------------

def login_required(view):
    """Redirect to /login?next=<current_url> if the user isn't logged in."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get(SESSION_USER_KEY):
            return redirect(url_for("login.login_view", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """403 unless the session scope is admin."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get(SESSION_USER_KEY):
            return redirect(url_for("login.login_view", next=request.path))
        if not is_admin():
            return "Admin access required", 403
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _is_safe_redirect_target(target: str) -> bool:
    """Allow only same-host, path-only redirects.

    Stops `?next=https://evil.example.com` open-redirect attacks.
    """
    if not target:
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return target.startswith("/")


def _render_form(error: str | None = None,
                 next_url: str = "",
                 status: int = 200):
    """Render the login form with a fresh captcha question."""
    question = captcha.current_question()
    nonce = captcha.current_nonce()
    return render_template(
        "login.html",
        error=error,
        next_url=next_url,
        captcha_question=question,
        captcha_nonce=nonce,
    ), status


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login_view():
    # Already logged in? Skip the form.
    if session.get(SESSION_USER_KEY):
        return redirect(url_for("reports.reports_view"))

    next_url = request.args.get("next") or request.form.get("next") or ""

    # ---- GET: just render ----
    if request.method == "GET":
        body, status = _render_form(next_url=next_url)
        return body, status

    # ---- POST flow ----
    ip = captcha.client_ip()

    # 1. rate-limit
    blocked, retry_after = captcha.is_blocked(ip)
    if blocked:
        mins = max(1, retry_after // 60)
        body, _ = _render_form(
            error=f"Too many failed attempts. Try again in {mins} minute(s).",
            next_url=next_url,
        )
        # Hint to well-behaved clients
        return body, 429, {"Retry-After": str(retry_after)}

    # 2. captcha
    captcha_answer = request.form.get("captcha_answer", "")
    captcha_nonce = request.form.get("captcha_nonce", "")
    if not captcha.verify(captcha_answer, nonce=captcha_nonce):
        # Captcha already rotated inside verify() — re-render shows the new one.
        body, _ = _render_form(error="Captcha was incorrect. Please try again.",
                               next_url=next_url)
        return body, 400

    # 3. credentials
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    scope = None
    if verify_credentials(username, password):
        scope = "admin"                                   # env-var admin
    else:
        u = users.authenticate(username, password)
        if u:
            scope = u["scope"]
    if scope is None:
        count, just_blocked = captcha.register_failure(ip)
        current_app.logger.info(
            "Failed login user=%r ip=%s count=%d", username, ip, count,
        )
        if just_blocked:
            mins = captcha.BLOCK_SECONDS // 60
            err = f"Too many failed attempts. Try again in {mins} minute(s)."
        else:
            remaining = max(0, captcha.MAX_FAILURES - count)
            err = "Incorrect username or password."
            if remaining and remaining <= 2:
                err += f" {remaining} attempt(s) remaining."
        body, _ = _render_form(error=err, next_url=next_url)
        return body, 401

    # 4. success
    captcha.clear_failures(ip)
    session.clear()
    session.permanent = True
    if scope in ("admin", "all"):
        session[SESSION_USER_KEY] = username.strip().lower()
        session[SESSION_SCOPE_KEY] = scope
        if _is_safe_redirect_target(next_url):
            return redirect(next_url)
        return redirect(url_for("admin.admin_home") if scope == "admin" else url_for("sites.sites_list"))
    # site-scoped user: unlock just that site
    session["site_auth"] = [scope]
    return redirect(url_for("sites.site_view", slug=scope))


# --------------------------------------------------------------------------
# Request access — emails the admin; same captcha + rate limit as login
# --------------------------------------------------------------------------

FIELDS = ("name", "designation", "organisation", "email", "phone", "site", "reason")


def _request_form(error=None, sent=None, values=None, status=200):
    return render_template("request_access.html", error=error, sent=sent,
                           values=values or {},
                           captcha_question=captcha.current_question(),
                           captcha_nonce=captcha.current_nonce()), status


@bp.route("/request-access", methods=["GET", "POST"])
def request_access():
    if request.method == "GET":
        return _request_form()

    values = {k: (request.form.get(k) or "").strip()[:500] for k in FIELDS}
    ip = captcha.client_ip()
    blocked, retry = captcha.is_blocked(ip)
    if blocked:
        return _request_form(f"Too many attempts. Try again in {max(1, retry // 60)} minute(s).", values=values, status=429)
    if not captcha.verify(request.form.get("captcha_answer", ""), nonce=request.form.get("captcha_nonce", "")):
        return _request_form("Security check was incorrect. Please try again.", values=values, status=400)
    if not (values["name"] and values["designation"] and values["site"] and values["email"]):
        return _request_form("Name, designation, email and site are required.", values=values, status=400)

    values["ip"] = ip
    sent = mailer.send_access_request(values)
    return _request_form(sent=True if sent else "logged")


@bp.route("/logout")
def logout_view():
    session.clear()
    return redirect(url_for("login.login_view"))
