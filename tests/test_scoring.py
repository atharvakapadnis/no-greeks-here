import pytest

from app.services.scoring import (
    Bet,
    GuestInfo,
    RaceResult,
    build_leaderboard,
    dense_rank_guests,
    points_for_bet,
    total_points_by_guest,
)

RESULT = RaceResult(race_number=1, first=10, second=20, third=30)


# --- points_for_bet --------------------------------------------------------


def test_points_for_bet_first_place():
    assert points_for_bet(10, RESULT) == 3


def test_points_for_bet_second_place():
    assert points_for_bet(20, RESULT) == 2


def test_points_for_bet_third_place():
    assert points_for_bet(30, RESULT) == 1


def test_points_for_bet_no_placement():
    assert points_for_bet(40, RESULT) == 0


def test_points_for_bet_none_horse_number_scores_zero():
    assert points_for_bet(None, RESULT) == 0


# --- total_points_by_guest --------------------------------------------------


def test_total_points_by_guest_single_race():
    results = {1: RESULT}
    bets = [Bet(guest_id=1, race_number=1, horse_number=10)]
    assert total_points_by_guest(bets, results) == {1: 3}


def test_total_points_by_guest_multiple_races_accumulates():
    results = {
        1: RaceResult(race_number=1, first=10, second=20, third=30),
        2: RaceResult(race_number=2, first=10, second=20, third=30),
    }
    bets = [
        Bet(guest_id=1, race_number=1, horse_number=10),  # 3
        Bet(guest_id=1, race_number=2, horse_number=20),  # 2
    ]
    assert total_points_by_guest(bets, results) == {1: 5}


def test_total_points_by_guest_ignores_races_absent_from_results():
    results = {1: RESULT}  # race 2 not settled / not present
    bets = [
        Bet(guest_id=1, race_number=1, horse_number=10),  # 3
        Bet(guest_id=1, race_number=2, horse_number=10),  # ignored
    ]
    assert total_points_by_guest(bets, results) == {1: 3}


def test_total_points_by_guest_guest_with_no_bets_absent_from_dict():
    results = {1: RESULT}
    bets = [Bet(guest_id=1, race_number=1, horse_number=10)]
    totals = total_points_by_guest(bets, results)
    assert 2 not in totals


# --- dense_rank_guests -------------------------------------------------------


def test_dense_rank_produces_1_2_3_4_4_5():
    guests = [GuestInfo(i, name) for i, name in enumerate("ABCDEF", start=1)]
    totals = {1: 10, 2: 9, 3: 8, 4: 7, 5: 7, 6: 6}
    rows = dense_rank_guests(guests, totals)
    assert [r.total_points for r in rows] == [10, 9, 8, 7, 7, 6]
    assert [r.rank for r in rows] == [1, 2, 3, 4, 4, 5]


def test_dense_rank_ties_sorted_alphabetically_by_display_name():
    guests = [
        GuestInfo(guest_id=1, display_name="Zoe"),
        GuestInfo(guest_id=2, display_name="Amy"),
    ]
    totals = {1: 5, 2: 5}
    rows = dense_rank_guests(guests, totals)
    assert [r.display_name for r in rows] == ["Amy", "Zoe"]
    assert [r.rank for r in rows] == [1, 1]


def test_dense_rank_stable_across_repeated_calls():
    guests = [GuestInfo(i, name) for i, name in enumerate("ABCDE", start=1)]
    totals = {1: 3, 2: 3, 3: 2, 4: 1, 5: 1}
    first = dense_rank_guests(guests, totals)
    second = dense_rank_guests(guests, totals)
    assert first == second


def test_dense_rank_empty_guest_list():
    assert dense_rank_guests([], {}) == []


def test_dense_rank_all_zero_points_ties_everyone_at_rank_1():
    guests = [GuestInfo(i, name) for i, name in enumerate("ABCDE", start=1)]
    rows = dense_rank_guests(guests, {})
    assert [r.rank for r in rows] == [1, 1, 1, 1, 1]
    assert [r.display_name for r in rows] == ["A", "B", "C", "D", "E"]


# --- build_leaderboard helpers ----------------------------------------------


def _distinct_totals_scenario(n: int) -> tuple[list[GuestInfo], list[Bet], dict[int, RaceResult]]:
    """n guests (ids 1..n), each with a distinct total of 3*i points, built
    from n races each won by horse 1 (guest i bets the winner in races
    1..i, so guest i's total is 3*i and all totals are distinct)."""
    guests = [GuestInfo(i, f"Guest{i:02d}") for i in range(1, n + 1)]
    results = {
        k: RaceResult(race_number=k, first=1, second=2, third=3)
        for k in range(1, n + 1)
    }
    bets = [
        Bet(guest_id=i, race_number=k, horse_number=1)
        for i in range(1, n + 1)
        for k in range(1, i + 1)
    ]
    return guests, bets, results


def _tied_zero_guests(start: int, count: int) -> list[GuestInfo]:
    return [GuestInfo(i, f"Guest{i:02d}") for i in range(start, start + count)]


# --- build_leaderboard --------------------------------------------------------


def test_build_leaderboard_top_10_no_ties():
    guests, bets, results = _distinct_totals_scenario(10)
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 11)),
        bets=bets,
        results=results,
    )
    assert len(board.rows) == 10
    assert [r.rank for r in board.rows] == list(range(1, 11))
    # guest10 has the highest total (30), so it's rank 1
    assert board.rows[0].guest_id == 10
    assert board.truncated_count == 0
    assert board.truncated_points is None


