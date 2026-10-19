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


def test_film_in_reels_is_a_film_not_tv(tmp_path, imdb_dir):
    """Chicken Run: one feature playlist of four ~21-minute reels looked like 4 episodes."""
    disc = fixtures.make_reel_movie_bluray(str(tmp_path / "CHICKEN_RUN"))
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    assert rep["kind"] == "movie"
    roles = {t["id"]: t["role"] for t in rep["titles"]}
    assert roles["00800.mpls"] == "feature"
    assert sorted(i for i, r in roles.items() if r == "part") == ["00101.m2ts", "00102.m2ts", "00103.m2ts", "00104.m2ts"]
    assert not any(r == "episode" for r in roles.values())
    assert rep["candidates"][0]["name"] == "Chicken Run" and rep["candidates"][0]["year"] == 2000
    assert rep["facts"]["feature_minutes"] == 84 and "episode_count" not in rep["facts"]
    assert rep["plex_names"][0].endswith("Chicken Run (2000).mkv")


def test_your_answer_without_a_season_means_film(tmp_path, imdb_dir):
    disc = fixtures.make_reel_movie_bluray(str(tmp_path / "MYSTERY"), name="Disc 1")
    rep, args = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    con = core.library_open(args.db)
    core.set_field(con, rep["content_hash"][:8], "truth", "Chicken Run (2000)")
    con.close()
    rep2, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    assert rep2["kind"] == "movie" and rep2["candidates"][0]["name"] == "Chicken Run"
    assert {t["id"]: t["role"] for t in rep2["titles"]}["00800.mpls"] == "feature"


def test_collection_relabels_the_films(tmp_path, imdb):
    """Short films become features (identify copies titles into the report after this)."""
    import json as _json
    disc = _json.load(open(os.path.join(os.path.dirname(__file__), "data", "wallace_gromit_titles.json")))
    titles = disc["titles"]
    coll = core.imdb_collection(imdb, [("Wallace And Gromit", False)], titles, disc["facts"])
    core.apply_collection(coll, titles)
    assert [t["id"] for t in titles if t["role"] == "feature"] == ["Title 2", "Title 3", "Title 6"]


def test_exact_pick_is_used_as_is(tmp_path, imdb_dir, imdb):
    """Picking 'Hercules (2014)' from the list must give that film, not the better-known 1997 one."""
    disc = fixtures.make_movie_bluray(str(tmp_path / "HERCULES"), minutes=95, seed=21)
    rep, args = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    pick = next(c for c in core.imdb_lookup(imdb, "Hercules") if c["year"] == 2014)
    core.set_truth(args.db, rep["content_hash"], core.truth_for(pick))
    rep2, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True)
    top = rep2["candidates"][0]
    assert (top["name"], top["year"], top["imdb_id"]) == ("Hercules", 2014, pick["imdb_id"])
    assert rep2["plex_names"][0].endswith("Hercules (2014).mkv")


def test_a_close_call_still_gets_names_and_a_plan(tmp_path, imdb_dir):
    """Hercules 1997 vs 2014 score close: not 'confident', but the top guess is used anyway
    (you correct it with Not right?), so names, plan and picture don't wait for a click."""
    disc = fixtures.make_movie_bluray(str(tmp_path / "HERCULES"), minutes=95, seed=31)
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="HERCULES")
    assert not core.is_probable(rep["candidates"])
    assert rep["status"].startswith("best guess: Hercules (1997)")
    assert rep["match"]["name"] == "Hercules" and rep["match"]["year"] == 1997 and not rep["match"]["sure"]
    assert rep["plex_names"][0].endswith("Hercules (1997).mkv")
    assert rep["best_guess"] == "Hercules (1997)"


def test_weak_guesses_are_not_acted_on():
    weak = [{"kind": "movie", "name": "X", "year": 1990, "score": 30}]
    assert core.adopt_guess(weak) is None and core.adopt_guess([]) is None
    assert core.adopt_guess([dict(weak[0], score=50)])["name"] == "X"


def _shorts(tmp_path, imdb_dir):
    disc = fixtures.make_shorts_bluray(str(tmp_path / "WALLACE_AND_GROMIT"))
    rep, args = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="WALLACE_AND_GROMIT")
    return disc, rep, args


def films_of(rep):
    return [(t["id"], t["role"]) for t in rep["titles"] if t["role"] in ("feature", "version")]


