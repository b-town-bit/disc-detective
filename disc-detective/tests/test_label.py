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
