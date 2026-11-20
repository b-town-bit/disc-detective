import json
import os

import pytest

from disc_detective import menus


def test_commands():
    assert menus.command(bytes.fromhex("3002000000030000")) == {"op": "title", "title": 3}
    assert menus.command(bytes.fromhex("3005000200010000")) == {"op": "vts_chapter", "title": 1, "chapter": 2}
    assert menus.command(bytes.fromhex("200400000000000b")) == {"op": "link_page", "to": 11}
    d = menus.command(bytes.fromhex("7104000600020002"))
    assert d["op"] == "link_page" and d["to"] == 2 and "sets" in d
    assert menus.command(bytes.fromhex("200100000000000d"))["op"] == "link_tail_page"
    assert menus.command(b"\0" * 8) == {"op": "nop"}


def _nav(vobu_ea, buttons=(), ptm=0.0):
    """A navigation packet: pack header, PCI with the given button rects and start time, DSI's VOBU
    end address."""
    s = bytearray(menus.SECTOR)
    s[0x2D + 0x0C:0x2D + 0x10] = round(ptm * 90000).to_bytes(4, "big")
    s[0:4] = b"\x00\x00\x01\xba"
    s[0x26:0x2A] = b"\x00\x00\x01\xbf"
    hli = 0x2D + 0x60
    s[hli + 17] = len(buttons)
    for i, (x0, y0, x1, y1) in enumerate(buttons):
        b = hli + 46 + 18 * i
        s[b:b + 3] = ((x0 << 12) | x1).to_bytes(3, "big")
        s[b + 3:b + 6] = ((y0 << 12) | y1).to_bytes(3, "big")
        s[b + 10:b + 18] = bytes.fromhex("200400000000000b")
    s[0x40F:0x413] = vobu_ea.to_bytes(4, "big")
    return bytes(s)


def test_buttons_found_partway_through_a_motion_menu(tmp_path):
    # VOBU at 0 (3 sectors) and 3 (2 sectors) have none; the one at 5 has the buttons
    vob = bytearray(menus.SECTOR * 8)
    for sector, data in ((0, _nav(2, ptm=0.25)), (3, _nav(1, ptm=0.75)),
                         (5, _nav(2, [(10, 20, 110, 60), (10, 70, 110, 99)], 1.25))):
        vob[sector * menus.SECTOR:(sector + 1) * menus.SECTOR] = data
    path = tmp_path / "VTS_01_0.VOB"
    path.write_bytes(bytes(vob))
    with open(path, "rb") as fh:
        btns, at = menus._buttons(fh, [{"first": 0, "last": 7}])
        assert [b["rect"] for b in btns] == [[10, 20, 110, 60], [10, 70, 110, 99]]
        assert btns[0]["cmd"] == {"op": "link_page", "to": 11}
        assert at == 1.0                        # (the units start 0.5 s apart: on in the third)
        assert menus._buttons(fh, [{"first": 0, "last": 4}]) == ([], 0.0)     # the page ends first
        assert menus._buttons(fh, []) == ([], 0.0)


def test_a_refused_read_says_so(monkeypatch):
    """A copy-protected disc's drive refuses its protected sectors until a player unlocks it:
    that's a Refused, and read_structure has VLC open the disc, then reads again."""
    class Locked:
        def seek(self, pos):
            pass

        def read(self, n):
            raise OSError(22, "Invalid argument")
    try:
        menus._buttons(Locked(), [{"first": 136, "last": 204}])
        assert False, "should have raised"
    except menus.Refused as e:
        assert "sector 136" in str(e)
    calls = []

    def structure(root):
        calls.append(root)
        if len(calls) == 1:
            raise menus.Refused("the drive won't read sector 136 of the menus")
        return {"vmgm": [], "vtsm": {}}

    class FakeVlc:
        def __init__(self, folder):
            self.frames = 1

        def open(self, mrl):
            calls.append("vlc " + mrl)

        def close(self):
            pass
    monkeypatch.setattr(menus, "dvd_structure", structure)
    monkeypatch.setattr(menus, "Vlc", FakeVlc)
    # a drive: unlocked first, before anything's read (no refused reads for the drive to log)
    assert menus.read_structure("E:", "C:/VLC", log=lambda s: None) == {"vmgm": [], "vtsm": {}}
    assert calls == ["vlc " + menus.dvd_mrl("E:"), "E:"]
    # a folder (no unlocking first): refused, then unlocked and read again
    calls.clear()
    assert menus.read_structure("C:/rips/MUPPETS", "C:/VLC", log=lambda s: None) == {"vmgm": [], "vtsm": {}}
    assert calls == ["C:/rips/MUPPETS", "vlc " + menus.dvd_mrl("C:/rips/MUPPETS"), "C:/rips/MUPPETS"]
    calls.clear()
    try:
        menus.read_structure("E:", None)                # no VLC to unlock it: say so
        assert False, "should have raised"
    except menus.Refused:
        pass


def _btn(n, cmd, up=0, down=0, auto=False):
    return {"n": n, "rect": [0, 0, 10, 10], "auto": auto, "up": up, "down": down, "left": 0, "right": 0, "cmd": cmd}


def test_a_button_playing_one_title_from_its_routes_chapter():
    """The Iron Giant's edition chooser: Play and each of its 34 scenes lead to it, so Signature
    Edition plays title 1 from chapter 1 to 34, by route. It plays title 1 (from the start, as
    Play has it), no press needed; two titles by route, or no Play's route, aren't this."""
    by_route = [{"plays": 1, "chapter": c, "before": []} for c in range(1, 35)]
    assert menus._one_title_by_route(by_route) == {"title": 1, "chapter": 1}
    assert menus._one_title_by_route(by_route[:1]) is None                       # (one answer: as ever)
    assert menus._one_title_by_route(by_route + [{"plays": 2, "chapter": 1}]) is None
    assert menus._one_title_by_route(by_route[1:]) is None                       # (no chapter 1)
    assert menus._one_title_by_route(by_route + [{"goes_to": (1, 2, 1)}]) is None


def test_crawl_routes_and_moves():
    play = {"op": "vts_title", "title": 1}
    scenes = {"op": "link_page", "to": 3}
    main = [_btn(1, play, up=3, down=2), _btn(2, scenes, up=1, down=3), _btn(3, {"op": "link_page", "to": 9}, up=2, down=1)]
    group1 = [_btn(1, {"op": "vts_chapter", "title": 1, "chapter": 1}, down=2),
              _btn(2, {"op": "link_program", "to": 2}, up=1, down=3), _btn(3, {"op": "link_program", "to": 2}, auto=True)]
    group2 = [_btn(1, {"op": "vts_chapter", "title": 1, "chapter": 4}, down=2), _btn(2, {"op": "link_page", "to": 2}, up=1)]
    structure = {"vmgm": [], "vtsm": {1: [
        {"pgc": 2, "buttons": main, "program_buttons": [main]},
        {"pgc": 3, "buttons": group1, "program_buttons": [group1, group2]},
        {"pgc": 9, "buttons": [], "program_buttons": [[]]}]}}
    pages = menus._pages(structure)
    routes = menus._routes(pages, (1, 2, 1))
    assert routes == {(1, 2, 1): [], (1, 3, 1): [((1, 2, 1), 2)], (1, 3, 2): [((1, 2, 1), 2), ((1, 3, 1), 2)]}
    assert menus._edge(pages, (1, 2, 1), main[2]) is None          # a page with no buttons: press to see
    # a link found by pressing (the page a "bonus material" button's commands pick as they run)
    learned = menus._routes(pages, (1, 2, 1), {((1, 2, 1), 1): (1, 3, 2)})
    assert learned[(1, 3, 2)] == [((1, 2, 1), 1)]
    assert menus._moves(main, 1, 3) == ["up"]
    assert menus._moves(group1, 1, 2) == ["down"]
    assert menus._moves(group1, 1, 3) == ["down", "down"]          # the auto button only as the target
    assert menus._moves(group1, 2, 1) == ["up"]


def test_the_discs_program_beats_the_buttons_own_links():
    """A button that links to a page goes where the disc's program found it goes: the page's own
    commands may pick another part of it (Enchanted: page 18 opens on its program 2), or play a
    title (then nothing to route through)."""
    main = [_btn(1, {"op": "link_page", "to": 3}, down=2), _btn(2, {"op": "link_page", "to": 3}, up=1)]
    other = [_btn(1, {"op": "link_page", "to": 2}, down=2), _btn(2, {"op": "link_page", "to": 2}, up=1)]
    structure = {"vmgm": [], "vtsm": {1: [
        {"pgc": 2, "buttons": main, "program_buttons": [main]},
        {"pgc": 3, "buttons": other, "program_buttons": [other, other]}]}}
    pages = menus._pages(structure)
    learned = {((1, 2, 1), 1): (1, 3, 2)}
    known = {((1, 2, 1), 2): {"plays": {"title": 4, "chapter": 1}, "engine": True}}
    assert menus._goes(pages, (1, 2, 1), main[0], learned, known) == (1, 3, 2)
    assert menus._goes(pages, (1, 2, 1), main[1], learned, known) is None
    assert menus._goes(pages, (1, 2, 1), main[1]) == (1, 3, 1)               # the button's say alone
    routes = menus._routes(pages, (1, 2, 1), learned, known)
    assert (1, 3, 2) in routes and (1, 3, 1) not in routes


def test_scene_page_by_the_discs_program():
    """Scene buttons that go through a command page first (Enchanted's page 21): the program's
    answers say they're scenes."""
    btns = [_btn(n, {"op": "link_page", "to": 30 + n}) for n in (1, 2, 3)]
    structure = {"titles": [], "vmgm": [], "vtsm": {1: [{"pgc": 21, "buttons": btns, "program_buttons": [btns]}]}}
    pages = menus._pages(structure)
    assert not menus._scene_page(structure, pages, (1, 21))
    known = {((1, 21, 1), n): {"plays": {"title": 2, "chapter": n}, "engine": True} for n in (1, 2, 3)}
    assert menus._scene_page(structure, pages, (1, 21), known)


def test_watching_waits_out_a_short_clip(monkeypatch):
    """A clip under a minute before the promised title (a notice) is watched to its end, not
    taken for what the button plays."""
    clock = [0.0]
    monkeypatch.setattr(menus.time, "time", lambda: clock[0])
    monkeypatch.setattr(menus.time, "sleep", lambda t: clock.__setitem__(0, clock[0] + t))

    class V:
        def press(self, key):
            pass

        def state(self):
            return {"title": 5 if clock[0] < 40 else 2, "chapter": 0}
    c = object.__new__(menus._Crawler)
    c.v = V()
    chain = c.watch({5: 45, 2: 5400})
    assert [x["title"] for x in chain] == [5, 2]


def test_vlc_dvd_menu_when_the_film_plays_instead(monkeypatch):
    # WALL-E: VLC plays the film where the disc's program waits in its menu, and the film
    # ignores Menu; VLC's DVD Menu gets there. Waiting on a page meanwhile gives up at once
    clock = [0.0]
    monkeypatch.setattr(menus.time, "time", lambda: clock[0])
    monkeypatch.setattr(menus.time, "sleep", lambda t: clock.__setitem__(0, clock[0] + t))

    class V:
        title, calls = 30, []

        def press(self, key):
            self.calls.append(key)

        def menu(self):
            self.calls.append("dvd menu")
            self.title = 0

        def state(self):
            return {"title": self.title, "chapter": 0}

        def titles(self):
            return [{"index": 0, "seconds": 0}, {"index": 30, "seconds": 5880}]
    c = object.__new__(menus._Crawler)
    c.v, c.lengths = V(), None
    c.menu_call()
    assert c.v.calls == ["popup", "dvd menu"] and c.v.state()["title"] == 0
    main = [_btn(1, {"op": "vts_title", "title": 30}, up=2, down=2), _btn(2, {"op": "link_page", "to": 3}, up=1, down=1)]
    c.pages = menus._pages({"vmgm": [], "vtsm": {7: [{"pgc": 8, "buttons": main, "program_buttons": [main]}]}})
    c.v.title, t0 = 30, clock[0]
    try:
        c.arrive((7, 8, 1), 1)
        assert False, "should have raised"
    except menus.Lost:
        pass
    assert clock[0] - t0 < 5


def test_plain_names_for_unreadable_words():
    words = {"sneak", "peeks", "making", "enchanted", "fairy", "tale", "deleted", "scenes", "serenade",
             "extraterrestrials"}
    is_word = lambda w: w.lower() in words
    assert menus.readable("The Making of Enchanted", is_word)
    assert menus.readable("Deleted Scenes 2", is_word)
    assert not menus.readable("Beye olticak Peeks", is_word)
    assert not menus.readable("", is_word)
    assert not menus.readable("2 Oh", lambda w: w.lower() in words | {"oh"})     # a scrap, not a name
    # Spider-Man 2's "Costume Design": both words; Enchanted's junk with scraps that are words
    # ("Rei", "Mec", "ay") but long parts that aren't doesn't pass
    common = lambda w: w.lower() in words | {"costume", "design", "rei", "mec", "ay"}
    assert menus.readable("Costume Design", common)
    assert not menus.readable("Rei Mec ai ay cummantie scien", common)
    found = {"feature": 1, "extras": [{"title": 3, "name": "Fairy Tale Making"}, {"title": 4, "name": "Rrw Oxq"}],
             "promos": []}
    assert [e["name"] for e in menus.plain_names(found, is_word)["extras"]] == ["Fairy Tale Making", ""]
    assert menus.plain_names(found, None) is found                          # no dictionary: as read
    # not words, but its button's other reading is (Order of the Phoenix's "The Choreography of Magic",
    # surest as "Iagic")
    found = {"feature": 1, "promos": [], "extras": [{"title": 5, "name": "Fqiry Tqle Iaking", "also": ["Fairy Tale Making"]},
                                                    {"title": 6, "name": "Rrw Oxq", "also": ["Rrw Oxo"]}]}
    assert [e["name"] for e in menus.plain_names(found, is_word)["extras"]] == ["Fairy Tale Making", ""]
    # extras' own words count, though no title has them (The Iron Giant's)
    shot = {"feature": 1, "promos": [], "extras": [{"title": 36, "name": "Storyboards and Animatics", "seconds": 60,
                                                    "listed": None, "kind": None}]}
    assert menus.plain_names(shot, lambda w: w.lower() == "and")["extras"][0]["name"] == "Storyboards and Animatics"
    # read by RapidOCR, a name stands though no title has its words (Muppets from Space's)
    muppets = {"feature": 1, "promos": [], "reader": "rapidocr",
               "extras": [{"title": 12, "name": "Cardshark Serenade"}, {"title": 7, "name": "Expectorating Extraterrestrials"}]}
    assert [e["name"] for e in menus.plain_names(muppets, is_word)["extras"]] == \
        ["Cardshark Serenade", "Expectorating Extraterrestrials"]
    assert [e["name"] for e in menus.plain_names(dict(muppets, reader=None), is_word)["extras"]] == ["", ""]
    # though a scrap of a name cut off isn't (Spider-Man's TV Spots: "22", "AMPION")
    scraps = dict(muppets, extras=[{"title": 20, "name": "22"}, {"title": 21, "name": "AMPION"}])
    assert [e["name"] for e in menus.plain_names(scraps, is_word)["extras"]] == ["", ""]
    # and a name RapidOCR read sure enough isn't judged at all ("Cingular" at 1.00: no title's
    # word); one it's less sure of is ("22" at 0.57)
    sure = dict(muppets, extras=[{"title": 22, "name": "Cingular", "sure": 1.0}, {"title": 20, "name": "22", "sure": 0.57},
                                 {"title": 24, "name": "Reviow", "sure": 0.9}])
    assert [e["name"] for e in menus.plain_names(sure, is_word)["extras"]] == ["Cingular", "", ""]
    # a little less sure, beside names on its page read well: a made-up name stands (Wallace &
    # Gromit's gadgets); a short word misread doesn't ("Piay All"), nor one alone on its page
    words |= {"christmas", "shopper", "the", "all"}
    page = lambda n, name, s, p=(7, 33, 1): {"title": n, "name": name, "sure": s, "page": list(p)}
    gadgets = dict(muppets, extras=[page(17, "The Soccamatic", 0.937), page(24, "A Christmas Cardomatic", 0.99),
                                    page(26, "Shopper 13", 0.99), page(15, "Piay All", 0.927),
                                    page(23, "The 525 Crackervac", 0.953, (7, 26, 1))])
    assert [e["name"] for e in menus.plain_names(gadgets, is_word)["extras"]] == \
        ["The Soccamatic", "A Christmas Cardomatic", "Shopper 13", "", ""]


def test_known_without_pressing():
    titles = [{"title": 1, "vts": 1, "ttn": 1, "chapters": 16}, {"title": 10, "vts": 3, "ttn": 2, "chapters": 1}]
    settings = [_btn(1, {"op": "link_page", "to": 11}), _btn(2, {"op": "link_page", "to": 5})]
    structure = {"titles": titles, "vmgm": [], "vtsm": {1: [
        {"pgc": 8, "cells": [{}], "buttons": settings, "program_buttons": [settings], "pre": []},
        {"pgc": 11, "cells": [], "buttons": [], "pre": [{"op": "set", "raw": ""}, {"op": "link_page", "to": 8}]},
        {"pgc": 5, "cells": [], "buttons": [], "pre": [{"op": "link_resume"}]}]}}
    pages = menus._pages(structure)
    known = lambda cmd, vts=1: menus._known(structure, pages, (vts, 8, 1), _btn(1, cmd))
    assert known({"op": "vts_chapter", "title": 1, "chapter": 4}) == {"plays": {"title": 1, "chapter": 4}}
    assert known({"op": "vts_title", "title": 2}, vts=3) == {"plays": {"title": 10, "chapter": 1}}
    assert known({"op": "title", "title": 10}) == {"plays": {"title": 10, "chapter": 1}}
    assert known({"op": "link_page", "to": 11}) == {"menus": True}          # sets things, back to page 8
    assert known({"op": "link_page", "to": 5}) is None                      # resumes the film: press it
    assert known({"op": "link_tail_page"}) is None


def test_button_words():
    assert menus._wordy("AN INVITATION TO FATHER OF THE BRIDE (10:35)") > menus._wordy("—" + "\" 4 | 2.A kh 8")
    assert menus._wordy("5. MEETING HIS FOLKS") > 0 > menus._wordy("4G 7: Ly")
    assert menus.nice_name("MARTIN & SHORT INTERVIEW EACH OTHER") == "Martin & Short Interview Each Other"
    assert menus.nice_name("AN INVITATION TO FATHER OF THE BRIDE,") == "An Invitation to Father of the Bride"
    assert menus.nice_name("Already Fine") == "Already Fine"
    # Father of the Bride Part II: lower-case lettering on ovals, OCR'd with specks
    assert menus.nice_name("_just between friends-") == "Just Between Friends"
    assert menus.nice_name("production) story") == "Production Story"
    assert menus.nice_name("AN INVITATION (10:35)") == "An Invitation (10:35)"     # paired: kept
    # a list's number goes, a name's number stays; symbols at either end go, not "!" ending it,
    # a bracket closing one in it, or a name all in brackets (out of them)
    assert menus.nice_name("4. Jonah Jameson") == "Jonah Jameson"
    assert menus.nice_name("2) Leaving Karate") == "Leaving Karate"
    assert menus.nice_name("3 Blind Mice") == "3 Blind Mice" and menus.nice_name("Stuart Little 2") == "Stuart Little 2"
    # RapidOCR's list numbers, no space after them; and a flourished "I" read as "1" (Enchanted)
    assert menus.nice_name("1.A Lock Of Prince Edward's Hair") == "A Lock Of Prince Edward's Hair"
    assert menus.nice_name("3.1 Am Not Waiting For My Prince") == "I Am Not Waiting For My Prince"
    assert menus.nice_name("2) Leaving Karate") == "Leaving Karate" and menus.nice_name("1. 2 Fast") == "2 Fast"
    assert menus.nice_name("& Deleted Scenes") == "Deleted Scenes"
    assert menus.nice_name("3. Not Wasting Time On My Prince &") == "Not Wasting Time On My Prince"
    assert menus.nice_name("Ghost Rider™.") == "Ghost Rider" and menus.nice_name("Dumped!") == "Dumped!"
    assert menus.nice_name("Close Encounters (of the Third Kind)") == "Close Encounters (of the Third Kind)"
    assert menus.nice_name("(Costume Design)") == "Costume Design"


def test_menu_titles():
    def page(n, buttons, route=()):
        return {"domain": 1, "page": n, "program": 1, "route": [{"page": p, "program": 1, "button": b} for p, b in route],
                "buttons": [dict({"n": i + 1}, **b) for i, b in enumerate(buttons)]}
    plays = lambda t, ch=1, s=6307.5: {"title": t, "chapter": ch, "seconds": s}
    out = {"pages": [
        page(2, [{"text": "PLAY", "plays": plays(1)}, {"text": "SCENE SELECTION", "goes_to": {"page": 3}},
                 {"text": "BONUS FEATURES", "goes_to": {"page": 10}}, {"text": "SNEAK PEEKS,", "goes_to": {"page": 14}}]),
        page(3, [{"text": "1, OPENING CREDITS", "plays": plays(1, 1)}, {"text": "2 A kh", "plays": plays(1, 2)},
                 {"text": "6. WEDDING PLANS", "plays": plays(1, 3)}], [(2, 2)]),
        page(10, [{"text": "AN INVITATION TO FATHER OF THE BRIDE (10:35)", "plays": plays(10, s=637.0)},
                  {"text": "MARTIN & SHORT INTERVIEW EACH OTHER (5:13)", "plays": plays(11, s=316.0)},
                  {"text": "AUDIO COMMENTARY", "plays": plays(1)}], [(2, 3)]),
        page(14, [{"text": "PLAY nul", "plays": plays(6, s=325.5)}, {"text": "", "plays": plays(9, s=52.5)}], [(2, 4)]),
    ]}
    found = menus.menu_titles(out)
    assert found["feature"] == 1
    assert found["extras"] == [
        {"title": 10, "name": "An Invitation to Father of the Bride", "seconds": 637.0, "listed": 635, "kind": None,
         "page": [1, 10, 1], "via": "BONUS FEATURES "},
        {"title": 11, "name": "Martin & Short Interview Each Other", "seconds": 316.0, "listed": 313, "kind": "Interview",
         "page": [1, 10, 1], "via": "BONUS FEATURES "}]
    assert found["promos"] == [6, 9]
    assert "menu says 10:35, plays 10:37" in menus.found_text(found)
    # Father of the Bride Part II: the film's trailer in 2 chapters, an ad reel in 4 (one each)
    out = {"chapters": {"4": 2, "16": 4}, "pages": [
        page(2, [{"text": "theatrical trailer", "plays": plays(4, s=156.5)},
                 {"text": "trailer", "plays": plays(16, s=87.3)}, {"text": "1. Opening", "plays": plays(1, 1)},
                 {"text": "2. Next", "plays": plays(1, 2)}])]}
    found = menus.menu_titles(out)
    assert [e["title"] for e in found["extras"]] == [4] and found["promos"] == [16]
    # Enchanted: the "Sneak Peeks" button misread, but the page it leads to says so over the ads,
    # and so does the "More" page after it
    out = {"pages": [
        page(18, [{"text": "Play", "plays": plays(42)}, {"text": "fey, olieak Pooks?", "goes_to": {"page": 34}}]),
        dict(page(34, [{"text": "THE", "plays": plays(53, s=120.0)}, {"text": "Beene", "goes_to": {"page": 36}}],
                  [(18, 2)]), heading="neak Peeks Qarreeer anon"),
        dict(page(36, [{"text": "", "plays": plays(14, s=121.0)}], [(18, 2), (34, 2)]), heading="Sneak Peeks IVAKIUA")]}
    assert menus.menu_titles(out)["promos"] == [14, 53]
    # The Sorcerer's Apprentice: the main menu's labels sit beside its buttons, "Sneak Peeks"
    # among them, so they're in its heading; that says nothing of its Bonus Features page
    out = {"pages": [
        dict(page(48, [{"text": "Movie", "plays": plays(35)}, {"text": "Peeks", "plays": plays(66, s=68.0)},
                       {"text": "Features", "goes_to": {"page": 73}}]),
             heading="Play Scene Selection Bonus Features Sneak Peeks Set Up"),
        dict(page(73, [{"text": "The Sorcerers Apprentice", "plays": plays(57, s=1322.0)}], [(48, 3)]),
             heading="BONUS FEATURES Deleted Scene")]}
    found = menus.menu_titles(out)
    assert [e["title"] for e in found["extras"]] == [57] and found["promos"] == [66]


def test_the_copy_the_menus_play_is_the_one_labelled():
    """Spider-Man 2: the scan kept title 10 and called 17 its copy, but the previews page plays
    17: 17 gets the Promo, 10 nothing (applying the labels makes it the copy)."""
    from disc_detective import core
    found = {"feature": 1, "extras": [], "promos": [17]}
    titles = [{"id": "Title 1", "role": "feature", "note": None},
              {"id": "Title 10", "role": "extra", "note": None},
              {"id": "Title 17", "role": "duplicate", "note": "same video as Title 10"}]
    labels = menus.detected_labels(found, titles)
    assert labels["Title 17"]["type"] == "Promo" and "Title 10" not in labels
    core.apply_labels(titles, labels)
    assert [(t["id"], t["role"]) for t in titles][1:] == [("Title 10", "duplicate"), ("Title 17", "promo")]
    # a copy the menus don't play stays a copy
    assert "Title 17" not in menus.detected_labels({"feature": 1, "extras": [], "promos": [10]}, [
        {"id": "Title 1", "role": "feature"}, {"id": "Title 10", "role": "extra"},
        {"id": "Title 17", "role": "duplicate", "note": "same video as Title 10"}])


