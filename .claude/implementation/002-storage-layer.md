# 002 — Storage layer

2026-07-31

## What was built and why

`app/migrations/001_initial.sql`, `app/db.py`, and `tests/test_db.py` — Step
2 of the design doc's build order. Raw SQL over stdlib `sqlite3`, no ORM, no
business logic. This is the layer beneath the pure Step 1 modules
(`app/services/guests.py`, `app/services/scoring.py`): it owns the schema
invariants SQLite can enforce on its own (one open race, one live bet per
guest per race, idempotent bets), and it hands rows to Step 1's dataclasses
with zero translation.

The single most important behaviour in this file, called out explicitly in
the design doc: last year's app silently re-initialised an empty database
after a container restart mid-event. `verify_ready()` exists specifically
to make that failure mode impossible — it raises loudly on a missing,
un-migrated, or uninitialised database and never creates, migrates, or
seeds anything as a side effect.

## Schema — `app/migrations/001_initial.sql`

Eight tables: `guest`, `horse`, `race`, `race_entry`, `bet`, `audit_log`,
`meta`. (`schema_migrations` is *not* created by this file — see below.)
All id/number columns are `INTEGER`, matching the Step 1 dataclass carry-
forward note.

Schema-level invariants enforced (not in Python):
- `race.status` CHECK constrained to `SCHEDULED` / `OPEN` / `LOCKED` /
  `SETTLED`.
- At most one `OPEN` race: partial unique index
  `idx_race_one_open ON race(status) WHERE status = 'OPEN'`.
- A `SETTLED` race must have all three placings filled in, and a race
  can't have the same horse in two positions — two `CHECK` constraints on
  `race`, added during plan review because `RaceResult` types
  first/second/third as plain `int`, so a `SETTLED` race with a `NULL`
  placing would only fail once it reached `scoring.py`, not at the write
  that caused it.
- At most one live bet per (race, guest): partial unique index
  `idx_bet_one_live_per_guest_race ON bet(race_number, guest_id) WHERE
  superseded_at IS NULL`. This is the invariant `total_points_by_guest`
  depends on — it sums every bet it's handed and cannot itself detect a
  duplicate.
- `bet.client_bet_id` UNIQUE (the idempotency key) and foreign keys on
  every reference except `superseded_at`, which is a plain nullable
  timestamp with no FK — see "The supersede ordering resolution" below for
  why.

## `schema_migrations` is owned by `db.py`, not by the migration file

Originally planned as a table created by `001_initial.sql`. Moved into
`run_migrations()` itself (`CREATE TABLE IF NOT EXISTS`, run before the
migrations directory is scanned) because a migration can't record its own
completion in a table it's the one creating — on a fresh database the very
first `run_migrations()` call would query a table that doesn't exist yet.

## Migration atomicity

Python's `sqlite3.Connection.executescript()` gives no transaction control
of its own, so "run the file, then insert the tracking row" is two
separate units of work — a crash between them would leave a schema change
with no record of it, and a second run would try to reapply it. Each
pending migration is instead applied as **one** `executescript()` call on a
connection opened with `isolation_level=None`, containing its own
`BEGIN` / (file contents) / `INSERT INTO schema_migrations ...` / `COMMIT`
as literal text. Either the whole migration and its tracking row land, or
(on any error, caught and turned into an explicit `ROLLBACK`) neither does.
Covered by `test_malformed_migration_is_fully_rolled_back`, which points
`run_migrations(migrations_dir=...)` at a deliberately broken `.sql` file
and asserts both that the partial `CREATE TABLE` in it never landed and
that no `schema_migrations` row was written.

`run_migrations()` takes an optional `migrations_dir` parameter for
exactly this reason — the real `app/migrations/` directory only ever holds
valid, already-applied-in-production files, so exercising rollback behavior
needs a throwaway directory instead.

## `app/db.py` — full public API

### Connections

```python
def get_connection() -> AbstractContextManager[sqlite3.Connection]
```
`@contextmanager`. One connection per operation, `row_factory = sqlite3.Row`,
pragmas applied every time: `journal_mode=WAL`, `foreign_keys=ON`,
`busy_timeout=5000`, `synchronous=FULL` (FULL rather than NORMAL — peak
load is ~4 writes/sec, so the fsync cost is irrelevant and it removes a
class of loss). Reads `DATABASE_PATH` from the environment on every call
(not cached), which is also what lets `verify_ready()` check file
existence before ever opening a connection and lets tests point at a fresh
temp file per test via `monkeypatch.setenv`.

Raises `DatabasePathNotConfiguredError` if `DATABASE_PATH` is unset or
empty — added during plan review so a missing env var fails with a named
exception instead of a bare `KeyError`.

### Transactions

