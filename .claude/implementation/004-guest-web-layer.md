# 004 — Guest-facing web layer (Step 4a)

2026-07-31

## What was built and why

`app/config.py`, `app/auth.py`, `app/templating.py`, `app/routers/guest.py`,
`app/main.py` (expanded from the health-only stub), `app/templates/*`,
`app/static/app.css`, `app/static/htmx.min.js` (vendored), `app/services/clock.py`,
`scripts/init_event.py`, plus one new `app/db.py` primitive
(`claim_guest_device`) and a guard retrofitted onto every `now`-taking
function in `races.py`/`bets.py`. This is Step 4 of the design doc's build
order, split into 4a (this session, guest side) and 4b (operator panel,
next session) — the design doc and tracker's build-order sections were
updated to reflect the split.

Every route handler in `app/routers/guest.py`, and the `current_guest`/
`require_guest` dependencies in `app/auth.py`, are plain `def`, never
`async def` — `app.db` is blocking stdlib sqlite3, and FastAPI runs sync
handlers in a threadpool, so one guest's DB call never stalls the single
event loop for the other 74. Step 5's SSE endpoint will be the one
legitimate `async def` in the app, and it must not do blocking DB work
inline either.

## Timezone guard

New `app/services/clock.py`: `require_aware(now)` raises `ValueError` if
`now.tzinfo is None`. Called as the first line of every public `now`-taking
function in `races.py` (`open_race`, `lock_race`, `settle_race`,
`reopen_race`, `correct_result`, `set_scratched`, `apply_auto_lock`,
`current_state`) and `bets.py` (`place_bet`, `operator_set_bet`) — 10
functions total, one guard, no duplication. All existing Step 3 tests
already passed timezone-aware datetimes, so this was purely additive; 11
new tests (one per guarded function) assert the naive-datetime rejection in
`test_races.py`/`test_bets.py`.

## `app/config.py`

`pydantic-settings`. `get_settings()` builds a fresh `Settings()` on every
call rather than caching one at import time — same philosophy as `db.py`
reading `DATABASE_PATH` from the environment lazily. This is what lets
tests set env vars per-test (`monkeypatch.setenv` + a fresh `tmp_path` DB)
without any cache-invalidation machinery. `SECRET_KEY` and `DATABASE_PATH`
have no defaults, so a missing one raises `pydantic.ValidationError`
immediately. `COOKIE_SECURE` is `bool | None`, defaulting to `None`
("unset"); the `cookie_secure` property resolves it to `True` unless
`ENV == "dev"`, with an explicit env value always winning.

`app/main.py`'s `lifespan` calls `get_settings()` then `db.verify_ready()`
on startup — both un-cached, so a mis-configured or empty/un-migrated
database fails loudly there, with the specific exception, before the app
ever serves a request. `test_startup_fails_loudly_when_verify_ready_raises`
confirms entering `TestClient(app)`'s context on an un-migrated DB raises.

## `app/auth.py`

Cookie name `ngh_auth`; value is `device_token` signed with
`itsdangerous.URLSafeSerializer` (salt `"guest-device-token"`), not
timed — the 30-day expiry is the cookie's own `max_age`, not a signature
deadline. `HttpOnly`, `SameSite=Lax`, `Secure=settings.cookie_secure`.

`current_guest(request) -> sqlite3.Row | None` is the *only* definition of
"is this device logged in" in the whole app: no cookie, a bad signature, or
a well-signed token no longer in `guest.device_token` (e.g. after an
operator unlock in 4b) all resolve to `None`. It never inspects
`claimed_at` — per the Step 3 carry-forward, those mean different things.

`require_guest` wraps it and raises `GuestLoginRequired` when `None`.
`app/main.py` registers an exception handler for that exception that
returns a 303 to `/login`. This is what makes every protected route redirect
on a missing/bad guest without a raw 500 and without repeating the same
`if guest is None` check seven times.

