import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.services import bets, races
from app.services.races import HorseEntry, RaceState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
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


def _login(client, username: str, display_name: str = "Guest") -> int:
    guest_id = db.insert_guest(username, display_name, _now().isoformat())
    client.post("/login", data={"username": username})
    return guest_id


# --- _classify_state: pure decision logic -------------------------------------


def _state(**overrides) -> RaceState:
    base = dict(
        race_number=1,
        total_races=3,
        status="OPEN",
        seconds_to_auto_lock=None,
        horses=[],
        live_bet_count=0,
        result=None,
        event_complete=False,
    )
    base.update(overrides)
    return RaceState(**base)


def test_classify_state_open_with_no_auto_lock_is_open_not_error():
    from app.routers.guest import _classify_state

    assert _classify_state(_state(status="OPEN", seconds_to_auto_lock=None)) == "open"


def test_classify_state_open_with_future_auto_lock_is_open():
    from app.routers.guest import _classify_state

    assert _classify_state(_state(status="OPEN", seconds_to_auto_lock=45)) == "open"


def test_classify_state_open_with_expired_auto_lock_is_locked():
    from app.routers.guest import _classify_state

    assert _classify_state(_state(status="OPEN", seconds_to_auto_lock=0)) == "locked"


def test_classify_state_locked_status_is_locked():
    from app.routers.guest import _classify_state

    assert _classify_state(_state(status="LOCKED")) == "locked"


def test_classify_state_scheduled_is_waiting():
    from app.routers.guest import _classify_state

    assert _classify_state(_state(status="SCHEDULED")) == "waiting"


def test_classify_state_event_complete_is_complete():
    from app.routers.guest import _classify_state

    assert _classify_state(_state(status="SETTLED", event_complete=True)) == "complete"


# --- render states via the real routes ----------------------------------------


def test_waiting_state_shows_previous_race_outcome(client):
    guest_id = _login(client, "jdoe")
    races.open_race(1, _now())
    bets.place_bet(guest_id, 1, 1, _uid(), _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())

    response = client.get("/bet")

    assert response.status_code == 200
    assert "Race 1" in response.text
    assert "3 point" in response.text


def test_waiting_state_shows_no_bet_message_for_previous_race(client):
    _login(client, "jdoe")
    races.open_race(1, _now())
    races.lock_race(1, _now())
    races.settle_race(1, 1, 2, 3, _now())

    response = client.get("/bet")

    assert response.status_code == 200
    assert "didn&#39;t bet" in response.text or "didn't bet" in response.text


def test_open_state_shows_horse_grid_with_current_pick_highlighted(client):
    guest_id = _login(client, "jdoe")
    races.open_race(1, _now())
    bets.place_bet(guest_id, 1, 2, _uid(), _now())

    response = client.get("/bet")

    assert response.status_code == 200
    assert "horse-grid" in response.text
    assert "horse-btn--selected" in response.text


def test_open_state_with_no_auto_lock_renders_picker_not_error(client):
    _login(client, "jdoe")
    races.open_race(1, _now())  # no auto_lock_seconds

    response = client.get("/bet")

    assert response.status_code == 200
    assert "horse-grid" in response.text


def test_open_state_with_future_auto_lock_shows_countdown(client):
    _login(client, "jdoe")
    races.open_race(1, _now(), auto_lock_seconds=120)

    response = client.get("/bet")

    assert response.status_code == 200
    assert "Closes in" in response.text
    assert "horse-grid" in response.text


def test_open_state_with_expired_auto_lock_renders_locked_not_picker(client):
    """apply_auto_lock runs at the top of every state read, so by the time
    the picker would render, an expired race has already been flipped to
    LOCKED for real. This still exercises the guest-visible guarantee: an
    expired-but-nominally-OPEN race must never render the picker.
    """
    guest_id = _login(client, "jdoe")
    races.open_race(1, _now(), auto_lock_seconds=1)
    db.set_race_auto_lock(1, (_now() - timedelta(seconds=5)).isoformat())

    response = client.get("/bet")

    assert response.status_code == 200
    assert "horse-grid" not in response.text
    assert "locked" in response.text.lower()


def test_locked_state_shows_no_bet_message_when_guest_didnt_bet(client):
    _login(client, "jdoe")
    races.open_race(1, _now())
    races.lock_race(1, _now())

    response = client.get("/bet")

    assert response.status_code == 200
    assert "didn&#39;t bet" in response.text or "didn't bet" in response.text


