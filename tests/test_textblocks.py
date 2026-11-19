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


def test_a_re_read_holding_the_description_puts_only_the_name_right():
    """A button's own picture read again holds its description under its name (a Blu-ray's: Prisoner of Azkaban's):
    the description's taken off before the name's put right by it, never put into the name; a reading without it,
    or of other words at its end, stays as it is."""
    said = "Interview with David Thewlis and Gary Oldman."
    reading = "Professor Lupin and Sirius Black Interview with David Thewlis and Gary 0ldman."
    assert menus.less_said(reading, said) == "Professor Lupin and Sirius Black"
    assert menus.less_said("Professor Lupin and Sirius Black", said) == "Professor Lupin and Sirius Black"
    assert menus.less_said("Behind the Story: The Making of the Film", said) == "Behind the Story: The Making of the Film"
    assert menus.less_said(reading, "") == reading
    is_word = lambda w: w.lower() in {"professor", "lupin", "and", "sirius", "black", "interview", "with", "david",
                                      "thewlis", "gary", "oldman"}
    lines = [("Professor Lupin and", None, 0.952), ("ir s Blach", None, 0.844)]
    assert menus.correct_words(lines, [(menus.less_said(reading.replace("0", "O"), said), 0.982)], is_word) == \
        "Professor Lupin and Sirius Black"


def test_a_word_ocr_split_is_put_back_whole():
    """correct_words: a word read in two pieces ("Revelat ion"), a second reading having it whole, is the whole word,
    no piece of it left over (10C0944E's "Nathaniel's Revelation"); a shorter reading of other letters still only
    swaps the misread word ("Destiny" for "Dostiny", nothing cut)."""
    is_word = lambda w: w.lower() in {"nathaniel's", "nathaniel", "revelation", "destiny", "cutdown", "spot"}
    assert menus.correct_words([("5. Nathaniel's Revelat ion", None, 0.96)], [("5. Nathaniel's Revelation", 0.97)],
                               is_word) == "5. Nathaniel's Revelation"
    assert menus.correct_words([("TV Spot: Dostiny Cutdown", None, 0.99)], [("Destiny", 0.95)],
                               is_word) == "TV Spot: Destiny Cutdown"


def test_a_re_read_spaced_better_is_taken_though_a_word_still_looks_odd():
    """correct_words: words OCR ran together ("PINOCCHIO7OTHANNIVERSARY", WALL-E's sneak peek), a second reading the
    same letters spaced otherwise ("PINOCCHIO7OTH ANNIVERSARY"): its spacing, though "PINOCCHIO7OTH" still looks
    misread -- moving spaces can't make a name worse; a re-spacing with more odd words than before isn't taken."""
    is_word = lambda w: w.lower() in {"pinocchio", "anniversary", "platinum", "edition", "the", "great", "escape"}
    lines = [("PINOCCHIO7OTHANNIVERSARY", None, 0.974), ("PLATINUM EDITION", None, 0.96)]
    assert menus.correct_words(lines, [("PINOCCHIO7OTH ANNIVERSARY PLATINUM EDITION", 0.952)], is_word) == \
        "PINOCCHIO7OTH ANNIVERSARY PLATINUM EDITION"
    assert menus.correct_words([("Thegreat Escape", None, 0.97)], [("Thegr eat Escape", 0.95)], is_word) == \
        "Thegreat Escape"


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


def test_a_name_read_right_again_surely_is_read_no_further(monkeypatch):
    """A name with a word no dictionary has ("Revelat ion") read again surely, every word right or read alike by the
    page (a name no dictionary has, "Nathaniel"): that's it -- its plate isn't read (the page at twice the size was,
    for the grouping), and it's as sure as the two readings (10C0944E's "Nathaniel's Revelation")."""
    p = Page()
    p.text((60, 60), "Nathaniel Revelation", size=24)
    p.text((60, 140), "Photo Gallery", size=24)
    rects = [[40, 50, 560, 110], [40, 130, 560, 180]]
    p.lines[0] = ("Nathaniel Revelat ion", p.lines[0][1], 0.959)
    known = lambda w: w.lower() in {"revelation", "photo", "gallery"}
    further = []
    monkeypatch.setattr(menus, "_plate_round", lambda *a: further.append("plate"))
    monkeypatch.setattr(menus, "read_alone", lambda *a, **k: [(menus.Words("Nathaniel Revelation"), 0.968)])
    menus._groupings.clear()
    got, sure, _ = menus.name_button(p.image, rects[0], rects, (720, 480), lines=p.lines, is_word=known)
    assert got == "Nathaniel Revelation" and sure == 0.959
    assert not further


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


