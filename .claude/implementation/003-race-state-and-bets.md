# 003 — Race state machine and bet rules

2026-07-31

## What was built and why

`app/services/races.py`, `app/services/bets.py`, `tests/test_races.py`,
`tests/test_bets.py`, and nine small additions to `app/db.py` — Step 3 of
the design doc's build order. Unlike Step 1's pure modules, both new
services read and write through `app.db`, but neither imports anything
web-related: Step 4 (routers) calls into these, never the reverse.

`races.py` owns the `SCHEDULED -> OPEN -> LOCKED -> SETTLED` state machine
plus the `reopen`/`correct_result` recovery paths and the derived read
model (`current_race_number`, `current_state`). `bets.py` owns bet
acceptance (`place_bet`, guest-facing) and the operator's fix-a-bet
override (`operator_set_bet`) — deliberately not unified, same as
`assign_usernames` vs `add_guest_username` in Step 1.

Every function that records or compares a time takes an explicit
`now: datetime` (UTC). Nothing in either module calls `datetime.now()`
itself — this is what makes auto-lock behavior testable without sleeping.

## `app/db.py` additions

Nine new primitives, all following the existing recurse-once-and-commit
`conn=None` convention: `update_race_status`, `settle_race_result`,
`set_race_auto_lock`, `set_horse_scratched`, `fetch_live_bet`,
`fetch_races`, `fetch_bets_for_race`, `count_live_bets`,
`set_guest_claimed_at`, plus `void_live_bets_for_horse` (added during
implementation, see below) and a `conn=None` kwarg on
`place_or_replace_bet` (previously always self-contained).

`settle_race_result` is a single atomic `UPDATE` of `status='SETTLED'`
plus all three placings plus `settled_at`, kept separate from
`update_race_status` because the `race` table's two `CHECK` constraints
(placings distinct, non-null-once-SETTLED) need status and placings
landing together in one statement.

`update_race_status` never touches `auto_lock_at` — that always goes
through `set_race_auto_lock` instead, so `races.py` can clear the
auto-lock timer independently of a status change (e.g. `lock_race` clears
it while also setting `status='LOCKED'` via two separate primitive calls
inside one `db.transaction()`).

**`place_or_replace_bet`'s docstring carries an explicit warning** (added
during plan review): a `sqlite3.IntegrityError` raised against a
caller-supplied `conn` cannot be caught-and-recovered-from — SQLite aborts
the whole transaction the moment a statement inside it fails. The
catch-and-recover pattern in `bets.place_bet` only works because it calls
`place_or_replace_bet` with `conn=None`, so that call's own
`db.transaction()` has already rolled back by the time the exception
reaches the caller. Anything composing this under a shared `conn` (i.e.
`bets.operator_set_bet`) must let the exception propagate. This is
recorded in the tracker's Step 4 open questions because the operator's
audited bet path is exactly where someone would later reach for the
broken pattern.

## `app/services/races.py`

Exception hierarchy: `RaceError` → `RaceNotFoundError`,
`IllegalTransitionError`, `AnotherRaceOpenError`, `InvalidResultError`.
`InvalidResultError` covers both invalid settle placings and
`set_scratched` given a horse not entered in the race — there's no
dedicated exception for the latter in the fixed hierarchy, and it's a
horse-validity problem, not a status problem.

Every transition writes exactly one `audit_log` row in the same
`db.transaction()` as the state change; an idempotent no-op (e.g. opening
an already-`OPEN` race) is not a transition and writes none. Audit action
strings: `race.opened`, `race.locked`, `race.settled`, `race.reopened`,
`race.result_corrected`, `race.scratch_set`, `race.auto_locked`
(`actor="system"`, everything else `actor="operator"`).

`RaceState` (returned by `current_state(now)`) is a frozen dataclass:
`race_number`, `total_races`, `status`, `seconds_to_auto_lock`, `horses`
(`list[HorseEntry]`), `live_bet_count`, `result` (`RaceResult | None`,
populated only when `status == "SETTLED"`), `event_complete`.

### Corrections made during implementation (beyond the approved plan)

