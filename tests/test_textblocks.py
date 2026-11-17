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


def test_a_grouped_names_line_over_its_box_isnt_lost_to_its_box_read_alone():
    """A name whose first line is over its box ("Dylan & Cole Sprouse: / Blu-ray Is Suite!"), with a word the
    dictionary hasn't ("Sprouse"): read again, it's its block that's read, both lines -- not its own picture (alone),
    which holds only the line in its box."""
    p = Page()
    p.text((60, 40), "Deleted Scene:", size=22)
    p.text((60, 66), "Balthazar Recruits Dave", size=22)
    p.text((60, 140), "Dylan & Cole Sprouse:", size=22)
    p.text((60, 166), "Blu-ray Is Suite!", size=22)
    rects = [[40, 60, 560, 100], [40, 160, 560, 200]]
    alone = lambda: [(menus.Words("Blu-ray Is Suite!", (60, 166, 250, 190)), 0.99)]
    known = {"deleted", "scene", "balthazar", "recruits", "dave", "dylan", "cole", "blu", "ray", "is", "suite"}
    got, _, _ = menus.name_button(p.image, rects[1], rects, (720, 480), lines=p.lines, alone=alone,
                                  is_word=lambda w: w.lower() in known)
    assert got == "Dylan & Cole Sprouse: Blu-ray Is Suite!"


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
    assert got.description == "A detailed look at the dragon."      # (kept beside it: its menu's description)
    old, menus.TEXT_BLOCKS = menus.TEXT_BLOCKS, False
    try:
        got, _, _ = menus.name_button(p.image, rects[0], rects, (720, 480), lines=p.lines, alone=lambda: [])
    finally:
        menus.TEXT_BLOCKS = old
    assert got.startswith("BEHIND THE STORY")


def test_a_tiny_buttons_name_is_its_nearest_text_block():
    """A button with no text in its box (a lamp by its label: Spider-Man's) is named by the text block beside it,
    paired by the page's usual offset -- each lamp its own label, not its neighbour's -- and read as any other."""
    p = Page()
    for k, name in enumerate(("Scene Selections", "Special Features", "Languages")):
        p.text((90, 100 + 60 * k), name, size=24)
    rects = {1: (50, 100, 70, 125), 2: (50, 160, 70, 185), 3: (50, 220, 70, 245)}
    assert p.names(rects) == {1: "Scene Selections", 2: "Special Features", 3: "Languages"}


def test_a_reading_is_put_right_word_by_word_never_cut_short():
    """correct_words: a misread word swapped for a second reading's word in its place ("Mmirror" for "Mirror"); a
    second reading of less (a box short of the name's first line) cuts nothing; a word missing from a line read
    unsure put in ("STUART LITTLE" for "STUART LITTLE 2"), not from one read surely."""
    known = {"the", "mirror", "explosion", "dylan", "cole", "blu", "ray", "is", "suite", "stuart", "little"}
    is_word = lambda w: w.lower() in known
    assert menus.correct_words([("The Mmirror Explosion", None, 0.99)], [("The Mirror Explosion", 0.97)],
                               is_word) == "The Mirror Explosion"
    name = [("Dylan & Cole Sprouse:", None, 0.98), ("Blu-ray Is Suite!", None, 1.0)]
    assert menus.correct_words(name, [("Blu-ray Is Suite!", 0.99)], is_word) == "Dylan & Cole Sprouse: Blu-ray Is Suite!"
    assert menus.correct_words([("STUART LITTLE", None, 0.8)], [("STUART LITTLE 2", 0.95)], is_word) == "STUART LITTLE 2"
    assert menus.correct_words([("STUART LITTLE", None, 0.97)], [("STUART LITTLE 2", 0.95)], is_word) == "STUART LITTLE"


def test_a_name_no_dictionary_has_is_read_again_and_stands_if_read_alike():
    """A line with a word no dictionary has is read again however sure (a misread can be sure: "Dostiny", 0.99): a
    known word read in its place puts it right; read the same (a person's name, "Sprouse"), it stands."""
    p = Page()
    p.text((60, 60), "Meet Sprouse", size=24)
    p.text((60, 140), "Photo Gallery", size=24)
    rects = [[40, 50, 560, 110], [40, 130, 560, 180]]
    known = lambda w: w.lower() in {"meet", "photo", "gallery", "destiny"}
    asked = []
    alone = lambda: asked.append(1) or [(menus.Words("Meet Sprouse"), 0.99)]
    got, _, _ = menus.name_button(p.image, rects[0], rects, (720, 480), lines=p.lines, alone=alone, is_word=known)
    assert got == "Meet Sprouse" and asked
    p.lines[0] = ("Meet Dostiny", p.lines[0][1], 0.99)
    menus._groupings.clear()
    alone = lambda: [(menus.Words("Meet Destiny"), 0.97)]
    got, _, _ = menus.name_button(p.image, rects[0], rects, (720, 480), lines=p.lines, alone=alone, is_word=known)
    assert got == "Meet Destiny"


def test_empty_buttons_by_a_list_each_take_their_own_line_and_a_hidden_one_none():
    """Two icons by a list stacked close (one block: 27F0BF0E's "THEATRICAL TRAILER / SNEAK PEEKS"), each its own
    line; a button with no text near it -- a hidden one -- has none, never a line further off past another's."""
    p = Page()
    p.text((100, 100), "THEATRICAL TRAILER", size=22)
    p.text((100, 126), "SNEAK PEEKS", size=22)
    p.text((100, 330), "MAIN MENU", size=22)
    rects = {1: (330, 98, 380, 122), 2: (240, 124, 290, 150), 3: (600, 60, 640, 90), 4: (90, 320, 260, 360)}
    names = p.names(rects)
    assert names[1] == "THEATRICAL TRAILER" and names[2] == "SNEAK PEEKS" and names[4] == "MAIN MENU"
    assert 3 not in names


