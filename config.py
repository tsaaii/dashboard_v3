"""
Centralised config for haritha_lite.
All knobs live here. Override via environment variables.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Master CSV — single source of truth for all 12 dashboard cards.
#
# Set SITES_MASTER_CSV to ONE of:
#     sites_master.csv                            -> local file (relative)
#     /abs/path/sites_master.csv                  -> local file (absolute)
#     gs://my-bucket/dir/sites_master.csv         -> GCS object
#
# Plug-and-play between local dev and cloud — same code path either way.
# In production (App Engine) this comes from app.yaml env_variables.
# ---------------------------------------------------------------------------
SITES_MASTER_CSV = os.environ.get("SITES_MASTER_CSV", "sites_master.csv")


def is_gcs_path(path: str) -> bool:
    return path.startswith("gs://")


def parse_gcs_path(path: str) -> tuple[str, str]:
    """gs://bucket/key/path -> ('bucket', 'key/path')."""
    rest = path[len("gs://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


# ---------------------------------------------------------------------------
# Records API
# Used by the /reports page only. The dashboard no longer calls it; everything
# the dashboard renders comes from the master CSV.
# ---------------------------------------------------------------------------
RECORDS_API_BASE = os.environ.get(
    "RECORDS_API_BASE", "https://weighbridge-api-asia-south1-287877277037.asia-south1.run.app",
).rstrip("/")
RECORDS_API_TIMEOUT_S = int(os.environ.get("RECORDS_API_TIMEOUT_S", "10"))


# ---------------------------------------------------------------------------
# Cache — process-local TTL cache wrapping the CSV read.
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", str(30 * 60)))


# ---------------------------------------------------------------------------
# Project deadline. Used to compute days_remaining when a site row leaves
# `deadline_date` blank.
# ---------------------------------------------------------------------------
PROJECT_DEADLINE = os.environ.get("PROJECT_DEADLINE", "2026-09-30")


# ---------------------------------------------------------------------------
# Synthetic today_mt ticker (cards 3 & 7).
# Today's MT grows linearly from 0 at IST midnight, capped at daily_required.
# 200 MT/hr matches the spec ("100 MT per 30 mins").
# ---------------------------------------------------------------------------
TODAY_MT_RATE_PER_HOUR = float(os.environ.get("TODAY_MT_RATE_PER_HOUR", "200"))


# ---------------------------------------------------------------------------
# RDF expected (cards 2 & 6) = RDF_EXPECTED_PCT * target_mt
# ---------------------------------------------------------------------------
RDF_EXPECTED_PCT = float(os.environ.get("RDF_EXPECTED_PCT", "0.15"))


# ---------------------------------------------------------------------------
# Display / branding
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Agency display names.
#
# Empty dict by default — every agency renders with its raw name from
# sites_master.csv. Add entries here only when you want a label that
# differs from the CSV value, e.g.:
#     "Tharuni Associates": "Tharuni",
#     "RaghuRam Hume Pipes": "RaghuRam",
# ---------------------------------------------------------------------------
AGENCY_DISPLAY_NAMES: dict[str, str] = {}

ROTATION_INTERVAL_MS = int(os.environ.get("ROTATION_INTERVAL_MS", "12000"))


def project_deadline_date() -> date:
    try:
        y, m, d = (int(x) for x in PROJECT_DEADLINE.split("-"))
        return date(y, m, d)
    except Exception:
        return date(2026, 9, 30)


# ---------------------------------------------------------------------------
# IST clock helpers
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> date:
    return datetime.now(IST).date()


def now_ist() -> datetime:
    return datetime.now(IST)


def hours_since_midnight_ist() -> float:
    """Float hours since IST midnight. 13.5 at 13:30 IST."""
    n = now_ist()
    return n.hour + n.minute / 60.0 + n.second / 3600.0
