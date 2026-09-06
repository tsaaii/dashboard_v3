"""
Full-filter Records API client for the /reports page.

Separate from data/records.py — that one's hard-wired for the dashboard's
site+date weight aggregates. This one passes through the full set of
filter parameters supported by the upstream FastAPI so the reports UI
can run arbitrary queries.

Caching strategy
    - /filters response cached 30 min (rarely changes)
    - /records response cached 60 s by filter signature

Each fetch returns a `_meta` block with diagnostic info (upstream URL,
elapsed time in ms, cache hit/miss). The /reports UI surfaces this in
a debug strip so you can copy-paste the URL into a new tab to compare.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

import config
from data._cache import TTLCache

logger = logging.getLogger(__name__)

# Pooled session => HTTP keep-alive => ~150 ms saved per call to Cloud Run.
_session = requests.Session()
_session_lock = threading.Lock()

_filters_cache = TTLCache(30 * 60)
_records_cache = TTLCache(60)

# All filter params accepted by the upstream API.
ALLOWED_FILTERS: set[str] = {
    "agency_name", "site_name", "cluster", "material",
    "start_date", "end_date", "vehicle_no", "ticket_no",
    "material_type", "user_name", "site_incharge",
    "transfer_party_name", "record_status",
    "min_net_weight", "max_net_weight",
}

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000        # upstream hard ceiling per page
EXPORT_HARD_CAP = 5000  # PDF export will refuse more than this many rows

# Fields that are noise for the UI — strip from every record before sending
# to the browser. Cuts ~12 KB / page off the JSON payload.
_STRIP_FIELDS: set[str] = {
    "_source_file", "_processed_timestamp", "_folder_source",
    "first_front_image", "first_back_image",
    "second_front_image", "second_back_image",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _signature(params: dict) -> str:
    """Stable hash of filter params for cache keying."""
    norm = {k: params[k] for k in sorted(params) if params[k] not in (None, "")}
    return hashlib.md5(json.dumps(norm, sort_keys=True).encode()).hexdigest()


def _clean_params(raw: dict) -> dict:
    """Drop empty values, restrict to ALLOWED_FILTERS, coerce numerics."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if k not in ALLOWED_FILTERS:
            continue
        if v in (None, "", "null"):
            continue
        if k in ("min_net_weight", "max_net_weight"):
            try:
                out[k] = float(v)
            except (ValueError, TypeError):
                continue
        else:
            out[k] = str(v).strip()
    return out


def _build_upstream_url(endpoint: str, params: dict) -> str:
    """Construct the URL we would (or did) hit upstream — for display
    in the UI debug strip and for log lines.
    """
    base = f"{config.RECORDS_API_BASE}{endpoint}"
    if params:
        return f"{base}?{urlencode(params, doseq=False)}"
    return base


def _cache_has(cache: TTLCache, key: str) -> bool:
    """Probe a TTLCache without triggering a load. Used to decide
    hit/miss BEFORE calling get_or_load (which doesn't expose this).
    Reads private state but only for diagnostics — safe enough.
    """
    with cache._lock:
        entry = cache._store.get(key)
        return bool(entry and entry[0] > time.time())


def _strip_record(record: dict) -> dict:
    """Drop noisy / unused fields from a single record."""
    return {k: v for k, v in record.items()
            if k not in _STRIP_FIELDS and not k.startswith("_")}


def _do_fetch_records(params: dict) -> dict:
    """Actual upstream call. Returns the upstream JSON, or an error stub."""
    url = f"{config.RECORDS_API_BASE}/records"
    started = time.monotonic()
    try:
        with _session_lock:
            r = _session.get(url, params=params,
                             timeout=config.RECORDS_API_TIMEOUT_S)
        r.raise_for_status()
        payload = r.json() or {}
        payload["_upstream_ms"] = int((time.monotonic() - started) * 1000)
        return payload
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning("Records fetch failed in %d ms: %s", elapsed_ms, exc)
        return {
            "records": [],
            "pagination": {"page": 1, "limit": 0, "total": 0, "pages": 0},
            "_error": str(exc),
            "_upstream_ms": elapsed_ms,
        }


