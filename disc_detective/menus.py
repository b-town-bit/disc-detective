"""A disc's menus: which button plays which title, and what the button says.

DVD menus are pages of video with buttons drawn on them. The disc's IFO files say, for every
menu page (a "PGC"), where its video is, and the page's navigation packets list its buttons:
each one's rectangle on screen and the command it runs ("play title 3", "go to page 9", or
"set a register, then go to a page that decides"). None of that is encrypted, so it's read here
directly.

The pictures are another matter: on most commercial DVDs the menu video is CSS-encrypted, like
the film. Disc Detective has no decryption of its own. It uses VLC, when it's installed, through
VLC's own library (libvlc): VLC plays the disc's menus, hands over the decoded frames, and can
press the buttons like a remote, which also says where each button really goes.

    python -m disc_detective.menus E:            structure, then the crawl through VLC
    python -m disc_detective.menus E: --all      ... pressing every button, not just the extras'
    python -m disc_detective.menus E: --probe    structure, then a short VLC probe
    python -m disc_detective.menus E: --no-vlc   structure only
    python -m disc_detective.menus --replay <fingerprint>   the disc's program, from a crawl's
                                                 kept test data (no disc, no VLC)
"""
import argparse
import ctypes
import difflib
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

from . import tools

SECTOR = 2048
MENU_TYPES = {2: "title", 3: "root", 4: "subpicture", 5: "audio", 6: "angle", 7: "chapter"}
SINS = {1: "top_cell", 2: "next_cell", 3: "prev_cell", 5: "top_program", 6: "next_program", 7: "prev_program",
        9: "top_page", 10: "next_page", 11: "prev_page", 12: "up_page", 13: "tail_page", 16: "resume"}
_u16 = lambda b, o: struct.unpack_from(">H", b, o)[0]
_u32 = lambda b, o: struct.unpack_from(">I", b, o)[0]


# --------------------------------------------------------------------------
# 1. The menu structure (IFO + navigation packets; never encrypted)
# --------------------------------------------------------------------------

def _pgc(ifo, off):
    """A PGC (a menu page, or a stretch of a title): its programs, cells, and commands (pre: on
    entering; post: at its end; cell: after a cell whose "cmd" names one), its next / previous /
    up PGCs (what LinkNextPGC and the like go to), and its length in seconds."""
    from .core import dvd_time
    p = {"cells": [], "pre": [], "post": [], "cell_cmds": []}
    p["programs"], ncells = ifo[off + 2], ifo[off + 3]
    p["next"], p["prev"], p["up"] = _u16(ifo, off + 0x9C), _u16(ifo, off + 0x9E), _u16(ifo, off + 0xA0)
    p["seconds"] = dvd_time(ifo, off + 4)
    cmd_off, cell_off = _u16(ifo, off + 0xE4), _u16(ifo, off + 0xE8)
    if cmd_off:
        c = off + cmd_off
        npre, npost, ncell = _u16(ifo, c), _u16(ifo, c + 2), _u16(ifo, c + 4)
        cmds = [ifo[c + 8 + 8 * i: c + 16 + 8 * i] for i in range(npre + npost + ncell)]
        p["pre"], p["post"], p["cell_cmds"] = cmds[:npre], cmds[npre:npre + npost], cmds[npre + npost:]
    # its 16 colours (Y, Cr, Cb): the subpicture's, a menu's highlight among them
    ycc = lambda y, cr, cb: [max(0, min(255, int(round(v)))) for v in
                             (y + 1.402 * (cr - 128), y - 0.344 * (cb - 128) - 0.714 * (cr - 128), y + 1.772 * (cb - 128))]
    p["palette"] = [ycc(*ifo[off + 0xA5 + 4 * i: off + 0xA8 + 4 * i]) for i in range(16)] \
        if len(ifo) >= off + 0xE4 else []
    pmap = _u16(ifo, off + 0xE6)
    if pmap:                                # each program's first cell (scene pages are programs)
        p["program_cells"] = list(ifo[off + pmap: off + pmap + p["programs"]])
    if cell_off:
        for i in range(ncells):
            q = off + cell_off + 24 * i
            # still: 255 waits for the viewer (a menu), else seconds; cmd: a cell command to run after;
            # seconds: how long it plays (a menu's intro before its buttons, for one)
            p["cells"].append({"first": _u32(ifo, q + 8), "last": _u32(ifo, q + 20),
                               "still": ifo[q + 2], "cmd": ifo[q + 3], "seconds": dvd_time(ifo, q + 4)})
    return p


def _menu_pgcs(ifo, sector):
    """[(number, menu type or None, entry?, pgc)] of a domain's first language unit."""
    if not sector:
        return []
    base = sector * SECTOR
    if base + 8 > len(ifo) or not _u16(ifo, base):
        return []
    lu = base + _u32(ifo, base + 8 + 4)
    out = []
    for j in range(_u16(ifo, lu)):
        r = lu + 8 + 8 * j
        cat = ifo[r]
        out.append((j + 1, MENU_TYPES.get(cat & 0x0F), bool(cat & 0x80), _pgc(ifo, lu + _u32(ifo, r + 4))))
    return out


class Refused(OSError):
    """The drive won't read part of the menus yet: a copy-protected disc's drive refuses its
    protected sectors until a player has unlocked it (VLC does, by opening the disc)."""


def _buttons(vob, cells):
    """A menu page's buttons: those of the first navigation packet that has any, or [] (none /
    unreadable). Motion menus often turn their buttons on partway through, so this walks the
    page's cells one video unit (VOBU) at a time; each navigation packet says where the next is.
    Also the seconds into these cells that the buttons come on (Father of the Bride's Bonus
    page: 2.75 s; keys pressed before then do nothing)."""
    before = 0.0
    for cell in cells:
        sector, first = cell["first"], None
        while sector <= cell["last"]:
            vob.seek(sector * SECTOR)
            try:
                s = vob.read(SECTOR)
            except OSError as e:
                raise Refused("the drive won't read sector %d of the menus" % sector) from e
            if len(s) < SECTOR or s[0:4] != b"\x00\x00\x01\xba" or s[0x26:0x2A] != b"\x00\x00\x01\xbf":
                break
            ptm = _u32(s, 0x2D + 0x0C) / 90000      # the unit's start time (PCI)
            first = ptm if first is None else first
            out = _nav_buttons(s)
            if out:
                return out, round(before + max(0.0, ptm - first), 2)
            sector += _u32(s, 0x40F) + 1        # the DSI's VOBU end address: the next unit follows it
        before += cell.get("seconds") or 0
    return [], 0.0


def _nav_buttons(s):
    """The buttons in one navigation packet (a 2048-byte sector)."""
    pci = s[0x2D:]
    hli = 0x60
    forced = pci[hli + 20]                      # the button the page selects as it comes on (0: none)
    # the colours a highlighted button is drawn in (its colour set's "selected" four: palette
    # numbers, those not see-through): Wallace & Gromit's first menu black, its Shorts page white
    lit = []
    for k in range(3):
        sel = pci[hli + 22 + 8 * k: hli + 26 + 8 * k]
        colours = (sel[1] & 15, sel[1] >> 4, sel[0] & 15, sel[0] >> 4)
        seen = (sel[3] & 15, sel[3] >> 4, sel[2] & 15, sel[2] >> 4)
        lit.append(sorted({c for c, a in zip(colours, seen) if a}))
    out = []
    for i in range(pci[hli + 17]):
        b = pci[hli + 46 + 18 * i: hli + 46 + 18 * (i + 1)]
        x = (b[0] << 16) | (b[1] << 8) | b[2]
        y = (b[3] << 16) | (b[4] << 8) | b[5]
        out.append({"n": i + 1, "rect": [(x >> 12) & 0x3FF, (y >> 12) & 0x3FF, x & 0x3FF, y & 0x3FF],
                    "auto": bool((b[3] >> 6) & 3), "up": b[6] & 0x3F, "down": b[7] & 0x3F,
                    "left": b[8] & 0x3F, "right": b[9] & 0x3F, "cmd": command(b[10:18]),
                    "raw": b[10:18].hex(), "lit": lit[(b[0] >> 6) - 1] if b[0] >> 6 else []})
        if forced == i + 1:
            out[-1]["forced"] = True
    return out


def command(c):
    """A DVD VM command, decoded for what a menu crawl needs: {"op": ..., ...}."""
    if c == b"\x00" * 8:
        return {"op": "nop"}
    hi = c[0] >> 4
    if c[0] == 0x30:
        op = c[1] & 0x0F
        if op == 1:
            return {"op": "exit"}
        if op == 2:
            return {"op": "title", "title": c[5]}
        if op == 3:
            return {"op": "vts_title", "title": c[5]}
        if op == 5:
            return {"op": "vts_chapter", "title": c[5], "chapter": _u16(c, 2) & 0x3FF}
        if op in (6, 8):
            kind = c[5] >> 6
            d = {"op": "menu" if op == 6 else "call_menu"}
            if kind == 1:
                d.update(domain="vmgm", menu=MENU_TYPES.get(c[5] & 0x0F))
            elif kind == 2:
                d.update(domain="vtsm", vts=c[4], menu=MENU_TYPES.get(c[5] & 0x0F))
            elif kind == 3:
                d.update(domain="vmgm", pgc=_u16(c, 2))
            else:
                d.update(domain="first_play")
            return d
    if (c[0] & 0xF0) == 0x20 or (hi in (5, 6, 7) and (c[1] & 0x0F)):
        # a link: on its own (0x20) or after setting a register (0x5x-0x7x "set, then link")
        op = c[1] & 0x0F
        if op == 1:                         # LinkSIns: a named spot in this menu, or resume
            d = {"op": "link_" + SINS.get(c[7] & 0x1F, "special")}
            if hi in (5, 6, 7):
                d["sets"] = c.hex()
            return d
        link = {4: ("page", _u16(c, 6) & 0x7FFF), 5: ("chapter", _u16(c, 6) & 0x3FF), 6: ("program", c[7]),
                7: ("cell", c[7])}.get(op)
        d = {"op": "link_" + link[0], "to": link[1]} if link else {"op": "link", "raw": c.hex()}
        if hi in (5, 6, 7):
            d["sets"] = c.hex()            # the register it sets first (a dispatcher page reads it)
        return d
    if hi in (4, 5, 6, 7):
        return {"op": "set", "raw": c.hex()}
    return {"op": "other", "raw": c.hex()}


def _pal_menus(ifo):
    """Whether a domain's menus are PAL: 576 lines to NTSC's 480 (its video attributes, in either IFO)."""
    return len(ifo) > 0x100 and (ifo[0x100] >> 4) & 3 == 1


def _to_frame(programs):
    """A PAL page's buttons, in place, on the 720x480 frames everything here goes by: VLC gives
    every disc's that size, and a button's rectangle in PAL's 576 lines is another part of the
    picture (Friends S1 D1's main menu: Play at line 478 of a 480-line frame, its words at 398)."""
    for buttons in programs:
        for b in buttons:
            x0, y0, x1, y1 = b["rect"]
            b["rect"] = [x0, round(y0 * 480 / 576), x1, round(y1 * 480 / 576)]


def dvd_structure(root):
    """Every menu page of a DVD folder (or drive): {"vmgm": [...], "vtsm": {vts: [...]}}; a page is
    {"pgc", "type", "entry", "cells", "buttons", "pre"} (pre: the page's own commands). Also
    "titles": the disc's titles, [{"title", "vts", "ttn", "chapters"}]."""
    vt = os.path.join(root, "VIDEO_TS") if os.path.isdir(os.path.join(root, "VIDEO_TS")) else root
    pick = lambda name: next((os.path.join(vt, f) for f in os.listdir(vt) if f.upper() == name), None)
    out = {"vmgm": [], "vtsm": {}}
    domains = [("vmgm", "VIDEO_TS.IFO", "VIDEO_TS.VOB", 0xC8)]
    for f in sorted(os.listdir(vt)):
        m = re.fullmatch(r"VTS_(\d\d)_0\.IFO", f.upper())
        if m:
            domains.append((int(m.group(1)), f.upper(), "VTS_%s_0.VOB" % m.group(1), 0xD0))
    for key, ifo_name, vob_name, ptr in domains:
        ifo_path, vob_path = pick(ifo_name), pick(vob_name)
        if not ifo_path:
            continue
        with open(ifo_path, "rb") as fh:
            ifo = fh.read()
        pages = []
        vob = open(vob_path, "rb") if vob_path else None
        try:
            for n, typ, entry, p in _menu_pgcs(ifo, _u32(ifo, ptr)):
                # a page's programs can each have their own buttons (scene pages: 1-3, 4-6, ...)
                starts = [c for c in p.get("program_cells") or [1] if 1 <= c <= len(p["cells"])] or [1]
                found = [_buttons(vob, p["cells"][a - 1:(starts[i + 1] - 1 if i + 1 < len(starts) else None)])
                         if vob else ([], 0.0) for i, a in enumerate(starts)]
                progs = [b for b, _ in found]
                if _pal_menus(ifo):
                    _to_frame(progs)
                pages.append({"pgc": n, "type": typ, "entry": entry, "cells": p["cells"],
                              "buttons": next((b for b in progs if b), []), "program_buttons": progs,
                              "buttons_at": [at for _, at in found],
                              "program_cells": starts, "pre": [command(c) for c in p["pre"]],
                              "palette": p.get("palette") or []})
        finally:
            if vob:
                vob.close()
        if key == "vmgm":
            out["vmgm"] = pages
            tt = _u32(ifo, 0xC4) * SECTOR       # the title table: each title's VTS and number there
            out["titles"] = [{"title": i + 1, "vts": ifo[tt + 8 + 12 * i + 6], "ttn": ifo[tt + 8 + 12 * i + 7],
                              "chapters": _u16(ifo, tt + 8 + 12 * i + 2)}
                             for i in range(_u16(ifo, tt) if tt + 8 <= len(ifo) else 0)]
        else:
            out["vtsm"][key] = pages
    return out


# --------------------------------------------------------------------------
# 2. Reading a button's label (OCR)
# --------------------------------------------------------------------------

# words menus use: a reading that has them is likelier the right one (a tie-breaker among readings)
MENU_VOCAB = {"play", "movie", "film", "main", "menu", "special", "features", "feature", "scene", "scenes",
              "selection", "selections", "chapters", "subtitles", "audio", "setup", "set", "up", "languages",
              "trailer", "trailers", "theatrical", "teaser", "bonus", "material", "extras", "behind", "the",
              "making", "of", "deleted", "outtakes", "music", "video", "commentary", "interview", "interviews",
              "documentary", "featurette", "gallery", "talent", "files", "cast", "crew", "previews", "credits",
              "story", "production", "back", "next", "more", "on", "off"}
# the words extras are named with, beyond a menu's own: film-making's, and the kinds of extras
# (a film title rarely has them: The Iron Giant's "Storyboards and Animatics" read as junk)
EXTRAS_VOCAB = MENU_VOCAB | {
    "storyboard", "storyboards", "animatic", "animatics", "animation", "animator", "animators", "sketch",
    "sketches", "sketchbook", "concept", "art", "artwork", "design", "designs", "costume", "costumes",
    "visual", "effects", "vfx", "sfx", "stunt", "stunts", "rehearsal", "rehearsals", "screen", "test", "tests",
    "camera", "previs", "previz", "pre-visualization", "layout", "lighting", "modeling", "rendering",
    "featurettes", "commentaries", "outtake", "blooper", "bloopers", "gag", "reel", "extended", "alternate",
    "alternative", "ending", "endings", "opening", "sequence", "sequences", "montage", "galleries", "photo",
    "photos", "stills", "documentaries", "retrospective", "filmmaker", "filmmakers", "filmmaking",
    "director", "director's", "directors", "producer", "producers", "composer", "score", "soundtrack",
    "videos", "teasers", "spot", "spots", "tv", "promo", "promos", "easter", "egg", "eggs", "sneak", "peek",
    "peeks", "premiere", "junket", "press", "kit", "voice", "voices", "recording", "session", "sessions",
    "screenplay", "script", "trivia", "factoids", "pop-up", "karaoke", "sing-along", "singalong",
    "read-along", "game", "games", "quiz", "vintage", "original", "introduction", "intro", "featuring",
    "performed", "q&a", "panel", "comic-con", "tour", "webisode", "webisodes", "short", "shorts",
}
WELL_READ = 14              # a reading this good (or with a menu word) needs no tilted second look
SHORT_WORDS = {"a", "i", "of", "to", "in", "on", "an", "at", "my", "me", "is", "it", "we", "up", "be", "by", "or",
               "no", "so", "do", "go", "oh", "tv", "dvd", "the", "and"}


_win_ocr = threading.local()        # Windows' OCR engine, one per thread (None: there's none)
# is_word(w) while a stakeout runs with a dictionary (crawl's is_word: the IMDb index): Windows'
# OCR reads a button twice, as it is and its bright lettering alone (white over a busy picture:
# Jurassic World's "DINOSAURS ROAM ONCE AGAIN", read as "OSAURS ROAM ONCE" as it is), and the
# reading with more real words stands
WORDS = None
BRIGHT = 200                        # lettering this bright (of 255), the rest blanked: the second reading


def _real(line):
    """(real words less the rest, real words) in a reading: WORDS's dictionary, or the extras'."""
    ws = re.findall(r"[A-Za-z]+", line)
    real = sum(1 for w in ws if w.lower() in ("a", "i") or len(w) >= 2 and (
        w.lower() in EXTRAS_VOCAB or (WORDS is not None and WORDS(w))))
    return real - (len(ws) - real), real


def windows_text(image):
    """What Windows' own OCR (Windows.Media.Ocr, Windows 10 and later) reads in a picture: its
    lines, joined; None when it isn't there (another system, or the optional winrt packages not
    installed: pip install "disc-detective[windows-ocr]"). On menu lettering it reads poster art
    Tesseract can't ("Spanglish", "Hellboy DVD", "Disney Parks"), in a fraction of the time; it
    gives up rather than guess where Tesseract makes junk of it."""
    lines = windows_lines(image)
    return None if lines is None else " ".join(t for t, _, _ in lines)


def windows_lines(image):
    """windows_text's lines: [(text, (x0, y0, x1, y1), 1.0)] (it doesn't say how sure it is), or
    None when there's no Windows OCR."""
    if os.name != "nt":
        return None
    eng = getattr(_win_ocr, "engine", False)
    if eng is False:
        try:
            from winrt.windows.media.ocr import OcrEngine
            eng = OcrEngine.try_create_from_user_profile_languages()
        except Exception:                       # (no winrt packages, or no OCR language installed)
            eng = None
        _win_ocr.engine = eng
    if eng is None:
        return None
    try:
        import asyncio
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
        img = image.convert("RGBA")
        w = DataWriter()
        w.write_bytes(img.tobytes())
        bmp = SoftwareBitmap.create_copy_from_buffer(w.detach_buffer(), BitmapPixelFormat.RGBA8,
                                                     img.width, img.height, BitmapAlphaMode.PREMULTIPLIED)

        async def recognize():
            return await eng.recognize_async(bmp)
        out = []
        for line in asyncio.run(recognize()).lines:
            bs = [wd.bounding_rect for wd in line.words]
            box = (min(b.x for b in bs), min(b.y for b in bs), max(b.x + b.width for b in bs),
                   max(b.y + b.height for b in bs)) if bs else (0, 0, 0, 0)
            out.append((line.text, box, 1.0))
        return out
    except Exception:
        return None


_rapid = None                       # RapidOCR's engine once made (False: it isn't installed)
_rapid_lock = threading.Lock()
RAPID_LEAST = 0.5                   # a line RapidOCR is less sure of than this is left out
# how sure RapidOCR was of each reading a stakeout made ({text: its least sure line}): a name it's
# this sure of (SURE_ENOUGH) isn't junked by plain_names' word filter (Spider-Man's "Cingular": no
# well-known title has the word, read at 1.00); those it's less sure of are judged ("22", 0.57)
_sure = {}
_sure_lock = threading.Lock()
SURE_ENOUGH = 0.97
SIBLING_SURE = 0.9      # an extra's name this sure stands beside names on its page read well (plain_names)


def _note_sure(text, sure):
    with _sure_lock:
        _sure[text] = max(sure, _sure.get(text, 0.0))


def readings_begin():
    """A stakeout starts: how sure of each reading (_note_sure), this one's only. A DVD's and a Blu-ray's."""
    with _sure_lock:
        _sure.clear()


def readings_sure():
    """{reading: how sure RapidOCR was of it, the surest time}, this stakeout's so far."""
    with _sure_lock:
        return dict(_sure)


def rapid_text(image):
    """What RapidOCR (PaddleOCR's models, on onnxruntime) reads in a picture: its lines, top to
    bottom, joined; None when it can't be had (its runtime needs Python 3.14 or older for now: a
    packaged release brings its own; or its models couldn't be fetched). Tuned on menus
    (temp/fair_ocr_bench.py): the picture as it is (colour, its own size: enlarged it reads junk
    into the margins), text found first (PP-OCRv6 small) without scaling small pictures up (its
    default, 736 px at least: 6-9 times slower), read by the English PP-OCRv5 model. Against your
    names for 135 buttons: 95 read with no wrong word (Windows OCR then Tesseract: 85), most
    of the poster art too ("That's How You Know", "Nathaniel's Revelation", "TinkerBell")."""
    lines = rapid_lines(image)
    if lines is None:
        return None
    return " ".join(t for t, _, _ in sorted(lines, key=lambda x: (x[1][1], x[1][0])))


def rapid_lines(image, least=None, corners=None):
    """rapid_text's lines it's sure of (least: RAPID_LEAST): [(text, (x0, y0, x1, y1), how sure)], or
    None when RapidOCR isn't installed. corners: a list given gets the same lines with their four
    corners as RapidOCR found them ((x, y) top left, top right, bottom right, bottom left): a line's
    slant (_keystone)."""
    global _rapid
    with _rapid_lock:
        if _rapid is None:
            try:
                import logging
                from rapidocr import LangDet, LangRec, ModelType, OCRVersion, RapidOCR
                logging.getLogger("RapidOCR").setLevel(logging.ERROR)
                _rapid = RapidOCR(params={
                    "Det.ocr_version": OCRVersion.PPOCRV6, "Det.lang_type": LangDet.EN,
                    "Det.model_type": ModelType.SMALL, "Det.limit_type": "max", "Det.limit_side_len": 960,
                    "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.EN,
                    "Rec.model_type": ModelType.MOBILE})
            except Exception:                   # (not installed, or its models can't be had)
                _rapid = False
        if not _rapid:
            return None
        try:
            import numpy as np
            got = _rapid(np.array(image.convert("RGB")), use_det=True, use_cls=False)
        except Exception:
            return None
    if got.boxes is None:
        return []
    least = RAPID_LEAST if least is None else least
    if corners is not None:
        corners.extend((t, tuple((float(x), float(y)) for x, y in b[:4]), float(s))
                       for b, t, s in zip(got.boxes, got.txts, got.scores) if s >= least)
    return [(t, (float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 0].max()), float(b[:, 1].max())), float(s))
            for b, t, s in zip(got.boxes, got.txts, got.scores) if s >= least]


_pages_read = {}                    # {(id, size): a page's rapid_lines}: each picture read once
_pages_lock = threading.Lock()


def _page_lines(image, scale=1):
    """rapid_lines of a whole menu picture, read once for all its buttons (None: no RapidOCR);
    scale: read that many times the size (small lettering), the boxes in the picture's pixels."""
    key = (id(image), image.size, scale)
    with _pages_lock:                       # (one reading of a page, its buttons' threads waiting for it)
        if key not in _pages_read:
            if len(_pages_read) > 8:        # (a stakeout's last few pictures)
                _pages_read.clear()
            corners = []
            if scale == 1:
                lines = rapid_lines(image, corners=corners)
            else:
                from PIL import Image
                lines = rapid_lines(image.resize((image.width * scale, image.height * scale), Image.LANCZOS))
                lines = None if lines is None else [(t, tuple(v / scale for v in b), s) for t, b, s in lines]
            _pages_read[key] = (image, lines, corners)   # (the picture kept: its id isn't reused)
        return _pages_read[key][1]


def _page_corners(image):
    """_page_lines' lines (its own size) with their four corners (rapid_lines' corners)."""
    _page_lines(image)
    with _pages_lock:
        got = _pages_read.get((id(image), image.size, 1))
    return got[2] if got else []


# A backup only (_second_readings' last): a page wrongly taken for one costs a reading of its weak buttons, kept
# only if surer, so these needn't be strict (205 pages: the five Oscorp ones, and some with tilted lettering).
KEYSTONE_SLANTS = 3.0       # degrees: a page's lines' slants differing this much, running to one point, are a
                            # picture of a screen at an angle (Spider-Man's Oscorp pages: 9-11, +2 at the top, -8 below)
KEYSTONE_MISS = 1.5         # degrees: a line further off the slant that point gives it isn't on that screen (title art)
KEYSTONE_LINES = 4          # lines on it, at least
KEYSTONE_FAR = 1.0          # page widths from its middle, the point at least (Oscorp: 2.1-2.4): well off the page
KEYSTONE_WIDE = 30          # pixels: a line narrower has too rough a slant to judge by


def _keystone(image):
    """Where a page's lines run to, if it's a picture of a screen at an angle (Spider-Man's Oscorp pages: a
    monitor seen from its left; their text slants more the lower it is): (x, y) in the picture's pixels, out to
    its left or right, else None. From the slants of the lines it reads well (_page_corners): the point nearest
    every line's own run (least squares), fitted again without the lines off it (a tilted title's art), their
    slants differing down the page."""
    import math
    use = []
    for _, c, sure in _page_corners(image):
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = c
        w = math.hypot(x1 - x0, y1 - y0)
        if sure < READ_LEAST or w < KEYSTONE_WIDE:
            continue
        a = math.atan2((y1 - y0) + (y2 - y3), (x1 - x0) + (x2 - x3))
        use.append(((x0 + x1 + x2 + x3) / 4, (y0 + y1 + y2 + y3) / 4, a, w))

    def fit(lines):                         # each line: n . V = n . c, n its normal; weighted by its width
        sxx = sxy = syy = bx = by = 0.0
        for cx, cy, a, w in lines:
            nx, ny = -math.sin(a), math.cos(a)
            d = nx * cx + ny * cy
            sxx, sxy, syy, bx, by = sxx + w * nx * nx, sxy + w * nx * ny, syy + w * ny * ny, bx + w * nx * d, by + w * ny * d
        det = sxx * syy - sxy * sxy
        v = None if abs(det) < 1e-9 else ((bx * syy - by * sxy) / det, (by * sxx - bx * sxy) / det)
        return v if v and abs(v[0] - image.width / 2) >= KEYSTONE_FAR * image.width else None

    slant = lambda v, l: math.atan((v[1] - l[1]) / (v[0] - l[0]))         # (the slant that point gives a line)
    on = lambda v: [l for l in use if abs(math.degrees(slant(v, l) - l[2])) <= KEYSTONE_MISS]
    if len(use) < KEYSTONE_LINES:
        return None
    v = fit(use)
    for _ in range(2):
        if not v or len(on(v)) < KEYSTONE_LINES:
            return None
        v = fit(on(v))
    kept = on(v) if v else []
    if len(kept) < KEYSTONE_LINES:
        return None
    seen = [math.degrees(slant(v, l)) for l in kept]
    return v if max(seen) - min(seen) >= KEYSTONE_SLANTS else None


def _flattened(image, box, point, scale=2):
    """A box of a page seen at an angle (_keystone's point), made level and square on: its words' lines
    straight across and as big on its far side as its near one, at scale times its size (smaller, they read
    worse; bigger, and sharpening or blurring after, did no better: Spider-Man's Oscorp pages). And where a
    spot in it is on the page. (A screen's sides are taken to be upright: so they are in a picture of one.)"""
    from PIL import Image
    vx, vy = point
    # the page to level: (x, y) to (x, y - x vy/vx) / (1 - x/vx), the point sent off to the far right or left
    def to_flat(x, y):
        w = 1 - x / vx
        return x / w, (y - x * vy / vx) / w
    pts = [to_flat(x, y) for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]
    X0, Y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    X1, Y1 = max(p[0] for p in pts), max(p[1] for p in pts)
    if X1 <= X0 or Y1 <= Y0:
        return None, None
    k = scale * ((box[2] - box[0]) * (box[3] - box[1]) / ((X1 - X0) * (Y1 - Y0))) ** 0.5
    # back: the levelled spot (X0 + u/k, Y0 + v/k) of the new picture's (u, v), on the page: X = x/w, Y = (y - x vy/vx)/w
    # with w = 1 - x/vx gives x = X / (1 + X/vx), y = (Y + X vy/vx) / (1 + X/vx)
    a, b = 1 / k, X0
    c, d = 1 / k, Y0
    out = (a, 0, b, vy / vx * a, c, vy / vx * b + d, a / vx, 0, b / vx + 1)       # rows of the 3x3 matrix
    out = tuple(v / out[8] for v in out)
    size = (max(1, int((X1 - X0) * k)), max(1, int((Y1 - Y0) * k)))
    pic = image.transform(size, Image.PERSPECTIVE, out[:8], Image.BICUBIC)

    def at_page(u, v):
        w = out[6] * u + out[7] * v + out[8]
        return (out[0] * u + out[1] * v + out[2]) / w, (out[3] * u + out[4] * v + out[5]) / w
    return pic, at_page


def _heading_boxes(image, rect, others, frame_size):
    """Where a page's heading dips into a button (boxes in the picture's pixels): a line of the
    whole page (RapidOCR's, _page_lines) straddling the button's edge (in it by a sixth to four
    fifths of its height, and more in it than in any other), stacked right on a line outside
    every button, no line between them: Enchanted's "Fantasy / Comes To Life" over its "Happy
    Working Song" button, whose area takes in "Comes To Life". (A line well inside a button is
    the button's, right under a heading or not: Spider-Man 3's "The Jane Austen" under
    "PREVIEWS".) [] without RapidOCR or other buttons. Assigning the page's lines to buttons
    outright did no better than reading each button's area: the areas aren't drawn round their
    names closely enough (temp/fair_ocr_bench.py)."""
    lines = _page_lines(image) if others else None
    if not lines:
        return []
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
    rects = [rect] + [r for r in others if tuple(r) != tuple(rect)]
    rects = [(r[0] * sx, r[1] * sy, r[2] * sx, r[3] * sy) for r in rects]
    across = lambda a, b: min(a[2], b[2]) > max(a[0], b[0])
    mid = lambda b: (b[1] + b[3]) / 2

    def share(b, r):
        h, w = max(1.0, b[3] - b[1]), max(1.0, b[2] - b[0])
        down = max(0.0, min(b[3], r[3]) - max(b[1], r[1])) / h
        return down if max(0.0, min(b[2], r[2]) - max(b[0], r[0])) / w >= 0.5 else 0.0
    boxes = [b for _, b, _ in lines]
    shares = [[share(b, r) for r in rects] for b in boxes]
    outside = [b for b, s in zip(boxes, shares) if max(s) < 0.3]

    def continues(b):
        h = b[3] - b[1]
        return any(across(b, o) and
                   -0.6 * min(h, o[3] - o[1]) <= max(b[1] - o[3], o[1] - b[3]) <= 0.8 * min(h, o[3] - o[1]) and
                   not any(x is not b and x is not o and across(x, b) and
                           min(mid(b), mid(o)) < mid(x) < max(mid(b), mid(o)) for x in boxes)
                   for o in outside)
    return [b for b, s in zip(boxes, shares) if 0.15 <= s[0] < 0.8 and s[0] >= max(s) and continues(b)]


WHOLE_LINE_PAST = 8      # px: a page line running this far past a button's area is cut off by it
WHOLE_LINE_CROSS = 6     # px (the disc's): and crossing it this far (not grazing its edge)


def _whole_lines(image, rect, others, frame_size, box):
    """The page's lines (RapidOCR's, _page_lines) a button's area holds only part of: Rush Hour
    3's highlights are streaks beside their names, the area over "PLAY FULLSCREEN VERSION" taking
    in only "ON". A line is the button's when it's mostly in the area's height, crosses it, runs
    past the part of the picture read for it (box, its pixels), and no other button holds more of
    its height (DVD-ROM/ONLINE FEATURES dips into the icon under it); one mostly in two buttons is
    two names read as one line, left to each button's own reading. [] without RapidOCR or the
    page's other buttons (a plate or a cell read on its own)."""
    lines = _page_lines(image) if others else None
    if not lines:
        return []
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
    rects = [rect] + [r for r in others if tuple(r) != tuple(rect)]
    rects = [(r[0] * sx, r[1] * sy, r[2] * sx, r[3] * sy) for r in rects]

    def share(b, r):                         # how much of the line's height is in r, if it crosses r
        if min(b[2], r[2]) - max(b[0], r[0]) < WHOLE_LINE_CROSS * sx:
            return 0.0
        return max(0.0, min(b[3], r[3]) - max(b[1], r[1])) / max(1.0, b[3] - b[1])
    # (an area with lines of its own, whole: one at its edge is a neighbour's, unless it's half in
    # it: WALL-E's Pinocchio, its two lines, and the robot's "ALLOCATE" just over its left edge)
    r0 = rects[0]
    inside = lambda b: b[0] >= r0[0] and b[2] <= r0[2] and min(b[3], r0[3]) - max(b[1], r0[1]) >= 0.5 * (b[3] - b[1])
    has_own = any(inside(b) for _, b, _ in lines)
    out = []
    for t, b, s in lines:
        if not (b[0] < box[0] - WHOLE_LINE_PAST or b[2] > box[2] + WHOLE_LINE_PAST):
            continue                         # (read whole from the area already)
        if has_own and min(b[2], r0[2]) - max(b[0], r0[0]) < 0.5 * (b[2] - b[0]):
            continue
        shares = [share(b, r) for r in rects]
        if shares[0] >= 0.5 and shares[0] >= max(shares) and sum(x >= 0.5 for x in shares) == 1:
            out.append((t, b, s))
    return out


class Words(str):
    """A reading, and where it was read: .where, [x0, y0, x1, y1] (a DVD's: the disc's 720x480; None,
    not known). Handed on with the words, so a title's picture outlines where its name was read, not
    where it's guessed to be: the lines RapidOCR kept, the name paired with a tiny button, its plate,
    its cell; Windows' OCR and Tesseract, which say no more, the area they were given. .others: its
    button's other readings, surest first (name_button's: also_fix puts a misread word right by them)."""
    where = None
    others = ()

    def __new__(cls, text, where=None, others=()):
        s = str.__new__(cls, text)
        s.where = [round(v) for v in where] if where else None
        s.others = list(others or ())
        return s

    def __reduce__(self):
        return (Words, (str(self), self.where, self.others))


def where_read(text):
    """Where a reading was read (Words.where), or None."""
    return list(text.where) if getattr(text, "where", None) else None


def _rapid_button(image, rect, others, frame_size, box, colour):
    """RapidOCR's reading of a button's area (colour: the picture cut at box, its pixels), and
    where the lines it kept are: ("words", (x0, y0, x1, y1) in the picture's pixels), or ("",
    None): nothing read, or no RapidOCR. Sure of each line it keeps (RAPID_LEAST), its reading
    isn't tidied as Tesseract's is ("Blu-ray", "McCoppin", "TinkerBell", "Q&A" look like junk to
    _tidy), just rid of stray symbols; and of a page heading's line dipping into the button's area
    (_heading_boxes: the page's other buttons known)."""
    lines = rapid_lines(colour)
    whole = _whole_lines(image, rect, others, frame_size, box) if lines is not None else []
    if not lines and not whole:
        return "", None
    # (the page read whole only for a button showing two lines or more: a heading's dip is one)
    heads = _heading_boxes(image, rect, others, frame_size) if len(lines or ()) >= 2 else []
    moved = lambda b: (b[0] + box[0], b[1] + box[1], b[2] + box[0], b[3] + box[1])
    over = lambda b, h: min(b[2], h[2]) > max(b[0], h[0]) and min(b[3], h[3]) - max(b[1], h[1]) > 0.5 * (b[3] - b[1])
    kept = [(t, moved(b), s) for t, b, s in lines or () if not any(over(moved(b), h) for h in heads)
            and not any(over(moved(b), w) for _, w, _ in whole)] + whole
    lines = [(t, b) for t, b, _ in kept]
    got = " ".join(re.sub(r"[^\w'&:!?.,()\- ]+", "", " ".join(
        t for t, _ in sorted(lines, key=lambda x: (x[1][1], x[1][0]))).replace("/", " ")).replace("_", " ").split())
    if not re.search(r"[A-Za-z]{2}", got):
        return "", None
    _note_sure(got, min(s for _, _, s in kept))
    return got, (min(b[0] for _, b in lines), min(b[1] for _, b in lines),
                 max(b[2] for _, b in lines), max(b[3] for _, b in lines))


def button_text(image, rect, frame_size=(720, 480), pad=4, others=()):
    """_button_text's reading, as Words: where it was read (RapidOCR's lines kept; Windows' OCR's and
    Tesseract's, the area they read: the button's, and pad round it)."""
    got = _button_text(image, rect, frame_size, pad, others)
    if not got or getattr(got, "where", None):
        return got
    x0, y0, x1, y1 = rect
    return Words(got, [max(0, x0 - pad), max(0, y0 - pad), min(frame_size[0], x1 + pad), min(frame_size[1], y1 + pad)])


def _button_text(image, rect, frame_size=(720, 480), pad=4, others=()):
    """What's written in a button's rectangle of a menu picture (a PIL image), or "". The
    rectangle is in the disc's 720x480 coordinates; the picture may be any size. The best reader
    there is first, its reading standing unless it has no words in it: RapidOCR (rapid_text),
    then Windows' own OCR (windows_text), then Tesseract: menu lettering is fancy, so it's read several ways and the reading that
    looks most like words wins: grey, black and white, and the bright coloured letters lifted
    off the background (yellow 3D letters over a starfield: Muppets from Space); each as one line
    and as a block, a name over two lines joined back up; and, read badly every way, tilted a
    little each way (slanted lettering). (The two together, against your names for 135 buttons:
    78 read with no wrong word, Tesseract alone 65.)"""
    from PIL import Image, ImageChops, ImageOps, ImageStat
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
    x0, y0, x1, y1 = rect
    box = (max(0, int((x0 - pad) * sx)), max(0, int((y0 - pad) * sy)),
           min(image.width, int((x1 + pad) * sx)), min(image.height, int((y1 + pad) * sy)))
    colour = image.crop(box).convert("RGB")
    if colour.width and colour.height:
        got, where = _rapid_button(image, rect, others, frame_size, box, colour)     # (RapidOCR first)
        if got:
            return Words(got, (where[0] / sx, where[1] / sy, where[2] / sx, where[3] / sy))
        if rapid_lines(colour, least=0.0) == []:
            # RapidOCR finds no text there at all, sure or not: none to read. The others would
            # make words of the backdrop (Spider-Man's title 28, a dark shirt: "oath nena Lan A
            # Arid art FAST"). Text it found but couldn't read surely, they have their turn
            return ""
        big = lambda g: g.resize((g.width * 3, g.height * 3), Image.LANCZOS)
        shown = [big(colour)]
        if WORDS:                               # (a dictionary to judge by: the lettering lifted out too)
            shown.append(big(ImageOps.grayscale(colour).point(lambda v: 0 if v > BRIGHT else 255)))
        wins = []
        for img in shown:
            got = windows_text(img)
            if got is None:
                break
            wins.append(_tidy(re.sub(r"[^\w'&:!?.,()\- ]+", "", got).replace("_", " ").strip()))
        win = max(wins, key=lambda s: (_real(s), _score(s)), default="")
        if _score(win) > 0:
            return win
    if not tools.which("tesseract"):
        return ""
    grey = ImageOps.autocontrast(ImageOps.grayscale(colour), cutoff=1)
    # menu lettering is light on dark as often as dark on light: tesseract wants dark on light
    if ImageStat.Stat(grey).mean[0] < 128:
        grey = ImageOps.invert(grey)
    _, s, v = colour.convert("HSV").split()
    lifted = ImageOps.invert(ImageOps.autocontrast(ImageChops.multiply(s, v), cutoff=1))   # bright and coloured
    big = lambda g: g.resize((g.width * 3, g.height * 3), Image.LANCZOS)
    bw = lambda g: big(g).point(lambda x: 255 if x > 128 else 0)
    tmp = _tmp_png()
    lines = []

    def read(img, psms):
        img.save(tmp)
        for psm in psms:
            out = subprocess.run([tools.which("tesseract"), tmp, "stdout", "--psm", psm], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=20, **_no_console()).stdout
            got = [_tidy(re.sub(r"[^\w'&:!?.,()\- ]+", "", s).replace("_", " ").strip()) for s in out.splitlines()]
            got = [g for g in got if g]
            lines.extend(got)
            good = [g for g in got if _score(g) >= 6]
            if len(good) > 1:                   # a name on two lines
                lines.append(" ".join(good))

    def well_read():
        best = max(lines, key=_score, default="")
        return _score(best) >= WELL_READ or any(w in MENU_VOCAB for w in re.findall(r"[a-z]+", best.lower()))
    try:
        for img, psms in ((big(grey), "76"), (bw(grey), "76"), (bw(lifted), "76")):
            read(img, psms)
        if not well_read():
            for angle in (-10, 10):
                for g in (grey, lifted):
                    read(bw(g.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)), "6")
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    best = max(lines, key=_score, default="")
    return best if _score(best) > 0 else ""


TINY = 40               # a button narrower than this (pixels) is an icon, its words printed round it
BESIDE, ABOVE = 220, 70  # how far from a tiny button its label may reach: sideways, up or down


def _cell(rect, others, frame_size=(720, 480)):
    """The area round a button that's its own: in each direction, up to halfway to the nearest
    button that way (BESIDE sideways, ABOVE up or down at most)."""
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left, right = max(0, x0 - BESIDE), min(frame_size[0], x1 + BESIDE)
    top, bottom = max(0, y0 - ABOVE), min(frame_size[1], y1 + ABOVE)
    for r in others:
        if tuple(r) == tuple(rect):
            continue
        ox, oy = (r[0] + r[2]) / 2 - cx, (r[1] + r[3]) / 2 - cy
        if abs(ox) >= abs(oy):                  # beside it
            if ox > 0:
                right = min(right, max(x1, (x1 + r[0]) // 2))
            else:
                left = max(left, min(x0, (r[2] + x0) // 2))
        elif oy > 0:                            # below it
            bottom = min(bottom, max(y1, (y1 + r[1]) // 2))
        else:
            top = max(top, min(y0, (r[3] + y0) // 2))
    return [int(left), int(top), int(right), int(bottom)]


TINY_REACH = 60         # px: a name this near a tiny button can be its label (the disc's 720x480)


NAME_PITCH = 0.9        # a name's next line starts within this much of the page's distance from button to button


def _button_pitch(rects):
    """How far apart a page's list of tiny buttons sit, top to top (the disc's coordinates): the middle of
    the distances from each to the next below it in its column; None for fewer than three in a column, as
    nothing's to be told from them. The page's own measure of where one name stops and the next starts."""
    tiny = [r for r in rects if r[2] - r[0] < TINY]
    centre = lambda r: (r[0] + r[2]) / 2
    steps = []
    for r in tiny:
        below = [o[1] - r[1] for o in tiny if o[1] > r[1] and abs(centre(o) - centre(r)) < TINY]
        if below:
            steps.append(min(below))
    if len(steps) < 2:
        return None
    import statistics
    return statistics.median(steps)


LIST_ITEM_RE = re.compile(r"^\W*\d{1,2}\s*[).]\s+\S")


def _blocks(lines, pitch=None):
    """A page's lines (rapid_lines) as names: lines stacked right on one another, overlapping side
    to side, one name ("Blockbuster / Countdown / Thursday"): [(text, (x0, y0, x1, y1), how sure:
    its least sure line)]. pitch: how far apart the page's buttons sit, in the lines' pixels
    (_button_pitch): lines this far apart are two names, however close (3 Ninjas: Knuckle Up's trailers,
    a list set tight, one of them three lines long). How near any two lines may be stays the limit."""
    blocks = []
    for t, b, sure in sorted(lines, key=lambda x: x[1][1]):
        h = b[3] - b[1]
        # (a numbered list's item starts a name of its own, however close: Chuck's "1) Chuck Versus the
        # Pink Slip", "3) Chuck Versus the Angel de la Muerte", a line apart)
        for blk in ([] if LIST_ITEM_RE.match(t) else blocks):
            o = blk[-1][1]
            low = min(h, o[3] - o[1])
            # (overlapping by half the narrower at least: "Review #1" over a MARKETING panel isn't one)
            over = min(b[2], o[2]) - max(b[0], o[0])
            if over >= 0.5 * min(b[2] - b[0], o[2] - o[0]) and -0.6 * low <= b[1] - o[3] <= 0.8 * low and \
                    not (pitch and b[1] - o[1] >= NAME_PITCH * pitch):
                blk.append((t, b, sure))
                break
        else:
            blocks.append([(t, b, sure)])
    return [(" ".join(t for t, _, _ in blk), (min(b[0] for _, b, _ in blk), min(b[1] for _, b, _ in blk),
                                            max(b[2] for _, b, _ in blk), max(b[3] for _, b, _ in blk)),
             min(x for _, _, x in blk)) for blk in blocks]


def _tiny_names(image, rects, frame_size=(720, 480), lines=None, empty=None):
    """{tiny button's rect: (its name, where the name is: a box in the disc's coordinates, how sure)}, off the whole page (RapidOCR, _page_lines, read at twice
    the size: such pages' names are small, Spider-Man's TV Spots' 15 px, "hread" for Thread at
    their own size): each tiny button (narrower than TINY: a lamp, a pointer, a sliver at a
    thumbnail's corner) paired with a name (_blocks). The page's layout first: where its names
    sit by their buttons, the middle of the nearest pairs' offsets (under a thumbnail, left of its
    sliver); then each button gets the name nearest that spot, within TINY_REACH, a name to one
    button only. A name on a wider button is that one's. {} without RapidOCR. lines: the page read
    already, in the image's pixels (a Blu-ray's, read whole at its own size: bdhdmv), not read again.
    empty: which of rects hold no words of their own, where that's known (a Blu-ray's, its buttons'
    boxes against the page's lines: Chuck's episode markers, 55 pixels wide), in place of TINY's
    guess by width (a DVD's)."""
    lines = _page_lines(image, 2) if lines is None else lines
    if not lines:
        return {}
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
    pitch = _button_pitch(rects)
    blocks = [(t, (b[0] / sx, b[1] / sy, b[2] / sx, b[3] / sy), sure) for t, b, sure in _blocks(lines, pitch and pitch * sy)]
    mid = lambda b: ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    inside = lambda b, r: r[0] <= mid(b)[0] <= r[2] and r[1] <= mid(b)[1] <= r[3]
    # (a list of names goes by where each starts: their lengths move their middles, a long one's far
    # from its button: Knuckle Up's Roughnecks, three lines. Thumbnails' names go by their middles)
    spot_of = (lambda b: (b[0], b[1])) if pitch else mid
    small = (lambda r: tuple(r) in {tuple(x) for x in empty}) if empty is not None else (lambda r: r[2] - r[0] < TINY)
    tiny = [tuple(r) for r in rects if small(r)]
    wide = [r for r in rects if not small(r)]
    blocks = [x for x in blocks if not any(inside(x[1], r) for r in wide)]

    def pair(cost):
        pairs = sorted((cost(i, k), i, k) for i in range(len(tiny)) for k in range(len(blocks))
                       if cost(i, k) <= TINY_REACH)
        got, used = {}, set()
        for c, i, k in pairs:
            if i not in got and k not in used:
                used.add(k)
                got[i] = k
        return got
    first = pair(lambda i, k: _gap(tiny[i], blocks[k][1]))           # nearest first: the layout
    if not first:
        return {}
    import statistics
    dx = statistics.median(spot_of(blocks[k][1])[0] - spot_of(tiny[i])[0] for i, k in first.items())
    dy = statistics.median(spot_of(blocks[k][1])[1] - spot_of(tiny[i])[1] for i, k in first.items())
    spot = lambda i, k: ((spot_of(blocks[k][1])[0] - spot_of(tiny[i])[0] - dx) ** 2 +
                         (spot_of(blocks[k][1])[1] - spot_of(tiny[i])[1] - dy) ** 2) ** 0.5
    return {tiny[i]: blocks[k] for i, k in pair(spot).items()}


def _tiny_labels(image, rects, frame_size=(720, 480)):
    """_tiny_names' names alone: {tiny button's rect: its name} (how sure of each: noted, _note_sure)."""
    found = _tiny_names(image, rects, frame_size)
    for t, _, sure in found.values():
        _note_sure(t, sure)
    return {r: Words(t, box) for r, (t, box, _) in found.items()}


def button_words(image, rect, others=(), frame_size=(720, 480)):
    """A DVD button's words: name_button's, off its page's picture (VLC's frame). Without RapidOCR,
    _button_words_ocr's (Windows' OCR, Tesseract)."""
    if rapid_missing():
        return _button_words_ocr(image, rect, others, frame_size)
    got, _, also = name_button(image, rect, list(others) or [list(rect)], frame_size, is_word=WORDS)
    return Words(got, where_read(got), also) if also else got


NAME_SHARE = 0.75               # no highlight to go by: a button's name is its lines in lettering this share of its tallest
LIT_SHARE = 0.05                # a line the highlight changes: this share of its box's pixels, at least


def name_lines(lines, lit=None):
    """Of a button's lines, its name's, top to bottom. A button can carry a heading over its name and a
    description under it (Order of the Phoenix's "Additional Footage" over "Additional Scenes"; "The
    Hidden Secrets of Harry Potter", then "Take a thrilling look back through the previous movies...":
    "previous" read as an ad's "previews"). Its name is what its highlight recolours: the lines that change
    between its normal and selected pictures (lit: a mask of where they differ, white, in the lines'
    pixels). Without one, its tallest lettering (The Sorcerer's Apprentice's "The Making Of / The
    Sorcerer's Apprentice", both)."""
    if lit is not None:
        changed = [l for l in lines if _share(lit, l[1]) >= LIT_SHARE]
        if changed:
            return [t for t, _, _ in reading_order(changed)]
    tallest = max((b[3] - b[1] for _, b, _ in lines), default=0)
    keep = [l for l in lines if l[1][3] - l[1][1] >= tallest * NAME_SHARE]
    return [t for t, _, _ in reading_order(keep)]


def reading_order(lines):
    """Lines as they're read: row by row, top to bottom, and in a row left to right. A row: lines level with
    each other, their middles within the row's height, however slanted (Sorcerer's Stone's yearbook, "Harry
    Potter" tilted up, RapidOCR's "Potter" a little higher than its "Harry": not "Potter Harry")."""
    rows = []
    for l in sorted(lines, key=lambda l: (l[1][1] + l[1][3]) / 2):
        mid = (l[1][1] + l[1][3]) / 2
        row = rows[-1] if rows else None
        if row and row["top"] <= mid <= row["bottom"]:
            row["lines"].append(l)
            row["top"], row["bottom"] = min(row["top"], l[1][1]), max(row["bottom"], l[1][3])
        else:
            rows.append({"top": l[1][1], "bottom": l[1][3], "lines": [l]})
    return [l for row in rows for l in sorted(row["lines"], key=lambda l: l[1][0])]


def _share(mask, box):
    """The share of a box's pixels that are white in a mask."""
    x0, y0, x1, y1 = (max(0, int(round(v))) for v in box)
    crop = mask.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
    return crop.histogram()[255] / max(1, crop.width * crop.height)


def read_ways(img, lit=None):
    """The ways to read a button's picture on its own, in turn: as it is, its tones inverted, then each at
    twice its size (a size up isn't always better: Order of the Phoenix's "Grawp" read "GRAWPUHE" at
    twice). (picture, its highlight mask the same size, or None)"""
    from PIL import Image, ImageOps
    for scale in (1, 2):
        im, mask = img, lit
        if scale > 1:
            im = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
            mask = lit.resize(im.size) if lit is not None else None
        yield im, mask
        yield ImageOps.invert(im.convert("L")).convert("RGB"), mask


def read_alone(img, lit=None, at=(0, 0), scale=(1.0, 1.0)):
    """A button's picture read on its own (read_ways), a way at a time, while not READ_CONFIDENT: each
    reading [(Words, how sure)], its name's lines (name_lines). Where: at (the picture's place, in the
    page's pixels) and scale (the page's pixels to the frame's: where's in the frame's coordinates)."""
    out = []
    for im, mask in read_ways(img, lit):
        lines = rapid_lines(im) or []
        names = name_lines(lines, mask)
        kept = [b for t, b, _ in lines if t in names]
        if not kept:
            continue
        k = im.width / img.width
        sure = min(c for t, _, c in lines if t in names)
        where = ((at[0] + min(b[0] for b in kept) / k) / scale[0], (at[1] + min(b[1] for b in kept) / k) / scale[1],
                 (at[0] + max(b[2] for b in kept) / k) / scale[0], (at[1] + max(b[3] for b in kept) / k) / scale[1])
        out.append((Words(" ".join(" ".join(names).split()), where), sure))
        if sure >= READ_CONFIDENT:
            break
    return out


def level_name(lit, lines, worded=(), reach=None):
    """The line level with a button's highlight, nearest beside it: (its text, its box, how sure), or
    None. lit: where the highlight shows ([x0, y0, x1, y1]; a DVD's button's box is its highlight, a
    Blu-ray's is where its selected picture differs: Chuck's ">>", in a box as tall as its row and the
    page's heading over it). lines: the page's, read whole ([(text, box, sure)], the same pixels); a
    line in a button that holds words of its own (worded) is that one's; reach: how far beside it, at
    most (None: anywhere on the page). A marker is a line's size: a highlight more than two lines
    tall names nothing beside it this way."""
    x0, y0, x1, y1 = lit
    inside = lambda m, r: r[0] <= m[0] <= r[2] and r[1] <= m[1] <= r[3]
    level = []
    for t, (a, b, c, d), sure in lines:
        mid = ((a + c) / 2, (b + d) / 2)
        # (a marker: a line's size, two at most; a picture button by a description isn't one: Spider-Man's
        # "CGI Bloopers", 164 pixels tall, by the Commentaries page's "Designer and Crew")
        if not _worded(t) or not (y0 <= mid[1] <= y1) or y1 - y0 > 2 * (d - b) or inside(mid, lit) or                 any(inside(mid, r) for r in worded):
            continue
        gap = min(abs(a - x1), abs(x0 - c))
        if reach is None or gap <= reach:
            level.append((gap, t, (a, b, c, d), sure))
    if not level:
        return None
    _, t, box, sure = min(level)
    return t, box, sure


# (a word of digits and letters run together, not an ordinal, 3D, 4K, 1080p, the 1990s, a scene's 38A, S1, "Disc2")
MIXED_RE = re.compile(r"\b(?!\d+(?:st|nd|rd|th|d|k|p|s|[a-z])\b)(?![a-z]{1,4}\d+\b)(?=\w*\d)(?=\w*[a-z])[a-z\d]{2,}\b", re.I)


def join_split_words(text, known):
    """A reading's words read in two put back together: two side by side, joined a word (known) that
    either alone isn't ("Nathaniel's Revelat ion": Revelation). Neither of two words is joined ("Up Set")."""
    toks = text.split(" ")
    out, i = [], 0
    while i < len(toks):
        a = toks[i]
        b = toks[i + 1] if i + 1 < len(toks) else None
        pa, pb = re.fullmatch(r"([A-Za-z]+)", a or ""), re.fullmatch(r"([A-Za-z]+)([^A-Za-z]*)", b or "")
        if pa and pb and len(a) >= 2 and not (known(a) and known(pb.group(1))) and known(a + pb.group(1)):
            out.append(a + b)
            i += 2
            continue
        out.append(a)
        i += 1
    return " ".join(out)


def _worded(text):
    return bool(re.search(r"[A-Za-z]{2}", text or ""))


def name_button(image, rect, rects, frame_size=(720, 480), lines=None, lit=None, alone=None, beside=None,
                is_word=None):
    """A button's name off its page's picture, the one way for a DVD's (VLC's frame) and a Blu-ray's (the
    page drawn from its files, bdhdmv): (Words, how sure, the other readings' words, surest first).
    rect, rects: the button's and its page's buttons' boxes in the frame's coordinates (frame_size); the
    picture any size. Its name is, in turn (temp/bench_naming.py, 181 labelled DVD buttons: the DVD's
    way 163 right, the Blu-ray's 164, this 166, every one either got):
      the page's lines (read whole once: lines, the picture's pixels) whose middle is in its box, its
      name's (name_lines: what its highlight changes, lit = (mask, its place in the picture), else its
      tallest lettering: Galavant's "PILOT" over a sliver, The Sorcerer's Apprentice's "The Making Of");
      none in a button too narrow for words (TINY: a pointer, a lamp), the name beside it (_tiny_names,
      the page read at twice the size) or its plate's, if clearly better (Spider-Man's "WHAT WE'RE ALL
      ABOUT by Sum 41");
      none, a line crossing its box and running past it (_whole_lines: Rush Hour 3's highlight streaks);
      still not READ_CONFIDENT (or a word not one, by is_word: "The Mmirror Explosion"), the button read
      alone (alone(): [(Words, sure)], its own picture; else read_alone off this one), the surest kept;
      nothing with letters, the name beside it (beside(): (name, box, sure); else _tiny_names) or its
      plate's (Enchanted's "BLOOPERS", under its box); then Windows' OCR and Tesseract (button_text).
    How sure of it: noted (_note_sure)."""
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
    x0, y0, x1, y1 = rect
    box = (max(0.0, x0 * sx), max(0.0, y0 * sy), min(image.width, x1 * sx), min(image.height, y1 * sy))
    if box[2] <= box[0] or box[3] <= box[1]:
        return Words(""), None, []
    frame = lambda w: (w[0] / sx, w[1] / sy, w[2] / sx, w[3] / sy)
    lines = (_page_lines(image) if lines is None else lines) or []
    mask, off = lit if lit else (None, (0, 0))
    tried = []                          # [(Words, sure)]
    # (a reading's words of four letters or more that aren't words, by is_word: "The Mmirror Explosion")
    # and a word of digits and letters run together ("3.1 0Am" for "3. I Am": Enchanted's), a dictionary or not
    odd = lambda text: len(MIXED_RE.findall(text or "")) + (0 if is_word is None else sum(
        1 for w in re.findall(r"[A-Za-z]+", text or "") if len(w) >= 4 and w.lower() not in EXTRAS_VOCAB and not is_word(w)))

    def take(found, use_lit):
        shifted = [(t, (a - off[0], b - off[1], c - off[0], d - off[1]), s) for t, (a, b, c, d), s in found]
        names = name_lines(shifted, mask if use_lit else None)
        kept = [l for l in found if l[0] in names]
        if not kept:
            return False
        sure = min(s for _, _, s in kept)
        text = " ".join(" ".join(names).split())
        where = (min(b[0] for _, b, _ in kept), min(b[1] for _, b, _ in kept),
                 max(b[2] for _, b, _ in kept), max(b[3] for _, b, _ in kept))
        tried.append((Words(text, frame(where)), sure))
        return sure >= READ_CONFIDENT and _worded(text) and not odd(text)

    def done(got, sure=None):
        if got and is_word is not None:         # (a word read in two: "Nathaniel's Revelat ion")
            joined = join_split_words(str(got), lambda w: w.lower() in EXTRAS_VOCAB or is_word(w))
            if joined != str(got):
                got = Words(joined, where_read(got))
        others = [str(t) for t, _ in sorted(tried, key=lambda x: -x[1]) if t and str(t) != str(got)]
        if got and sure is not None:
            _note_sure(str(got), sure)
        return got, sure, list(dict.fromkeys(others))

    def plate():
        i = [tuple(r) for r in rects].index(tuple(rect)) if tuple(rect) in [tuple(r) for r in rects] else None
        return plate_words(image, rects, frame_size, only=[i]).get(i) if i is not None else None

    def near():
        if beside is not None:
            return beside()
        holds = lambda r: any(r[0] * sx <= (a + c) / 2 <= r[2] * sx and r[1] * sy <= (b + d) / 2 <= r[3] * sy
                              for _, (a, b, c, d), _ in lines)
        empty = [list(r) for r in rects if not holds(r)]
        if list(rect) not in empty:
            return None
        # (the page's layout first, names paired with the buttons by where they sit, the page read at twice
        # the size: a name over two lines whole, Spider-Man's "HERO" by Chad Kroeger; then the line level
        # with its highlight, a DVD's button's box: temp/bench_naming.py, first it lost that one)
        hit = _tiny_names(image, [list(r) for r in rects], frame_size, lines=_page_lines(image, 2), empty=empty).get(tuple(rect))
        if hit:
            return hit
        worded = [(r[0] * sx, r[1] * sy, r[2] * sx, r[3] * sy) for r in rects if list(r) not in empty]
        hit = level_name(box, lines, worded, BESIDE * sx)
        return (hit[0], frame(hit[1]), hit[2]) if hit else None
    mid_in = lambda b: box[0] <= (b[0] + b[2]) / 2 <= box[2] and box[1] <= (b[1] + b[3]) / 2 <= box[3]
    mine = [l for l in lines if mid_in(l[1])]
    if len(mine) >= 2:                  # (a page heading's line dipping into its box: the page's, as on a DVD it
        heads = _heading_boxes(image, rect, rects, frame_size)          # always was: Enchanted's "Comes To
        over = lambda b, h: min(b[2], h[2]) > max(b[0], h[0]) and             min(b[3], h[3]) - max(b[1], h[1]) > 0.5 * (b[3] - b[1])    # Life" over "Happy Working Song")
        mine = [l for l in mine if not any(over(l[1], h) for h in heads)] or mine
    if mine and take(mine, True):
        return done(*tried[-1])
    if not mine and x1 - x0 < TINY:     # (a pointer, a lamp: the name beside it, or its plate's)
        hit = near()
        if hit:
            p = plate()
            got = _plate_or(hit[0], p)
            return done(got if got is p else Words(hit[0], hit[1]), hit[2] if got is not p else None)
    if not mine:
        pad = (box[0] - 4 * sx, box[1] - 4 * sy, box[2] + 4 * sx, box[3] + 4 * sy)
        whole = _whole_lines(image, rect, rects, frame_size, pad) if lines else []
        if whole and take(whole, False):
            return done(*tried[-1])
    if alone is not None:
        tried.extend(alone())
    else:
        crop = image.crop(tuple(int(round(v)) for v in box)).convert("RGB")
        cut = mask.crop((int(box[0] - off[0]), int(box[1] - off[1]), int(box[2] - off[0]), int(box[3] - off[1]))) \
            if mask is not None else None
        tried.extend(read_alone(crop, cut, (int(round(box[0])), int(round(box[1]))), (sx, sy)))
    good = [x for x in tried if _worded(x[0])]
    if good:
        # (the fewest words that aren't words; then one holding another's words, all of them: a surer
        # reading of less is a part of the name, as read_again has it, WALL-E's "THE CHRONICLES OF NARNIA:"
        # alone, its page's "... PRINCE CASPIAN" 0.77; then the surest)
        # (what it holds more being words: three letters or more, or a number; a stray mark read off the button's
        # edge isn't: Order of the Phoenix's "H The Centaurs of the Forbidden Forest", "... Decrees m")
        words = lambda t: re.findall(r"[a-z0-9]+", str(t).lower())
        more = lambda x, y: set(words(x[0])) - set(words(y[0]))
        holds = lambda x: sum(1 for y in good if y is not x and set(words(y[0])) < set(words(x[0]))
                              and all(len(w) >= 3 or w.isdigit() for w in more(x, y)))
        return done(*min(good, key=lambda x: (odd(x[0]), -holds(x), -x[1])))
    hit = near()
    if hit:
        return done(Words(hit[0], hit[1]), hit[2])
    p = plate()
    if p:
        return done(p)
    return done(button_text(image, rect, frame_size))      # (Windows' OCR, Tesseract: the last word)


def _button_words_ocr(image, rect, others=(), frame_size=(720, 480)):
    """A button's words without RapidOCR (button_text: Windows' OCR, Tesseract): for a tiny one, a light
    or a pointer by its label (Spider-Man's: lamps 5-16 pixels wide beside "SCENE SELECTIONS"), what's
    printed round it, if that reads better."""
    x0, y0, x1, y1 = rect
    if x1 - x0 < TINY and others:
        label = _tiny_labels(image, others, frame_size).get(tuple(rect))
        if label:
            return label
    text = button_text(image, rect, frame_size, others=others)
    if x1 - x0 >= TINY:
        return text
    cell = _cell(rect, others, frame_size)
    if cell[2] - cell[0] < TINY:
        return text
    got = button_text(image, cell, frame_size, pad=0)
    return got if _score(got) > max(_score(text), 5) else text


PLATE_GRID = 3          # px: plates are found on a grid this coarse
PLATE_REACH = 40        # px: a plate this near a button, and nearer it than any other, is its label
PLATE_WORDS = 10        # _score a plate's reading needs (and 3 more than the button's own) to stand


def plates(image, frame_size=(720, 480)):
    """The plain plates names are printed on (Spider-Man's Trailers: white panels, a pointer for a
    button at each one's edge): light or dark areas, joined across the letters on them (gaps a
    stroke wide; a plate's frame is wider), a name's size. Boxes in the disc's coordinates."""
    from PIL import Image, ImageFilter
    grey = image.convert("L").resize(frame_size)
    out = []
    for keep in (lambda v: 255 if v > 200 else 0, lambda v: 255 if v < 45 else 0):
        m = grey.point(keep).filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(7))
        g = PLATE_GRID
        small = m.resize((m.width // g, m.height // g), Image.BOX).point(lambda x: 255 if x > 200 else 0)
        w, h = small.size
        px = small.load()
        seen = set()
        for y in range(h):
            for x in range(w):
                if not px[x, y] or (x, y) in seen:
                    continue
                todo, n = [(x, y)], 0
                seen.add((x, y))
                x0 = x1 = x
                y0 = y1 = y
                while todo:
                    a, b = todo.pop()
                    n += 1
                    x0, x1, y0, y1 = min(x0, a), max(x1, a), min(y0, b), max(y1, b)
                    for q in ((a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)):
                        if 0 <= q[0] < w and 0 <= q[1] < h and q not in seen and px[q]:
                            seen.add(q)
                            todo.append(q)
                bw, bh = (x1 - x0 + 1) * g, (y1 - y0 + 1) * g
                if 40 <= bw <= 300 and 15 <= bh <= 120 and n * g * g > 0.5 * bw * bh:
                    out.append([x0 * g, y0 * g, (x1 + 1) * g, (y1 + 1) * g])
    return out


def _gap(r, q):
    dx = max(0, max(r[0], q[0]) - min(r[2], q[2]))
    dy = max(0, max(r[1], q[1]) - min(r[3], q[3]))
    return (dx * dx + dy * dy) ** 0.5


def plate_words(image, rects, frame_size=(720, 480), only=None):
    """{button index: what the plate nearest it says}, for buttons with a plate of their own: one
    within PLATE_REACH, nearer them than any other button. A button whose rectangle is a pointer
    or a lamp by its name reads the whole name this way (button_words's cell reaches only halfway
    to the next button)."""
    found = plates(image, frame_size)
    out = {}
    for i, r in enumerate(rects):
        if only is not None and i not in only:
            continue
        mine = [k for k in found if _gap(r, k) < PLATE_REACH and
                min(range(len(rects)), key=lambda j: _gap(rects[j], k)) == i]
        if mine:
            out[i] = button_text(image, min(mine, key=lambda k: _gap(r, k)), frame_size, pad=0)
    return out


def _plate_or(reading, plate):
    """A button's reading, or its plate's when that reads as words, clearly better (Spider-Man's
    "iene": "STUART LITTLE"). Not words for junk: across the saved discs, only three changed."""
    if plate and _score(plate) >= PLATE_WORDS and _score(plate) >= _score(reading) + 3:
        return plate
    return reading


def _better_reading(moved, still, rect, others=()):
    """The selected button's words, from the frame its highlight had moved off and the one it
    hadn't, whichever reads better: the highlight may not have moved yet (Spider-Man 3's
    "Coming to Blu-ray", white on yellow when lit, read as "Conihg to Biutvay")."""
    return max((button_words(moved, rect, others), button_words(still, rect, others)), key=_score)


def page_text(image, rects, frame_size=(720, 480)):
    """The words on a menu picture outside its buttons (a page's heading: "Sneak Peeks" over
    the ads, in plainer lettering than the button that led there), or "". The buttons are
    blanked first: a main menu's "Sneak Peeks" button isn't its heading. RapidOCR's reading, where
    it's installed (over the saved pages: "SPECIAL FEATURES", "PREVIEWS", "BONUS MATERIAL", where
    Tesseract's were mostly junk, "Kor ie BF Tt Pee lO TURES"); else Windows' OCR's or Tesseract's,
    whichever has more real words (each reads some headings the other can't)."""
    from PIL import Image, ImageDraw
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
    blank = image.convert("RGB")
    draw = ImageDraw.Draw(blank)
    for x0, y0, x1, y1 in rects:
        draw.rectangle((x0 * sx, y0 * sy, x1 * sx, y1 * sy), fill=(128, 128, 128))
    words = lambda s: " ".join(re.findall(r"[A-Za-z][A-Za-z']+", s or ""))
    got = words(rapid_text(blank))
    if got:
        return got
    seen = [words(windows_text(blank.resize((blank.width * 2, blank.height * 2), Image.LANCZOS))),
            words(_tesseract_page(image, rects, sx, sy))]
    return max(seen, key=_clean_share)


def _clean_share(line):
    """(the share of a reading's words that are real ones of 3 letters or more, how many): a
    heading's reading judged clean, not long (Tesseract's junk is full of scraps the dictionary
    knows, "as", "on", "Ss": by count, "is Ss lf LIN Sania ra" beat "BONUS")."""
    ws = line.split()
    real = [w for w in ws if len(w) >= 3 and (w.lower() in EXTRAS_VOCAB or (WORDS is not None and WORDS(w)))]
    return (len(real) / len(ws) if ws else 0.0, len(real))


def _tesseract_page(image, rects, sx, sy):
    """Tesseract's reading of a page with its buttons blanked (page_text's), or ""."""
    from PIL import Image, ImageDraw, ImageOps, ImageStat
    if not tools.which("tesseract"):
        return ""
    grey = ImageOps.autocontrast(ImageOps.grayscale(image.convert("RGB")), cutoff=1)
    if ImageStat.Stat(grey).mean[0] < 128:
        grey = ImageOps.invert(grey)
    draw = ImageDraw.Draw(grey)
    for x0, y0, x1, y1 in rects:
        draw.rectangle((x0 * sx, y0 * sy, x1 * sx, y1 * sy), fill=255)
    grey = grey.resize((grey.width * 2, grey.height * 2), Image.LANCZOS)
    tmp = _tmp_png()
    try:
        grey.save(tmp)
        out = subprocess.run([tools.which("tesseract"), tmp, "stdout", "--psm", "11"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30, **_no_console()).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return out


def _score(line):
    """How likely a reading is right: _wordy, a bonus for each word menus use, and a cost for
    each scrap of a letter or two that isn't a word ("Nv", "oo": OCR of the backdrop)."""
    words = re.findall(r"[a-z]+", line.lower())
    return _wordy(line) + 3 * sum(1 for w in words if w in MENU_VOCAB) - \
        3 * sum(1 for w in words if len(w) <= 2 and w not in SHORT_WORDS)


def _tidy(line):
    """A reading without the specks at its ends (OCR of the lettering's backdrop: "| ~ ac")."""
    toks = line.split()
    while toks and _score(toks[0]) <= 0:
        toks.pop(0)
    while toks and _score(toks[-1]) <= 0:
        toks.pop()
    return " ".join(toks)


def _wordy(line):
    """How much a line of OCR looks like words: letters in real-looking words, less the junk."""
    score = 0
    for tok in line.split():
        core = tok.strip(".,:;()'\"!?-")
        bare = core.replace("'", "")            # ("Nathaniel's": istitle says no, the s a word of its own)
        if len(core) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z'&-]*|\d+(:\d\d)*|&", core) and (
                bare.isupper() or bare.istitle() or bare.islower() or core.isdigit() or ":" in core):
            score += len(core)
        elif re.fullmatch(r"\d+[.,]?|&|[AI]", tok):
            score += 1                      # "1." numbering, "&", "A"
        else:
            score -= 2
    return score


def _tmp_png():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png", prefix="dd_btn_")
    os.close(fd)
    return path


def _no_console():
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


# --------------------------------------------------------------------------
# 3. VLC: the menus' pictures, and pressing their buttons
# --------------------------------------------------------------------------

VLC_DIRS = [r"C:\Program Files\VideoLAN\VLC", r"C:\Program Files (x86)\VideoLAN\VLC",
            "/Applications/VLC.app/Contents/MacOS/lib", "/usr/lib/x86_64-linux-gnu", "/usr/lib"]
ADJUST_ENABLE, ADJUST_CONTRAST, ADJUST_BRIGHTNESS, ADJUST_SATURATION = 0, 1, 2, 4    # libvlc_video_adjust_option_t
NAV = {"activate": 0, "up": 1, "down": 2, "left": 3, "right": 4, "popup": 5}


def find_vlc(setting=None):
    """The folder holding VLC's library (libvlc), from the setting or the usual places, or None."""
    names = ("libvlc.dll", "libvlc.dylib", "libvlc.so.5", "libvlc.so")
    for d in ([setting] if setting else []) + VLC_DIRS:
        if d and os.path.isdir(d) and any(os.path.exists(os.path.join(d, n)) for n in names):
            return d
    return None


def vlc_player(folder):
    """VLC's player program, for the folder holding its library (find_vlc), or None."""
    for f in ("vlc.exe", "VLC", os.path.join("..", "VLC"), "vlc"):
        p = os.path.normpath(os.path.join(folder, f))
        if os.path.isfile(p):
            return p
    return shutil.which("vlc")


_watching = None                    # the VLC window watch_title opened (one at a time)


MENU_LOOK_FRAMES, MENU_LOOK_GAP = 3, 2.0     # frames of the first menu, seconds apart (motion menus)


def menu_phrases(drive, vlc_dir, content_hash=None, log=None, is_word=None):
    """Phrases read off a disc's menus (core.ocr, as cover art is read), to name a disc nothing
    else could: a stakeout's page pictures if one was kept for it (no drive needed), else a look
    at its first menu in VLC, a few frames MENU_LOOK_GAP apart (its menu's video moves). The
    menus' own words go ("PLAY", "WITH ENGLISH SUBTITLES": films are called Play). Rush Hour /
    Rush Hour 2's double feature: a number for a label, no cover art; its menu, both posters:
    "Rush Hour", "TUCKER", "CHAN". [] when there's nothing to read, or no VLC."""
    import tempfile
    from . import core
    pictures, named = [], []
    if content_hash:
        kept = os.path.join(menu_dir(content_hash), "crawl.json")
        try:
            with open(kept, encoding="utf-8") as fh:
                pages = json.load(fh)["pages"]
            pictures = [os.path.join(os.path.dirname(kept), p["image"]) for p in pages
                        if p.get("image") and os.path.exists(os.path.join(os.path.dirname(kept), p["image"]))]
            named = making_of_names(b.get("text") for p in pages for b in p.get("buttons") or [])
        except (OSError, ValueError, KeyError):
            pictures = []
    tmp = tempfile.mkdtemp(prefix="dd_menu_look_")
    try:
        if not pictures and vlc_dir:
            pictures = _first_menu_frames(drive, vlc_dir, tmp, log)
        phrases = list(named)
        for path in pictures[:12]:
            read = core.ocr(path) or []
            read += [ph for ph in strip_phrases(path) if ph.lower() not in (x.lower() for x in read)]
            # (a sentence telling you what to do, and each line of it: "YOU WISH TO VIEW" isn't You Wish!)
            told = [_plain(ph) for ph in read if _instructions(ph)]
            read = [trim_menu_words(ph) for ph in read]
            for ph in (ph for ph in read if ph):
                if ph not in phrases and not _menu_words(ph) and not core.packaging(ph) and                         not any(_plain(ph) in t for t in told):
                    phrases.append(ph)
        return mend_phrases(phrases, is_word)[:12]      # (misread words put right by others' readings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


MAKING_OF_RE = re.compile(r"\b(?:the\s+)?(?:making\s+of|behind\s+the\s+scenes\s+(?:of|on))\s+(.{3,60})$", re.I)


def making_of_names(texts):
    """The film a disc's extras name ("The Making Of The Great Mouse Detective": The Great Mouse
    Detective), off its buttons' readings: its name read whole, where the menu's own lettering
    can read as anything ("SNEAK SET UP", its Sneak Peeks and Set Up buttons: The Set-Up)."""
    out = []
    for t in texts:
        m = MAKING_OF_RE.search((t or "").strip())
        name = m and re.sub(r"[\s:!?.,-]+$", "", m.group(1)).strip()
        if name and re.search(r"[A-Za-z]{2}", name) and name not in out:
            out.append(name)
    return out


def _menu_words(phrase):
    """Whether a phrase is only a menu's own words ("PLAY", "SCENE SELECTION", "WITH ENGLISH
    SUBTITLES"), nothing a film's called."""
    words = re.findall(r"[a-z]+", phrase.lower())
    return bool(words) and all(w in MENU_VOCAB or w in ("with", "english", "spanish", "french", "subtitle",
                                                        "sneak", "peek", "peeks", "fast")
                               for w in words)


MENU_STRIPS = (3, 4)     # a menu picture is also read in this many overlapping strips, top to bottom
# a menu's own words, trimmed off a phrase's ends (a film's name read in one block with the buttons
# under it: "PARDON WSARONG SCENES PRODICTION NOTES LANGUAGES"); misread ones too, by likeness
MENU_EDGE_WORDS = {"play", "menu", "main", "scenes", "scene", "selection", "selections", "languages", "language",
                   "setup", "bonus", "materials", "material", "features", "feature", "special", "production",
                   "notes", "commentary", "trailer", "trailers", "theatrical", "extras", "chapters", "audio",
                   "subtitles", "previews", "credits", "gallery", "resume", "options", "sneak", "peeks", "more", "back", "next"}


def _menu_edge(word):
    import difflib
    w = re.sub(r"[^a-z]", "", word.lower())
    return w in MENU_EDGE_WORDS or (len(w) >= 5 and any(
        difflib.SequenceMatcher(None, w, m).ratio() >= 0.8 for m in MENU_EDGE_WORDS if len(m) >= 5))


def trim_menu_words(phrase):
    """A phrase off a menu, its menu's own words gone from its ends: "PARDON WSARONG SCENES
    PRODICTION NOTES LANGUAGES" -> "PARDON WSARONG"; "Menu WHO DONE IT" -> "WHO DONE IT". ("" when
    that's all it said.) Words between stay: a film's name can have them."""
    words = phrase.split()
    while words and _menu_edge(words[0]):
        words.pop(0)
    while words and _menu_edge(words[-1]):
        words.pop()
    if all(re.sub(r"[^a-z]", "", w.lower()) in ("the", "a", "an", "of", "and", "") for w in words):
        return ""                         # ("The Gallery": nothing of a name left)
    return " ".join(words)


def strip_phrases(path):
    """Phrases of a menu picture read a strip at a time (MENU_STRIPS: each a quarter taller, into
    the strips round it): RapidOCR passes over lettering on a whole page that it reads in a part
    of it (October Collections' "The Ghost and Mr. Chicken", cartoon letters outlined in blue: on
    the page nothing; in strips "The Ghost and", "Mr. Chickeh"). Read twice, the surer reading
    stays; a piece inside a surer line goes. [] without RapidOCR."""
    from PIL import Image
    from . import core
    try:
        with Image.open(path) as im:
            pic = im.convert("RGB")
    except OSError:
        return []
    lines = []
    for n in MENU_STRIPS:
        step = pic.height / n
        for k in range(n):
            y0, y1 = max(0, int((k - 0.25) * step)), min(pic.height, int((k + 1.25) * step))
            got = rapid_lines(pic.crop((0, y0, pic.width, y1)))
            if got is None:
                return []
            lines += [(t, (b[0], b[1] + y0, b[2], b[3] + y0), s) for t, b, s in got]

    return core.ocr_phrases(_surest_lines(lines))


def _surest_lines(lines):
    """Lines read more than once (a picture read whole and in strips): the surer reading of each
    place stays, a piece inside a surer line goes. A longer line round a surer piece stays too: it
    says more ("LOSTIN" round "LOST", "LLASKA" round "A": Lost in Alaska's logo)."""
    def inside(a, b):                     # how much of box a is in box b
        w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        return w * h / max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    kept = []
    letters = lambda t: len(re.sub(r"[^A-Za-z0-9]", "", t))
    for t, b, s in sorted(lines, key=lambda x: -x[2]):
        if not any(inside(b, k[1]) >= 0.6 or (inside(k[1], b) >= 0.6 and letters(t) <= letters(k[0])) for k in kept):
            kept.append((t, b, s))
    return kept


def mend_phrases(phrases, is_word=None, covered=False):
    """A disc's readings put right by one another (is_word: a dictionary, core.word_checker): a
    misread word of one ("KARLOF", in "…MEET THE ILLE BORIS KARLOF") is the like word another
    reading has whole, a real one ("KARLOFF", from "BORIS KARLOFF"). covered: a reading whose words
    are all in a longer one goes, that one saying it whole ("MEET THE", "BORIS KARLOFF"; "MEXICAN"
    in "MEXICAN HLOE ARDE": searched alone it finds The Mexican). Only where the readings are one
    film's (its own pages): on a disc's own menu, "RUSH HOUR" in "RUSH HOUR 2" is another film."""
    import difflib
    bare = lambda w: re.sub(r"[^A-Za-z']", "", w)
    like = lambda a, b: difflib.SequenceMatcher(None, bare(a).lower(), bare(b).lower()).ratio()
    if is_word:
        good = {w for p in phrases for w in p.split() if len(bare(w)) >= 4 and is_word(bare(w))}
        mended = []
        for p in phrases:
            words = []
            for w in p.split():
                if len(bare(w)) >= 4 and not is_word(bare(w)):
                    # (a word of about its length: "KARLOF" is "KARLOFF"; "LOSTIN", two words run
                    # together, isn't "LOST")
                    near = [g for g in good if abs(len(bare(g)) - len(bare(w))) <= 1]
                    alt = max(near, key=lambda g: like(w, g), default=None)
                    if alt and like(w, alt) >= 0.8:
                        w = alt
                words.append(w)
            mended.append(" ".join(words))
        phrases = list(dict.fromkeys(mended))
    if covered:
        inside = lambda p, q: len(q.split()) > len(p.split()) and \
            all(any(like(a, b) >= 0.8 for b in q.split()) for a in p.split())
        phrases = [p for p in phrases if not any(inside(p, q) for q in phrases)]
    return phrases


def _area_phrases(path, rect):
    """The phrases a part of a menu picture holds (rect, the disc's 720x480): its lines, read on
    the whole picture and in strips (strip_phrases), those inside rect. A button's own words when
    reading the button alone finds none: the root menu's Lost in Alaska, its logo read only with
    the page round it ("LOSTIN", "LLASKA")."""
    from PIL import Image
    from . import core
    try:
        with Image.open(path) as im:
            pic = im.convert("RGB")
    except OSError:
        return []
    sx, sy = pic.width / 720, pic.height / 480
    box = (rect[0] * sx, rect[1] * sy, rect[2] * sx, rect[3] * sy)
    lines = list(rapid_lines(pic) or [])
    for n in MENU_STRIPS:
        step = pic.height / n
        for k in range(n):
            y0, y1 = max(0, int((k - 0.25) * step)), min(pic.height, int((k + 1.25) * step))
            lines += [(t, (b[0], b[1] + y0, b[2], b[3] + y0), s) for t, b, s in rapid_lines(pic.crop((0, y0, pic.width, y1))) or []]
    mid_in = lambda b: box[0] <= (b[0] + b[2]) / 2 <= box[2] and box[1] <= (b[1] + b[3]) / 2 <= box[3]
    return core.ocr_phrases(_surest_lines([x for x in lines if mid_in(x[1])]))


def feature_phrases(raw, folder, is_word=None):
    """A disc of several films, each with its own menu (Detect titles: raw, menus.crawl's result;
    folder, its pictures): the phrases read off each film's own pages, its menu and the pages
    reached through it, {"Title 1": [...]}: what names that title, apart from the others (Pardon
    My Sarong's page, Who Done It?'s). {} for a disc of one film."""
    from . import core
    features = (raw.get("found") or {}).get("features") or []
    if len(features) < 2:
        return {}
    pages = {(p["domain"], p["page"], p["program"]): p for p in raw.get("pages") or []}
    film_pages = {tuple(f["page"]): f["title"] for f in features if f.get("page")}
    # each film's menu heading first, read as one line (Meet the Killer, Boris Karloff's: "LOU BUD
    # ABBOTT COSTELLO MEET THE ILLE BORIS KARLOF", where its page's pieces each name nothing)
    out = {title_id(f["title"]): [trim_menu_words(f["name"])] for f in features
           if f.get("name") and trim_menu_words(f["name"])}
    for s, p in pages.items():
        if not p.get("image") or not os.path.exists(os.path.join(folder, p["image"])):
            continue
        # (as _film_of: the pages on its way, the nearest last; the page itself settles a domain)
        chain = [(s[0], r["page"], r["program"]) for r in p.get("route") or []] + [s]
        owners = [film_pages[c] for c in chain if c in film_pages]
        path = os.path.join(folder, p["image"])
        if not owners:
            # the disc's own menu, every film's: what's written on each of its buttons that leads
            # to a film's own page is that film's (Lost in Alaska's logo on the root menu, its own
            # page's unread: "LOSTIN", "LLASKA")
            for b in p.get("buttons") or []:
                # (the film's page reached by pressing this button here: its route's last step says
                # so; "page 1" alone is every film's menu set's first page)
                f = next((t for k, t in film_pages.items() if k in pages and _menu_parent(pages, k) == s
                          and ((pages[k].get("route") or [{}])[-1]).get("button") == b["n"]), None)
                if f is not None:
                    mine = out.setdefault(title_id(f), [])
                    for ph in (trim_menu_words(x) for x in _area_phrases(path, b["rect"])):
                        if ph and ph not in mine and not _menu_words(ph) and not _instructions(ph):
                            mine.append(ph)
            continue
        read = (core.ocr(path) or []) + strip_phrases(path)
        told = [_plain(ph) for ph in read if _instructions(ph)]
        mine = out.setdefault(title_id(owners[-1]), [])
        for ph in (trim_menu_words(ph) for ph in read):
            if ph and ph not in mine and not _menu_words(ph) and not core.packaging(ph) and \
                    not any(_plain(ph) in t for t in told):
                mine.append(ph)
    # (each film's readings put right by one another: a misread word the like real word another
    # reading has; the clean short readings kept beside the long, all of them searched, the best
    # film found winning. A scrap of a word or two letters short names nothing: "Zap", "Lou", "BUD"
    # found the films Zap, Lou, Buddy)
    scrap = lambda p: len(p.split()) == 1 and len(re.sub(r"[^A-Za-z]", "", p)) <= 4
    out = {t: [p for p in mend_phrases(phs, is_word) if not scrap(p)] for t, phs in out.items()}
    # (the words every film's pages have, the disc's own, are set aside where they're searched:
    # core.films_by_title, in the readings and the films' names alike)
    return {t: phs[:10] for t, phs in out.items() if phs}


INSTRUCTION_CUES = {"please", "choose", "select", "press", "wish", "view", "watch", "enter", "remote", "highlight",
                    "button", "arrow", "click", "would", "like", "language", "prefer"}


def _plain(phrase):
    return " ".join(re.findall(r"[a-z0-9]+", phrase.lower()))


def _instructions(phrase):
    """Whether a phrase tells you how to work the menu ("PLEASE CHOOSE THE FEATURE YOU WISH TO
    VIEW", "PRESS ENTER TO PLAY"): two of its words such cues, or "please" and one."""
    cues = [w for w in re.findall(r"[a-z]+", phrase.lower()) if w in INSTRUCTION_CUES]
    return len(cues) >= 2 or ("please" in cues and len(cues) >= 1 and len(phrase.split()) >= 2)


def _first_menu_frames(drive, vlc_dir, out_dir, log=None):
    """Frames of a disc's first menu, as VLC opens it (its warnings and logos waited out, up to
    a minute; a film it plays by itself: VLC's DVD Menu), saved in out_dir: [paths]."""
    v = None
    try:
        v = Vlc(vlc_dir, highlight=True)
        v.open(dvd_mrl(drive))
        t0 = time.time()
        while not v.frames and time.time() - t0 < 30:
            pause(0.25)
        t0 = time.time()
        while v.state()["title"] != 0 and time.time() - t0 < 60:
            pause(0.5)
        if v.state()["title"] != 0:              # (still a title: the film, playing by itself)
            v.menu()
            pause(4)
        pause(3)                            # (the page's intro)
        paths = []
        for i in range(MENU_LOOK_FRAMES):
            path = os.path.join(out_dir, "menu_%d.png" % i)
            v.frame().save(path)
            paths.append(path)
            pause(MENU_LOOK_GAP)
        return paths
    except Exception as e:
        if log:
            log("couldn't look at the menus: %s" % e)
        return []
    finally:
        if v is not None:
            v.close()


def watch_title(folder, source, title):
    """Plays one title of a DVD (a drive or a folder) in VLC, in a window of its own: to see
    what it is. One window: the one opened before is closed first. False if there's no VLC
    player to open."""
    global _watching
    exe = vlc_player(folder) if folder else None
    if not exe:
        return False
    stop_watching()
    _watching = subprocess.Popen([exe, dvd_mrl(source) + "#%d" % title], close_fds=True)
    return True


def stop_watching():
    """Close the VLC window watch_title opened, if it's still open: before the drive is needed
    (a scan, a stakeout, a rip), and as the app closes. Left open, VLC windows reading a disc
    that's been changed can hang, beyond closing, till the next sign-in."""
    global _watching
    p, _watching = _watching, None
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except OSError:
            pass


class _Title(ctypes.Structure):
    _fields_ = [("duration", ctypes.c_int64), ("name", ctypes.c_char_p), ("flags", ctypes.c_uint)]


class Vlc:
    """libvlc through ctypes: plays a disc with no window and keeps its latest decoded frame."""

    W, H = 720, 480

    def __init__(self, folder, highlight=False):
        if os.name == "nt":
            os.add_dll_directory(folder)
            os.environ.setdefault("VLC_PLUGIN_PATH", os.path.join(folder, "plugins"))
            self.lib = ctypes.CDLL(os.path.join(folder, "libvlc.dll"))
        else:
            name = next(n for n in ("libvlc.dylib", "libvlc.so.5", "libvlc.so") if os.path.exists(os.path.join(folder, n)))
            self.lib = ctypes.CDLL(os.path.join(folder, name))
        L = self.lib
        L.libvlc_new.restype = ctypes.c_void_p
        L.libvlc_new.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
        L.libvlc_media_new_location.restype = ctypes.c_void_p
        L.libvlc_media_new_location.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        L.libvlc_media_player_new_from_media.restype = ctypes.c_void_p
        L.libvlc_media_player_new_from_media.argtypes = [ctypes.c_void_p]
        for f in ("play", "stop", "release", "get_title", "get_chapter", "get_state", "get_title_count",
                  "get_chapter_count"):
            getattr(L, "libvlc_media_player_" + f).argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_get_time.restype = ctypes.c_int64
        L.libvlc_media_player_get_time.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_navigate.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        L.libvlc_media_player_set_title.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.libvlc_video_get_spu.argtypes = [ctypes.c_void_p]
        L.libvlc_video_set_spu.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.libvlc_video_set_adjust_int.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
        L.libvlc_video_set_adjust_float.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_float]
        self.is_dark, self.dark_level = False, None
        L.libvlc_media_player_get_full_title_descriptions.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_Title)))]
        L.libvlc_title_descriptions_release.argtypes = [ctypes.POINTER(ctypes.POINTER(_Title)), ctypes.c_uint]
        L.libvlc_video_set_format.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint,
                                              ctypes.c_uint]
        self.LOCK = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self.UNLOCK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self.DISPLAY = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
        L.libvlc_video_set_callbacks.argtypes = [ctypes.c_void_p, self.LOCK, self.UNLOCK, self.DISPLAY, ctypes.c_void_p]
        L.libvlc_media_release.argtypes = [ctypes.c_void_p]
        L.libvlc_release.argtypes = [ctypes.c_void_p]
        # the button highlight is a subpicture: off for clean frames to read, on to see the selection
        # --short-jump-size=0: with no buttons on screen (a warning, a logo, a page whose buttons
        # aren't on yet), VLC turns left / right into 10-second jumps; jumping over the page (The
        # Sorcerer's Apprentice's FastPlay chooser) lands in the ads. (Up / down: the volume, unheard)
        args = [b"--quiet", b"--no-audio", b"--no-video-title-show", b"--no-snapshot-preview", b"--no-osd",
                b"--intf=dummy", b"--short-jump-size=0"] + ([] if highlight else [b"--no-spu"])
        self.inst = L.libvlc_new(len(args), (ctypes.c_char_p * len(args))(*args))
        if not self.inst:
            raise RuntimeError("VLC didn't start (libvlc_new failed)")
        self.buf = ctypes.create_string_buffer(self.W * self.H * 4)
        self.frames = 0
        self.lock = threading.Lock()
        self.mp = None

    def open(self, mrl, options=()):
        """Play mrl; options: VLC's media options (":no-bluray-menu": a Blu-ray's titles, its
        menus not run)."""
        L = self.lib
        media = L.libvlc_media_new_location(self.inst, mrl.encode("utf-8"))
        if options:
            L.libvlc_media_add_option.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            for o in options:
                L.libvlc_media_add_option(media, o.encode("utf-8"))
        self.mp = L.libvlc_media_player_new_from_media(media)
        L.libvlc_media_release(media)

        def lock(opaque, planes):
            self.lock.acquire()
            planes[0] = ctypes.cast(self.buf, ctypes.c_void_p).value
            return None

        def unlock(opaque, picture, planes):
            self.frames += 1
            self.lock.release()

        def display(opaque, picture):
            pass
        self._cb = (self.LOCK(lock), self.UNLOCK(unlock), self.DISPLAY(display))   # kept alive
        L.libvlc_video_set_callbacks(self.mp, *self._cb, None)
        L.libvlc_video_set_format(self.mp, b"RV32", self.W, self.H, self.W * 4)
        L.libvlc_media_player_play(self.mp)

    def frame(self):
        """The latest frame as a PIL image (720x480; the disc's own button coordinates); marked
        dark (info) when taken with the video darkened (dark())."""
        from PIL import Image
        with self.lock:
            raw = bytes(self.buf)
        img = Image.frombuffer("RGBA", (self.W, self.H), raw, "raw", "BGRA", 0, 1).convert("RGB")
        if self.is_dark:
            img.info["dark"], img.info["level"] = True, self.dark_level or 0
        return img

    def settle(self, n=3, limit=1.5):
        """Wait for n more frames to be drawn (a change made just now in them); whether they were."""
        start, t0 = self.frames, time.time()
        while self.frames < start + n and time.time() - t0 < limit:
            pause(0.02)
        return self.frames >= start + n

    def dark(self, on, level=0):
        """The video made one flat grey (VLC's adjust filter: contrast and colour 0, brightness
        for the level: 0 black, 128 mid-grey, 255 white), or back. VLC draws the subpicture
        after its filters, so a menu's highlight stays as it is, over nothing: no moving scenery
        behind it (WALL-E's sweeping lights). Black hides a black highlight (Wallace & Gromit's
        first menu): Crawler._backdrop picks the level. Takes effect at once."""
        if on == self.is_dark and (not on or level == self.dark_level):
            return
        L = self.lib
        if on:
            L.libvlc_video_set_adjust_float(self.mp, ADJUST_CONTRAST, 0.0)
            L.libvlc_video_set_adjust_float(self.mp, ADJUST_SATURATION, 0.0)
            L.libvlc_video_set_adjust_float(self.mp, ADJUST_BRIGHTNESS, level / 128.0)
        L.libvlc_video_set_adjust_int(self.mp, ADJUST_ENABLE, 1 if on else 0)
        self.settle()
        self.is_dark, self.dark_level = on, level if on else None

    def state(self):
        L = self.lib
        return {"title": L.libvlc_media_player_get_title(self.mp), "chapter": L.libvlc_media_player_get_chapter(self.mp),
                "time_ms": L.libvlc_media_player_get_time(self.mp), "frames": self.frames,
                "player_state": L.libvlc_media_player_get_state(self.mp)}

    def titles(self):
        L = self.lib
        arr = ctypes.POINTER(ctypes.POINTER(_Title))()
        n = L.libvlc_media_player_get_full_title_descriptions(self.mp, ctypes.byref(arr))
        out = []
        for i in range(max(0, n)):
            t = arr[i].contents
            out.append({"index": i, "name": (t.name or b"").decode("utf-8", "replace"), "seconds": t.duration / 1000,
                        "menu": bool(t.flags & 1), "interactive": bool(t.flags & 2)})
        if n > 0:
            L.libvlc_title_descriptions_release(arr, n)
        return out

    def press(self, key):
        self.lib.libvlc_media_player_navigate(self.mp, NAV[key])

    def length_ms(self):
        """The length of what's playing (a Blu-ray's title: VLC's list has few of them)."""
        L = self.lib
        L.libvlc_media_player_get_length.restype = ctypes.c_longlong
        L.libvlc_media_player_get_length.argtypes = [ctypes.c_void_p]
        return L.libvlc_media_player_get_length(self.mp)

    def menu(self):
        """VLC's DVD Menu (its menu title): the disc's menu, from a title playing."""
        self.lib.libvlc_media_player_set_title(self.mp, 0)

    def seek(self, ms):
        """To this time in what's playing."""
        self.lib.libvlc_media_player_set_time.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        self.lib.libvlc_media_player_set_time(self.mp, int(ms))

    def bare_frame(self):
        """A frame without the subpicture over the video (a menu's highlight, and whatever else
        the disc draws there), or None. Turned back on after, the subpicture shows again only
        once the disc sends it again: a page's start, or its loop's (WALL-E's: 23 s)."""
        L = self.lib
        spu = L.libvlc_video_get_spu(self.mp)
        if spu < 0:
            return None
        L.libvlc_video_set_spu(self.mp, -1)
        try:
            return self.frame() if self.settle() else None      # (a few frames drawn without it)
        finally:
            L.libvlc_video_set_spu(self.mp, spu)

    def burst(self, n=5, gap=0.08):
        """A few frames in quick succession."""
        out = [self.frame()]
        for _ in range(n - 1):
            pause(gap)
            out.append(self.frame())
        return out

    def close(self):
        if self.mp:
            self.lib.libvlc_media_player_stop(self.mp)
            self.lib.libvlc_media_player_release(self.mp)
            self.mp = None
        if self.inst:
            self.lib.libvlc_release(self.inst)
            self.inst = None


def keeping_test_data():
    """Whether crawls are kept as test data (a developer's copy: run from a git checkout, or env
    DISC_DETECTIVE_TEST_DATA=1; =0 says not). Otherwise they're the session's only."""
    from . import core
    flag = core.env("TEST_DATA")
    if flag is not None:
        return flag.strip().lower() not in ("0", "no", "false", "")
    return os.path.isdir(os.path.join(_checkout(), ".git"))


def _checkout():
    """The folder above the package: a developer's git checkout, when it's one."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_session_dir = None


def menu_dir(content_hash):
    """Where a disc's crawl goes, by its fingerprint (labels repeat across discs; it doesn't).
    Kept as test data: in the checkout's temp/menus (git ignores temp/), out of your case files
    folder. Otherwise in a folder of this session's
    own in the system's temp folder, gone when the session ends (the system may clear it after a
    crash): the pictures of the last stakeout, and no more."""
    global _session_dir
    if keeping_test_data():
        return os.path.join(_checkout(), "temp", "menus", content_hash)
    if _session_dir is None:
        import atexit
        import tempfile
        _session_dir = tempfile.mkdtemp(prefix="disc_detective_menus_")
        atexit.register(end_session)
    return os.path.join(_session_dir, content_hash)


def end_session():
    """This session's crawls (menu_dir), gone."""
    global _session_dir
    if _session_dir:
        shutil.rmtree(_session_dir, ignore_errors=True)
        _session_dir = None


# a disc's menu data (dvd_structure) never changes: read once, kept by fingerprint in your data
# folder, and read back for the next stakeout (WALL-E's 443 pages: two minutes off the drive).
# Bumped whenever dvd_structure gives more or other than before: older copies are read again
STRUCTURE_VERSION = 4              # 4: a PAL disc's buttons on 720x480 frames (_to_frame); 2: when each program's buttons come on (buttons_at); 3: highlight colours


def _structure_file(content_hash, home=None):
    from .core import data_home
    return os.path.join(home or data_home(), "menu_data", content_hash + ".json.gz")


def saved_structure(content_hash, home=None, oldest=None):
    """The disc's menu data as read before (save_structure), or None. oldest: the earliest
    version that will do (the program map needs no highlight colours: 2's do)."""
    import gzip
    try:
        with gzip.open(_structure_file(content_hash, home), "rt", encoding="utf-8") as fh:
            kept = json.load(fh)
    except (OSError, ValueError, EOFError):
        return None
    least = STRUCTURE_VERSION if oldest is None else oldest
    return kept.get("structure") if least <= (kept.get("version") or 0) <= STRUCTURE_VERSION else None


def save_structure(content_hash, structure, home=None):
    import gzip
    path = _structure_file(content_hash, home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"version": STRUCTURE_VERSION, "structure": structure}, fh)
    os.replace(tmp, path)


def keep_disc_data(root, structure, out_dir):
    """Test data, kept with a disc's crawl: its IFO files (the menus' programs; small, never
    encrypted) and its structure (pages, buttons and their commands), in out_dir/disc. With them
    the disc's program (dvdvm) can be run again without the disc: --replay. Nothing in the app
    reads them back."""
    keep = os.path.join(out_dir, "disc")
    os.makedirs(keep, exist_ok=True)
    vt = os.path.join(root, "VIDEO_TS") if os.path.isdir(os.path.join(root, "VIDEO_TS")) else root
    for f in os.listdir(vt):
        if f.upper().endswith(".IFO"):
            shutil.copyfile(os.path.join(vt, f), os.path.join(keep, f.upper()))
    with open(os.path.join(keep, "structure.json"), "w", encoding="utf-8") as fh:
        json.dump(structure, fh)
    return keep


def replay(where, log=print):
    """Run the disc's program (dvdvm) on a crawl's kept test data (keep_disc_data): a
    fingerprint, or the folder. Every page it reaches and what each button does; no disc, no VLC."""
    from . import dvdvm
    keep = where if os.path.isdir(where) else os.path.join(menu_dir(where), "disc")
    if os.path.basename(keep.rstrip("\\/")) != "disc" and os.path.isdir(os.path.join(keep, "disc")):
        keep = os.path.join(keep, "disc")
    with open(os.path.join(keep, "structure.json"), encoding="utf-8") as fh:
        structure = json.load(fh)
    t0 = time.time()
    start, found, reached = dvdvm.explore(dvdvm.Engine(dvdvm.Disc(keep, structure)))
    log("the disc's program: starts on %s; %d pages, %d buttons (%.2fs)" % (
        start.get("goes_to") or start, len(reached), len(found), time.time() - t0))
    for (d, pgc, prog, n), answers in sorted(found.items(), key=lambda kv: (str(kv[0][0]), kv[0][1:])):
        log("  %s page %d%s button %2d: %s" % ("VMG" if d == "vmgm" else "VTS %s" % d, pgc,
                                               " program %d" % prog if prog > 1 else "", n,
                                               "; ".join(_answer_text(a) for a in answers)))
    return start, found


def _answer_text(a):
    if "plays" in a:
        return "plays title %s chapter %s%s" % (a["plays"], a["chapter"],
                                                " after %s" % a["before"] if a.get("before") else "")
    if "goes_to" in a:
        d, pgc, prog = a["goes_to"]
        return "-> %s page %d%s" % ("VMG" if d == "vmgm" else "VTS %s" % d, pgc, " program %d" % prog if prog > 1 else "")
    if a.get("stays"):
        return "stays"
    return "stops (%s)" % a["stops"] if "stops" in a else "unsure (%s)" % a.get("unsure")


def read_structure(root, vlc_dir=None, log=print):
    """dvd_structure(), with the drive unlocked first: a copy-protected disc's drive refuses its
    protected sectors until a player has unlocked it, and each refused read is an error the drive
    logs (dozens in a moment; a sensitive drive can do without them). So on a drive, with VLC
    there, VLC opens the disc for a moment before anything is read; and if the drive still
    refuses (or there was no unlocking first), VLC unlocks it and it's read again."""
    drive = bool(re.fullmatch(r"[A-Za-z]:[\\/]?", root.strip())) or root.startswith("/dev/")
    if vlc_dir and drive:
        unlock(root, vlc_dir)
    try:
        return dvd_structure(root)
    except Refused as e:
        if not vlc_dir:
            raise
        log("%s: opening the disc in VLC to unlock the drive" % e)
    unlock(root, vlc_dir)
    return dvd_structure(root)


def unlock(root, vlc_dir):
    """Have VLC open the disc, until its first picture: the drive serves the disc's protected
    sectors from then on."""
    v = Vlc(vlc_dir)
    try:
        v.open(dvd_mrl(root))
        t0 = time.time()
        while not v.frames and time.time() - t0 < 30:
            pause(0.25)
    finally:
        v.close()


def dvd_mrl(drive):
    """VLC's address for a DVD drive or folder: dvd:///E:/ (the menus play, as on a player)."""
    d = drive.rstrip("\\/")
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", d):
        return "dvd:///%s/" % d
    return "dvd://" + os.path.abspath(drive)


PROBE_RUNS = [                              # (name, keys pressed once the main menu is up)
    ("play", ["up", "activate"]),
    ("scene", ["activate", "right", "activate"]),
    ("bonus", ["down", "activate", "wait", "activate"]),   # wait: let the page's intro reach its buttons
]


def probe(drive, vlc_dir, out_dir, log=print, runs=PROBE_RUNS, menu_wait=15):
    """A first look at how VLC plays this disc's menus, highlight shown: for each run, open the
    disc, wait for the main menu, press the run's keys, and record the frame and VLC's title,
    chapter and time after each, and a few seconds into whatever plays last. Writes probe.json
    and frame PNGs into out_dir. (The groundwork for the crawl: VLC is driven blind, so this
    records what it actually does on a real disc.)"""
    os.makedirs(out_dir, exist_ok=True)
    for f in glob.glob(os.path.join(out_dir, "*.png")):
        os.remove(f)
    mrl = dvd_mrl(drive)
    log("VLC %s: opening %s" % (vlc_dir, mrl))
    titles, out = [], []
    for run, keys in runs:
        v = Vlc(vlc_dir, highlight=True)
        steps = []

        def snap(label, wait):
            pause(wait)
            st = v.state()
            name = "%s_%02d_%s.png" % (run, len(steps), re.sub(r"\W+", "_", label))
            if v.frames:
                v.frame().save(os.path.join(out_dir, name))
            st.update(step=label, image=name if v.frames else None)
            steps.append(st)
            log("  %-6s %-14s title %s chapter %s time %.1fs frames %d" % (
                run, label, st["title"], st["chapter"], st["time_ms"] / 1000, st["frames"]))
        try:
            v.open(mrl)
            t0 = time.time()
            while not v.frames and time.time() - t0 < 30:
                pause(0.25)
            titles = titles or v.titles()
            snap("menu", menu_wait)
            for i, key in enumerate(keys):
                if key != "wait":
                    v.press(key)
                snap("%d_%s" % (i + 1, key), 6 if key == "wait" else 2)
            snap("plus_3s", 3)
            snap("plus_8s", 5)
        finally:
            v.close()
        out.append({"run": run, "keys": keys, "steps": steps})
    result = {"drive": drive, "vlc": vlc_dir, "titles": titles, "runs": out}
    with open(os.path.join(out_dir, "probe.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return result


# --------------------------------------------------------------------------
# 4. The crawl: every button's words, and what it really plays
# --------------------------------------------------------------------------
# A menu "state" is (domain, page, program): domain "vmgm" or a VTS number; a page's programs
# can each have their own buttons (scene pages). VLC is driven blind, so the crawl finds which
# button is selected by pressing a direction and seeing which two button rectangles change.

SEEN = 30                                   # pixels of highlight that count as seeing it
HINT_WAIT = 75                              # seconds to find the page the disc's program starts on
NEAR = 24                                   # pixels round a page's buttons where a stray highlight would show
TITLE_WAIT = 180                            # seconds a title may play before the page a button leads to
WORDED = 6                                  # _score of a button reading that's words (bare frames: words gone?)
MENU_BACK_WAIT = 15                         # seconds to find the start page after DVD Menu (restart)
FILM_LENGTH = 20 * 60                      # seconds: a title this long playing is a film, not waited out


class Lost(Exception):
    """The crawl couldn't tell where it is in the menus."""


class Cancelled(Exception):
    """A crawl called off by the user (its progress or its log raising it, at the next step it tells of: a
    DVD's, a Blu-ray's, from VLC or the disc's files alike). Not Lost: nothing to retry, nothing to fall back to."""


class Unsupported(Lost):
    """A disc whose kind of menus no crawl of ours can work: not tried."""


class NoReader(Lost):
    """Nothing to read the menus with: a Blu-ray's crawl goes by RapidOCR's lines and their places."""


class NoVlc(Lost):
    """No VLC to crawl with, and the menus not read from the disc's files either."""


def rapid_missing():
    """Whether RapidOCR can't read anything here: not installed, or its models couldn't be had."""
    from PIL import Image
    return rapid_lines(Image.new("RGB", (64, 64))) is None


def _pages(structure):
    out = {("vmgm", p["pgc"]): p for p in structure["vmgm"]}
    for vts, pages in structure["vtsm"].items():
        out.update({(int(vts), p["pgc"]): p for p in pages})
    return out


def _waits_for_viewer(pages, st):
    """Whether a page (its program st) holds still until a button is pressed (a cell held for
    good: still 255), rather than playing on, maybe to something else when it ends."""
    p = pages.get(st[:2]) or {}
    starts = p.get("program_cells") or [1]
    cells = p.get("cells") or []
    if not 0 < st[2] <= len(starts):
        return any(c.get("still") == 255 for c in cells)
    end = starts[st[2]] - 1 if st[2] < len(starts) else len(cells)
    return any(c.get("still") == 255 for c in cells[starts[st[2] - 1] - 1:end])


def program_map(root, structure):
    """What a disc's menus do, from its program alone (dvdvm, in memory: no VLC, no presses):
    {"plays": {title: [{"page": [domain, pgc, program], "button", "chapter"}]}, the buttons
    that play each title; "chains": {title: {"after": [...], "into": title}}, what plays on
    after one (a run of shorts, a Play All going on into a film); "chained": the titles only a
    chain reaches (Wallace & Gromit's title 6, its Play All's way into The Wrong Trousers);
    "opening": what a player plays before the menu; "start": the page it opens on}. None when
    the disc's program can't be run. Titles as numbers."""
    from . import dvdvm
    try:
        eng = dvdvm.Engine(dvdvm.Disc(root, structure))
        began, found, _ = dvdvm.explore(eng)
        opening = dvdvm.opening(eng)
    except Exception:                          # (a program it can't follow: nothing known)
        return None
    plays, chains = {}, {}
    for (d, pgc, prog, n), answers in found.items():
        for a in answers:
            if "plays" not in a or not a["plays"]:
                continue
            where = {"page": [d, pgc, prog], "button": n, "chapter": a.get("chapter") or 1}
            if where not in plays.setdefault(a["plays"], []):
                plays[a["plays"]].append(where)
            if a.get("after") or a.get("into"):
                chains.setdefault(a["plays"], {"after": a.get("after") or [], "into": a.get("into")})
    reached = {t for c in chains.values() for t in c["after"] + ([c["into"]] if c["into"] else [])}
    # (buttons it couldn't follow: one of them may play what looks unplayed)
    unsure = sum(1 for answers in found.values() if any("unsure" in a for a in answers))
    return {"plays": plays, "chains": chains, "chained": sorted(reached - set(plays)),
            "opening": opening, "start": list(began.get("goes_to") or []), "unsure": unsure,
            "buttons": len(found)}


def _one_title_by_route(answers):
    """A button the disc's program answers by route (dvdvm.explore: one answer a route) with one
    title from different chapters, the route's choice: what it plays, from the start, as a Play
    reaching it has it ({"title", "chapter": 1}); else None. The Iron Giant's edition chooser,
    reached by Play and by each of its 34 scenes: Signature Edition plays title 1 from chapter 1
    to 34, by route (VLC pressed it to find out, a minute's presses)."""
    if not answers or not all("plays" in x for x in answers) or len({x["plays"] for x in answers}) != 1:
        return None
    chapters = {x.get("chapter") or 1 for x in answers}
    return {"title": answers[0]["plays"], "chapter": 1} if len(chapters) > 1 and 1 in chapters else None


def _lead_in(pages, st):
    """Seconds a page plays before the program whose buttons st is (its intro), as the disc says:
    keys pressed while its buttons are coming on can move the highlight half a round trip."""
    p = pages.get(st[:2]) or {}
    starts = p.get("program_cells") or [1]
    progs = p.get("program_buttons") or [p.get("buttons") or []]
    # up to the first program with buttons (Enchanted's main menu has them from its first: no wait)
    k = next((i for i in range(1, min(st[2], len(starts)) + 1) if i <= len(progs) and progs[i - 1]), st[2])
    first = starts[k - 1] if 0 < k <= len(starts) else 1
    at = p.get("buttons_at") or []             # (and into that program, till its buttons come on)
    return sum(c.get("seconds") or 0 for c in (p.get("cells") or [])[:first - 1]) + \
        (at[k - 1] if 0 < k <= len(at) else 0)


def _state_buttons(pages, st):
    p = pages.get(st[:2])
    progs = (p.get("program_buttons") or [p["buttons"]]) if p else []
    return progs[st[2] - 1] if 0 < st[2] <= len(progs) else []


def _edge(pages, st, b):
    """The menu state a button goes to when the disc says so outright (a page with buttons, or
    another part of this one), else None: then it has to be pressed to see what it does."""
    c = b["cmd"]
    nxt = {"link_page": (st[0], c.get("to"), 1), "link_program": (st[0], st[1], c.get("to"))}.get(c["op"])
    return nxt if nxt and _state_buttons(pages, nxt) else None


def _goes(pages, st, b, learned=None, known=None):
    """The menu state a button goes to: as learned (by the disc's program, which runs the
    commands before a page too, or by pressing it), else as the button says outright, unless the
    program found it plays a title or stays put."""
    k = (st, b["n"])
    if k in (learned or {}):
        return learned[k]
    if (known or {}).get(k, {}).get("engine"):
        return None
    return _edge(pages, st, b)


def _known(structure, pages, st, b):
    """What a button does when the disc says so outright, so it needn't be pressed:
    {"plays": {"title", "chapter"}} for a jump straight to a title, {"menus": True} for a page
    that only sets things and goes back to a menu (audio, captions), else None."""
    c = b["cmd"]
    if c["op"] == "title":
        return {"plays": {"title": c["title"], "chapter": 1}}
    if c["op"] in ("vts_title", "vts_chapter") and st[0] != "vmgm":
        t = next((t["title"] for t in structure.get("titles", []) if (t["vts"], t["ttn"]) == (st[0], c["title"])), None)
        if t:
            return {"plays": {"title": t, "chapter": c.get("chapter", 1)}}
    if c["op"] == "link_page":
        p = pages.get((st[0], c["to"]))
        if p and not p["cells"] and p["pre"] and all(
                x["op"] in ("set", "other", "nop") or (x["op"] == "link_page" and _state_buttons(pages, (st[0], x["to"], 1)))
                for x in p["pre"]):
            return {"menus": True}
    return None


def _scene_page(structure, pages, page, known=None):
    """A scene-selection page (any of its programs): several buttons jumping straight to
    different chapters of one title, by the buttons themselves or by the disc's program (known,
    {(state, button): ...}: some go through a command page first)."""
    p = pages.get(page)
    for k in range(1, len((p.get("program_buttons") or [p["buttons"]]) if p else []) + 1):
        st = page + (k,)
        hits = [(known or {}).get((st, b["n"])) or _known(structure, pages, st, b) for b in _state_buttons(pages, st)]
        chapters = {(h["plays"]["title"], h["plays"]["chapter"]) for h in hits if h and "plays" in h}
        if len(chapters) >= 2 and len({t for t, _ in chapters}) == 1:
            return True
    return False


# what a crawl looks at last: buttons (and the pages behind them) for settings, commentaries, scenes,
# and the way back; and ads, by core.AD_RE ("Sneak Peeks", "Previews"). A film's own trailer
# ("theatrical trailer") is an extra, so it isn't one. Words only order the search (_search_rank):
# what's skipped is skipped for its titles.
# text that points off the disc: something.com, www.something
WEB_ADDRESS = r"www\.|[\w-]+\.(?:com|net|org|tv|co\.uk)"
SKIP_RE = re.compile(r"\b(set ?up|settings|options|languages?|audio|subtitles?|captions|commentar\w*|"
                     r"scenes?|scene selection|chapters?|main ?menu|register\w*|dvd-?rom|web ?links?|" + WEB_ADDRESS + "|"
                     # pages of stills to flip through, nothing that plays (Muppets from Space's
                     # "Talent Files": 9 pages of cast bios)
                     r"talent files?|character files?|cast (and|&) crew|biograph\w*|filmograph\w*|production notes|"
                     r"photo galler\w*|galler(y|ies)|stills|dvd credits)\b", re.I)


# what points off the disc, no title's: an address, computer features, a line that's only "Online"
LINK = re.compile(r"\b(?:%s)|\b(?:dvd|bd)[- ]?rom\b|^\W*online(?:\s+features?)?\W*$" % WEB_ADDRESS, re.I)
HEADING_SIGNS = 2                 # signs a line must have to be taken for its page's heading
HEADING_TALLER = 1.2              # that much taller than the page's usual line
HEADING_COLOUR = 60               # how far from the page's usual line's colour (red + green + blue)
SECTION_WORDS = 4                 # at most, for a line's own words to count towards its being a heading


def lettering_colour(image, box):
    """A line's lettering's colour, not its background's: the pixels of its box furthest from the box's usual colour
    (the fifth of them), averaged; None without a picture. (line_colour's average takes in the backdrop: Sorcerer's
    Stone's yellow titles and white descriptions, on red parchment, come out alike.)"""
    try:
        crop = image.convert("RGB").crop(tuple(int(x) for x in box))
        flat = getattr(crop, "get_flattened_data", None) or getattr(crop, "getdata", None)   # (Pillow 12.1 renamed it)
        px = list(flat()) if flat else []
    except (AttributeError, ValueError, OSError):
        return None
    if not px:
        return None
    mid = [sorted(c[i] for c in px)[len(px) // 2] for i in range(3)]
    far = sorted(px, key=lambda c: -sum(abs(c[i] - mid[i]) for i in range(3)))[:max(1, len(px) // 5)]
    return tuple(sum(c[i] for c in far) / len(far) for i in range(3))


def same_lettering(a, b):
    """Whether two lines' lettering colours (lettering_colour) are one style's: the same mix of red, green and blue
    (LETTERING_MIX: the shares of each, summed), as light give or take (LETTERING_LIGHT). By the mix, not the raw
    difference: Chamber of Secrets' "PROFESSOR SEVERUS" over "SNAPE", one darker, 71 apart but 0.06 in mix;
    Sorcerer's Stone's pale gold "Scene 1" over a warmer gold description, 80 apart, 0.11 in mix."""
    sa, sb = sum(a) or 1, sum(b) or 1
    mix = sum(abs(x / sa - y / sb) for x, y in zip(a, b))
    return mix <= LETTERING_MIX and min(sa, sb) / max(sa, sb) >= LETTERING_LIGHT


def line_colour(frame, box):
    """A line's average colour on the page, its lettering and what's behind it; None without a picture."""
    try:
        return frame.convert("RGB").crop(tuple(int(x) for x in box)).resize((1, 1)).getpixel((0, 0))
    except (AttributeError, ValueError, OSError):
        return None


def heading_signs(line, others, frame):
    """What says a line is its page's heading, each no proof by itself: taller than the page's other
    lines; nothing over it in its column; and a colour of its own. {"taller", "top", "colour"}."""
    text, box, _ = line
    signs = set()
    height = lambda b: b[3] - b[1]
    middle = lambda values: sorted(values)[len(values) // 2]
    if others and height(box) >= HEADING_TALLER * middle([height(o[1]) for o in others]):
        signs.add("taller")
    if not any(o[1][1] < box[1] - 3 and o[1][0] < box[2] and o[1][2] > box[0] for o in others):
        signs.add("top")
    own = line_colour(frame, box)
    theirs = [c for c in (line_colour(frame, o[1]) for o in others) if c]
    if own and theirs:
        usual = [middle([c[i] for c in theirs]) for i in range(3)]
        if sum(abs(own[i] - usual[i]) for i in range(3)) >= HEADING_COLOUR:
            signs.add("colour")
    return signs


def _has_words(text):
    """Whether a line has letters to it, as a heading must: not a page's art read as text (3 Ninjas:
    Knuckle Up's throwing stars, "***", the tallest and brightest thing on its page)."""
    return bool(re.search(r"[^\W\d_]{2}", text or ""))


def best_heading(free, page, frame=None):
    """Of the lines that could be a page's heading, the one with the most signs, at least two, the
    higher if level; else None. page: all its lines, to compare with."""
    scored = [(len(heading_signs(l, [o for o in page if o is not l], frame)), -l[1][1], l[0]) for l in free if _has_words(l[0])]
    best = max(scored, default=None)
    return best[2] if best and best[0] >= HEADING_SIGNS else None


def several_headings(free, page, frame=None, names=()):
    """Whether a page has more than one heading, one naming a type, so no one type for it all: of the
    lines free of its buttons that stand out like a heading, a type's name in a few words being one more
    sign. A link off the disc counts whatever it looks like (Chuck's "ONLINE"). Not a button's own words,
    its box only a marker beside them (names: the buttons' readings; Spider-Man's TRAILERS, Rush Hour
    3's THEATRICAL TRAILER)."""
    letters = lambda text: re.sub(r"[^a-z]", "", (text or "").lower())
    readings = [letters(n) for n in names]

    def heading(line):
        if not _has_words(line[0]) or any(letters(line[0]) in r for r in readings):
            return False
        others = [o for o in page if o is not line]
        named = len(line[0].split()) <= SECTION_WORDS and heading_kind(line[0])
        return len(heading_signs(line, others, frame)) + bool(named) >= HEADING_SIGNS
    found = [l[0] for l in free if LINK.search(l[0]) or heading(l)]
    return len(found) >= 2 and any(heading_kind(t) for t in found)


def page_headings(image, rects, size=(720, 480), names=(), lines=None):
    """A DVD page's heading by its signs (best_heading), the line alone or None, and whether it has
    several (several_headings): of its lines outside its buttons (rects, in the frame's pixels), above
    the bottom quarter. The heading's no link off the disc. lines: the page read already, in the
    image's pixels (a Blu-ray's, read whole at its own size: bdhdmv), not read again."""
    from PIL import Image
    lines = _page_lines(image) if lines is None else lines
    if not lines:
        return None, False
    sx, sy = image.width / size[0], image.height / size[1]
    lines = [(t, (b[0] / sx, b[1] / sy, b[2] / sx, b[3] / sy), s) for t, b, s in lines]

    def in_button(box):
        x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in rects)
    page = [l for l in lines if l[1][1] < size[1] * 0.75]
    free = [l for l in page if not in_button(l[1])]
    same = image if image.size == size else image.convert("RGB").resize(size, Image.LANCZOS)
    return best_heading([l for l in free if not LINK.search(l[0])], page, same), several_headings(free, page, same, names)


# A page's layout: its text placed, then each line's role (page_layout)
STACK_EDGE = 1.0            # a column's next line: its left edge (or its middle) this many line heights from the last's
STACK_GAP = 1.0             # and no further below it than this many line heights
LAYOUT_BAR = 0.6            # a button this share of the page's width: a bar's, in no column's section
TITLE_SIZE = 0.85           # a title's next line in its box: lettering this share of its column's titles' at least
HEADING_SIZE = 1.25         # a main heading: lettering this much taller than its column's titles
LETTERING_MIX = 0.08        # a title's next line: its lettering's mix of red, green and blue this near the title's
LETTERING_LIGHT = 0.6       # and its lightness no less than this share of the other's (darker or lighter)
# (a line ending a sentence: a word, two letters at least, then a full stop; not initials, "Author J.K.")
SENTENCE_END = re.compile(r"[^\W\d_]{2}[.!?]+[\"'\u201d)\]]*\s*$")


def page_layout(lines, buttons, size, image=None):
    """A page's text laid out, the same for a DVD's and a Blu-ray's: its columns, and each line's role.
    lines: the page read whole, [(text, box, sure)], boxes in the page's pixels (size: its (width, height));
    buttons: [{"n", "box": its area in the same pixels, "lit": where its highlight changes it (a box) or None}],
    the buttons that do something; image: the page's picture (its pixels: lines' colours, to tell a title's next
    line from its description). Returns {"columns": [(x0, x1)], "lines": [{"text", "box", "sure", "column"
    (an index), "role": "title", "description", "heading" or "other", "button" (a
    title's or a description's), "section" (the heading over it in its column, its index in lines, or None),
    "part" (the main heading, level 1, over it), a heading's "level" (1: larger lettering; 2: a sub-heading, as
    tall as the titles, set over them)}]}.

    Columns: stacks of lines, each right under the last (STACK_GAP) and starting or centred where it does
    (STACK_EDGE): by alignment, not by empty page between them. A button's title: the
    line level with its highlight (Chamber of Secrets': a marker at its title's first line; Theatrical Trailer's,
    its words recoloured), else its first line in its tallest lettering; then the lines right under it in its
    box in lettering as tall (TITLE_SIZE: "PROFESSOR SEVERUS / SNAPE"). The rest of its box below: its
    description, smaller ("Explore every part of designing a scene..."). Headings: lines no title holds, in
    lettering HEADING_SIZE taller than their column's titles (Behind the Story, Additional Footage, Trailers),
    and those over a title in its own box (Goblet of Fire's first button holds "Behind the Story"). A title's
    section: the nearest heading above it in its column; above its column's first, a main heading over the column,
    else the section its left neighbour ends in."""
    w, h = size
    out = [{"text": t, "box": tuple(bx), "sure": sr, "column": None, "role": "other", "button": None, "section": None}
           for t, bx, sr in lines]
    mid = lambda bx: ((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2)
    tall = lambda bx: bx[3] - bx[1]
    inside = lambda bx, r: r[0] <= mid(bx)[0] <= r[2] and r[1] <= mid(bx)[1] <= r[3]
    # columns: stacks of lines, each right under the last, starting (or centred) where it does. By alignment, not
    # by empty page between them: Prisoner of Azkaban's two halves are 6 pixels apart, and its left half has three
    # columns and then two
    stacks = []
    for l in sorted(out, key=lambda l: (l["box"][1], l["box"][0])):
        bx, best = l["box"], None
        for k, st in enumerate(stacks):
            last = st[-1]["box"]
            size_ = max(tall(bx), tall(last))
            gap = bx[1] - last[3]
            if bx[1] <= last[1] + 0.3 * tall(last) or gap > STACK_GAP * size_:
                continue
            # (in line with one of its last few lines: a title back out at the edge of the title above, past its
            # indented description, Goblet of Fire's)
            edge = min(min(abs(bx[0] - o["box"][0]), abs(mid(bx)[0] - mid(o["box"])[0])) for o in st[-3:])
            if edge <= STACK_EDGE * size_ and (best is None or (gap, edge) < best[0]):
                best = ((gap, edge), k)
        if best:
            stacks[best[1]].append(l)
        else:
            stacks.append([l])
    stacks.sort(key=lambda st: (min(l["box"][0] for l in st), st[0]["box"][1]))
    columns = []
    for k, st in enumerate(stacks):
        for l in st:
            l["column"] = k
        columns.append((min(l["box"][0] for l in st), max(l["box"][2] for l in st)))
    # each button's title and description
    for b in buttons:
        own = sorted((l for l in out if inside(l["box"], b["box"]) and l["role"] == "other"), key=lambda l: l["box"][1])
        if not own:
            continue
        first = None
        lit = b.get("lit")
        if lit:
            level = [l for l in own if lit[1] <= mid(l["box"])[1] <= lit[3] or l["box"][1] <= (lit[1] + lit[3]) / 2 <= l["box"][3]]
            first = min(level, key=lambda l: l["box"][1]) if level else None
        if first is None:
            top = max(tall(l["box"]) for l in own)
            first = next(l for l in own if tall(l["box"]) >= NAME_SHARE * top)
        b["_first"], b["_own"] = first, own
    # (a title's usual height in each column: its buttons' first lines', the middle one)
    firsts = {}
    for b in buttons:
        if b.get("_first") and b["_first"]["column"] is not None:
            firsts.setdefault(b["_first"]["column"], []).append(tall(b["_first"]["box"]))
    usual_of = {c: sorted(v)[len(v) // 2] for c, v in firsts.items()}
    for b in buttons:
        first, own = b.pop("_first", None), b.pop("_own", None)
        if first is None:
            continue
        size_ = usual_of.get(first["column"], tall(first["box"]))
        title = [first]
        # (a title's next line: right under it, as tall, starting where it starts, in its colour; a description is
        # indented (Goblet of Fire's, 30 pixels in) or another colour (Sorcerer's Stone's, white under yellow))
        hue = lambda bx: lettering_colour(image, bx) if image is not None else None
        same_hue = lambda a, b: a is None or b is None or same_lettering(a, b)
        for l in own[own.index(first) + 1:]:
            last = title[-1]["box"]
            if tall(l["box"]) >= TITLE_SIZE * size_ and l["box"][1] - last[3] <= 0.8 * size_ and l["box"][1] >= last[1] \
                    and abs(l["box"][0] - first["box"][0]) <= 0.6 * tall(first["box"]) and same_hue(hue(l["box"]), hue(first["box"])):
                title.append(l)
            else:
                break
        # (a button across most of the page, a bar's: in no column's section, Sorcerer's Stone's PLAY MOVIE)
        wide = b["box"][2] - b["box"][0] >= LAYOUT_BAR * w
        for l in title:
            l["role"], l["button"] = "title", b["n"]
            if wide:
                l["no_section"] = True
        # (the rest of its box below: its description, in its title's column, till lettering a heading's size or
        # the line after a sentence ends: Sorcerer's Stone's "... Kings Cross." and then ONLINE, in the same box)
        ended = False
        for l in own:
            if l["role"] != "other":
                continue
            if l["box"][1] >= first["box"][1]:
                if ended or tall(l["box"]) >= HEADING_SIZE * size_:
                    break
                if l["column"] is not None and l["column"] != first["column"]:
                    continue
                l["role"], l["button"] = "description", b["n"]
                ended = bool(SENTENCE_END.search(l["text"]))
            elif tall(l["box"]) >= HEADING_SIZE * size_ * 0.9:
                l["role"], l["level"] = "heading", 1    # (over its title in its own box: its section's heading)
    # headings: lettering taller than its column's titles, or (none in its column: a heading centred over
    # several, Chamber of Secrets' Behind the Story) than the titles under it
    titles = [l for l in out if l["role"] == "title"]
    for l in out:
        if l["role"] != "other":
            continue
        sizes = sorted(tall(t["box"]) for t in titles if t["column"] == l["column"]) or \
            sorted(tall(t["box"]) for t in titles if t["box"][1] > l["box"][1]
                   and min(t["box"][2], l["box"][2]) > max(t["box"][0], l["box"][0]))
        if sizes and tall(l["box"]) >= HEADING_SIZE * sizes[len(sizes) // 2]:
            l["role"], l["level"] = "heading", 1
    # (and a line level with a main heading in another column, in its lettering: one too, its own column's titles
    # however large: Sorcerer's Stone's "FUN & GAMES", on the row of its "BEHIND THE STORY" and "ADDITIONAL FOOTAGE")
    mains = [l for l in out if l.get("level") == 1]
    for l in out:
        if l["role"] != "other" or l["column"] is None or not re.search(r"[A-Za-z]{3}", l["text"]):
            continue
        for m in mains:
            if m["column"] == l["column"] or not (m["box"][1] <= mid(l["box"])[1] <= m["box"][3]):
                continue
            ca = lettering_colour(image, l["box"]) if image is not None else None
            cb = lettering_colour(image, m["box"]) if image is not None else None
            if abs(tall(l["box"]) - tall(m["box"])) <= 0.25 * tall(m["box"]) and (ca is None or cb is None or same_lettering(ca, cb)):
                l["role"], l["level"] = "heading", 1
                break
    # descriptions outside their buttons' boxes: a line right under a title (or under its description), its left
    # edge near, its lettering no larger, till the next title (Chamber of Secrets': "Explore every part of
    # designing a scene..." under BUILDING A SCENE, its button's box the title's only)
    # (right under it, its left edge near; not after a description's sentence has ended: Chamber of Secrets'
    # "... YOUR REQUIRED READING FOR THE TERM WILL BE." and then BEHIND HOGWARTS, a sub-heading)
    below = lambda l, o: 0 <= l["box"][1] - o["box"][3] + 0.6 * tall(o["box"]) and \
        l["box"][1] - o["box"][3] <= 0.8 * tall(o["box"]) and abs(l["box"][0] - o["box"][0]) <= 1.5 * tall(o["box"]) and \
        not (o["role"] == "description" and SENTENCE_END.search(o["text"]))
    for l in sorted(out, key=lambda l: l["box"][1]):
        if l["role"] != "other" or l["column"] is None:
            continue
        over = [o for o in out if o["column"] == l["column"] and o["role"] in ("title", "description", "heading")
                and o is not l and below(l, o) and tall(l["box"]) <= 1.05 * tall(o["box"])]
        if over:
            o = max(over, key=lambda o: o["box"][3])
            l["role"], l["button"] = "description", o["button"]
            if o["role"] == "heading":          # (a heading's own: its section's description)
                l["button"], l["of_heading"] = None, out.index(o)
    # sub-headings: a line left of any title and description, with titles under it in its column before the next
    # heading (Chamber of Secrets' "LOCKHART'S CLASSROOM", "BEHIND HOGWARTS", "PROFESSORS AND MORE": as tall as titles)
    for l in sorted(out, key=lambda l: l["box"][1]):
        if l["role"] != "other" or l["column"] is None or not re.search(r"[A-Za-z]{3}", l["text"]):
            continue
        under = sorted((o for o in out if o["column"] == l["column"] and o["box"][1] > l["box"][1]
                        and o["role"] in ("title", "heading")), key=lambda o: o["box"][1])
        if under and under[0]["role"] == "title":
            l["role"], l["level"] = "heading", 2
            last = l
            # (its own lines under it: its description, now)
            for o in sorted((o for o in out if o["column"] == l["column"] and o["role"] == "other"
                             and o["box"][1] > l["box"][1]), key=lambda o: o["box"][1]):
                if below(o, last) and tall(o["box"]) <= 1.2 * tall(last["box"]):
                    o["role"], o["of_heading"] = "description", out.index(l)
                    last = o
                else:
                    break
    # (a sub-heading's own lines under it: its description, now it's a heading)
    for l in sorted(out, key=lambda l: l["box"][1]):
        if l["role"] != "other" or l["column"] is None:
            continue
        over = [o for o in out if o["column"] == l["column"] and o["role"] in ("heading", "description")
                and o is not l and below(l, o) and tall(l["box"]) <= 1.05 * tall(o["box"])]
        if over:
            o = max(over, key=lambda o: o["box"][3])
            l["role"], l["button"] = "description", o.get("button")
            l["of_heading"] = out.index(o) if o["role"] == "heading" else o.get("of_heading")
    # sections: the nearest heading above in its column (and the main one, a level-1 heading, over that). Over a
    # column's lines above its own first heading: a main heading above the column, across its width (Prisoner of
    # Azkaban's Behind the Story, centred over three columns), else the section its neighbour on the left ends in
    # (Order of the Phoenix's second column carries on its first's list)
    span = [(c0, min(l["box"][1] for l in st), c1, max(l["box"][3] for l in st)) for (c0, c1), st in zip(columns, stacks)]

    def place():
        heads = [(k, x) for k, x in enumerate(out) if x["role"] == "heading"]
        carried = {}

        def section_at(k, y, not_=None):
            own = [(i, x) for i, x in heads if x["column"] == k and x["box"][3] <= y and x is not not_]
            sec = max(own, key=lambda ix: ix[1]["box"][3])[0] if own else carried.get(k, (None, None))[0]
            mains = [(i, x) for i, x in own if x.get("level") == 1]
            part = max(mains, key=lambda ix: ix[1]["box"][3])[0] if mains else carried.get(k, (None, None))[1]
            return sec, part
        done = []
        for k in sorted(range(len(span)), key=lambda k: span[k][0]):
            done.append(k)
            x0, y0, x1, y1 = span[k]
            top = stacks[k][0]["box"]
            # (a title or description level with its first line, on its left: its row's, Chamber of Secrets' grid
            # of questions under STUDENTS, three columns of them beside the first)
            row = [o for o in out if o["column"] in done and o["column"] != k and o["role"] in ("title", "description")
                   and o["box"][2] <= x0 and o["box"][1] <= mid(top)[1] <= o["box"][3]]
            if row:
                o = max(row, key=lambda o: o["box"][2])
                carried[k] = section_at(o["column"], mid(o["box"])[1], o)
                continue
            over = [(i, x) for i, x in heads if x.get("level") == 1 and x["column"] != k
                    and x["box"][3] <= mid(top)[1] and min(x1, x["box"][2]) > max(x0, x["box"][0])]
            if over:
                i = max(over, key=lambda ix: ix[1]["box"][3])[0]
                carried[k] = (i, i)
                continue
            # (nothing over it: it carries on the column on its left from where that one ends, Order of the Phoenix's
            # second column of Focus Points)
            left = [j for j in done if span[j][2] <= x0 and span[j][1] < y1 and span[j][3] > y0]
            if left:
                j = max(left, key=lambda j: span[j][2])
                carried[k] = section_at(j, span[j][3] + 1)
        for l in out:
            l["section"] = None
            l.pop("part", None)
            if l["role"] not in ("title", "description", "heading"):
                continue
            sec, part = section_at(l["column"], mid(l["box"])[1], l)
            if l["role"] != "heading" and not l.get("no_section"):
                l["section"] = sec
            if l.get("level") != 1 and part is not None and not l.get("no_section"):
                l["part"] = part
    place()
    # (a heading no title is under: just text, a menu bar's label, ONLINE's; then the sections again without it)
    named = {l["section"] for l in out if l["role"] == "title"} | {l.get("part") for l in out if l["role"] == "title"}
    dropped = False
    for k, l in enumerate(out):
        if l["role"] == "heading" and k not in named:
            l["role"], dropped = "other", True
            l.pop("level", None)
    if dropped:
        place()
    for l in out:
        l.pop("no_section", None)
    return {"columns": columns, "lines": out, "order": layout_order(out)}


def position_order(boxes):
    """Boxes in reading order by their places alone (Breuel's rules): one before another when they're across each
    other (by a tenth of the narrower at least) and it starts higher, or it's wholly to the left, not wholly below it
    (a band under a row of columns: Prisoner of Azkaban's Choir Practice after the Filmmakers), and nothing across
    both lies between them; the top one first where nothing decides, and the top one of a loop's. Returns their
    indices."""
    import heapq
    n = len(boxes)
    across = lambda a, b: min(a[2], b[2]) - max(a[0], b[0]) > 0.1 * min(a[2] - a[0], b[2] - b[0])
    after = [set() for _ in range(n)]
    for i, a in enumerate(boxes):
        for j, b in enumerate(boxes):
            if i == j:
                continue
            if across(a, b):
                if (a[1], a[0]) < (b[1], b[0]):
                    after[i].add(j)
            elif a[2] <= b[0] and a[1] < b[3]:
                lo, hi = min(a[1], b[1]), max(a[3], b[3])
                if not any(k not in (i, j) and across(c, a) and across(c, b) and lo <= (c[1] + c[3]) / 2 <= hi
                           for k, c in enumerate(boxes)):
                    after[i].add(j)
    need = [0] * n
    for i in range(n):
        for j in after[i]:
            need[j] += 1
    ready = [(boxes[i][1], boxes[i][0], i) for i in range(n) if not need[i]]
    heapq.heapify(ready)
    order = []
    while len(order) < n:
        if not ready:
            i = min((i for i in range(n) if need[i] > 0), key=lambda i: (boxes[i][1], boxes[i][0]))
            need[i] = 0
            heapq.heappush(ready, (boxes[i][1], boxes[i][0], i))
        _, _, i = heapq.heappop(ready)
        need[i] = -1
        order.append(i)
        for j in after[i]:
            need[j] -= 1
            if need[j] == 0:
                heapq.heappush(ready, (boxes[j][1], boxes[j][0], j))
    return order


def layout_order(lines):
    """A laid-out page's lines (page_layout's) in reading order, by its headings, not line by line: the main
    headings and the lines under none in order by their places (position_order); under each heading, its own
    lines and its sub-headings column by column (the columns in order by their places, each top to bottom), a
    sub-heading's own following it. So a heading under one column (Sorcerer's Stone's ONLINE, under Scenes 1-3)
    comes after the column beside it (Scenes 4-7), both under ADDITIONAL FOOTAGE."""
    under = {}
    for i, l in enumerate(lines):
        if l["role"] == "heading":
            parent = l.get("part") if l.get("level") != 1 else None
        else:
            parent = l["section"] if l.get("section") is not None else l.get("part")
        under.setdefault(parent, []).append(i)
    out = []

    def every(i):
        return [x for k in under.get(i, []) for x in [k] + every(k)]

    def emit(i):
        out.append(i)
        kids = under.get(i, [])
        cols = {}
        for k in kids:
            cols.setdefault(lines[k]["column"], []).append(k)
        keys = list(cols)
        spans = []
        for c in keys:                          # (a column's place: its lines', a sub-heading's with all under it)
            boxes = [lines[x]["box"] for k in cols[c] for x in [k] + every(k)]
            spans.append((min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)))
        for c in position_order(spans):
            for k in sorted(cols[keys[c]], key=lambda k: lines[k]["box"][1]):
                emit(k)
    top = under.get(None, [])
    for t in position_order([lines[k]["box"] for k in top]):
        emit(top[t])
    return out


def name_rects(buttons, unread=None):
    """Where a page's buttons' names are, for its headings (page_words): each one's reading's (its
    "words": the lines it was read from, beside it for a marker) where it does something (plays, turns
    a page), else its box (decoration owns no name, whatever was read beside it: Spider-Man 3's
    31-pixel sliver by "SPECIAL FEATURES", which only calls a menu up again). unread(button): for
    one not read (a quick crawl's), where its name's lines are, if that can be told ([] if not: a
    Blu-ray's, the lines its highlight changes, bdhdmv.name_boxes; Chuck's first row's box holds the
    page's "EPISODES" too)."""
    acts = lambda b: bool(b.get("plays") or isinstance(b.get("goes_to"), dict))
    out = []
    for b in buttons:
        if b.get("words") and acts(b):
            out.append(list(b["words"]))
        else:
            out += (unread(b) if unread is not None and not b.get("words") else []) or [list(b["rect"])]
    return out


def page_words(image, rects, size=(720, 480), names=(), lines=None):
    """A page's words that aren't its buttons' names, and its heading: (its words, outside rects, top to
    bottom, as page_text has them; its heading line, page_headings'; whether it has several). rects:
    where its buttons' names were read (each reading's Words.where, else its button's box), in the
    frame's coordinates: a heading inside a button's box isn't its name (Chuck's "EPISODES" over its
    first row; Order of the Phoenix's "Additional Footage" over "Additional Scenes"). Off the page read
    once (lines, the picture's pixels; else _page_lines), a DVD's and a Blu-ray's alike; nothing there,
    the page read again with the names blanked (page_text). Without RapidOCR, page_text's and no
    heading line. (temp/bench_headings.py: 205 saved DVD pages, the heading line the same on 204.)"""
    lines = _page_lines(image) if lines is None else lines
    if lines is None:
        return page_text(image, rects, size), None, False
    sx, sy = image.width / size[0], image.height / size[1]
    inside = lambda b: any(r[0] <= (b[0] + b[2]) / 2 / sx <= r[2] and r[1] <= (b[1] + b[3]) / 2 / sy <= r[3] for r in rects)
    free = sorted((l for l in lines if not inside(l[1])), key=lambda l: (l[1][1], l[1][0]))
    words = " ".join(re.findall(r"[A-Za-z][A-Za-z']+", " ".join(t for t, _, _ in free)))
    if not words:
        # (nothing outside them in the page read whole: read again with them blanked, which finds what
        # it didn't, a film's title art among the names: The Time of Their Lives, "The ... Their Lives")
        words = page_text(image, rects, size)
    line, mixed = page_headings(image, rects, size, names=names, lines=lines)
    return words, line, mixed


def page_heading_line(image, rects, size=(720, 480)):
    """A DVD page's heading by its signs, the line alone, or None (page_headings)."""
    return page_headings(image, rects, size)[0]


def page_kind(page):
    """The type a page's heading gives its titles: by the line its signs pick out, else by all the
    words outside its buttons (a heading no taller than the rest, on a page of plain lettering). None
    for a page of several headings: which titles go under which is the user's to say."""
    if page.get("mixed"):
        return None
    return heading_kind(page.get("heading_line")) or heading_kind(page.get("heading"))


# (a stray letter before it: OCR of the lettering's backdrop, "i play")
PLAY_ALL_RE = re.compile(r"\bp[li1]ay\s*a(?:\w?l\w*)?\b", re.I)    # (OCR's Play All: "Play Aly", "PLAY aul",
                                                                  # "play A", "Piay All"; not "Play Again", "Display")
PLAY_RE = re.compile(r"^\W*([a-z]\W+)?(play|start|resume|watch)( the)?( movie| film| feature)?\W*$", re.I)
# a button naming a copy of the film ("PLAY WIDESCREEN VERSION"), not a film of its own
VERSION_WORDS_RE = re.compile(r"\b(versions?|editions?|cuts?|wide\s*screen|full\s*screen|letterbox\w*|pan\s*(&|and)?\s*scan)\b",
                              re.I)


# Ads, as a film's menu words them: core.AD_RE, plus a singular "Preview" ("preview trailer" on
# Father of the Bride Part II: other films' trailers). AD_RE leaves that one alone for TV, where
# it's often the show's own next episode; the crawl only reads films' menus.
MENU_AD_RE = re.compile(r"\b(previews?|sneak\w*|coming attractions?)\b", re.I)
AD_REEL_CHAPTERS = 3        # a "trailer" in this many chapters or more: one trailer each, an ad reel


# The same words, matched loosely too: OCR of fancy menu lettering is often a letter or two off
# ("ALENT ewes" for TALENT FILES, "RPUAY MOVIE." for PLAY MOVIE). Each a run of words.
SKIP_WORDS = [("set", "up"), ("setup",), ("settings",), ("options",), ("language",), ("languages",), ("audio",),
              ("subtitles",), ("subtitle",), ("captions",), ("commentary",), ("scene", "selection"),
              ("scene", "selections"), ("chapters",), ("main", "menu"), ("mainmenu",), ("register",),
              ("talent",), ("character", "files"), ("cast", "crew"), ("biographies",), ("filmographies",), ("production", "notes"),
              ("gallery",), ("stills",), ("dvd", "credits"), ("web", "links")]
AD_WORDS = [("previews",), ("preview",), ("peeks",), ("sneak", "peeks"), ("sneak", "peek"), ("coming", "soon"),
            ("also", "available"), ("available", "from"), ("advertisements",), ("commercials",)]
# scenes that are extras, not a scene selection
EXTRA_SCENES_RE = re.compile(r"\b(deleted|additional|extended|alternate|bonus|unused|missing)\s+scenes?\b", re.I)


def _edits(a, b):
    """The edit distance between two words (letters added, dropped or changed)."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def _near(word, key):
    """A word OCR may have misread as another: exact under 4 letters, then 1 letter off, 2 from 8."""
    allow = 0 if len(key) < 4 else 1 if len(key) < 8 else 2
    return abs(len(word) - len(key)) <= allow and _edits(word, key) <= allow


# real words a letter or two from the ad words, read right: not a misread "previews" or "peeks" (Order of the
# Phoenix's "Take a thrilling look back through THE PREVIOUS MOVIES", a description, made its page ads)
NEAR_AD_WORDS = {"previous", "review", "reviews", "reviewed", "preview's", "peaks", "peak", "seeks", "speaks"}


def _says(text, phrases):
    """Whether text holds one of the runs of words, loosely (a real word near one, NEAR_AD_WORDS, isn't it)."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    near = lambda w, k: w == k or (w not in NEAR_AD_WORDS and _near(w, k))
    return any(all(near(words[i + j], p[j]) for j in range(len(p)))
               for p in phrases for i in range(len(words) - len(p) + 1))


def _ad_text(text):
    from .core import AD_RE
    return bool(text) and bool(AD_RE.search(text) or MENU_AD_RE.search(text) or _says(text, AD_WORDS))


def _ads_page(way, heading, buttons, line=None):
    """Whether a page is a page of ads, by the buttons pressed on the way to it and its heading (its
    words outside its buttons' names). Not for the words of one of its own ad buttons, read into the
    heading: Rush Hour 3's page of a trailer and "SNEAK PEEKS", its heading read "THEATRICAL TRAILE
    SNEAK PEEI". (Those buttons are ads by their own words.) Nor, by its words alone, for a page whose
    heading line names it a page of trailers (line: page_headings'): its trailers are each judged,
    the film's own an extra (trailer_ads: Muppets from Space's "THEATRICAL TRAILERS", "OTHER HENSON
    ADVENTURES AVAILABLE FROM COLUMBIA TRISTAR HOME VIDEO", its own trailer and teaser among them)."""
    if any(_ad_text(w) for w in way):
        return True
    if line and heading_kind(line) == "Trailer" and not _ad_text(line):
        return False
    ours = {w for b in buttons if _ad_text(b.get("text"))
            for w in re.findall(r"[a-z]+", (b.get("text") or "").lower())}
    rest = " ".join(w for w in re.findall(r"[A-Za-z]+", heading or "") if not any(_near(w.lower(), o) for o in ours))
    return _ad_text(rest)


def _skip_text(text):
    if not text or EXTRA_SCENES_RE.search(text):
        return False
    return bool(SKIP_RE.search(text) or _says(text, SKIP_WORDS) or _ad_text(text))


def _main_menu_text(text):
    return bool(re.search(r"\bmain ?menu\b", text or "", re.I)) or _says(text, [("main", "menu"), ("mainmenu",)])


def _play_text(text):
    """A Play button: PLAY_RE, or loosely: a word or two, one of them near "movie" or "film" (the
    other, if any, near play / start / watch). OCR often reads just the second line of a
    two-line PLAY / MOVIE."""
    if PLAY_RE.match(text or ""):
        return True
    words = [w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) >= 2]
    if not 1 <= len(words) <= 2:
        return False
    # (cut off at the rectangle's edge counts too: "PLAY MOV")
    film = [w for w in words if _near(w, "movie") or _near(w, "film") or (len(w) >= 3 and "movie".startswith(w))]
    rest = [w for w in words if w not in film]
    return bool(film) and all(any(_edits(w, v) <= 2 for v in ("play", "start", "watch")) for w in rest)


def _can_select(buttons, b):
    """Whether the highlight can get to a button: arrows are all VLC has, and none may lead to it (Spider-Man's
    hidden corner buttons). On a page with no arrows set at all, nothing's to be told."""
    arrows = lambda o: [o.get(k) for k in ("up", "down", "left", "right")]
    others = [o for o in buttons if o is not b and not o.get("auto")]
    if not any(m and m != o["n"] for o in others for m in arrows(o)):
        return True
    return any(b["n"] in arrows(o) for o in others)


def _routes(pages, start, learned=None, known=None):
    """{state: [(state, button), ...]}: the buttons to press from the start menu to reach each
    state, by the disc's own links and those learned by pressing ({(state, button): state}). Only
    buttons the highlight can get to."""
    routes, queue = {start: []}, [start]
    while queue:
        st = queue.pop(0)
        buttons = _state_buttons(pages, st)
        for b in buttons:
            nxt = _goes(pages, st, b, learned, known)
            if nxt and nxt not in routes and not b["auto"] and _can_select(buttons, b):
                routes[nxt] = routes[st] + [(st, b["n"])]
                queue.append(nxt)
    return routes


# The search is for titles, since titles are what's being labelled. The disc's program says what most
# buttons play, whatever they say. So a page is read for a title no button read so far names, and a
# button is pressed while a title has no known way to it; a title with one already is left be.
NAMED_WORDY = 3         # (_wordy: "OFF" or more; not "On", "Vi", a stray mark)
TITLE_TRIES = 3         # a title's pages read at most while none names it (a DVD's crawl and a Blu-ray's)


def names_title(text, sure=None, need_sure=False):
    """Whether a button's reading names the title it plays, for a crawl to look no further for it, a DVD's
    and a Blu-ray's alike: words (NAMED_WORDY: not "On", a stray mark), not a Play All's, which plays the
    first episode and names none (Chuck's PLAY ALL); a Play button's do (the film's own: its name is
    the disc's). need_sure: and READ_CONFIDENT of them, where RapidOCR said how sure (sure)."""
    if not text or _wordy(text) < NAMED_WORDY or PLAY_ALL_RE.search(text):
        return False
    return not (need_sure and sure is not None and sure < READ_CONFIDENT)


def _named_titles(pages, visited, known, learned, pressed, word):
    """The titles named so far: a button with words, on a page read, that plays one from its start
    or leads to a page of its scenes. (Only its own: what plays on after it may have a button too.)
    Words badly read don't count, so the search goes on to the title's other buttons, or its copies'."""
    found = set()
    for s in visited:
        for b in _state_buttons(pages, s):
            if not names_title(word(s, b["n"]) or ""):
                continue
            k = (s, b["n"])
            plays = known.get(k, {}).get("plays")
            if plays and plays.get("chapter") in (1, None):
                found.add(plays["title"])
            chain = [x["title"] for x in (pressed.get(k) or {}).get("chain", []) if x["title"] > 0]
            if chain:
                found.add(chain[-1])
            nxt = _goes(pages, s, b, learned, known)
            scene = nxt and _scene_state(pages, nxt, known)
            if scene:
                found.add(scene)
    return found


def _given_up(pages, visited, known, found, ready):
    """The titles a crawl stops looking for: TITLE_TRIES pages read with a button playing one from its start,
    and none naming it (Ghostbusters II's film, its only buttons "Play": no use reading 31 pages for it).
    visited: the pages read; ready(page): its words in."""
    tries = {}
    for s in visited:
        if not ready(s):
            continue
        for t in {k["plays"]["title"] for b in _state_buttons(pages, s) for k in [known.get((s, b["n"])) or {}]
                  if k.get("plays") and k["plays"].get("chapter") in (1, None)} - found:
            tries[t] = tries.get(t, 0) + 1
    return {t for t, n in tries.items() if n >= TITLE_TRIES}


def _titles_with_a_way(routes, known, pressed, opening=()):
    """The titles some button on a page there's a way to plays, read yet or not, or that play on
    after one, or before the menu."""
    have = set(opening)
    for (s, _), k in known.items():
        plays = k.get("plays")
        if plays and s in routes:
            have.add(plays["title"])
            have.update(plays.get("after") or [])
            if plays.get("into"):
                have.add(plays["into"])
            have.update(o["title"] for o in k.get("others") or [])
    for r in pressed.values():
        have.update(x["title"] for x in r.get("chain", []) if x["title"] > 0)
    return have


def _leads_to_title(pages, s, visited, found, known, learned, pressed, unknown_matter):
    """Whether a page is worth reading: a button there, or on a page past it, plays a title not named
    yet; or, while a title has no way to it (unknown_matter), nobody knows what the button does. Only
    pages not visited yet count: a button already read, its words unreadable, won't read any better
    for reading another page. Spider-Man's Character Files: cast bios leading only to each other."""
    if s in visited:
        return False
    seen, todo = {s}, [s]
    while todo:
        x = todo.pop()
        for b in _state_buttons(pages, x):
            k = (x, b["n"])
            plays = known.get(k, {}).get("plays")
            if plays and plays["title"] not in found:
                return True
            nxt = _goes(pages, x, b, learned, known)
            if unknown_matter and not nxt and not b["auto"] and k not in known and k not in pressed:
                return True
            if nxt and nxt not in seen and nxt not in visited:
                seen.add(nxt)
                todo.append(nxt)
    return False


def _page_plan(pages, routes, done, found, known, learned, copies=None, rank=None):
    """The pages to read, in order, from what the disc's program says plays what, before a word is
    read: few, by taking the page that names the most titles still unnamed for each page it costs
    (the pages on the way to it count, and what they name). A copy's video counts once, whichever of
    its titles' buttons is read. done: pages read or given up on. found: the titles named (or hoped
    to be: then a page that fails them plans the way to their other buttons, when it's asked again).
    A button leading to a title's scene page names it as well as one playing it (Smallville's Episodes
    page). rank(page): ties go to the lowest (_search_rank: words only order the search)."""
    copies = copies or {}
    video = lambda t: copies.get(t, t)
    named = {video(t) for t in found}
    holds = {}
    for s in routes:
        if s in done:
            continue
        for b in _state_buttons(pages, s):
            plays = known.get((s, b["n"]), {}).get("plays")
            if plays:
                title = plays["title"] if plays.get("chapter") in (1, None) else None
            else:
                nxt = _goes(pages, s, b, learned, known)
                title = nxt and _scene_state(pages, nxt, known)
            if title and title not in found and video(title) not in named:
                holds.setdefault(s, set()).add(video(title))
    return plan_pages(holds, {s: [y for y, _ in r] for s, r in routes.items()}, done, rank)


def plan_pages(holds, ways, done=(), rank=None, cost=None):
    """The pages to read, in order, so every title still unnamed is read on some page: a DVD's and a
    Blu-ray's alike, each from what its buttons are known to play before a word is read. holds: {page:
    the titles (a copy's video once) a button on it would name}; ways: {page: the pages on the way to
    it, in order}; done: pages read or given up on; rank(page): ties go to the lowest. Greedy: the page
    that names the most titles still unnamed for what it costs (cost(pages): the pages new to the
    crawl it takes; by default how many, the pages on the way counted, and what they name)."""
    plan, chosen = [], set(done)
    holds = {s: t for s, t in holds.items() if s not in chosen}
    left = set().union(*holds.values()) if holds else set()
    cost = cost or len

    def worth(s):
        new = [x for x in list(ways.get(s, [])) + [s] if x not in chosen]
        return set().union(*(holds.get(x, set()) for x in new)) & left, new
    while left:
        def score(s):
            gained, new = worth(s)
            c = cost(new)
            return (len(gained) / c if c else len(gained) * 1e9, -(rank(s) if rank else 0), -len(new))
        gained, new = worth(max(holds, key=score))
        if not gained:
            break
        plan += new
        chosen.update(new)
        left -= gained
    return plan


def _search_rank(route, start, word, scenes):
    """What's looked at first: ordinary pages, then those behind words like Audio or Commentary, then
    scene pages (the film's Play button will usually have named it by then). route: [(state, button)]."""
    words = [word(x, n) for x, n in route]
    words = [w for (x, _), w in zip(route, words) if not (x == start and _main_menu_text(w))]
    # (not pages of ads: reading them is quick, and their pictures name the ads, posters often:
    # Spider-Man 2's previews, Hitch among them. Pressing their buttons is another matter)
    return 2 if scenes else 1 if any(_skip_text(w) and not _ad_text(w) for w in words) else 0


def _scan_titles(drive, log=print):
    """The disc's titles as the scan compares them ([] if they can't be read: then nothing's known of
    them, and they're all looked for)."""
    from . import core
    try:
        _, base = core.detect_format(drive)
        if not base:
            return []
        scanned = core.parse_dvd(base)["titles"]
        core.classify(scanned, "DVD")
        return scanned
    except (OSError, ValueError, IndexError, struct.error) as e:
        log("  couldn't compare the disc's titles (%s)" % e)
        return []


def _titles_sought(scanned, length):
    """The titles worth pressing buttons for: all but the too short and what the scan already says
    isn't a title of its own (a copy, a play-all, its parts, a loop), which may well have no button."""
    from . import core
    apart = {int(t["id"].split()[1]) for t in scanned if t.get("role") in HIDDEN_ROLES and t["role"] != "junk"}
    return {t for t, seconds in length.items() if (seconds or 0) >= core.PART_MIN and t not in apart}


def dvd_number(tid):
    """A scan's DVD title ("Title 5") as a crawl's (5)."""
    return int(tid.split()[1])


def _copies_of(scanned, key=dvd_number):
    """{title: the title the scan found it the same video as}, as the crawl names them (key: a scan's id as
    the crawl's: a DVD's number, dvd_number; a Blu-ray's playlist as it is), a DVD's and a Blu-ray's alike."""
    out = {}
    for t in scanned:
        m = re.match(r"same video as (.+?)\s*$", t.get("note") or "") if t.get("role") == "duplicate" else None
        if m:
            try:
                out[key(t["id"])] = key(m.group(1))
            except (ValueError, IndexError):
                pass
    return out


def _with_copies(found, copies):
    """The titles named, and every copy of them, whichever of a video's titles was read: no page need
    be read for the others. Psych S6 D3's episodes again behind Audio Commentaries, titles 18-20; its
    commentaries, each named on its episode's page and on a page of them. (The one read is then the
    one you confirm: core.prefer_played.)"""
    videos = {o for c, o in copies.items() if c in found or o in found}
    return set(found) | videos | {c for c, o in copies.items() if o in videos}


def _play_alls_of(scanned, key=dvd_number):
    """The titles the scan knows for play-alls, as the crawl names them (key: _copies_of's): nothing to
    name them by, and their order comes from the disc's program, not from reading their pages."""
    return {key(t["id"]) for t in scanned if t.get("role") == "play-all"}


def _moves(buttons, frm, to):
    """The arrow keys that move the highlight from one button to another, or None. Never through
    an auto-action button (selecting it presses it)."""
    by = {b["n"]: b for b in buttons}
    seen, queue = {frm: []}, [frm]
    while queue:
        n = queue.pop(0)
        if n == to:
            return seen[n]
        for key in ("up", "down", "left", "right"):
            m = by[n][key] if n in by else 0
            if m in by and m not in seen and (m == to or not by[m]["auto"]):
                seen[m] = seen[n] + [key]
                queue.append(m)
    return None


def _diff_mask(a, b, level=40):
    """White where two frames differ (black elsewhere), by the most-changed colour: a gold
    highlight on pale grey hardly changes the brightness."""
    from PIL import ImageChops
    r, g, b = ImageChops.difference(a, b).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda x: 255 if x > level else 0)


def _count_in(mask, rects):
    """How many white pixels a mask has inside any of some rectangles."""
    from PIL import Image, ImageChops, ImageDraw
    area = Image.new("L", mask.size, 0)
    draw = ImageDraw.Draw(area)
    for x0, y0, x1, y1 in rects:
        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=255)
    return ImageChops.multiply(mask, area).histogram()[255]


def _unseen(by):
    """Whether no arrow key on a page can show the highlight moving: none leads anywhere, or only
    to buttons too small for a highlight to be seen ({button: button} of the page)."""
    area = lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1])
    to = {b[k] for b in by.values() for k in ("up", "down", "left", "right") if b[k] in by and b[k] != b["n"]}
    return all(area(by[n]["rect"]) < SEEN for n in to)


def _overlaps(r, q):
    return min(r[2], q[2]) > max(r[0], q[0]) and min(r[3], q[3]) > max(r[1], q[1])


def _count_beyond(mask, rect, others):
    """How many white pixels a mask has inside a rectangle but outside some others."""
    inside = [[max(rect[0], q[0]), max(rect[1], q[1]), min(rect[2], q[2]), min(rect[3], q[3])] for q in others]
    return _count(mask, rect) - _count_in(mask, inside)


def _count(mask, rect):
    """How many white pixels a mask has inside a rectangle."""
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return 0
    return mask.crop((x0, y0, x1, y1)).histogram()[255]


MENU_ONLY_SECONDS = 30      # a menu program this long, with no buttons, is something to watch


def menu_only(pages, structure):
    """The buttons that play something kept among the disc's menus, not a title: no title list has it, so
    nothing can rip it (3 Ninjas: Knuckle Up's Jumanji trailer, the last of its Bonus Trailers: 2:52 in the
    top menu's own video). [(button, its length)], each marked "menu_only"."""
    programs = {p["pgc"]: p for p in structure.get("vmgm") or []}
    out = []
    for page in pages:
        for b in page.get("buttons") or []:
            cmd = b.get("cmd") or {}
            target = programs.get(cmd.get("pgc")) if cmd.get("domain") == "vmgm" else None
            if not target or target.get("buttons") or b.get("plays") or b.get("goes_to"):
                continue
            seconds = sum(c.get("seconds") or 0 for c in target.get("cells") or [])
            if seconds >= MENU_ONLY_SECONDS and re.search(r"[A-Za-z]{2}", b.get("text") or "") and not _nav_only(b["text"]):
                b["menu_only"] = round(seconds)
                out.append((b, seconds))
    return out


READ_CONFIDENT = 0.90      # a button's reading this sure stands; a second reading this sure ends the search
READ_LEAST = 0.80          # the least sure a line may be, to be kept at all
READ_SHORTEST = 2          # letters and digits: fewer are no name ("w", off WALL-E's title 84)


def _second_readings(pic, box, heads=()):
    """What else can be read in a button's area (a box in the picture's pixels), cheapest first, as they're
    wanted: the whole page's lines that fall in it, at its own size and twice it, then the area alone, grey,
    inverted, at twice the size. Tried on 276 buttons against the names they should have (12 ways of showing
    them, and these two sources of the page's lines): these do as well as all of them, and running the lot
    and keeping the surest did worse: the more readings, the more chances of a sure wrong one. Last, on a
    picture of a screen at an angle only (_keystone), the area made level (_flattened). heads: a page
    heading's lines dipping into the area (_heading_boxes), no part of the button's words."""
    from PIL import Image, ImageOps
    inside = lambda q: box[0] <= (q[0] + q[2]) / 2 <= box[2] and box[1] <= (q[1] + q[3]) / 2 <= box[3]
    over = lambda b, h: min(b[2], h[2]) > max(b[0], h[0]) and min(b[3], h[3]) - max(b[1], h[1]) > 0.5 * (b[3] - b[1])
    own = lambda b: not any(over(b, h) for h in heads)
    for scale in (1, 2):
        yield [(t, q, sure) for t, q, sure in _page_lines(pic, scale) or [] if inside(q) and own(q)]
    crop = pic.crop(box)
    if crop.width and crop.height:
        grey = ImageOps.autocontrast(ImageOps.grayscale(crop), cutoff=1)
        at_page = lambda q: (q[0] / 2 + box[0], q[1] / 2 + box[1], q[2] / 2 + box[0], q[3] / 2 + box[1])     # (it's read at twice the size)
        yield [l for l in rapid_lines(ImageOps.invert(grey.resize((grey.width * 2, grey.height * 2), Image.LANCZOS)).convert("RGB"), least=0.0) or []
               if own(at_page(l[1]))]
    point = _keystone(pic)          # a screen seen at an angle: the area made level, at twice the size
    if point:
        flat, spot = _flattened(pic, box, point)
        if flat is not None:
            def back(q):
                xs, ys = zip(*(spot(x, y) for x, y in ((q[0], q[1]), (q[2], q[1]), (q[2], q[3]), (q[0], q[3]))))
                return min(xs), min(ys), max(xs), max(ys)
            yield [l for l in rapid_lines(flat, least=0.0) or [] if own(back(l[1]))]


def _letters(text):
    return len(re.findall(r"[^\W_]", text or ""))


def _read_second(pic, box, heads=()):
    """The first of _second_readings that's READ_CONFIDENT, else the surest above READ_LEAST: (its lines
    joined, how sure: its least sure line), or None."""
    best = sure_one = None
    for lines in _second_readings(pic, box, heads):
        kept = [l for l in lines if l[2] >= READ_LEAST]
        # (rid of stray symbols, as a first reading is: _rapid_button)
        text = " ".join(re.sub(r"[^\w'&:!?.,()\- ]+", "", " ".join(l[0] for l in kept).replace("/", " ")).replace("_", " ").split())
        if not kept or _letters(text) < READ_SHORTEST:
            continue
        sure = min(l[2] for l in kept)
        whole = all(l[2] >= READ_LEAST or l[2] < RAPID_LEAST for l in lines)     # (no line of words left out)
        if sure >= READ_CONFIDENT:
            if sure_one is None or _letters(text) > _letters(sure_one[0]):
                sure_one = (text, sure)
            if whole:
                return sure_one
        if best is None or sure > best[1]:
            best = (text, sure)
    return sure_one or best


def read_again(pages, out_dir, log=print, sure=None):
    """The buttons that play a title from its start, whose words aren't READ_CONFIDENT (or are nothing), and
    whose title has no other button that is, read again (_read_second): the new words kept if surer than theirs
    and they lose nothing (a surer reading of less is a part of the name: "STUART LITTLE" for "STUART LITTLE
    2"). Off each page's saved picture, so only what's expected to say something costs anything: not a settings
    button, nor a scene's. A tiny button's words are on its plate, if it has one. sure: {reading: how sure}
    (_sure)."""
    from PIL import Image
    sure = _sure if sure is None else sure
    how_sure = lambda b: sure.get(b.get("text") or "", 0.0) if b.get("text") else 0.0
    plays = lambda b: b.get("plays") and b["plays"].get("chapter") in (1, None)
    settled = {b["plays"]["title"] for page in pages for b in page.get("buttons") or [] if plays(b) and how_sure(b) >= READ_CONFIDENT}
    changed = []
    for page in pages:
        path = os.path.join(out_dir, page.get("image") or "")
        if not page.get("image") or not os.path.exists(path):
            continue
        wanted = [b for b in page["buttons"] if plays(b) and b["plays"]["title"] not in settled]
        if not wanted:
            continue
        with Image.open(path) as im:
            pic = im.convert("RGB")
        sx, sy = pic.width / 720, pic.height / 480
        found = plates(pic)
        rects = [x["rect"] for x in page["buttons"]]
        for b in wanted:
            r, pad = b["rect"], 4
            if r[2] - r[0] < TINY:
                near = [k for k in found if _gap(r, k) < PLATE_REACH]
                if not near:
                    continue
                r, pad = min(near, key=lambda k: _gap(b["rect"], k)), 0
            box = (max(0, int((r[0] - pad) * sx)), max(0, int((r[1] - pad) * sy)),
                   min(pic.width, int((r[2] + pad) * sx)), min(pic.height, int((r[3] + pad) * sy)))
            got = _read_second(pic, box, _heading_boxes(pic, b["rect"], rects, (720, 480)))
            was = b.get("text") or ""
            if got and got[0] != was and got[1] > how_sure(b) and _letters(got[0]) >= _letters(was):
                log("read again: button %d of page %s: \"%s\" is \"%s\" (%.2f)" % (b["n"], page["page"], was, got[0], got[1]))
                changed.append((page, b, was, got[0], got[1]))
                b["text"] = got[0]
                b["words"] = [max(0, r[0] - pad), max(0, r[1] - pad), min(720, r[2] + pad), min(480, r[3] + pad)]
                if sure is _sure:
                    _note_sure(got[0], got[1])
                else:
                    sure[got[0]] = got[1]
    return changed


def _clock(seconds):
    s = int(round(seconds or 0))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


def _reachable(states, began, engine, reached):
    """The pages VLC can be on, when the disc's program followed every button and wasn't unsure
    of any: those it reaches (else None: any page with buttons). WALL-E: 228 pages, most of them
    decoys; one fitted the main menu's highlight moving, and was taken for it."""
    if not engine or any("unsure" in a for answers in engine.values() for a in answers):
        return None
    near = {tuple(x) for x in reached} | {tuple(began[k]) for k in ("goes_to", "menu_goes_to") if began.get(k)}
    return [s for s in states if s in near]


class _Crawler:
    def __init__(self, vlc_dir, mrl, pages, out_dir, log, progress=None):
        self.vlc_dir, self.mrl, self.pages, self.out_dir, self.log = vlc_dir, mrl, pages, out_dir, log
        self.progress = progress or no_progress          # (CRAWL_STAGES: each page read)
        self.v = None
        self.moving = None                      # where the current page's own video moved, lately
        self.learned = {}                       # {(state, button): state}: links found by pressing
        self.texts = {}                         # {state: {button: text}}, read (see words())
        self.reading = {}                       # {state: {button: OCR under way}}: the pages visited
        self.pictures = {}                      # {state: the picture its words were read off, and its lines}
        self.finished = {}                      # {state: its words, read every way it takes (_finish)}
        self.spent = {}                         # {what: seconds}, for the log (timed())
        self.overlay = {}                       # {state: whether its words are in its subpicture}
        self.expect = 0                         # pages the tour expects to read (for "2 of 6")
        self.menu_key = False                   # the disc plays its film by itself: Menu, after opening
        self.menu_skip = False                  # Menu, once in the menus: past the first page's intro
        self.lengths = None                     # {VLC's title: seconds}
        self.dark_ok, self.dark_missed = None, 0  # whether the highlight shows over the video darkened
        if os.environ.get("DISC_DETECTIVE_NO_DARK"):  # (for comparing: checked as it is only)
            self.dark_ok = False
        self.menu_back = None                   # whether DVD Menu leads back to the start (restart)
        self.film_first = False                 # VLC plays the film where the disc's program waits in a
                                                # menu (WALL-E): its DVD Menu, once the film is on
        # the disc's program's word on the highlighted button: where it opens, and after each
        # button that leads to a page ({(state, button): button there}); and how often it was right
        self.start_select, self.selects, self.placed = None, {}, [0, 0, 0]   # (right, wrong, moved by us)
        self.intros = {}                        # {(state, button): seconds of menu before its buttons come on}
        self.states = []                        # every page with buttons to tell apart (crawl)
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.more = ThreadPoolExecutor(max_workers=2)     # (each page's readings, finished: _finish)

    def open(self):
        self.v = Vlc(self.vlc_dir, highlight=True)
        self.v.open(self.mrl)
        t0 = time.time()
        while not self.v.frames and time.time() - t0 < 60:     # (a drive spinning up again: slow)
            pause(0.25)
        if not self.v.frames:
            raise Lost("no picture from VLC")
        if self.menu_key:
            self.menu_call()
        elif self.film_first:
            self.await_title()
            self.to_menus()

    def feature_length(self, title):
        """Whether VLC's title is a film's length (not a clip before a page)."""
        if self.lengths is None:
            self.lengths = {t["index"]: t["seconds"] for t in self.v.titles()}
        return self.lengths.get(title, 0) >= FILM_LENGTH

    def await_title(self, limit=150):
        """Wait (up to limit seconds) for VLC to play one of the disc's titles (past its warnings)."""
        t0 = time.time()
        while self.v.state()["title"] == 0 and time.time() - t0 < limit:
            pause(0.25)

    def to_menus(self):
        """VLC's DVD Menu, from a title playing: the disc's menu for it (where the remote's Menu,
        or a title's own ban on it, may do nothing: WALL-E's film)."""
        self.v.menu()
        pause(4)

    def restart(self, start, where=None):
        """Back on the page the disc opens on, to start a route over: by VLC's DVD Menu, in
        seconds, while that's found to lead there; else the disc opened again (its warnings and
        all: WALL-E's take a minute and more). Only between pages: a fresh disc forgets the
        settings presses made (a commentary switch), which a button's answer can depend on."""
        if self.v is not None and self.menu_back is not False:
            try:
                self.v.menu()
                pause(2)
                found = self.find([start], timeout=MENU_BACK_WAIT + _lead_in(self.pages, start))
                if self.menu_back is None:
                    self.log("  DVD Menu leads back to %s: starting over that way" % (start,))
                self.menu_back = True
                return found
            except Lost:
                # (it lands elsewhere: a disc that opens on its VMG menu. Not from another title
                # set's pages, though: there DVD Menu leads to that one's (WALL-E's Home Theater))
                if self.menu_back is None and (where is None or where[0] == start[0]):
                    self.log("  DVD Menu doesn't lead back to %s: opening the disc again instead" % (start,))
                    self.menu_back = False
        self.close()
        self.open()
        return self.at_start(start)

    def await_menus(self, limit=90):
        """Wait (up to limit seconds) for VLC to be in the disc's menus, not one of its titles:
        the warnings and logos a disc plays first take no keys."""
        t0 = time.time()
        while self.v.state()["title"] != 0 and time.time() - t0 < limit:
            pause(0.25)

    def at_start(self, start):
        """Back at the page the disc opens on, a fresh session: (state, selected button)."""
        self.await_menus()
        if self.menu_skip:                      # past the page's intro animation (crawl)
            self.v.press("popup")
            pause(2)
        if self.start_select:                   # (Menu lands past the page's intro)
            return self.arrive(start, self.start_select, wait=0 if self.menu_skip else _lead_in(self.pages, start))
        return self.find([start], timeout=HINT_WAIT)

    def arrive(self, st, sel, timeout=HINT_WAIT, wait=0):
        """On page st with button sel highlighted, as the disc's program says: (st, sel), once a
        round trip from sel shows its buttons answer (a page's buttons may come on late; and it
        checks the program was right), twice over. Else, the usual way (find), and the miss
        counted: how often the program places the highlight right is what this tries out.
        wait: seconds of the page's intro first (the disc's program says), no keys while its
        buttons come on: keys then can move the highlight half a round trip."""
        by = {b["n"]: b for b in _state_buttons(self.pages, st)}
        if sel in by and _unseen(by):
            # no move there would show: the program's word, untested (WALL-E's Home Theater pages:
            # Back and Next 4 pixels square). A wrong word shows at the next page's arrival
            pause(wait + 1)
            if st not in self.reading:
                self.log("  page %s of title set %s: its highlight is too small to see: taking the disc's "
                         "program's word for it" % (st[1], st[0]))
                self._read(st, self.v.frame(), None, None)
            return st, sel
        trips = [(k, back) for k, back in self.ROUND_TRIPS if sel in by and by[sel][k] in by and
                 by[sel][k] != sel and by[by[sel][k]][back] == sel and not by[by[sel][k]]["auto"]]
        if not trips:
            return self.find([st], timeout=timeout)
        pause(wait + 1)
        t0, i, playing = time.time(), 0, None
        while time.time() - t0 < timeout:
            now = self.v.state()["title"]
            if now != 0:
                # a title before the page (Muppets from Space: its outtakes' intro, the first time):
                # the page's time starts once the menus are back; a film, though, isn't waited out
                playing = playing or time.time()
                if time.time() - playing > TITLE_WAIT or self.feature_length(now):
                    break
                pause(1)
                t0 = time.time()
                continue
            key, back = trips[i % len(trips)]
            i += 1
            res, a, b = self._round_trip([st], key, back, weak=True)
            scores = {(n, m): sc for sc, s, n, m in res}
            m = by[sel][key]
            if scores.get((sel, m), -1) < SEEN:
                moved = self._moved(st, sel, res, key, back, a, b, ours=wait > 0)
                if moved:
                    return st, moved
            if scores.get((sel, m), -1) >= SEEN:
                again, _, _ = self._round_trip([st], key, back, weak=True)
                if dict(((n, m2), sc) for sc, s, n, m2 in again).get((sel, m), -1) >= SEEN:
                    # a round trip that fits from the other button too (two leading to each other:
                    # FastPlay | Main Menu) doesn't say which: the one lit before the press does
                    if scores.get((m, sel), -1) >= SEEN and self._lit(st, a, b, m, sel):
                        self.placed[1] += 1
                        self.log("  the highlight is on button %d of %s, not %d as the disc's program said"
                                 % (m, st, sel))
                        self._read(st, a, b, m)
                        return st, m
                    self.placed[0] += 1
                    self._read(st, a, b, sel)
                    return st, sel
            pause(0.3)
        if playing and self.v.state()["title"] != 0:
            raise Lost("VLC is playing a title, not in the menus")     # (no page to look round)
        self.placed[1] += 1
        self.log("  the highlight isn't where the disc's program said (%s, button %d): looking" % (st, sel))
        try:
            return self.find([st], timeout=20)
        except Lost:
            if not self.states:
                raise
        # not the page the program said at all (Muppets from Space: after its outtakes' intro, the
        # disc goes elsewhere than our run of its program does): which page is it?
        found = self.find(self.states, timeout=30)
        self.log("  it's %s instead" % (found[0],))
        return found

    def _moved(self, st, sel, res, key, back, a, b, ours=True):
        """The button the highlight is really on, when a round trip on page st fits from another
        one than sel, and again; else None. ours: the page had an intro, so keys pressed as its
        buttons came on may have moved it half a round trip (else the disc's program was wrong)."""
        other = next(((n, m) for sc, s, n, m in res if sc >= SEEN and n != sel), None)
        if not other:
            return None
        again, _, _ = self._round_trip([st], key, back, weak=True)
        n, m = other
        if dict(((x, y), sc) for sc, s, x, y in again).get((n, m), -1) < SEEN:
            return None
        if any(sc >= SEEN and (x, y) == (m, n) for sc, s, x, y in res) and self._lit(st, a, b, m, n):
            n = m                                   # (a fit both ways: the one lit before the press)
        if n == sel:                                # (where the program said, after all)
            self.placed[0] += 1
            self._read(st, a, b, n)
            return n
        if ours:
            self.placed[2] += 1
            self.log("  the highlight moved to button %d of %s (from %d: keys pressed as its buttons came on)"
                     % (n, st, sel))
        else:
            self.placed[1] += 1
            self.log("  the highlight is on button %d of %s, not %d as the disc's program said" % (n, st, sel))
        self._read(st, a, b, n)
        return n

    def menu_call(self):
        """Press Menu (the remote's), for a disc that plays its film by itself: its menus
        (a few seconds in, after its first warnings)."""
        pause(2)
        self.v.press("popup")
        pause(4)
        if self.v.state()["title"] != 0:        # (the press did nothing: VLC's DVD Menu instead)
            self.to_menus()

    def close(self):
        if self.v:
            self.v.close()
            self.v = None

    ROUND_TRIPS = (("down", "up"), ("right", "left"), ("up", "down"), ("left", "right"))

    def _lit(self, st, before, after, n, m):
        """Whether button n, not m, was the highlighted one in before (m in after). A disc's
        highlight is one colour wherever it's drawn: what changed at n in before looks like what
        changed at m in after if n was lit first (else what changed at n in after looks like m's
        before). Light or dark alike: Wallace & Gromit's is a black asterisk by the button's name.
        Without a change to compare, the lighter one (a highlight lights a button, as a rule)."""
        from PIL import ImageDraw, ImageStat
        by = {b["n"]: b for b in _state_buttons(self.pages, st)}
        changed = _diff_mask(before, after)

        def region(a, b):                       # (what changed in button a's place, not in b's)
            ra, rb = by[a]["rect"], by[b]["rect"]
            mask = changed.crop(tuple(ra)).copy()
            ImageDraw.Draw(mask).rectangle([rb[0] - ra[0], rb[1] - ra[1], rb[2] - ra[0], rb[3] - ra[1]], fill=0)
            return mask if mask.histogram()[255] >= SEEN // 3 else None

        rn, rm = region(n, m), region(m, n)
        colour = lambda img, k, mask: ImageStat.Stat(img.crop(tuple(by[k]["rect"])), mask=mask).mean
        far = lambda x, y: sum((p - q) ** 2 for p, q in zip(x, y))
        if rn is not None and before.info.get("dark"):
            # over a flat grey (Vlc.dark), an unlit button is that grey: the lit one stands out
            # from it (light or dark alike: white over black, Wallace & Gromit's black over white)
            grey = [before.info.get("level", 0)] * 3
            return far(colour(before, n, rn), grey) > far(colour(after, n, rn), grey)
        if rn is not None and rm is not None:
            n_first = far(colour(before, n, rn), colour(after, m, rm))
            m_first = far(colour(after, n, rn), colour(before, m, rm))
            if n_first != m_first:
                return n_first < m_first
        mean = lambda img, k: ImageStat.Stat(img.convert("L").crop(tuple(by[k]["rect"]))).mean[0]
        return (mean(before, n) - mean(after, n)) - (mean(before, m) - mean(after, m)) > 0

    def _returns(self, st):
        """Whether a round trip can show a page: one of its buttons leads to another that leads back."""
        by = {b["n"]: b for b in _state_buttons(self.pages, st)}
        return any(b[key] in by and b[key] != n and by[b[key]][back] == n and not b["auto"]
                   and not by[b[key]]["auto"] for n, b in by.items() for key, back in self.ROUND_TRIPS)

    # Highlight checks over the video darkened first (Vlc.dark): the highlight is the subpicture's,
    # drawn after VLC's filters, so it shows over black with nothing moving behind it (WALL-E's
    # main menu: a small dot over sweeping lights, not seen in 75 s of round trips). As it is,
    # when the dark view shows nothing: a highlight in the video itself, or buttons not on yet.
    DARK_MISSES = 3         # times the highlight showed as it is but not darkened: not for this disc
    AS_IS_EVERY = 3         # once shown darkened: as it is too, every this many misses in a row

    def _round_trip(self, candidates, key, back, weak=False):
        """_round_trip_as_is over the video darkened, else as it is (a round trip leaves the
        highlight where it was: tried twice, no harm)."""
        t0 = time.time()
        try:
            return self._round_trip_dark_first(candidates, key, back, weak)
        finally:
            self.timed("highlight checks", t0)

    def _round_trip_dark_first(self, candidates, key, back, weak=False):
        # a page whose highlight showed only as it is: looked at that way first (Wallace & Gromit:
        # the Shorts page's white asterisk shows darkened, the first menu's black one doesn't; a
        # check that sees it once in three never gets the two in a row a page's arrival wants)
        seen = getattr(self, "seen_as_is", set())
        if self.v is not None and candidates and all(st in seen for st in candidates):
            self._as_is()
            res = self._round_trip_as_is(candidates, key, back, weak)
            if res[0] and res[0][0][0] >= SEEN:
                return res
        if self.v is not None and getattr(self, "dark_ok", None) is not False:
            res = self._darkly(self._round_trip_as_is, candidates, key, back, weak)
            if res[0] and res[0][0][0] >= SEEN:
                if not self.dark_ok:
                    self.log("  the highlight shows over the video darkened: looking for it that way")
                    self.dark_ok = True
                self.dark_streak = 0
                return res
            # shown darkened on this disc before: a miss is a page's buttons not on yet (an intro),
            # as a rule; as it is only now and then, for a page whose highlight is in its video
            # (each a round trip more: The Iron Giant's intros, 5 s a page later to their buttons)
            self.dark_streak = getattr(self, "dark_streak", 0) + 1
            if self.dark_ok and self.dark_streak % self.AS_IS_EVERY:
                return res
            self._as_is()
            res = self._round_trip_as_is(candidates, key, back, weak)
            if res[0] and res[0][0][0] >= SEEN:
                self.seen_as_is = seen | {res[0][0][1]}
            if res[0] and res[0][0][0] >= SEEN and not self.dark_ok:
                self.dark_missed += 1
                if self.dark_missed >= self.DARK_MISSES:
                    self.log("  the highlight doesn't show over the video darkened (drawn in the video): "
                             "looking as before")
                    self.dark_ok = False
            return res
        self._as_is()
        return self._round_trip_as_is(candidates, key, back, weak)

    def timed(self, what, t0):
        """Add the seconds since t0 to what's been spent on what (the log's tally at the end)."""
        spent = getattr(self, "spent", None)
        if spent is not None:
            spent[what] = spent.get(what, 0.0) + time.time() - t0

    def _darkly(self, check, *args):
        """check, over the video darkened; left darkened for the next (each switch waits for a
        few frames: switched round each check, The Iron Giant's 13 pages took 20 s more). Back
        as it is for what needs the picture: a page read (_read), a check as it is (_as_is).
        Over the grey that shows the pages' highlights best (_backdrop)."""
        level = self._backdrop(args[0] if args else [])
        if not self.v.is_dark or getattr(self.v, "dark_level", level) != level:
            t0 = time.time()
            self.v.dark(True, level)
            self.timed("darkening", t0)
        return check(*args)

    BACKDROPS = (0, 255, 128)     # black, white, mid-grey: the first that shows them best

    def _backdrop(self, candidates):
        """The flat grey a highlight check looks over, for pages candidates: the one farthest from
        every colour their buttons light up in (the disc says: _nav_buttons' lit, each page's
        palette), by the most-changed colour as _diff_mask sees it. Wallace & Gromit's first menu
        lights a black asterisk: white; its Shorts page a white one: black. Colours unknown (menu
        data read before they were kept), or light and dark both: mid-grey, which shows either."""
        colours, pages = [], getattr(self, "pages", None) or {}
        for st in candidates or []:
            palette = (pages.get(st[:2]) or {}).get("palette") or []
            for b in _state_buttons(pages, st):
                colours += [palette[c] for c in b.get("lit") or [] if c < len(palette)]
        if not colours:
            return 128
        gap = lambda level: min(max(abs(v - level) for v in c) for c in colours)
        return max(self.BACKDROPS, key=gap)

    def _as_is(self):
        """The video as it is again (if darkened)."""
        if self.v is not None and getattr(self.v, "is_dark", False):
            t0 = time.time()
            self.v.dark(False)
            self.timed("darkening", t0)

    def _round_trip_as_is(self, candidates, key, back, weak=False):
        spent = getattr(self, "spent", None)
        if spent is not None:
            spent["round trips"] = spent.get("round trips", 0) + 1
        return self._round_trip_frames(candidates, key, back, weak)

    def _round_trip_frames(self, candidates, key, back, weak=False):
        """Press an arrow key and its opposite: the highlight goes to a button and comes back.
        ([(score, state, was, there)] best first, a frame with was highlighted, one with there).
        What counts went one way and came back: different from the frames before and after in
        both frames between, and the same before and after. A menu's moving scenery (Enchanted's
        fountain) doesn't come back, so it drops out. A move fits a state if its buttons say so
        both ways (key from was gets there, back from there gets to was)."""
        from PIL import ImageChops, ImageFilter
        v, d = self.v, _diff_mask
        a1 = v.frame()
        pause(0.1)
        a2 = v.frame()
        v.press(key)
        pause(0.3)
        b1 = v.frame()
        pause(0.15)
        b2 = v.frame()
        v.press(back)
        pause(0.3)
        c1 = v.frame()
        pause(0.1)
        c2 = v.frame()
        went = d(a2, b1)
        for m in (d(a2, b2), d(c1, b1), d(c1, b2)):
            went = ImageChops.multiply(went, m)
        for m in (d(a1, a2), d(b1, b2), d(c1, c2), d(a2, c1)):
            went = ImageChops.subtract(went, m)
        # specks out, two ways: lightly (a highlight that recolours thin lettering survives) and
        # heavily (the solid icon a highlight often is survives; scenery's thin glints don't)
        masks = [went.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k)) for k in (3, 7)]
        if weak:                                # (one page looked for: unfiltered too, for a highlight
            masks.append(went)                  # that's a thin line: The Sorcerer's Apprentice's underline)
        best = {}
        for st in candidates:
            by = {b["n"]: b for b in _state_buttons(self.pages, st)}
            # (weak: only what changes near the buttons counts against a move, within a band round
            # them all: behind them, Spider-Man's street changes thousands of pixels a round trip)
            rs = [b["rect"] for b in by.values()]
            near = [max(0, min(r[0] for r in rs) - NEAR), max(0, min(r[1] for r in rs) - NEAR),
                    max(r[2] for r in rs) + NEAR, max(r[3] for r in rs) + NEAR]
            for mask in masks:
                cnt = {n: _count(mask, b["rect"]) for n, b in by.items()}
                outside = mask.histogram()[255] - _count_in(mask, [b["rect"] for b in by.values()])
                for n, b in by.items():
                    m = b[key]
                    if m in by and m != n and by[m][back] == n and not by[m]["auto"]:
                        # both buttons changed; more than any other one did (a glint in the scenery
                        # behind it), outside these two (WALL-E's main menu: buttons 8 and 9 overlap
                        # 3 and 4, and the dot moving between those changed them as much); and
                        # little changed outside all the buttons
                        rest = max([_count_beyond(mask, by[x]["rect"], (b["rect"], by[m]["rect"])) if
                                    _overlaps(by[x]["rect"], b["rect"]) or _overlaps(by[x]["rect"], by[m]["rect"])
                                    else cnt[x] for x in by if x not in (n, m)] + [0])
                        score = min(cnt[n], cnt[m]) - rest - outside
                        if weak:
                            # (looking for one page) or one of them, at half weight: a button can
                            # have no highlight to see (Spider-Man's Play Movie oval; the gem above
                            # it lights up); and only the change near the buttons counted outside
                            around = _count(mask, near) - _count_in(mask, rs)
                            score = max(score, max(cnt[n], cnt[m]) // 2 - rest - min(outside, around))
                        best[(st, n, m)] = max(score, best.get((st, n, m), score))
        out = sorted(((score,) + k for k, score in best.items()), key=lambda x: -x[0])
        return out, a2, b2

    def _press(self, candidates, key, only=None):
        """_press_as_is, over the video darkened once that's shown the highlight on this disc (a
        press moves the highlight: not tried twice)."""
        seen = getattr(self, "seen_as_is", set())
        if self.v is not None and getattr(self, "dark_ok", None) and \
                not (candidates and all(st in seen for st in candidates)):     # (as _round_trip_dark_first)
            return self._darkly(self._press_as_is, candidates, key, only)
        self._as_is()
        return self._press_as_is(candidates, key, only)

    def _press_as_is(self, candidates, key, only=None):
        """Press an arrow key; ([(score, state, was, now)] best first, frame before, frame after).
        only: (state, was) to score just that move. What counts is a step: pixels steady in a
        burst of frames before the press, steady in one after, and different between them (the
        menu's moving video isn't steady). A move scores the fewer of its two buttons' step
        pixels (both must change), less the step pixels outside both (unexplained)."""
        from PIL import ImageChops
        before = self.v.burst(8, 0.25)          # 2s: slow menu video shows itself too
        self.v.press(key)
        pause(0.25)
        after = self.v.burst()
        # whatever has moved on this page lately (slideshows), over the last few presses only: an
        # intro moves everything, the buttons' words included, and those must count again later
        moved = _diff_mask(before[0], before[1])
        for b in before[2:]:
            moved = ImageChops.lighter(moved, _diff_mask(before[0], b))
        self.moving = (self.moving or [])[-2:] + [moved]
        step = _diff_mask(before[-1], after[0])
        for m in self.moving:
            step = ImageChops.subtract(step, m)
        for a, b in zip(after, after[1:]):
            step = ImageChops.subtract(step, _diff_mask(a, b))
        # only solid shapes count: a highlight is a blob; sparkles in animated scenery (a fountain
        # behind Enchanted's menu) are specks, gone once the mask is worn down and grown back
        from PIL import ImageFilter
        step = step.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
        total = step.histogram()[255]
        out = []
        for st in candidates:
            by = {b["n"]: b for b in _state_buttons(self.pages, st)}
            cnt = {n: _count(step, b["rect"]) for n, b in by.items()}
            for n, b in by.items():
                m = b[key]
                if m in by and m != n and (only is None or only == (st, n)):
                    r, q = b["rect"], by[m]["rect"]
                    both = _count(step, [max(r[0], q[0]), max(r[1], q[1]), min(r[2], q[2]), min(r[3], q[3])])
                    out.append((min(cnt[n], cnt[m]) - (total - (cnt[n] + cnt[m] - both)), st, n, m))
        out.sort(key=lambda x: -x[0])
        return out, before[-1], after[-1]

    def _safe_trips(self, candidates):
        """The round trips find may press, the highlight's button unknown: those that can't land
        on an auto-action button (selecting it presses it) from any button of these pages, going
        or coming back; none such, the least risky first. Muppets from Space's Special Features:
        "down" from ON/OFF to the music video, then "up" from there, lands on its auto button
        (a speck under LIVE VIDEO COMMENTARY), which re-opens the page: the highlight moved
        where nothing said, every press after it one button off (the music video played for
        OUTTAKES)."""
        def risk(trip):
            key, back = trip
            hits = 0
            for st in candidates:
                by = {b["n"]: b for b in _state_buttons(self.pages, st)}
                for n, b in by.items():
                    if b["auto"]:
                        continue
                    m = by.get(b[key])
                    if m is not None and (m["auto"] or (by.get(m[back]) or {}).get("auto")):
                        hits += 1
            return hits
        scored = sorted(self.ROUND_TRIPS, key=risk)
        safe = [t for t in scored if risk(t) == 0]
        return safe or scored

    def find(self, candidates, timeout=40):
        """(state, selected button), by sending the highlight to a neighbour and back (an arrow
        key and its opposite: _round_trip) and seeing where it went. Every page that round trip
        fits stays a suspect; more round trips rule out the rest, until one page's layout is left.
        Keeps trying while the page's buttons aren't on yet (intros); the one-way way (_find_one_way)
        is tried too, now and then, for a menu whose buttons don't lead back."""
        st = candidates[0]
        if len(candidates) == 1 and len(_state_buttons(self.pages, st)) == 1:
            return self._find_one_way(candidates)
        layout = lambda s: tuple(tuple(b["rect"]) for b in _state_buttons(self.pages, s))
        trips = self._safe_trips(candidates)
        t0, i = time.time(), 0
        while time.time() - t0 < timeout:
            key, back = trips[i % len(trips)]
            i += 1
            weak = len(candidates) == 1         # (one page expected: a looser fit, confirmed below)
            res, a, b = self._round_trip(candidates, key, back, weak)
            fits = {}
            for score, s, n, m in res:
                if score >= SEEN and s not in fits:
                    # two buttons that lead to each other both ways (a chooser's two: FastPlay, Main
                    # Menu) fit a round trip from either: the one lit before the press is
                    if (score, s, m, n) in res and self._lit(s, a, b, m, n):
                        n = m
                    fits[s] = n
            if fits and weak:                   # the same round trip again: a fit, again
                again, _, _ = self._round_trip(list(fits), key, back, weak)
                ok = {(s, n) for score, s, n, _ in again if score >= SEEN}
                fits = {s: n for s, n in fits.items() if (s, n) in ok}
            if not fits:
                # every way tried: the one-way way, once, for a page whose buttons don't lead back
                # (not otherwise: it would only lose time while a page's buttons are coming on)
                if i % 4 == 0 and not all(self._returns(s) for s in candidates):
                    try:
                        return self._find_one_way(candidates, timeout=10)
                    except Lost:
                        pass
                pause(0.3)
                continue
            for j in range(1, 4):               # more round trips, ruling pages out
                if len({layout(s) for s in fits}) == 1:
                    break
                k2, b2 = trips[(i + j) % len(trips)]
                res2, _, _ = self._round_trip(list(fits), k2, b2)
                scores = {(s, n): score for score, s, n, _ in res2}
                fits = {s: n for s, n in fits.items() if scores.get((s, n), SEEN) >= SEEN}
                if not fits:
                    break
            if fits and len({layout(s) for s in fits}) == 1:
                s = next(iter(fits))
                self._read(s, a, b, fits[s])
                return s, fits[s]
        raise Lost("couldn't find the highlight on %s" % (st,) if len(candidates) == 1
                   else "couldn't tell which menu VLC is on")

    def _find_one_way(self, candidates, timeout=40):
        """find(), by single arrow presses: every page a first press fits stays a suspect; each
        further press rules out those whose buttons say the highlight should have gone elsewhere,
        until one page's layout is left (Muppets from Space: its Theatrical Trailers page and its
        Outtakes' second page fit a first press alike)."""
        st = candidates[0]
        if len(candidates) == 1 and len(_state_buttons(self.pages, st)) == 1:
            pause(6)
            self._read(st, self.v.frame(), None, None)
            return st, _state_buttons(self.pages, st)[0]["n"]
        self.moving = None                      # a new page: learn what moves on it afresh
        layout = lambda s: tuple(tuple(b["rect"]) for b in _state_buttons(self.pages, s))
        t0, i = time.time(), 0
        while time.time() - t0 < timeout:
            key = ("down", "right", "up", "left")[i % 4]
            i += 1
            best, f1, f2 = self._press(candidates, key)
            # the suspects: each page's best fit for that move, if it's a fit at all
            fits = {}
            for score, s, was, now in best:
                if score >= SEEN and s not in fits:
                    fits[s] = (was, now)
            if not fits:
                pause(0.3)
                continue
            first = dict(fits)                  # (for reading the page: the move the answer made)
            for _ in range(6):                  # more presses, each ruling out pages it doesn't fit
                s0, (was0, now0) = next(iter(fits.items()))
                by = {b["n"]: b for b in _state_buttons(self.pages, s0)}
                # a key that moves the highlight on (not back, if there's a choice), never onto an
                # auto-action button
                moves = [k for k in ("down", "up", "right", "left")
                         if by[now0][k] in by and by[now0][k] != now0 and not by[by[now0][k]]["auto"]]
                k2 = next((k for k in moves if by[now0][k] != was0), moves[0] if moves else None)
                if not k2:
                    fits = {}                   # a guess that can't be checked isn't one
                    break
                check, _, _ = self._press(list(fits), k2)
                scores = {(s, w): sc for sc, s, w, _ in check}
                kept = {}
                for s, (was, now) in fits.items():
                    b2 = {b["n"]: b for b in _state_buttons(self.pages, s)}
                    nxt = b2[now][k2]
                    if nxt in b2 and nxt != now and scores.get((s, now), -1) >= SEEN:
                        kept[s] = (now, nxt)
                fits = kept
                if not fits or len({layout(s) for s in fits}) == 1:
                    break
            if fits and len({layout(s) for s in fits}) == 1:
                s = next(iter(fits))
                was = first.get(s, (None,))[0]
                self._read(s, f1, f2, was)
                return s, fits[s][1]
        raise Lost("couldn't find the highlight on %s" % (st,) if len(candidates) == 1
                   else "couldn't tell which menu VLC is on")

    def _read(self, st, f1, f2, was):
        """Read a state's buttons and heading, once, in the background while VLC goes on to the
        next page (words() waits for a page's when they're needed). From a frame without the
        subpicture: no highlight over the words, and the page's picture too; the frame as it
        is (and over black) only when its words turn out to be in the subpicture; and a plate's
        words only for a button read poorly. (Each reading costs: The Iron Giant, every reading
        every way, 0.8 minutes more over 13 pages, not a name changed.)"""
        if st in self.reading:
            return
        dark = None
        if f1.info.get("dark") or (self.v is not None and getattr(self.v, "is_dark", False)):
            # (checked over the video darkened: that's the page's frame over black; its picture
            # as it is to read)
            if getattr(self.v, "is_dark", False):
                dark = self.v.frame()
            self._as_is()
            f1, f2 = self.v.frame(), None
        f1.save(os.path.join(self.out_dir, "page_%s_%d_%d.png" % st))
        btns = _state_buttons(self.pages, st)
        rects = [b["rect"] for b in btns]
        bare = None
        t0 = time.time()
        if self.v is not None:
            bare = self.v.bare_frame()
            if bare is not None and dark is None:   # (and the subpicture alone, over black, for later)
                self.v.dark(True)
                try:
                    dark = self.v.frame()
                finally:
                    self.v.dark(False)
        if bare is not None:
            first = {b["n"]: self.pool.submit(button_words, bare, b["rect"], rects) for b in btns}
            self.timed("page frames", t0)
        else:                                   # (no subpicture to take away: as it is, the
            first = {b["n"]: self.pool.submit(button_words, f1, b["rect"], rects)   # lit button
                     if b["n"] != was or f2 is None             # from a frame it isn't lit in)
                     else self.pool.submit(_better_reading, f2, f1, b["rect"], rects) for b in btns}
        self.reading[st] = first
        self.finished[st] = self.more.submit(self._finish, st, f1, f2, was, bare, dark, first)
        self.log("  questioning page %s of title set %s (%d of %d)" % (st[1], st[0], len(self.reading),
                                                                         max(len(self.reading), self.expect)))
        self.progress("page", len(self.reading), max(len(self.reading), self.expect))

    def _finish(self, st, f1, f2, was, bare, dark, first):
        """(In the background) A page's words, once its first readings are in: {"words": {button:
        text}, "overlay": whether its words are in the subpicture, "picture": the picture they were
        read off and its lines (its heading's, once what each button does is known: page_words)}. Most of its buttons
        read poorly without the subpicture: read as it is too, and its words are in the
        subpicture if most of those that read as words then are gone without it (WALL-E's Bonus
        Features: read a little worse, 175 to 169 by _score over its moving backdrop, isn't gone).
        In the subpicture: read over black too, the better kept, and the page's picture as it is.
        Then a plate's reading for each button still read poorly (_plate_or)."""
        btns = _state_buttons(self.pages, st)
        rects = [b["rect"] for b in btns]
        words = {n: f.result() for n, f in first.items()}
        overlay = False
        if bare is not None:
            if 2 * sum(1 for t in words.values() if _score(t) < WORDED) > len(words):
                lit = {b["n"]: self.pool.submit(button_words, f1, b["rect"], rects) if b["n"] != was or f2 is None
                       else self.pool.submit(_better_reading, f2, f1, b["rect"], rects) for b in btns}
                lit = {n: f.result() for n, f in lit.items()}
                worded = [n for n, t in lit.items() if _score(t) >= WORDED]
                gone = [n for n in worded if _score(words.get(n, "")) <= 0]
                overlay = bool(worded) and 2 * len(gone) > len(worded)
                if overlay:
                    self.log("  page %s of title set %s: its words are in the subpicture" % (st[1], st[0]))
                    words = lit
                    if dark is not None:        # (over black, no scenery behind them: the better)
                        alone = {b["n"]: self.pool.submit(button_words, dark, b["rect"], rects) for b in btns}
                        words = {n: max((t, alone[n].result()), key=_score) for n, t in lit.items()}
                else:
                    # (a tie: without the subpicture, no highlight round the words: Spider-Man 2's
                    # yellow pill read as brackets, "(Costume Design)")
                    words = {n: max((t, lit.get(n, "")), key=_score) for n, t in words.items()}
            if not overlay:
                bare.save(os.path.join(self.out_dir, "page_%s_%d_%d.png" % st))
        # (or a button too narrow to hold its name: a pointer beside it, its reading cut short,
        # however wordy: Spider-Man's "STAN LEES MUTANTS:", 15 by _score)
        weak = [i for i, b in enumerate(btns) if _score(words.get(b["n"], "")) < PLATE_WORDS
                or b["rect"][2] - b["rect"][0] < TINY]
        if weak:
            got = plate_words(bare if bare is not None and not overlay else f1, rects, only=weak)
            words = {n: _plate_or(t, got.get(next(i for i, b in enumerate(btns) if b["n"] == n)))
                     for n, t in words.items()}
        pic = bare if bare is not None and not overlay else f1
        return {"words": words, "overlay": overlay, "picture": (pic, _page_lines(pic))}

    def word_now(self, st, n):
        """A button's words if they've been read already, else "" (without waiting for them): as
        finished, or its first reading meanwhile."""
        if st in self.finished and self.finished[st].done():
            return self.words(st).get(n, "")
        f = self.reading.get(st, {}).get(n)
        return f.result() if f is not None and f.done() else ""

    def read(self, st):
        """Whether a page's first readings are in (no waiting on OCR): enough to choose a route
        by ("Languages", "Set Up"); the rest (_finish: plates, the frame as it is) need not hold
        the tour up (The Iron Giant: 0.5 minutes over 13 pages, waiting on plates)."""
        return st in self.reading and all(f.done() for f in self.reading[st].values())

    def first_words(self, st):
        """A page's buttons' first readings, waiting for them (not for the rest: read())."""
        t0 = time.time()
        got = {n: f.result() for n, f in self.reading.get(st, {}).items()}
        self.timed("waiting for words", t0)
        return got

    def words(self, st):
        """A page's buttons' words, {button: text} ({} for a page not read), once they're read
        (_finish)."""
        if st not in self.texts and st in self.finished:
            t0 = time.time()
            got = self.finished[st].result()
            self.timed("waiting for words", t0)
            self.texts[st], self.overlay[st] = got["words"], got["overlay"]
            self.pictures[st] = got["picture"]
        return self.texts.get(st, {})

    def headings(self, st, buttons):
        """A page's words outside its buttons' names, its heading line, whether it has several
        (page_words): ("", None, False) for a page not read. buttons: its buttons as the crawl has them
        (what each does known): a name read beside a button counts as its only where it does something
        (Spider-Man 3's Special Features: a 31-pixel decoration by its heading, "SPECIAL FEATURES")."""
        self.words(st)
        if st not in self.pictures:
            return "", None, False
        pic, lines = self.pictures[st]
        return page_words(pic, name_rects(buttons), names=[b.get("text") or "" for b in buttons], lines=lines)

    def select(self, st, sel, n):
        """Move the highlight from button sel to n on a state."""
        moves = _moves(_state_buttons(self.pages, st), sel, n)
        if moves is None:
            raise Lost("no way from button %d to %d on %s" % (sel, n, st))
        for key in moves:
            self.v.press(key)
            pause(0.35)
        return n

    def follow(self, st, sel, route):
        """Press through a route, [(state, button)], from st with sel selected. Returns the
        (state, selected button) it ends on."""
        for state, n in route:
            if state != st:
                raise Lost("expected %s, at %s" % (state, st))
            self.select(st, sel, n)
            nxt = _goes(self.pages, st, {b["n"]: b for b in _state_buttons(self.pages, st)}[n], self.learned)
            self.v.press("activate")
            pause(1)
            expect = self.selects.get((state, n)) if self.learned.get((state, n)) == nxt else None
            if expect:                          # the disc's program says where the highlight is
                st, sel = self.arrive(nxt, expect, wait=self.intros.get((state, n), 0))
                if st != nxt:                   # it led elsewhere this time: carry on from there. (Not
                    return st, sel              # learned: the first press can differ from the next, as
                                                # Muppets from Space plays its outtakes' intro just once)
            else:
                st, sel = self.find([nxt], timeout=HINT_WAIT)   # (a page's buttons may come on late)
        return st, sel

    def watch(self, lengths, limit=120):
        """Press the selected button and follow what plays: [{"title", "chapter", "at"}], one entry
        per title VLC passes through (0: the menus). Ends once a long title has played 3s or any
        other title 15s, or the menus have held 15s (a commentary's "On" plays a menu clip first).
        A title under a minute is watched to its end: likely a notice before the promised title."""
        self.v.press("activate")
        t0 = since = time.time()
        chain = []
        while time.time() - t0 < limit:
            pause(0.5)
            s = self.v.state()
            if not chain or chain[-1]["title"] != s["title"]:
                chain.append({"title": s["title"], "chapter": s["chapter"] + 1 if s["title"] > 0 else None,
                              "at": round(time.time() - t0, 1)})
                since = time.time()
            elif time.time() - since < 2 and s["title"] > 0:
                chain[-1]["chapter"] = s["chapter"] + 1       # VLC counts chapters from 0
            held = time.time() - since
            seconds = lengths.get(s["title"]) or 0
            if s["title"] > 0 and (held > 3 and seconds >= 60 or held > 15 and not 0 < seconds < 60):
                break
            if s["title"] <= 0 and held > 15:
                break
        return chain


def no_progress(stage, k=0, n=0):
    """A crawl's progress, told to nobody (CRAWL_STAGES: what a crawl tells, a DVD's and a Blu-ray's alike)."""


# What a crawl tells as it goes (progress(stage, k, n)), a DVD's (menus.crawl, VLC) and a Blu-ray's (bdhdmv.read,
# the library; bdcrawl.crawl, VLC) alike, for the app to put in its own words: "setup", the disc's menus got
# ready (a DVD's menu data read, a Blu-ray opened and unlocked); "start", the crawl begun; "page", the k-th
# page of n read; "press", the k-th button of a round of n pressed (a player's: what it plays watched); "wait",
# a moment spent waiting on the player (nothing to tell: a chance to call the crawl off, its disc taken out).
CRAWL_STAGES = ("setup", "start", "page", "press", "wait")


_waiting = threading.local()   # (the crawl on this thread: told each time it waits on its player)


def pause(seconds):
    """time.sleep, the crawl running on this thread told first that it's waiting (its progress "wait"):
    each wait on VLC a chance to call it off, or to find its disc taken out (a wait for a menu that
    never comes, on a disc that's gone, took minutes)."""
    told = getattr(_waiting, "tell", None)
    if told is not None:
        told()
    time.sleep(seconds)


def waits_told(progress):
    """For a crawl's run, on this thread: each pause() tells progress("wait"). Use with `with`."""
    class _Told:
        def __enter__(self):
            self.before = getattr(_waiting, "tell", None)
            _waiting.tell = lambda: progress("wait")

        def __exit__(self, *exc):
            _waiting.tell = self.before
    return _Told()


def crawl(drive, vlc_dir, structure, out_dir, log=print, everything=False, is_word=None, tv=False, every_title=False,
          progress=no_progress):
    """_crawl, with is_word (a dictionary: core.word_checker) to judge Windows' OCR readings by
    (WORDS) while it runs. tv: a TV disc's, its episode pages read too. every_title: the pages of
    copies and Play Alls too, for a picture of each. progress: CRAWL_STAGES, as they come."""
    global WORDS
    WORDS = is_word
    readings_begin()                            # (this stakeout's readings: how sure of each)
    progress("start")
    try:
        with waits_told(progress):
            return _crawl(drive, vlc_dir, structure, out_dir, log, everything, tv, every_title, progress)
    finally:
        WORDS = None
        # VLC let go of, however it ended (an error, the disc taken out mid-stakeout): no player
        # left running on the drive
        while _crawlers:
            _crawlers.pop().close()


_crawlers = []        # the stakeout's VLC sessions (_Crawler), closed when it's over


def _crawl(drive, vlc_dir, structure, out_dir, log=print, everything=False, tv=False, every_title=False,
           progress=no_progress):
    """The menu buttons that matter: their words (OCR) and where they go, a page or what they
    play. In rounds: a tour through the pages there's a way to, reading their buttons; then each
    button whose result the disc doesn't state outright is pressed, from a fresh start, and what
    plays is watched. A button that stays in the menus has gone to a page the disc decides on
    as it runs ("bonus material" on Father of the Bride Part II): which one is seen, and the
    next round reads it and presses its buttons. The titles decide what's needed (_leads_to_title),
    and a page is passed by once its titles are named some other way; words only say what to look at
    first (_search_rank). Copies of a title found and the scan's play-alls have nothing to tell, so no
    page is read for them, unless every_title (a picture for each). Unless everything: then every page
    and button. Writes crawl.json, crawl.txt
    and a picture of each page read into out_dir, and, kept as test data, the disc's menu data
    (keep_disc_data)."""
    os.makedirs(out_dir, exist_ok=True)
    for f in glob.glob(os.path.join(out_dir, "page_*.png")):
        os.remove(f)
    if keeping_test_data():
        try:
            keep_disc_data(drive, structure, out_dir)
        except OSError as e:
            log("  couldn't keep the disc's menu data (%s)" % e)
    pages = _pages(structure)
    states = [(d, pgc, k) for (d, pgc), p in pages.items()
              for k, btns in enumerate(p.get("program_buttons") or [p["buttons"]], 1) if len(btns) >= 2]
    c = _Crawler(vlc_dir, dvd_mrl(drive), pages, out_dir, log, progress)
    _crawlers.append(c)
    c.states = states
    t0 = time.time()
    # The disc's own program, run without playing it (dvdvm): most buttons answered, and every
    # page a button leads to, before VLC presses anything; and where the disc starts, which VLC
    # is shown first (a disc that plays its film by itself: Menu pressed, as a viewer would).
    # What it can't follow, or answers differently by route, is left for VLC to press.
    began, engine, opening = {}, {}, []
    try:
        from . import dvdvm
        eng = dvdvm.Engine(dvdvm.Disc(drive, structure))
        began, engine, reached = dvdvm.explore(eng)
        opening = dvdvm.opening(eng)            # (a player's previews before the menu: VLC skips them)
        near = _reachable(states, began, engine, reached)
        if near is not None:
            states = c.states = near
            log("  looking only among the %d pages the disc's program reaches" % len(states))
    except Exception as e:                      # (a disc it can't read: VLC presses everything)
        log("  couldn't run the disc's program (%s): pressing instead" % e)
    c.menu_key = bool(began.get("menu_key"))
    if c.menu_key:
        log("  the disc plays its film by itself: pressing Menu to reach its menus")
    try:
        log("  opening the disc in VLC")
        c.open()
        log("  VLC is playing it (%.0f s): looking for the first menu" % (time.time() - t0))
        titles = c.v.titles()
        hint, start = tuple(began.get("goes_to") or ()), None
        if hint in states:
            # the disc gets there by itself: once VLC is in its menus, the page's buttons may still
            # be a while coming on (The Sorcerer's Apprentice's FastPlay chooser: ~35 s of animation).
            # When the disc's program says Menu leads back to that page, past the animation to its
            # buttons (a later program of the page), Menu is pressed there, as a viewer would
            c.menu_skip = not c.menu_key and hint[2] > 1 and tuple(began.get("menu_goes_to") or ()) == hint
            # the button the disc's program says is highlighted there (after Menu, if pressed)
            c.start_select = began.get("menu_select" if c.menu_skip else "select")
            try:
                start, sel = c.at_start(hint)
            except Lost:
                log("  not on the page the disc's program starts on: looking around")
        back = tuple(began.get("menu_goes_to") or ())
        if start is None and not c.menu_key and back in states and c.v.state()["title"] != 0:
            # VLC plays the film where the disc's program waits in a menu (WALL-E: libdvdnav's way
            # past its protection skips the menu): VLC's DVD Menu, to the page Menu leads to
            log("  VLC is playing the film instead: its DVD Menu")
            c.to_menus()
            c.film_first, c.menu_skip, c.start_select = True, False, began.get("menu_select")
            try:
                start, sel = c.at_start(back)
            except Lost:
                c.film_first = False
        if start is None:
            if c.menu_key:
                c.menu_call()                   # (the disc ignored the first press: warnings)
            start, sel = c.find(states)
    except Exception:
        c.close()
        raise
    log("the disc opens on %s page %s (program %s)" % start)
    length = {t["index"]: t["seconds"] for t in titles}
    routes, tries, known, pressed = {start: []}, {}, {}, {}
    # buttons staying in the menus, on a page depending on the way they were reached (known_outright)
    menus_by_route = {}
    if engine:
        if began.get("goes_to") and tuple(began["goes_to"]) != start:
            log("  the disc's program starts on %s, VLC on %s" % (began.get("goes_to"), start))
        # (a clip first or not: the same; so is the button highlighted on arrival, kept apart)
        same = lambda a: {k: v for k, v in a.items() if k not in ("before", "select", "intro", "after", "into", "run")}
        for (d, pgc, prog, n), answers in engine.items():
            s, a = (d, pgc, prog), answers[0]
            if all("plays" in x for x in answers) and len({x["plays"] for x in answers}) > 1:
                # a title or another by route (WALL-E's deleted scenes: their commentary versions
                # with its switch on): the first route's, and the others as other versions
                others, titles_seen = [], {a["plays"]}
                for x in answers[1:]:
                    if x["plays"] not in titles_seen:
                        titles_seen.add(x["plays"])
                        others.append({"title": x["plays"], "chapter": x["chapter"] or 1})
                known[(s, n)] = {"plays": {"title": a["plays"], "chapter": a["chapter"] or 1}, "engine": True,
                                 "others": others}
                continue
            one = _one_title_by_route(answers)
            if one:
                known[(s, n)] = {"plays": one, "engine": True}
                continue
            if any(same(x) != same(a) for x in answers) or "unsure" in a:
                if not any("plays" in x or "unsure" in x for x in answers):
                    menus_by_route[(s, n)] = {tuple(x["goes_to"]) for x in answers if "goes_to" in x}
                continue
            if "goes_to" in a and a["goes_to"] != s:
                c.learned[(s, n)] = tuple(a["goes_to"])
                there = {x.get("select") for x in answers}
                if len(there) == 1 and None not in there:   # (the same whatever the route: trusted)
                    c.selects[(s, n)] = there.pop()
                c.intros[(s, n)] = max(x.get("intro") or 0 for x in answers)
            elif "plays" in a:
                known[(s, n)] = {"plays": {"title": a["plays"], "chapter": a["chapter"] or 1}, "engine": True}
                if a.get("after"):              # (and those that play on after it: a run of sneak peeks)
                    known[(s, n)]["plays"]["after"] = a["after"]
                if a.get("into"):               # (and the film the run goes on into)
                    known[(s, n)]["plays"]["into"] = a["into"]
            else:
                known[(s, n)] = {"menus": True, "engine": True}
        answered = sum(1 for k in engine if (k[:3], k[3]) in known or (k[:3], k[3]) in c.learned)
        log("the disc's program answers %d of %d buttons" % (answered, len(engine)))

    def scene_page(s):
        # (a TV disc's, program by program: Smallville's Episodes page and its episodes' scene
        # pages are one page of the disc, and only the latter are scenes)
        return _scene_state(pages, s, known) if tv else _scene_page(structure, pages, s[:2], known)

    def named(hoping=False):
        # (hoping: as if the words still being read will name what their buttons play)
        word = (lambda s, n: c.word_now(s, n) or ("" if c.read(s) else "Being Read")) if hoping else c.word_now
        found = _named_titles(pages, c.reading, known, c.learned, pressed, word)
        return found if every_title else _with_copies(found, copies) | _play_alls_of(scanned)

    def no_way():
        """The titles sought that no button is known to play yet."""
        return sought - _titles_with_a_way(routes, known, pressed, opening)

    def passed_by(s):
        """Why a state wasn't worth a visit, or None."""
        if everything or s == start:
            return None
        found = named()
        visited = set(c.reading) | {x for x, _ in routes[s]} | {start}
        if _leads_to_title(pages, s, visited, found, known, c.learned, pressed, bool(no_way())):
            return None
        return "scenes" if scene_page(s) else "no title there that isn't named elsewhere"

    def known_outright():
        """What the buttons themselves say they do, so they needn't be pressed. And one whose page
        depends on the way it was reached, when there's another way to each of them: nothing to find
        by it (Rush Hour 3's BACK, from a version chooser to whichever scene page led there: half a
        minute's press). A page only it leads to, it's pressed for."""
        for k, there in menus_by_route.items():
            if k not in known and all(x in routes for x in there):
                known[k] = {"menus": True, "engine": True}
        for s in routes:
            for b in _state_buttons(pages, s):
                if not b["auto"] and not _goes(pages, s, b, c.learned, known) and (s, b["n"]) not in known:
                    k = _known(structure, pages, s, b)
                    if k:
                        known[(s, b["n"])] = k

    def page_rank(s):
        return _search_rank(routes[s], start, c.word_now, scene_page(s))

    def beyond_the_plan(done):
        """Pages the plan can't know of: all of them (everything), or, while a title has no way to
        it, those with buttons nobody knows, the disc's program not answering them."""
        cands = [s for s in routes if s not in done and all(x in c.reading for x, _ in routes[s])]
        if not everything:
            if not no_way():
                return []
            played = {k["plays"]["title"] for k in known.values() if "plays" in k}
            cands = [s for s in cands if _leads_to_title(pages, s, set(c.reading), played, known, c.learned, pressed, True)]
        return sorted(cands, key=lambda s: (page_rank(s), len(routes[s])))

    def tour(st, sel):
        """One session, page to page, by the plan (_page_plan). VLC moves on while OCR catches up, the
        words being read taken to name what their buttons play. At the plan's end they're waited for:
        a reading that failed plans the way to that title's other buttons."""
        goal = None
        try:
            while True:
                done = set(c.reading) | {s for s in routes if tries.get(s, 0) >= 2}
                found = named(hoping=True)
                # (and those read on TITLE_TRIES pages to no name: looked for no further, as on a Blu-ray)
                found |= _given_up(pages, c.reading, known, found, c.read)
                plan = [] if everything else _page_plan(pages, routes, done, found, known, c.learned,
                                                        {} if every_title else copies, page_rank)
                if not plan and not everything and not all(c.read(x) for x in c.reading):
                    for x in list(c.reading):
                        c.first_words(x)
                    continue
                plan = plan or beyond_the_plan(done)
                c.expect = len(c.reading) + len(plan)
                if not plan:
                    return
                # (the nearest of those it's on the way to already)
                todo = [s for s in plan if all(x in c.reading for x, _ in routes[s])]
                if not todo:                    # (the way to them given up on)
                    for s in plan:
                        tries[s] = 2
                    continue
                here = _routes(pages, st, c.learned, known)
                reach = [s for s in todo if s in here]
                try:
                    if not reach and st == start:
                        # not even from the start: no way there after all (a button the program said
                        # leads to them leads elsewhere, as pressing it showed): left unread
                        for s in todo:
                            tries[s] = 2
                        log("  no way to %d page%s after all (a button leads elsewhere than the disc's "
                            "program said): passed by" % (len(todo), "" if len(todo) == 1 else "s"))
                        continue
                    if not reach:               # a dead end: start over
                        st, sel = c.restart(start)
                        continue
                    goal = min(reach, key=lambda s: len(here[s]))
                    tries[goal] = tries.get(goal, 0) + 1
                    st, sel = c.follow(st, sel, here[goal])
                except Lost as e:
                    log("  on the way to %s: %s, starting over" % (goal, e))
                    st, sel = c.restart(start, where=goal)
        finally:
            c.close()

    session = (start, sel)                      # the first round tours in the session that found the start
    played = None           # the title Play played, once a Play button has been pressed: the others reuse it
    stalled = 0             # presses in a row VLC got no picture for: the drive has stopped answering
    stop = False
    scanned = _scan_titles(drive, log) if not everything else []
    sought, copies = _titles_sought(scanned, length) if not everything else set(), _copies_of(scanned)
    while not stop:
        routes = _routes(pages, start, c.learned, known)
        known_outright()
        try:
            if session is None:
                c.open()
                session = c.at_start(start)
            tour(*session)
        except Lost as e:                       # (a stalled drive: keep what's been found)
            c.close()
            log("  the menus can't be read any more (%s): stopping with what's been found" % e)
            break
        session = None
        routes = _routes(pages, start, c.learned, known)
        log("read %d of %d pages (%.1f minutes)" % (len(c.reading), len(routes), (time.time() - t0) / 60))
        sp = c.spent
        log("  the time: highlight checks %.0f s (%d round trips%s; of it darkening %.0f s), pages' frames %.0f s, "
            "waiting for words %.0f s; the rest, moving page to page and VLC opening %.0f s" % (
                sp.get("highlight checks", 0), sp.get("round trips", 0),
                ", darkened" if c.dark_ok else ", as it is" if c.dark_ok is False else "",
                sp.get("darkening", 0), sp.get("page frames", 0),
                sp.get("waiting for words", 0), time.time() - t0 - sp.get("highlight checks", 0)
                - sp.get("page frames", 0) - sp.get("waiting for words", 0)))
        if sum(c.placed[:2]):
            log("  the disc's program placed the highlight right %d of %d times%s" % (
                c.placed[0], sum(c.placed[:2]), " (and %d moved by our keys)" % c.placed[2] if c.placed[2] else ""))
        # the presses: only what the disc doesn't say, on pages read
        known_outright()
        # the feature, when scene buttons say it: then "Play" needn't be pressed (it may well run
        # trailers first)
        chapters = {}
        for k in known.values():
            if "plays" in k and not k.get("assumed"):
                chapters.setdefault(k["plays"]["title"], set()).add(k["plays"]["chapter"])
        feature = max(chapters, key=lambda t: len(chapters[t]), default=None)
        feature = feature if feature and len(chapters[feature]) >= 2 else None
        tasks = []
        for s in routes:
            for b in _state_buttons(pages, s):
                text = c.words(s).get(b["n"], "")
                if b["auto"] or _goes(pages, s, b, c.learned, known) or (s, b["n"]) in known or (s, b["n"]) in pressed \
                        or (s, b["n"]) in c.learned:
                    continue
                if feature and not everything and _play_text(text):
                    known[(s, b["n"])] = {"plays": {"title": feature, "chapter": 1}, "assumed": True}
                elif everything or s in c.reading:
                    tasks.append((s, b["n"]))
        # (the likeliest first: as soon as every title has its way, the rest needn't be pressed)
        tasks.sort(key=lambda t: _skip_text(c.words(t[0]).get(t[1], "")))
        if not tasks or not (everything or no_way()):
            break
        log("%d buttons to press (%d answered by the disc itself)" % (len(tasks), len(known)))
        for i, (s, n) in enumerate(tasks, 1):
            if not (everything or no_way()):
                log("  every title has a way to it now: the last %d left unpressed" % (len(tasks) - i + 1))
                break
            text = c.words(s).get(n, "")
            if played and not everything and _play_text(text):     # every page's Play Movie: the same film
                known[(s, n)] = {"plays": {"title": played, "chapter": 1}, "assumed": "play"}
                log("  [%d/%d] %s button %d: Play again: title %d (not pressed)" % (i, len(tasks), s, n, played))
                progress("press", i, len(tasks))
                continue
            for attempt in (1, 2):
                try:
                    c.open()
                    st, sel = c.follow(*c.at_start(start), routes[s])
                    c.select(st, sel, n)
                    chain = c.watch(length)
                    pressed[(s, n)] = {"chain": chain}
                    if all(x["title"] <= 0 for x in chain):
                        # still in the menus: on which page? (one the disc picks as it runs)
                        try:
                            land, _ = c.find(states, timeout=20)
                            if land != s:
                                c.learned[(s, n)] = land
                        except Lost:
                            pass
                    break
                except Lost as e:
                    log("  %s button %s: %s%s" % (s, n, e, ", trying again" if attempt == 1 else ""))
                    if attempt == 2:
                        pressed[(s, n)] = {"error": str(e)}
                finally:
                    c.close()
            r = pressed[(s, n)]
            land = c.learned.get((s, n))
            progress("press", i, len(tasks))
            log("  [%d/%d] %s button %d: %s" % (i, len(tasks), s, n, r.get("error") or (
                "goes to page %s (program %s)" % land[1:] if land else " -> ".join(
                    "menus" if e["title"] <= 0 else "title %d ch %s" % (e["title"], e["chapter"]) for e in r["chain"]))))
            plays = [x for x in r.get("chain", []) if x["title"] > 0]
            if _play_text(text) and plays:
                played = plays[-1]["title"]
            stalled = stalled + 1 if "no picture" in (r.get("error") or "") else 0
            if stalled >= 2:
                log("  VLC gets no picture from the drive any more: stopping with what's been found")
                stop = True
                break
    result, also = [], {}
    for s in routes:
        page = {"domain": s[0], "page": s[1], "program": s[2],
                "image": "page_%s_%d_%d.png" % s if s in c.reading else None,
                "passed_by": None if s in c.reading else passed_by(s),
                "route": [{"page": x[1], "program": x[2], "button": n} for x, n in routes[s]], "buttons": []}
        for b in _state_buttons(pages, s):
            e = {"n": b["n"], "rect": b["rect"], "cmd": b["cmd"], "text": c.words(s).get(b["n"], "")}
            if where_read(e["text"]):
                e["words"] = where_read(e["text"])
            if getattr(e["text"], "others", None):      # (its other readings: also_fix's, as a Blu-ray's)
                also.setdefault(str(e["text"]), list(e["text"].others))
            e["text"] = str(e["text"])
            nxt = _goes(pages, s, b, c.learned, known)
            if b["auto"]:
                e["goes_to"] = "auto"
            elif (s, b["n"]) in c.learned:
                e["goes_to"] = {"page": nxt[1], "program": nxt[2]}
                e["goes_to"]["pressed" if (s, b["n"]) in pressed else "engine"] = True
            elif nxt:
                e["goes_to"] = {"page": nxt[1], "program": nxt[2]}
            elif (s, b["n"]) in known:
                k = known[(s, b["n"])]
                e["menus"] = k.get("menus", False)
                if "plays" in k:
                    e["plays"] = dict(k["plays"], seconds=length.get(k["plays"]["title"]),
                                      **({"assumed": k["assumed"]} if k.get("assumed") else
                                         {"engine": True} if k.get("engine") else {"known": True}))
                    if k.get("others"):
                        e["plays"]["others"] = [dict(o, seconds=length.get(o["title"])) for o in k["others"]]
            elif (s, b["n"]) in pressed:
                r = pressed[(s, b["n"])]
                e.update(r)
                plays = [x for x in r.get("chain", []) if x["title"] > 0]
                if plays:
                    last = plays[-1]
                    e["plays"] = {"title": last["title"], "chapter": last["chapter"], "seconds": length.get(last["title"])}
                    e["before"] = [x["title"] for x in plays[:-1]]
                elif "chain" in r:
                    e["menus"] = True
            else:
                e["passed_by"] = True
            page["buttons"].append(e)
        page["heading"], page["heading_line"], page["mixed"] = c.headings(s, page["buttons"])
        result.append(page)
    c.pool.shutdown(wait=False)
    c.more.shutdown(wait=False)
    if _rapid:
        read_again(result, out_dir, log)
    for b, seconds in menu_only(result, structure):
        log("not rippable: \"%s\" (%s) plays from the disc's menu, it isn't a title" % (b["text"], _clock(seconds)))
    out = {"drive": drive, "titles": titles, "start": list(start), "minutes": round((time.time() - t0) / 60, 1),
           "pages": result, "chapters": {t["title"]: t["chapters"] for t in structure.get("titles", [])},
           "opening": opening, "also": also, "scan_play_alls": sorted(_play_alls_of(scanned))}
    if _rapid:                                  # (read by RapidOCR: its names judged as such)
        out["reader"] = "rapidocr"
        out["sure"] = readings_sure()
    out["found"] = menu_titles(out)
    out["tv"] = tv_titles(out)
    if tv:
        # what plays on after what (the disc's program) and what holds what (its cells): the order
        # of the episodes, by a Play All
        runs = [[x["plays"]] + list(x.get("run") or x.get("after") or [])
                for answers in engine.values() for x in answers if x.get("plays")]
        try:
            from . import core
            fmt, base = core.detect_format(drive)
            out["tv"]["play_order"] = core.play_all_order(core.parse_dvd(base)["titles"], runs)
        except (OSError, ValueError, IndexError, struct.error) as e:
            log("  couldn't read the disc's titles for its Play All (%s)" % e)
    if _rapid:
        out["found"]["reader"] = "rapidocr"
    return finish_crawl(out, out_dir)


def finish_crawl(out, out_dir):
    """A crawl's last steps, a DVD's or a Blu-ray's: each title's button to show, and the whole of it kept
    (crawl.json, to go over again with no disc; crawl.txt, to read)."""
    out["found"]["pictures"] = title_pictures(out, out_dir)
    with open(os.path.join(out_dir, "crawl.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    with open(os.path.join(out_dir, "crawl.txt"), "w", encoding="utf-8") as fh:
        fh.write(crawl_text(out) + "\n")
    return out


# --------------------------------------------------------------------------
# 5. What the menus say about the titles
# --------------------------------------------------------------------------

def title_id(n):
    """A title as the menus' answer has it, as a scan's id: a DVD's number ("Title 5"), a Blu-ray's id as it is."""
    return n if isinstance(n, str) and not n.isdigit() else "Title %d" % int(n)


SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
LISTED_RE = re.compile(r"[\s(\[]*\(?(\d{1,2}):(\d\d)\)?[\s)\]]*$")      # "(10:35)" after an extra's name


ICON_DIGIT_RE = re.compile(r"^\W*(\d)\s+(?=\D)")


def _icon_digits(texts):
    """The digits OCR reads a menu's bullet icons as: a lone digit before a name, on three buttons
    or more, the same digits again and again rather than counting up (The Iron Giant's: 3 and 6,
    "3 Hogarth Hughes", "6 The Deer"). A name of its own with a number stays ("3 Blind Mice")."""
    lead = [m.group(1) for m in map(ICON_DIGIT_RE.match, texts) if m]
    if len(lead) < 3:
        return set()
    counts = {d: lead.count(d) for d in set(lead)}
    return {d for d, n in counts.items() if n >= 2} if sum(n for n in counts.values() if n >= 2) * 2 > len(lead) else set()


def _unicon(text, icons):
    m = ICON_DIGIT_RE.match(text)
    return text[m.end():] if m and m.group(1) in icons else text


def nice_name(text):
    """A button's words as a name: menus shout in capitals (or whisper in lower case), names
    don't; and OCR's specks from the lettering's backdrop (an underscore, a lone bracket) go."""
    text = re.sub(r"[_|]+", " ", text)
    # a list's number before the name ("4. J. Jonah Jameson", "2) Leaving...", "1.A Lock Of...":
    # no space read after it); not a number that's part of it ("3 Blind Mice", "Stuart Little 2").
    # A "1" then, alone before a word, is the name's "I" in a flourished capital ("3.1 Am Not
    # Waiting For My Prince": I Am Not...)
    listed = re.match(r"^\s*\d{1,2}[.)]\s*(?=[^\d\s.,]|1\s+[A-Za-z])", text)
    if listed:
        text = text[listed.end():]
        text = re.sub(r"^1(?=\s+[A-Za-z])", "I", text)
    if text.count("(") != text.count(")"):
        text = text.replace("(", " ").replace(")", " ")
    text = re.sub(r"\s+", " ", text).strip(" ,.:;-'\"")
    text = re.sub(r"^\d{1,2}[.)]\s+", "", text)    # (once more, past a speck before it)
    # symbols at either end ("& Deleted Scenes", "...My Prince &", "Ghost Rider™."): not a "!" or
    # "?" ending it ("Dumped!"), nor a bracket closing one in it ("Close Encounters (of the
    # Third Kind)"); and a name all in brackets, out of them ("(Costume Design)")
    while True:
        was = text
        if text.startswith("(") and text.endswith(")") and text.count("(") == 1:
            text = text[1:-1]
        text = re.sub(r"^[^\w(]+|^\((?!.*\))", "", text)
        text = re.sub(r"[^\w!?)]+$", "", text)
        if text.endswith(")") and "(" not in text:
            text = text[:-1]
        text = text.strip()
        if text == was:
            break
    if not (text.isupper() or text.islower()):
        return text
    words = text.lower().split(" ")
    return " ".join(w if (i and w in SMALL_WORDS) else w[:1].upper() + w[1:] for i, w in enumerate(words))


# what a page's heading says its titles are, in TheDiscDB's types (Spider-Man: "TRAILERS",
# "MUSIC VIDEOS", "TV SPOTS"); TV spots are trailers to TheDiscDB
# ("Deleted Extended Scene", "Deleted and Extended Scenes": Psych's; "Extended Scenes" alone too)
# (and scenes or footage cut, in other words: Order of the Phoenix's "Additional Footage", "Additional Scenes")
DELETED_WORDS = r"\bdeleted(?:\s+\w+){0,2}?\s*scenes?\b|\bextended\s+scenes?\b|" \
                r"\b(?:additional|unused|extended|deleted)\s+(?:scenes?|footage)\b"
HEADING_KINDS = (("Music", r"\bmusic\s*videos?\b"), ("DeletedScene", DELETED_WORDS),
                 ("Featurette", r"\bfeaturettes?\b"), ("Interview", r"\binterviews?\b"),
                 ("Trailer", r"\btrailers?\b|\bspots\b"), ("Short", r"\bshort\s*films?\b|\bshorts\b"))
HUB_RE = re.compile(r"\b(bonus|special)\s*(features?|materials?)\b|\bextras\b", re.I)
MAIN_MENU_RE = re.compile(r"\bmain\s*menu\b", re.I)


def heading_kind(heading):
    """The type a page's heading gives its titles, or None: the kind it opens with (its first
    few words: a page's title, "SCORP TRAILERS"), and only one. Not a hub's: a page of bonus
    features lists its kinds, its labels printed beside its buttons (The Sorcerer's
    Apprentice's "BONUS FEATURES ... Deleted Scene ...")."""
    if not heading or HUB_RE.search(heading):
        return None
    opening = " ".join(heading.split()[:5])
    kinds = {k for k, rx in HEADING_KINDS if re.search(rx, opening, re.I)}
    return kinds.pop() if len(kinds) == 1 else None


def menu_titles(out):
    """The crawl, boiled down: {"feature": title, "extras": [{"title", "name", "seconds", "listed"}],
    "promos": [titles]}, in the disc's title numbers ("Title 10"). The feature is the title the
    scene buttons point into (or the longest one played); promos (ads) are what plays from a
    page reached through a "Sneak Peeks"/"Previews" button, or a button saying so (found only
    when the crawl pressed everything), or a "trailer" in a chapter per trailer (an ad reel);
    extras are the rest, named by their button, a film's own "theatrical trailer" among them.
    A running time on the menu ("10:35") is kept as "listed", to check the name belongs to the
    title."""
    pages = {(p["domain"], p["page"], p["program"]): p for p in out["pages"]}
    words = {(s, b["n"]): b["text"] for s, p in pages.items() for b in p["buttons"]}
    # (each title's chapters: a DVD's by its number, a Blu-ray's by its playlist, "00060.mpls")
    chapters = {(int(t) if str(t).isdigit() else t): n for t, n in (out.get("chapters") or {}).items()}
    plays = []
    for s, p in pages.items():
        on_way = [(s[0], r["page"], r["program"]) for r in p["route"]]
        # the buttons pressed on the way, and this page's heading (not those of the pages passed
        # through: a main menu's words, its "Sneak Peeks" label among them, say nothing of its
        # Bonus Features page; The Sorcerer's Apprentice's labels sit beside its buttons)
        way = [words.get((x, r["button"]), "") for x, r in zip(on_way, p["route"])]
        via = " ".join(way + [p.get("heading") or ""])
        # what the page holds: its heading says, else the nearest button on the way that does (The
        # Iron Giant's deleted scenes: no heading to read, a "Deleted Scenes" button, then "Next");
        # up to a button back to a hub, which says nothing of the kinds it holds (WALL-E's Bonus
        # Features page, reached back from its Deleted Scenes page: not deleted scenes)
        kind = page_kind(p)
        for w in reversed([] if p.get("mixed") else way):
            if kind or HUB_RE.search(w) or MAIN_MENU_RE.search(w):
                break
            kind = heading_kind(w)
        singles = {b["plays"]["title"] for b in p["buttons"] if b.get("plays")}
        ads = _ads_page(way, p.get("heading"), p["buttons"], p.get("heading_line"))
        for b in p["buttons"]:
            if b.get("plays"):
                reel = bool(re.search(r"\btrailer", b["text"], re.I)) and \
                    chapters.get(b["plays"]["title"], 0) >= AD_REEL_CHAPTERS
                run = b["plays"].get("after") or []
                # a run of titles from a page of buttons each playing its own: the page's Play All,
                # whatever its lettering reads as (Enchanted's sneak peeks: "PLAY aul", "ore PLAY";
                # its reels repeat the sneak peeks, copies the scan sets aside). Not a run from a
                # page of other things (The Sorcerer's Apprentice's Sneak Peeks, by Play Movie)
                whole = bool(run) and len(singles - {b["plays"]["title"]} - set(run)) >= 2
                promo = ads or _ad_text(b["text"]) or reel
                place = [r["button"] for r in p["route"]] + [b["n"]]
                plays.append(dict(b["plays"], text=b["text"], promo=promo, kind=kind, via=via, play_all=whole, page=s,
                                  place=place))
                # an ad button's run of titles: ads, all of them (The Sorcerer's Apprentice's Sneak
                # Peeks: 66, then 70, 74, 78, 84, 88, 92, 94). Not another button's: FastPlay's sneak
                # peeks and film are what their own buttons say; a Play All's, its own titles
                if promo or whole:
                    for k, t in enumerate(run, 1):      # (each in its turn, after the button's own)
                        plays.append({"title": t, "chapter": 1, "seconds": None, "text": b["text"], "promo": promo,
                                      "kind": kind, "via": via, "after": True, "play_all": whole, "place": place + [k]})
                for o in b["plays"].get("others") or []:
                    plays.append(dict(o, text=b["text"], promo=promo, kind=kind, via=via, other=True))
    if not plays:
        return {"feature": None, "extras": [], "promos": []}
    icons = _icon_digits([x["text"] for x in plays if not x.get("after") and not x.get("other")])
    by = {}
    for x in plays:
        by.setdefault(x["title"], []).append(x)
    feature = max(by, key=lambda t: (len({x["chapter"] for x in by[t]}), by[t][0]["seconds"] or 0))
    # the film's copies (as long as it, to the second): its own the one whose button is nearest the root,
    # fewest presses away (a page's depth: its menu's, where menus lead to menus), a scene page's
    # chapter 1 further in than a Play Movie; a tie as the scene buttons have it
    reach = lambda t: min((len(x["place"]) + (pages.get(x["page"]) or {}).get("depth", 0) for x in by[t]
                           if x.get("place") and not x.get("after") and not x.get("other")), default=99)
    # (a film the menus show over, a pop-up's: no button plays it, the menu shows while it plays, Chamber of
    # Secrets' 00023, 2:41, under menu 00157: the feature, not its longest extra a button plays, Building a Scene)
    under = [u for u in out.get("under_menu") or [] if (u.get("seconds") or 0) >= FILM_LENGTH]
    if under:
        u = max(under, key=lambda u: u["seconds"])
        if u["seconds"] > (by[feature][0]["seconds"] or 0):
            by.setdefault(u["title"], []).append({"title": u["title"], "chapter": 1, "seconds": u["seconds"], "text": "",
                                                  "promo": False, "kind": None, "via": "", "under_menu": True, "place": []})
            feature = u["title"]
    long = by[feature][0]["seconds"] or 0
    twins = [t for t in by if t != feature and long >= FILM_LENGTH and abs((by[t][0]["seconds"] or 0) - long) <= 1]
    if twins and reach(min(twins, key=reach)) < reach(feature):
        feature = min(twins, key=reach)
    # several films, each its own menu with a Play (October Collections: The Ghost and Mr. Chicken
    # and The Reluctant Astronaut): each is a feature, named by its menu's heading. A Play whose
    # lettering wasn't read counts (Abbott and Costello's Buck Privates: its page's first button)
    # ("MNC PLAY": a Play misread, its word still there.) Not a scene selection page: several of
    # its buttons play the film (Pardon My Sarong's, chapter 1's button unread)
    # (a Play naming a version starts one film's copy: Rush Hour 3's PLAY WIDESCREEN / FULLSCREEN VERSION)
    played = lambda text: not VERSION_WORDS_RE.search(text or "") and (
        _play_text(text) or not re.search(r"[A-Za-z]{2}", text or "") or
        (len((text or "").split()) <= 3 and "play" in re.findall(r"[a-z]+", (text or "").lower())))
    per_page = lambda xs, x: sum(1 for y in xs if y.get("page") == x.get("page"))
    scenes = {(x.get("page"), x["title"]) for t, xs in by.items() for x in xs if per_page(xs, x) >= 3}
    # (a Play read as something else, the film's title lettering run into it: Abbott and Costello
    # Meet Frankenstein's, "FKANKENSTEIN". A film's own menu all the same: the one button on its page
    # that plays it from the start, beside Scenes, Bonus and Languages; a commentary page has two,
    # ON and OFF, a scene page more. A Play read as one comes first, then the page nearest the first menu)
    own = {}
    for t, xs in by.items():
        best = None
        for x in xs:
            # (a page's Play All plays its page's titles, never a film's own Play: Order of the Phoenix's
            # Focus Points Play All, an hour long)
            if not (x.get("chapter") in (1, None) and (x["seconds"] or 0) >= FILM_SECONDS and x.get("page")
                    and x["page"] not in own.values() and (x["page"], t) not in scenes
                    and not VERSION_WORDS_RE.search(x["text"] or "") and not PLAY_ALL_RE.search(x["text"] or "")):
                continue
            sure = played(x["text"])
            if not sure and per_page(xs, x) != 1:
                continue
            rank = (sure, -len(pages[x["page"]].get("route") or []))
            if best is None or rank > best[0]:
                best = (rank, x["page"])
        if best:
            own[t] = best[1]
    # (two the same length to the second: copies of one film, unless their menus' headings name
    # different films: Abbott and Costello Go to Mars and Meet Dr. Jekyll and Mr. Hyde, a second apart)
    # (a film of a disc of several is a film's length beside the others: one less than half the disc's
    # feature is an extra with a button of its own, Order of the Phoenix's 44-minute documentary beside
    # its 138-minute film; double features' films come close: Abbott and Costello's)
    longest = max((x["seconds"] or 0 for x in by.get(feature, [])), default=0)
    for t in [t for t in own if t != feature]:
        if next((x["seconds"] for x in by[t] if x.get("seconds")), 0) < longest * OWN_FILM_SHARE:
            del own[t]
    secs = {t: next((x["seconds"] for x in by[t] if x.get("seconds")), 0) for t in own}
    heading = lambda t: set(re.findall(r"[a-z]{2,}", (pages[own[t]].get("heading") or "").lower()))
    apart = lambda a, b: bool(a and b) and len(a & b) < 0.5 * len(a | b)
    for t in sorted(own):
        if any(o < t and o in own and abs(secs[o] - secs[t]) <= 2 and not apart(heading(o), heading(t))
               for o in list(own)):
            del own[t]
    features = [{"title": t, "name": _heading_name(pages[own[t]].get("heading")), "page": list(own[t])}
                for t in sorted(own)] \
        if len(own) >= 2 else []
    film_pages = {p: t for t, p in own.items()}
    extras, promos, promo_names, play_alls = [], [], {}, []
    for t, xs in sorted(by.items()):
        if t == feature or t in own and features:
            continue
        if all(x.get("play_all") for x in xs):  # only ever in a page's Play All: the scan says what it is
            play_alls.append(t)
            continue
        if any(x["promo"] for x in xs):
            promos.append(t)
            promo_names[t] = promo_name(xs)
            continue
        text = max((x["text"] for x in xs), key=_wordy)
        if PLAY_ALL_RE.search(text):            # a page's "Play All": its titles, one after another
            play_alls.append(t)
            continue
        m = LISTED_RE.search(text)
        listed = int(m.group(1)) * 60 + int(m.group(2)) if m else None
        # (read by RapidOCR: letters are enough here, plain_names judges it with the dictionary;
        # _wordy, made for Tesseract's junk, takes "xXx", Spider-Man's trailer, for junk)
        named = re.search(r"[A-Za-z]{2}", text) if out.get("reader") == "rapidocr" else _wordy(text) > 0
        # (a menu's words name nothing, "Scene 3" a chapter of the film among them; but a button playing a title
        # of its own from its start isn't a chapter: there "Scene 17" is its name, Chamber of Secrets' deleted scenes)
        own_scene = bool(SCENE_NAME_RE.match(text or "")) and all(x.get("chapter") in (1, None) for x in xs)
        name = nice_name(_unicon(LISTED_RE.sub("", text), icons)) if named and (own_scene or not _nav_only(text)) else ""
        # (or its own button says: Jurassic World's "Deleted Scenes", on a page saying nothing)
        kind = next((x["kind"] for x in xs if x.get("kind")), None) or heading_kind(name)
        other = all(x.get("other") for x in xs)     # (only by another route: a commentary version?)
        if other and name:
            name += " (another version)"
        page = next((list(x["page"]) for x in xs if x.get("page")), None)
        sure = (out.get("sure") or {}).get(text)            # (how sure RapidOCR was of it)
        # (its other readings, less sure: for when the surest isn't words, plain_names)
        also = [nice_name(_unicon(LISTED_RE.sub("", a), icons)) for a in (out.get("also") or {}).get(text) or []] \
            if name else []
        film = _film_of(xs, pages, film_pages) if features else None
        extras.append(dict({"title": t, "name": name, "seconds": xs[0]["seconds"], "listed": listed, "kind": kind,
                            "page": page}, **({"sure": round(sure, 3)} if sure is not None else {}),
                           **({"also": [a for a in also if a and a != name]} if also else {}),
                           **({"other": True} if other else {}), **({"film": film} if film else {})))
    # the feature's own button, when it says more than Play ("Signature Edition": another cut of
    # the film may be on the disc, and is named the same way)
    # (from its start: a scene button's words name the scene)
    ftext = max([x["text"] for x in by[feature] if x.get("chapter") in (1, None)] or [""], key=_wordy)
    feature_name = nice_name(ftext) if _wordy(ftext) > 0 and not _play_text(ftext) and \
        not PLAY_ALL_RE.search(ftext) else ""
    # what a player shows before the menu, no button playing it: its previews
    offered = {feature} | {e["title"] for e in extras} | set(promos) | set(play_alls)
    opening = [t for t in out.get("opening") or [] if t not in offered]
    offered |= {f["title"] for f in features}
    opening = [t for t in opening if t not in offered]
    # what each Play All plays, as its page has it: the titles its page's other buttons play from
    # their start (Wallace & Gromit's Shorts page: 2, 3 and 7, its Play All's title 1 holding 2 and
    # 3, the disc going on to 7's twin after; its Cracking Contraptions' ten)
    mine = lambda y: y.get("page") and y.get("chapter") in (1, None) and not y.get("after") and not y.get("other")
    parts = {}
    for t, xs in by.items():
        page = next((x["page"] for x in xs if mine(x) and PLAY_ALL_RE.search(x["text"] or "")), None)
        sib = sorted({y["title"] for t2, ys in by.items() if t2 != t for y in ys if mine(y) and y["page"] == page})
        if page and len(sib) >= 2:
            parts[t] = sib
    # where each title is in the menus, for reading them in that order: by its button that names it,
    # else its first (not a commentary version's, nor a Play All's)
    def named_place(xs):
        own = [x for x in xs if x.get("place") and not x.get("other")] or [x for x in xs if x.get("place")]
        whole = [x for x in own if not x.get("play_all") and not PLAY_ALL_RE.search(x["text"] or "")] or own
        best = max(whole, key=lambda x: _wordy(x["text"]), default=None)
        return best and best["place"]
    places = {t: named_place(xs) for t, xs in by.items()}
    # (a Play All the scan knows, its button not read: a Play All still, not an extra with no name;
    # out["scan_play_alls"]: the crawl's, _play_alls_of)
    known = set(out.get("scan_play_alls") or [])
    unread = [e["title"] for e in extras if e["title"] in known and not e.get("name")]
    if unread:
        extras = [e for e in extras if e["title"] not in unread]
        play_alls += [t for t in unread if t not in play_alls]
    return dict({"feature": feature, "feature_name": feature_name, "extras": extras, "promos": promos,
                 "promo_names": promo_names, "play_alls": play_alls, "opening": opening,
                 "places": {t: p for t, p in places.items() if p}},
                **({"features": features} if features else {}), **({"play_all_parts": parts} if parts else {}))


def _menu_parent(pages, s):
    """The page a menu page is reached from, going in from the first menu (the last step of its
    route): in its own menus if there's such a page there (another than itself), else the disc's
    own (vmgm). None for the first menu."""
    route = pages[s].get("route") or []
    if not route:
        return None
    r = route[-1]
    here = [c for c in ((s[0], r["page"], r["program"]), ("vmgm", r["page"], r["program"])) if c in pages and c != s]

    def leads_here(c):                    # (its button pressed on the way goes to this page)
        b = next((b for b in pages[c]["buttons"] if b["n"] == r["button"]), None)
        g = (b or {}).get("goes_to") or {}
        return g.get("page") == s[1] and g.get("program", 1) in (s[2], None)
    # (October Collections: both films in one menu set, the second's page reached from the disc's
    # own first menu, not from the first film's page of the same number)
    return next((c for c in here if leads_here(c)), here[0] if here else None)


def _menu_path(pages, s):
    """A page and the pages above it, up to the first menu: [s, its parent, ..., the first]."""
    path = [s]
    while len(path) < 40:
        p = _menu_parent(pages, path[-1])
        if p is None or p in path:
            break
        path.append(p)
    return path


def _film_of(xs, pages, film_pages):
    """Whose film an extra is, of a disc of several films, by where it sits in the menus: the
    film whose own menu page is nearest its page, going in from the first menu (the pages above
    each, not "Menu" or "Back" buttons, which reach everything): the film's own page, or a page
    reached through it (The Shakiest Gun in the West's TRAILER, on its page). Nearer to two films
    alike (both Plays on one page, Rush Hour and Rush Hour 2's; a Bonus page off the first menu):
    the disc's, None. A film whose Play is on the first menu itself doesn't claim the pages under
    that, the whole disc's. The film's title number, or None."""
    paths = {f: _menu_path(pages, f) for f in film_pages if f in pages}
    # (a film's page the other films' pages hang under: the disc's hub, its Play on the first
    # menu; what's nearest it is the disc's, not that film's)
    hubs = {f for f in paths if any(f in p[1:] for g, p in paths.items() if g != f)}
    best = None                                # (distance, film), or (distance, None): the disc's
    for x in xs:
        s = tuple(x.get("page") or ())
        if s not in pages:
            continue
        up = _menu_path(pages, s)
        for f, fup in paths.items():
            common = next((a for a in up if a in fup), None)
            if common is None:
                continue
            d = up.index(common) + fup.index(common)
            claim = None if f in hubs else film_pages[f]
            if best is None or d < best[0]:
                best = (d, claim)
            elif d == best[0] and best[1] != claim:
                best = (d, None)
    return best[1] if best else None


FILM_SECONDS = 40 * 60      # a title this long, played by a menu's own Play, can be one of a disc's films
SCENE_NAME_RE = re.compile(r"^\W*scenes?\s*\d{1,3}[a-z]?\W*$", re.I)       # "Scene 17", "SCENE 3"
OWN_FILM_SHARE = 0.5        # ... and at least this share of the disc's feature's length
NAV_WORDS = {"play", "menu", "main", "movie", "film", "feature", "scene", "scenes", "selection", "selections",
             "chapter", "chapters", "language", "languages", "audio", "subtitles", "setup", "back",
             "next", "resume", "start", "the"}


def _nav_only(text):
    """A button saying only where it goes or what it does ("PLAY", "MENU", "Scene Selection"):
    no name for what it plays."""
    # ("Set Up" as one: alone, "Up" is a film, WALL-E's trailer for it)
    words = re.findall(r"[a-z]+", re.sub(r"\bset\s*up\b", "setup", (text or "").lower()))
    return bool(words) and all(w in NAV_WORDS for w in words)


def _heading_name(heading):
    """A film's name off its menu's heading, its menu words dropped ("" when that's all it says)."""
    words = [w for w in (heading or "").split() if re.sub(r"[^a-z]", "", w.lower()) not in NAV_WORDS - {"the"}]
    name = " ".join(words)
    return "" if not re.search(r"[A-Za-z]{3}", name) or _instructions(name) else name


# what an ad is called, in words that say it's an ad (core.AD_RE knows them: a TheDiscDB name
# like "Previews - Hitch" keeps it an ad for everyone's scans, not the film's own trailer)
AD_NAMES = (("Sneak Peeks", r"\bsneak|\bpeeks?\b"), ("Coming Soon", r"\bcoming\s+(soon|attractions?)\b"),
            ("Also Available", r"\bavailable\b"), ("Commercials", r"\bcommercials?\b"),
            ("Advertisements", r"\badvert"), ("Previews", r"\bpreviews?\b|\btrailers?\b"))


def promo_name(xs):
    """A name for an ad from what the menus said (xs: its plays, each with the words that made it
    an ad, "via", and its button's): the ad words first ("Previews"), then its button's own words
    if they read as a name ("Previews - Hitch"; a poster button says nothing: "Previews")."""
    said = " ".join([x.get("via") or "" for x in xs] + [x["text"] for x in xs])
    lead = next((name for name, rx in AD_NAMES if re.search(rx, said, re.I)), "Previews")
    # (less a trademark sign read into its last word: Spider-Man 2's "Spider-ManT", WALL-E's "ON BLU-RaY DISCTM")
    text = max((TRADEMARK_RE.sub("", x["text"] or "").strip() for x in xs), key=_wordy)
    # its own words, unless they're only the ad words ("PREVIEWS"): an ad can say what it is
    # ("Spider-Man: Friend or Foe Video Game Preview", Spider-Man 3)
    rest = re.sub(r"\b(previews?|sneak|peeks?|coming|soon|trailers?|ads?|advertisements?)\b", " ", text, flags=re.I)
    own = nice_name(text) if _wordy(text) > 0 and _wordy(rest) > 0 else ""
    return "%s - %s" % (lead, own) if own else lead


def readable(name, is_word, sure=False):
    """Whether OCR read a name rather than the lettering's backdrop: most of its words (two in
    three) are words ("Beye olticak Peeks" isn't), one of them three letters or more ("2 Oh"
    is a scrap). Numbers count as read. sure (RapidOCR's reading: it leaves out the lines it isn't
    sure of): a word of three letters or more, and half its longer words, are enough (Muppets from
    Space's "Cardshark Serenade"; not a scrap of a name cut off, "22", " AMPION")."""
    words = re.findall(r"[^\W_]+", name)
    real = [w for w in words if not w.isdigit() and w.lower() not in SMALL_WORDS and is_word(w)]
    good = len(real) + sum(1 for w in words if w.isdigit() or w.lower() in SMALL_WORDS)
    # and its longer words half real: scraps ("Rei Mec ai ay") don't carry a name on their own
    # ("Rei Mec ai ay cummantie scien", Enchanted's OCR of an animated button)
    longer = [w for w in words if len(w) >= 4 and not w.isdigit()]
    return any(len(w) >= 3 for w in real) and (sure or 3 * good >= 2 * len(words)) and \
        2 * sum(1 for w in longer if w in real) >= len(longer)


def unmarked(name, is_word):
    """A name without the trademark signs OCR reads as a letter: "Spider-Manw" (™ read as w),
    "Ghost Riderm" (® as m). A word that isn't one with its last w or m, and is without it."""
    def fix(m):
        w = m.group(0)
        return w[:-1] if len(w) >= 4 and not is_word(w) and is_word(w[:-1]) else w
    return re.sub(r"[A-Za-z]+[wmWM]\b", fix, name or "")


def _letters_off(a, b, most):
    """The edits (a letter changed, added or dropped) from a to b, or most + 1 if more."""
    if abs(len(a) - len(b)) > most:
        return most + 1
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
        if min(row) > most:
            return most + 1
    return row[-1]


def _close(a, b, long=7):
    """A misreading of b: a letter off; two when both are long words (long letters or more:
    "ARIELS" isn't Daniels, "scien" isn't Scriven)."""
    most = 2 if min(len(a), len(b)) >= long else 1
    return _letters_off(a.lower(), b.lower(), most) <= most


def _like(word, model):
    """word in model's letter case: CAPITALS, Capitalised, or as the model has it."""
    return model.upper() if word.isupper() and len(word) > 1 else model


def credit_fix(text, people, is_word):
    """A reading with this film's people's names put right where OCR missed a letter or two
    ("Hagart Hughs": "Hogarth Hughes", The Iron Giant's): a run of words close to one name of
    the film's credits, word by word, one of them no word at all; else a word that's no word, a
    letter or two from exactly one word of their names. Only the film's own names, a few hundred:
    a near miss there isn't chance (a whole dictionary's is: "Storyboards" read as "Storyboard")."""
    names = []
    for n in people:
        for part in re.split(r"\s*/\s*", re.sub(r"\([^)]*\)", " ", n or "")):
            ws = re.findall(r"[A-Za-z][A-Za-z'-]*", part)
            if ws and ws not in names:
                names.append(ws)
    words = sorted({w for ws in names for w in ws if len(w) >= 3}, key=str.lower)
    known = {w.lower() for w in words}
    # (a word's "'s" left out of it: "Nathaniel's" is the name and stays possessive, not
    # Enchanted's "Nathaniel Revelation"; "O'Brien" keeps its apostrophe)
    toks = list(re.finditer(r"[A-Za-z][A-Za-z-]*(?:'(?![sS]\b)[A-Za-z-]+)*", text))
    unread = lambda w: w.lower() not in known and w.lower() not in EXTRAS_VOCAB and not (is_word and is_word(w))
    out, i, fixes = [], 0, []
    while i < len(toks):
        done = False
        for ws in sorted({tuple(n) for n in names if len(n) >= 2}, key=len, reverse=True):
            run = toks[i:i + len(ws)]
            # (a whole name, word for word, is surer than a word: two letters off from six)
            fits = lambda n: all(t.group(0).lower() == w.lower() or _close(t.group(0), w, 6) for t, w in zip(run, n))
            if len(run) == len(ws) and fits(ws) and any(unread(t.group(0)) for t in run):
                same = [n for n in names if len(n) == len(ws) and fits(n)]
                if len({tuple(x.lower() for x in n) for n in same}) == 1:
                    fixes.append((run[0].start(), run[-1].end(), " ".join(_like(t.group(0), w) for t, w in zip(run, ws))))
                    i += len(ws)
                    done = True
                    break
        if done:
            continue
        w = toks[i].group(0)
        # (a name's plural or possessive is read right: "The Centaurs", "The Weasleys' Fireworks")
        if len(w) >= 4 and unread(w) and not (w[-1] in "sS" and w[:-1].lower() in known):
            near = {x.lower(): x for x in words if _close(w, x)}
            # (a plural misread: its name put right, its "s" kept: "Thestrels", "Thestrals")
            plural = w[-1] in "sS" and len(w) >= 5 and len({x.lower() for x in words if _close(w[:-1], x)}) == 1
            if plural:
                near = {x.lower(): x + w[-1] for x in words if _close(w[:-1], x)}
            # (a short word, only beside another of the same name: "Jonan" by "Jameson" is Jonah,
            # of J. Jonah Jameson; Spider-Man's "& Marve", by no one's name, isn't Marie)
            by = {toks[j].group(0).lower() for j in (i - 1, i + 1) if 0 <= j < len(toks)}
            if len(near) == 1 and (len(w) >= 6 or any(
                    next(iter(near)) in {x.lower() for x in n} and by & {x.lower() for x in n} for n in names)):
                fixes.append((toks[i].start(), toks[i].end(), _like(w, next(iter(near.values())))))
        i += 1
    for a, b, new in reversed(fixes):
        text = text[:a] + new + text[b:]
    return text


DISC_WORD_SURE = 0.95       # a word of a reading this sure, a word or a name, is the disc's own (disc_words)


def disc_words(raw, is_word=None, people=()):
    """The words this disc's menus read well, to put others right by (seen_fix): of each reading
    RapidOCR was DISC_WORD_SURE of (raw["sure"], and raw["page_lines"], pages read whole), its words that are words (is_word), the extras'
    own or the film's people's (people: names), as read: {lower: word}. Only those: a sure misreading
    ("Meville's Cactus", 96%) isn't one."""
    names = {w.lower() for n in people or () for w in re.findall(r"[A-Za-z]{3,}", n or "")}
    out = {}
    # (its buttons' readings, and every line of the pages read whole: their headings, descriptions)
    read = dict((raw or {}).get("page_lines") or {}, **((raw or {}).get("sure") or {}))
    for text, sure in sorted(read.items(), key=lambda x: -x[1]):
        if sure < DISC_WORD_SURE:
            continue
        for w in re.findall(r"[A-Za-z]{4,}", text):
            lw = w.lower()
            if lw not in out and (lw in names or lw in EXTRAS_VOCAB or (is_word and is_word(w))) and not _odd_case(w):
                out[lw] = w
    return out


def _odd_case(w):
    """A word's letter case as small capitals make it, read: a capital after a small letter twice
    ("UmBriDge", "DemEntORS"), or two together after one ("GrimMAuld", "AzkABan"); not "McDonald"."""
    return len(re.findall(r"[a-z][A-Z]", w)) >= 2 or bool(re.search(r"[a-z][A-Z]{2,}[a-z]", w))


def _cased(w, form):
    """form in the letter case of the reading w: CAPITALS, Capitalised, or small; a name's own case
    ("McDonald") kept, where w isn't in capitals."""
    if w.isupper() and len(w) > 1:
        return form.upper()
    if form.isupper() or form.islower():
        return form.capitalize() if w[:1].isupper() else form.lower()
    return form


def _swapped(a, b, most):
    """Whether a is b with at most this many letters changed (none added or dropped: "BURNE" isn't
    "Burn", it's BURN·E)."""
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) <= most


def seen_fix(text, seen, people, is_word):
    """A reading put right by the words read well elsewhere on the disc (seen: disc_words): a word
    that's no word, a letter changed from exactly one of them (not two: "Decrees" isn't "Secrets"), is it ("Iagic" by
    "Magic", read well in "The Ministry of Magic Atrium"; "Piay All"). And a word in small capitals'
    letter case as the film's people or the disc have it ("UmBridge": Umbridge), else as a word
    ("GrimMAuld": Grimmauld)."""
    forms = {w.lower(): w for n in people or () for w in re.findall(r"[A-Za-z]{3,}", re.sub(r"\([^)]*\)", " ", n or ""))}
    forms.update(seen or {})

    def fix(m):
        w = m.group(0)
        lw = w.lower()
        if lw in forms:                 # (a word it knows: in its letter case, if small capitals made it odd)
            return _cased(w[0] + w[1:].lower(), forms[lw]) if re.search(r"[a-z][A-Z]", w) and not w.isupper() else w
        if len(w) >= 4 and lw not in EXTRAS_VOCAB and not (is_word and is_word(w)):
            near = {x for x in (seen or {}) if _swapped(lw, x, 1)}
            if len(near) == 1:
                return _cased(w if not _odd_case(w) else w[0] + w[1:].lower(), seen[near.pop()])
        return w[0] + w[1:].lower() if _odd_case(w) else w
    return re.sub(r"[A-Za-z]+", fix, text or "")


def also_fix(text, others, is_word, seen=None):
    """A reading's words that aren't words put right by its button's other readings (others): where
    one has, in the same place, a word (a dictionary's, the extras', or one the disc reads well: seen)
    a letter or two from it ("The Mmirror Explosion", its button alone "The Mirror Explosion"; "Play
    Iovie", "Play Movie"), in the reading's letter case."""
    import difflib
    known = lambda w: w.lower() in EXTRAS_VOCAB or w.lower() in (seen or {}) or bool(is_word and is_word(w))
    spans = list(re.finditer(r"[A-Za-z]+", text or ""))
    fixes = {}
    for other in others or []:
        words = re.findall(r"[A-Za-z]+", other or "")
        sm = difflib.SequenceMatcher(None, [m.group(0).lower() for m in spans], [w.lower() for w in words])
        for op, a0, a1, b0, b1 in sm.get_opcodes():
            if op != "replace" or a1 - a0 != b1 - b0:
                continue
            for i, w in zip(range(a0, a1), words[b0:b1]):
                mine = spans[i].group(0)
                if i not in fixes and len(mine) >= 3 and not known(mine) and known(w) and \
                        _letters_off(mine.lower(), w.lower(), 2) <= 2:
                    fixes[i] = _cased(mine, w)
    for i in sorted(fixes, reverse=True):
        text = text[:spans[i].start()] + fixes[i] + text[spans[i].end():]
    return text


def fix_reading(text, people, is_word, others=None, seen=None):
    """One reading put right where it can be, a film's extra's or a show's bonus feature's alike: by the
    people in its credits (credit_fix), then its button's other readings (also_fix: others), then the
    words read well elsewhere on the disc (seen_fix; seen: disc_words)."""
    new = credit_fix(text, people, is_word) if text and people else text
    new = also_fix(new, others, is_word, seen) if new else new
    return seen_fix(new, seen, people, is_word) if new else new


def fix_tv_readings(tv, episodes, people, is_word, also=None, seen=None, sure=None):
    """tv_titles with its readings that aren't episodes (episodes: the indexes the show's episode list
    took, core.menu_episodes' picks: those are named by it) put right as a film's extras are
    (fix_reading), and one that isn't readable (readable: by is_word) given another reading of its
    button that is, if it has one (also: {reading: its other readings}; sure: {reading: how sure},
    RapidOCR's, a reading SURE_ENOUGH stands). What was read is kept ("read"), to show."""
    if not tv or not (people or is_word or seen):
        return tv
    also, sure = also or {}, sure or {}
    known = (lambda w: w.lower() in EXTRAS_VOCAB or is_word(w)) if is_word else None
    readings = []
    for i, r in enumerate(tv.get("readings") or []):
        text = r.get("text") or ""
        if i in episodes or not text:
            readings.append(r)
            continue
        new = fix_reading(text, people, is_word, also.get(text), seen)
        if known and sure.get(text, 0) < SURE_ENOUGH and not readable(new, known, True):
            new = next((a for a in also.get(text) or [] if readable(a, known, True)), new)
        readings.append(dict(r, text=new, read=text) if new != text else r)
    return dict(tv, readings=readings)


def correct_names(found, people, is_word, title_fix=None, seen=None):
    """found (menu_titles()) with its names put right where they can be: an extra's against the
    film's people (credit_fix), then the words read well elsewhere on the disc (seen_fix; seen:
    disc_words); an ad's against the films it may advertise (title_fix: a known title, closely,
    "Ghost Riderm." for Ghost Rider). What was read is kept ("read"), to show."""
    if not found:
        return found
    extras = []
    for e in found.get("extras") or []:
        new = fix_reading(e.get("name"), people, is_word, e.get("also"), seen)
        extras.append(dict(e, name=new, read=e["name"]) if new != e.get("name") else e)
    promo_names, promo_read = {}, dict(found.get("promo_read") or {})
    for t, n in (found.get("promo_names") or {}).items():
        lead, _, own = n.partition(" - ")
        fixed = title_fix(own) if own and title_fix else own
        promo_names[t] = "%s - %s" % (lead, fixed) if fixed else n
        if fixed != own:
            promo_read[t] = own
    return dict(found, extras=extras, promo_names=promo_names, promo_read=promo_read)


def trailer_ads(found, film, known_film):
    """found with a trailers page's ads for other films made promos: on a page of trailers
    (kind "Trailer"), when most are well-known films (known_film: core.known_film) other than
    this one, those are ads ("Previews - xXx": Spider-Man's Trailers page, xXx, Mr. Deeds, Men in
    Black II, Stuart Little 2); the film's own trailer stays an extra, and so does a page whose
    names are mostly its own spots, a film's name among them or not (Spider-Man's TV Spots:
    "Epic" is a film too). film: this disc's film's title."""
    import difflib
    if not found or not film:
        return found
    plain = lambda t: " ".join(re.findall(r"[a-z0-9]+", (t or "").lower()))
    own = lambda n: plain(film) in plain(n) or difflib.SequenceMatcher(None, plain(n), plain(film)).ratio() >= 0.8
    pages = {}
    for e in found.get("extras") or []:
        if e.get("kind") == "Trailer" and e.get("name") and e.get("page"):
            pages.setdefault(tuple(e["page"]), []).append(e)
    ads = {}
    for es in pages.values():
        # (most of those not the film's own well-known films; the film's own few: Muppets from Space's
        # trailer and teaser, beside The Dark Crystal, Labyrinth and The Storyteller)
        others = [e for e in es if not own(e["name"])]
        films = {e["title"]: known_film(e["name"]) for e in others}
        films = {t: f for t, f in films.items() if f}
        if len(es) >= 2 and 2 * len(films) > len(others) and 2 * (len(es) - len(others)) < len(es):
            ads.update(films)
    if not ads:
        return found
    return dict(found, extras=[e for e in found["extras"] if e["title"] not in ads],
                promos=sorted(set(found.get("promos") or []) | set(ads)),
                promo_names={**(found.get("promo_names") or {}), **{t: "Previews - " + f for t, f in ads.items()}})


# a ™ or ® read into a name's last word: a capital T after small letters ("Spider-ManT"), a TM ("DISCTM")
TRADEMARK_RE = re.compile(r"(?:(?<=[a-z]{2})T|(?<=[A-Za-z]{3})TM|\u2122|\u00ae)(?=\W*$)")


def plain_names(found, is_word):
    """found (menu_titles()) with the names that aren't readable dropped: the extra keeps a plain
    name. As found when there's no dictionary (is_word None). Read by RapidOCR (found["reader"]),
    judged more loosely (readable's sure): it leaves out the lines it isn't sure of, and a real
    name of words no well-known title has stands (Muppets from Space's "Cardshark Serenade",
    "Expectorating Extraterrestrials"), though a scrap of a name cut off doesn't ("22");
    Windows' OCR's and Tesseract's readings make junk of a backdrop ("Beye olticak Peeks")."""
    if not found or not is_word:
        return found
    known = is_word
    is_word = lambda w: w.lower() in EXTRAS_VOCAB or known(w)      # (and extras' own words)
    sure = found.get("reader") == "rapidocr"
    ok = lambda n: readable(n, is_word, sure)
    extras = [dict(e, name=unmarked(e["name"], is_word)) for e in found.get("extras") or []]
    # (a name RapidOCR read sure enough isn't judged: Spider-Man's "Cingular", no title's word)
    good = lambda e: e["name"] and ((e.get("sure") or 0) >= SURE_ENOUGH or ok(e["name"]))
    # (or, a little less sure, beside names on its page read well: the page's lettering reads, and
    # its words are the disc's own: Wallace & Gromit's gadgets, "The Soccamatic" 94%, "The 525
    # Crackervac" 95%, beside "A Christmas Cardomatic" and "Shopper 13")
    page_good = {}
    for e in extras:
        if e.get("page") and good(e):
            page_good[tuple(e["page"])] = page_good.get(tuple(e["page"]), 0) + 1
    # (its unknown words long ones, as made-up names have: a short one is a word misread, The Iron
    # Giant's "Piay All")
    coined = lambda n: all(len(w) >= 6 for w in re.findall(r"[A-Za-z]+", n) if not is_word(w))
    beside = lambda e: bool(e["name"]) and sure and (e.get("sure") or 0) >= SIBLING_SURE and \
        page_good.get(tuple(e.get("page") or ()), 0) >= 2 and coined(e["name"])
    # (not words, but another reading of its button is: Order of the Phoenix's "The Choreography of
    # Magic", surest as "Iagic")
    other = lambda e: next((a for a in e.get("also") or [] if ok(unmarked(a, is_word))), None)
    extras = [dict(e, name=e["name"] if good(e) or beside(e) else unmarked(other(e) or "", is_word)) for e in extras]
    # an ad's own words, too: unreadable, it's just "Previews" (the ad words, always readable)
    promo_names = {t: unmarked(n, is_word) for t, n in (found.get("promo_names") or {}).items()}
    promo_names = {t: n if " - " not in n or ok(n.split(" - ", 1)[1]) else n.split(" - ", 1)[0]
                   for t, n in promo_names.items()}
    fname = unmarked(found.get("feature_name") or "", is_word)
    return dict(found, extras=extras, promo_names=promo_names,
                feature_name=fname if fname and ok(fname) else "")


def title_pictures(out, out_dir):
    """For each title a menu button plays, the button to show for it: {title: {"image": the
    page's picture (a path), "rect": the button's [x0, y0, x1, y1] (the disc's 720x480), "text":
    its words as read}}. Only pages read have a picture; of a title's buttons, the one read
    best (the film: its Play button sooner than a scene's; nothing read, the first). The
    feature's, though: the button nearest the menu the disc opens on, as a viewer plays the film
    (WALL-E's main menu Play, read as "DO", not a commentary page's "Play Film" three presses on)."""
    best = {}
    found = out.get("found") or {}
    # (every film-length title's, as the feature's: each film of a disc of several (Who Done It?'s
    # Play on its own menu, not its commentary page's "Frank ON", which reads better), each cut of
    # one (its own Play): a commentary's button may go on with whichever is playing, its own doesn't)
    films = {found.get("feature")} | {f["title"] for f in found.get("features") or []} | {
        b["plays"]["title"] for p in out["pages"] for b in p["buttons"]
        if b.get("plays") and b["plays"].get("chapter") in (1, None) and (b["plays"].get("seconds") or 0) >= FILM_SECONDS}
    # (each title's reading its name comes from, as menu_titles takes it: its wordiest; the button that read it
    # is the one to show, its words there, not another of its buttons unread: Order of the Phoenix's "GRAWP",
    # one word, scores no more than nothing, and its unread copy on another menu won)
    named_by = {}
    for p in out["pages"]:
        for b in p["buttons"]:
            t, text = (b.get("plays") or {}).get("title"), b.get("text") or ""
            # (a name, a word of four letters at least: not a scrap off a logo, "pie 4", over an unread Play)
            if t and re.search(r"[A-Za-z]{4}", text) and (t not in named_by or _wordy(text) > _wordy(named_by[t])):
                named_by[t] = text
    for p in out["pages"]:
        if not p.get("image"):
            continue
        for b in p["buttons"]:
            t = (b.get("plays") or {}).get("title")
            if not t:
                continue
            score = _score(b.get("text") or "")
            if _ad_text(b.get("text")):         # (a run of ads: the button that starts it, for each)
                for t2 in b["plays"].get("after") or []:
                    if t2 not in best:
                        best[t2] = ((0, True, False, -b["n"]), {"image": os.path.join(out_dir, b.get("picture") or p["image"]),
                                                                "rect": list(b["rect"]), "text": b.get("text") or ""})
            # (a scrap read off a logo counts for nothing: then the first button, Play before FastPlay)
            # (and one that says it's an ad over one saying nothing: The Sorcerer's Apprentice's title
            # 66, FastPlay's first sneak peek too, but "Peeks" names it, "Ploy" doesn't)
            rank = (score if score >= 6 else 0, (b.get("text") or "") == named_by.get(t, None), _ad_text(b.get("text")),
                    b["plays"].get("chapter") == 1, -b["n"])
            if t in films:                      # (from its start, then nearest the first menu)
                rank = (b["plays"].get("chapter") == 1, -len(p.get("route") or [])) + rank
            if t not in best or rank > best[t][0]:
                best[t] = (rank, {"image": os.path.join(out_dir, b.get("picture") or p["image"]), "rect": list(b["rect"]),
                                  "text": b.get("text") or "", "_rects": [x["rect"] for x in p["buttons"]],
                                  "_words": b.get("words"), "lit": bool(b.get("picture"))})
    # outlined: where the name was read, as the reading says ("words": the lines kept, the name beside
    # a marker, its plate), never looked for again (a guess can show words that weren't the ones read);
    # not known (a reading saved before), the button's area, as the disc draws it ("button": that, either way)
    for _, v in best.values():
        v.pop("_rects", None)
        known = v.pop("_words", None)
        v["button"] = list(v["rect"])
        if known:
            v["rect"] = list(known)
    return {t: v for t, (_, v) in sorted(best.items())}


# A TV disc's menus. A film's reading (menu_titles) looks for the one title everything points to;
# on a TV disc most titles are episodes, and only the show's episode list can say which words name one.
TV_BONUS_RE = re.compile(r"\b(bonus|extras?|features?|materials?)\b", re.I)
TV_PLAY_RE = re.compile(r"\b(resume|continue)\b|^\W*play(\s+(the\s+)?(episodes?|show|season))?\W*$|^\W*start\W*$", re.I)
TV_SWITCH_RE = re.compile(r"^\W*(on|off)\W*$|\bcommentar", re.I)
# (a commentary you watch: a real extra, not the episode with its audio switched)
VIDEO_COMMENTARY_RE = re.compile(r"\bvideo\s+commentar\w*", re.I)


# a button saying only which cut of something it plays, no name for it
CUT_ALONE_RE = re.compile(r"^\W*(director'?s?\W*cut|producer'?s?\W*cut|(extended|alternate|uncut|unrated)\W*(cut|version|edition|episode)|"
                          r"uncut|unrated)\W*$", re.I)


def _scene_state(pages, s, known=None):
    """The one title a page's program jumps into by chapters (an episode's scene page: Smallville's
    Episodes page leads to one each, no button there playing the episode itself), or None."""
    hits = [(known or {}).get((s, b["n"])) for b in _state_buttons(pages, s)]
    chapters = {(h["plays"]["title"], h["plays"]["chapter"]) for h in hits if h and "plays" in h}
    return next(iter(chapters))[0] if len(chapters) >= 2 and len({t for t, _ in chapters}) == 1 else None


def _kind_alone(text):
    """Words saying only what kind of thing a button plays ("deleted scene"), no name for it."""
    rest = text or ""
    for _, rx in HEADING_KINDS:
        rest = re.sub(rx, "", rest, flags=re.I)
    return bool(heading_kind(text)) and not re.search(r"[A-Za-z]{2}", rest)


def _episode_named(words, episodes):
    """Of a disc's episodes, the one a few words name, by all of its name or a part (a deleted
    scene's button: "Real Girl", the name's first line unread, for Shawn and the Real Girl), or None;
    none either when two fit as well."""
    from . import core
    ratio = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
    r = core._menu_key(words)
    if len(r) < 4:
        return None
    scored = []
    for e in episodes:
        n = core._menu_key(e["name"])
        whole = ratio(r, n)
        part = max((ratio(r, n[i:i + len(r)]) for i in range(len(n) - len(r) + 1)), default=0) * 0.95
        scored.append((max(whole, part), e))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.8 or (len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02):
        return None
    return scored[0][1]


def _two_versions(p):
    """A page's buttons, by what they play and whatever they say: [{"title", "seconds", "place"}]
    when they play exactly two titles from their start, of about one length, or None. Most commentary
    pages switch the audio of one title, or have a button per episode."""
    titles = {}
    for b in p["buttons"]:
        plays = b.get("plays")
        if plays and plays.get("chapter") in (1, None) and not _nav_only(b.get("text") or ""):
            titles.setdefault(plays["title"], {"title": plays["title"], "seconds": plays.get("seconds"),
                                               "place": [r["button"] for r in p["route"]] + [b["n"]]})
    two = list(titles.values())
    if len(two) != 2 or abs((two[0]["seconds"] or 0) - (two[1]["seconds"] or 0)) > 60:
        return None
    return two


# a name giving a scene's number: "Sc. 8-10", "Sc. 12,13", "Sc. B 29-30", "Scene 4". With a link to an
# episode too, that's a deleted scene (the word "scene" by itself says nothing: a page of "Scenes" may
# be the film's chapters). Outtakes are bloopers, plain extras. The Scene type (a single shot or moment
# of the film) is left to be chosen by hand
SCENE_NUMBER_RE = re.compile(r"\b(?:sc[.,]?|scene)\s*(?:[a-z]\s+)?\d", re.I)         # (a comma: "Sc. 44" misread, Chuck S2 D3)
NOT_A_SCENE_RE = re.compile(r"\bout-?takes?\b|\bbloopers?\b|\bgag\s*reels?\b|\bcommentar\w*", re.I)


def numbered_scene(text):
    """Whether a name gives a scene's number, and isn't something else with one."""
    return bool(text and SCENE_NUMBER_RE.search(text) and not NOT_A_SCENE_RE.search(text))


def _episode_opening(words, episodes):
    """The episode a scene's name opens with, and the rest of its name ("Chuck Versus the Fat Lady
    Sc. 8-10 - Chuck with Jill": that episode, "Sc. 8-10 - Chuck with Jill"), or None; none either
    when two fit as well, or nothing's left of the name. It has to end as the episode's name ends:
    "Chuck Versus the" is most of "Chuck Versus the Ex", and no more that one's than any other's."""
    from . import core
    ratio = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
    ends = [m.end() for m in re.finditer(r"\S+", words or "")]
    last = lambda text: core._menu_key((text.split() or [""])[-1])
    scored = []
    for e in episodes:
        n = core._menu_key(e["name"])
        score, end = max(((ratio(core._menu_key(words[:i]), n), i) for i in ends
                          if ratio(last(words[:i]), last(e["name"])) >= 0.6), default=(0, 0))
        scored.append((score, end, e))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.85 or (len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02):
        return None
    rest = words[scored[0][1]:].lstrip(" -\u2013\u2014:,.")
    return (scored[0][2], rest) if rest else None


def _episode_above(r, placed):
    """The episode a button sits under on its page ("Video Commentary", its words naming nothing):
    the nearest above it that it shares the most width with. placed: [(reading, pick)]."""
    x0, y0, x1, _ = r["rect"]
    best = None
    for other, pick in placed:
        if other.get("page") != r.get("page"):
            continue
        ox0, oy0, ox1, _ = other["rect"]
        width = min(x1, ox1) - max(x0, ox0)
        if width > 0 and oy0 < y0 and (best is None or (width, oy0) > best[0]):
            best = ((width, oy0), pick)
    return best[1] if best else None


def tv_titles(out):
    """What a TV disc's menus say, before anyone asks the show: {"readings": [{"title", "text",
    "seconds", "bonus", "kind", "page", "rect", "image"}], one for each title with words of its own,
    in the menus' order; "promos", "promo_names", "play_alls", "opening"}. A button stands for the
    title it plays from its start, or the title of the scene page it leads to. "bonus": found only
    on a page of bonus materials (Psych's International Pilot, as long as an episode). A title with
    any other button isn't a bonus: Smallville's Special Features play its episodes, audio switched."""
    pages = {(p["domain"], p["page"], p["program"]): p for p in out["pages"]}
    words = {(s, b["n"]): b["text"] for s, p in pages.items() for b in p["buttons"]}

    def scene_title(domain, goes):
        """The title of the scene page a button leads to."""
        p = pages.get((domain, goes.get("page"), goes.get("program"))) if isinstance(goes, dict) else None
        plays = [b["plays"] for b in (p or {}).get("buttons", []) if b.get("plays")]
        titles = {x["title"] for x in plays}
        return plays[0] if len(titles) == 1 and len({x.get("chapter") for x in plays}) >= 2 else None

    seen, promos, promo_names, runs, versions = {}, [], {}, [], []
    for s, p in pages.items():
        on_way = [(s[0], r["page"], r["program"]) for r in p["route"]]
        way = [words.get((x, r["button"]), "") for x, r in zip(on_way, p["route"])]
        via = " ".join(way + [p.get("heading") or ""])
        kind = page_kind(p)
        for w in reversed([] if p.get("mixed") else way):
            if kind or HUB_RE.search(w) or MAIN_MENU_RE.search(w):
                break
            kind = heading_kind(w)
        bonus = bool(TV_BONUS_RE.search(way[-1] if way else "") or TV_BONUS_RE.search(p.get("heading") or ""))
        ads = _ads_page(way, p.get("heading"), p["buttons"], p.get("heading_line"))
        for b in p["buttons"]:
            text = b.get("text") or ""
            plays = b.get("plays") or scene_title(s[0], b.get("goes_to"))
            if not plays or (b.get("plays") and b["plays"].get("chapter") not in (1, None)):
                continue
            t = plays["title"]
            if ads or _ad_text(text):
                promos.extend(x for x in [t] + list(plays.get("after") or []) if x not in promos)
                promo_names.setdefault(t, promo_name([{"text": text, "via": via}]))
                continue
            if PLAY_ALL_RE.search(text) or TV_PLAY_RE.search(text) or _play_text(text):
                runs.append(t)
                continue
            if not re.search(r"[A-Za-z]{2}", text) or _nav_only(text):
                continue
            x = seen.get(t)
            # where it is in the menus: the buttons pressed to reach it, this one last (a page's
            # extras come where the button to that page is on the page before)
            place = [r["button"] for r in p["route"]] + [b["n"]]
            if x is None:
                seen[t] = dict(title=t, text=text, seconds=plays.get("seconds"), bonus=bonus, kind=kind,
                               page=list(s), rect=list(b["rect"]), words=b.get("words"), place=place,
                               image=b.get("picture") or p.get("image"), lit=bool(b.get("picture")),
                               switch=bool(TV_SWITCH_RE.search(text)) and not VIDEO_COMMENTARY_RE.search(text),
                               # (only its kind: "Deleted Scenes" isn't Smallville's "Delete")
                               only_kind=_kind_alone(text),
                               # (Psych S8 D2's "Director's Cut": no episode's name, whatever its place)
                               only_cut=bool(CUT_ALONE_RE.search(text)),
                               # (a page of them: its buttons say only whose)
                               commentaries=bool(VIDEO_COMMENTARY_RE.search(via)))
            elif x["bonus"] and not bonus:        # (its own button, over a commentary's)
                seen[t] = dict(x, text=text, bonus=False, kind=kind, page=list(s), rect=list(b["rect"]), words=b.get("words"),
                               image=b.get("picture") or p.get("image"), lit=bool(b.get("picture")), switch=False, place=place)
            elif not bonus:
                x["bonus"] = False
        # an episode again with a commentary for its audio, stored as its own title (Boy Meets World
        # S1 D1: no cells shared, so the scan can't tell). Its page's words need only say commentary
        if not ads and re.search(r"\bcommentar", via, re.I) and not VIDEO_COMMENTARY_RE.search(via):
            versions.append(_two_versions(p))
    offered = set(seen) | set(promos)
    play_alls = sorted(set(runs) - offered)
    # every title any button plays, read or not: only the rest aren't in the menus (Psych's bonus
    # pages' buttons, their words unread, play real extras)
    played = set()
    for p in pages.values():
        for b in p["buttons"]:
            got = b.get("plays") or scene_title(p["domain"], b.get("goes_to"))
            if got:
                played.update([got["title"]] + list(got.get("after") or []) + [o["title"] for o in got.get("others") or []])
    return {"readings": list(seen.values()), "promos": promos, "promo_names": promo_names, "played": sorted(played),
            "play_alls": play_alls, "commentary_versions": [v for v in versions if v],
            "opening": [t for t in out.get("opening") or [] if t not in offered and t not in play_alls]}


def tv_found(tv, picks=None):
    """tv_titles, with the show's answer (core.menu_episodes' picks: {index in tv's readings:
    {"season", "number", "name", "score", "by"}}): {"episodes": [{"title", "season", "episode",
    "name", "read", "by"}], "extras": [...], and tv_titles' promos, play_alls, opening}. A reading
    no episode took is an extra only when the menus file it under their bonus materials or it's too
    short for an episode; else nothing is said of it (the scan's guess stands)."""
    picks = picks or {}
    longest = max((r["seconds"] or 0 for i, r in enumerate(tv["readings"]) if i in picks), default=0)
    placed = [(r, picks[i]) for i, r in enumerate(tv["readings"]) if i in picks and r.get("rect")]
    picked = {r["title"]: picks[i] for i, r in enumerate(tv["readings"]) if i in picks}
    # of a commentary page's two titles, the one that isn't the episode, unless its button names it
    said = {r["title"]: r for r in tv["readings"]}
    versions = {}
    for two in tv.get("commentary_versions") or []:
        known = [v for v in two if v["title"] in picked]
        other = [v for v in two if v["title"] not in picked]
        r = said.get(other[0]["title"]) if other else None
        if len(known) == 1 and (not r or r.get("switch") or len(re.findall(r"[A-Za-z]", r["text"])) < 4):
            versions[other[0]["title"]] = dict(other[0], of=picked[known[0]["title"]])
    episodes, extras = [], []
    for i, r in enumerate(tv["readings"]):
        if i in picks:
            e = picks[i]
            episodes.append({"title": r["title"], "season": e["season"], "episode": e["number"], "name": e["name"],
                             "read": r["text"], "by": e.get("by") or "name"})
        elif r.get("only_cut"):
            # the episode's it sits under, an extra: Plex has one file for an episode, and this is
            # another cut of it, its own encode, 6 seconds longer (Psych S8 D2's A Nightmare on State Street)
            ep = r.get("rect") and _episode_above(r, placed)
            said = nice_name(r["text"])
            extras.append({"title": r["title"], "name": "%s - %s" % (ep["name"], said) if ep else said,
                           "seconds": r["seconds"], "kind": None})
            if ep:
                extras[-1].update(season=ep["season"], episode=ep["number"])
            if r.get("place"):
                extras[-1]["place"] = r["place"]
        elif r["title"] not in versions and r["text"] and not r.get("switch") and (r["bonus"] or r.get("kind") or
                                                    (longest and (r["seconds"] or 0) < 0.5 * longest)):
            extras.append({"title": r["title"], "name": nice_name(LISTED_RE.sub("", r["text"])),
                           "seconds": r["seconds"], "kind": r.get("kind") or heading_kind(r["text"])})
            if r.get("read"):                 # (put right: fix_tv_readings, what was read to show)
                extras[-1]["read"] = r["read"]
            if r.get("own_name"):             # (a whole page of an episode's scenes: each says which)
                extras[-1]["own_name"] = True
            if r.get("place"):
                extras[-1]["place"] = r["place"]
            # (the disc's own files say whose)
            if r.get("of_episode") is not None:
                extras[-1]["under"] = next((p for p in picks.values() if p["number"] == r["of_episode"]), None)
            # a button saying only that it's a deleted scene: the episode it sits under's (Psych S1 D3's)
            elif r.get("only_kind") and extras[-1]["kind"] == "DeletedScene" and r.get("rect"):
                extras[-1]["under"] = _episode_above(r, placed)
            said = VIDEO_COMMENTARY_RE.search(r["text"])
            # whose: the words before it ("Shawn and the Real Girl Video Commentary with Writer Andy
            # Berman"), else the episode it sits under. What it says from there on is its name:
            # the line above a button runs into it when read ("Extended Version Video Commentary")
            ep = said and (_episode_named(r["text"][:said.start()], list(picks.values()))
                           or (r.get("rect") and _episode_above(r, placed)))
            if ep:
                extras[-1].update(season=ep["season"], episode=ep["number"],
                                  name="%s %s" % (ep["name"], nice_name(r["text"][said.start():])))
            elif r.get("commentaries"):
                ep = _episode_named(r["text"], list(picks.values()))
                if ep:
                    extras[-1].update(season=ep["season"], episode=ep["number"],
                                      name="%s Video Commentary" % ep["name"])
    for v in versions.values():
        e = v["of"]
        extras.append({"title": v["title"], "name": "%s Audio Commentary" % e["name"], "seconds": v["seconds"],
                       "kind": None, "season": e["season"], "episode": e["number"], "place": v["place"]})
    # a button saying only its kind ("deleted scene", under an episode's name), and the same length
    # named on the bonus page: one scene, reached twice (Psych S1 D3's), its kind from the one, its
    # name from the other
    for bare in [e for e in extras if _kind_alone(e["name"])]:
        twin = next((e for e in extras if e is not bare and not _kind_alone(e["name"])
                     and abs((e["seconds"] or 0) - (bare["seconds"] or 0)) <= 1), None)
        if twin:
            # (its name may say so already: "Was It Something I Said? Music Video", and "Music Video")
            said = bare["name"].lower() in (twin["name"] or "").lower()
            twin["kind"], twin["name"] = heading_kind(bare["name"]), twin["name"] if said else "%s %s" % (twin["name"], bare["name"])
            extras.remove(bare)
    # a deleted scene whose words name one of this disc's episodes: that episode's, named for it
    for e in extras:
        # (a page of them, each saying whose, whatever they are: that's its episode, the rest its name)
        # (only its kind left: "Pilot Deleted Scene", named for its episode below)
        opening = _episode_opening(e["name"], episodes) if not e.get("episode") else None
        if opening and not _kind_alone(opening[1]):
            e.pop("under", None)
            e.update(season=opening[0]["season"], episode=opening[0]["episode"], name=opening[1],
                     kind=e["kind"] or ("DeletedScene" if numbered_scene(opening[1]) else None))
            continue
        if e["kind"] == "DeletedScene":
            said = re.search(DELETED_WORDS, e["name"], re.I)
            ep = _episode_named(re.sub(DELETED_WORDS, " ", e["name"], flags=re.I), episodes)
            under = e.pop("under", None)
            if not ep and under:
                ep = {"season": under["season"], "episode": under["number"], "name": under["name"]}
            if ep:
                e.update(season=ep["season"], episode=ep["episode"])
                if not e.get("own_name"):
                    e["name"] = "%s %s" % (ep["name"], nice_name(said.group(0)) if said else "Deleted Scene")
    return dict({k: tv.get(k) for k in ("promos", "promo_names", "play_alls", "opening", "played")},
                tv=True, episodes=episodes, extras=extras)


NOT_IN_MENUS = "not in the disc's menus"


def detected_tv_labels(found, titles, keep=()):
    """tv_found as labels for a scan's titles, the shape of core.get_labels. Only what the menus
    say: a title they say nothing of keeps the scan's guess (never an ad for that alone, as on a
    film's disc: a TV disc's own episodes may have no button). Titles in keep (your labels,
    TheDiscDB's names) and those the scan sets aside as copies are left be."""
    out = {}
    name = title_id
    episodes = {name(e["title"]): e for e in found.get("episodes") or []}
    extras = {name(e["title"]): e for e in found.get("extras") or []}
    promos = {title_id(n) for n in found.get("promos") or []}
    names = {title_id(n): v for n, v in (found.get("promo_names") or {}).items()}
    opening = {title_id(n) for n in found.get("opening") or []}
    labelled = set()
    played = found.get("played")
    played = None if played is None else {name(t) for t in played}
    # copies, as the scan has them (a title's cells don't outlast it): one label, on the title it
    # kept, with the most any of their buttons said
    copy_of = {}
    for t in titles:
        m = re.match(r"same video as (.+?)\s*$", t.get("note") or "") if t.get("role") == "duplicate" else None
        if m:
            copy_of[t["id"]] = m.group(1)
    for tid, e in list(extras.items()):
        home = copy_of.get(tid, tid)
        if home != tid:
            del extras[tid]
            own = extras.get(home) or {}
            if home not in extras or len(e["name"] or "") > len(own["name"] or ""):
                # (its own place in the menus, if it has one: the page of them, not a copy's button)
                extras[home] = dict(own, **e)
                if own.get("place"):
                    extras[home]["place"] = own["place"]
    for t in titles:
        tid = t["id"]
        if tid in keep or t.get("role") in ("duplicate", "segment"):
            continue
        if tid in episodes:
            e = episodes[tid]
            read = " (read as \"%s\")" % e["read"] if e["read"] else " (no name read)"
            out[tid] = {"type": "Episode", "title": e["name"], "season": e["season"], "episode": e["episode"],
                        "note": "the disc's menu files" if e["by"] == "disc"
                        else "the menus: \"%s\"" % e["read"] if e["by"] == "name"
                        else "the menus: its place in the disc's Play All" + read if e["by"] == "play-all"
                        else "the menus: between its neighbours on their episode page" + read}
            # (read otherwise than the episode list names it: as read, to show, as an extra put right is;
            # its list number isn't a difference, "1) Chuck Versus the Pink Slip", but its letters are)
            if e["read"] and e["name"] and _listed_off(e["read"]) != " ".join(e["name"].split()):
                out[tid]["read"] = e["read"]
        elif tid in extras and extras[tid]["name"]:
            e = extras[tid]
            # (the same scene by two buttons, the scan not having said: one label)
            same = (e["name"], round((e["seconds"] or 0) / 2))
            if same in labelled:
                continue
            labelled.add(same)
            out[tid] = {"type": e.get("kind") or "Extra", "title": e["name"], "note": "the menus: an extra"}
            if e.get("read"):               # (a name put right: as read, to show)
                out[tid]["read"] = e["read"]
            if e.get("place"):
                out[tid]["place"] = e["place"]
            if e.get("episode"):
                out[tid].update(season=e["season"], episode=e["episode"], note="the menus: this episode's")
        elif tid in promos:
            out[tid] = {"type": "Promo", "title": names.get(tid), "note": "the menus: an ad"}
        elif tid in opening:
            out[tid] = {"type": "Promo", "title": "Previews",
                        "note": "plays before the menu on a player (VLC skips it): previews, as a rule"}
        elif episodes and extras and played is not None and t.get("role") == "extra" and tid not in extras and tid not in played:
            # (a show's menus list what it's selling: the scan's extra they don't is a warning, a logo.
            # Out of the way, for you to look at if you like. Not its episodes: some have no button.
            # Only once its extras were found too: a page the crawl missed says nothing of them)
            out[tid] = {"type": "Other", "title": None, "note": NOT_IN_MENUS}
    return out


def _listed_off(text):
    """A reading without the list number a menu prints before it ("3) "), its spaces made single."""
    return " ".join(re.sub(r"^\W*\d{1,2}\s*[).]\s+", "", text or "").split())


def tv_pictures(tv, out_dir, pictures=None):
    """title_pictures, with each reading's own button over it: the button that names the title, one
    leading to its scene page too (no button there plays it: Smallville's Episodes page)."""
    out = dict(pictures or {})
    for r in tv.get("readings") or []:
        if r.get("image"):
            out[r["title"]] = {"image": os.path.join(out_dir, r["image"]), "rect": list(r.get("words") or r["rect"]),
                               "button": list(r["rect"]), "text": r.get("on_button", r.get("read") or r["text"]),
                               "alone": bool(r.get("alone")),       # (what was read, as a film's: put right, "read")
                               "lit": bool(r.get("lit"))}              # (the page drawn with its button selected)
    return out


def found_tv_text(found):
    """tv_found in words, for the log and the command line."""
    how = {"name": "", "neighbours": ", by its neighbours", "play-all": ", by the Play All"}
    lines = ["Episode:  title %s  S%sE%s %s (read \"%s\"%s)" % (
        e["title"], e["season"], e["episode"], e["name"], e["read"], how.get(e["by"], ""))
        for e in found.get("episodes") or []]
    lines += ["%-9s title %s  %s%s" % ("Deleted:" if e.get("kind") == "DeletedScene" else "Extra:", e["title"], e["name"],
                                       " (S%sE%s)" % (e["season"], e["episode"]) if e.get("episode") else "")
              for e in found.get("extras") or []]
    if found.get("promos"):
        lines.append("Ads:      titles " + ", ".join(map(str, found["promos"])))
    if found.get("play_alls"):
        lines.append("Play All: titles " + ", ".join(map(str, found["play_alls"])))
    if found.get("opening"):
        lines.append("Previews: titles " + ", ".join(map(str, found["opening"])))
    return "\n".join(lines)


HIDDEN_ROLES = ("duplicate", "play-all", "segment", "promo", "loop", "junk", "decoy")
# set aside by the scan as copies of something else: the menus naming one doesn't change that
# (a short "junk" clip the menus offer is real content: an outtake, a teaser)
COPY_ROLES = ("duplicate", "play-all", "segment")


def detected_labels(found, titles, keep=()):
    """menu_titles() as labels for a scan's titles ({"Title N": {"type", "title", "note"}}, the
    shape of core.get_labels): the feature MainMovie, each extra an Extra with its menu name,
    and every other title a Promo, the menus offering nothing that plays it. Titles in keep
    (your labels, TheDiscDB's names) are left as they are, and so are titles the scan sets aside
    as copies or play-alls; one it sets aside as too short to matter ("junk") is added when the
    menus offer it (Muppets from Space's outtakes: 10-44 seconds each), but not made a Promo.
    Of two titles the scan found the same video, the one the menus play is the one labelled (its
    twin, then, the copy: Spider-Man 2's previews page plays title 17, the scan kept 10).
    {} when the menus said nothing (then nothing changes)."""
    if not found or not (found.get("feature") or found.get("extras")):
        return {}
    named = {title_id(e["title"]): e["name"] for e in found.get("extras") or []}
    kinds = {title_id(e["title"]): e.get("kind") for e in found.get("extras") or []}
    others = {title_id(e["title"]) for e in found.get("extras") or [] if e.get("other")}
    # (a name put right: as read, to show: correct_names)
    read_as = {title_id(e["title"]): e["read"] for e in found.get("extras") or [] if e.get("read")}
    read_as.update({title_id(t): r for t, r in (found.get("promo_read") or {}).items()})
    said = lambda tid, note: note + (" (read as \"%s\")" % read_as[tid] if tid in read_as else "")
    feature = title_id(found["feature"]) if found.get("feature") else None
    # several films, each with its own menu: each a main movie (its heading in the note)
    films = {title_id(f["title"]): f.get("name") or "" for f in found.get("features") or []}
    promos = {title_id(n) for n in found.get("promos") or []}
    promo_names = {title_id(n): name for n, name in (found.get("promo_names") or {}).items()}
    # what a player shows before the menu: its previews (Jurassic World's preview reel)
    opening = {title_id(n) for n in found.get("opening") or []}
    played = lambda tid: tid in named or tid == feature or tid in promos or tid in films
    # the feature named by its button only beside another cut ("Signature Edition")
    cuts = any(t["id"] in named and t.get("role") in ("feature", "version") for t in titles)
    # a page's Play All: the scan says what it plays (left unlabelled, never an ad)
    play_alls = {title_id(n) for n in found.get("play_alls") or []}
    # copies the menus play, of titles they don't: labelled in their twin's place
    twins = {}
    for t in titles:
        m = re.match(r"same video as (.+?)\s*$", t.get("note") or "") if t.get("role") == "duplicate" else None
        if m and played(t["id"]) and not played(m.group(1)):
            twins[t["id"]] = m.group(1)
    out = {}
    for t in titles:
        tid = t["id"]
        offered = played(tid) or tid in opening
        if tid in keep or tid in play_alls or tid in twins.values() or (t.get("role") in COPY_ROLES and tid not in twins) or \
                (t.get("role") in HIDDEN_ROLES and not offered):
            continue
        if tid in films:
            out[tid] = {"type": "MainMovie", "title": None,
                        "note": "the menus: a film of its own" + (" (its menu says \"%s\")" % films[tid] if films[tid] else "")}
        elif tid == feature:
            out[tid] = {"type": "MainMovie", "title": (cuts and found.get("feature_name")) or None,
                        "note": "the menus: the feature"}
        elif tid in named and t.get("role") in ("feature", "version"):
            # another cut of the film, offered beside it (The Iron Giant's "Signature Edition" and
            # "Original Edition"): a main movie too, not an extra
            out[tid] = {"type": "MainMovie", "title": named[tid] or None, "note": "the menus: another cut of the film"}
        elif tid in named:
            # its page's heading says ("MUSIC VIDEOS"); or the film's own trailer: TheDiscDB's
            # Trailer (ads were promos already, by AD_RE)
            # (a letter misread is still a trailer: Abbott and Costello Go to Mars's "THEATRICAL TAAILER",
            # its name put right later)
            # (not another word: a featurette's "Trainer")
            kind = kinds.get(tid) or ("Trailer" if any(_edits(w, "trailer") <= 1 and w not in ("trainer", "trailed")
                                                       for w in re.findall(r"[a-z]+", (named[tid] or "").lower()))
                                      else "Extra")
            out[tid] = {"type": kind, "title": named[tid] or None,
                        "note": said(tid, "the menus: another version of an extra (a setting picks it): worth a look"
                                     if tid in others else "the menus: an extra")}
        elif tid in promos:
            out[tid] = {"type": "Promo", "title": promo_names.get(tid), "note": said(tid, "the menus: an ad")}
        elif tid in opening:
            out[tid] = {"type": "Promo", "title": "Previews",
                        "note": "plays before the menu on a player (VLC skips it): previews, as a rule"}
        else:
            out[tid] = {"type": "Promo", "title": None, "note": "the menus: nothing plays it"}
    places = {title_id(n): p for n, p in (found.get("places") or {}).items()}
    for tid, label in out.items():
        if tid in places:
            label["place"] = places[tid]
        if tid in read_as and label.get("title"):       # (a name put right: as read, to show)
            label["read"] = read_as[tid]
    return out


def found_text(found):
    """menu_titles(), readable."""
    lines = []
    if found.get("features"):
        for f in found["features"]:
            lines.append("Film:     title %s  %s" % (f["title"], f["name"] or "(its menu's heading not read)"))
    elif found["feature"]:
        lines.append("Feature:  title %s" % found["feature"])
    for e in found["extras"]:
        check = ""
        if e["listed"]:
            ok = abs(e["listed"] - (e["seconds"] or 0)) <= max(10, 0.03 * e["listed"])
            check = "  (menu says %s, plays %s%s)" % (_clock(e["listed"]), _clock(e["seconds"]), "" if ok else ": doesn't fit")
        kind = "  [%s]" % e["kind"] if e.get("kind") else ""
        lines.append("Extra:    title %s  %s%s%s" % (e["title"], e["name"] or "(no name read)", kind, check))
    if found["promos"]:
        lines.append("Trailers: title%s %s" % ("s" if len(found["promos"]) > 1 else "", ", ".join(map(str, found["promos"]))))
    if found.get("play_alls"):
        lines.append("Play all: title%s %s" % ("s" if len(found["play_alls"]) > 1 else "",
                                               ", ".join(map(str, found["play_alls"]))))
    if found.get("opening"):
        lines.append("Previews: title%s %s (before the menu, on a player)" % (
            "s" if len(found["opening"]) > 1 else "", ", ".join(map(str, found["opening"]))))
    return "\n".join(lines)


def crawl_text(out):
    """The crawl, readable: each page, then each button's words and what it does."""
    lines = []
    for p in out["pages"]:
        where = "VMG" if p["domain"] == "vmgm" else "VTS %s" % p["domain"]
        head = "%s page %d%s" % (where, p["page"], " program %d" % p["program"] if p["program"] > 1 else "")
        if not p.get("image", True):
            lines.append("%s   passed by: %s" % (head, p.get("passed_by") or "not reached"))
            continue
        lines.append("%s   (%s)" % (head, p["image"]))
        for b in p["buttons"]:
            g = b.get("goes_to")
            if g == "auto":
                does = "(turns the page on its own)"
            elif g:
                does = "-> page %d%s%s" % (g["page"], " program %d" % g["program"] if g["program"] > 1 else "",
                                           " (found by pressing)" if g.get("pressed") else
                                           " (the disc's program)" if g.get("engine") else "")
            elif b.get("plays"):
                pl = b["plays"]
                does = "plays title %s (%s) from chapter %s%s" % (
                    pl["title"], _clock(pl["seconds"]), pl["chapter"],
                    ", as the first Play button did (not pressed)" if pl.get("assumed") == "play"
                    else ", the feature the scenes point to (not pressed)" if pl.get("assumed")
                    else ", the disc's program" if pl.get("engine")
                    else "" if pl.get("known") else ", pressed")
                if b.get("before"):
                    does += ", after title%s %s" % ("s" if len(b["before"]) > 1 else "", ", ".join(map(str, b["before"])))
                for o in pl.get("others") or []:
                    does += "; or title %s (%s), by another route" % (o["title"], _clock(o.get("seconds")))
                if pl.get("after"):
                    does += ", then title%s %s" % ("s" if len(pl["after"]) > 1 else "", ", ".join(map(str, pl["after"])))
            elif b.get("menu_only"):
                does = "plays from the disc's menu (%s): not a title, not rippable" % _clock(b["menu_only"])
            elif b.get("error"):
                does = "?? " + b["error"]
            elif b.get("passed_by"):
                does = "(passed by)"
            else:
                does = "stays in the menus"
            lines.append("    %2d  %-34s %s" % (b["n"], '"%s"' % b["text"], does))
        lines.append("")
    if out.get("found"):
        lines.append(found_text(out["found"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------

def _summary(structure):
    lines = []
    for dom, pages in [("VMG", structure["vmgm"])] + [("VTS %d" % k, v) for k, v in sorted(structure["vtsm"].items())]:
        for p in pages:
            progs = p.get("program_buttons") or [p["buttons"]]
            for k, btns in enumerate(progs, 1):
                if not btns or (k > 1 and btns == progs[0]):
                    continue
                lines.append("%s page %d%s (%s): %d buttons" % (dom, p["pgc"], " program %d" % k if k > 1 else "",
                                                               p["type"] or "-", len(btns)))
                for b in btns:
                    c = b["cmd"]
                    lines.append("    %2d %-20s %s" % (b["n"], b["rect"], " ".join("%s=%s" % kv for kv in c.items())))
    return "\n".join(lines)


def _dictionary():
    """core.title_words() from the IMDb index, or None without one."""
    from . import core
    try:
        return core.title_words(core.imdb_open(core.default_imdb_dir(), allow_download=False, log=lambda *a: None))
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m disc_detective.menus", description=__doc__.split("\n\n")[0])
    ap.add_argument("drive", nargs="?", help="the DVD drive or folder, e.g. E:")
    ap.add_argument("--replay", metavar="FINGERPRINT",
                    help="run the disc's program on a crawled disc's kept test data (no disc, no VLC)")
    ap.add_argument("--vlc", help="VLC's folder (default: found where VLC installs)")
    ap.add_argument("--out", help="where to write the results (default: ~/.disc_detective/menus/<fingerprint>)")
    ap.add_argument("--no-vlc", action="store_true", help="the menu structure only")
    ap.add_argument("--probe", action="store_true", help="a short VLC probe instead of the crawl")
    ap.add_argument("--tv", action="store_true", help="a TV disc: its episode pages read too")
    ap.add_argument("--all", action="store_true",
                    help="press every button, scenes, trailers and settings too (slow; for checking a disc)")
    a = ap.parse_args(argv)
    if a.replay:
        replay(a.replay)
        return 0
    if not a.drive:
        ap.error("give a drive (or --replay FINGERPRINT)")
    from . import core
    root = a.drive if not re.fullmatch(r"[A-Za-z]:", a.drive.rstrip("\\/")) else a.drive.rstrip("\\/") + "\\"
    out = a.out
    if not out:
        fmt, base = core.detect_format(root)
        if not fmt:
            print("%s: no DVD (VIDEO_TS) found" % root)
            return 1
        out = menu_dir(core.fingerprint(fmt, base)["hash"])
    os.makedirs(out, exist_ok=True)
    vlc = None
    if not a.no_vlc:
        setting = a.vlc
        if not setting:                     # the VLC folder from Settings, if one was set
            try:
                with open(os.path.join(core.data_home(), "settings.json"), encoding="utf-8") as fh:
                    setting = json.load(fh).get("vlc") or None
            except (OSError, ValueError):
                pass
        vlc = find_vlc(setting)
        if not vlc:
            print("VLC not found: install it, or pass --vlc with its folder")
            return 1
    try:
        s = read_structure(root, vlc)
    except Refused as e:
        print("%s: the disc is copy-protected; run without --no-vlc (VLC unlocks the drive)" % e)
        return 1
    with open(os.path.join(out, "structure.json"), "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2)
    print(_summary(s))
    print("structure written to", os.path.join(out, "structure.json"))
    if a.no_vlc:
        return 0
    if a.probe:
        probe(root, vlc, out)
        print("probe written to", os.path.join(out, "probe.json"))
        return 0
    result = crawl(root, vlc, s, out, everything=a.all, is_word=core.word_checker(core.default_imdb_dir()),
                   tv=a.tv)
    result["found"] = plain_names(result["found"], _dictionary())
    print()
    print(crawl_text(result))
    print("crawl written to %s (%.1f minutes)" % (os.path.join(out, "crawl.json"), result["minutes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