```python
def transaction() -> AbstractContextManager[sqlite3.Connection]
```
`@contextmanager`. Same pragmas as `get_connection()`. Commits on clean
exit, rolls back on any exception raised inside the block, always closes.
Generalizes the inline open/try/commit/except-rollback pattern that used
to live only in `initialise_event` (now itself rewritten to use
`transaction()`). Every write primitive below accepts an optional
keyword-only `conn`: passing it makes the primitive use that connection
and not commit (the `transaction()` block owns the commit/rollback);
omitting it keeps the primitive's old standalone behaviour — open its own
connection via `get_connection()` and commit immediately. This is how
several writes are composed into one atomic unit, e.g.
`place_or_replace_bet` below.

### Migration runner

```python
def run_migrations(migrations_dir: Path | None = None) -> None
```
See atomicity note above. Re-running is a no-op.

### Fail-loud checks

```python
class DatabaseNotReadyError(Exception)        # base class
class DatabaseMissingError(DatabaseNotReadyError)
class MigrationsPendingError(DatabaseNotReadyError)
class EventNotInitialisedError(DatabaseNotReadyError)
class EventAlreadyInitialisedError(Exception)  # not a DatabaseNotReadyError
```

```python
def verify_ready() -> None
```
Called at app startup. Checks, in order: the database file exists
(`DatabaseMissingError`) — checked via `Path.exists()` *before* opening any
connection, since `sqlite3.connect()` silently creates the file otherwise;
`schema_migrations` exists and is non-empty (`MigrationsPendingError`);
`meta['event_initialised'] == 'true'` (`EventNotInitialisedError`). Opens
its own bare connection (not `get_connection()`) so it applies no pragmas
and causes zero side effects — purely a read.

```python
def initialise_event(horse_count: int, total_races: int) -> None
```
The deliberate, hand-run bootstrap. Assumes migrations already applied.
Raises `EventAlreadyInitialisedError` if called twice. Seeds horses
`1..horse_count`, races `1..total_races` as `SCHEDULED`, a `race_entry`
row for every (race, horse) pair, and sets `meta['total_races']` /
`meta['event_initialised']`.

### Reads that feed Step 1 directly

```python
def get_guests() -> list[GuestInfo]
def get_logged_in_guest_ids() -> set[int]           # claimed_at IS NOT NULL
def get_settled_results() -> dict[int, RaceResult]   # status = 'SETTLED' only
def get_live_bets() -> list[Bet]                     # superseded_at IS NULL
```
Import `GuestInfo`, `Bet`, `RaceResult` directly from
`app.services.scoring` — no local redefinition, no adapter. Every
bet-reading function in the module filters `superseded_at IS NULL`.

### Primitives for Step 3

Return `sqlite3.Row` / `sqlite3.Row | None` / plain values — not part of
the Step 1 contract, so no dataclass wrapping. No validation beyond what
the schema enforces (e.g. `insert_bet` does not check race status — that's
the state machine's job in Step 3):

```python
fetch_guest_by_id(guest_id) -> sqlite3.Row | None
fetch_guest_by_username(username) -> sqlite3.Row | None
fetch_guest_by_device_token(device_token) -> sqlite3.Row | None
insert_guest(username, display_name, created_at, *, conn=None) -> int
get_horses() -> list[sqlite3.Row]
fetch_race(race_number) -> sqlite3.Row | None
get_race_entries(race_number) -> list[sqlite3.Row]
insert_bet(race_number, guest_id, horse_number, client_bet_id, created_at, *, conn=None) -> int
place_or_replace_bet(race_number, guest_id, horse_number, client_bet_id, created_at) -> BetWriteResult
fetch_bet_by_client_bet_id(client_bet_id) -> sqlite3.Row | None
append_audit_log(at, actor, action, payload_json, *, conn=None) -> int
get_meta(key) -> str | None
set_meta(key, value, *, conn=None) -> None
```

`insert_bet` is a low-level primitive with no supersede handling and no
idempotency check — it exists for test setup and bulk seeding only.
Guest-facing bet writes must go through `place_or_replace_bet` instead.

## The supersede ordering resolution: `superseded_at`, not `superseded_by`

The original schema had `bet.superseded_by INTEGER REFERENCES bet(id)`.
The partial unique index on `bet` is checked per-statement, not at
commit, so within one transaction there was no ordering of "insert the
replacement bet" / "mark the old bet superseded" that satisfied both the
uniqueness index (which forbids the replacement's INSERT while the old
bet is still live) and the FK on `superseded_by` (which forbids pointing
the old bet at a not-yet-existing replacement id). `tests/test_db.py`
worked around this by superseding a bet using the id of an unrelated bet
in a different race as a temporary valid FK target — a hack that
corrupted the meaning of test data and was never going to translate into
a real `bets.py` operation.

The column is now `bet.superseded_at TEXT`, a plain nullable timestamp
with no FK and no pointer to any other row. This sidesteps the ordering
problem entirely: `UPDATE bet SET superseded_at = ? WHERE race_number = ?
AND guest_id = ? AND superseded_at IS NULL` turns the old live bet
non-live *before* the replacement is inserted, satisfying the partial
unique index with nothing left to satisfy on the FK side, because there
is no FK. The replacement bet needs no pointer back to what it replaced —
it's recoverable as that guest's next bet in the race, ordered by
`created_at`.

`db.py` now owns this as one atomic composite primitive:

```python
@dataclass(frozen=True)
class BetWriteResult:
    bet_id: int
    replaced: bool


def place_or_replace_bet(
    race_number: int, guest_id: int, horse_number: int,
    client_bet_id: str, created_at: str,
) -> BetWriteResult
```

In a single `transaction()`: supersede any live bet for `(race_number,
guest_id)` by setting its `superseded_at` to `created_at`, then insert
the new bet via `insert_bet(..., conn=conn)`. `replaced` comes straight
from the UPDATE's `cursor.rowcount` (0 or 1 — the partial unique index
guarantees it can never exceed 1), so callers can tell "placed a first
bet" from "changed an existing bet" without a second query. If the
INSERT fails (e.g. a retried `client_bet_id` colliding with the
now-superseded original row, which is still subject to the table-wide
`client_bet_id` UNIQUE constraint), `transaction()` rolls back the
UPDATE too — the original bet stays live, exactly as if the call had
never happened. No validation beyond what the schema enforces — race
status, horse-in-race, and idempotency checks are Step 3's `bets.py`'s
job, same as `insert_bet` never checked those either. `bets.py` should
call `place_or_replace_bet` directly rather than reassembling the
UPDATE/INSERT itself.

