"""Bet acceptance rules: guest-facing placement and the operator's
fix-a-bet override.

Not pure: reads and writes through app.db. No FastAPI, no routers, no
templates, no SSE, no AWS imports.

Every function that records or compares a time takes an explicit
`now: datetime` (timezone-aware UTC). No function here calls
datetime.now() itself.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

from app import db
from app.services.races import RaceNotFoundError


class BetError(Exception):
    """Base class for bet acceptance errors."""


class BettingClosedError(BetError):
    """The race is not OPEN, or its auto_lock_at has passed."""


class HorseNotInRaceError(BetError):
    """horse_number is not an entry in this race, or is scratched."""


class GuestNotFoundError(BetError):
    """guest_id does not exist."""


class GuestNotLoggedInError(BetError):
    """The guest exists but has not claimed a device (claimed_at is NULL)."""


@dataclass(frozen=True)
class BetOutcome:
    bet_id: int
    horse_number: int
    replaced: bool
    idempotent: bool


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp)


def _entries_by_horse(race_number: int) -> dict[int, object]:
    return {e["horse_number"]: e for e in db.get_race_entries(race_number)}


def _outcome_from_row(row, *, idempotent: bool) -> BetOutcome:
    """Builds a BetOutcome entirely from a stored bet row — never from a
    caller-submitted horse_number. Required for correctness on the
    idempotent path: a stale retry's client_bet_id may point at a bet row
    that has since been superseded by a different, faster request. Echoing
    what the caller submitted instead of what's actually stored would tell
    the guest they're confirmed on a horse the database doesn't have them
    on. Callers must always render BetOutcome.horse_number, never the horse
    they submitted (carry forward: Step 4 routers must do the same).
    """
    return BetOutcome(
        bet_id=row["id"],
        horse_number=row["horse_number"],
        replaced=False,
        idempotent=idempotent,
    )


def place_bet(
    guest_id: int,
    race_number: int,
    horse_number: int,
    client_bet_id: str,
    now: datetime,
) -> BetOutcome:
    """The guest-facing path.

    Idempotency runs first, before any other validation: if client_bet_id
    was already written, the outcome is returned unchanged (built from the
    stored row — see _outcome_from_row) with no further checks. This
    guarantees a retried request never raises even if race/guest state has
    since changed (e.g. the race locked between the original request and
    the retry) — "never surface an error to the guest" applies to retries
    unconditionally.

    For a genuinely new client_bet_id, validates in order: guest exists and
    is logged in; race exists and is OPEN; auto_lock_at has not passed
    (checked against the clock directly, independent of stored status —
    never rely on apply_auto_lock having run); horse is an entry in this
    race and not scratched. Then writes via db.place_or_replace_bet.

    A sqlite3.IntegrityError from that write (the client_bet_id UNIQUE
    constraint — a same-request race with another in-flight write) is
    caught and treated as the same idempotent success: re-fetch by
    client_bet_id and build the outcome from that row. This is only safe
    because place_or_replace_bet is called here with conn=None, so its own
    transaction has already rolled back by the time the exception reaches
    us — see the warning on db.place_or_replace_bet before reusing this
    pattern anywhere a shared conn is involved.
    """
    existing = db.fetch_bet_by_client_bet_id(client_bet_id)
    if existing is not None:
        return _outcome_from_row(existing, idempotent=True)

    guest = db.fetch_guest_by_id(guest_id)
    if guest is None:
        raise GuestNotFoundError(f"guest {guest_id} not found")
    if guest["claimed_at"] is None:
        raise GuestNotLoggedInError(f"guest {guest_id} has not logged in")

    race = db.fetch_race(race_number)
    if race is None:
        raise RaceNotFoundError(f"race {race_number} not found")
    if race["status"] != "OPEN":
        raise BettingClosedError(
            f"race {race_number} is not open (status={race['status']})"
        )
    if race["auto_lock_at"] is not None and _parse(race["auto_lock_at"]) <= now:
        raise BettingClosedError(f"race {race_number} auto-lock has passed")

    entry = _entries_by_horse(race_number).get(horse_number)
    if entry is None or entry["scratched"]:
        raise HorseNotInRaceError(
            f"horse {horse_number} is not a running entry in race {race_number}"
        )

    try:
        result = db.place_or_replace_bet(
            race_number, guest_id, horse_number, client_bet_id, _iso(now)
        )
    except sqlite3.IntegrityError:
        existing = db.fetch_bet_by_client_bet_id(client_bet_id)
        return _outcome_from_row(existing, idempotent=True)

    return BetOutcome(
        bet_id=result.bet_id,
        horse_number=horse_number,
        replaced=result.replaced,
        idempotent=False,
    )


def get_live_bet(guest_id: int, race_number: int) -> int | None:
    row = db.fetch_live_bet(race_number, guest_id)
    return row["horse_number"] if row is not None else None


def operator_set_bet(
    guest_id: int,
    race_number: int,
    horse_number: int,
    actor: str,
    now: datetime,
) -> BetOutcome:
    """The fix-a-bet path. Different rules from place_bet, deliberately not
    unified with it:

    - Permitted while the race is OPEN or LOCKED; rejected on SCHEDULED and
      SETTLED (BettingClosedError). Ignores auto_lock_at entirely.
    - Still validates the horse is entered in this race and not scratched
      (HorseNotInRaceError) — a bet on a horse that never ran can't score.
    - Does NOT require the guest to be logged in (no GuestNotLoggedInError
      check) — only that the guest exists (GuestNotFoundError). This is the
      paper-fallback path: the operator keys in bets after a network outage
      for guests who may never have claimed a device. If the guest's
      claimed_at is currently NULL, it is set to now in the same
      transaction as the bet and audit writes (claimed_at means
      "participating" — this is what makes the guest appear on the
      leaderboard at all). An existing claimed_at is never overwritten.
      device_token is left untouched (stays NULL), so the guest can still
      claim a phone later.

    Generates its own client_bet_id and writes an audit_log row
    ("bet.operator_set") in the same transaction as the bet write.

    Does NOT catch sqlite3.IntegrityError anywhere in this function: unlike
    place_bet, the bet write here shares a caller-owned conn with the
    claimed_at update and the audit row. Once any statement inside that
    transaction fails, SQLite has already aborted the whole transaction —
    there is no catch-and-still-commit recovery. Any IntegrityError must
    propagate out of the `with db.transaction()` block so the bet, the
    claimed_at update, and the audit row all roll back together.
    """
    guest = db.fetch_guest_by_id(guest_id)
    if guest is None:
        raise GuestNotFoundError(f"guest {guest_id} not found")

    race = db.fetch_race(race_number)
    if race is None:
        raise RaceNotFoundError(f"race {race_number} not found")
    if race["status"] not in ("OPEN", "LOCKED"):
        raise BettingClosedError(
            f"race {race_number} is not open or locked (status={race['status']})"
        )

    entry = _entries_by_horse(race_number).get(horse_number)
    if entry is None or entry["scratched"]:
        raise HorseNotInRaceError(
            f"horse {horse_number} is not a running entry in race {race_number}"
        )

    client_bet_id = str(uuid.uuid4())
    now_iso = _iso(now)
    with db.transaction() as conn:
        if guest["claimed_at"] is None:
            db.set_guest_claimed_at(guest_id, now_iso, conn=conn)
        result = db.place_or_replace_bet(
            race_number, guest_id, horse_number, client_bet_id, now_iso, conn=conn
        )
        db.append_audit_log(
            now_iso,
            actor,
            "bet.operator_set",
            json.dumps(
                {
                    "race_number": race_number,
                    "guest_id": guest_id,
                    "horse_number": horse_number,
                }
            ),
            conn=conn,
        )

    return BetOutcome(
        bet_id=result.bet_id,
        horse_number=horse_number,
        replaced=result.replaced,
        idempotent=False,
    )
