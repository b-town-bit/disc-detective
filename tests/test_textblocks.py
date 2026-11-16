"""textblocks: a menu page's text grouped by spacing and alignment, roles by style -- on pages drawn here, each line's
box where OCR would put it (its lettering's, padded a little), so no OCR is needed."""
import os

from PIL import Image, ImageDraw, ImageFont

from disc_detective import menus, textblocks

FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "disc_detective", "fonts")
WHITE, GREY, GOLD, CREAM = (235, 235, 235), (150, 150, 150), (230, 180, 40), (220, 210, 170)


class Page:
    """A menu page drawn line by line: its picture and its lines as OCR reads them, [(text, box, sure)]."""

    def __init__(self, size=(720, 480), back=(20, 30, 60)):
        self.image = Image.new("RGB", size, back)
        self.draw = ImageDraw.Draw(self.image)
        self.lines = []

    def text(self, xy, text, size=24, colour=WHITE, bold=True, line=True):
        font = ImageFont.truetype(os.path.join(FONTS, "Inter-Bold.ttf" if bold else "Inter-Regular.ttf"), size)
        self.draw.text(xy, text, font=font, fill=colour)
        b = self.draw.textbbox(xy, text, font=font)
        if line:
            self.lines.append((text, (b[0] - 3.0, b[1] - 3.0, b[2] + 3.0, b[3] + 3.0), 0.98))
        return b

    def names(self, rects):
        return {n: " ".join(t for t, _, _ in got) for n, got in textblocks.page_names(self.image, self.lines, rects).items()}


def test_a_buttons_name_is_its_item_not_the_heading_or_description_in_its_box():
    """A heading over a list, each item's description under it in smaller grey lettering, the first box reaching up
    over the heading: each button's name is its item alone."""
    p = Page()
    p.text((60, 30), "SPECIAL FEATURES", size=40, colour=GOLD)
    p.text((60, 110), "Production Diary", size=26)       # (not "The Making Of": a name ending in "of" carries on)
    p.text((60, 145), "A look behind the scenes.", size=16, colour=GREY, bold=False)
    p.text((60, 210), "Deleted Scenes", size=26)
    p.text((60, 245), "Six scenes cut from the film.", size=16, colour=GREY, bold=False)
    p.text((60, 310), "Theatrical Trailer", size=26)
    p.text((60, 345), "The film's first trailer.", size=16, colour=GREY, bold=False)
    rects = {1: (40, 20, 560, 190), 2: (40, 190, 560, 290), 3: (40, 290, 560, 390)}
    assert p.names(rects) == {1: "Production Diary", 2: "Deleted Scenes", 3: "Theatrical Trailer"}


def test_a_name_wrapped_onto_two_lines_in_its_box_is_whole():
    """A name taking two lines, a little further apart than a line height but within its own small box (the
    Muppets' "8. THE RETURN OF / ART LINKLETTER!"), its items further apart again: one name."""
    p = Page()
    p.text((60, 60), "7. TOTAL RECOIL", size=22, colour=CREAM)
    p.text((60, 130), "8. THE RETURN OF", size=22, colour=CREAM)
    p.text((100, 162), "ART LINKLETTER!", size=22, colour=CREAM)
    p.text((60, 240), "9. CARDSHARK SERENADE", size=22, colour=CREAM)
    rects = {1: (40, 50, 560, 110), 2: (40, 120, 560, 200), 3: (40, 230, 560, 290)}
    assert p.names(rects)[2] == "8. THE RETURN OF ART LINKLETTER!"


def test_a_line_ending_in_a_colon_keeps_the_next_in_its_name():
    """A colon ties its line to the one item after it, whatever its lettering: both are the name (the namer may
    promote "Music Video" to a heading later)."""
    p = Page()
    p.text((60, 60), "Bonus Song", size=24)
    p.text((60, 150), "Music Video:", size=18, colour=GREY)
    p.text((60, 176), "Signal Fire", size=24)
    p.text((60, 270), "Photo Gallery", size=24)
    rects = {1: (40, 50, 560, 110), 2: (40, 140, 560, 220), 3: (40, 260, 560, 320)}
    assert p.names(rects)[2] == "Music Video: Signal Fire"


def test_text_beside_a_title_on_its_line_is_part_of_it():
    """A line changing colour part-way (cream "Year 1", grey film name: one OCR line) is one title -- descriptions
    start on their own line -- whatever the words after the change."""
    p = Page()
    for k, (year, film) in enumerate((("Year 1", "Harry Potter and the Stone"),
                                      ("Year 2", "Harry Potter and the Chamber of Secrets"))):
        y = 100 + 60 * k
        b = p.text((60, y), year, size=24, colour=CREAM, line=False)
        c = p.text((b[2] + 14, y), film, size=24, colour=GREY, line=False)
        p.lines.append((year + " " + film, (b[0] - 3.0, min(b[1], c[1]) - 3.0, c[2] + 3.0, max(b[3], c[3]) + 3.0), 0.98))
    rects = {1: (40, 90, 680, 140), 2: (40, 150, 680, 200)}
    assert p.names(rects) == {1: "Year 1 Harry Potter and the Stone", 2: "Year 2 Harry Potter and the Chamber of Secrets"}


