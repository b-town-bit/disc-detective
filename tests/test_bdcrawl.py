"""A TV Blu-ray's episode page read and pressed, on a pretend VLC that behaves as Chuck's menu does."""
import os
import sys
import types

import pytest

from disc_detective import bdcrawl, bdmenus, core, menus

W = [(72, 282), (376, 282), (72, 305), (376, 305), (72, 328), (376, 328)]
NAMES = ["Chuck Versus the Fat Lady", "Chuck Versus the Gravitron", "Chuck Versus the Sensei", "Chuck Versus the DeLorean",
         "Chuck Versus Santa Claus", "Chuck Versus the Third Dimension"]
CLIPS = ["00016", "00007", "00008", "00018", "00001", "00006"]
LENGTH = 2500.0
ITEMS = [(c, i * LENGTH, LENGTH) for i, c in enumerate(CLIPS)]
TITLES = [{"id": "00001.mpls", "clip_list": ",".join(CLIPS), "duration": 6 * LENGTH}] + \
         [{"id": "%s.m2ts" % c, "clip_list": c, "duration": LENGTH} for c in CLIPS]
BAR_LINES = [("LANGUAGES", (190, 405, 300, 430), 0.9), ("EPISODES", (345, 405, 445, 430), 0.9), ("SPECIAL", (495, 395, 580, 415), 0.9)]


def line(text, x, y, width=260):
    return (text, (x, y, x + width, y + 21), 0.95)


def test_a_page_of_numbered_buttons():
    rows = bdcrawl.page_rows([line(">>7) Chuck Versus the Fat Lady", 72, 282), line("8) Chuck Versus the Gravitron", 383, 282),
                              line("9) Chuck Versus the Sensei", 85, 305)] + BAR_LINES)
    assert [(r["number"], r["name"], r["marked"]) for r in rows] == [
        (7, "Chuck Versus the Fat Lady", True), (8, "Chuck Versus the Gravitron", False), (9, "Chuck Versus the Sensei", False)]


def test_columns_read_as_one_line_are_two_buttons_and_the_marker_stays_on_its_own():
    rows = bdcrawl.page_rows([("Chuck Versus the Gravitron >> 1o) Chuck Versus the DeLorean", (80, 305, 640, 326), 0.9),
                              ("8) Chuck Versus Santa Claus 9) Chuck Versus the Sensei", (80, 330, 640, 350), 0.9)])
    numbered = {r["number"]: r for r in rows}
    assert numbered[10]["marked"] and numbered[10]["name"] == "Chuck Versus the DeLorean" and numbered[10]["box"][0] > 300
    assert numbered[8]["name"] == "Chuck Versus Santa Claus" and numbered[9]["box"][0] > numbered[8]["box"][0]
    assert not numbered[8]["marked"]


def test_a_wrapped_name_joins_its_row_and_the_bar_doesnt():
    rows = bdcrawl.page_rows([line("12) Chuck Versus the Third", 378, 330), line("Dimension", 410, 352, 90)] + BAR_LINES)
    assert [r["name"] for r in rows] == ["Chuck Versus the Third Dimension"]


def test_no_numbers_takes_the_lines_around_a_marker_and_no_marker_says_no_page():
    rows = bdcrawl.page_rows([line(">> Pilot", 72, 282), line("Helicopter", 72, 305)] + BAR_LINES)
    assert [(r["number"], r["name"], r["marked"]) for r in rows] == [(None, "Pilot", True), (None, "Helicopter", False)]
    assert bdcrawl.page_rows([line("EMERGENCY EXIT ONLY", 300, 200)] + BAR_LINES) == []


def test_arrows_go_along_a_row_or_between_rows():
    assert bdcrawl.key_towards((72, 282, 0, 0), (376, 285, 0, 0)) == "right"
    assert bdcrawl.key_towards((376, 282, 0, 0), (72, 282, 0, 0)) == "left"
    assert bdcrawl.key_towards((376, 282, 0, 0), (72, 305, 0, 0)) == "down"
    assert bdcrawl.key_towards((72, 328, 0, 0), (376, 305, 0, 0)) == "up"


def test_a_landing_names_a_clip_only_near_its_start():
    assert bdcrawl.clip_at(ITEMS, 2503) == ("00007", LENGTH, LENGTH)
    assert bdcrawl.clip_at(ITEMS, 3000) is None


def test_a_title_by_its_clip_or_its_one_length():
    assert bdcrawl.title_of(TITLES, clip="00018") == "00018.m2ts"
    assert bdcrawl.title_of(TITLES, clip="00099") is None
    assert bdcrawl.title_of(TITLES, seconds=6 * LENGTH) == "00001.mpls"
    assert bdcrawl.title_of(TITLES, seconds=LENGTH) is None              # (six of that length: can't say)


def test_the_play_all_found_in_the_discs_own_playlists(tmp_path):
    from tests.test_sting import make_disc, mpls
    root = make_disc(tmp_path, {"00001.mpls": mpls(["00016", "00007"], seconds=100), "00002.mpls": mpls(["00900"], seconds=200)})
    assert bdcrawl.play_alls(root, 200) == [("00016", 0.0, 100.0), ("00007", 100.0, 100.0)]
    assert bdcrawl.play_alls(root, 999) == []


# the special features page: a Play All, then two columns of scenes with a line under each
SCENES = [("Chuck Versus the Fat Lady Sc. 8-10", "Chuck with Jill, Sarah and Casey.", 72, 97, 64.564),
          ("Chuck Versus the Gravitron Sc. 25", "Awesome talks with Ellie.", 72, 174, 112.446),
          ("Chuck Versus Santa Claus Sc. 13", "Casey and Sarah running toward Buy More.", 376, 54, 11.345)]
SCENE_TITLES = [{"id": "00010.mpls", "clip_list": "00022", "duration": 64.564}, {"id": "00014.mpls", "clip_list": "00025", "duration": 112.446},
                {"id": "00017.mpls", "clip_list": "00027", "duration": 11.345}, {"id": "00040.mpls", "clip_list": "00022,00025,00027", "duration": 188.4}]


LOW_SCENE = [("Chuck Versus Santa Claus Sc. 24", "Extended Emmett scene after robbery.", 376, 100, 30.5)]
LOW_TITLES = [{"id": "00010.mpls", "clip_list": "00022", "duration": 64.564}, {"id": "00014.mpls", "clip_list": "00025", "duration": 112.446},
              {"id": "00017.mpls", "clip_list": "00027", "duration": 11.345}, {"id": "00018.mpls", "clip_list": "00028", "duration": 30.5},
              {"id": "00040.mpls", "clip_list": "00022,00025,00027,00028", "duration": 218.9}]


