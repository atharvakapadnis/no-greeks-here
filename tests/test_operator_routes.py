"""Step 4b python pass: auth-independent behavior, _effective_state view
selection, action transitions, exception -> message mapping, guest
management, fix-a-bet, and export.

The template pass (per-view markup presence/absence assertions) is
deliberately NOT in this file yet — it follows in a later session once the
full per-view templates exist.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.routers import operator
from app.services import bets, races


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _contains(text: str, phrase: str) -> bool:
    """Jinja2 autoescapes apostrophes to &#39; — check both forms so
    assertions don't depend on that escaping detail."""
    return phrase in text or phrase.replace("'", "&#39;") in text


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("OPERATOR_PASSWORD", "hunter2")
    monkeypatch.setenv("ENV", "dev")
    return db_path


@pytest.fixture
def initialised_db(app_env):
    db.run_migrations()
    db.initialise_event(horse_count=6, total_races=3)
    return app_env


@pytest.fixture
def client(initialised_db):
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    auth.record_operator_login_success()
    yield
    auth.record_operator_login_success()


def _login_operator(client) -> None:
    client.post("/operator/login", data={"password": "hunter2"})


def _add_guest(username: str, display_name: str = "Guest", *, claimed: bool = True) -> int:
    guest_id = db.insert_guest(username, display_name, _now_iso())
    if claimed:
        db.claim_guest_device(guest_id, f"token-{username}", _now_iso())
    return guest_id


# --- _effective_state view selection ---------------------------------------


def test_effective_state_scheduled_race_one_no_predecessor(initialised_db):
    state, view = operator._effective_state(_now())

    assert view == "scheduled"
    assert state.race_number == 1


def test_effective_state_mid_event_gap_shows_previous_settled(initialised_db):
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    state, view = operator._effective_state(now)

    assert view == "settled"
    assert state.race_number == 1
    assert state.result.first == 1
    assert state.status == "SETTLED"


def test_effective_state_final_race_gap_not_offered_shows_complete(initialised_db):
    now = _now()
    for n in range(1, 4):  # initialised_db has 3 races
        races.open_race(n, now)
        races.lock_race(n, now)
        races.settle_race(n, 1, 2, 3, now)

    state, view = operator._effective_state(now)

    assert view == "complete"
    assert state.event_complete is True


def test_effective_state_open_past_auto_lock_resolves_locked(initialised_db):
    t0 = _now()
    races.open_race(1, t0, auto_lock_seconds=30)
    later = t0 + timedelta(seconds=31)

    state, view = operator._effective_state(later)

    assert view == "locked"


def test_effective_state_open_view(initialised_db):
    now = _now()
    races.open_race(1, now)

    _, view = operator._effective_state(now)

    assert view == "open"


def test_effective_state_locked_view(initialised_db):
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)

    _, view = operator._effective_state(now)

    assert view == "locked"


# --- action transitions + redirects -----------------------------------------


def test_open_race_from_scheduled_redirects_and_opens(client):
    _login_operator(client)

    response = client.post(
        "/operator/race/open",
        data={"race_number": 1, "auto_lock_seconds": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/operator"
    assert db.fetch_race(1)["status"] == "OPEN"


def test_open_race_with_auto_lock_seconds_sets_deadline(client):
    _login_operator(client)

    client.post(
        "/operator/race/open", data={"race_number": 1, "auto_lock_seconds": "60"}
    )

    assert db.fetch_race(1)["auto_lock_at"] is not None


def test_lock_race_redirects_and_locks(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})

    response = client.post(
        "/operator/race/lock", data={"race_number": 1}, follow_redirects=False
    )

    assert response.status_code == 303
    assert db.fetch_race(1)["status"] == "LOCKED"


def test_reopen_race_redirects_and_reopens(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/reopen", data={"race_number": 1}, follow_redirects=False
    )

    assert response.status_code == 303
    assert db.fetch_race(1)["status"] == "OPEN"


def test_settle_first_post_shows_confirm_does_not_settle(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 1, "second": 2, "third": 3},
    )

    assert response.status_code == 200
    assert "Confirm and publish" in response.text
    assert db.fetch_race(1)["status"] == "LOCKED"


