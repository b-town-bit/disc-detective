"""Ranking IMDb candidates from the label and the disc's facts."""
import json
import os
from types import SimpleNamespace

import pytest

from disc_detective import core

DATA = os.path.join(os.path.dirname(__file__), "data")


@pytest.mark.parametrize("query, facts, expected", [
    ("Maverick", {"mastered": "2011-03", "feature_minutes": 127}, ("Maverick", 1994)),
    ("Gone In 60 Seconds", {"mastered": "2002-05", "feature_minutes": 118}, ("Gone in 60 Seconds", 2000)),
    ("The Prince Of Egypt", {"mastered": "2018-01", "feature_minutes": 99}, ("The Prince of Egypt", 1998)),
    ("Shrek 2", {"mastered": "2004-10", "feature_minutes": 93}, ("Shrek 2", 2004)),
    ("Rocky III", {"mastered": "2001-01", "feature_minutes": 99}, ("Rocky III", 1982)),
    ("Apollo 13", {"mastered": "1998-01", "feature_minutes": 140}, ("Apollo 13", 1995)),
    # sequel number not in the title: the 2nd film of the franchise by release order
    ("An American Tail 2", {"mastered": "2003-12", "feature_minutes": 75},
     ("An American Tail: Fievel Goes West", 1991)),
])
def test_movies(imdb, query, facts, expected):
    c = core.imdb_candidates(imdb, [(query, False)], "movie", facts)
    assert core.is_probable(c), c[:2]
    assert (c[0]["name"], c[0]["year"]) == expected


def test_run_together_label_words(imdb):
    """HARRYPOTTER3: no title has the word "harrypotter", so split it into title words."""
    assert core.split_runon(imdb, "Princeofegypt") == "Prince Of Egypt"
    assert core.split_runon(imdb, "Shrek 2") is None                  # real words stay as they are
    c = core.imdb_candidates(imdb, [("Theprinceofegypt", False)], "movie", {"mastered": "2018-01", "feature_minutes": 99})
    assert core.is_probable(c) and c[0]["name"] == "The Prince of Egypt"
    # the label also lost a small word and an apostrophe: spelled as the title writes it
    assert core.split_runon(imdb, "Itswonderfullife Dom") == "It's a Wonderful Life Dom"
    c = core.imdb_candidates(imdb, [("Itswonderfullife", False)], "movie", {"mastered": "2007-09", "feature_minutes": 131})
    assert core.is_probable(c) and c[0]["name"] == "It's a Wonderful Life"
    # two small words dropped (FATHERBRIDE_US: "of the")
    assert core.split_runon(imdb, "Fatherbride") == "Father of the Bride"
    c = core.imdb_candidates(imdb, [("Fatherbride", False)], "movie", {"mastered": "2005-04", "feature_minutes": 105})
    assert (c[0]["name"], c[0]["year"]) == ("Father of the Bride", 1991)
    # any words dropped between, as long as a known title has the rest in that order (a label cut to fit)
    assert core.split_runon(imdb, "Mengoats") == "Men Who Stare at Goats"
    assert core.split_runon(imdb, "Bonusdisc") is None
    assert core.split_runon(imdb, "Kingspeech") == "King's Speech"            # "king" in "King's"


def test_a_film_number_alone_is_a_guess(imdb):
    """Counting films by number needs the disc to agree: the runtime close, or the series' closest,
    or the label's own words in the title. Otherwise it's only a guess to check."""
    loose = {"mastered": "2003-12", "feature_minutes": 88}       # nearer the 3rd film's 83 min
    c = core.imdb_candidates(imdb, [("An American Tail 2", False)], "movie", loose)
    assert not core.is_probable(c) and core.adopt_guess(c)["year"] == 1991
    # no runtime to check: a little-known look-alike (1988) still isn't counted as one of its films
    c = core.imdb_candidates(imdb, [("An American Tail 2", False)], "movie", {"mastered": "2003-12"})
    assert not core.is_probable(c) and core.adopt_guess(c)["name"] == "An American Tail: Fievel Goes West"
    c = core.imdb_initials(imdb, "AT2_FIEVEL", {"mastered": "2003-12"})       # HP5_PHOENIX: the word confirms
    assert core.is_probable(c) and c[0]["year"] == 1991
    assert not core.is_probable(core.imdb_initials(imdb, "AT2", loose))


def test_one_word_series_label_goes_by_runtime(imdb):
    """MUPPET_US: the label names only the series. No film named by it runs as long as the disc,
    so the series' closest runtime (either cut: theatrical 86, extended 89) is the guess."""
    facts = {"mastered": "2005-09", "feature_minutes": 89,
             "feature_cuts": core.feature_cuts([85.8 * 60, 88.9 * 60])}
    c = core.imdb_candidates(imdb, [("Gizmo", False)], "movie", facts)
    assert c[0]["name"] == "The Gizmo Christmas Carol" and not core.is_probable(c)
    # a film that does run as long keeps its place
    c = core.imdb_candidates(imdb, [("Gizmo", False)], "movie", {"mastered": "2005-09", "feature_minutes": 95})
    assert c[0]["name"] == "The Gizmo Movie"


