"""The credits witness's reading rules (credits.py): years and certificate numbers off a film's cards, the close
window, the number's pick, and the MPA numbers' table against a small IMDb index of its own."""
import collections
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
    sc.why_of = {"tt0000001": ["ann one", "bob two", "cat three"], "tt0000002": ["ann one"]}
    assert [x[2] for x in sc.window_of(r)] == ["A"]                       # (a clear lead)
    r = [(10.0, 5, "A", 1990, "movie", 90, "tt0000001"), (7.0, 4, "B", 1991, "movie", 90, "tt0000002")]
    assert [x[2] for x in sc.window_of(r)] == ["A", "B"]                  # (close: both)
    # (a clear lead on two names another film has too: more evidence needed)
    r = [(10.0, 2, "A", 1990, "movie", 90, "tt0000001"), (3.0, 2, "C", 1992, "movie", 90, "tt0000003")]
    sc.why_of = {"tt0000001": ["ann one", "bob two"], "tt0000003": ["ann one", "bob two"]}
    assert [x[2] for x in sc.window_of(r)] == ["A", "C"]
    # (the same with its title read and a guess named so: the title the witness the others lack)
    sc.why_of["tt0000001"] = ["ann one", "bob two", "title a"]
    assert [x[2] for x in sc.window_of(r)] == ["A"]
    # (two films of one name: each its own evidence)
    r = [(10.0, 2, "A", 1990, "movie", 90, "tt0000001"), (3.0, 1, "A", 1998, "movie", 90, "tt0000004")]
    sc.why_of["tt0000004"] = ["ann one"]
    assert [x[2] for x in sc.window_of(r)] == ["A"]


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
    assert wanted(dict(rep, kind="tv"))                        # (a show nothing named: its first episode's)
    assert not wanted(dict(rep, kind="tv", candidates=[_cand(1, "A", 80)]))
    # (one film named for sure, another film's worth of video beside it: read for the other)
    assert wanted(dict(rep, candidates=[_cand(1, "A", 80)], films_apart=2))
    assert not wanted(dict(rep, candidates=[_cand(1, "A", 80)], films_apart=2, kind="tv"))
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


def _film(n, name, runtime, typ="movie"):
    return {"imdb_id": "tt%07d" % n, "name": name, "year": 1941, "type": typ, "runtime": runtime, "score": 9.0,
            "names": 5, "by": "the end credits"}


def test_credits_read_for_the_other_film_and_a_shows_first_episode(monkeypatch):
    """One film named for sure beside another film's worth of video: the other title's credits read, not the named
    one's. A show's disc: its first episode only."""
    from disc_detective import app, core
    from disc_detective.kinds import Role
    navy = {"id": "Title 1", "role": Role.FEATURE, "duration": 85.5 * 60}
    ghost = {"id": "Title 2", "role": Role.EXTRA, "duration": 85.9 * 60}
    monkeypatch.setattr(core, "apart_titles", lambda titles: [t for t in titles if t["duration"] >= 40 * 60])
    top = dict(_cand(1, "In the Navy", 80), runtime=86)
    rep = {"kind": "movie", "titles": [navy, ghost], "candidates": [top], "films_apart": 2, "discdb": {"hit": None}}
    assert app.App.credits_titles(rep) == [navy, ghost]               # (both 86 min: which is In the Navy's, unknown)
    short = dict(ghost, duration=70 * 60)                             # (only one as long as it: that one skipped)
    assert app.App.credits_titles(dict(rep, titles=[navy, short])) == [short]
    assert app.App.credits_titles(dict(rep, films_apart=1)) == [navy]
    # (the report's titles have no segments: which are their own video, as the scan kept them)
    monkeypatch.setattr(core, "apart_titles", lambda titles: [])
    doc = {"id": "Title 3", "role": Role.EXTRA, "duration": 51 * 60}
    got = app.App.credits_titles(dict(rep, titles=[navy, doc], apart_ids=["Title 1", "Title 3"]))
    assert got == [doc]                                               # (In the Navy the only one its length fits)
    # (the film's own titles read first, a title it may be after last: whatever their disc order)
    early = dict(doc, id="Title 0")
    got = app.App.credits_titles(dict(rep, titles=[early, navy], apart_ids=["Title 0", "Title 1"], films_apart=2,
                                      candidates=[_cand(1, "A", 50)]))
    assert got == [navy, early]
    e1 = {"id": "Title 2", "role": Role.EPISODE, "duration": 42 * 60}
    e2 = {"id": "Title 3", "role": Role.EPISODE, "duration": 41 * 60}
    assert app.App.credits_titles({"kind": "tv", "titles": [e1, e2], "discdb": {"hit": None}}) == [e1]


