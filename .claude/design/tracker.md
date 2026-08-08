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
- [x] 4a. Guest-facing routers and templates. Completed 2026-07-31, fix-up
      pass 2026-07-31 (crypto.randomUUID LAN fallback, pending-tap state,
      countdown tick, leaderboard polling + settle banner, `POST /bet`
      current_state unification, corrected render-rule wording, active tab
      indicator — see "Step 4a fix-up pass" in
      `@.claude/implementation/004-guest-web-layer.md`). 32 new tests
      (`test_auth.py`, `test_guest_routes.py`) plus 11 naive-datetime guard
      tests at initial completion, plus 12 more in the fix-up pass — 242
      total passing.
- [x] 4b. Operator-facing routers and templates: race control panel
      (open/lock/settle/scratch), results entry, add guest, unlock device.
      Python pass completed 2026-08-08: `app/routers/operator.py`,
      operator auth/rate-limit/flash in `app/auth.py`, two new `db.py`
      primitives, `scripts/import_guests.py`, functional (not yet
      polished) templates under `app/templates/operator/`. 70 new tests
      (`test_operator_auth.py`, `test_operator_routes.py`,
      `test_import_guests.py`) — 312 total passing. See
      `@.claude/implementation/005-operator-panel.md`. Template pass
      completed 2026-08-08: tap-a-position/tap-a-horse results entry
      (`app/static/app.js`'s `initResultsEntry()`), scratch checkboxes on
      scheduled/open/settled posting their own desired state, per-view
      layout (dominant primary action, muted secondary actions, bet-count
      chips, add-guest callout, mid-dot confirm page), a username-based
      guest picker for Fix-a-bet/Unlock (separate claimed-only vs.
      all-guests lists), and `tests/test_operator_templates.py`'s
      parametrized per-view action-form presence/absence table. 30 new
      tests — 342 total passing. `scripts/operator_demo.py` added for
      pre-event rehearsal. See
      `@.claude/implementation/006-operator-panel-template-pass.md`.
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
- Step 4b's operator login rate limiter is a single global in-process
  counter (`app/auth.py`'s `_operator_login_failures`), not per-IP as the
  original brief specified. Behind Caddy at the venue every device shares
  one NAT'd address, so a per-IP dict would in practice be one global
  counter anyway — this makes that honest instead of pretending an
  isolation that doesn't exist. It also never rejects a correct password:
  past 5 failures, every subsequent WRONG password is answered after a
  fixed 2s delay (`operator_login_delay_seconds()`) rather than being
  locked out, so an operator mistyping their own password mid-event is
  never blocked from their own event. A correct password always succeeds
  immediately and resets the counter.
- Operator action feedback (error messages, the generated username on
  add-guest) travels via a one-shot signed cookie
  (`ngh_operator_flash`, 30s max-age, read once by `GET /operator` and
  always deleted on that response), not a `?error=...` query string. A
  query string survives refresh/back-navigation, so the operator would see
  stale results for actions that already succeeded; the cookie payload is
  a message KEY (`operator.py`'s `FLASH_MESSAGES`), never free text, so
  operator-facing copy stays in one place and nothing user-influenced is
  ever rendered as HTML.
- `POST /operator/race/settle` and `POST /operator/race/correct` are a
  two-step, server-rendered confirm (a `confirmed` form field; the first
  POST renders `operator/confirm.html` naming the placings without writing
  anything, and its button re-POSTs with `confirmed=1`), not a client-side
  `window.confirm()` — that API is unreliable on iOS Safari, and this is
  the one confirm step in the app.
- `POST /operator/race/correct` validates `race_number` against the MOST
  RECENTLY settled race (`race_number == max(db.get_settled_results())`),
  not against `races.current_state(now).race_number` like every other
  action route. `correct_result`'s own fresh SETTLED-status check passes
  for *any* settled race, forever — relying on that alone would let a
  stale tab from two races ago silently rewrite an old result. `_effective_
  state`'s reconstructed "settled" state must never be used for this
  check either (see its docstring).
- Step 4b's template pass changed `POST /operator/guest/unlock` and
  `POST /operator/bet/set` from a `guest_id: int` form field to
  `username: str`, resolved server-side via `db.fetch_guest_by_username`
  — a numeric guest ID was never something an operator could supply from
  memory mid-event. Approved scope addition beyond the session's original
  brief; see `@.claude/implementation/006-operator-panel-template-pass.md`.
- `POST /operator/race/scratch`'s `scratched` field changed from
  `Form(...)` (required, either `"true"` or `"false"`) to `Form(False)`
  (optional, defaulting to unscratched). The scratch checkbox posts the
  state the operator just expressed (checked -> `scratched=true`,
  unchecked -> field omitted entirely), not a client-computed opposite of
  the current state — the earlier hidden-inverse-field design was exactly
  the "client asserts current server state" pattern Step 3 removed
  everywhere else, and silently no-oped on a double submit in the same
  state.

## Step 3's open questions, resolved in Step 4a

All five of Step 3's carry-forward notes (leaderboard `ValueError` ->
redirect, catching `races.RaceError`/`bets.BetError` together, rendering
the guest's current live bet not the submitted horse, `device_token`-not-
`claimed_at` for login state, never catching `IntegrityError` under a
shared `conn`) were implemented exactly as specified in
`app/routers/guest.py`. See
`@.claude/implementation/004-guest-web-layer.md` for where each landed.
The third of these was worded imprecisely in the original carry-forward note
("render `BetOutcome.horse_number`, never the submitted horse") and was
corrected in the Step 4a fix-up pass: `BetOutcome` is used for the write
path only (idempotency dedup inside `place_bet`); the render path
(`_bet_screen_context`) always renders the guest's current live bet via
`bets.get_live_bet`, and never reads `BetOutcome` or the submitted
`horse_number` at all.

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

## Environment notes (not bugs)

- Manual testing during Step 4a found `ConnectionResetError [WinError 10054]`
  logged on iOS Safari connections, and `uvicorn`'s graceful shutdown
  sometimes hanging on Ctrl-C. Both are Windows Proactor event-loop
  artifacts (the asyncio event loop this dev machine defaults to), not
  application errors — they don't indicate a request was mishandled or a
  response was lost. Step 6's deployment verification should confirm
  neither appears once the app runs on Linux (Docker/Lightsail), where the
  event loop is selector-based, not Proactor-based.
