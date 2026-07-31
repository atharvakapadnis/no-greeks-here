# Tracker

Status against `@.claude/design/design-doc.md`, updated after each session.

## Build order

- [x] 1. `services/guests.py` + `services/scoring.py` with tests. Pure functions, no
      DB, no AWS. Username collisions and dense ranking settled here.
      Completed 2026-07-30. 54 tests passing. See
      `@.claude/implementation/001-pure-service-modules.md`.
- [x] 2. `db.py` + `migrations/001_initial.sql`. Completed 2026-07-31, amended
      2026-07-31 (`superseded_at` + composable transactions). 29 tests in
      `test_db.py` (83 total passing). See
      `@.claude/implementation/002-storage-layer.md`.
- [x] 3. `services/races.py` + `services/bets.py` with tests. The state machine is
      where correctness lives. Completed 2026-07-31. 104 tests in
      `test_races.py` + `test_bets.py` (187 total passing). See
      `@.claude/implementation/003-race-state-and-bets.md`.
- [ ] 4. Routers and templates, guest side first.
- [ ] 5. SSE and client resilience in `static/app.js`.
- [ ] 6. Docker, Litestream, Lightsail. Then rehearse a restore twice before the
      event.

## Decisions changed since design doc

- The design doc's ranking example was malformed (five elements, missing
  rank 3: `1, 2, 4, 4, 5`) and has been corrected to the six-element example
  `1, 2, 3, 4, 4, 5`. The algorithm itself is, and always was, dense
  ranking — only the illustrative example was wrong, not the rule.
- `max_rows` (default 25) added as a cap on the leaderboard's boundary-tie
  extension. Not in the original design: dense ranking compresses ranks, so
  early in the evening most guests are tied on zero points, and an
  uncapped "never split a tie" rule would return the entire guest list
  after race 1. Truncation only ever removes guests from within the single
  tied group at the boundary; `Leaderboard.truncated_count` and
  `truncated_points` report what was cut.
- `bet.superseded_by` (a self-referencing FK pointing at the replacement
  bet) was replaced with `bet.superseded_at` (a plain nullable timestamp,
  no FK). The partial unique index on `bet` is checked per-statement, not
  at commit, so `superseded_by` made it impossible to satisfy both the
  uniqueness index and the FK in any two-statement ordering — this was the
  "supersede ordering gotcha" recorded against Step 3 below. It also forced
  `tests/test_db.py` into supersede-target workarounds that pointed a bet
  at an unrelated bet in a different race purely to have a valid FK value,
  which corrupted the meaning of the column in test data.
  `superseded_at` sidesteps the ordering problem entirely: an `UPDATE ...
  SET superseded_at = ?` on the old live bet, followed by the `INSERT` of
  its replacement, satisfies the partial unique index with no FK to
  satisfy at all. The replacement bet is still recoverable without a
  pointer — it's simply that guest's next bet in the race, ordered by
  `created_at`. Implemented as `db.place_or_replace_bet()`, a single
  atomic operation via the new `db.transaction()` helper, replacing the
  raw `mark_bet_superseded` primitive.
- `current_state(now)` never returns a null/empty view, even once every
  race is `SETTLED`. `current_race_number()` still returns `None` in that
  case exactly as specified, but `current_state` falls back to the
  highest-numbered race and reports it `SETTLED` with its result, plus a
  new `RaceState.event_complete: bool` field (`True` only when every race
  is settled). The design doc's guest-screen states (waiting/open/locked)
  never defined a fourth "event over" state — that was a gap in the doc,
  not a reason for the read model to go blank at the end of the night.
