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
    assert (a["what"], a["source"]) == ("box art", "TheDiscDB")
    assert any("front.jpg" in p for p, _ in github.log)
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