def test_a_labelled_copy_takes_its_originals_other_copies_too():
    """Order of the Phoenix: three playlists of the film (00000, 00059, 00060), the scan keeping 00000 and the
    others its copies; your label makes 00059 the film -- its Play button's -- and 00000 its copy. 00060, the one
    its scene buttons jump into, is a copy of 00059 then too, not of a copy: not an extra."""
    from disc_detective import core
    titles = [{"id": "00000.mpls", "role": "feature", "note": None},
              {"id": "00059.mpls", "role": "duplicate", "note": "same video as 00000.mpls"},
              {"id": "00060.mpls", "role": "duplicate", "note": "same video as 00000.mpls"}]
    core.apply_labels(titles, {"00059.mpls": {"type": "MainMovie"}})
    notes = {t["id"]: (t["role"], t["note"]) for t in titles}
    assert notes["00000.mpls"] == ("duplicate", "same video as 00059.mpls")
    assert notes["00060.mpls"] == ("duplicate", "same video as 00059.mpls")
    found = {"feature": "00059.mpls", "extras": [{"title": "00060.mpls", "name": ""}], "promos": []}
    labels = menus.detected_labels(found, titles)
    assert labels["00059.mpls"]["type"] == "MainMovie" and "00060.mpls" not in labels
    core.apply_labels(titles, {})                       # (the label cleared: as the scan had them)
    assert [(t["id"], t["role"], t["note"]) for t in titles] == [
        ("00000.mpls", "feature", None), ("00059.mpls", "duplicate", "same video as 00000.mpls"),
        ("00060.mpls", "duplicate", "same video as 00000.mpls")]


def test_detected_labels():
    found = {"feature": 1, "extras": [{"title": 10, "name": "An Invitation to Father of the Bride"},
                                      {"title": 11, "name": ""}], "promos": []}
    titles = [{"id": "Title %d" % n, "role": role} for n, role in
              ((1, "feature"), (2, "extra"), (5, "extra"), (6, "extra"), (7, "duplicate"), (10, "extra"), (11, "extra"))]
    labels = menus.detected_labels(found, titles, keep={"Title 5"})
    assert labels["Title 1"]["type"] == "MainMovie"
    assert labels["Title 10"] == {"type": "Extra", "title": "An Invitation to Father of the Bride",
                                  "note": "the menus: an extra"}
    assert labels["Title 11"]["type"] == "Extra" and labels["Title 11"]["title"] is None
    assert labels["Title 2"]["type"] == labels["Title 6"]["type"] == "Promo"
    assert "Title 5" not in labels                  # yours (or TheDiscDB's): unchanged
    assert "Title 7" not in labels                  # a copy stays a copy
    # a short clip the scan set aside as junk, that the menus offer (an outtake): added
    clips = [{"id": "Title 1", "role": "feature"}, {"id": "Title 12", "role": "junk"}, {"id": "Title 13", "role": "junk"}]
    outtake = menus.detected_labels({"feature": 1, "extras": [{"title": 12, "name": "Cardshark"}]}, clips)
    assert outtake["Title 12"] == {"type": "Extra", "title": "Cardshark", "note": "the menus: an extra"}
    assert "Title 13" not in outtake                # junk the menus don't offer stays junk (not a Promo)
    assert menus.detected_labels({"feature": None, "extras": [], "promos": []}, titles) == {}
    trailer = menus.detected_labels({"feature": 1, "extras": [{"title": 2, "name": "Theatrical Trailer"}]}, titles)
    assert trailer["Title 2"]["type"] == "Trailer"            # TheDiscDB's type for a film's own trailer
    misread = menus.detected_labels({"feature": 1, "extras": [{"title": 2, "name": "Theatrical Taailer"}]}, titles)
    assert misread["Title 2"]["type"] == "Trailer"            # a letter misread (Abbott and Costello Go to Mars)
    # what a player shows before the menu (Jurassic World's preview reel, and an 85 s one the
    # scan called junk): previews, even set aside as junk
    reel = [{"id": "Title 10", "role": "feature"}, {"id": "Title 2", "role": "junk", "duration": 8.0},
            {"id": "Title 3", "role": "extra", "duration": 150.0}]
    previews = menus.detected_labels({"feature": 10, "extras": [], "opening": [2, 3]}, reel)
    assert previews["Title 3"]["type"] == "Promo" and "Title 2" not in previews     # (8 s: a logo, left be)
    assert previews["Title 3"]["title"] == "Previews" and "before the menu" in previews["Title 3"]["note"]


def test_other_films_trailers_are_ads_and_sing_alongs_music():
    """The Swan Princess (DVD): a Trailers page of other films' trailers (ads: Promos, named as the menus'
    other ads are, "Previews - Daddy Day Care"), and its songs on a Sing-Along page (Music, "Sing-Along:
    <song>"). The same rule on a show's disc and a BD-J's (extra_label)."""
    titles = [{"id": "Title %d" % n, "role": "feature" if n == 1 else "extra"} for n in range(1, 8)]
    trailers = [("The Swan Princess Collection", 2), ("Daddy Day Care", 3), ("Family Pun Trailer", 4),
                ("Theatrical Trailer", 5), ("The Swan Princess Teaser", 6)]
    found = {"feature": 1, "extras": [{"title": t, "name": n, "kind": "Trailer"} for n, t in trailers] +
             [{"title": 7, "name": "Far Longer Than Forever", "kind": "Music", "sing": True}]}
    labels = menus.detected_labels(found, titles, film="The Swan Princess")
    assert labels["Title 2"] == {"type": "Promo", "title": "Previews - The Swan Princess Collection",
                                 "note": "the menus: a trailer for another film, an ad"}
    assert labels["Title 3"]["type"] == labels["Title 4"]["type"] == "Promo"
    assert labels["Title 5"]["type"] == labels["Title 6"]["type"] == "Trailer"      # its own
    assert labels["Title 7"]["type"] == "Music" and labels["Title 7"]["title"] == "Sing-Along: Far Longer Than Forever"
    # the film not known: trailers as the page has them
    assert menus.detected_labels(found, titles)["Title 3"]["type"] == "Trailer"
    assert menus.heading_kind("SING-ALONG SONGS") == "Music" and menus.heading_kind("Sing Along") == "Music"
    assert menus.sing_along("Sing-Along: Practice, Practice, Practice") == "Sing-Along: Practice, Practice, Practice"
    # a show's disc: another show's trailer an ad, its own season's not
    tv = {"episodes": [], "extras": [{"title": 2, "name": "Monk", "kind": "Trailer", "seconds": 90},
                                     {"title": 3, "name": "Psych Season 2 Trailer", "kind": "Trailer", "seconds": 90},
                                     {"title": 4, "name": "The Psych Theme", "kind": "Music", "seconds": 90, "sing": True}]}
    shows = menus.detected_tv_labels(tv, [{"id": "Title %d" % n, "role": "extra"} for n in (2, 3, 4)], film="Psych")
    assert (shows["Title 2"]["type"], shows["Title 2"]["title"]) == ("Promo", "Previews - Monk")
    assert shows["Title 3"]["type"] == "Trailer"
    assert (shows["Title 4"]["type"], shows["Title 4"]["title"]) == ("Music", "Sing-Along: The Psych Theme")
    # a BD-J's menu project (Sony's): the same
    from disc_detective import bdmenus
    entries = [{"name": "SF_Daddy_Day_Care_Trailer", "playlist": "00010.mpls", "seconds": 120, "fps": 23.976},
               {"name": "SF_Theatrical_Trailer", "playlist": "00011.mpls", "seconds": 120, "fps": 23.976}]
    got = bdmenus.project_labels([{"id": "00010.mpls", "duration": 120}, {"id": "00011.mpls", "duration": 120}],
                                 entries, film="The Swan Princess")
    assert (got["00010.mpls"]["type"], got["00010.mpls"]["title"]) == ("Promo", "Previews - Daddy Day Care Trailer")
    assert got["00011.mpls"]["type"] == "Trailer" and got["00011.mpls"]["exact"] is False


def test_one_namer_for_every_disc():
    """The same words named the same on a film's DVD, a show's disc and a BD-J's menu project: one namer
    (name_extras) after the crawl, the labels' builders only saying which title is an extra."""
    said = ["Music Video: Shining Star", "THEATRICAL TAAILER", "Sing-Along: Far Longer Than Forever",
            "Easter Egg", "Daddy Day Care Trailer", "The Making of"]
    want = [("Music", "Shining Star"), ("Trailer", "THEATRICAL TAAILER"),
            ("Music", "Sing-Along: Far Longer Than Forever"), ("Extra", "Easter Egg"),
            ("Promo", "Previews - Daddy Day Care Trailer"), ("Extra", "The Making of")]
    ids = range(2, 2 + len(said))
    film = menus.detected_labels({"feature": 1, "extras": [{"title": t, "name": n} for t, n in zip(ids, said)]},
                                 [{"id": "Title %d" % t, "role": "extra"} for t in ids], film="The Swan Princess")
    show = menus.detected_tv_labels({"episodes": [], "extras": [{"title": t, "name": n, "seconds": 60 + t}
                                                                for t, n in zip(ids, said)]},
                                    [{"id": "Title %d" % t, "role": "extra"} for t in ids], film="The Swan Princess")
    from disc_detective import bdmenus
    entries = [{"name": "SF_" + n.replace(" ", "_"), "playlist": "%05d.mpls" % t, "seconds": 60, "fps": 23.976}
               for t, n in zip(ids, said)]
    project = bdmenus.project_labels([{"id": "%05d.mpls" % t, "duration": 60} for t in ids], entries,
                                     film="The Swan Princess", texts={"%05d.mpls" % t: n for t, n in zip(ids, said)})
    for t, w in zip(ids, want):
        for labels, key in ((film, "Title %d" % t), (show, "Title %d" % t), (project, "%05d.mpls" % t)):
            assert (labels[key]["type"], labels[key]["title"]) == w, (key, labels[key])


def test_the_previews_a_player_shows_before_the_menu():
    """Father of the Bride: a player plays a logo (12 s) and 6 minutes of trailers (title 2 here)
    before its menu; VLC skips both. The trailers count, the logo doesn't (too short); a title
    a menu button also plays isn't one."""
    from disc_detective import dvdvm
    from test_dvdvm import pgc
    disc = dvdvm.Disc.__new__(dvdvm.Disc)
    disc.titles = [{"title": n, "vts": 1, "ttn": n} for n in (1, 2, 3)]
    disc.first_play = pgc(pre=["3002000000030000"])                         # JumpTT 3: the logo
    disc.title_pgcs = {1: {1: pgc(6000), 2: pgc(361, post=["3008000000830000"]),   # the trailers; root menu
                           3: pgc(12, post=["3002000000020000"])}}           # the logo, then JumpTT 2
    disc.ptt = {1: {1: [(1, 1)], 2: [(2, 1)], 3: [(3, 1)]}}
    disc.menus = {"vmgm": {}, 1: {1: ("root", True, pgc())}}
    disc.buttons = {(1, 1): [[{"n": 1, "raw": "3002000000010000", "rect": [0, 0, 1, 1], "cmd": {}}]]}
    assert dvdvm.opening(dvdvm.Engine(disc)) == [2]
    page = {"domain": 1, "page": 1, "program": 1, "route": [], "image": None,
            "buttons": [{"n": 1, "rect": [0, 0, 1, 1], "text": "Play", "plays": {"title": 1, "chapter": 1, "seconds": 6000}}]}
    assert menus.menu_titles({"pages": [page], "opening": [2, 1]})["opening"] == [2]


def test_passing_by_scenes_trailers_and_settings():
    titles = [{"title": 1, "vts": 1, "ttn": 1, "chapters": 16}]
    chapter = lambda n, ch: _btn(n, {"op": "vts_chapter", "title": 1, "chapter": ch})
    scenes = [chapter(1, 1), chapter(2, 2), _btn(3, {"op": "link_page", "to": 2})]
    extras = [_btn(1, {"op": "link_tail_page"}), _btn(2, {"op": "vts_chapter", "title": 1, "chapter": 1})]
    structure = {"titles": titles, "vmgm": [], "vtsm": {1: [
        {"pgc": 3, "buttons": scenes, "program_buttons": [scenes]},
        {"pgc": 10, "buttons": extras, "program_buttons": [extras]}]}}
    pages = menus._pages(structure)
    assert menus._scene_page(structure, pages, (1, 3))
    assert not menus._scene_page(structure, pages, (1, 10))                  # one chapter isn't scenes
    for words in ("SNEAK PEEKS,", "SET UP", "AUDIO COMMENTARY", "SCENE SELECTION", "MAIN MENU", "Previews",
                  "preview trailer", "Sneak Preview",     # a film's menu: ads (Father of the Bride Part II)
                  "TALENT FILES", "Cast & Crew", "Biographies", "Photo Gallery"):   # stills (Muppets from Space)
        assert menus._skip_text(words), words
    for words in ("BONUS FEATURES", "AN INVITATION TO FATHER OF THE BRIDE (10:35)", "PLAY", "", "ow",
                  "theatrical trailer"):                  # the film's own trailer is an extra
        assert not menus._skip_text(words), words
    for words in ("PLAY", "Play Movie", "PLAY THE FILM,", "Resume", "Start", "i play"):
        assert menus.PLAY_RE.match(words), words
    for words in ("PLAY ALL", "Play Featurette", "ow", "Play Trailer"):
        assert not menus.PLAY_RE.match(words), words


def test_loose_reading_of_menu_words():
    """OCR of fancy lettering is a letter or two off (Muppets from Space): skip words and Play
    are matched loosely, but not so loosely that an extra goes with them."""
    for words in ("ALENT ewes", "TALENT, FILES", "Main MENU", "SCENE SELECTION", "AVAILABLE FROM COLUME"):
        assert menus._skip_text(words), words
    for words in ("DELETED SCENES", "Shining Star", "Radio Spot", "OUTTAKES", "Theatrical Trailer", "Production Story"):
        assert not menus._skip_text(words), words
    for words in ("RPUAY MOVIE.", "MOVIE", "-MOVIE", "PLAY MOV", "Play Movie"):
        assert menus._play_text(words), words
    for words in ("pray Mos", "Making of the Movie", "Movie Magic", "PLAY ALL"):
        assert not menus._play_text(words), words
    assert menus._edits("alent", "talent") == 1 and menus._near("alent", "talent")
    assert not menus._near("mos", "movie") and not menus._near("set", "sat")       # under 4 letters: exact
    assert menus._tidy("| ~ ac 1. TOTAL RECOIL * e") == "1. TOTAL RECOIL"
    assert menus._score("PLAY MOVIE") > menus._score("Nv pray Mos an")


def test_find_vlc(tmp_path):
    assert menus.find_vlc(str(tmp_path)) in (None,) + tuple(menus.VLC_DIRS)
    (tmp_path / "libvlc.dll").write_bytes(b"")
    assert menus.find_vlc(str(tmp_path)) == str(tmp_path)


def test_mrl():
    if os.name == "nt":
        assert menus.dvd_mrl("E:") == "dvd:///E:/"
    assert menus.dvd_mrl("/x/disc").startswith("dvd://")


def test_crawls_are_the_sessions_unless_kept_as_test_data(monkeypatch, tmp_path):
    """A stakeout's pictures and findings: in a temp folder of the session's own, gone when it
    ends (an installed app); kept by fingerprint as test data in a developer's checkout (temp/,
    which git ignores)."""
    monkeypatch.setenv("DISC_DETECTIVE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISC_DETECTIVE_TEST_DATA", "1")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(menus.__file__)))
    assert menus.menu_dir("ABC") == os.path.join(repo, "temp", "menus", "ABC")     # out of the case files
    monkeypatch.setenv("DISC_DETECTIVE_TEST_DATA", "0")
    d = menus.menu_dir("ABC")
    assert not d.startswith(str(tmp_path / "home")) and menus.menu_dir("DEF").startswith(os.path.dirname(d))
    os.makedirs(d)
    open(os.path.join(d, "page_1_2_1.png"), "wb").close()
    menus.end_session()
    assert not os.path.exists(os.path.dirname(d))
    assert menus.menu_dir("ABC") != d                        # (a new session, a new folder)
    menus.end_session()


def test_the_button_to_show_for_each_title(tmp_path):
    """Each title a menu button plays: the button to show (Edit titles' picture), on a page read,
    the one read best; nothing read, the first (Play rather than a FastPlay logo)."""
    def page(n, buttons, image=True):
        return {"domain": 1, "page": n, "program": 1, "route": [], "image": "page_1_%d_1.png" % n if image else None,
                "buttons": [dict({"n": i + 1, "rect": [10, 10 + 40 * i, 200, 40 + 40 * i]}, **b) for i, b in enumerate(buttons)]}
    plays = lambda t, ch=1: {"title": t, "chapter": ch}
    out = {"pages": [
        page(2, [{"text": "", "plays": plays(1)}, {"text": "pie 4", "plays": plays(1)},
                 {"text": "", "goes_to": {"page": 3}}]),
        page(3, [{"text": "Making Of", "plays": plays(5)}, {"text": "1. Opening", "plays": plays(1, 1)}]),
        page(4, [{"text": "Bloopers", "plays": plays(6)}], image=False)]}       # not read: no picture
    pics = menus.title_pictures(out, str(tmp_path))
    assert sorted(pics) == [1, 5]
    assert pics[5] == {"image": os.path.join(str(tmp_path), "page_1_3_1.png"), "rect": [10, 10, 200, 40],
                       "text": "Making Of", "button": [10, 10, 200, 40], "lit": False}
    assert pics[1]["text"] == "1. Opening"                     # read best: a scene's words, over none
    # the feature: the button nearest the menu the disc opens on, read well or not (WALL-E's main
    # menu Play, read as "DO", not a page further in that reads "Play Film")
    out["pages"][1]["route"] = [{"page": 2, "program": 1, "button": 3}]
    feature = menus.title_pictures(dict(out, found={"feature": 1}), str(tmp_path))[1]
    assert feature["image"].endswith("page_1_2_1.png") and feature["rect"] == [10, 10, 200, 40]
    out["pages"].pop(1)
    assert menus.title_pictures(out, str(tmp_path))[1]["rect"] == [10, 10, 200, 40]   # the first: Play


def test_a_main_movies_and_episodes_page_nearest_the_menu_is_read_whatever_else_names_it():
    """The search (the user's, 2026-11-19): the greedy plan as ever, and each main movie's and episode's page nearest
    the first menu (anchor_pages) read too, first, the pages on the way with it, though another page in the plan names
    its title -- an episode's nearest that isn't a Play All starting at it, a Play All's only if that's all."""
    holds = {"deep": {1, 2}, "main": {1}}
    assert menus.plan_pages(holds, {}) == ["deep"]                                # (greedy: one page names both)
    assert menus.plan_pages(holds, {}, must=["main"]) == ["main", "deep"]
    assert menus.plan_pages(holds, {"main": ["root"]}, must=["main"]) == ["root", "main", "deep"]   # (and its way)
    assert menus.plan_pages({"main": {1}}, {}, must=["main"]) == ["main"]                   # (its title not sought again)
    cands = [(1, "main", 0, False), (1, "scenes", 2, False),                     # the film: its Play on the main menu
             (5, "episodes", 1, False), (5, "menu", 0, True),                   # an episode: not its Play All's copy
             (6, "menu", 0, True),                                              # ... unless that's all there is
             (9, "menu", 0, False)]                                             # (an extra: not anchored)
    assert menus.anchor_pages({1: "main", 5: "episode", 6: "episode"}, cands) == ["main", "menu", "episodes"]


def test_an_episodes_picture_is_its_nearest_button_not_a_play_alls(tmp_path):
    """An episode's picture: its nearest button that isn't a Play All starting at it (one starting part way in, or
    saying Play All), a Play All's only when that's all there is; any other title read alike, its nearest."""
    page = lambda n, depth, buttons: {"domain": "bd", "page": n, "program": 1, "depth": depth, "route": [],
                                      "image": "p%d.png" % n, "buttons": buttons}
    near = page(1, 0, [{"n": 1, "rect": [0, 0, 10, 10], "text": "Play All", "plays": {"title": "00010.mpls", "chapter": 3}}])
    far = page(2, 1, [{"n": 2, "rect": [0, 20, 10, 30], "text": "Chuck Versus the Ex", "plays": {"title": "00010.mpls", "chapter": 1}}])
    pic = menus.title_pictures({"pages": [near, far]}, str(tmp_path), {"00010.mpls": "episode"})["00010.mpls"]
    assert pic["image"].endswith("p2.png")
    pic = menus.title_pictures({"pages": [near]}, str(tmp_path), {"00010.mpls": "episode"})["00010.mpls"]
    assert pic["image"].endswith("p1.png")                              # (only its Play All's: that, then)
    a = page(1, 0, [{"n": 1, "rect": [0, 0, 10, 10], "text": "Bloopers", "plays": {"title": "00020.mpls", "chapter": 1}}])
    b = page(2, 1, [{"n": 2, "rect": [0, 20, 10, 30], "text": "Bloopers", "plays": {"title": "00020.mpls", "chapter": 1}}])
    assert menus.title_pictures({"pages": [b, a]}, str(tmp_path))["00020.mpls"]["image"].endswith("p1.png")


def test_a_long_extras_picture_is_its_read_copy_not_an_unread_one_nearer_the_menu(tmp_path):
    """A title long enough to be a film but not the disc's film found (Order of the Phoenix's 44-minute "The Hidden
    Secrets of Harry Potter"), its button on two copies of a page: the copy read is its picture, though the other is
    nearer the first menu -- never "couldn't read its words" for a name read. The disc's own film keeps the copy
    nearest the menu (the test above)."""
    near = {"domain": 1, "page": 1, "program": 1, "route": [], "image": "page_1_1_1.png",
            "buttons": [{"n": 30, "rect": [10, 10, 200, 40], "text": "", "plays": {"title": 30, "chapter": 1, "seconds": 2635}}]}
    far = {"domain": 1, "page": 2, "program": 1, "route": [{"page": 1, "program": 1, "button": 9}], "image": "page_1_2_1.png",
           "buttons": [{"n": 33, "rect": [10, 50, 200, 80], "text": "The Hidden Secrets of Harry Potter",
                        "plays": {"title": 30, "chapter": 1, "seconds": 2635}}]}
    pic = menus.title_pictures({"pages": [near, far], "found": {"feature": 1}}, str(tmp_path))[30]
    assert pic["image"].endswith("page_1_2_1.png") and pic["text"] == "The Hidden Secrets of Harry Potter"


def test_a_titles_picture_outlines_the_words_read(tmp_path):
    """The outline on a title's picture: where its name was read, as the reading handed it on
    ("words": Enchanted's tall button areas, the lines in them), never looked for again; not known
    (a reading saved before), the button's area as the disc draws it. Both kept: "rect" outlined,
    "button" the disc's."""
    out = {"pages": [{"domain": 1, "page": 3, "program": 1, "route": [], "image": "page_1_3_1.png",
                      "buttons": [{"n": 1, "rect": [76, 285, 397, 349], "text": "Happy Working Song",
                                   "words": [148, 311, 350, 349], "plays": {"title": 38, "chapter": 1}},
                                  {"n": 2, "rect": [76, 351, 397, 384], "text": "That's How You Know",
                                   "plays": {"title": 39, "chapter": 1}}]}]}
    pics = menus.title_pictures(out, str(tmp_path))
    assert pics[38]["rect"] == [148, 311, 350, 349] and pics[38]["button"] == [76, 285, 397, 349]
    assert pics[39]["rect"] == pics[39]["button"] == [76, 351, 397, 384]       # (not known: the button's)
    # (a run of ads one button starts: each shown as its first is, where the button's words were read -- The
    # Sorcerer's Apprentice's Sneak Peeks)
    out = {"pages": [{"domain": 17, "page": 48, "program": 2, "route": [], "image": "page_17_48_2.png",
                      "buttons": [{"n": 3, "rect": [444, 360, 499, 409], "text": "Sneak Peeks", "words": [454, 341, 494, 379],
                                   "plays": {"title": 66, "chapter": 1, "after": [70, 74]}}]}]}
    pics = menus.title_pictures(out, str(tmp_path))
    assert pics[66]["rect"] == pics[70]["rect"] == pics[74]["rect"] == [454, 341, 494, 379]


def test_a_reading_says_where_it_was_read(monkeypatch):
    """button_text hands on where it read (Words.where, the disc's 720x480): RapidOCR's lines kept, at
    the picture's own scale; another reader's, the area it was given. A TV reading carries it to its
    picture (tv_pictures)."""
    from PIL import Image
    img = Image.new("RGB", (1440, 960), "white")
    monkeypatch.setattr(menus, "_rapid_button", lambda *a: ("Bloopers", (300, 200, 500, 240)))
    got = menus.button_text(img, [100, 90, 300, 130])
    assert got == "Bloopers" and menus.where_read(got) == [150, 100, 250, 120]
    monkeypatch.setattr(menus, "_button_text", lambda *a: "Bloopers")
    assert menus.where_read(menus.button_text(img, [100, 90, 300, 130])) == [96, 86, 304, 134]
    import pickle
    assert menus.where_read(pickle.loads(pickle.dumps(menus.Words("x", [1, 2, 3, 4])))) == [1, 2, 3, 4]
    tv = {"readings": [{"title": 5, "image": "p.png", "rect": [10, 10, 40, 40], "words": [50, 12, 300, 38], "text": "1) Pilot"}]}
    assert menus.tv_pictures(tv, "d")[5]["rect"] == [50, 12, 300, 38]



