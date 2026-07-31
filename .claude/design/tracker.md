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
- [x] 4a. Guest-facing routers and templates. Completed 2026-07-31. 32 new
      tests (`test_auth.py`, `test_guest_routes.py`) plus 11 naive-datetime
      guard tests added to `test_races.py`/`test_bets.py` — 230 total
      passing. See `@.claude/implementation/004-guest-web-layer.md`.
- [ ] 4b. Operator-facing routers and templates: race control panel
      (open/lock/settle/scratch), results entry, add guest, unlock device.
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

## Step 3's open questions, resolved in Step 4a

All five of Step 3's carry-forward notes (leaderboard `ValueError` ->
redirect, catching `races.RaceError`/`bets.BetError` together, rendering
`BetOutcome.horse_number` not the submitted horse, `device_token`-not-
`claimed_at` for login state, never catching `IntegrityError` under a
shared `conn`) were implemented exactly as specified in
`app/routers/guest.py`. See
`@.claude/implementation/004-guest-web-layer.md` for where each landed.

## Open questions for later steps

- Step 4b (operator panel) / Step 5 (SSE): at 75 guests polling `GET
  /state` every 3s, each request opens roughly six separate SQLite
  connections (`apply_auto_lock`'s read, `current_state`'s four reads,
  `get_live_bet`). Fine at this scale — this is a measured decision, not an
  unexamined one — and Step 5's SSE removes most of this polling entirely,
  but don't scale the polling interval down without re-checking it.
- Step 4b: every route handler in `app/routers/guest.py` is a plain `def`,
  not `async def`, because `app.db` is blocking stdlib sqlite3 and FastAPI
  runs sync handlers in a threadpool — an `async def` handler calling `db.*`
  directly would block the single event loop for every other guest. Step 4b
  must follow the same convention. Step 5's SSE endpoint is the one
  legitimate `async def` in the app, and it must not do blocking DB work
  inline (push pre-computed state instead).
- Step 4b: `app/config.py`'s `get_settings()` and `db.verify_ready()` are
  both called, uncached, from `app/main.py`'s `lifespan` startup — not at
  module import time. Any new startup check Step 4b/5/6 need should go in
  that same `lifespan` function, not a new `@app.on_event` (deprecated) or
  a module-level singleton, or per-test env isolation breaks.
- Step 4b: `app/routers/guest.py`'s `_classify_state(state: RaceState) ->
  str` is pure (no DB access) and is the one place that decides
  waiting/open/locked/complete. The operator panel's own render states are
  a different decision (it needs to show scratch checkboxes, who-hasn't-bet,
  etc.) and should get its own equivalent function rather than reusing or
  branching on this one.
- Step 5: HTMX is vendored at `app/static/htmx.min.js` (pinned 1.9.12), not
  loaded from a CDN — the design doc's stated most-likely failure mode is
  the venue network, so nothing on the guest's critical path should depend
  on an external host. Keep this rule for anything else Step 5 would
  otherwise reach for from a CDN.
