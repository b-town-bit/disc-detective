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
    assert app.standing_full == "Saved to your case files"
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
    ok = [w for f in top.winfo_children() for w in f.winfo_children()
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
    # scrolled to the end, then rows taken away (Show more unticked): back within the rows left,
    # not blank past their end
    long = dd_app.ScrollArea(root)
    rows = [dd_app.ttk.Label(long.inner, text="row %d" % i) for i in range(400)]
    for w in rows:
        w.pack()
    long.pack()
    root.update()
    long.canvas.yview_moveto(1.0)
    root.update()
    for w in rows[200:]:
        w.destroy()
    long.fit()                                  # (as the lists do after a refill)
    root.update()
    assert long.scrolls()
    # (the last rows in view: within a scroll step of the end)
    assert 0 < long.canvas.canvasy(0) <= long.inner.winfo_reqheight() - long.max_h + 20



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
    assert str(app.rip_btn.arrow.cget("state")) == "normal"          # Titles still opens
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
    assert dd_app.picture_source(rep, art) == "VERIFIED TheDiscDB ASSET Indiana Jones 4-Movie Collection"
    # the line under the picture: where it's from, and the film's id there
    assert dd_app.picture_source(rep, {"source": "TMDB", "ref": "TMDb ID #11943"}) == "VERIFIED TMDb ASSET #11943"
    assert dd_app.picture_source(rep, {"source": "TheDiscDB", "what": "poster"}) == "VERIFIED TheDiscDB ASSET"
    assert dd_app.picture_source(rep, {"source": "disc"}) == "VERIFIED Disc Art ASSET"


def test_choosing_no_titles_rips_nothing():
    """Every title unticked in Titles: the plan is empty (not the longest title as a
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


def test_help_thediscdb_lists_your_discs(gui, tmp_path, monkeypatch):
    """Settings > Help TheDiscDB: the library's discs TheDiscDB is missing, in a list to pick from."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    db = str(tmp_path / "lib.sqlite")
    con = core.library_open(db)
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'DVD', 'ARSENIC', 'probable: x', 'Arsenic and Old Lace (1944)', "
                    "'Arsenic and Old Lace (1944)', '2026', '2026')", ("9B9067B7" + "0" * 24,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, 'Title 1', 'feature', 7080)",
                    ("9B9067B7" + "0" * 24,))
    con.close()
    orig = app.scan_args

    def args():
        a = orig()
        a.db, a.discdb_dir, a.no_discdb_online = db, str(tmp_path / "none"), True
        return a
    monkeypatch.setattr(app, "scan_args", args)
    d = dd_app.HelpDiscDBDialog(root, app)
    assert pump(lambda: "Up to date" in d.status.cget("text"), 20)
    tv = d.lists["submit"]["tv"]
    assert [tv.item(i)["values"][0] for i in tv.get_children()] == ["Arsenic and Old Lace (1944)"]
    assert d.tabs.tab(0, "text") == "NOT IN THEDISCDB (1)"
    tv.selection_set(tv.get_children()[0])
    root.update()
    assert str(d.packet_btn.cget("state")) == "normal" and "Confirmed by you" in d.lists["submit"]["note"].cget("text")
    d.top.destroy()


def test_label_titles_saves_your_labels(gui, tmp_path, monkeypatch):
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    db = str(tmp_path / "lib.sqlite")
    h = "L" * 32
    con = core.library_open(db)
    with con:
        con.execute("INSERT INTO discs (hash, format, label, first_seen, last_seen) VALUES (?, 'DVD', 'X', '2026', '2026')", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration, tracks) VALUES (?, 'Title 4', 'extra', 420, ?)",
                    (h, '{"video":{"aspect":"16:9"},"audio":[{"lang":"en","codec":"AC3","channels":6}],"subs":[]}'))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, 'Title 9', 'junk', 5)", (h,))
    con.close()
    orig = app.scan_args

    def args():
        a = orig()
        a.db, a.discdb_dir = db, str(tmp_path / "none")
        return a
    monkeypatch.setattr(app, "scan_args", args)
    saved = []
    d = dd_app.TitlesDialog(root, app, h, "A Disc", on_save=lambda: saved.append(1))
    assert list(d.vars) == ["Title 4"]                          # logos and menus aren't listed
    assert d.state["Title 4"]["type"] == "Extra" and not d.changed("Title 4")     # our guess: not saved as it is
    assert str(d.confirm_btn.cget("state")) == "disabled"      # nothing found by the menus to confirm
    assert not d.episode_boxes["Title 4"][0].winfo_manager()    # no season and number for an extra
    # the wheel over a dropdown scrolls the list: it doesn't change the type (nor make it yours)
    kind = [w for w in d.area.inner.winfo_children() if w.winfo_class() == "TCombobox"][0]
    for delta in (-120, 120, -120):
        kind.event_generate("<MouseWheel>", delta=delta)
    root.update()
    assert d.state["Title 4"]["type"] == "Extra" and not d.changed("Title 4")
    d.vars["Title 4"]["type"].set("Featurette")
    d.vars["Title 4"]["title"].set("Making Of")
    assert d.changed("Title 4")                                 # changed: it'll be saved
    d.save()
    assert saved and core.get_labels(core.library_open(db), h)["Title 4"]["title"] == "Making Of"
    # a saved title cleared: its type and name emptied
    d = dd_app.TitlesDialog(root, app, h, "A Disc")
    assert d.state["Title 4"]["type"] == "Featurette"
    d.vars["Title 4"]["type"].set("")
    d.vars["Title 4"]["title"].set("")
    d.save()
    assert core.get_labels(core.library_open(db), h) == {}
    assert dd_app.tracks_text('{"video":{"aspect":"16:9"},"audio":[{"lang":"en","codec":"AC3","channels":6}],'
                              '"subs":[{"lang":"en"}]}') == "16:9  ·  English AC3 5.1  ·  1 subtitle"


