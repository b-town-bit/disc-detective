"""The credits witness's reading rules (credits.py): years and certificate numbers off a film's cards, the close
window, the number's pick, and the MPA numbers' table against a small IMDb index of its own."""
import json
import sqlite3

import pytest

from disc_detective import core, credits


@pytest.mark.parametrize("row, years", [
    ("COPYRIGHT © 1989 by UNIVERSAL CITY STUDIOS, INC.", {1989}),
    ("Copyright©199I TriStar Pictures. Inc.", {1991}),            # (I for 1)
    ("©L999 Warner Bros.", {1999}),                                # (L for 1)
    ("COPYRIGHT MCMXLI BY UNIVERSAL PICTURES COMPANY, INC.", {1941}),
    ("© MMI New Line Productions", {2001}),                        # (a short Roman year: MM...)
    ("OMM Disney", {2000}),                                        # (the © read as O)
    ("This motion picture is protected", set()),
])
def test_row_years(row, years):
    assert credits.row_years(row) == years


def test_copyright_rows():
    assert credits.copyright_row("© 1994 WARNER BROS. AND ICON DISTRIBUTION, INC.")
    assert credits.copyright_row("COPYRIGNT MCMXLI BY UNIVERSAL PICTURES")      # (a misread "copyright")
    assert not credits.copyright_row("©2005 WARNER-OLIVE MUSIC, LLC")            # (a song's, not the film's)
    assert not credits.copyright_row("DOLBY logo used with permission")
    assert credits.company_row("2008 DISNEY ENTERPRISES, INC.")


@pytest.mark.parametrize("rows, nums", [
    (["MOTION PICTURE ASSOCIATION OF AMERICA", "No. 42055"], ["42055"]),
    (["CERTIFICATE NO. 29589"], ["29589"]),
    (["APPROVED N0.21531"], ["21531"]),                           # (0 for O)
    (["N.42616"], ["42616"]),                                     # (the o of "No." not read)
    (["33124"], ["33124"]),                                       # (a bare number beside the logo)
    (["NO ANIMALS WERE HARMED. (AHD 09769)"], []),                # (the American Humane's number)
    (["1994"], []),                                               # (a year, not a number)
])
def test_numbers(rows, nums):
    assert credits.nums_in(rows) == nums


def test_norm():
    assert credits.norm("Renée Stock-Poynton, O'Brien") == ["renee", "stock", "poynton", "o", "brien"]


def test_clear_lead():
    assert credits.clear(10, 4)          # (6 ahead: half of 10)
    assert not credits.clear(10, 6)
    assert not credits.clear(3, 1.5)     # (2 ahead at least)


class FakeIndex:
    """titles: {id: (title, year, type, runtime, votes)}"""

    def __init__(self, titles):
        self.t = titles

    def title(self, i):
        return self.t[i][:4]

    def votes(self, i):
        return self.t[i][4]


def scan_with(titles, certs):
    sc = credits.Scan(FakeIndex(titles), certs)
    sc._reset(None)
    return sc


def test_number_needs_its_year():
    sc = scan_with({36763: ("The Sixth Sense", 1999, "movie", 107, 1000000)}, {"36763": [36763]})
    sc.cert = [{36763}, "36763", ["36763"]]
    assert sc.number_pick() == (None, "no year read")
    sc.years = {2000: 1.0}                   # (Shanghai Noon's card reused: its year, 2000)
    assert sc.number_pick()[0] is None
    sc.years = {1999: 1.0}
    assert sc.number_pick() == (36763, None)
    sc.years = {1998: 1.0}                   # (made one year, out the next)
    assert sc.number_pick() == (36763, None)


