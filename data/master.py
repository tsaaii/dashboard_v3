"""
Load + cache the master sites CSV — the single source of truth for every
metric on the 12-card dashboard.

Reads from config.SITES_MASTER_CSV which is either a local path or a
gs:// URL. No other I/O.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import config
from data._cache import TTLCache

logger = logging.getLogger(__name__)
_cache = TTLCache(config.CACHE_TTL_SECONDS)
_CACHE_KEY = "sites_master"


def slugify(name: str) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, trim.

    'Tharuni Srikakulam' -> 'tharuni-srikakulam'; 'Ananthapur' -> 'ananthapur'.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return s.strip("-")


@dataclass(frozen=True)
class Site:
    # Identity. site_name may be EMPTY for baseline / carry-in rows
    # (e.g. Old_Remediation_2024). Empty-site rows count toward agency
    # and project totals but are excluded from per-site cards.
    site_name: str
    api_site_names: tuple[str, ...]   # may be empty for baseline rows
    agency_name: str
    cluster: str
    # Targets / dates
    target_mt: float
    remediated_mt: float               # explicit column, NOT computed
    deadline_date: Optional[date]
    status: str                        # active / inactive
    reclamation_status: str
    start_date: Optional[date]
    # Disposal — outward processing only. Track a SUBSET of remediated_mt,
    # not the whole thing. Used by Card 11 + Card 9's has_outward signal.
    soil_disposed_mt: float = 0.0
    rdf_disposed_mt: float = 0.0
    cnd_disposed_mt: float = 0.0
    inert_disposed_mt: float = 0.0
    # Land
    land_total_acres: float = 0.0
    land_reclaimed_acres: float = 0.0
    # Per-site login (used only by the /sites/<slug> gate). Plaintext for the
    # MVP; swap to hashes later without touching the route logic.
    login_id: str = ""
    login_pwd: str = ""

    # ---- Computed properties ----

    @property
    def slug(self) -> str:
        """URL slug for the /sites/<slug> route. Derived from site_name."""
        return slugify(self.site_name)

    @property
    def is_active(self) -> bool:
        return self.status.strip().lower() == "active"

    @property
    def is_reclaimed(self) -> bool:
        return self.reclamation_status.strip().lower() == "reclaimed"

    @property
    def is_renderable(self) -> bool:
        """True iff this row represents a real site that should appear in
        site-level cards (4/8 buckets, 9 outward, 10 lagging, cluster list).
        Empty-site rows are baseline tonnage — they contribute to totals
        only.
        """
        return bool(self.site_name.strip())

    @property
    def completion_pct(self) -> float:
        """0..100, capped, never negative."""
        if self.target_mt <= 0:
            return 0.0
        raw = (self.remediated_mt / self.target_mt) * 100.0
        return max(0.0, min(100.0, raw))

    @property
    def has_outward(self) -> bool:
        """Card 9: True iff any of the four disposal columns is > 0."""
        return (self.soil_disposed_mt + self.rdf_disposed_mt
                + self.cnd_disposed_mt + self.inert_disposed_mt) > 0


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> Optional[date]:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(raw: str) -> float:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _parse_api_names(raw: str, fallback: str) -> tuple[str, ...]:
    raw = (raw or "").strip()
    if not raw:
        return (fallback.strip(),) if fallback.strip() else tuple()
    return tuple(s.strip() for s in raw.split("|") if s.strip())


def _row_to_site(row: dict) -> Optional[Site]:
    # Skip rows that have neither a site nor an agency (truly empty).
    site_name = (row.get("site_name") or "").strip()
    agency_name = (row.get("agency_name") or "").strip()
    if not site_name and not agency_name:
        return None
    return Site(
        site_name=site_name,
        api_site_names=_parse_api_names(row.get("api_site_names", ""), site_name),
        agency_name=agency_name,
        cluster=(row.get("cluster") or "").strip(),
        target_mt=_parse_float(row.get("target_mt", "0")),
        remediated_mt=_parse_float(row.get("remediated_mt", "0")),
        deadline_date=_parse_date(row.get("deadline_date", "")),
        status=(row.get("status") or "").strip(),
        reclamation_status=(row.get("reclamation_status") or "").strip(),
        start_date=_parse_date(row.get("start_date", "")),
        soil_disposed_mt=_parse_float(row.get("soil_disposed_mt", "0")),
        rdf_disposed_mt=_parse_float(row.get("rdf_disposed_mt", "0")),
        cnd_disposed_mt=_parse_float(row.get("cnd_disposed_mt", "0")),
        inert_disposed_mt=_parse_float(row.get("inert_disposed_mt", "0")),
        land_total_acres=_parse_float(row.get("land_total_acres", "0")),
        land_reclaimed_acres=_parse_float(row.get("land_reclaimed_acres", "0")),
        login_id=(row.get("login_id") or "").strip(),
        login_pwd=(row.get("login_pwd") or ""),   # do NOT strip — preserve as-is
    )


# ---------------------------------------------------------------------------
# I/O — local file or GCS, decided by the SITES_MASTER_CSV prefix
# ---------------------------------------------------------------------------

def _read_text() -> str:
    path = config.SITES_MASTER_CSV
    if config.is_gcs_path(path):
        return _read_text_from_gcs(path)
    return _read_text_from_local(path)


def _read_text_from_gcs(gs_path: str) -> str:
    from google.cloud import storage  # type: ignore
    bucket_name, key = config.parse_gcs_path(gs_path)
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(key)
    if not blob.exists():
        raise FileNotFoundError(f"{gs_path} not found")
    return blob.download_as_text()


def _read_text_from_local(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load() -> list[Site]:
    logger.info("Loading sites from %s", config.SITES_MASTER_CSV)
    text = _read_text()
    reader = csv.DictReader(io.StringIO(text))
    sites: list[Site] = []
    for row in reader:
        site = _row_to_site(row)
        if site is not None:
            sites.append(site)
    logger.info("Loaded %d sites", len(sites))
    return sites


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_sites() -> list[Site]:
    return _cache.get_or_load(_CACHE_KEY, _load)


def get_active_sites() -> list[Site]:
    return [s for s in get_sites() if s.is_active]


def get_site_by_slug(slug: str) -> Optional[Site]:
    """Resolve a URL slug to a renderable Site, or None.

    Matches case/space-insensitively via slugify() on both sides. If two
    sites slugify to the same value the first in CSV order wins.
    """
    target = slugify(slug)
    if not target:
        return None
    for s in get_sites():
        if s.is_renderable and s.slug == target:
            return s
    return None


def get_agencies() -> list[str]:
    """Distinct agency names that have at least one renderable (named) site.

    Baseline-only agencies (e.g. Old_Remediation) are excluded from the
    rotating slides — they still contribute to project totals via
    get_sites(), they just don't get their own card view.
    """
    return sorted({s.agency_name for s in get_sites()
                   if s.agency_name and s.is_renderable})


def get_all_agency_names() -> list[str]:
    """Every agency name in the CSV, including baseline-only buckets.
    Used internally for total rollups, not for rendering.
    """
    return sorted({s.agency_name for s in get_sites() if s.agency_name})


def sites_for_agency(agency_name: str) -> list[Site]:
    return [s for s in get_sites() if s.agency_name == agency_name]


def invalidate_cache() -> None:
    _cache.invalidate(_CACHE_KEY)