def test_franchise_needs_whole_words(imdb):
    c = core.imdb_candidates(imdb, [("An American Tail 2", False)], "movie", {"mastered": "2003-12", "feature_minutes": 75})
    assert all("Tailor" not in x["name"] for x in c[:1])


def test_tv_and_season_names(imdb):
    c = core.imdb_candidates(imdb, [("Psych", False)], "tv", {"mastered": "2007-01", "episode_minutes": 43})
    assert core.is_probable(c) and c[0]["name"] == "Psych"
    guess = core.imdb_season(imdb, c[0], 1, 2, 3)
    assert guess["guess"][0].startswith("S01E04")


def test_episode_zero_is_not_the_first_episode_on_disc_one(imdb):
    """Smallville S1 on IMDb starts with E00 Unaired Pilot; disc 1 starts with E01 Pilot."""
    c = core.imdb_candidates(imdb, [("Smallville", False)], "tv", {"mastered": "2002-09", "episode_minutes": 43})
    assert c[0]["name"] == "Smallville"
    guess = core.imdb_season(imdb, c[0], 1, 1, 4)
    assert guess["guess"] == ["S01E01 Pilot", "S01E02 Metamorphosis", "S01E03 Hothead", "S01E04 X-Ray"]
    assert core.imdb_season(imdb, c[0], 1, 2, 4)["guess"] == ["S01E05 Cool"]   # 4 per disc


def test_short_film_collection(imdb):
    disc = json.load(open(os.path.join(DATA, "wallace_gromit_titles.json")))
    titles, facts = disc["titles"], disc["facts"]
    q = [("Wallace And Gromit", False)]
    coll = core.imdb_collection(imdb, q, titles, facts)
    cands = [coll] + core.imdb_candidates(imdb, q, "tv", facts)
    assert core.is_probable(cands)
    assert [(i["title_id"], i["name"]) for i in coll["items"]] == [
        ("Title 2", "A Grand Day Out"), ("Title 3", "Wallace & Gromit: The Wrong Trousers"),
        ("Title 6", "Wallace & Gromit: A Close Shave")]
    assert coll["versions"] == [{"title_id": "Title 7", "same_as": "Title 6"}]
    names = core.apply_collection(coll, titles)
    assert len(names) == 3 and all("{imdb-tt" in n for n in names)
    assert {t["id"]: t["role"] for t in titles}["Title 16"] == "extra"


def test_a_tv_disc_is_not_a_collection(imdb):
    eps = [dict(id="Title %d" % i, role="episode", duration=d) for i, d in enumerate([2650, 2640, 2655, 2648], 1)]
    assert core.imdb_collection(imdb, [("Psych", False)], eps, {"mastered": "2007-01"}) is None


def test_tmdb_fallback_ranking(monkeypatch):
    fake = {("/search/movie", "Anasta"): {"results": [
                {"id": 9444, "title": "Anastasia", "release_date": "1997-11-20"},
                {"id": 13325, "title": "Anastasia", "release_date": "1956-12-13"}]},
            ("/movie/9444", None): {"runtime": 94}, ("/movie/13325", None): {"runtime": 105}}
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **p: fake.get((path, p.get("query")), {"results": []}))
    c = core.tmdb_candidates("k", [("Anasta", True)], "movie", {"mastered": "1999-09", "feature_minutes": 94})
    assert c[0]["year"] == 1997


def test_type_ahead_finds_every_hercules(imdb):
    found = core.imdb_lookup(imdb, "Hercules")
    years = [(c["name"], c["year"], c["kind"]) for c in found if c["name"] == "Hercules"]
    assert ("Hercules", 1997, "movie") in years and ("Hercules", 1998, "tv") in years
    assert found[0]["year"] == 1997                     # most known first among exact names
    assert core.imdb_lookup(imdb, "H") == []


def test_exact_pick_round_trip(imdb):
    pick = next(c for c in core.imdb_lookup(imdb, "Hercules") if c["year"] == 2014)
    truth = core.truth_for(pick)
    assert truth == "Hercules (2014) {imdb-%s}" % pick["imdb_id"]
    pt = core.parse_truth(truth)
    assert (pt["title"], pt["year"], pt["imdb_id"]) == ("Hercules", 2014, pick["imdb_id"])
    c = core.imdb_by_id(imdb, pick["imdb_id"])
    assert (c["name"], c["year"], c["score"]) == ("Hercules", 2014, 100.0)
    tv = core.truth_for(dict(pick, kind="tv"), 1, 2)
    assert core.parse_truth(tv)["season"] == 1 and core.parse_truth(tv)["disc"] == 2


def test_tmdb_matches_carry_their_imdb_id(monkeypatch):
    fake = {("/search/movie", "Hercules"): {"results": [{"id": 11970, "title": "Hercules", "release_date": "1997-06-13",
                                                         "poster_path": "/h.jpg"}]},
            ("/movie/11970", None): {"runtime": 93, "imdb_id": "tt0119282"}}
    seen = []
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **p: seen.append(p) or fake.get((path, p.get("query")), {}))
    c = core.tmdb_candidates("k", [("Hercules", False)], "movie", {"mastered": "2000-01", "feature_minutes": 93})
    assert c[0]["imdb_id"] == "tt0119282" and c[0]["poster_path"] == "/h.jpg"
    assert any(p.get("append_to_response") == "external_ids" for p in seen)