def test_a_second_film_named_by_its_own_credits(monkeypatch):
    """The scan's film for one title and another film its credits name for the other: the disc's two films. A
    documentary or video its credits name isn't a second film (Rudolph's); one film on titles of two lengths: its
    cuts."""
    from disc_detective import core
    from disc_detective.kinds import Role
    navy = {"id": "Title 1", "role": Role.FEATURE, "duration": 85.5 * 60}
    ghost = {"id": "Title 2", "role": Role.EXTRA, "duration": 85.9 * 60}
    monkeypatch.setattr(core, "apart_titles", lambda titles: list(titles))
    top = dict(_cand(1, "In the Navy", 80), runtime=86)
    per = {"Title 2": {"winner": _film(2, "Hold That Ghost", 86)}}
    df = core.credits_films([navy, ghost], per, [top])
    assert df and df["kind"] == "collection" and [i["name"] for i in df["items"]] == ["In the Navy", "Hold That Ghost"]
    assert [i["title_id"] for i in df["items"]] == ["Title 1", "Title 2"]
    doc = {"Title 2": {"winner": _film(3, "The Animagic World of Rankin/Bass", 47, "video")}}
    assert core.credits_films([navy, ghost], doc, [top]) is None
    far = {"Title 2": {"winner": _film(2, "Hold That Ghost", 120)}}           # (not as long as its title)
    assert core.credits_films([navy, ghost], far, [top]) is None
    legend = {"Title 1": {"winner": _film(4, "Legend", 89)}, "Title 2": {"winner": _film(4, "Legend", 89)}}
    cut = {"id": "Title 2", "role": Role.VERSION, "duration": 113.5 * 60}
    assert core.credits_cuts([navy, cut], legend) == {"imdb_id": "tt0000004", "name": "Legend",
                                                       "titles": ["Title 1", "Title 2"]}
    assert core.credits_films([navy, cut], legend, []) is None


def test_an_episode_looks_for_a_show():
    """An episode's credits: a series is what's looked for (films weighed down), guests count little, "Special
    Guest Star" is a heading."""
    s = credits.Scan(None, {})
    assert s.low_kind("tvSeries", "Buffy") and not s.low_kind("movie", "In the Navy")
    s.want = "episode"
    assert not s.low_kind("tvSeries", "Smallville") and s.low_kind("movie", "In the Navy")
    assert credits.head_of(credits.norm("Special Guest Star")) == "guest"
    assert credits.head_of(credits.norm("Guest Starring")) == "guest"
    assert credits.head_of(credits.norm("STARRING")) == "cast"

    class Ix:
        def roles(self, p):
            return {7: {"actor"}}, {"actor"}
    s.ix, s.cast_pts = Ix(), {}
    assert s.credit_points([1], "cast") == {7: credits.ROLE_MATCH}
    assert s.credit_points([1], "guest") == {7: credits.ROLE_MATCH * credits.GUEST_W}


def test_an_episode_reads_its_opening_first(monkeypatch):
    """An episode: no MPA card, the opening first; a show named there, the end credits never read."""
    s = credits.Scan(None, {})
    calls = []
    monkeypatch.setattr(s, "card_read", lambda: calls.append("card") or (None, {}, [], None))
    win = (9.0, 8, "Smallville", 2001, "tvSeries", 44, "tt0279600")
    monkeypatch.setattr(s, "front_scan", lambda: calls.append("front") or ([win], [win], "the cards are over"))
    monkeypatch.setattr(s, "end_scan", lambda: calls.append("end") or ([], [], "no end credits"))
    monkeypatch.setattr(s, "seen", lambda card_at: {"any": True})
    monkeypatch.setattr(s, "title_readings", lambda: [])
    monkeypatch.setattr(s, "people_of", lambda x: [1] * 8)
    src = type("Src", (), {"grabs": 0})()
    r = s.scan(src, want="episode")
    assert calls == ["front"] and r["winner"]["name"] == "Smallville" and r["by"] == "the opening credits"


def _titled(titles, people=()):
    s = credits.Scan(None, {})
    s.ix = type("Ix", (), {"person": lambda self, w: " ".join(w) in people})()
    s.titles = [list(x) for x in titles]
    return s


