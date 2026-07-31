import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.services import races


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


def _place_bet(race_number: int, guest_id: int, horse_number: int):
    return db.place_or_replace_bet(
        race_number, guest_id, horse_number, _uid(), _now().isoformat()
    )


def _audit_rows():
    with db.get_connection() as conn:
        return conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()


# --- open_race ----------------------------------------------------------------


def test_open_race_transitions_scheduled_to_open(initialised_db):
    now = _now()
    races.open_race(1, now)
    race = db.fetch_race(1)
    assert race["status"] == "OPEN"
    assert race["opened_at"] == now.isoformat()


def test_open_race_sets_auto_lock_at_when_given_seconds(initialised_db):
    now = _now()
    races.open_race(1, now, auto_lock_seconds=60)
    race = db.fetch_race(1)
    assert race["auto_lock_at"] == (now + timedelta(seconds=60)).isoformat()


def test_open_race_leaves_auto_lock_at_null_when_not_given(initialised_db):
    races.open_race(1, _now())
    assert db.fetch_race(1)["auto_lock_at"] is None


def test_open_race_is_idempotent_when_already_open(initialised_db):
    races.open_race(1, _now())
    before = len(_audit_rows())
    races.open_race(1, _now())
    assert len(_audit_rows()) == before
    assert db.fetch_race(1)["status"] == "OPEN"


def test_open_race_raises_another_race_open_error(initialised_db):
    races.open_race(1, _now())
    with pytest.raises(races.AnotherRaceOpenError):
        races.open_race(2, _now())


