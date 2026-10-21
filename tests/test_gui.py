"""The app, driven headlessly: first-run download, Settings, a scan, the rip plan."""
import os
import time

import pytest

tk = pytest.importorskip("tkinter")
from disc_detective import app as dd_app, core  # noqa: E402
import fixtures  # noqa: E402

pytestmark = pytest.mark.gui


def shown_title(app):
    """The title as shown: the name, and the year beside it in its own label."""
    return (app.title_lbl.cget("text") + " " + app.year_lbl.cget("text")).strip()


@pytest.fixture
def gui(monkeypatch, tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.geometry("+0+0")
    monkeypatch.setattr(dd_app, "SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(dd_app, "optical_drives", lambda: [])
    monkeypatch.setattr(dd_app.App, "MIN_STATUS_MS", 0)      # the story plays instantly in tests
    # a real message box waits for a click forever (on Windows the tests open real windows)
    for name in ("showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(dd_app.messagebox, name, lambda *a, **k: None)
    for name in ("askyesno", "askokcancel", "askyesnocancel", "askquestion", "askretrycancel"):
        monkeypatch.setattr(dd_app.messagebox, name, lambda *a, **k: True)
    # likewise a native popup menu (Windows tracks it until clicked) and the file pickers;
    # the menu's entries are still built, so tests can read them
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, *a, **k: None)
    monkeypatch.setattr(dd_app.ThemedMenu, "headless", True)
    for name in ("askdirectory", "askopenfilename", "asksaveasfilename"):
        monkeypatch.setattr(dd_app.filedialog, name, lambda *a, **k: "")
    made = {}
    for cls in (dd_app.FirstRunDialog, dd_app.SettingsDialog):
        orig = cls.__init__

        def init(self, *a, _o=orig, _n=cls.__name__, **k):
            _o(self, *a, **k)
            made[_n] = self
        monkeypatch.setattr(cls, "__init__", init)

    def pump(cond, secs=30):
        t = time.time()
        while time.time() - t < secs:
            root.update()
            time.sleep(0.02)
            if cond():
                return True
        return False
    yield root, made, pump
    import gc
    made.clear()
    gc.collect()            # free Tk objects here, on the main thread: freed later from a worker
    root.destroy()          # thread, Tcl aborts the whole test run
    gc.collect()


def test_first_run_then_settings(gui, github, tmp_path, monkeypatch):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", str(tmp_path / "imdb"))
    app = dd_app.App(root)
    assert pump(lambda: "FirstRunDialog" in made, 5)
    fr = made["FirstRunDialog"]
    assert sorted(fr.missing) == ["discdb", "imdb"]
    fr.start()
    assert pump(lambda: "Ready" in fr.prog.text.cget("text") or "Couldn't" in fr.prog.text.cget("text"), 60)
    assert "Ready" in fr.prog.text.cget("text")
    app.open_settings()
    sd = made["SettingsDialog"]
    assert "2 discs" in sd.status["discdb"].cget("text") and "titles" in sd.status["imdb"].cget("text")
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **p: {})
    sd.key.set("abc123")
    sd.test_key()
    assert pump(lambda: "works" in sd.key_note.cget("text"), 5)
    sd.pictures.set(False)
    assert sd.order.get() == "aired"                 # Plex's default
    sd.order.set("dvd")
    sd.update("discdb")
    assert pump(lambda: "Up to date" in sd.prog.text.cget("text"))
    sd.close()
    args = app.scan_args()
    assert args.tmdb_key == "abc123" and args.no_art and not args.no_discdb_online
    assert args.episode_order == "dvd"
    assert dd_app.load_settings()["tmdb_key"] == "abc123"


def test_scan_shows_the_answer_and_plan(gui, tmp_path, imdb_dir, monkeypatch):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    orig = core.default_args
    monkeypatch.setattr(core, "default_args", lambda **kw: orig(**dict(kw, offline=True)))
    disc = fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED"))
    app = dd_app.App(root)
    app.source.set(disc)
    app.source_changed()
    app.outdir.set(str(tmp_path / "Rips"))
    app.scan()
    assert pump(lambda: app.rep is not None and not app.busy, 60), app.status.cget("text")
    assert shown_title(app).startswith("Justified")
    assert app.plan_lbl.cget("text") == "Will rip 4 episodes (S01E01–S01E04)"
    assert app.standing_lbl.cget("text") == "Case closed" and app.status.cget("text") == ""
    plan = dd_app.plan_rip(app.rep, dd_app.preview_titles(app.rep), False)
    assert [p["dest"].split("/")[-1] for p in plan][0].startswith("Justified (2010) - s01e01 - Fire in the Hole")
    if app.rep.get("art"):
        assert app.pic_col.winfo_ismapped()


def test_choose_another_match_and_forget_it(gui, tmp_path, imdb_dir, monkeypatch):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    orig = core.default_args
    monkeypatch.setattr(core, "default_args", lambda **kw: orig(**dict(kw, offline=True)))
    disc = fixtures.make_reel_movie_bluray(str(tmp_path / "CHICKEN_RUN"))
    app = dd_app.App(root)
    app.source.set(disc)
    app.source_changed()

    def scan():
        app.rep = None
        app.scan()
        assert pump(lambda: app.rep is not None and not app.busy, 60), app.status.cget("text")
    scan()
    assert shown_title(app).startswith("Chicken Run (2000)")
    assert app.plan_lbl.cget("text").startswith("Will rip the feature")
    assert app.match_btn.winfo_ismapped() and app.match_btn.cget("text") == "Not right?"
    assert app.confirm_btn.winfo_ismapped() and not app.confirmed_lbl.winfo_ismapped()
    app.match_menu()
    labels = [app.match_popup.entrycget(i, "label") for i in range(app.match_popup.index("end") + 1)
              if app.match_popup.type(i) == "command"]
    app.match_popup.unpost()
    assert labels == ["Enter it myself…"]          # one confident match: nothing else to offer; no "yes" in here
    app.choose({"kind": "movie", "name": "Chicken Run: Dawn of the Nugget", "year": 2023})   # saves + rescans
    assert pump(lambda: app.rep is not None and not app.busy and app.rep.get("personal_hit"), 60)
    assert shown_title(app).startswith("Chicken Run: Dawn of the Nugget (2023)")
    assert app.confirmed_lbl.winfo_ismapped() and not app.confirm_btn.winfo_ismapped()
    app.use_answer(None)                                  # "Forget my answer"
    assert pump(lambda: app.rep is not None and not app.busy and not app.rep.get("personal_hit"), 60)
    assert shown_title(app).startswith("Chicken Run (2000)")
    app.confirm()                                         # ✓ That's right
    assert pump(lambda: app.rep is not None and not app.busy and app.rep.get("personal_hit"), 60)
    assert app.rep["personal_hit"]["truth"].startswith("Chicken Run (2000) {imdb-tt")
    assert app.confirmed_lbl.winfo_ismapped() and shown_title(app) == "Chicken Run (2000)"


def test_type_the_answer_in(gui, tmp_path, monkeypatch):
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    got = []
    monkeypatch.setattr(app, "use_answer", got.append)
    d = dd_app.MatchDialog(root, app, title="Justified", year=2010, tv=True, season=1, disc=None)
    d.disc.set("2")
    d.ok()
    d2 = dd_app.MatchDialog(root, app, title="", year=None)
    d2.ok()                                               # no title: stays open and says so
    assert "Enter the title" in d2.note.cget("text")
    d2.title.set("Hercules")
    d2.year.set("1997")
    d2.ok()
    assert got == ["Justified (2010) S1 D2", "Hercules (1997)"]


def test_type_ahead_picks_an_exact_imdb_entry(gui, imdb_dir, monkeypatch, tmp_path):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    png = str(tmp_path / "p.png")
    tk.PhotoImage(width=40, height=60).write(png, format="png")
    asked = []
    monkeypatch.setattr(core, "thumbs_for", lambda cands, args: asked.append(len(cands)) or
                        {c["imdb_id"]: png for c in cands})
    app = dd_app.App(root)
    got = []
    monkeypatch.setattr(app, "use_answer", got.append)
    d = dd_app.MatchDialog(root, app, title="")
    d.title.set("Hercul")
    assert pump(lambda: len(d.results.get_children()) >= 4, 5)
    rows = {iid: d.results.item(iid, "text") for iid in d.results.get_children()}
    assert pump(lambda: all(d.results.item(i, "image") for i in rows), 5)      # posters filled in
    assert asked
    iid = next(i for i, t in rows.items() if "Hercules (1998)" in t)
    d.results.selection_set(iid)
    d.pick()
    assert d.tv.get() and d.picked["kind"] == "tv" and d.pick_lbl.cget("text").startswith("Using IMDb tt")
    d.ok()                                                # TV without a season: asks for it
    assert "season" in d.note.cget("text") and not got
    d.season.set("1")
    d.ok()
    assert got[0].startswith("Hercules (1998) S1 {imdb-tt")


def test_menu_shows_posters_beside_guesses(gui, tmp_path, monkeypatch):
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    png = str(tmp_path / "p.png")
    tk.PhotoImage(width=40, height=60).write(png, format="png")
    app = dd_app.App(root)
    app.rep = {"content_hash": "B" * 32, "label_parsed": {}, "kind": "movie", "best_guess": "Hercules (1997)",
               "candidates": [{"kind": "movie", "name": "Hercules", "year": 1997, "score": 70, "imdb_id": "tt1", "thumb": png},
                              {"kind": "movie", "name": "Hercules", "year": 2014, "score": 65, "imdb_id": "tt2", "thumb": png},
                              {"kind": "tv", "name": "Hercules", "year": 1998, "score": 50, "imdb_id": "tt3"}]}
    app.match_menu()
    m = app.match_popup
    entries = [(m.entrycget(i, "label"), m.entrycget(i, "image")) for i in range(m.index("end") + 1)
               if m.type(i) == "command"]
    m.unpost()
    assert not any(l.startswith("✓") for l, _ in entries)               # confirming is its own button now
    assert not any("(1997)" in l for l, _ in entries)                   # the guess in use isn't an "other"
    with_img = [l for l, img in entries if img]
    assert any("(2014)" in l for l in with_img)
    assert any("(1998)" in l for l, img in entries if not img)       # no poster: still listed


def test_title_is_clean_and_details_go_underneath():
    rep = {"content_hash": "ABCDEF0123456789" * 2, "kind": "movie", "label": "HERCULES", "mastered": "2000-05",
           "best_guess": "Hercules (1997) {imdb-tt0119282}", "personal_hit": {"truth": "Hercules (1997) {imdb-tt0119282}"},
           "match": {"name": "Hercules", "year": 1997, "kind": "movie", "imdb_id": "tt0119282", "sure": True}}
    assert dd_app.display_title(rep) == "Hercules (1997)"
    d = dict(dd_app.detail_rows(rep))
    assert d == {"IMDb": "tt0119282", "Disc label": "HERCULES", "Mastered": "2000-05", "Fingerprint": "ABCDEF01"}
    tv = {"content_hash": "0" * 32, "kind": "tv", "match": {"name": "Psych", "year": 2006, "kind": "tv",
                                                          "season": 1, "disc": 2}}
    assert dd_app.display_title(tv) == "Psych (2006)  ·  Season 1, Disc 2"
    assert dd_app.display_title({"content_hash": "0" * 32, "best_guess": "Some Label (unconfirmed) | x"}) == "Some Label (unconfirmed)"


def test_short_film_disc_confirm_and_fix_titles(gui, tmp_path, imdb_dir, monkeypatch):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    orig = core.default_args
    monkeypatch.setattr(core, "default_args", lambda **kw: orig(**dict(kw, offline=True, label="WALLACE_AND_GROMIT")))
    disc = fixtures.make_shorts_bluray(str(tmp_path / "WALLACE_AND_GROMIT"))
    app = dd_app.App(root)
    app.source.set(disc)
    app.source_changed()

    def wait(cond):
        assert pump(lambda: app.rep is not None and not app.busy and cond(), 60), app.status.cget("text")
    app.scan()
    wait(lambda: True)
    assert shown_title(app) == "Wallace & Gromit Collection"
    assert app.plan_lbl.cget("text") == "Will rip 3 films"
    assert app.confirm_btn.winfo_ismapped()                         # collections can be confirmed now
    app.match_menu()
    labels = [app.match_popup.entrycget(i, "label") for i in range(app.match_popup.index("end") + 1)
              if app.match_popup.type(i) == "command"]
    app.match_popup.unpost()
    assert "Change the films…" in labels and "It's one film or TV show instead…" in labels
    scans = []
    app.scan = lambda: scans.append(1)                              # (an instance attribute: del restores)
    app.confirm()                                                   # ✓ That's right: saved, no rescan
    del app.scan
    root.update()
    assert not scans and app.rep.get("personal_hit")
    assert app.status.cget("text").startswith("Confirmed")
    assert app.rep["kind"] == "collection" and app.confirmed_lbl.winfo_ismapped()
    # Change the films: swap two, drop one
    d = dd_app.CollectionDialog(root, app)
    rows = [t["id"] for t in d.rows]
    assert rows[:4] == ["00001.mpls", "00002.mpls", "00003.mpls", "00004.mpls"]
    shave = dict(d.assign["00003.mpls"])
    d.set(next(t for t in d.rows if t["id"] == "00002.mpls"), shave)
    d.clear(next(t for t in d.rows if t["id"] == "00003.mpls"))
    texts = [w.cget("text") for w in d.grid.winfo_children() if w.winfo_class() == "TLabel" and w.cget("text")]
    assert any(x.endswith("A Close Shave (1995)") for x in texts) and "not a film (extra)" in texts
    d.name_var.set("Wallace & Gromit: Three Amazing Adventures")    # the name is editable too
    d.save()
    app.rep = None
    wait(lambda: app.rep.get("personal_hit"))
    items = {i["title_id"]: i["name"] for i in app.rep["candidates"][0]["items"]}
    assert items == {"00001.mpls": "A Grand Day Out", "00002.mpls": "Wallace & Gromit: A Close Shave"}
    assert shown_title(app) == "Wallace & Gromit: Three Amazing Adventures"
    assert app.plan_lbl.cget("text") == "Will rip 2 films"


def test_picking_one_film_for_a_title(gui, imdb_dir, monkeypatch):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    monkeypatch.setattr(core, "thumbs_for", lambda cands, args: {})
    app = dd_app.App(root)
    got = []
    d = dd_app.MatchDialog(root, app, title="", on_pick=got.append)
    assert not d.tv_row.winfo_ismapped()
    d.ok()
    assert "Pick one" in d.note.cget("text") and not got                 # typing alone isn't enough here
    d.title.set("Wrong Trousers")
    assert pump(lambda: len(d.results.get_children()) >= 1, 5)
    d.results.selection_set("0")
    d.pick()
    d.ok()
    assert got and got[0]["name"].endswith("The Wrong Trousers") and got[0]["imdb_id"].startswith("tt")


def test_long_title_list_scrolls_and_keeps_the_buttons_on_screen(gui, tmp_path, imdb_dir, monkeypatch):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    orig = core.default_args
    monkeypatch.setattr(core, "default_args", lambda **kw: orig(**dict(kw, offline=True)))
    app = dd_app.App(root)
    app.source.set(fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED")))
    app.source_changed()
    app.scan()
    assert pump(lambda: app.rep is not None and not app.busy, 60)
    app.rep["titles"] += [{"id": "%05d.mpls" % (500 + i), "role": "extra", "duration": 120 + i, "audio": []}
                          for i in range(90)]                           # a disc with lots of extras
    picker = dd_app.TitlePicker(root, app)
    root.update()
    top, screen = picker.top, root.winfo_screenheight()
    assert picker.area.scrolls() and picker.area.bar.winfo_ismapped()
    assert top.winfo_height() <= screen
    ok = [w for w in top.winfo_children()[0].winfo_children() if w.cget("text") == "OK"] or \
         [w for f in top.winfo_children() for w in f.winfo_children()
          if w.winfo_class() == "TButton" and w.cget("text") == "OK"]
    assert ok and ok[0].winfo_ismapped() and ok[0].winfo_rooty() + ok[0].winfo_height() <= screen
    # the wheel scrolls the list with the pointer over a row, not just over the scrollbar
    row = [w for w in picker.area.inner.winfo_children() if w.winfo_class() == "TLabel"][10]
    assert picker.area.canvas.yview()[0] == 0
    row.event_generate("<MouseWheel>", delta=-120)
    root.update()
    assert picker.area.canvas.yview()[0] > 0
    row.event_generate("<MouseWheel>", delta=120)
    root.update()
    assert picker.area.canvas.yview()[0] == 0
    # a trackpad (Windows precision touchpad) sends many small deltas: they add up
    for _ in range(8):
        row.event_generate("<MouseWheel>", delta=-15)
    root.update()
    assert picker.area.canvas.yview()[0] > 0
    picker.area.canvas.yview_moveto(0)
    root.update()
    # Tk 9 trackpads send <TouchpadScroll> (vertical step in the low 16 bits of %D), measured
    # on Windows: 65347 is -189 (down the page), 268 is up; Tk 8.6 can't generate it, so fake one
    assert dd_app.ScrollArea.touch_dy(65347) == -189 and dd_app.ScrollArea.touch_dy(268) == 268
    assert dd_app.ScrollArea.touch_dy((5 << 16) | 0) == 0                 # sideways only
    fake = type("E", (), {"widget": row, "delta": 65347, "x_root": 0, "y_root": 0})()
    dd_app.ScrollArea._dispatch_touch(fake)
    root.update()
    assert picker.area.canvas.yview()[0] > 0
    picker.area.canvas.yview_moveto(0)
    root.update()
    # Windows may deliver the wheel to the focused widget elsewhere: the pointer decides
    ok[0].event_generate("<MouseWheel>", delta=-120, rootx=row.winfo_rootx() + 5, rooty=row.winfo_rooty() + 5,
                         x=0, y=0)
    root.update()
    assert picker.area.canvas.yview()[0] > 0
    picker.top.destroy()
    small = dd_app.ScrollArea(root)                                      # short lists don't scroll
    ttk_label = dd_app.ttk.Label(small.inner, text="one row")
    ttk_label.pack()
    small.pack()
    root.update()
    assert not small.scrolls() and not small.bar.winfo_ismapped()



def test_ticked_version_is_an_edition():
    rep = {"format": "DVD", "content_hash": "8" * 32, "plex_names": [], "discdb": {"hit": None},
           "candidates": [{"kind": "movie", "name": "Film", "year": 1999, "imdb_id": "tt0000009", "score": 99}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 5400, "video": "NTSC 16:9"},
                      {"id": "Title 2", "role": "version", "duration": 5400, "video": "NTSC 4:3",
                       "edition": "Fullscreen"}]}
    rep["plex_names"] = ["Title 1        -> Film (1999) {imdb-tt0000009}/Film (1999).mkv"]
    plan = dd_app.plan_rip(rep, dd_app.preview_titles(rep), False)
    assert [p["title_id"] for p in plan] == ["Title 1"]                    # widescreen only by default
    both = dd_app.plan_rip(rep, dd_app.preview_titles(rep), False, selected={"Title 1", "Title 2"})
    assert [p["dest"] for p in both] == ["Film (1999) {imdb-tt0000009}/Film (1999).mkv",
                                         "Film (1999) {imdb-tt0000009}/Film (1999) {edition-Fullscreen}.mkv"]


def test_a_discdb_hit_needs_no_confirming(gui, monkeypatch):
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    cand = {"kind": "movie", "name": "WALL·E", "year": 2008, "imdb_id": "tt0910970", "score": 145}
    rep = {"candidates": [cand], "match": {"name": "WALL·E"}, "personal_hit": None,
           "discdb": {"hit": {"title": "WALL·E", "titles": []}}}
    app.show_match_button(rep)
    root.update()
    assert app.confirmed_lbl.winfo_ismapped() and app.confirmed_lbl.cget("text") == "TheDiscDB"
    assert not app.confirm_btn.winfo_ismapped() and app.match_btn.cget("text") == "Not right?"
    rep["discdb"] = {"hit": None}                                     # an IMDb match still asks
    app.show_match_button(rep)
    root.update()
    assert app.confirm_btn.winfo_ismapped() and not app.confirmed_lbl.winfo_ismapped()


def test_without_makemkv_rip_is_greyed_but_the_menu_works(gui, tmp_path, imdb_dir, monkeypatch):
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    orig = core.default_args
    monkeypatch.setattr(core, "default_args", lambda **kw: orig(**dict(kw, offline=True)))
    monkeypatch.setattr(dd_app.App, "makemkv_exe", lambda self: None)
    app = dd_app.App(root)
    app.source.set(fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED")))
    app.source_changed()
    app.scan()
    assert pump(lambda: app.rep is not None and not app.busy, 60)
    assert str(app.rip_btn.button.cget("state")) == "disabled"
    assert str(app.rip_btn.arrow.cget("state")) == "normal"          # Choose titles still opens
    assert "install MakeMKV" in app.rip_note.cget("text")
    monkeypatch.setattr(dd_app.App, "makemkv_exe", lambda self: "/usr/bin/makemkvcon")
    app.show_plan_summary()
    assert str(app.rip_btn.button.cget("state")) == "normal"


def test_fallback_never_picks_a_menu_loop():
    rep = {"format": "Blu-ray", "content_hash": "7" * 32, "plex_names": [], "discdb": {"hit": None}, "candidates": [],
           "label": "X", "titles": [{"id": "00020.mpls", "role": "loop", "duration": 14356, "video": ""},
                                    {"id": "00705.mpls", "role": "extra", "duration": 1132, "video": ""}]}
    plan = dd_app.plan_rip(rep, dd_app.preview_titles(rep), False)
    assert [p["title_id"] for p in plan] == ["00705.mpls"]


def test_discdb_row_names_the_disc_before_its_set():
    rows = dict(dd_app.detail_rows({"discdb": {"hit": {"disc": "Main Movie Blu-ray",
                                                       "release": "Ultimate Collector's Edition 4K"}}}))
    assert rows["TheDiscDB"] == "Main Movie Blu-ray, in the Ultimate Collector's Edition 4K release"


def test_plan_lists_dvd_ordered_episodes_as_they_are():
    plan = [{"role": "episode", "dest": "Firefly (2002)/Season 01/Firefly (2002) - s01e%02d - x.mkv" % n}
            for n in (11, 1, 2)]
    assert "(S01E11, S01E01, S01E02)" in dd_app.describe_plan(plan, False)
    plan = [{"role": "episode", "dest": "S/Season 01/S - s01e%02d - x.mkv" % n} for n in (5, 6, 7)]
    assert "(S01E05–S01E07)" in dd_app.describe_plan(plan, False)


def test_enter_it_myself_with_nothing_identified(gui, monkeypatch):
    """A disc with a placeholder label (LOGICAL_VOLUME_ID): no guess at all, best_guess None."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    app.rep = {"content_hash": "0" * 32, "candidates": [], "best_guess": None, "kind": "movie",
               "label_parsed": {"raw": "LOGICAL_VOLUME_ID", "query": "", "placeholder": True}}
    app.match_menu()
    m = app.match_popup
    i = next(i for i in range(m.index("end") + 1) if m.type(i) == "command"
             and m.entrycget(i, "label") == "Enter it myself…")
    before = set(root.winfo_children())
    m.invoke(i)                                         # opened the dialog, no AttributeError
    new = [w for w in root.winfo_children() if w not in before]
    assert new
    for w in new:
        w.destroy()


def test_status_story_and_case_notes(gui, tmp_path, imdb_dir, monkeypatch):
    """Each step stays on the status line for a while, the verdict comes last, and the case notes
    window shows the technical side (DEBUG detail included) of the same scan."""
    root, made, pump = gui
    monkeypatch.setenv("DISC_DETECTIVE_IMDB", imdb_dir)
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    orig = core.default_args
    monkeypatch.setattr(core, "default_args", lambda **kw: orig(**dict(kw, offline=True)))
    monkeypatch.setattr(dd_app.App, "MIN_STATUS_MS", 30)
    old_level = core.logger.level
    core.logger.setLevel(10)                          # as setup_logging leaves it: DEBUG reaches the app
    try:
        disc = fixtures.make_reel_movie_bluray(str(tmp_path / "CHICKEN_RUN"))
        app = dd_app.App(root)
        shown = []
        real = app.set_status
        monkeypatch.setattr(app, "set_status", lambda text: (shown.append(text), real(text)))
        app.source.set(disc)
        app.source_changed()
        app.scan()
        assert pump(lambda: app.rep is not None and not app.busy, 60), app.status.cget("text")
        steps = [x for x in shown if x.endswith("\u2026")]
        assert steps[0] == "Arriving at the scene\u2026" and "Dusting for fingerprints\u2026" in steps
        assert app.standing_lbl.cget("text") == "Case closed" and shown[-1] == ""    # the verdict after the story
        app.open_notes()
        text = app.notes_win.text.get("1.0", "end")
        assert "step: fingerprint" in text and "candidate" in text and "---- scan of" in text
        assert not text.startswith("\n")
        app.scan()                                    # a second scan: a blank line before its divider
        assert pump(lambda: not app.busy and app.rep is not None, 60)
        text = app.notes_win.text.get("1.0", "end")
        lines = text.split("\n")
        i = [k for k, x in enumerate(lines) if "---- scan of" in x]
        assert len(i) >= 2 and lines[i[-1] - 1] == ""
        app.notes_win.top.destroy()
        app.scan()                                    # scanned with the notes closed: only that scan
        assert pump(lambda: not app.busy and app.rep is not None, 60)
        app.open_notes()
        text = app.notes_win.text.get("1.0", "end")
        assert text.count("---- scan of") == 1 and text.startswith(text.split("\n")[0]) and "---- scan of" in text.split("\n")[0]
        app.notes_win.top.destroy()
    finally:
        core.logger.setLevel(old_level)


def test_a_films_special_features_disc_rips_its_extras():
    """MY FAIR LADY DISC 2: the film's extras disc. Nothing on it is the film (TheDiscDB names
    the 58-minute making-of an extra), and its extras are what to rip, under TheDiscDB's names."""
    hit = {"title": "My Fair Lady", "year": 1964, "type": "movie", "titles": [
        {"source": "00000.mpls", "item": {"title": "More Loverly Than Ever: The Making of My Fair Lady",
                                          "type": "Extra"}},
        {"source": "00030.mpls", "item": {"title": "Alternate Audrey Hepburn Vocals: Show Me",
                                          "type": "DeletedScene"}},
        {"source": "00028.mpls", "item": {}}]}
    titles = [{"id": "00000.mpls", "role": "feature", "duration": 3478, "video": ""},
              {"id": "00030.mpls", "role": "episode", "duration": 168, "video": "", "episode_index": 1},
              {"id": "00016.mpls", "role": "loop", "duration": 1218, "video": ""}]
    assert core.discdb_extras_disc(titles, hit)
    assert [t["role"] for t in titles] == ["extra", "extra", "loop"]
    assert titles[0]["note"].startswith("TheDiscDB: More Loverly")
    rep = {"content_hash": "B9C6C324" + "0" * 24, "format": "Blu-ray", "kind": "movie", "extras_disc": True,
           "discdb": {"hit": hit}, "candidates": [], "best_guess": "My Fair Lady | Blu-ray", "titles": titles}
    plan = dd_app.plan_rip(rep, dd_app.preview_titles(rep), False)            # extras on by default here
    assert sorted(p["title_id"] for p in plan) == ["00000.mpls", "00030.mpls"]
    assert dd_app.describe_plan(plan, False) == "Will rip 2 special features"
    assert any("Deleted Scenes/Alternate Audrey Hepburn Vocals - Show Me" in p["dest"] for p in plan)
    # a disc with the film on it is left alone
    assert not core.discdb_extras_disc([{"id": "00800.mpls", "role": "feature", "duration": 9000}],
                                       dict(hit, titles=hit["titles"] + [{"source": "00800.mpls",
                                                                           "item": {"type": "MainMovie"}}]))


def test_a_second_copy_of_a_film_gets_an_edition(tmp_path):
    """Disc 2 of a set is the same film in full screen: its file is already there from disc 1, so
    it's named as Plex's other edition of the film, not 'Film (2).mkv'. Extras just get a (2)."""
    folder = tmp_path / "Swiss Family Robinson (1960) {imdb-tt0054357}"
    folder.mkdir()
    film = folder / "Swiss Family Robinson (1960).mkv"
    film.write_bytes(b"")
    rep = {"format": "DVD", "titles": [{"id": "Title 1", "duration": 7572, "role": "feature",
                                        "video_info": {"aspect": "4:3"}}]}
    item = {"title_id": "Title 1", "role": "feature"}
    assert dd_app.rip_path(str(film), item, rep) == str(folder / "Swiss Family Robinson (1960) {edition-Fullscreen}.mkv")
    rep["titles"][0]["video_info"] = {"aspect": "16:9"}
    assert dd_app.rip_path(str(film), item, rep).endswith("{edition-Widescreen}.mkv")
    rep["format"], rep["titles"][0]["video_info"] = "Blu-ray", {"height": 2160, "aspect": "16:9"}
    assert dd_app.rip_path(str(film), item, rep).endswith("{edition-4K}.mkv")
    extra = folder / "Other" / "Making Of.mkv"
    extra.parent.mkdir()
    extra.write_bytes(b"")
    assert dd_app.rip_path(str(extra), {"title_id": "Title 4", "role": "extra"}, rep).endswith("Making Of (2).mkv")
    new = folder / "Nothing There Yet.mkv"
    assert dd_app.rip_path(str(new), item, rep) == str(new)          # free names are left alone


def test_collection_strip_columns_line_up(gui, monkeypatch):
    """Each film in a collection's strip is as wide as the others, whatever its name's length and
    whether it has a poster, so a long first name doesn't push the rest along."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    items = [{"title_id": "Title %d" % k, "name": n, "year": y, "imdb_id": None}
             for k, (n, y) in enumerate([("Kung Fu Panda: Secrets of the Scroll", 2016),
                                         ("Kung Fu Panda: Secrets of the Masters", 2011),
                                         ("Kung Fu Panda: Secrets of the Furious Five", 2008)], 53)]
    app.rep = {"content_hash": "0" * 32, "kind": "collection",
               "candidates": [{"kind": "collection", "score": 90, "items": items}]}
    plan = [{"title_id": it["title_id"], "role": "feature", "length": "0:25:00", "dest": "x/y.mkv"} for it in items]
    app.show_rip_films(plan)
    root.update()
    cells = app.strip.winfo_children()
    assert len(cells) == 3 and app.films_box.winfo_manager()
    widths = {c.winfo_width() for c in cells}
    xs = [c.winfo_x() for c in cells]
    assert xs == sorted(xs) and len({xs[1] - xs[0], xs[2] - xs[1]}) == 1     # evenly spaced columns
    assert max(widths) <= dd_app.di.ART_THUMB[0] + 26 + 2                       # about a poster wide
    app.show_rip_films(plan[1:])                         # only the titles being ripped
    root.update()
    assert len(app.strip.winfo_children()) == 2
    app.show_rip_films([])
    assert not app.films_box.winfo_manager()


def test_rip_films_lists_what_is_ripped():
    """A short-film disc: the ticked films, a second copy marked; extras aren't listed (the summary
    counts them). Any other disc: nothing (the picture already says it)."""
    items = [{"title_id": "Title 2", "name": "Wallace & Gromit: A Grand Day Out", "year": 1989, "art": {"image": "a.png"}},
             {"title_id": "Title 6", "name": "Wallace & Gromit: A Close Shave", "year": 1995}]
    rep = {"candidates": [{"kind": "collection", "score": 90, "items": items,
                           "versions": [{"title_id": "Title 7", "same_as": "Title 6"}]}]}
    plan = [{"title_id": "Title 2", "dest": "a/A.mkv", "length": "0:24:29"},
            {"title_id": "Title 7", "dest": "b/B.mkv", "length": "0:31:02"},
            {"title_id": "Title 16", "dest": "a/Other/Cracking Contraptions.mkv", "length": "0:23:48"}]
    got = dd_app.rip_films(rep, plan)
    assert [(e["name"], e["image"]) for e in got] == [
        ("A Grand Day Out (1989)", "a.png"), ("A Close Shave (1995) \u00b7 copy", None)]
    assert dd_app.rip_films({"extras_disc": True, "candidates": []}, plan) == []
    assert dd_app.rip_films({"kind": "movie", "candidates": [{"kind": "movie", "score": 90}]}, plan) == []


def test_a_bonus_disc_has_no_include_extras_option(gui, monkeypatch):
    """A bonus disc's extras are what it rips, so the Rip menu doesn't offer to include them."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    labels = lambda: [e["label"] for e in app.rip_btn.menu.entries]
    app._build_rip_menu()
    assert "Include extras" in labels()
    app.rep = {"content_hash": "0" * 32, "extras_disc": True}
    app._build_rip_menu()
    assert "Include extras" not in labels() and "Choose titles…" in labels()


