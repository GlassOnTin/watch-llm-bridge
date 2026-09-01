import pytest

from app import resolve_target

BOARDS = {"Home": "b1", "Plans": "b2"}
LISTS = {
    "b1": [("Shopping List", "l1"), ("Hugge", "l2")],
    "b2": [("Done", "l3"), ("Done address changes", "l4")],
}


def test_resolves_case_insensitively():
    assert resolve_target(BOARDS, LISTS, "home", "shopping list") == ("b1", "l1")


def test_no_default_on_unknown_list():
    with pytest.raises(KeyError, match="matches nothing"):
        resolve_target(BOARDS, LISTS, "Home", "Freezer")


def test_no_default_on_unknown_board():
    with pytest.raises(KeyError, match="known"):
        resolve_target(BOARDS, LISTS, "Work", "Done")


def test_duplicate_list_names_fail_loudly():
    # real case: "Move To A Nicer Spot" has two lists named "Done"
    lists = {"b1": [("Done", "l3"), ("Done", "l9")]}
    with pytest.raises(KeyError, match="2 lists named 'Done'"):
        resolve_target(BOARDS, lists, "Home", "Done")