class FakeVlc:
    """Chuck's pop-up: opens on its first item, Episodes a press right and activate away, the marker
    on the last button pressed, left up after a press in the Play All; the top menu after a scene."""
    frames = 10

    def __init__(self, landing=None, lose_marker=(), lag=0, stuck_up=False, slow_bar=0, low_page=False):
        # low_page: Chuck S2 D1's, laid out lower: across from the Play All is the right column's second,
        # its first above that and as high as the heading, reached only going up
        self.low_page, self.scenes = low_page, SCENES + (LOW_SCENE if low_page else [])
        self.title, self.time, self.pressed, self.saved = 1, 10.0, [], []
        self.bar, self.bar_at, self.page, self.at, self.extra_at = False, 0, None, 0, 0
        self.landing = landing or (lambda i: i * LENGTH)
        self.lose_marker, self.lag, self.lagging, self.before = lose_marker, lag, 0, 0
        self.stuck_up, self.slow_bar, self.waiting = stuck_up, slow_bar, 0
        self.length = 6 * LENGTH
        self.scenes_pressed = []

    def state(self):
        return {"title": self.title, "time_ms": int(self.time * 1000), "chapter": 0}

    def titles(self):
        return [{"index": 0, "menu": True, "seconds": 0}, {"index": 1, "seconds": 6 * LENGTH}]

    def length_ms(self):
        return self.length * 1000

    def menu(self):
        """The page again by itself, no bar; the marker back on its Play All."""
        self.title, self.time, self.bar, self.page, self.extra_at = 0, 0.0, True, "extras", 0

    def press(self, key):
        self.time += 0.1
        if key == "popup":
            self.bar, self.bar_at, self.page = not self.bar, 0, None
            self.waiting = self.slow_bar if self.bar else 0
        elif key == "activate" and self.page == "episodes":
            self.pressed.append(self.at)
            self.title, self.time, self.page, self.bar = 1, self.landing(self.at), None, self.stuck_up
        elif key == "activate" and self.page == "extras":
            if self.extra_at:
                name = self.scenes[self.extra_at - 1]
                self.scenes_pressed.append(name[0])
                self.title, self.time, self.length, self.page, self.bar = 20 + self.extra_at, 0.0, name[4], None, False
        elif key == "activate" and self.bar:
            self.page = {1: "episodes", 2: "extras"}.get(self.bar_at)
        elif self.page == "episodes":
            move = {"right": 1, "left": -1, "down": 2, "up": -2}[key]
            self.before = self.at
            if 0 <= self.at + move < len(NAMES):
                self.at += move
            self.lagging = self.lag
        elif self.page == "extras":
            # Play All, down its column; right across to the other column's first; off the page below
            moves = {(0, "down"): 1, (1, "up"): 0, (1, "down"): 2, (2, "up"): 1, (0, "right"): 3, (1, "right"): 3, (2, "right"): 3, (3, "left"): 0}
            if self.low_page:
                moves = {(0, "down"): 1, (1, "up"): 0, (1, "down"): 2, (2, "up"): 1, (0, "right"): 4, (1, "right"): 4, (2, "right"): 4,
                         (4, "left"): 0, (4, "up"): 3, (3, "down"): 4, (3, "left"): 0}
            self.extra_at = moves.get((self.extra_at, key), self.extra_at)
        elif self.bar and key == "right":
            self.bar_at += 1

    def frame(self):
        return self

    def save(self, path):
        self.saved.append(path)

    def lines(self):
        if not self.bar:
            return []
        if self.waiting:
            self.waiting -= 1
            return []
        if self.page == "extras":
            out = [line("DECLASSIFIED SCENES", 125, 55 if self.low_page else 45, 190), line("%sPLAY ALL" % (">>" if not self.extra_at else ""), 169, 74, 84)]
            for i, (name, under, x, y, _) in enumerate(self.scenes):
                out += [line("%s%s" % (">>" if self.extra_at == i + 1 else "", name), x, y, 280), line(under, x, y + 17, 280)]
            return out + [line("ONLINE", 480, 295, 60), line("Visit us at warnerblu.com", 400, 330, 240)] + BAR_LINES
        if self.page != "episodes":
            return BAR_LINES
        shown = self.at
        if self.lagging:                    # (the marker a frame behind the press)
            self.lagging -= 1
            shown = self.before
        out = []
        for i, name in enumerate(NAMES):
            mark = ">>" if i == shown and i not in self.lose_marker else ""
            out.append(line("%s%d) %s" % (mark, 7 + i, name), W[i][0], W[i][1]))
        return out + BAR_LINES


def crawl(monkeypatch, v, tmp_path):
    clock = {"t": 0.0}
    if v.low_page:
        monkeypatch.setattr(sys.modules[__name__], "SCENE_TITLES", LOW_TITLES)

    def sleep(s):
        clock["t"] += s
        v.time += s

    monkeypatch.setattr(menus, "rapid_lines", lambda frame, least=None: frame.lines())
    scene_titles = sys.modules[__name__].SCENE_TITLES
    c = bdcrawl.Crawler(v, TITLES + scene_titles, ITEMS, str(tmp_path), log=lambda s: None, sleep=sleep, clock=lambda: clock["t"])
    got = c.crawl(v.titles()[1:], 1)
    v.extras = c.extras(v.titles()[1:], 1)
    return got


def test_every_button_is_pressed_and_named_for_the_clip_it_lands_on(monkeypatch, tmp_path):
    v = FakeVlc()
    pressed = crawl(monkeypatch, v, tmp_path)
    assert [(p["number"], p["name"], p["title"]) for p in pressed] == [
        (7 + i, NAMES[i], "%s.m2ts" % CLIPS[i]) for i in range(6)]
    assert v.pressed == list(range(6))


def test_buttons_out_of_the_discs_order_go_by_where_they_land(monkeypatch, tmp_path):
    """The whole point: the third button plays the fourth clip."""
    order = [5, 2, 3, 0, 1, 4]
    pressed = crawl(monkeypatch, FakeVlc(lambda i: order[i] * LENGTH), tmp_path)
    assert {p["number"]: p["title"] for p in pressed} == {7 + i: "%s.m2ts" % CLIPS[order[i]] for i in range(6)}


def test_a_pop_up_left_up_after_a_press_is_closed_first_and_one_slow_to_come_is_waited_for(monkeypatch, tmp_path):
    for v in (FakeVlc(stuck_up=True), FakeVlc(slow_bar=2)):
        assert len(crawl(monkeypatch, v, tmp_path)) == 6 and v.pressed == list(range(6))


def test_a_button_above_where_the_marker_came_across_is_found_and_the_page_keeps_its_kind(monkeypatch, tmp_path):
    """Chuck S2 D1's: up from the right column's second is the only way to its first, which sits
    higher than the heading. Three found against a Play All of four: every way from each, then."""
    v = FakeVlc(low_page=True)
    crawl(monkeypatch, v, tmp_path)
    assert {p["title"] for p in v.extras} == {"00010.mpls", "00014.mpls", "00017.mpls", "00018.mpls"}
    assert v.scenes_pressed == [SCENES[0][0], LOW_SCENE[0][0]]                 # (the two ends again, not all)


def test_a_found_page_tries_no_more_ways_than_it_needs(monkeypatch, tmp_path):
    v = FakeVlc()
    keys = []
    press = v.press
    v.press = lambda k: (keys.append((v.page, v.extra_at, k)), press(k))
    crawl(monkeypatch, v, tmp_path)
    assert not [k for k in keys if k[0] == "extras" and k[1] == 3 and k[2] == "up"]


def tall(text, x, y, width=190):
    return (text, (x, y, x + width, y + 28), 0.95)


def test_a_scenes_own_words_arent_the_pages_heading():
    """Chuck S2 D1's "Emmett interviews Chuck" made every scene on its page an interview."""
    sawyer = {"text": "Chuck Versus Tom Sawyer", "box": (400, 140, 620, 156)}
    lines = [tall("DECLASSIFIED SCENES", 123, 73), line("Chuck Versus Tom Sawyer", 400, 140), line("Sc. B 29-30", 450, 156),
             line("Emmett interviews Chuck", 400, 172)]
    assert bdcrawl.page_heading(lines, [sawyer]) == "DECLASSIFIED SCENES"
    assert menus.heading_kind(bdcrawl.page_heading(lines, [sawyer])) is None
    assert bdcrawl.page_heading(lines[1:], [sawyer]) is None           # (the heading unread: still no interview)