def test_tmdb_guesses_take_the_imdb_title(imdb, imdb_dir):
    """TMDB's English title can be the British one (Philosopher's Stone); IMDb's is the
    American (Sorcerer's Stone), and the IMDb id is what names the file: use IMDb's."""
    n = imdb.execute("SELECT id FROM titles WHERE title='Hercules' AND year=1997").fetchone()[0]
    cands = [{"source": "TMDB", "kind": "movie", "imdb_id": core.tt(n), "name": "Hercules (UK title)", "year": 1997},
             {"source": "TMDB", "kind": "movie", "imdb_id": None, "name": "No IMDb id", "year": 2000}]
    core.imdb_names(SimpleNamespace(imdb_dir=imdb_dir), cands)
    assert (cands[0]["name"], cands[0]["tmdb_name"]) == ("Hercules", "Hercules (UK title)")
    assert cands[1]["name"] == "No IMDb id"


def test_code_labels_try_the_title_initials(imdb):
    """BCS0NNW1: no words to search, but B C = The Black Cauldron (80 min, by far the best known)."""
    facts = {"mastered": "2010-05", "feature_minutes": 80}
    c = core.imdb_initials(imdb, "BCS0NNW1", facts)
    assert c[0]["name"] == "The Black Cauldron" and core.adopt_guess(c) is c[0]
    assert not core.is_probable(c)                      # a best guess, not a confident match
    assert all(x["year"] <= 2010 for x in c)            # Big City (2012) is after the mastering date
    assert core.imdb_initials(imdb, "SHREK", facts) == []          # a word, not a code
    assert core.imdb_initials(imdb, "SISTER_ACT2", facts) == []    # words with a number, not initials
    assert core.imdb_initials(imdb, "BLACKCAUL2", facts) == []     # too long a letter run for initials
    assert core.imdb_initials(imdb, "BCS0NNW1", {"mastered": "2010-05"}) == []   # no feature length


def test_initials_with_a_film_number(imdb):
    """HP4 = the 4th Harry Potter film: the franchise whose titles start with those initials,
    its films by release year (not look-alikes with a sliver of its votes); runtime has to fit."""
    c = core.imdb_initials(imdb, "AT2", {"mastered": "2003-12", "feature_minutes": 75})
    assert core.is_probable(c) and (c[0]["name"], c[0]["year"]) == ("An American Tail: Fievel Goes West", 1991)
    assert core.imdb_initials(imdb, "AT_2", {"mastered": "2003-12"})[0]["year"] == 1991    # no runtime: still
    assert core.imdb_initials(imdb, "AT2", {"mastered": "2003-12", "feature_minutes": 140}) == []   # not this film


def test_initials_can_skip_the_first_word(imdb):
    """NGE0NNW1: N G = (The Emperor's) New Groove. A title that starts with the initials ranks
    above one that has them after its first word, unless the latter is far better known."""
    c = core.imdb_initials(imdb, "NGE0NNW1", {"mastered": "2005-09", "feature_minutes": 78})
    assert c[0]["name"] == "The Emperor's New Groove" and core.adopt_guess(c) is c[0]
    assert {"Kronk's New Groove", "No Greater Glory"} <= {x["name"] for x in c}
    # The Nightmare Before Christmas (N B C) is better known, but B C starts The Black Cauldron
    c = core.imdb_initials(imdb, "BCS0NNW1", {"mastered": "2005-09", "feature_minutes": 79})
    assert c[0]["name"] == "The Black Cauldron"


def test_one_word_titles_are_coded_by_their_consonants(imdb):
    """TZN0NNW1: no film has the initials T Z N; it is TarZaN (first and last letters with some
    in between). It beats Torrid Zone's T Z because it is far better known, while a little-known
    one-word match (NiGhtmarE) doesn't beat the initials N G of The Emperor's New Groove."""
    c = core.imdb_initials(imdb, "TZN0NNW1", {"mastered": "2005-09", "feature_minutes": 88})
    assert c[0]["name"] == "Tarzan" and core.adopt_guess(c) is c[0]
    assert "Torrid Zone" in [x["name"] for x in c]
    assert "Tarzan Escapes" not in [x["name"] for x in c]
    c = core.imdb_initials(imdb, "NGE0NNW1", {"mastered": "2005-09", "feature_minutes": 79})
    assert c[0]["name"] == "The Emperor's New Groove"
    # the letters needn't end the word: RKM0NNF1 is RocKetMan, not the R K of Repli-Kate
    c = core.imdb_initials(imdb, "RKM0NNF1", {"mastered": "2005-02", "feature_minutes": 94})
    assert c[0]["name"] == "RocketMan" and "Repli-Kate" in [x["name"] for x in c]


def test_disneys_code_with_separators_and_a_longer_title(imdb):
    """SRC-0N-NW1.1_DES: Disney's code (0N NW1) written with separators is still a code, and its
    letters can spell out a longer title's first word: The SoRCerer's Apprentice, far better known
    than Scarecrow, which has the letters in one word."""
    c = core.imdb_initials(imdb, "SRC-0N-NW1.1_DES", {"mastered": "2010-10", "feature_minutes": 109})
    assert (c[0]["name"], c[0]["year"]) == ("The Sorcerer's Apprentice", 2010) and core.adopt_guess(c) is c[0]
    assert "Scarecrow" in [x["name"] for x in c]
    # not Disney's code: words with separators aren't initials
    assert core.imdb_initials(imdb, "SRC-DISC-1", {"mastered": "2010-10", "feature_minutes": 109}) == []


