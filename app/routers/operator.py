"""Operator-facing routes: the single race-control panel.

Every handler is a plain `def`, not `async def` — same reason as
app/routers/guest.py: app.db is blocking stdlib sqlite3, and FastAPI runs
sync handlers in a threadpool.

The operator SHOULD see what went wrong (unlike the guest side): every
RaceError/BetError subtype this module can hit is mapped to a specific,
plain-English message via FLASH_MESSAGES, never a raw error page or a 500.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import db
from app.auth import (
    clear_flash_cookie,
    enforce_operator_login_delay,
    read_flash_cookie,
    record_operator_login_failure,
    record_operator_login_success,
    require_operator,
    set_flash_cookie,
    set_operator_auth_cookie,
    verify_operator_password,
)
from app.services import bets, guests, races, scoring
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

AUTO_LOCK_CHOICES = [None, 60, 90, 120, 180]
_AUTO_LOCK_ALLOWED = set(AUTO_LOCK_CHOICES)

# Every operator-facing message lives here, keyed by a short code the routes
# below set on the flash cookie — never free text, so all the copy is in one
# place and nothing user-influenced is ever rendered as HTML.
FLASH_MESSAGES: dict[str, str] = {
    "race_stale": "The board has moved on — refresh and try again.",
    "race_not_found": "That race no longer exists — refresh and try again.",
    "race_open_illegal": "That race can't be opened from its current state — refresh and try again.",
    "race_another_open": "Another race is already open — lock or settle it first.",
    "race_lock_illegal": "That race isn't open — refresh and try again.",
    "race_reopen_illegal": "That race isn't locked — refresh and try again.",
    "race_settle_illegal": "That race isn't locked yet — refresh and try again.",
    "race_invalid_result": "Placings must be three different horses that are entered and not scratched.",
    "race_correct_illegal": "That race hasn't been settled yet — refresh and try again.",
    "race_scratch_illegal": "Scratches can't be changed once a race is locked or settled.",
    "race_scratch_invalid_horse": "That horse isn't entered in this race.",
    "auto_lock_invalid": "Choose a valid auto-lock duration.",
    "bet_closed": "Betting is closed for the current race.",
    "bet_horse_not_in_race": "That horse isn't running in this race.",
    "guest_not_found": "That guest doesn't exist.",
    "guest_name_blank": "Enter a name.",
    "guest_added": "Guest added:",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _flash_redirect(key: str, **detail: str) -> RedirectResponse:
    response = RedirectResponse("/operator", status_code=303)
    set_flash_cookie(response, key, **detail)
    return response


# --- _effective_state ---------------------------------------------------


def _reconstruct_settled_state(
    race_row: sqlite3.Row, total_races: int
) -> races.RaceState:
    """Rebuilds the RaceState races.current_state(now) would have returned
    for race_row at the moment it settled, before current_race_number()
    advanced past it. Duplicates the relevant slice of current_state's body
    on purpose — races.py has no "describe an arbitrary non-current race"
    entrypoint, and shouldn't grow one just for this read-only panel view.

    READ-ONLY: the RaceState this returns must never be passed to any
    races.py/bets.py write function.
    """
    race_number = race_row["number"]
    horses_by_number = {h["number"]: h for h in db.get_horses()}
    horses = [
        races.HorseEntry(
            number=e["horse_number"],
            name=horses_by_number[e["horse_number"]]["name"],
            scratched=bool(e["scratched"]),
        )
        for e in db.get_race_entries(race_number)
    ]
    result = scoring.RaceResult(
        race_number=race_number,
        first=race_row["first"],
        second=race_row["second"],
        third=race_row["third"],
    )
    return races.RaceState(
        race_number=race_number,
        total_races=total_races,
        status="SETTLED",
        seconds_to_auto_lock=None,
        horses=horses,
        live_bet_count=db.count_live_bets(race_number),
        result=result,
        event_complete=False,
    )


def _effective_state(now: datetime) -> tuple[races.RaceState, str]:
    """Reconciles races.current_state's "lowest not-SETTLED race" contract
    with the operator's need to see the race it just published before
    moving on.

    current_state(now) never reports "race N, SETTLED" except for the final
    race (event_complete=True) — the instant race N settles,
    current_race_number() advances and current_state reports race N+1 as
    SCHEDULED instead. This function reconstructs that missing window by
    looking one race back whenever the real state is SCHEDULED with a
    SETTLED predecessor.

    In that reconstructed case the returned RaceState describes the
    PREVIOUS race (via _reconstruct_settled_state, built by hand from
    db.fetch_race/get_race_entries/get_horses/count_live_bets for race N —
    NOT races.current_state's output), and MUST NEVER be passed to any
    races.py/bets.py write function. Every POST action route re-derives its
    own authority from a fresh races.current_state(now) (or, for
    /race/correct, from db.get_settled_results() — see that route), never
    from this function's return value.
    """
    state = races.current_state(now)

    # 1. Must be checked first. The gap case does NOT apply to the final
    #    race: current_state's event_complete fallback already reports the
    #    final race as SETTLED (not the next race as SCHEDULED), so step 2
    #    would never fire for it anyway — this ordering is what stops the
    #    panel from reconstructing a settled view and offering to open a
    #    race that doesn't exist.
    if state.event_complete:
        return state, "complete"

    # 2. The gap: current race reports SCHEDULED, but its predecessor is
    #    SETTLED and hasn't been shown as "just settled" yet. Guard on BOTH
    #    conditions — race 1 is SCHEDULED with no predecessor and must fall
    #    through to "scheduled", not be inferred as a gap.
    if state.status == "SCHEDULED" and state.race_number > 1:
        previous = db.fetch_race(state.race_number - 1)
        if previous is not None and previous["status"] == "SETTLED":
            return (
                _reconstruct_settled_state(previous, state.total_races),
                "settled",
            )

    # 3. Otherwise: view from status. status here is guaranteed to be one
    #    of SCHEDULED/OPEN/LOCKED — current_state only reports SETTLED for
    #    its target race when event_complete is True (handled in step 1),
    #    since target_number always equals current_race_number(), which by
    #    definition excludes SETTLED races.
    if (
        state.status == "OPEN"
        and state.seconds_to_auto_lock is not None
        and state.seconds_to_auto_lock <= 0
    ):
        return state, "locked"  # same OPEN-past-auto-lock rule as guest's _classify_state
    return state, state.status.lower()  # "scheduled" | "open" | "locked"


def _backup_status() -> str:
    """Stub — Litestream doesn't exist until Step 6. The UI slot exists now
    so the footer layout doesn't need to change later; wire the real
    freshness check there.
    """
    return "not configured"


def _horses_for_race(race_number: int) -> list[races.HorseEntry]:
    horses_by_number = {h["number"]: h for h in db.get_horses()}
    return [
        races.HorseEntry(
            number=e["horse_number"],
            name=horses_by_number[e["horse_number"]]["name"],
            scratched=bool(e["scratched"]),
        )
        for e in db.get_race_entries(race_number)
    ]


def _flash_context(flash: dict | None) -> dict:
    if not flash:
        return {"flash_message": None, "flash_key": None, "flash_username": None}
    key = flash.get("key")
    return {
        "flash_message": FLASH_MESSAGES.get(key, ""),
        "flash_key": key,
        "flash_username": flash.get("username") if key == "guest_added" else None,
    }


def _panel_context(
    state: races.RaceState, view: str, now: datetime, flash: dict | None
) -> dict:
    context: dict = {
        "view": view,
        "state": state,
        "backup_status": _backup_status(),
    }
    context.update(_flash_context(flash))

    if view == "scheduled":
        context["auto_lock_choices"] = AUTO_LOCK_CHOICES

    elif view == "open":
        bet_guest_ids = {
            b["guest_id"] for b in db.fetch_bets_for_race(state.race_number)
        }
        logged_in_ids = db.get_logged_in_guest_ids()
        context["bet_count"] = state.live_bet_count
        context["guest_count"] = len(logged_in_ids)
        context["not_bet"] = [
            g
            for g in db.get_guests()
            if g.guest_id in logged_in_ids and g.guest_id not in bet_guest_ids
        ]

    elif view == "settled":
        next_race_number = state.race_number + 1
        next_race = db.fetch_race(next_race_number)
        context["next_race_number"] = next_race_number
        context["next_horses"] = (
            _horses_for_race(next_race_number) if next_race is not None else []
        )
        context["auto_lock_choices"] = AUTO_LOCK_CHOICES

    elif view == "complete":
        all_guests = db.get_guests()
        n = max(len(all_guests), 1)
        context["board"] = scoring.build_leaderboard(
            all_guests,
            db.get_logged_in_guest_ids(),
            db.get_live_bets(),
            db.get_settled_results(),
            requesting_guest_id=None,
            top_n=n,
            max_rows=n,
        )

    return context


# --- login ----------------------------------------------------------------


@router.get("/operator/login")
def get_operator_login(request: Request):
    return templates.TemplateResponse(
        request, "operator/login.html", {"error_message": None}
    )


@router.post("/operator/login")
def post_operator_login(request: Request, password: str = Form(...)):
    if verify_operator_password(password):
        record_operator_login_success()
        response = RedirectResponse("/operator", status_code=303)
        set_operator_auth_cookie(response)
        return response
    enforce_operator_login_delay()
    record_operator_login_failure()
    return templates.TemplateResponse(
        request, "operator/login.html", {"error_message": "Incorrect password."}
    )


# --- the panel --------------------------------------------------------------


@router.get("/operator")
def get_operator(request: Request, _: None = Depends(require_operator)):
    now = _now()
    races.apply_auto_lock(now)
    state, view = _effective_state(now)
    flash = read_flash_cookie(request)
    context = _panel_context(state, view, now, flash)
    response = templates.TemplateResponse(request, "operator/panel.html", context)
    clear_flash_cookie(response)
    return response


# --- race actions -------------------------------------------------------


def _parse_auto_lock_seconds(raw: str) -> int | None:
    """Raises ValueError if raw isn't "" or one of AUTO_LOCK_CHOICES."""
    if raw == "":
        return None
    value = int(raw)
    if value not in _AUTO_LOCK_ALLOWED:
        raise ValueError(f"invalid auto_lock_seconds: {raw!r}")
    return value


@router.post("/operator/race/open")
def post_race_open(
    race_number: int = Form(...),
    auto_lock_seconds: str = Form(""),
    _: None = Depends(require_operator),
):
    now = _now()
    races.apply_auto_lock(now)
    real = races.current_state(now)
    if race_number != real.race_number:
        return _flash_redirect("race_stale")
    try:
        auto_lock = _parse_auto_lock_seconds(auto_lock_seconds)
    except ValueError:
        return _flash_redirect("auto_lock_invalid")
    try:
        races.open_race(race_number, now, auto_lock)
    except races.RaceNotFoundError:
        return _flash_redirect("race_not_found")
    except races.IllegalTransitionError:
        return _flash_redirect("race_open_illegal")
    except races.AnotherRaceOpenError:
        return _flash_redirect("race_another_open")
    return RedirectResponse("/operator", status_code=303)


@router.post("/operator/race/lock")
def post_race_lock(race_number: int = Form(...), _: None = Depends(require_operator)):
    now = _now()
    races.apply_auto_lock(now)
    real = races.current_state(now)
    if race_number != real.race_number:
        return _flash_redirect("race_stale")
    try:
        races.lock_race(race_number, now)
    except races.RaceNotFoundError:
        return _flash_redirect("race_not_found")
    except races.IllegalTransitionError:
        return _flash_redirect("race_lock_illegal")
    return RedirectResponse("/operator", status_code=303)


@router.post("/operator/race/reopen")
def post_race_reopen(
    race_number: int = Form(...), _: None = Depends(require_operator)
):
    now = _now()
    races.apply_auto_lock(now)
    real = races.current_state(now)
    if race_number != real.race_number:
        return _flash_redirect("race_stale")
    try:
        races.reopen_race(race_number, now)
    except races.RaceNotFoundError:
        return _flash_redirect("race_not_found")
    except races.IllegalTransitionError:
        return _flash_redirect("race_reopen_illegal")
    except races.AnotherRaceOpenError:
        return _flash_redirect("race_another_open")
    return RedirectResponse("/operator", status_code=303)


@router.post("/operator/race/settle")
def post_race_settle(
    request: Request,
    race_number: int = Form(...),
    first: int = Form(...),
    second: int = Form(...),
    third: int = Form(...),
    confirmed: str = Form(""),
    _: None = Depends(require_operator),
):
    """Two-step, server-rendered confirm — no window.confirm(), which is
    unreliable on iOS Safari and this is the one confirm step in the app.
    The first POST (confirmed empty) renders operator/confirm.html naming
    the placings, without settling anything. Its "Confirm and publish"
    button re-POSTs here with confirmed=1, which actually settles.
    """
    now = _now()
    races.apply_auto_lock(now)
    real = races.current_state(now)
    if race_number != real.race_number:
        return _flash_redirect("race_stale")

    if not confirmed:
        return templates.TemplateResponse(
            request,
            "operator/confirm.html",
            {
                "action": "/operator/race/settle",
                "verb": "Publish",
                "race_number": race_number,
                "first": first,
                "second": second,
                "third": third,
            },
        )

    try:
        races.settle_race(race_number, first, second, third, now)
    except races.RaceNotFoundError:
        return _flash_redirect("race_not_found")
    except races.IllegalTransitionError:
        return _flash_redirect("race_settle_illegal")
    except races.InvalidResultError:
        return _flash_redirect("race_invalid_result")
    return RedirectResponse("/operator", status_code=303)


@router.post("/operator/race/correct")
def post_race_correct(
    request: Request,
    race_number: int = Form(...),
    first: int = Form(...),
    second: int = Form(...),
    third: int = Form(...),
    confirmed: str = Form(""),
    _: None = Depends(require_operator),
):
    """correct_result's own fresh SETTLED check passes for EVERY settled
    race, forever — that alone isn't a stale-tab guard, since a stale tab
    from two races ago would silently rewrite an old result. So this route
    validates race_number against the MOST RECENTLY settled race
    (db.get_settled_results()), not against races.current_state(now), whose
    "current" race is never the one correct_result targets except on the
    final race.
    """
    now = _now()
    races.apply_auto_lock(now)

    settled = db.get_settled_results()
    if not settled or race_number != max(settled):
        return _flash_redirect("race_stale")

    if not confirmed:
        return templates.TemplateResponse(
            request,
            "operator/confirm.html",
            {
                "action": "/operator/race/correct",
                "verb": "Correct",
                "race_number": race_number,
                "first": first,
                "second": second,
                "third": third,
            },
        )

    try:
        races.correct_result(race_number, first, second, third, now)
    except races.RaceNotFoundError:
        return _flash_redirect("race_not_found")
    except races.IllegalTransitionError:
        return _flash_redirect("race_correct_illegal")
    except races.InvalidResultError:
        return _flash_redirect("race_invalid_result")
    return RedirectResponse("/operator", status_code=303)


@router.post("/operator/race/scratch")
def post_race_scratch(
    race_number: int = Form(...),
    horse_number: int = Form(...),
    scratched: bool = Form(...),
    _: None = Depends(require_operator),
):
    now = _now()
    races.apply_auto_lock(now)
    real = races.current_state(now)
    if race_number != real.race_number:
        return _flash_redirect("race_stale")
    try:
        races.set_scratched(race_number, horse_number, scratched, now)
    except races.RaceNotFoundError:
        return _flash_redirect("race_not_found")
    except races.IllegalTransitionError:
        return _flash_redirect("race_scratch_illegal")
    except races.InvalidResultError:
        return _flash_redirect("race_scratch_invalid_horse")
    return RedirectResponse("/operator", status_code=303)


# --- guest management ---------------------------------------------------


@router.post("/operator/guest/add")
def post_guest_add(
    display_name: str = Form(...), _: None = Depends(require_operator)
):
    now = _now()
    try:
        name = guests.split_full_name(display_name)
    except ValueError:
        return _flash_redirect("guest_name_blank")
    existing_usernames = {g["username"] for g in db.fetch_guests()}
    username = guests.add_guest_username(name, existing_usernames)
    db.insert_guest(username, display_name.strip(), now.isoformat())
    return _flash_redirect("guest_added", username=username)


@router.post("/operator/guest/unlock")
def post_guest_unlock(
    guest_id: int = Form(...), _: None = Depends(require_operator)
):
    guest = db.fetch_guest_by_id(guest_id)
    if guest is None:
        return _flash_redirect("guest_not_found")
    db.clear_guest_device(guest_id)
    return RedirectResponse("/operator", status_code=303)


@router.post("/operator/bet/set")
def post_bet_set(
    guest_id: int = Form(...),
    horse_number: int = Form(...),
    _: None = Depends(require_operator),
):
    now = _now()
    races.apply_auto_lock(now)
    real = races.current_state(now)
    try:
        bets.operator_set_bet(
            guest_id, real.race_number, horse_number, actor="operator", now=now
        )
    except races.RaceNotFoundError:
        return _flash_redirect("race_not_found")
    except bets.BettingClosedError:
        return _flash_redirect("bet_closed")
    except bets.HorseNotInRaceError:
        return _flash_redirect("bet_horse_not_in_race")
    except bets.GuestNotFoundError:
        return _flash_redirect("guest_not_found")
    return RedirectResponse("/operator", status_code=303)


# --- export -----------------------------------------------------------------


@router.get("/operator/export")
def get_export(_: None = Depends(require_operator)):
    all_guests = db.get_guests()
    n = max(len(all_guests), 1)
    board = scoring.build_leaderboard(
        all_guests,
        db.get_logged_in_guest_ids(),
        db.get_live_bets(),
        db.get_settled_results(),
        requesting_guest_id=None,
        top_n=n,
        max_rows=n,
    )
    payload = {
        "rows": [
            {
                "guest_id": row.guest_id,
                "display_name": row.display_name,
                "total_points": row.total_points,
                "rank": row.rank,
            }
            for row in board.rows
        ],
        "truncated_count": board.truncated_count,
        "truncated_points": board.truncated_points,
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": "attachment; filename=standings.json"},
    )
