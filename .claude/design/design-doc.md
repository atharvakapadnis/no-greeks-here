# no-greeks-here — design doc

Betting game for a private derby party. ~55 guests, provision for 75. Single
evening, ~10 races. Guests bet from their own phones; one operator runs the
event from a laptop. Read this file before implementing anything.

## Context: what failed last year

The previous version was a Streamlit app with SQLite on an ephemeral container
filesystem. Mid-event the container restarted, the database vanished, and the
app silently re-initialised an empty event instead of failing loudly. Every
durability decision below exists because of that.

Two other inherited bugs worth not repeating: scoring was implemented three
times and the copies drifted, and bets lived in browser session state until the
operator submitted results, so a refresh lost them.

## Non-negotiables

1. The database file must never be the only copy of the data.
2. The app must fail loudly on an empty/missing database, never bootstrap one.
3. Points are calculated in exactly one place: `app/services/scoring.py`.
4. Bets are written to disk the moment they are placed, not at settlement.
5. A guest's screen must never claim a bet is saved before the server confirms.

## Stack

FastAPI + Jinja2 + HTMX, server-rendered. Python stdlib `sqlite3`, raw SQL, no
ORM — there are five tables and an ORM buys nothing. SQLite in WAL mode.
Litestream replicating to S3. Docker + Caddy on a single AWS Lightsail
instance. Small payloads matter: venue wifi is the most likely thing to fail.

## Data model

    guest(id, username, display_name, device_token, claimed_at, created_at)
    horse(number PK, name)
    race(number PK, status, first, second, third,
         opened_at, locked_at, settled_at)
    race_entry(race_number, horse_number, scratched)
    bet(id, race_number, guest_id, horse_number, client_bet_id,
        created_at, superseded_at)
    audit_log(id, at, actor, action, payload_json)

Notes:

- `race_entry` exists so scratching a horse cannot rewrite completed races.
  Default is every horse runs every race.
- `bet` is append-only in its substantive fields. Changing a bet inserts a
  new row and sets `superseded_at` on the old one — the only field ever
  updated on an existing bet row, and only to record when it stopped being
  live. Current bet = latest row with `superseded_at IS NULL` per
  (race, guest); the superseded row itself is recoverable as that guest's
  next bet in the race ordered by `created_at`, so no pointer between the
  two rows is needed.
- `client_bet_id` is a UUID generated on the phone. It is the idempotency key:
  a retried request with the same id is a no-op, never a second bet.
- Types must match the Step 1 pure service dataclasses so the Step 2 DB layer
  needs no translation: all id columns, `horse.number`, `race.number`,
  `race.first`, `race.second`, `race.third`, `bet.horse_number`, and
  `bet.race_number` are all `INTEGER`.

### Module conventions

Modules under `app/services/` are pure: no I/O, no database, no network, no
framework imports. Every function takes plain data in (dataclasses, plain
values) and returns plain data out. The database layer is responsible for
adapting rows to and from these shapes — the services never reach for a
connection themselves.

## Race state machine

    SCHEDULED --open--> OPEN --lock--> LOCKED --settle--> SETTLED
                          ^              |
                          +---reopen-----+

- Bets accepted **only** in OPEN. Acceptance is decided by server state and the
  server clock. Never trust what the client thought the state was.
- At most one race may be OPEN at any time.
- Transitions are operator-only, idempotent, and each writes an `audit_log` row
  and triggers a snapshot.
- `reopen` (LOCKED -> OPEN) and correcting a settled result are both allowed
  and both audited. They are recovery paths, not accidents to prevent.
- Optional auto-lock timer set when opening. Forgetting to lock is the most
  likely operator error.

## Betting rules

- One horse per guest per race. Unlimited free changes until lock.
- Not betting is legal and scores 0. Locking does **not** require all guests to
  have bet — with 55 people someone will always be away from their phone.
- Scratched horses render greyed and untappable, never removed from the grid.
- Guests cannot see other guests' bets at any point. Bets are stored for the
  operator and for dispute resolution only.

## Scoring

3 points for 1st, 2 for 2nd, 1 for 3rd. Only from settled races.

Ranking is **dense**. Distinct score values get consecutive ranks; tied
guests share a rank; the next distinct score takes the immediately
following rank.

    scores    10  9  8  7  7  6
    ranks      1  2  3  4  4  5

This is deliberate and settled. Do NOT change it to either of these:
  - standard competition ranking: 1, 2, 3, 4, 4, 6
  - modified competition ranking: 1, 2, 3, 5, 5, 6
Under modified competition the tied pair would be 5th and 5th, which is
not the required behaviour. An earlier draft of this document contained
a malformed five-element example; that has been fixed. If a future
session believes this section is inconsistent, the example above is
correct and the label "dense" is correct.

The requesting guest's own row is always pinned at the bottom regardless of rank.

Visible rows are the top 10 people, extended to include everyone sharing
the rank at the boundary. Capped at `max_rows` (default 25), because
dense ranking compresses ranks and early in the evening most guests are
tied on zero — without a cap, race 1 would return the whole roster.
Truncation may only ever remove guests from within a single tied group,
never split guests on different scores. `Leaderboard` reports
`truncated_count` and `truncated_points` so the UI can render
"+30 others on 0 points" rather than silently cutting.

Guests appear on the leaderboard only once they have logged in, so ~20
no-shows never clutter the board.

## Identity

Username = first initial + full last name, lowercase. `John Doe` -> `jdoe`.
No password. Guest list is imported ahead of the event.