def test_short_films_collection(tmp_path, imdb_dir):
    disc, rep, args = _shorts(tmp_path, imdb_dir)
    assert rep["kind"] == "collection" and rep["match"]["kind"] == "collection"
    top = rep["candidates"][0]
    assert [(i["title_id"], i["name"]) for i in top["items"]] == [
        ("00001.mpls", "A Grand Day Out"), ("00002.mpls", "Wallace & Gromit: The Wrong Trousers"),
        ("00003.mpls", "Wallace & Gromit: A Close Shave")]
    assert films_of(rep) == [("00001.mpls", "feature"), ("00002.mpls", "feature"), ("00003.mpls", "feature"),
                             ("00004.mpls", "version")]
    assert len(rep["plex_names"]) == 3


def test_confirmed_collection_rebuilds_from_its_ids(tmp_path, imdb_dir):
    disc, rep, args = _shorts(tmp_path, imdb_dir)
    films = [(i["title_id"], i["imdb_id"]) for i in rep["candidates"][0]["items"]]
    truth = core.truth_for_collection(rep["candidates"][0]["name"], films)
    assert truth.startswith("Wallace & Gromit Collection {films:00001.mpls=tt")
    core.set_truth(args.db, rep["content_hash"], truth)
    rep2, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="WALLACE_AND_GROMIT")
    assert rep2["status"] == "identified (your library)" and rep2["kind"] == "collection"
    assert rep2["candidates"][0]["why"].startswith("your answer")
    assert [(i["title_id"], i["imdb_id"]) for i in rep2["candidates"][0]["items"]] == films
    assert rep2["best_guess"] == "Wallace & Gromit Collection"


def test_fixing_one_title_of_a_collection(tmp_path, imdb_dir):
    """The Wrong Trousers and A Close Shave run the same length: swap them, then drop one."""
    disc, rep, args = _shorts(tmp_path, imdb_dir)
    ids = {i["title_id"]: i["imdb_id"] for i in rep["candidates"][0]["items"]}
    swapped = [("00001.mpls", ids["00001.mpls"]), ("00002.mpls", ids["00003.mpls"]), ("00003.mpls", ids["00002.mpls"])]
    core.set_truth(args.db, rep["content_hash"], core.truth_for_collection("Wallace & Gromit - 3 short films", swapped))
    rep2, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="WALLACE_AND_GROMIT")
    names = {i["title_id"]: i["name"] for i in rep2["candidates"][0]["items"]}
    assert names["00002.mpls"].endswith("A Close Shave") and names["00003.mpls"].endswith("The Wrong Trousers")
    two = swapped[:2]                                    # 00003 is not a film after all
    t2 = core.truth_for_collection("Wallace & Gromit Collection", two)
    assert t2.startswith("Wallace & Gromit Collection {films:")
    core.set_truth(args.db, rep["content_hash"], t2)
    rep3, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="WALLACE_AND_GROMIT")
    roles = dict((t["id"], t["role"]) for t in rep3["titles"])
    assert roles["00001.mpls"] == roles["00002.mpls"] == "feature"
    assert roles["00003.mpls"] == "extra" and len(rep3["plex_names"]) == 2


def test_collection_names():
    assert core.collection_name("Wallace And Gromit") == "Wallace & Gromit Collection"
    assert core.collection_name("Christmas Cartoon Collection") == "Christmas Cartoon Collection"


def test_collection_answer_text():
    pt = core.parse_truth("Wallace & Gromit - 2 short films {films:Title 2=tt0104361|Title 3=tt0108598}")
    assert pt["films"] == [("Title 2", "tt0104361"), ("Title 3", "tt0108598")]
    assert pt["title"] == "Wallace & Gromit - 2 short films" and pt["season"] is None
    assert core.strip_tags("W {films:Title 2=tt1}") == "W"


def test_exact_series_name_beats_a_partial_film_match(tmp_path, imdb_dir):
    """PSYCH S1 D1: a 78-minute extended pilot and a 66-minute cut make the disc look like a
    film, and Psycho Cop Returns (80 min) fits that length - but "Psych" is exactly a
    well-known series, while every film is only a partial name match."""
    disc = fixtures.make_movie_bluray(str(tmp_path / "PSYCH"), minutes=78)
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="PSYCH")
    assert rep["kind"] == "tv" and rep["candidates"][0]["name"] == "Psych"
    assert sum(1 for t in rep["titles"] if t["role"] == "episode") == 2      # pilot cut + documentary-length
    # an exact film name stays a film: the same disc labelled CHICKEN_RUN
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="CHICKEN_RUN")
    assert rep["kind"] == "movie"
