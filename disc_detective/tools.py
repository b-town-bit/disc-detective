"""The programs Disc Detective works with that you install yourself: what each is for, where it's found, and
how to get it. On Windows, with your consent, its publisher's own installer is launched through winget
(Microsoft's package manager): the installer's window shows, with its own licence to agree to. Elsewhere, its
download page. Nothing is bundled, and nothing but a publisher's own installer is ever fetched (no keys, no
decryption libraries: see the decryption rule in CONTRIBUTING)."""
import os
import shutil
import subprocess
import sys

# each tool: what it's for (a few words, after its name), its winget package (checked 2026-11-11: each one's
# installer from its publisher's own site or releases), its publisher and licence as winget lists them, and
# its download page (no winget, or another system)
TOOLS = (
    {"key": "makemkv", "name": "MakeMKV", "for": "rips discs, and unlocks Blu-ray menus for Detect titles",
     "winget": "GuinpinSoft.MakeMKV", "publisher": "GuinpinSoft", "license": "Shareware (free while in beta; it asks for its beta key)",
     "page": "https://www.makemkv.com/download/"},
    {"key": "vlc", "name": "VLC", "for": "plays DVD menus for Detect titles, and previews titles",
     "winget": "VideoLAN.VLC", "publisher": "VideoLAN", "license": "GPL-2.0",
     "page": "https://www.videolan.org/vlc/"},
    {"key": "ffmpeg", "name": "FFmpeg", "for": "previews a Blu-ray's titles in Edit titles",
     "winget": "Gyan.FFmpeg", "publisher": "Gyan (gyan.dev builds)", "license": "GPL-3.0",
     "page": "https://ffmpeg.org/download.html"},
    {"key": "tesseract", "name": "Tesseract", "for": "reads text off cover art",
     "winget": "UB-Mannheim.TesseractOCR", "publisher": "Tesseract-OCR community (UB Mannheim builds)", "license": "Apache-2.0",
     "page": "https://tesseract-ocr.github.io/tessdoc/Installation.html"},
)
BY_KEY = {t["key"]: t for t in TOOLS}
# the ones Settings lists, to install or find: Tesseract isn't (RapidOCR, bundled, and Windows' OCR read first;
# it's rarely reached), but a copy already installed is still used, last, wherever text is read. Whether it
# stays at all: decided once end credits are read (it may read their plain lettering well)
LISTED = tuple(t for t in TOOLS if t["key"] != "tesseract")
# (winget's community repository: each package's manifest, naming its publisher's installer and its checksum)
WINGET_REPO = "https://github.com/microsoft/winget-pkgs"


def _program_files():
    return [p for p in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if p]


def _winget_links():
    """Where winget puts its links to portable tools (FFmpeg's): on PATH only for programs started after."""
    local = os.environ.get("LOCALAPPDATA")
    return os.path.join(local, "Microsoft", "WinGet", "Links") if local else None


# (where a tool's installer puts it, when it doesn't add itself to PATH: Tesseract's)
_USUAL = {"tesseract": [("Tesseract-OCR", "tesseract.exe")]}


# (how a program shows it runs: its version asked for; one whose install didn't finish can't start, its
# libraries missing: Tesseract's, its installer stopped halfway, "libtiff-6.dll: cannot open")
_VERSION_ARG = {"tesseract": "--version", "ffmpeg": "-version", "ffplay": "-version"}
_RUNS = {}            # {(path, size, mtime): whether it ran}: asked once a file


def runs(name, path):
    """Whether a program found at path starts (its version asked for, quickly); a program with no such
    check, yes."""
    arg = _VERSION_ARG.get(os.path.splitext(os.path.basename(name))[0].lower())
    if arg is None:
        return True
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (os.path.normcase(path), st.st_size, st.st_mtime)
    if key not in _RUNS:
        try:
            r = subprocess.run([path, arg], capture_output=True, timeout=15,
                               creationflags=0x08000000 if os.name == "nt" else 0)
            _RUNS[key] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _RUNS[key] = False
    return _RUNS[key]


# Programs whose folders hold libraries other programs load by name, through PATH: Tesseract's has
# libfribidi-0.dll, which Pillow loads when it's there. Loaded into the app, Windows won't let Tesseract's
# installer replace it ("Error opening file for writing"). Their folders are taken off the app's own PATH at
# startup (keep_off_path), and remembered as where the program is.
_OFF_PATH = ("tesseract",)
_ON_PATH = {}         # {program: [its folders that were on PATH]}


