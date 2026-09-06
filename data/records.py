"""
Records API client.

The spine tells us *what should be remediated*. The records API tells us
*what has actually been remediated*: returns a list of weighbridge records,
each with a net_weight (kg), ticket_no, and material_type.

API contract (matches the FastAPI in /records):
    GET {RECORDS_API_BASE}/records?site_name=X&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

    -> {
         "records": [{ticket_no, net_weight, material_type, ...}, ...],
         "pagination": {"page": 1, "limit": ..., "total": N, "pages": ...},
         ...
       }
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import requests

import config
from data._cache import TTLCache
from data.master import Site

logger = logging.getLogger(__name__)
_cache = TTLCache(config.CACHE_TTL_SECONDS)

# Anything in this set (case-insensitive) is treated as raw legacy waste.
# Any other material_type means outward (post-)processing has happened
# (e.g. RDF, Soil, C&D, Inert, Compost, etc.).
LEGACY_MATERIAL_TYPES = {
    "legacy/msw",
    "legacy msw",
    "legacy",
    "msw",
}


@dataclass(frozen=True)
class SiteRecords:
    """Aggregated remediation records for one spine-site."""
    site_name: str
    total_trips: int
    total_weight_kg: float
    has_outward: bool = False   # True iff at least one record had a
                                # material_type outside LEGACY_MATERIAL_TYPES.

    @property
    def total_weight_mt(self) -> float:
        return self.total_weight_kg / 1000.0


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_outward_material(raw: str) -> bool:
    """True if the material_type indicates outward (post-)processing."""
    mat = (raw or "").strip().lower()
    if not mat:
        return False        # missing => assume legacy (conservative)
    return mat not in LEGACY_MATERIAL_TYPES


def _fetch_records_for_api_name(api_name: str, start: date, end: date) -> list[dict]:
    url = f"{config.RECORDS_API_BASE}/records"
    params = {
        "site_name": api_name,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    try:
        r = requests.get(url, params=params, timeout=config.RECORDS_API_TIMEOUT_S)
        r.raise_for_status()
        payload = r.json() or {}
    except Exception as exc:
        logger.warning("Records API failed for %s: %s", api_name, exc)
        return []

    records = payload.get("records") or []
    if not isinstance(records, list):
        logger.warning("Records API returned non-list 'records' for %s", api_name)
        return []
    return records


def _aggregate_records(site: Site, raw_per_name: list[list[dict]]) -> SiteRecords:
    """Dedupe by ticket_no across the per-api-name responses, then sum.

    Also flips has_outward=True the first time we see a record whose
    material_type is anything other than Legacy / MSW.
    """
    seen_tickets: set[str] = set()
    trips = 0
    weight_kg = 0.0
    has_outward = False

    for batch in raw_per_name:
        for rec in batch:
            ticket_no = (rec.get("ticket_no") or "").strip()
            if ticket_no:
                if ticket_no in seen_tickets:
                    continue
                seen_tickets.add(ticket_no)
            trips += 1
            nw_calc = _safe_float(rec.get("net_weight_calculated"))
            nw = _safe_float(rec.get("net_weight"))
            weight_kg += nw_calc if nw_calc > 0 else nw

            if not has_outward and _is_outward_material(rec.get("material_type")):
                has_outward = True

    return SiteRecords(
        site_name=site.site_name,
        total_trips=trips,
        total_weight_kg=weight_kg,
        has_outward=has_outward,
    )


def _records_for_site(site: Site, start: date, end: date) -> SiteRecords:
    raw_per_name = [
        _fetch_records_for_api_name(name, start, end)
        for name in site.api_site_names
    ]
    return _aggregate_records(site, raw_per_name)


def get_records_for_site(site: Site, start: date, end: date) -> SiteRecords:
    """Cached per (site_name, start, end)."""
    key = f"records:{site.site_name}:{start.isoformat()}:{end.isoformat()}"
    return _cache.get_or_load(key, lambda: _records_for_site(site, start, end))


def get_records_for_sites(
    sites: Iterable[Site], start: date, end: date
) -> dict[str, SiteRecords]:
    """Convenience: return {site_name: SiteRecords} for the given sites."""
    return {s.site_name: get_records_for_site(s, start, end) for s in sites}


def invalidate_cache() -> None:
    _cache.invalidate()