## Test coverage summary

83 tests passing total (29 in `tests/test_db.py`, plus the 54 from
Step 1), run via `venv\Scripts\Activate.ps1` then `pytest` from the repo
root. Uses a file-backed temp DB per test (`tmp_path`, `DATABASE_PATH` via
`monkeypatch`), never `:memory:`, since WAL and file-existence behavior are
under test.

Required cases locked in: migrations apply cleanly and re-running is a
no-op; a malformed migration is fully rolled back with nothing recorded;
WAL and `foreign_keys` are actually on; `get_connection()` names its error
when `DATABASE_PATH` is unset; `verify_ready()` raises the right specific
exception for missing/un-migrated/uninitialised, passes after
`initialise_event`, and — checked as its own assertion, not just inferred
from the raise — never creates the database file when it raises;
`initialise_event` seeds the right row counts and raises on a second call;
the one-open-race and one-live-bet partial indexes reject violations and
the live-bet one permits a replacement once `place_or_replace_bet`
supersedes the old bet; duplicate `client_bet_id` and FK violations are
rejected; the two new `race` `CHECK` constraints reject a `SETTLED` race
with a `NULL` placing and a race with a repeated horse across positions;
`get_live_bets()`/`get_settled_results()` respect their filters;
`get_guests()` / `get_logged_in_guest_ids()` / `get_live_bets()` /
`get_settled_results()` feed `build_leaderboard()` directly with no
adapter, producing correct ranks and totals; `place_or_replace_bet` called
twice leaves exactly one live and one superseded bet, inserts-only when
there's no existing bet, and rolls back its UPDATE when the INSERT fails
on a duplicate `client_bet_id`; and `transaction()` rolls back fully on an
exception and commits or rolls back multiple composed writes together.

## Carry-forward note for Step 3

`services/races.py` (state machine) and `services/bets.py` (bet rules)
land next. `races.py` will drive `race.status` transitions using
`db.fetch_race`, and will need to decide the OPEN/LOCKED/SETTLED write
path — likely a small set of new `db.py` primitives (`update_race_status`,
`settle_race`, etc.) rather than raw `UPDATE race SET ...` scattered
through the service, though `db.py` should stay opinion-free about *when*
those transitions are valid; that logic belongs in `races.py`. `bets.py`'s
"place or change a bet" operation should call `db.place_or_replace_bet`
directly (the supersede-ordering problem is already solved at the `db.py`
layer — see "The supersede ordering resolution" above) and should reuse
`db.fetch_bet_by_client_bet_id` for the idempotency pre-check, but treat
the `IntegrityError` from a duplicate `client_bet_id` as the authoritative
idempotency signal rather than relying on the pre-check alone (see the
tracker's open questions for the TOCTOU note on this). `insert_bet` is a
low-level primitive now — do not call it directly for guest-facing writes.