def test_an_episodes_name_comes_from_its_season_and_number(gui, tmp_path, monkeypatch):
    """Titles: give an episode its season and number and the name is filled in (from
    TVmaze or IMDb); a name you type yourself stays."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    db = str(tmp_path / "lib.sqlite")
    h = "P" * 32
    con = core.library_open(db)
    with con:
        con.execute("INSERT INTO discs (hash, format, label, kind, imdb_id, truth, first_seen, last_seen) VALUES "
                    "(?, 'DVD', 'PSYCH', 'tv', 'tt0491738', 'Psych (2006) S1 D2', '2026', '2026')", (h,))
        for n in (1, 2):
            con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, ?, 'episode', 2600)",
                        (h, "Title %d" % n))
    con.close()
    orig = app.scan_args

    def args():
        a = orig()
        a.db, a.discdb_dir = db, str(tmp_path / "none")
        return a
    monkeypatch.setattr(app, "scan_args", args)
    asked = []
    monkeypatch.setattr(core, "episode_name", lambda series, s, e, order="aired", **kw: asked.append(series) or
                        {("1", "3"): "Speak Now or Forever Hold Your Piece"}.get((str(s), str(e))))
    d = dd_app.TitlesDialog(root, app, h, "Psych")
    v = d.vars["Title 1"]
    assert d.episode_boxes["Title 1"][0].winfo_manager()        # an episode: its season and number
    v["season"].set("1")
    v["episode"].set("3")
    d.fill_episode("Title 1")
    assert pump(lambda: v["title"].get() == "Speak Now or Forever Hold Your Piece", 5)
    assert v["type"].get() == "Episode" and asked[0] == "tt0491738"
    w = d.vars["Title 2"]
    w["title"].set("My Own Name")
    w["season"].set("1")
    w["episode"].set("3")
    d.fill_episode("Title 2")
    root.update()
    assert w["title"].get() == "My Own Name"                     # yours stays
    w["title"].set("")                                         # a deleted scene of the same episode
    w["type"].set("DeletedScene")
    d.fill_episode("Title 2")
    assert pump(lambda: w["title"].get() == "Speak Now or Forever Hold Your Piece Deleted Scene", 5)
    d.save()
    labels = core.get_labels(core.library_open(db), h)
    assert labels["Title 1"] == {"type": "Episode", "title": "Speak Now or Forever Hold Your Piece", "season": 1, "episode": 3}
    assert labels["Title 2"]["type"] == "DeletedScene"


def test_your_labels_shape_the_rip_plan():
    """Psych S1 D1: you labelled the international pilot an Extra and a deleted scene, so they
    rip as extras under your names; an unlabelled episode keeps the app's own name."""
    rep = {"content_hash": "1" * 32, "format": "DVD", "kind": "tv", "best_guess": "Psych (2006) S1 D1",
           "candidates": [{"kind": "tv", "name": "Psych", "year": 2006, "imdb_id": "tt0491738", "score": 90}],
           "titles": [{"id": "Title 1", "role": "episode", "duration": 3965, "episode_index": 1},
                      {"id": "Title 2", "role": "episode", "duration": 3965, "episode_index": 2},
                      {"id": "Title 4", "role": "extra", "duration": 461}],
           "plex_names": ["Title 1 -> Psych (2006) {imdb-tt0491738}/Season 01/Psych - s01e01 - Pilot.mkv",
                          "Title 2 -> Psych (2006) {imdb-tt0491738}/Season 01/Psych - s01e02 - Spellingg Bee.mkv"],
           "labels": {"Title 2": {"type": "Extra", "title": "International Pilot", "season": 1, "episode": 1},
                      "Title 4": {"type": "DeletedScene", "title": "Shawn's Lies", "season": None, "episode": None}}}
    plan = {p["title_id"]: p for p in dd_app.plan_rip(rep, dd_app.preview_titles(rep), True)}
    assert plan["Title 1"]["dest"].endswith("Season 01/Psych - s01e01 - Pilot.mkv")
    assert (plan["Title 2"]["role"], plan["Title 2"]["dest"]) == ("extra", "Psych (2006) {imdb-tt0491738}/Other/International Pilot.mkv")
    assert plan["Title 4"]["dest"] == "Psych (2006) {imdb-tt0491738}/Deleted Scenes/Shawn's Lies.mkv"
    no_extras = dd_app.plan_rip(rep, dd_app.preview_titles(rep), False)
    assert [p["title_id"] for p in no_extras] == ["Title 1"]       # your Extra isn't ripped as an episode
    rep["labels"] = {"Title 4": {"type": "Episode", "title": "Bonus Episode", "season": 1, "episode": 9}}
    p4 = {p["title_id"]: p for p in dd_app.plan_rip(rep, dd_app.preview_titles(rep), False)}["Title 4"]
    assert p4["dest"].endswith("Season 01/Psych (2006) - s01e09 - Bonus Episode.mkv")