def test_a_buttons_title_in_the_pages_title_lettering_is_no_heading_over_its_description():
    """One button's block, its title in the lettering the page's other titles are in, its description under it
    ending in a comma, not a full stop (Chuck S2 D1's "Chuck Versus the Cougars Sc. 33" / "Chuck at Sarah's high
    school reunion,"): the title is its name, the line under it its description -- never the description named and
    the title taken for a heading."""
    p = Page()
    for k, (title, said) in enumerate((("The Break-Up Sc. 27", "Morgan and Lester ponder their end."),
                                       ("The Cougars Sc. 33", "Chuck at the high school reunion,"),
                                       ("The Ex Sc. 10", "Chuck gets prepped for the mission."))):
        p.text((60, 60 + 110 * k), title, size=26)
        p.text((60, 96 + 110 * k), said, size=16, colour=GREY, bold=False)
    rects = {1: (40, 50, 600, 160), 2: (40, 160, 600, 270), 3: (40, 270, 600, 380)}
    got = textblocks.page_names(p.image, p.lines, rects)
    assert {n: " ".join(t for t, _, _ in v) for n, v in got.items()} == {1: "The Break-Up Sc. 27", 2: "The Cougars Sc. 33",
                                                                        3: "The Ex Sc. 10"}
    assert " ".join(t for t, _, _ in got[2].description) == "Chuck at the high school reunion,"


def test_a_line_carrying_on_into_a_title_under_it_is_part_of_it_whatever_its_size():
    """A title over two lines, its first measured bigger than the page's other titles (Spider-Man 2's "Peter Parker
    and" / "Mary Jane Watson"): a line ending in "and" carries on into the title under it, in its colour -- no
    heading."""
    p = Page()
    for k, title in enumerate(("Costume Design", "Comic-Con Q&A", "J. Jonah Jameson")):
        p.text((60, 60 + 50 * k), title, size=22)
    p.text((60, 210), "Peter Parker and", size=30)
    p.text((60, 250), "Mary Jane Watson", size=22)
    rects = {1: (40, 50, 600, 100), 2: (40, 100, 600, 150), 3: (40, 150, 600, 200), 4: (40, 200, 600, 290)}
    got = {n: " ".join(t for t, _, _ in v) for n, v in textblocks.page_names(p.image, p.lines, rects).items()}
    assert got[4] == "Peter Parker and Mary Jane Watson"


def test_a_line_touching_the_title_under_it_is_part_of_it_however_its_lettering_measures():
    """Abbott and Costello's "BONUS / MATERIALS": "BONUS", all wide letters, measured as other lettering than the
    page's titles, but touching the line under it, no empty rows between, in its colour and its button's box -- no
    heading (a heading has space under it), so part of the name; nor dropped to start it where its siblings start
    theirs."""
    p = Page()
    p.text((60, 60), "PLAY", size=24)
    p.text((60, 110), "SCENES", size=24)
    p.text((300, 50), "BONUS", size=34)
    p.text((300, 82), "MATERIALS", size=24)
    p.text((300, 140), "LANGUAGES", size=24)
    p.text((300, 190), "MENU", size=24)
    rects = {1: (40, 50, 250, 100), 2: (40, 100, 250, 150), 3: (280, 40, 560, 120), 4: (280, 130, 560, 180),
             5: (280, 180, 560, 230)}
    got = {n: " ".join(t for t, _, _ in v) for n, v in textblocks.page_names(p.image, p.lines, rects).items()}
    assert got[3] == "BONUS MATERIALS"


