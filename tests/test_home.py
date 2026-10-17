"""Where things live, and moving data over from the old name (disc_identify)."""
import os

from disc_detective import core


def test_old_folder_moves_over(tmp_path, monkeypatch):
    user = tmp_path / "user2"
    old = user / ".disc_identify"
    (old / "imdb").mkdir(parents=True)
    (old / "library.sqlite").write_bytes(b"lib")
    (old / "disc_ripper.json").write_text('{"tmdb_key": "abc"}')
    monkeypatch.delenv("DISC_DETECTIVE_HOME")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setattr(core, "LEGACY_HOME", str(old))
    home = core.data_home()
    assert home == os.path.join(str(user), ".disc_detective")
    assert not old.exists()
    assert open(os.path.join(home, "library.sqlite"), "rb").read() == b"lib"
    assert os.path.exists(os.path.join(home, "settings.json"))
    assert core.default_library() == os.path.join(home, "library.sqlite")


def test_old_environment_names_still_work(monkeypatch, tmp_path):
    monkeypatch.setenv("DISC_IDENTIFY_DB", str(tmp_path / "x.sqlite"))
    assert core.default_library() == str(tmp_path / "x.sqlite")
    monkeypatch.setenv("DISC_DETECTIVE_DB", str(tmp_path / "y.sqlite"))
    assert core.default_library() == str(tmp_path / "y.sqlite")
