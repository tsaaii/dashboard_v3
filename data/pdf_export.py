"""
ReportLab PDF exporter for the /reports page.

Produces a multi-section PDF where each section covers ONE
(Agency × Site × Material) slice of the filtered records.

Section layout
    1.  Section banner (agency / site / material)
    2.  Title with date range
    3.  Summary statistics
    4.  Breakdown by transfer party (skipped if only one party)
    5.  Party-wise daily detail
    6.  DETAIL RECORDS — flat table of every record in this slice,
        with the columns the user picked in the UI's column picker.

The detail table honours `columns` passed in by the caller. If
`columns` is omitted, a sensible default set is used.
"""
from __future__ import annotations

import io
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

# Landscape A4 page width = 842 pt. With 20 pt left/right margins = 802 pt usable.
_USABLE_WIDTH = 802


# ---------------------------------------------------------------------------
# Column schema — single source of truth for what every column means.
# Mirrors the COLUMNS array in static/js/reports.js. Keep them in sync.
# ---------------------------------------------------------------------------

COLUMN_SCHEMA: dict[str, dict] = {
    "date":                  {"label": "Date",            "numeric": False},
    "time":                  {"label": "Time",            "numeric": False},
    "site_name":             {"label": "Site",            "numeric": False},
    "cluster":               {"label": "Cluster",         "numeric": False},
    "agency_name":           {"label": "Agency",          "numeric": False},
    "vehicle_no":            {"label": "Vehicle No",      "numeric": False},
    "ticket_no":             {"label": "Ticket No",       "numeric": False},
    "material":              {"label": "Material",        "numeric": False},
    "material_type":         {"label": "Material Type",   "numeric": False},
    "transfer_party_name":   {"label": "Transfer Party",  "numeric": False},
    "first_weight":          {"label": "First Wt (kg)",   "numeric": True},
    "first_timestamp":       {"label": "First Time",      "numeric": False},
    "second_weight":         {"label": "Second Wt (kg)",  "numeric": True},
    "second_timestamp":      {"label": "Second Time",     "numeric": False},
    "net_weight":            {"label": "Net Wt (kg)",     "numeric": True},
    "net_weight_calculated": {"label": "Net Wt Calc",     "numeric": True},
    "site_incharge":         {"label": "Site Incharge",   "numeric": False},
    "user_name":             {"label": "User",            "numeric": False},
    "record_status":         {"label": "Status",          "numeric": False},
    "cloud_upload_timestamp":{"label": "Uploaded",        "numeric": False},
}

DEFAULT_COLUMNS: list[str] = [
    "date", "time", "site_name", "vehicle_no", "ticket_no",
    "material", "transfer_party_name",
    "first_weight", "second_weight", "net_weight",
]


def resolve_columns(keys: Optional[list[str]]) -> list[str]:
    """Sanitise a user-supplied list of column keys.

    Drops unknown keys, dedupes while preserving order, falls back to the
    default set if the result is empty.
    """
    if not keys:
        return list(DEFAULT_COLUMNS)
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        k = (k or "").strip()
        if not k or k in seen or k not in COLUMN_SCHEMA:
            continue
        seen.add(k)
        out.append(k)
    return out or list(DEFAULT_COLUMNS)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_int(n: float) -> str:
    """Indian-style thousands grouping: 1,23,45,678."""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return "0"
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    pieces: list[str] = []
    while len(head) > 2:
        pieces.append(head[-2:])
        head = head[:-2]
    if head:
        pieces.append(head)
    return sign + ",".join(reversed(pieces)) + "," + tail


def _fmt_mt(weight_kg: float, decimals: int = 3) -> str:
    return f"{weight_kg / 1000.0:.{decimals}f}"


def _record_date(record: dict) -> str:
    return (record.get("date") or "").strip() or "Unknown"


def _record_time(record: dict) -> str:
    return (record.get("time") or "").strip()


def _record_material(record: dict) -> str:
    m = (record.get("material") or record.get("material_type") or "").strip()
    return m or "Unknown"


def _record_party(record: dict) -> str:
    p = (record.get("transfer_party_name") or "").strip()
    return p or "Unknown"


def _record_weight(record: dict) -> float:
    nw_calc = _safe_float(record.get("net_weight_calculated"))
    if nw_calc > 0:
        return nw_calc
    return _safe_float(record.get("net_weight"))