def test_play_alls_and_others_stay_out_of_the_rip_once_labelled():
    """Deleted Scenes: Play All labelled a DeletedScene stays a play-all (its scenes rip on their
    own); a title labelled Other (or TheDiscDB's Other: a logo, a warning) is left out like a
    promo, extras or not. Ticked by hand, each saves under its label."""
    rep = {"content_hash": "4" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)",
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 6000},
                      {"id": "Title 2", "role": "play-all", "duration": 600},
                      {"id": "Title 3", "role": "extra", "duration": 300},
                      {"id": "Title 4", "role": "extra", "duration": 300},
                      {"id": "Title 5", "role": "extra", "duration": 200},
                      {"id": "Title 6", "role": "extra", "duration": 20}],
           "labels": {"Title 2": {"type": "DeletedScene", "title": "Deleted Scenes - Play All"},
                      "Title 3": {"type": "DeletedScene", "title": "Alternate Ending"},
                      "Title 4": {"type": "DeletedScene", "title": "Extended Opening"},
                      "Title 5": {"type": "Other", "title": "Disclaimer"}},
           "discdb": {"hit": {"titles": [{"source": "06", "item": {"type": "Other", "title": "studio logo"}}]}}}
    core.discdb_promos(rep["titles"], rep["discdb"]["hit"])
    core.apply_labels(rep["titles"], rep["labels"])
    roles = {t["id"]: t["role"] for t in rep["titles"]}
    assert roles == {"Title 1": "feature", "Title 2": "play-all", "Title 3": "extra", "Title 4": "extra",
                     "Title 5": "promo", "Title 6": "promo"}
    plan = {p["title_id"]: p["dest"] for p in dd_app.plan_rip(rep, dd_app.preview_titles(rep), True)}
    assert set(plan) == {"Title 1", "Title 3", "Title 4"}
    ticked = {p["title_id"]: p["dest"] for p in dd_app.plan_rip(rep, dd_app.preview_titles(rep), True,
                                                                selected={"Title 2", "Title 5"})}
    assert ticked == {"Title 2": "Film (2001) {imdb-tt0000001}/Deleted Scenes/Deleted Scenes - Play All.mkv",
                      "Title 5": "Film (2001) {imdb-tt0000001}/Other/Disclaimer.mkv"}
    core.apply_labels(rep["titles"], {})                # cleared: the scan's reading back
    assert rep["titles"][1]["role"] == "play-all" and rep["titles"][4]["role"] == "extra"


def test_detected_titles_shape_the_rip_plan_under_your_labels():
    """Detect titles (the menus) named two extras and set the rest aside as promos; your own
    label still wins, and what TheDiscDB names stays as TheDiscDB has it."""
    from disc_detective import menus
    rep = {"content_hash": "3" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)",
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 6000},
                      {"id": "Title 5", "role": "extra", "duration": 361},
                      {"id": "Title 6", "role": "extra", "duration": 325},
                      {"id": "Title 9", "role": "extra", "duration": 52},
                      {"id": "Title 10", "role": "extra", "duration": 637},
                      {"id": "Title 11", "role": "extra", "duration": 316}],
           "labels": {"Title 11": {"type": "Interview", "title": "My Own Name"}},
           "discdb": {"hit": {"titles": [{"source": "09", "item": {"type": "Trailer", "title": "Teaser"}}]}}}
    assert dd_app.discdb_named(rep) == {"Title 9"}
    found = {"feature": 1, "extras": [{"title": 10, "name": "An Invitation to Film"},
                                      {"title": 11, "name": "Menu Name"}], "promos": []}
    rep["detected"] = menus.detected_labels(found, rep["titles"], keep=set(rep["labels"]) | dd_app.discdb_named(rep))
    core.apply_labels(rep["titles"], dd_app.all_labels(rep))
    roles = {t["id"]: t["role"] for t in rep["titles"]}
    assert roles == {"Title 1": "feature", "Title 5": "promo", "Title 6": "promo", "Title 9": "extra",
                     "Title 10": "extra", "Title 11": "extra"}
    plan = {p["title_id"]: p["dest"] for p in dd_app.plan_rip(rep, dd_app.preview_titles(rep), True)}
    assert set(plan) == {"Title 1", "Title 9", "Title 10", "Title 11"}
    assert plan["Title 10"].endswith("/Other/An Invitation to Film.mkv")
    assert plan["Title 11"].endswith("/Interviews/My Own Name.mkv")              # yours, not the menu's
    assert [p["title_id"] for p in dd_app.plan_rip(rep, dd_app.preview_titles(rep), False)] == ["Title 1"]