def test_a_titles_picture_outlines_a_name_read_beside_its_button(tmp_path):
    """A Blu-ray's marker named from beside it (Chuck's ">>", its episode's name a button over):
    the outline is where that name was read ("words"), not looked for again in the marker's box."""
    from PIL import Image
    Image.new("RGB", (720, 480), "white").save(str(tmp_path / "hdmv_00005_p0.png"))
    out = {"pages": [{"domain": "bd", "page": 0, "program": 1, "route": [], "image": "hdmv_00005_p0.png",
                      "buttons": [{"n": 1, "rect": [60, 150, 115, 180], "words": [130, 152, 420, 178],
                                   "text": "1) Chuck Versus the Pink Slip", "plays": {"title": 71, "chapter": 1}}]}]}
    pic = menus.title_pictures(out, str(tmp_path))[71]
    assert pic["rect"] == [130, 152, 420, 178] and pic["button"] == [60, 150, 115, 180]


def test_which_pages_a_round_trip_can_show():
    """A round trip (an arrow key and back) shows a page whose buttons lead to each other; a page
    whose buttons only lead on (or to an auto-action button) needs the one-way way."""
    play = {"op": "vts_title", "title": 1}
    chooser = [dict(_btn(1, play), right=2, left=2), dict(_btn(2, play), left=1, right=1)]   # FastPlay | Main Menu
    one_way = [_btn(1, play, down=2), _btn(2, play, down=3), _btn(3, play, down=1)]
    to_auto = [_btn(1, play, down=2, up=2), _btn(2, play, up=1, down=1, auto=True)]
    page = lambda pgc, btns: {"pgc": pgc, "cells": [{}], "buttons": btns, "program_buttons": [btns], "pre": []}
    structure = {"titles": [], "vmgm": [], "vtsm": {1: [page(1, chooser), page(2, one_way), page(3, to_auto)]}}
    c = object.__new__(menus._Crawler)
    c.pages = menus._pages(structure)
    assert c._returns((1, 1, 1)) and not c._returns((1, 2, 1)) and not c._returns((1, 3, 1))


def test_the_highlights_colours_and_the_grey_to_look_over():
    """A page's navigation packet says what colours its buttons light up in (each button's
    colour set; the page's palette, from its program): Wallace & Gromit's first menu a black
    asterisk, its Shorts page a white one. The check looks over the grey farthest from them:
    white, black; light and dark both, or unknown, mid-grey."""
    s = bytearray(2048)
    pci = 0x2D
    hli = pci + 0x60
    s[hli + 17] = 2                                         # two buttons
    s[hli + 22: hli + 26] = bytes([0x00, 0x10, 0x00, 0xF0])  # set 1 selected: pattern colour 1, opaque
    s[hli + 30: hli + 34] = bytes([0x00, 0x20, 0x00, 0xF0])  # set 2 selected: pattern colour 2
    s[hli + 46] = 0x40                                      # button 1: colour set 1
    s[hli + 46 + 18] = 0x80                                 # button 2: colour set 2
    buttons = menus._nav_buttons(bytes(s))
    assert [b["lit"] for b in buttons] == [[1], [2]]
    c = object.__new__(menus._Crawler)
    palette = [[0, 0, 0], [16, 16, 16], [209, 209, 209]] + [[128, 128, 128]] * 13
    c.pages = {(7, 25): {"buttons": [{"n": 1, "lit": [1]}], "palette": palette},
               (7, 26): {"buttons": [{"n": 1, "lit": [2]}], "palette": palette},
               (7, 27): {"buttons": [{"n": 1, "lit": [1]}, {"n": 2, "lit": [2]}], "palette": palette},
               (7, 28): {"buttons": [{"n": 1}]}}
    assert c._backdrop([(7, 25, 1)]) == 255 and c._backdrop([(7, 26, 1)]) == 0
    assert c._backdrop([(7, 27, 1)]) == 128 and c._backdrop([(7, 28, 1)]) == 128


def test_which_of_two_buttons_was_lit():
    """A two-button chooser fits a round trip from either button: the highlight (drawn over a
    button) says which it started on, lit before the press and not after."""
    from PIL import Image, ImageDraw
    play = {"op": "vts_title", "title": 1}
    chooser = [dict(_btn(1, play), rect=[100, 380, 260, 430], right=2, left=2),
               dict(_btn(2, play), rect=[440, 380, 600, 430], left=1, right=1)]
    structure = {"titles": [], "vmgm": [], "vtsm": {1: [
        {"pgc": 43, "cells": [{}], "buttons": chooser, "program_buttons": [chooser], "pre": []}]}}
    c = object.__new__(menus._Crawler)
    c.pages = menus._pages(structure)

    def frame(lit):
        img = Image.new("RGB", (720, 480), (20, 30, 120))
        ImageDraw.Draw(img).polygon([(lit[0] + 5, 395), (lit[0] + 20, 405), (lit[0] + 5, 415)], fill=(250, 220, 40))
        return img
    before, after = frame(chooser[1]["rect"]), frame(chooser[0]["rect"])     # Main Menu lit, then FastPlay
    assert c._lit((1, 43, 1), before, after, 2, 1) and not c._lit((1, 43, 1), before, after, 1, 2)

    # a dark highlight on lighter buttons (Wallace & Gromit's: a black asterisk by the name), the
    # buttons' pictures each their own colour: what's lit is what looks alike, before and after
    def menu(lit):
        img = Image.new("RGB", (720, 480), (190, 190, 120))
        ImageDraw.Draw(img).rectangle([110, 390, 250, 420], fill=(230, 220, 170))
        ImageDraw.Draw(img).rectangle([450, 390, 590, 420], fill=(240, 120, 60))
        ImageDraw.Draw(img).ellipse([lit[0] + 2, 395, lit[0] + 16, 409], fill=(10, 10, 10))
        return img
    before, after = menu(chooser[0]["rect"]), menu(chooser[1]["rect"])      # 1 lit, then 2
    assert c._lit((1, 43, 1), before, after, 1, 2) and not c._lit((1, 43, 1), before, after, 2, 1)

    # over a flat grey (the video blanked white, for a black highlight): an unlit button is that
    # grey, so the lit one is what stands out from it
    def blank(lit, level):
        img = Image.new("RGB", (720, 480), (level,) * 3)
        img.info.update(dark=True, level=level)
        ImageDraw.Draw(img).ellipse([lit[0] + 2, 395, lit[0] + 16, 409], fill=(16, 16, 16) if level else (209,) * 3)
        return img
    for level in (255, 0):
        before, after = blank(chooser[0]["rect"], level), blank(chooser[1]["rect"], level)
        assert c._lit((1, 43, 1), before, after, 1, 2) and not c._lit((1, 43, 1), before, after, 2, 1)


def test_a_pages_heading_says_what_its_titles_are():
    """Spider-Man's pages open with what they hold ("SCORP TRAILERS", "MUSIC VIDEOS", "SPOTS"
    with its TV misread); a hub's heading lists its kinds beside its buttons, and says nothing
    (The Sorcerer's Apprentice's "BONUS FEATURES ... Deleted Scene ...")."""
    assert menus.heading_kind("OSCO ag MUSIC VIDEOS HERO by Chad Kroeger") == "Music"
    assert menus.heading_kind("SCORP TRAILERS Les go eS al STAN LE") == "Trailer"
    assert menus.heading_kind("SPOTS Amazin OST Uu side Suit") == "Trailer"
    assert menus.heading_kind("DELETED SCENES") == "DeletedScene"
    assert menus.heading_kind("ez BONUS ATURES re Uimten nc Deleted Scene Discover") is None
    assert menus.heading_kind("SPECIAL FEATURES Commentaries Trailers Music Videos") is None
    assert menus.heading_kind("Trailers and Music Videos") is None                # two kinds: neither
    assert menus.heading_kind("") is None
    page = lambda n, heading, buttons: {"domain": 1, "page": n, "program": 1, "route": [], "heading": heading,
                                        "buttons": [dict({"n": i + 1}, **b) for i, b in enumerate(buttons)]}
    plays = lambda t, s=6307.5: {"title": t, "chapter": 1, "seconds": s}
    found = menus.menu_titles({"pages": [
        page(2, "", [{"text": "PLAY", "plays": plays(1)}, {"text": "2", "plays": plays(1)}]),
        page(17, "OSCO ag MUSIC VIDEOS", [{"text": "HERO by Chad Kroeger", "plays": plays(26, 206.0)}])]})
    assert [(e["title"], e["kind"]) for e in found["extras"]] == [(26, "Music")]
    titles = [{"id": "Title 1", "role": "feature"}, {"id": "Title 26", "role": "extra"}]
    assert menus.detected_labels(found, titles)["Title 26"]["type"] == "Music"
    assert "[Music]" in menus.found_text(found)


def test_two_cuts_play_alls_and_a_button_that_says_what_a_page_holds():
    """The Iron Giant: its Play page offers two cuts ("Signature Edition", "Original Edition"),
    both main movies; each deleted-scenes page has a Play All (left to the scan: a play-all) and
    no heading read, but the button that led there says "Deleted Scenes" (and "Next" after it
    says nothing)."""
    def page(n, buttons, route=()):
        return {"domain": 1, "page": n, "program": 1, "heading": "",
                "route": [{"page": p, "program": 1, "button": b} for p, b in route],
                "buttons": [dict({"n": i + 1}, **b) for i, b in enumerate(buttons)]}
    plays = lambda t, s=60.0, ch=1: {"title": t, "chapter": ch, "seconds": s}
    found = menus.menu_titles({"pages": [
        page(1, [{"text": "PLAY", "goes_to": {"page": 8}}, {"text": "& Deleted Scenes", "goes_to": {"page": 5}}]),
        page(8, [{"text": "SIGNATURE EDITION", "plays": plays(1, 5200.0)},
                 {"text": "ORIGINAL EDITION", "plays": plays(2, 5160.0)},
                 {"text": "4. The Giant", "plays": plays(1, 5200.0, 4)}], [(1, 1)]),
        page(5, [{"text": "Play Aly", "plays": plays(15, 600.0)}, {"text": "Hogarth's Bedroom", "plays": plays(16)},
                 {"text": "Next", "goes_to": {"page": 6}}], [(1, 2)]),
        page(6, [{"text": "Eating Metal", "plays": plays(19)}], [(1, 2), (5, 3)])]})
    assert found["feature"] == 1 and found["feature_name"] == "Signature Edition"
    assert found["play_alls"] == [15]
    assert [(e["title"], e["kind"]) for e in found["extras"]] == [(2, None), (16, "DeletedScene"), (19, "DeletedScene")]
    titles = [{"id": "Title 1", "role": "feature"}, {"id": "Title 2", "role": "version"},
              {"id": "Title 15", "role": "play-all"}, {"id": "Title 16", "role": "extra"},
              {"id": "Title 19", "role": "extra"}]
    labels = menus.detected_labels(found, titles)
    assert labels["Title 1"].pop("place")                       # (where its button is, for reading them in order)
    assert labels["Title 1"] == {"type": "MainMovie", "title": "Signature Edition", "note": "the menus: the feature"}
    assert labels["Title 2"]["type"] == "MainMovie" and labels["Title 2"]["title"] == "Original Edition"
    assert labels["Title 16"]["type"] == labels["Title 19"]["type"] == "DeletedScene"
    assert "Title 15" not in labels
    # a Play All the scan didn't catch: still not an ad
    titles[2]["role"] = "extra"
    assert "Title 15" not in menus.detected_labels(found, titles)
    # one cut only: the feature keeps no name (its Play button's words say nothing of a cut)
    alone = menus.detected_labels(found, [titles[0]])
    assert alone["Title 1"]["title"] is None
    assert "Play all: title 15" in menus.found_text(found)
    # WALL-E: its Bonus Features page, reached back from its Deleted Scenes page too: a button
    # back to a hub ends the way; the kinds before it aren't this page's
    bonus = [{"text": "Presto: Theatrical Short", "plays": plays(73, 318.0)},
             {"text": "Deleted Scenes", "goes_to": {"page": 25}}]
    found = menus.menu_titles({"pages": [
        page(8, [{"text": "Play", "plays": plays(30, 5893.0)}, {"text": "", "goes_to": {"page": 21}}]),
        page(21, bonus, [(8, 2)]),
        page(25, [{"text": "Dumped!", "plays": dict(plays(81, 90.0), others=[plays(80, 90.0)])},
                  {"text": "BONUS FEATURES", "goes_to": {"page": 22}}], [(8, 2), (21, 2)]),
        page(22, bonus, [(8, 2), (21, 2), (25, 2)])]})
    assert [(e["title"], e["kind"]) for e in found["extras"]] == [(73, None), (80, "DeletedScene"), (81, "DeletedScene")]
    # with its commentary switch on, Dumped! plays title 80: another version, to look at
    assert found["extras"][1]["name"] == "Dumped! (another version)"
    labels = menus.detected_labels(found, [{"id": "Title %d" % t, "role": "extra"} for t in (73, 80, 81)])
    assert labels["Title 80"]["type"] == "DeletedScene" and "worth a look" in labels["Title 80"]["note"]
    assert labels["Title 81"]["title"] == "Dumped!"


def test_a_tiny_buttons_cell():
    """Spider-Man's Trailers page: a pointer in each panel, its label round it; the cell reaches
    halfway to the next button each way."""
    rects = [[263, 154, 278, 183], [280, 242, 296, 268], [407, 170, 424, 200]]
    cell = menus._cell(rects[0], rects)
    assert cell[0] == 263 - menus.BESIDE                  # nothing to its left: as far as it reaches
    assert cell[2] == (278 + 407) // 2 and cell[3] == (183 + 242) // 2 and cell[1] == 154 - menus.ABOVE


def test_an_ad_is_named_as_an_ad():
    """Spider-Man 2's previews: a poster button with its film's name read is "Previews - Hitch";
    one with nothing read is "Previews"; the ad words come from what made it an ad (Enchanted's
    "Sneak Peeks"), so TheDiscDB's name keeps it an ad for everyone's scans."""
    page = lambda n, route_text, buttons: {"domain": 1, "page": n, "program": 1, "heading": "",
                                           "route": [{"page": 2, "program": 1, "button": 2}] if route_text else [],
                                           "buttons": [dict({"n": i + 1}, **b) for i, b in enumerate(buttons)]}
    plays = lambda t, s=6000.0: {"title": t, "chapter": 1, "seconds": s}
    for word, lead in (("PREVIEWS", "Previews"), ("SNEAK PEEKS", "Sneak Peeks")):
        found = menus.menu_titles({"pages": [
            page(2, "", [{"text": "PLAY", "plays": plays(1)}, {"text": word, "goes_to": {"page": 7}}]),
            page(7, word, [{"text": "HITCH", "plays": plays(17, 147.0)}, {"text": "", "plays": plays(11, 90.0)}])]})
        assert found["promos"] == [11, 17]
        assert {t: menus.ad_name(a) for t, a in found["promo_said"].items()} == {11: lead, 17: "%s - Hitch" % lead}
    titles = [{"id": "Title 1", "role": "feature"}, {"id": "Title 11", "role": "extra"}, {"id": "Title 17", "role": "extra"}]
    assert menus.detected_labels(found, titles)["Title 17"] == {"type": "Promo", "title": "Sneak Peeks - Hitch",
                                                                 "note": "the menus: an ad", "place": [2, 1]}
    # its own words unreadable: just the ad words
    junk = menus.plain_names(dict(found, promo_said={17: {"said": "PREVIEWS", "own": "Qxzv Wrrt"}}),
                             lambda w: w.lower() == "previews")
    assert menus.ad_name(junk["promo_said"][17]) == "Previews"


def test_trademark_signs_read_as_letters():
    """OCR reads ™ as w and ® as m ("Spider-Manw", "Ghost Riderm"): a word that isn't one with
    that last letter, and is without it, loses it; real words ending so keep theirs."""
    words = {"spider", "man", "ghost", "rider", "show", "me", "program", "friend", "or", "foe"}
    is_word = lambda w: w.lower() in words
    assert menus.unmarked("Spider-Manw: Friend or Foe", is_word) == "Spider-Man: Friend or Foe"
    assert menus.unmarked("Ghost Riderm", is_word) == "Ghost Rider"
    assert menus.unmarked("Show Me Program", is_word) == "Show Me Program"


def test_an_ad_that_says_its_an_ad():
    """Spider-Man 3: "Spider-Man: Friend or Foe Video Game Preview" names the ad though it says
    "Preview"; a button that says only "PREVIEWS" doesn't."""
    xs = lambda text: [{"text": text, "via": "PREVIEWS"}]
    assert menus.promo_name(xs("Spider-Man: Friend or Foe Video Game Preview")) == \
        "Previews - Spider-Man: Friend or Foe Video Game Preview"
    assert menus.promo_name(xs("PREVIEWS")) == "Previews"


def test_watch_a_title_in_vlc(monkeypatch, tmp_path):
    (tmp_path / "vlc.exe").write_text("")
    ran = []

    class Player:                               # a VLC window: open till killed
        def __init__(self, cmd, **kw):
            self.cmd, self.open = cmd, True
            ran.append(self)

        def poll(self):
            return None if self.open else 1

        def kill(self):
            self.open = False
    monkeypatch.setattr(menus.subprocess, "Popen", Player)
    assert menus.watch_title(str(tmp_path), "E:\\", 30)
    assert ran[0].cmd == [str(tmp_path / "vlc.exe"), menus.dvd_mrl("E:\\") + "#30"]
    # one window at a time: the next title closes the one before; and stop_watching closes it
    assert menus.watch_title(str(tmp_path), "E:\\", 31)
    assert [p.open for p in ran] == [False, True]
    menus.stop_watching()
    assert not ran[1].open
    menus.stop_watching()                       # (nothing open: nothing to do)
    monkeypatch.setattr(menus.shutil, "which", lambda name: None)
    assert not menus.watch_title(str(tmp_path / "nowhere"), "E:\\", 30)


def test_starting_over_by_dvd_menu(monkeypatch):
    # WALL-E: opening the disc again takes its warnings, a minute and more; VLC's DVD Menu goes
    # straight back to its main menu. Used while it leads there, else the disc's opened again
    monkeypatch.setattr(menus.time, "sleep", lambda t: None)
    calls = []

    class V:
        def menu(self):
            calls.append("dvd menu")
    c = object.__new__(menus._Crawler)
    c.v, c.menu_back, c.pages, c.log = V(), None, {}, lambda s: None
    monkeypatch.setattr(menus, "_lead_in", lambda pages, st: 0)
    lands = {"start": True}

    def find(cands, timeout=40):
        if lands["start"]:
            return cands[0], 1
        raise menus.Lost("not there")
    c.find = find
    c.close = lambda: calls.append("close")
    c.open = lambda: calls.append("open")
    c.at_start = lambda st: (st, 2)
    assert c.restart((7, 8, 1)) == ((7, 8, 1), 1) and calls == ["dvd menu"] and c.menu_back
    # a disc whose DVD Menu lands elsewhere (it opens on its VMG menu): opened again, from then on
    c.menu_back, lands["start"] = None, False
    calls.clear()
    assert c.restart((1, 2, 1)) == ((1, 2, 1), 2) and calls == ["dvd menu", "close", "open"]
    calls.clear()
    c.restart((1, 2, 1))
    assert calls == ["close", "open"]


def test_only_pages_the_disc_reaches_are_looked_among():
    # WALL-E: a decoy title set's page fitted the main menu's highlight moving. With every button
    # followed by the disc's program, only the pages it reaches are suspects
    states = [(7, 8, 1), (7, 21, 1), (3, 25, 1)]
    began = {"goes_to": (7, 7, 1), "menu_goes_to": (7, 8, 1)}
    engine = {(7, 8, 1, 3): [{"goes_to": (7, 21, 1)}]}
    assert menus._reachable(states, began, engine, [(7, 7, 1), (7, 21, 1)]) == [(7, 8, 1), (7, 21, 1)]
    # one button it couldn't follow: it may lead anywhere
    unsure = dict(engine)
    unsure[(7, 21, 1, 2)] = [{"unsure": "a random number"}]
    assert menus._reachable(states, began, unsure, [(7, 21, 1)]) is None
    assert menus._reachable(states, {}, {}, []) is None              # the program didn't run


def test_a_scenes_number_is_read_the_same_for_any_format():
    for text in ("Sc. 8-10", "Sc.9-3", "Scene 4", "Sc. B 29-30", "SC. 51", "Sc, 44"):
        assert menus.numbered_scene(text), text
    for text in ("Scenes", "Scene Selection", "Deleted Scenes", "Emmett scene after robbery", "Outtake Sc. 4", "Scene 4 Commentary", "Misc. 4", None):
        assert not menus.numbered_scene(text), text


def test_only_a_heading_saying_deleted_makes_a_page_of_deleted_scenes():
    assert menus.page_kind({"heading_line": "Deleted Scenes"}) == "DeletedScene"
    assert menus.page_kind({"heading": "Extended Scenes"}) == "DeletedScene"
    for heading in ("Scenes", "Declassified Scenes", "Cut Scenes", "Scene Selection", "Behind the Scenes"):
        assert menus.page_kind({"heading_line": heading}) is None, heading


def test_a_film_discs_button_saying_deleted_counts_and_scene_alone_doesnt():
    button = lambda n, text, title: {"n": n, "rect": [0, n, 9, n + 9], "text": text, "plays": {"title": title, "chapter": 1, "seconds": 90}}
    out = {"reader": "rapidocr", "pages": [{"domain": 1, "page": 2, "program": 1, "heading": "", "route": [], "buttons": [
        {"n": 1, "rect": [0, 0, 9, 9], "text": "Play Movie", "plays": {"title": 1, "chapter": 1, "seconds": 6000}},
        button(2, "Deleted Scenes", 2), button(3, "The Alternate Scene", 3), button(4, "Sc. 12 - At the Party", 4),
        button(5, "Theatrical Trailer", 5), button(6, "Music Video", 6)]}]}
    assert {e["title"]: e["kind"] for e in menus.menu_titles(out)["extras"]} == {2: "DeletedScene", 3: None, 4: None, 5: "Trailer", 6: "Music"}


def test_words_from_the_frame_without_the_subpicture(tmp_path, monkeypatch):
    """A page's words read from a frame without the subpicture (no highlight over them), and its
    picture too. Only when most read poorly that way, the frame as it is: its words may be in the
    subpicture (most of those it reads as words gone without it); then over black too, and the
    best of three headings."""
    from concurrent.futures import Future
    from PIL import Image

    def done(x):
        f = Future()
        f.set_result(x)
        return f
    lit, bare, dark = (Image.new("RGB", (720, 480), c) for c in ("white", "black", "red"))
    frame = {lit.tobytes()[:3]: "lit", bare.tobytes()[:3]: "bare", dark.tobytes()[:3]: "dark"}
    said = {}                                   # {(frame, button): what OCR reads}, and headings
    which = lambda img: frame[img.tobytes()[:3]]
    monkeypatch.setattr(menus, "button_words", lambda img, rect, rects: said.get((which(img), rect[1]), ""))
    monkeypatch.setattr(menus, "plate_words", lambda img, rects, only=None: {})
    monkeypatch.setattr(menus, "_page_lines", lambda img, scale=1: [])
    asked = []
    pool = type("Pool", (), {"submit": staticmethod(lambda fn, *a: asked.append(which(a[0])) or done(fn(*a)))})()
    logged = []
    c = object.__new__(menus._Crawler)
    c.out_dir, c.log, c.pool = str(tmp_path), logged.append, pool
    c.pages = {(7, 8): {"buttons": [{"n": 1, "rect": [0, 0, 9, 9]}, {"n": 2, "rect": [0, 20, 9, 29]}]}}
    monkeypatch.setattr(menus, "_state_buttons", lambda pages, st: pages[st[:2]]["buttons"])
    first = lambda: {n: done(said.get(("bare", r), "")) for n, r in ((1, 0), (2, 20))}

    # WALL-E: its words in the video, read well without the subpicture: nothing else read
    said.update({("bare", 0): "Play Movie", ("bare", 20): "Bonus Features", ("bare", "heading"): "Bonus"})
    got = c._finish((7, 8, 1), lit, None, 1, bare, dark, first())
    assert got == {"words": {1: "Play Movie", 2: "Bonus Features"}, "overlay": False, "picture": (bare, [])}
    assert asked == [] and Image.open(tmp_path / "page_7_8_1.png").getpixel((0, 0)) == (0, 0, 0)

    # read poorly without it, but not gone: the words are in the video; each the better reading
    # (a tie: without the subpicture, no highlight round it: Spider-Man 2's "(Costume Design)")
    said.clear()
    said.update({("bare", 0): "Costume Design", ("bare", 20): "Comc Cn", ("bare", 40): "Jnah",
                 ("lit", 0): "(Costume Design)", ("lit", 20): "Comic-Con Q & A", ("lit", 40): "J. Jonah Jameson"})
    c.pages[(7, 9)] = {"buttons": c.pages[(7, 8)]["buttons"] + [{"n": 3, "rect": [0, 40, 9, 49]}]}
    three = {n: done(said[("bare", r)]) for n, r in ((1, 0), (2, 20), (3, 40))}
    got = c._finish((7, 9, 1), lit, None, 1, bare, dark, three)
    assert got["words"] == {1: "Costume Design", 2: "Comic-Con Q & A", 3: "J. Jonah Jameson"} and not got["overlay"]

    # words drawn in the subpicture: gone without it. Read as it is and over black, the better
    # kept; its headings off the frame as it is, the picture its words were read off
    said.clear()
    said.update({("lit", 0): "Scene Selectlon", ("lit", 20): "Bonus Features", ("dark", 0): "Scene Selection",
                 ("dark", 20): "Bonus Feat", ("bare", "heading"): "MAIN MENU", ("lit", "heading"): "MAIN MENU 0 o"})
    asked.clear()
    c.pages[(1, 2)] = c.pages[(7, 8)]
    got = c._finish((1, 2, 1), lit, None, 1, bare, dark, first())
    assert got == {"words": {1: "Scene Selection", 2: "Bonus Features"}, "overlay": True, "picture": (lit, [])}
    assert "lit" in asked and "dark" in asked and any("in the subpicture" in m for m in logged)

    # the crawl asks: a page still being read; its first readings in (enough to route by, the
    # rest still under way: plates); then finished
    first_pending, pending = Future(), Future()
    c.reading, c.finished = {(1, 3, 1): {1: first_pending}}, {(1, 3, 1): pending}
    c.texts, c.overlay, c.pictures = {}, {}, {}
    assert not c.read((1, 3, 1)) and c.word_now((1, 3, 1), 1) == ""
    first_pending.set_result("Extra")
    assert c.read((1, 3, 1)) and c.word_now((1, 3, 1), 1) == "Extra" and c.first_words((1, 3, 1)) == {1: "Extra"}
    pending.set_result({"words": {1: "Extras"}, "overlay": False, "picture": (bare, [("EXTRAS", (0, 60, 9, 69), 0.99)])})
    assert c.word_now((1, 3, 1), 1) == "Extras" and c.words((1, 3, 1)) == {1: "Extras"}
    # its headings once what its buttons do is known: the words outside where their names were read
    monkeypatch.setattr(menus, "page_headings", lambda *a, **k: ("EXTRAS", False))
    assert c.headings((1, 3, 1), [{"n": 1, "rect": [0, 0, 9, 9], "text": "Extras"}]) == ("EXTRAS", "EXTRAS", False)
    assert c.headings((9, 9, 9), []) == ("", None, False)                       # (a page not read)
    # OCR's Play All, however it comes out; not a Play Again
    assert all(menus.PLAY_ALL_RE.search(t) for t in ("play A", "Play Aly", "PLAY aul", "Play All"))
    assert not any(menus.PLAY_ALL_RE.search(t) for t in ("Play Movie", "Play Again", "Display All"))