Collisions extend to the full first name for **both** members of the pair, so
neither is the afterthought. The actual guest list has exactly one collision:

    Carolyn Campbell -> carolyncampbell
    Chris Campbell   -> chriscampbell

Do not switch the global rule to two initials to solve this. Handle collisions
locally.

**First device wins.** The first phone to log in as a username claims it and
stays logged in via a device token cookie. A second device gets "already
claimed, see the organiser." The operator has an unlock action for the genuine
dead-battery case. Plus-ones are registered by the operator mid-event and can
bet on the next race with zeros behind them.

## Guest screens

Two tabs only: Bet and Leaderboard. No search, no filters, no pagination.

The Bet tab has exactly three states, driven by race status:

1. **Waiting** — "nothing to do yet", plus last race's outcome for this guest.
2. **Open** — grid of horse numbers, big tap targets, countdown if auto-lock is
   set. Tap saves immediately. Selected horse is highlighted.
3. **Locked** — their horse, large. Or "you didn't bet this race" if they
   didn't. Silent zero-point rounds read as a broken app.

The Leaderboard tab shows rank, name, total. Nothing else. On settle, a banner
above the table shows the race result and the guest's points from it.

## Operator panel

One screen, all night. It states what is happening and offers only the actions
valid in the current state. No navigation. No destructive actions on screen —
there is no "reset all data" button anywhere.

Per state: `Open betting` (with scratch checkboxes) / `Lock betting` (with live
bet count and a list of who hasn't bet yet) / results entry / `Publish`.

Results entry is **tap, not type**: tap a position, tap a horse; used horses
grey out. This makes duplicate positions and non-existent horses impossible to
enter rather than something to validate. Publishing has one confirm step.

Always available: add guest, unlock a device, fix a bet (works until publish).

Footer shows backup freshness ("backed up 6 seconds ago"). If it goes stale,
the operator's instruction is to carry on and phone the owner.

## Durability and recovery

Three independent layers:

1. Litestream streams the WAL to S3 continuously.
2. A full JSON snapshot to local disk **and** S3 on every open, lock, and
   settle transition.
3. An operator export button producing the same snapshot on demand.

`scripts/entrypoint.sh` boot order, in this order, no exceptions:
restore from S3 if the local DB is missing -> run migrations -> start
`litestream replicate` -> start uvicorn. Getting this wrong means silently
booting an empty database, which is precisely last year's failure.

`/health` reports liveness and Litestream replication freshness. It is what
feeds the operator's backup indicator.

Worst realistic loss is the bets of the currently-open race, which are
recoverable by asking — people remember what they tapped 60 seconds ago.

Paper fallback: the operator has a printed roster. If the network dies entirely
they record bets on paper and the operator keys them in afterwards.

## Client resilience

The most likely way a guest misses a race is their phone locking and the live
connection dying silently. Handle explicitly:

- SSE for state push. Every message is a **complete state snapshot**, not a
  delta, so a phone that missed 20 minutes needs no catch-up.
- On `visibilitychange` to visible: refetch full state and reopen the stream
  before rendering.
- Heartbeat every 15s. No message for 30s -> mark stale, show "reconnecting",
  refetch.
- If SSE can't be established, fall back to polling every 3s. At 75 clients
  that is ~25 req/s, which is nothing.
- Refreshing the page is always safe and always correct — state lives on the
  server. It is the first fix on the operator's card.
- Optimistic UI is fine for the highlight, but the "saved" tick appears only on
  server confirmation.

No web push notifications. They need permission prompts and home-screen
installs on iOS and would fail more often than the operator announcing it out
loud, which is the primary channel anyway.

## Constraints that are easy to get wrong

- **One uvicorn worker.** SSE subscribers live in process memory, so multiple
  workers would each reach only a fraction of the room. It also keeps SQLite to
  a single writer. 75 guests leaves compute to spare.
- Dense ranking, not competition ranking.
- Bets are substantively append-only: `superseded_at` is the only column
  ever updated on an existing bet row (to mark it no longer live). Every
  other field is immutable after insert.
- Raw SQL, not an ORM.
- No credentials in source. Everything via env vars.
- Total races is configurable (default 10) but cannot be reduced below the
  number already settled.

## Capacity

75 guests x 10 races = 750 bet writes for the whole evening. Peak is roughly
4 writes/sec if everyone bets in the last 20 seconds. This is a small app; do
not build for scale it will never see.

## Build order

1. `services/guests.py` + `services/scoring.py` with tests. Pure functions, no
   DB, no AWS. Username collisions and dense ranking settled here.
2. `db.py` + `migrations/001_initial.sql`.
3. `services/races.py` + `services/bets.py` with tests. The state machine is
   where correctness lives.
4a. Guest-facing routers and templates: `app/config.py`, `app/auth.py`,
    `app/routers/guest.py`, `app/templates/guest/*`, `app/static/`,
    `scripts/init_event.py`. Login, the bet screen, the leaderboard.
4b. Operator-facing routers and templates: race control panel (open/lock/
    settle/scratch), results entry, add guest, unlock device.
5. SSE and client resilience in `static/app.js`.
6. Docker, Litestream, Lightsail. Then rehearse a restore twice before the
   event.

Steps 1-3 need no cloud account open.

## Out of scope

Multi-event support. User accounts across events. Live odds or stakes. Money.
Historical stats beyond this evening. Anything requiring the guest to install
something.