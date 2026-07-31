"""Guest-facing routes: login, the bet screen, and the leaderboard.

Every handler is a plain `def`, not `async def` — app.db is blocking
stdlib sqlite3, and FastAPI runs sync handlers in a threadpool so one
guest's DB call never stalls the single event loop for the other 74. (The
one legitimate `async def` exception will be Step 5's SSE endpoint, and it
must not do blocking DB work inline either.)

A guest must never see a raw error page. Every DB/service error a guest
route can hit is either mapped to a re-rendered state partial with a short
message, or redirected to /login — see _bet_screen_context and the
except clauses in post_bet/get_leaderboard.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import db
from app.auth import (
    COOKIE_NAME,
    current_guest,
    require_guest,
    set_auth_cookie,
    unsign_device_token,
)
from app.services import bets, races, scoring
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _classify_state(state: races.RaceState) -> str:
    """Pure decision logic, no DB access — takes an already-computed
    RaceState and returns one of "complete"/"locked"/"open"/"waiting".

    Kept separate from _bet_screen_context so it can be unit-tested against
    a hand-built RaceState directly, independent of whether
    races.apply_auto_lock happened to have already run. seconds_to_auto_lock
    is None whenever no auto-lock timer was set (the normal manual-lock
    path) — that must be checked with `is not None` before comparing, never
    compared bare, or a manually-run OPEN race would 500 every guest's
    screen.
    """
    if state.event_complete:
        return "complete"
    if state.status == "LOCKED":
        return "locked"
    if state.status == "OPEN":
        if state.seconds_to_auto_lock is not None and state.seconds_to_auto_lock <= 0:
            return "locked"
        return "open"
    return "waiting"  # SCHEDULED


def _bet_screen_context(
    guest_row: sqlite3.Row, now: datetime, error_message: str | None = None
) -> tuple[str, dict]:
    """The single choke point for GET /bet, GET /state, and POST /bet's
    response. Applies the auto-lock guard, reads current_state, classifies
    it via _classify_state, and builds that state's template context.
    """
    races.apply_auto_lock(now)
    state = races.current_state(now)
    guest_id = guest_row["id"]
    state_name = _classify_state(state)

    if state_name == "complete":
        guest_horse = bets.get_live_bet(guest_id, state.race_number)
        guest_points = (
            scoring.points_for_bet(guest_horse, state.result)
            if state.result is not None
            else None
        )
        return "complete", {
            "guest": guest_row,
            "race_number": state.race_number,
            "result": state.result,
            "guest_horse": guest_horse,
            "guest_points": guest_points,
            "error_message": error_message,
        }

    if state_name == "locked":
        current_horse = bets.get_live_bet(guest_id, state.race_number)
        return "locked", {
            "guest": guest_row,
            "race_number": state.race_number,
            "current_horse": current_horse,
            "error_message": error_message,
        }

    if state_name == "open":
        current_horse = bets.get_live_bet(guest_id, state.race_number)
        return "open", {
            "guest": guest_row,
            "race_number": state.race_number,
            "horses": state.horses,
            "seconds_to_auto_lock": state.seconds_to_auto_lock,
            "current_horse": current_horse,
            "error_message": error_message,
        }

    # "waiting" (SCHEDULED)
    previous_number = state.race_number - 1
    previous_result = None
    previous_horse = None
    previous_points = None
    if previous_number >= 1:
        previous_result = db.get_settled_results().get(previous_number)
        if previous_result is not None:
            previous_horse = bets.get_live_bet(guest_id, previous_number)
            previous_points = scoring.points_for_bet(previous_horse, previous_result)
    return "waiting", {
        "guest": guest_row,
        "race_number": state.race_number,
        "previous_race_number": previous_number if previous_result is not None else None,
        "previous_result": previous_result,
        "previous_horse": previous_horse,
        "previous_points": previous_points,
        "error_message": error_message,
    }


@router.get("/")
def index(guest: sqlite3.Row | None = Depends(current_guest)):
    if guest is not None:
        return RedirectResponse("/bet", status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/login")
def get_login(request: Request):
    return templates.TemplateResponse(request, "guest/login.html", {"error_message": None})


@router.post("/login")
def post_login(request: Request, username: str = Form(...)):
    now = _now()
    username = username.strip().lower()
    guest = db.fetch_guest_by_username(username)
    if guest is None:
        return templates.TemplateResponse(
            request,
            "guest/login.html",
            {"error_message": "we don't have that name, see the organiser"},
        )

    if guest["device_token"] is None:
        device_token = secrets.token_urlsafe(32)
        if db.claim_guest_device(guest["id"], device_token, now.isoformat()):
            response = RedirectResponse("/bet", status_code=303)
            set_auth_cookie(response, device_token)
            return response
        guest = db.fetch_guest_by_username(username)  # lost the race; re-read

    cookie_value = request.cookies.get(COOKIE_NAME)
    cookie_token = unsign_device_token(cookie_value) if cookie_value else None
    if cookie_token is not None and cookie_token == guest["device_token"]:
        return RedirectResponse("/bet", status_code=303)

    return templates.TemplateResponse(
        request,
        "guest/login.html",
        {"error_message": "already claimed, see the organiser"},
    )


@router.get("/bet")
def get_bet(request: Request, guest: sqlite3.Row = Depends(require_guest)):
    state_name, context = _bet_screen_context(guest, _now())
    context["state_name"] = state_name
    return templates.TemplateResponse(request, "guest/bet.html", context)


@router.get("/state")
def get_state(request: Request, guest: sqlite3.Row = Depends(require_guest)):
    state_name, context = _bet_screen_context(guest, _now())
    return templates.TemplateResponse(request, f"guest/partials/{state_name}.html", context)


@router.post("/bet")
def post_bet(
    request: Request,
    horse_number: int = Form(...),
    client_bet_id: str = Form(...),
    guest: sqlite3.Row = Depends(require_guest),
):
    now = _now()
    races.apply_auto_lock(now)
    race_number = races.current_race_number()
    error_message = None

    if race_number is None:
        logger.info("bet attempt after event complete, guest_id=%s", guest["id"])
        error_message = "Betting is closed"
    else:
        try:
            bets.place_bet(guest["id"], race_number, horse_number, client_bet_id, now)
        except (races.RaceNotFoundError, bets.BettingClosedError) as exc:
            logger.info("betting closed for guest_id=%s: %s", guest["id"], exc)
            error_message = "Betting is closed"
        except bets.HorseNotInRaceError as exc:
            logger.info("horse not in race for guest_id=%s: %s", guest["id"], exc)
            error_message = "That horse isn't running"
        except (bets.GuestNotFoundError, bets.GuestNotLoggedInError) as exc:
            logger.warning("guest login problem placing bet: %s", exc)
            return RedirectResponse("/login", status_code=303)

    state_name, context = _bet_screen_context(guest, now, error_message=error_message)
    return templates.TemplateResponse(request, f"guest/partials/{state_name}.html", context)


@router.get("/leaderboard")
def get_leaderboard(request: Request, guest: sqlite3.Row = Depends(require_guest)):
    try:
        board = scoring.build_leaderboard(
            db.get_guests(),
            db.get_logged_in_guest_ids(),
            db.get_live_bets(),
            db.get_settled_results(),
            requesting_guest_id=guest["id"],
        )
    except ValueError as exc:
        logger.warning("leaderboard lookup failed for guest_id=%s: %s", guest["id"], exc)
        return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse(
        request, "guest/leaderboard.html", {"guest": guest, "board": board}
    )