def _format_date_dmy(yyyy_mm_dd: str) -> str:
    if not yyyy_mm_dd or yyyy_mm_dd == "Unknown":
        return yyyy_mm_dd
    try:
        return datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return yyyy_mm_dd


def _date_range(records: list[dict]) -> tuple[str, str, int]:
    days = sorted({_record_date(r) for r in records if _record_date(r) != "Unknown"})
    if not days:
        return "", "", 0
    return days[0], days[-1], len(days)


def _format_cell(value, numeric: bool) -> str:
    """Format a single cell value for display in the detail records table."""
    if value is None or value == "":
        return "—"
    if numeric:
        try:
            return _fmt_int(float(value))
        except (ValueError, TypeError):
            return str(value)
    return str(value)


# ---------------------------------------------------------------------------
# Style palette
# ---------------------------------------------------------------------------

_HEADER_BG    = colors.lightblue
_BREAKDOWN_BG = colors.lightgreen
_DETAIL_BG    = colors.lightgrey
_ZEBRA_ALT    = colors.beige
_TITLE_COLOR  = colors.darkblue
_SECTION_COLOR = colors.darkgreen


def _styles() -> dict:
    return {
        "cover_title": ParagraphStyle(
            name="CoverTitle", fontSize=18, alignment=TA_CENTER,
            fontName="Helvetica-Bold", textColor=_TITLE_COLOR,
            spaceAfter=14, spaceBefore=4,
        ),
        "title": ParagraphStyle(
            name="Title", fontSize=14, alignment=TA_CENTER,
            fontName="Helvetica-Bold", textColor=_TITLE_COLOR,
            spaceAfter=8, spaceBefore=10,
        ),
        "sub": ParagraphStyle(
            name="Sub", fontSize=11, alignment=TA_CENTER,
            fontName="Helvetica", textColor=colors.black, spaceAfter=4,
        ),
        "section": ParagraphStyle(
            name="Section", fontSize=12, alignment=TA_CENTER,
            fontName="Helvetica-Bold", textColor=_TITLE_COLOR,
            spaceAfter=8, spaceBefore=14,
        ),
        "attribution": ParagraphStyle(
            name="Attribution", fontSize=9, alignment=TA_CENTER,
            fontName="Helvetica", textColor=colors.darkgrey, spaceAfter=4,
        ),
        "warn": ParagraphStyle(
            name="Warn", fontSize=10, alignment=TA_CENTER,
            fontName="Helvetica-Oblique", textColor=colors.darkred,
            spaceAfter=6,
        ),
    }


# ---------------------------------------------------------------------------
# Filter pretty-printer
# ---------------------------------------------------------------------------

def _filters_summary(filters: dict) -> str:
    if not filters:
        return "All records (no filters)"
    pretty = {
        "agency_name": "Agency", "site_name": "Site", "cluster": "Cluster",
        "material": "Material", "material_type": "Material Type",
        "vehicle_no": "Vehicle", "ticket_no": "Ticket",
        "transfer_party_name": "Transfer Party",
        "user_name": "User", "site_incharge": "Site Incharge",
        "record_status": "Status",
        "min_net_weight": "Min Wt (kg)", "max_net_weight": "Max Wt (kg)",
        "start_date": "From", "end_date": "To",
    }
    parts = []
    for k, v in filters.items():
        if v in (None, "", "null"):
            continue
        parts.append(f"{pretty.get(k, k)} = {v}")
    return ", ".join(parts) if parts else "All records (no filters)"


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _section_key(record: dict) -> tuple[str, str, str]:
    agency = (record.get("agency_name") or "").strip() or "Unknown Agency"
    site   = (record.get("site_name") or "").strip()   or "Unknown Site"
    material = _record_material(record)
    return (agency, site, material)


