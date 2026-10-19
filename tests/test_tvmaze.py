"""TVmaze episode lists: DVD order (Firefly), short last discs and two-part finales (Smallville)."""
import json
import os
import urllib.error

import pytest

from disc_detective import core
import fixtures
from test_identify import run

DATA = os.path.join(os.path.dirname(__file__), "data")
M = 60


@pytest.fixture
def tvmaze(monkeypatch):
    """Serve the saved TVmaze answers; count the calls that reach the 'network'."""
    answers = json.load(open(os.path.join(DATA, "tvmaze.json")))
    calls = []

    def fetch(path):
        calls.append(path)
        if path not in answers:
            raise urllib.error.HTTPError(core.TVMAZE_API + path, 404, "Not Found", {}, None)
        return answers[path]
    monkeypatch.setattr(core, "_tvmaze_fetch", fetch)
    return calls


def firefly(tmp_path):
    return core.tvmaze_orders("tt0303461", str(tmp_path / "tvm"))


def smallville(tmp_path):
    return core.tvmaze_orders("tt0279600", str(tmp_path / "tvm"))


def test_firefly_disc_one_follows_the_dvd_order(tvmaze, tmp_path):
    """The DVDs start with the two-hour Serenity, which aired last (S01E11)."""
    o = firefly(tmp_path)
    assert o["dvd"] and o["show"] == 180
    disc = [86 * M, 43 * M, 43 * M]
    g = core.tvmaze_season_guess(o, 1, 1, disc, "aired", "DVD")
    assert g["guess"] == ["S01E11 Serenity", "S01E01 The Train Job", "S01E02 Bushwhacked"]
    assert "DVD order" in g["note"] and "aired order" in g["note"]
    g = core.tvmaze_season_guess(o, 1, 1, disc, "dvd", "DVD")
    assert g["guess"] == ["S01E01 Serenity", "S01E02 The Train Job", "S01E03 Bushwhacked"]


def test_a_two_hour_episode_counts_twice_when_counting_discs(tvmaze, tmp_path):
    """Disc 1 holds Serenity + 2 (four hours' worth), so disc 2 starts at Shindig, not Safe."""
    g = core.tvmaze_season_guess(firefly(tmp_path), 1, 2, [43 * M] * 4, "aired", "DVD")
    assert g["guess"][0] == "S01E06 Shindig"
    assert [x.split()[0] for x in g["guess"]] == ["S01E06", "S01E07", "S01E03", "S01E04"]


def test_short_last_disc_holds_the_seasons_last_episodes(tvmaze, tmp_path):
    o = smallville(tmp_path)
    assert o["dvd"] is None                                   # no DVD list: aired order
    g = core.tvmaze_season_guess(o, 10, 6, [42 * M, 43 * M], "aired", "DVD")
    assert g["guess"] == ["S10E21 Finale (1)", "S10E22 Finale (2)"]
    g = core.tvmaze_season_guess(o, 10, 5, [42 * M] * 4, "aired", "DVD")
    assert [x.split()[0] for x in g["guess"]] == ["S10E17", "S10E18", "S10E19", "S10E20"]


def test_a_long_title_can_hold_both_parts_of_a_finale(tvmaze, tmp_path):
    g = core.tvmaze_season_guess(smallville(tmp_path), 10, 6, [42 * M, 42 * M, 84 * M], "aired", "DVD")
    assert g["guess"][-1] == "S10E21-E22 Finale (1) / Finale (2)"
    assert "two episodes" in g["note"]


def test_unaired_pilot_special_is_not_episode_one(tvmaze, tmp_path):
    g = core.tvmaze_season_guess(smallville(tmp_path), 1, 1, [43 * M] * 4, "aired", "DVD")
    assert g["guess"] == ["S01E01 Pilot", "S01E02 Metamorphosis", "S01E03 Hothead", "S01E04 X-Ray"]


def test_answers_are_cached_and_used_offline(tvmaze, tmp_path):
    firefly(tmp_path)
    n = len(tvmaze)
    assert n == 4                                    # lookup, episodes, lists, DVD list
    firefly(tmp_path)
    assert len(tvmaze) == n                          # fresh cache: no new calls
    o = core.tvmaze_orders("tt0303461", str(tmp_path / "tvm"), online=False)
    assert o["dvd"] and len(tvmaze) == n
    assert core.tvmaze_orders("tt9999999", str(tmp_path / "tvm")) is None      # not on TVmaze


def test_scan_names_episodes_from_tvmaze(tvmaze, tmp_path, imdb_dir, monkeypatch):
    """A TV disc identified through IMDb gets its episode names from TVmaze when online."""
    answers = {"/lookup/shows?imdb=": {"id": 9, "name": "Justified"},
               "/shows/9/episodes?specials=1": [{"season": 1, "number": i, "name": "Ep %d" % i, "runtime": 60}
                                     for i in range(1, 14)],
               "/shows/9/alternatelists": []}
    monkeypatch.setattr(core, "_tvmaze_fetch",
                        lambda p: answers["/lookup/shows?imdb="] if p.startswith("/lookup") else answers[p])
    disc = fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED"))        # its metadata: Season 1: Disc 1
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, no_discdb_online=True, no_art=True,
                 tvmaze_dir=str(tmp_path / "tvm"))
    assert rep["episode_guess"]["source"] == "TVmaze aired order"
    assert rep["episode_guess"]["guess"][0] == "S01E01 Ep 1"
    assert any("s01e01 - Ep 1" in n for n in rep["plex_names"])
    rep, _ = run(disc, tmp_path, imdb_dir=imdb_dir, no_discdb_online=True, no_art=True, no_tvmaze=True)
    assert rep.get("episode_guess", {}).get("source") != "TVmaze aired order"


