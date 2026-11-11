"""A packaged build's self-check: that what it carries works, with no disc and nothing installed beside it.
`Disc Detective.exe --self-check [report.json]` (or `python -m disc_detective.selfcheck`): its modules load, its
own files are there, RapidOCR reads text with the models it brought, OpenCV decodes video, and its windows can be
drawn (where there's a screen). Exits 0 when every required check passes; the report says what each found."""
import json
import os
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ("app", "core", "menus", "bdmenus", "bdcrawl", "bdhdmv", "dvdvm", "makemkv", "theme", "tools", "i18n")


def _modules():
    import importlib
    for m in MODULES:
        importlib.import_module("disc_detective." + m)
    import hdmv_menus  # noqa: F401
    return "%d modules and hdmv-menus" % len(MODULES)


def _files():
    need = {"icon.png": 1, "fonts": 7, "icons": 20, "art": 2}
    for name, least in need.items():
        p = os.path.join(HERE, name)
        n = 1 if os.path.isfile(p) else len(os.listdir(p)) if os.path.isdir(p) else 0
        if n < least:
            raise AssertionError("%s: %d of at least %d" % (name, n, least))
    return "icon, fonts, icons, art"


def _sample():
    """A picture of words in the app's own font: what the readers are asked to read."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (900, 220), "white")
    font = ImageFont.truetype(os.path.join(HERE, "fonts", "Inter-Bold.ttf"), 72)
    ImageDraw.Draw(im).text((40, 60), "DISC DETECTIVE", font=font, fill="black")
    return im


def _rapidocr():
    from . import menus
    text = menus.rapid_text(_sample())
    if not text or "DETECTIVE" not in text.upper():
        raise AssertionError("read %r" % text)
    return "read %r" % text


def _windows_ocr():
    from . import menus
    if os.name != "nt":
        return "not Windows"
    text = menus.windows_text(_sample())
    return "read %r" % text if text is not None else "not available (no OCR language, or no winrt)"


def _opencv():
    """OpenCV's own FFmpeg: a short clip written, then read back through it (as a DVD's cover still is)."""
    import cv2
    import numpy as np
    path = os.path.join(tempfile.mkdtemp(), "check.avi")
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 5, (64, 48))
    for _ in range(3):
        out.write(np.full((48, 64, 3), 200, np.uint8))
    out.release()
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise AssertionError("OpenCV's FFmpeg read nothing back")
    return "OpenCV %s decoded a frame %dx%d" % (cv2.__version__, frame.shape[1], frame.shape[0])


def _windows():
    import tkinter as tk
    from . import theme
    try:
        root = tk.Tk()
        root.withdraw()                         # (drawn, not shown)
    except tk.TclError as exc:
        return "no screen to draw on (%s)" % exc
    try:
        theme.setup(root)
        theme.icon("scan")
        root.update_idletasks()
        return "Tk %s, theme and icons" % root.tk.call("info", "patchlevel")
    finally:
        root.destroy()


CHECKS = (("modules", _modules, True), ("files", _files, True), ("rapidocr", _rapidocr, True),
          ("opencv", _opencv, True), ("windows", _windows, True), ("windows_ocr", _windows_ocr, False))


def run():
    """Each check: {"name", "ok", "required", "said", "seconds"}."""
    out = []
    for name, check, required in CHECKS:
        t0 = time.time()
        try:
            said, ok = check(), True
        except Exception as exc:
            said, ok = "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc(limit=4)), False
        out.append({"name": name, "ok": ok, "required": required, "said": said, "seconds": round(time.time() - t0, 2)})
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    from . import core
    results = run()
    report = {"version": core.VERSION, "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)),
              "ok": all(r["ok"] for r in results if r["required"]), "checks": results}
    text = json.dumps(report, indent=2)
    if argv:
        with open(argv[0], "w", encoding="utf-8") as fh:
            fh.write(text)
    if sys.stdout is not None:                  # (a windowed build has no console)
        try:
            print(text)
        except OSError:
            pass
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
