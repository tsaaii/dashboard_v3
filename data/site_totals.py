"""
Live per-site totals — the source of truth for the four headline tiles.

WHY THIS EXISTS
    The tiles used to read `remediated_mt` / `*_disposed_mt` straight off
    sites_master.csv — hand-maintained numbers that drift from what the
    weighbridge actually recorded. This module derives them from the real
    records instead, so the tiles reconcile with the explorer.

WHY NOT JUST CALL THE API
    An all-time total would mean paginating every record the site ever
    produced, on every page load — slow, and vulnerable to any server-side
    page cap silently truncating the result.

    Instead we read `processed_csv/master.csv`, which the upstream Cloud
    Function already produces. It is ALREADY DEDUPED by
    (site_name, ticket_no) keeping the latest _processed_timestamp, so
    reprocessed tickets and backfilled folders are handled BEFORE the file is
    written. We inherit that correctness for free.

THE SEAM (why we don't just use master.csv alone)
    master.csv is only as fresh as the last pipeline run, and its final day is
    usually PARTIAL (the run happened mid-day). So:

        total = (master.csv, EXCLUDING its last day per site)
              + (live API, from that last day through today)

    Excluding the last day and re-fetching it is what stops the overlap from
    double-counting. The API leg is date-bounded to a few days — cheap.

COST
    One streaming pass over master.csv (stdlib csv, not pandas — pandas isn't
    a dependency and would bloat cold starts). Cached for CACHE_TTL_SECONDS,
    and one pass computes totals for EVERY site at once. Steady-state request
    cost is a dict lookup.

DEFINITIONS (inherited from data/records.py, not invented here)
    remediated = inward legacy waste  (material_type in LEGACY_MATERIAL_TYPES)
    disposal   = outward fractions    (Soil / RDF / C&D / Inert)
"""
from __future__ import annotations

import csv
import logging
from typing import Iterable, Optional

import config
from data._cache import TTLCache

logger = logging.getLogger(__name__)

_cache = TTLCache(getattr(config, "CACHE_TTL_SECONDS", 1800))
_CACHE_KEY = "site_totals"

# Same definition the dashboard already uses. Do not fork it.
LEGACY_MATERIAL_TYPES = {
    "legacy/msw", "legacy msw", "legacy", "msw",
}

# Outward fractions -> the four tile buckets. Left side is lowercased
# material_type as it appears upstream; add aliases here if new ones show up.
_DISPOSAL_ALIASES = {
    "soil": "Soil",
    "fine soil": "Soil",
    "soil/fines": "Soil",
    "fines": "Soil",
    "rdf": "RDF",
    "refuse derived fuel": "RDF",
    "ap fuel": "RDF",
    "cnd": "CnD",
    "c&d": "CnD",
    "c & d": "CnD",
    "c and d": "CnD",
    "construction and demolition": "CnD",
    "inert": "Inert",
    "inerts": "Inert",
}

DISPOSAL_BUCKETS = ("Soil", "RDF", "CnD", "Inert")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def is_legacy(material_type: str) -> bool:
    """True if this material is inward legacy waste (i.e. counts as remediated)."""
    m = (material_type or "").strip().lower()
    if not m:
        return True          # missing => assume legacy (matches data/records.py)
    return m in LEGACY_MATERIAL_TYPES


def disposal_bucket(material_type: str) -> Optional[str]:
    """Map an outward material to one of the four tile buckets, or None."""
    m = (material_type or "").strip().lower()
    if not m or m in LEGACY_MATERIAL_TYPES:
        return None
    return _DISPOSAL_ALIASES.get(m)


def _to_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# master.csv location + streaming read
# ---------------------------------------------------------------------------

def master_records_path() -> str:
    """Where the pipeline's deduped master.csv lives.

    Defaults to the same bucket as sites_master.csv, under processed_csv/.
    Override with MASTER_RECORDS_CSV (local path or gs:// URL) for dev.
    """
    explicit = getattr(config, "MASTER_RECORDS_CSV", "") or ""
    if explicit:
        return explicit

    spine = config.SITES_MASTER_CSV
    if config.is_gcs_path(spine):
        bucket, _ = config.parse_gcs_path(spine)
        return f"gs://{bucket}/processed_csv/master.csv"
    return "master.csv"      # local dev fallback


# Sites whose records live OUTSIDE the pipeline's master.csv, in their own
# small CSV(s) — e.g. a brand-new site backfilled from a support export before
# its JSON pipeline is live. Drop a file here and it's folded into the totals;
# master.csv is never touched. On a ticket collision master.csv wins, so once
# the pipeline starts producing a site the extra file stops double-counting it.
_DEFAULT_EXTRA_PREFIX = "processed_csv/site_totals_extra/"


