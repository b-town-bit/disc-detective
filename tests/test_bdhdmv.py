"""A Blu-ray's HDMV menus through the hdmv-menus library (bdhdmv), on a stand-in for the library: a disc of
a pop-up menu with a bar and a page of extras, as Order of the Phoenix's."""
import sys
import types

import pytest

from disc_detective import bdhdmv, menus

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


class Picture:
    def __init__(self, width, height, x=0, y=0, colour=(200, 190, 150, 255)):
        self.width, self.height, self.x, self.y, self.colour = width, height, x, y, colour

    def to_pil(self):
        return Image.new("RGBA", (self.width, self.height), self.colour)


def button(bid, x, y):
    return {"id": bid, "x": x, "y": y, "commands": []}


BAR = {"id": 0, "groups": [{"default": 4, "buttons": [button(4, 0, 950), button(5, 600, 950), button(6, 1100, 950)]}]}
EXTRAS = {"id": 2, "groups": [{"default": 25, "buttons": [button(25, 735, 300)]}, {"default": 4, "buttons": [button(40, 0, 340)]},
                              {"default": 1, "buttons": [button(1, 700, 940)]}]}
SCENES = {"id": 3, "groups": [{"default": 7, "buttons": [button(7, 300, 700), button(8, 500, 700)]}]}
NAMES = {(0, 6): "SPECIAL FEATURES", (0, 4): "LANGUAGES", (0, 5): "SCENE SELECTIONS", (2, 25): "The Explosion of Decrees",
         (2, 40): "Play All", (2, 1): "", (3, 7): "1", (3, 8): "2"}
LINKS = [{"page": 0, "button": 6, "outcome": {"kind": "page", "page": 2, "button": 38}},
         {"page": 0, "button": 4, "outcome": {"kind": "page", "page": 1, "button": 76}},
         {"page": 0, "button": 5, "outcome": {"kind": "page", "page": 3, "button": 7}},
         {"page": 2, "button": 25, "outcome": {"kind": "play", "playlist": 83, "mark": None, "title": 25}},
         {"page": 2, "button": 40, "outcome": {"kind": "play", "playlist": 89, "mark": None, "title": 34}},
         {"page": 2, "button": 1, "outcome": {"kind": "ended"}},
         {"page": 3, "button": 7, "outcome": {"kind": "play", "playlist": 60, "mark": 0, "title": 1}},
         {"page": 3, "button": 8, "outcome": {"kind": "play", "playlist": 60, "mark": 1, "title": 1}}]
# each button's picture a width of its own: its name, known by it
WIDTHS = {k: 200 + 10 * i for i, k in enumerate(NAMES)}
TITLES = [{"id": "00060.mpls", "duration": 8294.0}, {"id": "00083.mpls", "duration": 117.4}, {"id": "00089.mpls", "duration": 3790.6}]


class Menu:
    composition = {"width": 1920, "height": 1080}
    pages = [BAR, EXTRAS, SCENES]

    def buttons(self, page):
        return [b for g in page["groups"] for b in g["buttons"]]

    def draw(self, page, selected=None, background=(0, 0, 0, 0)):
        return Picture(1920, 1080)

    def button_image(self, page, b, state="normal"):
        key = (page["id"], b["id"])
        return Picture(WIDTHS[key], 40, b["x"], b["y"]) if NAMES.get(key) else None


def read_name(img):
    """The stand-in's OCR: a button's name, by its picture's width."""
    return next(NAMES[k] for k, w in WIDTHS.items() if w == img.width)


def stream(clip, lang="eng"):
    return {"clip": clip, "pid": 0x1400, "lang": lang, "playlists": ["00060"]}


