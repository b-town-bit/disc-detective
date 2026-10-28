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
    assert rep2["status"] == "identified (your case files)"
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


def test_collection_beats_one_of_its_own_shorts(tmp_path, imdb_dir):
    """The real disc also has a title no short explains, which lowers the collection's score
    below A Close Shave's on its own (its runtime matches a title exactly). The short is one of
    the collection's films, so the collection stands instead of the disc becoming that one film."""
    disc = fixtures.make_shorts_bluray(str(tmp_path / "WALLACE_AND_GROMIT"), compilation=True)
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="WALLACE_AND_GROMIT")
    assert rep["kind"] == "collection" and rep["match"]["kind"] == "collection"
    assert [i["name"] for i in rep["candidates"][0]["items"]] == [
        "A Grand Day Out", "Wallace & Gromit: The Wrong Trousers", "Wallace & Gromit: A Close Shave"]
    assert len(rep["plex_names"]) == 3


def test_confirmed_collection_rebuilds_from_its_ids(tmp_path, imdb_dir):
    disc, rep, args = _shorts(tmp_path, imdb_dir)
    films = [(i["title_id"], i["imdb_id"]) for i in rep["candidates"][0]["items"]]
    truth = core.truth_for_collection(rep["candidates"][0]["name"], films)
    assert truth.startswith("Wallace & Gromit Collection {films:00001.mpls=tt")
    core.set_truth(args.db, rep["content_hash"], truth)
    rep2, _ = run(disc, tmp_path, imdb_dir=imdb_dir, offline=True, label="WALLACE_AND_GROMIT")
    assert rep2["status"] == "identified (your case files)" and rep2["kind"] == "collection"
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


def test_a_bonus_disc_is_extras_named_after_its_film_disc(tmp_path, imdb_dir):
    """SWISS_FAMILY_ROBINSON_BONUS: shaped like a TV disc (a documentary, some 20-minute pieces),
    but the label says it's the set's bonus disc: all extras, under the film disc's answer."""
    film = fixtures.make_movie_bluray(str(tmp_path / "GONE_IN_60_SECONDS"), minutes=118, seed=3)
    rep, args = run(film, tmp_path, imdb_dir=imdb_dir, offline=True, label="GONE_IN_60_SECONDS")
    assert core.is_probable(rep["candidates"]) and rep["candidates"][0]["year"] == 2000
    bonus = fixtures.make_shorts_bluray(str(tmp_path / "GONE_IN_60_SECONDS_BONUS"))
    rep2, _ = run(bonus, tmp_path, imdb_dir=imdb_dir, offline=True, label="GONE_IN_60_SECONDS_BONUS")
    assert rep2["label_parsed"]["bonus"] and rep2["extras_disc"] and rep2["kind"] == "movie"
    assert not [t for t in rep2["titles"] if t["role"] in ("feature", "episode", "version")]
    assert rep2["bonus_of"].startswith("Gone in 60 Seconds (2000) {imdb-tt")
    assert rep2["best_guess"] == "Gone in 60 Seconds (2000)"
    assert rep2["identified_by"] == "your case files (the set's film disc)"
    # with no film disc in the library: still extras, searched under the title without "Bonus"
    rep3, _ = run(bonus, tmp_path, imdb_dir=imdb_dir, offline=True, label="GONE_IN_60_SECONDS_BONUS",
                  db=str(tmp_path / "empty.sqlite"))
    assert rep3["extras_disc"] and rep3["search_queries"][0] == "Gone In 60 Seconds" and not rep3.get("bonus_of")


def test_a_short_can_premiere_on_its_own_disc(tmp_path, imdb):
    """Kung Fu Panda: Secrets of the Scroll (2016) premiered on a disc mastered in October 2015:
    a short made for the set comes out after its disc is mastered, so a year's grace."""
    disc = fixtures.make_shorts_bluray(str(tmp_path / "WALLACE_AND_GROMIT"))
    fmt, base = core.detect_format(disc)
    titles = core.parse_bluray(base)["titles"]
    core.classify(titles, fmt)
    for mastered, years in (("1994-06", [1989, 1993, 1995]), ("1993-06", [1989, 1993])):
        c = core.imdb_collection(imdb, [("Wallace And Gromit", False)], titles, {"mastered": mastered})
        assert [i["year"] for i in c["items"]] == years, mastered      # two years after: still too late


def test_shorts_can_run_newest_first(imdb):
    """The Kung Fu Panda Secrets disc runs its shorts newest first. Here: Loaf and Death (2008),
    A Close Shave, The Wrong Trousers, then A Grand Day Out (1989), the only 23-minute one."""
    mk = lambda i, secs: {"id": "%05d.mpls" % i, "role": "episode", "duration": secs, "segments": ((str(i), 0, 1),)}
    titles = [mk(1, 1800), mk(2, 1830), mk(3, 1860), mk(4, 23 * 60)]
    c = core.imdb_collection(imdb, [("Wallace And Gromit", False)], titles, {"mastered": "2010-01"})
    assert [i["year"] for i in c["items"]] == [2008, 1995, 1993, 1989]
    assert "newest first" in c["why"]


