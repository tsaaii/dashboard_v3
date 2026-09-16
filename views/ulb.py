"""
Data by ULB — public phase-wise view of sites_phases.csv.

    GET /ulb                 the screen (all state in the query string)
    GET /ulb/export.csv      the filtered entries as CSV
    GET /ulb/export.pdf      the grouped table as a PDF

Query string:
    phase   zero or more phase names (repeat the param); none = all
    group   phase | site | agency        how the table is grouped
    q       search ULB name / location / cluster
    agency  one agency, or blank
    sort    name | count | pct | disp     dir = asc | desc
    open    all | none                    expand or collapse every group
    roll    ULB name for the roll-up box; off = entry ids excluded from it

Every number here is a plain sum over sites_phases.csv rows. "ULB" means
site_name (one town), an "entry" is one row (one phase of that town).
"""
from __future__ import annotations

import csv
import io
from datetime import date

from flask import Blueprint, Response, render_template, request, url_for

import config
from data import phases
from data.aggregate import fmt_int_indian
from data.phases import phase_rank

bp = Blueprint("ulb", __name__, url_prefix="/ulb")

SEGS = [("rdf", "RDF"), ("inert", "Inert"), ("soil", "Soil"), ("cnd", "C&D")]
GROUPS = [("phase", "By phase"), ("site", "By ULB"), ("agency", "By agency")]


# ---- aggregation helpers ---------------------------------------------------
def agg(rows: list[dict]) -> dict:
    """Sum a list of entries; add pct, disposal split and remaining."""
    o = {"target": sum(r["target_mt"] for r in rows), "rem": sum(r["remediated_mt"] for r in rows)}
    for k, _ in SEGS:
        o[k] = sum(r[f"{k}_mt"] for r in rows)
    o["disp"] = sum(o[k] for k, _ in SEGS)
    o["remaining"] = max(0.0, o["target"] - o["rem"])
    o["pct"] = min(100.0, o["rem"] / o["target"] * 100) if o["target"] else 0.0
    o["disp_pct"] = (o["disp"] / o["rem"] * 100) if o["rem"] else 0.0
    o["over"] = o["disp"] > o["rem"] * 1.001 and o["rem"] > 0
    o["gap"] = max(0.0, o["rem"] - o["disp"])
    base = max(o["disp"], o["rem"]) or 1.0
    o["segs"] = [{"key": k, "label": lbl, "mt": o[k], "w": o[k] / base * 100,
                  "pct": (o[k] / o["rem"] * 100) if o["rem"] else 0.0} for k, lbl in SEGS]
    o["n"] = len(rows)
    return o


def by_ulb(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["site_name"], []).append(r)
    return out


def bucket(pct: float) -> str:
    return "done" if pct >= 100 else "b75" if pct >= 75 else "b50" if pct >= 50 else "low"


# ---- state ------------------------------------------------------------------
def _state() -> dict:
    a = request.args
    return {
        "phases": [x.strip() for x in a.getlist("phase") if x.strip()],
        "group": a.get("group") if a.get("group") in dict(GROUPS) else "phase",
        "q": (a.get("q") or "").strip(),
        "agency": (a.get("agency") or "").strip(),
        "sort": a.get("sort") if a.get("sort") in ("name", "count", "pct", "disp") else "name",
        "desc": a.get("dir") == "desc",
        "open": a.get("open", ""),
        "roll": (a.get("roll") or "").strip(),
        "off": {int(x) for x in a.getlist("off") if x.isdigit()},
    }


def _filtered(rows: list[dict], st: dict) -> list[dict]:
    q = st["q"].lower()
    return [r for r in rows
            if (not st["phases"] or r["phase"] in st["phases"])
            and (not st["agency"] or r["agency_name"] == st["agency"])
            and (not q or q in r["site_name"].lower() or q in r["location"].lower() or q in r["cluster"].lower())]


# ---- KPIs -------------------------------------------------------------------
def _kpis(rows: list[dict], today: date) -> dict:
    a = agg(rows)
    ulbs = {name: agg(rs) for name, rs in by_ulb(rows).items()}
    done = sum(1 for u in ulbs.values() if u["pct"] >= 100)
    idle = sum(1 for u in ulbs.values() if u["rem"] <= 0)
    active = len(ulbs) - done - idle
    buckets = {"done": 0, "b75": 0, "b50": 0, "low": 0}
    for u in ulbs.values():
        buckets[bucket(u["pct"])] += 1

    starts = [r["start_date"] for r in rows if len(r["start_date"]) == 10]
    ends = [r["deadline_date"] for r in rows if len(r["deadline_date"]) == 10]
    start = min(starts) if starts else ""
    end = max(ends) if ends else config.project_deadline_date().isoformat()
    try:
        days_left = (date.fromisoformat(end) - today).days
    except ValueError:
        days_left = 0
    required = a["remaining"] / days_left if days_left > 0 else None
    elapsed = (today - date.fromisoformat(start)).days if start else 0
    avg = a["rem"] / elapsed if elapsed > 0 else None
    return {**a, "ulbs": len(ulbs), "done": done, "active": active, "idle": idle, "buckets": buckets,
            "start": start, "end": end, "days_left": days_left, "required": required, "avg": avg,
            "pace_ok": required is None or avg is None or avg >= required}


