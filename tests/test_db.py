import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from app import db
from app.services.scoring import build_leaderboard


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    return path


@pytest.fixture
def migrated_db(db_path):
    db.run_migrations()
    return db_path


@pytest.fixture
def initialised_db(migrated_db):
    db.initialise_event(horse_count=6, total_races=3)
    return migrated_db


def _add_guest(username: str, display_name: str, *, logged_in: bool = False) -> int:
    guest_id = db.insert_guest(username, display_name, _now())
    if logged_in:
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE guest SET claimed_at = ? WHERE id = ?", (_now(), guest_id)
            )
            conn.commit()
    return guest_id


def _set_race_status(race_number: int, status: str, **placings) -> None:
    with db.get_connection() as conn:
        columns = ", ".join(f"{col} = ?" for col in placings)
        params = list(placings.values()) + [status, race_number]
        sql = "UPDATE race SET "
        sql += (columns + ", ") if columns else ""
        sql += "status = ? WHERE number = ?"
        conn.execute(sql, params)
        conn.commit()


# --- migrations --------------------------------------------------------------


def test_migrations_apply_cleanly(migrated_db):
    with db.get_connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        applied = conn.execute("SELECT filename FROM schema_migrations").fetchall()

    assert {
        "guest",
        "horse",
        "race",
        "race_entry",
        "bet",
        "audit_log",
        "meta",
        "schema_migrations",
    } <= tables
    assert [row["filename"] for row in applied] == ["001_initial.sql"]


def test_rerunning_migrations_is_a_noop(migrated_db):
    db.run_migrations()
    with db.get_connection() as conn:
        applied = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    assert [row["filename"] for row in applied] == ["001_initial.sql"]


