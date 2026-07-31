import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.services import bets, races


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    return path


@pytest.fixture
def initialised_db(db_path):
    db.run_migrations()
    db.initialise_event(horse_count=6, total_races=3)
    return db_path


def _add_guest(username: str, display_name: str, *, logged_in: bool = False) -> int:
    guest_id = db.insert_guest(username, display_name, _now().isoformat())
    if logged_in:
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE guest SET claimed_at = ? WHERE id = ?",
                (_now().isoformat(), guest_id),
            )
            conn.commit()
    return guest_id


def _audit_rows():
    with db.get_connection() as conn:
        return conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()


def _bet_count():
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM bet").fetchone()["n"]


# --- place_bet: happy path and validation order --------------------------------


def test_place_bet_succeeds_for_logged_in_guest_on_open_race(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    outcome = bets.place_bet(guest_id, 1, 3, _uid(), _now())
    assert outcome.horse_number == 3
    assert outcome.idempotent is False
    assert bets.get_live_bet(guest_id, 1) == 3


def test_place_bet_raises_guest_not_found(initialised_db):
    races.open_race(1, _now())
    with pytest.raises(bets.GuestNotFoundError):
        bets.place_bet(999, 1, 1, _uid(), _now())


def test_place_bet_raises_guest_not_logged_in(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=False)
    with pytest.raises(bets.GuestNotLoggedInError):
        bets.place_bet(guest_id, 1, 1, _uid(), _now())


def test_place_bet_raises_race_not_found_for_nonexistent_race(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(races.RaceNotFoundError):
        bets.place_bet(guest_id, 999, 1, _uid(), _now())


def test_place_bet_raises_betting_closed_when_scheduled(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.BettingClosedError):
        bets.place_bet(guest_id, 1, 1, _uid(), _now())


def test_place_bet_raises_betting_closed_when_locked(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.BettingClosedError):
        bets.place_bet(guest_id, 1, 1, _uid(), _now())


def test_place_bet_raises_betting_closed_when_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.BettingClosedError):
        bets.place_bet(guest_id, 1, 1, _uid(), _now())


def test_place_bet_raises_betting_closed_when_auto_lock_at_has_passed_even_though_status_still_open(
    initialised_db,
):
    now = _now()
    races.open_race(1, now, auto_lock_seconds=30)
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    # apply_auto_lock has NOT run — status is still OPEN in the DB.
    assert db.fetch_race(1)["status"] == "OPEN"
    with pytest.raises(bets.BettingClosedError):
        bets.place_bet(guest_id, 1, 1, _uid(), now + timedelta(seconds=31))


def test_place_bet_raises_horse_not_in_race_for_unentered_horse(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.HorseNotInRaceError):
        bets.place_bet(guest_id, 1, 999, _uid(), _now())


def test_place_bet_raises_horse_not_in_race_for_scratched_horse(initialised_db):
    races.set_scratched(1, 2, True, _now())
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.HorseNotInRaceError):
        bets.place_bet(guest_id, 1, 2, _uid(), _now())


# --- place_bet: replace semantics -----------------------------------------