def test_a_real_heading_gives_the_page_its_kind():
    assert menus.heading_kind(bdcrawl.page_heading([tall("DELETED SCENES", 123, 41), line("First Date", 84, 100), line("Morgan and Herd", 84, 120)], [])) == "DeletedScene"


def test_a_heading_takes_two_signs_and_a_button_not_found_has_only_one():
    missed = line("Chuck Versus the Break-Up Sc. 27", 375, 72)
    heading = tall("DELETED SCENES", 123, 73, 190)
    rest = [line("Morgan and Lester ponder their end", 375, 95), line("Play All", 169, 110), line("Chuck Versus the First Date", 84, 140)]
    assert bdcrawl.page_heading([missed, heading] + rest, []) == "DELETED SCENES"
    assert bdcrawl.page_heading([missed] + rest, []) is None             # (the heading unread: nothing's taken for it)


def test_a_line_as_tall_as_the_rest_is_a_heading_by_its_colour_and_place():
    from PIL import Image, ImageDraw
    page = Image.new("RGB", (720, 480), (150, 30, 30))
    ImageDraw.Draw(page).rectangle((123, 41, 313, 62), fill=(255, 255, 255))
    lines = [line("OUTTAKES", 123, 41, 190), line("First Date", 84, 100), line("Morgan and Herd", 84, 120)]
    assert bdcrawl.page_heading(lines, []) is None                       # (no picture: only its place says so)
    assert bdcrawl.page_heading(lines, [], page) == "OUTTAKES"


def dvd_page(monkeypatch, lines):
    from PIL import Image
    monkeypatch.setattr(menus, "_page_lines", lambda image, scale=1: lines)
    return Image.new("RGB", (720, 480), (20, 20, 60))


def test_a_dvds_heading_line_is_the_one_with_the_signs_outside_its_buttons(monkeypatch):
    lines = [tall("DELETED AND EXTENDED SCENES", 100, 40, 400), line("Pilot", 100, 120), line("Bonus scene", 100, 160), line("Main menu", 100, 300)]
    page = dvd_page(monkeypatch, lines)
    assert menus.page_heading_line(page, [(90, 110, 400, 150), (90, 150, 400, 190), (90, 290, 400, 330)]) == "DELETED AND EXTENDED SCENES"
    # the same line inside a button is that button's
    assert menus.page_heading_line(page, [(90, 30, 520, 80), (90, 110, 400, 150), (90, 150, 400, 190), (90, 290, 400, 330)]) is None


def test_a_dvds_page_with_no_reading_has_no_heading_line(monkeypatch):
    assert menus.page_heading_line(dvd_page(monkeypatch, None), []) is None


def test_a_pages_type_goes_by_its_heading_line_then_all_its_other_words():
    """Over the saved DVD pages: "eS iN Les" over a page whose heading line says deleted scenes; and
    "Theatrical Trailers", no taller than the rest, kept by its words."""
    assert menus.page_kind({"heading": "eS iN Les", "heading_line": "DELETED AND EXTENDED SCENES"}) == "DeletedScene"
    assert menus.page_kind({"heading": "Theatrical Trailers", "heading_line": None}) == "Trailer"
    assert menus.page_kind({"heading": "Theatrical Trailers"}) == "Trailer"          # (a crawl saved before)
    assert menus.page_kind({"heading": "Bonus Features", "heading_line": "Bonus Features"}) is None


def test_the_bar_and_a_link_are_no_heading_however_they_stand_out():
    lines = [tall("LANGUAGES", 190, 405, 110), tall("ONLINE", 480, 295, 60), line("Morgan and Herd", 84, 120), line("First Date", 84, 100)]
    assert bdcrawl.page_heading(lines, []) is None


PLAIN = [line("First Date", 84, 100), line("Morgan and Herd", 84, 130), line("Tyler arrives", 84, 240), line("The Long Goodbye", 400, 100)]


def test_a_page_of_several_headings_gives_its_titles_no_type():
    """Which title goes under which is the user's to say, whatever the buttons on the way said."""
    for second in ("FEATURETTES", "ONLINE"):
        lines = [tall("DELETED SCENES", 84, 60, 240), tall(second, 84, 200, 240)]
        assert menus.several_headings(lines, lines + PLAIN)
        assert bdcrawl.several_headings(lines + PLAIN, [{"text": l[0], "box": l[1]} for l in PLAIN])
    assert menus.page_kind({"heading_line": "Deleted Scenes", "heading": "Deleted Scenes", "mixed": True}) is None


def test_one_heading_or_none_naming_a_type_is_not_several():
    assert not menus.several_headings([tall("DELETED SCENES", 84, 60, 240)], [tall("DELETED SCENES", 84, 60, 240)] + PLAIN)
    lines = [tall("DECLASSIFIED SCENES", 84, 60, 240), tall("ONLINE", 400, 200, 100)]
    assert not menus.several_headings(lines, lines + PLAIN)


def test_menu_options_beside_their_markers_are_no_headings():
    """Spider-Man's TRAILERS and TV SPOTS, each button's box a small marker beside its words."""
    lines = [tall("MARKETING", 304, 66, 200), tall("TRAILERS", 207, 229, 150), tall("TV SPOTS", 208, 340, 150)]
    assert menus.several_headings(lines, lines + PLAIN)
    assert not menus.several_headings(lines, lines + PLAIN, names=["TRAILERS", "TRAILERS MUSIC VIDEOS TV SPOTS", "TV SPOTS"])


def test_a_button_already_where_it_leads_is_pressed_again_after_the_others(monkeypatch, tmp_path):
    """Chuck S2 D1: the ad reel over, the Play All just begun, so its first button seemed to do nothing."""
    v = FakeVlc()
    v.landing = lambda i: v.time if len(v.pressed) == 1 else i * LENGTH
    pressed = crawl(monkeypatch, v, tmp_path)
    assert sorted(p["number"] for p in pressed) == [7 + i for i in range(6)]
    assert v.pressed == [0, 1, 2, 3, 4, 5, 0]


def test_a_button_that_never_moves_is_tried_only_twice(monkeypatch, tmp_path):
    v = FakeVlc()
    v.landing = lambda i: v.time if i == 0 else i * LENGTH
    pressed = crawl(monkeypatch, v, tmp_path)
    assert v.pressed.count(0) == 2 and sorted(p["number"] for p in pressed) == [8, 9, 10, 11, 12]


def test_looking_every_way_stops_once_the_page_adds_up(monkeypatch, tmp_path):
    v = FakeVlc(low_page=True)
    keys = []
    press = v.press
    v.press = lambda k: (keys.append((v.page, v.extra_at, k)), press(k))
    crawl(monkeypatch, v, tmp_path)
    tried = {k for k in keys if k[0] == "extras" and k[2] != "activate"}
    assert ("extras", 4, "up") in tried and len(v.extras) == 4
    assert len(tried) < 5 * 4                                           # (five buttons, four ways: not all of them)


