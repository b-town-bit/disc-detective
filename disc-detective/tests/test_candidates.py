"""Ranking IMDb candidates from the label and the disc's facts."""
import json
import os

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


def test_franchise_needs_whole_words(imdb):
    c = core.imdb_candidates(imdb, [("An American Tail 2", False)], "movie", {"mastered": "2003-12", "feature_minutes": 75})
    assert all("Tailor" not in x["name"] for x in c[:1])


def test_tv_and_season_names(imdb):
    c = core.imdb_candidates(imdb, [("Psych", False)], "tv", {"mastered": "2007-01", "episode_minutes": 43})
    assert core.is_probable(c) and c[0]["name"] == "Psych"
    guess = core.imdb_season(imdb, c[0], 1, 2, 3)
    assert guess["guess"][0].startswith("S01E04")


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
