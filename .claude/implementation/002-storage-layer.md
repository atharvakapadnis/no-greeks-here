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
  superseded_by IS NULL`. This is the invariant `total_points_by_guest`
  depends on — it sums every bet it's handed and cannot itself detect a
  duplicate.
- `bet.client_bet_id` UNIQUE (the idempotency key) and foreign keys on
  every reference, including `bet.superseded_by -> bet(id)`.

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
def get_live_bets() -> list[Bet]                     # superseded_by IS NULL
```
Import `GuestInfo`, `Bet`, `RaceResult` directly from
`app.services.scoring` — no local redefinition, no adapter. Every
bet-reading function in the module filters `superseded_by IS NULL`.

### Primitives for Step 3

Return `sqlite3.Row` / `sqlite3.Row | None` / plain values — not part of
the Step 1 contract, so no dataclass wrapping. No validation beyond what
the schema enforces (e.g. `insert_bet` does not check race status — that's
the state machine's job in Step 3):

```python
fetch_guest_by_id(guest_id) -> sqlite3.Row | None
fetch_guest_by_username(username) -> sqlite3.Row | None
fetch_guest_by_device_token(device_token) -> sqlite3.Row | None
insert_guest(username, display_name, created_at) -> int
get_horses() -> list[sqlite3.Row]
fetch_race(race_number) -> sqlite3.Row | None
get_race_entries(race_number) -> list[sqlite3.Row]
insert_bet(race_number, guest_id, horse_number, client_bet_id, created_at) -> int
mark_bet_superseded(bet_id, superseded_by) -> None
fetch_bet_by_client_bet_id(client_bet_id) -> sqlite3.Row | None
append_audit_log(at, actor, action, payload_json) -> int
get_meta(key) -> str | None
set_meta(key, value) -> None
```

## The supersede ordering gotcha for Step 3

The partial unique index on `bet` checks immediately per-statement, not at
commit — so within one transaction you cannot have a replacement bet's
`INSERT` land while the old bet's `superseded_by` is still `NULL` (uniqueness
violation), and you cannot mark the old bet superseded by a not-yet-existing
new bet's id (FK violation). There is no ordering of "insert new bet" /
"mark old bet superseded" that satisfies both constraints in two statements.

`mark_bet_superseded` itself is a raw primitive with no opinion on this —
Step 3's `bets.py` is where the resolution needs to live. `test_db.py`
demonstrates one working pattern (used only for test setup, not
prescriptive for Step 3): supersede the old bet using the id of some
*other* already-existing bet as a temporary valid target, insert the
replacement, done. A cleaner real implementation likely wants something
along those lines or a self-referencing placeholder — either way, Step 3
needs to design the actual supersede operation with this ordering
constraint in mind, not just call `insert_bet` then `mark_bet_superseded`
in the obvious order.

## Test coverage summary

77 tests passing total (23 new in `tests/test_db.py`, plus the 54 from
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
the live-bet one permits a replacement once the old bet is marked
superseded; duplicate `client_bet_id` and FK violations are rejected; the
two new `race` `CHECK` constraints reject a `SETTLED` race with a `NULL`
placing and a race with a repeated horse across positions;
`get_live_bets()`/`get_settled_results()` respect their filters; and
`get_guests()` / `get_logged_in_guest_ids()` / `get_live_bets()` /
`get_settled_results()` feed `build_leaderboard()` directly with no
adapter, producing correct ranks and totals.

## Carry-forward note for Step 3

`services/races.py` (state machine) and `services/bets.py` (bet rules)
land next. `races.py` will drive `race.status` transitions using
`db.fetch_race`, and will need to decide the OPEN/LOCKED/SETTLED write
path — likely a small set of new `db.py` primitives (`update_race_status`,
`settle_race`, etc.) rather than raw `UPDATE race SET ...` scattered
through the service, though `db.py` should stay opinion-free about *when*
those transitions are valid; that logic belongs in `races.py`. `bets.py`
must solve the supersede-ordering gotcha above as part of its "place or
change a bet" operation, and should reuse `db.fetch_bet_by_client_bet_id`
for the idempotency check before writing anything.