def test_initials_also_list_the_alternatives(imdb):
    c = core.imdb_initials(imdb, "BC2", {"mastered": "2010-05", "feature_minutes": 79})
    names = [x["name"] for x in c]
    assert "Booty Call" in names


def test_numbered_sequel_title_beats_counting_by_year(imdb):
    """POCAHONTAS_II: counting "Pocahontas" films by year makes #2 the 1995 Disney film (a 1994
    look-alike came first); the title that carries the number is the sequel."""
    c = core.imdb_candidates(imdb, [("Pocahontas Ii", False)], "movie", {"feature_minutes": 73, "mastered": "2012-06"})
    assert c[0]["name"] == "Pocahontas 2: Journey to a New World" and core.is_probable(c)
    assert core.parse_label("Pocahontas_II_USA_DES").get("short_query") is None   # II isn't a code


def test_sequel_numbered_after_part(imdb):
    """FATHER_OF_THE_BRIDE_2: counting by year makes #2 the 1991 remake (of the 1950 film), and
    it runs about as long; the sequel is titled "Part II"."""
    c = core.imdb_candidates(imdb, [("Father Of The Bride 2", False)], "movie",
                             {"feature_minutes": 106, "mastered": "2000-03"})
    assert (c[0]["name"], c[0]["year"]) == ("Father of the Bride Part II", 1995) and core.is_probable(c)


def test_the_far_better_known_namesake_wins(imdb):
    c = core.imdb_candidates(imdb, [("Robin Hood Prince Of Thieves", False)], "movie",
                             {"feature_minutes": 155, "mastered": "2009-03"})      # the extended cut
    assert (c[0]["name"], c[0]["year"]) == ("Robin Hood: Prince of Thieves", 1991) and core.is_probable(c)


def test_season_from_the_shows_dates():
    """SHERLOCK DISC 1 (mastered 2010-10): only Series 1 had aired, so it's season 1."""
    sherlock = {1: 2010, 2: 2012, 3: 2014, 4: 2017}
    assert core.season_from_dates(sherlock, "2010-10")[0] == 1
    assert core.season_from_dates(sherlock, "2015-03") == (None, None)       # three were out by then
    assert core.season_from_dates({1: 2002}, None)[0] == 1                    # Firefly: only one season


def test_a_year_off_by_one_still_counts(imdb):
    """V for Vendetta is 2006 in TheDiscDB (its release) and 2005 on IMDb (its premiere): a strict
    year match made the same-titled 30-minute making-of from 2006 win over the film itself."""
    c = core.imdb_candidates(imdb, [("Gone in 60 Seconds", False)], "movie",
                             {"mastered": "2008-04", "feature_minutes": 118, "truth_year": 2001})
    assert (c[0]["name"], c[0]["year"]) == ("Gone in 60 Seconds", 2000)


def test_gemini_reads_the_cover_and_imdb_checks_it(imdb_dir, tmp_path, monkeypatch):
    """With a Gemini key: nothing confident, the disc has a picture, so Gemini is asked. Its
    answer only counts once IMDb has the film and the runtime fits the disc."""
    pic = tmp_path / "cover.png"
    pic.write_bytes(b"\x89PNG not really")
    asked = []

    def fake(key, question, image=None, timeout=40):
        asked.append((key, question, image))
        return '```json\n{"title": "Tarzan", "year": 1999, "kind": "film"}\n```', "gemini-test"
    monkeypatch.setattr(core, "gemini_ask", fake)
    args = core.default_args(imdb_dir=imdb_dir, gemini_key="k")
    rep = {"candidates": [], "facts": {"feature_minutes": 88}, "format": "DVD", "label": "DVD_VIDEO",
           "label_parsed": {"placeholder": True}, "mastered": "2000-02"}
    c = core.ask_gemini(rep, [str(pic)], args, "movie")
    assert c["name"] == "Tarzan" and core.is_probable(rep["candidates"])
    assert asked[0][0] == "k" and "JSON" in asked[0][1] and "88 minutes" in asked[0][1]
    assert rep["ai_answers"][0]["model"] == "gemini-test"
    # the same answer for a disc whose feature is far longer: only a guess to check
    rep = dict(rep, candidates=[], facts={"feature_minutes": 140})
    c = core.ask_gemini(rep, [str(pic)], args, "movie")
    assert c["score"] < 60 and not core.is_probable(rep["candidates"])
    # a made-up film IMDb doesn't have: nothing added
    monkeypatch.setattr(core, "gemini_ask", lambda *a, **k: ('{"title": "Zzyzx Moonfall Quest", "year": 2003}', "m"))
    rep = dict(rep, candidates=[])
    assert core.ask_gemini(rep, [str(pic)], args, "movie") is None and rep["candidates"] == []


@pytest.mark.parametrize("text, want", [
    ('{"title": "The Swan Princess", "year": 1994, "kind": "film"}', ("The Swan Princess", 1994)),
    ("**The Swan Princess (1994)**", ("The Swan Princess", 1994)),
    ('{"title": null}', None),
    ("I can't tell from this picture.", None),
])
def test_ai_answers_are_read(text, want):
    a = core.parse_ai_answer(text)
    assert (a and (a["title"], a["year"])) == want or (a is None and want is None)