def test_malformed_migration_is_fully_rolled_back(db_path, tmp_path):
    bad_dir = tmp_path / "bad_migrations"
    bad_dir.mkdir()
    (bad_dir / "001_bad.sql").write_text(
        "CREATE TABLE ok (id INTEGER);\nTHIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.DatabaseError):
        db.run_migrations(migrations_dir=bad_dir)

    with db.get_connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        applied = conn.execute("SELECT * FROM schema_migrations").fetchall()

    assert "ok" not in tables
    assert applied == []


# --- pragmas -------------------------------------------------------------


def test_wal_mode_is_enabled(migrated_db):
    with db.get_connection() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_are_enabled(migrated_db):
    with db.get_connection() as conn:
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert enabled == 1


def test_get_connection_raises_without_database_path(monkeypatch):
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    with pytest.raises(db.DatabasePathNotConfiguredError):
        with db.get_connection():
            pass


# --- verify_ready --------------------------------------------------------


def test_verify_ready_raises_on_missing_database_file(db_path):
    with pytest.raises(db.DatabaseMissingError):
        db.verify_ready()


def test_verify_ready_does_not_create_database_file(db_path):
    with pytest.raises(db.DatabaseMissingError):
        db.verify_ready()
    assert not db_path.exists()


def test_verify_ready_raises_on_unmigrated_database(db_path):
    sqlite3.connect(str(db_path)).close()
    with pytest.raises(db.MigrationsPendingError):
        db.verify_ready()


def test_verify_ready_raises_on_migrated_but_uninitialised_database(migrated_db):
    with pytest.raises(db.EventNotInitialisedError):
        db.verify_ready()


def test_verify_ready_passes_after_initialise_event(initialised_db):
    db.verify_ready()  # must not raise


# --- initialise_event ------------------------------------------------------


def test_initialise_event_seeds_horses_races_and_entries(initialised_db):
    with db.get_connection() as conn:
        horse_count = conn.execute("SELECT COUNT(*) AS n FROM horse").fetchone()["n"]
        race_count = conn.execute("SELECT COUNT(*) AS n FROM race").fetchone()["n"]
        entry_count = conn.execute(
            "SELECT COUNT(*) AS n FROM race_entry"
        ).fetchone()["n"]

    assert horse_count == 6
    assert race_count == 3
    assert entry_count == 18
    assert db.get_meta("total_races") == "3"
    assert db.get_meta("event_initialised") == "true"


def test_initialise_event_raises_if_called_twice(initialised_db):
    with pytest.raises(db.EventAlreadyInitialisedError):
        db.initialise_event(horse_count=6, total_races=3)


# --- schema constraints ----------------------------------------------------


def test_only_one_open_race_allowed(initialised_db):
    _set_race_status(1, "OPEN")
    with pytest.raises(sqlite3.IntegrityError):
        _set_race_status(2, "OPEN")


def test_settling_race_with_null_placing_rejected(initialised_db):
    with pytest.raises(sqlite3.IntegrityError):
        _set_race_status(1, "SETTLED", first=1)


def test_settling_race_with_duplicate_horse_in_two_positions_rejected(initialised_db):
    with pytest.raises(sqlite3.IntegrityError):
        _set_race_status(1, "SETTLED", first=1, second=1, third=2)


def test_only_one_live_bet_per_guest_per_race_allowed(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    db.insert_bet(1, guest_id, 1, _uid(), _now())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_bet(1, guest_id, 2, _uid(), _now())


def test_place_or_replace_bet_supersedes_the_old_live_bet(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    first_bet_id = db.insert_bet(1, guest_id, 1, _uid(), _now())

    result = db.place_or_replace_bet(1, guest_id, 2, _uid(), _now())
    assert result.replaced is True

    with db.get_connection() as conn:
        first_bet = conn.execute(
            "SELECT superseded_at FROM bet WHERE id = ?", (first_bet_id,)
        ).fetchone()
    assert first_bet["superseded_at"] is not None

    live = {(b.race_number, b.horse_number) for b in db.get_live_bets()}
    assert (1, 2) in live
    assert (1, 1) not in live


def test_place_or_replace_bet_called_twice_leaves_one_live_and_one_superseded(
    initialised_db,
):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    first_result = db.place_or_replace_bet(1, guest_id, 1, _uid(), _now())
    assert first_result.replaced is False
    second_result = db.place_or_replace_bet(1, guest_id, 2, _uid(), _now())
    assert second_result.replaced is True

    with db.get_connection() as conn:
        rows = {
            row["id"]: row["superseded_at"]
            for row in conn.execute("SELECT id, superseded_at FROM bet").fetchall()
        }
    assert rows[first_result.bet_id] is not None
    assert rows[second_result.bet_id] is None


def test_place_or_replace_bet_with_no_existing_bet_inserts_only(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)

    result = db.place_or_replace_bet(1, guest_id, 4, _uid(), _now())
    assert result.replaced is False

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT superseded_at FROM bet WHERE id = ?", (result.bet_id,)
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM bet").fetchone()["n"]
    assert row["superseded_at"] is None
    assert count == 1


def test_place_or_replace_bet_rolls_back_when_insert_fails(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    client_bet_id = _uid()
    original_id = db.place_or_replace_bet(1, guest_id, 1, client_bet_id, _now()).bet_id

    with pytest.raises(sqlite3.IntegrityError):
        db.place_or_replace_bet(1, guest_id, 2, client_bet_id, _now())

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT superseded_at FROM bet WHERE id = ?", (original_id,)
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM bet").fetchone()["n"]
    assert row["superseded_at"] is None  # original still live
    assert count == 1  # no orphan row
    assert len(db.get_live_bets()) == 1


def test_transaction_rolls_back_fully_on_exception(migrated_db):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            db.insert_guest("jdoe", "Jane Doe", _now(), conn=conn)
            raise RuntimeError("boom")

    with db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM guest").fetchone()["n"]
    assert count == 0


def test_transaction_commits_multiple_writes_together(migrated_db):
    with db.transaction() as conn:
        db.insert_guest("jdoe", "Jane Doe", _now(), conn=conn)
        db.append_audit_log(_now(), "system", "test_action", "{}", conn=conn)

    with db.get_connection() as conn:
        guest_count = conn.execute("SELECT COUNT(*) AS n FROM guest").fetchone()["n"]
        audit_count = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log"
        ).fetchone()["n"]
    assert guest_count == 1
    assert audit_count == 1


def test_transaction_rolls_back_multiple_writes_together(migrated_db):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            db.insert_guest("jdoe", "Jane Doe", _now(), conn=conn)
            db.append_audit_log(_now(), "system", "test_action", "{}", conn=conn)
            raise RuntimeError("boom")

    with db.get_connection() as conn:
        guest_count = conn.execute("SELECT COUNT(*) AS n FROM guest").fetchone()["n"]
        audit_count = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log"
        ).fetchone()["n"]
    assert guest_count == 0
    assert audit_count == 0


def test_duplicate_client_bet_id_rejected(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    client_bet_id = _uid()
    db.insert_bet(1, guest_id, 1, client_bet_id, _now())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_bet(2, guest_id, 1, client_bet_id, _now())


def test_foreign_key_violation_rejected(initialised_db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_bet(1, 999, 1, _uid(), _now())


# --- read functions ----------------------------------------------------------


def test_get_live_bets_excludes_superseded_bets(initialised_db):
    guest_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    db.insert_bet(1, guest_id, 1, _uid(), _now())
    db.place_or_replace_bet(1, guest_id, 3, _uid(), _now())

    live_bets = db.get_live_bets()
    assert [b.horse_number for b in live_bets] == [3]


def test_get_settled_results_excludes_non_settled_races(initialised_db):
    _set_race_status(1, "SETTLED", first=1, second=2, third=3)

    results = db.get_settled_results()

    assert set(results.keys()) == {1}
    assert results[1].first == 1
    assert results[1].second == 2
    assert results[1].third == 3


def test_db_reads_feed_build_leaderboard_directly(initialised_db):
    winner_id = _add_guest("jdoe", "Jane Doe", logged_in=True)
    loser_id = _add_guest("bsmith", "Bob Smith", logged_in=True)
    _add_guest("nobody", "No Body", logged_in=False)  # not logged in, excluded

    db.insert_bet(1, winner_id, 1, _uid(), _now())
    db.insert_bet(1, loser_id, 2, _uid(), _now())
    _set_race_status(1, "SETTLED", first=1, second=2, third=3)

    leaderboard = build_leaderboard(
        guests=db.get_guests(),
        logged_in_guest_ids=db.get_logged_in_guest_ids(),
        bets=db.get_live_bets(),
        results=db.get_settled_results(),
        requesting_guest_id=winner_id,
    )

    assert [row.guest_id for row in leaderboard.rows] == [winner_id, loser_id]
    assert leaderboard.rows[0].total_points == 3
    assert leaderboard.rows[1].total_points == 2
    assert leaderboard.requesting_guest.guest_id == winner_id
    assert leaderboard.requesting_guest.total_points == 3
