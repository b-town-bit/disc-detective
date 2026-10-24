"""Pictures: the disc's own art, TheDiscDB box art / posters, TMDB; cached for next time."""
import io
import urllib.request

from conftest import needs_images
from disc_detective import core
import fixtures

pytestmark = needs_images


def art_args(tmp_path, **kw):
    return core.default_args(art_dir=str(tmp_path / "art"), **kw)


def test_box_art_for_a_discdb_hit(github, tmp_path, movie_disc):
    con = core.discdb_index_open(str(tmp_path / "discdb"))
    fmt, base = core.detect_format(movie_disc)
    fp = core.fingerprint(fmt, base)
    rep = {"content_hash": fp["hash"], "discdb": core.discdb_lookup(fp, online=False, index=con), "candidates": []}
    a = core.find_artwork(rep, art_args(tmp_path), con)
    assert (a["what"], a["source"]) == ("poster", "TheDiscDB")       # the film's poster beats box art
    assert any("cover.jpg" in p for p, _ in github.log)
    box = core.discdb_art(rep, str(tmp_path / "art"), prefer="box")  # a collection keeps its box
    assert box["what"] == "box art" and any("front.jpg" in p for p, _ in github.log)
    import glob
    assert not glob.glob(str(tmp_path / "art" / "*_dl.jpg"))         # downloads go once converted
    con.close()


def test_poster_by_imdb_id(github, tmp_path):
    con = core.discdb_index_open(str(tmp_path / "discdb"))
    rep = {"content_hash": "1" * 32, "discdb": {"hit": None},
           "candidates": [{"kind": "movie", "imdb_id": "tt7000001", "name": "X", "year": 2011, "score": 99}]}
    a = core.find_artwork(rep, art_args(tmp_path), con)
    assert (a["what"], a["source"]) == ("poster", "TheDiscDB")
    n = len(github.log)                                     # cached: offline, no request
    again = core.find_artwork(rep, art_args(tmp_path, offline=True), con)
    assert again["image"] == a["image"] and len(github.log) == n
    con.close()


def test_tmdb_poster_with_credit(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **p: calls.append(path) or
                        {"movie_results": [{"poster_path": "/p.jpg"}]})

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: calls.append(req.full_url) or R(fixtures.POSTER_JPG))
    rep = {"content_hash": "2" * 32, "discdb": {"hit": None},
           "candidates": [{"kind": "movie", "imdb_id": "tt0000002", "name": "X", "year": 1990, "score": 99}]}
    a = core.find_artwork(rep, art_args(tmp_path, tmdb_key="k" * 32))
    assert a["source"] == "TMDB" and a["credit"] == core.TMDB_CREDIT
    assert calls == ["/find/tt0000002", core.TMDB_IMG + "/p.jpg"]


def foreign_index(releases):
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE posters (imdb, folder, title, year)")
    con.execute("CREATE TABLE discs (path, release, front)")
    f = "movie/Harry Potter and the Philosopher's Stone (2001)"
    con.execute("INSERT INTO posters VALUES ('tt0241527', ?, 'x', 2001)", (f,))
    for r in releases:
        con.execute("INSERT INTO discs VALUES (?, ?, 1)", (f + "/" + r.lower().replace(" ", "-") + "/disc01.json", r))
    return con, f


def test_another_markets_poster_gives_way_to_tmdb(tmp_path, monkeypatch):
    """TheDiscDB has the film only as a German release, under the British title: its poster is
    probably that market's art, so TMDB's goes first (TheDiscDB's stays the fallback)."""
    con, f = foreign_index(["2017 4K UHD - German"])
    us = "Harry Potter and the Sorcerer's Stone"
    assert core.foreign_shelf(f, us, index=con)
    assert not core.foreign_shelf(f, "Harry Potter and the Philosopher's Stone", index=con)   # same title
    us_con, _ = foreign_index(["2017 4K UHD - German", "2001 DVD"])
    assert not core.foreign_shelf(f, us, index=us_con)        # a US release too: its poster is fine
    calls = []
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **p: calls.append(path) or
                        {"movie_results": [{"poster_path": "/p.jpg"}]})

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: calls.append(req.full_url) or R(fixtures.POSTER_JPG))
    for d in ("art", "art2", "art3"):
        (tmp_path / d).mkdir()
    a = core.poster_for_imdb("tt0241527", str(tmp_path / "art"), key="k" * 32, index=con, title=us)
    assert a["source"] == "TMDB" and calls[0] == "/find/tt0241527"
    calls.clear()
    b = core.poster_for_imdb("tt0241527", str(tmp_path / "art2"), key="k" * 32, index=us_con, title=us)
    assert b["source"] == "TheDiscDB" and "cover.jpg" in calls[0]
    calls.clear()
    c = core.poster_for_imdb("tt0241527", str(tmp_path / "art3"), index=con, title=us)   # no key: TheDiscDB's
    assert c["source"] == "TheDiscDB"


def test_nothing_available_is_fine(tmp_path):
    rep = {"content_hash": "3" * 32, "discdb": {"hit": None},
           "candidates": [{"kind": "movie", "imdb_id": "tt0000003", "name": "X", "year": 1900, "score": 99}]}
    assert core.find_artwork(rep, art_args(tmp_path, offline=True)) is None