def extra_records_paths() -> list[str]:
    """Discover per-site 'extra' record CSVs to fold in alongside master.csv.

    Precedence: an explicit config.EXTRA_RECORDS_CSVS list wins; otherwise we
    scan config.EXTRA_RECORDS_PREFIX (default processed_csv/site_totals_extra/)
    in the spine's bucket for *.csv. Empty/unreachable => no extras.
    """
    explicit = getattr(config, "EXTRA_RECORDS_CSVS", None)
    if explicit:
        return list(explicit)

    prefix = getattr(config, "EXTRA_RECORDS_PREFIX", None)
    spine = config.SITES_MASTER_CSV

    if config.is_gcs_path(spine):
        if prefix is None:
            prefix = _DEFAULT_EXTRA_PREFIX
        bucket, _ = config.parse_gcs_path(spine)
        try:
            from google.cloud import storage  # type: ignore
            client = storage.Client()
            return [f"gs://{bucket}/{b.name}"
                    for b in client.bucket(bucket).list_blobs(prefix=prefix)
                    if b.name.lower().endswith(".csv")]
        except Exception as exc:  # noqa: BLE001
            logger.warning("site_totals: listing extras failed (%s)", exc)
            return []

    # local dev: only if a prefix/dir is explicitly configured
    if prefix:
        import glob
        import os
        return sorted(glob.glob(os.path.join(prefix, "*.csv")))
    return []


def _open_rows(path: str) -> Iterable[dict]:
    """Yield master.csv rows one at a time.

    Streams rather than download_as_text(): master.csv is wide (it carries
    image-path columns) and can be tens of MB. Holding it all as one string
    is real memory pressure on a small App Engine instance.
    """
    if config.is_gcs_path(path):
        from google.cloud import storage  # type: ignore
        bucket_name, key = config.parse_gcs_path(path)
        blob = storage.Client().bucket(bucket_name).blob(key)
        if not blob.exists():
            raise FileNotFoundError(f"{path} not found")
        with blob.open("r") as fh:
            yield from csv.DictReader(fh)
    else:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            yield from csv.DictReader(fh)


# ---------------------------------------------------------------------------
# The single pass
# ---------------------------------------------------------------------------

def _blank() -> dict:
    return {
        "legacy_kg": 0.0,
        "legacy_trips": 0,
        "disposal_kg": {b: 0.0 for b in DISPOSAL_BUCKETS},
        "other_kg": 0.0,          # outward, but unrecognised material name
    }


def _fold_row(by_site: dict, site: str, mat: str, kg: float, day: str) -> None:
    """Fold one record into a site's all-time + last-day buckets.

    Order-independent: last-day tracking resets whenever a later date is seen,
    so master.csv and extra files can be folded in any sequence.
    """
    st = by_site.setdefault(site, {
        "all": _blank(), "last_day": _blank(), "max_date": "",
    })
    _add(st["all"], mat, kg)
    if day:
        if day > st["max_date"]:
            st["max_date"] = day
            st["last_day"] = _blank()
            _add(st["last_day"], mat, kg)
        elif day == st["max_date"]:
            _add(st["last_day"], mat, kg)


def _load() -> dict:
    """Fold master.csv + any extra per-site CSVs into per-site totals.

    Returns:
        {
          "by_site": { "<site_name>": {
              "all":      <bucket>,   # every row for this site
              "last_day": <bucket>,   # rows on this site's MAX date only
              "max_date": "YYYY-MM-DD" | "",
          }},
          "path": "<master.csv path>",
          "extra_paths": [ ... ],
        }

    Extra files let a brand-new site show real numbers before its JSON pipeline
    lands in master.csv. On a (site_name, ticket_no) collision master.csv wins,
    so an extra file can't double-count a site the pipeline already covers.
    """
    main_path = master_records_path()
    extra_paths = extra_records_paths()

    # Read the (small) extra files first so we know which sites they cover —
    # that bounds the dedup set to just those sites instead of all ~140k rows.
    extra_rows: list[tuple[str, str, str, float, str]] = []
    extra_sites: set[str] = set()
    for ep in extra_paths:
        try:
            for row in _open_rows(ep):
                site = (row.get("site_name") or "").strip()
                if not site:
                    continue
                extra_sites.add(site)
                extra_rows.append((
                    site,
                    (row.get("ticket_no") or "").strip(),
                    (row.get("material_type") or row.get("material") or "").strip(),
                    _to_float(row.get("net_weight")),
                    (row.get("date") or "").strip(),
                ))
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("site_totals: extra file %s failed (%s)", ep, exc)

    by_site: dict[str, dict] = {}
    seen: set[tuple[str, str]] = set()   # (site, ticket) for extra-covered sites
    n_rows = 0

    # -- master.csv (authoritative; its tickets win) --
    logger.info("site_totals: streaming %s", main_path)
    try:
        for row in _open_rows(main_path):
            site = (row.get("site_name") or "").strip()
            if not site:
                continue
            mat = (row.get("material_type") or row.get("material") or "").strip()
            kg = _to_float(row.get("net_weight"))
            day = (row.get("date") or "").strip()
            if site in extra_sites:
                tk = (row.get("ticket_no") or "").strip()
                if tk:
                    seen.add((site, tk))
            _fold_row(by_site, site, mat, kg, day)
            n_rows += 1
    except FileNotFoundError:
        logger.warning("site_totals: %s not found; using extras only", main_path)

    # -- extra files (skip any ticket master.csv already has) --
    skipped = 0
    for site, tk, mat, kg, day in extra_rows:
        if tk and (site, tk) in seen:
            skipped += 1
            continue
        _fold_row(by_site, site, mat, kg, day)
        n_rows += 1

    logger.info("site_totals: %d rows over %d sites (%d extra files, "
                "%d overlap rows skipped)",
                n_rows, len(by_site), len(extra_paths), skipped)
    return {"by_site": by_site, "path": main_path, "extra_paths": extra_paths}


