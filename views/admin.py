"""
Admin console — /admin. Env-var admin only (scope "admin").

    GET  /admin                       data files + users + cache
    POST /admin/upload                replace a data file (sites_master.csv, users.csv, ...)
    GET  /admin/download/<name>       download the current copy
    POST /admin/users/add             add / replace a user
    POST /admin/users/remove          delete a user
    POST /admin/refresh-cache         re-read all files now
"""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from data import master, phases, storage, users
from views.login import admin_required

bp = Blueprint("admin", __name__, url_prefix="/admin")

# Files the console manages: name -> (label, required header columns or None)
DATA_FILES = {
    "sites_master.csv": ("Sites master", ["site_name", "agency_name", "target_mt", "remediated_mt"]),
    "sites_phases.csv": ("Phase data — Data by ULB page", ["phase", "site_name", "agency_name", "target_mt"]),
    "users.csv":        ("Users",        ["username", "password_hash", "scope"]),
}
SAFE = set(DATA_FILES)


def _invalidate_all() -> None:
    master.invalidate_cache()
    users.invalidate()
    phases.invalidate()
    try:
        from views import record_images
        record_images.invalidate_cache()
    except Exception:                        # noqa: BLE001
        pass
    try:                                     # per-site totals cache, if present
        from data import site_totals
        site_totals.invalidate_cache()       # type: ignore[attr-defined]
    except Exception:                        # noqa: BLE001
        pass


@bp.route("")
@admin_required
def admin_home():
    files = [{"name": n, "label": lbl, **storage.info(n)} for n, (lbl, _) in DATA_FILES.items()]
    try:
        n_sites = len(master.get_sites())
    except Exception as exc:                 # noqa: BLE001
        n_sites = f"error: {exc}"
    site_slugs = sorted(s.slug for s in master.get_sites() if s.is_renderable) if isinstance(n_sites, int) else []
    return render_template("admin.html", nav="admin", files=files, n_sites=n_sites,
                           users=users.all_users(), site_slugs=site_slugs,
                           folder=storage.path_for(""))


@bp.route("/upload", methods=["POST"])
@admin_required
def upload():
    name = request.form.get("name", "")
    f = request.files.get("file")
    if name not in SAFE or not f:
        abort(400)
    data = f.read()
    if len(data) > 5 * 1024 * 1024:
        flash("File is larger than 5 MB.", "danger"); return redirect(url_for("admin.admin_home"))
    # validate header before overwriting anything
    try:
        header = next(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    except Exception:                        # noqa: BLE001
        flash("That is not a readable CSV.", "danger"); return redirect(url_for("admin.admin_home"))
    need = DATA_FILES[name][1] or []
    missing = [c for c in need if c not in [h.strip() for h in header]]
    if missing:
        flash(f"Missing columns: {', '.join(missing)}. Nothing was changed.", "danger")
        return redirect(url_for("admin.admin_home"))
    path = storage.write_bytes(name, data)
    _invalidate_all()
    flash(f"{name} replaced ({len(data):,} bytes) at {path}. Dashboard refreshed.", "success")
    return redirect(url_for("admin.admin_home"))


@bp.route("/download/<name>")
@admin_required
def download(name):
    if name not in SAFE:
        abort(404)
    try:
        text = storage.read_text(name)
    except FileNotFoundError:
        abort(404)
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/users/add", methods=["POST"])
@admin_required
def users_add():
    try:
        users.add(request.form.get("username", ""), request.form.get("password", ""),
                  request.form.get("scope", "all"), request.form.get("name", ""))
        flash(f"User '{request.form.get('username','').strip().lower()}' saved.", "success")
    except Exception as exc:                 # noqa: BLE001
        flash(f"Could not save user: {exc}", "danger")
    return redirect(url_for("admin.admin_home"))


@bp.route("/users/remove", methods=["POST"])
@admin_required
def users_remove():
    users.remove(request.form.get("username", ""))
    flash("User removed.", "success")
    return redirect(url_for("admin.admin_home"))


@bp.route("/refresh-cache", methods=["POST"])
@admin_required
def refresh_cache():
    _invalidate_all()
    flash("Caches cleared; files will be re-read on the next request.", "success")
    return redirect(url_for("admin.admin_home"))