def test_says_why_without_a_converter(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "image_converter", lambda: None)
    rep = {"content_hash": "4" * 32, "discdb": {"hit": None},
           "candidates": [{"kind": "movie", "imdb_id": "tt7000001", "name": "X", "year": 2011, "score": 99}]}
    assert core.find_artwork(rep, art_args(tmp_path)) is None
    assert "Pillow" in rep["art_note"]


def test_says_why_without_the_index(tmp_path):
    rep = {"content_hash": "5" * 32, "discdb": {"hit": None},
           "candidates": [{"kind": "movie", "imdb_id": "tt7000001", "name": "X", "year": 2011, "score": 99}]}
    assert core.find_artwork(rep, art_args(tmp_path), index=None) is None
    assert "index" in rep["art_note"]


def test_top_guess_gets_a_labelled_poster(github, tmp_path):
    con = core.discdb_index_open(str(tmp_path / "discdb"))
    rep = {"content_hash": "6" * 32, "discdb": {"hit": None}, "candidates": [
        {"kind": "movie", "imdb_id": "tt7000001", "name": "Test Movie Mystery", "year": 2011, "score": 55},
        {"kind": "movie", "imdb_id": "tt0000009", "name": "Test Movie Mystery", "year": 1960, "score": 50}]}
    a = core.find_artwork(rep, art_args(tmp_path), con)
    assert a["guess"] and a["source"] == "TheDiscDB"
    con.close()


def test_tmdb_match_uses_its_poster_path(tmp_path, monkeypatch):
    calls = []

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: calls.append(req.full_url) or R(fixtures.POSTER_JPG))
    rep = {"content_hash": "7" * 32, "discdb": {"hit": None}, "candidates": [
        {"kind": "movie", "tmdb_id": 11970, "name": "Hercules", "year": 1997, "score": 90, "poster_path": "/h.jpg",
         "source": "TMDB"}]}
    a = core.find_artwork(rep, art_args(tmp_path, tmdb_key="k" * 32))
    assert a["source"] == "TMDB" and not a.get("guess") and calls == [core.TMDB_IMG + "/h.jpg"]


def test_nothing_identified_says_so(tmp_path):
    rep = {"content_hash": "8" * 32, "discdb": {"hit": None}, "candidates": []}
    assert core.find_artwork(rep, art_args(tmp_path)) is None
    assert "nothing identified" in rep["art_note"]


def test_poster_beats_the_discs_own_images(github, tmp_path):
    con = core.discdb_index_open(str(tmp_path / "discdb"))
    still = tmp_path / "menu_still.jpg"
    still.write_bytes(fixtures.THUMB_JPG)
    rep = {"content_hash": "9" * 32, "discdb": {"hit": None}, "cover": {"image": str(still)},
           "candidates": [{"kind": "movie", "imdb_id": "tt7000001", "name": "X", "year": 2011, "score": 99}]}
    assert core.find_artwork(rep, art_args(tmp_path), con)["what"] == "poster"
    rep["candidates"] = [{"kind": "movie", "imdb_id": "tt0000077", "name": "Y", "year": 1999, "score": 99}]
    rep["content_hash"] = "A" * 32
    assert core.find_artwork(rep, art_args(tmp_path), con)["what"] == "disc art"   # nothing else: the disc's own
    con.close()


def test_guesses_get_small_posters(github, tmp_path):
    con = core.discdb_index_open(str(tmp_path / "discdb"))
    rep = {"content_hash": "C" * 32, "discdb": {"hit": None}, "candidates": [
        {"kind": "movie", "imdb_id": "tt7000001", "name": "Test Movie Mystery", "year": 2011, "score": 60},
        {"kind": "tv", "imdb_id": "tt7000002", "name": "Example Show", "year": 2004, "score": 58},
        {"kind": "movie", "imdb_id": "tt0000099", "name": "Unknown", "year": 1980, "score": 40}]}
    core.find_artwork(rep, art_args(tmp_path), con)
    thumbs = [c.get("thumb") for c in rep["candidates"]]
    assert thumbs[0] and thumbs[1] and not thumbs[2]
    assert thumbs[0].endswith("_40x60.png")
    con.close()


def test_collection_picture_is_the_whole_disc(tmp_path, monkeypatch):
    """Big picture for a collection: TMDB's collection poster, else the disc's art, else the
    first film's poster (said so), never silently one film standing in for the disc."""
    calls = []

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **p: calls.append((path, p.get("query"))) or
                        ({"results": [{"id": 5, "name": "Wallace & Gromit Collection", "poster_path": "/wg.jpg"}]}
                         if path == "/search/collection" else {}))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: R(fixtures.POSTER_JPG))
    coll = {"kind": "collection", "name": "Wallace & Gromit Collection", "score": 90, "items": [
        {"title_id": "Title 2", "imdb_id": "tt0104361", "name": "A Grand Day Out", "year": 1989}]}
    rep = {"content_hash": "D" * 32, "discdb": {"hit": None}, "candidates": [coll]}
    a = core.find_artwork(rep, art_args(tmp_path, tmdb_key="k" * 32))
    assert a["what"] == "collection poster" and ("/search/collection", "Wallace & Gromit") in calls
    # no key: the disc's own art comes before one film's poster
    still = tmp_path / "s.jpg"
    still.write_bytes(fixtures.THUMB_JPG)
    rep = {"content_hash": "E" * 32, "discdb": {"hit": None}, "candidates": [dict(coll)], "cover": {"image": str(still)}}
    assert core.find_artwork(rep, art_args(tmp_path))["what"] == "disc art"