def test_a_readings_outline_follows_its_lines_slant():
    """A name over two lines on a screen at an angle (WALL-E's tilted list): one outline round both, by RapidOCR's
    corners for them -- not the upright box round them. A piece of a line (split off it) takes that line's slant."""
    lines = [("SCENE", (100.0, 100.0, 200.0, 140.0), 0.98), ("SELECTION", (110.0, 125.0, 300.0, 175.0), 0.98)]
    corners = [("SCENE", ((100, 120), (200, 100), (200, 120), (100, 140)), 0.98),
               ("SELECTION", ((110, 155), (300, 125), (300, 145), (110, 175)), 0.98)]
    shape = textblocks.reading_shape(lines, corners)
    assert (100.0, 120.0) in shape and (300.0, 125.0) in shape and (110.0, 175.0) in shape
    assert (100.0, 100.0) not in shape                  # (the upright box's corner: off the slanted text)
    whole = [("BONUS FEATURES", ((100, 220), (400, 160), (400, 190), (100, 250)), 0.98)]
    part = textblocks.line_shape("FEATURES", (250.0, 160.0, 400.0, 250.0), whole)
    assert part[0] == (250.0, 190.0) and part[1] == (400.0, 160.0)   # (its stretch of the slanted band)


def test_a_shorter_reading_puts_a_misread_word_right_and_cuts_nothing():
    """A reading of fewer words than the name (a plate's or a word's own: "Destiny") puts right only the misread
    word it's spelt like -- never the name for less."""
    known = {"spot", "destiny", "cutdown"}
    is_word = lambda w: w.lower() in known
    assert menus.correct_words([("TV Spot: Dostiny Cutdown", (0, 0, 1, 1), 0.99)], [("Destiny", 0.95)], is_word) == \
        "TV Spot: Destiny Cutdown"
    assert menus.correct_words([("TV Spot: Dostiny Cutdown", (0, 0, 1, 1), 0.99)], [("Gallery", 0.95)], is_word) == \
        "TV Spot: Dostiny Cutdown"                          # (nothing spelt like it: as read)


def test_a_line_read_across_two_buttons_is_cut_between_them():
    """Small lettering on a slanted screen, two names side by side read as one line (Spider-Man's TV SPOTS:
    "Blockbustor Cutdown" across two names): a marker by each name says where one stops -- the line cut at its
    widest ink gap between them, however narrow, each marker its own part."""
    p = Page()
    a = p.text((60, 100), "Countdown", size=20, line=False)
    b = p.text((a[2] + 6, 100), "Cutdown", size=20, line=False)
    p.lines.append(("Countdown Cutdown", (a[0] - 3.0, a[1] - 3.0, b[2] + 3.0, b[3] + 3.0), 0.95))
    rects = {1: (a[2] - 30, a[1] - 4, a[2] - 18, a[3] + 4), 2: (b[2] - 30, b[1] - 4, b[2] - 18, b[3] + 4)}
    got = {n: " ".join(t for t, _, _ in v) for n, v in textblocks.page_names(p.image, p.lines, rects).items()}
    assert got == {1: "Countdown", 2: "Cutdown"}


def test_a_slanted_line_belongs_to_the_button_by_its_own_start():
    """A page drawn at a slant (WALL-E's main menu): each line's button the one whose box its lettering's band covers
    most of, not the one nearest its upright box's middle -- PLAY's by its start, SCENE SELECTION's under it by its own
    start, though PLAY's box is nearer its middle; an upright line as before, by its middle."""
    def slanted(left, right, cy, k=-0.28, h=30):
        cx = (left + right) / 2
        rise = abs(k) * (right - left)
        return {"left": left, "right": right, "top": cy - rise / 2 - h / 2, "bottom": cy + rise / 2 + h / 2,
                "cx": cx, "cy": cy, "k": k, "straight": h, "h": h + int(rise)}
    play = ("PLAY", slanted(346, 432, 184))
    scene = ("SCENE SELECTION", slanted(293, 482, 226))
    level = ("Languages", {"left": 100, "right": 200, "top": 400, "bottom": 420, "h": 20})
    rects = {1: (247, 177, 366, 236), 2: (269, 238, 356, 273), 3: (90, 395, 210, 425)}
    textblocks.slant_holders([play, scene, level], rects)
    assert play[1]["held_by"] == 1 and scene[1]["held_by"] == 2 and "held_by" not in level[1]
    assert textblocks._holder(rects, scene[1]) == 2 and textblocks._holder(rects, level[1]) == 3
    assert textblocks._holder({1: rects[1]}, scene[1]) is None      # (its button not among these: by its middle)


