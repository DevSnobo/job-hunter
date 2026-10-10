import pytest

from cv_batch.selection import SelectionError, parse_selection


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1,3-5,12", [1, 3, 4, 5, 12]),
        ("1", [1]),
        ("12", [12]),
        (" 1 , 3 - 5 ", [1, 3, 4, 5]),
        ("3-3", [3]),
        ("5,5,1", [1, 5]),
        ("all", list(range(1, 13))),
        ("*", list(range(1, 13))),
    ],
)
def test_valid_selections(spec, expected):
    assert parse_selection(spec, 12) == expected


def test_blank_cancels_and_never_selects_all():
    assert parse_selection("", 12) == []
    assert parse_selection("   ", 12) == []
    assert parse_selection(None, 12) == []


@pytest.mark.parametrize(
    "spec",
    ["0", "13", "5-3", "abc", "1,,2", "1-", "-2", "1.5", "1;2"],
)
def test_rejected_selections(spec):
    with pytest.raises(SelectionError):
        parse_selection(spec, 12)


def test_dedupe_preserves_display_order():
    assert parse_selection("9,1,9,2-4", 12) == [1, 2, 3, 4, 9]


def test_out_of_range_message_names_the_bounds():
    with pytest.raises(SelectionError) as excinfo:
        parse_selection("7", 3)
    assert "1 and 3" in str(excinfo.value)


def test_reversed_range_message_suggests_the_fix():
    with pytest.raises(SelectionError) as excinfo:
        parse_selection("5-3", 12)
    assert "3-5" in str(excinfo.value)
