"""Data-access layer: raw SQL over stdlib sqlite3, no ORM, no business logic.

Connections are opened per-operation via get_connection() and never shared
across threads. The one behaviour that matters most in this file: an empty,
missing, or un-migrated database must make verify_ready() raise, never get
silently bootstrapped. See the design doc's account of last year's failure.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.services.scoring import Bet, GuestInfo, RaceResult

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class DatabasePathNotConfiguredError(Exception):
    """DATABASE_PATH is unset or empty."""


class DatabaseNotReadyError(Exception):
    """Base class for verify_ready() failures."""


class DatabaseMissingError(DatabaseNotReadyError):
    """The database file does not exist."""


class MigrationsPendingError(DatabaseNotReadyError):
    """Migrations have not been applied."""


class EventNotInitialisedError(DatabaseNotReadyError):
    """The database is migrated but initialise_event() has never run."""


class EventAlreadyInitialisedError(Exception):
    """initialise_event() was called on an already-initialised database."""


def _database_path() -> str:
    path = os.environ.get("DATABASE_PATH")
    if not path:
        raise DatabasePathNotConfiguredError(
            "DATABASE_PATH environment variable is not set"
        )
    return path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # journal_mode returns the resulting mode as a row even when setting it.
    conn.execute("PRAGMA journal_mode=WAL").fetchall()
    conn.execute("PRAGMA foreign_keys=ON").fetchall()
    conn.execute("PRAGMA busy_timeout=5000").fetchall()
    conn.execute("PRAGMA synchronous=FULL").fetchall()


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_database_path())
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Composes multiple write primitives into one atomic unit of work.

    Pass the yielded connection as the `conn` kwarg to write primitives so
    they use it instead of opening their own. Commits on clean exit, rolls
    back on any exception raised inside the block, always closes.
    """
    conn = sqlite3.connect(_database_path())
    try:
        _apply_pragmas(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_migrations(migrations_dir: Path | None = None) -> None:
    """Applies any *.sql file in migrations_dir not yet recorded in
    schema_migrations, in filename order. Re-running is a no-op.

    Owns the schema_migrations bookkeeping table itself (creating it if
    absent) rather than relying on a migration file to create it, since a
    migration can't record its own completion before it runs.

    Each migration is applied as a single script containing its own BEGIN/
    COMMIT, on a connection opened with isolation_level=None (autocommit),
    so the migration's DDL and its schema_migrations row land atomically —
    either both or neither. A malformed migration leaves the database
    exactly as it was and records nothing.
    """
    directory = migrations_dir or MIGRATIONS_DIR
    conn = sqlite3.connect(_database_path(), isolation_level=None)
    try:
        _apply_pragmas(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row["filename"]
            for row in conn.execute("SELECT filename FROM schema_migrations")
        }
        for migration_file in sorted(directory.glob("*.sql")):
            if migration_file.name in applied:
                continue
            sql = migration_file.read_text(encoding="utf-8")
            script = (
                "BEGIN;\n"
                f"{sql}\n"
                "INSERT INTO schema_migrations (filename, applied_at) VALUES "
                f"('{_sql_quote(migration_file.name)}', "
                f"'{_sql_quote(_utc_now_iso())}');\n"
                "COMMIT;\n"
            )
            try:
                conn.executescript(script)
            except sqlite3.DatabaseError:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
    finally:
        conn.close()


def verify_ready() -> None:
    """Called at app startup. Never creates, migrates, or seeds anything.

    Raises DatabaseMissingError, MigrationsPendingError, or
    EventNotInitialisedError. Returns None if the database is ready to
    serve.
    """
    path = Path(_database_path())
    if not path.exists():
        raise DatabaseMissingError(f"database file not found at {path}")

    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations"
            ).fetchone()["n"]
        except sqlite3.OperationalError as exc:
            raise MigrationsPendingError("migrations have not been applied") from exc
        if count == 0:
            raise MigrationsPendingError("migrations have not been applied")

        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'event_initialised'"
        ).fetchone()
        if row is None or row["value"] != "true":
            raise EventNotInitialisedError("event has not been initialised")
    finally:
        conn.close()


