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
