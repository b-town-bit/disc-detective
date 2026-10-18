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