New `db.py` primitive, `claim_guest_device(guest_id, device_token,
claimed_at, *, conn=None)`: a single conditional `UPDATE ... WHERE id = ?
AND device_token IS NULL`, `claimed_at = COALESCE(claimed_at, ?)`. Returns
`True` iff this call performed the claim. SQLite serializes concurrent
writers on the same row, so of two threads racing this for the same
`guest_id`, exactly one gets `True` — verified with a real
`threading.Thread` pair in
`test_login_concurrent_claim_exactly_one_succeeds`, not a mock.

## `app/routers/guest.py`

Seven routes: `GET /`, `GET /login`, `POST /login`, `GET /bet`,
`POST /bet`, `GET /leaderboard`, `GET /state`.

`POST /login`'s claim flow matches the design doc's first-device-wins
sequence exactly: unknown username -> organiser message; unclaimed guest ->
`claim_guest_device`, and on success set cookie + redirect (on losing the
race, re-fetch and fall through); claimed guest whose cookie's unsigned
token matches -> redirect (already logged in); otherwise -> "already
claimed" message. `race_number` for `POST /bet` is never read from the
client — it's `races.current_race_number()`, computed server-side, so a
stale form can't target the wrong race.

**`_classify_state(state: races.RaceState) -> str`** is a small pure
function (no DB access) holding the four-way render-state decision,
deliberately factored out of `_bet_screen_context` so it can be
unit-tested against a hand-built `RaceState` independent of whether
`apply_auto_lock` has already run:

```python
if state.event_complete: return "complete"
if state.status == "LOCKED": return "locked"
if state.status == "OPEN":
    if state.seconds_to_auto_lock is not None and state.seconds_to_auto_lock <= 0:
        return "locked"
    return "open"
return "waiting"  # SCHEDULED
```

**This was a real bug caught in plan review, not a hypothetical one**: the
original plan compared `seconds_to_auto_lock <= 0` unguarded, and that
value is `None` whenever a race is opened without an auto-lock timer (the
normal manual-lock path) — every guest's screen would have raised
`TypeError` the moment an operator opened a race without a countdown.
`test_classify_state_open_with_no_auto_lock_is_open_not_error` and its
future-auto-lock and expired-auto-lock siblings pin this down directly
against constructed `RaceState` values, and
`test_open_state_with_no_auto_lock_renders_picker_not_error` re-confirms it
through the real route.

Note that `_bet_screen_context` calls `races.apply_auto_lock(now)` before
`current_state`, so via the real HTTP routes an expired-but-nominally-OPEN
race is always already flipped to `LOCKED` by the time `_classify_state`
sees it — the "OPEN but expired" branch is unreachable through the router
today and exists as defense-in-depth per the design doc's explicit
instruction ("apply_auto_lock may not have run"). It's the
`_classify_state`-level unit tests, not an HTTP-level test, that actually
exercise that branch.

Exception mapping in `POST /bet` (guest never sees a raw error):

| Exception(s) | Guest sees | Action |
|---|---|---|
| `races.RaceNotFoundError`, `bets.BettingClosedError` | "Betting is closed" | re-render current state |
| `bets.HorseNotInRaceError` | "That horse isn't running" | re-render current state |
| `bets.GuestNotFoundError`, `bets.GuestNotLoggedInError` | — | redirect `/login` |
| `scoring.build_leaderboard`'s `ValueError` (in `GET /leaderboard`) | — | redirect `/login` |

Every branch logs the specific exception even where the guest-facing
message is shared.

## Templates and CSS

Server-rendered Jinja2 + HTMX, no build step. `app/templating.py` holds one
shared `Jinja2Templates` instance (kept out of `main.py` to avoid a circular
import with the routers). Four partials
(`guest/partials/{waiting,open,locked,complete}.html`) are rendered
directly by `GET /state` and `POST /bet`, and included by `guest/bet.html`
for the initial page load — one template per state, not a big
`{% if %}` chain in a single file.

**HTMX is vendored at `app/static/htmx.min.js`** (`unpkg.com/htmx.org@1.9.12/dist/htmx.min.js`,
downloaded and pinned, version string verified in the file), not loaded
from a CDN — per plan-review feedback, the design doc's stated most-likely
failure mode is the venue network, and a `<script src="https://...">` on
the critical path would leave every guest with a dead page if that CDN is
slow or blocked.