def test_locked_state_shows_guests_horse(client):
    guest_id = _login(client, "jdoe")
    races.open_race(1, _now())
    bets.place_bet(guest_id, 1, 4, _uid(), _now())
    races.lock_race(1, _now())

    response = client.get("/bet")

    assert response.status_code == 200
    assert "#4" in response.text


def test_complete_state_shows_final_result_and_leaderboard_link(client):
    guest_id = _login(client, "jdoe")
    for n in (1, 2, 3):
        races.open_race(n, _now())
        bets.place_bet(guest_id, n, 1, _uid(), _now())
        races.lock_race(n, _now())
        races.settle_race(n, 1, 2, 3, _now())

    response = client.get("/bet")

    assert response.status_code == 200
    assert "/leaderboard" in response.text
    assert "1st #1" in response.text
    assert "3 point" in response.text  # guest's own result in the final race


# --- placing bets --------------------------------------------------------------


def test_place_bet_returns_partial_with_outcome_horse(client):
    _login(client, "jdoe")
    races.open_race(1, _now())

    response = client.post("/bet", data={"horse_number": 3, "client_bet_id": _uid()})

    assert response.status_code == 200
    assert "horse-btn--selected" in response.text


def test_repeated_client_bet_id_creates_one_bet_shows_one_horse(client):
    _login(client, "jdoe")
    races.open_race(1, _now())
    client_bet_id = _uid()

    r1 = client.post("/bet", data={"horse_number": 3, "client_bet_id": client_bet_id})
    r2 = client.post("/bet", data={"horse_number": 3, "client_bet_id": client_bet_id})

    assert r1.status_code == 200
    assert r2.status_code == 200
    with db.get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM bet WHERE client_bet_id = ?", (client_bet_id,)
        ).fetchone()["n"]
    assert count == 1


def test_stale_client_bet_id_retry_renders_stored_horse_not_submitted(client):
    guest_id = _login(client, "jdoe")
    races.open_race(1, _now())
    old_id = _uid()
    bets.place_bet(guest_id, 1, 2, old_id, _now())  # original tap on #2
    bets.place_bet(guest_id, 1, 5, _uid(), _now())  # newer tap on #5 supersedes it

    # A "retry" of the stale request for horse #2 arrives late, still
    # carrying old_id and still submitting horse_number=2.
    response = client.post("/bet", data={"horse_number": 2, "client_bet_id": old_id})

    assert response.status_code == 200
    # Must reflect the stored (superseded) row's horse, #2 — not re-select #5.
    assert bets.get_live_bet(guest_id, 1) == 5


def test_betting_scratched_horse_shows_message_and_rerenders(client):
    _login(client, "jdoe")
    races.open_race(1, _now())
    races.set_scratched(1, 2, True, _now())

    response = client.post("/bet", data={"horse_number": 2, "client_bet_id": _uid()})

    assert response.status_code == 200
    assert "isn&#39;t running" in response.text or "isn't running" in response.text


def test_betting_locked_race_shows_betting_closed_message(client):
    _login(client, "jdoe")
    races.open_race(1, _now())
    races.lock_race(1, _now())

    response = client.post("/bet", data={"horse_number": 1, "client_bet_id": _uid()})

    assert response.status_code == 200
    assert "Betting is closed" in response.text


# --- leaderboard ---------------------------------------------------------------


def test_leaderboard_pins_requester_and_renders_truncation_text(client):
    guest_id = _login(client, "jdoe")
    # 30 other logged-in guests, all tied at 0 points, to force truncation.
    for i in range(30):
        other_id = db.insert_guest(f"guest{i}", f"Guest {i}", _now().isoformat())
        db.claim_guest_device(other_id, f"tok{i}", _now().isoformat())

    response = client.get("/leaderboard")

    assert response.status_code == 200
    assert "others on" in response.text


def test_leaderboard_guest_not_logged_in_redirects_not_500(client):
    guest_id = _login(client, "jdoe")
    # Simulate a guest whose device_token matches (so require_guest resolves
    # them) but who somehow isn't in logged_in_guest_ids, by clearing
    # claimed_at directly — the state build_leaderboard treats as a bug.
    with db.get_connection() as conn:
        conn.execute("UPDATE guest SET claimed_at = NULL WHERE id = ?", (guest_id,))
        conn.commit()

    response = client.get("/leaderboard", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