def test_a_heading_starts_a_block_its_lines_never_an_item_before_its():
    """A heading close under a list's last item (Chuck S1 D3's ONLINE plate under its EXTRAS), lines in the items'
    lettering under it that no button holds: a heading starts a block, as an empty line does -- they're no item's
    before it, the last item's name its own line."""
    p = Page()
    for k, t in enumerate(("Meet Jeff", "Movie Villains", "Sword Skills")):
        p.text((60, 60 + 26 * k), t, size=22)
    p.text((60, 140), "ONLINE", size=40, colour=GOLD)
    p.text((60, 194), "Visit us at www.example.com", size=22)
    p.text((60, 222), "for the latest on", size=16, colour=GREY, bold=False)
    p.text((60, 242), "our discs.", size=22)
    rects = {1: (40, 56, 500, 82), 2: (40, 82, 500, 108), 3: (40, 108, 500, 134)}
    assert p.names(rects) == {1: "Meet Jeff", 2: "Movie Villains", 3: "Sword Skills"}


def test_a_heading_in_a_list_splits_its_block_in_two():
    """A big heading in another style joined to the list over it and the text under it by its own height: a heading
    starts a block, so the list's block ends at its last item."""
    p = Page()
    for k, t in enumerate(("Meet Jeff", "Movie Villains", "Sword Skills")):
        p.text((60, 60 + 26 * k), t, size=22)
    p.text((60, 140), "ONLINE", size=40, colour=GOLD)
    p.text((60, 194), "Visit us at www.example.com", size=22)
    p.text((60, 222), "for the latest on", size=16, colour=GREY, bold=False)
    rects = {1: (40, 56, 500, 82), 2: (40, 82, 500, 108), 3: (40, 108, 500, 134)}
    m = textblocks.measure(p.image, p.lines)
    st = {i: textblocks.style(p.image, m[i][0], m[i][1]) for i in range(len(m))}
    ref = textblocks.item_style(m, st, {i: next((n for n, r in rects.items() if textblocks._holds(r, m[i][1])), None)
                                        for i in range(len(m))})
    got = [[m[i][0] for i in g] for g in textblocks.split_at_headings(m, textblocks.blocks(m, rects), st, ref)]
    assert ["Meet Jeff", "Movie Villains", "Sword Skills"] in got
    assert ["ONLINE", "Visit us at www.example.com", "for the latest on"] in got


def test_a_logos_bigger_lettering_touching_the_smaller_over_it_stays_in_its_block():
    """A film's title as a logo, small lettering just over big (Abbott and Costello's pickers: "Hold That / GHOST"):
    no empty rows between them, so no heading's start -- one block."""
    p = Page()
    p.text((120, 300), "Hold That", size=22, colour=CREAM)
    p.text((100, 304), "GHOST", size=56, colour=GOLD)
    p.text((420, 330), "In the Navy", size=22, colour=CREAM)
    rects = {1: (60, 260, 340, 420), 2: (380, 260, 660, 420)}
    m = textblocks.measure(p.image, p.lines)
    st = {i: textblocks.style(p.image, m[i][0], m[i][1]) for i in range(len(m))}
    ref = textblocks.item_style(m, st, {i: next((n for n, r in rects.items() if textblocks._holds(r, m[i][1])), None)
                                        for i in range(len(m))})
    assert textblocks.heading_look(st[1], ref)          # (big enough to look like one)
    got = [[m[i][0] for i in g] for g in textblocks.split_at_headings(m, textblocks.blocks(m, rects), st, ref)]
    assert ["Hold That", "GHOST"] in got


def test_ocrs_lines_a_word_apart_on_one_row_are_one_line_and_others_stay_apart():
    """OCR can return one line as two, a word's gap of ink between them (Chuck Season 2's "Chuck Versus" | "the Third
    Dimension"): one line, its corners from both ends. Two columns on a row stay apart, and so does a line starting
    well inside another (a logo's pieces, stacked lines)."""
    p = Page()
    b = p.text((60, 100), "Chuck Versus", size=24)
    p.text((b[2] + 8, 100), "the Third Dimension", size=24)
    p.text((60, 300), "Play", size=24)
    p.text((400, 300), "Scenes", size=24)
    p.text((60, 380), "The Making Of", size=24)
    p.text((120, 392), "Detective", size=24, line=False)
    p.lines.append(("Detective", (117.0, 389.0, 260.0, 424.0), 0.98))       # (inside "The Making Of", a little lower)
    corners = [(t, ((x0, y0), (x1, y0), (x1, y1), (x0, y1)), sure) for t, (x0, y0, x1, y1), sure in p.lines]
    got, got_corners = textblocks.join_rows(p.image, p.lines, corners)
    texts = [t for t, _, _ in got]
    assert texts[0] == "Chuck Versus the Third Dimension"
    assert "Play" in texts and "Scenes" in texts and "The Making Of" in texts and "Detective" in texts
    left, right = corners[0][1], corners[1][1]          # (its corners: the first's left ends, the second's right)
    assert textblocks.corner_of(got_corners, *got[0][:2])[1] == (left[0], right[1], right[2], left[3])