def test_settle_confirm_post_settles_and_redirects(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 1, "second": 2, "third": 3, "confirmed": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    race = db.fetch_race(1)
    assert race["status"] == "SETTLED"
    assert race["first"] == 1


def test_correct_first_post_shows_confirm_does_not_correct(client):
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    response = client.post(
        "/operator/race/correct",
        data={"race_number": 1, "first": 3, "second": 2, "third": 1},
    )

    assert response.status_code == 200
    assert "Confirm and correct" in response.text
    assert db.fetch_race(1)["first"] == 1


def test_correct_confirm_post_corrects_and_redirects(client):
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    response = client.post(
        "/operator/race/correct",
        data={
            "race_number": 1,
            "first": 3,
            "second": 2,
            "third": 1,
            "confirmed": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert db.fetch_race(1)["first"] == 3


def test_correct_result_rejects_race_that_is_not_most_recently_settled(client):
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)
    races.open_race(2, now)
    races.lock_race(2, now)
    races.settle_race(2, 1, 2, 3, now)

    response = client.post(
        "/operator/race/correct",
        data={
            "race_number": 1,
            "first": 3,
            "second": 2,
            "third": 1,
            "confirmed": "1",
        },
        follow_redirects=True,
    )

    assert "board has moved on" in response.text.lower()
    assert db.fetch_race(1)["first"] == 1  # unchanged


def test_settle_confirm_page_rejected_if_race_moved_on_before_confirming(client):
    """The confirm page is a two-step flow with a gap in between where
    another tab/operator can act. Render the confirm page for race 1, then
    settle race 1 out of band (as if a second tab got there first) so the
    current race advances to race 2 — the original tab's confirmed=1 POST
    must be rejected as stale, not silently re-apply (or worse, error out
    trying to settle an already-SETTLED race).
    """
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)

    confirm_response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 1, "second": 2, "third": 3},
    )
    assert confirm_response.status_code == 200
    assert "Confirm and publish" in confirm_response.text

    # Out of band: race 1 gets settled through some other path before the
    # original tab's confirm click arrives.
    races.settle_race(1, 4, 5, 6, now)

    response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 1, "second": 2, "third": 3, "confirmed": "1"},
        follow_redirects=True,
    )

    assert "board has moved on" in response.text.lower()
    race1 = db.fetch_race(1)
    assert race1["first"] == 4  # the out-of-band result, not the stale confirm's


def test_correct_confirm_page_rejected_if_a_later_race_settles_first(client):
    """Same gap, for correct: render the correct confirm page for race 1,
    then settle race 2 out of band so max(settled) moves to 2 — the
    original tab's confirmed=1 POST for race 1 must be rejected as stale,
    not silently correct an old race after a newer one has already settled.
    """
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    confirm_response = client.post(
        "/operator/race/correct",
        data={"race_number": 1, "first": 3, "second": 2, "third": 1},
    )
    assert confirm_response.status_code == 200
    assert "Confirm and correct" in confirm_response.text

    # Out of band: race 2 settles before the original tab's confirm click
    # arrives, moving max(settled) past race 1.
    races.open_race(2, now)
    races.lock_race(2, now)
    races.settle_race(2, 4, 5, 6, now)

    response = client.post(
        "/operator/race/correct",
        data={"race_number": 1, "first": 3, "second": 2, "third": 1, "confirmed": "1"},
        follow_redirects=True,
    )

    assert "board has moved on" in response.text.lower()
    assert db.fetch_race(1)["first"] == 1  # unchanged


