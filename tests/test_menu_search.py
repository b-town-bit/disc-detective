"""The crawl's search: for titles, words only ordering it."""
from disc_detective import menus


def _b(n, op="none", to=None):
    return {"n": n, "rect": [0, 0, 9, 9], "auto": False, "cmd": {"op": op, "to": to}}


# a disc: Play and Bonus on its main menu; Bonus holds an extra, an Audio page and a Scenes page
SEARCH_PAGES = {(1, 1): {"buttons": [_b(1), _b(2, "link_page", 2), _b(3, "link_page", 3), _b(4, "link_page", 4)]},
                (1, 2): {"buttons": [_b(1), _b(2)]},
                (1, 3): {"buttons": [_b(1), _b(2)]},
                (1, 4): {"buttons": [_b(1), _b(2)]}}
MAIN, BONUS, AUDIO, SCENES = (1, 1, 1), (1, 2, 1), (1, 3, 1), (1, 4, 1)
SEARCH_KNOWN = {(MAIN, 1): {"plays": {"title": 1, "chapter": 1}, "engine": True},
                (BONUS, 1): {"plays": {"title": 5, "chapter": 1}, "engine": True},
                (BONUS, 2): {"plays": {"title": 1, "chapter": 1}, "engine": True},
                (AUDIO, 1): {"plays": {"title": 9, "chapter": 1}, "engine": True},
                (AUDIO, 2): {"plays": {"title": 1, "chapter": 1}, "engine": True},
                (SCENES, 1): {"plays": {"title": 1, "chapter": 1}, "engine": True},
                (SCENES, 2): {"plays": {"title": 1, "chapter": 2}, "engine": True}}


def search_words(texts):
    return lambda s, n: texts.get((s, n), "")


def test_a_title_named_elsewhere_needs_no_visit():
    words = search_words({(MAIN, 1): "Play Movie", (MAIN, 2): "Bonus", (BONUS, 1): "Making Of", (BONUS, 2): "Play"})
    found = menus._named_titles(SEARCH_PAGES, [MAIN], SEARCH_KNOWN, {}, {}, words)
    assert found == {1}
    leads = lambda s, found: menus._leads_to_title(SEARCH_PAGES, s, {MAIN}, found, SEARCH_KNOWN, {}, {}, False)
    assert leads(BONUS, found) and leads(AUDIO, found)      # titles 5 and 9: no button's named them
    assert not leads(SCENES, found)                         # only the film, named by Play
    found = menus._named_titles(SEARCH_PAGES, [MAIN, BONUS], SEARCH_KNOWN, {}, {}, words)
    assert found == {1, 5} and not leads(BONUS, found) and leads(AUDIO, found)
    assert not leads(AUDIO, found | {9})                    # (found by another way: not looked at)


def test_a_button_already_read_sends_nobody_anywhere():
    # Bonus's title 5 has a button whose words can't be read: Scenes links back to Bonus, and that's
    # no reason to read Scenes
    pages = dict(SEARCH_PAGES)
    pages[(1, 4)] = {"buttons": [_b(1), _b(2, "link_page", 2)]}
    known = {k: v for k, v in SEARCH_KNOWN.items() if k[0] != SCENES}
    known[(SCENES, 1)] = {"plays": {"title": 1, "chapter": 2}, "engine": True}
    found = {1}
    assert menus._leads_to_title(pages, SCENES, {MAIN}, found, known, {}, {}, False)        # (Bonus not read yet)
    assert not menus._leads_to_title(pages, SCENES, {MAIN, BONUS}, found, known, {}, {}, False)
    assert not menus._leads_to_title(pages, BONUS, {MAIN, BONUS}, found, known, {}, {}, False)