def test_a_box_sets_bonus_disc_is_titled_by_its_set():
    """A box set's bonus disc: the set is the title and the disc's name sits under it; a film's
    disc keeps the film as its title. Either way the ID card says which set and which disc."""
    bs = {"title": "Indiana Jones 4-Movie Collection", "year": 2021, "disc": "Indiana Jones Bonus Disc",
          "number": 5, "count": 5}
    rep = {"content_hash": "D466AB14" + "0" * 24, "kind": "movie", "extras_disc": True, "boxset": bs,
           "match": {"name": "Indiana Jones and the Raiders of the Lost Ark", "year": 1981, "kind": "movie",
                     "imdb_id": "tt0082971"},
           "discdb": {"hit": {"disc": "Indiana Jones Bonus Disc", "release": "2021 UHD"}}}
    assert dd_app.display_title(rep) == "Indiana Jones 4-Movie Collection (2021)"
    assert ("Box set", "Indiana Jones 4-Movie Collection (2021) \u00b7 disc 5 of 5") in dd_app.detail_rows(rep)
    film = dict(rep, extras_disc=False, boxset=dict(bs, disc="Indiana Jones and the Raiders of the Lost Ark", number=1))
    assert dd_app.display_title(film) == "Indiana Jones and the Raiders of the Lost Ark (1981)"
    assert ("Box set", "Indiana Jones 4-Movie Collection (2021) \u00b7 disc 1 of 5") in dd_app.detail_rows(film)
    art = {"source": "TheDiscDB", "what": "box set cover", "ref": "TheDiscDB \u00b7 Indiana Jones 4-Movie Collection"}
    assert dd_app.picture_source(rep, art) == "SOURCE: TheDiscDB \u00b7 Indiana Jones 4-Movie Collection"


