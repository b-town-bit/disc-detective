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


def test_blu_ray_disc_number_and_trailing_codes():
    """PUSHING_DAISIES_S1_BD01_DIM1: BD01 is disc 1; what follows the season / disc marks is a code."""
    p = parse_label("PUSHING_DAISIES_S1_BD01_DIM1")
    assert (p["query"], p["season"], p["disc"]) == ("Pushing Daisies", 1, 1)
    p = parse_label("PUSHING_DAISIES_S1_BD01_DIM2")       # every disc of the set says BD01
    assert (p["query"], p["season"], p["disc"]) == ("Pushing Daisies", 1, 2)
    p = parse_label("STAR_TREK_TNG_S3_D2_WS")
    assert (p["query"], p["season"], p["disc"]) == ("Star Trek Tng", 3, 2)
    p = parse_label("PSYCH_S2_DVD3")
    assert (p["query"], p["season"], p["disc"]) == ("Psych", 2, 3)


def test_british_series_number_is_the_season():
    """SHERLOCK_SERIES_3_DISC_1 was read as "Sherlock Series" and found Sherlock Jr. (1924)."""
    p = parse_label("SHERLOCK_SERIES_3_DISC_1")
    assert (p["query"], p["season"], p["disc"]) == ("Sherlock", 3, 1)
    p = parse_label("DOCTOR_WHO_SERIES_1_D2")
    assert (p["query"], p["season"], p["disc"]) == ("Doctor Who", 1, 2)


@pytest.mark.parametrize("label,expected", [
    ("SHERLOCK_SERIES_THREE_DISC_1", ("Sherlock", 3, 1)),        # spelled-out number
    ("SMALLVILLE_SEASON_TWO_D3", ("Smallville", 2, 3)),
    ("SHERLOCK_SERIES_3", ("Sherlock", 3, None)),                 # the show's name comes first
    ("SERIES_7_THE_CONTENDERS", ("Series 7 The Contenders", None, None)),   # a film's title
    ("A_SERIES_OF_UNFORTUNATE_EVENTS", ("A Series Of Unfortunate Events", None, None)),
    ("THE_OFFICE_COMPLETE_SERIES_D4", ("The Office", None, 4)),
])
def test_series_means_season_only_on_a_tv_disc(label, expected):
    p = parse_label(label)
    assert (p["query"], p["season"], p["disc"]) == expected


def test_placeholder_labels_are_no_label():
    """LOGICAL_VOLUME_ID, BDROM, DVD_VIDEO: the authoring tool's default, not a title."""
    for raw in ("LOGICAL_VOLUME_ID", "BDROM", "DVD_VIDEO", "Untitled", "NEW VOLUME", "DISC1"):
        p = parse_label(raw)
        assert p["placeholder"] and p["query"] == "", raw
    assert not parse_label("SHREK_2").get("placeholder")
    assert parse_label("DISC_OF_THE_DEAD")["query"]          # a placeholder word inside a title is fine


def test_disc_metadata_names_lose_the_format():
    """The Blu-ray's own title says "Miracle Blu-ray™": the film is "Miracle"."""
    from disc_detective.core import meta_title
    assert meta_title("Miracle Blu-ray™") == "Miracle"
    assert meta_title("Cars Blu-ray Disc") == "Cars"
    assert meta_title("Avatar 4K Ultra HD") == "Avatar"
    assert meta_title("Shrek®") == "Shrek"
    assert meta_title("Blu-ray™") == ""                 # only the format: no name at all
    assert meta_title("The Dark Knight") == "The Dark Knight"


def test_cover_art_needs_ffmpeg_to_read():
    from disc_detective.core import status
    rep = {"label_parsed": parse_label("DVD_VIDEO"), "candidates": [], "search_queries": [], "discdb": {"hit": None},
           "cover": {"found": "JACKET_P/J00___5L.MP2", "image": None, "note": "install ffmpeg to decode"}}
    assert "needs ffmpeg" in status(rep, None)


def test_cover_text_is_read_as_phrases(tmp_path):
    """A menu-sized cover still: the title phrase comes back on its own, apart from the blurb."""
    import shutil
    from disc_detective.core import ocr
    PIL = pytest.importorskip("PIL")
    if not shutil.which("tesseract"):
        pytest.skip("no tesseract")
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (720, 480), "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=40)
    except TypeError:
        pytest.skip("Pillow too old for a sized default font")
    d.text((60, 80), "KICK BACK", fill="black", font=font)
    d.text((60, 300), "bigger kicks", fill="black", font=ImageFont.load_default(size=22))
    path = str(tmp_path / "cover.png")
    im.save(path)
    assert "KICK BACK" in (ocr(path) or [])
