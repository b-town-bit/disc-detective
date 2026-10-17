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
    root.destroy()


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