- **`set_scratched` permitted while `SCHEDULED` or `OPEN`**, not
  `SCHEDULED`-only as originally scoped. Caught during plan review: a
  horse can pull up lame after betting opens, and there was no recovery
  path otherwise. Scratching (`scratched=True`) a horse mid-`OPEN` voids
  every live bet on that horse in that race — superseded with no
  replacement, in the same transaction as the flag change, via the new
  `db.void_live_bets_for_horse(race_number, horse_number, voided_at,
  conn=conn)` primitive (returns the affected `guest_id`s).
  `place_or_replace_bet` couldn't express this: it always inserts a
  replacement bet, which a void is not. Exactly one `race.scratch_set`
  audit row is written regardless of how many bets were voided, with
  `voided_guest_ids` in the payload. Unscratching does **not** restore
  voided bets — guests must re-bet; this is deliberate, not an oversight.
- **`current_state`'s terminal state.** `current_race_number()` still
  returns `None` once every race is `SETTLED`, exactly as specified. But
  `current_state` doesn't mirror that with a null view — it falls back to
  the highest-numbered race, reports it `SETTLED` with its result, and
  sets the new `event_complete: bool` field to `True`. Step 4 never needs
  a null branch. The design doc's three guest-screen states
  (waiting/open/locked) don't define an "event over" state; that's a gap
  in the doc, not a reason for the read model to go blank at the end of
  the night.

## `app/services/bets.py`

Exception hierarchy: `BetError` → `BettingClosedError`,
`HorseNotInRaceError`, `GuestNotFoundError`, `GuestNotLoggedInError`.
`place_bet` also imports and re-raises `races.RaceNotFoundError` directly
for a `race_number` that doesn't exist at all — `BetError` has no
`RaceNotFoundError` of its own, kept distinct from `BettingClosedError`
because a locked race is a normal guest-facing state and a nonexistent
race is a bug or a tampered request; they shouldn't look identical in
logs or audit trails.

`BetOutcome` (frozen dataclass): `bet_id`, `horse_number`, `replaced`,
`idempotent`.

`place_bet`'s idempotency pre-check (`db.fetch_bet_by_client_bet_id`) runs
**before** any other validation, so a retried request can never raise even
if race/guest state changed since the original request (e.g. the race
locked in between). **Correction made during plan review:** the returned
`BetOutcome` is built entirely from the stored row — `bet_id` and
`horse_number` both — never from the `horse_number` argument the caller
passed in. Scenario this prevents: guest taps horse #4 (slow request),
then taps #6 before the retry of #4 arrives. Both have distinct
`client_bet_id`s and both legitimately write; #6 is correctly live when
#4's retry lands. Echoing the submitted horse_number (4, from the retry)
instead of the stored one would show the guest a confirmed tick on #4
while the database holds #6. The same rule applies to the
`sqlite3.IntegrityError` TOCTOU-catch path: re-fetch by `client_bet_id`
and build the outcome from that row.

`operator_set_bet` differs from the original brief in two ways, both
required by the design doc's paper-fallback path (operator keys in bets
after a network outage, for guests who may never have claimed a device):

1. It still validates the horse is entered and not scratched
   (`HorseNotInRaceError`), same as `place_bet` — a bet on a horse that
   never ran can't score, so silently accepting it would create a dead
   bet. (The original brief's "rejects only on SETTLED" wording was read
   too literally in the initial plan; this was corrected before
   implementation.)
2. It does **not** require the guest to be logged in — only that the
   guest exists (`GuestNotFoundError`, no `GuestNotLoggedInError` check).
   If the guest's `claimed_at` is currently `NULL`, it's set to `now` in
   the same transaction as the bet and audit writes (`claimed_at` means
   "participating" — this is what makes such a guest appear on the
   leaderboard, since `build_leaderboard` only ranks
   `claimed_at IS NOT NULL` guests). An existing `claimed_at` is never
   overwritten. `device_token` stays `NULL`, so the guest can still claim
   a phone later.