def test_window():
    sc = scan_with({1: ("A", 1990, "movie", 90, 5000), 2: ("B", 1991, "movie", 90, 9000), 3: ("C", 1992, "movie", 90, 100)}, {})
    r = [(10.0, 5, "A", 1990, "movie", 90, "tt0000001"), (4.0, 2, "B", 1991, "movie", 90, "tt0000002")]
    sc.why_of = {"A": ["ann one", "bob two", "cat three"], "B": ["ann one"]}
    assert [x[2] for x in sc.window_of(r)] == ["A"]                       # (a clear lead)
    r = [(10.0, 5, "A", 1990, "movie", 90, "tt0000001"), (7.0, 4, "B", 1991, "movie", 90, "tt0000002")]
    assert [x[2] for x in sc.window_of(r)] == ["A", "B"]                  # (close: both)
    # (a clear lead on two names another film has too: more evidence needed)
    r = [(10.0, 2, "A", 1990, "movie", 90, "tt0000001"), (3.0, 2, "C", 1992, "movie", 90, "tt0000003")]
    sc.why_of = {"A": ["ann one", "bob two"], "C": ["ann one", "bob two"]}
    assert [x[2] for x in sc.window_of(r)] == ["A", "C"]


def test_mpa_build(tmp_path):
    ti = sqlite3.connect(tmp_path / core.IMDB_DB)
    ti.execute("create table titles (id integer primary key, title text, alt text, year int, type text, runtime int)")
    ti.executemany("insert into titles values (?, ?, ?, ?, ?, ?)", [
        (1, "3 Ninjas", None, 1992, "movie", 84), (2, "Red Dawn", None, 2012, "movie", 93),
        (3, "Red Dawn", None, 2011, "short", 10), (4, "The Great American Escape", None, 1971, "movie", 90)])
    ti.commit()
    ti.close()
    n, shared = credits.mpa_build(str(tmp_path), [("40839", "tt0368008")],
                                  [("32001", "Three Ninjas", "1992"), ("47700", "Red Dawn", "2011"),
                                   ("12345", "Great American Escape, The", "1970")])
    table = json.load(open(tmp_path / credits.MPA_FILE))
    assert table["40839"] == [368008]                     # (Wikidata's, by IMDb id)
    assert table["32001"] == [1]                          # (number words as digits)
    assert table["47700"] == [2]                          # (the feature, a year on: not the 2011 short)
    assert table["12345"] == [4]                          # ("Title, The"; a year on)
    assert credits.mpa_load(str(tmp_path)) == table
    assert not credits.mpa_stale(str(tmp_path))


def test_drawn_mark_found():
    """The drawn mark (no film's picture) is found in a frame that shows it, at any size, light on dark or dark
    on light, and not in a frame of plain shapes."""
    import numpy as np
    import cv2
    for w, inv in ((120, False), (60, False), (90, True)):
        frame = np.zeros((540, 960), np.uint8)
        mark = credits.draw_logo(w)
        frame[300:300 + mark.shape[0], 400:400 + w] = mark
        if inv:
            frame = 255 - frame
        sc, box = credits.find_logo(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
        assert sc >= credits.LOGO_MIN and abs(box[0] - 400 / 960) < 0.03
    frame = np.zeros((540, 960), np.uint8)
    cv2.rectangle(frame, (100, 100), (300, 180), 255, -1)
    cv2.ellipse(frame, (600, 300), (100, 40), 0, 0, 360, 255, 4)
    assert credits.find_logo(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))[0] < credits.LOGO_MIN


def boxed_card(w, h):
    """A test boxed card (DreamWorks' kind): the plain drawn mark's lines, dark on a light box w x h."""
    import numpy as np
    import cv2
    p = credits.LOGO_SHAPE
    S = 8
    big = np.full((h * S, w * S), 255, np.uint8)
    cx, cy, ry, rx = w / 2 * S, h * 0.445 * S, h * 0.277 * S, w * 0.446 * S
    t = max(1, int(ry * 0.145))
    P = lambda x, y: (int(x), int(y))
    cv2.ellipse(big, P(cx, cy), P(rx, ry), 0, 0, 360, 0, t, cv2.LINE_AA)
    cv2.ellipse(big, P(cx, cy), P(rx * p["iw"] / p["ow"], ry), 0, 0, 360, 0, t, cv2.LINE_AA)
    cv2.line(big, P(cx - rx, cy), P(cx + rx, cy), 0, t, cv2.LINE_AA)
    cv2.circle(big, P(cx, cy), int(ry * 0.712), 255, -1, cv2.LINE_AA)
    cv2.circle(big, P(cx, cy), int(ry * 0.712), 0, t, cv2.LINE_AA)
    return cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA)