def _add(bucket: dict, material_type: str, kg: float) -> None:
    if is_legacy(material_type):
        bucket["legacy_kg"] += kg
        bucket["legacy_trips"] += 1
        return
    b = disposal_bucket(material_type)
    if b:
        bucket["disposal_kg"][b] += kg
    else:
        bucket["other_kg"] += kg


def get_all_totals() -> dict:
    return _cache.get_or_load(_CACHE_KEY, _load)


def invalidate_cache() -> None:
    _cache.invalidate(_CACHE_KEY)


# ---------------------------------------------------------------------------
# Public: totals for one spine site (master.csv + live top-up)
# ---------------------------------------------------------------------------

def site_totals(api_names: tuple[str, ...], *, live_topup: bool = True) -> dict:
    """All-time remediated + disposal for one spine site, in MT.

    A spine site can map to several upstream names, so we sum across them.

    total = master.csv (excluding each site's last, possibly-partial day)
          + live API from that day through today

    Set live_topup=False to skip the API leg entirely (pure master.csv).

    Returns:
        {"remediated_mt": float,
         "disposal_mt": {"Soil": .., "RDF": .., "CnD": .., "Inert": ..},
         "total_disposed_mt": float,
         "legacy_trips": int,
         "as_of": "YYYY-MM-DD" | "",     # master.csv's latest date
         "live": bool,                    # did the API leg actually run
         "source": "master.csv+api" | "master.csv" | "spine"}
    """
    try:
        data = get_all_totals()
    except Exception as exc:  # noqa: BLE001
        logger.warning("site_totals: master.csv unavailable (%s)", exc)
        return {"remediated_mt": 0.0,
                "disposal_mt": {b: 0.0 for b in DISPOSAL_BUCKETS},
                "total_disposed_mt": 0.0, "legacy_trips": 0,
                "as_of": "", "live": False, "source": "unavailable"}

    by_site = data["by_site"]

    legacy_kg = 0.0
    trips = 0
    disposal = {b: 0.0 for b in DISPOSAL_BUCKETS}
    seam_dates: dict[str, str] = {}      # upstream name -> its max date
    live_any = False

    # Import the API layer once. If it's unusable we degrade to pure master.csv.
    site_api = None
    if live_topup:
        try:
            from data import site_api as _sa
            site_api = _sa
        except Exception as exc:  # noqa: BLE001
            logger.warning("site_totals: site_api unavailable (%s)", exc)
            site_api = None

    for name in (api_names or ()):
        st = by_site.get(name)
        if not st:
            continue
        all_b, last_b = st["all"], st["last_day"]
        max_date = st["max_date"]

        # Ask the API for everything from this site's last csv day onward.
        api_recs = None
        if site_api is not None and max_date:
            try:
                api_recs = site_api.query_records((name,), start_date=max_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning("site_totals: top-up failed for %s (%s)", name, exc)
                api_recs = None

        # SEAM GUARD: only drop master.csv's last (partial) day if the API
        # actually returns records to replace it. api_recs == [] means the API
        # knows nothing for that day yet — e.g. a brand-new site still running
        # on support-CSV data, or the JSON pipeline not live. In that case we
        # KEEP the csv day, or the tile silently undercounts by a full day.
        drop_last = bool(api_recs)

        legacy_kg += all_b["legacy_kg"] - (last_b["legacy_kg"] if drop_last else 0.0)
        trips += all_b["legacy_trips"] - (last_b["legacy_trips"] if drop_last else 0)
        for b in DISPOSAL_BUCKETS:
            drop = last_b["disposal_kg"][b] if drop_last else 0.0
            disposal[b] += all_b["disposal_kg"][b] - drop

        # Add back the API's version of that day-forward (only if we dropped).
        if drop_last:
            for r in api_recs:
                kg = float(r.get("net_weight_kg") or 0.0)
                mat = r.get("material_type") or ""
                if is_legacy(mat):
                    legacy_kg += kg
                    trips += 1
                else:
                    b = disposal_bucket(mat)
                    if b:
                        disposal[b] += kg
            live_any = True

        if max_date:
            seam_dates[name] = max_date

    as_of = max(seam_dates.values()) if seam_dates else ""
    if live_topup and live_any:
        source, live = "master.csv+api", True
    else:
        source, live = "master.csv", False

    disposal_mt = {b: round(v / 1000.0, 1) for b, v in disposal.items()}
    return {
        "remediated_mt": round(legacy_kg / 1000.0, 1),
        "disposal_mt": disposal_mt,
        "total_disposed_mt": round(sum(disposal_mt.values()), 1),
        "legacy_trips": trips,
        "as_of": as_of,
        "live": live,
        "source": source,
    }