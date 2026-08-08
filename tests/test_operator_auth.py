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
    """The failure counter is module-global state — reset it around every
    test in this file so tests never leak into each other regardless of
    execution order.
    """
    auth._operator_login_failures = 0
    yield
    auth._operator_login_failures = 0


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Never actually sleep in tests: patch time.sleep where auth.py looks
    it up, so the 2s post-lockout delay doesn't cost real seconds per test.
    """
    monkeypatch.setattr(auth.time, "sleep", lambda seconds: None)


def _login_operator(client, password: str = "hunter2"):
    return client.post("/operator/login", data={"password": password})


# --- login --------------------------------------------------------------


def test_operator_login_wrong_password_shows_error(client):
    response = client.post("/operator/login", data={"password": "wrong"})

    assert response.status_code == 200
    assert "Incorrect password" in response.text


def test_operator_login_correct_password_sets_cookie_and_redirects(client):
    response = client.post(
        "/operator/login", data={"password": "hunter2"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/operator"
    assert auth.OPERATOR_COOKIE_NAME in response.cookies


# --- cross-auth isolation ------------------------------------------------


def test_operator_cookie_never_authenticates_guest_route(client):
    _login_operator(client)

    response = client.get("/bet", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_guest_cookie_never_authenticates_operator_route(client):
    db.insert_guest("jdoe", "Jane Doe", _now_iso())
    client.post("/login", data={"username": "jdoe"})

    response = client.get("/operator", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/operator/login"


def test_operator_route_redirects_to_operator_login_when_unauthenticated(client):
    response = client.get("/operator", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/operator/login"


# --- startup / cookie shape -----------------------------------------------


def test_operator_login_missing_operator_password_env_fails_startup(
    app_env, monkeypatch
):
    monkeypatch.delenv("OPERATOR_PASSWORD", raising=False)
    from app.main import app

    with pytest.raises(Exception):
        with TestClient(app):
            pass


def test_operator_cookie_max_age_is_24_hours(client):
    response = client.post(
        "/operator/login", data={"password": "hunter2"}, follow_redirects=False
    )

    set_cookie_headers = [
        v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
    ]
    assert any("Max-Age=86400" in h for h in set_cookie_headers)


def test_operator_session_invalidated_when_password_rotated(client, monkeypatch):
    _login_operator(client)
    assert client.get("/operator").status_code == 200

    monkeypatch.setenv("OPERATOR_PASSWORD", "different-password")

    response = client.get("/operator", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/operator/login"


# --- rate limiting ---------------------------------------------------------


def test_operator_rate_limit_engages_after_five_failures(client):
    for _ in range(5):
        client.post("/operator/login", data={"password": "wrong"})

    assert auth.operator_login_delay_seconds() == 2


def test_correct_password_always_succeeds_even_after_five_failures(client):
    for _ in range(5):
        client.post("/operator/login", data={"password": "wrong"})

    response = client.post(
        "/operator/login", data={"password": "hunter2"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/operator"


def test_operator_rate_limit_is_global_not_per_client(client):
    for _ in range(5):
        client.post("/operator/login", data={"password": "wrong"})

    # A single module-level int, not a per-IP dict — documents the
    # deliberate choice not to pretend per-client isolation exists behind
    # a proxy that collapses every device to one address.
    assert isinstance(auth._operator_login_failures, int)
    assert auth._operator_login_failures == 5


def test_operator_login_success_resets_failure_count(client):
    for _ in range(3):
        client.post("/operator/login", data={"password": "wrong"})
    assert auth._operator_login_failures == 3

    client.post("/operator/login", data={"password": "hunter2"})

    assert auth._operator_login_failures == 0