def _group_by_section(records: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        groups[_section_key(r)].append(r)
    return groups


# ---------------------------------------------------------------------------
# Cover
# ---------------------------------------------------------------------------

def _section_header_banner(agency: str, site: str, material: str) -> Table:
    rows = [
        [agency.upper()],
        [f"SITE: {site}"],
        [f"MATERIAL: {material}"],
    ]
    table = Table(rows, colWidths=[_USABLE_WIDTH])
    table.setStyle(TableStyle([
        ("BOX",        (0, 0), (-1, -1), 2, colors.black),
        ("LINEBELOW",  (0, 0), (-1, 0),  0.5, colors.grey),
        ("LINEBELOW",  (0, 1), (-1, 1),  0.5, colors.grey),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME",   (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0),  15),
        ("FONTNAME",   (0, 1), (-1, 1),  "Helvetica-Bold"),
        ("FONTSIZE",   (0, 1), (-1, 1),  12),
        ("FONTNAME",   (0, 2), (-1, 2),  "Helvetica-Bold"),
        ("FONTSIZE",   (0, 2), (-1, 2),  12),
        ("TEXTCOLOR",  (0, 2), (-1, 2),  _SECTION_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def _cover_totals_table(total_trips: int, total_weight_kg: float,
                       n_combos: int, n_agencies: int, n_sites: int,
                       n_materials: int, n_parties: int,
                       earliest: str, latest: str, days: int) -> Table:
    if earliest and latest and earliest != latest:
        date_range = f"{_format_date_dmy(earliest)} to {_format_date_dmy(latest)}"
    elif earliest:
        date_range = _format_date_dmy(earliest)
    else:
        date_range = "—"

    data = [
        ["Metric", "Value"],
        ["Total Records (Trips)", _fmt_int(total_trips)],
        ["Total Net Weight",
         f"{_fmt_mt(total_weight_kg)} MT  ({_fmt_int(total_weight_kg)} kg)"],
        ["Date Range", date_range],
        ["Distinct Days", str(days)],
        ["Number of Reports in this PDF", _fmt_int(n_combos)],
        ["Distinct Agencies", _fmt_int(n_agencies)],
        ["Distinct Sites", _fmt_int(n_sites)],
        ["Distinct Materials", _fmt_int(n_materials)],
        ["Distinct Transfer Parties", _fmt_int(n_parties)],
    ]
    table = Table(data, colWidths=[300, 460])
    table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0),  _HEADER_BG),
        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0),  11),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), 10),
        ("ALIGN",          (0, 0), (-1, -1), "LEFT"),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",           (0, 0), (-1, -1), 0.75, colors.black),
        ("TOPPADDING",     (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA_ALT]),
    ]))
    return table


def _contents_table(keys: list[tuple[str, str, str]]) -> Table:
    header = ["#", "Agency", "Site", "Material"]
    rows = [header]
    for i, (agency, site, material) in enumerate(keys, 1):
        rows.append([str(i), agency, site, material])

    table = Table(rows, colWidths=[40, 280, 200, 240], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0),  _DETAIL_BG),
        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0),  10),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), 9),
        ("ALIGN",          (0, 0), (-1, 0),  "CENTER"),
        ("ALIGN",          (0, 1), (0, -1),  "CENTER"),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",           (0, 0), (-1, -1), 0.4, colors.grey),
        ("TOPPADDING",     (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA_ALT]),
    ]))
    return table


def _build_cover(records, groups, filters, capped, total_in_db,
                 columns: list[str], styles: dict) -> list:
    elements: list = []

    elements.append(Paragraph("Filtered Records Reports", styles["cover_title"]))
    elements.append(Paragraph(
        f"<b>Filters Applied:</b> {_filters_summary(filters or {})}",
        styles["sub"]))

    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    elements.append(Paragraph(
        f"<b>Report Generated:</b> {timestamp}", styles["sub"]))

    column_labels = ", ".join(COLUMN_SCHEMA[k]["label"] for k in columns)
    elements.append(Paragraph(
        f"<b>Detail Columns ({len(columns)}):</b> {column_labels}",
        styles["sub"]))

    if capped and total_in_db and total_in_db > len(records):
        elements.append(Paragraph(
            f"Note: result set was capped at {_fmt_int(len(records))} rows; "
            f"the live filter matches {_fmt_int(total_in_db)} records in total. "
            f"Tighten the filters for full coverage.",
            styles["warn"]))

    elements.append(Spacer(1, 6))

    earliest, latest, days = _date_range(records)
    total_trips = len(records)
    total_weight = sum(_record_weight(r) for r in records)
    n_combos = len(groups)

    distinct_agencies = sorted({k[0] for k in groups.keys()})
    distinct_sites    = sorted({k[1] for k in groups.keys()})
    distinct_materials = sorted({k[2] for k in groups.keys()})
    distinct_parties = sorted({_record_party(r) for r in records})

    elements.append(Paragraph("OVERALL TOTALS", styles["section"]))
    elements.append(_cover_totals_table(
        total_trips=total_trips,
        total_weight_kg=total_weight,
        n_combos=n_combos,
        n_agencies=len(distinct_agencies),
        n_sites=len(distinct_sites),
        n_materials=len(distinct_materials),
        n_parties=len(distinct_parties),
        earliest=earliest, latest=latest, days=days,
    ))

    sorted_keys = sorted(groups.keys())
    elements.append(Paragraph(
        f"CONTENTS — {n_combos} REPORT{'S' if n_combos != 1 else ''}",
        styles["section"]))
    elements.append(_contents_table(sorted_keys))

    elements.append(Spacer(1, 14))
    elements.append(Paragraph("─" * 50, styles["attribution"]))
    elements.append(Paragraph(
        "Each report below contains the full breakdown plus a detail records "
        "table for one Agency × Site × Material combination.",
        styles["attribution"]))

    return elements


