"""The DVD command engine: single commands (built from the spec's bit layout), and a little
made-up disc run from its first-play program to a button press."""
import pytest

from disc_detective import dvdvm


def player():
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles, disc.ptt, disc.title_pgcs, disc.menus, disc.buttons, disc.first_play = [], {}, {}, {}, {}, None
    return dvdvm.Player(disc)


def run(p, *hexes):
    return p.run([bytes.fromhex(h) for h in hexes])


def test_bits_count_from_the_top_bit_down():
    cmd = bytes.fromhex("3005000200010000")
    assert dvdvm.bits(cmd, 63, 3) == 1 and dvdvm.bits(cmd, 22, 7) == 1 and dvdvm.bits(cmd, 41, 10) == 2


def test_commands():
    p = player()
    assert run(p, "7100000400020000") is None and p.gprm[4] == 2              # set GPRM4 = 2
    assert run(p, "2004000000000005") == {"op": "pgcn", "n": 5}               # link to PGC 5
    assert run(p, "20A4000400020007") == {"op": "pgcn", "n": 7}               # if GPRM4 == 2: link PGC 7
    p.gprm[4] = 3
    assert run(p, "20A4000400020007") is None
    # goto line 3 skips the line that would set GPRM1
    assert run(p, "0001000000000003", "7100000100090000", "7100000200010000") is None
    assert p.gprm[1] == 0 and p.gprm[2] == 1
    assert run(p, "3005000200010000") == {"op": "jump_vts_ptt", "ttn": 1, "ptt": 2}
    assert run(p, "3008000000830000")["op"] == "vtsm"                         # call: the root menu
    with pytest.raises(dvdvm.Unsure):
        run(p, "7800000100050000")                                          # a random number


def pgc(seconds=0.0, pre=(), post=(), cells=1, programs=1):
    return {"pre": [bytes.fromhex(h) for h in pre], "post": [bytes.fromhex(h) for h in post], "cell_cmds": [],
            "cells": [{"first": 0, "last": 0, "still": 0, "cmd": 0}] * cells, "programs": programs,
            "program_cells": list(range(1, programs + 1)), "next": 0, "prev": 0, "up": 0, "seconds": seconds}


def test_a_disc_from_first_play_to_a_button():
    """First play: a 12-second warning (title 1), which ends in the root menu; its button 1 jumps
    to chapter 2 of the feature (title 2), button 2 sets a register and goes to page 2, whose pre
    commands read it."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles = [{"title": 1, "vts": 1, "ttn": 1}, {"title": 2, "vts": 1, "ttn": 2}]
    disc.first_play = pgc(pre=["3002000000010000"])                         # JumpTT 1
    disc.title_pgcs = {1: {1: pgc(12, post=["3008000000830000"]),            # then: CallSS root menu
                           2: pgc(6000, cells=2, programs=2)}}
    disc.ptt = {1: {1: [(1, 1)], 2: [(2, 1), (2, 2)]}}
    disc.menus = {"vmgm": {}, 1: {1: ("root", True, pgc()), 2: ("chapter", False, pgc(pre=["20A4000100070004"]))}}
    btn = lambda n, raw: {"n": n, "raw": raw, "rect": [0, 0, 1, 1], "auto": False, "up": 0, "down": 0,
                          "left": 0, "right": 0, "cmd": {}}
    disc.buttons = {(1, 1): [[btn(1, "3005000200020000"), btn(2, "7100000100070000")]],
                    (1, 2): [[btn(1, "2001000000000001")]], (1, 4): [[btn(1, "2001000000000001")]]}
    # (button 2 of page 1 only sets GPRM1 = 7: it stays on the page)
    eng = dvdvm.Engine(disc)
    p, out = eng.start()
    # (as VLC opens a disc: the clip the first-play program jumps to isn't played; title 1, then
    # the Title menu (none here), then the Root menu)
    assert out["menu"] == (1, 1, 1) and "played" not in out
    assert eng._start_played()[1]["menu"] == (1, 1, 1)                      # (a player: the clip first)
    assert [x["title"] for x in eng._start_played()[1]["played"]] == [1]
    assert dvdvm.answer(eng.press(p, 1)) == {"plays": 2, "chapter": 2, "before": []}
    assert dvdvm.answer(eng.press(p, 2)) == {"stays": True}
    start, found, menus = dvdvm.explore(eng)
    assert start["goes_to"] == (1, 1, 1) and "menu_key" not in start
    assert found[(1, 1, 1, 1)] == [{"plays": 2, "chapter": 2, "before": []}]
    assert start["menu_goes_to"] == (1, 1, 1)                               # Menu: back to its root menu


def test_the_disc_opens_as_vlc_opens_it():
    """Father of the Bride: its start plays a logo, then 6 minutes of trailers; VLC goes from the
    first-play program's settings to title 1 (whose pre commands set GPRM6 = 1, their jump
    ignored), then the Title menu: the main menu, highlighting by GPRM6 (0: button 1, else 2)."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles = [{"title": 1, "vts": 1, "ttn": 1}, {"title": 2, "vts": 1, "ttn": 2}]
    disc.first_play = pgc(pre=["7100000900010000", "3002000000020000"])     # GPRM9 = 1; JumpTT 2
    disc.title_pgcs = {1: {1: pgc(6000, pre=["7100000600010000", "3002000000020000"]),   # GPRM6 = 1; JumpTT 2
                           2: pgc(360, post=["3008000000830000"])}}          # trailers, then the root menu
    disc.ptt = {1: {1: [(1, 1)], 2: [(2, 1)]}}
    # GPRM0 = GPRM6; button 2; GPRM15 = 0; if GPRM15 == GPRM0: button 1
    main = pgc(pre=["6100000000060000", "5600000008000000", "7100000f00000000", "5620000004000f00"])
    disc.menus = {"vmgm": {1: ("title", True, pgc(pre=["2004000000000002"])),     # the title menu: to PGC 2
                           2: ("", False, pgc(pre=["3006000101830000"]))},        # JumpSS the root menu
                  1: {1: ("root", True, main)}}
    btn = lambda n: {"n": n, "raw": "3002000000010000", "rect": [0, 0, 1, 1], "auto": False, "up": 0,
                     "down": 0, "left": 0, "right": 0, "cmd": {}}
    disc.buttons = {(1, 1): [[btn(1), btn(2)]]}
    eng = dvdvm.Engine(disc)
    p, out = eng.start()
    assert out["menu"] == (1, 1, 1) and out["button"] == 2 and "played" not in out
    assert p.gprm[9] == 1 and p.gprm[6] == 1
    # a player, from first play: the trailers, and GPRM6 never set
    p2, out2 = eng._start_played()
    assert out2["button"] == 1 and [x["title"] for x in out2["played"]] == [2]


