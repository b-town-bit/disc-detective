"""TV discs' menus: which titles their buttons name, read against the show's episode list. The
show here is made up; the shapes are those of real discs' crawls."""
from disc_detective import core, menus


def eps(names, season=1, first=1, runtime=45):
    return [{"season": season, "number": first + i, "name": n, "runtime": runtime} for i, n in enumerate(names)]


SEASON_1 = ["Opening Night", "The Lighthouse Keeper", "Lost and Found", "A Stitch in Time", "Harbor Lights",
            "The Long Way Round, or How I Learned to Stop Worrying", "Keeper of the Flame", "Night Shift"]
SEASON_2 = ["Opening Night Again", "Second Wind", "The Lighthouse Keeper's Daughter", "Low Tide"]
ORDERS = {"aired": eps(SEASON_1) + eps(SEASON_2, season=2) + [{"season": 0, "number": None, "name": "Special"}],
          "dvd": None}
EPISODE = 2700


def reading(text, seconds=EPISODE, **kw):
    return dict({"text": text, "seconds": seconds}, **kw)


def numbers(got):
    return {i: p["number"] for i, p in got["picks"].items()}


def test_fuzzy_names_found_in_their_season():
    got = core.menu_episodes([reading("1. Opening Night"), reading("2 The Llghthouse Keepr"), reading("3Lost and Found")],
                             ORDERS)
    assert got["season"] == 1 and numbers(got) == {0: 1, 1: 2, 2: 3}


def test_a_wrapped_name_stray_words_and_a_misprint():
    got = core.menu_episodes([reading("The Long Way Round,"), reading("MISSING Keeper of the Flame"),
                              reading("Night the Shift")], ORDERS)
    assert numbers(got) == {0: 6, 1: 7, 2: 8}


def test_similar_names_settled_by_their_neighbours():
    # "The Lighthouse Keeper" is much like season 2's "The Lighthouse Keeper's Daughter", and the
    # reading's closer to neither alone; the run beside it says which
    got = core.menu_episodes([reading("Opening Night Again"), reading("Second Wind"),
                              reading("The Lighthouse Keeper")], ORDERS)
    assert got["season"] == 2 and numbers(got) == {0: 1, 1: 2, 2: 3}


def test_a_reading_too_damaged_takes_the_place_between_its_neighbours():
    got = core.menu_episodes([reading("Opening Night"), reading("xq zz ptv"), reading("Lost and Found")], ORDERS)
    assert numbers(got) == {0: 1, 1: 2, 2: 3} and got["picks"][1]["by"] == "neighbours"
    assert got["picks"][0]["by"] == "name"


def test_the_disc_out_of_the_lists_order():
    got = core.menu_episodes([reading("Harbor Lights"), reading("Night Shift"), reading("Lost and Found")], ORDERS)
    assert numbers(got) == {0: 5, 1: 8, 2: 3}


def test_not_episodes():
    # bonus materials, a commentary's switch, and what's far too short, whatever their words
    got = core.menu_episodes([reading("Opening Night"), reading("Lost and Found", bonus=True),
                              reading("Harbor Lights", seconds=52), reading("Night Shift", switch=True)], ORDERS)
    assert numbers(got) == {0: 1}
    assert numbers(core.menu_episodes([reading("Opening Night"), reading("Night Shift", only_kind=True)], ORDERS)) == {0: 1}
    # words nothing like a name: nothing at all
    assert core.menu_episodes([reading("Gag Reel"), reading("Cast Interviews")], ORDERS) is None
    assert core.menu_episodes([reading("Opening Night")], {"aired": []}) is None


def title(n, seconds=EPISODE, cells=None):
    cells = [(1, n * 1000 + k, n * 1000 + k + 900) for k in range(0, 5000, 1000)] if cells is None else cells
    return {"id": "Title %d" % n, "duration": seconds, "segments": tuple(cells)}


def test_a_play_all_by_cells_and_by_the_programs_run():
    a, b, c, d = title(1), title(2), title(3), title(4)
    whole = title(9, 3 * EPISODE, a["segments"] + b["segments"] + c["segments"])
    twin = dict(title(14), segments=d["segments"])            # the video of title 4
    ad = title(20, 60)
    got = core.play_all_order([a, b, c, d, whole, twin, ad, title(21, 3 * EPISODE)], [[9, 14, 21], [20, 9]])
    assert got["order"] == [1, 2, 3, 14, 21] and got["twins"]["14"] == [4]
    assert core.play_all_order([a, b, ad], [[20]]) is None     # (a page of ads says nothing)
    assert core.play_all_order([a, b, ad], [[1, 2]])["order"] == [1, 2]
    assert core.play_all_order([b, a, whole], [])["order"] == [1, 2]    # (its cells' order, not the titles')