- `set_scratched` is permitted while `SCHEDULED` **or** `OPEN`, not
  `SCHEDULED`-only as originally scoped — a horse can pull up lame after
  betting opens and there must be a recovery path. Scratching
  (`scratched=True`) a horse mid-`OPEN` now voids every live bet on that
  horse in that race (superseded, no replacement — a new
  `db.void_live_bets_for_horse` primitive, since `place_or_replace_bet`
  always inserts a replacement and can't express a void) in the same
  transaction as the flag change, with exactly one `race.scratch_set`
  audit row whose payload lists the affected `guest_id`s regardless of how
  many bets were voided. Unscratching deliberately does not restore voided
  bets — guests must re-bet.
- `operator_set_bet` does not require the guest to be logged in (no
  `GuestNotLoggedInError` check — only `GuestNotFoundError`) and validates
  the horse is entered/not-scratched (`HorseNotInRaceError`, same as
  `place_bet`) even though the original brief read as "rejects only on
  SETTLED." The no-login-required change is required for the paper
  fallback in the design doc: the operator keys in bets after a network
  outage for guests who never claimed a device. If the guest's
  `claimed_at` is `NULL`, `operator_set_bet` now sets it to `now` in the
  same transaction as the bet and audit writes (a new `db.set_guest_claimed_at`
  primitive) — this is what makes such a guest appear on the leaderboard at
  all, since `build_leaderboard` only ranks `claimed_at IS NOT NULL`
  guests. An existing `claimed_at` is never overwritten. `device_token`
  stays `NULL`, so the guest can still claim a phone later.
- `place_bet`'s idempotent short-circuit (a repeated `client_bet_id`, via
  either the pre-check or the `IntegrityError` catch) builds its
  `BetOutcome` **entirely from the stored bet row** — `bet_id` and
  `horse_number` both — never from the `horse_number` argument the caller
  passed in. A slow retry for an old horse can arrive after a faster,
  distinct-`client_bet_id` request already changed the live bet; echoing
  the submitted horse instead of the stored one would tell the guest
  they're confirmed on a horse the database doesn't have them on.
- `place_bet` reuses `races.RaceNotFoundError` (imported, not redeclared)
  for a `race_number` that doesn't exist — `BetError` itself has no
  `RaceNotFoundError`, kept distinct from `BettingClosedError` because a
  locked race is a normal state and a nonexistent race is a bug/tampered
  request.

## Open questions for later steps

- Step 4 (routers): `build_leaderboard` raises `ValueError` when
  `requesting_guest_id` is given but not found among `guests`, or not
  present in `logged_in_guest_ids`. The router must catch this and redirect
  to login rather than letting it surface as a 500 — a guest should never
  see an error page mid-event.
- Step 3 (`bets.py`): `fetch_bet_by_client_bet_id` is the natural idempotency
  pre-check, but a pre-check-then-write is technically a TOCTOU race — two
  requests with the same `client_bet_id` could both pass the check before
  either writes. Harmless under the single-uvicorn-worker deployment target
  here, and `place_or_replace_bet` rolls back cleanly on the resulting
  `client_bet_id` UNIQUE violation, but `bets.py` should treat that
  `IntegrityError` as the authoritative idempotency signal rather than
  relying on the pre-check alone.
- Step 4 (routers): bet endpoints must catch `(races.RaceError, bets.BetError)`
  together — `place_bet` can now raise from either hierarchy (see
  `RaceNotFoundError` reuse above). The guest-facing message is the same
  for both ("betting is closed" + resync) even though the internal
  exception differs; don't let that similarity erase the distinction in
  logs.
- Step 4 (routers/templates): render `BetOutcome.horse_number`, never the
  horse the guest's request submitted — on the idempotent path they can
  differ (see the stored-row decision above).
- Step 4 (login/claim check): test `device_token`, not `claimed_at`, for
  "is this device logged in." The two now mean different things:
  `claimed_at` means "participating" (may be set by `operator_set_bet` for
  a guest who never touched a phone), `device_token` means "this specific
  device is the one that claimed the username."
- Step 4 (or any future code composing `db.place_or_replace_bet(conn=...)`
  under a shared `db.transaction()`): never catch `sqlite3.IntegrityError`
  inline and try to continue — once a statement inside an explicit SQLite
  transaction fails, the whole transaction is aborted, so there is no
  catch-and-still-commit recovery. Only the standalone form
  (`conn=None`, which owns and has already rolled back its own
  transaction by the time the exception surfaces) may catch
  `IntegrityError` and recover by re-reading, as `bets.place_bet` does.
  `bets.operator_set_bet` deliberately does not catch it, for exactly this
  reason — see its docstring and `db.place_or_replace_bet`'s.