class Disc:
    """The library's stand-in: its crawl plan (Disc.crawl) of the menus in `shown`, each title's buttons
    ranked as the library does (one starting its title first, then as the menus and buttons come)."""
    read, plans = [], []
    shown = []                          # [(clip, its menu, its links, the way there)]

    def __init__(self, root, aacs=None):
        self.root, self.aacs = root, aacs

    def crawl(self, scope="titles"):
        Disc.plans.append(scope)
        out, ranked = [], {}
        for mi, (clip, menu, links, path) in enumerate(Disc.shown):
            out.append({"stream": stream(clip), "path": path, "reached": True, "links": links})
            for bi, x in enumerate(links):
                o = x["outcome"]
                if o["kind"] == "play":
                    place = {"clip": clip, "pid": 0x1400, "page": x["page"], "button": x["button"], "item": None, "mark": o["mark"]}
                    ranked.setdefault(o["playlist"], []).append(((bool(o["mark"]), mi, bi), place))
        titles = [{"playlist": t, "buttons": [p for _, p in sorted(v, key=lambda kv: kv[0])]} for t, v in sorted(ranked.items())]
        return {"scope": scope, "menus": out, "titles": titles}

    def menu(self, clip, pid=0x1400):
        Disc.read.append(clip)
        return next(m for c, m, _, _ in Disc.shown if c == clip)

    traced = []                         # (what the first play does, event by event: Disc.trace)

    def trace(self):
        return list(Disc.traced)


@pytest.fixture
def library(monkeypatch):
    Disc.read, Disc.plans, Disc.traced = [], [], []
    Disc.shown = [("00072", Menu(), LINKS, ["first play", "plays 00060"])]
    monkeypatch.setitem(sys.modules, "hdmv_menus", types.SimpleNamespace(Disc=Disc))
    monkeypatch.setattr(menus, "page_headings", lambda image, rects, size, names=(), lines=None: (None, False))
    return Disc


def counting(names):
    """A stand-in reader that counts the buttons it reads."""
    def read(img):
        names.append(read_name(img))
        return names[-1]
    return read


def test_a_discs_hdmv_menus_named_by_their_buttons_and_what_they_play(library, tmp_path):
    seen = []
    got = bdhdmv.read("E:/", TITLES, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=counting(seen))
    assert library.plans == ["titles"]
    # (a quick crawl: one button for each title, the film's first scene for want of a Play Movie; not the bar)
    assert sorted(seen) == ["1", "Play All", "The Explosion of Decrees"]
    found = got["found"]
    assert found["feature"] == "00060.mpls"                     # (the film the scene buttons point into)
    assert [(e["title"], e["name"]) for e in found["extras"]] == [("00083.mpls", "The Explosion of Decrees")]
    assert found["play_alls"] == ["00089.mpls"] and not found.get("features")
    extras = next(p for p in got["pages"] if p["page"] == 2)
    assert extras["route"] == [{"page": 0, "program": 1, "button": 6}]    # (Special Features leads there)
    assert [b["n"] for b in extras["buttons"]] == [25, 40]                # (decoration left out)
    played = next(b for b in extras["buttons"] if b["n"] == 25)
    assert played["plays"] == {"title": "00083.mpls", "chapter": 1, "seconds": 117.4, "engine": True}
    assert played["rect"] == [276, 133, 276 + round(WIDTHS[(2, 25)] * 0.375), 151]   # (on the 720x480 page)
    scenes = next(p for p in got["pages"] if p["page"] == 3)
    assert [(b["plays"]["chapter"], b["text"]) for b in scenes["buttons"]] == [(1, "1"), (2, "")]   # (kept, not read)
    assert 0 not in [p["page"] for p in got["pages"]]          # (the bar: nothing read on it, no scenes)
    assert (tmp_path / "crawl.json").exists() and (tmp_path / extras["image"]).exists()


def test_a_full_crawl_reads_every_title_button_and_the_way_to_each_page(library, tmp_path):
    seen = []
    got = bdhdmv.read("E:/", TITLES, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=counting(seen),
                      scope="language")
    assert library.plans == ["language"]
    # (2 on the bar leading to a page, 2 extras, the first scene: not Languages, its page not one of the
    # menu's, nor a film's scene further in, its words its scene's)
    assert sorted(seen) == ["1", "Play All", "SCENE SELECTIONS", "SPECIAL FEATURES", "The Explosion of Decrees"]
    assert [e["title"] for e in got["found"]["extras"]] == ["00083.mpls"]


