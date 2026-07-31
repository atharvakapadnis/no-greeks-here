# Tracker

Status against `@.claude/design/design-doc.md`, updated after each session.

## Build order

- [x] 1. `services/guests.py` + `services/scoring.py` with tests. Pure functions, no
      DB, no AWS. Username collisions and dense ranking settled here.
      Completed 2026-07-30. 54 tests passing. See
      `@.claude/implementation/001-pure-service-modules.md`.
- [ ] 2. `db.py` + `migrations/001_initial.sql`.
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

## Open questions for later steps

- Step 4 (routers): `build_leaderboard` raises `ValueError` when
  `requesting_guest_id` is given but not found among `guests`, or not
  present in `logged_in_guest_ids`. The router must catch this and redirect
  to login rather than letting it surface as a 500 — a guest should never
  see an error page mid-event.
