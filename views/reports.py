"""Records explorer / reports. Rebuilt in step 7."""
from __future__ import annotations

from flask import Blueprint, render_template

from views.login import login_required

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("")
@login_required
def reports_view():
    return render_template("placeholder.html", nav="reports", title="Reports", step=7)