def test_a_title_behind_words_like_commentary_is_still_looked_for():
    # its button's words put it later; they don't rule it out
    rank = lambda route, words={}, scenes=False: menus._search_rank(route, MAIN, search_words(words), scenes)
    assert rank([(MAIN, 2)], {(MAIN, 2): "Bonus Features"}) == 0
    for text in ("Audio Commentary", "Languages", "Set Up"):
        assert rank([(MAIN, 3)], {(MAIN, 3): text}) == 1, text
    assert rank([(MAIN, 3)]) == 0                           # (unread: nothing against it)
    assert rank([(MAIN, 2)], {(MAIN, 2): "Sneak Peeks"}) == 0      # (ads' pages are quick to read)
    assert rank([(MAIN, 2)], {(MAIN, 2): "Main Menu"}) == 0        # (a FastPlay chooser's way in)
    assert rank([(MAIN, 4)], {(MAIN, 4): "Bonus"}, scenes=True) == 2


def test_a_button_nobody_knows_matters_only_while_a_title_has_no_way():
    pages = {(1, 1): {"buttons": [_b(1), _b(2, "link_page", 2)]}, (1, 2): {"buttons": [_b(1)]}}
    leads = lambda known, unknown: menus._leads_to_title(pages, BONUS, {MAIN}, {1}, known, {}, {}, unknown)
    assert leads({}, True) and not leads({}, False)
    assert not leads({}, False) and not menus._leads_to_title(
        pages, BONUS, {MAIN}, {1}, {}, {}, {(BONUS, 1): {"chain": [{"title": 0, "chapter": 0}]}}, True)    # (pressed: answered)


def test_titles_with_a_way_count_what_plays_on_and_only_pages_there_is_a_way_to():
    known = {(MAIN, 1): {"plays": {"title": 1, "chapter": 1, "after": [2, 3], "into": 4}},
             (BONUS, 1): {"plays": {"title": 5, "chapter": 1}, "others": [{"title": 6, "chapter": 1}]},
             (AUDIO, 1): {"plays": {"title": 9, "chapter": 1}},     # (a page with no way to it: not a way)
             (BONUS, 2): {"menus": True}}
    pressed = {(BONUS, 3): {"chain": [{"title": 7, "chapter": 1}, {"title": 0, "chapter": 0}]}, (BONUS, 4): {"error": "x"}}
    assert menus._titles_with_a_way({MAIN: [], BONUS: []}, known, pressed, opening=[8]) == {1, 2, 3, 4, 5, 6, 7, 8}


def test_named_titles_by_a_scene_page_or_a_press():
    pages = {(1, 1): {"buttons": [_b(1, "link_page", 2), _b(2)]}, (1, 2): {"buttons": [_b(1), _b(2)]}}
    known = {(BONUS, 1): {"plays": {"title": 3, "chapter": 1}, "engine": True},
             (BONUS, 2): {"plays": {"title": 3, "chapter": 2}, "engine": True}}
    pressed = {(MAIN, 2): {"chain": [{"title": 8, "chapter": 1}, {"title": 6, "chapter": 1}]}}
    words = search_words({(MAIN, 1): "Pilot", (MAIN, 2): "Trailer"})
    # (the last of a chain's the button's own: what played first may have a button of its own)
    assert menus._named_titles(pages, [MAIN], known, {}, pressed, words) == {3, 6}
    # no words read for it: not named, and its other buttons are looked at
    assert menus._named_titles(pages, [MAIN], known, {}, pressed, search_words({})) == set()


class FakeFuture:
    def __init__(self, value):
        self.value = value

    def done(self):
        return True

    def result(self):
        return self.value


SLOW_WORDS = set()      # pages whose words aren't in until they're waited for (OCR behind VLC)


