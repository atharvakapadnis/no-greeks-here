import pytest

from app.services.guests import (
    GuestName,
    add_guest_username,
    assign_usernames,
    base_username,
    full_username,
    split_full_name,
)


# --- base_username -----------------------------------------------------


def test_base_username_simple():
    assert base_username("John", "Doe") == "jdoe"


def test_base_username_lowercases_mixed_case_input():
    assert base_username("JOHN", "DoE") == "jdoe"


def test_base_username_strips_accents():
    assert base_username("Anna", "Müller") == "amuller"


def test_base_username_strips_apostrophe():
    assert base_username("Pat", "O'Brien") == "pobrien"


def test_base_username_strips_hyphen():
    assert base_username("Sam", "Smith-Jones") == "ssmithjones"


def test_base_username_strips_internal_spaces():
    assert base_username("Anna", "van der Berg") == "avanderberg"


def test_base_username_strips_leading_trailing_whitespace():
    assert base_username("  John  ", "  Doe  ") == "jdoe"


def test_base_username_both_empty_raises_value_error():
    with pytest.raises(ValueError):
        base_username("", "")


def test_base_username_whitespace_only_raises_value_error():
    with pytest.raises(ValueError):
        base_username("   ", "   ")


def test_base_username_symbols_only_normalises_to_empty_raises():
    with pytest.raises(ValueError):
        base_username("--", "''")


def test_base_username_single_word_name():
    # split_full_name would produce first_name="" for "Madonna"
    assert base_username("", "Madonna") == "madonna"


# --- full_username -------------------------------------------------------


def test_full_username_simple():
    assert full_username("Chris", "Campbell") == "chriscampbell"


# --- split_full_name -----------------------------------------------------


def test_split_full_name_single_word():
    assert split_full_name("Madonna") == GuestName(first_name="", last_name="Madonna")


def test_split_full_name_multi_word_surname():
    assert split_full_name("Anna van der Berg") == GuestName(
        first_name="Anna van der", last_name="Berg"
    )


def test_split_full_name_empty_raises():
    with pytest.raises(ValueError):
        split_full_name("")


def test_split_full_name_whitespace_only_raises():
    with pytest.raises(ValueError):
        split_full_name("   ")


def test_split_full_name_strips_leading_trailing_whitespace():
    assert split_full_name("  John Doe  ") == GuestName(
        first_name="John", last_name="Doe"
    )


# --- assign_usernames (bulk import) --------------------------------------


def test_assign_usernames_no_collisions():
    guests = [GuestName("John", "Doe"), GuestName("Jane", "Smith")]
    result = assign_usernames(guests)
    assert [u.username for u in result.usernames] == ["jdoe", "jsmith"]
    assert result.extended == []


def test_assign_usernames_campbell_pair_resolves_to_full_names():
    guests = [
        GuestName("Carolyn", "Campbell"),
        GuestName("Chris", "Campbell"),
    ]
    result = assign_usernames(guests)
    usernames = [u.username for u in result.usernames]
    assert usernames == ["carolyncampbell", "chriscampbell"]


def test_assign_usernames_reports_extended_names():
    guests = [
        GuestName("Carolyn", "Campbell"),
        GuestName("Chris", "Campbell"),
    ]
    result = assign_usernames(guests)
    assert len(result.extended) == 2
    assert {e.short_username for e in result.extended} == {"ccampbell"}
    assert {e.final_username for e in result.extended} == {
        "carolyncampbell",
        "chriscampbell",
    }


def test_assign_usernames_further_collision_appends_integer_suffix():
    guests = [
        GuestName("Chris", "Campbell"),
        GuestName("Chris", "Campbell"),
    ]
    result = assign_usernames(guests)
    usernames = [u.username for u in result.usernames]
    assert usernames == ["chriscampbell", "chriscampbell2"]


def test_assign_usernames_deterministic_given_input_order():
    guests = [
        GuestName("Chris", "Campbell"),
        GuestName("Chris", "Campbell"),
        GuestName("Chris", "Campbell"),
    ]
    result = assign_usernames(guests)
    usernames = [u.username for u in result.usernames]
    assert usernames == ["chriscampbell", "chriscampbell2", "chriscampbell3"]

    # re-running with the same input order gives the same result
    result2 = assign_usernames(guests)
    assert [u.username for u in result2.usernames] == usernames


def test_assign_usernames_empty_list():
    result = assign_usernames([])
    assert result.usernames == []
    assert result.extended == []


# --- add_guest_username (incremental) ------------------------------------


def test_add_guest_username_no_collision():
    existing = {"jdoe"}
    username = add_guest_username(GuestName("Jane", "Smith"), existing)
    assert username == "jsmith"


def test_add_guest_username_collision_extends_new_guest_and_leaves_existing_untouched():
    existing = {"ccampbell"}
    username = add_guest_username(GuestName("Chris", "Campbell"), existing)
    assert username == "chriscampbell"
    # existing guest's username is unchanged
    assert existing == {"ccampbell"}


def test_add_guest_username_further_collision_appends_integer_suffix():
    existing = {"ccampbell", "chriscampbell"}
    username = add_guest_username(GuestName("Chris", "Campbell"), existing)
    assert username == "chriscampbell2"


def test_add_guest_username_does_not_mutate_existing_usernames_set():
    existing = {"ccampbell"}
    original = set(existing)
    add_guest_username(GuestName("Chris", "Campbell"), existing)
    assert existing == original
