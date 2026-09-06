"""
Pure aggregation. Takes spine sites and produces the exact dicts the
templates render. No I/O here, no Flask, no records API, no overrides.

Every metric on every one of the 12 cards is derived from the columns
on each Site (loaded from sites_master.csv).

Card map:
    Top 4 (project-level)
        1  Project Overview          remediated / target
        2  RDF Disposal Status       Σ rdf_disposed_mt / (15% × Σ target_mt)
        3  Required Performance      synthetic today / Σ daily_required
        4  Site Reclamation Status   buckets by completion_pct (inactive → <50%)

    8 per-agency (rotating slide)
        5  Agency Progress           same as 1, scoped
        6  Agency RDF Disposal       same as 2, scoped
        7  Agency Performance        same as 3, scoped
        8  Site Reclamation Status   same as 4, scoped
        9  Outward Performance       active sites where remediated_mt == 0
        10 Lagging Sites             active sites, ascending completion %
        11 Disposal Statistics       Σ soil / rdf / cnd / inert per agency
        12 Land Reclaimed            Σ land_reclaimed / Σ land_total
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

import config
from data.master import Site


# ===========================================================================
# Number formatting
# ===========================================================================

def fmt_int_indian(n: float) -> str:
    """Indian thousand-separator: 1,23,45,678."""
    if n is None:
        return "—"
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return "—"
    if n == 0:
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


def fmt_mt(value: float, decimals: int = 0) -> str:
    if value is None:
        return "—"
    if decimals == 0:
        return f"{fmt_int_indian(value)} MT"
    return f"{fmt_int_indian(int(value))}.{int((value % 1) * 10**decimals):0{decimals}d} MT"


def fmt_pct(value: float, decimals: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}%"

def fmt_k(value: float) -> str:
    """Compact thousands format: 1300 -> '1.3K', 12500 -> '12.5K', 125000 -> '125K'."""
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(n) < 1000:
        return str(int(round(n)))
    k = n / 1000.0
    s = f"{k:.1f}".rstrip("0").rstrip(".")
    return f"{s}K"

def _size_class(s: str) -> str:
    n = len(str(s))
    if n <= 5:  return "is-lg"
    if n <= 8:  return "is-md"
    if n <= 11: return "is-sm"
    return "is-xs"


def _stat(value, label: str, color: str) -> dict:
    v = str(value)
    return {"value": v, "label": label, "color": color, "size_class": _size_class(v)}


def _today_str(value: float) -> str:
    """One decimal under 1000, Indian-comma integer otherwise."""
    return f"{value:.1f}" if value < 1000 else fmt_int_indian(int(value))


# ===========================================================================
# Math helpers
# ===========================================================================

def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _completion_pct(numer: float, denom: float, *, cap: bool = True) -> float:
    raw = _safe_div(numer, denom) * 100.0
    raw = max(0.0, raw)
    return min(100.0, raw) if cap else raw


def _days_remaining(deadline: Optional[date], today: date) -> int:
    deadline = deadline or config.project_deadline_date()
    return max(0, (deadline - today).days)


def _site_daily_required(site: Site, today: date) -> float:
    """MT/day this site must process to finish on time."""
    if site.deadline_date is not None and today > site.deadline_date:
        return 0.0
    remaining = max(0.0, site.target_mt - site.remediated_mt)
    days = max(1, _days_remaining(site.deadline_date, today))
    return remaining / days


def _synthetic_today_mt(daily_required: float) -> float:
    """Synthetic ticker for cards 3 / 7.

    Linear growth from 0 at IST midnight at TODAY_MT_RATE_PER_HOUR,
    capped at the scope's daily_required.
    """
    if daily_required <= 0:
        return 0.0
    grown = config.hours_since_midnight_ist() * config.TODAY_MT_RATE_PER_HOUR
    return min(grown, daily_required)


def _completion_color(pct: float) -> str:
    if pct >= 100: return "var(--success, #38A169)"
    if pct >= 80:  return "var(--info, #3182CE)"
    if pct >= 50:  return "var(--warning, #DD6B20)"
    return "var(--error, #E53E3E)"


def _completion_buckets(sites: Iterable[Site]) -> tuple[int, int, int, int]:
    """(>=100%, 75–99%, 50–74%, <50%) site counts.

    Only renderable sites count (blank-site_name baseline rows are skipped).
    Inactive renderable sites are forced into the <50% bucket regardless of
    completion %.
    """
    b100 = b75 = b50 = below = 0
    for s in sites:
        if not s.is_renderable:
            continue
        if not s.is_active:
            below += 1
            continue
        pct = s.completion_pct
        if pct >= 100:    b100 += 1
        elif pct >= 75:   b75  += 1
        elif pct >= 50:   b50  += 1
        else:             below += 1
    return b100, b75, b50, below


# ===========================================================================
# Card builders — one helper per layout, reused across all 12 cards
# ===========================================================================

def _ratio_card(*,
                icon: str, title: str,
                num: float, num_label: str, num_color: str,
                den: float, den_label: str, den_color: str,
                badge_variant: str = "orange",
                num_str: Optional[str] = None,
                den_str: Optional[str] = None,
                cap_pct: bool = True) -> dict:
    """Stacked numerator-over-denominator card with a % badge."""
    return {
        "icon": icon, "title": title,
        "badge_text": fmt_pct(_completion_pct(num, den, cap=cap_pct)),
        "badge_variant": badge_variant,
        "layout": "stacked",
        "stats": [
            _stat(num_str if num_str is not None else fmt_int_indian(int(num)),
                  num_label, num_color),
            _stat(den_str if den_str is not None else fmt_int_indian(int(den)),
                  den_label, den_color),
        ],
    }


def _grid_2x2_card(*, icon: str, title: str, badge_text: str,
                   stats: list[dict], badge_variant: str = "orange") -> dict:
    return {
        "icon": icon, "title": title,
        "badge_text": badge_text, "badge_variant": badge_variant,
        "layout": "grid_2x2", "stats": stats,
    }


def _list_card(*, icon: str, title: str,
               entries: list[dict],
               subtitle: Optional[str] = None,
               badge_text: Optional[str] = None,
               badge_variant: str = "orange",
               empty_text: str = "—") -> dict:
    card = {
        "icon": icon, "title": title,
        "layout": "list",
        "entries": entries, "empty_text": empty_text,
    }
    if subtitle:
        card["subtitle"] = subtitle
    if badge_text:
        card["badge_text"] = badge_text
        card["badge_variant"] = badge_variant
    return card


# ===========================================================================
# Project-level aggregation (top 4 cards)
# ===========================================================================

@dataclass
class ProjectOverview:
    total_sites: int
    active_sites: int
    total_target_mt: float
    total_remediated_mt: float
    today_mt: float
    required_today_mt: float
    overall_completion_pct: float
    rdf_disposed_mt: float
    rdf_expected_mt: float
    recl_100: int
    recl_75_99: int
    recl_50_75: int
    recl_below_50: int


def project_overview(sites: list[Site],
                     today: Optional[date] = None) -> ProjectOverview:
    today = today or config.today_ist()

    # Totals — include EVERY row regardless of status / blank site_name.
    # Per spec: "While considering Total Quantity, consider everything
    # irrespective of active site or not."
    total_target     = sum(s.target_mt for s in sites)
    total_remediated = sum(s.remediated_mt for s in sites)
    rdf_disposed     = sum(s.rdf_disposed_mt for s in sites)
    rdf_expected     = total_target * config.RDF_EXPECTED_PCT

    required_today   = sum(_site_daily_required(s, today) for s in sites)
    today_mt         = _synthetic_today_mt(required_today)

    pct = _completion_pct(total_remediated, total_target)
    # Card 4 buckets: only renderable sites; inactive forced to <50%.
    b100, b75, b50, below = _completion_buckets(sites)

    # Site counts shown in the "X Sites" badge are renderable-only.
    renderable = [s for s in sites if s.is_renderable]

    return ProjectOverview(
        total_sites=len(renderable),
        active_sites=sum(1 for s in renderable if s.is_active),
        total_target_mt=total_target,
        total_remediated_mt=total_remediated,
        today_mt=today_mt,
        required_today_mt=required_today,
        overall_completion_pct=pct,
        rdf_disposed_mt=rdf_disposed,
        rdf_expected_mt=rdf_expected,
        recl_100=b100, recl_75_99=b75, recl_50_75=b50, recl_below_50=below,
    )


def overview_cards(o: ProjectOverview) -> list[dict]:
    """The 4 top cards."""
    return [
        # Card 1
        _ratio_card(
            icon="📊", title="Project Overview",
            num=o.total_remediated_mt, num_label="REMEDIATED (MT)",
            num_color="var(--success, #38A169)",
            den=o.total_target_mt,     den_label="TARGET (MT)",
            den_color="var(--brand-primary)",
        ),
        # Card 2
        _ratio_card(
            icon="♻️", title="RDF Disposal Status",
            num=o.rdf_disposed_mt, num_label="DISPOSED (MT)",
            num_color="var(--success, #38A169)",
            den=o.rdf_expected_mt, den_label="EXPECTED (MT)",
            den_color="var(--brand-primary)",
            badge_variant="green",
        ),
        # Card 3
        _ratio_card(
            icon="⚡", title="Required Performance",
            num=o.today_mt,          num_label="TODAY (MT)",
            num_color="var(--brand-primary)",
            den=o.required_today_mt, den_label="REQUIRED TODAY (MT)",
            den_color="var(--error, #E53E3E)",
            num_str=_today_str(o.today_mt),
            cap_pct=False,
        ),
        # Card 4 — buckets include inactive (forced to <50%), so badge counts total
        _grid_2x2_card(
            icon="🏭", title="Site Reclamation Status",
            badge_text=f"{o.total_sites} Sites",
            stats=[
                _stat(o.recl_100,      "100% COMPLETE", "var(--success, #38A169)"),
                _stat(o.recl_75_99,    "75–99%",        "var(--info, #3182CE)"),
                _stat(o.recl_50_75,    "50–75%",        "var(--warning, #DD6B20)"),
                _stat(o.recl_below_50, "BELOW 50%",     "var(--error, #E53E3E)"),
            ],
        ),
    ]


# Alias kept for any external imports.
def header_cards(overview: ProjectOverview) -> list[dict]:
    return overview_cards(overview)


# ===========================================================================
# Agency-level aggregation (rotating 2×4 main grid — cards 5-12)
# ===========================================================================

@dataclass
class AgencyMetrics:
    agency_name: str
    display_name: str
    sites: list[Site]
    total_sites: int
    active_sites: int
    inactive_sites: int
    clusters: list[str]
    target_mt: float
    remediated_mt: float
    today_mt: float
    completion_pct: float
    daily_rate_required: float
    site_rankings: list[dict]
    recl_100: int
    recl_75_99: int
    recl_50_75: int
    recl_below_50: int
    rdf_disposed_mt: float
    rdf_expected_mt: float
    disposal_soil_mt: float
    disposal_rdf_mt: float
    disposal_cnd_mt: float
    disposal_inert_mt: float
    land_reclaimed_acres: float
    land_to_reclaim_acres: float
    no_outward_sites: list[dict] = field(default_factory=list)


def agency_metrics(agency: str, sites: list[Site],
                   today: Optional[date] = None) -> AgencyMetrics:
    today = today or config.today_ist()

    # a_sites_all: every row for this agency. Used for sums.
    # a_sites_renderable: only rows with a real site_name. Used for card 8.
    # a_sites_active: renderable AND active. Used for cards 9 & 10.
    a_sites_all        = [s for s in sites if s.agency_name == agency]
    a_sites_renderable = [s for s in a_sites_all if s.is_renderable]
    a_sites_active     = [s for s in a_sites_renderable if s.is_active]

    # Totals — include all rows for this agency.
    target           = sum(s.target_mt for s in a_sites_all)
    remediated       = sum(s.remediated_mt for s in a_sites_all)
    rdf_disposed     = sum(s.rdf_disposed_mt for s in a_sites_all)
    rdf_expected     = target * config.RDF_EXPECTED_PCT

    disposal_soil    = sum(s.soil_disposed_mt for s in a_sites_all)
    disposal_rdf     = sum(s.rdf_disposed_mt for s in a_sites_all)
    disposal_cnd     = sum(s.cnd_disposed_mt for s in a_sites_all)
    disposal_inert   = sum(s.inert_disposed_mt for s in a_sites_all)

    land_reclaimed   = sum(s.land_reclaimed_acres for s in a_sites_all)
    land_total       = sum(s.land_total_acres for s in a_sites_all)

    daily_required   = sum(_site_daily_required(s, today) for s in a_sites_all)
    today_mt         = _synthetic_today_mt(daily_required)

    pct = _completion_pct(remediated, target)
    # Card 8 buckets: only renderable sites; inactive forced to <50%.
    b100, b75, b50, below = _completion_buckets(a_sites_renderable)

    # Card 10 (Lagging Sites) — renderable + active only.
    rankings = [{
        "site_name": s.site_name,
        "cluster": s.cluster,
        "target_mt": s.target_mt,
        "remediated_mt": s.remediated_mt,
        "completion_pct": s.completion_pct,
    } for s in a_sites_active]
    rankings.sort(key=lambda r: r["completion_pct"], reverse=True)

    # Card 9 (Outward Performance) — renderable + active sites with no outward.
    no_outward = [{
        "site_name": s.site_name,
        "cluster": s.cluster,
        "total_weight_mt": s.remediated_mt,
    } for s in a_sites_active if not s.has_outward]
    no_outward.sort(key=lambda x: x["site_name"])

    return AgencyMetrics(
        agency_name=agency,
        display_name=config.AGENCY_DISPLAY_NAMES.get(agency, agency),
        sites=a_sites_renderable,
        total_sites=len(a_sites_renderable),
        active_sites=len(a_sites_active),
        inactive_sites=len(a_sites_renderable) - len(a_sites_active),
        clusters=sorted({s.cluster for s in a_sites_renderable if s.cluster}),
        target_mt=target,
        remediated_mt=remediated,
        today_mt=today_mt,
        completion_pct=pct,
        daily_rate_required=daily_required,
        site_rankings=rankings,
        recl_100=b100, recl_75_99=b75, recl_50_75=b50, recl_below_50=below,
        rdf_disposed_mt=rdf_disposed,
        rdf_expected_mt=rdf_expected,
        disposal_soil_mt=disposal_soil,
        disposal_rdf_mt=disposal_rdf,
        disposal_cnd_mt=disposal_cnd,
        disposal_inert_mt=disposal_inert,
        land_reclaimed_acres=land_reclaimed,
        land_to_reclaim_acres=land_total,
        no_outward_sites=no_outward,
    )


def main_cards(am: AgencyMetrics) -> list[dict]:
    """The 8 main agency cards (2×4 grid, cards 5-12)."""

    # ---- Row 1 (cards 5-8) ----
    row1 = [
        # Card 5
        _ratio_card(
            icon="📋", title="Agency Progress",
            num=am.remediated_mt, num_label="TOTAL REMEDIATED (MT)",
            num_color="var(--success, #38A169)",
            den=am.target_mt,     den_label="TOTAL REQUIRED (MT)",
            den_color="var(--brand-primary)",
        ),
        # Card 6
        _ratio_card(
            icon="♻️", title="RDF Disposal Status",
            num=am.rdf_disposed_mt, num_label="DISPOSED (MT)",
            num_color="var(--success, #38A169)",
            den=am.rdf_expected_mt, den_label="EXPECTED RDF (MT)",
            den_color="var(--brand-primary)",
            badge_variant="green",
        ),
        # Card 7
        _ratio_card(
            icon="⚡", title="Agency Performance",
            num=am.today_mt,            num_label="TODAY (MT)",
            num_color="var(--brand-primary)",
            den=am.daily_rate_required, den_label="REQUIRED (MT)",
            den_color="var(--error, #E53E3E)",
            num_str=_today_str(am.today_mt),
            cap_pct=False,
        ),
        # Card 8 — same logic as card 4
        _grid_2x2_card(
            icon="🏭", title="Site Reclamation Status",
            badge_text=f"{am.total_sites} Sites",
            stats=[
                _stat(am.recl_100,      "100% COMPLETE", "var(--success, #38A169)"),
                _stat(am.recl_75_99,    "75–99%",        "var(--info, #3182CE)"),
                _stat(am.recl_50_75,    "50–75%",        "var(--warning, #DD6B20)"),
                _stat(am.recl_below_50, "BELOW 50%",     "var(--error, #E53E3E)"),
            ],
        ),
    ]

    # ---- Row 2 (cards 9-12) ----

    # Card 9 — Outward Performance
    n_no_out = len(am.no_outward_sites)
    outward_card = _list_card(
        icon="📤", title="Outward Performance",
        badge_text=(f"{n_no_out} Site{'s' if n_no_out != 1 else ''} pending"
                    if n_no_out else "All clear"),
        badge_variant=("red" if n_no_out else "green"),
        entries=[{"label": x["site_name"], "value": "No outward yet"}
                 for x in am.no_outward_sites[:5]],
        empty_text="Every site has outward processing 🎉",
    )

    # Card 10 — Lagging Sites (top 5 ascending by completion %)
    lagging = sorted(am.site_rankings, key=lambda r: r["completion_pct"])[:5]
    lagging_card = _list_card(
        icon="📉", title="Lagging Sites",
        entries=[{"label": r["site_name"],
                  "value": fmt_pct(r["completion_pct"]),
                  "color": _completion_color(r["completion_pct"])}
                 for r in lagging],
        empty_text="No active sites in this agency",
    )

    # Card 11 — Disposal Statistics
    total_disposal = (am.disposal_soil_mt + am.disposal_rdf_mt
                      + am.disposal_cnd_mt + am.disposal_inert_mt)
    disposal_card = _grid_2x2_card(
        icon="🗑️", title="Disposal Statistics",
        badge_text=fmt_mt(total_disposal),
        stats=[
            _stat(fmt_k(am.disposal_soil_mt),  "SOIL (MT)",  "var(--warning, #DD6B20)"),
            _stat(fmt_k(am.disposal_rdf_mt),   "RDF (MT)",   "var(--success, #38A169)"),
            _stat(fmt_k(am.disposal_cnd_mt),   "C&D (MT)",   "var(--info, #3182CE)"),
            _stat(fmt_k(am.disposal_inert_mt), "INERT (MT)", "var(--text-secondary)"),
        ],
    )

    # Card 12 — Land Reclaimed
    land_card = _ratio_card(
        icon="🌳", title="Land Reclaimed",
        num=am.land_reclaimed_acres,  num_label="RECLAIMED (ACRES)",
        num_color="var(--success, #38A169)",
        den=am.land_to_reclaim_acres, den_label="TO BE RECLAIMED (ACRES)",
        den_color="var(--brand-primary)",
        badge_variant="green",
    )

    return [*row1, outward_card, lagging_card, disposal_card, land_card]
