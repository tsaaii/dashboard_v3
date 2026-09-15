"""
Phase-wise site assignments — sites_phases.csv, uploaded from /admin.

One row per (phase, site). The same site appears once per phase it was
assigned work in.

    location,site_name,phase,agency_name,cluster,target_mt,remediated_mt,
    soil_disposed_mt,rdf_disposed_mt,cnd_disposed_mt,inert_disposed_mt,
    start_date,deadline_date,status,link_to,notes

    location       the PLACE, stable across contractors: "Kadiri". Rows with the
                   same location are grouped together. Defaults to site_name.
    site_name      the name used in that contract/phase: "Tharuni Kadiri",
                   "Kadiri2". Shown as typed.
    phase          free text for the dropdown: Previous, Phase1, ...
    target_mt / remediated_mt          assigned and done in that phase
    *_disposed_mt  outputs of that phase (soil, RDF, C&D, inert); optional
    link_to        which sites_master.csv site_name this row opens when
                   clicked. Defaults to site_name. Use it when the live page
                   is under a different name ("Tharuni_Kadiri") than the row.
    status         active | completed | not started (free text)

Numbers may contain commas ("1,20,000"). Missing file => empty list, and
/sites falls back to the current sites_master.csv view.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime

import config
from data import master, storage
from data._cache import TTLCache

logger = logging.getLogger(__name__)
FILE = "sites_phases.csv"
REQUIRED = ["phase", "site_name", "agency_name", "target_mt"]
_cache = TTLCache(config.CACHE_TTL_SECONDS)


def _num(v) -> float:
    try:
        return float(str(v or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _iso(v: str) -> str:
    """Accept 2026-09-30, 30-09-2026, 30/09/2026; return ISO or ''."""
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return v


def phase_rank(name: str) -> tuple:
    """'Phase 3 - current' first, then Phase 2, Phase 1, 'Phase 1 - 15% Excess',
    and Old/Previous last — the order the ULB screen shows tabs in."""
    n = name.lower()
    if n.startswith(("old", "prev")):
        return (3, 0, n)
    m = re.search(r"(\d+)", n)
    num = int(m.group(1)) if m else 0
    if "current" in n:
        return (0, -num, n)
    extra = 1 if (m and n.strip() != f"phase {num}") else 0
    return (1, -num, extra, n)


def _load() -> list[dict]:
    try:
        text = storage.read_text(FILE)
    except FileNotFoundError:
        return []
    text = text.lstrip("\ufeff")
    by_name = {master.slugify(s.site_name): s.slug for s in master.get_sites() if s.is_renderable}
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text))):
        name = (r.get("site_name") or "").strip()
        if not name:
            continue
        target, done = _num(r.get("target_mt")), _num(r.get("remediated_mt"))
        link = (r.get("link_to") or name).strip()
        disp = {k: _num(r.get(f"{k}_disposed_mt")) for k in ("soil", "rdf", "cnd", "inert")}
        rows.append({
            "id": i,
            "location": (r.get("location") or name).strip(),
            "phase": (r.get("phase") or "").strip() or "Unspecified",
            "site_name": name,
            **{f"{k}_mt": v for k, v in disp.items()},
            "disposed_mt": sum(disp.values()),
            "agency_name": (r.get("agency_name") or "").strip(),
            "cluster": (r.get("cluster") or "").strip(),
            "target_mt": target,
            "remediated_mt": done,
            "pct": min(100.0, done / target * 100) if target else 0.0,
            "start_date": _iso(r.get("start_date")),
            "deadline_date": _iso(r.get("deadline_date")),
            "status": (r.get("status") or "").strip().lower(),
            "notes": (r.get("notes") or "").strip(),
            "slug": by_name.get(master.slugify(link)),       # None => no live page
        })
    logger.info("Loaded %d phase rows", len(rows))
    return rows


def all_rows() -> list[dict]:
    return _cache.get_or_load("phases", _load)


def phases() -> list[str]:
    """Distinct phase names, in a sensible order: Previous/Old first, then the rest as in the file."""
    return sorted({r["phase"] for r in all_rows()}, key=phase_rank)


def invalidate() -> None:
    _cache.invalidate()