def test_a_title_read_off_cover_art(imdb):
    """A phrase read off a stylized cover can miss a letter word match needs (The Swan Princess's
    gold script: "the wan princess"): searched loosely, well-known titles with any of its words,
    the nearest whole title and the disc's runtime decide. A label's query isn't loose."""
    q = "the wan princess"
    c = core.imdb_candidates(imdb, [(q, False)], "movie", {"mastered": None, "feature_minutes": 90, "cover_phrases": [q]})
    assert core.is_probable(c), c[:2]
    assert (c[0]["name"], c[0]["year"]) == ("The Swan Princess", 1994)
    c = core.imdb_candidates(imdb, [(q, False)], "movie", {"mastered": None, "feature_minutes": 90})
    assert "The Swan Princess" not in [x["name"] for x in c]


def test_a_double_feature_from_the_scans_own_guesses():
    """Rush Hour and Rush Hour 2 on one disc: two feature-length titles of their own video, each
    as long as a different strong guess, make a collection of both films."""
    t3 = {"id": "Title 3", "role": "feature", "duration": 5862, "segments": ((1, 0, 999),)}
    t5 = {"id": "Title 5", "role": "extra", "duration": 5396, "segments": ((2, 0, 899),)}
    cands = [{"kind": "movie", "name": "Rush Hour", "year": 1998, "imdb_id": "tt0120812", "runtime": 98, "score": 58},
             {"kind": "movie", "name": "Rush Hour 2", "year": 2001, "imdb_id": "tt0266915", "runtime": 90, "score": 55},
             {"kind": "movie", "name": "Rush Hour 3", "year": 2007, "imdb_id": "tt0293564", "runtime": 91, "score": 40}]
    df = core.double_feature([t3, t5], cands)
    assert df["kind"] == "collection" and df["name"] == "Rush Hour & Rush Hour 2"
    assert [(i["title_id"], i["imdb_id"]) for i in df["items"]] == [("Title 3", "tt0120812"), ("Title 5", "tt0266915")]
    assert core.adopt_guess([df] + cands) is df
    # The Iron Giant: two cuts of one film share most of their video, so they stay one film
    theatrical = dict(t3, segments=((1, 0, 499), (1, 500, 999)))
    signature = dict(t5, segments=((1, 0, 499), (3, 0, 49), (1, 500, 999)))
    assert core.double_feature([theatrical, signature], cands) is None
    # a confident single match isn't split up because another film happens to fit a long extra
    sure = [dict(cands[0], score=92)] + cands[1:]
    assert core.double_feature([t3, t5], sure) is None


def test_films_you_name_matched_to_titles_by_length():
    """Multi-film: the films you list, each matched to a title by length (an Abbott and Costello
    disc of three); a film with no length takes the longest title left; more films than titles
    leaves one over."""
    titles = [{"id": "Title 1", "role": "junk", "duration": 30}, {"id": "Title 2", "role": "feature", "duration": 83 * 60},
              {"id": "Title 4", "role": "extra", "duration": 70 * 60}, {"id": "Title 6", "role": "extra", "duration": 79 * 60},
              {"id": "Title 9", "role": "extra", "duration": 12 * 60}]
    films = [{"imdb_id": "tt1", "name": "Buck Privates", "runtime": 84}, {"imdb_id": "tt2", "name": "In the Navy", "runtime": 85},
             {"imdb_id": "tt3", "name": "Hold That Ghost"}]
    pairs, left = core.films_to_titles(titles, films)
    assert [(t["id"], f["name"]) for t, f in pairs] == [("Title 2", "Buck Privates"), ("Title 4", "Hold That Ghost"),
                                                        ("Title 6", "In the Navy")]
    assert not left
    pairs, left = core.films_to_titles(titles[:2], films)
    assert len(pairs) == 1 and [f["name"] for f in left] == ["In the Navy", "Hold That Ghost"]


def test_a_double_features_films_are_named_by_what_was_read():
    """October Collections: The Ghost and Mr. Chicken and The Reluctant Astronaut, read off its
    menu. Ghost Town shares only "Ghost" (60%) but is far better known and exactly as long as
    the second feature: the second film is the one whose name was read (84%), fourth or not."""
    t1 = {"id": "Title 1", "role": "feature", "duration": 5386, "segments": ((1, 0, 999),)}
    t2 = {"id": "Title 2", "role": "feature", "duration": 6120, "segments": ((2, 0, 999),)}
    film = lambda name, year, rt, score, ns: {"kind": "movie", "name": name, "year": year, "imdb_id": "tt%d" % year,
                                             "runtime": rt, "score": score, "name_score": ns}
    cands = [film("The Ghost and Mr. Chicken", 1966, 90, 98.1, 0.95), film("Ghost Town", 2008, 102, 94.8, 0.6),
             film("The Ghost Galleon", 1974, 89, 84.3, 0.77), film("The Reluctant Astronaut", 1967, 101, 86.5, 0.84),
             film("Little Ghost", 1997, 90, 80.6, 0.64)]
    df = core.double_feature([t1, t2], cands)
    assert df["name"] == "The Ghost and Mr. Chicken & The Reluctant Astronaut"