def test_highlight_checks_over_the_video_darkened():
    """WALL-E: the highlight (the subpicture's) shows over the video darkened, nothing moving
    behind it: checked that way first. A disc whose highlight is drawn in the video shows nothing
    darkened: after a few such misses, checked as it is only."""
    logged, calls = [], []

    class V:
        is_dark = False

        def dark(self, on, level=0):
            self.is_dark = on
    hit, miss = ([(menus.SEEN + 50, (7, 8, 1), 1, 2)], None, None), ([], None, None)
    c = object.__new__(menus._Crawler)
    c.v, c.log, c.dark_ok, c.dark_missed = V(), logged.append, None, 0
    shows = {"dark": True, "as is": True}

    def as_is(cands, key, back, weak=False):
        kind = "dark" if c.v.is_dark else "as is"
        calls.append(kind)
        return hit if shows[kind] else miss
    c._round_trip_as_is = as_is
    c._press_as_is = lambda cands, key, only=None: calls.append("press dark" if c.v.is_dark else "press") or hit
    assert c._round_trip([(7, 8, 1)], "down", "up") == hit and calls == ["dark"] and c.dark_ok
    assert c.v.is_dark                                      # (left darkened for the next check)
    c._press([(7, 8, 1)], "down")
    assert calls[-1] == "press dark"
    # shown darkened before: a miss (a page's buttons not on yet) tries as it is only every third
    shows["dark"] = False
    calls.clear()
    for _ in range(3):
        c._round_trip([(7, 8, 1)], "down", "up")
    assert calls == ["dark", "dark", "dark", "as is"] and c.dark_ok
    # that page, seen as it is only (Wallace & Gromit's first menu: a black asterisk, gone in the
    # dark): as it is first there from now on, and its presses too; dark still for other pages
    calls.clear()
    c._round_trip([(7, 8, 1)], "down", "up")
    c._press([(7, 8, 1)], "down")
    assert calls == ["as is", "press"]
    calls.clear()
    c._press([(7, 9, 1)], "down")
    assert calls == ["press dark"]
    shows["dark"] = True
    # drawn in the video: dark shows nothing, as it is does (page after page)
    c.dark_ok, shows["dark"] = None, False
    calls.clear()
    for _ in range(menus._Crawler.DARK_MISSES):
        c.seen_as_is = set()
        assert c._round_trip([(7, 8, 1)], "down", "up") == hit
    assert calls == ["dark", "as is"] * menus._Crawler.DARK_MISSES and c.dark_ok is False
    calls.clear()
    c._round_trip([(7, 8, 1)], "down", "up")
    c._press([(7, 8, 1)], "down")
    assert calls == ["as is", "press"]
    assert any("darkened (drawn in the video)" in m for m in logged)


def test_a_round_trip_between_buttons_others_overlap(monkeypatch):
    """WALL-E's main menu: buttons 8 and 9 overlap Bonus Features (3) and Set Up (4). The dot
    moving between 3 and 4 changes 8 and 9 as much; that isn't a change elsewhere."""
    from PIL import Image, ImageDraw
    monkeypatch.setattr(menus.time, "sleep", lambda t: None)
    go = {"op": "link_page", "to": 9}
    main = [dict(_btn(1, go), rect=[247, 177, 366, 236], down=3, up=4),
            dict(_btn(3, go), rect=[366, 245, 409, 288], up=1, down=4),
            dict(_btn(4, go), rect=[419, 278, 458, 325], up=3, down=1),
            dict(_btn(8, go), rect=[361, 254, 412, 313], up=1, down=4),
            dict(_btn(9, go), rect=[419, 265, 458, 308], up=3, down=1)]
    structure = {"titles": [], "vmgm": [], "vtsm": {7: [
        {"pgc": 8, "cells": [{}], "buttons": main, "program_buttons": [main], "pre": []}]}}
    dots = {3: (371, 257, 391, 281), 4: (428, 290, 448, 314), 1: (340, 186, 360, 210)}

    class V:
        sel = 3

        def frame(self):
            img = Image.new("RGB", (720, 480), (0, 0, 0))
            ImageDraw.Draw(img).ellipse(dots[self.sel], fill=(255, 255, 255))
            return img

        def press(self, key):
            self.sel = {b["n"]: b for b in main}[self.sel][key]
    c = object.__new__(menus._Crawler)
    c.pages, c.v = menus._pages(structure), V()
    res, _, _ = c._round_trip_as_is([(7, 8, 1)], "down", "up", weak=True)
    assert res[0][0] >= menus.SEEN and res[0][2:] == (3, 4)
    assert menus._count_beyond(Image.new("L", (10, 10), 255), [0, 0, 10, 10], [[0, 0, 5, 10]]) == 50


def test_a_page_whose_highlight_cant_be_seen(monkeypatch, tmp_path):
    """WALL-E's Home Theater pages: Back and Next are 4 pixels square, its other buttons go
    nowhere by arrow keys. No move there would show: the disc's program's word, untested."""
    from PIL import Image
    monkeypatch.setattr(menus.time, "sleep", lambda t: None)
    go = {"op": "link_page", "to": 4}
    wizard = [dict(_btn(i, go), rect=[230, 140 + 30 * i, 239, 149 + 30 * i]) for i in range(1, 7)] + [
        dict(_btn(7, go), rect=[369, 373, 373, 377], left=8, right=9),
        dict(_btn(8, go), rect=[334, 372, 338, 376]), dict(_btn(9, go), rect=[406, 372, 410, 376])]
    for b in wizard:                            # (an arrow key that goes nowhere: the button itself)
        for k in ("up", "down", "left", "right"):
            b[k] = b[k] or b["n"]
    structure = {"titles": [], "vmgm": [], "vtsm": {14: [
        {"pgc": 3, "cells": [{}], "buttons": wizard, "program_buttons": [wizard, wizard], "pre": []}]}}
    pages = menus._pages(structure)
    assert menus._unseen({b["n"]: b for b in menus._state_buttons(pages, (14, 3, 2))})
    big = [dict(b, rect=[300, 300 + 40 * i, 400, 330 + 40 * i]) for i, b in enumerate(wizard)]
    assert not menus._unseen({b["n"]: b for b in big})
    logged, read = [], []
    c = object.__new__(menus._Crawler)
    c.pages, c.log, c.reading = pages, logged.append, {}
    c.v = type("V", (), {"frame": lambda self: Image.new("RGB", (720, 480))})()
    c._read = lambda st, f1, f2, was: read.append(st)
    assert c.arrive((14, 3, 2), 7) == ((14, 3, 2), 7) and read == [(14, 3, 2)]
    assert "too small to see" in logged[0]


def test_dvd_menu_from_another_title_set(monkeypatch):
    # WALL-E: lost in its Home Theater pages (title set 14), DVD Menu leads to that title set's
    # menu, not the main menu (7): opened again this time, and DVD Menu kept for next time
    monkeypatch.setattr(menus.time, "sleep", lambda t: None)
    monkeypatch.setattr(menus, "_lead_in", lambda pages, st: 0)
    calls = []
    c = object.__new__(menus._Crawler)
    c.v = type("V", (), {"menu": lambda self: calls.append("dvd menu")})()
    c.menu_back, c.pages, c.log = None, {}, lambda s: None

    def lost(cands, timeout=40):
        raise menus.Lost("not there")
    c.find = lost
    c.close, c.open = (lambda: calls.append("close")), (lambda: calls.append("open"))
    c.at_start = lambda st: (st, 1)
    c.restart((7, 8, 1), where=(14, 3, 2))
    assert calls == ["dvd menu", "close", "open"] and c.menu_back is None


def test_a_discs_menu_data_is_kept_for_next_time(tmp_path, monkeypatch):
    structure = {"vmgm": [], "vtsm": {"7": [{"pgc": 8, "buttons": []}]}, "titles": [{"title": 30}]}
    assert menus.saved_structure("C9A1", home=str(tmp_path)) is None
    menus.save_structure("C9A1", structure, home=str(tmp_path))
    assert menus.saved_structure("C9A1", home=str(tmp_path)) == structure
    # read by an older Disc Detective, giving less: read again
    monkeypatch.setattr(menus, "STRUCTURE_VERSION", menus.STRUCTURE_VERSION + 1)
    assert menus.saved_structure("C9A1", home=str(tmp_path)) is None


def test_a_name_read_from_the_plate_its_printed_on():
    """Spider-Man's Trailers: each name on a white plate, its button a pointer at the plate's
    edge. The plate nearest a button, and nearer it than any other, is its label."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (720, 480), (30, 90, 70))
    d = ImageDraw.Draw(img)
    plates_at = [[170, 125, 330, 185], [345, 140, 480, 190]]
    for x0, y0, x1, y1 in plates_at:
        d.rectangle([x0, y0, x1, y1], fill=(240, 240, 240))
        d.rectangle([x0 + 20, y0 + 25, x0 + 26, y0 + 40], fill=(0, 0, 0))      # a letter's stroke
    rects = [[150, 150, 165, 180], [482, 160, 497, 185], [600, 400, 615, 430]]
    found = menus.plates(img)
    assert len(found) == 2 and all(abs(f[0] - p[0]) <= 6 and abs(f[3] - p[3]) <= 6 for f, p in zip(found, plates_at))
    near = {i for i, r in enumerate(rects) if any(menus._gap(r, k) < menus.PLATE_REACH for k in found)}
    assert near == {0, 1}                       # the third button has no plate by it
    # a plate's reading stands only as words, clearly better than the button's own
    assert menus._plate_or("iene", "STUART LITTLE") == "STUART LITTLE"
    assert menus._plate_or("lien", "TFET IT I") == "lien"                   # junk for junk: no
    assert menus._plate_or("Amazing", "Amazing!") == "Amazing"              # no better: no
    assert menus._plate_or("at", None) == "at"


def test_an_ad_buttons_run_of_titles_are_all_ads(tmp_path):
    """The Sorcerer's Apprentice: Sneak Peeks plays 66, then 70, 74... one after another; all ads,
    each named by the button (its picture too). A Play All's run is its scenes' own business."""
    def page(n, buttons):
        return {"domain": 17, "page": n, "program": 2, "heading": "", "route": [], "image": "page_%d.png" % n,
                "buttons": [dict({"n": i + 1, "rect": [0, 40 * i, 50, 40 * i + 30]}, **b) for i, b in enumerate(buttons)]}
    plays = lambda t, s=60.0, after=(): dict({"title": t, "chapter": 1, "seconds": s}, **({"after": list(after)} if after else {}))
    out = {"pages": [
        page(48, [{"text": "Movie", "plays": plays(35, 6540.0)}, {"text": "Movie", "plays": plays(35, 6540.0, ())},
                  {"text": "Peeks", "plays": plays(66, 68.3, (70, 74))}]),
        page(6, [{"text": "Play All", "plays": plays(38, 159.0, (22,))}, {"text": "Campfire", "plays": plays(22, 337.0)}])]}
    found = menus.menu_titles(out)
    assert found["promos"] == [66, 70, 74]
    assert {menus.ad_name(found["promo_said"][t]) for t in (66, 70, 74)} == {"Sneak Peeks"}
    assert [e["title"] for e in found["extras"]] == [22] and found["play_alls"] == [38]
    pics = menus.title_pictures(out, str(tmp_path))
    assert pics[70]["text"] == pics[66]["text"] == "Peeks"
    assert "then titles 70, 74" in menus.crawl_text(dict(out, found=found))


def test_a_play_all_known_by_its_page_not_its_lettering():
    """Enchanted's sneak peeks: a button whose lettering reads "PLAY aul" plays a run of reels
    (copies the scan sets aside) on a page of buttons each playing a sneak peek of its own: the
    page's Play All, not ads named "Play aul". A run from a page of other things isn't one."""
    def page(n, buttons, heading=""):
        return {"domain": 12, "page": n, "program": 1, "heading": heading, "route": [], "image": None,
                "buttons": [dict({"n": i + 1, "rect": [0, 0, 1, 1]}, **b) for i, b in enumerate(buttons)]}
    plays = lambda t, s=90.0, after=(): dict({"title": t, "chapter": 1, "seconds": s}, **({"after": list(after)} if after else {}))
    found = menus.menu_titles({"pages": [
        page(18, [{"text": "Play", "plays": plays(42, 6400.0)}, {"text": "Sneak Peeks", "goes_to": {"page": 34}}]),
        page(34, [{"text": "PLAY aul", "plays": plays(56, 178.0, (20, 57, 21))},
                  {"text": "Sneak Peeks Ariel's Beginning", "plays": plays(54)},
                  {"text": "Sneak Peeks Tinker Bell", "plays": plays(18)}])]})
    assert found["play_alls"] == [20, 21, 56, 57] and found["promos"] == [18, 54]
    assert "PLAY aul" not in " ".join(menus.ad_name(a) for a in found["promo_said"].values())


def test_a_menus_bullet_icons_read_as_digits():
    # The Iron Giant: a bullet before each name, read as "3" or "6", again and again: not the name's
    texts = ["play A", "6 The Voice of the Giant", "3 Hogarth Hughes", "3 Opening Sequence", "6 The Deer"]
    icons = menus._icon_digits(texts)
    assert icons == {"3", "6"} and menus._unicon("3 Kent Mansley", icons) == "Kent Mansley"
    assert menus._icon_digits(["1 Opening", "2 Middle", "3 End"]) == set()          # counting up: a list's
    assert menus._icon_digits(["3 Blind Mice", "Play Movie"]) == set()             # a name's own number


def test_names_put_right_by_the_films_credits():
    """A misread name of the film's own people put right (The Iron Giant's "Hagart Hughs"): a
    whole name word for word, or a word that's no word a letter off one of theirs. Not a near
    miss of a word that is one, nor a long reach on a short word ("ARIELS" isn't Daniels)."""
    people = ["Eli Marienthal", "Hogarth Hughes", "Christopher McDonald", "Kent Mansley", "Amy Adams", "Daniels",
              "Dean McCoppin (voice)"]
    is_word = lambda w: w.lower() in {"and", "beginning", "kent"}
    fix = lambda t: menus.credit_fix(t, people, is_word)
    assert fix("Hagart Hughs") == "Hogarth Hughes" and fix("Kent Mansly") == "Kent Mansley"
    assert fix("4. Jonan Mansley,") == "4. Jonan Mansley,"                   # (no Jonah in these credits)
    assert menus.credit_fix("4. Jonan Jameson", ["J. Jonah Jameson", "Marie"], is_word) == "4. Jonah Jameson"
    assert menus.credit_fix("Mutants: & Marve", ["J. Jonah Jameson", "Marie"], is_word) == "Mutants: & Marve"
    assert fix("KENT MANSLY") == "KENT MANSLEY"                              # in the reading's capitals
    assert fix("ARIELS BEGINNING") == "ARIELS BEGINNING" and fix("Dean MGCOPRR") == "Dean MGCOPRR"
    assert fix("Storyboards and Animatics") == "Storyboards and Animatics"  # (extras' own words)
    # a possessive stays one: Enchanted's "Nathaniel's Revelation" isn't "Nathaniel Revelation"
    enchanted = ["Nathaniel", "Prince Edward", "Conan O'Brien"]
    assert menus.credit_fix("Nathaniel's Revelation", enchanted, is_word) == "Nathaniel's Revelation"
    assert menus.credit_fix("Nathanel's Revelation", enchanted, is_word) == "Nathaniel's Revelation"
    assert menus.credit_fix("A Lock Of Prince Edward's Hair", enchanted, is_word) == "A Lock Of Prince Edward's Hair"
    assert menus.credit_fix("Conan O'Brlen", enchanted, is_word) == "Conan O'Brien"
    # a name's plural or possessive is read right: Order of the Phoenix's "The Centaurs", "The Weasleys'"
    potter = ["Centaur", "Mr. Weasley", "Thestral"]
    assert menus.credit_fix("The Centaurs Take Umbridge", potter, is_word) == "The Centaurs Take Umbridge"
    assert menus.credit_fix("The Weasleys' Fireworks", potter, is_word) == "The Weasleys' Fireworks"
    assert menus.credit_fix("The Thestrels", potter, is_word) == "The Thestrals"
    found = {"feature": 1, "promos": [9], "promo_said": {9: {"said": "Previews", "own": "Ghost Riderm"}},
             "extras": [{"title": 23, "name": "Hagart Hughs", "seconds": 60, "listed": None, "kind": None}]}
    fixed = menus.correct_names(found, people, is_word, lambda t: "Ghost Rider" if t == "Ghost Riderm" else t)
    assert fixed["extras"][0]["name"] == "Hogarth Hughes" and fixed["extras"][0]["read"] == "Hagart Hughs"
    assert menus.ad_name(fixed["promo_said"][9]) == "Previews - Ghost Rider" and fixed["promo_read"][9] == "Ghost Riderm"
    labels = menus.detected_labels(fixed, [{"id": "Title 1", "role": "feature"}, {"id": "Title 23", "role": "extra"},
                                           {"id": "Title 9", "role": "extra"}])
    assert labels["Title 23"]["title"] == "Hogarth Hughes" and 'read as "Hagart Hughs"' in labels["Title 23"]["note"]
    assert labels["Title 23"]["read"] == "Hagart Hughs" and labels["Title 9"]["read"] == "Ghost Riderm"
    assert labels["Title 9"]["title"] == "Previews - Ghost Rider" and "Ghost Riderm" in labels["Title 9"]["note"]


def test_windows_ocr_first_then_tesseract(monkeypatch):
    """A button's words: Windows' own OCR first; its reading stands unless it has no words in
    it (junk, or nothing), and then Tesseract reads it as before."""
    from PIL import Image
    img = Image.new("RGB", (720, 480), "white")
    asked = []
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: None)  # (no RapidOCR)
    monkeypatch.setattr(menus, "windows_text", lambda im: asked.append(im.size) or "Spanglish")
    monkeypatch.setattr(menus.shutil, "which", lambda name: None)          # (no Tesseract at all)
    assert menus.button_text(img, [100, 100, 200, 130]) == "Spanglish"
    assert asked == [(3 * 108, 3 * 38)]                                     # the button's area, 3x
    for junk in ("", "~ |", None):                                          # no words: Tesseract's turn
        monkeypatch.setattr(menus, "windows_text", lambda im, j=junk: j)
        assert menus.button_text(img, [100, 100, 200, 130]) == ""


def test_rapidocr_first(monkeypatch):
    """RapidOCR, where it's installed, reads first, the button's area as it is (its own size);
    its reading stands unless it has no words in it, and then Windows' OCR has its turn."""
    from PIL import Image
    img = Image.new("RGB", (720, 480), "white")
    asked = []
    line = lambda t: [(t, (4, 4, 100, 30), 0.99)]
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: asked.append(im.size) or line("Nathaniel's Revelation"))
    monkeypatch.setattr(menus, "windows_text", lambda im: "Ngthaniel Rgvelaion")
    assert menus.button_text(img, [100, 100, 200, 130]) == "Nathaniel's Revelation"
    assert asked == [(108, 38)]
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: line("~ |"))
    assert menus.button_text(img, [100, 100, 200, 130]) == "Ngthaniel Rgvelaion"
    # no text found there at all, sure or not: nothing to read; the others aren't asked to make
    # words of the backdrop (Spider-Man's title 28, a dark shirt)
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: [])
    assert menus.button_text(img, [100, 100, 200, 130]) == ""


def test_a_heading_dipping_into_a_button(monkeypatch):
    """Enchanted's music page: its heading "Fantasy / Comes To Life" over two lines, the second
    dipping into the "Happy Working Song" button's area. Read off the whole page, that line is
    the heading's (straddling the button's edge, stacked on a line outside every button) and
    left out of the button's words; a line well inside a button stays, under a heading or not."""
    from PIL import Image
    img = Image.new("RGB", (720, 480), "white")
    rects = [[76, 285, 397, 349], [76, 351, 397, 384], [76, 386, 397, 426], [412, 398, 679, 450]]
    page = [("Fantasy", (182, 237, 295, 285), 0.99), ("Comes To Life", (139, 268, 343, 317), 0.97),
            ("Happy Working Song", (148, 311, 350, 353), 0.94), ("That's How You Know", (150, 346, 354, 380), 0.97)]
    monkeypatch.setattr(menus, "_page_lines", lambda im: page)

    def crop_lines(im):                     # the button's area: the page's lines in it, its own pixels
        return [(t, (b[0] - 72, max(0, b[1] - 281), b[2] - 72, min(im.height, b[3] - 281)), s)
                for t, b, s in page[1:3]] if im.height > 60 else []
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: crop_lines(im))
    assert menus._heading_boxes(img, rects[0], rects, (720, 480)) == [(139, 268, 343, 317)]
    assert menus.button_text(img, rects[0], others=rects) == "Happy Working Song"
    assert menus.button_text(img, rects[0]) == "Comes To Life Happy Working Song"   # (its page unknown)
    # "The Jane Austen" well inside its button, right under "PREVIEWS": the button's
    page = [("PREVIEWS", (223, 45, 496, 88), 0.94), ("The Jane Austen", (159, 95, 336, 125), 0.99)]
    assert menus._heading_boxes(img, [108, 93, 457, 152], [[112, 154, 429, 208]], (720, 480)) == []


def test_rapidocr_reads_a_label():
    """The real thing, where RapidOCR is installed (lines top to bottom)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (480, 140), (60, 60, 60))
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except OSError:
        pytest.skip("no Arial")
    ImageDraw.Draw(img).text((20, 15), "DINOSAURS ROAM", font=font, fill="white")
    ImageDraw.Draw(img).text((20, 75), "ONCE AGAIN", font=font, fill="white")
    got = menus.rapid_text(img)
    if got is None:
        pytest.skip("no RapidOCR here")
    assert got == "DINOSAURS ROAM ONCE AGAIN"


def test_windows_ocr_reads_a_label():
    """The real thing, where there is one (Windows with the winrt packages)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (480, 90), "white")
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except OSError:
        pytest.skip("no Arial")
    ImageDraw.Draw(img).text((20, 20), "Bonus Features", font=font, fill="black")
    got = menus.windows_text(img)
    if got is None:
        pytest.skip("no Windows OCR here")
    assert got.strip().lower() == "bonus features"


