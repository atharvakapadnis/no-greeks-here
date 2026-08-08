"""Guest identity: a device token in a signed cookie. No passwords.

`current_guest` is the only definition of "is this device logged in" — it
resolves through device_token, never claimed_at (those mean different
things: claimed_at = participating, device_token = which phone claimed the
username; see the Step 3 handoff's carry-forward notes).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

from fastapi import Depends, Request, Response
from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeSerializer,
    URLSafeTimedSerializer,
)

from app import db
from app.config import get_settings

COOKIE_NAME = "ngh_auth"
_SALT = "guest-device-token"
_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days


class GuestLoginRequired(Exception):
    """Raised by require_guest when no guest is resolved from the request.

    main.py registers an exception handler for this that redirects to
    /login — this is what lets every protected route depend on
    require_guest without repeating an `if guest is None: redirect` check.
    """


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().SECRET_KEY, salt=_SALT)


def sign_device_token(device_token: str) -> str:
    return _serializer().dumps(device_token)


def unsign_device_token(cookie_value: str) -> str | None:
    try:
        return _serializer().loads(cookie_value)
    except BadSignature:
        return None


def set_auth_cookie(response: Response, device_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        sign_device_token(device_token),
        max_age=_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def current_guest(request: Request) -> sqlite3.Row | None:
    """FastAPI dependency. None on: no cookie, tampered/unsigned cookie, or
    a well-signed token that no longer matches any guest (e.g. unlocked).
    """
    cookie_value = request.cookies.get(COOKIE_NAME)
    if cookie_value is None:
        return None
    device_token = unsign_device_token(cookie_value)
    if device_token is None:
        return None
    return db.fetch_guest_by_device_token(device_token)


def require_guest(guest: sqlite3.Row | None = Depends(current_guest)) -> sqlite3.Row:
    if guest is None:
        raise GuestLoginRequired()
    return guest


# --- operator session: separate cookie, separate salt, no password stored ---
#
# Entirely independent of the guest block above: distinct cookie name,
# distinct itsdangerous salt, no shared code path. A guest cookie must never
# authenticate an operator route and vice versa.

OPERATOR_COOKIE_NAME = "ngh_operator_auth"
_OPERATOR_MARKER = "operator"
_OPERATOR_MAX_AGE_SECONDS = 60 * 60 * 24  # 24h


class OperatorLoginRequired(Exception):
    """Raised by require_operator when no valid operator session is found.

    main.py registers a handler for this that redirects to /operator/login
    — same shape as GuestLoginRequired -> /login.
    """


def _operator_salt() -> str:
    """Derived from the current OPERATOR_PASSWORD, not a fixed string like
    the guest salt. Operator auth has no device_token row to re-check a
    signed cookie against — the signature is the only proof that whoever
    set it knew the password at signing time. Deriving the salt from the
    password means rotating OPERATOR_PASSWORD invalidates every previously
    issued cookie on its very next use, with no separate revocation list.
    """
    settings = get_settings()
    return "operator-session:" + hashlib.sha256(
        settings.OPERATOR_PASSWORD.encode()
    ).hexdigest()


def _operator_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SECRET_KEY, salt=_operator_salt())


def sign_operator_marker() -> str:
    return _operator_serializer().dumps(_OPERATOR_MARKER)


def unsign_operator_marker(cookie_value: str) -> bool:
    """True iff cookie_value is a signature made with the *current*
    password's salt, within the 24h server-side window enforced here (not
    just via the browser-honored Set-Cookie Max-Age). Never raises.
    """
    try:
        marker = _operator_serializer().loads(
            cookie_value, max_age=_OPERATOR_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return False
    return marker == _OPERATOR_MARKER


def set_operator_auth_cookie(response: Response) -> None:
    settings = get_settings()
    response.set_cookie(
        OPERATOR_COOKIE_NAME,
        sign_operator_marker(),
        max_age=_OPERATOR_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def verify_operator_password(password: str) -> bool:
    settings = get_settings()
    return secrets.compare_digest(password, settings.OPERATOR_PASSWORD)


def current_operator(request: Request) -> bool:
    cookie_value = request.cookies.get(OPERATOR_COOKIE_NAME)
    if cookie_value is None:
        return False
    return unsign_operator_marker(cookie_value)


def require_operator(is_operator: bool = Depends(current_operator)) -> None:
    if not is_operator:
        raise OperatorLoginRequired()


# --- operator login rate limiting -------------------------------------------
#
# Global, not per-IP: behind Caddy every device at the venue shares one
# NAT'd address, and request.client.host is often just the proxy's own
# loopback address regardless of which guest is connecting — so a per-IP
# counter would in practice be one global counter anyway. This is honest
# about that instead of pretending an isolation that doesn't exist there.
#
# Never rejects a correct password outright. An operator mistyping their
# own password in a noisy room, with a race waiting, must never be locked
# out of their own event. Once 5 failures have accumulated, every
# subsequent WRONG password is answered after a fixed delay instead of
# instantly — enough friction to make automated guessing impractical
# without ever blocking the person who actually knows the password. A
# correct password always succeeds immediately and resets the counter.

_RATE_LIMIT_DELAY_THRESHOLD = 5
_RATE_LIMIT_DELAY_SECONDS = 2

_operator_login_failures = 0


def operator_login_delay_seconds() -> float:
    """Delay to apply before responding to a WRONG password attempt, given
    the current global failure count. Callers must never apply this on the
    success path — a correct password always responds immediately,
    regardless of the failure count.
    """
    return (
        _RATE_LIMIT_DELAY_SECONDS
        if _operator_login_failures >= _RATE_LIMIT_DELAY_THRESHOLD
        else 0
    )


def record_operator_login_failure() -> None:
    global _operator_login_failures
    _operator_login_failures += 1


def record_operator_login_success() -> None:
    global _operator_login_failures
    _operator_login_failures = 0


def enforce_operator_login_delay() -> None:
    """Sleeps for operator_login_delay_seconds() if nonzero. Kept as its own
    function (rather than inlining time.sleep at each call site) so tests
    can monkeypatch auth.time.sleep to a no-op instead of taking real
    seconds per test.
    """
    delay = operator_login_delay_seconds()
    if delay:
        time.sleep(delay)


# --- operator flash messages -------------------------------------------------
#
# No session store exists anywhere in this app. A one-shot signed cookie
# carries feedback (an error key, or the generated username on add-guest)
# across the mandatory 303 redirect back to /operator — GET /operator reads
# it once and the response always deletes it, so a refresh or back-navigate
# never re-shows a stale result. The payload is a message KEY, never free
# text: all operator-facing copy lives in one place (operator.py's
# FLASH_MESSAGES), so nothing user-influenced is ever rendered as HTML.

_FLASH_COOKIE_NAME = "ngh_operator_flash"
_FLASH_SALT = "operator-flash"
_FLASH_MAX_AGE_SECONDS = 30


def _flash_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SECRET_KEY, salt=_FLASH_SALT)


def set_flash_cookie(response: Response, key: str, **detail: str) -> None:
    settings = get_settings()
    payload = {"key": key, **detail}
    response.set_cookie(
        _FLASH_COOKIE_NAME,
        _flash_serializer().dumps(payload),
        max_age=_FLASH_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def read_flash_cookie(request: Request) -> dict | None:
    """Pure read, no cookie mutation — call before building the response
    context, since Jinja2Templates.TemplateResponse renders its body at
    construction time, too early to still be adjusting context afterward.
    """
    cookie_value = request.cookies.get(_FLASH_COOKIE_NAME)
    if cookie_value is None:
        return None
    try:
        return _flash_serializer().loads(cookie_value, max_age=_FLASH_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def clear_flash_cookie(response: Response) -> None:
    """Always deletes the cookie on the given response, whether or not a
    valid flash was found, so a tampered/expired cookie doesn't linger.
    """
    response.delete_cookie(_FLASH_COOKIE_NAME)
