"""
Outgoing email for access requests. Plain smtplib, nothing else.

Env vars (all required for sending; if any is missing the request is only
logged, and the page still tells the visitor it was received):
    SMTP_HOST           smtp.gmail.com
    SMTP_PORT           587
    SMTP_USER           you@gmail.com
    SMTP_PASSWORD       a Gmail *App Password* (not your login password)
    ACCESS_REQUEST_TO   inbox that receives the requests (defaults to SMTP_USER)
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


def send_access_request(fields: dict) -> bool:
    """Email one access request. Returns True if it was actually sent."""
    body = "\n".join(f"{k.replace('_', ' ').title():<14} {v}" for k, v in fields.items() if v)
    logger.info("Access request:\n%s", body)
    if not configured():
        logger.warning("SMTP not configured; request logged only")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"[Swachh Andhra dashboard] Access request — {fields.get('name', '')}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ.get("ACCESS_REQUEST_TO") or os.environ["SMTP_USER"]
    if fields.get("email"):
        msg["Reply-To"] = fields["email"]
    msg.set_content(body + "\n")

    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
        return True
    except Exception:                                  # noqa: BLE001
        logger.exception("Access request email failed")
        return False
