"""Points calculation and leaderboard construction.

Pure functions only: no I/O, no database, no network. Standard library only.

This module has no concept of race *status*. Callers must only pass
already-settled races in `results` — that is how "only settled races
count" is enforced, without this module needing to import the race state
machine. Points are calculated in exactly one place: here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

FIRST_PLACE_POINTS = 3
SECOND_PLACE_POINTS = 2
THIRD_PLACE_POINTS = 1


@dataclass(frozen=True)
class RaceResult:
    race_number: int
    first: int
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
    rows: list[LeaderboardRow]
    requesting_guest: LeaderboardRow | None
    requesting_guest_in_rows: bool
    truncated_count: int
    truncated_points: int | None


def points_for_bet(horse_number: int | None, result: RaceResult) -> int:
    """3/2/1/0 points for 1st/2nd/3rd/other.

    horse_number=None (no bet placed) scores 0 — a normal state, not an
    error.
    """
    if horse_number is None:
        return 0
    if horse_number == result.first:
        return FIRST_PLACE_POINTS
    if horse_number == result.second:
        return SECOND_PLACE_POINTS
    if horse_number == result.third:
        return THIRD_PLACE_POINTS
    return 0


def total_points_by_guest(
    bets: Iterable[Bet], results: Mapping[int, RaceResult]
) -> dict[int, int]:
    """Sums points per guest_id across bets whose race_number is a key in
    results (settled races only; bets for races absent from results are
    ignored). Guests with zero bets are absent from the returned dict —
    treat a missing key as 0.
    """
    totals: dict[int, int] = {}
    for bet in bets:
        result = results.get(bet.race_number)
        if result is None:
            continue
        totals[bet.guest_id] = totals.get(bet.guest_id, 0) + points_for_bet(
            bet.horse_number, result
        )
    return totals


def dense_rank_guests(
    guests: Iterable[GuestInfo], totals: Mapping[int, int]
) -> list[LeaderboardRow]:
    """Ranks ALL given guests (missing totals default to 0).

    Sort key: total_points desc, then display_name asc (stable and
    deterministic across calls). True dense ranking: distinct score values
    get consecutive ranks, tied guests share a rank, and the next distinct
    score takes the immediately following rank — no gaps. E.g. scores
    10,9,8,7,7,6 produce ranks 1,2,3,4,4,5.
    """
    scored = sorted(
        ((g.guest_id, g.display_name, totals.get(g.guest_id, 0)) for g in guests),
        key=lambda t: (-t[2], t[1]),
    )
    rows: list[LeaderboardRow] = []
    rank = 0
    prev_total: int | None = None
    for guest_id, display_name, total in scored:
        if prev_total is None or total != prev_total:
            rank += 1
            prev_total = total
        rows.append(
            LeaderboardRow(
                guest_id=guest_id,
                display_name=display_name,
                total_points=total,
                rank=rank,
            )
        )
    return rows


def build_leaderboard(
    guests: Iterable[GuestInfo],
    logged_in_guest_ids: set[int],
    bets: Iterable[Bet],
    results: Mapping[int, RaceResult],
    requesting_guest_id: int | None = None,
    top_n: int = 10,
    max_rows: int = 25,
) -> Leaderboard:
    """Builds the visible leaderboard plus the requesting guest's pinned row.

    Ranks only logged_in_guest_ids (via dense_rank_guests). Visible rows
    are the top top_n by position, extended to include every guest sharing
    the rank at that boundary — unless that extension would exceed
    max_rows, in which case the boundary tie group (and only that group;
    every guest with a strictly better score is always kept in full) is
    truncated down to however many slots remain. truncated_count/
    truncated_points report what was cut so the UI can render e.g. '+30
    others on 0 points' instead of silently dropping people.

    requesting_guest_id=None is a legitimate, expected input (operator
    panel, public scoreboard, end-of-night export) and returns
    requesting_guest=None, requesting_guest_in_rows=False, never an error.
    When given, the requester's row is always resolved and returned (with
    correct rank/total) even if it fell inside a truncated portion of the
    boundary group.

    Raises ValueError if requesting_guest_id is given but not found among
    `guests`, or found but not present in `logged_in_guest_ids` — a guest
    can only reach this code path with a valid device token, so a failed
    lookup means a bug upstream, not a normal user state.
    """
    guests = list(guests)
    logged_in_guests = [g for g in guests if g.guest_id in logged_in_guest_ids]

    if requesting_guest_id is not None:
        known_guest_ids = {g.guest_id for g in guests}
        if requesting_guest_id not in known_guest_ids:
            raise ValueError(
                f"requesting guest {requesting_guest_id!r} not found in guests"
            )
        if requesting_guest_id not in logged_in_guest_ids:
            raise ValueError(
                f"requesting guest {requesting_guest_id!r} has not logged in"
            )

    totals = total_points_by_guest(bets, results)
    all_rows = dense_rank_guests(logged_in_guests, totals)
    row_by_id = {r.guest_id: r for r in all_rows}
    requesting_row = (
        row_by_id[requesting_guest_id] if requesting_guest_id is not None else None
    )

    if len(all_rows) <= top_n:
        visible = all_rows
        truncated_count = 0
        truncated_points: int | None = None
    else:
        boundary_rank = all_rows[top_n - 1].rank
        before_boundary = [r for r in all_rows if r.rank < boundary_rank]
        boundary_group = [r for r in all_rows if r.rank == boundary_rank]
        if len(before_boundary) + len(boundary_group) <= max_rows:
            visible = before_boundary + boundary_group
            truncated_count = 0
            truncated_points = None
        else:
            slots_left = max(max_rows - len(before_boundary), 0)
            visible = before_boundary + boundary_group[:slots_left]
            truncated_count = len(boundary_group) - slots_left
            truncated_points = boundary_group[0].total_points

    requesting_guest_in_rows = any(
        r.guest_id == requesting_guest_id for r in visible
    ) if requesting_guest_id is not None else False

    return Leaderboard(
        rows=visible,
        requesting_guest=requesting_row,
        requesting_guest_in_rows=requesting_guest_in_rows,
        truncated_count=truncated_count,
        truncated_points=truncated_points,
    )
