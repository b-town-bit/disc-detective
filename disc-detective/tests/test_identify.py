"""Whole investigations on fake discs, the way the app and the command line run them."""
import json
import os
import subprocess
import sys

from disc_detective import core
import fixtures

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(disc, tmp_path, **kw):
    args = core.default_args(**kw)
    args.tmp_out = str(tmp_path / "tmp")
    args.library = core.library_open(args.db)
    try:
        return core.identify(disc, args), args
    finally:
        args.library.close()


def test_tv_bluray(tmp_path, imdb_dir):
    disc = fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED_S1_D1"))
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    assert rep["kind"] == "tv"
    eps = sorted((t for t in rep["titles"] if t["role"] == "episode"), key=lambda t: t["episode_index"])
    assert [t["id"] for t in eps] == ["00001.mpls", "00002.mpls", "00003.mpls", "00004.mpls"]
    roles = {t["id"]: t["role"] for t in rep["titles"]}
    assert roles["00005.mpls"] == "play-all" and roles["00006.mpls"] == "duplicate"
    top = rep["candidates"][0]
    assert core.is_probable(rep["candidates"]) and top["name"] == "Justified"
    assert any("s01e01 - Fire in the Hole" in n for n in rep["plex_names"])


def test_movie_found_in_downloaded_index(tmp_path, github, imdb_dir, movie_disc):
    rep, _ = run(movie_disc, tmp_path, imdb_dir=imdb_dir, no_discdb_online=True)
    assert rep["status"] == "identified (TheDiscDB)"
    assert rep["discdb"]["hit"]["title"] == "Test Movie Mystery"
    assert rep["best_guess"].startswith("Test Movie Mystery")


def test_your_library_remembers(tmp_path, imdb_dir):
    disc = fixtures.make_movie_bluray(str(tmp_path / "SOME_MOVIE"), minutes=118, seed=3)
    rep, args = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    assert rep["recorded"] and rep["personal_hit"] is None
    con = core.library_open(args.db)
    core.set_field(con, rep["content_hash"][:8], "truth", "Gone in 60 Seconds (2000)")
    con.close()
    rep2, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    assert rep2["status"] == "identified (your library)"
    assert rep2["best_guess"] == "Gone in 60 Seconds (2000)"
    assert core.is_probable(rep2["candidates"]) and rep2["candidates"][0]["year"] == 2000


def test_command_line_json(tmp_path, imdb_dir):
    disc = fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED"), with_thumb=False)
    env = dict(os.environ, PYTHONPATH=ROOT)
    out = subprocess.run([sys.executable, "-m", "disc_detective", disc, "--json", "--offline", "--no-record",
                          "--imdb-dir", imdb_dir], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    rep = json.loads(out.stdout)
    assert rep["kind"] == "tv" and rep["format"] == "Blu-ray"


def test_makemkv_log_mapping(tmp_path):
    log = open(os.path.join(os.path.dirname(__file__), "data", "makemkv_psych_disc02.txt")).read()
    mk = core.parse_makemkv_log(log)
    assert mk["disc_name"] == "PSYCH" and len(mk["titles"]) == 13
    assert mk["titles"][3]["duration"] == 42 * 60 + 47 and mk["titles"][3]["dvd_title"] == 4
    ours = [{"title_id": "Title %d" % i, "role": "episode", "duration": t["duration"]}
            for i, t in enumerate(mk["titles"], 1)]
    pairs = core.map_makemkv(mk["titles"], ours, "DVD")
    assert pairs[3][0]["title_id"] == "Title 4" and pairs[3][1] == "source"