def test_a_films_menu_heading_says_which_title_it_is():
    """Each film's own menu, its heading as read: The Ghost and Mr. Chicken's says "The Ghost and
    Chicken" and plays title 1. Headings that don't clearly name one film each leave it to the
    lengths."""
    items = [{"title_id": "Title 2", "imdb_id": "tt0059221", "name": "The Ghost and Mr. Chicken"},
             {"title_id": "Title 1", "imdb_id": "tt0062190", "name": "The Reluctant Astronaut"}]
    heads = {"Title 1": "The Ghost and Chicken", "Title 2": "RELUCTAT ASTRONAUT The LEMING"}
    assert core.films_by_headings(items, heads) == {"tt0059221": "Title 1", "tt0062190": "Title 2"}
    assert core.films_by_headings(items, {"Title 1": "The Ghost and Chicken", "Title 2": ""}) is None
    assert core.films_by_headings(items, {"Title 1": "Feature One", "Title 2": "Feature Two"}) is None


def test_films_apart_and_headings_with_more_on_them():
    """Abbott and Costello: two features of their own video are two films apart (cuts of one film
    share theirs: one); each film's menu heading names its stars too ("BUD ABBOTT COSTELLO IN BUCK
    PRIVATES"), the film's name inside it still says which title it is."""
    a = {"id": "Title 1", "role": "feature", "duration": 4950, "segments": ((1, 0, 999),)}
    b = {"id": "Title 2", "role": "extra", "duration": 5046, "segments": ((2, 0, 999),)}
    cut = {"id": "Title 3", "role": "version", "duration": 5200, "segments": ((1, 0, 999), (3, 0, 99))}
    assert core.films_apart([a, b]) == 2 and core.films_apart([a, cut]) == 1
    items = [{"title_id": "Title 2", "imdb_id": "tt0032873", "name": "One Night in the Tropics"},
             {"title_id": "Title 1", "imdb_id": "tt0033436", "name": "Buck Privates"}]
    heads = {"Title 1": "LOU BUD ABBOTT COSTELLO OneNight TROiICS", "Title 2": "BUD ABBOTT COSTELLO IN BUCK PRIVATES"}
    assert core.films_by_headings(items, heads) == {"tt0032873": "Title 1", "tt0033436": "Title 2"}


def test_the_last_film_takes_the_last_title():
    """Abbott and Costello: In the Navy's menu heading names it; Hold That Ghost's only its stars.
    The one film left takes the one title left (the two are 85.5 and 85.9 minutes: lengths can't tell)."""
    items = [{"title_id": "Title 1", "imdb_id": "tt0033723", "name": "Hold That Ghost"},
             {"title_id": "Title 2", "imdb_id": "tt0033754", "name": "In the Navy"}]
    heads = {"Title 1": "BUD ABBOTT LOU COSTELLO IN THE NAVY", "Title 2": "BUD ABBOTT LOU COSTELLO"}
    assert core.films_by_headings(items, heads) == {"tt0033754": "Title 1", "tt0033723": "Title 2"}
    three = items + [{"title_id": "Title 3", "imdb_id": "tt3", "name": "Buck Privates"}]
    assert core.films_by_headings(three, dict(heads, **{"Title 3": "BUD ABBOTT LOU COSTELLO"})) is None   # two unread


def test_a_film_named_for_sure_needs_no_top_score():
    """Abbott and Costello: Keep 'Em Flying and Ride 'Em Cowboy. Ride 'Em Cowboy's name was read
    off the menu for sure (92%), but it scores 17 points under the first film (IMDb has it 82
    minutes, the disc 86): it's the second film all the same. A film only sharing a word, as far
    down, isn't."""
    t1 = {"id": "Title 1", "role": "feature", "duration": 5160, "segments": ((1, 0, 999),)}
    t2 = {"id": "Title 2", "role": "feature", "duration": 5136, "segments": ((2, 0, 999),)}
    film = lambda name, year, rt, score, ns: {"kind": "movie", "name": name, "year": year, "imdb_id": "tt%d" % year,
                                             "runtime": rt, "score": score, "name_score": ns}
    cands = [film("Keep 'Em Flying", 1941, 86, 90.9, 0.87), film("Ride 'Em Cowboy", 1942, 82, 74.1, 0.92),
             film("Abbott and Costello Meet Frankenstein", 1948, 83, 71.4, 0.68)]
    assert core.double_feature([t1, t2], cands)["name"] == "Keep 'Em Flying & Ride 'Em Cowboy"
    cands[1]["name_score"] = 0.78                                 # (named less surely: too far down)
    assert core.double_feature([t1, t2], cands) is None


