"""Race state machine: SCHEDULED -> OPEN -> LOCKED -> SETTLED, with reopen
and correction recovery paths.

Not pure: reads and writes through app.db. Still no FastAPI, no routers, no
templates, no SSE, no AWS imports — Step 4 calls into this module, never
the other way around.

Every function that records or compares a time takes an explicit
`now: datetime` (timezone-aware UTC). No function here calls
datetime.now() itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import db
from app.services.scoring import RaceResult


class RaceError(Exception):
    """Base class for race state machine errors."""


class RaceNotFoundError(RaceError):
    """race_number does not exist."""


class IllegalTransitionError(RaceError):
    """The requested transition is not legal from the race's current status."""


class AnotherRaceOpenError(RaceError):
    """A different race is already OPEN; at most one may be OPEN at a time."""


class InvalidResultError(RaceError):
    """Placings are not distinct, are not entries in this race, are
    scratched, or (for set_scratched) horse_number is not an entry in this
    race at all.
    """


@dataclass(frozen=True)
class HorseEntry:
    number: int
    name: str | None
    scratched: bool


@dataclass(frozen=True)
class RaceState:
    """Everything the operator panel and guest screens need, in one cheap,
    always-complete snapshot. race_number falls back to the final race once
    event_complete is True — current_state never returns a null/empty view.
    """

    race_number: int
    total_races: int
    status: str  # SCHEDULED | OPEN | LOCKED | SETTLED
    seconds_to_auto_lock: int | None
    horses: list[HorseEntry]
    live_bet_count: int
    result: RaceResult | None  # populated only when status == SETTLED
    event_complete: bool  # True only when every race is SETTLED


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp)


def _get_race(race_number: int):
    race = db.fetch_race(race_number)
    if race is None:
        raise RaceNotFoundError(f"race {race_number} not found")
    return race


def _other_race_open(race_number: int) -> bool:
    return any(
        r["status"] == "OPEN" and r["number"] != race_number for r in db.fetch_races()
    )


def _entries_by_horse(race_number: int) -> dict[int, object]:
    return {e["horse_number"]: e for e in db.get_race_entries(race_number)}


def _validate_placings(race_number: int, first: int, second: int, third: int) -> None:
    placings = (first, second, third)
    if len(set(placings)) != 3:
        raise InvalidResultError(
            f"placings must be distinct, got {placings!r}"
        )
    entries = _entries_by_horse(race_number)
    for horse_number in placings:
        entry = entries.get(horse_number)
        if entry is None:
            raise InvalidResultError(
                f"horse {horse_number} is not entered in race {race_number}"
            )
        if entry["scratched"]:
            raise InvalidResultError(
                f"horse {horse_number} is scratched in race {race_number}"
            )


def open_race(
    race_number: int, now: datetime, auto_lock_seconds: int | None = None
) -> None:
    race = _get_race(race_number)
    if race["status"] == "OPEN":
        return
    if race["status"] != "SCHEDULED":
        raise IllegalTransitionError(
            f"cannot open race {race_number} from status {race['status']}"
        )
    if _other_race_open(race_number):
        raise AnotherRaceOpenError(
            f"cannot open race {race_number}: another race is already open"
        )
    auto_lock_at = (
        _iso(now + timedelta(seconds=auto_lock_seconds))
        if auto_lock_seconds is not None
        else None
    )
    with db.transaction() as conn:
        db.update_race_status(race_number, "OPEN", conn=conn, opened_at=_iso(now))
        db.set_race_auto_lock(race_number, auto_lock_at, conn=conn)
        db.append_audit_log(
            _iso(now),
            "operator",
            "race.opened",
            json.dumps(
                {"race_number": race_number, "auto_lock_seconds": auto_lock_seconds}
            ),
            conn=conn,
        )


def lock_race(race_number: int, now: datetime) -> None:
    race = _get_race(race_number)
    if race["status"] == "LOCKED":
        return
    if race["status"] != "OPEN":
        raise IllegalTransitionError(
            f"cannot lock race {race_number} from status {race['status']}"
        )
    with db.transaction() as conn:
        db.update_race_status(race_number, "LOCKED", conn=conn, locked_at=_iso(now))
        db.set_race_auto_lock(race_number, None, conn=conn)
        db.append_audit_log(
            _iso(now),
            "operator",
            "race.locked",
            json.dumps({"race_number": race_number}),
            conn=conn,
        )


def settle_race(
    race_number: int, first: int, second: int, third: int, now: datetime
) -> None:
    race = _get_race(race_number)
    if race["status"] != "LOCKED":
        raise IllegalTransitionError(
            f"cannot settle race {race_number} from status {race['status']}"
        )
    _validate_placings(race_number, first, second, third)
    with db.transaction() as conn:
        db.settle_race_result(race_number, first, second, third, _iso(now), conn=conn)
        db.append_audit_log(
            _iso(now),
            "operator",
            "race.settled",
            json.dumps(
                {"race_number": race_number, "first": first, "second": second, "third": third}
            ),
            conn=conn,
        )


def reopen_race(race_number: int, now: datetime) -> None:
    race = _get_race(race_number)
    if race["status"] == "OPEN":
        return
    if race["status"] != "LOCKED":
        raise IllegalTransitionError(
            f"cannot reopen race {race_number} from status {race['status']}"
        )
    if _other_race_open(race_number):
        raise AnotherRaceOpenError(
            f"cannot reopen race {race_number}: another race is already open"
        )
    with db.transaction() as conn:
        db.update_race_status(race_number, "OPEN", conn=conn, locked_at=None)
        db.append_audit_log(
            _iso(now),
            "operator",
            "race.reopened",
            json.dumps({"race_number": race_number}),
            conn=conn,
        )


