"""Sites list and per-site detail. Rebuilt in steps 5 and 6."""
from __future__ import annotations

from flask import Blueprint, render_template

from views.login import login_required

bp = Blueprint("sites", __name__, url_prefix="/sites")


@bp.route("")
@login_required
def sites_list():
    return render_template("placeholder.html", nav="sites", title="Sites", step=5)


@bp.route("/<slug>")
@login_required
def site_view(slug):
    return render_template("placeholder.html", nav="sites", title=slug, step=6)