class FakeCrawler:
    """A player with no disc: goes where it's told, and says which pages it read and what it pressed."""
    made = []

    def __init__(self, vlc_dir, mrl, pages, out_dir, log):
        self.pages, self.reading, self.learned, self.selects, self.intros = pages, {}, {}, {}, {}
        self.spent, self.placed, self.dark_ok, self.states, self.expect = {}, [0, 0, 0], None, [], 0
        self.menu_key = self.menu_skip = self.film_first = False
        self.start_select = None
        self.visited, self.presses, self.pressing = [], [], None
        self.v = type("V", (), {"titles": staticmethod(lambda: [{"index": t, "seconds": 600} for t in (1, 5, 7, 9)])})()
        self.pool = self.more = type("P", (), {"shutdown": staticmethod(lambda wait=False: None)})()
        FakeCrawler.made.append(self)

    def open(self):
        pass

    def close(self):
        pass

    def read_page(self, s):
        if s not in self.reading:
            self.visited.append(s)
            self.reading[s] = {b["n"]: FakeFuture(WORDS.get((s, b["n"]), "")) for b in menus._state_buttons(self.pages, s)}

    def at_start(self, s):
        self.read_page(s)
        return s, 1

    def restart(self, s, where=None):
        return self.at_start(s)

    def follow(self, st, sel, route):
        for x, n in route:
            st = self.learned[(x, n)]
            self.read_page(st)
        return st, 1

    def select(self, st, sel, n):
        self.pressing = (st, n)

    def watch(self, length):
        self.presses.append(self.pressing)
        return [{"title": PRESSED.get(self.pressing, 0), "chapter": 1}]

    def find(self, states, timeout=None):
        return self.pressing[0], 1

    def word_now(self, s, n):
        return WORDS.get((s, n), "") if self.read(s) else ""

    def read(self, s):
        return s in self.reading and s not in SLOW_WORDS

    def first_words(self, s):
        SLOW_WORDS.discard(s)
        return {b["n"]: self.word_now(s, b["n"]) for b in menus._state_buttons(self.pages, s)}

    def words(self, s):
        return self.first_words(s) if s in self.reading else {}

    def heading(self, s):
        return None


# Play and four ways on from the main menu. Bonus holds an extra, the film again, and two buttons the
# disc's program can't answer; Audio holds a title nothing else plays; Scenes and Credits, only the film
EE_PAGES = {(1, 1): {"buttons": [_b(1), _b(2), _b(3), _b(4), _b(5)]},
            (1, 2): {"buttons": [_b(1), _b(2), _b(3), _b(4)]},
            (1, 3): {"buttons": [_b(1), _b(2)]},
            (1, 4): {"buttons": [_b(1), _b(2)]},
            (1, 5): {"buttons": [_b(1), _b(2)]}}
CREDITS = (1, 5, 1)
WORDS = {(MAIN, 1): "Play Movie", (MAIN, 2): "Audio Commentary", (MAIN, 3): "Scene Selection", (MAIN, 4): "Bonus Features",
         (MAIN, 5): "Credits", (BONUS, 1): "Making Of", (BONUS, 2): "Play Film", (BONUS, 3): "Set Up", (BONUS, 4): "Mystery"}
PRESSED = {(BONUS, 4): 7}
ENGINE = {(1, 1, 1, 1): [{"plays": 1, "chapter": 1}], (1, 1, 1, 2): [{"goes_to": (1, 3, 1)}], (1, 1, 1, 3): [{"goes_to": (1, 4, 1)}],
          (1, 1, 1, 4): [{"goes_to": (1, 2, 1)}], (1, 1, 1, 5): [{"goes_to": CREDITS}],
          (1, 2, 1, 1): [{"plays": 5, "chapter": 1}], (1, 2, 1, 2): [{"plays": 1, "chapter": 1}],
          (1, 3, 1, 1): [{"plays": 9, "chapter": 1}], (1, 3, 1, 2): [{"plays": 1, "chapter": 1}],
          (1, 4, 1, 1): [{"plays": 1, "chapter": 1}], (1, 4, 1, 2): [{"plays": 1, "chapter": 2}],
          (1, 5, 1, 1): [{"plays": 1, "chapter": 1}], (1, 5, 1, 2): [{"menus": True}]}


SCANNED = []


def test_a_disc_that_cant_be_compared_is_all_looked_for(monkeypatch):
    from disc_detective import core
    monkeypatch.setattr(core, "detect_format", lambda drive: (None, None))
    assert menus._scan_titles("E:\\", log=lambda m: None) == []
    assert menus._titles_sought([], {1: 600, 2: 3}) == {1}