# ---------------------------------------------------------------------------
# Per-section building blocks
# ---------------------------------------------------------------------------

def _section_summary_table(records: list[dict]) -> Table:
    earliest, latest, days = _date_range(records)
    if earliest and latest and earliest != latest:
        date_range = f"{_format_date_dmy(earliest)} to {_format_date_dmy(latest)}"
    elif earliest:
        date_range = _format_date_dmy(earliest)
    else:
        date_range = "—"

    total_weight = sum(_record_weight(r) for r in records)
    parties = {_record_party(r) for r in records}
    vehicles = {(r.get("vehicle_no") or "").strip()
                for r in records if (r.get("vehicle_no") or "").strip()}

    data = [
        ["Metric", "Value"],
        ["Total Trips", _fmt_int(len(records))],
        ["Total Net Weight",
         f"{_fmt_mt(total_weight)} MT  ({_fmt_int(total_weight)} kg)"],
        ["Date Range", date_range],
        ["Distinct Days", str(days)],
        ["Distinct Transfer Parties", str(len(parties))],
        ["Distinct Vehicles", str(len(vehicles))],
    ]
    table = Table(data, colWidths=[260, 500])
    table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0),  _HEADER_BG),
        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0),  11),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), 10),
        ("ALIGN",          (0, 0), (-1, -1), "LEFT"),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",           (0, 0), (-1, -1), 0.75, colors.black),
        ("TOPPADDING",     (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA_ALT]),
    ]))
    return table


def _section_party_breakdown(records: list[dict]) -> Optional[Table]:
    by_party: dict[str, dict] = defaultdict(lambda: {"trips": 0, "weight_kg": 0.0})
    total_weight = 0.0
    for r in records:
        wt = _record_weight(r)
        p = _record_party(r)
        by_party[p]["trips"] += 1
        by_party[p]["weight_kg"] += wt
        total_weight += wt

    if len(by_party) < 2:
        return None

    data = [["Transfer Party", "Trips", "Total Weight (MT)", "% of Total"]]
    rows = sorted(by_party.items(), key=lambda kv: kv[1]["weight_kg"], reverse=True)
    for name, st in rows:
        pct = (st["weight_kg"] / total_weight * 100.0) if total_weight else 0.0
        data.append([
            name,
            _fmt_int(st["trips"]),
            _fmt_mt(st["weight_kg"]),
            f"{pct:.1f}%",
        ])

    table = Table(data, colWidths=[360, 120, 180, 120])
    table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0),  _BREAKDOWN_BG),
        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0),  11),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), 10),
        ("ALIGN",          (0, 0), (-1, 0),  "CENTER"),
        ("ALIGN",          (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",           (0, 0), (-1, -1), 0.75, colors.black),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA_ALT]),
    ]))
    return table


