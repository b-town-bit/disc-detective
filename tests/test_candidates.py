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
