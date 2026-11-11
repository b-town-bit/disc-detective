"""Builds Disc Detective's download-and-run package for this system: PyInstaller's one folder (the app's
program, Python and its libraries in it), every bundled package's licence gathered into licenses/, the build's
self-check run, then zipped. Run from the repository's root, in an environment with the app, its Windows OCR
extra (on Windows) and PyInstaller installed:

    python -m pip install ".[windows-ocr]" pyinstaller
    python release/build.py

Out: dist/Disc Detective/ (Windows, Linux) or dist/Disc Detective.app (macOS), and dist/Disc-Detective-<version>-
<system>.zip. --no-check skips the self-check (a system with no screen and no virtual one)."""
import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HERE = os.path.join(ROOT, "release")
WORK = os.path.join(HERE, "build")
DIST = os.path.join(ROOT, "dist")
NAME = "Disc Detective"

sys.path.insert(0, ROOT)
from disc_detective.core import VERSION  # noqa: E402

# (files a package's licence is in: its licence, copying terms, notices of what it carries)
LICENCE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|THIRDPARTYNOTICES|LICENSE-3RD-PARTY)[^/]*$", re.I)
# packages that ship no licence file of their own: the text of the licence they name
NO_FILE = {"flatbuffers": "Apache-2.0.txt"}