def test_smallville_s10_disc6_as_it_really_is(tvmaze, tmp_path):
    """The real disc: Prophecy (42:19) and both parts of the Finale as one 84-minute title."""
    g = core.tvmaze_season_guess(smallville(tmp_path), 10, 6, [42 * M + 19, 84 * M], "aired", "DVD")
    assert g["guess"] == ["S10E20 Prophecy", "S10E21-E22 Finale (1) / Finale (2)"]


def test_disc_playlist_names_beat_the_dvd_order(tvmaze, tmp_path):
    """FIREFLYUS_D3 plays War Stories, Heart of Gold, Objects in Space, Trash, The Message, not
    in TVmaze's DVD order; its own playlist names (EP10 HeartOfGold, EP13_Mesg) say which is which."""
    hints = ["EP9 WarStories", "EP10 HeartOfGold", "EP11_ObjectInSpace", "EP12_Trash", "EP13_Mesg"]
    g = core.tvmaze_season_guess(firefly(tmp_path), 1, 3, [44 * M] * 5, "aired", "Blu-ray", hints=hints)
    assert g["guess"] == ["S01E09 War Stories", "S01E14 Heart of Gold", "S01E10 Objects in Space",
                          "S01E12 Trash", "S01E13 The Message"]
    assert "playlist names" in g["note"]
    # numbered names (FEATURE_EP_01) carry no title: counting decides, as before
    g = core.tvmaze_season_guess(firefly(tmp_path), 1, 1, [86 * M, 43 * M, 43 * M, 43 * M], "aired", "Blu-ray",
                                 hints=["FEATURE_EP_Pilot", "FEATURE_EP_01", "FEATURE_EP_02", "FEATURE_EP_03"])
    assert g["guess"] == ["S01E11 Serenity", "S01E01 The Train Job", "S01E02 Bushwhacked", "S01E06 Shindig"]


def test_name_matching_drops_ambiguous_names():
    seq = [{"name": "Trash"}, {"name": "Trash Talk"}, {"name": "The Message"}]
    m = core.match_episode_names(["EP12_Trash", "EP13_Mesg", "Menu"], seq)
    assert list(m) == [1] and m[1]["name"] == "The Message"          # TRASH fits two titles


def test_specials_on_a_disc_are_extras_not_episodes():
    """SHERLOCK DISC 2: The Great Game (89 min), the unaired pilot (55) and Unlocking Sherlock
    (32). The specials are listed with those runtimes; the episodes run 90."""
    titles = [{"id": "00000.mpls", "role": "episode", "duration": 5364, "note": ""},
              {"id": "00002.mpls", "role": "episode", "duration": 3318, "note": ""},
              {"id": "00001.mpls", "role": "extra", "duration": 1958, "note": ""}]
    specials = [{"name": "Unaired Pilot", "runtime": 55}, {"name": "Unlocking Sherlock", "runtime": 32}]
    changed = core.set_aside_specials(titles, specials, [90, 90, 90])
    assert [t["id"] for t in changed] == ["00002.mpls"] and titles[1]["special_name"] == "Unaired Pilot"
    assert [t["role"] for t in titles] == ["episode", "extra", "extra"] and titles[0]["episode_index"] == 1
    # a special listed at the episodes' own (time-slot) length tells nothing
    ts = [{"id": "a", "role": "episode", "duration": 43 * 60, "note": ""}]
    assert core.set_aside_specials(ts, [{"name": "Unaired Pilot", "runtime": 60}], [60] * 21) == []


def test_earlier_discs_in_the_library_say_where_this_one_starts(tmp_path):
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    con.execute("INSERT INTO discs (hash, label, truth) VALUES ('AAA', 'SHERLOCK DISC 1', 'Sherlock (2010) S1 D1')")
    con.executemany("INSERT INTO titles (hash, title_id, role, duration) VALUES ('AAA', ?, 'episode', ?)",
                    [("00001.mpls", 5290), ("00002.mpls", 5309)])
    assert core.episodes_before(con, "Sherlock", 1, 2, "BBB") == 2
    assert core.episodes_before(con, "Sherlock", 1, 3, "BBB") is None        # disc 2 not scanned yet
    assert core.episodes_before(con, "Psych", 1, 2, "BBB") is None
    g = core.tvmaze_season_guess({"aired": [{"season": 1, "number": i, "name": n, "runtime": 90} for i, n in
                                            enumerate(["A Study in Pink", "The Blind Banker", "The Great Game"], 1)],
                                  "dvd": None}, 1, 2, [5364], before=2)
    assert g["guess"] == ["S01E03 The Great Game"] and "your library" in g["note"]
    con.close()
