"""
Read/write the dashboard's data files, wherever SITES_MASTER_CSV points.

All dashboard data lives in ONE folder — the folder of SITES_MASTER_CSV —
either local (repo root in dev) or gs://bucket/dashboard_config/ in
production. Every file is addressed by its bare name:

    read_text("sites_master.csv")
    write_bytes("users.csv", b"...")
    info("sites_master.csv")  -> {"exists", "size", "updated"}
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import config


def _folder_and_name() -> tuple[str, str]:
    p = config.SITES_MASTER_CSV
    if config.is_gcs_path(p):
        bucket, key = config.parse_gcs_path(p)
        folder, _, name = key.rpartition("/")
        return f"gs://{bucket}/{folder}".rstrip("/"), name
    folder, name = os.path.split(os.path.abspath(p))
    return folder, name


def path_for(name: str) -> str:
    folder, _ = _folder_and_name()
    return f"{folder}/{name}"


def _blob(name: str):
    from google.cloud import storage  # type: ignore
    bucket, key = config.parse_gcs_path(path_for(name))
    return storage.Client().bucket(bucket).blob(key)


def read_text(name: str) -> str:
    p = path_for(name)
    if config.is_gcs_path(p):
        b = _blob(name)
        if not b.exists():
            raise FileNotFoundError(p)
        return b.download_as_text()
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def write_bytes(name: str, data: bytes, content_type: str = "text/csv") -> str:
    p = path_for(name)
    if config.is_gcs_path(p):
        _blob(name).upload_from_string(data, content_type=content_type)
    else:
        with open(p, "wb") as f:
            f.write(data)
    return p


def info(name: str) -> dict:
    p = path_for(name)
    try:
        if config.is_gcs_path(p):
            b = _blob(name)
            if not b.exists():
                return {"path": p, "exists": False}
            b.reload()
            return {"path": p, "exists": True, "size": b.size,
                    "updated": b.updated.astimezone(config.IST).strftime("%d %b %Y, %H:%M IST")}
        st = os.stat(p)
        return {"path": p, "exists": True, "size": st.st_size,
                "updated": datetime.fromtimestamp(st.st_mtime, timezone.utc).astimezone(config.IST).strftime("%d %b %Y, %H:%M IST")}
    except FileNotFoundError:
        return {"path": p, "exists": False}