def test_a_title_seen_once_looked_at_again(monkeypatch):
    """Big lettering read in one frame: its card read again either side; a re-read like it counts as held, and the
    fuller reading is kept (A Charlie Brown Christmas' title card, animating on and off)."""
    s = _titled([[0.29, 132.0, "CHARLIE hristmas", 1], [0.165, 302.0, "THE DOCTOR", 2]])
    reads = {131.0: ("A CHARLIE BROWN Christmas", 0.3), 131.5: (None, 0.0), 132.5: ("CHARLIE BROWN hristmas", 0.29),
             133.0: ("w", 0.1)}
    asked = []
    s.src = type("Src", (), {"frame": lambda self, t: asked.append(t) or t})()
    monkeypatch.setattr(credits, "card_blocks", lambda f: f)
    monkeypatch.setattr(credits, "title_lines", lambda t: reads.get(t, (None, 0.0)))
    s.second_look()
    assert asked == []                         # (like no guess, no film found for it: not a title, no time spent)
    s.candidates = [{"imdb_id": "tt0059026", "name": "A Charlie Brown Christmas"}]
    s.second_look()
    assert asked == [131.0, 131.5, 132.5, 133.0]                    # (only the once-read one: THE DOCTOR's held)
    assert s.titles[0][2] == "A CHARLIE BROWN Christmas" and s.titles[0][3] == 3
    assert s.title_readings()[0] == "A CHARLIE BROWN Christmas"


def test_titles_among_credits_far_bigger_no_studio_no_billing():
    """A title card sits among the credits (lettering near a card of people weighed up, a scene's headline not); one
    read once but far bigger than the rest, in the opening's first half, still counts; a studio's name (misread too)
    and a billing card are no titles, a name inside a title is."""
    s = _titled([[0.183, 178.0, "RUDOLPH THE RED-NOSED REINDEER", 4], [0.206, 10.0, "We're FrOzen", 3]])
    s.frames = [{"t": "0:02:50", "rows": [{"person": True}]}]
    assert s.title_readings()[0] == "RUDOLPH THE RED-NOSED REINDEER"
    s = _titled([[0.183, 178.0, "RUDOLPH THE RED-NOSED REINDEER", 4, 1], [0.206, 10.0, "We're FrOzen", 3, 7]])
    assert s.title_readings()[0] == "RUDOLPH THE RED-NOSED REINDEER"  # (the headline in a page of 7 other lines)
    assert credits.title_lines([[("RUDOLPH", (0, 0, 9, 2), 0.175, 0.95), ("STUDIO", (0, 2, 9, 3), 0.06, 0.9)],
                                [("a", (0, 5, 1, 6), 0.01, 0.9)]], others=True) == ("RUDOLPH", 0.175, 2, 1.0, 0.06)
    s = _titled([[0.30, 40.0, "Little Giant", 1], [0.12, 200.0, "MAIN STREET", 2]])
    assert "Little Giant" in s.title_readings()                     # (2.5 times bigger, early: kept, the one
                                                                    #  held longer first -- sizes not compared)
    s = _titled([[0.30, 250.0, "Little Giant", 1], [0.12, 200.0, "MAIN STREET", 2]])
    assert s.title_readings() == ["MAIN STREET"]                     # (late: read once isn't enough)
    s = _titled([[0.58, 18.0, "UNIVERSA", 3], [0.3, 10.0, "BuenaVista", 2], [0.25, 12.0, "Walt Dianey", 2],
                 [0.2, 20.0, "Peter Pan", 2]])
    assert s.title_readings() == ["Peter Pan"]
    s = _titled([[0.3, 30.0, "EFREM ZIMBALIST,JR. as SAM", 3], [0.29, 132.0, "CHARLIE BROWN hrisimas", 5]],
                people={"efrem zimbalist", "charlie brown"})
    assert s.title_readings() == ["CHARLIE BROWN hrisimas"]
    s = _titled([[0.3, 60.0, "DICK FORAN BUTCH AND BUDDY SHEMP HOWARD CONDOS BROTHERS", 3],
                 [0.3, 30.0, "EFREM ZIMBALIST,JR. as SAM", 3], [0.2, 20.0, "IN THE NAVY", 2]],
                people={"dick foran", "shemp howard", "efrem zimbalist jr"})
    assert s.title_readings() == ["IN THE NAVY"]                     # (two people on a card, a name with its "Jr.")
    assert credits.alike("CHARLIE hristmas", "A CHARLIE BROWN Christmas") >= credits.TITLE_LIKE
    assert credits.alike("THE DOCTOR", "A CHARLIE BROWN Christmas") < credits.TITLE_LIKE
    assert credits.alike("w", "A CHARLIE BROWN Christmas") == 0.0


