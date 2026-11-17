"""The one way menu buttons are named, through every pipeline that names them off a page picture, each called as the
app calls it: a DVD's crawl (menus.button_words off its frame, the page read whole by RapidOCR) and a Blu-ray's HDMV
menus drawn from its files (bdhdmv: menus.name_button with the page's lines, the button's own picture read alone, the
buttons that do something). Pages drawn here, each line's box where OCR puts it; what's read again is read by
RapidOCR off the picture, as the app reads it."""
import pytest

from disc_detective import menus
from test_textblocks import CREAM, GOLD, GREY, Page

PIPELINES = ("dvd", "bluray")


@pytest.fixture(params=PIPELINES)
def named(request, monkeypatch):
    """named(page, rects, is_word=None) -> {button: its name}, through one pipeline; rects [x0, y0, x1, y1] in the
    720x480 frame (the drawn page's own pixels)."""
    menus._groupings.clear()

    def run(page, rects, is_word=None):
        monkeypatch.setattr(menus, "_page_lines", lambda im, scale=1: page.lines if scale == 1 else page.twice)
        monkeypatch.setattr(menus, "_page_corners", lambda im: [])
        rects = [list(r) for r in rects]
        out = {}
        for n, rect in enumerate(rects, 1):
            if request.param == "dvd":
                monkeypatch.setattr(menus, "WORDS", is_word)
                got = menus.button_words(page.image, rect, rects)
            else:
                own = page.image.crop(tuple(rect))
                got = menus.name_button(page.image, rect, rects, (720, 480), lines=page.lines,
                                        alone=lambda own=own, rect=rect: menus.read_alone(own, None, tuple(rect[:2])),
                                        beside=lambda: None, is_word=is_word, corners=[], acting=rects)[0]
            if str(got or ""):
                out[n] = str(got)
        return out
    return run


def page():
    p = Page()
    p.twice = []                # (the page read at twice the size: nothing more, unless a case says)
    return p


def test_each_button_is_named_by_its_item_not_a_heading_or_description(named):
    p = page()
    p.text((60, 30), "SPECIAL FEATURES", size=40, colour=GOLD)
    p.text((60, 110), "Production Diary", size=26)
    p.text((60, 145), "A look behind the scenes.", size=16, colour=GREY, bold=False)
    p.text((60, 210), "Deleted Scenes", size=26)
    p.text((60, 245), "Six scenes cut from the film.", size=16, colour=GREY, bold=False)
    p.text((60, 310), "Theatrical Trailer", size=26)
    p.text((60, 345), "The film's first trailer.", size=16, colour=GREY, bold=False)
    got = named(p, [(40, 20, 560, 190), (40, 190, 560, 290), (40, 290, 560, 390)])
    assert got == {1: "Production Diary", 2: "Deleted Scenes", 3: "Theatrical Trailer"}


def test_a_name_wrapped_onto_two_lines_is_whole(named):
    p = page()
    p.text((60, 60), "7. TOTAL RECOIL", size=22, colour=CREAM)
    p.text((60, 130), "8. THE RETURN OF", size=22, colour=CREAM)
    p.text((100, 162), "ART LINKLETTER!", size=22, colour=CREAM)
    p.text((60, 240), "9. CARDSHARK SERENADE", size=22, colour=CREAM)
    got = named(p, [(40, 50, 560, 110), (40, 120, 560, 200), (40, 230, 560, 290)])
    assert got[2] == "8. THE RETURN OF ART LINKLETTER!"


def test_a_colons_line_over_its_box_stays_in_the_name(named):
    """"Dylan & Cole Sprouse: / Blu-ray Is Suite!": the colon's line over its box, "Sprouse" no dictionary word --
    read again, it stands; its box alone, short of that line, doesn't cut it short."""
    p = page()
    p.text((60, 40), "Deleted Scene:", size=22)
    p.text((60, 66), "Balthazar Recruits Dave", size=22)
    p.text((60, 140), "Dylan & Cole Sprouse:", size=22)
    p.text((60, 166), "Blu-ray Is Suite!", size=22)
    known = {"deleted", "scene", "balthazar", "recruits", "dave", "dylan", "cole", "blu", "ray", "is", "suite"}
    got = named(p, [(40, 60, 560, 100), (40, 160, 560, 200)], is_word=lambda w: w.lower() in known)
    assert got == {1: "Deleted Scene: Balthazar Recruits Dave", 2: "Dylan & Cole Sprouse: Blu-ray Is Suite!"}


def test_text_beside_a_title_on_its_line_is_part_of_it(named):
    p = page()
    for k, (year, film) in enumerate((("Year 1", "Harry Potter and the Stone"), ("Year 2", "Harry Potter and the Chamber"))):
        y = 100 + 60 * k
        b = p.text((60, y), year, size=24, colour=CREAM, line=False)
        c = p.text((b[2] + 14, y), film, size=24, colour=GREY, line=False)
        p.lines.append((year + " " + film, (b[0] - 3.0, min(b[1], c[1]) - 3.0, c[2] + 3.0, max(b[3], c[3]) + 3.0), 0.98))
    got = named(p, [(40, 90, 680, 140), (40, 150, 680, 200)])
    assert got == {1: "Year 1 Harry Potter and the Stone", 2: "Year 2 Harry Potter and the Chamber"}