# ---- grouping ---------------------------------------------------------------
def _row(r: dict, all_rows: list[dict], name: str, sub: str) -> dict:
    a = agg([r])
    others = [o for o in all_rows if o["site_name"] == r["site_name"] and o["id"] != r["id"]]
    others.sort(key=lambda o: phase_rank(o["phase"]))
    return {**a, "r": r, "name": name, "sub": sub, "others": [{**agg([o]), "r": o} for o in others]}


def _groups(rows: list[dict], all_rows: list[dict], st: dict) -> list[dict]:
    g = st["group"]
    groups: list[dict] = []
    if g == "phase":
        for ph in sorted({r["phase"] for r in rows}, key=phase_rank):
            rs = [r for r in rows if r["phase"] == ph]
            items = [_row(r, all_rows, r["site_name"], _sub(r)) for r in rs]
            groups.append({**agg(rs), "name": ph, "meta": f"{len(by_ulb(rs))} ULBs · {len({r['agency_name'] for r in rs})} Agencies",
                           "count": len(by_ulb(rs)), "sections": [{"header": None, "rows": items}]})
    elif g == "site":
        for name, rs in by_ulb(rows).items():
            items = [_row(r, all_rows, r["phase"], _sub(r)) for r in sorted(rs, key=lambda r: phase_rank(r["phase"]))]
            groups.append({**agg(rs), "name": name, "meta": " · ".join(sorted({r["agency_name"] for r in rs})),
                           "count": len(rs), "sections": [{"header": None, "rows": items}]})
    else:  # agency -> ULB -> phase
        for ag in sorted({r["agency_name"] for r in rows}, key=str.lower):
            rs = [r for r in rows if r["agency_name"] == ag]
            sections = []
            for name, urs in by_ulb(rs).items():
                items = [_row(r, all_rows, r["phase"], _sub(r)) for r in sorted(urs, key=lambda r: phase_rank(r["phase"]))]
                sections.append({"header": {**agg(urs), "name": name, "meta": f"{len(urs)} Phase{'s' if len(urs) != 1 else ''}", "count": len(urs)}, "rows": items})
            groups.append({**agg(rs), "name": ag, "meta": f"{len(by_ulb(rs))} ULBs", "count": len(by_ulb(rs)), "sections": sections})

    # ---- sorting applies to groups, sections and rows alike ----
    key = {"name": lambda x: x["name"].lower(), "count": lambda x: x.get("count", 1),
           "pct": lambda x: x["pct"], "disp": lambda x: x["disp_pct"]}[st["sort"]]
    if st["sort"] != "name" or g != "phase":            # phases keep their natural order under name sort
        groups.sort(key=key, reverse=st["desc"])
    for grp in groups:
        if grp["sections"] and grp["sections"][0]["header"]:
            grp["sections"].sort(key=lambda s: key(s["header"]), reverse=st["desc"])
        for sec in grp["sections"]:
            if st["sort"] != "name" or g == "phase":     # in site/agency mode rows are phases in phase order
                sec["rows"].sort(key=key, reverse=st["desc"])
    return groups


def _sub(r: dict) -> str:
    bits = []
    if r["location"] and r["location"].lower() != r["site_name"].lower():
        bits.append(r["location"])
    if r["cluster"]:
        bits.append(r["cluster"])
    return " · ".join(bits)