def test_what_the_menus_found_is_saved_once_confirmed(gui, tmp_path, monkeypatch):
    """Edit titles: a row Detect titles filled is saved only once confirmed (ticked for you
    when you give it a type or name); the table is locked while a stakeout is under way."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    app.settings["menu_detect"] = True
    rep = {"content_hash": "6" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)", "path": str(tmp_path),
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title %d" % n, "role": "extra", "duration": 300 + n} for n in (1, 2, 3, 4)] +
                     [{"id": "Title 5", "role": "promo", "duration": 120}, {"id": "Title 6", "role": "promo", "duration": 90}],
           "labels": {"Title 6": {"type": "Promo", "title": None}},
           "detected": {"Title 1": {"type": "MainMovie", "title": None}, "Title 2": {"type": "Extra", "title": "Making Of"},
                        "Title 3": {"type": "Extra", "title": None}, "Title 5": {"type": "Promo", "title": None},
                        "Title 6": {"type": "Promo", "title": None}}}
    app.rep = rep
    d = dd_app.TitlesDialog(root, app, rep["content_hash"], "Film", rep=rep)
    # an extra the menus couldn't name (Title 3) isn't found: the scan's guess, no Confirm? box
    # (Title 6, a promo you saved, isn't the menus' to offer again: it stays tucked away)
    assert sorted(d.found()) == ["Title 1", "Title 2", "Title 5"] and str(d.confirm_btn.cget("state")) == "normal"
    assert "Title 6" not in d.vars
    assert d.state["Title 3"]["from"] == "scan"
    # what's to confirm comes first, an ad too (shown though promos are tucked away): film, extras, promos
    assert list(d.vars)[:3] == ["Title 1", "Title 2", "Title 5"]
    boxes = [w for w in d.area.inner.winfo_children() if w.winfo_class() == "TCheckbutton"]
    assert len(boxes) == 3                                      # a Confirm? box only on what the menus found
    d.vars["Title 3"]["title"].set("Bloopers")                  # changed: saved, no confirming needed
    d.vars["Title 2"]["title"].set("Making Of Film")
    assert d.state["Title 2"]["ok"]                             # yours now: confirmed for you
    d.vars["Title 1"]["type"].set("")
    assert not d.state["Title 1"]["ok"]                         # emptied: not confirmed
    d.vars["Title 4"]["type"].set("DeletedScene")              # a film's deleted scene: no season or episode
    assert all(not w.winfo_manager() for w in d.episode_boxes["Title 4"])
    d.vars["Title 4"]["type"].set("Episode")
    assert all(w.winfo_manager() for w in d.episode_boxes["Title 4"])
    d.vars["Title 4"]["type"].set("Extra")
    d.vars["Title 4"]["title"].set("Trailer")                   # not the menus': saved as it's changed
    # a stakeout: hands off the table till it's back
    monkeypatch.setattr(app, "detect_titles", lambda: setattr(app, "busy", True))
    d.detect()
    entries = [w for w in d.area.inner.winfo_children() if w.winfo_class() == "TEntry"]
    assert d.locked and all(str(w.cget("state")) == "disabled" for w in entries)
    assert str(d.save_btn.cget("state")) == "disabled"
    app.busy = False
    d.detected()
    assert not d.locked and d.state["Title 3"]["title"] == "Bloopers"        # your change kept
    d.save()
    labels = core.get_labels(core.library_open(app.scan_args().db), rep["content_hash"])
    assert {k: (v["type"], v["title"]) for k, v in labels.items()} == {
        "Title 2": ("Extra", "Making Of Film"), "Title 3": ("Extra", "Bloopers"),
        "Title 4": ("Extra", "Trailer")}                        # Title 1 (the film, unconfirmed): left be


def test_a_window_is_kept_above_the_taskbar(gui, monkeypatch):
    """A window whose bottom would sit under the taskbar (the work area ends above it) is moved
    up, its title bar still on the screen; one that fits stays where it is."""
    import tkinter as tk
    root, made, pump = gui
    monkeypatch.setattr(dd_app, "work_area", lambda w: (0, 0, 1200, 600))
    top = tk.Toplevel(root)
    tk.Frame(top, width=300, height=400).pack()
    top.geometry("+100+400")
    pump(lambda: top.winfo_ismapped(), 5)
    dd_app.keep_on_screen(top)
    pump(lambda: top.winfo_rooty() + top.winfo_height() <= 600, 5)
    assert top.winfo_rooty() + top.winfo_height() <= 600 and top.winfo_rooty() >= 0
    x, y = top.winfo_rootx(), top.winfo_rooty()
    dd_app.keep_on_screen(top)                                  # fits now: not moved again
    root.update()
    assert (top.winfo_rootx(), top.winfo_rooty()) == (x, y)
    top.destroy()


def test_a_copy_is_shown_as_one(gui, tmp_path, monkeypatch):
    """Edit titles: a title the scan found the same video as another is shown as its copy, with
    nothing to fill in (labelling the other is what counts)."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    rep = {"content_hash": "9" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)", "path": str(tmp_path),
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 6000},
                      {"id": "Title 10", "role": "duplicate", "duration": 147, "note": "same video as Title 17"},
                      {"id": "Title 17", "role": "promo", "duration": 147}], "labels": {}}
    app.rep = rep
    d = dd_app.TitlesDialog(root, app, rep["content_hash"], "Film", rep=rep)
    d.show_all.set(True)
    d.refill()
    texts = [w.cget("text") for w in d.area.inner.winfo_children() if w.winfo_class() == "TLabel"]
    assert "Copy of Title 17" in texts and "Title 10" not in d.vars and "Title 17" in d.vars
    d.save()
    assert core.get_labels(core.library_open(app.scan_args().db), rep["content_hash"]) == {}
    d.close()
    # a play-all likewise: what it plays, nothing to fill in (The Iron Giant's deleted scenes)
    rep["titles"] += [{"id": "Title 15", "role": "play-all", "duration": 600, "note": "plays Title 16, Title 17, Title 18, Title 20"}]
    d = dd_app.TitlesDialog(root, app, rep["content_hash"], "Film", rep=rep)
    d.show_all.set(True)
    d.refill()
    texts = [w.cget("text") for w in d.area.inner.winfo_children() if w.winfo_class() == "TLabel"]
    assert "Play-all of Titles 16–18, 20" in texts and "Title 15" not in d.vars
    assert dd_app.play_all_text("length equals Title 3") == "Play-all of Title 3"
    d.save()
    assert core.get_labels(core.library_open(app.scan_args().db), rep["content_hash"]) == {}
    d.close()


def test_the_menu_button_that_plays_a_title(gui, tmp_path, monkeypatch):
    """Edit titles: a title a menu button plays (Detect titles saw the page) has a picture beside
    it; clicked, the page shows with the button outlined and what was read on it."""
    from PIL import Image
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    page = str(tmp_path / "page_1_3_1.png")
    Image.new("RGB", (720, 480), (20, 20, 60)).save(page)
    found = {"feature": 1, "extras": [{"title": 2, "name": "Making Of"}], "promos": [],
             "pictures": {1: {"image": page, "rect": [10, 10, 200, 40], "text": ""},
                          2: {"image": page, "rect": [10, 50, 200, 80], "text": "Making Of"},
                          3: {"image": str(tmp_path / "gone.png"), "rect": [0, 0, 1, 1], "text": ""}}}
    rep = {"content_hash": "8" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)", "path": str(tmp_path),
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title %d" % n, "role": "extra", "duration": 300 + n} for n in (1, 2, 3)],
           "labels": {}, "menu_found": found}
    app.rep = rep
    app.edit_titles()
    d = app.titles_dialog
    assert sorted(d.pictures) == ["Title 1", "Title 2"]         # (a picture no longer there: none)
    d.show_picture("Title 2")
    v = d.viewer
    assert v.alive() and v.head.cget("text") == "Title 2 plays from this button"
    assert "Making Of" in v.read.cget("text") and v.photo is not None
    d.show_picture("Title 1")
    assert d.viewer is v and "couldn't read" in v.read.cget("text")      # one window, reused
    # the guide to the types (TYPE's help): every type in the list, one window
    assert {t for t, _, _ in dd_app.TypeGuide.TYPES} == set(core.LABEL_TYPES)
    d.heads["TYPE"].event_generate("<Button-1>")
    guide = d.guide
    assert guide.alive()
    d.show_types()
    assert d.guide is guide
    d.close()
    assert not v.alive() and not guide.alive()