def keep_off_path():
    """At startup, before anything loads a library: each of _OFF_PATH's folders taken off this process's PATH
    (and so off its children's), kept as where that program is. It's always called by its full path (which)."""
    if os.name != "nt":
        return
    keep = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        hit = [n for n in _OFF_PATH if d and os.path.isfile(os.path.join(d, n + ".exe"))]
        for n in hit:
            _ON_PATH.setdefault(n, []).append(d)
        if not hit:
            keep.append(d)
    os.environ["PATH"] = os.pathsep.join(keep)


def loaded_from(folder):
    """The files of a folder this process has loaded (a library: VLC's, MakeMKV's, one through PATH), which
    an installer can't replace while it runs. [] elsewhere than Windows, or if it can't be told."""
    if os.name != "nt" or not folder:
        return []
    import ctypes
    from ctypes import wintypes as w
    try:
        psapi, k32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = w.HANDLE
        psapi.EnumProcessModules.argtypes = [w.HANDLE, ctypes.POINTER(w.HMODULE), w.DWORD, ctypes.POINTER(w.DWORD)]
        psapi.GetModuleFileNameExW.argtypes = [w.HANDLE, w.HMODULE, w.LPWSTR, w.DWORD]
        proc = k32.GetCurrentProcess()
        mods, need = (w.HMODULE * 2048)(), w.DWORD()
        if not psapi.EnumProcessModules(proc, mods, ctypes.sizeof(mods), ctypes.byref(need)):
            return []
        base = os.path.normcase(os.path.abspath(folder)).rstrip("\\") + "\\"
        out, buf = [], ctypes.create_unicode_buffer(1024)
        for m in mods[:need.value // ctypes.sizeof(w.HMODULE)]:
            if m and psapi.GetModuleFileNameExW(proc, m, buf, 1024) and os.path.normcase(buf.value).startswith(base):
                out.append(buf.value)
        return out
    except (OSError, AttributeError):
        return []


def still_running(pid):
    """Whether a process is still running (the app's copy before a restart, letting go of a tool's files)."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        h = k32.OpenProcess(0x00100000, False, int(pid))         # (SYNCHRONIZE)
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x00000102    # (WAIT_TIMEOUT: not ended)
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def folders_of(key, settings=None):
    """The folders a tool's installer writes to, as far as this app knows: where it's found (or broken), and
    where its installer usually puts it."""
    out = []
    p = find(key, settings) if key != "vlc" else None
    if key == "vlc":
        p = find("vlc", settings)
        out += [p] if p else []
    elif p:
        out.append(os.path.dirname(p))
    if key in ("ffmpeg", "tesseract"):
        b = found_broken(key)
        out += [os.path.dirname(b)] if b else []
    for base in _program_files():
        out += [os.path.join(base, folder) for folder, _ in _USUAL.get(key, [])]
    return list(dict.fromkeys(os.path.normcase(x) for x in out if x))


def _saved_path():
    """Windows' saved PATH, yours then the system's, as it is now: an installer that adds its folder to it
    (winget, a portable tool whose link it couldn't make: FFmpeg's bin folder) changes it only for programs
    started after, not for the app's own."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for hive, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, sub) as k:
                value = winreg.QueryValueEx(k, "Path")[0]
        except OSError:
            continue
        out += [os.path.expandvars(d.strip()) for d in str(value).split(";") if d.strip()]
    return out


# (a program's copy chosen in Settings, Find…: used before any other, wherever the app runs it)
_CHOSEN = {}          # {program: its path}


def choose(settings):
    """The copies chosen in Settings (Find…) of the programs run by name (FFmpeg, Tesseract), used from now on;
    one not chosen, or not there, looked for as usual."""
    _CHOSEN.clear()
    for key in ("ffmpeg", "tesseract"):
        p = ((settings or {}).get(key) or "").strip()
        if p and os.path.isfile(p):
            _CHOSEN[key] = p


def _places(name):
    """Where a program may be, in turn: PATH (and its folders taken off the app's own, keep_off_path); winget's
    links and Windows' saved PATH (a tool installed this session, the app's PATH not yet showing it); where
    its installer usually puts it (Tesseract's Program Files folder). A copy chosen in Settings before all
    of them (FFmpeg's, its ffplay beside it)."""
    base = os.path.splitext(os.path.basename(name))[0].lower()
    if base in _CHOSEN:
        yield _CHOSEN[base]
    elif base == "ffplay" and "ffmpeg" in _CHOSEN:
        d = os.path.dirname(_CHOSEN["ffmpeg"])
        yield os.path.join(d, "ffplay.exe" if os.name == "nt" else "ffplay")
    yield shutil.which(name)
    if os.name != "nt":
        return
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    for d in _ON_PATH.get(name, []):
        yield os.path.join(d, exe)
    links = _winget_links()
    if links:
        yield os.path.join(links, exe)
    for d in _saved_path():
        yield os.path.join(d, exe)
    for base in _program_files():
        for folder, file in _USUAL.get(name, []):
            yield os.path.join(base, folder, file)


def which(name):
    """A program's full path, or None: the first one found that runs (runs), on PATH or where winget or its
    installer put it (_places). What the app calls it by, so it runs wherever it was found."""
    for p in _places(name):
        if p and os.path.isfile(p) and runs(name, p):
            return p
    return None


def found_broken(name):
    """A copy of a program that's there but doesn't run (an install that didn't finish), or None."""
    return next((p for p in _places(name) if p and os.path.isfile(p) and not runs(name, p)), None)


def find(key, settings=None):
    """Where a tool is (its program, or VLC's folder), or None: a path set in Settings first (one typed in
    Settings, not yet saved, too), else looked for."""
    settings = settings or {}
    if key in ("ffmpeg", "tesseract"):
        p = (settings.get(key) or "").strip()
        if p and os.path.isfile(p) and runs(key, p):
            return p
    if key == "makemkv":
        from . import core
        p = (settings.get("makemkv") or "").strip()
        return p if p and os.path.isfile(p) else core.find_makemkvcon()
    if key == "vlc":
        from . import menus
        return menus.find_vlc((settings.get("vlc") or "").strip() or None)
    if key == "ffmpeg":
        return which("ffmpeg")
    if key == "tesseract":
        return which("tesseract")
    raise KeyError(key)


def winget():
    """winget's path, or None (Windows only; it comes with Microsoft's App Installer, missing on some
    Windows 10 PCs)."""
    return which("winget") if os.name == "nt" else None


def install_command(key):
    """The winget command that installs a tool: from winget's own repository (not the Store), its exact
    package; its installer's window shown (--interactive: its licence and choices are the user's); the
    agreements accepted only because the user agreed to them in the app's consent box first."""
    return [winget() or "winget", "install", "--id", BY_KEY[key]["winget"], "--exact", "--source", "winget",
            "--interactive", "--accept-source-agreements", "--accept-package-agreements", "--disable-interactivity"]


def install(key, log=lambda line: None):
    """Install a tool through winget (off the Tk thread: it waits for the installer). Returns (ok, its last
    words). The installer's own window shows; Windows asks for permission where it installs for everyone."""
    cmd = install_command(key)
    log("installing %s: %s" % (BY_KEY[key]["name"], subprocess.list2cmdline(cmd)))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=0x08000000 if os.name == "nt" else 0)     # (CREATE_NO_WINDOW: winget's console)
    except OSError as e:
        return False, str(e)
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip() and not set(ln.strip()) <= set("-\\|/ █▒")]
    for ln in lines:
        log("  winget: %s" % ln)
    # (0: installed; a tool already there, or newer: winget says so and that's as good)
    ok = r.returncode == 0 or any("already installed" in ln.lower() for ln in lines)
    last = lines[-1] if lines else "winget exited with code %s" % r.returncode
    if not ok and "installer failed with exit code" in last.lower():
        # (its installer stopped: closed, cancelled, or it failed partway; NSIS's 2 for any of those)
        last = "the installer stopped before it finished (exit code %s)" % last.rsplit(":", 1)[-1].strip()
    return ok, last


def how_to_get(key):
    """For a system with no winget: where to get a tool, in a sentence."""
    t = BY_KEY[key]
    if sys.platform == "darwin":
        extra = {"ffmpeg": " (or: brew install ffmpeg)", "tesseract": " (or: brew install tesseract)",
                 "vlc": " (or: brew install --cask vlc)"}.get(key, "")
    elif os.name != "nt":
        extra = {"ffmpeg": " (or your package manager: ffmpeg)", "tesseract": " (or your package manager: tesseract-ocr)",
                 "vlc": " (or your package manager: vlc)",
                 "makemkv": " (Linux: its forum has the download and the build steps)"}.get(key, "")
    else:
        extra = ""
    return "Download it from %s%s." % (t["page"], extra)
