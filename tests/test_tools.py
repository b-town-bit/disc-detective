"""The programs you install yourself: found where their installers put them, and installed only through their
publisher's own installer, its window shown, after you've agreed."""
import os

import pytest

from disc_detective import tools

_REAL_SAVED_PATH = tools._saved_path         # (before conftest stands in for it)


@pytest.mark.skipif(os.name != "nt", reason="winget's folders and Program Files are Windows'")
def test_which_finds_a_tool_beyond_path(tmp_path, monkeypatch):
    """Not on PATH: in winget's links (a portable tool, FFmpeg, installed this session), or where its installer
    usually puts it (Tesseract, in Program Files, never added to PATH). Full paths: the app runs it from there."""
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools, "runs", lambda name, path: True)       # (stand-in files: their running not asked)
    links = tmp_path / "Local" / "Microsoft" / "WinGet" / "Links"
    links.mkdir(parents=True)
    (links / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(tools, "_winget_links", lambda: str(links))
    pf = tmp_path / "Program Files"
    (pf / "Tesseract-OCR").mkdir(parents=True)
    (pf / "Tesseract-OCR" / "tesseract.exe").write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setattr(tools, "_USUAL", {"tesseract": [("Tesseract-OCR", "tesseract.exe")]})
    assert tools.which("ffmpeg") == str(links / "ffmpeg.exe")
    assert tools.which("tesseract") == str(pf / "Tesseract-OCR" / "tesseract.exe")
    assert tools.which("ffplay") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows' saved PATH")
def test_which_finds_a_tool_added_to_the_saved_path_since_startup(tmp_path, monkeypatch):
    """winget couldn't make a portable tool's link (no admin, no Developer Mode): it adds the tool's folder to
    your saved PATH instead (FFmpeg's bin), which the app's own PATH, from its start, doesn't show."""
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools, "runs", lambda name, path: True)
    bin_ = tmp_path / "ffmpeg-9.0.2-full_build" / "bin"
    bin_.mkdir(parents=True)
    (bin_ / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setattr(tools, "_saved_path", lambda: [str(tmp_path / "elsewhere"), str(bin_)])
    assert tools.which("ffmpeg") == str(bin_ / "ffmpeg.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows' registry")
def test_saved_path_reads_the_registry():
    """The real one, read: this PC's saved PATH has its Windows folder (the system's part)."""
    windir = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))
    assert any(os.path.normcase(d).startswith(windir) for d in _REAL_SAVED_PATH())


def test_the_install_command_is_the_publishers_installer_shown():
    """winget's own repository (not the Store), the exact package, the installer's window shown; its agreements
    passed on only because the app asked first (SettingsDialog.get_tools)."""
    for t in tools.TOOLS:
        cmd = tools.install_command(t["key"])
        assert cmd[1:5] == ["install", "--id", t["winget"], "--exact"]
        assert "--interactive" in cmd and cmd[cmd.index("--source") + 1] == "winget"
        assert "--silent" not in cmd
        assert t["page"].startswith("https://") and t["publisher"] and t["license"]


def test_install_reports_wingets_last_words(monkeypatch):
    """Its outcome, and winget's last line to show if it failed; "already installed" is as good as installed."""
    runs = []

    def run(cmd, **kw):
        runs.append(cmd)
        return type("R", (), {"returncode": runs[-1] and len(runs) - 1, "stdout": ["Successfully installed\n",
                                                                                 "Found an existing package already installed.\n"][len(runs) - 1]})()
    monkeypatch.setattr(tools.subprocess, "run", run)
    assert tools.install("vlc") == (True, "Successfully installed")
    assert tools.install("vlc") == (True, "Found an existing package already installed.")


def test_how_to_get_names_the_download_page():
    for t in tools.TOOLS:
        assert t["page"] in tools.how_to_get(t["key"])


@pytest.mark.skipif(os.name != "nt", reason="Tesseract's Program Files folder is Windows'")
def test_a_copy_that_wont_run_isnt_found(tmp_path, monkeypatch):
    """An install that stopped halfway can leave the program without its libraries (Tesseract's, "libtiff-6.dll:
    cannot open"): not found, so nothing runs it, and said to be there but broken."""
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    pf = tmp_path / "Program Files"
    (pf / "Tesseract-OCR").mkdir(parents=True)
    exe = pf / "Tesseract-OCR" / "tesseract.exe"
    exe.write_bytes(b"not a program")
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setattr(tools, "_USUAL", {"tesseract": [("Tesseract-OCR", "tesseract.exe")]})
    monkeypatch.setattr(tools, "_RUNS", {})
    assert tools.which("tesseract") is None
    assert tools.found_broken("tesseract") == str(exe)


def test_an_installer_that_stopped_says_so(monkeypatch):
    """winget's "Installer failed with exit code: 2" (an NSIS installer closed or failed partway): not installed,
    in words."""
    out = "Successfully verified installer hash\nStarting package install...\nInstaller failed with exit code: 2\n"
    monkeypatch.setattr(tools.subprocess, "run", lambda cmd, **kw: type("R", (), {"returncode": 1, "stdout": out})())
    assert tools.install("tesseract") == (False, "the installer stopped before it finished (exit code 2)")


@pytest.mark.skipif(os.name != "nt", reason="Windows' DLL search through PATH")
def test_tesseracts_folder_is_kept_off_the_apps_path(tmp_path, monkeypatch):
    """Tesseract's folder on PATH (its installer puts it there): taken off the app's own PATH at startup, so no
    library of the app's is loaded from it (Pillow's libfribidi-0.dll, which then blocks Tesseract's installer),
    and still where Tesseract is found."""
    tess = tmp_path / "Tesseract-OCR"
    tess.mkdir()
    (tess / "tesseract.exe").write_bytes(b"")
    other = tmp_path / "bin"
    other.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(other), str(tess)]))
    monkeypatch.setattr(tools, "_ON_PATH", {})
    monkeypatch.setattr(tools, "runs", lambda name, path: True)
    tools.keep_off_path()
    assert os.environ["PATH"] == str(other)
    assert tools.which("tesseract") == str(tess / "tesseract.exe")


@pytest.mark.skipif(os.name != "nt", reason="the loaded libraries of a Windows process")
def test_loaded_from_sees_this_processs_libraries():
    """What an installer can't replace while the app runs: its folder's libraries this process has loaded."""
    import _ctypes                              # (an extension module this process has loaded, from its folder)
    folder = os.path.dirname(_ctypes.__file__)
    held = tools.loaded_from(folder)
    assert any(os.path.basename(p).lower().startswith("_ctypes") for p in held)
    assert tools.loaded_from(os.path.join(folder, "no such folder")) == []