def system():
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _key(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def bundled_packages():
    """The installed packages the build carries: the app's requirements and theirs, as installed here (Windows:
    its OCR extra too), and PyInstaller (its launcher starts the program)."""
    from packaging.requirements import Requirement
    seen, out = set(), []

    def walk(req, extras=()):
        r = Requirement(req) if isinstance(req, str) else req
        if r.marker is not None and not any(r.marker.evaluate({"extra": e}) for e in (extras or ("",))):
            return
        k = _key(r.name)
        if k in seen:
            return
        try:
            d = md.distribution(r.name)
        except md.PackageNotFoundError:
            return
        seen.add(k)
        out.append(d)
        for sub in d.requires or []:
            walk(sub, tuple(r.extras))
    import tomllib
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        project = tomllib.load(fh)["project"]
    reqs = list(project["dependencies"])
    if sys.platform == "win32":
        reqs += project["optional-dependencies"]["windows-ocr"]
    for r in reqs:
        req = Requirement(r)
        if req.marker is None or req.marker.evaluate():
            walk(Requirement(str(req.name) + str(req.specifier)))
    out.append(md.distribution("pyinstaller"))      # (its launcher only: not its own build tools)
    return out


def gather_licences(dest):
    """Each bundled package's licence files into dest/<package>/, Python's own (it lists the libraries its
    build carries: Tcl/Tk, OpenSSL, libffi...), and a list of them all. A package with none: the build fails."""
    os.makedirs(dest, exist_ok=True)
    rows, missing = [], []
    for d in sorted(bundled_packages(), key=lambda d: _key(d.metadata["Name"])):
        name, version = d.metadata["Name"], d.version
        lic = d.metadata.get("License-Expression") or ""
        if not lic:
            classes = [c.split("::")[-1].strip() for c in d.metadata.get_all("Classifier") or [] if c.startswith("License ::")]
            lic = ", ".join(classes) or (d.metadata.get("License") or "").splitlines()[0][:60] if (classes or d.metadata.get("License")) else ""
        files = [f for f in d.files or [] if LICENCE_FILE.search(str(f).replace("\\", "/"))]
        out = os.path.join(dest, _key(name))
        os.makedirs(out, exist_ok=True)
        for f in files:
            src = f.locate()
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(out, os.path.basename(str(f))))
        if not os.listdir(out):
            text = NO_FILE.get(_key(name))
            if text:
                shutil.copy2(os.path.join(HERE, "licenses", text), os.path.join(out, "LICENSE (%s, as it names)" % text))
            else:
                missing.append(name)
        rows.append((name, version, lic or "see its files"))
    py = next((p for p in (os.path.join(sys.base_prefix, "LICENSE.txt"),
                           os.path.join(sys.base_prefix, "lib", "python%d.%d" % sys.version_info[:2], "LICENSE.txt"))
               if os.path.isfile(p)), None)
    if py:
        os.makedirs(os.path.join(dest, "python"), exist_ok=True)
        shutil.copy2(py, os.path.join(dest, "python", "LICENSE.txt"))
    else:
        missing.append("Python")
    rows.insert(0, ("Python", sys.version.split()[0], "PSF-2.0 (and the libraries its build carries, in its file)"))
    with open(os.path.join(dest, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write("Software bundled with %s %s, each under its own licence (its files in the folder named\n"
                 "after it). Disc Detective's own licence is LICENSE, beside the program; the data and fonts it\n"
                 "uses are credited in THIRD_PARTY.md.\n\n" % (NAME, VERSION))
        width = max(len(r[0]) for r in rows) + 2
        for n, v, lic in rows:
            fh.write("%s%s%s\n" % (n.ljust(width), v.ljust(20), lic))
    if missing:
        raise SystemExit("no licence file for: %s (add its licence's text to release/licenses and NO_FILE)"
                         % ", ".join(missing))
    return rows


def make_icons():
    from PIL import Image
    os.makedirs(WORK, exist_ok=True)
    with Image.open(os.path.join(ROOT, "disc_detective", "icon.png")) as im:
        im = im.convert("RGBA")
        im.save(os.path.join(WORK, "icon.ico"), sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
        if sys.platform == "darwin":
            im.resize((1024, 1024), Image.LANCZOS).save(os.path.join(WORK, "icon.icns"))
    parts = [int(p) for p in re.findall(r"\d+", VERSION)][:4]
    parts += [0] * (4 - len(parts))
    with open(os.path.join(WORK, "version.txt"), "w", encoding="utf-8") as fh:
        fh.write(VERSION_INFO % {"tuple": tuple(parts), "version": VERSION, "name": NAME})


VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(tuple)r, prodvers=%(tuple)r, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Disc Detective contributors'),
      StringStruct('FileDescription', '%(name)s'),
      StringStruct('FileVersion', '%(version)s'),
      StringStruct('InternalName', '%(name)s'),
      StringStruct('LegalCopyright', 'PolyForm Noncommercial 1.0.0; bundled software under its own licences'),
      StringStruct('OriginalFilename', '%(name)s.exe'),
      StringStruct('ProductName', '%(name)s'),
      StringStruct('ProductVersion', '%(version)s')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def program_and_folder():
    """The built program, and the folder its own files go in (licences, LICENSE): beside it, or in a macOS
    app's Resources."""
    if sys.platform == "darwin":
        app = os.path.join(DIST, NAME + ".app")
        return os.path.join(app, "Contents", "MacOS", NAME), os.path.join(app, "Contents", "Resources"), app
    folder = os.path.join(DIST, NAME)
    exe = os.path.join(folder, NAME + (".exe" if sys.platform == "win32" else ""))
    return exe, folder, folder


def self_check(exe):
    """The built program's --self-check, its report read back. Windowed on Windows: no console, so the report
    comes through a file."""
    report = os.path.join(tempfile.mkdtemp(), "self-check.json")
    r = subprocess.run([exe, "--self-check", report], timeout=300)
    try:
        with open(report, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        raise SystemExit("the build's self-check wrote no report (exit code %s)" % r.returncode)
    for c in got["checks"]:
        print("  %-12s %s  %s" % (c["name"], "ok  " if c["ok"] else ("FAIL" if c["required"] else "note"),
                                  c["said"].splitlines()[0]))
    if r.returncode != 0 or not got["ok"]:
        raise SystemExit("the build's self-check failed:\n" + json.dumps(got, indent=2))
    return got


def main(argv):
    check = "--no-check" not in argv
    make_icons()
    print("building with PyInstaller ...")
    subprocess.run([sys.executable, "-m", "PyInstaller", os.path.join(HERE, "disc_detective.spec"), "--noconfirm",
                    "--distpath", DIST, "--workpath", os.path.join(WORK, "work")], check=True, cwd=ROOT)
    exe, own, top = program_and_folder()
    for f in ("LICENSE", "THIRD_PARTY.md"):
        shutil.copy2(os.path.join(ROOT, f), os.path.join(own, f))
    rows = gather_licences(os.path.join(own, "licenses"))
    print("licences of %d bundled packages gathered" % len(rows))
    if check:
        print("self-check ...")
        self_check(exe)
    zip_path = os.path.join(DIST, "Disc-Detective-%s-%s.zip" % (VERSION, system()))
    size = zip_folder(top, zip_path)
    print("%s: %.0f MB unzipped, %.0f MB zipped" % (zip_path, size / 1e6, os.path.getsize(zip_path) / 1e6))


def zip_folder(top, zip_path):
    """Zip the built folder (or .app) with its name at the top. Symbolic links go in as links, as unzip and macOS's
    Archive Utility restore them: on Linux and macOS PyInstaller links each library to one copy, and followed, every
    one would be stored twice (and a linked folder, which os.walk lists but doesn't enter, left out). The size of
    what it holds, links counted as links, is returned."""
    base, size = os.path.dirname(top), 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, dirs, files in os.walk(top):
            for f in sorted(files) + sorted(d for d in dirs if os.path.islink(os.path.join(folder, d))):
                p = os.path.join(folder, f)
                name = os.path.relpath(p, base).replace(os.sep, "/")
                if os.path.islink(p):
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3                  # Unix, so the mode bits below are read
                    info.external_attr = 0o120777 << 16     # a symbolic link; its target is the entry's data
                    target = os.readlink(p)
                    z.writestr(info, target)
                    size += len(target)
                else:
                    z.write(p, name)
                    size += os.path.getsize(p)
    return size


if __name__ == "__main__":
    main(sys.argv[1:])