def test_scratch_horse_from_scheduled_marks_current_race(client):
    _login_operator(client)

    response = client.post(
        "/operator/race/scratch",
        data={"race_number": 1, "horse_number": 2, "scratched": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    entries = {e["horse_number"]: e for e in db.get_race_entries(1)}
    assert bool(entries[2]["scratched"]) is True


def test_scratch_horse_from_settled_view_marks_next_race_not_current(client):
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    response = client.post(
        "/operator/race/scratch",
        data={"race_number": 2, "horse_number": 4, "scratched": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    race2_entries = {e["horse_number"]: e for e in db.get_race_entries(2)}
    race1_entries = {e["horse_number"]: e for e in db.get_race_entries(1)}
    assert bool(race2_entries[4]["scratched"]) is True
    assert bool(race1_entries[4]["scratched"]) is False


def test_open_race_n_plus_1_from_settled_view_accepts_auto_lock(client):
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    response = client.post(
        "/operator/race/open",
        data={"race_number": 2, "auto_lock_seconds": "90"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    race2 = db.fetch_race(2)
    assert race2["status"] == "OPEN"
    assert race2["auto_lock_at"] is not None


def test_stale_race_number_rejected_with_refresh_message(client):
    _login_operator(client)

    response = client.post(
        "/operator/race/lock", data={"race_number": 5}, follow_redirects=True
    )

    assert "board has moved on" in response.text.lower()


# --- exception -> message mapping -------------------------------------------


def test_open_race_not_found_message(client, monkeypatch):
    _login_operator(client)

    def _raise(*args, **kwargs):
        raise races.RaceNotFoundError("gone")

    monkeypatch.setattr(races, "open_race", _raise)

    response = client.post(
        "/operator/race/open",
        data={"race_number": 1, "auto_lock_seconds": ""},
        follow_redirects=True,
    )

    assert "no longer exists" in response.text.lower()


def test_open_race_illegal_transition_message(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/open",
        data={"race_number": 1, "auto_lock_seconds": ""},
        follow_redirects=True,
    )

    assert _contains(response.text.lower(), "can't be opened")


def test_open_race_another_open_message(client, monkeypatch):
    # AnotherRaceOpenError is unreachable through this route in legitimate
    # use: race_number is always validated against current_state's target,
    # which is always the one race allowed to be "current" — so no other
    # OPEN race can exist to collide with. Monkeypatched as defense in
    # depth, mirroring guest.py's documented TOCTOU-only branches.
    _login_operator(client)

    def _raise(*args, **kwargs):
        raise races.AnotherRaceOpenError("busy")

    monkeypatch.setattr(races, "open_race", _raise)

    response = client.post(
        "/operator/race/open",
        data={"race_number": 1, "auto_lock_seconds": ""},
        follow_redirects=True,
    )

    assert "already open" in response.text.lower()


def test_lock_race_illegal_transition_message(client):
    _login_operator(client)

    response = client.post(
        "/operator/race/lock", data={"race_number": 1}, follow_redirects=True
    )

    assert _contains(response.text.lower(), "isn't open")


def test_reopen_race_illegal_transition_message(client):
    _login_operator(client)

    response = client.post(
        "/operator/race/reopen", data={"race_number": 1}, follow_redirects=True
    )

    assert _contains(response.text.lower(), "isn't locked")


def test_reopen_another_race_open_message(client, monkeypatch):
    _login_operator(client)

    def _raise(*args, **kwargs):
        raise races.AnotherRaceOpenError("busy")

    monkeypatch.setattr(races, "reopen_race", _raise)

    response = client.post(
        "/operator/race/reopen", data={"race_number": 1}, follow_redirects=True
    )

    assert "already open" in response.text.lower()


def test_settle_race_illegal_transition_message(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})

    response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 1, "second": 2, "third": 3, "confirmed": "1"},
        follow_redirects=True,
    )

    assert _contains(response.text.lower(), "isn't locked yet")


def test_settle_race_invalid_result_duplicate_placing_message(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 1, "second": 1, "third": 3, "confirmed": "1"},
        follow_redirects=True,
    )

    assert "three different horses" in response.text.lower()


def test_settle_race_invalid_result_unentered_horse_message(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 99, "second": 2, "third": 3, "confirmed": "1"},
        follow_redirects=True,
    )

    assert "three different horses" in response.text.lower()


def test_settle_race_invalid_result_scratched_horse_message(client):
    _login_operator(client)
    client.post(
        "/operator/race/scratch",
        data={"race_number": 1, "horse_number": 2, "scratched": "true"},
    )
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/settle",
        data={"race_number": 1, "first": 2, "second": 3, "third": 4, "confirmed": "1"},
        follow_redirects=True,
    )

    assert "three different horses" in response.text.lower()