def test_every_picture_for_a_disc(github, tmp_path, movie_disc, discdb_repo, monkeypatch):
    """Groundwork for the TheDiscDB submission form: this release's box (front; the back is
    missing here), the film's poster, another release's box, TMDB posters."""
    con = core.discdb_index_open(str(tmp_path / "discdb"))
    fmt, base = core.detect_format(movie_disc)
    fp = core.fingerprint(fmt, base)
    rep = {"content_hash": fp["hash"], "discdb": core.discdb_lookup(fp, online=False, index=con),
           "candidates": [{"kind": "movie", "imdb_id": "tt7000001", "name": "Test Movie Mystery",
                           "year": 2011, "score": 99}]}
    args = art_args(tmp_path)
    opts = core.art_options_loaded(rep, args)        # index + GitHub raw files
    assert [o["what"] for o in opts] == ["box art", "poster"]
    assert "back.jpg" in core.art_options(rep, args, con)[1]["src"]      # offered, but 404: dropped
    local = core.art_options(rep, core.default_args(discdb_repo=discdb_repo), None)
    assert [o["label"] for o in local] == ["Box front (2012 Blu-ray)", "Poster: Test Movie Mystery (2011)",
                                           "Box front (2015 4k)"]
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **p: {"movie_results": [{"id": 5}]}
                        if path.startswith("/find") else {"posters": [{"file_path": "/a.jpg", "vote_average": 1},
                                                                       {"file_path": "/b.jpg", "vote_average": 9}]})
    tm = [o for o in core.art_options(rep, core.default_args(discdb_repo=discdb_repo, tmdb_key="k" * 32))
          if o["source"] == "TMDB"]
    assert [o["src"] for o in tm] == [core.TMDB_IMG + "/b.jpg", core.TMDB_IMG + "/a.jpg"]   # best voted first
    con.close()


def _artwork(path, seed, size=(300, 450)):
    """A made-up cover: coloured blocks, different for every seed."""
    import random
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    im = Image.new("RGB", size, tuple(rnd.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(14):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        d.ellipse((x - 60, y - 60, x + rnd.randrange(30, 140), y + rnd.randrange(30, 140)),
                  fill=tuple(rnd.randrange(256) for _ in range(3)))
    im.save(path)
    return im


def test_cover_line_up_finds_the_same_artwork(tmp_path):
    """A DVD's cover still (black bars, squeezed, lossy) is the box art TheDiscDB has for another
    release of the film: that candidate moves up; a different film's art doesn't."""
    import sqlite3
    import pytest
    Image = pytest.importorskip("PIL.Image")
    art = tmp_path / "art"
    art.mkdir()
    front = _artwork(str(art / "tt0000002_2005_widescreen_dvd_front.jpg"), 1)
    _artwork(str(art / "tt0000001_2010_bluray_front.jpg"), 2)
    still = Image.new("RGB", (720, 480))
    still.paste(front.resize((350, 430)), (185, 25))
    still.save(str(tmp_path / "still.jpg"), quality=35)
    index = sqlite3.connect(":memory:")
    index.execute("CREATE TABLE posters (imdb TEXT, folder TEXT, title TEXT, year INT)")
    index.execute("CREATE TABLE discs (path TEXT, front INT)")
    for imdb, folder, rel in (("tt0000001", "movie/Gizmo (1982)", "2010-bluray"),
                              ("tt0000002", "movie/Gizmo (2004)", "2005-widescreen-dvd")):
        index.execute("INSERT INTO posters VALUES (?, ?, 'Gizmo', 0)", (imdb, folder))
        index.execute("INSERT INTO discs VALUES (?, 1)", ("%s/%s/disc01.json" % (folder, rel),))
    rep = {"candidates": [
        {"name": "Gizmo", "year": 1982, "imdb_id": "tt0000001", "kind": "movie", "score": 70.0, "why": "name"},
        {"name": "Gizmo", "year": 2004, "imdb_id": "tt0000002", "kind": "movie", "score": 60.0, "why": "name"}]}
    seen = core.cover_lineup(rep, [str(tmp_path / "still.jpg")], art_args(tmp_path, offline=True), index)
    assert rep["candidates"][0]["year"] == 2004 and core.is_probable(rep["candidates"])
    assert "2005-widescreen-dvd" in rep["candidates"][0]["why"]
    far = next(x for x in seen if x["year"] == 1982)
    assert far["distance"] > 0.3 and "cover_match" not in rep["candidates"][1]