PLAY = {"order": [1, 2, 3, 14, 5, 6], "seconds": [EPISODE] * 6, "twins": {"14": [4]}}


def in_order(*texts):
    """Readings for titles 1, 2, 3, 4 (played as its twin 14), 5, 6: None for a button not read."""
    return [dict(reading(t), title=n, bonus=False) for n, t in zip([1, 2, 3, 4, 5, 6], texts) if t is not None]


def test_a_play_all_places_what_wasnt_read():
    # the user's case: four read cleanly, two not; the first and last too, past any neighbour
    got = core.menu_episodes(core.menu_play_order(in_order("xq zz", "The Lighthouse Keeper", "Lost and Found",
                                                           "pwq vv", "Harbor Lights", "kk jj"), PLAY), ORDERS)
    assert numbers(got) == {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6}
    assert [got["picks"][i]["by"] for i in (0, 1, 3, 5)] == ["play-all", "name", "play-all", "play-all"]


def test_a_title_with_no_reading_at_all_found_by_its_place():
    rs = core.menu_play_order(in_order("Opening Night", None, "Lost and Found", None, "Harbor Lights", None), PLAY)
    got = core.menu_episodes(rs, ORDERS)
    placed = {rs[i]["title"]: p["number"] for i, p in got["picks"].items()}
    assert placed == {1: 1, 2: 2, 3: 3, 14: 4, 5: 5, 6: 6}


def test_a_play_all_goes_by_its_order_not_the_titles():
    # (the button plays 4, the Play All its twin 14, between 3 and 5: Psych S1 D3's episode 10)
    rs = core.menu_play_order(in_order("Opening Night", "The Lighthouse Keeper", "Lost and Found",
                                       "A Stitch in Time", "Harbor Lights", "The Long Way Round"), PLAY)
    assert [r["title"] for r in rs] == [1, 2, 3, 4, 5, 6]
    assert [r.get("in_play") for r in core.menu_play_order([dict(reading("x"), title=30)], PLAY)][-1] is None


def test_a_name_it_is_sure_of_stops_the_play_all_guessing():
    # the Play All's order isn't the list's: names read stand, and the unread aren't placed by it
    got = core.menu_episodes(core.menu_play_order(in_order("xq zz", "Night Shift", "Keeper of the Flame", "Lost and Found",
                                                           "kk jj", None), PLAY), ORDERS)
    assert sorted(p["number"] for p in got["picks"].values()) == [3, 7, 8]
    assert {p["by"] for p in got["picks"].values()} == {"name"}


SHARED = ["The One with the Lantern", "The One with the Ladder", "The One with the Letter", "The One with the Lodger",
          "The One Where They Sail", "The One Where They Sing", "The One Where Nobody Sails", "Finale"]
SHARED_ORDERS = {"aired": eps(SHARED), "dvd": None}
VERSUS = {"aired": eps(["Hal Versus the Ring", "Hal Versus the King", "Hal Versus the Wing", "Hal Versus the Storm"]),
          "dvd": None}


def test_what_a_shows_names_share_says_nothing():
    assert {("the", "one", "with", "the"), ("the", "one", "where")} <= core.name_commons(SHARED)
    # a lone "the" isn't one; nor a whole name; nor what only a few share
    assert ("the",) not in core.name_commons(["The Fog", "The Mist", "The Rain", "The Hail"])
    assert not core.name_commons(["Alpha One", "Alpha Two", "Beta", "Gamma", "Delta", "Omega", "Sigma", "Zeta", "Eta", "Rho",
                                  "Tau", "Psi", "Pi", "Mu", "Nu", "Xi", "Chi", "Phi", "Iota", "Kappa", "Theta"])
    # a name starting with a number keeps it, read either way
    assert core._menu_score("100 Clues", "100 Clues") == 1 and core._menu_score("7. 100 Clues", "100 Clues") == 1
    # the shared opening alone, a wrapped name's first line, names nobody
    assert core.menu_episodes([reading("The One with the")], SHARED_ORDERS) is None
    assert core._menu_score("The One with the", "The One with the Lantern", core.name_commons(SHARED)) == 0
    # words nothing like any name's own part: without the opening, nothing much alike
    assert core.menu_episodes([reading("The One with the Pizza Delivery Man")], SHARED_ORDERS) is None


