# 001 — Pure service modules

2026-07-30

## What was built and why this came first

`app/services/guests.py` (username generation and collision resolution) and
`app/services/scoring.py` (points and leaderboard) — Step 1 of the design
doc's build order, plus `tests/test_guests.py` and `tests/test_scoring.py`.

This came first because it's the part of the app where correctness is
easiest to get wrong and cheapest to verify in isolation: username collision
handling and dense ranking are both fiddly, both explicitly called out as
having failed or drifted in last year's version, and both are pure logic
with no dependency on a database or web framework. Settling them here, fully
tested, before `db.py` or FastAPI exist means the DB layer in Step 2 has a
fixed, trusted contract to adapt rows into rather than a moving target.

Both modules are pure: no I/O, no database, no network, no framework
imports, standard library only. Every function takes plain data in and
returns plain data out. This document is written so a later session can call
these modules correctly from the API surface below alone, without opening
the source.

## `app/services/guests.py` — full public API

### Dataclasses

```python
@dataclass(frozen=True)
class GuestName:
    first_name: str
    last_name: str

@dataclass(frozen=True)
class UsernameResult:
    guest: GuestName
    username: str

@dataclass(frozen=True)
class ExtendedName:
    guest: GuestName
    short_username: str    # the base form that collided
    final_username: str    # the extended (and possibly integer-suffixed) form assigned

@dataclass(frozen=True)
class BulkAssignmentResult:
    usernames: list[UsernameResult]   # one per input guest, same order as input
    extended: list[ExtendedName]      # every guest bumped past the base form
```

### Functions

```python
def base_username(first_name: str, last_name: str) -> str
```
First initial + full last name, lowercased and normalised (accents
stripped, apostrophes/hyphens/spaces/periods removed). Falls back to
whichever of `first_name`/`last_name` is non-empty if only one is given
(e.g. single-word names). Raises `ValueError` if both are empty/
whitespace-only, or the result normalises to an empty string.

```python
def full_username(first_name: str, last_name: str) -> str
```
Full first name + full last name, same normalisation. This is the extended/
collision form. Same emptiness validation as `base_username`.

```python
def split_full_name(full_name: str) -> GuestName
```
Best-effort split for the operator's single-box "add guest" form: last
whitespace token = `last_name`, everything before it = `first_name` (empty
string for single-word names like "Madonna"). Raises `ValueError` if
`full_name` is empty or whitespace-only. Not used on the bulk import path —
bulk import gets `first_name`/`last_name` as separate columns directly.

```python
def assign_usernames(guests: list[GuestName]) -> BulkAssignmentResult
```
Bulk import, before the event. Any `base_username` shared by more than one
guest promotes **every** guest in that group to `full_username` — never
just one side. If extended forms still collide (two guests with an
identical full name), the smallest integer ≥ 2 is appended, resolved
deterministically in input order. Guests with no collision keep the base
form.

```python
def add_guest_username(new_guest: GuestName, existing_usernames: set[str]) -> str
```
Incremental, during the event. `existing_usernames` is never mutated or
renamed. Only the new guest is bumped to `full_username` (then
integer-suffixed if still colliding) if their base form is already taken.

## `app/services/scoring.py` — full public API

### Dataclasses

`guest_id` and `race_number` are `int`, matching the DB schema's integer
primary keys (`guest.id`, `race.number`) — see the carry-forward note below.

```python
@dataclass(frozen=True)
class RaceResult:
    race_number: int
    first: int   # horse number
    second: int
    third: int

@dataclass(frozen=True)
class Bet:
    guest_id: int
    race_number: int
    horse_number: int

@dataclass(frozen=True)
class GuestInfo:
    guest_id: int
    display_name: str

@dataclass(frozen=True)
class LeaderboardRow:
    guest_id: int
    display_name: str
    total_points: int
    rank: int

@dataclass(frozen=True)
class Leaderboard:
    rows: list[LeaderboardRow]              # visible rows, rank asc then name asc
    requesting_guest: LeaderboardRow | None  # None iff requesting_guest_id was None
    requesting_guest_in_rows: bool           # always False when requesting_guest is None
    truncated_count: int                     # guests omitted from the boundary tie group, 0 if none
    truncated_points: int | None             # the point total those omitted guests share, else None
```

This module has no concept of race *status*. `results` mappings passed in
must already be settled-only races — that's how "only settled races count"
is enforced, without importing the state machine from `races.py` (Step 3).

### Functions

```python
def points_for_bet(horse_number: int | None, result: RaceResult) -> int
```
3/2/1/0 for 1st/2nd/3rd/other. `horse_number=None` (no bet placed) scores 0
— a normal state, not an error.

```python
def total_points_by_guest(
    bets: Iterable[Bet], results: Mapping[int, RaceResult]
) -> dict[int, int]
```
Sums points per `guest_id` across bets whose `race_number` is a key in
`results` (settled races only; bets for races absent from `results` are
ignored). Guests with zero bets are absent from the returned dict — treat a
missing key as 0.

