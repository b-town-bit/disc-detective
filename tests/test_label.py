"""Volume labels: season / disc / years / noise words / run-together words."""
import pytest

from disc_detective.core import parse_label


@pytest.mark.parametrize("label, query, season, disc", [
    ("SMALLVILLE_SEASON_10_DISC_1", "Smallville", 10, 1),
    ("PRINCE_OF_EGYPT_UPB2", "Prince Of Egypt", None, None),
    ("AN_AMERICAN_TAIL_2", "An American Tail 2", None, None),
    ("WALLACE_AND_GROMIT", "Wallace And Gromit", None, None),
])
def test_labels(label, query, season, disc):
    p = parse_label(label)
    assert p["query"].lower() == query.lower()
    assert p["season"] == season and p["disc"] == disc


def test_region_code_ends_the_title():
    """POCAHONTAS_USA_DES: what follows a region code is a release / edition code."""
    assert parse_label("Pocahontas_USA_DES")["query"] == "Pocahontas"
    assert parse_label("SHREK_UK_SE_D1")["query"] == "Shrek"
    assert parse_label("US")["query"] == "Us"                    # a region word on its own is the title


def test_short_trailing_code_is_only_a_fallback():
    p = parse_label("Pocahontas_DES")
    assert p["query"] == "Pocahontas Des" and p["short_query"] == "Pocahontas"
    assert parse_label("ICE_AGE_DS1")["query"] == "Ice Age"     # full query first; "Ice" only if nothing found
    assert parse_label("CARS_2").get("short_query") is None


def test_number_stuck_to_the_last_word():
    assert parse_label("SISTER_ACT2")["query"] == "Sister Act 2"
    assert parse_label("PRINCE_OF_EGYPT_UPB2")["query"] == "Prince Of Egypt"     # still a production code