def test_names_told_apart_by_their_own_part():
    got = core.menu_episodes([reading("The One with the Lettre"), reading("The One with the Lodgr")], SHARED_ORDERS)
    assert numbers(got) == {0: 3, 1: 4}
    # the menu leaving the opening off, or with stray words before it
    assert numbers(core.menu_episodes([reading("Lantern"), reading("x y The One with the Ladder")], SHARED_ORDERS)) == {0: 1, 1: 2}
    # "vs." for "Versus"; one letter's all that differs, and it's the one that counts
    assert numbers(core.menu_episodes([reading("Hal vs. the King")], VERSUS)) == {0: 2}
    assert numbers(core.menu_episodes([reading("Hal Versus the Storm")], VERSUS)) == {0: 4}
    # too little of its own to be sure of, one letter off: not guessed
    assert core._menu_score("Hal Versus the Bing", "Hal Versus the Ring", core.name_commons(["Hal Versus the Ring"] * 4)) == 0


class FakeImdb:
    """An index of a few shows, as shows_by_episodes reads it."""

    def __init__(self):
        import sqlite3
        self.con = sqlite3.connect(":memory:")
        self.con.executescript("""
            CREATE TABLE titles (id INT, type TEXT, title TEXT, year INT, runtime INT);
            CREATE TABLE ratings (id INT, votes INT);
            CREATE TABLE episodes (parent INT, season INT, episode INT, id INT, title TEXT, runtime INT, year INT);
        """)

    def show(self, n, name, votes, seasons, kind="tvSeries"):
        self.con.execute("INSERT INTO titles VALUES (?,?,?,?,?)", (n, kind, name, 2000 + n, 45))
        self.con.execute("INSERT INTO ratings VALUES (?,?)", (n, votes))
        for s, names in seasons.items():
            for e, t in enumerate(names, 1):
                self.con.execute("INSERT INTO episodes VALUES (?,?,?,?,?,?,?)", (n, s, e, n * 1000 + e, t, 45, 2000))
        return self


def test_a_show_found_by_its_episodes():
    db = FakeImdb().show(7, "Harbor Watch", 5000, {1: SEASON_1, 2: SEASON_2}).show(
        8, "Another Show", 9000, {1: ["Harbor Lights", "Marooned", "The Quarry"]})
    rs = [reading("Second Wind"), reading("The Lighthouse Keeper's Daughter"), reading("Low Tide"), reading("Opening Night Again")]
    # "Low Tide": too few letters to search by, found once the show is being read through
    db.show(9, "Spare", 10, {1: ["Tide Pools"]})
    got = core.shows_by_episodes(db.con, [reading("Opening Night Again"), reading("Second Wind"),
                                          reading("The Lighthouse Keeper's Daughter")])
    assert got["name"] == "Harbor Watch" and got["season"] == 2 and got["named"] == 3 and got["imdb_id"].endswith("7")
    assert len(rs) == 4


def test_too_little_or_too_even_to_name_a_show():
    db = FakeImdb().show(7, "Harbor Watch", 500, {1: SEASON_1}).show(8, "Harbor Watch Again", 600, {1: SEASON_1})
    two = [reading("Opening Night"), reading("Lost and Found")]
    three = two + [reading("Harbor Lights")]
    assert core.shows_by_episodes(db.con, two) is None         # (too few names)
    assert core.shows_by_episodes(db.con, three) is None       # (a remake and its original)
    db.show(9, "Harbor Watch Classic", 5000, {1: SEASON_1})
    assert core.shows_by_episodes(db.con, three)["name"] == "Harbor Watch Classic"       # (far the better known)
    # bonus materials and the like are no evidence
    bonus = [dict(r, bonus=True) for r in three]
    assert core.shows_by_episodes(db.con, bonus) is None