def test_boxed_mark_found_plain_box_not():
    """The boxed card (the mark in dark lines on a light box) is found; a plain filled box, or a box of other
    content, isn't."""
    import numpy as np
    import cv2
    frame = np.zeros((540, 960), np.uint8)
    w, h = 150, 96
    box = boxed_card(w, h)
    frame[200:200 + h, 300:300 + w] = box
    cv2.rectangle(frame, (600, 200), (750, 296), 255, -1)                       # (a plain box)
    cv2.rectangle(frame, (600, 350), (750, 446), 255, -1)                       # (a box of lettering)
    cv2.putText(frame, "AVID", (612, 420), cv2.FONT_HERSHEY_SIMPLEX, 1.6, 0, 6)
    sc, where = credits.find_logo(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
    assert sc >= credits.LOGO_MIN and abs(where[0] - 300 / 960) < 0.03
    frame[200:200 + h, 300:300 + w] = 0                                          # (the mark gone: the boxes alone)
    assert credits.find_logo(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))[0] < credits.LOGO_MIN


def test_tilted_mark_found():
    """A mark printed a few degrees off level (a scrapbook-style card's) is found by turning it level."""
    import numpy as np
    import cv2
    frame = np.zeros((540, 960), np.uint8)
    mark = credits.draw_logo(140)
    frame[250:250 + mark.shape[0], 400:540] = mark
    R = cv2.getRotationMatrix2D((470, 250 + mark.shape[0] / 2), 5, 1.0)
    frame = cv2.warpAffine(frame, R, (960, 540))
    assert credits.find_logo(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))[0] >= credits.LOGO_MIN


@pytest.mark.parametrize("rows, nums", [
    (["N.42616"], ["42616"]), (["MPAA #32795"], ["32795"]), (["No.45254"], ["45254"]),
    (["CERTIFICATE NO. 29589"], ["29589"]), (["(AHA 01057)", "No. 42582"], ["1057", "42582"]),
    (["Copyright 2002 DreamWorks"], []), (["SOUND"], []), (["4I8O3"], ["41803"]),
])
def test_numbers_near_a_logo(rows, nums):
    """Beside a found logo, any 4-6 digit run counts (the table and the year decide), years apart."""
    assert credits.nums_near(rows) == nums


def test_playlist_chapter_times():
    """A playlist's chapter marks in its own seconds, each item's counted from where it starts in the playlist."""
    from fixtures import _mpls, T45
    p = core.parse_mpls(_mpls([("00001", T45 * 10, T45 * 70), ("00002", T45 * 5, T45 * 35)], 2))
    assert p["chapter_times"] == [0.0, 30.0, 60.0, 75.0]


def _clpi(points, spn_c=0x10000):
    """A clip info file whose EP_map holds one coarse entry and these fine ones: [(90 kHz pts, packet number)]."""
    import struct
    fine = b"".join(struct.pack(">I", (((p >> 9) & 0x7FF) << 17) | (s & 0x1FFFF)) for p, s in points)
    coarse = struct.pack(">II", (0 << 14) | (points[0][0] >> 19), spn_c)
    table = struct.pack(">I", 4 + len(coarse)) + coarse + fine
    bits = (1 << 66) | (1 << 50) | (len(points) << 32) | 14
    ep = bytes([0, 1]) + struct.pack(">H", 0x1011) + bits.to_bytes(10, "big") + table
    cpi = struct.pack(">I", 2 + len(ep)) + b"\x00\x01" + ep
    return b"HDMV0200" + struct.pack(">IIIII", 0, 0, 40, 0, 0) + b"\x00" * 12 + cpi