`operator_set_bet` generates its own `client_bet_id` and does **not**
catch `sqlite3.IntegrityError` anywhere — per the warning on
`db.place_or_replace_bet`, any such error must propagate out of its
`db.transaction()` block so the bet, the `claimed_at` update, and the
audit row (`bet.operator_set`) all roll back together.

## Test coverage summary

187 tests passing total (52 in `test_races.py`, 33 in `test_bets.py` —
some parametrized cases push the raw collected-item count to 104 new,
plus the 83 from Steps 1-2), run via `venv\Scripts\Activate.ps1` then
`pytest` from the repo root.

Required cases locked in, beyond the obvious per-transition
happy/illegal-path tests: opening a second race while one is `OPEN`
raises `AnotherRaceOpenError`; settling with a scratched horse, a
duplicate placing, or a horse not entered in that race each raise
`InvalidResultError`; `reopen_race` returns a `LOCKED` race to `OPEN` and
is rejected if another race is `OPEN`; `correct_result` on a `SETTLED`
race changes the placings and writes an audit row; `set_scratched` is
rejected once `LOCKED` or `SETTLED` but permitted on `SCHEDULED`/`OPEN`,
and voids live bets on that horse (leaving other horses' bets untouched,
the voided guest scoring 0) with exactly one audit row listing the
affected guest ids, and unscratching does not restore them; every legal
transition writes exactly one audit row and every illegal one writes
none (parametrized across all six transition kinds and nine illegal-case
kinds respectively); idempotent no-ops (re-open, re-lock, re-reopen)
write no audit row; `current_race_number` skips settled races and returns
`None` only once every race is settled; `apply_auto_lock` locks an
expired race, is a no-op before expiry, when `auto_lock_at` is `NULL`,
and when no race is `OPEN`; `place_bet` is rejected once `auto_lock_at`
has passed even though `status` is still `OPEN` and `apply_auto_lock` has
not run; a repeated `client_bet_id` returns the same bet unchanged and
writes nothing new, including the corrected stale-retry case (a retry for
an old horse, after a newer bet superseded it, returns the *old* horse
and leaves the *new* one live) and the simulated `IntegrityError`
TOCTOU-recovery path; `place_bet` is rejected for `SCHEDULED`/`LOCKED`/
`SETTLED` races and for a guest who hasn't logged in; changing a bet
leaves exactly one live bet; `operator_set_bet` succeeds on `LOCKED` and
on `OPEN`-past-auto-lock, is rejected on `SCHEDULED`/`SETTLED`, does not
require the guest logged in, claims an unclaimed guest (without
overwriting an existing claim) and makes them appear on the leaderboard,
writes its bet and audit row atomically, and — a forced
`sqlite3.IntegrityError` via `actor=None` (violates `audit_log.actor NOT
NULL`) — leaves no bet, no claimed_at change, and no audit row when the
transaction is forced to fail; `current_state` returns correct values in
each of the four statuses plus the all-settled terminal case.

## Carry-forward notes for Step 4

- Bet endpoints must catch `(races.RaceError, bets.BetError)` together —
  `place_bet` can raise from either hierarchy now. The guest-facing
  message is identical for both ("betting is closed" + resync) even
  though the internal exception differs.
- Always render `BetOutcome.horse_number`, never the horse a request
  submitted — they can differ on the idempotent path.
- The actual "is this device logged in" check must test `device_token`,
  not `claimed_at` — the two now mean different things (`claimed_at` =
  participating, `device_token` = which phone claimed the username).
  `operator_set_bet` can set `claimed_at` for a guest who never touched a
  device.
- Never catch `sqlite3.IntegrityError` inline inside code that calls
  `db.place_or_replace_bet(conn=...)` under a shared
  `db.transaction()` — let it propagate. Only the standalone
  (`conn=None`) call, as in `bets.place_bet`, may catch and recover.
- `services/races.py` and `services/bets.py` are both complete and
  tested. Step 4 (routers/templates) can now call `races.current_state`,
  `races.apply_auto_lock`, and the transition/bet functions directly; no
  further `db.py` primitives are anticipated for the guest-facing or
  operator-facing read/write paths, though Step 4 may need its own
  device-token/session primitives for the login flow itself (not yet
  built anywhere).