def initialise_event(horse_count: int, total_races: int) -> None:
    """The deliberate, explicit, hand-run bootstrap. Assumes migrations have
    already been applied. Raises EventAlreadyInitialisedError if called
    twice.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'event_initialised'"
        ).fetchone()
        if row is not None and row["value"] == "true":
            raise EventAlreadyInitialisedError("event has already been initialised")
        conn.executemany(
            "INSERT INTO horse (number, name) VALUES (?, NULL)",
            [(n,) for n in range(1, horse_count + 1)],
        )
        conn.executemany(
            "INSERT INTO race (number, status) VALUES (?, 'SCHEDULED')",
            [(n,) for n in range(1, total_races + 1)],
        )
        conn.executemany(
            "INSERT INTO race_entry (race_number, horse_number) VALUES (?, ?)",
            [
                (race_n, horse_n)
                for race_n in range(1, total_races + 1)
                for horse_n in range(1, horse_count + 1)
            ],
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('total_races', ?)",
            (str(total_races),),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('event_initialised', 'true')"
        )


# --- reads that feed the Step 1 pure modules directly -----------------------


def get_guests() -> list[GuestInfo]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, display_name FROM guest").fetchall()
    return [
        GuestInfo(guest_id=row["id"], display_name=row["display_name"])
        for row in rows
    ]


def get_logged_in_guest_ids() -> set[int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM guest WHERE claimed_at IS NOT NULL"
        ).fetchall()
    return {row["id"] for row in rows}


def get_settled_results() -> dict[int, RaceResult]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT number, first, second, third FROM race WHERE status = 'SETTLED'"
        ).fetchall()
    return {
        row["number"]: RaceResult(
            race_number=row["number"],
            first=row["first"],
            second=row["second"],
            third=row["third"],
        )
        for row in rows
    }


def get_live_bets() -> list[Bet]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT guest_id, race_number, horse_number FROM bet "
            "WHERE superseded_at IS NULL"
        ).fetchall()
    return [
        Bet(
            guest_id=row["guest_id"],
            race_number=row["race_number"],
            horse_number=row["horse_number"],
        )
        for row in rows
    ]


# --- primitives for Step 3 ---------------------------------------------------


def fetch_guest_by_id(guest_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM guest WHERE id = ?", (guest_id,)
        ).fetchone()


def fetch_guest_by_username(username: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM guest WHERE username = ?", (username,)
        ).fetchone()


def fetch_guest_by_device_token(device_token: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM guest WHERE device_token = ?", (device_token,)
        ).fetchone()


def insert_guest(
    username: str,
    display_name: str,
    created_at: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    if conn is None:
        with get_connection() as c:
            result = insert_guest(username, display_name, created_at, conn=c)
            c.commit()
            return result
    cur = conn.execute(
        "INSERT INTO guest (username, display_name, created_at) VALUES (?, ?, ?)",
        (username, display_name, created_at),
    )
    return cur.lastrowid


def get_horses() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM horse ORDER BY number").fetchall()


def fetch_race(race_number: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM race WHERE number = ?", (race_number,)
        ).fetchone()


def get_race_entries(race_number: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM race_entry WHERE race_number = ? ORDER BY horse_number",
            (race_number,),
        ).fetchall()


def insert_bet(
    race_number: int,
    guest_id: int,
    horse_number: int,
    client_bet_id: str,
    created_at: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Low-level primitive: inserts a bet row with no supersede handling and
    no idempotency check. Guest-facing bet writes must go through
    place_or_replace_bet instead — this exists for test setup and bulk
    seeding only.
    """
    if conn is None:
        with get_connection() as c:
            result = insert_bet(
                race_number, guest_id, horse_number, client_bet_id, created_at, conn=c
            )
            c.commit()
            return result
    cur = conn.execute(
        "INSERT INTO bet "
        "(race_number, guest_id, horse_number, client_bet_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (race_number, guest_id, horse_number, client_bet_id, created_at),
    )
    return cur.lastrowid


@dataclass(frozen=True)
class BetWriteResult:
    bet_id: int
    replaced: bool


def place_or_replace_bet(
    race_number: int,
    guest_id: int,
    horse_number: int,
    client_bet_id: str,
    created_at: str,
) -> BetWriteResult:
    """Supersedes any live bet for (race_number, guest_id) and inserts the
    replacement, atomically. No validation beyond what the schema enforces —
    race status, horse-in-race, and idempotency checks are Step 3's
    bets.py's job; db.py only guarantees this happens atomically.
    """
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE bet SET superseded_at = ? "
            "WHERE race_number = ? AND guest_id = ? AND superseded_at IS NULL",
            (created_at, race_number, guest_id),
        )
        replaced = cur.rowcount > 0
        bet_id = insert_bet(
            race_number, guest_id, horse_number, client_bet_id, created_at, conn=conn
        )
        return BetWriteResult(bet_id=bet_id, replaced=replaced)


def fetch_bet_by_client_bet_id(client_bet_id: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM bet WHERE client_bet_id = ?", (client_bet_id,)
        ).fetchone()


def append_audit_log(
    at: str,
    actor: str,
    action: str,
    payload_json: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    if conn is None:
        with get_connection() as c:
            result = append_audit_log(at, actor, action, payload_json, conn=c)
            c.commit()
            return result
    cur = conn.execute(
        "INSERT INTO audit_log (at, actor, action, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (at, actor, action, payload_json),
    )
    return cur.lastrowid


def get_meta(key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_meta(key: str, value: str, *, conn: sqlite3.Connection | None = None) -> None:
    if conn is None:
        with get_connection() as c:
            set_meta(key, value, conn=c)
            c.commit()
            return
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