def correct_result(
    race_number: int, first: int, second: int, third: int, now: datetime
) -> None:
    race = _get_race(race_number)
    if race["status"] != "SETTLED":
        raise IllegalTransitionError(
            f"cannot correct race {race_number} from status {race['status']}"
        )
    _validate_placings(race_number, first, second, third)
    with db.transaction() as conn:
        db.settle_race_result(race_number, first, second, third, _iso(now), conn=conn)
        db.append_audit_log(
            _iso(now),
            "operator",
            "race.result_corrected",
            json.dumps(
                {
                    "race_number": race_number,
                    "previous_first": race["first"],
                    "previous_second": race["second"],
                    "previous_third": race["third"],
                    "first": first,
                    "second": second,
                    "third": third,
                }
            ),
            conn=conn,
        )


def set_scratched(
    race_number: int, horse_number: int, scratched: bool, now: datetime
) -> None:
    """Permitted while SCHEDULED or OPEN — a horse can pull up lame after
    betting opens, so LOCKED/SETTLED are the only illegal statuses.

    Scratching (scratched=True) a horse mid-OPEN voids every live bet on
    that horse in this race (superseded, no replacement) in the same
    transaction as the flag change and the single audit row. Unscratching
    never restores voided bets — guests must re-bet.
    """
    race = _get_race(race_number)
    if race["status"] in ("LOCKED", "SETTLED"):
        raise IllegalTransitionError(
            f"cannot change scratch status for race {race_number} "
            f"from status {race['status']}"
        )
    entries = _entries_by_horse(race_number)
    entry = entries.get(horse_number)
    if entry is None:
        raise InvalidResultError(
            f"horse {horse_number} is not entered in race {race_number}"
        )
    if bool(entry["scratched"]) == scratched:
        return
    with db.transaction() as conn:
        db.set_horse_scratched(race_number, horse_number, scratched, conn=conn)
        voided_guest_ids: list[int] = []
        if scratched:
            voided_guest_ids = db.void_live_bets_for_horse(
                race_number, horse_number, _iso(now), conn=conn
            )
        db.append_audit_log(
            _iso(now),
            "operator",
            "race.scratch_set",
            json.dumps(
                {
                    "race_number": race_number,
                    "horse_number": horse_number,
                    "scratched": scratched,
                    "voided_guest_ids": voided_guest_ids,
                }
            ),
            conn=conn,
        )


def apply_auto_lock(now: datetime) -> int | None:
    """If the OPEN race's auto_lock_at has passed, locks it (with audit row)
    and returns its number. No-op (returns None) if no race is OPEN,
    auto_lock_at is NULL, or it hasn't passed yet. Never relies on this
    having been called — bet acceptance checks the clock directly too.
    """
    open_races = [r for r in db.fetch_races() if r["status"] == "OPEN"]
    if not open_races:
        return None
    race = open_races[0]
    if race["auto_lock_at"] is None:
        return None
    if _parse(race["auto_lock_at"]) > now:
        return None
    race_number = race["number"]
    with db.transaction() as conn:
        db.update_race_status(race_number, "LOCKED", conn=conn, locked_at=_iso(now))
        db.set_race_auto_lock(race_number, None, conn=conn)
        db.append_audit_log(
            _iso(now),
            "system",
            "race.auto_locked",
            json.dumps({"race_number": race_number}),
            conn=conn,
        )
    return race_number


def current_race_number() -> int | None:
    """The lowest-numbered race that is not SETTLED. Derived, never stored.
    Returns None when every race is settled.
    """
    candidates = [r["number"] for r in db.fetch_races() if r["status"] != "SETTLED"]
    return min(candidates) if candidates else None


def current_state(now: datetime) -> RaceState:
    """The single source of truth for "what is happening right now". Cheap:
    a handful of indexed queries, no partial views.

    Once every race is SETTLED, current_race_number() still returns None
    (unchanged), but current_state falls back to the highest-numbered race
    and reports it SETTLED with its result, plus event_complete=True — it
    never returns a null/empty view.
    """
    races = db.fetch_races()
    race_number = current_race_number()
    event_complete = race_number is None
    target_number = race_number if race_number is not None else max(
        r["number"] for r in races
    )
    race = db.fetch_race(target_number)

    horses_by_number = {h["number"]: h for h in db.get_horses()}
    horses = [
        HorseEntry(
            number=e["horse_number"],
            name=horses_by_number[e["horse_number"]]["name"],
            scratched=bool(e["scratched"]),
        )
        for e in db.get_race_entries(target_number)
    ]

    seconds_to_auto_lock = None
    if race["status"] == "OPEN" and race["auto_lock_at"] is not None:
        remaining = (_parse(race["auto_lock_at"]) - now).total_seconds()
        seconds_to_auto_lock = max(0, int(remaining))

    result = None
    if race["status"] == "SETTLED":
        result = RaceResult(
            race_number=target_number,
            first=race["first"],
            second=race["second"],
            third=race["third"],
        )

    return RaceState(
        race_number=target_number,
        total_races=len(races),
        status=race["status"],
        seconds_to_auto_lock=seconds_to_auto_lock,
        horses=horses,
        live_bet_count=db.count_live_bets(target_number),
        result=result,
        event_complete=event_complete,
    )