The horse grid uses `hx-vals='js:{"horse_number": N, "client_bet_id":
crypto.randomUUID()}'` so each tap generates its own idempotency key
client-side with no custom JS file needed. `guest/bet.html`'s poll
container:

```html
<div id="bet-state" hx-get="/state" hx-trigger="every 3s" hx-swap="innerHTML">
```

with an inline comment marking it as Step 5's SSE replacement point
(push + visibility resync + heartbeat + this same polling loop kept only
as the fallback).

Palette (`app/static/app.css`, concrete values, no undefined custom
properties): `--color-bg:#14161c`, `--color-surface:#1e212b`,
`--color-surface-alt:#262a36`, `--color-text:#f1f3f8`,
`--color-text-muted:#9aa1b2`, `--color-accent:#ffb020`,
`--color-accent-text:#14161c`, `--color-scratched:#4a4f5c`,
`--color-danger:#e5484d`, `--color-border:#2f3340`. Horse grid buttons are
56px tall (min 48px enforced), scratched horses render greyed with
`pointer-events:none` (never removed from the grid).

## `scripts/init_event.py`

`argparse` CLI: `--horses` (default 6), `--races` (default 10). Runs
migrations, then `db.initialise_event`; catches
`db.EventAlreadyInitialisedError` and refuses a second run (exit code 1,
no further DB touch). Manually verified: first run initialises and prints
a summary, second run against the same `DATABASE_PATH` refuses cleanly.

## Test coverage summary

230 tests passing total: 187 from Steps 1-3, plus 11 naive-datetime guard
tests (`test_races.py`/`test_bets.py`), 10 in the new `test_auth.py`, and
22 in the new `test_guest_routes.py`. Six of the 22 are pure
`_classify_state` unit tests (no DB, no HTTP) pinning down the auto-lock
edge cases directly.

Notable cases beyond the obvious happy paths: concurrent claim via two real
`threading.Thread`s (not mocked) resolves to exactly one winner; a tampered
cookie, a well-signed cookie for a token that no longer exists, and no
cookie at all all redirect to `/login` with no 500; a stale
`client_bet_id` retry for a since-superseded horse renders the *stored*
horse, not the one the retry submitted; a guest whose `device_token`
resolves but whose `claimed_at` is `NULL` (a deliberately corrupted test
state) hits `build_leaderboard`'s `ValueError` and redirects rather than
500ing; the leaderboard truncation banner renders under a 30-guest tie.

## Carry-forward notes for Step 4b / 5

- At 75 guests polling `GET /state` every 3s, each request opens roughly
  six separate SQLite connections (`apply_auto_lock`, `current_state`'s
  four reads, `get_live_bet`). Fine at this scale, and Step 5's SSE removes
  most of it — recorded so it's a measured decision, not an unexamined one.
- Every route handler must stay a plain `def`; only Step 5's SSE endpoint
  should be `async def`, and it must not do blocking DB work inline.
- `get_settings()`/`db.verify_ready()` live in `app/main.py`'s `lifespan`,
  uncached. Any new startup check belongs there too, not a module-level
  singleton or a new `@app.on_event`.
- `_classify_state` is guest-bet-screen-specific. The operator panel needs
  its own equivalent (it has different states: scratch checkboxes,
  who-hasn't-bet, results entry) rather than branching on this one.
- Operator unlock (clearing `guest.device_token`) is Step 4b's job; nothing
  in this session clears it, and `current_guest` will correctly stop
  resolving a guest the moment it's cleared (already covered by
  `test_protected_route_redirects_to_login_when_token_no_longer_exists`,
  which simulates the post-unlock state directly).
- No browser was available in this environment to click through the app;
  verification was end-to-end via `TestClient` (which exercises the same
  ASGI app, lifespan included) plus a manual run of `scripts/init_event.py`.
  A real mobile-browser pass (tap targets, HTMX swap behavior, viewport)
  is still worth doing before the event.