def test_a_title_whose_button_reads_nothing_is_read_off_its_next(library, tmp_path):
    """The same extras page shown again (Order of the Phoenix's 00035): its button for a title the first's
    couldn't be read off is."""
    class Blank(Menu):
        def button_image(self, page, b, state="normal"):
            return Picture(999, 40, b["x"], b["y"]) if (page["id"], b["id"]) == (2, 25) else super().button_image(page, b, state)
    library.shown = [("00072", Blank(), LINKS, ["a"]), ("00035", Menu(), LINKS, ["a", "b"])]
    NAMES[(9, 9)], WIDTHS[(9, 9)] = "", 999
    seen = []
    try:
        got = bdhdmv.read("E:/", TITLES, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=counting(seen))
    finally:
        del NAMES[(9, 9)], WIDTHS[(9, 9)]
    # (a Play All's words name nothing: its next button read too, as Chuck's first episode needed)
    assert seen.count("The Explosion of Decrees") == 1 and seen.count("") == 1 and seen.count("Play All") == 2
    assert [(e["title"], e["name"]) for e in got["found"]["extras"]] == [("00083.mpls", "The Explosion of Decrees")]


def test_a_menu_over_its_video_left_out(library, tmp_path):
    """Order of the Phoenix's editing game: its art in the video, the disc's files only its highlights:
    no words, and none for what it plays anywhere else."""
    class Game(Menu):
        pages = [{"id": 0, "groups": [{"default": 0, "buttons": [button(0, 500, 600)]}]}]

        def button_image(self, page, b, state="normal"):
            return Picture(999, 40, b["x"], b["y"])           # (a box of colour: no words)
    game = [{"page": 0, "button": 0, "outcome": {"kind": "play", "playlist": 119, "mark": None, "title": 9}}]
    library.shown.append(("00135", Game(), game, ["y"]))
    NAMES[(0, 0)], WIDTHS[(0, 0)] = "", 999
    try:
        got = bdhdmv.read("E:/", TITLES + [{"id": "00119.mpls", "duration": 10.0}], str(tmp_path), log=lambda m: None,
                          aacs="libaacs.dll", read_words=read_name)
    finally:
        del NAMES[(0, 0)], WIDTHS[(0, 0)]
    assert sorted({p["menu"] for p in got["pages"]}) == ["00072"]
    assert [e["title"] for e in got["found"]["extras"]] == ["00083.mpls"]


def test_reading_says_each_stage_and_counts_down_the_buttons(library, tmp_path):
    told = []
    bdhdmv.read("E:/", TITLES, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=read_name,
                progress=lambda *a: told.append(a), scope="language")
    # (the stages a DVD's crawl tells too, menus.CRAWL_STAGES: the disc opened, the crawl begun, each page)
    assert told[:2] == [("setup",), ("start",)]
    reads = [t for t in told if t[0] == "page"]
    # (each page questioned, its buttons read: the bar, the extras, the scenes)
    assert [(k, n) for _, k, n in reads] == [(1, 3), (2, 3), (3, 3)]
    assert {t[0] for t in told} <= set(menus.CRAWL_STAGES)


def test_time_left_in_words():
    from disc_detective.app import time_left
    assert (time_left(None), time_left(20), time_left(170)) == ("", "under a minute left", "about 3 min left")


def test_a_buttons_name_is_what_its_highlight_recolours_else_its_tallest_lettering():
    heading = ("ADDITIONAL FOOTAGE", (0, 0, 300, 40), 0.9)
    name = ("Additional Scenes", (10, 50, 250, 75), 0.9)
    words = ("Take a thrilling look back through the previous movies", (10, 80, 290, 95), 0.9)
    lit = Image.new("L", (300, 100), 0)
    lit.paste(255, (10, 50, 250, 75))                         # (only the name changes when selected)
    assert bdhdmv.name_lines([heading, name, words], lit) == ["Additional Scenes"]
    assert bdhdmv.name_lines([heading, name, words]) == ["ADDITIONAL FOOTAGE"]   # (no highlight: its tallest)
    assert bdhdmv.name_lines([name, words]) == ["Additional Scenes"]


def test_no_library_no_reading(monkeypatch):
    monkeypatch.setitem(sys.modules, "hdmv_menus", None)
    assert not bdhdmv.available()
    assert bdhdmv.read("E:/", TITLES, ".", log=lambda m: None) is None


