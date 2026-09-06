"""
Live records client for the per-site dashboard explorer.

Talks to config.RECORDS_API_BASE (the weighbridge public API). ONLY the
records explorer uses this — the four headline tiles come from the spine
(sites_master.csv) and need no network call.

Design:
  * Filtering is pushed SERVER-SIDE (site_name / start_date / end_date /
    material) so we never pull the full ~140k-row table to filter in Python.
  * Results are deduped by (site_name, ticket_no), keeping the row with the
    latest _processed_timestamp — mirrors the upstream processor, so a
    reprocessed / corrected ticket doesn't double-count.
  * net_weight is KILOGRAMS upstream; we expose MT (÷1000).
  * Identical queries are cached for a short TTL.

If the API envelope differs from what's assumed here, only _extract_records
and _paginate need to change.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterable, Optional

import requests

import config
from data._cache import TTLCache

logger = logging.getLogger(__name__)

# Short cache so repeated identical queries (e.g. re-render, PDF print) don't
# re-hit the API. Keyed by the full filter tuple.
_query_cache = TTLCache(getattr(config, "RECORDS_QUERY_TTL_SECONDS", 120))

# ---- Pagination knobs (the part most likely to need tuning) ----
_PAGE_PARAM = "page"
_PAGE_SIZE_PARAM = "page_size"
_PAGE_SIZE = 1000      # only helps if the server honors a larger size
_MAX_PAGES = 500       # higher, but still a guard        # hard ceiling so a bad response can't loop forever
_TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _base() -> str:
    base = getattr(config, "RECORDS_API_BASE", "") or ""
    return base.rstrip("/")


def describe_calls(api_names: tuple[str, ...],
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None,
                   material: Optional[str] = None) -> list[str]:
    """The exact page-1 GET URL(s) query_records() will hit — for the UI box.

    One per upstream name. Pagination just bumps `page`. `vehicle` is NOT here
    because it's filtered client-side, not sent to the API.
    """
    from urllib.parse import urlencode
    calls: list[str] = []
    for name in (api_names or ()):
        params: dict = {"site_name": name}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if material:
            params["material"] = material
        params[_PAGE_PARAM] = 1
        params[_PAGE_SIZE_PARAM] = _PAGE_SIZE
        calls.append(f"{_base()}/records?{urlencode(params)}")
    return calls


def _get(params: dict) -> Any:
    url = f"{_base()}/records"
    resp = requests.get(url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _extract_records(payload: Any) -> list[dict]:
    """Pull the record list out of whatever envelope the API uses."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("records", "data", "results", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def _has_next(payload: Any, got: int) -> bool:
    """Best-effort 'is there another page'."""
    if isinstance(payload, dict):
        pag = payload.get("pagination")
        if isinstance(pag, dict):
            if "next" in pag:
                return bool(pag.get("next"))
            if "has_next" in pag:
                return bool(pag.get("has_next"))
            tp = pag.get("total_pages")
            cp = pag.get("page")
            if isinstance(tp, int) and isinstance(cp, int):
                return cp < tp
    # Fallback: a full page probably means more rows exist.
    return got >= _PAGE_SIZE




def _paginate(params: dict) -> list[dict]:
    out, seen_first, page = [], set(), 1
    while page <= _MAX_PAGES:
        p = dict(params); p[_PAGE_PARAM] = page; p[_PAGE_SIZE_PARAM] = _PAGE_SIZE
        try:
            payload = _get(p)
        except Exception as exc:
            logger.warning("records fetch failed (page=%d): %s", page, exc)
            break
        rows = _extract_records(payload)
        if not rows:
            break
        marker = (rows[0].get("site_name"), rows[0].get("ticket_no"))
        if marker in seen_first:      # server isn't actually paging — stop
            break
        seen_first.add(marker)
        out.extend(rows)
        if not _has_next(payload, len(rows)):
            break
        page += 1
    return out


# ---------------------------------------------------------------------------
# Normalisation + dedup
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _timepart(ts: Any) -> str:
    """'31-05-2026 14:46:55' -> '14:46:55'; passthrough if no space."""
    s = str(ts or "").strip()
    return s.split(" ", 1)[1] if " " in s else s


def _normalize(rec: dict) -> dict:
    net_kg = _to_float(rec.get("net_weight"))
    return {
        "date": rec.get("date", ""),
        "time": rec.get("time", ""),
        "site_name": rec.get("site_name", ""),
        "ticket_no": str(rec.get("ticket_no", "")),
        "vehicle_no": (rec.get("vehicle_no") or "").strip(),
        "material_type": (rec.get("material_type") or rec.get("material") or "").strip(),
        "transfer_party_name": (rec.get("transfer_party_name") or "").strip(),
        "first_weight_kg": _to_float(rec.get("first_weight")),
        "first_time": _timepart(rec.get("first_timestamp")),
        "second_weight_kg": _to_float(rec.get("second_weight")),
        "second_time": _timepart(rec.get("second_timestamp")),
        "net_weight_kg": net_kg,
        "net_weight_mt": round(net_kg / 1000.0, 3),
        "_processed_timestamp": rec.get("_processed_timestamp", ""),
    }