```python
def dense_rank_guests(
    guests: Iterable[GuestInfo], totals: Mapping[int, int]
) -> list[LeaderboardRow]
```
Ranks ALL given guests (missing totals default to 0). Sort key:
`total_points` desc, then `display_name` asc — stable and deterministic
across calls. True dense ranking: distinct score values get consecutive
ranks, tied guests share a rank, the next distinct score takes the
immediately following rank (no gaps). Scores `10,9,8,7,7,6` → ranks
`1,2,3,4,4,5`.

```python
def build_leaderboard(
    guests: Iterable[GuestInfo],
    logged_in_guest_ids: set[int],
    bets: Iterable[Bet],
    results: Mapping[int, RaceResult],
    requesting_guest_id: int | None = None,
    top_n: int = 10,
    max_rows: int = 25,
) -> Leaderboard
```
Ranks only `logged_in_guest_ids` (via `dense_rank_guests`). Visible rows are
the top `top_n` by position, extended to include every guest sharing the
rank at that boundary (a 4-way tie at the cutoff yields 13 rows, not 10) —
unless that extension would exceed `max_rows`, in which case the boundary
tie group only (every guest with a strictly better score is always kept in
full) is truncated to however many slots remain. `truncated_count`/
`truncated_points` report what was cut.

`requesting_guest_id=None` is a legitimate, expected input (operator panel,
public scoreboard, end-of-night export) and returns
`requesting_guest=None`, `requesting_guest_in_rows=False` — never an error.
When given, the requester's row is always resolved and returned (correct
rank/total) even if it fell inside a truncated portion of the boundary
group.

Raises `ValueError` if `requesting_guest_id` is given but not found among
`guests`, or found but not present in `logged_in_guest_ids`. A guest can
only reach this code path with a valid device token, so a failed lookup
means a bug upstream, not a normal user state to handle gracefully — see
the open question logged in the tracker for what Step 4's router must do
with this.

## The bulk-vs-incremental collision asymmetry

`assign_usernames` and `add_guest_username` are deliberately two separate
functions with different behaviour, not one function with a mode flag:

- **Bulk (`assign_usernames`)** runs before the event, on the full guest
  list at once. When two guests collide it extends **both** to the full
  first+last form — neither one is treated as the "original" who gets to
  keep the short username while the other is bumped.
- **Incremental (`add_guest_username`)** runs during the event, one guest
  at a time, as plus-ones are registered. Here, **existing usernames are
  immutable**: a guest who has already logged in on their phone (and is
  holding a device-token cookie for e.g. `ccampbell`) cannot be silently
  renamed to `carolyncampbell` just because a second Campbell shows up
  later. Renaming would either break their session or require
  re-authenticating someone mid-event over a collision they didn't cause.
  So the new arrival alone takes the extended form, and everyone already
  registered keeps exactly what they have.

Unifying these into one function would mean either bulk import loses the
"extend both" fairness rule, or incremental registration starts renaming
guests who are already logged in — both are explicitly wrong per the design
doc. Keep them separate.

## Test coverage summary

54 tests passing (`tests/test_guests.py` + `tests/test_scoring.py`), run via
`venv\Scripts\Activate.ps1` then `pytest` from the repo root.

Required cases locked in:
- Carolyn Campbell / Chris Campbell resolving to `carolyncampbell` /
  `chriscampbell` under bulk import
  (`test_assign_usernames_campbell_pair_resolves_to_full_names`).
- A plus-one Chris Campbell arriving mid-event does not rename the existing
  `ccampbell`
  (`test_add_guest_username_collision_extends_new_guest_and_leaves_existing_untouched`).
- Dense ranking producing `1,2,3,4,4,5`
  (`test_dense_rank_produces_1_2_3_4_4_5`).
- A tie spanning the top-10 boundary returning 13 rows
  (`test_build_leaderboard_tie_spanning_top_10_boundary_returns_more_than_10_rows`).
- Boundary-tie truncation at `max_rows`, reporting `truncated_count`/
  `truncated_points`, and never splitting guests on different scores
  (`test_build_leaderboard_boundary_tie_larger_than_max_rows_truncates_and_reports_counts`,
  `test_build_leaderboard_truncation_never_splits_guests_with_different_totals`).
- The requesting guest pinned correctly both when merely outside the top 10
  and when inside a truncated portion of the boundary group
  (`test_build_leaderboard_requesting_guest_pinned_outside_top_10`,
  `test_build_leaderboard_requesting_guest_pinned_correctly_when_truncated`).
- Accented, hyphenated, apostrophed, and multi-word-surname names
  normalising correctly, plus empty/whitespace-only inputs raising
  `ValueError`.

## Carry-forward note for Step 2

`horse.number`, `race.number`, `race.first`, `race.second`, `race.third`,
`bet.horse_number`, `bet.race_number`, and all id columns must be `INTEGER`
in `migrations/001_initial.sql`. The scoring dataclasses above are typed
`int` throughout specifically so `db.py` can hand rows to these functions
with no translation layer.