def test_title_signs_sentences_and_credit_text_near():
    """A sentence (a narration's line) is weighed down below a title; a title counts as credits seen with credit
    text near it, or when it named a guess -- a magazine in a scene, far from any name, doesn't."""
    assert credits.sentence("recounts His ministry among the inhabitants of ancient America.")
    assert credits.sentence("anything about our hidden fr")
    assert not credits.sentence("OF ONE FOLDAND ONE SHEPHERD") and not credits.sentence("A Court Jester")
    assert not credits.sentence("Abbott and Costello Meet Dr. Jekyll and Mr. Hyde")
    s = _titled([[0.11, 84.0, "OF ONE FOLDAND ONE SHEPHERD", 2], [0.08, 24.0, "recounts His ministry among the inhabitants", 5]])
    s.frames = [{"t": "0:00:26", "rows": [{"head": "narrator"}]}]
    assert s.title_readings()[0] == "OF ONE FOLDAND ONE SHEPHERD"
    s = _titled([[0.19, 42.0, "A Gvand Day Out", 2], [0.40, 64.0, "RAVEL Magagine", 3, 0, 0.7]])
    s.frames = [{"t": "0:00:38", "rows": [{"person": True}]}]
    assert s.title_readings()[0] == "A Gvand Day Out" and s.titled()
    s = _titled([[0.14, 302.0, "PSYCHIATRIC HELP 5", 6, 2, 1.0, 0.13], [0.29, 132.0, "CHARLIE BROWN hrisimas", 2, 0, 1.0, 0.0],
                 [0.17, 174.0, "Wolfgang Reitherman", 8, 2, 1.0, 0.16]])
    assert s.title_readings()[0] == "CHARLIE BROWN hrisimas"      # (alone on its screen; a sign, a director's card
                                                                 #  among lettering their size, held longer)
    s = _titled([[0.40, 64.0, "RAVEL Magagine", 3, 0, 0.7]])
    s.frames = [{"t": "0:00:38", "rows": [{"person": True}]}]
    assert not s.titled()
    s.boosted = {58536: 0.0}
    assert s.titled()


NAMED = [(1, "Wait Until Dark", None, "movie", 108, 37000), (2, "Dark", None, "movie", 108, 1300),
         (3, "Rear Window", None, "movie", 112, 570000), (4, "A Charlie Brown Christmas", None, "tvMovie", 25, 51000),
         (5, "Charlie", None, "movie", 25, 60), (6, "Wallace & Gromit: A Grand Day Out", "A Grand Day Out", "short", 23,
         42000), (7, "Peter Pan", None, "movie", 77, 168000), (8, "Arsenic and Old Lace", None, "movie", 118, 78000),
         (9, "Results", None, "movie", 110, 8000), (10, "Help", None, "tvSeries", 25, 900),
         (11, "Wallace & Gromit: A Close Shave", None, "short", 30, 48000),
         (12, "Timmy's Gift: A Precious Moments Christmas", None, "video", 23, 120),
         (13, "Cricket on the Hearth", None, "tvMovie", 60, 400), (14, "Hearth and Home", None, "movie", 60, 900)]


@pytest.fixture(scope="module")
def named(tmp_path_factory):
    db = sqlite3.connect(str(tmp_path_factory.mktemp("named") / "imdb.sqlite"))
    db.executescript("CREATE TABLE titles (id INTEGER PRIMARY KEY, type TEXT, title TEXT, year INT, end_year INT, "
                     "runtime INT, norm TEXT, alt TEXT); CREATE TABLE ratings (id INTEGER PRIMARY KEY, votes INT);"
                     "CREATE VIRTUAL TABLE titles_fts USING fts5(title, alt, content='titles', content_rowid='id');")
    rows = list(NAMED)
    words = {w for r in NAMED for w in credits.norm(r[1])}       # (each word in 3 titles: the index's title words)
    for k, w in enumerate(sorted(words)):
        rows += [(100 + 3 * k + i, "%s Story %d" % (w.capitalize(), i), None, "movie", 500, 5) for i in range(3)]
    for i, title, alt, typ, rt, votes in rows:
        db.execute("INSERT INTO titles VALUES (?, ?, ?, 2000, NULL, ?, '', ?)", (i, typ, title, rt, alt))
        db.execute("INSERT INTO ratings VALUES (?, ?)", (i, votes))
    db.execute("INSERT INTO titles_fts(titles_fts) VALUES('rebuild')")
    db.commit()
    yield db
    db.close()