def test_a_columns_last_box_reaching_over_the_pages_foot_is_trimmed():
    """The last box of a column reaching down over text below its own (a footer: "Visit us at ..."): trimmed to its
    own block, so its name is its item, not the bigger text under it."""
    p = Page()
    p.text((60, 60), "Scene 1", size=24)
    p.text((60, 130), "Scene 2", size=24)
    p.text((60, 200), "Scene 3", size=24)
    p.text((60, 300), "Visit us at www.example.com for", size=18, colour=GREY)
    p.text((60, 326), "the latest on our discs.", size=18, colour=GREY)
    rects = {1: (40, 50, 400, 120), 2: (40, 120, 400, 190), 3: (40, 190, 400, 380)}
    assert p.names(rects)[3] == "Scene 3"


def test_a_play_all_on_its_own_line_is_a_name_by_itself():
    """A play-all on a line of its own is its button's whole name, the line over it in its box its heading."""
    p = Page()
    p.text((60, 60), "Deleted Scenes", size=24)
    p.text((60, 92), "Play All", size=24)
    p.text((60, 170), "Scene 1", size=24)
    p.text((60, 240), "Scene 2", size=24)
    rects = {1: (40, 50, 400, 130), 2: (40, 160, 400, 210), 3: (40, 230, 400, 280)}
    assert p.names(rects)[1] == "Play All"


def test_lines_split_where_a_gap_is_two_letters_wide_and_join_again_in_one_box():
    """split_line: a line OCR read across two buttons' names splits at a gap two letters wide; rejoin puts the parts
    back when one box holds both ("PLAY | ALL" in letter-spaced capitals), not when two boxes do."""
    ik = {"left": 0, "right": 299, "top": 0, "bottom": 20, "h": 20, "letter": 10.0,
          "gaps": [(40, 5), (100, 30), (160, 5)], "core": 16}
    parts = textblocks.split_line("Play Movie Scenes", ik)
    assert [t for t, _ in parts] == ["Play Movie", "Scenes"]
    one = {1: (0, 0, 300, 20)}
    assert [t for t, _ in textblocks.rejoin(parts, one)] == ["Play Movie Scenes"]
    two = {1: (0, 0, 110, 20), 2: (120, 0, 300, 20)}
    assert [t for t, _ in textblocks.rejoin(parts, two)] == ["Play Movie", "Scenes"]


def test_the_signs_a_text_carries_on_or_reads_as_a_description():
    assert textblocks.carries_on("Professor Lupin and", "Sirius Black")
    assert textblocks.carries_on("Interview with Michael Gambon", "and Robbie Coltrane.")
    assert not textblocks.carries_on("The Slytherins", "Interview with actors")
    assert textblocks.sentence_like(["A look behind the scenes."])
    assert not textblocks.sentence_like(["Harry Potter:", "The Magic of Editing"])
    assert textblocks.play_all_alone("PLAY aul") and not textblocks.play_all_alone("Play All Episodes")


def test_lettering_one_colour_by_either_reading():
    """Two lines' lettering is one colour when their own boxes' readings agree, or their readings against what's
    behind them do (outlined lettering: fill or outline by turns in a box hugging its letters)."""
    style = lambda colour, ring: {"colour": colour, "ring": ring}
    assert textblocks.colours_match(style((5, 6, 5), (201, 203, 169)), style((200, 202, 168), (202, 204, 170)))
    assert textblocks.colours_match(style((150, 110, 70), (200, 170, 118)), style((140, 100, 52), (78, 47, 13)))
    assert not textblocks.colours_match(style((230, 180, 40), (230, 180, 40)), style((150, 150, 150), (150, 150, 150)))


def test_name_button_takes_the_grouped_name_before_its_boxs_other_lines():
    """name_button with the page's text grouping: a box holding a heading, its item and the item's description is
    named by the item; with the grouping off, as before, the heading in its box too."""
    p = Page()
    p.text((60, 30), "BEHIND THE STORY", size=36, colour=GOLD)
    p.text((60, 100), "Harry vs. the Horntail", size=24)
    p.text((60, 134), "A detailed look at the dragon.", size=16, colour=GREY, bold=False)
    p.text((60, 200), "In Too Deep", size=24)
    p.text((60, 234), "A look at the second task.", size=16, colour=GREY, bold=False)
    rects = [[40, 20, 600, 180], [40, 180, 600, 280]]
    got, _, _ = menus.name_button(p.image, rects[0], rects, (720, 480), lines=p.lines, alone=lambda: [])
    assert got == "Harry vs. the Horntail"
    old, menus.TEXT_BLOCKS = menus.TEXT_BLOCKS, False
    try:
        got, _, _ = menus.name_button(p.image, rects[0], rects, (720, 480), lines=p.lines, alone=lambda: [])
    finally:
        menus.TEXT_BLOCKS = old
    assert got.startswith("BEHIND THE STORY")