def test_redrawing_the_details_keeps_the_layout(gui, monkeypatch):
    """The details card, redrawn (Detect titles' answer relabels the disc), stays above the
    status strip that sits just over the rip card: nothing on the desk moves about."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    rep = {"content_hash": "7" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)",
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 6000}], "facts": {}}
    app.scan_done(rep, None)
    app.set_status("Still a mystery: Choose the match to say what it is")
    row = lambda: app.rip_card.master.pack_slaves()
    before = row()
    assert before.index(app.details_card) < before.index(app.status_strip) < before.index(app.rip_card)
    shown = app.details.winfo_children()
    app.relabel()
    assert row() == before
    assert app.details.winfo_children() == shown                # nothing on it changed: not redrawn
    rep["label"] = "ANOTHER_DISC"
    app.show_details(rep)                                       # changed: redrawn, in the same place
    assert row() == before and app.details.winfo_children() != shown
    assert any(str(w.cget("text")) == "ANOTHER_DISC" for w in app.details.winfo_children() if w.winfo_class() == "TLabel")


def test_detect_titles_button(gui, tmp_path, monkeypatch):
    """Detect titles (Edit titles) shows only when switched on, for a film DVD; its news and
    answer show there, the answer reshapes the plan, and an answer of nothing changes nothing."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    rep = {"content_hash": "4" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)", "path": str(tmp_path),
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 6000},
                      {"id": "Title 2", "role": "extra", "duration": 300},
                      {"id": "Title 3", "role": "extra", "duration": 200}]}
    app.rep = rep
    menu = app.rip_btn.menu
    labels = lambda: [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                      if menu.type(i) != "separator"]
    app.settings["menu_detect"] = False
    app._build_rip_menu()
    assert not app.can_detect() and labels()[labels().index("Choose titles…") + 1] == "Edit titles…"
    assert "Detect titles" not in labels()                     # it's in Edit titles
    d = dd_app.TitlesDialog(root, app, rep["content_hash"], "Film", rep=rep)
    assert d.detect_btn is None                                 # switched off: not offered
    d.top.destroy()
    app.show_plan_summary()
    assert not app.vlc_lbl.winfo_manager()                     # VLC means nothing to a rip without it
    app.settings["menu_detect"] = True
    app._build_rip_menu()
    app.show_plan_summary()
    assert app.vlc_lbl.winfo_manager() and app.vlc_lbl.cget("text") in ("VLC linked", "VLC not found")
    assert app.engine_lbl.cget("text") in ("MakeMKV linked", "MakeMKV not found")
    assert app.can_detect()
    d = dd_app.TitlesDialog(root, app, rep["content_hash"], "Film", rep=rep)
    assert d.detect_btn is not None and d.state["Title 2"]["from"] == "scan"
    assert d.detect_btn.cget("text") == "Detect titles"
    app.edit_titles()
    assert app.titles_dialog is d                               # one Edit titles at a time
    assert not app.can_detect.__func__(type("A", (), {"settings": app.settings, "rep": dict(rep, kind="tv")})())
    # while it works, its news shows in Edit titles, not the main window
    app.show_plan_summary()
    app.detecting = True
    app.set_status("Interrogating suspect…")
    assert d.news_lbl.cget("text") == "Interrogating suspect…" and app.status.cget("text") != "Interrogating suspect…"
    app.detect_done(rep, {"feature": None, "extras": [], "promos": []}, None)
    assert "detected" not in rep and d.news_lbl.cget("text") == "The stakeout turned up nothing: titles unchanged"
    app.detect_done(rep, {"feature": 1, "extras": [{"title": 3, "name": "Making Of"}], "promos": []}, None)
    assert {t["id"]: t["role"] for t in rep["titles"]} == {"Title 1": "feature", "Title 2": "promo", "Title 3": "extra"}
    assert d.news_lbl.cget("text") == "The suspects named the film and 1 extra"
    assert d.news_dot.itemcget(1, "fill") == dd_app.th.GREEN_DOT
    # the rows not yours take the menus' word (an ad now: shown, at the top, to confirm)
    assert pump(lambda: d.state["Title 2"]["from"] == "menus", 5)
    assert d.state["Title 2"]["type"] == "Promo" and d.state["Title 3"]["title"] == "Making Of"
    assert list(d.vars)[:2] == ["Title 1", "Title 3"] and "Title 2" in d.vars and not d.state["Title 3"]["ok"]
    d.close()
    # with Edit titles closed, the outcome goes to the footer, and the strip folds away
    app.show_plan_summary()
    app.set_status("Interrogating suspect…")
    order = app.rip_card.master.pack_slaves()
    assert order.index(app.status_strip) == order.index(app.rip_card) - 1
    app.detect_done(rep, {"feature": 1, "extras": [{"title": 3, "name": "Making Of"}], "promos": []}, None)
    assert app.standing_full == "The suspects named the film and 1 extra"
    assert app.case_dot.itemcget(1, "fill") == dd_app.th.GREEN_DOT
    assert not app.status_strip.winfo_manager()
    # the same disc scanned again (an answer saved: Enchanted): what the menus said still holds;
    # another disc starts clean
    fresh = lambda h: dict(rep, content_hash=h, titles=[dict(t, role=r) for t, r in
                                                        zip(rep["titles"], ("feature", "extra", "extra"))])
    again = {k: v for k, v in fresh(rep["content_hash"]).items() if k not in ("detected", "menu_found")}
    app.scan_done(again, None)
    assert again["detected"]["Title 3"]["type"] == "Extra" and again["detected"]["Title 2"]["type"] == "Promo"
    assert {t["id"]: t["role"] for t in again["titles"]} == {"Title 1": "feature", "Title 2": "promo", "Title 3": "extra"}
    other = {k: v for k, v in fresh("5" * 32).items() if k not in ("detected", "menu_found")}
    app.scan_done(other, None)
    assert "detected" not in other
    # too long for the room the footer's links leave: an ellipsis, and the whole on hover
    app.set_outcome("The suspects named the film and 3 extras " + "and a great deal more besides " * 8, dd_app.th.GREEN_DOT)
    root.update()
    shown = app.standing_lbl.cget("text")
    assert shown.endswith("…") and len(shown) < len(app.standing_full)
    assert app.standing_lbl.winfo_x() + app.standing_lbl.winfo_width() <= app.standing_lbl.master.winfo_width()