def test_windows_ocr_judged_by_real_words(monkeypatch):
    """With a dictionary (a stakeout's: the IMDb index), Windows' OCR reads a button twice, as
    it is and its bright lettering alone, and the reading with more real words stands: Jurassic
    World's white lettering over a busy picture, "OSAURS ROAM ONCE" as it is. Without one, once."""
    from PIL import Image
    img = Image.new("RGB", (720, 480), "white")
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: None)  # (no RapidOCR)
    reads = iter(["OSAURS ROAM ONCE", "DINOSAURS ROAM ONCE AGAIN"])
    monkeypatch.setattr(menus, "windows_text", lambda im: next(reads))
    words = {"dinosaurs", "roam", "once", "again"}
    monkeypatch.setattr(menus, "WORDS", lambda w: w.lower() in words)
    assert menus.button_text(img, [100, 100, 300, 160]) == "DINOSAURS ROAM ONCE AGAIN"
    # a tie on real words less the rest: the one with more real words (Martin & Short's)
    reads = iter(["MARTIN & SHORT IVITRVIEW FAC", "MARTIN UCH"])
    monkeypatch.setattr(menus, "WORDS", lambda w: w.lower() in {"martin", "short"})
    assert menus.button_text(img, [100, 100, 300, 160]) == "MARTIN & SHORT IVITRVIEW FAC"
    # no dictionary: one reading, as it is
    asked = []
    monkeypatch.setattr(menus, "WORDS", None)
    monkeypatch.setattr(menus, "windows_text", lambda im: asked.append(1) or "PLAY MOVIE")
    assert menus.button_text(img, [100, 100, 300, 160]) == "PLAY MOVIE" and len(asked) == 1


def test_a_word_checker_for_any_thread(tmp_path):
    """core.word_checker: the IMDb index as a dictionary, usable from a stakeout's reading
    threads (its own read-only connection); None without an index."""
    import threading
    from disc_detective import core
    import sqlite3
    assert core.word_checker(str(tmp_path)) is None and core.word_checker(None) is None
    con = sqlite3.connect(str(tmp_path / core.IMDB_DB))                    # a tiny index: one title
    try:
        con.execute("CREATE VIRTUAL TABLE titles_fts USING fts5(title)")
    except sqlite3.OperationalError:
        pytest.skip("no FTS5 in this sqlite")
    con.execute("CREATE TABLE ratings (id INTEGER PRIMARY KEY, votes INTEGER)")
    con.execute("INSERT INTO titles_fts (rowid, title) VALUES (1, 'Dinosaurs Roam Once Again')")
    con.execute("INSERT INTO ratings VALUES (1, ?)", (core.WORD_VOTES,))
    con.commit()
    con.close()
    is_word = core.word_checker(str(tmp_path))
    got = []
    t = threading.Thread(target=lambda: got.append((is_word("dinosaurs"), is_word("osaurs"))))
    t.start()
    t.join()
    assert got == [(True, False)]


def test_a_pages_heading_read_by_the_best_reader(monkeypatch):
    """A page's heading (its words outside the buttons): RapidOCR's, where it's installed; else
    Windows' OCR's or Tesseract's, whichever is cleaner (more of it real words), not longer:
    Tesseract's junk is full of scraps a dictionary knows ("is Ss lf LIN" against "BONUS")."""
    from PIL import Image
    img = Image.new("RGB", (720, 480), "white")
    rects = [[100, 200, 300, 240]]
    monkeypatch.setattr(menus, "WORDS", lambda w: w.lower() in {"bonus", "is", "lin", "sneak", "peeks"})
    monkeypatch.setattr(menus, "rapid_text", lambda im: "Sneak Peeks")
    monkeypatch.setattr(menus, "windows_text", lambda im: "BONUS")
    assert menus.page_text(img, rects) == "Sneak Peeks"
    monkeypatch.setattr(menus, "rapid_text", lambda im: None)
    monkeypatch.setattr(menus, "_tesseract_page", lambda *a: "is Ss lf LIN Sania ra Wi ite oh SP")
    assert menus.page_text(img, rects) == "BONUS"
    monkeypatch.setattr(menus, "windows_text", lambda im: None)
    assert menus.page_text(img, rects) == "is Ss lf LIN Sania ra Wi ite oh SP"     # (only Tesseract)


def test_finding_the_highlight_keeps_off_auto_buttons():
    """find, the highlight's button unknown, presses the round trips that can't land on an
    auto-action button first (selecting it presses it): Muppets from Space's Special Features,
    where "up" from the music video lands on one, re-opening the page and leaving every press
    after it a button off. None safe from every button: the least risky first."""
    btn = lambda n, rect, up, down, left, right, auto=False: {
        "n": n, "rect": rect, "up": up, "down": down, "left": left, "right": right, "auto": auto, "cmd": {}}
    page = [btn(1, [263, 138, 391, 196], 8, 3, 8, 2), btn(2, [403, 117, 541, 168], 8, 3, 1, 3),
            btn(3, [391, 168, 641, 235], 9, 4, 2, 4), btn(4, [381, 235, 617, 292], 3, 5, 3, 5),
            btn(8, [370, 401, 646, 442], 4, 9, 4, 9), btn(9, [378, 92, 387, 100], 1, 1, 1, 1, auto=True)]
    c = object.__new__(menus._Crawler)
    c.pages = menus._pages({"titles": [], "vmgm": [], "vtsm": {1: [
        {"pgc": 6, "cells": [{}], "buttons": page, "program_buttons": [page], "pre": []}]}})
    trips = c._safe_trips([(1, 6, 1)])
    assert trips[0] == ("right", "left") and trips[-1] in (("down", "up"), ("up", "down"))
    plain = [btn(1, [0, 0, 9, 9], 2, 2, 2, 2), btn(2, [0, 20, 9, 29], 1, 1, 1, 1)]
    c.pages = menus._pages({"titles": [], "vmgm": [], "vtsm": {1: [
        {"pgc": 1, "cells": [{}], "buttons": plain, "program_buttons": [plain], "pre": []}]}})
    assert c._safe_trips([(1, 1, 1)]) == list(menus._Crawler.ROUND_TRIPS)   # (all safe: as before)


def test_tiny_buttons_named_by_the_pages_layout(monkeypatch):
    """Spider-Man's TV Spots: slivers of buttons at the thumbnails' corners, each name under its
    thumbnail, left of its sliver. Read off the whole page (twice the size), names stacked over
    lines kept together; the page's layout learned from the nearest pairs, then each button the
    name nearest where its layout says (nearest first alone gave "Blockbuster" to "Suit Up")."""
    from PIL import Image
    img = Image.new("RGB", (720, 480), "white")
    rects = [[232, 154, 247, 182], [317, 150, 332, 176], [389, 152, 404, 180], [236, 242, 251, 271],
             [317, 236, 332, 265], [393, 238, 408, 266], [450, 368, 465, 395]]
    page = [("Amazing", (161, 163, 240, 180), 0.99), ("Epic", (272, 160, 311, 179), 1.0),
            ("Thread", (355, 160, 403, 177), 0.98), ("of Hope", (341, 168, 406, 187), 0.9),
            ("Upside", (194, 252, 237, 270), 0.94), ("Down", (190, 263, 235, 283), 0.89),
            ("Suit Up", (260, 246, 328, 266), 0.89), ("Blockbuster", (331, 239, 425, 261), 0.95),
            ("Countdown", (345, 249, 423, 271), 0.97), ("Thursday", (343, 261, 416, 280), 0.9),
            ("Review #1", (336, 322, 421, 350), 0.91), ("MARKETING", (408, 350, 491, 378), 0.95)]
    monkeypatch.setattr(menus, "_page_lines", lambda im, scale=1: page)
    got = menus._tiny_labels(img, rects)
    assert [got.get(tuple(r)) for r in rects[:6]] == ["Amazing", "Epic", "Thread of Hope", "Upside Down",
                                                      "Suit Up", "Blockbuster Countdown Thursday"]
    assert got.get(tuple(rects[6])) != "Review #1 MARKETING"         # (a panel under it, not stacked)
    # (without the text grouping: a blank page, no lettering to group -- test_menu_pipelines has it grouped)
    monkeypatch.setattr(menus, "TEXT_BLOCKS", False)
    assert menus.button_words(img, rects[1], rects) == "Epic"
    # where it was read, handed on with it: the name it was read from, not the sliver's own area
    assert menus.where_read(menus.button_words(img, rects[1], rects)) == [272, 160, 311, 179]
    monkeypatch.setattr(menus, "_page_lines", lambda im, scale=1: None)  # (no RapidOCR: as before)
    assert menus._tiny_labels(img, rects) == {}


def test_a_name_read_by_rapidocr_needs_only_letters_here(tmp_path):
    """Read by RapidOCR, a name with letters is kept for plain_names to judge with the dictionary
    (Spider-Man's trailer "xXx": _wordy, made for Tesseract's junk, takes it for junk)."""
    page = {"domain": 1, "page": 9, "program": 16, "route": [], "image": None, "buttons": [
        {"n": 1, "rect": [0, 0, 9, 9], "text": "Play Movie", "plays": {"title": 1, "chapter": 1, "seconds": 7000}},
        {"n": 2, "rect": [0, 20, 9, 29], "text": "xXx", "plays": {"title": 9, "chapter": 1, "seconds": 92}}]}
    named = lambda out: {e["title"]: e["name"] for e in menus.menu_titles(out)["extras"]}
    assert named({"pages": [page], "reader": "rapidocr"})[9] == "xXx"
    assert named({"pages": [page]})[9] == ""                           # (Tesseract's: as before)


def test_a_trailers_page_of_other_films_is_ads():
    """The one rule for other films' trailers (menus.other_films), Spider-Man's two pages of them: its
    Trailers page, the film's own among them, the others ads ("Previews - Men in Black II", the well-known
    film's name as known_films looked it up; Stan Lee's Mutants as read); its TV Spots page (its heading
    says, "via"), one named like a film ("Epic"): the film's own spots."""
    films = {"xXx": "xXx", "Mr. Deeds": "Mr. Deeds", "Men in Black Ii": "Men in Black II", "Epic": "Epic",
             "Spider-man": "Spider-Man"}
    t = lambda n, name, page: {"title": n, "name": name, "seconds": 60, "listed": None, "kind": "Trailer", "page": page,
                               "via": "SPECIAL FEATURES " + ("TRAILERS" if page[2] == 16 else "TV SPOTS")}
    found = {"feature": 1, "promos": [], "promo_said": {}, "extras": [
        t(8, "Spider-man", [1, 9, 16]), t(9, "xXx", [1, 9, 16]), t(10, "Stan Lee's Mutants", [1, 9, 16]),
        t(11, "Mr. Deeds", [1, 9, 16]), t(12, "Men in Black Ii", [1, 9, 16]),
        t(14, "Amazing", [1, 9, 18]), t(15, "Epic", [1, 9, 18]), t(16, "Thread of Hope", [1, 9, 18]),
        t(17, "Suit Up", [1, 9, 18])]}
    found = menus.known_films(found, films.get, "Spider-Man")
    assert "names_film" not in found["extras"][0]                   # (the film's own: not looked up)
    titles = [{"id": "Title %d" % n, "role": "feature" if n == 1 else "extra"} for n in (1, 8, 9, 10, 11, 12, 14, 15, 16, 17)]
    labels = menus.detected_labels(found, titles, film="Spider-Man")
    assert sorted(int(t.split()[1]) for t, x in labels.items() if x["type"] == "Promo") == [9, 10, 11, 12]
    assert labels["Title 12"]["title"] == "Previews - Men in Black II"
    assert labels["Title 10"]["title"] == "Previews - Stan Lee's Mutants"
    assert {labels["Title %d" % n]["type"] for n in (8, 14, 15, 16, 17)} == {"Trailer"}
    assert menus.detected_labels(found, titles)["Title 9"]["type"] == "Trailer"     # (no film known: as found)


def test_play_all_as_ocr_reads_it():
    for text in ("Play All", "PLAY aul", "Play Aly", "play A", "Piay All", "P1ay All"):
        assert menus.PLAY_ALL_RE.search(text), text
    for text in ("Play Again", "Display", "Play Movie", "Pilot", "Pray All Day"):
        assert not menus.PLAY_ALL_RE.search(text), text


def test_a_buttons_more_or_back_names_no_film():
    # (LOTR_FELLOWSHIP_TH_D2's "more" button: "more" found Strangers No More and Mary No More)
    for words in ("more", "MORE", "back", "more back", "next"):
        assert menus._menu_words(words), words
    assert not menus._menu_words("More American Graffiti")
    assert menus.trim_menu_words("Two Wizards more back") == "Two Wizards"


def test_a_pal_discs_buttons_are_on_the_frames_the_crawl_sees():
    # (Friends S1 D1's main menu, a UK disc: Play and Episodes, at the words in a 720x480 frame)
    progs = [[{"n": 1, "rect": [218, 478, 260, 520]}, {"n": 2, "rect": [269, 478, 344, 521]}], []]
    menus._to_frame(progs)
    assert progs[0][0]["rect"] == [218, 398, 260, 433] and progs[0][1]["rect"] == [269, 398, 344, 434]
    ifo = bytearray(0x102)
    assert not menus._pal_menus(bytes(ifo))                 # NTSC
    ifo[0x100] = 0x10 | 0x0C
    assert menus._pal_menus(bytes(ifo))
    assert not menus._pal_menus(b"\x00" * 16)               # (too short to say)


def test_an_ad_buttons_words_read_into_the_heading_dont_make_its_page_all_ads():
    trailer = {"text": "THEATRICAL TRAILER"}
    peeks = {"text": "SNEAK PEEKS"}
    # (Rush Hour 3's: the heading's the buttons' words, the last cut short)
    assert not menus._ads_page(["SPECIAL FEATURES", ""], "THEATRICAL TRAILE SNEAK PEEI SpEcIAl FATURES AIN MENU", [trailer, peeks])
    assert not menus._ads_page([], "SNEAK PEEKS", [trailer, peeks])
    # its own heading, the buttons naming films: a page of ads
    assert menus._ads_page([], "Sneak Peeks", [{"text": "Hitch"}, {"text": "xXx"}])
    assert menus._ads_page([], "Coming Soon", [trailer, peeks])     # (words no button has)
    # the way there says so, whatever the page's own words
    assert menus._ads_page(["Special Features", "Sneak Peeks"], "", [trailer, peeks])
    assert menus._ads_page(["Previews"], None, [{"text": "Hitch"}])
    assert not menus._ads_page(["Bonus"], "Deleted Scenes", [{"text": "Hitch"}])


def test_a_discs_menus_as_phrases(tmp_path, monkeypatch):
    """What a disc's menus show, to name it: a kept stakeout's page pictures read as cover art
    (no drive needed); the menus' own words left out ("PLAY", "WITH ENGLISH SUBTITLES": films
    are called Play), and packaging's."""
    from disc_detective import core
    d = tmp_path / "ABC"
    d.mkdir()
    (d / "page_1_7_1.png").write_bytes(b"")
    (d / "crawl.json").write_text(json.dumps({"pages": [{"image": "page_1_7_1.png"}, {"image": None}]}))
    monkeypatch.setattr(menus, "menu_dir", lambda h: str(d))
    read = []
    monkeypatch.setattr(core, "ocr", lambda path: read.append(path) or
                        ["PLAY WITH ENGLISH SUBTITLES", "PLAY", "CHAN RUSHHOUR", "Rush Hour", "WIDESCREEN EDITION"])
    assert menus.menu_phrases("E:\\", None, "ABC") == ["CHAN RUSHHOUR", "Rush Hour"]
    assert read == [str(d / "page_1_7_1.png")]
    monkeypatch.setattr(menus, "menu_dir", lambda h: str(tmp_path / "none"))
    assert menus.menu_phrases("E:\\", None, "ABC") == []                    # (nothing kept, no VLC)


def test_a_button_area_holding_part_of_its_name(monkeypatch):
    """Rush Hour 3: each highlight is a streak beside its name, the area over "PLAY FULLSCREEN
    VERSION" taking in only its last letters. The name is the page's whole line (read off the
    whole page) mostly in the area's height and crossing it; a line in another button's height
    stays that one's (DVD-ROM/ONLINE FEATURES dipping into the icon under it), and a heading
    grazing a button's edge isn't its name."""
    from PIL import Image
    img = Image.new("RGB", (720, 480), "white")
    page = [("PLAY FULLSCREEN VERSION", (140, 171, 355, 194), 1.0), ("PLAY WIDESCREEN VERSION", (141, 212, 356, 232), 0.99),
            ("VERSION SELECTION", (141, 342, 388, 414), 0.97), ("BACK", (459, 377, 510, 412), 1.0)]
    rects = [[339, 158, 394, 199], [339, 206, 402, 244], [387, 374, 486, 425]]
    monkeypatch.setattr(menus, "_page_lines", lambda im: page)

    def crop_lines(im):                     # the area's own reading: what of the page's lines is in it
        return [("ON", (0, 13, 20, 36), 0.9)] if im.height < 50 else []      # (the two version buttons)
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: crop_lines(im))
    assert [menus.button_text(img, r, others=rects) for r in rects] == \
        ["PLAY FULLSCREEN VERSION", "PLAY WIDESCREEN VERSION", "BACK"]
    page = [("DVD-ROM/ONLINE FEATURES", (341, 379, 539, 409), 0.99)]
    rects = [[268, 381, 352, 407], [360, 401, 438, 436]]
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: [])      # (its area: no text of its own)
    assert menus.button_text(img, rects[0], others=rects) == "DVD-ROM ONLINE FEATURES"
    assert menus._whole_lines(img, rects[1], rects, (720, 480), (356, 397, 442, 440)) == []
    # an area holding its own lines whole: a line just over its edge is a neighbour's (WALL-E's
    # Pinocchio ad, its two lines, the robot's "ALLOCATE" beside them), unless half in it
    page = [("ALLOCATE", (15, 198, 76, 214), 0.99), ("PINOCCHIO 70TH ANNIVERSARY", (92, 210, 380, 226), 0.98),
            ("PLATINUM EDITION", (150, 226, 326, 242), 0.99), ("UP", (220, 130, 252, 148), 1.0)]
    rects = [[72, 206, 391, 245], [162, 127, 316, 153]]
    assert menus._whole_lines(img, rects[0], rects, (720, 480), (72, 206, 391, 245)) == []
    page[0] = ("ALLOCATE", (40, 206, 120, 222), 0.99)            # (over half of it in the area: its)
    assert [t for t, _, _ in menus._whole_lines(img, rects[0], rects, (720, 480), (72, 206, 391, 245))] == ["ALLOCATE"]


def test_menu_instructions_arent_a_films_name(monkeypatch, tmp_path):
    """October Collections: its menu says "PLEASE CHOOSE THE FEATURE YOU WISH TO VIEW", read as a
    block and line by line; none of it is searched ("YOU WISH TO VIEW" found You Wish!). A name
    on the same menu still is."""
    from disc_detective import core
    shot = tmp_path / "menu.png"
    shot.write_bytes(b"")
    monkeypatch.setattr(menus, "_first_menu_frames", lambda drive, vlc, out, log=None: [str(shot)])
    monkeypatch.setattr(core, "ocr", lambda path: ["PLEASE CHOOSE THE FEATURE YOU WISH TO VIEW", "PLEASE CHOOSE THE FEATURE",
                                                   "YOU WISH TO VIEW", "The Haunted Mansion"])
    assert menus.menu_phrases("E:\\", "vlc") == ["The Haunted Mansion"]
    assert not menus._instructions("A View to a Kill") and menus._instructions("Press Enter to Play")


def test_a_menu_read_in_strips(monkeypatch, tmp_path):
    """October Collections: RapidOCR passes over "The Ghost and Mr. Chicken" (cartoon letters) on
    the whole page and reads it in strips of it. Each strip's lines are put back where they are
    on the page; read twice, the surer reading stays, and a piece inside a surer line goes."""
    from PIL import Image
    path = tmp_path / "menu.png"
    Image.new("RGB", (720, 480), "black").save(path)

    # (the strips' tops in the order they're read: 3 strips, then 4; each line as placed on the page)
    tops = [0, 120, 280, 0, 90, 210, 330]
    page = {0: [("The Ghost and", (250, 150, 460, 190), 1.0)], 1: [("RELLUCTANT", (240, 290, 380, 330), 0.87)],
            5: [("Mr. Chickeh", (250, 195, 470, 235), 0.95), ("RELUCIAN ASTRONAUT", (210, 290, 510, 360), 0.91)]}
    calls = []

    def strip(im, least=None):
        k = len(calls)
        calls.append(im.height)
        y0 = tops[k]
        return [(t, (b[0], b[1] - y0, b[2], b[3] - y0), sc) for t, b, sc in page.get(k, [])]
    monkeypatch.setattr(menus, "rapid_lines", strip)
    got = menus.strip_phrases(str(path))
    assert "The Ghost and Mr Chickeh" in got and "RELUCIAN ASTRONAUT" in got
    assert not any("RELLUCTANT" in g for g in got)            # (inside the surer line)


def test_several_films_each_with_its_own_menu():
    """October Collections: a page per film, each headed with its name, a PLAY on each playing
    its film. Both are films (not the longer one and an extra called "Play"), each with its
    heading; a button saying only where it goes names nothing."""
    page = lambda n, heading, buttons: {"domain": 1, "page": n, "program": 1, "heading": heading, "route": [],
                                        "buttons": buttons, "passed_by": None}
    play = lambda n, text, title, secs: {"n": n, "rect": [0, 0, 50, 20], "text": text,
                                         "plays": {"title": title, "chapter": 1, "seconds": secs}}
    out = {"pages": [page(1, "The Ghost and Chicken", [play(4, "PLAY", 1, 5385.6), play(2, "MENU", 3, 145.0)]),
                     page(13, "RELUCTAT ASTRONAUT The LEMING", [play(4, "play", 2, 6120.2), play(2, "trailer", 4, 127.8)])],
           "chapters": {}, "reader": "rapidocr"}
    found = menus.menu_titles(out)
    assert [(f["title"], f["name"], f["page"]) for f in found["features"]] ==         [(1, "The Ghost and Chicken", [1, 1, 1]), (2, "RELUCTAT ASTRONAUT The LEMING", [1, 13, 1])]
    assert [(e["title"], e["name"]) for e in found["extras"]] == [(3, ""), (4, "Trailer")]
    assert [e.get("film") for e in found["extras"]] == [1, 2]          # each on its own film's menu
    titles = [{"id": "Title %d" % n, "role": r, "duration": d} for n, r, d in
              ((1, "feature", 5386), (2, "feature", 6120), (3, "extra", 145), (4, "extra", 128))]
    labels = menus.detected_labels(found, titles, keep=set())
    assert labels["Title 1"]["type"] == labels["Title 2"]["type"] == "MainMovie"
    assert labels["Title 1"]["title"] is None and "The Ghost and Chicken" in labels["Title 1"]["note"]
    assert menus._nav_only("PLAY") and menus._nav_only("Scene Selection") and not menus._nav_only("Theatrical Trailer")
    # "Set Up" says where it goes; "Up" alone is a film (WALL-E's trailer for it)
    assert menus._nav_only("SET UP") and menus._nav_only("Setup") and not menus._nav_only("UP")
    # a film's Play whose lettering wasn't read still makes it a film (Abbott and Costello's Buck Privates)
    out["pages"][1]["buttons"][0]["text"] = ""
    assert [f["title"] for f in menus.menu_titles(out)["features"]] == [1, 2]


def test_two_films_a_second_apart():
    """Abbott and Costello Go to Mars and Meet Dr. Jekyll and Mr. Hyde: 1:16:32 and 1:16:33, each
    its own menu. Both films, their headings naming different ones; two the same length under
    one heading are copies of one film."""
    page = lambda d, heading, title, secs: {
        "domain": d, "page": 1, "program": 1, "heading": heading, "route": [], "passed_by": None,
        "buttons": [{"n": 1, "rect": [0, 0, 50, 20], "text": "PLAY",
                     "plays": {"title": title, "chapter": 1, "seconds": secs}}]}
    out = {"pages": [page(1, "BUD ABBOTT LOU COSTELLO GO TO MARS", 1, 4591.6),
                     page(2, "ABBOTTAND COSTELLO LOU BUD MEET DR DJSEKYA HYDE", 2, 4592.3)],
           "chapters": {}, "reader": "rapidocr"}
    assert [f["title"] for f in menus.menu_titles(out)["features"]] == [1, 2]
    out["pages"][1]["heading"] = "BUD ABBOTT LOU COSTELLO GO TO MARS"
    assert not menus.menu_titles(out).get("features")


def test_an_extra_named_for_its_film():
    """The Great Mouse Detective: its menu's lettering reads as "SNEAK SET UP" (its Sneak Peeks
    and Set Up buttons: The Set-Up, 1949); its bonus page's "The Making Of The Great Mouse
    Detective" names the film whole."""
    assert menus.making_of_names(["The Making Of The Great Mouse Detective", "Behind the Scenes of Up!",
                                  "Making Of", "So You Think You Can Sleuth?"]) == ["The Great Mouse Detective", "Up"]
    assert menus._menu_words(menus.trim_menu_words("SNEAK SET UP")) and menus._menu_words("Fast Play")


def test_a_menus_own_words_off_a_phrases_ends():
    """Pardon My Sarong's page reads as one block with its buttons: "PARDON WSARONG SCENES
    PRODICTION NOTES LANGUAGES". The menu's own words (misread ones too) come off its ends;
    words in a name stay; a phrase of nothing else goes."""
    assert menus.trim_menu_words("PARDON WSARONG SCENES PRODICTION NOTES LANGUAGES") == "PARDON WSARONG"
    assert menus.trim_menu_words("Menu WHO DONE IT") == "WHO DONE IT"
    assert menus.trim_menu_words("The Making of Jurassic Park") == "The Making of Jurassic Park"
    assert menus.trim_menu_words("Materials Languages") == "" and menus.trim_menu_words("The Gallery") == ""


