"""An ad's reading matched to the well-known film it names (core.promo_match, core.title_fix), against a small
IMDb index of its own: titles, ratings and their word index."""
import sqlite3

import pytest

from disc_detective import core, menus

FILMS = [  # (title, year, votes): the well-known ones
    ("Spider-Man", 2002, 900000), ("Spider-Man 2", 2004, 600000), ("Spider-Man 3", 2007, 500000),
    ("Ghost Rider", 2007, 250000), ("Ghost Town", 2008, 90000),
    ("Hannah Montana", 2006, 40000), ("Hannah and Her Sisters", 1986, 80000),
    ("Herbie: Fully Loaded", 2005, 70000), ("The Little Mermaid", 1989, 300000),
    ("The Little Mermaid: Ariel's Beginning", 2008, 13000), ("Hellboy", 2004, 350000),
    ("Book Club", 2018, 30000), ("Black Book", 2006, 80000), ("The Pacifier", 2005, 90000),
    ("Iron Man", 2008, 1100000), ("InSight", 2011, 1500), ("Extended Play", 2003, 1200), ("30 Rock", 2006, 190000),
    ("Rock Star", 2001, 60000),
]
# (each word in three titles or more, as a word list keeps: less well-known titles, under PROMO_VOTES)
FILLER = ["spider", "man", "ghost", "rider", "town", "hannah", "montana", "sisters", "herbie", "fully", "loaded",
          "little", "mermaid", "ariel", "beginning", "hellboy", "book", "club", "black", "pacifier", "iron", "insight", "extended", "play", "rock", "star"]


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("promo") / "imdb.sqlite")
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE titles (id INTEGER PRIMARY KEY, title TEXT, year INTEGER, type TEXT);"
                     "CREATE TABLE ratings (id INTEGER PRIMARY KEY, votes INTEGER);"
                     "CREATE VIRTUAL TABLE titles_fts USING fts5(title, content='titles', content_rowid='id');")
    rows = list(FILMS) + [("%s Story %s" % (w.capitalize(), n), 1990, 10) for w in FILLER for n in ("One", "Two")]
    for i, (title, year, votes) in enumerate(rows, 1):
        db.execute("INSERT INTO titles VALUES (?, ?, ?, 'movie')", (i, title, year))
        db.execute("INSERT INTO ratings VALUES (?, ?)", (i, votes))
    db.execute("INSERT INTO titles_fts(titles_fts) VALUES('rebuild')")
    db.commit()
    yield db
    db.close()


def test_a_trademark_sign_read_as_letters_and_a_sequel_number(con):
    # ("Spider-Mantm 2": the ™ cut off, and its 2 picks the sequel over Spider-Man and Spider-Man 3)
    assert core.title_fix(con, "Spider-Mantm 2", 2005) == "Spider-Man 2"
    assert core.title_fix(con, "Spider-ManT", 2005) == "Spider-Man"
    assert core.title_fix(con, "Ghost Riderm", 2007) == "Ghost Rider"


def test_logo_words_dropped_and_misread_words_put_right(con):
    assert core.title_fix(con, "Disney ANNAH ONTANA", 2008) == "Hannah Montana"
    # (two words run together on a logo, one of them misread)
    assert core.title_fix(con, "Herbie FULLYOADED", 2005) == "Herbie: Fully Loaded"
    assert core.title_fix(con, "Pacifier", 2005) == "The Pacifier"


def test_a_numbers_letters_read_as_digits(con):
    # (a show's logo, its 0 read as O and run into its name: Psych's previews' "3OROCK")
    assert core.title_fix(con, "3OROCK", 2013) == "30 Rock"
    # (no words as read: its reading a candidate only, a name when it matches a title, else just "Previews")
    said = menus.ad_words([{"text": "3OROCK", "via": "PREVIEWS"}])
    assert said["own"] == "" and said["reads"]
    fix = lambda t, also=(): core.title_fix(con, t, 2013, also)
    found = menus.fix_promos({"promo_said": {6: said}}, fix)
    assert menus.ad_name(found["promo_said"][6]) == "Previews - 30 Rock" and found["promo_read"][6] == said["reads"][0]
    junk = menus.fix_promos({"promo_said": {7: menus.ad_words([{"text": "X7QZ", "via": "PREVIEWS"}])}}, fix)
    assert menus.ad_name(junk["promo_said"][7]) == "Previews" and not junk["promo_read"]


def test_number_readings_one_at_a_time_fewest_first():
    # (each letter OCR reads for a digit, in a run with a digit: taken one way and the other, fewest taken first)
    assert list(core.number_readings("3orock")) == ["30rock"]
    assert list(core.number_readings("PINOCCHIO7OTH")) == [
        "PINOCCH1O7OTH", "PINOCCHI07OTH", "PINOCCHIO70TH",
        "PINOCCH107OTH", "PINOCCH1O70TH", "PINOCCHI070TH", "PINOCCH1070TH"]
    assert list(core.number_readings("2OO7"))[-1] == "2007" and len(list(core.number_readings("2OO7"))) == 3
    assert list(core.number_readings("ROCK")) == [] and list(core.number_readings("IOTH")) == []
    assert len(list(core.number_readings("1OIlO1OIlO1", most=5))) == 5


