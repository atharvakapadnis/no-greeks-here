import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth, db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("OPERATOR_PASSWORD", "test-operator-password")
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


def _add_guest(username: str, display_name: str = "Guest") -> int:
    return db.insert_guest(username, display_name, _now_iso())


# --- login / claim -----------------------------------------------------------


def test_login_claims_unclaimed_guest_sets_cookie_and_claimed_at(client):
    _add_guest("jdoe", "Jane Doe")

    response = client.post("/login", data={"username": "jdoe"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/bet"
    assert auth.COOKIE_NAME in response.cookies

    guest = db.fetch_guest_by_username("jdoe")
    assert guest["device_token"] is not None
    assert guest["claimed_at"] is not None


def test_login_already_claimed_from_different_device_is_refused(client):
    guest_id = _add_guest("jdoe", "Jane Doe")
    db.claim_guest_device(guest_id, "someone-elses-token", _now_iso())

    response = client.post("/login", data={"username": "jdoe"})

    assert response.status_code == 200
    assert "already claimed" in response.text


def test_login_concurrent_claim_exactly_one_succeeds(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe")
    results: list[bool] = []
    lock = threading.Lock()

    def attempt(token: str) -> None:
        claimed = db.claim_guest_device(guest_id, token, _now_iso())
        with lock:
            results.append(claimed)

    threads = [
        threading.Thread(target=attempt, args=(f"token-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]
    guest = db.fetch_guest_by_id(guest_id)
    assert guest["device_token"] in ("token-0", "token-1")


def test_login_unknown_username_shows_organiser_message(client):
    response = client.post("/login", data={"username": "nobody"})

    assert response.status_code == 200
    assert "have that name, see the organiser" in response.text


# --- cookie / redirect behaviour ----------------------------------------------


def test_root_redirects_to_bet_when_claimed_with_valid_cookie(client):
    _add_guest("jdoe", "Jane Doe")
    client.post("/login", data={"username": "jdoe"})

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/bet"


def test_root_redirects_to_login_with_no_cookie(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_protected_route_redirects_to_login_with_tampered_cookie(client):
    client.cookies.set(auth.COOKIE_NAME, "not-a-real-signed-value")

    response = client.get("/bet", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_protected_route_redirects_to_login_when_token_no_longer_exists(client):
    signed = auth.sign_device_token("a-token-nobody-has")
    client.cookies.set(auth.COOKIE_NAME, signed)

    response = client.get("/bet", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_protected_route_never_500s_on_bad_cookie(client):
    client.cookies.set(auth.COOKIE_NAME, "garbage")

    response = client.get("/state", follow_redirects=False)

    assert response.status_code < 500


# --- startup ------------------------------------------------------------------


def test_startup_fails_loudly_when_verify_ready_raises(app_env):
    """No migrations, no initialise_event — verify_ready() must raise, and
    that failure must surface from entering the TestClient's lifespan, not
    get swallowed into a silently-bootstrapped empty database.
    """
    from app.main import app

    with pytest.raises(Exception) as exc_info:
        with TestClient(app):
            pass

    assert "database" in str(exc_info.value).lower() or "migrat" in str(exc_info.value).lower()
