"""Every test gets its own empty Disc Detective home and no route to the real internet."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disc_detective import core  # noqa: E402
import fixtures  # noqa: E402

DEAD = "http://127.0.0.1:9/"      # connection refused at once


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DISC_DETECTIVE_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    for var in ("TMDB_API_KEY", "DISC_DETECTIVE_DB", "DISC_DETECTIVE_IMDB", "DISC_DETECTIVE_DISCDB",
                "DISC_DETECTIVE_DISCDB_DIR", "DISC_IDENTIFY_DB", "DISC_IDENTIFY_IMDB", "DISC_IDENTIFY_DISCDB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(core, "DISCDB_INDEX_URL", DEAD + "index.gz")
    monkeypatch.setattr(core, "DISCDB_GRAPHQL", DEAD + "graphql")
    monkeypatch.setattr(core, "DISCDB_RAW", DEAD + "raw/")
    monkeypatch.setattr(core, "IMDB_BASE", DEAD + "imdb/")
    monkeypatch.setattr(core, "TMDB_API", DEAD + "tmdb")
    monkeypatch.setattr(core, "log", lambda msg: None)
    monkeypatch.setattr(core, "progress", None)
    return home


@pytest.fixture(scope="session")
def imdb_dir(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("imdb"))
    fixtures.build_imdb(d)
    core.imdb_open(d, allow_download=False, log=lambda m: None).close()
    return d


@pytest.fixture(scope="session")
def imdb(imdb_dir):
    con = core.imdb_open(imdb_dir, allow_download=False, log=lambda m: None)
    yield con
    con.close()


@pytest.fixture(scope="session")
def movie_disc(tmp_path_factory):
    return fixtures.make_movie_bluray(str(tmp_path_factory.mktemp("disc") / "TEST_MOVIE"))


@pytest.fixture(scope="session")
def discdb_repo(tmp_path_factory, movie_disc):
    fmt, base = core.detect_format(movie_disc)
    fp = core.fingerprint(fmt, base)
    root = str(tmp_path_factory.mktemp("discdb"))
    fixtures.build_discdb_repo(root, fp["hash"], [(n, s) for n, s in fp["files"]])
    return root


@pytest.fixture(scope="session")
def index_gz(tmp_path_factory, discdb_repo):
    import gzip
    import shutil
    d = tmp_path_factory.mktemp("index")
    db = str(d / "discdb_index.sqlite")
    core.discdb_build_index(discdb_repo, db, commit="test", log=lambda m: None)
    with open(db, "rb") as src, gzip.open(db + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    return db + ".gz"


@pytest.fixture
def github(index_gz, discdb_repo, imdb_dir, monkeypatch):
    srv = fixtures.FakeGitHub(asset=index_gz, raw_root=os.path.join(discdb_repo, "data"), imdb_dir=imdb_dir)
    monkeypatch.setattr(core, "DISCDB_INDEX_URL", srv.base + "/dl/discdb_index.sqlite.gz")
    monkeypatch.setattr(core, "DISCDB_RAW", srv.base + "/raw/")
    monkeypatch.setattr(core, "IMDB_BASE", srv.base + "/imdb/")
    yield srv
    srv.stop()


def can_convert_images():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        import shutil
        return bool(shutil.which("ffmpeg"))


needs_images = pytest.mark.skipif(not can_convert_images(), reason="needs Pillow or ffmpeg")