def test_a_copy_counts_as_named_only_with_its_original():
    copies = menus._copies_of([{"id": "Title 18", "role": "duplicate", "note": "same video as Title 13"},
                               {"id": "Title 17", "role": "duplicate", "note": "same video as Title 10"},
                               {"id": "Title 8", "role": "play-all", "note": "plays Title 1, Title 2"},
                               {"id": "Title 1", "role": "episode", "note": ""}])
    assert copies == {18: 13, 17: 10}
    assert menus._with_copies({13}, copies) == {13, 18}     # (the other video not named: still looked for)
    assert menus._with_copies({18}, copies) == {13, 18}     # (copies are equals: either names both)
    three = {6: 5, 7: 5}
    assert menus._with_copies({7}, three) == {5, 6, 7}
    assert menus._play_alls_of([{"id": "Title 8", "role": "play-all"}, {"id": "Title 1", "role": "episode"}]) == {8}


def test_the_plan_is_made_before_a_word_is_read():
    # Psych S6 D3's shape: from the menu, Episodes and Bonus. Episodes has two episodes, a commentary
    # and a deleted scene, and leads on to a second page with two more of each sort; Bonus has an
    # extra of its own, and leads to pages of the commentaries and deleted scenes again
    M, EP, EP2, BO, DE, CO = (1, 1, 1), (1, 2, 1), (1, 3, 1), (1, 4, 1), (1, 5, 1), (1, 6, 1)
    pages = {(1, 1): {"buttons": [_b(1, "link_page", 2), _b(2, "link_page", 4)]},
             (1, 2): {"buttons": [_b(1), _b(2), _b(3), _b(4), _b(5, "link_page", 3)]},
             (1, 3): {"buttons": [_b(1), _b(2), _b(3), _b(4)]},
             (1, 4): {"buttons": [_b(1), _b(2, "link_page", 5), _b(3, "link_page", 6)]},
             (1, 5): {"buttons": [_b(1), _b(2)]},
             (1, 6): {"buttons": [_b(1), _b(2)]}}
    plays = lambda t: {"plays": {"title": t, "chapter": 1}, "engine": True}
    known = {(EP, 1): plays(13), (EP, 2): plays(14), (EP, 3): plays(6), (EP, 4): plays(23),
             (EP2, 1): plays(15), (EP2, 2): plays(16), (EP2, 3): plays(10), (EP2, 4): plays(26),
             (BO, 1): plays(27), (DE, 1): plays(22), (DE, 2): plays(24), (CO, 1): plays(5), (CO, 2): plays(9)}
    copies = {6: 5, 10: 9, 23: 22, 26: 24}
    routed = menus._routes(pages, M)
    plan = lambda done, found, copies=copies: menus._page_plan(pages, routed, done, found, known, {}, copies)
    assert plan({M}, set()) == [EP, EP2, BO]                # (the pages of copies never read)
    # without the scan's word on copies, they're titles of their own: all the pages it takes
    assert set(plan({M}, set(), {})) == {EP, EP2, BO, DE, CO}
    # as far as it goes: Episodes read, the rest as planned
    assert plan({M, EP}, menus._with_copies({13, 14, 6, 23}, copies)) == [EP2, BO]
    # title 26's button read badly: its deleted scene's left, and the page that has it is planned
    done = {M, EP, EP2, BO}
    found = menus._with_copies({13, 14, 6, 23, 15, 16, 10, 27}, copies)
    assert plan(done, found) == [DE]
    # title 6's and its other button both read badly, every page read: nothing more to plan
    assert plan(done | {DE, CO}, found) == []
    # already named: a page with nothing new isn't planned, whatever's on the way
    assert plan({M, EP, EP2}, menus._with_copies({13, 14, 6, 23, 15, 16, 10, 26}, copies)) == [BO]