def test_open_race_raises_illegal_transition_from_locked(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.open_race(1, _now())


def test_open_race_raises_illegal_transition_from_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.open_race(1, _now())


def test_open_race_raises_race_not_found(initialised_db):
    with pytest.raises(races.RaceNotFoundError):
        races.open_race(999, _now())


# --- lock_race ------------------------------------------------------------


def test_lock_race_transitions_open_to_locked(initialised_db):
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    race = db.fetch_race(1)
    assert race["status"] == "LOCKED"
    assert race["locked_at"] == now.isoformat()


def test_lock_race_clears_auto_lock_at(initialised_db):
    races.open_race(1, _now(), auto_lock_seconds=60)
    races.lock_race(1, _now())
    assert db.fetch_race(1)["auto_lock_at"] is None


def test_lock_race_is_idempotent_when_already_locked(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    before = len(_audit_rows())
    races.lock_race(1, _now())
    assert len(_audit_rows()) == before


def test_lock_race_raises_illegal_transition_from_scheduled(initialised_db):
    with pytest.raises(races.IllegalTransitionError):
        races.lock_race(1, _now())


def test_lock_race_raises_illegal_transition_from_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.lock_race(1, _now())


# --- settle_race ------------------------------------------------------------


def test_settle_race_transitions_locked_to_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    race = db.fetch_race(1)
    assert race["status"] == "SETTLED"
    assert (race["first"], race["second"], race["third"]) == (1, 2, 3)


def test_settle_race_raises_illegal_transition_when_not_locked(initialised_db):
    with pytest.raises(races.IllegalTransitionError):
        races.settle_race(1, 1, 2, 3, _now())


def test_settle_race_raises_illegal_transition_when_already_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.settle_race(1, 2, 3, 4, _now())


def test_settle_race_raises_invalid_result_on_duplicate_placing(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    with pytest.raises(races.InvalidResultError):
        races.settle_race(1, 1, 1, 2, _now())


def test_settle_race_raises_invalid_result_on_horse_not_entered(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    with pytest.raises(races.InvalidResultError):
        races.settle_race(1, 1, 2, 999, _now())


def test_settle_race_raises_invalid_result_on_scratched_horse(initialised_db):
    races.set_scratched(1, 3, True, _now())
    races.open_race(1, _now())
    races.lock_race(1, _now())
    with pytest.raises(races.InvalidResultError):
        races.settle_race(1, 1, 2, 3, _now())


# --- reopen_race ------------------------------------------------------------


def test_reopen_race_transitions_locked_to_open(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.reopen_race(1, _now())
    assert db.fetch_race(1)["status"] == "OPEN"


def test_reopen_race_clears_locked_at(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.reopen_race(1, _now())
    assert db.fetch_race(1)["locked_at"] is None


def test_reopen_race_raises_another_race_open_error(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.open_race(2, _now())
    with pytest.raises(races.AnotherRaceOpenError):
        races.reopen_race(1, _now())


def test_reopen_race_raises_illegal_transition_from_scheduled(initialised_db):
    with pytest.raises(races.IllegalTransitionError):
        races.reopen_race(1, _now())


def test_reopen_race_raises_illegal_transition_from_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.reopen_race(1, _now())


# --- correct_result -----------------------------------------------------------


def test_correct_result_updates_placings_on_settled_race(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    races.correct_result(1, 3, 2, 1, _now())
    race = db.fetch_race(1)
    assert (race["first"], race["second"], race["third"]) == (3, 2, 1)


def test_correct_result_writes_audit_row(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    before = len(_audit_rows())
    races.correct_result(1, 3, 2, 1, _now())
    after = _audit_rows()
    assert len(after) == before + 1
    assert after[-1]["action"] == "race.result_corrected"


def test_correct_result_raises_illegal_transition_when_not_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.correct_result(1, 1, 2, 3, _now())


def test_correct_result_raises_invalid_result_on_duplicate_placing(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    with pytest.raises(races.InvalidResultError):
        races.correct_result(1, 1, 1, 2, _now())


# --- set_scratched ------------------------------------------------------------


def test_set_scratched_marks_horse_scratched_while_scheduled(initialised_db):
    races.set_scratched(1, 1, True, _now())
    entries = {e["horse_number"]: e for e in db.get_race_entries(1)}
    assert bool(entries[1]["scratched"]) is True


def test_set_scratched_unmarks_horse(initialised_db):
    races.set_scratched(1, 1, True, _now())
    races.set_scratched(1, 1, False, _now())
    entries = {e["horse_number"]: e for e in db.get_race_entries(1)}
    assert bool(entries[1]["scratched"]) is False


def test_set_scratched_is_noop_and_writes_no_audit_when_unchanged(initialised_db):
    before = len(_audit_rows())
    races.set_scratched(1, 1, False, _now())  # already not scratched
    assert len(_audit_rows()) == before


def test_set_scratched_raises_invalid_result_for_horse_not_entered(initialised_db):
    with pytest.raises(races.InvalidResultError):
        races.set_scratched(1, 999, True, _now())


def test_set_scratched_permitted_while_open(initialised_db):
    races.open_race(1, _now())
    races.set_scratched(1, 1, True, _now())  # must not raise
    entries = {e["horse_number"]: e for e in db.get_race_entries(1)}
    assert bool(entries[1]["scratched"]) is True


def test_set_scratched_rejected_once_locked(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.set_scratched(1, 1, True, _now())


def test_set_scratched_rejected_once_settled(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    with pytest.raises(races.IllegalTransitionError):
        races.set_scratched(1, 4, True, _now())


def test_set_scratched_open_race_voids_live_bets_on_that_horse(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    _place_bet(1, guest_id, 1)
    races.set_scratched(1, 1, True, _now())
    assert db.fetch_live_bet(1, guest_id) is None


def test_set_scratched_open_race_leaves_bets_on_other_horses_untouched(initialised_db):
    races.open_race(1, _now())
    g1 = _add_guest("jdoe", "Jane Doe", logged_in=True)
    g2 = _add_guest("bsmith", "Bob Smith", logged_in=True)
    _place_bet(1, g1, 1)
    _place_bet(1, g2, 2)
    races.set_scratched(1, 1, True, _now())
    assert db.fetch_live_bet(1, g1) is None
    assert db.fetch_live_bet(1, g2)["horse_number"] == 2


def test_set_scratched_voided_guest_has_no_live_bet_and_scores_zero(initialised_db):
    from app.services.scoring import Bet, total_points_by_guest

    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    _place_bet(1, guest_id, 1)
    races.set_scratched(1, 1, True, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 2, 3, 4, _now())

    bets = [
        Bet(guest_id=b["guest_id"], race_number=b["race_number"], horse_number=b["horse_number"])
        for b in db.get_live_bets()
    ]
    results = db.get_settled_results()
    totals = total_points_by_guest(bets, results)
    assert totals.get(guest_id, 0) == 0


def test_set_scratched_audit_payload_lists_voided_guest_ids(initialised_db):
    races.open_race(1, _now())
    g1 = _add_guest("jdoe", "Jane Doe", logged_in=True)
    g2 = _add_guest("bsmith", "Bob Smith", logged_in=True)
    _place_bet(1, g1, 1)
    _place_bet(1, g2, 1)
    races.set_scratched(1, 1, True, _now())
    audit = _audit_rows()[-1]
    payload = json.loads(audit["payload_json"])
    assert set(payload["voided_guest_ids"]) == {g1, g2}


def test_set_scratched_writes_exactly_one_audit_row_regardless_of_voided_count(
    initialised_db,
):
    races.open_race(1, _now())
    g1 = _add_guest("jdoe", "Jane Doe", logged_in=True)
    g2 = _add_guest("bsmith", "Bob Smith", logged_in=True)
    _place_bet(1, g1, 1)
    _place_bet(1, g2, 1)
    before = len(_audit_rows())
    races.set_scratched(1, 1, True, _now())
    assert len(_audit_rows()) == before + 1


def test_set_scratched_unscratching_does_not_restore_voided_bets(initialised_db):
    races.open_race(1, _now())
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    _place_bet(1, guest_id, 1)
    races.set_scratched(1, 1, True, _now())
    races.set_scratched(1, 1, False, _now())
    assert db.fetch_live_bet(1, guest_id) is None


# --- audit-row bookkeeping across all transitions ------------------------------


def _legal_transition_action(kind, now):
    if kind == "open":
        return lambda: races.open_race(1, _now())
    if kind == "lock":
        races.open_race(1, now)
        return lambda: races.lock_race(1, _now())
    if kind == "settle":
        races.open_race(1, now)
        races.lock_race(1, _now())
        return lambda: races.settle_race(1, 1, 2, 3, _now())
    if kind == "reopen":
        races.open_race(1, now)
        races.lock_race(1, _now())
        return lambda: races.reopen_race(1, _now())
    if kind == "correct":
        races.open_race(1, now)
        races.lock_race(1, _now())
        races.settle_race(1, 1, 2, 3, _now())
        return lambda: races.correct_result(1, 2, 1, 3, _now())
    if kind == "scratch":
        return lambda: races.set_scratched(1, 1, True, _now())
    raise ValueError(kind)


@pytest.mark.parametrize(
    "kind", ["open", "lock", "settle", "reopen", "correct", "scratch"]
)
def test_every_legal_transition_writes_exactly_one_audit_row(initialised_db, kind):
    action = _legal_transition_action(kind, _now())
    before = len(_audit_rows())
    action()
    assert len(_audit_rows()) == before + 1


def _illegal_transition_action(case_name):
    if case_name == "open_from_locked":
        races.open_race(1, _now())
        races.lock_race(1, _now())
        return lambda: races.open_race(1, _now())
    if case_name == "open_from_settled":
        races.open_race(1, _now())
        races.lock_race(1, _now())
        races.settle_race(1, 1, 2, 3, _now())
        return lambda: races.open_race(1, _now())
    if case_name == "open_another_race_open":
        races.open_race(1, _now())
        return lambda: races.open_race(2, _now())
    if case_name == "lock_from_scheduled":
        return lambda: races.lock_race(1, _now())
    if case_name == "settle_not_locked":
        return lambda: races.settle_race(1, 1, 2, 3, _now())
    if case_name == "settle_duplicate_placing":
        races.open_race(1, _now())
        races.lock_race(1, _now())
        return lambda: races.settle_race(1, 1, 1, 2, _now())
    if case_name == "reopen_from_scheduled":
        return lambda: races.reopen_race(1, _now())
    if case_name == "correct_not_settled":
        return lambda: races.correct_result(1, 1, 2, 3, _now())
    if case_name == "scratch_when_locked":
        races.open_race(1, _now())
        races.lock_race(1, _now())
        return lambda: races.set_scratched(1, 1, True, _now())
    raise ValueError(case_name)


@pytest.mark.parametrize(
    "case_name",
    [
        "open_from_locked",
        "open_from_settled",
        "open_another_race_open",
        "lock_from_scheduled",
        "settle_not_locked",
        "settle_duplicate_placing",
        "reopen_from_scheduled",
        "correct_not_settled",
        "scratch_when_locked",
    ],
)
def test_every_illegal_transition_writes_no_audit_row(initialised_db, case_name):
    action = _illegal_transition_action(case_name)
    before = len(_audit_rows())
    with pytest.raises(races.RaceError):
        action()
    assert len(_audit_rows()) == before


def test_idempotent_noop_writes_no_audit_row(initialised_db):
    races.open_race(1, _now())
    before = len(_audit_rows())
    races.open_race(1, _now())  # already OPEN
    assert len(_audit_rows()) == before

    races.lock_race(1, _now())
    before = len(_audit_rows())
    races.lock_race(1, _now())  # already LOCKED
    assert len(_audit_rows()) == before

    races.reopen_race(1, _now())
    before = len(_audit_rows())
    races.reopen_race(1, _now())  # already OPEN
    assert len(_audit_rows()) == before


# --- current_race_number --------------------------------------------------


def test_current_race_number_skips_settled_races(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    assert races.current_race_number() == 2


def test_current_race_number_returns_none_when_all_settled(initialised_db):
    for n in (1, 2, 3):
        races.open_race(n, _now())
        races.lock_race(n, _now())
        races.settle_race(n, 1, 2, 3, _now())
    assert races.current_race_number() is None


# --- apply_auto_lock -----------------------------------------------------------


def test_apply_auto_lock_locks_expired_race(initialised_db):
    now = _now()
    races.open_race(1, now, auto_lock_seconds=30)
    locked_number = races.apply_auto_lock(now + timedelta(seconds=31))
    assert locked_number == 1
    assert db.fetch_race(1)["status"] == "LOCKED"


def test_apply_auto_lock_is_noop_before_expiry(initialised_db):
    now = _now()
    races.open_race(1, now, auto_lock_seconds=300)
    result = races.apply_auto_lock(now + timedelta(seconds=5))
    assert result is None
    assert db.fetch_race(1)["status"] == "OPEN"


def test_apply_auto_lock_is_noop_when_auto_lock_at_is_null(initialised_db):
    races.open_race(1, _now())
    assert races.apply_auto_lock(_now()) is None


def test_apply_auto_lock_is_noop_when_no_race_open(initialised_db):
    assert races.apply_auto_lock(_now()) is None


def test_apply_auto_lock_writes_audit_row_with_system_actor(initialised_db):
    now = _now()
    races.open_race(1, now, auto_lock_seconds=1)
    races.apply_auto_lock(now + timedelta(seconds=2))
    audit = _audit_rows()[-1]
    assert audit["action"] == "race.auto_locked"
    assert audit["actor"] == "system"


# --- current_state --------------------------------------------------------


def test_current_state_in_scheduled_status(initialised_db):
    state = races.current_state(_now())
    assert state.race_number == 1
    assert state.status == "SCHEDULED"
    assert state.event_complete is False
    assert state.result is None
    assert state.seconds_to_auto_lock is None
    assert len(state.horses) == 6


def test_current_state_in_open_status_includes_seconds_to_auto_lock(initialised_db):
    now = _now()
    races.open_race(1, now, auto_lock_seconds=60)
    state = races.current_state(now + timedelta(seconds=10))
    assert state.status == "OPEN"
    assert state.seconds_to_auto_lock == 50


def test_current_state_in_locked_status(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    state = races.current_state(_now())
    assert state.status == "LOCKED"
    assert state.seconds_to_auto_lock is None


def test_current_state_horses_reflect_scratched_flags(initialised_db):
    races.set_scratched(1, 2, True, _now())
    state = races.current_state(_now())
    scratched_numbers = {h.number for h in state.horses if h.scratched}
    assert scratched_numbers == {2}


def test_current_state_live_bet_count(initialised_db):
    races.open_race(1, _now())
    g1 = _add_guest("jdoe", "Jane Doe", logged_in=True)
    g2 = _add_guest("bsmith", "Bob Smith", logged_in=True)
    _place_bet(1, g1, 1)
    _place_bet(1, g2, 2)
    state = races.current_state(_now())
    assert state.live_bet_count == 2


def test_current_state_when_all_races_settled_shows_final_result_and_event_complete_true(
    initialised_db,
):
    for n in (1, 2, 3):
        races.open_race(n, _now())
        races.lock_race(n, _now())
        races.settle_race(n, 1, 2, 3, _now())
    state = races.current_state(_now())
    assert state.event_complete is True
    assert state.race_number == 3
    assert state.status == "SETTLED"
    assert state.result is not None
    assert (state.result.first, state.result.second, state.result.third) == (1, 2, 3)


def test_current_state_event_complete_false_while_races_remain(initialised_db):
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())
    state = races.current_state(_now())
    assert state.event_complete is False