def test_place_bet_first_bet_reports_replaced_false(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    outcome = bets.place_bet(guest_id, 1, 1, _uid(), _now())
    assert outcome.replaced is False


def test_place_bet_replaces_existing_live_bet_and_reports_replaced_true(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    bets.place_bet(guest_id, 1, 1, _uid(), _now())
    outcome = bets.place_bet(guest_id, 1, 2, _uid(), _now())
    assert outcome.replaced is True


def test_place_bet_changing_bet_leaves_exactly_one_live_bet(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    bets.place_bet(guest_id, 1, 1, _uid(), _now())
    bets.place_bet(guest_id, 1, 2, _uid(), _now())
    live = [b for b in db.get_live_bets() if b.guest_id == guest_id]
    assert len(live) == 1
    assert live[0].horse_number == 2


# --- place_bet: idempotency ------------------------------------------------


def test_place_bet_repeated_client_bet_id_returns_same_bet_and_is_idempotent(
    initialised_db,
):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    cbid = _uid()
    first = bets.place_bet(guest_id, 1, 1, cbid, _now())
    second = bets.place_bet(guest_id, 1, 1, cbid, _now())
    assert second.idempotent is True
    assert second.bet_id == first.bet_id
    assert second.horse_number == 1


def test_place_bet_repeated_client_bet_id_writes_no_new_row(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    cbid = _uid()
    bets.place_bet(guest_id, 1, 1, cbid, _now())
    bets.place_bet(guest_id, 1, 1, cbid, _now())
    assert _bet_count() == 1


def test_place_bet_idempotent_outcome_bet_id_matches_original_row_not_a_new_one(
    initialised_db,
):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    cbid = _uid()
    first_outcome = bets.place_bet(guest_id, 1, 2, cbid, _now())
    retry_outcome = bets.place_bet(guest_id, 1, 2, cbid, _now())
    assert retry_outcome.bet_id == first_outcome.bet_id
    assert _bet_count() == 1


def test_place_bet_retrying_stale_client_bet_id_returns_stale_horse_not_current_live_horse(
    initialised_db,
):
    """Guest taps #4 (slow request), then taps #6 before the retry of #4
    arrives — distinct client_bet_ids, so both legitimately write and #6 is
    correctly live. The retry of #4's original request must return #4 (what
    actually got recorded under that client_bet_id), not #6 (the current
    live horse) — echoing #6 would tell the guest they're confirmed on a
    horse the database doesn't have them on.
    """
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    cbid_old = _uid()
    bets.place_bet(guest_id, 1, 4, cbid_old, _now())
    cbid_new = _uid()
    bets.place_bet(guest_id, 1, 6, cbid_new, _now())

    retry_outcome = bets.place_bet(guest_id, 1, 6, cbid_old, _now())
    assert retry_outcome.idempotent is True
    assert retry_outcome.horse_number == 4
    assert bets.get_live_bet(guest_id, 1) == 6


def test_place_bet_integrity_error_toctou_race_treated_as_idempotent_success(
    initialised_db, monkeypatch
):
    """Simulates the pre-check missing a write that lands concurrently: the
    pre-check (db.fetch_bet_by_client_bet_id) is forced to return None once
    even though a bet with that client_bet_id already exists, so place_bet
    proceeds to its own write, hits the client_bet_id UNIQUE constraint, and
    must recover via the IntegrityError catch rather than raising or
    duplicating.
    """
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    client_bet_id = _uid()
    # A "concurrent" write that already landed under this client_bet_id.
    db.insert_bet(1, guest_id, 3, client_bet_id, _now().isoformat())

    real_fetch = db.fetch_bet_by_client_bet_id
    call_count = {"n": 0}

    def flaky_fetch(cbid):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_fetch(cbid)

    monkeypatch.setattr(bets.db, "fetch_bet_by_client_bet_id", flaky_fetch)

    outcome = bets.place_bet(guest_id, 1, 5, client_bet_id, _now())
    assert outcome.idempotent is True
    assert outcome.horse_number == 3  # the row that actually landed, not 5
    assert _bet_count() == 1
    assert bets.get_live_bet(guest_id, 1) == 3


# --- get_live_bet -----------------------------------------------------------


def test_get_live_bet_returns_none_when_no_bet(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    assert bets.get_live_bet(guest_id, 1) is None


def test_get_live_bet_returns_current_horse(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    bets.place_bet(guest_id, 1, 5, _uid(), _now())
    assert bets.get_live_bet(guest_id, 1) == 5


# --- operator_set_bet --------------------------------------------------------


def test_operator_set_bet_succeeds_on_locked_race(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    outcome = bets.operator_set_bet(guest_id, 1, 3, "operator", _now())
    assert outcome.horse_number == 3
    assert bets.get_live_bet(guest_id, 1) == 3


def test_operator_set_bet_succeeds_on_open_race_past_auto_lock(initialised_db):
    now = _now()
    races.open_race(1, now, auto_lock_seconds=10)
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    later = now + timedelta(seconds=20)
    assert db.fetch_race(1)["status"] == "OPEN"  # apply_auto_lock has not run
    outcome = bets.operator_set_bet(guest_id, 1, 3, "operator", later)
    assert outcome.horse_number == 3


def test_operator_set_bet_rejected_on_settled_race(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.BettingClosedError):
        bets.operator_set_bet(guest_id, 1, 4, "operator", _now())


def test_operator_set_bet_rejected_on_scheduled_race(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.BettingClosedError):
        bets.operator_set_bet(guest_id, 1, 1, "operator", _now())


def test_operator_set_bet_raises_horse_not_in_race_for_unentered_horse(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.HorseNotInRaceError):
        bets.operator_set_bet(guest_id, 1, 999, "operator", _now())


def test_operator_set_bet_raises_horse_not_in_race_for_scratched_horse(initialised_db):
    races.set_scratched(1, 2, True, _now())
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(bets.HorseNotInRaceError):
        bets.operator_set_bet(guest_id, 1, 2, "operator", _now())


def test_operator_set_bet_does_not_require_guest_logged_in(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=False)
    outcome = bets.operator_set_bet(guest_id, 1, 1, "operator", _now())
    assert outcome.horse_number == 1


def test_operator_set_bet_claims_guest_when_claimed_at_null(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=False)
    assert db.fetch_guest_by_id(guest_id)["claimed_at"] is None
    bets.operator_set_bet(guest_id, 1, 1, "operator", _now())
    assert db.fetch_guest_by_id(guest_id)["claimed_at"] is not None


def test_operator_set_bet_does_not_overwrite_existing_claimed_at(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    original_claimed_at = db.fetch_guest_by_id(guest_id)["claimed_at"]
    bets.operator_set_bet(guest_id, 1, 1, "operator", _now())
    assert db.fetch_guest_by_id(guest_id)["claimed_at"] == original_claimed_at


def test_operator_set_bet_entered_guest_appears_on_leaderboard(initialised_db):
    from app.services.scoring import build_leaderboard

    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=False)
    bets.operator_set_bet(guest_id, 1, 1, "operator", _now())

    guests = db.get_guests()
    logged_in = db.get_logged_in_guest_ids()
    assert guest_id in logged_in

    leaderboard = build_leaderboard(
        guests, logged_in, db.get_live_bets(), db.get_settled_results()
    )
    assert any(row.guest_id == guest_id for row in leaderboard.rows)


def test_operator_set_bet_writes_audit_row_atomically_with_bet(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    before = len(_audit_rows())
    bets.operator_set_bet(guest_id, 1, 1, "operator", _now())
    after = _audit_rows()
    assert len(after) == before + 1
    assert after[-1]["action"] == "bet.operator_set"
    assert after[-1]["actor"] == "operator"


def test_operator_set_bet_generates_own_client_bet_id(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    outcome = bets.operator_set_bet(guest_id, 1, 1, "operator", _now())
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT client_bet_id FROM bet WHERE id = ?", (outcome.bet_id,)
        ).fetchone()
    assert row["client_bet_id"]
    uuid.UUID(row["client_bet_id"])  # raises ValueError if not a valid UUID


def test_operator_set_bet_forced_failure_leaves_no_bet_no_claim_no_audit(
    initialised_db,
):
    """Forces a sqlite3.IntegrityError inside operator_set_bet's shared
    transaction by passing actor=None (audit_log.actor is NOT NULL) and
    asserts the bet write, the claimed_at update, and the audit row all
    roll back together — the composed-primitive rollback guarantee required
    for db.place_or_replace_bet(conn=...).
    """
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=False)
    audit_before = len(_audit_rows())
    bet_before = _bet_count()

    with pytest.raises(sqlite3.IntegrityError):
        bets.operator_set_bet(guest_id, 1, 1, None, _now())

    assert db.fetch_guest_by_id(guest_id)["claimed_at"] is None
    assert len(_audit_rows()) == audit_before
    assert _bet_count() == bet_before


# --- naive datetime rejection -------------------------------------------------

_NAIVE = datetime(2026, 1, 1, 12, 0, 0)


def test_place_bet_rejects_naive_datetime(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(ValueError):
        bets.place_bet(guest_id, 1, 1, _uid(), _NAIVE)


def test_operator_set_bet_rejects_naive_datetime(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    with pytest.raises(ValueError):
        bets.operator_set_bet(guest_id, 1, 1, "operator", _NAIVE)