def test_the_plan_counts_the_way_there_and_ties_go_by_rank():
    M, A, B, C = (1, 1, 1), (1, 2, 1), (1, 3, 1), (1, 4, 1)
    pages = {(1, 1): {"buttons": [_b(1, "link_page", 2), _b(2, "link_page", 3)]},
             (1, 2): {"buttons": [_b(1), _b(2, "link_page", 4)]},
             (1, 3): {"buttons": [_b(1), _b(2)]},
             (1, 4): {"buttons": [_b(1), _b(2)]}}
    plays = lambda t: {"plays": {"title": t, "chapter": 1}, "engine": True}
    # A costs one page for one title; C past it, two for three; B one for two: B, then C by way of A
    known = {(A, 1): plays(1), (B, 1): plays(2), (B, 2): plays(3), (C, 1): plays(4), (C, 2): plays(5)}
    routed = menus._routes(pages, M)
    assert menus._page_plan(pages, routed, {M}, set(), known, {}) == [B, A, C]
    # one title each: words put the page behind "Audio" later
    known = {(A, 1): plays(1), (B, 1): plays(2)}
    assert menus._page_plan(pages, routed, {M}, set(), known, {}, rank=lambda s: {A: 1, B: 0}.get(s, 0)) == [B, A]
    assert menus._page_plan(pages, routed, {M}, set(), known, {}, rank=lambda s: {A: 0, B: 1}.get(s, 0)) == [A, B]


def test_the_plan_takes_a_scene_pages_button_as_naming_its_title():
    # Smallville's: its Episodes page's buttons lead to each episode's scenes: no need to read those
    M, E, S1, S2 = (1, 1, 1), (1, 2, 1), (1, 3, 1), (1, 4, 1)
    pages = {(1, 1): {"buttons": [_b(1, "link_page", 2)]},
             (1, 2): {"buttons": [_b(1, "link_page", 3), _b(2, "link_page", 4)]},
             (1, 3): {"buttons": [_b(1), _b(2)]},
             (1, 4): {"buttons": [_b(1), _b(2)]}}
    chapter = lambda t, n: {"plays": {"title": t, "chapter": n}, "engine": True}
    known = {(S1, 1): chapter(3, 1), (S1, 2): chapter(3, 2), (S2, 1): chapter(4, 1), (S2, 2): chapter(4, 2)}
    routed = menus._routes(pages, M)
    assert menus._page_plan(pages, routed, {M}, set(), known, {}) == [E]
    assert menus._named_titles(pages, [M, E], known, {}, {}, search_words({(E, 1): "Pilot", (E, 2): "Metamorphosis"})) == {3, 4}
    assert menus._page_plan(pages, routed, {M, E}, {3, 4}, known, {}) == []


def test_words_badly_read_name_nothing():
    pages = {(1, 1): {"buttons": [_b(1)]}}
    known = {(MAIN, 1): {"plays": {"title": 3, "chapter": 1}, "engine": True}}
    named = lambda text: menus._named_titles(pages, [MAIN], known, {}, {}, search_words({(MAIN, 1): text}))
    for bad in ("", "On", "Vi", "~ ."):
        assert named(bad) == set(), bad
    for good in ("OFF", "Video Commentary", "Pilot"):
        assert named(good) == {3}, good


def test_the_crawl_follows_its_plan_and_a_failed_reading_changes_it(monkeypatch, tmp_path):
    from disc_detective import dvdvm
    held = {k: dict(v, cells=[{"still": 255}]) for k, v in EE_PAGES.items()}
    monkeypatch.setattr(menus, "_Crawler", FakeCrawler)
    monkeypatch.setattr(menus, "_pages", lambda structure: held)
    monkeypatch.setattr(menus, "_scan_titles", lambda drive, log: [{"id": "Title 9", "role": "duplicate", "note": "same video as Title 5"}])
    monkeypatch.setattr(menus, "title_pictures", lambda out, out_dir: {})
    monkeypatch.setattr(menus, "keeping_test_data", lambda: False)
    monkeypatch.setattr(dvdvm, "Disc", lambda drive, structure: None)
    monkeypatch.setattr(dvdvm, "Engine", lambda disc: None)
    monkeypatch.setattr(dvdvm, "explore", lambda eng: ({"goes_to": MAIN}, ENGINE, []))
    monkeypatch.setattr(dvdvm, "opening", lambda eng: [])
    original = FakeCrawler.read_page

    def slowly(self, s):
        fresh = s not in self.reading
        original(self, s)
        if fresh and s == BONUS:
            SLOW_WORDS.add(s)
    monkeypatch.setattr(FakeCrawler, "read_page", slowly)
    FakeCrawler.made.clear()
    try:
        menus._crawl("E:\\", "vlc", {}, str(tmp_path), log=lambda m: None)
    finally:
        SLOW_WORDS.clear()
    # title 5's button is still being read: taken to name title 9 too, so Audio's not planned. Its
    # words are waited for at the plan's end, and they do
    assert FakeCrawler.made[0].visited == [MAIN, BONUS]
    # they don't: then the plan goes on to the copy's page
    monkeypatch.setitem(WORDS, (BONUS, 1), "On")
    FakeCrawler.made.clear()
    try:
        menus._crawl("E:\\", "vlc", {}, str(tmp_path), log=lambda m: None)
    finally:
        SLOW_WORDS.clear()
    assert FakeCrawler.made[0].visited == [MAIN, BONUS, AUDIO]