def _dedup(records: Iterable[dict]) -> list[dict]:
    """Keep the latest _processed_timestamp per (site_name, ticket_no)."""
    best: dict[tuple[str, str], dict] = {}
    for r in records:
        key = (r["site_name"], r["ticket_no"])
        prev = best.get(key)
        if prev is None or r["_processed_timestamp"] > prev["_processed_timestamp"]:
            best[key] = r
    return list(best.values())


# ---------------------------------------------------------------------------
# Public query
# ---------------------------------------------------------------------------

def query_records(api_names: tuple[str, ...],
                  start_date: Optional[str] = None,
                  end_date: Optional[str] = None,
                  material: Optional[str] = None,
                  vehicle: Optional[str] = None) -> list[dict]:
    """Return deduped, normalized records for a spine site.

    site_name / start_date / end_date / material are pushed to the API.
    vehicle is filtered client-side (not a documented server filter).
    A spine site can map to several upstream names, so we fan out and merge.
    """
    cache_key = repr((api_names, start_date, end_date, material, vehicle))

    def _load():
        merged: list[dict] = []
        for name in (api_names or ()):
            params: dict = {"site_name": name}
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
            if material:
                params["material"] = material
            raw = _paginate(params)
            merged.extend(_normalize(r) for r in raw)

        records = _dedup(merged)

        # Client-side safety nets for filters the server may not honour.
        if material:
            records = [r for r in records if r["material_type"] == material]
        if vehicle:
            v = vehicle.strip().lower()
            records = [r for r in records if v in r["vehicle_no"].lower()]
        if start_date:
            records = [r for r in records if r["date"] >= start_date]
        if end_date:
            records = [r for r in records if r["date"] <= end_date]

        records.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
        return records

    return _query_cache.get_or_load(cache_key, _load)


def summarize(records: list[dict]) -> dict:
    """Totals + breakdown by material_type and by vehicle."""
    by_material: dict[str, dict] = {}
    by_vehicle: dict[str, dict] = {}
    total_mt = 0.0

    for r in records:
        total_mt += r["net_weight_mt"]

        m = r["material_type"] or "(unspecified)"
        bm = by_material.setdefault(m, {"count": 0, "net_weight_mt": 0.0})
        bm["count"] += 1
        bm["net_weight_mt"] += r["net_weight_mt"]

        v = r["vehicle_no"] or "(unknown)"
        bv = by_vehicle.setdefault(v, {"count": 0, "net_weight_mt": 0.0})
        bv["count"] += 1
        bv["net_weight_mt"] += r["net_weight_mt"]

    for d in (*by_material.values(), *by_vehicle.values()):
        d["net_weight_mt"] = round(d["net_weight_mt"], 3)

    material_rows = sorted(by_material.items(),
                           key=lambda kv: kv[1]["net_weight_mt"], reverse=True)
    vehicle_rows = sorted(by_vehicle.items(),
                          key=lambda kv: kv[1]["net_weight_mt"], reverse=True)

    return {
        "total_records": len(records),
        "total_net_weight_mt": round(total_mt, 3),
        "by_material": [{"material_type": k, **v} for k, v in material_rows],
        "by_vehicle": [{"vehicle_no": k, **v} for k, v in vehicle_rows],
    }


# ---------------------------------------------------------------------------
# Daily pace — live throughput of the site's main material (tile 2).
# "per day" => default is TODAY's net weight for PACE_MATERIAL_TYPE, the only
# reading that visibly moves in realtime. For an all-time total or running
# average, widen start/end and use the delta-sync hook below so you don't
# re-pull all history on every poll.
# ---------------------------------------------------------------------------

PACE_MATERIAL_TYPE = "Legacy/MSW"   # generic: one place to change per deployment


def _today_str() -> str:
    """Local 'today' as YYYY-MM-DD. Prefer the app's IST clock (matches the
    main dashboard) so the day rolls at local midnight, not UTC."""
    fn = getattr(config, "today_ist", None)
    if callable(fn):
        try:
            return fn().isoformat()
        except Exception:  # noqa: BLE001
            pass
    return date.today().isoformat()


def daily_pace(api_names: tuple[str, ...], day: Optional[str] = None) -> float:
    """Net weight (MT) of PACE_MATERIAL_TYPE records for `day` (default today).

    Cheap: server-side filtered to one day + one material, then cached by
    query_records' short TTL so frequent polls mostly hit cache.
    """
    d = day or _today_str()
    recs = query_records(api_names, start_date=d, end_date=d,
                         material=PACE_MATERIAL_TYPE)
    mt = sum(r["net_weight_mt"] for r in recs
             if r["material_type"] == PACE_MATERIAL_TYPE)
    return round(mt, 3)


# ---------------------------------------------------------------------------
# OPTIONAL future hook — delta-synced all-time aggregate.
# Only worth wiring up if you add an always-on "all-time totals" view.
# It would persist {last_sync_date, by_ticket: {...}} and fetch only
# start_date=last_sync_date on refresh, merging with _dedup() above to absorb
# corrected tickets. Left unimplemented on purpose: the current explorer is
# date-bounded, so this would be premature.
# ---------------------------------------------------------------------------