def test_films_of_different_names_pair_first():
    """"WHO DONE IT" read off the menu finds Who Done It? (1942) and Who Done It? (1956), both near
    the top: more likely one name read twice than two films. Pardon My Sarong, read as "PARDON
    WSARONG", is the other film. Two of one name alone don't pair here (an original and its remake
    on one disc pair by their own menus, films_by_title)."""
    t1 = {"id": "Title 1", "role": "feature", "duration": 5034, "segments": ((1, 0, 999),)}
    t2 = {"id": "Title 2", "role": "feature", "duration": 4626, "segments": ((2, 0, 999),)}
    film = lambda name, year, rt, score, ns: {"kind": "movie", "name": name, "year": year, "imdb_id": "tt%d" % year,
                                             "runtime": rt, "score": score, "name_score": ns}
    who42, who56 = film("Who Done It?", 1942, 77, 99.8, 1.0), film("Who Done It?", 1956, 85, 91.5, 1.0)
    sarong = film("Pardon My Sarong", 1943, 84, 94.1, 0.89)
    df = core.double_feature([t1, t2], [who42, sarong, who56])
    assert [(i["title_id"], i["name"]) for i in df["items"]] == [("Title 1", "Pardon My Sarong"), ("Title 2", "Who Done It?")]
    assert core.double_feature([t1, t2], [who42, who56]) is None             # one name read twice
    little = [film("The Little Giant", 1933, 76, 93.6, 0.88), film("Little Giant", 1946, 91, 59.5, 1.0)]
    assert core.double_feature([t1, dict(t2, duration=91 * 60)], little) is None     # ("The": one name too)


def test_each_title_searched_by_its_own_menu(monkeypatch):
    """A disc of two films, each with its own menu (Detect titles): each title searched for by its
    own menu's words and its own length, as if it were a disc of its own. An original and its
    remake of one name pair this way (Gone in 60 Seconds, 1974 and 2000), each by its length.
    A title its own words don't name: no answer this way."""
    titles = [{"id": "Title 1", "role": "feature", "duration": 103 * 60}, {"id": "Title 2", "role": "feature", "duration": 118 * 60}]
    films = [{"kind": "movie", "name": "Gone in 60 Seconds", "year": y, "imdb_id": "tt%d" % y, "runtime": rt,
              "score": 90, "name_score": 1.0} for y, rt in ((2000, 118), (1974, 103))]
    asked = []

    def search(con, queries, kind, facts):
        asked.append((queries, facts["feature_minutes"]))
        return sorted(films, key=lambda c: abs(c["runtime"] - facts["feature_minutes"]))
    monkeypatch.setattr(core, "imdb_candidates", search)
    df = core.films_by_title(None, titles, {"Title 1": ["GONE IN 60 SECONDS"], "Title 2": ["GONE IN 60 SECONDS"]}, {})
    assert [(i["title_id"], i["year"]) for i in df["items"]] == [("Title 1", 1974), ("Title 2", 2000)]
    assert [m for _, m in asked] == [103, 118]                       # (each its own length)
    assert df["why"].startswith("each film named by its own menu")
    monkeypatch.setattr(core, "imdb_candidates", lambda con, q, k, f: [dict(films[0], name_score=0.5)])
    assert core.films_by_title(None, titles, {"Title 1": ["X"], "Title 2": ["Y"]}, {}) is None


def test_words_side_by_side_are_one_line():
    """The Naughty Nineties' menu: its arched title read as three pieces side by side, each a
    little higher or lower, touching: one phrase again, left to right. Buttons stacked under it
    stay their own lines."""
    lines = [("NINETIES", (369, 47, 585, 171), 0.96), ("NAUGHTY", (184, 51, 392, 148), 0.98),
             ("THE", (151, 79, 224, 142), 0.92), ("PLAY", (211, 192, 274, 229), 1.0),
             ("ST.LOUIS", (370, 236, 419, 257), 0.98), ("SCENES", (201, 239, 282, 271), 1.0)]
    got = core.ocr_phrases(lines)
    assert "THE NAUGHTY NINETIES" in got                                   # (and stacked on its buttons, trimmed later)
    assert not any("SCENES ST" in g or "PLAY ST" in g for g in got)       # (a word across the page isn't its line)
    assert core._plain_name("The Little Giant") == core._plain_name("Little Giant")


def test_a_title_its_menu_doesnt_name_takes_a_guess_by_length(monkeypatch):
    """Mexican Hayride's menu names it; the other title's page only in pieces that name nothing
    alone. The named one stands, the other takes the scan's best other guess as long as it (not
    the same film, nor another of its name)."""
    titles = [{"id": "Title 1", "role": "feature", "duration": 77 * 60}, {"id": "Title 2", "role": "feature", "duration": 84 * 60}]
    hayride = {"kind": "movie", "name": "Mexican Hayride", "year": 1948, "imdb_id": "tt1", "runtime": 77, "score": 80,
               "name_score": 0.83}
    monkeypatch.setattr(core, "imdb_candidates", lambda con, q, k, f: [hayride] if f["feature_minutes"] == 77 else [])
    killer = {"kind": "movie", "name": "Meet the Killer, Boris Karloff", "year": 1949, "imdb_id": "tt2", "runtime": 84,
              "score": 83, "name_score": 0.85}
    mars = {"kind": "movie", "name": "Abbott and Costello Go to Mars", "year": 1953, "imdb_id": "tt3", "runtime": 77,
            "score": 90, "name_score": 0.8}
    df = core.films_by_title(None, titles, {"Title 1": ["MEXICAN HLOE ARDE"], "Title 2": ["BUD ABBOTT"]}, {},
                             [mars, killer, hayride])
    assert [(i["title_id"], i["name"]) for i in df["items"]] == [("Title 1", "Mexican Hayride"),
                                                                  ("Title 2", "Meet the Killer, Boris Karloff")]
    assert df["why"].startswith("films named by their own menus, the rest by length")
    assert core.films_by_title(None, titles, {"Title 1": ["MEXICAN"], "Title 2": ["BUD"]}, {}, [mars]) is None


