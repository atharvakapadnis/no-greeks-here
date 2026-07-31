"""Guest identity: a device token in a signed cookie. No passwords.

`current_guest` is the only definition of "is this device logged in" — it
resolves through device_token, never claimed_at (those mean different
things: claimed_at = participating, device_token = which phone claimed the
username; see the Step 3 handoff's carry-forward notes).
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

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