def test_a_reading_already_its_films_name_stays_as_read(con):
    # (its own words added, as the menu has them: TheDiscDB's names keep them)
    assert core.title_fix(con, "Hellboy DVD", 2005) == "Hellboy DVD"
    assert core.title_fix(con, "HELLBOY", 2005) == "HELLBOY"            # (its casing nice_name's, as ever)


def test_too_little_read_or_a_near_tie_stays_as_read(con):
    assert core.title_fix(con, "Book", 2008) == "Book"                  # (Book Club or Black Book)
    assert core.promo_match(con, "Book", 2008) is None
    assert core.title_fix(con, "Qxzv Wrrt", 2008) == "Qxzv Wrrt"
    # (one word read, not close enough alone: In Plain Sight's logo, a DVD's own "Special Extended Edition")
    assert core.title_fix(con, "Nplainsight", 2013) == "Nplainsight"
    assert core.title_fix(con, "Special Extended DVD Edition Preview", 2003) == "Special Extended DVD Edition Preview"


def test_no_film_from_after_the_disc(con):
    assert core.title_fix(con, "Iron Manm", 2008) == "Iron Man"
    assert core.title_fix(con, "Iron Manm", 2003) == "Iron Manm"       # (Iron Man is 2008's: not on a 2003 disc)


def test_its_other_readings_tried_the_best_kept(con):
    assert core.title_fix(con, "Qxzv Wrrt", 2007, ("Ghost Riderm",)) == "Ghost Rider"
    assert core.title_fix(con, "Ghost", 2007, ("Ghost Riderm",)) == "Ghost Rider"


def test_an_ads_buttons_readings_handed_to_title_fix():
    said = menus.ad_words([{"text": "Ghost Riderm", "via": "PREVIEWS"}, {"text": "GHOST RlDER", "via": "PREVIEWS"},
                           {"text": "PREVIEWS", "via": "PREVIEWS"}])
    assert said["reads"][0] == said["own"] and len(said["reads"]) == 2
    asked = []
    fix = lambda t, also=(): asked.append((t, tuple(also))) or "Ghost Rider"
    found = {"feature": 1, "promos": [9], "promo_said": {9: said}, "extras": []}
    fixed = menus.correct_names(found, [], lambda w: True, fix)
    assert asked == [(said["own"], tuple(said["reads"][1:]))]
    assert menus.ad_name(fixed["promo_said"][9]) == "Previews - Ghost Rider"


def test_a_shows_ads_put_right_as_a_films_are():
    # (Psych's Previews page: other shows' logos; one ad's two buttons, both readings kept)
    import test_tvmenus as tvm
    out = {"opening": [], "pages": [
        tvm.page(1, [tvm.plays("play opening night", 1), tvm.goes("previews", 16)]),
        tvm.page(16, [tvm.plays("GRIMIM", 9, 85), tvm.plays("Hannah Montana", 10, 70), tvm.goes("MENU", 1)],
                 heading="PREVIEWS", route=[(1, 1, 2)]),
        tvm.page(17, [tvm.plays("GRIMM", 9, 85), tvm.goes("MENU", 1)], heading="PREVIEWS", route=[(1, 1, 2)])]}
    tv = menus.tv_titles(out)
    assert tv["promo_said"][9]["own"] == "Grimim" and tv["promo_said"][9]["reads"] == ["Grimim", "Grimm"]
    found = menus.tv_found(tv, {})
    asked = []
    fix = lambda t, also=(): asked.append((t, tuple(also))) or {"Grimim": "Grimm"}.get(t, t)
    found = menus.fix_promos(found, fix)
    assert asked[0] == ("Grimim", ("Grimm",))
    labels = menus.detected_tv_labels(found, [{"id": "Title %d" % n, "role": "extra"} for n in (1, 9, 10)])
    # (as read, in the label's "read": the menu picture's "read as", as an extra's put right is; its note as ever)
    assert labels["Title 9"]["title"] == "Previews - Grimm" and labels["Title 9"]["read"] == "Grimim"
    assert labels["Title 9"]["note"] == "the menus: an ad"
    assert labels["Title 10"]["title"] == "Previews - Hannah Montana" and "read" not in labels["Title 10"]


def test_another_films_trailer_its_name_misread_put_right(con):
    found = {"extras": [{"title": 12, "name": "Spider-Mantm 2 Trailer"}, {"title": 13, "name": "Theatrical Trailer"}]}
    fix = lambda t, also=(): core.title_fix(con, t, 2005, also)
    got = menus.known_films(found, lambda n: None, "Hitch", fix)
    assert got["extras"][0]["names_film"] == "Spider-Man 2" and "names_film" not in got["extras"][1]
    assert got["extras"][0]["read"] == "Spider-Mantm 2 Trailer" and "read" not in got["extras"][1]
    naming = menus.name_extras([dict(e, key=e["title"]) for e in got["extras"]], "Hitch")
    assert naming[12][:2] == (core.Type.PROMO, "Previews - Spider-Man 2")