def _fetch_filter_values() -> dict:
    url = f"{config.RECORDS_API_BASE}/filters"
    try:
        with _session_lock:
            r = _session.get(url, timeout=config.RECORDS_API_TIMEOUT_S)
        r.raise_for_status()
        return r.json() or {}
    except Exception as exc:
        logger.warning("Filters fetch failed: %s", exc)
        return {"filters": {}, "_error": str(exc)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_filter_values() -> dict:
    """Cached fetch of the upstream /filters payload (raw)."""
    return _filters_cache.get_or_load("filters", _fetch_filter_values)


def normalize_filter_options(raw: dict) -> dict:
    """Trim + dedupe + sort each value list. Returns a shape ready for
    direct use in dropdowns. Date and weight ranges become flat fields.
    """
    f = (raw or {}).get("filters") or {}

    def clean(vals):
        return sorted({(v or "").strip() for v in (vals or []) if v and v.strip()})

    return {
        "agencies":         clean(f.get("agencies")),
        "sites":            clean(f.get("sites")),
        "clusters":         clean(f.get("clusters")),
        "materials":        clean(f.get("materials")),
        "transfer_parties": clean(f.get("transfer_parties")),
        "site_incharges":   clean(f.get("site_incharges")),
        "users":            clean(f.get("users")),
        "date_min":   (f.get("date_range") or {}).get("min_date", ""),
        "date_max":   (f.get("date_range") or {}).get("max_date", ""),
        "weight_min": (f.get("net_weight_range") or {}).get("min_weight", 0),
        "weight_max": (f.get("net_weight_range") or {}).get("max_weight", 0),
    }


def fetch_records(
    filters: dict,
    page: int = 1,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Cached single-page fetch with diagnostic meta.

    Returns: {
        records:    list[dict]   (with noise fields stripped)
        pagination: dict
        filters_applied: dict
        total_records_before_filters: int
        _meta: {
            upstream_url: str    (full URL incl. query string)
            took_ms: int         (total time spent in this call)
            upstream_ms: int     (time of the upstream HTTP call only;
                                  0 if served from cache)
            cached: bool         (true if served from cache)
            error: str | None
        }
    }
    """
    overall_start = time.monotonic()

    params = _clean_params(filters)
    params["page"] = max(1, int(page))
    params["limit"] = max(1, min(int(limit), MAX_LIMIT))

    sig = _signature(params)
    cache_key = f"records:{sig}"
    was_cached = _cache_has(_records_cache, cache_key)

    payload = _records_cache.get_or_load(
        cache_key,
        lambda: _do_fetch_records(params),
    )

    overall_ms = int((time.monotonic() - overall_start) * 1000)
    upstream_ms = 0 if was_cached else int(payload.get("_upstream_ms") or 0)

    # Strip private fields from each record for the wire response
    records = [_strip_record(r) for r in (payload.get("records") or [])]

    result = {
        "records": records,
        "pagination": payload.get("pagination", {}),
        "filters_applied": payload.get("filters_applied", {}),
        "total_records_before_filters": payload.get("total_records_before_filters"),
        "_meta": {
            "upstream_url": _build_upstream_url("/records", params),
            "took_ms": overall_ms,
            "upstream_ms": upstream_ms,
            "cached": was_cached,
            "error": payload.get("_error"),
        },
    }
    logger.info(
        "fetch_records cached=%s took_ms=%d upstream_ms=%d total=%s",
        was_cached, overall_ms, upstream_ms,
        result["pagination"].get("total"),
    )
    return result


def fetch_all_records(filters: dict, hard_cap: int = EXPORT_HARD_CAP) -> dict:
    """Pull every record matching `filters` (paginated), capped at hard_cap.

    Returns: {records, total_in_db, capped (bool), error (str|None)}.
    Used by the PDF exporter so it sees the same data the user is seeing.
    """
    page = 1
    limit = MAX_LIMIT
    out: list[dict] = []
    total = 0
    error = None
    capped = False

    while True:
        result = fetch_records(filters, page=page, limit=limit)
        meta = result.get("_meta") or {}
        if meta.get("error"):
            error = meta["error"]
            break

        batch = result.get("records") or []
        pagination = result.get("pagination") or {}
        total = pagination.get("total", total)

        if not batch:
            break

        if len(out) + len(batch) > hard_cap:
            out.extend(batch[: hard_cap - len(out)])
            capped = True
            break

        out.extend(batch)
        if page >= pagination.get("pages", 0):
            break
        page += 1

    return {
        "records": out,
        "total_in_db": total,
        "capped": capped,
        "error": error,
    }


def invalidate_cache() -> None:
    _filters_cache.invalidate()
    _records_cache.invalidate()