def test_a_slanted_names_lines_read_in_order_along_the_slant():
    """A name in two slanted lines (Muppets from Space's main menu, "SPECIAL / FEATURES" rising to the right): the
    longer lower line's upright box starts higher, but along the slant the upper line comes first; side by side on
    one slanted line, left to right; upright lines as they came."""
    def slanted(left, right, cy, k=-0.2, h=26):
        rise = abs(k) * (right - left)
        return {"left": left, "right": right, "top": cy - rise / 2 - h / 2, "bottom": cy + rise / 2 + h / 2,
                "cx": (left + right) / 2, "cy": cy, "k": k, "straight": h, "h": h + int(rise)}
    special, features = ("SPECIAL", slanted(440, 573, 247)), ("FEATURES", slanted(485, 657, 250))
    assert features[1]["top"] < special[1]["top"]
    assert [t for t, _ in textblocks.slant_order([features, special])] == ["SPECIAL", "FEATURES"]
    play, all_ = ("PLAY", slanted(100, 160, 200)), ("ALL", slanted(170, 210, 200 - 0.2 * 55))
    assert [t for t, _ in textblocks.slant_order([all_, play])] == ["PLAY", "ALL"]
    up = [("B", {"left": 0, "right": 9, "top": 50, "bottom": 60, "h": 10}), ("A", {"left": 0, "right": 9, "top": 10, "bottom": 20, "h": 10})]
    assert textblocks.slant_order(up) == up
    # (a logo's words each at its own slant, "THE" tipped over a level "JUNGLE": as they came, not along one)
    logo = [("JUNGLE", dict(slanted(320, 425, 226), k=0.0)), ("THE", slanted(326, 358, 211, k=-0.3))]
    assert textblocks.slant_order(logo) == logo


def described_page(entries, title_size=26):
    """Buttons stacked, each a title over a description of two lines in smaller grey lettering (Order of the
    Phoenix's, Azkaban's): (page, rects, measured lines, styles, blocks), as classify has them."""
    p = Page(size=(720, 120 * len(entries) + 40))
    rects = {}
    for k, (title, said) in enumerate(entries):
        y = 30 + 120 * k
        for j, t in enumerate(title):
            p.text((60, y + 36 * j), t, size=title_size)
        y += 36 * len(title) + 4
        for j, t in enumerate(said):
            p.text((60, y + 22 * j), t, size=16, colour=GREY, bold=False)
        rects[k + 1] = (40, 20 + 120 * k, 680, 20 + 120 * (k + 1))
    measured = textblocks.measure(p.image, p.lines)
    st = {i: textblocks.style(p.image, t, ik) for i, (t, ik) in enumerate(measured)}
    return p, rects, measured, st, textblocks.blocks(measured, rects)


def named(measured, title, said=()):
    """A button's names entry as classify gives it: its title's and description's lines by their words."""
    at = lambda t: next(i for i, (x, _) in enumerate(measured) if x == t)
    return {"name": " ".join(title), "idx": [at(t) for t in title], "lines": [measured[at(t)] for t in title],
            "description": " ".join(said), "desc_idx": [at(t) for t in said], "description_lines": [measured[at(t)] for t in said],
            "heading": ""}


PHOENIX = [(["Trailing Tonks"], ["Spend a day with the actress and", "receive a very personal tour."]),
           (["The Thestral Flight"], ["A look at how the flight was filmed", "over the streets of London."]),
           (["The Hidden Secrets of", "Harry Potter"], ["Take a thrilling look back through", "the previous films for clues."])]


