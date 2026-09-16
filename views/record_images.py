"""
Weighbridge image proxy + 2x2 viewer.

    GET /record-images/<site>/<date>/<ticket>            -> 2x2 HTML sheet
    GET /record-images/<site>/<date>/<ticket>/<slot>.jpg -> one JPEG

Why proxy instead of pointing <img> at the records API: reads upstream need no
key, so a direct URL in the page source of a login-gated page would publish an
open endpoint for every site's records. Everything goes through Flask, behind
the checks the rest of the page already uses.

Authorisation: a global login sees every site (same as /reports). A per-site
login sees only the upstream names in that slug's api_site_names, compared
EXACTLY — the records API does partial matching on site_name, so a substring
test here would let an "Ananthapur" grant reach "Ananthapur2".

Rate limit: the records API allows 120 req/min per IP and every proxied image
leaves from one Cloud Run egress IP shared by all users. Four images per popup
adds up, so bytes are cached in-process and in the browser.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from collections import OrderedDict
from urllib.parse import quote

import requests
from flask import Blueprint, Response, abort, current_app, render_template, session, url_for

import config
from data import master, records_api
from views.login import SESSION_USER_KEY
from views.sites import SESSION_SITE_KEY

logger = logging.getLogger(__name__)
bp = Blueprint("record_images", __name__, url_prefix="/record-images")

# Order IS the 2x2 layout, read left-to-right, top-to-bottom.
SLOTS: tuple[tuple[str, str], ...] = (
    ("first_front", "1ST WEIGHMENT - FRONT"),
    ("first_back", "1ST WEIGHMENT - BACK"),
    ("second_front", "2ND WEIGHMENT - FRONT"),
    ("second_back", "2ND WEIGHMENT - BACK"),
)
SLOT_KEYS: tuple[str, ...] = tuple(s for s, _ in SLOTS)
SLOT_LABELS: dict[str, str] = dict(SLOTS)
_TIMEOUT = 20

# Bounded by TOTAL BYTES, not entry count: captures vary widely in size and an
# entry-count cap would happily hold 200 large JPEGs and OOM the instance.
_CACHE_MAX_BYTES = 48 * 1024 * 1024
_CACHE_TTL_S = 900
_cache: "OrderedDict[str, tuple[float, str, bytes]]" = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def _evict_locked(key: str) -> None:
    global _cache_bytes
    entry = _cache.pop(key, None)
    if entry is not None:
        _cache_bytes -= len(entry[2])


def _cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires, ctype, blob = entry
        if expires < time.time():
            _evict_locked(key)
            return None
        _cache.move_to_end(key)                      # LRU touch
        return ctype, blob


def _cache_put(key: str, ctype: str, blob: bytes) -> None:
    global _cache_bytes
    if len(blob) > _CACHE_MAX_BYTES // 4:            # one huge image must not evict everything
        return
    with _cache_lock:
        _evict_locked(key)
        _cache[key] = (time.time() + _CACHE_TTL_S, ctype, blob)
        _cache_bytes += len(blob)
        while _cache_bytes > _CACHE_MAX_BYTES and _cache:
            _evict_locked(next(iter(_cache)))


def cache_stats() -> dict:
    with _cache_lock:
        return {"entries": len(_cache), "bytes": _cache_bytes, "max_bytes": _CACHE_MAX_BYTES}


def invalidate_cache() -> None:
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0


def _authorized(site_name: str) -> bool:
    if session.get(SESSION_USER_KEY):
        return True
    granted = session.get(SESSION_SITE_KEY) or []
    wanted = (site_name or "").strip()
    if not granted or not wanted:
        return False
    for slug in granted:
        site = master.get_site_by_slug(slug)
        if site is not None and wanted in (site.api_site_names or ()):
            return True
    return False


def _iso(d: str) -> str:
    """Records use dd-mm-yyyy; the API's date filters want yyyy-mm-dd."""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(d.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return d