def test_build_leaderboard_tie_spanning_top_10_boundary_returns_more_than_10_rows():
    guests, bets, results = _distinct_totals_scenario(9)
    tied = _tied_zero_guests(10, 4)  # 4 guests tied at 0 points, no bets
    guests = guests + tied
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 14)),
        bets=bets,
        results=results,
    )
    assert len(board.rows) == 13
    assert board.rows[-1].rank == 10
    assert sum(1 for r in board.rows if r.rank == 10) == 4
    assert board.truncated_count == 0
    assert board.truncated_points is None


def test_build_leaderboard_requesting_guest_pinned_outside_top_10():
    guests, bets, results = _distinct_totals_scenario(11)
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 12)),
        bets=bets,
        results=results,
        requesting_guest_id=1,  # lowest total (3), rank 11
    )
    assert len(board.rows) == 10
    assert board.requesting_guest_in_rows is False
    assert board.requesting_guest is not None
    assert board.requesting_guest.guest_id == 1
    assert board.requesting_guest.rank == 11


def test_build_leaderboard_requesting_guest_in_rows_flag_true_when_visible():
    guests, bets, results = _distinct_totals_scenario(10)
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 11)),
        bets=bets,
        results=results,
        requesting_guest_id=10,  # rank 1, definitely visible
    )
    assert board.requesting_guest_in_rows is True
    assert board.requesting_guest.guest_id == 10


def test_build_leaderboard_no_settled_races_everyone_zero():
    guests = [GuestInfo(i, f"Guest{i:02d}") for i in range(1, 6)]
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids={1, 2, 3, 4, 5},
        bets=[],
        results={},
        requesting_guest_id=1,
    )
    assert len(board.rows) == 5
    assert all(r.total_points == 0 for r in board.rows)
    assert all(r.rank == 1 for r in board.rows)
    assert board.requesting_guest_in_rows is True


def test_build_leaderboard_only_requesting_guest_logged_in():
    guests = [GuestInfo(i, f"Guest{i:02d}") for i in range(1, 6)]
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids={1},
        bets=[],
        results={},
        requesting_guest_id=1,
    )
    assert len(board.rows) == 1
    assert board.rows[0].guest_id == 1
    assert board.requesting_guest_in_rows is True


def test_build_leaderboard_requesting_guest_id_none_returns_rows_with_no_pinned_row():
    guests, bets, results = _distinct_totals_scenario(5)
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 6)),
        bets=bets,
        results=results,
        requesting_guest_id=None,
    )
    assert board.requesting_guest is None
    assert board.requesting_guest_in_rows is False
    assert len(board.rows) == 5


def test_build_leaderboard_unknown_requesting_guest_raises():
    guests, bets, results = _distinct_totals_scenario(5)
    with pytest.raises(ValueError):
        build_leaderboard(
            guests=guests,
            logged_in_guest_ids=set(range(1, 6)),
            bets=bets,
            results=results,
            requesting_guest_id=999,
        )


def test_build_leaderboard_requesting_guest_not_logged_in_raises():
    guests, bets, results = _distinct_totals_scenario(5)
    with pytest.raises(ValueError):
        build_leaderboard(
            guests=guests,
            logged_in_guest_ids={1, 2, 3, 4},  # guest 5 known but not logged in
            bets=bets,
            results=results,
            requesting_guest_id=5,
        )


# --- build_leaderboard: boundary tie truncation (max_rows) -------------------


def test_build_leaderboard_boundary_tie_larger_than_max_rows_truncates_and_reports_counts():
    guests, bets, results = _distinct_totals_scenario(9)
    tied = _tied_zero_guests(10, 20)  # 20 guests tied at 0 points
    guests = guests + tied
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 30)),
        bets=bets,
        results=results,
    )
    assert len(board.rows) == 25  # default max_rows
    assert board.truncated_count == 4  # 9 distinct + 20 tied = 29, over by 4
    assert board.truncated_points == 0


def test_build_leaderboard_truncation_never_splits_guests_with_different_totals():
    guests, bets, results = _distinct_totals_scenario(9)
    tied = _tied_zero_guests(10, 20)
    guests = guests + tied
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 30)),
        bets=bets,
        results=results,
    )
    # all 9 distinct-score guests must be present in full
    visible_ids = {r.guest_id for r in board.rows}
    assert {1, 2, 3, 4, 5, 6, 7, 8, 9}.issubset(visible_ids)
    # every visible row from the tied group has the same (0) total
    tied_rows = [r for r in board.rows if r.rank == 10]
    assert all(r.total_points == 0 for r in tied_rows)
    assert len(tied_rows) == 16  # 25 max_rows - 9 distinct


def test_build_leaderboard_requesting_guest_pinned_correctly_when_truncated():
    guests, bets, results = _distinct_totals_scenario(9)
    tied = _tied_zero_guests(10, 20)
    guests = guests + tied
    # alphabetical sort of Guest10..Guest29 keeps 10..25 visible, cuts 26..29
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 30)),
        bets=bets,
        results=results,
        requesting_guest_id=27,
    )
    assert board.truncated_count == 4
    assert board.requesting_guest_in_rows is False
    assert board.requesting_guest.guest_id == 27
    assert board.requesting_guest.rank == 10
    assert board.requesting_guest.total_points == 0
    assert 27 not in {r.guest_id for r in board.rows}


def test_build_leaderboard_no_truncation_reports_zero_and_none():
    guests, bets, results = _distinct_totals_scenario(9)
    tied = _tied_zero_guests(10, 4)
    guests = guests + tied
    board = build_leaderboard(
        guests=guests,
        logged_in_guest_ids=set(range(1, 14)),
        bets=bets,
        results=results,
    )
    assert board.truncated_count == 0
    assert board.truncated_points is None