def test_suspect_report_and_a_breathing_light(gui, monkeypatch):
    """Detect titles' answer counts every extra (a film's own trailer too) and its suspects;
    the status light breathes while something's under way, and stops when it's done."""
    line = dd_app.suspect_line
    assert line("Questioning", 1, 1) == "Questioning suspect…"
    assert line("Questioning", 3, 3) == "Questioning suspect 3…"                # caught up: no "of 3"
    assert line("Interrogating", 2, 5) == "Interrogating suspect 2 of 5…"
    report = dd_app.suspect_report
    assert report({"feature": 1, "extras": [{}, {}, {}], "suspects": 4}) == "The suspects named the film and 3 extras"
    assert report({"feature": 1, "extras": [], "suspects": 1}) == "The suspect named the film"
    assert report({"feature": None, "extras": [{}], "suspects": 1}) == "The suspect named 1 extra"
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    app.show_progress(True, indeterminate=True)
    app.set_status("Interrogating suspect 2 of 4…")
    assert app.status_dot._pulse                               # breathing
    app.show_progress(False)
    app.set_status("The suspects named the film and 3 extras")
    assert not app.status_dot._pulse                           # done: a steady green
    assert app.status_dot.itemcget(1, "fill") == dd_app.th.GREEN_DOT


def test_makemkv_reads_a_confirmed_disc_ahead(gui, tmp_path, monkeypatch):
    """With MakeMKV linked, a disc you confirm has its title list read in the background and
    saved (for the rip, and TheDiscDB packets without the disc); once saved, never again.
    MakeMKV opens only that drive (--noscan, dev:E:). A rip waits for the lab; a stakeout or a
    new scan has it step aside, and starts once MakeMKV has quit and the drive has settled."""
    import threading
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    app.source.set("E:")
    store = lambda h: str(tmp_path / "makemkv_logs" / ("%s.txt" % h))
    monkeypatch.setattr(dd_app.di, "makemkv_log_store", store)
    monkeypatch.setattr(app, "makemkv_exe", lambda: "makemkvcon64.exe")
    release = threading.Event()
    runs = []

    class FakeMakeMKV:                        # 'makemkvcon -r info': one 1:45:56 title
        def __init__(self, cmd, **kw):
            runs.append(cmd)

        def communicate(self):
            release.wait(5)
            return ('CINFO:2,0,"FATHER_OF_THE_BRIDE_2"\nTINFO:0,9,0,"1:45:56"\nTINFO:0,24,0,"1"\n', None)

        def poll(self):
            return None

        def terminate(self):
            release.set()
    monkeypatch.setattr(dd_app.subprocess, "Popen", FakeMakeMKV)

    def settle(job):
        assert job["done"].wait(5)
        assert pump(lambda: app.mkv_scan is not job, 5)
    rep = app.rep = {"content_hash": "6" * 32, "format": "DVD"}
    app.prescan(rep)
    job = app.mkv_scan
    assert job and pump(lambda: runs, 5)
    assert runs[-1][1:] == ["-r", "--noscan", "--cache=1", "info", "dev:E:"]      # this drive only
    # the rip card's lab light: it breathes while the lab works, then says it's done
    assert app.bg_box.winfo_manager() and app.bg_lbl.cget("text") == "The lab is examining the disc…"
    assert app.bg_dot._pulse
    # a rip waits for the lab's reading (it needs it)
    waited = threading.Event()
    threading.Thread(target=lambda: (app.wait_for_lab(job), waited.set()), daemon=True).start()
    assert not waited.wait(0.3)
    release.set()
    assert waited.wait(5)
    settle(job)
    assert app.mkv_scan is None and "1:45:56" in open(store(rep["content_hash"])).read()
    assert app.bg_lbl.cget("text") == "Lab report filed" and not app.bg_dot._pulse
    assert app.bg_dot.itemcget(1, "fill") == dd_app.th.GREEN_DOT
    app.prescan(rep)                                           # read already: not again
    assert app.mkv_scan is None and len(runs) == 1 and app.bg_lbl.cget("text") == "Lab report on file"
    known = {"content_hash": "8" * 32, "format": "DVD", "discdb": {"hit": {"titles": []}}}
    app.prescan(known)                                         # TheDiscDB has it: no packet needed, no read
    assert app.mkv_scan is None and len(runs) == 1
    release.clear()
    other = app.rep = {"content_hash": "7" * 32, "format": "DVD"}
    app.prescan(other)
    job = app.mkv_scan
    assert pump(lambda: job["proc"], 5)
    # a stakeout (or a new scan) has the lab step aside: MakeMKV quits, the drive settles, then
    # the drive's theirs; a stopped reading saves nothing
    monkeypatch.setattr(app, "DRIVE_SETTLE", 0.2)
    stopped = app.stop_prescan("The lab waits out the stakeout…")
    assert stopped is job and app.mkv_scan is None
    assert app.bg_lbl.cget("text") == "The lab waits out the stakeout…" and not app.bg_dot._pulse
    t = time.time()
    app.wait_for_lab(stopped)
    assert job["done"].is_set() and time.time() - t >= 0.2
    pump(lambda: True, 0.2)
    assert not os.path.exists(store(other["content_hash"]))
    assert app.bg_lbl.cget("text") == "The lab waits out the stakeout…"      # not "couldn't read": stopped
    assert app.stop_prescan() is None
    assert dd_app.makemkv_source("E:\\") == "dev:E:" and dd_app.makemkv_source(str(tmp_path)).startswith("file:")