# ---- routes -----------------------------------------------------------------
@bp.route("")
def ulb_view():
    st = _state()
    all_rows = phases.all_rows()
    if not all_rows:
        return render_template("ulb.html", nav="ulb", empty=True, st=st)
    today = config.today_ist()
    rows = _filtered(all_rows, st)

    # roll-up: one ULB, all its entries, any of them switchable off
    roll = None
    ulb_names = sorted(by_ulb(all_rows), key=str.lower)
    if st["roll"] in by_ulb(all_rows):
        entries = sorted(by_ulb(all_rows)[st["roll"]], key=lambda r: phase_rank(r["phase"]))
        on = [r for r in entries if r["id"] not in st["off"]]
        roll = {"name": st["roll"], "entries": entries, "on_ids": {r["id"] for r in on}, **agg(on)}

    groups = _groups(rows, all_rows, st)
    open_all = st["open"] == "all" or (st["open"] != "none" and len(groups) <= 1)
    st["phase_label"] = ", ".join(sorted(st["phases"], key=phase_rank)) if st["phases"] else "all phases"
    return render_template(
        "ulb.html", nav="ulb", empty=False, st=st, totals=agg(rows),
        phase_tabs=[""] + phases.phases(), group_tabs=GROUPS,
        agencies=sorted({r["agency_name"] for r in all_rows if r["agency_name"]}, key=str.lower),
        hs=_kpis(rows, today), groups=groups, n_rows=len(rows), n_ulbs=len(by_ulb(rows)),
        ulb_names=ulb_names, roll=roll, open_all=open_all, segs=SEGS,
        today_str=today.strftime("%d %b %Y"),
    )


def _export_rows(st: dict) -> list[dict]:
    return _filtered(phases.all_rows(), st)


@bp.route("/export.csv")
def export_csv():
    st = _state()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ulb", "location", "phase", "agency", "cluster", "target_mt", "remediated_mt", "pct",
                "rdf_mt", "inert_mt", "soil_mt", "cnd_mt", "disposed_mt", "start_date", "deadline_date", "status", "notes"])
    for r in _export_rows(st):
        w.writerow([r["site_name"], r["location"], r["phase"], r["agency_name"], r["cluster"],
                    round(r["target_mt"], 2), round(r["remediated_mt"], 2), round(r["pct"], 1),
                    round(r["rdf_mt"], 2), round(r["inert_mt"], 2), round(r["soil_mt"], 2), round(r["cnd_mt"], 2),
                    round(r["disposed_mt"], 2), r["start_date"], r["deadline_date"], r["status"], r["notes"]])
    name = f"ulb_{'_'.join(st['phases']) or 'all-phases'}_{config.today_ist().isoformat()}.csv".replace(" ", "_")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/export.pdf")
def export_pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = _state()
    all_rows = phases.all_rows()
    rows = _filtered(all_rows, st)
    hs = _kpis(rows, config.today_ist())
    groups = _groups(rows, all_rows, st)
    f = fmt_int_indian
    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
    story = [Paragraph("Swachh Andhra · Data by ULB", styles["Title"]),
             Paragraph(f"{', '.join(st['phases']) or 'All phases'} · {dict(GROUPS)[st['group']]} · {hs['ulbs']} ULBs · "
                       f"remediated {f(hs['rem'])} of {f(hs['target'])} MT ({hs['pct']:.1f}%) · disposed {f(hs['disp'])} MT · "
                       f"generated {config.today_ist().strftime('%d %b %Y')}", styles["Normal"]), Spacer(1, 6)]
    data = [["ULB / phase", "Agency", "Target MT", "Remediated", "%", "Disposal (RDF · Inert · Soil · C&D)"]]
    bold = []
    for g in groups:
        bold.append(len(data))
        data.append([g["name"], g["meta"], f(g["target"]), f(g["rem"]), f"{g['pct']:.1f}%", f"{f(g['disp'])} MT · {g['disp_pct']:.0f}%"])
        for sec in g["sections"]:
            if sec["header"]:
                h = sec["header"]; data.append(["   " + h["name"], h["meta"], f(h["target"]), f(h["rem"]), f"{h['pct']:.1f}%", f"{f(h['disp'])} MT"])
            for it in sec["rows"]:
                r = it["r"]
                data.append([("      " if sec["header"] else "   ") + it["name"] + (f" · {it['sub']}" if it["sub"] else ""), r["agency_name"],
                             f(it["target"]), f(it["rem"]), f"{it['pct']:.1f}%",
                             " · ".join(f"{s['label']} {f(s['mt'])}" for s in it["segs"] if s["mt"]) or "—"])
    t = Table(data, colWidths=[75 * mm, 50 * mm, 28 * mm, 28 * mm, 16 * mm, 76 * mm], repeatRows=1)
    ts = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("FONT", (0, 1), (-1, -1), "Helvetica", 7.5),
          ("ALIGN", (2, 0), (4, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), .6, colors.black),
          ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f1")]), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    for i in bold:
        ts += [("FONT", (0, i), (-1, i), "Helvetica-Bold", 8), ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#e6f1ea"))]
    t.setStyle(TableStyle(ts))
    story.append(t)
    doc.build(story)
    name = f"ulb_{'_'.join(st['phases']) or 'all-phases'}_{config.today_ist().isoformat()}.pdf".replace(" ", "_")
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