def test_a_copy_is_listed_only_with_its_original():
    logo = ((1, 100, 5000),)
    ts = [{"id": "Title 12", "duration": 24, "group": "VTS 1", "segments": logo},
          {"id": "Title 13", "duration": 24, "group": "VTS 1", "segments": logo},
          {"id": "Title 1", "duration": 1500, "group": "VTS 1", "segments": ((1, 9000, 400000),)},
          {"id": "Title 2", "duration": 1500, "group": "VTS 1", "segments": ((1, 9000, 400000),)}]
    core.classify(ts, "DVD")
    assert ts[1]["role"] == "duplicate" and ts[3]["role"] == "duplicate"
    listed = core.without_lost_copies([t for t in ts if t["role"] != "junk"])
    assert [t["id"] for t in listed] == ["Title 1", "Title 2"]      # (the logo's copy gone, the film's kept)
    # the menus naming the original lists it, and so its copy (Psych S6 D3's deleted scene)
    ts[0]["role"] = "extra"
    listed = core.without_lost_copies([t for t in ts if t["role"] != "junk"])
    assert {t["id"] for t in listed} == {"Title 12", "Title 13", "Title 1", "Title 2"}


def test_short_titles_of_only_stubs_are_not_copies():
    stub = lambda n: {"id": "Title %d" % n, "duration": 20, "group": "VTS 1", "segments": ((1, 0, 3),)}
    ts = [stub(1), stub(2), {"id": "Title 3", "duration": 1500, "group": "VTS 1", "segments": ((1, 5000, 400000),)}]
    core.classify(ts, "DVD")
    assert ts[0]["role"] == "junk" and ts[1]["role"] == "junk"


def test_one_weak_match_says_nothing():
    assert core._menu_score("Harvard Nights", "Harbor Lights") < core.MENU_NAME_SURE
    assert core.menu_episodes([reading("Harvard Nights")], ORDERS) is None
    # beside a sure one, the same words may be it
    assert numbers(core.menu_episodes([reading("Harvard Nights"), reading("A Stitch in Time")][::-1], ORDERS))[0] == 4


def page(p, buttons, heading=None, route=(), image=True, program=1):
    return {"domain": 1, "page": p, "program": program, "heading": heading, "image": "p%d_%d.png" % (p, program) if image else None,
            "route": [{"page": a, "program": b, "button": n} for a, b, n in route],
            "buttons": [dict(b, n=i, rect=[0, 0, 9, 9]) for i, b in enumerate(buttons, 1)]}


def plays(text, title, seconds=EPISODE, chapter=1, **kw):
    return {"text": text, "plays": dict({"title": title, "chapter": chapter, "seconds": seconds}, **kw)}


def goes(text, p, program=1):
    return {"text": text, "goes_to": {"page": p, "program": program}}


def test_an_episode_page_beside_a_bonus_page():
    # (Psych S1 D1's shape: a pilot, and another pilot as long as it among the bonus materials)
    out = {"opening": [8], "pages": [
        page(1, [plays("play opening night", 1), goes("bonus materials", 13), goes("sneak peeks", 16)]),
        page(13, [plays("the other opening", 20, 4600), plays("gag reel", 7, 400), plays("RESUME EPISODE", 1),
                  plays("on", 2), goes("MENU", 1)], heading="bonus materials", route=[(1, 1, 2)]),
        page(16, [plays("Another Show", 9, 60), plays("PLAY ALL", 18, 500)], heading=None, route=[(1, 1, 3)])]}
    tv = menus.tv_titles(out)
    got = core.menu_episodes(tv["readings"], ORDERS)
    found = menus.tv_found(tv, got["picks"])
    assert [(e["title"], e["season"], e["episode"]) for e in found["episodes"]] == [(1, 1, 1)]
    assert {e["title"]: e["name"] for e in found["extras"]} == {20: "The Other Opening", 7: "Gag Reel"}
    assert found["promos"] == [9, 18] and found["opening"] == [8] and found["play_alls"] == []
    titles = [{"id": "Title %d" % n, "role": r} for n, r in
              [(1, "episode"), (2, "duplicate"), (20, "episode"), (7, "extra"), (9, "junk"), (18, "extra"),
               (8, "extra"), (30, "episode")]]
    labels = menus.detected_tv_labels(found, titles)
    assert labels["Title 1"]["type"] == "Episode" and labels["Title 1"]["episode"] == 1
    assert labels["Title 1"]["title"] == "Opening Night" and labels["Title 1"]["season"] == 1
    assert labels["Title 20"]["type"] == "Extra" and labels["Title 18"]["type"] == "Promo"
    assert labels["Title 8"]["title"] == "Previews"
    # a copy stays one; a title the menus say nothing of keeps the scan's word, not an ad
    assert "Title 2" not in labels and "Title 30" not in labels
    assert "Title 1" not in menus.detected_tv_labels(found, titles, keep={"Title 1"})