def test_each_films_picture_is_its_own_play(tmp_path):
    """Pardon My Sarong & Who Done It?: Who Done It?'s menu has its Play, its Bonus Materials page
    a commentary "Frank ON" that plays it too, and reads better. Each film of a disc of several
    shows its Play nearest the first menu, as the one feature does: not the commentary's button."""
    from PIL import Image
    for name in ("page_1_1_1.png", "page_2_1_1.png", "page_2_13_1.png"):
        Image.new("RGB", (720, 480), "black").save(tmp_path / name)
    btn = lambda n, text, title: {"n": n, "rect": [100, 100 + 40 * n, 300, 130 + 40 * n], "text": text,
                                  "plays": {"title": title, "chapter": 1, "seconds": {1: 5034.0, 2: 4626.0}.get(title, 100.0)}}
    out = {"pages": [{"domain": 1, "page": 1, "program": 1, "image": "page_1_1_1.png", "heading": "",
                      "route": [{"page": 1, "program": 1, "button": 1}], "buttons": [btn(1, "MNC PLAY", 1)]},
                     {"domain": 2, "page": 1, "program": 1, "image": "page_2_1_1.png", "heading": "WHO DONE IT",
                      "route": [{"page": 1, "program": 1, "button": 2}], "buttons": [btn(1, "Play", 2)]},
                     {"domain": 2, "page": 13, "program": 1, "image": "page_2_13_1.png", "heading": "BONUS MATERIALS",
                      "route": [{"page": 1, "program": 1, "button": 2}, {"page": 1, "program": 1, "button": 3}],
                      "buttons": [btn(1, "Frank ON", 2), btn(2, "Theatrical Trailer", 3)]}],
           "chapters": {}, "reader": "rapidocr"}
    out["found"] = menus.menu_titles(out)
    assert [f["title"] for f in out["found"]["features"]] == [1, 2]
    pics = menus.title_pictures(out, str(tmp_path))
    assert pics[2]["image"].endswith("page_2_1_1.png") and pics[2]["text"] == "Play"


def test_a_films_menu_with_its_play_misread():
    """Abbott and Costello Meet Frankenstein's menu: its Play read as "FKANKENSTEIN" (the film's
    title lettering run into it). Still that film's own menu: the one button on its page playing
    it from the start (its commentary page has two, ON and OFF). Its trailer is that film's."""
    page = lambda d, n, buttons, route=1: {"domain": d, "page": n, "program": 1, "heading": "",
                                           "route": [{"page": 1, "program": 1, "button": 1}] * route,
                                           "buttons": buttons, "passed_by": None}
    btn = lambda n, text, title, secs: {"n": n, "rect": [0, 0, 50, 20], "text": text,
                                        "plays": {"title": title, "chapter": 1, "seconds": secs}}
    out = {"pages": [page(1, 1, [btn(1, "PLAY", 1, 4666.0)]),
                     page(2, 1, [btn(1, "FKANKENSTEIN", 2, 4965.0)]),
                     page(2, 13, [btn(1, "Gregory ON", 2, 4965.0), btn(2, "-OFF", 2, 4965.0),
                                  btn(4, "Theatrical Trailer", 3, 154.0)], route=2)],
           "chapters": {}, "reader": "rapidocr"}
    found = menus.menu_titles(out)
    assert [(f["title"], f["page"]) for f in found["features"]] == [(1, [1, 1, 1]), (2, [2, 1, 1])]
    assert [(e["title"], e.get("film")) for e in found["extras"]] == [(3, 2)]


def test_an_extra_is_the_film_nearest_it_in_the_menus():
    """Whose film an extra is, by where it sits in the menus: the film whose own page is nearest,
    going in from the first menu. A Bonus page off the first menu is as near both films: the
    disc's. A film's own Bonus page: that film's. A film whose Play is on the first menu itself
    doesn't claim everything under it."""
    def page(d, n, route, buttons):
        return {"domain": d, "page": n, "program": 1, "route": route, "buttons": buttons}
    go = lambda n, page: {"n": n, "rect": [0, 0, 9, 9], "text": "", "goes_to": {"page": page, "program": 1}}
    pages = {("vmgm", 1, 1): page("vmgm", 1, [], [go(1, 1), go(2, 1), go(3, 9)]),
             (1, 1, 1): page(1, 1, [{"page": 1, "program": 1, "button": 1}], [go(1, 13)]),     # film 1's page
             (2, 1, 1): page(2, 1, [{"page": 1, "program": 1, "button": 2}], [go(1, 13)]),     # film 2's page
             (2, 13, 1): page(2, 13, [{"page": 1, "program": 1, "button": 2},
                                      {"page": 1, "program": 1, "button": 1}], []),            # film 2's bonus
             (1, 9, 1): page(1, 9, [{"page": 1, "program": 1, "button": 3}], [])}              # the disc's bonus
    films = {(1, 1, 1): 1, (2, 1, 1): 2}
    on = lambda s: [{"page": list(s)}]
    assert menus._menu_parent(pages, (1, 9, 1)) == ("vmgm", 1, 1)          # (off the first menu, not film 1's)
    assert menus._film_of(on((2, 13, 1)), pages, films) == 2
    assert menus._film_of(on((1, 1, 1)), pages, films) == 1
    assert menus._film_of(on((1, 9, 1)), pages, films) is None             # as near both: the disc's
    assert menus._film_of(on((1, 9, 1)), pages, {("vmgm", 1, 1): 1, (2, 1, 1): 2}) is None   # (a Play on the first menu)


def test_a_discs_readings_put_one_another_right():
    """A misread word of one reading put right by another's clean one ("KARLOF" in Meet the
    Killer, Boris Karloff's heading, "BORIS KARLOFF" read whole); the clean short readings kept
    beside the long, all of them searched. Only real words correct (a dictionary says so)."""
    words = {"meet", "the", "boris", "karloff", "lou", "bud", "abbott", "costello", "love", "god"}
    is_word = lambda w: w.lower() in words
    got = menus.mend_phrases(["LOU BUD ABBOTT COSTELLO MEET THE ILLE BORIS KARLOF", "BORIS KARLOFF", "MEET THE"], is_word)
    assert got[0] == "LOU BUD ABBOTT COSTELLO MEET THE ILLE BORIS KARLOFF" and "BORIS KARLOFF" in got
    assert menus.mend_phrases(["The Jove God Love God", "The Love God"], is_word)[1] == "The Love God"   # (kept)
    assert menus.mend_phrases(["BORIS KARLOF"], None) == ["BORIS KARLOF"]          # (no dictionary: as read)


def test_each_films_own_words_apart_from_the_discs(tmp_path, monkeypatch):
    """Words on every film's pages are the disc's (its stars: "BUD ABBOTT LOU COSTELLO"): each
    film is searched by its own words, "HOLD TH", not the stars' names that find Bud Abbott and
    Lou Costello in Hollywood (set aside where it's searched). A scrap of a word names nothing
    ("Zap", "Lou")."""
    from PIL import Image
    for n in ("a.png", "b.png"):
        Image.new("RGB", (720, 480)).save(tmp_path / n)
    read = {"a.png": ["BUD ABBOTT LOU COSTELLO IN THE NAVY", "Lou"], "b.png": ["BUD ABBOTT LOU COSTELLO HOLD TH", "Zap"]}
    from disc_detective import core
    monkeypatch.setattr(core, "ocr", lambda path: read[os.path.basename(path)])
    monkeypatch.setattr(menus, "strip_phrases", lambda path: [])
    page = lambda d, image: {"domain": d, "page": 1, "program": 1, "image": image, "buttons": [],
                             "route": [{"page": 1, "program": 1, "button": d}]}
    raw = {"pages": [page(1, "a.png"), page(2, "b.png")],
           "found": {"features": [{"title": 1, "name": "", "page": [1, 1, 1]}, {"title": 2, "name": "", "page": [2, 1, 1]}]}}
    assert menus.feature_phrases(raw, str(tmp_path)) == {"Title 1": ["BUD ABBOTT LOU COSTELLO IN THE NAVY"],
                                                         "Title 2": ["BUD ABBOTT LOU COSTELLO HOLD TH"]}
    # (the stars' names set aside where it's searched: films_by_title, its readings and the films' names)
    asked = []
    monkeypatch.setattr(core, "imdb_candidates", lambda con, q, k, f: asked.append([x for x, _ in q]) or [])
    titles = [{"id": "Title 1", "duration": 5130}, {"id": "Title 2", "duration": 5154}]
    core.films_by_title(None, titles, menus.feature_phrases(raw, str(tmp_path)), {})
    # (searched by its own words first, its whole readings after: weighed by its own words)
    assert asked == [["IN THE NAVY", "BUD ABBOTT LOU COSTELLO IN THE NAVY"], ["HOLD TH", "BUD ABBOTT LOU COSTELLO HOLD TH"]]


def test_the_root_menus_buttons_name_their_films(tmp_path, monkeypatch):
    """The disc's own first menu: what's written on each button leading to a film's own page is
    that film's (Lost in Alaska's logo, read only on the root menu: its own page's unread). The
    page a button leads to is the one whose route ends pressing it ("page 1" is every film's)."""
    from PIL import Image
    Image.new("RGB", (720, 480)).save(tmp_path / "root.png")
    monkeypatch.setattr(menus, "rapid_lines", lambda im, least=None: [("COMIN' ROUND", (180, 330, 328, 364), 0.98),
                                                                      ("LOSTIN", (417, 317, 512, 364), 0.97)]
                        if im.height == 480 else [])
    monkeypatch.setattr(menus, "strip_phrases", lambda path: [])
    from disc_detective import core
    monkeypatch.setattr(core, "ocr", lambda path: [])
    btn = lambda n, rect: {"n": n, "rect": rect, "text": "", "goes_to": {"page": 1, "program": 1}}
    raw = {"pages": [{"domain": "vmgm", "page": 1, "program": 1, "image": "root.png", "route": [],
                      "buttons": [btn(1, [120, 100, 362, 435]), btn(2, [363, 101, 600, 435])]},
                     {"domain": 1, "page": 1, "program": 1, "route": [{"page": 1, "program": 1, "button": 1}], "buttons": []},
                     {"domain": 2, "page": 1, "program": 1, "route": [{"page": 1, "program": 1, "button": 2}], "buttons": []}],
           "found": {"features": [{"title": 1, "name": "", "page": [1, 1, 1]}, {"title": 2, "name": "", "page": [2, 1, 1]}]}}
    assert menus.feature_phrases(raw, str(tmp_path)) == {"Title 1": ["COMIN' ROUND"], "Title 2": ["LOSTIN"]}


def test_names_put_right_by_the_words_the_disc_reads_well():
    """Order of the Phoenix: "Iagic" put right by "Magic", read well in "The Ministry of Magic Atrium";
    a sure misreading ("Meville's", no word) isn't one of the disc's words; small capitals' letter case
    as the credits have it, else as a word; "McDonald" as it is."""
    words = {"the", "ministry", "magic", "atrium", "editing", "of", "place", "professor", "choreography", "cactus"}
    is_word = lambda w: w.lower() in words
    raw = {"sure": {"The Ministry of Magic Atrium": 0.965, "Meville's Cactus": 0.966, "The Iagic Hour": 0.5}}
    seen = menus.disc_words(raw, is_word, ["Dolores Umbridge"])
    assert "magic" in seen and "meville" not in seen and "iagic" not in seen
    fix = lambda t: menus.seen_fix(t, seen, ["Dolores Umbridge", "Christopher McDonald"], is_word)
    assert fix("The Choreography of Iagic") == "The Choreography of Magic"
    assert fix("THE IAGIC OF EDITING") == "THE MAGIC OF EDITING"                 # (in the reading's capitals)
    assert fix("Professor UmBridge") == "Professor Umbridge" and fix("GrimMAuld Place") == "Grimmauld Place"
    assert fix("McDonald's Farm") == "McDonald's Farm" and fix("HARRY POTTER") == "HARRY POTTER"
    assert fix("Neville's Cactus") == "Neville's Cactus"                         # (a word it doesn't know: as read)
    # a letter changed, not one added or dropped: WALL-E's "BURN·E", read "BURNE", isn't "Burn"; in the
    # reading's own letter case
    seen = menus.disc_words({"sure": {"BURN THE TRAILER": 0.99}}, lambda w: w.lower() in {"burn", "the", "trailer"})
    fix = lambda t: menus.seen_fix(t, seen, [], lambda w: w.lower() in {"burn", "the", "trailer", "theatrical"})
    assert fix("BURNE: All New Original Short") == "BURNE: All New Original Short"
    assert fix("Theatrical Taailer") == "Theatrical Trailer" and fix("Theatrical TrAiler") == "Theatrical Trailer"


def test_a_reading_put_right_by_its_buttons_other_reading():
    """Order of the Phoenix, read off its page: "The Mmirror Explosion", "Play Iovie with Focus Points";
    its button alone: "The Mirror Explosion", "Play Movie...". A word of the other reading, in the same
    place, that's a word; not one that isn't, nor one too far off."""
    words = {"the", "mirror", "explosion", "play", "movie", "with", "focus", "points", "decrees"}
    is_word = lambda w: w.lower() in words
    assert menus.also_fix("The Mmirror Explosion", ["The Mirror Explosion"], is_word) == "The Mirror Explosion"
    assert menus.also_fix("PLAY IOVIE", ["Play Movie"], is_word) == "PLAY MOVIE"
    assert menus.also_fix("Grawp", ["GRAWPUHE"], is_word) == "Grawp"
    assert menus.also_fix("The Thestral Flight", ["The Thestrals Flight"], is_word) == "The Thestral Flight"
    # one letter changed only, by the disc's words: "Decrees" (no title's word) isn't "Secrets"
    seen = {"secrets": "Secrets"}
    assert menus.seen_fix("The Explosion of Decrees", seen, [], lambda w: False) == "The Explosion of Decrees"


def test_the_films_copy_nearest_the_root_is_the_film():
    """Two titles as long as each other, the film and its copy (Order of the Phoenix's Play Movie and
    Play Movie with Focus Points): the one whose button is fewer presses away is the feature, though
    the scene buttons point into the other."""
    page = lambda n, route, buttons, depth=0: {"domain": "bd", "page": n, "program": 1, "heading": "", "depth": depth,
                                               "route": [{"page": 0, "program": 1, "button": b} for b in route], "buttons": buttons}
    play = lambda n, t, ch, text: {"n": n, "rect": [0, 0, 1, 1], "text": text, "plays": {"title": t, "chapter": ch, "seconds": 8294}}
    out = {"pages": [page(0, [], [play(1, "00060.mpls", 1, "Play Movie")]),
                     page(4, [7], [play(2, "00059.mpls", 1, "Play Movie with Focus Points")]),
                     page(3, [5], [play(10 + i, "00059.mpls", i + 1, str(i + 1)) for i in range(4)])]}
    assert menus.menu_titles(out)["feature"] == "00060.mpls"
    out["pages"][0]["depth"] = 3                     # (its menu itself further in: the scenes' nearer)
    assert menus.menu_titles(out)["feature"] == "00059.mpls"


def test_a_blu_rays_trailer_reel_by_its_chapters():
    """A Blu-ray's chapters by playlist ("00090.mpls": 5): a "Trailers" button playing one in several
    chapters, an ad reel, as on a DVD; one in a chapter, the film's own trailer."""
    play = lambda n, t, text, s=120: {"n": n, "rect": [0, 0, 1, 1], "text": text, "plays": {"title": t, "chapter": 1, "seconds": s}}
    out = {"chapters": {"00060.mpls": 33, "00090.mpls": 5, "00091.mpls": 1},
           "pages": [{"domain": "bd", "page": 0, "program": 1, "heading": "", "route": [],
                      "buttons": [play(1, "00060.mpls", "Play Movie", 8294), play(2, "00090.mpls", "Trailers"),
                                  play(3, "00091.mpls", "Theatrical Trailer")]}]}
    found = menus.menu_titles(out)
    assert found["promos"] == ["00090.mpls"] and [e["title"] for e in found["extras"]] == ["00091.mpls"]


def test_a_numbered_lists_items_are_names_of_their_own():
    """Chuck's episode page: "1) Chuck Versus the Pink Slip", "3) Chuck Versus the Angel de la Muerte", a
    line apart: two names, not one of two lines. A name's second line, unnumbered, still joins it."""
    row = lambda text, y: (text, (55, y, 330, y + 15), 0.98)
    got = [t for t, _, _ in menus._blocks([row("1) Chuck Versus the Pink Slip", 284), row("3) Chuck Versus the Angel", 307),
                                           row("de la Muerte", 324)])]
    assert got == ["1) Chuck Versus the Pink Slip", "3) Chuck Versus the Angel de la Muerte"]


def test_buttons_known_to_hold_no_words_are_named_beside_them_whatever_their_width():
    """A Blu-ray says which buttons hold no words (empty): Chuck's 55-pixel markers, too wide for a DVD's
    guess by width, still get the names beside them."""
    from PIL import Image
    rects = [[0, 280, 55, 302], [0, 303, 55, 326]]
    lines = [("1) Chuck Versus the Pink Slip", (57, 284, 300, 300), 0.98), ("3) Chuck Versus the Angel", (57, 307, 330, 323), 0.97)]
    img = Image.new("RGB", (720, 480))
    assert menus._tiny_names(img, rects, lines=lines) == {}                 # (by width: not tiny)
    got = menus._tiny_names(img, rects, lines=lines, empty=rects)
    assert [got[tuple(r)][0] for r in rects] == ["1) Chuck Versus the Pink Slip", "3) Chuck Versus the Angel"]


def test_a_pages_words_are_those_outside_where_its_names_were_read(monkeypatch):
    """page_words: the page's lines outside where each button's name was read (name_rects): a button
    that does something owns its reading's place; one that doesn't (a decoration by the heading,
    Spider-Man 3's), only its box. Nothing outside them: the page read again, the names blanked."""
    from PIL import Image
    img = Image.new("RGB", (720, 480))
    lines = [("SPECIAL FEATURES", (150, 45, 570, 88), 0.99), ("Bloopers", (404, 145, 505, 179), 0.99)]
    buttons = [{"n": 1, "rect": [395, 140, 526, 180], "words": [404, 145, 505, 179], "plays": {"title": 15}},
               {"n": 2, "rect": [217, 55, 248, 85], "words": [150, 45, 570, 88], "menus": True}]
    assert menus.name_rects(buttons) == [[404, 145, 505, 179], [217, 55, 248, 85]]
    monkeypatch.setattr(menus, "page_headings", lambda image, rects, size, names=(), lines=None: ("SPECIAL FEATURES", False))
    assert menus.page_words(img, menus.name_rects(buttons), lines=lines) == ("SPECIAL FEATURES", "SPECIAL FEATURES", False)
    monkeypatch.setattr(menus, "page_text", lambda image, rects, size: "The Time of Their Lives")
    assert menus.page_words(img, [[0, 0, 720, 480]], lines=lines)[0] == "The Time of Their Lives"
    # (a quick crawl's button not read: where its name is, if that can be told)
    assert menus.name_rects([{"n": 3, "rect": [0, 0, 99, 99]}], lambda b: [[10, 50, 90, 70]]) == [[10, 50, 90, 70]]


def test_a_trailers_page_is_judged_trailer_by_trailer():
    """A page headed as trailers isn't a page of ads for its other words ("OTHER HENSON ADVENTURES
    AVAILABLE FROM COLUMBIA TRISTAR HOME VIDEO"): each is judged (other_films), the film's own trailer and
    teaser kept, the other films' made previews, however many of the film's own there are."""
    buttons = [{"text": "MUPPETS FROM SPACE"}, {"text": "LABYRINTH"}]
    words = "THEATRICAL TRAILERS OTHER HENSON ADVENTURES ALSO AVAILABLE FROM COLUMBIA TRISTAR HOME VIDEO"
    assert menus._ads_page([], words, buttons) and not menus._ads_page([], words, buttons, "THEATRICAL TRAILERS")
    assert menus._ads_page(["Sneak Peeks"], "", buttons, "THEATRICAL TRAILERS")       # (the way there says so)
    page = [1, 5, 1]
    found = {"extras": [{"title": t, "name": n, "kind": "Trailer", "page": page} for t, n in
                        ((23, "Muppets from Space"), (24, "Muppets from Space Teaser"), (25, "The Dark Crystal"),
                         (26, "Labyrinth"), (27, "The Storyteller"))]}
    known = {"The Dark Crystal": "The Dark Crystal", "Labyrinth": "Labyrinth"}
    found = menus.known_films(found, known.get, "Muppets from Space")
    labels = menus.detected_labels(found, [{"id": "Title %d" % t, "role": "extra"} for t in range(23, 28)],
                                   film="Muppets from Space")
    # (beside the film's own, The Storyteller too, though no well-known film: Henson's series, another ad)
    assert [t for t in range(23, 28) if labels["Title %d" % t]["type"] == "Promo"] == [25, 26, 27]
    assert labels["Title 24"]["type"] == "Trailer"


def test_a_markers_name_is_the_line_level_with_its_highlight():
    """level_name: the line level with a marker's highlight, nearest beside it (Chuck's ">>": its
    episode's name, not the page's heading over it); not a line in a button with words of its own, nor
    one with no letters; a highlight more than two lines tall is no marker (a picture button)."""
    lines = [("EPISODES", (60, 200, 300, 240), 0.99), ("1) Chuck Versus the Pink Slip", (130, 280, 600, 310), 0.98),
             ("PLAY ALL", (700, 280, 800, 310), 0.99), ("......", (100, 285, 120, 305), 0.5)]
    assert menus.level_name((60, 282, 100, 308), lines)[0] == "1) Chuck Versus the Pink Slip"
    assert menus.level_name((60, 282, 100, 308), lines, worded=[(120, 270, 610, 320)])[0] == "PLAY ALL"
    assert menus.level_name((60, 282, 100, 308), lines, reach=20) is None
    assert menus.level_name((60, 200, 100, 400), lines) is None              # (164 pixels tall: a picture)


def test_a_button_read_again_keeps_the_reading_with_fewest_misread_words(monkeypatch):
    """With a dictionary, a page's reading with a word that isn't one has its button read alone too;
    of the readings, the one with the fewest such words, then the surest ("2. Bat Scene (Storyboard)"
    over the page's surer "Sforyboard"); the others handed on with it, for also_fix."""
    from PIL import Image
    img = Image.new("RGB", (720, 480))
    lines = [("2. Bat Scene (Sforyboard)", (100, 100, 300, 130), 0.99)]
    alone = lambda: [(menus.Words("2. Bat Scene (Storyboard)", (100, 100, 300, 130)), 0.9)]
    words = {"bat", "scene", "storyboard"}
    got, sure, others = menus.name_button(img, [90, 90, 310, 140], [[90, 90, 310, 140]], lines=lines, alone=alone,
                                          is_word=lambda w: w.lower() in words)
    assert got == "2. Bat Scene (Storyboard)" and sure == 0.9 and others == ["2. Bat Scene (Sforyboard)"]
    got, _, _ = menus.name_button(img, [90, 90, 310, 140], [[90, 90, 310, 140]], lines=lines, alone=alone)
    assert got == "2. Bat Scene (Sforyboard)"                                # (no dictionary: the page's, sure)


def test_a_shows_bonus_features_are_put_right_as_a_films_extras_are():
    """fix_tv_readings: a TV disc's readings that aren't episodes (the episode list named those) put
    right by the show's people, the button's other readings and the disc's words, as a film's extras
    are (fix_reading); what was read kept, for Edit titles' corrected-name link."""
    words = {"the", "making", "of", "gag", "reel", "psych"}
    is_word = lambda w: w.lower() in words
    tv = {"readings": [{"title": 3, "text": "Psych Pilot", "seconds": 2600, "bonus": False},
                       {"title": 9, "text": "Gag Rcel", "seconds": 300, "bonus": True},
                       {"title": 10, "text": "The Making of", "seconds": 600, "bonus": True}]}
    got = menus.fix_tv_readings(tv, {0}, [], is_word, also={"Gag Rcel": ["Gag Reel"]})
    assert [r["text"] for r in got["readings"]] == ["Psych Pilot", "Gag Reel", "The Making of"]
    assert got["readings"][1]["read"] == "Gag Rcel" and "read" not in got["readings"][2]
    found = menus.tv_found(got, {0: {"season": 1, "number": 1, "name": "Pilot", "score": 1}})
    extra = next(e for e in found["extras"] if e["title"] == 9)
    assert extra["name"] == "Gag Reel" and extra["read"] == "Gag Rcel"
    label = menus.detected_tv_labels(found, [{"id": "Title 9", "role": "extra"}])["Title 9"]
    assert label["title"] == "Gag Reel" and label["read"] == "Gag Rcel"
    # (its picture says what was read, as a film's does: the correction is the label's)
    pictures = menus.tv_pictures({"readings": [dict(got["readings"][1], image="p.png", rect=[0, 0, 9, 9])]}, "d")
    assert pictures[9]["text"] == "Gag Rcel"


def test_an_episode_read_otherwise_than_its_listed_name_shows_its_reading():
    """An episode named by the show's list, read otherwise (its letters, its case): its label carries
    what was read, for Edit titles' corrected-name link, as an extra put right does; its list number
    before it isn't a difference."""
    found = {"tv": True, "extras": [], "episodes": [
        {"title": 71, "season": 3, "episode": 1, "name": "Chuck Versus the Pink Slip", "read": "1) Chuck Versus the Pink Slip", "by": "name"},
        {"title": 73, "season": 3, "episode": 3, "name": "Chuck Versus the Angel de la Muerte",
         "read": "3) Chuck Versus the angel de la Muerte", "by": "name"},
        {"title": 74, "season": 3, "episode": 4, "name": "Chuck Versus Operation Awesome", "read": "", "by": "play-all"}]}
    labels = menus.detected_tv_labels(found, [{"id": "Title %d" % t, "role": "episode"} for t in (71, 73, 74)])
    assert "read" not in labels["Title 71"] and "read" not in labels["Title 74"]
    assert labels["Title 73"]["read"] == "3) Chuck Versus the angel de la Muerte"
    assert labels["Title 73"]["title"] == "Chuck Versus the Angel de la Muerte"