def test_the_page_left_forces_the_highlight():
    """libdvdnav forces the highlight at a cell change, to the forced button of the video before
    it: Enchanted's Bonus page forces button 1; its Main Menu button asks the main menu for button
    3 (the one last used), and VLC shows 1. The main menu's own forced button counts only at its
    next cell change: it doesn't force anything on arrival."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles, disc.ptt, disc.title_pgcs, disc.first_play = [], {}, {}, None
    disc.menus = {1: {1: ("root", True, pgc(pre=["7500000b04000000", "46000000000b0000"])),   # button = GPRM11
                      2: ("", False, pgc())}}
    btn = lambda n, raw, forced=False: {"n": n, "raw": raw, "rect": [0, 0, 1, 1], "cmd": {}, "forced": forced}
    disc.buttons = {(1, 1): [[btn(1, "2004000000000002", True), btn(2, "2004000000000002"), btn(3, "2004000000000002")]],
                    (1, 2): [[btn(1, "7104000b00030001", True), btn(2, "7104000b00030001")]]}  # GPRM11 = 3; main
    eng = dvdvm.Engine(disc)
    p = dvdvm.Player(disc)
    p.domain, p.pgcn = 1, 2
    page = eng._waits(p, [])["player"]
    assert eng.press(page, 2)["button"] == 1                 # (the page left forces 1)
    page.fosl = 0                                             # a page forcing nothing: the program's 3
    assert eng.press(page, 2)["button"] == 3


def test_a_menu_counts_the_wait_till_its_buttons_come_on():
    """Father of the Bride's Bonus page: buttons from 2.75 s into its first cell; keys before then
    do nothing, so the page's intro is that long."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles, disc.ptt, disc.title_pgcs, disc.first_play = [], {}, {}, None
    disc.menus = {1: {1: ("root", True, pgc(pre=["2004000000000002"])), 2: ("", False, pgc(40))}}
    btn = {"n": 1, "raw": "2004000000000002", "rect": [0, 0, 1, 1], "cmd": {}}
    disc.buttons = {(1, 2): [[btn]]}
    disc.buttons_at = {(1, 2): [2.75]}
    p = dvdvm.Player(disc)
    p.domain, p.pgcn = 1, 1
    out = dvdvm.Engine(disc).follow(p, {"op": "pgcn", "n": 2}, through=False)
    assert out["menu"] == (1, 2, 1) and out["intro"] == 2.75