def test_open_makemkv_beside_the_rip_folder(gui, tmp_path, monkeypatch):
    """Rip ▾ > Open MakeMKV, under Open rip folder, when MakeMKV's window is there; greyed out
    while the drive's in use (its window reads the disc as it opens)."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    (tmp_path / "makemkvcon64.exe").write_bytes(b"")
    (tmp_path / "makemkv.exe").write_bytes(b"")
    monkeypatch.setattr(app, "makemkv_exe", lambda: str(tmp_path / "makemkvcon64.exe"))
    assert app.makemkv_app() == str(tmp_path / "makemkv.exe")
    menu = app.rip_btn.menu
    entries = lambda: {menu.entrycget(i, "label"): menu.entrycget(i, "state")
                       for i in range(menu.index("end") + 1) if menu.type(i) != "separator"}
    app._build_rip_menu()
    labels = list(entries())
    assert labels[labels.index("Open rip folder") + 1] == "Open MakeMKV" and entries()["Open MakeMKV"] == "normal"
    app.mkv_scan = {"hash": "x"}                               # the lab has the drive
    app._build_rip_menu()
    assert entries()["Open MakeMKV"] == "disabled"
    app.mkv_scan = None
    monkeypatch.setattr(app, "makemkv_exe", lambda: None)      # no MakeMKV: no entry
    app._build_rip_menu()
    assert "Open MakeMKV" not in entries()


def test_rip_outcome_goes_to_the_footer(gui, tmp_path, monkeypatch):
    """A rip's light breathes while it rips; its outcome lands in the footer (green, amber or
    red) and the status strip folds away."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    errors = []
    monkeypatch.setattr(dd_app.messagebox, "showerror", lambda *a: errors.append(a))
    app = dd_app.App(root)
    app.rep = {"content_hash": "5" * 32}
    app.show_progress(True)
    dd_app.th.pulse_dot(app.rip_dot, dd_app.th.BRASS_600, dd_app.th.CARD)
    app.set_status("Bagging the evidence, 1 of 3: Film.mkv")
    files = [str(tmp_path / ("%d.mkv" % i)) for i in range(3)]
    app.rip_done(files, 3, None)
    assert app.standing_full == "Evidence bagged: 3 of 3 titles ripped"
    assert app.case_dot.itemcget(1, "fill") == dd_app.th.GREEN_DOT
    assert not app.status_strip.winfo_manager() and not app.rip_dot._pulse
    app.rip_done(files[:2], 3, None)
    assert app.case_dot.itemcget(1, "fill") == dd_app.th.AMBER_DOT        # one didn't make it
    app.cancelled = True
    app.rip_done(files[:1], 3, None)
    assert app.standing_full == "Rip called off after 1 of 3 titles"
    app.cancelled = False
    app.rip_done([], 0, "RuntimeError: MakeMKV found no titles")
    assert app.case_dot.itemcget(1, "fill") == dd_app.th.WAX and errors


def test_promos_and_repeats_stay_out_of_the_way(gui, monkeypatch):
    """A promo isn't ripped, even with extras on; Titles hides duplicates, play-alls and
    promos until you ask to see them, and keeps ticks you made on hidden rows."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    rep = {"content_hash": "2" * 32, "format": "DVD", "kind": "movie", "best_guess": "Film (2001)",
           "candidates": [{"kind": "movie", "name": "Film", "year": 2001, "imdb_id": "tt0000001", "score": 90}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 6000},
                      {"id": "Title 2", "role": "duplicate", "duration": 6000, "note": "same video as Title 1"},
                      {"id": "Title 3", "role": "extra", "duration": 300},
                      {"id": "Title 4", "role": "extra", "duration": 200}],
           "labels": {"Title 4": {"type": "Promo", "title": "Coming Soon to DVD"}}}
    core.apply_labels(rep["titles"], rep["labels"])
    assert [p["title_id"] for p in dd_app.plan_rip(rep, dd_app.preview_titles(rep), True)] == ["Title 1", "Title 3"]
    app.rep = rep
    picker = dd_app.TitlePicker(root, app)
    root.update()
    assert set(picker.vars) == {"Title 1", "Title 3"}
    picker.show_all.set(True)
    picker.refill()
    assert set(picker.vars) == {"Title 1", "Title 2", "Title 3", "Title 4"}
    picker.vars["Title 4"].set(True)
    picker.show_all.set(False)
    picker.refill()
    assert "Title 4" in picker.vars                                 # ticked, so still shown
    assert picker.what(rep["titles"][3]) == "Promo: Coming Soon to DVD"    # what it is, by your label
    assert picker.what(rep["titles"][1]) == "Copy of Title 1"
    picker.ok()
    assert app.selected == {"Title 1", "Title 4"}


def test_choose_titles_names_titles_as_edit_titles_does(gui, monkeypatch):
    """The Iron Giant: both cuts named by the menus, a play-all by what it plays, and the
    deleted scenes by type and name, the same words as Edit titles'."""
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    rep = {"content_hash": "4" * 32, "format": "DVD", "kind": "movie", "best_guess": "The Iron Giant (1999)",
           "candidates": [{"kind": "movie", "name": "The Iron Giant", "year": 1999, "imdb_id": "tt0129167", "score": 90}],
           "titles": [{"id": "Title 1", "role": "feature", "duration": 5200},
                      {"id": "Title 2", "role": "version", "duration": 5160},
                      {"id": "Title 15", "role": "play-all", "duration": 600, "note": "plays Title 16, Title 17"},
                      {"id": "Title 16", "role": "extra", "duration": 60},
                      {"id": "Title 17", "role": "extra", "duration": 60}],
           "detected": {"Title 1": {"type": "MainMovie", "title": "Signature Edition"},
                        "Title 2": {"type": "MainMovie", "title": "Original Edition"},
                        "Title 16": {"type": "DeletedScene", "title": "Campfire"}},
           "labels": {}}
    app.rep = rep
    picker = dd_app.TitlePicker(root, app)
    what = {t["id"]: picker.what(t) for t in rep["titles"]}
    assert what["Title 1"] == "Feature: Signature Edition" and what["Title 2"] == "Feature: Original Edition"
    assert what["Title 15"] == "Play-all of Titles 16–17"
    assert what["Title 16"] == "Deleted Scene: Campfire"           # (in plain words, not TheDiscDB's)
    assert what["Title 17"] == "Extra"                       # nothing named it: the scan's word
    picker.top.destroy()