def test_the_stars_names_dont_pair_a_film_with_a_heading():
    """Headings that all name the stars ("BUD ABBOTT LOU COSTELLO …"), and a film whose own name
    has them (Bud Abbott Lou Costello Meet the Killer, Boris Karloff): the stars' names don't pair
    it with In the Navy's heading; each film by its own words."""
    items = [{"title_id": "Title 1", "imdb_id": "tt1", "name": "Bud Abbott Lou Costello Meet the Killer Boris Karloff"},
             {"title_id": "Title 2", "imdb_id": "tt2", "name": "In the Navy"}]
    heads = {"Title 1": "BUD ABBOTT LOU COSTELLO IN THE NAVY", "Title 2": "LOU BUD ABBOTT COSTELLO MEET THE ILLE BORIS KARLOFF"}
    assert core.films_by_headings(items, heads) == {"tt2": "Title 1", "tt1": "Title 2"}


def test_the_films_cast_has_the_discs_stars(monkeypatch):
    """Both films' pages read "BUD ABBOTT COSTELLO …": the disc's stars. Three 1950-ish films of 80
    minutes match title 1's "IN THE FOREIGN LEGION"; the one whose cast has Abbott and Costello is
    the one (Caught in the Foreign Legion only shares the name)."""
    titles = [{"id": "Title 1", "role": "feature", "duration": 80 * 60}, {"id": "Title 2", "role": "feature", "duration": 82 * 60}]
    film = lambda iid, name, rt, score, ns: {"kind": "movie", "name": name, "year": 1950, "imdb_id": iid, "runtime": rt,
                                             "score": score, "name_score": ns}
    legion = [film("tt0161045", "Caught in the Foreign Legion", 80, 88.2, 0.86),
              film("tt0042179", "Abbott and Costello in the Foreign Legion", 80, 84.5, 0.68)]
    invisible = [film("tt0043255", "Bud Abbott and Lou Costello Meet the Invisible Man", 82, 80, 0.8)]
    monkeypatch.setattr(core, "imdb_candidates", lambda con, q, k, f: legion if f["feature_minutes"] == 80 else invisible)
    cast = {"tt0042179": {"bud", "abbott", "lou", "costello", "patricia", "medina"}, "tt0161045": {"someone", "else"},
            "tt0043255": {"bud", "abbott", "lou", "costello"}}
    phrases = {"Title 1": ["BUD ABBOTT COSTELLO FO FORN LEGION", "IN THE FOREIGN LEGION"],
               "Title 2": ["BUD ABBOTT COSTELLO MEET THE INVISIBLE MAN"]}
    df = core.films_by_title(None, titles, phrases, {}, [], cast=lambda i: cast.get(i, set()))
    assert [i["name"] for i in df["items"]] == ["Abbott and Costello in the Foreign Legion",
                                                 "Bud Abbott and Lou Costello Meet the Invisible Man"]


def test_a_film_named_after_its_stars_is_weighed_by_the_rest(monkeypatch):
    """Comin' Round the Mountain's page reads "Bud ABBOTT and COSTELLO COMIN' ROUND THE MOUNTAIN":
    Abbott and Costello Go to Mars, named after its stars, isn't it for sharing their names (its
    stars set aside, "Go to Mars" is nothing like it). Found by the whole reading, Bud Abbott and
    Lou Costello Meet the Invisible Man is weighed by "Meet the Invisible Man"."""
    titles = [{"id": "Title 1", "role": "feature", "duration": 77 * 60}, {"id": "Title 2", "role": "feature", "duration": 82 * 60}]
    film = lambda iid, name, rt, score, ns: {"kind": "movie", "name": name, "year": 1951, "imdb_id": iid, "runtime": rt,
                                             "score": score, "name_score": ns}
    mars = film("tt1", "Abbott and Costello Go to Mars", 77, 95, 0.8)
    comin = film("tt2", "Comin' Round the Mountain", 77, 85, 0.7)
    invisible = film("tt3", "Bud Abbott and Lou Costello Meet the Invisible Man", 82, 93, 0.83)
    stripes = film("tt4", "Invisible Stripes", 81, 83, 0.79)
    monkeypatch.setattr(core, "imdb_candidates", lambda con, q, k, f: [mars, comin] if f["feature_minutes"] == 77
                        else [invisible, stripes])
    leads = {"tt1": {"bud", "abbott", "lou", "costello"}, "tt3": {"bud", "abbott", "lou", "costello"},
             "tt2": {"bud", "abbott", "lou", "costello"}, "tt4": {"george", "raft"}}
    phrases = {"Title 1": ["Bud ABBOTT and COSTELLO COMIN' ROUND THE MOUNTAIN"],
               "Title 2": ["ABBOTT COSTELLO MEET Invisible mAn"]}
    df = core.films_by_title(None, titles, phrases, {}, [], leads=lambda i: leads.get(i, set()))
    assert [i["name"] for i in df["items"]] == ["Comin' Round the Mountain", "Bud Abbott and Lou Costello Meet the Invisible Man"]