def test_a_chapter_link_skips_the_titles_pre_commands():
    """The Sorcerer's Apprentice: the film's pre commands clear a flag and link to its chapter 1.
    A chapter link doesn't run them again (they'd find the flag cleared and loop): it plays."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles = [{"title": 1, "vts": 1, "ttn": 1}]
    disc.first_play = None
    # pre: if GPRM2 == 1, call the root menu; set GPRM2 = 1; link to chapter 1
    disc.title_pgcs = {1: {1: pgc(6000, pre=["30A8000200010083", "7100000200010000", "2005000000000001"])}}
    disc.ptt = {1: {1: [(1, 1)]}}
    disc.menus = {"vmgm": {}, 1: {1: ("root", True, pgc())}}
    btn = {"n": 1, "raw": "3002000000010000", "rect": [0, 0, 1, 1], "auto": False, "up": 0, "down": 0,
           "left": 0, "right": 0, "cmd": {}}                                # JumpTT 1
    disc.buttons = {(1, 1): [[btn]]}
    eng = dvdvm.Engine(disc)
    p = dvdvm.Player(disc)
    p.domain, p.pgcn, p.pgn = 1, 1, 1
    assert dvdvm.answer(eng.press(p, 1)) == {"plays": 1, "chapter": 1, "before": []}


def test_a_menu_waits_where_its_buttons_come_on():
    """The Sorcerer's Apprentice's FastPlay chooser: one menu PGC, Tinker Bell first (no buttons),
    then the chooser (its second program, with buttons). The disc waits there, not past it."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles = [{"title": 1, "vts": 1, "ttn": 1}]
    disc.first_play = None                                                    # (the title menu)
    disc.title_pgcs = {1: {1: pgc(6000)}}
    disc.ptt = {1: {1: [(1, 1)]}}
    # its post commands (FastPlay, when nobody chooses) play the film
    disc.menus = {"vmgm": {1: ("title", True, pgc(18, post=["3002000000010000"], cells=2, programs=2))}}
    btn = lambda n: {"n": n, "raw": "3002000000010000", "rect": [0, 0, 1, 1], "auto": False, "up": n, "down": n,
                     "left": 1, "right": 2, "cmd": {}}
    disc.buttons = {("vmgm", 1): [[], [btn(1), btn(2)]]}
    page = disc.menus["vmgm"][1][2]
    page["cells"] = [dict(page["cells"][0], seconds=5.6), dict(page["cells"][1], seconds=12.4)]
    p, out = dvdvm.Engine(disc).start()
    assert out["menu"] == ("vmgm", 1, 2) and "menu_key" not in out
    assert out["intro"] == 5.6                # Tinker Bell plays before the chooser's buttons come on