def test_interactual_disc_id_names_the_film(tmp_path):
    """Spider-Man (2002), disc 1: volume label DVD_VIDEO, but InterActual Player's disc.id (its
    DVD-ROM software) names the film. Any other disc.id, or none, says nothing."""
    (tmp_path / "disc.id").write_bytes(b"[InterActual]\r\nid=100000150000180000018\r\nver=1.00.02.0731\r\n"
                                       b"name=Spiderman 2002 disc 1\r\nowner=Coulumbia Tri Star Home Video\r\n")
    assert core.interactual_name(str(tmp_path)) == "Spiderman 2002 disc 1"
    lp = core.parse_label(core.interactual_name(str(tmp_path)))
    assert (lp["query"], lp["year"], lp["disc"]) == ("Spiderman", 2002, 1)
    (tmp_path / "disc.id").write_bytes(b"name=Something Else\r\n")
    assert core.interactual_name(str(tmp_path)) is None
    (tmp_path / "disc.id").unlink()
    assert core.interactual_name(str(tmp_path)) is None


def test_a_dvds_own_text_names_the_film(tmp_path):
    """Spider-Man 2: volume label DVD_VIDEO, no DVD-ROM files, but VIDEO_TS.IFO points to text
    data naming the disc; a picture format in it isn't the name (Muppets from Space's
    "MUPPETS PAN-SCAN"); no text data, no name."""
    vts = tmp_path / "VIDEO_TS"
    vts.mkdir()

    def ifo(text, sector=1):
        head = bytearray(b"DVDVIDEO-VMG" + bytes(2048 - 12))
        head[0xD4:0xD8] = sector.to_bytes(4, "big")
        txt = b"Sony" + bytes(8) + b"\x00\x01\x00\x00\x01Aen" + bytes(40) + b"\x00\x3b" + text + b"\t" + text + b"\t"
        (vts / "VIDEO_TS.IFO").write_bytes(bytes(head) + txt.ljust(2048, b"\x00"))
    ifo(b"Spider-Man 2 (Special Edition)")
    assert core.dvd_text_name(str(vts)) == "Spider-Man 2 (Special Edition)"
    assert core.parse_label(core.dvd_text_name(str(vts)))["query"] == "Spider Man 2"
    ifo(b"MUPPETS PAN-SCAN")
    assert core.dvd_text_name(str(vts)) == "MUPPETS"
    ifo(b"Anything", sector=0)
    assert core.dvd_text_name(str(vts)) is None


def test_an_editions_name_isnt_the_titles():
    """IRON_GIANT_SIGNATURE_EDITION is The Iron Giant: an edition's name goes with the EDITION,
    CUT or VERSION after it; the same words in a title stay (Ultimate Avengers, Blood Diamond)."""
    q = lambda label: core.parse_label(label)["query"]
    assert q("IRON_GIANT_SIGNATURE_EDITION") == "Iron Giant"
    assert q("ALIEN_25TH_ANNIVERSARY_EDITION") == "Alien"
    assert q("BLADE_RUNNER_FINAL_CUT") == "Blade Runner"
    assert q("ULTIMATE_AVENGERS") == "Ultimate Avengers"
    assert q("BLOOD_DIAMOND") == "Blood Diamond"
    assert q("SPIDER_MAN_2_SPECIAL_EDITION") == "Spider Man 2"            # a sequel's number stays


def test_a_disc_with_none_of_its_film_is_its_extras_disc():
    """Speed Racer's DVD game disc (SRTMDGFF2_DES): named Speed Racer by you, but nothing on it
    is the film (its longest title 30 minutes, the film 135): the film's special-features disc.
    A 47-minute special whose one title is the special stays the film."""
    game = [{"id": "Title %d" % n, "role": "extra", "duration": d} for n, d in ((1, 397), (2, 1816), (3, 305))]
    film = {"name": "Speed Racer", "year": 2008, "kind": "movie", "runtime": 135}
    assert core.not_the_film({"kind": "unknown"}, game, film)
    assert core.not_the_film({"kind": "unknown"}, game, dict(film, runtime=None))     # (90 minutes, then)
    special = [{"id": "Title 1", "role": "extra", "duration": 47 * 60}]
    assert not core.not_the_film({"kind": "unknown"}, special, {"kind": "movie", "runtime": 47})
    # a disc the scan found the film on, a show, or one already an extras disc: not this
    assert not core.not_the_film({"kind": "unknown"}, game + [{"id": "Title 9", "role": "feature", "duration": 8100}], film)
    assert not core.not_the_film({"kind": "movie"}, game, film)
    assert not core.not_the_film({"kind": "unknown"}, game, dict(film, kind="tv"))
    assert not core.not_the_film({"kind": "unknown", "extras_disc": True}, game, film)