def test_a_film_less_than_half_the_features_length_or_a_play_all_is_no_film_of_its_own():
    """Order of the Phoenix: its Focus Points Play All (an hour) and a 44-minute documentary, each the one
    button on its page playing it, beside its 138-minute film: no double feature."""
    page = lambda n, buttons: {"domain": "bd", "page": n, "program": 1, "route": [], "heading": "", "buttons": buttons}
    play = lambda n, t, s, text: {"n": n, "rect": [0, 0, 1, 1], "text": text, "plays": {"title": t, "chapter": 1, "seconds": s}}
    out = {"pages": [page(0, [play(1, "00060.mpls", 8294, "Play Movie")] + [play(9 + i, "00060.mpls", 8294, "%d" % i) for i in range(3)]),
                     page(1, [play(2, "00030.mpls", 2635, "The Hidden Secrets of Harry Potter")]),
                     page(2, [play(3, "00089.mpls", 3790, "Play All")])]}
    found = menus.menu_titles(out)
    assert found["feature"] == "00060.mpls" and not found.get("features")
    assert [e["title"] for e in found["extras"]] == ["00030.mpls"] and found["play_alls"] == ["00089.mpls"]


def test_what_plays_before_the_first_menu_is_its_opening():
    """The first play's warnings and previews, before the playlist carrying the menu; nothing after a press."""
    title_for = lambda n: "%05d.mpls" % n if n in (35, 38, 39, 60) else None
    plan = [{"reached": True, "path": ["first play", "plays 00035", "plays 00038", "plays 00039", "plays 00060"]},
            {"reached": True, "path": ["first play", "plays 00035", "00072 page 2 button 35", "plays 00032"]}]
    assert bdhdmv._opening(plan, title_for) == ["00035.mpls", "00038.mpls", "00039.mpls"]
    assert bdhdmv._opening(plan[1:], title_for) == ["00035.mpls"]
    assert bdhdmv._opening([], title_for) == []


def test_a_buttons_name_read_off_its_page_first():
    """Its own lines of the page read whole (the middle of each in its box), sure enough: no reading of
    the button alone. Its page's other lines, a heading over it, aren't its."""
    pic = Picture(300, 60, x=100, y=200)
    page = [("Harry and Cho Under the Mistletoe", (110, 210, 380, 240), 0.97), ("Behind the Story", (100, 100, 300, 130), 0.99)]
    assert bdhdmv.button_words(pic, page=page) == ("Harry and Cho Under the Mistletoe", 0.97, [])


def test_a_buttons_name_lines_on_its_page_are_what_its_highlight_changes():
    """Order of the Phoenix: "Additional Footage" over "Additional Scenes", both in its button's box: only
    the name, which its highlight recolours, is the button's; the heading is the page's. No highlight to go
    by: its tallest lettering."""
    class Lit(Picture):
        def to_pil(self):
            img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            img.paste((255, 255, 255, 255), (0, 50, 200, 80))         # (the name's line, lit)
            return img
    pic, selected = Picture(300, 100, x=100, y=200, colour=(0, 0, 0, 0)), Lit(300, 100, x=100, y=200)
    lines = [("ADDITIONAL FOOTAGE", (100, 200, 400, 245), 0.9), ("Additional Scenes", (100, 252, 300, 278), 0.97),
             ("Spend a day", (600, 600, 700, 620), 0.9)]
    assert bdhdmv.name_boxes(pic, selected, lines) == [(100, 252, 300, 278)]
    assert bdhdmv.name_boxes(pic, None, lines) == [(100, 200, 400, 245)]


def test_a_play_all_the_scan_knows_isnt_read_and_stays_one(library, tmp_path):
    seen = []
    titles = [dict(t, role="play-all") if t["id"] == "00089.mpls" else t for t in TITLES]
    got = bdhdmv.read("E:/", titles, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=counting(seen))
    assert "Play All" not in seen
    assert got["found"]["play_alls"] == ["00089.mpls"] and [e["title"] for e in got["found"]["extras"]] == ["00083.mpls"]