def test_choosing_no_titles_rips_nothing():
    """Every title unticked in Choose titles: the plan is empty (not the longest title as a
    fallback), and the summary says so."""
    rep = {"content_hash": "0" * 32, "format": "DVD", "kind": "movie", "candidates": [], "best_guess": "Film",
           "titles": [{"id": "Title 1", "role": "feature", "duration": 5400, "video": ""},
                      {"id": "Title 2", "role": "extra", "duration": 600, "video": ""}]}
    mk = dd_app.preview_titles(rep)
    assert dd_app.plan_rip(rep, mk, False, set()) == []
    assert dd_app.describe_plan([], True) == "No titles chosen to rip"
    assert len(dd_app.plan_rip(rep, mk, False)) == 1                 # the default still picks the film


def _png(path, w=40, h=60):
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"".join(bytes((x * 6 % 256, y * 4 % 256, 90)) for x in range(w)) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def test_ask_an_ai_about_the_discs_picture(gui, tmp_path, monkeypatch):
    """No key needed: the disc's pictures, a question to copy, and the AI's answer looked up."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    pics = [{"image": _png(str(tmp_path / "a.png")), "full": _png(str(tmp_path / "a_full.png"), 80, 120)},
            {"image": _png(str(tmp_path / "b.png")), "full": _png(str(tmp_path / "b_full.png"), 80, 120)}]
    app.rep = {"content_hash": "0" * 32, "candidates": [], "best_guess": None, "kind": "movie", "format": "DVD",
               "label": "DVD_VIDEO", "mastered": "2001-05", "facts": {"feature_minutes": 93},
               "label_parsed": {"raw": "DVD_VIDEO", "query": "", "placeholder": True}, "disc_images": pics}
    app.match_menu()
    m = app.match_popup
    labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) == "command"]
    assert "Ask an AI about the disc's picture…" in labels
    d = dd_app.AskAIDialog(root, app)
    assert "93 minutes" in d.question and "DVD_VIDEO" not in d.question     # a placeholder label is no clue
    assert d.count.cget("text") == "1 of 2"
    d.show(1)
    assert d.count.cget("text") == "2 of 2" and d.current() is pics[1]
    d.copy_question()
    assert root.clipboard_get() == d.question
    opened = []
    monkeypatch.setattr(dd_app.webbrowser, "open", lambda u: opened.append(u))
    d.open_site("ChatGPT", dd_app.AI_SITES[0][1])
    assert opened[-1].startswith("https://chatgpt.com/?q=This%20picture")       # the question, filled in
    # with a Gemini key in Settings, it can be asked from here
    app.settings["gemini_key"] = "k"
    monkeypatch.setattr(core, "gemini_ask", lambda key, q, image=None, **kw:
                        ('{"title": "The Swan Princess", "year": 1994, "kind": "film"}', "m"))
    d2 = dd_app.AskAIDialog(root, app)
    d2.ask_gemini()
    assert pump(lambda: d2.answer.get() == "The Swan Princess (1994)", 5)
    d2.top.destroy()
    seen = {}
    monkeypatch.setattr(dd_app.MatchDialog, "__init__", lambda self, *a, **k: seen.update(k))
    d.answer.set("**The Swan Princess (1994)**")
    d.look_up()
    assert seen["title"] == "The Swan Princess" and seen["year"] == 1994


def test_the_bundled_fonts_and_disc_facts(gui):
    """The app's own typefaces load for the app (where the system lets a program add fonts),
    and the toolbar says what the disc is from its size."""
    from disc_detective import theme
    if os.name == "nt" or theme.ctypes_fontconfig_available():
        assert theme.SERIF == "Detective Serif" and theme.MONO == "Courier Prime" and theme.SANS == "Inter"
    assert core.medium_text({"format": "DVD", "content_bytes": 7_300_000_000}) == ("DVD", "Dual Layer", "7.30 GB")
    assert core.medium_text({"format": "Blu-ray", "content_bytes": 46_200_000_000,
                             "titles": [{"video_info": {"height": 2160}}]})[:2] == ("4K UHD Blu-ray", "Dual Layer")
    assert dd_app.split_title("Psych (2006)  ·  Season 1") == ("Psych", "(2006)  ·  Season 1")
    assert dd_app.audio_summary({"titles": [{"id": "Title 1", "audio": [
        {"lang": "en", "codec": "AC3", "channels": 6}, {"lang": "en", "codec": "AC3", "channels": 2, "kind": "commentary"}]}]},
        [{"title_id": "Title 1"}]) == "English (AC3 5.1)  •  English commentary (AC3 2.0)"


def test_themed_menu_shows_and_picks(gui, monkeypatch):
    """The app's own drop-down: shown in its colours, a click on an entry runs it and closes it."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.ThemedMenu, "headless", False)
    picked, var = [], tk.StringVar(value="b")
    m = dd_app.ThemedMenu(root)
    m.add_command(label="Header:", state="disabled")
    m.add_radiobutton(label="A", value="a", variable=var)
    m.add_radiobutton(label="B", value="b", variable=var)
    m.add_separator()
    m.add_command(label="Do it", command=lambda: picked.append(1))
    assert m.index("end") == 4 and m.type(3) == "separator" and m.entrycget(4, "label") == "Do it"
    m.tk_popup(10, 10)
    root.update()
    assert m.top is not None and m.top.winfo_ismapped()
    m.invoke(0)                                       # a header does nothing
    assert m.top is not None and not picked
    m.invoke(1)
    assert var.get() == "a" and m.top is None         # chosen, and closed
    m.tk_popup(10, 10)
    m.invoke(4)
    assert picked == [1] and m.top is None
    m.delete(0, "end")
    assert m.index("end") is None