def _record(site: str, date: str, ticket: str) -> dict:
    """Fetch the one record behind this ticket so the sheet can print its
    details. A failure here must not hide the photos, so it returns {}."""
    try:
        day = _iso(date)
        payload = records_api.fetch_records(
            {"site_name": site, "ticket_no": ticket, "start_date": day, "end_date": day}, page=1, limit=50)
        for r in payload.get("records") or []:
            if str(r.get("ticket_no", "")).strip() == ticket.strip():
                return r
    except Exception:                                 # noqa: BLE001
        logger.warning("ticket lookup failed for %s/%s/%s", site, date, ticket)
    return {}


@bp.route("/<site>/<date>/<ticket>")
def sheet_view(site, date, ticket):
    """Printable ticket sheet: header, vehicle and weighment details, 2x2 photos."""
    if not _authorized(site):
        abort(403)
    tiles = [{"slot": slot, "caption": caption,
              "url": url_for("record_images.image_proxy", site=site, date=date, ticket=ticket, slot=slot)}
             for slot, caption in SLOTS]
    r = _record(site, date, ticket)

    def kg(v):
        """PDF prints weights as 48970.00 kg — same here."""
        try:
            return f"{float(v):.2f} kg"
        except (TypeError, ValueError):
            return "Not captured"

    # Address line under the agency name, as in the PDF letterhead. It is
    # site-specific, so it comes from an optional `address` column in
    # sites_master.csv — add the column and the line appears; leave it out and
    # the header is just the agency name.
    wanted = (r.get("site_name") or site or "").strip()
    address = ""
    for st in master.get_sites():
        if st.site_name == wanted or wanted in (st.api_site_names or ()):
            address = (getattr(st, "address", "") or "").strip()
            break

    return render_template(
        "record_images.html", ticket=ticket, site=site, date=r.get("date") or date,
        record=r, tiles=tiles, address=address, agency=r.get("agency_name", ""),
        first_w=kg(r.get("first_weight")), second_w=kg(r.get("second_weight")), net=kg(r.get("net_weight")),
        printed=datetime.now(config.IST).strftime("%d-%m-%Y %H:%M:%S"))


@bp.route("/<site>/<date>/<ticket>/<slot>.jpg")
def image_proxy(site, date, ticket, slot):
    """Stream one JPEG from the records API."""
    if slot not in SLOT_KEYS:
        abort(404)
    if not _authorized(site):
        abort(403)

    key = f"{site}|{date}|{ticket}|{slot}"
    cached = _cache_get(key)
    if cached is not None:
        return _respond(cached[1], cached[0], "HIT")

    url = (f"{config.RECORDS_API_BASE}/records/{quote(site, safe='')}"
           f"/{quote(date, safe='')}/{quote(ticket, safe='')}/image/{slot}")
    try:
        r = requests.get(url, timeout=_TIMEOUT)
    except Exception as exc:                          # noqa: BLE001
        current_app.logger.warning("image proxy request failed %s: %s", url, exc)
        abort(502)

    # 404 is normal — the slot was never captured. The template's onerror turns
    # it into "No image in this slot", so pass it through rather than
    # substituting a placeholder here.
    if r.status_code == 404:
        abort(404)
    if r.status_code == 429:
        current_app.logger.warning("image proxy rate-limited upstream — consider raising the cache TTL")
        abort(429)
    if r.status_code >= 400:
        current_app.logger.warning("image proxy upstream %s for %s", r.status_code, url)
        abort(502)

    ctype = r.headers.get("Content-Type", "image/jpeg")
    _cache_put(key, ctype, r.content)
    return _respond(r.content, ctype, "MISS")


def _respond(blob: bytes, ctype: str, cache_state: str) -> Response:
    return Response(blob, mimetype=ctype, headers={
        # `private` matters: gated content must never land in a shared or CDN
        # cache where an unauthenticated request could pick it up.
        "Cache-Control": "private, max-age=86400",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
        "X-Image-Cache": cache_state,
        "Content-Length": str(len(blob)),
    })