def test_a_description_classify_took_for_a_title_goes_back_to_its_title():
    """The style dictionary's second look (the user's design): Order of the Phoenix's "The Hidden Secrets of / Harry
    Potter" left over its sentence, the sentence taken for its title -- its lines' style group is the page's
    descriptions (after another group's line in its buttons, reading as sentences), so it's the description, and the
    unused lines above it, in the page's title lettering, the title."""
    p, rects, measured, st, blocks = described_page(PHOENIX)
    names = {1: named(measured, PHOENIX[0][0], PHOENIX[0][1]), 2: named(measured, PHOENIX[1][0], PHOENIX[1][1]),
             3: named(measured, PHOENIX[2][1])}
    textblocks.restyle(measured, blocks, rects, names, st)
    assert names[3]["name"] == "The Hidden Secrets of Harry Potter"
    assert names[3]["description"] == "Take a thrilling look back through the previous films for clues."
    assert names[1]["name"] == "Trailing Tonks" and names[2]["name"] == "The Thestral Flight"


def test_a_colon_binds_the_title_line_between_it_and_its_description():
    """Order of the Phoenix's "Harry Potter: / The Magic of Editing": classify's colon bound the description's first
    line, the title's own second line left out -- the colon's is the line between in the title lettering, and the
    description's lines go back to it whole."""
    entries = PHOENIX[:2] + [(["Harry Potter:", "The Magic of Editing"], ["Director and editor show us what", "a good edit makes of a scene."])]
    p, rects, measured, st, blocks = described_page(entries)
    names = {1: named(measured, entries[0][0], entries[0][1]), 2: named(measured, entries[1][0], entries[1][1]),
             3: named(measured, ["Harry Potter:", "Director and editor show us what"], ["a good edit makes of a scene."])}
    textblocks.restyle(measured, blocks, rects, names, st)
    assert names[3]["name"] == "Harry Potter: The Magic of Editing"
    assert names[3]["description"] == "Director and editor show us what a good edit makes of a scene."


def test_a_description_classify_dropped_comes_back_from_its_style_group():
    """Azkaban's interviews: a button's description left out though it's in the lettering its siblings' confident
    descriptions are in, close under its title -- it's the button's description."""
    p, rects, measured, st, blocks = described_page(PHOENIX[:2] + [(["The Paper Swallow"], ["A note that flies across the", "office from desk to desk."])])
    names = {1: named(measured, PHOENIX[0][0], PHOENIX[0][1]), 2: named(measured, PHOENIX[1][0], PHOENIX[1][1]),
             3: named(measured, ["The Paper Swallow"])}
    textblocks.restyle(measured, blocks, rects, names, st)
    assert names[3]["name"] == "The Paper Swallow"
    assert names[3]["description"] == "A note that flies across the office from desk to desk."


def test_text_under_titles_with_no_confident_description_in_its_group_isnt_added():
    """The noise guard: new lines join a description only where confident lines of their group are descriptions --
    a backdrop's print read as words (DA502D7E's newspaper) has none; and a page whose names classify got right is
    left as it is."""
    p, rects, measured, st, blocks = described_page(PHOENIX[:2])
    names = {1: named(measured, PHOENIX[0][0]), 2: named(measured, PHOENIX[1][0])}
    textblocks.restyle(measured, blocks, rects, names, st)
    assert names[1]["description"] == "" and names[2]["description"] == ""
    right = {1: named(measured, PHOENIX[0][0], PHOENIX[0][1]), 2: named(measured, PHOENIX[1][0], PHOENIX[1][1])}
    textblocks.restyle(measured, blocks, rects, right, st)
    assert right[1]["description"] == " ".join(PHOENIX[0][1]) and right[2]["name"] == "The Thestral Flight"