def _section_party_daily_table(records: list[dict]) -> Table:
    by_party_date: dict = defaultdict(
        lambda: defaultdict(lambda: {"trips": 0, "weight_kg": 0.0}))
    by_party_total: dict[str, dict] = defaultdict(
        lambda: {"trips": 0, "weight_kg": 0.0})

    for r in records:
        p = _record_party(r)
        d = _record_date(r)
        w = _record_weight(r)
        by_party_date[p][d]["trips"] += 1
        by_party_date[p][d]["weight_kg"] += w
        by_party_total[p]["trips"] += 1
        by_party_total[p]["weight_kg"] += w

    header = ["Transfer Party", "Date", "Trips", "Total Weight (MT)"]
    data = [header]

    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0),  _DETAIL_BG),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  10),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 9),
        ("ALIGN",         (0, 0), (-1, 0),  "CENTER"),
        ("ALIGN",         (2, 1), (3, -1),  "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]

    party_order = sorted(by_party_total.keys(),
                         key=lambda p: by_party_total[p]["weight_kg"], reverse=True)
    for party in party_order:
        p_stats = by_party_total[party]
        total_row_idx = len(data)
        data.append([
            f"{party} — TOTAL",
            "",
            _fmt_int(p_stats["trips"]),
            _fmt_mt(p_stats["weight_kg"]),
        ])
        style_cmds += [
            ("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), _BREAKDOWN_BG),
            ("FONTNAME",   (0, total_row_idx), (-1, total_row_idx), "Helvetica-Bold"),
        ]

        for date in sorted(by_party_date[party].keys()):
            stats = by_party_date[party][date]
            data.append([
                "",
                _format_date_dmy(date),
                _fmt_int(stats["trips"]),
                _fmt_mt(stats["weight_kg"]),
            ])

    col_widths = [320, 160, 130, 190]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle(style_cmds))
    return table


# ---------------------------------------------------------------------------
# Detail records table — NEW
# ---------------------------------------------------------------------------

def _detail_records_table(records: list[dict], columns: list[str]) -> Table:
    """Flat record-by-record table for this section, using the picked columns.

    Records are sorted by (date, time) for consistent ordering. Numeric
    columns are right-aligned. Body font size auto-shrinks if many
    columns were chosen so it still fits landscape A4.
    """
    if not columns:
        columns = list(DEFAULT_COLUMNS)

    schemas = [(k, COLUMN_SCHEMA[k]) for k in columns]

    # Header row
    header = [s[1]["label"] for s in schemas]
    data: list[list] = [header]

    # Sort records by date, then time (consistent + chronological)
    sorted_records = sorted(
        records,
        key=lambda r: (_record_date(r), _record_time(r)),
    )

    for r in sorted_records:
        row = []
        for key, meta in schemas:
            row.append(_format_cell(r.get(key), meta["numeric"]))
        data.append(row)

    # Column widths: equal split of usable width
    n_cols = len(schemas)
    col_widths = [_USABLE_WIDTH / n_cols] * n_cols

    # Heuristic font sizing — many columns => smaller font
    if n_cols <= 7:
        body_font = 9
        header_font = 10
        pad_v = 3
    elif n_cols <= 11:
        body_font = 8
        header_font = 9
        pad_v = 2
    else:
        body_font = 7
        header_font = 8
        pad_v = 2

    style_cmds = [
        ("BACKGROUND",     (0, 0), (-1, 0),  _DETAIL_BG),
        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0),  header_font),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), body_font),
        ("ALIGN",          (0, 0), (-1, 0),  "CENTER"),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",           (0, 0), (-1, -1), 0.3, colors.grey),
        ("TOPPADDING",     (0, 0), (-1, -1), pad_v),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), pad_v),
        ("LEFTPADDING",    (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA_ALT]),
    ]

    # Right-align numeric columns
    for i, (_key, meta) in enumerate(schemas):
        if meta["numeric"]:
            style_cmds.append(("ALIGN", (i, 1), (i, -1), "RIGHT"))

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle(style_cmds))
    return table


# ---------------------------------------------------------------------------
# Section assembly
# ---------------------------------------------------------------------------

