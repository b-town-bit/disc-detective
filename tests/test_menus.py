import os
from disc_detective import menus


def test_commands():
    assert menus.command(bytes.fromhex("3002000000030000")) == {"op": "title", "title": 3}
    assert menus.command(bytes.fromhex("3005000200010000")) == {"op": "vts_chapter", "title": 1, "chapter": 2}
    assert menus.command(bytes.fromhex("200400000000000b")) == {"op": "link_page", "to": 11}
    d = menus.command(bytes.fromhex("7104000600020002"))
    assert d["op"] == "link_page" and d["to"] == 2 and "sets" in d
    assert menus.command(bytes.fromhex("200100000000000d"))["op"] == "link_tail_page"
    assert menus.command(b"\0" * 8) == {"op": "nop"}


def _nav(vobu_ea, buttons=()):
    """A navigation packet: pack header, PCI with the given button rects, DSI's VOBU end address."""
    s = bytearray(menus.SECTOR)
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
    for sector, data in ((0, _nav(2)), (3, _nav(1)), (5, _nav(2, [(10, 20, 110, 60), (10, 70, 110, 99)]))):
        vob[sector * menus.SECTOR:(sector + 1) * menus.SECTOR] = data
    path = tmp_path / "VTS_01_0.VOB"
    path.write_bytes(bytes(vob))
    with open(path, "rb") as fh:
        btns = menus._buttons(fh, [{"first": 0, "last": 7}])
        assert [b["rect"] for b in btns] == [[10, 20, 110, 60], [10, 70, 110, 99]]
        assert btns[0]["cmd"] == {"op": "link_page", "to": 11}
        assert menus._buttons(fh, [{"first": 0, "last": 4}]) == []     # the page ends first
        assert menus._buttons(fh, []) == []


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


def test_plain_names_for_unreadable_words():
    words = {"sneak", "peeks", "making", "enchanted", "fairy", "tale", "deleted", "scenes"}
    is_word = lambda w: w.lower() in words
    assert menus.readable("The Making of Enchanted", is_word)
    assert menus.readable("Deleted Scenes 2", is_word)
    assert not menus.readable("Beye olticak Peeks", is_word)
    assert not menus.readable("", is_word)
    assert not menus.readable("2 Oh", lambda w: w.lower() in words | {"oh"})     # a scrap, not a name
    found = {"feature": 1, "extras": [{"title": 3, "name": "Fairy Tale Making"}, {"title": 4, "name": "Rrw Oxq"}],
             "promos": []}
    assert [e["name"] for e in menus.plain_names(found, is_word)["extras"]] == ["Fairy Tale Making", ""]
    assert menus.plain_names(found, None) is found                          # no dictionary: as read


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
        {"title": 10, "name": "An Invitation to Father of the Bride", "seconds": 637.0, "listed": 635, "kind": None},
        {"title": 11, "name": "Martin & Short Interview Each Other", "seconds": 316.0, "listed": 313, "kind": None}]
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
                       "text": "Making Of"}
    assert pics[1]["text"] == "1. Opening"                     # read best: a scene's words, over none
    out["pages"].pop(1)
    assert menus.title_pictures(out, str(tmp_path))[1]["rect"] == [10, 10, 200, 40]   # the first: Play


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


def test_a_tiny_buttons_cell():
    """Spider-Man's Trailers page: a pointer in each panel, its label round it; the cell reaches
    halfway to the next button each way."""
    rects = [[263, 154, 278, 183], [280, 242, 296, 268], [407, 170, 424, 200]]
    cell = menus._cell(rects[0], rects)
    assert cell[0] == 263 - menus.BESIDE                  # nothing to its left: as far as it reaches
    assert cell[2] == (278 + 407) // 2 and cell[3] == (183 + 242) // 2 and cell[1] == 154 - menus.ABOVE
