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
    assert out["menu"] == (1, 1, 1) and [x["title"] for x in out["played"]] == [1]
    assert dvdvm.answer(eng.press(p, 1)) == {"plays": 2, "chapter": 2, "before": []}
    assert dvdvm.answer(eng.press(p, 2)) == {"stays": True}
    start, found, menus = dvdvm.explore(eng)
    assert start["goes_to"] == (1, 1, 1) and "menu_key" not in start
    assert found[(1, 1, 1, 1)] == [{"plays": 2, "chapter": 2, "before": []}]
    assert start["menu_goes_to"] == (1, 1, 1)                               # Menu: back to its root menu


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
    p, out = dvdvm.Engine(disc).start()
    assert out["menu"] == ("vmgm", 1, 2) and "menu_key" not in out