def test_scenes_on_a_page_of_no_known_kind_still_go_with_their_episodes():
    made = {"title": "00018.mpls", "number": None, "name": "Chuck Versus The Ex Sc. 51 - Sexual Harassment Seminar", "seconds": 90, "box": (0, 0, 1, 1),
            "order": 100, "image": None, "extra": True, "kind": None}
    ep = {"title": "00020.m2ts", "number": 6, "name": "Chuck Versus the Ex", "seconds": LENGTH, "box": (0, 0, 1, 1), "order": 4, "image": None}
    tv = {"readings": bdcrawl.readings_of([ep, made]), "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    extra = menus.tv_found(tv, {0: {"season": 2, "number": 6, "name": "Chuck Versus the Ex", "score": 1.0, "by": "name"}})["extras"][0]
    assert (extra["season"], extra["episode"], extra["name"]) == (2, 6, "Sc. 51 - Sexual Harassment Seminar")
    assert extra["kind"] == "DeletedScene"


def found_for(name, kind=None):
    made = {"title": "00018.mpls", "number": None, "name": name, "seconds": 90, "box": (0, 0, 1, 1),
            "order": 100, "image": None, "extra": True, "kind": kind}
    ep = {"title": "00020.m2ts", "number": 6, "name": "Chuck Versus the Ex", "seconds": LENGTH, "box": (0, 0, 1, 1), "order": 4, "image": None}
    tv = {"readings": bdcrawl.readings_of([ep, made]), "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    return menus.tv_found(tv, {0: {"season": 2, "number": 6, "name": "Chuck Versus the Ex", "score": 1.0, "by": "name"}})["extras"][0]


def test_a_scenes_number_and_its_episode_make_a_deleted_scene_however_its_written():
    for rest in ("Sc. 8-10 - Chuck", "Sc. 12,13", "Sc.9-3 - Emmett", "Scene 4", "Sc. B 29-30", "SC. 51"):
        extra = found_for("Chuck Versus the Ex - " + rest)
        assert extra["kind"] == "DeletedScene" and extra["episode"] == 6, rest


def test_the_word_scene_with_its_episode_is_only_linked():
    for rest in ("Emmett scene after robbery", "Two Scenes", "Behind the Scenes", "Scene Selection", "Outtake Sc. 4", "Bloopers"):
        extra = found_for("Chuck Versus the Ex - " + rest)
        assert extra["kind"] is None and extra["episode"] == 6, rest


def test_the_scene_type_is_never_given_by_the_menus():
    assert "Scene" not in {found_for("Chuck Versus the Ex " + r)["kind"] for r in ("Sc. 4", "Scene 4", "A scene", "Inside the Episode")}


def test_an_episodes_extra_with_no_scene_number_stays_a_plain_extra():
    for rest in ("Inside the Episode", "Scenery of Burbank", "Science Fair", "Commentary"):
        extra = found_for("Chuck Versus the Ex - " + rest)
        assert extra["kind"] is None and extra["episode"] == 6, rest


def test_a_scene_number_with_no_episode_is_one_sign_and_says_nothing():
    extra = found_for("Sc. 12 - Someone at a party")
    assert extra["kind"] is None and "episode" not in extra


def test_a_pages_word_for_its_titles_stands_over_the_scene_number():
    assert found_for("Chuck Versus the Ex Sc. 51", "Featurette")["kind"] == "Featurette"


def test_text_pointing_off_the_disc_is_no_name():
    for text in ("Visit us at warnerblu.com for", "www.example.co.uk", "WARNERBLU.COM", "see sonypictures.net", "ONLINE", "Online Features",
                 "DVD-ROM/ONLINE FEATURES", "BD-ROM Features"):
        assert bdcrawl.NOT_A_NAME.search(text), text
    for text in ("Morgan goes online.", "Chuck Versus the Online Date Sc. 4", "Sc.9-3", "Dr. Com Sc. 4", "Tyler arrives."):
        assert not bdcrawl.NOT_A_NAME.search(text), text


def test_a_button_pointing_off_the_disc_isnt_counted_or_pressed():
    assert bdcrawl.LINK.search("warnerblu.com")
    assert not bdcrawl.LINK.search("Chuck Versus the Online Date Sc. 4")


def test_an_address_goes_to_the_back_of_a_dvds_search():
    assert menus.SKIP_RE.search("Visit www.example.com") and menus.SKIP_RE.search("example.net")
    assert not menus.SKIP_RE.search("Dr. Com")


def test_a_series_own_word_for_its_scenes_means_nothing_to_the_page():
    lines = [line("DECLASSIFIED SCENES", 123, 41), line(">> PLAY ALL", 169, 74)]
    assert menus.heading_kind(bdcrawl.page_heading(lines, [])) is None


def test_special_features_are_found_by_their_marker_and_named_by_their_lengths(monkeypatch, tmp_path):
    v = FakeVlc()
    crawl(monkeypatch, v, tmp_path)
    got = {p["title"]: p for p in v.extras}
    assert set(got) == {"00010.mpls", "00014.mpls", "00017.mpls"}          # never the Play All's
    assert v.scenes_pressed == [SCENES[0][0], SCENES[-1][0]]               # the two ends: the Play All names the rest
    assert got["00010.mpls"]["name"] == "Chuck Versus the Fat Lady Sc. 8-10 - Chuck with Jill, Sarah and Casey"
    assert got["00017.mpls"]["name"] == "Chuck Versus Santa Claus Sc. 13 - Casey and Sarah running toward Buy More"
    assert not any(p["kind"] for p in v.extras)                           # ("Declassified" says nothing by itself)
    readings = bdcrawl.readings_of(v.extras)
    assert all(r["bonus"] and r["own_name"] for r in readings)


def test_ends_that_dont_fit_the_play_all_mean_pressing_every_button(monkeypatch, tmp_path):
    monkeypatch.setitem(SCENE_TITLES[3], "clip_list", "00027,00025,00022")
    v = FakeVlc()
    crawl(monkeypatch, v, tmp_path)
    assert sorted(v.scenes_pressed) == sorted(s[0] for s in SCENES)
    assert {p["title"]: p["name"].split(" - ")[0] for p in v.extras} == {
        "00010.mpls": SCENES[0][0], "00014.mpls": SCENES[1][0], "00017.mpls": SCENES[2][0]}


def test_no_play_all_means_pressing_every_button(monkeypatch, tmp_path):
    monkeypatch.setitem(SCENE_TITLES[3], "clip_list", "00022,00025")
    v = FakeVlc()
    crawl(monkeypatch, v, tmp_path)
    assert len(v.scenes_pressed) == 3 and len(v.extras) == 3


def test_a_pages_reading_order_goes_down_each_column():
    b = lambda x, y: {"box": (x, y, x + 280, y + 17)}
    made = [b(376, 54), b(72, 174), b(72, 97), b(376, 100)]
    assert [x["box"][:2] for x in bdcrawl.page_order(made)] == [(72, 97), (72, 174), (376, 54), (376, 100)]


def test_small_capitals_read_oddly_come_out_plain():
    assert bdcrawl.plain_caps("Tyler ARRIVES - Extended EmmeTt scene Sc. 8") == "Tyler Arrives - Extended Emmett scene Sc. 8"


def test_a_name_stops_at_the_next_buttons_line_even_read_a_little_higher():
    one = {"text": "Gravitron Sc. 58", "box": (72, 251, 350, 268)}
    two = {"text": "DeLorean Sc. 59", "box": (72, 290, 350, 306)}
    lines = [line("Big Mike comes back to Buy More.", 72, 268), line("DeLorean Sc. 59", 80, 287)]
    assert bdcrawl.button_words(one, [one, two], lines) == "Gravitron Sc. 58 - Big Mike comes back to Buy More"


def test_two_of_one_length_name_neither(monkeypatch, tmp_path):
    assert bdcrawl.title_of([{"id": "a", "duration": 64.5}, {"id": "b", "duration": 64.55}], seconds=64.5) is None


def test_scenes_keep_their_own_names_under_their_episode(monkeypatch, tmp_path):
    made = {"title": "00017.mpls", "number": None, "name": "Chuck Versus Santa Claus Sc. 13 - Casey and Sarah", "seconds": 11.3, "box": (0, 0, 1, 1),
            "order": 100, "image": None, "extra": True, "kind": "DeletedScene"}
    ep = {"title": "00001.m2ts", "number": 11, "name": "Chuck Versus Santa Claus", "seconds": LENGTH, "box": (0, 0, 1, 1), "order": 4, "image": None}
    tv = {"readings": bdcrawl.readings_of([ep, made]), "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    found = menus.tv_found(tv, {0: {"season": 2, "number": 11, "name": "Chuck Versus Santa Claus", "score": 1.0, "by": "name"}})
    assert [e["episode"] for e in found["episodes"]] == [11]
    extra = found["extras"][0]
    assert extra["kind"] == "DeletedScene" and (extra["season"], extra["episode"]) == (2, 11)
    assert extra["name"] == "Sc. 13 - Casey and Sarah"
    labels = menus.detected_tv_labels(found, [{"id": "00017.mpls", "role": "extra"}])["00017.mpls"]
    assert (labels["title"], labels["season"], labels["episode"]) == ("Sc. 13 - Casey and Sarah", 2, 11)


EPISODES = [{"season": 2, "episode": n, "name": "Chuck Versus " + x} for n, x in (
    (7, "the Fat Lady"), (8, "the Gravitron"), (9, "the Sensei"), (10, "the DeLorean"), (11, "Santa Claus"), (12, "the Third Dimension"))]


def test_a_scenes_name_opens_with_its_episode_whichever_it_is():
    for name, episode, rest in (("Chuck Versus the Fat Lady Sc. 12,13 - Chuck talks to Emmett, then Casey", 7, "Sc. 12,13 - Chuck talks to Emmett, then Casey"),
                                ("Chuck Versus the Gravitron Sc. 25 - Awesome talks with Ellie", 8, "Sc. 25 - Awesome talks with Ellie"),
                                ("Chuck Versus the DeLorean Sc. 59 - Morgan and Ellie", 10, "Sc. 59 - Morgan and Ellie"),
                                ("Chuck Versus the Third Dimension Sc. 8 - Tyler Arrives", 12, "Sc. 8 - Tyler Arrives"),
                                ("Chuck Versus the Gravtron Sc. 58 - Big Mike", 8, "Sc. 58 - Big Mike")):       # (a misreading)
        got = menus._episode_opening(name, EPISODES)
        assert (got[0]["episode"], got[1]) == (episode, rest)


def test_the_names_chuck_s2_d1_read_go_with_their_episodes():
    eps = [{"season": 2, "episode": n, "name": x} for n, x in (
        (1, "Chuck Versus the First Date"), (2, "Chuck Versus the Seduction"), (3, "Chuck Versus the Break-Up"),
        (4, "Chuck Versus the Cougars"), (5, "Chuck Versus Tom Sawyer"), (6, "Chuck Versus the Ex"))]
    for name, episode, rest in (("Chuck Versus Tom Sawyer Sc.9-3 - Emmett interviews Chuck", 5, "Sc.9-3 - Emmett interviews Chuck"),
                                ("Chuck Versus The Ex Sc. 10 - Chuck gets prepped for date/mission", 6, "Sc. 10 - Chuck gets prepped for date/mission"),
                                ("Chuck Versus the Break-Up Sc. 4 - Morgan Date-Crashes", 3, "Sc. 4 - Morgan Date-Crashes"),
                                ("Chuck Versus the First Date Sc. 34 - Sarah and Casey in Orange Drange", 1, "Sc. 34 - Sarah and Casey in Orange Drange")):
        got = menus._episode_opening(name, eps)
        assert (got[0]["episode"], got[1]) == (episode, rest)


def test_most_of_an_episodes_name_isnt_its_name():
    """Chuck S2 D1, E1 not pressed: "Chuck Versus the" fitted "Chuck Versus the Ex" and took its scenes."""
    eps = [{"season": 2, "episode": n, "name": x} for n, x in (
        (2, "Chuck Versus the Seduction"), (3, "Chuck Versus the Break-Up"), (6, "Chuck Versus the Ex"))]
    assert menus._episode_opening("Chuck Versus the First Date Sc. 26 - Morgan speaks to Herd", eps) is None
    assert menus._episode_opening("Chuck Versus the Ex Sc. 51", eps)[0]["episode"] == 6


def test_a_scene_naming_no_episode_of_the_disc_keeps_its_whole_name():
    assert menus._episode_opening("Chuck Versus the Tooth Sc. 4 - Morgan", EPISODES) is None
    assert menus._episode_opening("Chuck Versus Santa Claus", EPISODES) is None          # (nothing left to call it)


def chuck_rep(labels):
    return {"content_hash": "4" * 32, "format": "Blu-ray", "kind": "tv", "best_guess": "Chuck (2007) S2 D2",
            "candidates": [{"kind": "tv", "name": "Chuck", "year": 2007, "imdb_id": "tt0934814", "score": 90}],
            "titles": [{"id": "00016.m2ts", "role": "episode", "duration": 2520, "episode_index": 1},
                       {"id": "00010.mpls", "role": "extra", "duration": 64},
                       {"id": "00014.mpls", "role": "extra", "duration": 112}],
            "labels": labels}


def test_a_linked_scene_sits_beside_its_episode_named_as_plex_wants():
    from disc_detective import app
    rep = chuck_rep({"00016.m2ts": {"type": "Episode", "title": "Chuck Versus the Fat Lady", "season": 2, "episode": 7},
                     "00010.mpls": {"type": "DeletedScene", "title": "Sc. 8-10 - Chuck with Jill", "season": 2, "episode": 7}})
    plan = {p["title_id"]: p["dest"] for p in app.plan_rip(rep, app.preview_titles(rep), True)}
    episode = "Chuck (2007) {imdb-tt0934814}/Season 02/Chuck (2007) - s02e07 - Chuck Versus the Fat Lady"
    assert plan["00016.m2ts"] == episode + ".mkv"
    assert plan["00010.mpls"] == episode + " - Sc. 8 to 10 Chuck with Jill-deleted.mkv"


def test_a_scene_finds_its_episodes_file_though_that_isnt_ripped_this_time():
    from disc_detective import app
    rep = chuck_rep({"00016.m2ts": {"type": "Episode", "title": "Chuck Versus the Fat Lady", "season": 2, "episode": 7},
                     "00010.mpls": {"type": "DeletedScene", "title": "Sc. 8", "season": 2, "episode": 7}})
    plan = app.plan_rip(rep, app.preview_titles(rep), True, selected={"00010.mpls"})
    assert [p["dest"] for p in plan] == ["Chuck (2007) {imdb-tt0934814}/Season 02/Chuck (2007) - s02e07 - Chuck Versus the Fat Lady - Sc. 8-deleted.mkv"]


def test_a_scene_goes_by_the_scans_name_for_its_episode_with_no_label_on_that():
    from disc_detective import app
    rep = chuck_rep({"00010.mpls": {"type": "DeletedScene", "title": "Sc. 8", "season": 2, "episode": 7}})
    rep["plex_names"] = ["00016.m2ts -> Chuck (2007) {imdb-tt0934814}/Season 02/Chuck (2007) - s02e07 - The Guess.mkv"]
    plan = {p["title_id"]: p["dest"] for p in app.plan_rip(rep, app.preview_titles(rep), True)}
    assert plan["00010.mpls"] == "Chuck (2007) {imdb-tt0934814}/Season 02/Chuck (2007) - s02e07 - The Guess - Sc. 8-deleted.mkv"


def test_a_scene_whose_episode_has_no_file_here_keeps_the_code_under_that_season():
    from disc_detective import app
    rep = chuck_rep({"00010.mpls": {"type": "DeletedScene", "title": "Sc. 8", "season": 2, "episode": 7}})
    plan = {p["title_id"]: p["dest"] for p in app.plan_rip(rep, app.preview_titles(rep), True)}
    assert plan["00010.mpls"] == "Chuck (2007) {imdb-tt0934814}/Season 02/Deleted Scenes/s02e07 - Sc. 8.mkv"


def test_a_scene_tied_to_no_episode_stays_in_the_shared_folder():
    from disc_detective import app
    rep = chuck_rep({"00010.mpls": {"type": "DeletedScene", "title": "Shawn's Lies"}})
    plan = {p["title_id"]: p["dest"] for p in app.plan_rip(rep, app.preview_titles(rep), True)}
    assert plan["00010.mpls"] == "Chuck (2007) {imdb-tt0934814}/Deleted Scenes/Shawn's Lies.mkv"


def test_any_extra_tied_to_an_episode_takes_its_types_ending_beside_it():
    from disc_detective import app
    file = "Psych (2006)/Season 08/Psych (2006) - s08e05 - A Nightmare on State Street.mkv"
    stem = file[:-4]
    assert app.linked_dest(file, "Inside the Episode", "Featurette") == stem + " - Inside the Episode-featurette.mkv"
    assert app.linked_dest(file, "Audio Commentary", "Extra") == stem + " - Audio Commentary-other.mkv"
    assert app.linked_dest(file, "A Nightmare on State Street - Director's Cut", None) == stem + " - Director's Cut-other.mkv"
    assert app.linked_dest(file, "A Nightmare on State Street Deleted Scene", "DeletedScene") == stem + " - Deleted Scene-deleted.mkv"
    assert app.linked_dest(file, "A Nightmare on State Street", "DeletedScene") == stem + " - A Nightmare on State Street-deleted.mkv"


def level(season=None, note=None, label=None):
    from disc_detective import app
    rep = chuck_rep({"00010.mpls": label or {"type": "Featurette", "title": "Declassified"}})
    rep["match"] = {"kind": "tv", "season": season}
    if note:
        rep["season_note"] = note
    return {p["title_id"]: p["dest"] for p in app.plan_rip(rep, app.preview_titles(rep), True)}["00010.mpls"]


SHOW = "Chuck (2007) {imdb-tt0934814}"


def test_an_extra_of_a_seasons_disc_goes_under_that_season():
    assert level(season=2) == SHOW + "/Season 02/Featurettes/Declassified.mkv"
    assert level(season=2, label={"type": "Extra", "title": "Gag Reel"}) == SHOW + "/Season 02/Other/Gag Reel.mkv"


def test_an_extra_of_a_disc_of_no_season_goes_under_the_show():
    assert level(season=None) == SHOW + "/Featurettes/Declassified.mkv"
    assert level(season=0) == SHOW + "/Featurettes/Declassified.mkv"


def test_a_season_only_worked_out_doesnt_place_an_extra():
    assert level(season=1, note="assumed: the label has a disc number but no season") == SHOW + "/Featurettes/Declassified.mkv"


def test_season_0s_own_episodes_take_their_extras_beside_them():
    from disc_detective import app
    rep = chuck_rep({"00016.m2ts": {"type": "Episode", "title": "Special", "season": 0, "episode": 2},
                     "00010.mpls": {"type": "Interview", "title": "The Cast", "season": 0, "episode": 2}})
    plan = {p["title_id"]: p["dest"] for p in app.plan_rip(rep, app.preview_titles(rep), True)}
    assert plan["00010.mpls"] == SHOW + "/Season 00/Chuck (2007) - s00e02 - Special - The Cast-interview.mkv"


def test_a_film_has_no_season_to_go_under():
    from disc_detective import app
    rep = {"content_hash": "5" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)",
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "match": {"kind": "movie", "season": None},
           "titles": [{"id": "Title 1", "role": "feature", "duration": 6000}, {"id": "Title 2", "role": "extra", "duration": 300}],
           "labels": {"Title 2": {"type": "Featurette", "title": "Making Of"}}}
    plan = {p["title_id"]: p["dest"] for p in app.plan_rip(rep, app.preview_titles(rep), True)}
    assert plan["Title 2"] == "Film (2001) {imdb-tt0000001}/Featurettes/Making Of.mkv"


def test_a_marker_that_lags_a_press_is_waited_for(monkeypatch, tmp_path):
    v = FakeVlc(lag=2)
    assert len(crawl(monkeypatch, v, tmp_path)) == 6 and v.pressed == list(range(6))


def test_a_button_whose_marker_cant_be_read_isnt_pressed(monkeypatch, tmp_path):
    v = FakeVlc(lose_marker={2})
    pressed = crawl(monkeypatch, v, tmp_path)
    assert 2 not in v.pressed and 9 not in [p["number"] for p in pressed]


def test_a_landing_inside_a_clip_names_nothing(monkeypatch, tmp_path):
    pressed = crawl(monkeypatch, FakeVlc(lambda i: i * LENGTH + (500 if i == 1 else 0)), tmp_path)
    assert 8 not in [p["number"] for p in pressed] and len(pressed) == 5


def test_a_title_two_buttons_land_on_is_nobodys():
    made = lambda n, t: {"title": t, "number": n, "name": "x%d" % n, "seconds": LENGTH, "box": (0, 0, 1, 1), "order": n, "image": None}
    readings = bdcrawl.readings_of([made(7, "a.m2ts"), made(8, "a.m2ts"), made(9, "b.m2ts")])
    assert [r["title"] for r in readings] == ["b.m2ts"] and readings[0]["text"] == "9) x9"


def test_a_blu_rays_episodes_become_labels_under_its_own_ids():
    pressed = [{"title": "00018.m2ts", "number": 10, "name": "Chuck Versus the DeLorean", "seconds": LENGTH, "box": (0, 0, 1, 1),
                "order": 3, "image": None}]
    tv = {"readings": bdcrawl.readings_of(pressed), "promos": [], "promo_names": {}, "play_alls": [], "opening": []}
    found = menus.tv_found(tv, {0: {"season": 2, "number": 10, "name": "Chuck Versus the DeLorean", "score": 1.0, "by": "name"}})
    labels = menus.detected_tv_labels(found, TITLES)
    assert labels["00018.m2ts"]["episode"] == 10 and labels["00018.m2ts"]["title"] == "Chuck Versus the DeLorean"
    assert "00018.m2ts" in menus.found_tv_text(found)


def test_the_apps_own_labelling_takes_a_blu_rays_ids():
    pytest.importorskip("tkinter")
    from disc_detective import app
    found = {"tv": True, "episodes": [{"title": "00018.m2ts", "season": 2, "episode": 10, "name": "Chuck Versus the DeLorean",
                                       "read": "10) Chuck Versus the DeLorean", "by": "name"}],
             "extras": [], "promos": [], "play_alls": [], "suspects": 6}
    rep = {"titles": [dict(t, role="episode", kind="tv") for t in TITLES], "kind": "tv", "format": "Blu-ray", "content_hash": "AB"}
    assert app.menu_played(found) == {"00018.m2ts"} and app.menu_title_id(3) == "Title 3" and app.menu_title_id("3") == "Title 3"
    assert app.detected_for(rep, found)["00018.m2ts"]["episode"] == 10
    assert app.films_by_menus(rep, found) is None
    assert app.suspect_report(found) == "The suspects named 1 episode"


def test_no_page_answering_says_so(monkeypatch, tmp_path):
    v = FakeVlc()
    v.lines = lambda: BAR_LINES
    assert crawl(monkeypatch, v, tmp_path) == []


def wb_disc(tmp_path):
    """A disc with Warner's BD-J menu files, as Arrow's has them: made up, in their layout."""
    tmp_path.mkdir(exist_ok=True)
    jar = tmp_path / "BDMV" / "JAR"
    menu, shared = jar / "04518", jar / "15631"
    menu.mkdir(parents=True)
    shared.mkdir()
    (menu / "images.txt").write_text("# sheets\nep_1_sel,composite_show_eng_1.png,0,0,10,10\n", encoding="latin1")
    (menu / "menu_base.prop").write_text("""
episode01.type=EpisodeButton
episode01.discName=SHOW_S3_D1
episode01.playlistNumber=51
episode01.episodeStringName=1
episode01.images=ep_1_nor,ep_1_sel
episode01scis.type=MenuButton
episode01scis.onEnterPressed=ecController.playEC,ec_d1,201
episode02.type=EpisodeButton
episode02.discName=SHOW_S3_D1
episode02.playlistNumber=71
episode02.episodeStringName=2
episode03.type=EpisodeButton
episode03.discName=SHOW_S3_D2
episode03.playlistNumber=51
episode03.episodeStringName=3
sf.type=Button
sf.onEnterPressed=menuController.activateMenu,sfpage
sfpage.children=btn1,btn2,other
btn1.type=Button
btn1.onEnterPressed=ecController.playEC,ec_d1,201
btn2.type=Button
btn2.onEnterPressed=ecController.playEC,ec_d1,202
other.type=Button
other.onEnterPressed=ecController.playEC,ec_d2,203
""", encoding="latin1")
    (shared / "states.prop").write_text("ec_d1.discName=SHOW_S3_D1\nec_d2.discName=SHOW_S3_D2\n", encoding="latin1")
    return str(tmp_path)


def test_warners_menu_files_say_which_episode_each_playlist_is(tmp_path):
    root = wb_disc(tmp_path)
    menu = bdmenus.wb_menu(root, "SHOW_S3_D1")
    assert menu["season"] == 3
    assert [(e["number"], e["playlist"]) for e in menu["episodes"]] == [(1, "00051.mpls"), (2, "00071.mpls")]
    assert {x["playlist"]: x["of_episode"] for x in menu["extras"]} == {"00201.mpls": 1, "00202.mpls": None}   # (not disc 2's)
    assert bdmenus.wb_menu(root, "SHOW_S3_D2")["episodes"][0]["number"] == 3
    assert bdmenus.wb_menu(root, "SOME_OTHER_DISC") is None
    assert bdmenus.wb_menu(str(tmp_path / "nothing"), "SHOW_S3_D1") is None


def test_a_disc_without_those_files_is_left_to_the_crawl(tmp_path):
    assert bdcrawl.from_files(str(tmp_path), "SHOW_S3_D1", [], read=lambda image: "") is None


ORDERS = {"aired": [{"season": 3, "number": 1, "name": "Pilot Light"}, {"season": 3, "number": 2, "name": "Second Wind"}]}
FILE_TITLES = [{"id": "00051.mpls", "duration": 2500}, {"id": "00071.mpls", "duration": 2500},
               {"id": "00201.mpls", "duration": 300}, {"id": "00202.mpls", "duration": 400}]


def test_episodes_by_their_files_number_are_named_from_the_shows_list(tmp_path):
    """No words read at all (no OCR installed): still every episode, its extra linked."""
    raw = bdcrawl.from_files(wb_disc(tmp_path), "SHOW_S3_D1", FILE_TITLES, read=lambda image: "")
    tv = raw["tv"]
    got = core.numbered_episodes(tv["readings"], ORDERS, tv["season"])
    assert {p["number"]: (p["name"], p["by"]) for p in got["picks"].values()} == {1: ("Pilot Light", "disc"), 2: ("Second Wind", "disc")}
    found = menus.tv_found(tv, got["picks"])
    assert [(e["title"], e["episode"], e["name"]) for e in found["episodes"]] == [("00051.mpls", 1, "Pilot Light"), ("00071.mpls", 2, "Second Wind")]
    scene = next(e for e in found["extras"] if e["title"] == "00201.mpls")
    assert (scene["episode"], scene["kind"], scene["name"]) == (1, "DeletedScene", "Pilot Light Deleted Scenes")
    labels = menus.detected_tv_labels(found, [dict(t, role="episode") for t in FILE_TITLES])
    assert labels["00051.mpls"]["note"] == "the disc's menu files"
    assert labels["00201.mpls"]["type"] == "DeletedScene" and labels["00201.mpls"]["episode"] == 1


def test_names_read_off_the_menu_check_it_is_the_right_show():
    read = lambda n, text: {"number": n, "text": "%d) %s" % (n, text), "seconds": 2500}
    agree = [read(1, "Pilot Light"), read(2, "Second Wind")]
    assert core.numbered_episodes(agree, ORDERS, 3)
    wrong = [read(1, "Something Else Entirely"), read(2, "Another Name Altogether")]
    assert core.numbered_episodes(wrong, ORDERS, 3) is None              # (for a search by name to take over)
    assert core.numbered_episodes(agree, ORDERS, 9) is None              # (no such season)
    assert core.numbered_episodes([read(7, "Pilot Light")], ORDERS, 3) is None


def test_a_shows_extra_its_menus_dont_list_is_other_and_an_episode_isnt():
    found = {"episodes": [{"title": "a", "season": 1, "episode": 1, "name": "Pilot", "read": "1) Pilot", "by": "disc"}],
             "extras": [{"title": "b", "name": "Deleted", "seconds": 60, "kind": "DeletedScene"}],
             "played": ["a", "b", "unread"]}
    titles = [{"id": "a", "role": "episode"}, {"id": "b", "role": "extra"}, {"id": "warning", "role": "extra"},
              {"id": "unlisted", "role": "episode"}, {"id": "logo", "role": "junk"}, {"id": "mine", "role": "extra"},
              {"id": "unread", "role": "extra"}]
    labels = menus.detected_tv_labels(found, titles, keep={"mine"})
    assert labels["warning"]["type"] == "Other" and labels["warning"]["note"] == menus.NOT_IN_MENUS
    assert labels["b"]["type"] == "DeletedScene"
    assert not {"unlisted", "logo", "mine", "unread"} & set(labels)     # ("unread": a button plays it, its words not read)
    assert menus.detected_tv_labels(dict(found, episodes=[]), [{"id": "warning", "role": "extra"}]) == {}    # (the menus unread)
    assert "warning" not in menus.detected_tv_labels(dict(found, played=None), titles)        # (a stakeout saved before)
    assert "warning" not in menus.detected_tv_labels(dict(found, extras=[]), titles)          # (its extras' page not found)


def test_each_title_gets_its_own_buttons_picture_from_the_menus_sheets(tmp_path):
    from PIL import Image
    root = wb_disc(tmp_path / "disc")
    Image.new("RGBA", (40, 30), (200, 200, 200, 255)).save(os.path.join(root, "BDMV", "JAR", "04518", "composite_show_eng_1.png"))
    out = str(tmp_path / "out")
    raw = bdcrawl.from_files(root, "SHOW_S3_D1", FILE_TITLES, read=lambda image: "Pilot Light", out_dir=out)
    pics = menus.tv_pictures(raw["tv"], out)
    assert set(pics) == {"00051.mpls"}                     # (the one button with an image on the sheet, here)
    pic = pics["00051.mpls"]
    assert pic["alone"] and pic["text"] == "Pilot Light" and pic["rect"] == [0, 0, 10, 10]
    assert Image.open(pic["image"]).size == (10, 10)
    assert bdcrawl.from_files(root, "SHOW_S3_D1", FILE_TITLES, read=lambda image: "")["tv"]["readings"][0]["image"] is None


def test_a_scissor_buttons_episode_holds_with_a_picture_too():
    """A button's own picture gives it a rect: not a reason to look for the episode above it instead."""
    plain = dict(page=["bd", 0, 0], image="b.png", switch=False, only_cut=False, commentaries=False)
    tv = {"readings": [dict(plain, title="e", text="1) Pilot", number=1, seconds=2500, bonus=False, kind=None, only_kind=False,
                            rect=[0, 0, 150, 85], place=[1]),
                       dict(plain, title="s", text="Deleted Scenes", seconds=120, bonus=True, kind="DeletedScene", only_kind=True,
                            of_episode=1, rect=[0, 0, 150, 85], place=[100])]}
    picks = {0: {"season": 1, "number": 1, "name": "Pilot", "score": 1.0, "by": "disc"}}
    scene = menus.tv_found(tv, picks)["extras"][0]
    assert (scene["season"], scene["episode"], scene["name"]) == (1, 1, "Pilot Deleted Scenes")


def test_a_title_left_out_of_the_menus_reads_as_other_with_nothing_to_confirm():
    from disc_detective import app
    detected = {"00007.mpls": {"type": "Other", "title": None, "note": app.NOT_IN_MENUS}}
    word, source = app.title_word({"id": "00007.mpls", "role": "promo"}, {}, {}, detected, {})
    assert (word["type"], source) == ("Other", "scan")
    assert app.NOT_IN_MENUS == menus.NOT_IN_MENUS


class Wanting:
    """The app's say on when to ask a disc's menus, without its window."""
    from disc_detective.app import App as _App
    stakeout_in_scan, files_in_scan, menus_wanted, scan_menus_mode, more_films = (
        _App.stakeout_in_scan, _App.files_in_scan, _App.menus_wanted, _App.scan_menus_mode, staticmethod(_App.more_films))

    def __init__(self, mode, on=True):
        self.settings = {"menu_detect": on, "detect_in_scan": mode}


def test_a_blu_rays_menu_files_are_read_as_the_setting_says_a_dvds_menus_are():
    fresh = {"format": "Blu-ray", "path": "E:\\", "kind": "movie", "status": "unidentified", "candidates": []}
    mine = dict(fresh, status="identified (your case files)", personal_hit=True, kind="tv", labels={"a": {}}, match={"season": 1})
    unknown = dict(mine, labels={})
    assert Wanting("unknown").files_in_scan(fresh) and Wanting("unsure").files_in_scan(fresh)
    assert Wanting("unknown").files_in_scan(unknown) and not Wanting("unsure").files_in_scan(unknown)
    assert Wanting("unknown").files_in_scan(mine) and Wanting("unsure").files_in_scan(mine)     # (known: for its buttons)
    assert not Wanting("unknown").menus_wanted(mine)
    assert not Wanting("never").files_in_scan(mine) and not Wanting("unknown", on=False).files_in_scan(mine)
    assert not Wanting("unknown").stakeout_in_scan(dict(mine, format="DVD"), "vlc")             # (a DVD's stakeout: no)
    assert not Wanting("never").files_in_scan(fresh) and not Wanting("unknown", on=False).files_in_scan(fresh)
    assert not Wanting("unknown").files_in_scan(dict(fresh, from_sample=True))
    assert not Wanting("unknown").files_in_scan(dict(fresh, format="DVD"))
    assert Wanting("unknown").stakeout_in_scan(dict(fresh, format="DVD"), "vlc") and not Wanting("unknown").stakeout_in_scan(fresh, "vlc")


def test_the_menu_files_say_which_disc_of_the_set(tmp_path):
    root = wb_disc(tmp_path)
    (tmp_path / "BDMV" / "JAR" / "15631" / "disc_set.prop").write_text("SHOW_S3_D1.discNum=1\nSHOW_S3_D2.discNum=2\n", encoding="latin1")
    assert bdmenus.wb_menu(root, "SHOW_S3_D2")["disc"] == 2
    tv = bdcrawl.from_files(root, "SHOW_S3_D1", FILE_TITLES, read=lambda image: "")["tv"]
    assert tv["disc"] == 1


def test_the_disc_number_from_the_files_corrects_the_answer_but_not_yours():
    from disc_detective.app import App
    got = {"season": 1, "picks": {0: {"by": "disc", "season": 1, "number": 1, "name": "Pilot"}}}
    match = {"name": "Show", "year": 2012, "imdb_id": "tt1", "season": 1, "disc": None}
    assert "S1 D2" in App._tv_settles({"match": match}, [], got, None, 2)["answer"]
    assert App._tv_settles({"match": dict(match, disc=2)}, [], got, None, 2) is None
    assert App._tv_settles({"match": dict(match, disc=1), "personal_hit": True}, [], got, None, 2) is None


def test_a_blu_ray_stakeout_says_so_when_rapidocr_cant_run(monkeypatch):
    from disc_detective.app import App
    a = App.__new__(App)
    a.post = lambda *args: None
    a._menu_files = lambda rep: None
    monkeypatch.setattr(menus, "rapid_missing", lambda: True)
    monkeypatch.setattr(bdcrawl, "crawl", lambda *args: pytest.fail("no crawl without a reader"))
    with pytest.raises(menus.NoReader):
        a._stakeout({"format": "Blu-ray", "path": "E:\\", "content_hash": "AB", "titles": []}, "vlc")
    assert issubclass(menus.NoReader, menus.Lost)             # (never an error for a scan)


def test_rapidocr_really_reads_a_picture():
    """The real thing, models and all, on whichever Python this is (not on 3.15: no runtime for it yet)."""
    pytest.importorskip("onnxruntime")
    pytest.importorskip("rapidocr")
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (720, 240), (20, 30, 60))
    ImageDraw.Draw(image).text((40, 90), "DELETED SCENES", font=ImageFont.load_default(size=56), fill=(240, 240, 240))
    assert not menus.rapid_missing()
    words = " ".join(t for t, _, _ in menus.rapid_lines(image)).upper()
    assert "DELETED" in words and "SCENES" in words


def test_edit_titles_goes_by_type_then_menu_page_then_episode():
    from disc_detective import app
    word = lambda kind, ep=None: {"type": kind, "season": "1" if ep else "", "episode": str(ep) if ep else ""}
    rows = {"ep2": word("Episode", 2), "ep12": word("Episode", 12), "ep1": word("Episode", 1),
            "del5": word("DeletedScene", 5), "del1": word("DeletedScene", 1), "del3": word("DeletedScene", 3),
            "extra": word("Extra"), "linked": word("Extra", 4)}
    # (del3's on an earlier page than the others; ep12's button on an earlier page, which says nothing)
    detected = {"del5": {"place": [7, 1]}, "del1": {"place": [7, 2]}, "del3": {"place": [2, 9]}, "ep12": {"place": [1, 1]},
                "ep1": {"place": [9, 1]}, "ep2": {"place": [9, 2]}}
    order = sorted(rows, key=lambda t: app.found_order(t, rows[t], detected))
    assert order == ["ep1", "ep2", "ep12", "del3", "del1", "del5", "linked", "extra"]
    # (saved labels: no menu place to go by, the numbers still sort them)
    order = sorted(rows, key=lambda t: app.found_order(t, rows[t], {}))
    assert order == ["ep1", "ep2", "ep12", "del1", "del3", "del5", "linked", "extra"]
    # (nothing numbered: as the scan has them, then by title)
    plain = {"Title 10": word("Extra"), "Title 2": word("Extra")}
    assert sorted(plain, key=lambda t: app.found_order(t, plain[t], {})) == ["Title 2", "Title 10"]
    assert app.found_order("b", word("Episode"), {}, 1) < app.found_order("a", word("Episode"), {}, 2)