def _build_section(key: tuple[str, str, str],
                   records: list[dict],
                   columns: list[str],
                   styles: dict,
                   index: int,
                   total_sections: int) -> list:
    agency, site, material = key
    elements: list = []

    # ---- Banner ----
    elements.append(_section_header_banner(agency, site, material))

    # ---- Title ----
    earliest, latest, days = _date_range(records)
    if earliest and latest and earliest != latest:
        title_range = f"{_format_date_dmy(earliest)} to {_format_date_dmy(latest)}"
    elif earliest:
        title_range = _format_date_dmy(earliest)
    else:
        title_range = "All Records"

    elements.append(Paragraph(
        f"<b>REPORT — {title_range}</b>", styles["title"]))
    elements.append(Paragraph(
        f"Report {index} of {total_sections}", styles["sub"]))

    elements.append(Spacer(1, 6))

    # ---- Summary stats ----
    elements.append(Paragraph("SUMMARY STATISTICS", styles["section"]))
    elements.append(_section_summary_table(records))

    # ---- Party breakdown (only if 2+ parties) ----
    party_breakdown = _section_party_breakdown(records)
    if party_breakdown is not None:
        elements.append(Paragraph("BREAKDOWN BY TRANSFER PARTY", styles["section"]))
        elements.append(party_breakdown)

    # ---- Daily detail per party ----
    elements.append(Paragraph("PARTY-WISE DAILY DETAIL", styles["section"]))
    elements.append(_section_party_daily_table(records))

    # ---- Detail records — NEW ----
    elements.append(Paragraph(
        f"DETAIL RECORDS — {_fmt_int(len(records))} ROW{'S' if len(records) != 1 else ''}",
        styles["section"]))
    elements.append(_detail_records_table(records, columns))

    return elements


# ---------------------------------------------------------------------------
# Page footer
# ---------------------------------------------------------------------------

def _page_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillGray(0.5)
    page_w, _ = landscape(A4)
    canvas.drawRightString(page_w - 20, 14, f"Page {doc.page}")
    canvas.drawString(20, 14, "Swachha Andhra Monitor — Advitia Labs")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def build_summary_pdf(
    records: list[dict],
    *,
    filters: Optional[dict] = None,
    capped: bool = False,
    total_in_db: Optional[int] = None,
    columns: Optional[list[str]] = None,
) -> bytes:
    """Build the multi-section PDF and return raw bytes.

    Args:
        records:    Flat list of record dicts.
        filters:    Active filter dict (used in cover summary only).
        capped:     If True, the source dataset hit the export cap.
        total_in_db: Total matching rows upstream (for the capped warning).
        columns:    Ordered list of column keys to include in the per-
                    section detail records table. Pass None / empty to use
                    the default set. Unknown keys are silently dropped.
    """
    columns = resolve_columns(columns)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        rightMargin=20, leftMargin=20, topMargin=24, bottomMargin=30,
        title="Filtered Records Reports",
        author="Swachha Andhra Corporation",
    )
    styles = _styles()
    elements: list = []

    if not records:
        elements.append(Paragraph(
            "Filtered Records Reports", styles["cover_title"]))
        elements.append(Paragraph(
            "No records matched the active filter set.", styles["sub"]))
        doc.build(elements,
                  onFirstPage=_page_footer, onLaterPages=_page_footer)
        return buf.getvalue()

    groups = _group_by_section(records)
    sorted_keys = sorted(groups.keys())

    # ---- Cover ----
    elements.extend(_build_cover(
        records, groups, filters or {}, capped, total_in_db,
        columns, styles))

    # ---- One section per (agency, site, material) ----
    for i, key in enumerate(sorted_keys, 1):
        elements.append(PageBreak())
        elements.extend(_build_section(
            key, groups[key], columns, styles,
            index=i, total_sections=len(sorted_keys),
        ))

    doc.build(elements,
              onFirstPage=_page_footer, onLaterPages=_page_footer)
    return buf.getvalue()


def build_pdf_filename(filters: dict) -> str:
    bits = []
    site = (filters.get("site_name") or "").strip()
    agency = (filters.get("agency_name") or "").strip()
    if site:
        bits.append(site.replace(" ", "_"))
    elif agency:
        bits.append(agency.replace(" ", "_")[:30])

    start = (filters.get("start_date") or "").strip()
    end = (filters.get("end_date") or "").strip()
    if start and end:
        bits.append(f"{start}_to_{end}")
    elif start:
        bits.append(f"from_{start}")
    elif end:
        bits.append(f"to_{end}")

    if not bits:
        bits.append("all_records")

    bits.append(datetime.now().strftime("%Y%m%d_%H%M%S"))
    return "consolidated_reports_" + "_".join(bits) + ".pdf"