def test_films_named_by_a_title_misread(named):
    """A title read names films as the end credits' names do: its words in order, a slip allowed, a word read as two
    run together, a possessive credit in front, either side of a name's colon; as long as the title; the reading's
    other words count against a film too (Charlie, 2015, isn't "CHARLIE hristmas")."""
    first = lambda text, m: (credits.films_named(named, text, m) or [(0, None)])[0][1]
    assert first("UNTL DARK", 108) == 1                              # (Wait Until Dark, not Dark: both ways)
    assert first("ALFRED HITCHCOCK'S REAR WINDOW", 112) == 3
    assert first("A Gvand Day Out", 23) == 6                         # (its other title; the slip)
    assert first("PeierPan", 77) == 7                                # (two words run together, one slipped)
    assert first('Capra\'s "ARSENIC AND OLDLACE"', 118) == 8
    assert first("CHARLIE hristmas", 25) == 4
    assert first("Rear Window", 30) is None                          # (not the title's length)
    assert credits.films_named(named, "PSYCHIATRIC HELP 5", 25) == []   # (a sign: no film; a series isn't one)
    assert first("HELP", 25) is None
    assert credits.films_named(named, "HELP", 25, credits.TITLE_SERIES)[0][1] == 10
    # (a side of a name's colon alone: only with the rest of the name read elsewhere -- a slip allowed)
    assert first("CLOSE SHAVE", 30) == 11
    alone = credits.films_named(named, "CLOSE SHAVE", 30, seen=set())          # (its whole name half read: weakly)
    assert alone[0][1] == 11 and alone[0][0] < 0.7
    assert credits.films_named(named, "CLOSE SHAVE", 30, seen={"with", "wallace", "gromlt"})[0] == (1.0, 11)
    assert credits.films_named(named, "Timmy's Gift", 23, seen={"timmy", "gift"}) == []
    assert credits.films_named(named, "Timmy's Gift", 23, seen={"precious", "moments", "christmas"})[0][1] == 12
    # (a TV movie's IMDb runtime, its broadcast slot: the file up to a quarter shorter, never longer; a film's not)
    assert first("GRICKET On The HEARTH", 49.5) == 13
    assert credits.films_named(named, "HEARTH AND HOME", 49.5) == []
    assert credits.runtime_fits(60, 49.5, "tvMovie") and not credits.runtime_fits(60, 49.5, "movie")
    assert not credits.runtime_fits(60, 44, "tvSpecial") and not credits.runtime_fits(40, 49.5, "tvMovie")
    assert credits.name_score(credits.norm("CHARLIE hristmas"), credits.norm("A Charlie Brown Christmas")) > \
        credits.name_score(credits.norm("CHARLIE hristmas"), credits.norm("Charlie"))
    # (a title read is all or part of a real title: a word between the name's words that it lacks, no; most of the
    #  reading outside the name, no; billing before it or text after, only weighed against it)
    sc = lambda r, n: credits.name_score(credits.norm(r), credits.norm(n))
    assert sc("CHARLIE BROWN hrisimas", "Charlie's Christmas") == 0.0
    assert sc("CHARLIE BROWN hrisimas", "The Making of 'A Charlie Brown Christmas'") >= credits.TITLE_FOUND
    assert sc("THE DOCTOR REALIN", "Oh Doctor!") == 0.0
    assert sc("PeterPan Presents", "Peter Pan") >= credits.TITLE_FOUND
    assert sc("Buck Privates Come Home BEDDOE", "Buck Privates Come Home") >= credits.TITLE_FOUND