def test_a_marker_button_named_by_the_words_beside_it():
    """Chuck's episode pop-up: each button only a ">>" marker, shown when it's selected (no picture
    otherwise); its episode's name printed beside it. The page's lines read whole: each marker given the
    name the page's layout puts by it, the DVD's way (Spider-Man's lamps); the heading no one's."""
    class Markers(Menu):
        composition = {"width": 1920, "height": 1080}

        def button_image(self, page, b, state="normal"):
            return Picture(48, 36, b["x"], b["y"]) if state == "selected" else None
    page = {"id": 1, "groups": [{"default": 1, "buttons": [button(i, 200, 300 + 90 * i) for i in (1, 2, 3)]}]}
    lines = [("EPISODES", (200, 120, 600, 190), 0.99)] + \
            [("Chuck Versus the %s" % n, (270, 300 + 90 * i, 900, 336 + 90 * i), 0.97)
             for i, n in ((1, "Intersect"), (2, "Helicopter"), (3, "Tango"))]
    got = bdhdmv.beside_names(Markers(), page, lines)
    assert [got[i][0] for i in (1, 2, 3)] == ["Chuck Versus the Intersect", "Chuck Versus the Helicopter", "Chuck Versus the Tango"]
    assert bdhdmv._box(Markers(), page, button(1, 200, 390)) == [75, 173, 93, 189]


def test_a_marker_named_by_the_line_level_with_its_highlight():
    """Chuck S3 D1: the first episode's marker box as tall as its row and the page's heading over it; its
    name the line level with its ">>", not "EPISODES". The name's own button, which does nothing, owns no name."""
    class Lit(Picture):
        def to_pil(self):
            img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            img.paste((255, 255, 255, 255), (60, 95, 120, 125))       # (">>", low in its box)
            return img

    class Row(Menu):
        composition = {"width": 1920, "height": 1080}

        def button_image(self, page, b, state="normal"):
            if b["id"] == 31:                                         # (the name's own, the heading in it too)
                return Picture(837, 131, 147, 554)
            return Lit(147, 131, 0, 554) if state == "selected" else Picture(147, 131, 0, 554, colour=(0, 0, 0, 0))
    page = {"id": 4, "groups": [{"default": 11, "buttons": [button(11, 0, 554), button(31, 147, 554)]}]}
    lines = [("EPISODES", (150, 564, 410, 617), 0.99), ("1) Chuck Versus the Pink Slip", (144, 639, 808, 679), 0.96)]
    assert bdhdmv.beside_names(Row(), page, lines, ids={11})[11][0] == "1) Chuck Versus the Pink Slip"


def test_a_page_is_named_the_dvds_way_off_its_drawing(library, tmp_path, monkeypatch):
    """With RapidOCR (no stand-in reader), a button's name is menus.name_button's, as a DVD's is: the page
    drawn whole and read once, the lines whose middle is in the button's box; and where it was read is
    what its picture outlines (on the 720x480 page)."""
    page_lines = [("The Explosion of Decrees", (745, 305, 925, 335), 0.99), ("Play All", (5, 345, 150, 375), 0.99),
                  ("SPECIAL FEATURES", (1105, 955, 1300, 985), 0.99)]
    monkeypatch.setattr(menus, "rapid_lines", lambda img, least=None, corners=None: page_lines if img.size == (1920, 1080) else [])
    monkeypatch.setattr(menus, "plate_words", lambda *a, **k: {})
    monkeypatch.setattr(menus, "button_text", lambda *a, **k: "")
    got = bdhdmv.read("E:/", TITLES, str(tmp_path), log=lambda m: None, aacs="libaacs.dll")
    button = next(b for p in got["pages"] for b in p["buttons"] if b["n"] == 25)
    assert button["text"] == "The Explosion of Decrees"
    assert button["words"] == [round(745 * 720 / 1920), round(305 * 480 / 1080), round(925 * 720 / 1920), round(335 * 480 / 1080)]