def test_a_reading_names_its_title_unless_its_a_play_alls():
    """names_title: a reading with words names the title its button plays, a film's Play button's too (the
    film's name is the disc's); a Play All's doesn't (it plays the first episode and names none); sure of
    it only where the crawl asks (a Blu-ray's: another reading costs it nothing)."""
    assert menus.names_title("Bloopers") and menus.names_title("Play Movie") and menus.names_title("PLAY")
    assert not menus.names_title("PLAY ALL") and not menus.names_title("On") and not menus.names_title("")
    assert menus.names_title("Grawp", 0.6) and not menus.names_title("Grawp", 0.6, need_sure=True)
    assert menus.names_title("Grawp", None, need_sure=True)                 # (not told how sure: as read)


def test_the_shared_planner_takes_the_page_naming_most_for_its_cost():
    """plan_pages: the page naming the most titles still unnamed for the pages it costs (those on the way
    counted, and what they name), ties to the lowest rank; a page done isn't read again."""
    holds = {"main": {1}, "extras": {5, 6, 7}, "scenes": {1}}
    ways = {"extras": ["main"], "scenes": ["main"]}
    assert menus.plan_pages(holds, ways) == ["main", "extras"]
    assert menus.plan_pages(holds, {}, done={"extras"}) == ["main"]
    assert menus.plan_pages({"a": {1}, "b": {1}}, {}, rank=lambda s: 0 if s == "b" else 1) == ["b"]
    # (a Blu-ray's: no presses to get anywhere, each page its reading)
    assert menus.plan_pages(holds, {}) == ["extras", "main"]


def test_a_title_is_given_up_after_its_tries():
    """_given_up: a title read on TITLE_TRIES pages, none naming it, is looked for no further (Ghostbusters
    II's film, its buttons all "Play"), a page counted once its words are in."""
    pages = {(1, k): {"buttons": [{"n": 1, "rect": [0, 0, 9, 9], "auto": False}]} for k in range(4)}
    known = {((1, k, 1), 1): {"plays": {"title": 1, "chapter": 1}} for k in range(4)}
    read = [(1, k, 1) for k in range(3)]
    assert menus._given_up(pages, read, known, set(), lambda s: True) == {1}
    assert menus._given_up(pages, read[:2], known, set(), lambda s: True) == set()
    assert menus._given_up(pages, read, known, {1}, lambda s: True) == set()
    assert menus._given_up(pages, read, known, set(), lambda s: s != (1, 2, 1)) == set()


def test_a_word_read_in_two_is_put_back_and_digits_in_a_word_are_doubted():
    """join_split_words: two words side by side joined a known word that either alone isn't ("Nathaniel's
    Revelat ion"), two words left as they are; MIXED_RE: a word of digits and letters run together ("0Am"),
    not an ordinal, 3D, 4K, 1080p, the 1990s, a scene's 38A, S1 or Disc2."""
    known = lambda w: w.lower() in {"revelation", "up", "set", "upset", "nathaniel's"}
    assert menus.join_split_words("Nathaniel's Revelat ion", known) == "Nathaniel's Revelation"
    assert menus.join_split_words("Up Set", known) == "Up Set"
    assert menus.MIXED_RE.findall("3.1 0Am Not Waiting 2nd 3D 1080p 4K S1 Disc2 1990s Sc. 38A") == ["0Am"]


def test_a_page_headings_dip_into_a_button_is_the_pages(monkeypatch):
    """name_button: a page heading's line dipping into a button's box is left out of its name, as a DVD's
    reading always did (Enchanted's "Comes To Life" over "Happy Working Song")."""
    from PIL import Image
    img = Image.new("RGB", (720, 480))
    lines = [("Comes To Life", (90, 280, 300, 300), 0.99), ("Happy Working Song", (100, 310, 380, 345), 0.99)]
    monkeypatch.setattr(menus, "_heading_boxes", lambda image, rect, others, size: [(90, 280, 300, 300)])
    got, _, _ = menus.name_button(img, [76, 285, 397, 349], [[76, 285, 397, 349]], lines=lines)
    assert got == "Happy Working Song"


def test_the_scans_copies_and_play_alls_as_either_crawl_names_them():
    """_copies_of, _play_alls_of: the scan's copies and Play Alls, by a DVD's numbers or a Blu-ray's
    playlists (key); and menu_titles: a Play All the scan knows, its button not read, a Play All still,
    not an extra with no name (out["scan_play_alls"]), a DVD's and a Blu-ray's alike."""
    dvd = [{"id": "Title 12", "role": "duplicate", "note": "same video as Title 5"}, {"id": "Title 9", "role": "play-all"}]
    assert menus._copies_of(dvd) == {12: 5} and menus._play_alls_of(dvd) == {9}
    bd = [{"id": "00203.mpls", "role": "duplicate", "note": "same video as 00201.mpls"}, {"id": "00089.mpls", "role": "play-all"}]
    assert menus._copies_of(bd, str) == {"00203.mpls": "00201.mpls"} and menus._play_alls_of(bd, str) == {"00089.mpls"}
    page = {"domain": "bd", "page": 2, "program": 1, "route": [], "heading": "", "image": "p.png",
            "buttons": [{"n": 25, "rect": [0, 0, 9, 9], "text": "The Explosion of Decrees", "plays": {"title": "00083.mpls", "chapter": 1, "seconds": 117.4}},
                        {"n": 40, "rect": [0, 20, 9, 29], "text": "", "plays": {"title": "00089.mpls", "chapter": 1, "seconds": 1500.0}},
                        {"n": 1, "rect": [0, 40, 9, 49], "text": "Play Movie", "plays": {"title": "00060.mpls", "chapter": 1, "seconds": 8294.0}}]}
    out = {"pages": [page], "chapters": {}}
    assert [e["title"] for e in menus.menu_titles(out)["extras"]] == ["00083.mpls", "00089.mpls"]
    got = menus.menu_titles(dict(out, scan_play_alls=["00089.mpls"]))
    assert [e["title"] for e in got["extras"]] == ["00083.mpls"] and got["play_alls"] == ["00089.mpls"]


def test_a_surer_reading_of_less_is_part_of_the_name(monkeypatch):
    """name_button: of two readings, the one holding all of the other's words wins over a surer one of
    less (WALL-E's "THE CHRONICLES OF NARNIA: PRINCE CASPIAN", read 0.77 on its page, alone its first
    line only, 0.95), as read_again has it."""
    from PIL import Image
    img = Image.new("RGB", (720, 480))
    lines = [("THE CHRONICLES OF NARNIA:", (98, 264, 373, 281), 0.93), ("PRINCE CASPIAN", (153, 279, 321, 296), 0.77)]
    alone = lambda: [(menus.Words("THE CHRONICLES OF NARNIA:", (98, 264, 373, 281)), 0.95)]
    monkeypatch.setattr(menus, "_heading_boxes", lambda *a: [])
    got, _, _ = menus.name_button(img, [80, 262, 379, 301], [[80, 262, 379, 301]], lines=lines, alone=alone)
    assert got == "THE CHRONICLES OF NARNIA: PRINCE CASPIAN"


def test_an_ads_name_without_a_trademark_read_into_it():
    """promo_name: a trademark sign read into an ad's last word taken off, the name kept ("Spider-ManT":
    Spider-Man; "DISCTM"), a word ending in T or TM left as it is ("THE LOST", "ATM")."""
    name = lambda t: menus.promo_name([{"text": t, "via": "Previews"}])
    assert name("Spider-ManT") == "Previews - Spider-Man" and name("ON BLU-RaY DISCTM") == "Previews - ON BLU-RaY DISC"
    assert name("THE LOST") == "Previews - The Lost" and name("The Last ATM") == "Previews - The Last ATM"


def test_the_film_under_a_pop_up_menu_is_the_feature():
    """menu_titles: a film-length title the menus show over (out["under_menu"]: a pop-up's, its menu in the
    film's own stream, no button playing it: Chamber of Secrets' 00023) is the feature, not the longest
    extra a button plays (Building a Scene, 17 minutes); "Scene 17", a button playing a title of its own from
    its start, is its name (a deleted scene), not a chapter's menu word."""
    page = {"domain": "bd", "page": 12, "program": 1, "route": [], "heading": "Deleted/Extended SCEneS", "image": "p.png",
            "buttons": [{"n": 13, "rect": [0, 0, 9, 9], "text": "BUILDING A SCENE", "plays": {"title": "00083.mpls", "chapter": 1, "seconds": 1041.0}},
                        {"n": 26, "rect": [0, 20, 9, 29], "text": "SCENE 17", "plays": {"title": "00125.mpls", "chapter": 1, "seconds": 20.0}}]}
    out = {"pages": [page], "chapters": {}}
    assert menus.menu_titles(out)["feature"] == "00083.mpls"                       # (nothing else to go by)
    got = menus.menu_titles(dict(out, under_menu=[{"title": "00023.mpls", "seconds": 9656.0}]))
    assert got["feature"] == "00023.mpls"
    names = {e["title"]: (e["name"], e.get("kind")) for e in got["extras"]}
    assert names["00083.mpls"][0] == "Building a Scene" and names["00125.mpls"] == ("Scene 17", "DeletedScene")
    # (shorter than what a button plays: not the feature; nor an extra's length)
    assert menus.menu_titles(dict(out, under_menu=[{"title": "00001.mpls", "seconds": 600.0}]))["feature"] == "00083.mpls"


def test_a_crawls_waits_are_told_on_its_own_thread():
    """Every wait on the player (menus.pause, VLC's and the crawls' alike) tells the crawl running on that thread
    (progress "wait"): a chance to call it off, or find its disc taken out. Outside a crawl, or on another
    thread, nothing is told."""
    import threading
    told = []
    menus.pause(0)
    with menus.waits_told(lambda stage, *a: told.append(stage)):
        menus.pause(0)
        menus.pause(0)
        other = threading.Thread(target=lambda: menus.pause(0))
        other.start()
        other.join()
    menus.pause(0)
    assert told == ["wait", "wait"]
    with pytest.raises(menus.Cancelled):            # (what calls it off: raised from the wait itself)
        with menus.waits_told(lambda stage, *a: (_ for _ in ()).throw(menus.Cancelled())):
            menus.pause(0)
    menus.pause(0)                                  # (and nothing told after: the crawl's gone)


def test_a_line_across_a_row_of_buttons_is_cut_to_each_ones_words():
    """The Swan Princess's main menu: OCR reads its bottom row as one line, "SCENE SELECTIONS SPECIAL
    FEATURES TRAILERS", its middle over Special Features. That button gets its own words, not the row's."""
    boxes = [(68, 404, 299, 443), (298, 404, 529, 441), (528, 403, 654, 444), (85, 361, 237, 404)]
    row = ("SCENE SELECTIONS SPECIAL FEATURES TRAILERS", (75, 408, 649, 428), 0.97)
    t, b, s = menus._row_cut(row, boxes[1], boxes)
    assert t == "SPECIAL FEATURES" and 298 <= b[0] and b[2] <= 529 and s == 0.97
    assert menus._row_cut(row, boxes[2], boxes)[0] == "TRAILERS"
    alone = ("PLAY MOVIE", (90, 373, 233, 396), 0.99)                 # (a line in one button: as it is)
    assert menus._row_cut(alone, boxes[3], boxes) == alone


def test_a_story_per_friend_picked_before_has_its_own_picture_and_way_there(tmp_path):
    """The Swan Princess: title set 1's Special Features leads to title set 10's friends page; each friend
    leads to one page whose Odette Reads plays that friend's story (the disc remembers the choice). Each
    story gets the page's picture, its way there named by the friend picked."""
    page = lambda d, n, prog, route, buttons, image=None: ((d, n, prog), dict(
        {"domain": d, "page": n, "program": prog, "route": route, "buttons": buttons}, **({"image": image} if image else {})))
    rect = [0, 0, 10, 10]
    story = {"title": 16, "chapter": 1, "seconds": 245, "others": [
        {"title": 17, "chapter": 1, "by": [[1, 5, 1, 5], [1, 29, 1, 1], [10, 2, 2, 2]]},
        {"title": 18, "chapter": 1, "by": [[1, 5, 1, 5], [1, 29, 1, 1], [10, 2, 2, 3]]}]}
    pages = dict([
        page(1, 5, 1, [], [{"n": 5, "text": "SPECIAL FEATURES", "goes_to": {"page": 29, "program": 1}, "rect": rect}]),
        page(1, 29, 1, [{"page": 5, "program": 1, "button": 5}],
             [{"n": 1, "text": "ODETTE'S BOOK OF WONDERFUL FRIENDS", "goes_to": {"page": 2, "program": 2}, "rect": rect}]),
        page(10, 2, 2, [{"page": 5, "program": 1, "button": 5}, {"page": 29, "program": 1, "button": 1}],
             [{"n": k, "text": f, "goes_to": {"page": 2, "program": 5}, "rect": rect}
              for k, f in ((1, "Jean-Bob"), (2, "Speed"), (3, "Puffin"))]),
        page(10, 2, 5, [{"page": 5, "program": 1, "button": 5}, {"page": 29, "program": 1, "button": 1},
                        {"page": 2, "program": 2, "button": 1}],
             [{"n": 1, "text": "Odette Reads", "plays": story, "rect": [5, 5, 50, 20]},
              {"n": 3, "text": "friends", "goes_to": {"page": 2, "program": 3}, "rect": rect}], image="page_10_2_5.png"),
    ])
    # (the friends page's parent: in another title set's menus)
    assert menus.page_trail(pages, (10, 2, 5)) == ["SPECIAL FEATURES", "ODETTE'S BOOK OF WONDERFUL FRIENDS", "Jean-Bob"]
    assert menus.route_names(pages, story["others"][0]["by"])[-1] == "Speed"
    pics = menus.title_pictures({"pages": list(pages.values()), "found": {"feature": 1, "extras": []}}, str(tmp_path))
    assert (pics[16]["page_from"], pics[17]["page_from"], pics[18]["page_from"]) == ("Jean-Bob", "Speed", "Puffin")
    assert pics[17]["page_trail"] == ["SPECIAL FEATURES", "ODETTE'S BOOK OF WONDERFUL FRIENDS", "Speed"]
    assert pics[17]["image"] == pics[16]["image"] and pics[17]["text"] == "Odette Reads"


def test_a_page_is_named_by_the_button_its_reached_by_past_any_next_or_arrow():
    """A page's name: the button it's reached by, on the page before; a Next, a Back, an arrow, an unread one
    passed over for the one before it (Harry Potter 1's yearbook pages: an arrow, then "Yearbook Character
    Clips"); a hub's ("Special Features") as good as any; the first menu: none."""
    page = lambda n, route, buttons: (("bd", n, 1), {"domain": "bd", "page": n, "program": 1, "route": route,
                                                     "buttons": buttons})
    pages = dict([
        page(0, [], [{"n": 1, "text": "SPECIAL FEATURES", "goes_to": {"page": 1, "program": 1}}]),
        page(1, [{"page": 0, "program": 1, "button": 1}],
             [{"n": 6, "text": "Yearbook Character Clips", "goes_to": {"page": 2, "program": 1}}]),
        page(2, [{"page": 0, "program": 1, "button": 1}, {"page": 1, "program": 1, "button": 6}],
             [{"n": 5, "text": "", "goes_to": {"page": 3, "program": 1}}]),
        page(3, [{"page": 0, "program": 1, "button": 1}, {"page": 1, "program": 1, "button": 6},
                 {"page": 2, "program": 1, "button": 5}], [{"n": 9, "text": "Next", "goes_to": {"page": 4, "program": 1}}]),
        page(4, [{"page": 0, "program": 1, "button": 1}, {"page": 1, "program": 1, "button": 6},
                 {"page": 2, "program": 1, "button": 5}, {"page": 3, "program": 1, "button": 9}], []),
    ])
    assert menus.page_from(pages, ("bd", 0, 1)) == ""
    assert menus.page_from(pages, ("bd", 1, 1)) == "SPECIAL FEATURES"
    assert menus.page_from(pages, ("bd", 2, 1)) == "Yearbook Character Clips"
    assert menus.page_from(pages, ("bd", 3, 1)) == "Yearbook Character Clips"          # (past an arrow)
    assert menus.page_from(pages, ("bd", 4, 1)) == "Yearbook Character Clips"          # (past an arrow and a Next)
    # the way to it from the first menu, each name once (the arrow and Next pages named as the one before)
    assert menus.page_trail(pages, ("bd", 4, 1)) == ["SPECIAL FEATURES", "Yearbook Character Clips"]
    assert menus.page_trail(pages, ("bd", 1, 1)) == ["SPECIAL FEATURES"] and menus.page_trail(pages, ("bd", 0, 1)) == []
    from disc_detective import app
    assert app.menu_trail(["SPECIAL FEATURES", "Odette's Friends", "Jean-Bob"]) == \
        "Main Menu › Special Features › Odette's Friends › Jean-Bob"
    assert app.menu_trail(["SPECIAL FEATURES"]) == "Main Menu › Special Features" and app.menu_trail([]) == ""
    for nav in ("Next", "BACK", ">>", "Main Menu", "Page 2", "Next Page", "Menu", "2"):
        assert menus.NAV_RE.match(nav), nav
    for name in ("Yearbook Character Clips", "More Features", "Back to the Future", "Special Features"):
        assert not menus.NAV_RE.match(name), name


def test_a_name_that_opens_with_its_kind():
    """A title named with its kind first, then a colon or a dash: that kind, and the rest its name;
    its kind alone, or a page saying it's something else, as it is."""
    assert menus.kind_name("Music Video: “Shining Star” by The Dust Brothers") == (
        "Music", "“Shining Star” by The Dust Brothers")
    assert menus.kind_name("Featurette - The Making of", "Extra") == ("Featurette", "The Making of")
    assert menus.kind_name("Deleted Scenes – Alternate Ending") == ("DeletedScene", "Alternate Ending")
    assert menus.kind_name("Music Video", "Music") == ("Music", "Music Video")
    assert menus.kind_name("Interview: Jim Henson", "Featurette") == ("Featurette", "Interview: Jim Henson")
    assert menus.kind_name("Pepe: The King Prawn", "Extra") == ("Extra", "Pepe: The King Prawn")
    assert menus.kind_name(None) == (None, None)


def test_a_play_all_or_its_clips_whichever_the_menus_name():
    """A Blu-ray's crawl: a button plays a gag reel whose clips are each a title of their own, which no
    button plays. The reel is named; its clips aren't ads ("nothing plays it") but its pieces, set aside
    once the labels are applied. With a button for each clip too, each clip's its own and the reel their
    Play All."""
    from disc_detective import core
    t = lambda pid, secs, clips: {"id": pid, "role": "extra", "duration": secs, "clip_list": clips, "note": ""}
    titles = [{"id": "00800.mpls", "role": "feature", "duration": 5000, "clip_list": "00001", "note": ""},
              t("00700.mpls", 300, "00011,00012"), t("00711.mpls", 140, "00011"), t("00712.mpls", 160, "00012")]
    found = {"feature": "00800.mpls", "extras": [{"title": "00700.mpls", "name": "Gag Reel"}], "promos": []}
    labels = menus.detected_labels(found, titles)
    assert set(labels) == {"00800.mpls", "00700.mpls"}
    core.apply_labels(titles, labels)
    assert [x["role"] for x in titles] == ["feature", "extra", "segment", "segment"]
    found["extras"] += [{"title": "00711.mpls", "name": "Take One"}, {"title": "00712.mpls", "name": "Take Two"}]
    labels = menus.detected_labels(found, titles)
    core.apply_labels(titles, labels)
    assert [x["role"] for x in titles] == ["feature", "play-all", "extra", "extra"]
    assert titles[1]["note"] == "plays 00711.mpls, 00712.mpls (each named)"


def _menu_frame(btns, lit=None, level=None, words=True):
    """A DVD menu frame (720x480): each button's name in the subpicture (words), the highlighted one (lit) in gold;
    over a flat grey (level: the video darkened, the subpicture still drawn), else over a busy backdrop."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (720, 480), (level,) * 3 if level is not None else (40, 90, 140))
    d = ImageDraw.Draw(img)
    if level is None:
        for x in range(0, 720, 24):
            d.line((x, 0, x + 200, 480), fill=(70, 120, 60), width=6)
    for b in btns:
        x0, y0, x1, y1 = b["rect"]
        if b["n"] == lit:
            d.rectangle((x0 + 4, y0 + 4, x1 - 4, y1 - 4), fill=(230, 180, 40))
        elif words:
            d.rectangle((x0 + 10, y0 + 12, x1 - 10, y1 - 12), fill=(240, 240, 240))
    if level is not None:
        img.info["dark"], img.info["level"] = True, level
    return img


def _menu_btns():
    rect = lambda k: [100, 60 + 70 * k, 400, 110 + 70 * k]
    out = []
    for k in range(4):
        n = k + 1
        out.append({"n": n, "rect": rect(k), "auto": False, "up": n - 1 if n > 1 else 0, "down": n + 1 if n < 4 else 0,
                    "left": 0, "right": 0, "cmd": {"op": "vts_title", "title": n}})
    return out


def test_a_pages_words_in_its_subpicture_are_told_over_a_flat_grey():
    """_subpicture_words: over the video darkened the subpicture still shows; most unselected buttons showing
    lettering there: the page's words are in it (WALL-E's); only the highlighted one showing: a plain page."""
    btns = _menu_btns()
    assert menus._subpicture_words([_menu_frame(btns, lit=1, level=0), _menu_frame(btns, lit=1, level=255)], btns, 1)
    plain = [_menu_frame(btns, lit=1, level=0, words=False), _menu_frame(btns, lit=1, level=255, words=False)]
    assert not menus._subpicture_words(plain, btns, 1)


def test_a_subpicture_pages_buttons_are_each_pictured_selected(tmp_path, monkeypatch):
    """On a page whose words are in its subpicture, the crawl moves the highlight onto each button in turn and
    pictures it selected, then back where it was; on a plain page nothing's moved. The pictures are a crawl's
    buttons' only if its words prove to be in the subpicture (crawl)."""
    btns = _menu_btns()
    monkeypatch.setattr(menus, "pause", lambda s: None)

    class Vlc:
        def __init__(self, words):
            self.lit, self.level, self.words, self.keys = 1, None, words, []
            self.is_dark = False

        def dark(self, on, level=0):
            self.level, self.is_dark = (level if on else None), on

        def settle(self, n=3, limit=1.5):
            return True

        def frame(self):
            return _menu_frame(btns, self.lit, self.level, self.words)

        def press(self, key):
            self.keys.append(key)
            self.lit = {b["n"]: b for b in btns}[self.lit][key] or self.lit

    st = (1, 2, 1)
    pages = {(1, 2): {"buttons": btns, "program_buttons": [btns]}}
    for words, pictured in ((True, {1, 2, 3, 4}), (False, set())):
        c = menus._Crawler("C:/VLC", "dvd:///E:/", pages, str(tmp_path), lambda s: None)
        c.v = Vlc(words)
        c._picture_each(st, c.v.frame(), 1, btns)
        assert set(c.shots.get(st, {})) == pictured
        assert c.v.lit == 1 and c.v.level is None             # (left as it was: highlight and video)
        if words:
            assert c.v.keys == ["down", "down", "down", "up", "up", "up"]
            for n, name in c.shots[st].items():
                from PIL import Image
                with Image.open(os.path.join(str(tmp_path), name)) as im:   # (each with its own button lit)
                    x0, y0, x1, y1 = {b["n"]: b for b in btns}[n]["rect"]
                    assert im.convert("RGB").getpixel((x0 + 6, y0 + 6)) == (230, 180, 40)
        c.pool.shutdown(wait=False)
        c.more.shutdown(wait=False)


def test_a_play_all_the_menus_name_alone_is_labelled_by_its_buttons_name():
    """A Play All the scan knows, all the menus offer of its clips (Chuck S1 D1's "Declassified Scenes": its five
    scenes in a row, no button of their own): an extra by its button's own name (a name of its own: not its
    heading, "ADDITIONAL FOOTAGE"), labelled; its clips its pieces, left be."""
    out = {"reader": "rapidocr", "scan_play_alls": ["00026.mpls"], "pages": [
        {"domain": "bd", "page": 2, "program": 1, "route": [], "image": "p.png", "heading": "", "buttons": [
            {"n": 1, "rect": [0, 20, 9, 29], "text": "Play Episode", "plays": {"title": "00012.mpls", "chapter": 1, "seconds": 17958.0}},
            {"n": 4, "rect": [0, 0, 9, 9], "text": "Declassified Scenes", "heading": "ADDITIONAL FOOTAGE",
             "plays": {"title": "00026.mpls", "chapter": 1, "seconds": 344.0}}]}]}
    found = menus.menu_titles(out)
    assert [(e["title"], e["name"]) for e in found["extras"]] == [("00026.mpls", "Declassified Scenes")]
    titles = [{"id": "00026.mpls", "role": "play-all", "duration": 344.0},
              {"id": "00032.mpls", "role": "junk", "duration": 25.0}]
    labels = menus.detected_labels(found, titles)
    assert labels["00026.mpls"]["title"] == "Declassified Scenes" and "00032.mpls" not in labels


