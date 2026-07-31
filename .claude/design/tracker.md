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
- [ ] 3. `services/races.py` + `services/bets.py` with tests. The state machine is
      where correctness lives.
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