def test_clip_entry_points():
    """A clip's entry points: seconds on its clock and byte offsets (192-byte packets), the fine packet numbers
    carried on past their 17 bits."""
    pts = [k * 45056 for k in range(4)]
    spn = [0x10000 + k * 0x8000 for k in range(4)]                       # (0x20000 and on: past 17 bits)
    got = core.clpi_entry_points(_clpi(list(zip(pts, spn))))
    assert [round(t, 4) for t, _ in got] == [round(p / 90000, 4) for p in pts]
    assert [o for _, o in got] == [s * 192 for s in spn]


def test_disc_source_finds_the_entry_point():
    """A time on a two-clip playlist: its clip, the entry point at or before it, the seconds on to it, and reading
    on to an entry point past it."""
    src = credits.DiscSource.__new__(credits.DiscSource)
    src.items, src.starts = [("00001", 10.0), ("00002", 5.0)], [0.0, 60.0]
    src.maps = {"00001": [(10.0 + k, k * 10 ** 6) for k in range(61)], "00002": [(5.0 + k, k * 10 ** 6) for k in range(31)]}
    clip, off, delta, end = src.where(62.4)
    assert (clip, off, round(delta, 1)) == ("00002", 2 * 10 ** 6, 0.4)
    assert end >= 4 * 10 ** 6


def _imdb(rows):
    """An IMDb index holding these films: [(number, title, year, votes)]."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE titles (id INTEGER PRIMARY KEY, type TEXT, title TEXT, year INT, runtime INT)")
    con.execute("CREATE TABLE ratings (id INTEGER PRIMARY KEY, votes INT)")
    for n, title, year, votes in rows:
        con.execute("INSERT INTO titles VALUES (?, 'movie', ?, ?, 120)", (n, title, year))
        con.execute("INSERT INTO ratings VALUES (?, ?)", (n, votes))
    return con


def _cand(n, name, score):
    return {"kind": "movie", "imdb_id": "tt%07d" % n, "name": name, "year": 2000, "score": score, "why": "name",
            "source": "IMDb"}


def test_credits_winner_goes_first_and_probable():
    """A credits winner the search didn't find goes first, far enough ahead to be probable."""
    con = _imdb([(373889, "Harry Potter and the Order of the Phoenix", 2007, 600000)])
    rep = {"candidates": [_cand(1, "Phoenix", 70), _cand(2, "Order", 62)]}
    core.credits_seen(rep, {"winner": {"imdb_id": "tt0373889"}, "by": "the MPA card",
                            "why": "its certificate number 43476 (2007)", "number": "43476"}, con)
    assert rep["candidates"][0]["imdb_id"] == "tt0373889" and core.is_probable(rep["candidates"])
    assert rep["candidates"][0]["why"] == "its certificate number 43476 (2007)"
    assert [c["imdb_id"] for c in rep["candidates"][1:]] == ["tt0000001", "tt0000002"]


def test_credits_close_films_raise_the_search_s_own():
    """No winner: a candidate the close films hold too is raised over one they don't; the rest of them are
    listed below, never probable on the credits alone."""
    con = _imdb([(10, "Film A", 2001, 5000), (11, "Film B", 2002, 9000), (12, "Film C", 2003, 100)])
    rep = {"candidates": [_cand(99, "Other", 64), _cand(10, "Film A", 60)]}
    core.credits_seen(rep, {"winner": None, "by": None,
                            "window": [{"imdb_id": "tt0000011"}, {"imdb_id": "tt0000010"}, {"imdb_id": "tt0000012"}]}, con)
    ids = [c["imdb_id"] for c in rep["candidates"]]
    assert ids[:2] == ["tt0000010", "tt0000099"]
    assert set(ids[2:]) == {"tt0000011", "tt0000012"} and ids.index("tt0000011") < ids.index("tt0000012")
    only = {"candidates": []}
    core.credits_seen(only, {"winner": None, "window": [{"imdb_id": "tt0000011"}, {"imdb_id": "tt0000012"}]}, con)
    assert not core.is_probable(only["candidates"])


