"""The slim TheDiscDB index: built by the Action, downloaded and refreshed by every user."""
import gzip
import os
import shutil
import sqlite3

from disc_detective import core


def test_build_from_checkout(index_gz, tmp_path):
    db = tmp_path / "i.sqlite"
    with gzip.open(index_gz) as src, open(db, "wb") as dst:
        shutil.copyfileobj(src, dst)
    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta"))
    assert meta["discs"] == "2" and meta["posters"] == "2" and "Luke Foust" in meta["notice"]
    assert con.execute("SELECT front FROM discs WHERE kind='movie'").fetchone()[0] == 1
    try:
        import PIL  # noqa: F401
        assert meta["prints"] == "2"                       # each release's box art, fingerprinted
        assert {r for r, in con.execute("SELECT release FROM prints")} == {"2012-blu-ray", "2015-4k"}
    except ImportError:
        assert meta["prints"] == "0"


def test_box_sets_say_which_set_a_disc_is_in(index_gz, tmp_path):
    """TheDiscDB files a box set's discs under its films, in a release of the set's slug; the
    index keeps the sets so a disc knows its set, and which disc of it it is: by name (the
    bonus disc), or as the one disc filed under its film that no other disc's name claims."""
    db = tmp_path / "i.sqlite"
    with gzip.open(index_gz) as src, open(db, "wb") as dst:
        shutil.copyfileobj(src, dst)
    con = sqlite3.connect(db)
    assert dict(con.execute("SELECT key, value FROM meta"))["boxsets"] == "1"
    film = {"slug": "2012-blu-ray", "disc": "Blu-ray", "title": "Test Movie Mystery", "year": 2011}
    bs = core.discdb_boxset(film, con)        # two discs of the set are filed under the film
    assert (bs["title"], bs["year"], bs["number"], bs["count"], bs["front"]) == ("Test Mystery Collection", 2012, None, 2, True)
    con.execute("INSERT INTO discs (hash, slug, disc) VALUES (?, '2012-blu-ray', 'Bonus Disc')", ("B" * 32,))
    assert core.discdb_boxset(film, con)["number"] == 1     # the bonus disc has its own name: this is 1
    bonus = core.discdb_boxset(dict(film, disc="Bonus Disc"), con)
    assert (bonus["disc"], bonus["number"]) == ("Bonus Disc", 2)
    assert core.discdb_boxset({"slug": "s1-dvd", "disc": "Disc 1"}, con) is None      # not in a set
    con.execute("DROP TABLE boxsets")                                    # an index from before sets
    assert core.discdb_boxset(film, con) is None


def test_a_disc_found_by_its_cover_alone(index_gz, tmp_path):
    """A DVD-style still of a release's box art finds the film, label or not; a box set's art,
    filed under each of its films, needs the runtime to say which film is on the disc."""
    import io
    import pytest
    Image = pytest.importorskip("PIL.Image")
    import fixtures
    db = tmp_path / "i.sqlite"
    with gzip.open(index_gz) as src, open(db, "wb") as dst:
        shutil.copyfileobj(src, dst)
    index = sqlite3.connect(db)
    box = Image.open(io.BytesIO(fixtures.BOX_JPG)).convert("RGB")
    still = Image.new("RGB", (720, 480))
    still.paste(box.resize((300, 400)), (210, 40))
    still.save(str(tmp_path / "still.jpg"), quality=40)
    args = core.default_args(no_imdb=True)
    rep = {"candidates": [], "facts": {"feature_minutes": 99}}
    c = core.found_by_cover(rep, [str(tmp_path / "still.jpg")], index, args, "movie")
    assert c["name"] == "Test Movie Mystery" and core.is_probable(rep["candidates"])
    assert "box art" in c["why"]
    # the same art under a second film (a box set): no runtimes to choose by, so no answer
    p = index.execute("SELECT print FROM prints LIMIT 1").fetchone()[0]
    index.execute("INSERT INTO posters VALUES ('tt7000009', 'movie/Other (2013)', 'Other', 2013)")
    index.execute("INSERT INTO prints VALUES ('movie/Other (2013)', '2012-blu-ray', ?)", (p,))
    rep = {"candidates": [], "facts": {"feature_minutes": 99}}
    assert core.found_by_cover(rep, [str(tmp_path / "still.jpg")], index, args, "movie") is None


def test_download_refresh_and_offline(github, tmp_path, index_gz):
    d = str(tmp_path / "discdb")
    seen = []
    core.progress = lambda label, done, total: seen.append((done, total))
    con = core.discdb_index_open(d)                       # first use: downloads, with progress
    assert con is not None and seen and seen[-1][0] == seen[-1][1]
    con.close()
    n = len(github.log)
    core.discdb_index_open(d).close()                     # within a week: no request at all
    assert len(github.log) == n
    assert core.discdb_index_download(d, force=True) is False      # unchanged: 304
    assert github.log[-1][1]
    # the publisher rebuilt it: downloaded again
    alt = tmp_path / "alt.sqlite"
    with gzip.open(index_gz) as src, open(alt, "wb") as dst:
        shutil.copyfileobj(src, dst)
    c = sqlite3.connect(alt)
    c.execute("UPDATE meta SET value='2099-01-01T00:00:00Z' WHERE key='built'")
    c.commit()
    c.close()
    with open(alt, "rb") as src, gzip.open(str(alt) + ".gz", "wb") as dst:
        dst.write(src.read())
    github.asset = str(alt) + ".gz"
    assert core.discdb_index_download(d, force=True) is True
    assert core.discdb_index_info(d)["built"].startswith("2099")
    # a format this version can't read is refused; the current copy stays
    c = sqlite3.connect(alt)
    c.execute("UPDATE meta SET value='99' WHERE key='version'")
    c.commit()
    c.close()
    with open(alt, "rb") as src, gzip.open(str(alt) + ".gz", "wb") as dst:
        dst.write(src.read())
    errs = []
    con = core.discdb_index_open(d, force=True, errors=errs)
    assert con is not None and "different version" in errs[0] and core.discdb_index_info(d)["version"] == "1"
    con.close()
    github.stop()                                          # offline: keep using what we have
    errs = []
    con = core.discdb_index_open(d, force=True, errors=errs)
    assert con is not None and errs
    con.close()


def test_lookup_and_near_match(github, tmp_path, movie_disc):
    con = core.discdb_index_open(str(tmp_path / "discdb"))
    fmt, base = core.detect_format(movie_disc)
    fp = core.fingerprint(fmt, base)
    hit = core.discdb_lookup(fp, online=False, index=con)["hit"]
    assert (hit["title"], hit["year"], hit["release"]) == ("Test Movie Mystery", 2011, "2012 Blu-ray")
    assert hit["titles"][1]["item"]["type"] == "Trailer"
    # another pressing: different fingerprint, same big files -> a near match
    other = {"hash": "F" * 32, "files": fp["files"] + [("99999.m2ts", 123)]}
    near = core.discdb_lookup(other, online=False, index=con)["near"]
    assert near and near[0]["path"].endswith("2012-blu-ray/disc01.json")
    con.close()


def test_no_index_is_not_an_error(tmp_path):
    assert core.discdb_index_open(str(tmp_path / "none"), allow_download=False) is None