def test_the_crawl_searches_for_titles(monkeypatch, tmp_path):
    from disc_detective import dvdvm
    monkeypatch.setattr(menus, "_Crawler", FakeCrawler)
    monkeypatch.setattr(menus, "_pages", lambda structure: EE_PAGES)
    monkeypatch.setattr(menus, "_scan_titles", lambda drive, log: SCANNED)
    monkeypatch.setattr(menus, "title_pictures", lambda out, out_dir: {})
    monkeypatch.setattr(menus, "keeping_test_data", lambda: False)
    monkeypatch.setattr(dvdvm, "Disc", lambda drive, structure: None)
    monkeypatch.setattr(dvdvm, "Engine", lambda disc: None)
    monkeypatch.setattr(dvdvm, "explore", lambda eng: ({"goes_to": MAIN}, ENGINE, []))
    monkeypatch.setattr(dvdvm, "opening", lambda eng: [])
    FakeCrawler.made.clear()
    out = menus._crawl("E:\\", "vlc", {}, str(tmp_path), log=lambda m: None)
    c = FakeCrawler.made[0]
    # Bonus before the page behind "Audio Commentary", which is still read for its title 9. Scenes and
    # Credits hold only the film, named by Play: not read, whatever they say
    assert c.visited == [MAIN, BONUS, AUDIO]
    by = {(p["page"], p["program"]): p for p in out["pages"]}
    assert by[(4, 1)]["passed_by"] == "scenes"
    assert by[(5, 1)]["passed_by"] == "no title there that isn't named elsewhere"
    assert by[(3, 1)]["passed_by"] is None and by[(3, 1)]["buttons"][0]["text"] == ""
    # Mystery found title 7, the last with no way to it: "Set Up" is never pressed, its turn being last
    assert c.presses == [(BONUS, 4)]
    assert by[(2, 1)]["buttons"][3]["plays"]["title"] == 7
    # title 9 the same video as Bonus's title 5, named there: nothing behind Audio Commentary to look for
    SCANNED.append({"id": "Title 9", "role": "duplicate", "note": "same video as Title 5"})
    try:
        FakeCrawler.made.clear()
        menus._crawl("E:\\", "vlc", {}, str(tmp_path), log=lambda m: None)
        assert FakeCrawler.made[0].visited == [MAIN, BONUS]
        # unless a picture of every title's asked for
        FakeCrawler.made.clear()
        menus._crawl("E:\\", "vlc", {}, str(tmp_path), log=lambda m: None, every_title=True)
        assert FakeCrawler.made[0].visited == [MAIN, BONUS, AUDIO]
        # a Play All the scan knows: no page read for it either
        SCANNED[:] = [{"id": "Title 9", "role": "play-all", "note": "plays Title 1, Title 5"}]
        FakeCrawler.made.clear()
        menus._crawl("E:\\", "vlc", {}, str(tmp_path), log=lambda m: None)
        assert FakeCrawler.made[0].visited == [MAIN, BONUS]
    finally:
        SCANNED.clear()