def test_credits_asked_only_when_nothing_is_sure():
    """The scan reads credits for a Blu-ray film nothing named for sure, with the setting on (its default)."""
    import types
    from disc_detective import app
    from disc_detective.kinds import Role
    me = types.SimpleNamespace(settings={}, CREDITS_FORMATS=app.App.CREDITS_FORMATS)
    feature = {"id": "00800.mpls", "role": Role.FEATURE, "duration": 7200}
    rep = {"path": "E:\\", "format": "Blu-ray", "kind": "movie", "discdb": {"hit": None}, "titles": [feature],
           "candidates": [_cand(1, "A", 62), _cand(2, "B", 58)]}
    wanted = lambda r, vlc="C:/VLC", **st: app.App.credits_wanted(
        types.SimpleNamespace(**dict(vars(me), settings=st, credits_vlc=lambda: vlc)), r)
    assert wanted(rep)
    assert not wanted(rep, use_credits=False)
    assert not wanted(dict(rep, candidates=[_cand(1, "A", 80)]))
    assert not wanted(dict(rep, personal_hit=True))
    assert not wanted(dict(rep, kind="tv"))
    assert wanted(dict(rep, format="DVD"))                     # (a DVD's through VLC)
    assert not wanted(dict(rep, format="DVD"), vlc=None)
    assert not wanted(dict(rep, format="Ultra HD"))
    assert app.App.credits_titles(rep) == [feature]
    extra = {"id": "00001.mpls", "role": Role.EXTRA, "duration": 1200}
    copy = {"id": "00002.mpls", "role": Role.DUPLICATE, "duration": 7200}
    assert app.App.credits_titles(dict(rep, titles=[extra, copy])) == []
    # (each title of type MainMovie: yours too, whatever the scan read it as)
    both = dict(rep, titles=[feature, extra, copy], labels={"00001.mpls": {"type": "MainMovie"}})
    assert app.App.credits_titles(both) == [feature, extra]


def test_credits_of_several_films_together():
    """Several titles' credits: the same film named (its cuts) is that film; different films, no winner, both
    close."""
    from disc_detective import app
    a = {"winner": {"imdb_id": "tt1", "name": "A"}, "window": [{"imdb_id": "tt1"}], "by": "the MPA card"}
    b = {"winner": {"imdb_id": "tt2", "name": "B"}, "window": [{"imdb_id": "tt2"}], "by": "the end credits"}
    none_a = {"winner": None, "window": [{"imdb_id": "tt3"}, {"imdb_id": "tt1"}]}
    assert app.credits_together([a]) is a
    assert app.credits_together([a, dict(a, by="the end credits")])["winner"]["imdb_id"] == "tt1"
    assert app.credits_together([none_a, a])["winner"]["imdb_id"] == "tt1"
    two = app.credits_together([a, b])
    assert two["winner"] is None and [f["imdb_id"] for f in two["window"]] == ["tt1", "tt2"]


def test_credits_seen_or_none():
    """Credits seen, film named or not: a rating card, an end crawl, a heading, or a few people read. None of
    them (a sign naming one person at most): no credits."""
    sc = credits.Scan.__new__(credits.Scan)
    row = lambda text, got=(), head=None: {"text": text, "got": list(got), "head": head, "person": bool(got)}
    sc.crawl_at, sc.frames = None, [{"rows": [row("Welcome to Springfield"), row("Mayor Joe Quimby", ["joe quimby"]),
                                              row("char mayor", ["char mayor"])]}]
    assert not sc.seen(None)["any"]
    assert sc.seen(12.0)["any"] and sc.seen(12.0)["card"]
    sc.crawl_at = 6100.0
    assert sc.seen(None)["any"]
    sc.crawl_at = None
    sc.frames.append({"rows": [row("Burl Ives", ["burl ives"]), row("Romeo Muller", ["romeo muller"])]})
    assert sc.seen(None)["names"] == 3 and sc.seen(None)["any"]
    sc.frames = [{"rows": [row("Directed by", head="director")]}]
    assert sc.seen(None)["any"]