def test_correct_result_illegal_transition_message(client, monkeypatch):
    # Also unreachable in legitimate use: the pre-check already requires
    # race_number == max(db.get_settled_results()), which by construction
    # is always SETTLED at that moment, and SETTLED never transitions away.
    # Monkeypatched as defense in depth only.
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    def _raise(*args, **kwargs):
        raise races.IllegalTransitionError("not settled")

    monkeypatch.setattr(races, "correct_result", _raise)

    response = client.post(
        "/operator/race/correct",
        data={"race_number": 1, "first": 3, "second": 2, "third": 1, "confirmed": "1"},
        follow_redirects=True,
    )

    assert _contains(response.text.lower(), "hasn't been settled")


def test_correct_result_invalid_result_message(client):
    _login_operator(client)
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    response = client.post(
        "/operator/race/correct",
        data={"race_number": 1, "first": 1, "second": 1, "third": 3, "confirmed": "1"},
        follow_redirects=True,
    )

    assert "three different horses" in response.text.lower()


def test_scratch_illegal_transition_when_locked_message(client):
    _login_operator(client)
    client.post("/operator/race/open", data={"race_number": 1, "auto_lock_seconds": ""})
    client.post("/operator/race/lock", data={"race_number": 1})

    response = client.post(
        "/operator/race/scratch",
        data={"race_number": 1, "horse_number": 2, "scratched": "true"},
        follow_redirects=True,
    )

    assert "locked or settled" in response.text.lower()


def test_scratch_invalid_horse_not_entered_message(client):
    _login_operator(client)

    response = client.post(
        "/operator/race/scratch",
        data={"race_number": 1, "horse_number": 99, "scratched": "true"},
        follow_redirects=True,
    )

    assert _contains(response.text.lower(), "isn't entered")