def test_choose_titles_shows_episode_numbers_and_ads_from_thediscdb(gui, monkeypatch):
    root, made, pump = gui
    monkeypatch.setattr(dd_app.App, "maybe_first_run", lambda self: None)
    app = dd_app.App(root)
    rep = {"content_hash": "3" * 32, "format": "DVD", "kind": "tv", "best_guess": "Psych (2006) S1 D1",
           "candidates": [{"kind": "tv", "name": "Psych", "year": 2006, "imdb_id": "tt0491738", "score": 90}],
           "titles": [{"id": "Title 5", "role": "extra", "duration": 2600},
                      {"id": "Title 9", "role": "episode", "duration": 2600, "episode_index": 1},
                      {"id": "Title 1", "role": "episode", "duration": 2600, "episode_index": 2}],
           "plex_names": ["Title 1 -> Psych (2006) {imdb-tt0491738}/Season 01/Psych - s01e02 - The Spelling Bee.mkv",
                          "Title 9 -> Psych (2006) {imdb-tt0491738}/Season 01/Psych - s01e04 - Woman Seeking.mkv"],
           "labels": {"Title 5": {"type": "Episode", "title": "Speak Now", "season": 1, "episode": 3}}}
    core.apply_labels(rep["titles"], rep["labels"])
    app.rep = rep
    picker = dd_app.TitlePicker(root, app)
    root.update()
    texts = [w.cget("text") for w in picker.area.inner.winfo_children() if isinstance(w, dd_app.ttk.Label)]
    # their numbers in the season, and their names (as Edit titles has them)
    assert "Episode 2: The Spelling Bee" in texts and "Episode 3: Speak Now" in texts
    assert [t["id"] for t in picker.rows] == ["Title 1", "Title 5", "Title 9"]   # E2, E3 (yours), E4
    picker.names["Title 1"] = "Psych - s01e09-e10 - Two Parter.mkv"
    assert picker.episode_text({"id": "Title 1"}) == "Episode 9-10"
    # Edit titles (Rip ▾): the scan's guesses, and yours
    app.edit_titles()
    edit = app.titles_dialog
    st = edit.state
    # the scan's guesses, by the names it would save them as; yours as you left it
    assert (st["Title 1"]["season"], st["Title 1"]["episode"], st["Title 1"]["title"]) == ("1", "2", "The Spelling Bee")
    assert st["Title 1"]["from"] == "scan" and not st["Title 1"]["ok"]
    assert st["Title 5"]["from"] == "yours" and st["Title 5"]["episode"] == "3"
    assert str(edit.confirm_btn.cget("state")) == "disabled"   # no stakeout: nothing to confirm
    edit.close()
    picker.top.destroy()
    # TheDiscDB's "Trailer" that is an ad for other releases is a promo; this film's trailer isn't
    titles = [{"id": "Title 7", "role": "extra"}, {"id": "Title 8", "role": "extra"}]
    hit = {"titles": [{"source": "07", "item": {"type": "Trailer", "title": "Previews - The Smurfs"}},
                      {"source": "08", "item": {"type": "Trailer", "title": "Theatrical Trailer"}}]}
    assert [t["id"] for t in core.discdb_promos(titles, hit)] == ["Title 7"]
    assert [t["role"] for t in titles] == ["promo", "extra"]


def test_a_4k_discs_film_says_so():
    """The Dark Knight Rises 4K: its film is 2160p HDR10, so the facts say 4K and HDR10;
    a 4K disc's 1080p extras, or a Blu-ray's film, don't make it 4K."""
    rep = {"titles": [{"id": "00020.mpls", "role": "feature", "video_info": {"codec": "HEVC", "height": 2160, "hdr": "HDR10"}},
                      {"id": "00100.mpls", "role": "extra", "video_info": {"codec": "AVC", "height": 1080}}]}
    assert dd_app.picture_format(rep) == (True, "HDR10")
    rep["titles"][0]["video_info"] = {"codec": "AVC", "height": 1080}
    rep["titles"][1]["video_info"] = {"codec": "HEVC", "height": 2160}
    assert dd_app.picture_format(rep) == (False, None)