def test_an_episode_button_leads_to_its_scene_page():
    # (Smallville's shape: the Episodes page goes to a scene page each; the commentaries play the
    # episodes themselves, so they're no extras)
    scene = lambda t, program: page(2, [plays("", t, chapter=c) for c in (1, 2, 3)], image=False, program=program,
                                    route=[(1, 1, 2)])
    out = {"pages": [
        page(1, [plays("PLAY", 1, 8000), goes("EPISODES", 2), goes("FEATURES", 6)]),
        page(2, [goes("1 Opening Night", 2, 2), goes("2 The Lighthouse Keeper", 2, 3), goes("MAIN MENU", 1)],
             route=[(1, 1, 2)]),
        scene(3, 2), scene(4, 3),
        page(6, [plays("OPENING NIGHT Commentary by the Producers", 3), goes("MAIN MENU", 1)],
             heading="SPECIAL FEATURES", route=[(1, 1, 3)])]}
    tv = menus.tv_titles(out)
    found = menus.tv_found(tv, core.menu_episodes(tv["readings"], ORDERS)["picks"])
    assert [(e["title"], e["episode"]) for e in found["episodes"]] == [(3, 1), (4, 2)]
    assert found["extras"] == [] and found["play_alls"] == [1]
    # its picture: the button naming it, on the page read, over a commentary's
    pics = menus.tv_pictures(tv, "out", {3: {"image": "old", "text": "x"}, 1: {"image": "kept", "text": "PLAY"}})
    assert pics[3]["text"] == "1 Opening Night" and pics[4]["text"] == "2 The Lighthouse Keeper"
    assert pics[3]["image"].endswith("p2_1.png") and pics[1]["image"] == "kept"