def test_a_shows_play_all_order_is_what_its_buttons_play_in_a_row(library, tmp_path):
    """A show's disc: what each button's play goes on to (the library's "then") gives its episodes' order
    by its Play All, as a DVD's program does (core.play_titles_order): Chuck's PLAY ALL plays its first
    episode's own playlist and goes on to the next."""
    titles = [{"id": "00060.mpls", "duration": 8294.0}, {"id": "00083.mpls", "duration": 2600.0, "role": "episode"},
              {"id": "00089.mpls", "duration": 2610.0, "role": "episode"}]
    links = [dict(x) for x in LINKS]
    links[4] = dict(links[4], then=[83])                 # (the Play All: 00089, then 00083)
    library.shown = [("00072", Menu(), links, ["first play", "plays 00060"])]
    got = bdhdmv.read("E:/", titles, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=read_name, tv=True)
    assert got["tv"]["play_order"]["order"] == ["00089.mpls", "00083.mpls"]



def test_a_first_play_gone_on_to_java_is_a_bd_j_disc(library, tmp_path):
    """No HDMV menu reached, the first play going on to a Java title (Enchanted's: two intros, then title
    1, its JAR 00000; an HDMV menu stream of its own elsewhere): its menus are BD-J, menus.Unsupported,
    not a crawl's to try. Reaching nothing, nor Java: nothing (None), as before."""
    library.shown = []
    library.traced = [{"kind": "play", "playlist": 100}, {"kind": "play", "playlist": 28},
                      {"kind": "bdj", "title": 1, "jar": "00000"}]
    logged = []
    with pytest.raises(menus.Unsupported):
        bdhdmv.read("E:/", TITLES, str(tmp_path), log=logged.append, aacs="libaacs.dll", read_words=read_name)
    assert any("goes on to Java (title 1, JAR 00000)" in m for m in logged)
    library.traced = [{"kind": "play", "playlist": 100}, {"kind": "ended"}]
    assert bdhdmv.read("E:/", TITLES, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=read_name) is None



def test_a_button_read_is_pictured_selected(library, tmp_path):
    """Each button read that plays a title gets a picture of its own: the page drawn with it selected, as a
    player shows it, a button hidden as the page opens among them (a group shows one at a time, its default
    first: Prisoner of Azkaban's RESUME MOVIE, 69, under the default 77); its title's picture is that one."""
    group = {"id": 2, "groups": [{"default": 77, "buttons": [button(77, 700, 940), button(69, 700, 940)]},
                                 {"default": 25, "buttons": [button(25, 735, 300)]}]}
    drawn = []

    class Grouped(Menu):
        pages = [group]

        def draw(self, page, selected=None, background=(0, 0, 0, 0)):
            drawn.append(selected)
            return Picture(1920, 1080)

        def button_image(self, page, b, state="normal"):
            if b["id"] in (69, 77):
                return Picture(400, 60, b["x"], b["y"])
            return Picture(WIDTHS[(2, 25)], 40, b["x"], b["y"])
    links = [{"page": 2, "button": 69, "outcome": {"kind": "play", "playlist": 60, "mark": None, "title": 1}},
             {"page": 2, "button": 77, "outcome": {"kind": "play", "playlist": 60, "mark": None, "title": 1}},
             {"page": 2, "button": 25, "outcome": {"kind": "play", "playlist": 83, "mark": None, "title": 25}}]
    library.shown = [("00095", Grouped(), links, ["first play", "plays 00060"])]
    reader = lambda img: "RESUME MOVIE" if img.width == 400 else "The Explosion of Decrees"
    got = bdhdmv.read("E:/", TITLES, str(tmp_path), log=lambda m: None, aacs="libaacs.dll", read_words=reader)
    b69 = next(b for p in got["pages"] for b in p["buttons"] if b["n"] == 69)
    assert b69["picture"] == "hdmv_00095_p2_b69.png" and (tmp_path / b69["picture"]).exists() and 69 in drawn
    b25 = next(b for p in got["pages"] for b in p["buttons"] if b["n"] == 25)
    assert b25["picture"] == "hdmv_00095_p2_b25.png" and 25 in drawn
    assert not any("picture" in b for p in got["pages"] for b in p["buttons"] if b["n"] == 77)    # (not read)
    pic = got["found"]["pictures"]["00060.mpls"]
    assert pic["image"].endswith("hdmv_00095_p2_b69.png") and pic["text"] == "RESUME MOVIE"