def _settle_page(lines, words, answers, monkeypatch, blocks=None, owners=None):
    """The settle loop alone, its classify and verdicts stood in for: lines' texts, 20 pixels apart (never touching);
    words: {line: its group's word}, or a list of them, one per round; answers [(fixed, {button: (title lines,
    description lines)})], classify's by the fixed titles it's asked with; blocks (default: one, all the lines) and
    owners ({line: button}, default 1). Returns (names, the fixed titles classify was asked with)."""
    measured = [(t, {"top": 20 * i, "bottom": 20 * i + 9, "left": 0, "right": 200, "h": 10}) for i, t in enumerate(lines)]
    blocks = blocks or [list(range(len(lines)))]
    owners = owners or {i: 1 for i in range(len(lines))}
    rounds = words if isinstance(words, list) else None
    asked, calls = [], []

    def classify(image, lines_, groups, rects, disc_rects=None, st=None, fixed=None):
        asked.append(fixed)
        got = next(a for f, a in answers if (f or None) == (fixed or None))
        return {n: {"name": " ".join(lines[i] for i in t), "idx": list(t), "desc_idx": list(d)} for n, (t, d) in got.items()}, []

    def verdicts(*a):
        w = rounds[min(len(calls), len(rounds) - 1)] if rounds else words
        calls.append(1)
        return {"word": w.get, "confident": {}, "owner": owners, "order": lambda i: i, "by_button": {}, "evidence": [],
                "group_of": {}}
    monkeypatch.setattr(textblocks, "_classify", classify)
    monkeypatch.setattr(textblocks, "_verdicts", verdicts)
    st = {i: {"size": 10, "width": 8, "box": 12, "colour": (200, 200, 200), "ring": None, "caps": None} for i in range(len(lines))}
    names, _, _v = textblocks._settle(None, measured, blocks, {n: (0, 0, 9, 9) for n in set(owners.values())}, None, st, None)
    return names, asked


def test_the_settle_loop_fixes_a_blocks_title_and_classify_judges_the_rest_by_it(monkeypatch):
    """The user's settle loop: classify had "Trailing Tonks" for the block's heading, its sentence for the name; its
    style group says it's the title, so it's fixed as the block's title and classify runs again, the sentence then its
    description -- and nothing more to fix: settled, in one round."""
    lines = ["Trailing Tonks", "Spend a day with the actress", "on a personal tour", "of the stages."]
    names, asked = _settle_page(lines, {0: "title", 1: "description", 2: "description", 3: "description"},
                                [(None, {1: ([1, 2, 3], [])}), ({0: [0]}, {1: ([0], [1, 2, 3])})], monkeypatch)
    assert asked == [None, {0: [0]}]
    assert names[1]["idx"] == [0] and names[1]["desc_idx"] == [1, 2, 3]


def test_the_settle_loops_fixed_title_grows_through_the_hard_rules(monkeypatch):
    """Spider-Man 2's "Peter Parker and" / "Mary Jane Watson", its first line in other lettering, no title word of its
    own: the run fixed grows up through a line carrying on into it, so the title's never cut short."""
    lines = ["Peter Parker and", "Mary Jane Watson", "A look at the pair."]
    names, asked = _settle_page(lines, {1: "title", 2: "description"},
                                [(None, {1: ([2], [])}), ({0: [0, 1]}, {1: ([0, 1], [2])})], monkeypatch)
    assert asked[1] == {0: [0, 1]} and names[1]["name"] == "Peter Parker and Mary Jane Watson"


def test_a_settle_loop_coming_round_again_keeps_classifys_own_names(monkeypatch):
    """A state seen before, not the last: a cycle -- classify's own names kept, the page noted (SETTLE_CYCLES). Two
    blocks: the first fixed in one round, the second the next, and classify's answer then its first again."""
    lines = ["Title One", "Words under it.", "Title Two", "More words."]
    first = {1: ([1], []), 2: ([3], [])}
    textblocks.SETTLE_CYCLES.clear()
    names, asked = _settle_page(lines, [{0: "title", 1: "description"}, {0: "title", 2: "title", 3: "description"}],
                                [(None, first), ({0: [0]}, {1: ([0], [1]), 2: ([3], [])}), ({0: [0], 1: [2]}, first)],
                                monkeypatch, blocks=[[0, 1], [2, 3]], owners={0: 1, 1: 1, 2: 2, 3: 2})
    assert asked == [None, {0: [0]}, {0: [0], 1: [2]}]
    assert names[1]["idx"] == [1] and names[2]["idx"] == [3] and len(textblocks.SETTLE_CYCLES) == 1
