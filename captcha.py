"""
Lightweight anti-bot defenses for the login page.

Two layers:

1. **Math captcha** — `new_challenge()` generates an arithmetic problem
   ("7 + 4 = ?") and stashes the expected answer in the user's session.
   `verify(answer)` checks the form submission against the stash and
   then rotates the answer (so a single solved captcha can't be replayed
   for multiple POSTs).

2. **IP rate-limiting** — `register_failure(ip)` tracks failed login
   attempts in a process-local dict; `is_blocked(ip)` returns True once
   `MAX_FAILURES` is exceeded within `WINDOW_SECONDS`. On success call
   `clear_failures(ip)`.

This is "good enough for a generic credential pair" — stops naive
crawlers and scripted brute-force. For production-grade bot resistance
swap the math captcha for hCaptcha or Cloudflare Turnstile (the
session-state shape stays the same).

Process-local state means rate-limit counts reset on app restart and
don't share across multiple gunicorn workers. For App Engine F1/F2 with
the default single-worker Python config that's fine; if you scale up
or out, plug in memcache/redis behind the same API.
"""
from __future__ import annotations

import logging
import random
import secrets
import threading
import time
from typing import Optional

from flask import session, request

logger = logging.getLogger(__name__)


# ---- Captcha session keys ----
_CAPTCHA_QUESTION_KEY = "captcha_q"
_CAPTCHA_ANSWER_KEY = "captcha_a"
_CAPTCHA_NONCE_KEY = "captcha_n"

# ---- Rate-limit knobs ----
MAX_FAILURES = 5            # failed logins before block
WINDOW_SECONDS = 15 * 60    # rolling window
BLOCK_SECONDS = 15 * 60     # how long the block lasts after threshold

# Process-local store: ip -> (first_failure_ts, count, blocked_until_ts)
_lock = threading.Lock()
_failures: dict[str, tuple[float, int, float]] = {}


# ===========================================================================
# Captcha
# ===========================================================================

def new_challenge() -> str:
    """Generate a fresh math problem; stash the answer; return question text."""
    op = random.choice(("+", "-", "×"))
    if op == "+":
        a, b = random.randint(2, 12), random.randint(2, 12)
        answer = a + b
    elif op == "-":
        a = random.randint(8, 20)
        b = random.randint(2, a - 1)        # keep result positive
        answer = a - b
    else:  # ×
        a, b = random.randint(2, 9), random.randint(2, 9)
        answer = a * b

    question = f"{a} {op} {b}"
    nonce = secrets.token_urlsafe(8)

    session[_CAPTCHA_QUESTION_KEY] = question
    session[_CAPTCHA_ANSWER_KEY] = answer
    session[_CAPTCHA_NONCE_KEY] = nonce
    return question


def current_question() -> str:
    """Return the current question, generating a new one if missing."""
    q = session.get(_CAPTCHA_QUESTION_KEY)
    if not q:
        return new_challenge()
    return q


def verify(answer_str: str, nonce: Optional[str] = None) -> bool:
    """True if `answer_str` matches the stashed answer.

    Always rotates the challenge after this call (success OR failure) —
    that prevents both replay attacks and brute-force on a single
    challenge. The optional `nonce` ties a form submission to the exact
    challenge it was rendered with; if provided and mismatched, fail.
    """
    expected = session.get(_CAPTCHA_ANSWER_KEY)
    expected_nonce = session.get(_CAPTCHA_NONCE_KEY)

    # Always rotate before returning, no matter the result.
    try:
        if expected is None:
            return False
        if nonce is not None and nonce != expected_nonce:
            return False
        try:
            return int((answer_str or "").strip()) == int(expected)
        except (TypeError, ValueError):
            return False
    finally:
        new_challenge()


def current_nonce() -> str:
    """Return the nonce of the currently-stashed challenge."""
    n = session.get(_CAPTCHA_NONCE_KEY)
    if not n:
        new_challenge()
        n = session.get(_CAPTCHA_NONCE_KEY, "")
    return n


# ===========================================================================
# Rate-limiting
# ===========================================================================

def client_ip() -> str:
    """Best-effort client IP. Honors X-Forwarded-For when behind a proxy."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        # take the leftmost (original client), strip whitespace
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def is_blocked(ip: str) -> tuple[bool, int]:
    """Return (blocked, seconds_remaining)."""
    now = time.time()
    with _lock:
        entry = _failures.get(ip)
        if not entry:
            return False, 0
        _first, _count, blocked_until = entry
        if blocked_until > now:
            return True, int(blocked_until - now)
        return False, 0


def register_failure(ip: str) -> tuple[int, bool]:
    """Increment failure count for `ip`. Returns (new_count, just_blocked)."""
    now = time.time()
    just_blocked = False
    with _lock:
        entry = _failures.get(ip)
        if not entry or (now - entry[0]) > WINDOW_SECONDS:
            # New window
            _failures[ip] = (now, 1, 0.0)
            count = 1
        else:
            first, count, blocked_until = entry
            count += 1
            if count >= MAX_FAILURES and blocked_until <= now:
                blocked_until = now + BLOCK_SECONDS
                just_blocked = True
                logger.warning("Login rate-limit triggered for ip=%s", ip)
            _failures[ip] = (first, count, blocked_until)
    return count, just_blocked


def clear_failures(ip: str) -> None:
    """Wipe the failure record for `ip` (call on successful login)."""
    with _lock:
        _failures.pop(ip, None)