def test_a_last_box_over_the_pages_foot_is_its_item(named):
    p = page()
    for k in range(3):
        p.text((60, 60 + 70 * k), "Scene %d" % (k + 1), size=24)
    p.text((60, 300), "Visit us at www.example.com for", size=18, colour=GREY)
    p.text((60, 326), "the latest on our discs.", size=18, colour=GREY)
    got = named(p, [(40, 50, 400, 120), (40, 120, 400, 190), (40, 190, 400, 380)])
    assert got[3] == "Scene 3"


def test_a_play_all_on_its_own_line_is_its_name(named):
    p = page()
    p.text((60, 60), "Deleted Scenes", size=24)
    p.text((60, 92), "Play All", size=24)
    p.text((60, 170), "Scene 1", size=24)
    p.text((60, 240), "Scene 2", size=24)
    got = named(p, [(40, 50, 400, 130), (40, 160, 400, 210), (40, 230, 400, 280)])
    assert got[1] == "Play All"


def test_tiny_buttons_are_named_by_the_line_nearest_and_a_hidden_one_has_nothing(named):
    """Lamps by their labels, and two icons by a list stacked close: each its own nearest line; a button with no
    text near it (a hidden one) has none -- never a line further off past another's."""
    p = page()
    for k, name in enumerate(("Scene Selections", "Special Features", "Languages")):
        p.text((90, 100 + 60 * k), name, size=24)
    p.text((100, 330), "THEATRICAL TRAILER", size=22)
    p.text((100, 356), "SNEAK PEEKS", size=22)
    got = named(p, [(50, 100, 70, 125), (50, 160, 70, 185), (50, 220, 70, 245),
                    (330, 328, 380, 352), (240, 354, 290, 380), (640, 40, 680, 70)])
    assert got == {1: "Scene Selections", 2: "Special Features", 3: "Languages", 4: "THEATRICAL TRAILER",
                   5: "SNEAK PEEKS"}


def test_a_word_misread_surely_is_put_right_by_reading_again(named):
    """A word no dictionary has, read however surely ("Dostiny", 0.99): read again, a known word in its place puts
    it right; nothing else of the name changes."""
    p = page()
    p.text((60, 60), "TV Spot: Destiny Cutdown", size=24)
    p.text((60, 140), "Photo Gallery", size=24)
    p.lines[0] = ("TV Spot: Dostiny Cutdown", p.lines[0][1], 0.99)
    known = {"spot", "destiny", "cutdown", "photo", "gallery"}
    got = named(p, [(40, 50, 560, 110), (40, 130, 560, 180)], is_word=lambda w: w.lower() in known)
    assert got[1] == "TV Spot: Destiny Cutdown"


def test_a_line_only_reading_at_twice_the_size_finds_is_in_the_name(named):
    """Small lettering, Spider-Man's trailers: a lamp by each film's name, a logo's stack of sizes ("MEN IN / BLACK /
    II"), its last line missed by the page's reading and found at twice the size, under a line read surely: the
    lamp's text the whole stack. Small print found so on its own (a backdrop's: "by Waif") is no line of anything."""
    p = page()
    p.text((60, 60), "MEN IN", size=18)
    p.text((60, 84), "BLACK", size=24)
    b = p.text((80, 112), "II", size=14, line=False)
    p.twice = [("II", (b[0] - 2.0, b[1] - 2.0, b[2] + 2.0, b[3] + 2.0), 0.85), ("by Waif", (400, 300, 460, 316), 0.88)]
    p.text((60, 200), "STUART LITTLE 2", size=22)
    got = named(p, [(200, 88, 216, 112), (260, 200, 276, 226)])
    assert got == {1: "MEN IN BLACK II", 2: "STUART LITTLE 2"}


def test_no_heading_shares_a_line_with_its_title(named):
    """A lead-in in other lettering on its title's line ("Music Video" in gold, its song in white: one OCR line) is
    the title's start, never a heading or a description of it -- a heading has a line of its own; promoting a
    keyword at a title's start is the namer's (menus.name_extras), later."""
    p = page()
    for k, (lead, title) in enumerate((("Music Video", "Signal Fire"), ("Deleted Scene", "Balthazar Recruits Dave"))):
        y = 100 + 70 * k
        a = p.text((60, y), lead, size=20, colour=GOLD, line=False)
        c = p.text((a[2] + 14, y), title, size=24, colour=(240, 240, 240), line=False)
        p.lines.append((lead + " " + title, (a[0] - 3.0, min(a[1], c[1]) - 3.0, c[2] + 3.0, max(a[3], c[3]) + 3.0), 0.98))
    got = named(p, [(40, 90, 680, 140), (40, 160, 680, 210)])
    assert got == {1: "Music Video Signal Fire", 2: "Deleted Scene Balthazar Recruits Dave"}


def test_lines_under_a_heading_are_no_item_before_it(named):
    p = page()
    for k, t in enumerate(("Meet Jeff", "Movie Villains", "Sword Skills")):
        p.text((60, 60 + 26 * k), t, size=22)
    p.text((60, 140), "ONLINE", size=40, colour=GOLD)
    p.text((60, 194), "Visit us at www.example.com", size=22)
    p.text((60, 222), "for the latest on", size=16, colour=GREY, bold=False)
    p.text((60, 242), "our discs.", size=22)
    got = named(p, [(40, 56, 500, 82), (40, 82, 500, 108), (40, 108, 500, 134)])
    assert got[3] == "Sword Skills"
