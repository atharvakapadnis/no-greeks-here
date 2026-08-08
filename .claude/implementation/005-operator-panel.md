# 005 — Operator panel, python pass (Step 4b)

2026-08-08

## What was built and why

This session covered the **python pass** of Step 4b: operator auth, the
`_effective_state` reconciliation logic, every action route, the
exception-to-message mapping, the two new `db.py` primitives, and
`scripts/import_guests.py` — all with functional (not yet visually
polished) templates so the whole thing runs end to end. The **template
pass** (per-view markup presence/absence assertions, the tap-to-select
results grid, scratch-checkbox and results-entry visual polish) was
explicitly deferred to a later session at the user's request — see
"Deferred to the template pass" below.

A plan was written, reviewed, and approved via plan mode, then corrected in
four ways plus three smaller items before implementation began (all
applied as designed, not as originally planned — see "Corrections from the
approved plan" below). The corrected plan is what's implemented.

## Carry-forwards from 4a testing (done first, per this session's brief)

- `.horse-btn { touch-action: manipulation; }` added to `app/static/app.css`
  — Safari's double-tap-to-zoom was masking the `hx-disabled-elt` guard.
- `ConnectionResetError [WinError 10054]` on iOS Safari and uvicorn's
  hanging graceful shutdown recorded in the tracker as Windows Proactor
  event-loop artifacts, not application errors, with a line added to Step
  6's verification to confirm neither appears on Linux.

## Corrections from the approved plan

The plan (written and approved via plan mode) was corrected by the user in
four ways before implementation, plus three smaller items. All are
implemented as corrected, not as originally planned:

1. **Rate limiter**: global in-process counter, not per-IP (behind Caddy at
   the venue every device shares one NAT'd address, so per-IP would be one
   counter anyway — `app/auth.py` admits this instead of pretending
   otherwise). Never rejects a correct password: past 5 failures, every
   subsequent WRONG password gets a fixed 2s delay
   (`operator_login_delay_seconds()` / `enforce_operator_login_delay()`)
   rather than a lockout. A correct password always succeeds immediately
   and resets `_operator_login_failures` to 0.
2. **Flash feedback**: a one-shot signed cookie (`ngh_operator_flash`,
   `URLSafeTimedSerializer`, 30s max-age), not `?ok=1&...` query params.
   `read_flash_cookie`/`clear_flash_cookie`/`set_flash_cookie` in
   `app/auth.py`; the payload is a message KEY, resolved against
   `operator.py`'s `FLASH_MESSAGES` dict — never free text rendered as
   HTML. `GET /operator` reads the cookie before building the Jinja
   context (Jinja2Templates renders the body at `TemplateResponse.__init__`
   time, too early to mutate context afterward) and always deletes it on
   the response it returns, whether or not a flash was found.
3. **`POST /operator/race/correct`** validates `race_number` against
   `race_number == max(db.get_settled_results())`, not against
   `races.current_state(now).race_number` (which is never the race
   `correct_result` targets, except on the final race). Without this, a
   stale tab from two races ago could silently rewrite an old result.
4. **No `window.confirm()`** — unreliable on iOS Safari. `race/settle` and
   `race/correct` are both a two-step, server-rendered confirm: the first
   POST (no `confirmed` field) renders `operator/confirm.html` naming the
   three placings, without writing anything; its "Confirm and publish/
   correct" button re-POSTs the same route with `confirmed=1`, which
   performs the actual write. This is a deliberate, narrow exception to
   "every action route always redirects 303" — the interim confirm-render
   step returns 200, and only the confirmed write redirects. A refresh on
   the confirm page just re-renders it (harmless — nothing was written yet).
5. **`scripts/import_guests.py`** checks the batch's generated usernames
   against `db.fetch_guests()` *before* inserting anything (only relevant
   under `--append`, since `assign_usernames` only resolves collisions
   within its own CSV batch) and reports every collision at once, rather
   than discovering the first one via `sqlite3.IntegrityError` mid-insert.
   The `IntegrityError` catch is kept as a backstop for a race against
   another writer between the check and the insert.
6. **Add-guest collision checking** uses `db.fetch_guests()` (ALL guest
   rows, claimed or not) — already correct in the plan, but now has an
   explicit test
   (`test_add_guest_username_does_not_collide_with_unclaimed_imported_guest`)
   proving a plus-one can't collide with a pre-imported, never-claimed
   guest.
7. **Who-hasn't-bet** filters to `db.get_logged_in_guest_ids()` (claimed
   guests only), not every guest row — with 20 no-shows in the list it
   would be useless for chasing people. Composed from existing primitives
   (`db.get_guests()` ∩ `db.get_logged_in_guest_ids()`, minus
   `db.fetch_bets_for_race(race_number)`'s guest_ids), no new db.py
   primitive needed. Tested directly
   (`test_who_hasnt_bet_excludes_unclaimed_guests`).

The complete-view secondary actions (Add guest / Unlock / Fix a bet) are
kept visible per the original plan's decision, with one honest caveat: Fix
a bet on the complete view will always hit `BettingClosedError` (no race is
ever OPEN/LOCKED once the event is complete) — a known, harmless dead end,
not a bug, since the mapped message ("Betting is closed for the current
race.") explains why rather than 500ing.

## `_effective_state`

Lives entirely in `app/routers/operator.py`, exactly as planned:
`current_state(now)`'s "lowest not-SETTLED race" contract never reports
"race N, just settled" except for the final race — `_effective_state`
reconstructs that missing window (`_reconstruct_settled_state`, built by
hand from `db.fetch_race`/`get_race_entries`/`get_horses`/`count_live_bets`
for the previous race) whenever the real state is SCHEDULED with a SETTLED
predecessor, guarding on both `race_number > 1` and the predecessor's
status so race 1 pre-open still renders "scheduled". `event_complete` is
checked first, so the gap logic never fires for the final race. The
reconstructed `RaceState` is read-only by construction — nothing in
`races.py` was touched, and no route ever passes it to a write function;
every action route re-derives its own authority from a fresh
`races.current_state(now)` (or, for `/race/correct`, from
`db.get_settled_results()`).

Unit-tested against a real `initialised_db` (not hand-built `RaceState`
values, unlike guest.py's `_classify_state`) — `_effective_state` takes
only `now` and calls `races.current_state(now)` internally, so there's no
seam to inject a hand-built state without monkeypatching `db.fetch_race`
for the gap case; driving real race transitions through the actual
`races.py` service functions is more direct and just as fast at this scale.

## New `db.py` primitives

```python
def clear_guest_device(guest_id: int, *, conn=None) -> None
def fetch_guests() -> list[sqlite3.Row]
```

Both follow the file's existing `conn=None` self-recursive commit pattern.
`clear_guest_device` clears `device_token` only, never `claimed_at` — a
guest whose device is unlocked stays on the leaderboard. Tested that
`claimed_at` survives (`test_unlock_guest_clears_device_token_preserves_claimed_at`).

## Routes, exceptions, templates

Implemented exactly per the corrected plan (see the plan file at
`.claude/plans/read-claude-design-design-doc-md-claude-staged-crescent.md`
for the full route table, per-view action table, and exception-to-message
mapping) — not re-derived here. One implementation note not in the plan:

- **Jinja2 autoescapes apostrophes to `&#39;`.** Several `FLASH_MESSAGES`
  values contain apostrophes ("can't be opened", "isn't open", "doesn't
  exist", ...). This surfaced as 9 initially-failing test assertions that
  checked for a literal `'` — not a production bug, the messages render
  correctly, just HTML-escaped as intended. Fixed by asserting on both the
  literal and escaped forms (`tests/test_operator_routes.py`'s `_contains`
  helper) rather than by disabling escaping, which would reopen exactly
  the XSS surface autoescaping exists to close.

## Templates (functional, not the template pass)

`app/templates/operator/{base,login,confirm,panel}.html` and
`app/templates/operator/partials/{scheduled,open,locked,settled,complete,
_scratch_list,_results_entry,_secondary_actions}.html`. Deliberately no
`app/static/app.js`/HTMX dependency on the operator side this session —
removing `window.confirm()` meant no JS was needed at all for the confirm
step, and the results-entry grid is plain `<select>` dropdowns (scratched
horses rendered `disabled`) rather than the spec's tap-a-position/tap-a-
horse button grid. All manually smoke-tested end to end through every view
(scheduled → open → locked → settled → complete) via a real `TestClient`
walk-through — see "Verification" below.

## Deferred to the template pass

Explicitly NOT done this session, per the user's instruction ("Template
pass to follow separately once this is agreed — do not start it yet"):

- The tap-a-position/tap-a-horse results-entry grid (currently `<select>`
  dropdowns) and duplicate-horse-across-positions disabling (currently
  only enforced server-side via `InvalidResultError`, not visually).
- Per-view template-pass tests asserting invalid actions are **absent**
  from the HTML (not merely disabled) for each of the 5 views.
- Visual polish: scratch checkboxes as an actual checkbox grid rather than
  toggle buttons, a proper guest picker for Fix-a-bet/Unlock instead of
  raw guest-ID number inputs, and general layout work.
- `test_operator_routes.py`'s template-pass test names from the approved
  plan (`test_scheduled_view_shows_only_valid_actions`, the auto-lock/
  scratch-checkbox presence-on-both-views tests, etc.) are not yet
  written — only the "python pass" behavioral tests are.

## Test coverage

312 tests passing total (242 from Steps 1–4a, +70 this session):
`tests/test_operator_auth.py` (13 tests: login, cross-auth isolation in
both directions, cookie shape, password-rotation invalidation, the
corrected rate limiter), `tests/test_operator_routes.py` (~57 tests:
`_effective_state` view selection, every action's transition + redirect,
the exception-to-message mapping — including monkeypatched coverage for
`AnotherRaceOpenError` and `race/open`'s `RaceNotFoundError`, both
genuinely unreachable through the route in legitimate use and documented
as such inline, mirroring guest.py's TOCTOU-branch convention — guest
management, fix-a-bet, who-hasn't-bet filtering, export), and
`tests/test_import_guests.py` (6 tests, run as real subprocesses against a
real sqlite file rather than importing the script as a module, since it's
a CLI tool with its own `sys.path` bootstrapping).

Existing `tests/test_auth.py` and `tests/test_guest_routes.py` fixtures
needed a one-line addition (`monkeypatch.setenv("OPERATOR_PASSWORD", ...)`)
since `Settings.OPERATOR_PASSWORD` is now a required field read at
`lifespan` startup — without it every existing `TestClient(app)` in the
suite would fail before this session's own tests even ran.

## Verification

Full suite: `venv\Scripts\Activate.ps1` then `pytest -q` — 312 passed.
Manually walked a real `TestClient` through every view in order
(scheduled → open → locked → settled → complete, across a 2-race event)
confirming each renders 200 with no Jinja error, since the automated tests
don't happen to hit `GET /operator` while LOCKED or after the final settle.
`scripts/import_guests.py` verified via `test_import_guests.py`'s
subprocess-based dry-run/normal-run/append/collision-report tests — no
separate manual run was needed beyond that. No browser available in this
environment, consistent with 4a's note; a real mobile-browser click-through
is still worth doing once the template pass lands.

## Carry-forward notes for the template pass / Step 5

- `operator/base.html` has no `<script>` tags at all currently — the
  template pass will need to decide whether the tap-to-select grid reuses
  `app/static/app.js` (adding operator-only functions there, guarded inert
  on guest pages like `tickCountdown`'s existing DOM-presence guards) or
  gets its own file. No CDN either way.
- `_results_entry.html`'s `<select>`-based approach already enforces
  "scratched horses not selectable" (via `disabled` on the `<option>`) but
  not "a horse already assigned to another position renders disabled" —
  that needs either JS or restructuring into the real tap-grid.
- The Fix-a-bet and Unlock-a-device forms take a raw numeric `guest_id` —
  fine functionally (tested), but the template pass should replace this
  with a name-based picker; nothing in `_panel_context` currently supplies
  the full guest list to those partials, so that context will need
  extending.
- `FLASH_MESSAGES` and the `_contains`-style apostrophe-escaping gotcha are
  worth keeping in mind for any new operator-facing copy the template pass
  adds — Jinja escapes it, tests must check both forms or avoid the
  apostrophe entirely in their assertions.
