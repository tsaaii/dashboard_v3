"""
Dashboard users, stored in users.csv next to sites_master.csv.

    username,password_hash,scope,name,created
    priya,scrypt:...,all,Priya (SAC),2026-09-07
    kdp_eng,scrypt:...,kadapa,Site engineer Kadapa,2026-09-07

scope: "all" = every screen incl. /admin? NO — "all" = sites list, every site,
reports. Only "admin" gets /admin. A site slug = that site's page only.

The env-var admin (auth.py) always works in addition, so you can never lock
yourself out by editing this file.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date

from werkzeug.security import check_password_hash, generate_password_hash

import config
from data import storage
from data._cache import TTLCache

logger = logging.getLogger(__name__)
FILE = "users.csv"
FIELDS = ["username", "password_hash", "scope", "name", "created"]
_cache = TTLCache(config.CACHE_TTL_SECONDS)


def _load() -> list[dict]:
    try:
        text = storage.read_text(FILE)
    except FileNotFoundError:
        return []
    rows = [r for r in csv.DictReader(io.StringIO(text)) if (r.get("username") or "").strip()]
    for r in rows:
        r["username"] = r["username"].strip().lower()
        r["scope"] = (r.get("scope") or "").strip().lower() or "all"
    return rows


def all_users() -> list[dict]:
    return _cache.get_or_load("users", _load)


def invalidate() -> None:
    _cache.invalidate()


def _save(rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in FIELDS})
    storage.write_bytes(FILE, buf.getvalue().encode("utf-8"))
    invalidate()


def authenticate(username: str, password: str) -> dict | None:
    """Return the user row (with scope) if the credentials match."""
    u = (username or "").strip().lower()
    for r in all_users():
        if r["username"] == u:
            try:
                return r if check_password_hash(r["password_hash"], password) else None
            except Exception:  # noqa: BLE001
                return None
    return None


def add(username: str, password: str, scope: str, name: str = "") -> None:
    u = (username or "").strip().lower()
    if not u or not password:
        raise ValueError("Username and password are required")
    rows = [r for r in all_users() if r["username"] != u]     # replace if exists
    rows.append({"username": u, "password_hash": generate_password_hash(password),
                 "scope": (scope or "all").strip().lower(), "name": name.strip(),
                 "created": date.today().isoformat()})
    _save(rows)


def remove(username: str) -> None:
    u = (username or "").strip().lower()
    _save([r for r in all_users() if r["username"] != u])