def test_titles_give_the_films_they_name_evidence(monkeypatch):
    """Each title read gives the films it names points by how well (TITLE_BOOST at best), a sign in a scene less; a
    film's best once; a guess it names the whole boost; the list is those films' names, the most evidence first."""
    found = {"UNTL DARK": [(1, 1000, [(0.82, ())]), (2, 1000, [(0.67, ())])],
             "WAIT UNTIL DARK": [(1, 1000, [(1.0, ())])], "PSYCHIATRIC HELP 5": []}
    monkeypatch.setattr(credits, "name_scores", lambda con, text, m, types=None: found.get(text, []))
    s = _titled([[0.2, 30.0, "UNTL DARK", 2, 0, 1.0, 0.0], [0.2, 40.0, "PSYCHIATRIC HELP 5", 3, 5, 0.7, 0.1]])
    s.ix.ti = None
    s.ix.title = lambda n: {1: ("Wait Until Dark", 1967, "movie", 108), 2: ("Dark", 2026, "movie", 108)}[n]
    s.ix.votes = lambda n: 1000
    s.src = type("Src", (), {"dur": 108 * 60})()
    s.want, s.candidates, s.boosted, s.title_pts, s._found, s.disc_words = "film", [], {}, {}, {}, frozenset()
    s.cast, s.log = collections.Counter(), lambda m: None
    s.frames = [{"t": "0:00:40", "rows": [{"person": True, "text": "ALAN ARKIN"}]}]   # (credit text near both)
    score, why = collections.Counter(), collections.defaultdict(set)
    assert sorted(s.title_evidence(score, why, 30.0)) == [1, 2]
    assert round(score[1], 2) == round(3 * 0.82, 2) and round(s.cast[2], 2) == round(3 * 0.67 / 2 ** 0.5, 2)   # (Dark: as well named as one more, at best)
    assert s.boosted == {1: 30.0, 2: 30.0} and "title untl dark" in why[1]
    assert s.title_evidence(score, why, 34.0) == [] and round(score[1], 2) == 2.46   # (given once)
    s.titles.append([0.2, 50.0, "WAIT UNTIL DARK", 2, 0, 1.0, 0.0])
    s.title_evidence(score, why, 50.0)
    assert round(score[1], 2) == 3.0 and s.boosted[1] == 30.0        # (read better: only what's new added)
    assert s.title_phrases() == ["Wait Until Dark", "Dark"] and s.titled()
    s.titles[-1][4] = 6                                              # (the same in a busy scene: weighed down)
    s.title_pts, s._weighed, score = {}, set(), collections.Counter()
    s.title_evidence(score, why, 50.0)
    assert round(score[1], 2) == round(3 * 0.82, 2)


def test_a_title_named_like_a_guess_boosts_its_film():
    """The opening's title held and named like one guess alone, whole name to whole name and as long as the title:
    that film's (boosted; the names still to agree). A part of a name, a film of another length, one frame, a guess
    of one name the length doesn't pick: none."""
    assert credits.title_like("RUDALPH THE RED-NOSED REINDEER", "Rudolph the Red-Nosed Reindeer") >= credits.TITLE_MATCH
    assert credits.title_like("RUDOLPH", "Rudolph's Shiny New Year") < credits.TITLE_MATCH
    rudolph = {"imdb_id": "tt0058536", "name": "Rudolph the Red-Nosed Reindeer", "year": 1964, "runtime": 47}
    shiny = {"imdb_id": "tt0073640", "name": "Rudolph's Shiny New Year", "year": 1976, "runtime": 50}
    s = _titled([[0.17, 40.0, "RUDALPH THE RED-NOSED REINDEER", 2, 1]])
    s.candidates, s.src, s.years = [shiny, rudolph], type("Src", (), {"dur": 47 * 60})(), {}
    assert s.title_matches() == {58536: "RUDALPH THE RED-NOSED REINDEER"}
    s.titles[0][3] = 1                                              # (one frame: not enough)
    assert s.title_matches() == {}
    s.titles[0][3] = 2
    s.src.dur = 100 * 60                                            # (a title twice as long isn't it)
    assert s.title_matches() == {}
    s.src.dur = 85 * 60                                             # (two films of one name, neither by length)
    s.candidates = [dict(rudolph, imdb_id="tt1", runtime=None), dict(rudolph, imdb_id="tt2", runtime=None, year=1990)]
    assert s.title_matches() == {}
    s.years = {1990: 1.0}                                           # (its copyright year picks one)
    assert s.title_matches() == {2: "RUDALPH THE RED-NOSED REINDEER"}
    s.src.dur, s.years = 38 * 60, {}                                # (a TV special's IMDb runtime its broadcast
    s.candidates = [dict(rudolph, type="tvMovie")]                  #  slot: the file shorter fits; a film's not)
    assert s.title_matches() == {58536: "RUDALPH THE RED-NOSED REINDEER"}
    s.candidates = [dict(rudolph, type="movie")]
    assert s.title_matches() == {}