def test_a_deleted_scene_button_and_its_named_twin():
    # (Psych S1 D3's: a small button under the episode's name, and the same scene on the bonus page)
    tv = {"readings": [dict(title=1, text="Opening Night", seconds=EPISODE, bonus=False, kind=None),
                       dict(title=8, text="deleted scene", seconds=52, bonus=False, kind=None),
                       dict(title=6, text="opening night", seconds=52.4, bonus=True, kind=None)],
          "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    found = menus.tv_found(tv, core.menu_episodes(tv["readings"], ORDERS)["picks"])
    assert found["extras"] == [{"title": 6, "name": "Opening Night Deleted Scene", "seconds": 52.4,
                                "kind": "DeletedScene", "season": 1, "episode": 1}]


def test_a_video_commentary_is_an_extra_of_the_episode_above_it():
    # (Psych S6 D3's episode page: its button's words read with the line above them run in)
    picks = {0: {"season": 6, "number": 11, "name": "Heeeeere's Lassie", "score": 1, "by": "name"},
             1: {"season": 6, "number": 12, "name": "Shawn and the Real Girl", "score": 1, "by": "name"}}
    read = lambda t, text, rect, seconds=2600: dict(title=t, text=text, seconds=seconds, bonus=False, kind=None, page=[6, 28, 1],
                                                    rect=rect, switch=bool(menus.TV_SWITCH_RE.search(text)) and
                                                    not menus.VIDEO_COMMENTARY_RE.search(text))
    tv = {"readings": [read(15, "11 Heeeeere's Lassie", [164, 93, 361, 266]), read(16, "12. Shawn and the", [369, 93, 579, 270]),
                       read(10, "Extended Version Video Commentary", [118, 269, 361, 318], 217),
                       read(12, "Video Commentary", [365, 320, 583, 351], 231),
                       read(18, "Audio Commentary On", [174, 319, 255, 369], 2600)],
          "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    found = menus.tv_found(tv, picks)
    by = {e["title"]: e for e in found["extras"]}
    assert (by[10]["name"], by[10]["episode"]) == ("Heeeeere's Lassie Video Commentary", 11)
    assert (by[12]["name"], by[12]["episode"], by[12]["season"]) == ("Shawn and the Real Girl Video Commentary", 12, 6)
    assert 18 not in by                                         # (an audio commentary: the episode, switched)
    # the title the scan kept gets the label; the button played its copy
    titles = [{"id": "Title 9", "role": "extra", "duration": 217},
              {"id": "Title 10", "role": "duplicate", "duration": 217, "note": "same video as Title 9"}]
    labels = menus.detected_tv_labels(found, titles)
    assert labels["Title 9"]["title"] == "Heeeeere's Lassie Video Commentary" and labels["Title 9"]["type"] == "Extra"
    assert labels["Title 9"]["episode"] == 11 and "Title 10" not in labels


def test_a_button_saying_only_deleted_scene_is_the_episodes_above_it():
    # (Psych S1 D3's: no need of the bonus page that names them)
    picks = {0: {"season": 1, "number": 1, "name": "Opening Night", "score": 1, "by": "name"},
             1: {"season": 1, "number": 2, "name": "The Lighthouse Keeper", "score": 1, "by": "name"}}
    row = lambda title, text, seconds, rect: dict(title=title, text=text, seconds=seconds, bonus=False, kind=None,
                                                  rect=rect, page=[1, 2, 1], only_kind=text == "Deleted Scene")
    tv = {"readings": [row(1, "Opening Night", EPISODE, [100, 50, 300, 90]), row(2, "The Lighthouse Keeper", EPISODE, [400, 50, 600, 90]),
                       row(8, "Deleted Scene", 90, [100, 120, 300, 150]), row(9, "Deleted Scene", 60, [400, 120, 600, 150])],
          "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    by = {e["title"]: e for e in menus.tv_found(tv, picks)["extras"]}
    assert (by[8]["name"], by[8]["kind"], by[8]["episode"]) == ("Opening Night Deleted Scene", "DeletedScene", 1)
    assert (by[9]["name"], by[9]["season"], by[9]["episode"]) == ("The Lighthouse Keeper Deleted Scene", 1, 2)
    assert "under" not in by[8]


def test_a_page_of_video_commentaries():
    # (the Bonus Features page's way to them: each button says only whose it is)
    out = {"pages": [
        page(1, [plays("Opening Night", 1), plays("The Lighthouse Keeper", 2), goes("Bonus", 12)]),
        page(12, [goes("Video Commentaries", 20), goes("Audio Commentaries", 21)], heading="bonus features", route=[(1, 1, 3)]),
        page(20, [plays("The Lighthouse Keeper", 5, 257), plays("Opening Night", 7, 192)], route=[(12, 1, 1)])]}
    tv = menus.tv_titles(out)
    found = menus.tv_found(tv, core.menu_episodes(tv["readings"], ORDERS)["picks"])
    assert sorted((e["title"], e["episode"]) for e in found["episodes"]) == [(1, 1), (2, 2)]
    by = {e["title"]: e for e in found["extras"]}
    assert (by[5]["name"], by[5]["episode"]) == ("The Lighthouse Keeper Video Commentary", 2)
    assert (by[7]["name"], by[7]["episode"]) == ("Opening Night Video Commentary", 1)
    # a button naming its episode, and who's talking; the fuller name wins over its copy's
    picks = {0: {"season": 6, "number": 12, "name": "Shawn and the Real Girl", "score": 1, "by": "name"}}
    tv = {"readings": [dict(title=16, text="12. Shawn and the Real Girl", seconds=EPISODE, bonus=False, kind=None,
                            rect=[369, 93, 579, 270], page=[6, 28, 1]),
                       dict(title=12, text="Video Commentary", seconds=231, bonus=False, kind=None,
                            rect=[365, 320, 583, 351], page=[6, 28, 1]),
                       dict(title=11, text="Shawn and the Real Girl Video Commentary with Writer Andy Berman", seconds=231,
                            bonus=False, kind=None, rect=[106, 280, 431, 351], page=[6, 23, 1], commentaries=True)],
          "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    found = menus.tv_found(tv, picks)
    by = {e["title"]: e for e in found["extras"]}
    assert (by[11]["name"], by[11]["episode"]) == ("Shawn and the Real Girl Video Commentary with Writer Andy Berman", 12)
    assert (by[12]["name"], by[12]["episode"]) == ("Shawn and the Real Girl Video Commentary", 12)
    labels = menus.detected_tv_labels(found, [
        {"id": "Title 11", "role": "extra", "duration": 231},
        {"id": "Title 12", "role": "duplicate", "duration": 231, "note": "same video as Title 11"}])
    assert labels["Title 11"]["title"].endswith("with Writer Andy Berman") and labels["Title 11"]["season"] == 6
    assert "Title 12" not in labels


def test_extras_keep_their_place_in_the_menus():
    # a page's extras come where its button is on the page before; a title on a page of them keeps
    # that place over its copy's button on an episode page
    out = {"pages": [
        page(25, [plays("Opening Night", 1), goes("Bonus", 12)]),
        page(9, [plays("The Lighthouse Keeper", 2), plays("Video Commentary", 6, 192)], route=[(25, 1, 1)]),
        page(12, [goes("Scenes Deleted", 15), plays("Psychouts", 21, 40), goes("Video Commentaries", 20)],
             heading="bonus features", route=[(25, 1, 2)]),
        page(15, [plays("Opening Night", 22, 58)], heading="deleted scenes", route=[(25, 1, 2), (12, 1, 1)]),
        page(20, [plays("The Lighthouse Keeper Video Commentary", 5, 192)], route=[(25, 1, 2), (12, 1, 3)])]}
    tv = menus.tv_titles(out)
    found = menus.tv_found(tv, core.menu_episodes(tv["readings"], ORDERS)["picks"])
    titles = [{"id": "Title 5", "role": "extra", "duration": 192},
              {"id": "Title 6", "role": "duplicate", "duration": 192, "note": "same video as Title 5"},
              {"id": "Title 21", "role": "junk", "duration": 40}, {"id": "Title 22", "role": "junk", "duration": 58}]
    labels = menus.detected_tv_labels(found, titles)
    assert "Title 6" not in labels
    assert labels["Title 22"]["place"] == [2, 1, 1] and labels["Title 21"]["place"] == [2, 2]
    assert labels["Title 5"]["place"] == [2, 3, 1]
    # a copy's fuller name is taken; the title's own place stays
    copy = {"extras": [{"title": 5, "name": "Short", "seconds": 192, "kind": None, "place": [2, 3, 1]},
                       {"title": 6, "name": "A Much Fuller Name", "seconds": 192, "kind": None, "place": [1, 2]}]}
    labels = menus.detected_tv_labels(copy, titles)
    assert labels["Title 5"]["title"] == "A Much Fuller Name" and labels["Title 5"]["place"] == [2, 3, 1]


def commentary_disc(on, off, heading="Audio Commentary The Lighthouse Keeper With The Cast"):
    return {"pages": [
        page(2, [plays("Opening Night", 1), plays("The Lighthouse Keeper", 2), goes("Bonus", 6)]),
        page(6, [on, off, goes("Main Menu", 2)], heading=heading, route=[(2, 1, 3)])]}


def commentary_extras(out):
    tv = menus.tv_titles(out)
    return menus.tv_found(tv, core.menu_episodes(tv["readings"], ORDERS)["picks"])


def test_an_audio_commentary_stored_as_its_own_title():
    # (Boy Meets World S1 D1's: ON plays a copy of the episode with the commentary for its audio)
    found = commentary_extras(commentary_disc(plays("ON", 14, EPISODE - 1), plays("OFF", 2)))
    assert sorted((e["title"], e["episode"]) for e in found["episodes"]) == [(1, 1), (2, 2)]
    assert [(e["title"], e["name"], e["episode"]) for e in found["extras"]] == [
        (14, "The Lighthouse Keeper Audio Commentary", 2)]
    labels = menus.detected_tv_labels(found, [{"id": "Title 2", "role": "episode", "duration": EPISODE},
                                              {"id": "Title 14", "role": "extra", "duration": EPISODE - 1}])
    assert labels["Title 14"]["type"] == "Extra" and labels["Title 14"]["season"] == 1
    assert labels["Title 14"]["place"] == [3, 1]
    # what the buttons play says it, whatever they say or in what order: unread, misread, other words
    for on, off in ((plays("", 14, EPISODE - 1), plays("", 2)), (plays("0N", 14), plays("0FF", 2)),
                    (plays("OFF", 2), plays("ON", 14)), (plays("With Commentary", 14), plays("Without Commentary", 2))):
        found = commentary_extras(commentary_disc(on, off))
        assert [(e["title"], e["name"]) for e in found["extras"]] == [(14, "The Lighthouse Keeper Audio Commentary")]


def test_other_on_and_off_buttons_name_nothing():
    # the one title, its audio switched; not about a commentary; not as long as the episode; no episode
    for out in (commentary_disc(plays("ON", 2), plays("OFF", 2)),
                commentary_disc(plays("ON", 14), plays("OFF", 2), heading="Subtitles"),
                commentary_disc(plays("ON", 14, 300), plays("OFF", 2)),
                commentary_disc(plays("ON", 14), plays("OFF", 15))):
        assert commentary_extras(out)["extras"] == []
    # a button naming its own title isn't a version of the episode beside it
    found = commentary_extras(commentary_disc(plays("Behind The Lighthouse", 14), plays("OFF", 2)))
    assert [e["name"] for e in found["extras"]] == ["Behind The Lighthouse"]


def test_deleted_scenes_in_their_many_wordings():
    for words in ("Deleted Scenes", "deleted and extended scenes", "Deleted Extended Scene", "Extended Scenes"):
        assert menus.heading_kind(words) == "DeletedScene", words
        assert menus._kind_alone(words), words
    assert not menus._kind_alone("Real Girl Deleted Extended Scenes")
    assert menus.heading_kind("Extended Version") is None and menus.heading_kind("Deleted") is None


def test_deleted_scenes_matched_to_their_episodes():
    # (Psych S6 D3's: under an episode, part of its name read; on a page of them, named in full)
    picks = {0: {"season": 6, "number": 9, "name": "Neil Simon's Lover's Retreat", "score": 1, "by": "name"},
             1: {"season": 6, "number": 12, "name": "Shawn and the Real Girl", "score": 1, "by": "name"}}
    read = lambda t, text, seconds=EPISODE, **kw: dict(title=t, text=text, seconds=seconds, bonus=False, kind=None, **kw)
    tv = {"readings": [read(13, "9. Neil Simon's"), read(16, "12. Shawn and the Real Girl"),
                       read(23, "Lover's Retreat Deleted Extended Scene", 65.6), read(26, "Real Girl Deleted Extended Scenes", 572.2),
                       dict(read(22, "Neil Simon's Lover's Retreat", 58.6), kind="DeletedScene"),
                       dict(read(24, "Shawn and the Real Girl", 572.2), kind="DeletedScene"),
                       dict(read(27, "Underground with the Cast", 245.7), bonus=True)],
          "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    found = menus.tv_found(tv, picks)
    by = {e["title"]: e for e in found["extras"]}
    assert (by[23]["kind"], by[23]["season"], by[23]["episode"]) == ("DeletedScene", 6, 9)
    assert by[23]["name"] == "Neil Simon's Lover's Retreat Deleted Extended Scene"
    assert (by[26]["episode"], by[26]["name"]) == (12, "Shawn and the Real Girl Deleted Extended Scenes")
    assert (by[22]["episode"], by[22]["name"]) == (9, "Neil Simon's Lover's Retreat Deleted Scene")
    assert by[27]["kind"] is None and "episode" not in by[27]                 # (an extra's an extra)
    titles = [{"id": "Title %d" % n, "role": "extra"} for n in (23, 24, 26, 27)]
    labels = menus.detected_tv_labels(found, titles)
    assert labels["Title 23"]["type"] == "DeletedScene" and labels["Title 23"]["episode"] == 9
    assert labels["Title 23"]["season"] == 6 and labels["Title 27"]["type"] == "Extra"
    # the same video, short titles too: one label, on the plainest, with the fullest name
    # (by the scan's say: as in the app, whose titles have lost their cells by now)
    scene = [(8, 188, 20624)]
    short = [{"id": "Title 22", "duration": 58.6, "segments": tuple(scene + [(8, 0, 3)])},
             {"id": "Title 23", "duration": 65.6, "segments": tuple(scene + [(8, 20625, 20951), (8, 0, 3)])},
             {"id": "Title 24", "duration": 572, "segments": ((8, 21129, 98000),)},
             {"id": "Title 26", "duration": 572, "segments": ((8, 21129, 98000),)},
             {"id": "Title 28", "duration": 1500, "segments": ((8, 99000, 400000),)}]
    for t in short:
        t["group"] = "VTS 8"
    core.classify(short, "DVD")
    assert (short[1]["role"], short[1]["note"]) == ("duplicate", "same video as Title 22") and short[0]["role"] == "junk"
    assert (short[3]["role"], short[3]["note"]) == ("duplicate", "same video as Title 24")
    copies = [{k: v for k, v in t.items() if k != "segments"} for t in short]
    labels = menus.detected_tv_labels(found, copies)
    assert "Title 23" not in labels and "Title 26" not in labels
    assert labels["Title 22"]["title"] == "Neil Simon's Lover's Retreat Deleted Extended Scene"
    assert labels["Title 24"]["title"] == "Shawn and the Real Girl Deleted Extended Scenes"
    # two episodes it fits as well: not guessed
    two = [{"season": 1, "number": 1, "name": "The Real Girl"}, {"season": 1, "number": 2, "name": "The Real Girl"}]
    assert menus._episode_named("Real Girl", two) is None
    assert menus._episode_named("Nothing Like It", two) is None
