"""The app, driven headlessly: first-run download, Settings, a scan, the rip plan."""
import os
import time

import pytest

tk = pytest.importorskip("tkinter")
from disc_detective import app as dd_app, core  # noqa: E402
import fixtures  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture
def gui(monkeypatch, tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.geometry("+0+0")
    monkeypatch.setattr(dd_app, "SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(dd_app, "optical_drives", lambda: [])
    for name in ("showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(dd_app.messagebox, name, lambda *a, **k: None)
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
    sd.update("discdb")
    assert pump(lambda: "Up to date" in sd.prog.text.cget("text"))
    sd.close()
    args = app.scan_args()
    assert args.tmdb_key == "abc123" and args.no_art and not args.no_discdb_online
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
    assert app.title_lbl.cget("text").startswith("Justified")
    assert app.plan_lbl.cget("text") == "Will rip 4 episodes (S01E01–S01E04)"
    assert app.status.cget("text").startswith("Case closed")
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
    assert app.title_lbl.cget("text").startswith("Chicken Run (2000)")
    assert app.plan_lbl.cget("text").startswith("Will rip the feature")
    assert app.match_btn.winfo_ismapped() and app.match_btn.cget("text") == "Not right? ▾"
    assert app.confirm_btn.winfo_ismapped() and not app.confirmed_lbl.winfo_ismapped()
    app.match_menu()
    labels = [app.match_popup.entrycget(i, "label") for i in range(app.match_popup.index("end") + 1)
              if app.match_popup.type(i) == "command"]
    app.match_popup.unpost()
    assert labels == ["Enter it myself…"]          # one confident match: nothing else to offer; no "yes" in here
    app.choose({"kind": "movie", "name": "Chicken Run: Dawn of the Nugget", "year": 2023})   # saves + rescans
    assert pump(lambda: app.rep is not None and not app.busy and app.rep.get("personal_hit"), 60)
    assert app.title_lbl.cget("text").startswith("Chicken Run: Dawn of the Nugget (2023)")
    assert app.confirmed_lbl.winfo_ismapped() and not app.confirm_btn.winfo_ismapped()
    app.use_answer(None)                                  # "Forget my answer"
    assert pump(lambda: app.rep is not None and not app.busy and not app.rep.get("personal_hit"), 60)
    assert app.title_lbl.cget("text").startswith("Chicken Run (2000)")
    app.confirm()                                         # ✓ That's right
    assert pump(lambda: app.rep is not None and not app.busy and app.rep.get("personal_hit"), 60)
    assert app.rep["personal_hit"]["truth"].startswith("Chicken Run (2000) {imdb-tt")
    assert app.confirmed_lbl.winfo_ismapped() and app.title_lbl.cget("text") == "Chicken Run (2000)"


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
    assert app.title_lbl.cget("text") == "Wallace & Gromit Collection"
    assert app.plan_lbl.cget("text") == "Will rip 3 films"
    assert app.confirm_btn.winfo_ismapped()                         # collections can be confirmed now
    app.match_menu()
    labels = [app.match_popup.entrycget(i, "label") for i in range(app.match_popup.index("end") + 1)
              if app.match_popup.type(i) == "command"]
    app.match_popup.unpost()
    assert "Change the films…" in labels and "It's one film or TV show instead…" in labels
    app.confirm()                                                   # ✓ That's right
    app.rep = None
    wait(lambda: app.rep.get("personal_hit"))
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
    assert app.title_lbl.cget("text") == "Wallace & Gromit: Three Amazing Adventures"
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