def test_a_short_or_an_old_film_reads_its_opening_first(monkeypatch):
    """A short title, or a likeliest guess before 1968: the opening first, then the end, the MPA card last."""
    for dur, year in ((47 * 60, 2000), (95 * 60, 1941)):
        s = credits.Scan(None, {})
        calls = []
        monkeypatch.setattr(s, "card_read", lambda: calls.append("card") or (None, {}, [], None))
        monkeypatch.setattr(s, "front_scan", lambda: calls.append("front") or ([], [], "the cap"))
        monkeypatch.setattr(s, "end_scan", lambda: calls.append("end") or ([], [], "no end credits"))
        monkeypatch.setattr(s, "seen", lambda card_at: {"any": False})
        monkeypatch.setattr(s, "title_readings", lambda: [])
        src = type("Src", (), {"grabs": 0, "dur": dur})()
        s.scan(src, candidates=[{"imdb_id": "tt0000001", "name": "A", "year": year, "runtime": 95}])
        assert calls == ["front", "end", "card"]
    s = credits.Scan(None, {})
    calls = []
    monkeypatch.setattr(s, "card_read", lambda: calls.append("card") or (None, {}, [], None))
    monkeypatch.setattr(s, "front_scan", lambda: calls.append("front") or ([], [], "the cap"))
    monkeypatch.setattr(s, "end_scan", lambda: calls.append("end") or ([], [], "no end credits"))
    monkeypatch.setattr(s, "seen", lambda card_at: {"any": False})
    monkeypatch.setattr(s, "title_readings", lambda: [])
    s.scan(type("Src", (), {"grabs": 0, "dur": 95 * 60})(), candidates=[{"imdb_id": "tt1", "name": "A", "year": 1999}])
    assert calls == ["card", "end", "front"]


def test_credits_guesses():
    """The credits are handed the scan's likeliest films, a collection's own among them; a show's on a TV disc."""
    from disc_detective import app
    navy = {"imdb_id": "tt0033754", "name": "In the Navy", "year": 1941, "runtime": 86}
    ghost = {"imdb_id": "tt0033723", "name": "Hold That Ghost", "year": 1941, "runtime": 86}
    rep = {"kind": "movie", "candidates": [{"kind": "collection", "name": "x", "items": [navy, ghost]},
                                           dict(navy, kind="movie", score=80), {"kind": "tv", "imdb_id": "tt9", "name": "Show"}]}
    assert app.credits_guesses(rep) == [navy, ghost]
    assert app.credits_guesses(dict(rep, kind="tv")) == [{"imdb_id": "tt9", "name": "Show", "year": None, "runtime": None}]


def test_a_series_prefix_set_aside_when_the_disc_shows_it():
    """A guess's name before its colon ("Wallace & Gromit: A Close Shave") is set aside only when the disc's own
    words or its opening show it: "CLOSE SHAVE" matches then, not otherwise."""
    shave = {"imdb_id": "tt0112691", "name": "Wallace & Gromit: A Close Shave", "year": 1995, "runtime": 30}
    s = _titled([[0.2, 30.0, "CLOSE SHAVE", 3]])
    s.candidates, s.src, s.years, s.frames = [shave], type("Src", (), {"dur": 30 * 60})(), {}, []
    assert s.title_matches() == {}
    s.disc_words = frozenset({"wallace", "gromit"})                 # (its label: WALLACE_AND_GROMIT)
    assert s.title_matches() == {112691: "CLOSE SHAVE"}
    s.disc_words = frozenset()
    s.frames = [{"t": "0:00:10", "rows": [{"text": "WALLACE & GROMIT"}]}]   # (or its opening says so)
    assert s.title_matches() == {112691: "CLOSE SHAVE"}


def test_a_title_held_is_credits_seen():
    """A title with no card, no crawl, no heading and a name or two, but its title held on screen (a short of crew-only
    cards): credits seen, not "most likely an extra"."""
    s = _titled([])
    s.frames, s.crawl_at = [{"t": "0:00:30", "rows": [{"text": "X", "got": ["nick park"], "person": True, "head": None}]}], None
    assert s.seen(None)["any"] is False
    s.titles = [[0.3, 20.0, "CLOSE SHAVE", 3]]
    seen = s.seen(None)
    assert seen["title"] and seen["any"]