def test_bet_set_betting_closed_message(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe")

    response = client.post(
        "/operator/bet/set",
        data={"guest_id": guest_id, "horse_number": 1},
        follow_redirects=True,
    )

    assert "betting is closed" in response.text.lower()


def test_bet_set_horse_not_in_race_message(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe")
    races.open_race(1, _now())

    response = client.post(
        "/operator/bet/set",
        data={"guest_id": guest_id, "horse_number": 99},
        follow_redirects=True,
    )

    assert _contains(response.text.lower(), "isn't running")


def test_bet_set_guest_not_found_message(client):
    _login_operator(client)
    races.open_race(1, _now())

    response = client.post(
        "/operator/bet/set",
        data={"guest_id": 999, "horse_number": 1},
        follow_redirects=True,
    )

    assert _contains(response.text.lower(), "doesn't exist")


# --- guest management --------------------------------------------------


def test_unlock_guest_clears_device_token_preserves_claimed_at(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe", "Jane Doe", claimed=True)
    before = db.fetch_guest_by_id(guest_id)
    assert before["device_token"] is not None
    assert before["claimed_at"] is not None

    response = client.post(
        "/operator/guest/unlock", data={"guest_id": guest_id}, follow_redirects=False
    )

    assert response.status_code == 303
    after = db.fetch_guest_by_id(guest_id)
    assert after["device_token"] is None
    assert after["claimed_at"] == before["claimed_at"]


def test_unlock_unknown_guest_shows_message(client):
    _login_operator(client)

    response = client.post(
        "/operator/guest/unlock", data={"guest_id": 999}, follow_redirects=True
    )

    assert _contains(response.text.lower(), "doesn't exist")


def test_add_guest_generates_username_and_displays_large(client):
    _login_operator(client)

    response = client.post(
        "/operator/guest/add", data={"display_name": "Jane Doe"}, follow_redirects=True
    )

    assert "jdoe" in response.text
    guest = db.fetch_guest_by_username("jdoe")
    assert guest is not None
    assert guest["display_name"] == "Jane Doe"


def test_add_guest_forced_collision_extends_to_full_username(client):
    _login_operator(client)
    client.post("/operator/guest/add", data={"display_name": "Carolyn Campbell"})

    response = client.post(
        "/operator/guest/add",
        data={"display_name": "Chris Campbell"},
        follow_redirects=True,
    )

    assert "chriscampbell" in response.text
    assert db.fetch_guest_by_username("chriscampbell") is not None
    assert db.fetch_guest_by_username("ccampbell") is not None  # first guest unaffected


def test_add_guest_username_does_not_collide_with_unclaimed_imported_guest(client):
    _login_operator(client)
    # Simulates a pre-imported guest who never claimed a device — the
    # plus-one path must see this username as taken via db.fetch_guests()
    # (ALL guests), not just logged-in ones.
    db.insert_guest("jdoe", "John Doe", _now_iso())

    response = client.post(
        "/operator/guest/add", data={"display_name": "Jane Doe"}, follow_redirects=True
    )

    assert "janedoe" in response.text
    assert db.fetch_guest_by_username("janedoe") is not None


def test_add_guest_blank_name_shows_message(client):
    _login_operator(client)

    response = client.post(
        "/operator/guest/add", data={"display_name": "   "}, follow_redirects=True
    )

    assert "enter a name" in response.text.lower()


# --- fix a bet --------------------------------------------------------------


def test_operator_set_bet_works_while_open(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe", claimed=False)
    races.open_race(1, _now())

    response = client.post(
        "/operator/bet/set",
        data={"guest_id": guest_id, "horse_number": 2},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert bets.get_live_bet(guest_id, 1) == 2


def test_operator_set_bet_works_while_locked(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe")
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)

    response = client.post(
        "/operator/bet/set",
        data={"guest_id": guest_id, "horse_number": 3},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert bets.get_live_bet(guest_id, 1) == 3


def test_operator_set_bet_rejected_on_settled(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe")
    now = _now()
    races.open_race(1, now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    response = client.post(
        "/operator/bet/set",
        data={"guest_id": guest_id, "horse_number": 4},
        follow_redirects=True,
    )

    assert "closed" in response.text.lower()


def test_operator_set_bet_sets_claimed_at_if_null(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe", claimed=False)
    races.open_race(1, _now())

    client.post("/operator/bet/set", data={"guest_id": guest_id, "horse_number": 2})

    guest = db.fetch_guest_by_id(guest_id)
    assert guest["claimed_at"] is not None
    assert guest["device_token"] is None


def test_operator_set_bet_does_not_overwrite_existing_claimed_at(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe", claimed=True)
    original_claimed_at = db.fetch_guest_by_id(guest_id)["claimed_at"]
    races.open_race(1, _now())

    client.post("/operator/bet/set", data={"guest_id": guest_id, "horse_number": 2})

    assert db.fetch_guest_by_id(guest_id)["claimed_at"] == original_claimed_at


# --- who-hasn't-bet ----------------------------------------------------


def test_who_hasnt_bet_excludes_unclaimed_guests(client):
    _login_operator(client)
    _add_guest("jdoe", "Jane Doe", claimed=True)
    _add_guest("bsmith", "Bob Smith", claimed=False)
    races.open_race(1, _now())

    response = client.get("/operator")

    assert "Jane Doe" in response.text
    assert "Bob Smith" not in response.text


# --- export -----------------------------------------------------------------


def test_export_returns_standings_json(client):
    _login_operator(client)
    guest_id = _add_guest("jdoe")
    now = _now()
    races.open_race(1, now)
    bets.place_bet(guest_id, 1, 1, "cb-1", now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    response = client.get("/operator/export")

    assert response.status_code == 200
    data = response.json()
    assert any(
        row["guest_id"] == guest_id and row["total_points"] == 3
        for row in data["rows"]
    )


def test_export_requires_operator_auth(client):
    response = client.get("/operator/export", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/operator/login"


def test_export_content_disposition_is_attachment(client):
    _login_operator(client)

    response = client.get("/operator/export")

    assert "attachment" in response.headers.get("content-disposition", "")


# --- backup status -----------------------------------------------------


def test_backup_status_stub_returns_not_configured(client):
    _login_operator(client)

    response = client.get("/operator")

    assert "not configured" in response.text