def test_stacked_names_on_a_card():
    """A card's name over two lines (ROMEO / MULLER) is tried as one too, within its own block; a heading line
    isn't joined to a name."""
    blocks = [[("WRITTEN BY", (0, 0, 9, 1), 0.04, 0.99), ("ROMEO", (0, 1, 9, 2), 0.06, 0.99), ("MULLER", (0, 2, 9, 3), 0.06, 0.99)],
              [("AND THE", (20, 5, 29, 6), 0.04, 0.99), ("SONG BY", (20, 6, 29, 7), 0.04, 0.99),
               ("JOHNNY", (20, 7, 29, 8), 0.06, 0.99), ("MARKS", (20, 8, 29, 9), 0.06, 0.99)]]
    rows = [t for t, _ in credits.card_rows(blocks)]
    assert "ROMEO MULLER" in rows and "JOHNNY MARKS" in rows
    assert "WRITTEN BY ROMEO" not in rows and "ROMEO JOHNNY" not in rows


def test_title_on_a_card():
    """The biggest held lettering is the title, its small joining words kept, a smaller studio line not."""
    blocks = [[("RUDOLPH", (0, 0, 9, 3), 0.175, 0.92), ("THE", (3, 3, 6, 4), 0.06, 1.0), ("RED-NOSED", (0, 4, 9, 6), 0.117, 0.99),
               ("REINDEER", (0, 6, 9, 8), 0.104, 0.9), ("VIDEOCRAFT INTL. LTD.", (0, 8, 9, 9), 0.066, 0.85)]]
    assert credits.title_lines(blocks) == ("RUDOLPH THE RED-NOSED REINDEER", 0.175)
    assert credits.title_lines([[("n", (0, 0, 1, 1), 0.3, 0.6)]]) == (None, 0.0)          # (a shape, not sure)
    assert credits.title_lines([[("Directed by", (0, 0, 9, 1), 0.03, 0.99)]]) == (None, 0.0)   # (a credit's size)


def test_side_by_side_words_on_a_card():
    """A hand-lettered name read a word at a time, each word its own block (FRED, ASTAIRE), is tried as one row
    too, after its words; stacked lines stay as they were."""
    blocks = [[("TOLD AND SUNG", (380, 440, 1310, 760), 0.1, 0.99), ("By", (690, 705, 835, 820), 0.06, 0.99)],
              [("FRED", (390, 868, 870, 1113), 0.17, 0.99)], [("ASTAIRE", (826, 830, 1490, 1150), 0.2, 0.99)]]
    rows = [t for t, _ in credits.card_rows(blocks)]
    assert rows.index("FRED ASTAIRE") > rows.index("ASTAIRE")
    assert "TOLD AND SUNG By" in rows and "By FRED" not in rows


def test_one_unlock_a_scan(monkeypatch):
    """A Blu-ray's opened (unlocked) once a scan, its menus and its credits sharing it; another disc, or outside a
    scan, opened afresh."""
    import hdmv_menus
    from disc_detective import app as app_mod
    opened = []
    monkeypatch.setattr(hdmv_menus, "Disc", lambda root, aacs=None: opened.append(root) or object())
    from disc_detective import bdhdmv
    monkeypatch.setattr(bdhdmv, "aacs_library", lambda: None)
    a = app_mod.App.__new__(app_mod.App)
    rep = {"path": "E:\\", "content_hash": "AAAA"}
    a._bd_open = {}
    first = a.bd_disc(rep)
    assert a.bd_disc(rep) is first and opened == ["E:\\"]
    assert a.bd_disc({"path": "E:\\", "content_hash": "BBBB"}) is not first and len(opened) == 2
    a._bd_open = None
    a.bd_disc(rep)
    a.bd_disc(rep)
    assert len(opened) == 4