def test_the_button_highlighted_on_arrival():
    """Where a page's highlight starts (what the crawl trusts instead of looking): the page's
    forced button; else the one SPRM 8 holds (the button pressed, or a link's), if the page has
    it; else button 1."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles, disc.ptt, disc.title_pgcs, disc.first_play = [], {}, {}, None
    disc.menus = {"vmgm": {1: ("title", True, pgc()), 2: ("", False, pgc()), 3: ("", False, pgc())}}
    btn = lambda n, raw, **kw: dict({"n": n, "raw": raw, "rect": [0, 0, 1, 1], "auto": False, "up": n,
                                     "down": n, "left": n, "right": n, "cmd": {}}, **kw)
    disc.buttons = {("vmgm", 1): [[btn(1, "2004000000000002"), btn(2, "2004000000000003"),
                                   btn(3, "2004000000000002")]],
                    ("vmgm", 2): [[btn(1, "2001000000000001"), btn(2, "2001000000000001"),
                                   btn(3, "2001000000000001")]],
                    ("vmgm", 3): [[btn(1, "2001000000000001"), btn(2, "2001000000000001", forced=True)]]}
    eng = dvdvm.Engine(disc)
    p, out = eng.start()
    assert out["button"] == 1
    assert dvdvm.answer(eng.press(p, 2)) == {"goes_to": ("vmgm", 3, 1), "select": 2, "intro": 0}    # forced
    assert dvdvm.answer(eng.press(p, 1)) == {"goes_to": ("vmgm", 2, 1), "select": 1, "intro": 0}    # SPRM 8 kept
    assert dvdvm.answer(eng.press(p, 3)) == {"goes_to": ("vmgm", 2, 1), "select": 3, "intro": 0}    # (the one pressed)


def test_a_page_too_brief_to_use_plays_on():
    """WALL-E's start: a VMG page of 0.4 s, no still, 36 specks of buttons (a trap for programs
    that press them); a player passes it: its post commands play the warning (title 1), then the
    root menu waits. A held page, or one that comes back to itself, still waits."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles = [{"title": 1, "vts": 1, "ttn": 1}, {"title": 2, "vts": 1, "ttn": 2}]
    disc.first_play = None                                                   # (the title menu: the trap)
    trap = pgc(post=["3002000000010000"])                                    # then: JumpTT 1
    trap["cells"] = [dict(trap["cells"][0], seconds=0.4)]
    disc.title_pgcs = {1: {1: pgc(12, post=["3008000000830000"]), 2: pgc(6000)}}    # warning, then root menu
    disc.ptt = {1: {1: [(1, 1)], 2: [(2, 1)]}}
    root = pgc()
    root["cells"] = [dict(root["cells"][0], seconds=20.0)]
    disc.menus = {"vmgm": {1: ("title", True, trap)}, 1: {1: ("root", True, root)}}
    btn = lambda n, raw: {"n": n, "raw": raw, "rect": [0, 0, 10, 7], "auto": False, "up": n, "down": n,
                          "left": n, "right": n, "cmd": {}}
    disc.buttons = {("vmgm", 1): [[btn(n, "3002000000020000") for n in range(1, 37)]],   # each: the film
                    (1, 1): [[btn(1, "3002000000020000")]]}
    eng = dvdvm.Engine(disc)
    p, out = eng.start()
    assert out["menu"] == (1, 1, 1) and [x["title"] for x in out["played"]] == [1]
    # held by a still, the same page waits (a still menu)
    trap["cells"] = [dict(trap["cells"][0], still=255)]
    assert eng.start()[1]["menu"] == ("vmgm", 1, 1)
    # brief but coming back to itself (a short loop): it's the page
    trap["cells"] = [dict(trap["cells"][0], still=0)]
    trap["post"] = [bytes.fromhex("2004000000000001")]                       # LinkPGCN 1: itself
    assert eng.start()[1]["menu"] == ("vmgm", 1, 1)


def test_a_set_that_compares_a_register_first():
    """WALL-E's commentary switch: "GPRM2 = 333 if GPRM1 == 1" (a set with a compare: the
    register compared is 4 bits, GPRM1, not GPRM0 with the next byte's bits)."""
    p = dvdvm.Player(None)
    cmd = bytes.fromhex("71a00102014d0001")
    p.gprm[1] = 1
    p.execute(cmd)
    assert p.gprm[2] == 333
    p.gprm[1], p.gprm[2] = 0, 111
    p.execute(cmd)
    assert p.gprm[2] == 111


def test_the_titles_that_play_on_after_a_buttons_title():
    """The Sorcerer's Apprentice's Sneak Peeks: its button plays one, and each jumps to the next
    when it ends, the last back to the menu. The answer: the first, and those after it."""
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles = [{"title": t, "vts": 1, "ttn": t} for t in (1, 2, 3, 4)]
    disc.first_play = None
    jump = lambda t: "30020000000%d0000" % t
    disc.title_pgcs = {1: {1: pgc(6000), 2: pgc(70, post=[jump(3)]), 3: pgc(80, post=[jump(4)]),
                           4: pgc(90, post=["3008000000830000"])}}            # (then: the root menu)
    disc.ptt = {1: {t: [(t, 1)] for t in (1, 2, 3, 4)}}
    disc.menus = {"vmgm": {2: ("title", True, pgc(post=["3006000000420000"]))}, 1: {1: ("root", True, pgc())}}
    btn = lambda n, raw: {"n": n, "raw": raw, "rect": [0, 0, 1, 1], "auto": False, "up": 0, "down": 0,
                          "left": 0, "right": 0, "cmd": {}}
    disc.buttons = {(1, 1): [[btn(1, jump(2)), btn(2, jump(1))]]}
    eng = dvdvm.Engine(disc)
    p = dvdvm.Player(disc)
    p.domain, p.pgcn, p.pgn = 1, 1, 1
    assert dvdvm.answer(eng.press(p, 1)) == {"plays": 2, "chapter": 1, "before": [], "after": [3, 4]}
    assert dvdvm.answer(eng.press(p, 2)) == {"plays": 1, "chapter": 1, "before": []}    # the film: on its own
