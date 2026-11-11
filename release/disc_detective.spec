# PyInstaller spec for Disc Detective: one folder (not one file: it starts fast, and fewer antivirus
# false alarms), the app's windowed program in it. Built by release/build.py, which also gathers the
# licences. Run from the repository's root: pyinstaller release/disc_detective.spec
import os
import sys

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
PKG = os.path.join(ROOT, "disc_detective")

# the app's own files (pyproject.toml's package-data)
datas = [(os.path.join(PKG, "icon.png"), "disc_detective")]
for sub in ("fonts", "icons", "art"):
    datas.append((os.path.join(PKG, sub), os.path.join("disc_detective", sub)))

# RapidOCR: its settings, and only the models the app reads with (menus.rapid_lines: PP-OCRv6 small to find
# text, PP-OCRv5 English mobile to read it, and the small direction model it loads at start); the rest it
# never uses (83 MB). Bundled, so menus are read with no download first.
MODELS = ("PP-OCRv6_det_small.onnx", "en_PP-OCRv5_rec_mobile.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx")
for src, dest in collect_data_files("rapidocr"):
    name = os.path.basename(src)
    if name.endswith(".onnx") and name not in MODELS:
        continue
    datas.append((src, dest))

hidden = ["hdmv_menus"]
if sys.platform == "win32":
    # Windows' own OCR, the second reader (pip install ".[windows-ocr]" in the build's environment)
    hidden += ["winrt.windows.media.ocr", "winrt.windows.graphics.imaging", "winrt.windows.storage.streams",
               "winrt.windows.foundation", "winrt.windows.foundation.collections", "winrt.windows.globalization"]

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hidden,
    excludes=["pytest", "ruff", "matplotlib", "IPython", "PyQt5", "PyQt6", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = os.path.join(SPECPATH, "build", "icon.ico" if sys.platform == "win32" else "icon.icns")
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Disc Detective",
    console=False,
    icon=icon if os.path.exists(icon) else None,
    version=os.path.join(SPECPATH, "build", "version.txt") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Disc Detective")

if sys.platform == "darwin":
    app = BUNDLE(coll, name="Disc Detective.app", icon=icon if os.path.exists(icon) else None,
                 bundle_identifier="io.github.b-town-bit.disc-detective")