def test_an_ad_run_the_menus_offer_alone_is_labelled_and_its_ads_are_its_pieces():
    """A Play All of ads the menus offer as one ("SNEAK PEEKS", two previews in a row, no button of their own --
    Muppets from Space's): labelled a promo, as TheDiscDB gets them, its previews its pieces."""
    out = {"reader": "rapidocr", "scan_play_alls": ["Title 5"], "pages": [
        {"domain": 1, "page": 2, "program": 1, "route": [], "image": "p.png", "heading": "", "buttons": [
            {"n": 1, "rect": [0, 20, 9, 29], "text": "Play Movie", "plays": {"title": 1, "chapter": 1, "seconds": 5292.0}},
            {"n": 2, "rect": [0, 0, 9, 9], "text": "Sneak Peeks", "plays": {"title": 5, "chapter": 1, "seconds": 116.0}}]}]}
    found = menus.menu_titles(out)
    assert found["promos"] == [5]
    titles = [{"id": "Title 1", "role": "feature", "duration": 5292.0, "clips": "1"},
              {"id": "Title 5", "role": "play-all", "duration": 116.0, "clips": "6 7"},
              {"id": "Title 6", "role": "junk", "duration": 60.0, "clips": "6"},
              {"id": "Title 7", "role": "junk", "duration": 56.0, "clips": "7"}]
    labels = menus.detected_labels(found, titles)
    assert labels["Title 5"]["type"] == "Promo" and "Title 6" not in labels and "Title 7" not in labels


def test_a_play_all_button_named_by_its_heading_when_nothing_else_plays_its_titles():
    """A DVD page's "Play All" under its heading ("DELETED SCENES"), its scenes no button of their own: named by its
    heading, an extra, its scenes its pieces; with a button for one of its scenes, a Play All, as before."""
    def out(more=()):
        return {"reader": "rapidocr", "pages": [
            {"domain": 1, "page": 2, "program": 1, "route": [], "image": "p.png", "heading": "", "buttons": [
                {"n": 1, "rect": [0, 20, 9, 29], "text": "Play Movie", "plays": {"title": 1, "chapter": 1, "seconds": 5292.0}},
                {"n": 2, "rect": [0, 0, 9, 9], "text": "Play All", "heading": "DELETED SCENES",
                 "plays": {"title": 5, "chapter": 1, "seconds": 300.0}}] + list(more)}]}
    titles = [{"id": "Title 1", "role": "feature", "duration": 5292.0, "clips": "1"},
              {"id": "Title 5", "role": "play-all", "duration": 300.0, "clips": "6 7"},
              {"id": "Title 6", "role": "extra", "duration": 140.0, "clips": "6"},
              {"id": "Title 7", "role": "extra", "duration": 160.0, "clips": "7"}]
    labels = menus.detected_labels(menus.menu_titles(out()), titles)
    assert (labels["Title 5"]["type"], labels["Title 5"]["title"]) == ("DeletedScene", "Deleted Scenes")
    assert "Title 6" not in labels and "Title 7" not in labels
    scene = {"n": 3, "rect": [0, 40, 9, 49], "text": "The Long Goodbye", "plays": {"title": 6, "chapter": 1, "seconds": 140.0}}
    labels = menus.detected_labels(menus.menu_titles(out([scene])), titles)
    assert "Title 5" not in labels and labels["Title 6"]["title"] == "The Long Goodbye"


def test_what_plays_before_the_menu_is_previews_only_if_long_enough_and_numbered():
    """Before the menu: a clip long enough to be a trailer "Previews" (several: "Previews 1", "Previews 2", in the
    order they play); a short one (a studio's logo, a warning: Chuck's 12-second clips) left as the scan has it."""
    titles = [{"id": "Title 1", "role": "junk", "duration": 12.0}, {"id": "Title 2", "role": "junk", "duration": 95.0},
              {"id": "Title 3", "role": "junk", "duration": 64.0}]
    assert menus.opening_names({"opening": [2]}, titles) == {"Title 2": "Previews"}
    assert menus.opening_names({"opening": [1, 3, 2]}, titles) == {"Title 3": "Previews 1", "Title 2": "Previews 2"}
    found = {"feature": 9, "extras": [], "opening": [1, 2]}
    labels = menus.detected_labels(found, titles + [{"id": "Title 9", "role": "feature", "duration": 5000.0}])
    assert labels["Title 2"]["title"] == "Previews" and "Title 1" not in labels


def test_a_button_no_one_can_get_to_isnt_offered():
    """reachable_buttons: from what a page opens on (or a command selects), the buttons the arrows lead to. Chuck S1
    D2's "Chuck's World" (4): its arrows only to itself, none to it, the page opening on 5 -- not reachable; DVD-style
    neighbours alike; no arrows known at all, every one."""
    nb = lambda up, down, left, right: {"up": up, "down": down, "left": left, "right": right}
    page = [{"id": 4, "neighbours": nb(4, 4, 4, 4)}, {"id": 5, "neighbours": nb(5, 6, 5, 12)},
            {"id": 6, "neighbours": nb(5, 7, 6, 12)}, {"id": 7, "neighbours": nb(6, 7, 7, 12)},
            {"id": 12, "neighbours": nb(12, 12, 5, 12)}]
    assert menus.reachable_buttons(page, {5}) == {5, 6, 7, 12}
    dvd = [{"n": 1, "up": 2, "down": 2, "left": 1, "right": 1}, {"n": 2, "up": 1, "down": 1, "left": 2, "right": 2},
           {"n": 3, "up": 3, "down": 3, "left": 3, "right": 3}]
    assert menus.reachable_buttons(dvd, {1}) == {1, 2}
    assert menus.reachable_buttons([{"id": 1}, {"id": 2}], set()) == {1, 2}


def test_an_extras_description_goes_with_its_name_through_the_namer():
    """The words a menu prints under a button's name (Harry Potter's): the extra's label keeps them as its
    description, beside its name, the name itself unchanged."""
    titles = [{"id": "Title 1", "role": "feature"}, {"id": "Title 3", "role": "extra"}, {"id": "Title 4", "role": "extra"}]
    found = {"feature": 1, "promos": [],
             "extras": [{"title": 3, "name": "The Hidden Secrets of Harry Potter",
                         "description": "Take a thrilling look back through the previous movies."},
                        {"title": 4, "name": "Conjuring a Scene"}]}
    labels = menus.detected_labels(found, titles)
    assert labels["Title 3"]["title"] == "The Hidden Secrets of Harry Potter"
    assert labels["Title 3"]["description"] == "Take a thrilling look back through the previous movies."
    assert "description" not in labels["Title 4"]


def test_a_buttons_description_lines_come_with_its_grouped_name():
    from test_textblocks import GREY, Page
    from disc_detective import textblocks
    p = Page()
    p.text((60, 60), "Production Diary", size=26)
    p.text((60, 95), "A look behind the scenes of the film.", size=16, colour=GREY, bold=False)
    p.text((60, 170), "Deleted Scenes", size=26)
    got = textblocks.page_names(p.image, p.lines, {1: (40, 50, 560, 150), 2: (40, 160, 560, 220)})
    assert [t for t, _, _ in got[1]] == ["Production Diary"]
    assert [t for t, _, _ in got[1].description] == ["A look behind the scenes of the film."]
    assert list(got[2].description) == []


def test_scenes_shorter_than_the_scans_episodes_are_extras_though_the_menus_name_none():
    """A Play All's episodes no menu button names (Chuck Season 2): the scan's own episode titles say how long an
    episode is, so a button's minute-long scene is an extra all the same."""
    tv = {"readings": [{"title": "00005.mpls", "text": "Chuck Versus the Fat Lady Sc. 8-10", "seconds": 153.3, "bonus": False},
                       {"title": "00010.mpls", "text": "Chuck Versus the Gravitron Sc. 25", "seconds": 64.6, "bonus": False}]}
    assert menus.tv_found(tv, {})["extras"] == []
    found = menus.tv_found(tv, {}, [2521.1, 2578.6])
    assert [e["title"] for e in found["extras"]] == ["00005.mpls", "00010.mpls"]


def test_a_titles_picture_has_its_description_inside_its_one_outline(tmp_path):
    """A button with a description under its name (Chuck Season 2's deleted scenes): the description is part of its
    title in the picture -- its text kept, one outline round the name's lines and the description's, as a name over
    two lines has; a TV reading's too. A reading hands where its description was read on, kept through a copy
    (pickle) as its other parts are."""
    import pickle
    out = {"pages": [{"domain": "bd", "page": 3, "program": 1, "route": [], "image": "hdmv_00017_p3.png",
                      "buttons": [{"n": 7, "rect": [324, 100, 720, 140], "text": "Chuck Versus the Fat Lady Sc. 8-10",
                                   "words": [330, 104, 700, 116], "description": "Chuck with Jill, Sarah and Casey.",
                                   "description_words": [380, 118, 650, 128], "plays": {"title": "00005.mpls", "chapter": 1}}]}]}
    pic = menus.title_pictures(out, str(tmp_path))["00005.mpls"]
    assert pic["description"] == "Chuck with Jill, Sarah and Casey." and "description_rect" not in pic
    assert pic["rect"] == [330, 104, 700, 128] and pic["button"] == [324, 100, 720, 140]
    tv = {"readings": [{"title": "00005.mpls", "image": "p.png", "rect": [324, 100, 720, 140], "words": [330, 104, 700, 116],
                        "text": "Chuck Versus the Fat Lady Sc. 8-10", "description": "Chuck with Jill, Sarah and Casey.",
                        "description_words": [380, 118, 650, 128]}]}
    assert menus.tv_pictures(tv, "d")["00005.mpls"]["rect"] == [330, 104, 700, 128]
    w = pickle.loads(pickle.dumps(menus.Words("Name", [1, 2, 3, 4], description="Its words.", described=[1, 5, 3, 8])))
    assert (w.description, w.described) == ("Its words.", [1, 5, 3, 8])


def test_a_readings_slanted_outline_reaches_its_picture(tmp_path):
    """Where a reading kept its outline following its lines' slant (Words.shape), the crawl's button and its
    picture keep it, a film's and a TV reading's alike; kept through a copy (pickle)."""
    import pickle
    w = pickle.loads(pickle.dumps(menus.Words("Set Up", [10, 10, 60, 30], shape=[(10, 20), (60, 10), (60, 25), (10, 35)])))
    assert w.shape == [[10, 20], [60, 10], [60, 25], [10, 35]]
    shape = [[400, 300], [560, 260], [560, 290], [400, 330]]
    out = {"pages": [{"domain": 1, "page": 8, "program": 7, "route": [], "image": "page_7_8_1.png",
                      "buttons": [{"n": 4, "rect": [380, 250, 600, 340], "text": "SET UP", "words": [400, 260, 560, 330],
                                   "shape": shape, "plays": {"title": 4, "chapter": 1}}]}]}
    assert menus.title_pictures(out, str(tmp_path))[4]["shape"] == shape
    tv = {"readings": [{"title": 4, "image": "p.png", "rect": [380, 250, 600, 340], "words": [400, 260, 560, 330],
                        "text": "SET UP", "shape": shape}]}
    assert menus.tv_pictures(tv, "d")[4]["shape"] == shape


def test_a_title_reached_by_a_setting_on_its_own_page_has_it_pressed_first():
    """A title its button plays once a setting on the same page is pressed (WALL-E's deleted scenes: "ON", the
    commentary, coming back to the same menu): its picture names that button, where its words were read, to be
    numbered before the title's own; one pressed on a page before (The Swan Princess's friend) isn't on this
    picture; the title as it plays without the setting has none."""
    page = {"domain": 7, "page": 25, "program": 1, "image": "page_7_25_1.png", "route": [],
            "buttons": [{"n": 1, "rect": [309, 187, 351, 221], "text": "ON", "words": [322, 194, 350, 218]},
                        {"n": 4, "rect": [222, 268, 471, 292], "text": '"Garbage Airlock"', "words": [295, 274, 425, 287],
                         "plays": {"title": 83, "chapter": 1,
                                   "others": [{"title": 82, "chapter": 1, "by": [[7, 8, 1, 3], [7, 21, 1, 4], [7, 25, 1, 1]]}]}}]}
    out = {"pages": [{"domain": 7, "page": 8, "program": 1, "buttons": [{"n": 3, "rect": [0, 0, 9, 9], "text": "BONUS FEATURES",
                                                                       "goes_to": [7, 21, 1]}]},
                     {"domain": 7, "page": 21, "program": 1, "route": [{"page": 8, "program": 1, "button": 3}],
                      "buttons": [{"n": 4, "rect": [0, 0, 9, 9], "text": "Deleted Scenes", "goes_to": [7, 25, 1]}]}, page]}
    pics = menus.title_pictures(out, "d")
    assert pics[82]["pressed"] == [{"rect": [322, 194, 350, 218]}] and "pressed" not in pics[83]
    assert menus.pressed_first(page, page["buttons"][1], [[7, 8, 1, 3], [7, 21, 1, 4]]) == []
    assert menus.pressed_first(page, page["buttons"][1], [[7, 25, 1, 4]]) == []         # (its own button: not first)


def _corners_at(k, boxes):
    """Lines [(text, (x0, y0, x1, y1) as drawn level)] turned by slope k about the origin: RapidOCR's corners."""
    import math
    a = math.atan(k)
    turn = lambda x, y: (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))
    return [(t, tuple(turn(x, y) for x, y in ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3]))), 0.99)
            for t, b in boxes]


def test_a_menu_drawn_at_a_slant_is_one_region_perspective_none(monkeypatch):
    """_slant_regions: a menu's lines of one slope near each other (WALL-E's, about -16 degrees) are one region, its
    level HUD text left out; a page whose slants run steadily from level to steep (Spider-Man's Oscorp monitor in
    perspective) has none; nor an upright page."""
    monkeypatch.setattr(menus, "_keystone", lambda image: None)
    menu = _corners_at(-0.29, [("PLAY", (300, 200, 380, 230)), ("SCENE SELECTION", (260, 250, 440, 280)),
                               ("BONUS FEATURES", (330, 300, 510, 330)), ("SET UP", (380, 350, 470, 380)),
                               ("SNEAK PEEKS", (420, 400, 560, 430))])
    hud = [("zoom lock", ((560, 240), (610, 240), (610, 252), (560, 252)), 0.99)]
    got = menus._slant_regions(None, menu + hud)
    assert len(got) == 1 and got[0]["idx"] == [0, 1, 2, 3, 4] and abs(got[0]["slope"] + 0.29) < 0.01
    steady = [(t, c, s) for k, y in ((0.0, 100), (-0.03, 160), (-0.05, 220), (-0.08, 280), (-0.12, 340), (-0.14, 400))
              for t, c, s in _corners_at(k, [("Trailer %d" % y, (100, y, 300, y + 20))])]
    assert menus._slant_regions(None, steady) == []
    assert menus._slant_regions(None, _corners_at(0.0, [("PLAY MOVIE", (100, 100, 300, 130)),
                                                        ("SCENE SELECTIONS", (100, 150, 340, 180))])) == []


def test_a_slanted_menu_is_grouped_level_not_by_its_upright_boxes(monkeypatch):
    """_page_names: a menu drawn at a slant, its logo's big "SPACE" ending just short of "SUBTITLES" a row lower
    (Muppets from Space's main menu): by their upright boxes on the page, one row -- grouped turned level, the logo
    is no part of SUBTITLES' name; the page's own text kept, its level reading beside it to put it right by."""
    import math
    from PIL import Image, ImageDraw, ImageFont
    k = -0.2
    font = lambda n: ImageFont.load_default(size=n)
    level = Image.new("RGB", (800, 520), (10, 10, 40))
    dr = ImageDraw.Draw(level)
    drawn = [("SPACE", (150, 60), 70), ("SUBTITLES", (460, 150), 30), ("AUDIO SET-UP", (150, 200), 30),
             ("SPECIAL FEATURES", (460, 200), 30)]
    level_lines = []
    for text, (x, y), size in drawn:
        dr.text((x, y), text, font=font(size), fill=(250, 200, 60))
        b = dr.textbbox((x, y), text, font=font(size))
        level_lines.append((text, b))
    a = math.atan(k)                    # (PIL turns counter-clockwise: the lines rise to the right, moved down 200)
    page_img = level.rotate(-math.degrees(a), expand=False, fillcolor=(10, 10, 40), center=(0, 0), translate=(0, 200))
    to_page = lambda x, y: (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a) + 200)
    to_level = lambda x, y: (x * math.cos(a) + (y - 200) * math.sin(a), -x * math.sin(a) + (y - 200) * math.cos(a))
    pcorners = [(t, tuple(to_page(x, y) for x, y in ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3]))), 0.99)
                for t, b in level_lines]
    pcorners[-1] = ("SPECIAL EATURES",) + pcorners[-1][1:]          # (the page read misreads one)
    plines = [(t, (min(q[0] for q in c), min(q[1] for q in c), max(q[0] for q in c), max(q[1] for q in c)), s)
              for t, c, s in pcorners]
    lcorners = [(t, ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])), 0.99) for t, b in level_lines]
    monkeypatch.setattr(menus, "_level_read", lambda image, region: (level, [(t, b, 0.99) for t, b in level_lines],
                                                                     lcorners, to_level, to_page))
    monkeypatch.setattr(menus, "_keystone", lambda image: None)
    box_of = lambda t: next(b for tt, b, _ in plines if tt == t)

    def button(t):                      # (a highlight's box: the words' own size, upright, round their middle)
        lb = next(b for tt, b in level_lines if tt == t)
        mx, my = to_page((lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2)
        return (mx - (lb[2] - lb[0]) / 2, my - (lb[3] - lb[1]) / 2, mx + (lb[2] - lb[0]) / 2, my + (lb[3] - lb[1]) / 2)
    names = {1: button("SUBTITLES"), 2: button("AUDIO SET-UP"), 3: button("SPECIAL FEATURES")}
    assert box_of("SPACE")[3] > box_of("SUBTITLES")[1]          # (on the page, their upright boxes share rows)
    got = menus._page_names(page_img, plines, names, pcorners, dict(names))
    assert [t for t, _, _ in got[1]] == ["SUBTITLES"]
    assert [t for t, _, _ in got[3]] == ["SPECIAL EATURES"] and got[3].alts == [("SPECIAL FEATURES", 0.99)]
    assert [t for t, _, _ in got[2]] == ["AUDIO SET-UP"]


def test_a_name_in_capitals_is_title_cased_letter_by_letter():
    """nice_name's recasing (the user's rule): a letter's a capital where the one before it isn't a letter -- after a
    bracket, a hyphen, an ellipsis too (Muppets from Space's outtakes) -- not after an apostrophe inside a word, nor
    straight after a digit; the small words small but first, last, after a colon; known forms and Roman numerals as
    they are."""
    assert menus.nice_name("THIS ELEVATOR IS GOING DOWN...WAY DOWN (VERSION 1,2 & 3)") == \
        "This Elevator Is Going Down...Way Down (Version 1,2 & 3)"
    assert menus.nice_name("RIZZO'S BIGGEST-FAN") == "Rizzo's Biggest-Fan" and menus.nice_name("I'M WEIRD FOR YOU") == "I'm Weird for You"
    assert menus.nice_name("MIND IF I DROP IN") == "Mind If I Drop In"
    assert menus.nice_name("PINOCCHIO 70TH ANNIVERSARY PLATINUM EDITION") == "Pinocchio 70th Anniversary Platinum Edition"
    assert menus.nice_name("IN TOO DEEP: THE SECOND TASK") == "In Too Deep: The Second Task"
    assert menus.nice_name("MEN IN BLACK II") == "Men in Black II" and menus.nice_name("SPIDER-MAN") == "Spider-Man"
    assert menus.nice_name("DVD-ROM ONLINE FEATURES") == "DVD-ROM Online Features" and menus.nice_name("TV SPOTS") == "TV Spots"
    assert menus.nice_name("BLU-RAY DISC") == "Blu-ray Disc" and menus.nice_name("O'BRIEN'S STORY") == "O'Brien's Story"
    assert menus.nice_name("Close Encounters (of the Third Kind)") == "Close Encounters (of the Third Kind)"   # (mixed: kept)
    # (after an apostrophe a capital, the user's, but a contraction's end small)
    assert [menus.nice_name(t) for t in ("L'OREAL", "D'ARTAGNAN", "YOU'RE ON", "WE'VE GOT IT", "THEY'LL COME", "FIVE O'CLOCK")] ==         ["L'Oreal", "D'Artagnan", "You're On", "We've Got It", "They'll Come", "Five O'clock"]
    # (a quote at either end kept where it pairs with one in the name, else dropped as a speck)
    assert menus.nice_name('MUSIC VIDEO- "SHINING STAR"') == 'Music Video- "Shining Star"'
    assert menus.nice_name('"HERO" BY CHAD KROEGER') == '"Hero" by Chad Kroeger' and menus.nice_name('DUMPED!"') == "Dumped!"


def test_initials_stay_capitals_and_a_name_after_mc_has_its_own_capital():
    """The recasing's known forms take the initials menus print (Spider-Man's Blu-ray: "HBO MAKING OF SPIDER-MAN",
    "CGI SPIDER-MAN"), and a name after "Mc" its own capital ("BONE SAW MCGRAW"); a short word that only starts so
    as it was."""
    assert menus.nice_name("HBO MAKING OF SPIDER-MAN") == "HBO Making of Spider-Man"
    assert menus.nice_name("CGI SPIDER-MAN") == "CGI Spider-Man" and menus.nice_name("IMAX: THE STORY") == "IMAX: The Story"
    assert menus.nice_name("BONE SAW MCGRAW") == "Bone Saw McGraw" and menus.nice_name("DR. MCCOY'S") == "Dr. McCoy's"
    assert menus.nice_name("MC HAMMER") == "Mc Hammer"


def test_a_description_in_small_capitals_reads_as_a_sentence():
    """A description as OCR reads a menu's small capitals (Order of the Phoenix's): its words small, but names as the
    film's people have them (a whole name; a single one no dictionary has), the film's title as it is, a role before a
    name ("Editor Mark Day") and its first word a capital; its words put right first (fix: the credits' "Mark" for
    "Iark"); one written as a sentence already as it is."""
    people = ["David Yates", "Mark Day", "Natalia Tena", "Daniel Radcliffe"]
    is_word = lambda w: w.lower() in {"day", "mark", "director", "editor", "difference", "good", "edit", "scene", "spend",
                                      "actress", "tour", "stages", "movies", "harry", "potter", "order", "phoenix", "mat"}
    fix = lambda t: t.replace("iark day", "Mark Day")
    said = menus.nice_description("Director David Yates and Editor Iark Day show us what A DIFFERENCE A GOOD EDIT MAKES AND "
                                  "ALLOW YOU TO EDIT A SCENE.", people, is_word, fix=fix)
    assert said == "Director David Yates and Editor Mark Day show us what a difference a good edit makes and allow you to " \
                   "edit a scene."
    said = menus.nice_description("Spend a day with Actress Mat Tena AnD RECEiVE A VERY PERSONAL tour of the Harry Potter And "
                                  "The Order of the Phoenix Stages.", people, is_word, "Harry Potter and the Order of the Phoenix")
    assert said == "Spend a day with Actress Mat Tena and receive a very personal tour of the Harry Potter and the Order of " \
                   "the Phoenix stages."
    assert menus.nice_description("Morgan and Lester ponder their end.", people, is_word) == "Morgan and Lester ponder their end."
    # (and as correct_names hands an extra on: its name put right as ever, its description made a sentence)
    got = menus.correct_names({"extras": [{"title": 32, "name": "Harry Potter: The Magic of Editing",
                                           "description": "SHOW US WHAT A GOOD EDIT MAKES."}]}, people, is_word)
    assert got["extras"][0]["description"] == "Show us what a good edit makes."


def test_ocrs_slips_in_numbers_are_put_right_but_codes_are_left_alone():
    """number_slips (the user's): only words already holding a digit, each fix one a number there must be -- an item
    number at the start (Muppets from Space's "1O."), an ordinal whose ending fits, a year, O between digits, a
    dictionary word run into a number (WALL-E's "PINOCCHIO7OTH"); codes, which keep clear of O, I and l, as read."""
    is_word = lambda w: w.lower() in {"pinocchio", "anniversary", "season", "this", "elevator", "dalmatians", "the"}
    fix = lambda t: menus.number_slips(t, is_word)
    assert fix("1O. THIS ELEVATOR") == "10. THIS ELEVATOR" and fix("I0) PEPE") == "10) PEPE"
    assert fix("PINOCCHIO7OTH ANNIVERSARY") == "PINOCCHIO 70TH ANNIVERSARY" and fix("SEASON9") == "SEASON 9"
    assert fix("THE 2OTH ANNIVERSARY") == "THE 20TH ANNIVERSARY" and fix("1ITH HOUR") == "11TH HOUR"
    assert fix("THE 2OND FILM") == "THE 2OND FILM"                      # (20nd: no such ordinal, left)
    assert fix("COPYRIGHT 2OO7") == "COPYRIGHT 2007" and fix("1O1 DALMATIANS") == "101 DALMATIANS"
    for code in ("SN124B", "SN12O4B", "UNIT 4O7B", "OO7", "1OO", "T-1000", "PG-13", "MP3", "R2-D2", "CX.00013", "4K", "1080P"):
        assert fix(code) == code, code
    assert menus.number_slips("SE7EN", lambda w: w.lower() in {"se7en", "seven"}) == "SE7EN"   # (a word it knows)