def test_a_films_credits_as_words(tmp_path, monkeypatch):
    """The film's people (cast, characters, crew), from TMDB once, kept in your case files:
    words for reading its menus ("Hogarth Hughes", The Iron Giant's)."""
    import json
    calls = []

    def fake_get(key, path, **params):
        calls.append(path)
        if path.startswith("/find/"):
            return {"movie_results": [{"id": 10386}], "tv_results": []}
        return {"cast": [{"name": "Eli Marienthal", "character": "Hogarth Hughes"},
                         {"name": "Christopher McDonald", "character": "Kent Mansley"}],
                "crew": [{"name": "Brad Bird", "job": "Director"}]}
    monkeypatch.setattr(core, "tmdb_get", fake_get)
    db, imdb = str(tmp_path / "library.sqlite"), str(tmp_path / "imdb")
    words = core.film_credit_words("tt0129167", "KEY", home=str(tmp_path), db=db, imdb_dir=imdb)
    assert {"hogarth", "hughes", "mansley", "bird", "marienthal"} <= words
    con = core.library_open(db)
    assert ("Brad Bird", None, "director") in core.film_credits(con, "tt0129167")
    assert con.execute("SELECT source FROM credits_fetched").fetchone()[0] == "tmdb"
    calls.clear()                               # kept: no TMDB the next time, nor offline
    assert core.film_credit_words("tt0129167", None, online=False, home=str(tmp_path), db=db, imdb_dir=imdb) == words
    assert not calls
    assert core.film_credit_words(None, "KEY", db=db) == set()
    # none to be had (no key): not marked as fetched, asked again another time
    assert core.film_credits(con, "tt0000001", None, imdb, home=str(tmp_path)) == []
    assert con.execute("SELECT 1 FROM credits_fetched WHERE imdb_id='tt0000001'").fetchone() is None


def test_the_imdb_credits_index(tmp_path, monkeypatch):
    """IMDb's credits, read as they download: the well-known titles' cast and crew, searchable by
    name and by character; a film's credits from it, not TMDB, when it's there."""
    import sqlite3
    idx = sqlite3.connect(":memory:")
    idx.executescript("CREATE TABLE titles (id INTEGER PRIMARY KEY); CREATE TABLE ratings (id INTEGER PRIMARY KEY, votes INT);"
                      "INSERT INTO titles VALUES (129167), (5); INSERT INTO ratings VALUES (129167, 200000), (5, 3);")
    monkeypatch.setattr(core, "imdb_open", lambda *a, **k: idx)
    rows = {"title.principals.tsv.gz": [
                ["tt0129167", "1", "nm0547093", "actor", "\\N", '["Hogarth Hughes"]'],
                ["tt0129167", "2", "nm0083348", "director", "\\N", "\\N"],
                ["tt0129167", "3", "nm0000001", "cinematographer", "\\N", "\\N"],     # (not kept: the job)
                ["tt0000005", "1", "nm0000002", "actor", "\\N", '["Somebody"]']],        # (not kept: too few votes)
            "name.basics.tsv.gz": [["nm0547093", "Eli Marienthal"], ["nm0083348", "Brad Bird"], ["nm0000002", "X"]]}
    monkeypatch.setattr(core, "_remote_tsv", lambda name: iter(rows[name]))
    imdb = tmp_path / "imdb"
    imdb.mkdir()
    info = core.imdb_credits_build(str(imdb), log=lambda m: None)
    assert info["people"] == 2 and info["current"]
    con = core.imdb_credits_open(str(imdb))
    assert con.execute("SELECT rowid FROM people_fts WHERE people_fts MATCH 'marienthal'").fetchone()[0] == 547093
    assert con.execute("SELECT title FROM credits WHERE rowid IN (SELECT rowid FROM characters_fts "
                       "WHERE characters_fts MATCH 'hogarth')").fetchone()[0] == 129167
    con.close()
    monkeypatch.setattr(core, "tmdb_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no TMDB")))
    lib = core.library_open(str(tmp_path / "library.sqlite"))
    got = core.film_credits(lib, "tt0129167", "KEY", str(imdb), home=str(tmp_path))
    assert sorted(got) == [("Brad Bird", None, "director"), ("Eli Marienthal", "Hogarth Hughes", "actor")]
    assert lib.execute("SELECT source FROM credits_fetched").fetchone()[0] == "imdb"
