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
    page's cells one video unit (VOBU) at a time; each navigation packet says where the next is."""
    for cell in cells:
        sector = cell["first"]
        while sector <= cell["last"]:
            vob.seek(sector * SECTOR)
            try:
                s = vob.read(SECTOR)
            except OSError as e:
                raise Refused("the drive won't read sector %d of the menus" % sector) from e
            if len(s) < SECTOR or s[0:4] != b"\x00\x00\x01\xba" or s[0x26:0x2A] != b"\x00\x00\x01\xbf":
                break
            out = _nav_buttons(s)
            if out:
                return out
            sector += _u32(s, 0x40F) + 1        # the DSI's VOBU end address: the next unit follows it
    return []


def _nav_buttons(s):
    """The buttons in one navigation packet (a 2048-byte sector)."""
    pci = s[0x2D:]
    hli = 0x60
    forced = pci[hli + 20]                      # the button the page selects as it comes on (0: none)
    out = []
    for i in range(pci[hli + 17]):
        b = pci[hli + 46 + 18 * i: hli + 46 + 18 * (i + 1)]
        x = (b[0] << 16) | (b[1] << 8) | b[2]
        y = (b[3] << 16) | (b[4] << 8) | b[5]
        out.append({"n": i + 1, "rect": [(x >> 12) & 0x3FF, (y >> 12) & 0x3FF, x & 0x3FF, y & 0x3FF],
                    "auto": bool((b[3] >> 6) & 3), "up": b[6] & 0x3F, "down": b[7] & 0x3F,
                    "left": b[8] & 0x3F, "right": b[9] & 0x3F, "cmd": command(b[10:18]),
                    "raw": b[10:18].hex()})
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
                progs = [_buttons(vob, p["cells"][a - 1:(starts[i + 1] - 1 if i + 1 < len(starts) else None)])
                         if vob else [] for i, a in enumerate(starts)]
                pages.append({"pgc": n, "type": typ, "entry": entry, "cells": p["cells"],
                              "buttons": next((b for b in progs if b), []), "program_buttons": progs,
                              "program_cells": starts, "pre": [command(c) for c in p["pre"]]})
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
              "story", "production", "back", "next", "on", "off"}
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


def button_text(image, rect, frame_size=(720, 480), pad=4):
    """What's written in a button's rectangle of a menu picture (a PIL image), or "". The
    rectangle is in the disc's 720x480 coordinates; the picture may be any size. Menu lettering
    is fancy, so it's read several ways and the reading that looks most like words wins: grey,
    black and white, and the bright coloured letters lifted off the background (yellow 3D letters
    over a starfield: Muppets from Space); each as one line and as a block, a name over two lines
    joined back up; and, read badly every way, tilted a little each way (slanted lettering)."""
    if not shutil.which("tesseract"):
        return ""
    from PIL import Image, ImageChops, ImageOps, ImageStat
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
    x0, y0, x1, y1 = rect
    box = (max(0, int((x0 - pad) * sx)), max(0, int((y0 - pad) * sy)),
           min(image.width, int((x1 + pad) * sx)), min(image.height, int((y1 + pad) * sy)))
    colour = image.crop(box).convert("RGB")
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
            out = subprocess.run(["tesseract", tmp, "stdout", "--psm", psm], capture_output=True, text=True,
                                 timeout=20, **_no_console()).stdout
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


def button_words(image, rect, others=(), frame_size=(720, 480)):
    """A button's words (button_text): for a tiny one, a light or a pointer by its label
    (Spider-Man's: lamps 5-16 pixels wide beside "SCENE SELECTIONS", pointers in panels saying
    "STAN LEE'S MUTANTS, MONSTERS & MARVELS"), what's printed round it, if that reads better."""
    text = button_text(image, rect, frame_size)
    x0, y0, x1, y1 = rect
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
    blanked first: a main menu's "Sneak Peeks" button isn't its heading."""
    if not shutil.which("tesseract"):
        return ""
    from PIL import Image, ImageDraw, ImageOps, ImageStat
    sx, sy = image.width / frame_size[0], image.height / frame_size[1]
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
        out = subprocess.run(["tesseract", tmp, "stdout", "--psm", "11"], capture_output=True, text=True,
                             timeout=30, **_no_console()).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    words = re.findall(r"[A-Za-z][A-Za-z']+", out)
    return " ".join(words)


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
        if len(core) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z'&-]*|\d+(:\d\d)*|&", core) and (
                core.isupper() or core.istitle() or core.islower() or core.isdigit() or ":" in core):
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
ADJUST_ENABLE, ADJUST_CONTRAST, ADJUST_BRIGHTNESS = 0, 1, 2     # libvlc_video_adjust_option_t
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


def watch_title(folder, source, title):
    """Plays one title of a DVD (a drive or a folder) in VLC, in a window of its own: to see
    what it is. False if there's no VLC player to open."""
    exe = vlc_player(folder) if folder else None
    if not exe:
        return False
    subprocess.Popen([exe, dvd_mrl(source) + "#%d" % title], close_fds=True)
    return True


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
        self.is_dark = False
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

    def open(self, mrl):
        L = self.lib
        media = L.libvlc_media_new_location(self.inst, mrl.encode("utf-8"))
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
            img.info["dark"] = True
        return img

    def settle(self, n=3, limit=1.5):
        """Wait for n more frames to be drawn (a change made just now in them); whether they were."""
        start, t0 = self.frames, time.time()
        while self.frames < start + n and time.time() - t0 < limit:
            time.sleep(0.02)
        return self.frames >= start + n

    def dark(self, on):
        """The video darkened to black (VLC's adjust filter: brightness and contrast 0), or back.
        VLC draws the subpicture after its filters, so a menu's highlight stays as it is, over
        nothing: no moving scenery behind it (WALL-E's sweeping lights). Takes effect at once."""
        if on == self.is_dark:
            return
        L = self.lib
        if on:
            L.libvlc_video_set_adjust_float(self.mp, ADJUST_CONTRAST, 0.0)
            L.libvlc_video_set_adjust_float(self.mp, ADJUST_BRIGHTNESS, 0.0)
        L.libvlc_video_set_adjust_int(self.mp, ADJUST_ENABLE, 1 if on else 0)
        self.settle()
        self.is_dark = on

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

    def menu(self):
        """VLC's DVD Menu (its menu title): the disc's menu, from a title playing."""
        self.lib.libvlc_media_player_set_title(self.mp, 0)

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
            time.sleep(gap)
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
STRUCTURE_VERSION = 1


def _structure_file(content_hash, home=None):
    from .core import data_home
    return os.path.join(home or data_home(), "menu_data", content_hash + ".json.gz")


def saved_structure(content_hash, home=None):
    """The disc's menu data as read before (save_structure), or None."""
    import gzip
    try:
        with gzip.open(_structure_file(content_hash, home), "rt", encoding="utf-8") as fh:
            kept = json.load(fh)
    except (OSError, ValueError, EOFError):
        return None
    return kept.get("structure") if kept.get("version") == STRUCTURE_VERSION else None


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
            time.sleep(0.25)
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
            time.sleep(wait)
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
                time.sleep(0.25)
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


def _lead_in(pages, st):
    """Seconds a page plays before the program whose buttons st is (its intro), as the disc says:
    keys pressed while its buttons are coming on can move the highlight half a round trip."""
    p = pages.get(st[:2]) or {}
    starts = p.get("program_cells") or [1]
    progs = p.get("program_buttons") or [p.get("buttons") or []]
    # up to the first program with buttons (Enchanted's main menu has them from its first: no wait)
    k = next((i for i in range(1, min(st[2], len(starts)) + 1) if i <= len(progs) and progs[i - 1]), st[2])
    first = starts[k - 1] if 0 < k <= len(starts) else 1
    return sum(c.get("seconds") or 0 for c in (p.get("cells") or [])[:first - 1])


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


# what a crawl for the feature and its extras passes by: buttons (and the pages behind them)
# for settings, commentaries, scenes, and the way back; and ads, by core.AD_RE ("Sneak Peeks",
# "Previews"). A film's own trailer ("theatrical trailer") is an extra, so it's kept.
SKIP_RE = re.compile(r"\b(set ?up|settings|options|languages?|audio|subtitles?|captions|commentar\w*|"
                     r"scenes?|scene selection|chapters?|main ?menu|register\w*|dvd-?rom|web ?links?|"
                     # pages of stills to flip through, nothing that plays (Muppets from Space's
                     # "Talent Files": 9 pages of cast bios)
                     r"talent files?|character files?|cast (and|&) crew|biograph\w*|filmograph\w*|production notes|"
                     r"photo galler\w*|galler(y|ies)|stills|dvd credits)\b", re.I)


# (a stray letter before it: OCR of the lettering's backdrop, "i play")
PLAY_ALL_RE = re.compile(r"\bplay\s*a(?:\w?l\w*)?\b", re.I)    # (OCR's Play All: "Play Aly", "PLAY aul",
                                                              # "play A"; not "Play Again", "Display")
PLAY_RE = re.compile(r"^\W*([a-z]\W+)?(play|start|resume|watch)( the)?( movie| film| feature)?\W*$", re.I)


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


def _says(text, phrases):
    """Whether text holds one of the runs of words, loosely."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    return any(all(_near(words[i + j], p[j]) for j in range(len(p)))
               for p in phrases for i in range(len(words) - len(p) + 1))


def _ad_text(text):
    from .core import AD_RE
    return bool(text) and bool(AD_RE.search(text) or MENU_AD_RE.search(text) or _says(text, AD_WORDS))


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


def _routes(pages, start, learned=None, known=None):
    """{state: [(state, button), ...]}: the buttons to press from the start menu to reach each
    state, by the disc's own links and those learned by pressing ({(state, button): state})."""
    routes, queue = {start: []}, [start]
    while queue:
        st = queue.pop(0)
        for b in _state_buttons(pages, st):
            nxt = _goes(pages, st, b, learned, known)
            if nxt and nxt not in routes and not b["auto"]:
                routes[nxt] = routes[st] + [(st, b["n"])]
                queue.append(nxt)
    return routes


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
    def __init__(self, vlc_dir, mrl, pages, out_dir, log):
        self.vlc_dir, self.mrl, self.pages, self.out_dir, self.log = vlc_dir, mrl, pages, out_dir, log
        self.v = None
        self.moving = None                      # where the current page's own video moved, lately
        self.learned = {}                       # {(state, button): state}: links found by pressing
        self.texts = {}                         # {state: {button: text}}, read (see words())
        self.reading = {}                       # {state: {button: OCR under way}}: the pages visited
        self.headings = {}                      # {state: the words outside the buttons}, read
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
            time.sleep(0.25)
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
            time.sleep(0.25)

    def to_menus(self):
        """VLC's DVD Menu, from a title playing: the disc's menu for it (where the remote's Menu,
        or a title's own ban on it, may do nothing: WALL-E's film)."""
        self.v.menu()
        time.sleep(4)

    def restart(self, start, where=None):
        """Back on the page the disc opens on, to start a route over: by VLC's DVD Menu, in
        seconds, while that's found to lead there; else the disc opened again (its warnings and
        all: WALL-E's take a minute and more). Only between pages: a fresh disc forgets the
        settings presses made (a commentary switch), which a button's answer can depend on."""
        if self.v is not None and self.menu_back is not False:
            try:
                self.v.menu()
                time.sleep(2)
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
            time.sleep(0.25)

    def at_start(self, start):
        """Back at the page the disc opens on, a fresh session: (state, selected button)."""
        self.await_menus()
        if self.menu_skip:                      # past the page's intro animation (crawl)
            self.v.press("popup")
            time.sleep(2)
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
            time.sleep(wait + 1)
            if st not in self.reading:
                self.log("  page %s of title set %s: its highlight is too small to see: taking the disc's "
                         "program's word for it" % (st[1], st[0]))
                self._read(st, self.v.frame(), None, None)
            return st, sel
        trips = [(k, back) for k, back in self.ROUND_TRIPS if sel in by and by[sel][k] in by and
                 by[sel][k] != sel and by[by[sel][k]][back] == sel and not by[by[sel][k]]["auto"]]
        if not trips:
            return self.find([st], timeout=timeout)
        time.sleep(wait + 1)
        t0, i, playing = time.time(), 0, None
        while time.time() - t0 < timeout:
            now = self.v.state()["title"]
            if now != 0:
                # a title before the page (Muppets from Space: its outtakes' intro, the first time):
                # the page's time starts once the menus are back; a film, though, isn't waited out
                playing = playing or time.time()
                if time.time() - playing > TITLE_WAIT or self.feature_length(now):
                    break
                time.sleep(1)
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
            time.sleep(0.3)
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
        time.sleep(2)
        self.v.press("popup")
        time.sleep(4)
        if self.v.state()["title"] != 0:        # (the press did nothing: VLC's DVD Menu instead)
            self.to_menus()

    def close(self):
        if self.v:
            self.v.close()
            self.v = None

    ROUND_TRIPS = (("down", "up"), ("right", "left"), ("up", "down"), ("left", "right"))

    def _lit(self, st, before, after, n, m):
        """Whether button n, not m, was the highlighted one in before (m in after): a highlight is
        drawn over a button, so the button it leaves gets darker and the one it goes to lighter."""
        from PIL import ImageStat
        by = {b["n"]: b for b in _state_buttons(self.pages, st)}
        mean = lambda img, n: ImageStat.Stat(img.convert("L").crop(tuple(by[n]["rect"]))).mean[0]
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
        as it is for what needs the picture: a page read (_read), a check as it is (_as_is)."""
        if not self.v.is_dark:
            t0 = time.time()
            self.v.dark(True)
            self.timed("darkening", t0)
        return check(*args)

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
        time.sleep(0.1)
        a2 = v.frame()
        v.press(key)
        time.sleep(0.3)
        b1 = v.frame()
        time.sleep(0.15)
        b2 = v.frame()
        v.press(back)
        time.sleep(0.3)
        c1 = v.frame()
        time.sleep(0.1)
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
        if self.v is not None and getattr(self, "dark_ok", None):
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
        time.sleep(0.25)
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
        t0, i = time.time(), 0
        while time.time() - t0 < timeout:
            key, back = self.ROUND_TRIPS[i % 4]
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
                time.sleep(0.3)
                continue
            for j in range(1, 4):               # more round trips, ruling pages out
                if len({layout(s) for s in fits}) == 1:
                    break
                k2, b2 = self.ROUND_TRIPS[(i + j) % 4]
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
            time.sleep(6)
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
                time.sleep(0.3)
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
            heading = self.pool.submit(page_text, bare, rects)
            self.timed("page frames", t0)
        else:                                   # (no subpicture to take away: as it is, the
            first = {b["n"]: self.pool.submit(button_words, f1, b["rect"], rects)   # lit button
                     if b["n"] != was or f2 is None             # from a frame it isn't lit in)
                     else self.pool.submit(_better_reading, f2, f1, b["rect"], rects) for b in btns}
            heading = self.pool.submit(page_text, f1, rects)
        self.reading[st] = first
        self.finished[st] = self.more.submit(self._finish, st, f1, f2, was, bare, dark, first, heading)
        self.log("  questioning page %s of title set %s (%d of %d)" % (st[1], st[0], len(self.reading),
                                                                         max(len(self.reading), self.expect)))

    def _finish(self, st, f1, f2, was, bare, dark, first, heading):
        """(In the background) A page's words, once its first readings are in: {"words": {button:
        text}, "heading", "overlay": whether its words are in the subpicture}. Most of its buttons
        read poorly without the subpicture: read as it is too, and its words are in the
        subpicture if most of those that read as words then are gone without it (WALL-E's Bonus
        Features: read a little worse, 175 to 169 by _score over its moving backdrop, isn't gone).
        In the subpicture: read over black too, the better kept, and the page's picture as it is.
        Then a plate's reading for each button still read poorly (_plate_or)."""
        btns = _state_buttons(self.pages, st)
        rects = [b["rect"] for b in btns]
        words = {n: f.result() for n, f in first.items()}
        head = heading.result()
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
                    heads = [page_text(f1, rects), head]
                    if dark is not None:        # (over black, no scenery behind them: the better)
                        alone = {b["n"]: self.pool.submit(button_words, dark, b["rect"], rects) for b in btns}
                        words = {n: max((t, alone[n].result()), key=_score) for n, t in lit.items()}
                        heads.append(page_text(dark, rects))
                    head = max(heads, key=_score)
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
        return {"words": words, "heading": head, "overlay": overlay}

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
            self.headings[st] = got["heading"]
        return self.texts.get(st, {})

    def heading(self, st):
        """A page's words outside its buttons (page_text), or "" for a page not read."""
        self.words(st)
        return self.headings.get(st, "")

    def select(self, st, sel, n):
        """Move the highlight from button sel to n on a state."""
        moves = _moves(_state_buttons(self.pages, st), sel, n)
        if moves is None:
            raise Lost("no way from button %d to %d on %s" % (sel, n, st))
        for key in moves:
            self.v.press(key)
            time.sleep(0.35)
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
            time.sleep(1)
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
            time.sleep(0.5)
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


def crawl(drive, vlc_dir, structure, out_dir, log=print, everything=False):
    """The menu buttons that matter: their words (OCR) and where they go, a page or what they
    play. In rounds: a tour through the pages there's a way to, reading their buttons; then each
    button whose result the disc doesn't state outright is pressed, from a fresh start, and what
    plays is watched. A button that stays in the menus has gone to a page the disc decides on
    as it runs ("bonus material" on Father of the Bride Part II): which one is seen, and the
    next round reads it and presses its buttons. Only the feature and the extras matter, so
    scene pages, and buttons for trailers, settings and the like (and the pages behind them),
    are passed by, unless everything. Writes crawl.json, crawl.txt and a picture of each page
    read into out_dir, and, kept as test data, the disc's menu data (keep_disc_data)."""
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
    c = _Crawler(vlc_dir, dvd_mrl(drive), pages, out_dir, log)
    c.states = states
    t0 = time.time()
    # The disc's own program, run without playing it (dvdvm): most buttons answered, and every
    # page a button leads to, before VLC presses anything; and where the disc starts, which VLC
    # is shown first (a disc that plays its film by itself: Menu pressed, as a viewer would).
    # What it can't follow, or answers differently by route, is left for VLC to press.
    began, engine = {}, {}
    try:
        from . import dvdvm
        began, engine, reached = dvdvm.explore(dvdvm.Engine(dvdvm.Disc(drive, structure)))
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
    if engine:
        if began.get("goes_to") and tuple(began["goes_to"]) != start:
            log("  the disc's program starts on %s, VLC on %s" % (began.get("goes_to"), start))
        # (a clip first or not: the same; so is the button highlighted on arrival, kept apart)
        same = lambda a: {k: v for k, v in a.items() if k not in ("before", "select", "intro", "after")}
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
            if any(same(x) != same(a) for x in answers) or "unsure" in a:
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
            else:
                known[(s, n)] = {"menus": True, "engine": True}
        answered = sum(1 for k in engine if (k[:3], k[3]) in known or (k[:3], k[3]) in c.learned)
        log("the disc's program answers %d of %d buttons" % (answered, len(engine)))

    def passed_by(s):
        """Why a state isn't worth a visit, or None."""
        if everything or s == start:
            return None
        if _scene_page(structure, pages, s[:2], known):
            return "scenes"
        if engine and not plays_beyond(s):
            return "nothing plays from there"
        # (Main Menu on the page the disc opens on leads into the menus, not back: a FastPlay chooser).
        # On a page that plays on, words still being read don't hold VLC up: it can time out while
        # they're read (The Sorcerer's Apprentice's FastPlay chooser takes FastPlay after 12 s), and
        # unread, they skip nothing. A page that waits for the viewer waits for them (Muppets from
        # Space's "Talent Files": its pages of cast bios, passed by)
        words = [c.first_words(x).get(n, "") if _waits_for_viewer(pages, x) else c.word_now(x, n)
                 for x, n in routes[s]]
        words = [w for (x, _), w in zip(routes[s], words) if not (x == start and _main_menu_text(w))]
        # (not pages of ads: reading them is quick, and their pictures name the ads, posters often:
        # Spider-Man 2's previews, Hitch among them. Pressing their buttons is another matter)
        return next(("via \"%s\"" % w for w in words if _skip_text(w) and not _ad_text(w)), None)

    def plays_beyond(s):
        """Whether anything plays from s, or from a page past it (not back the way it came): a
        button the disc's program says plays a title, or one nobody knows yet. Spider-Man's
        Character Files: pages of cast bios leading only to each other and back, nothing to name."""
        back = {x for x, _ in routes[s]} | {start}
        seen, todo = {s}, [s]
        while todo:
            x = todo.pop()
            for b in _state_buttons(pages, x):
                k = (x, b["n"])
                if "plays" in known.get(k, {}) or (k not in known and k not in c.learned):
                    return True
                nxt = _goes(pages, x, b, c.learned, known)
                if nxt and nxt not in seen and nxt not in back:
                    seen.add(nxt)
                    todo.append(nxt)
        return False

    def ready(s):
        """Whether the words of the buttons on the way to s have been read (no waiting on OCR)."""
        return all(c.read(x) for x, n in routes[s] if n in c.reading.get(x, {}))

    def tour(st, sel):
        """One session, page to page, reading each page there's a way to (and that matters).
        Pages whose way there has been read go first: VLC moves on while OCR catches up."""
        goal = None
        try:
            while True:
                # the next pages: their way there visited (its words may say "Sneak Peeks")
                cands = [s for s in routes if s not in c.reading and tries.get(s, 0) < 2
                         and all(x in c.reading for x, _ in routes[s])]
                todo = [s for s in cands if ready(s) and not passed_by(s)] or \
                    [s for s in cands if not passed_by(s)]          # (none ready: wait for the words)
                c.expect = len(c.reading) + len(todo)
                if not todo:
                    return
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
    while not stop:
        routes = _routes(pages, start, c.learned, known)
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
        # the presses: only what the disc doesn't say, on pages read, and not passed by
        for s in routes:
            for b in _state_buttons(pages, s):
                if not b["auto"] and not _goes(pages, s, b, c.learned, known) and (s, b["n"]) not in known:
                    k = _known(structure, pages, s, b)
                    if k:
                        known[(s, b["n"])] = k
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
                elif everything or (s in c.reading and not _skip_text(text)):
                    tasks.append((s, b["n"]))
        if not tasks:
            break
        log("%d buttons to press (%d answered by the disc itself)" % (len(tasks), len(known)))
        for i, (s, n) in enumerate(tasks, 1):
            text = c.words(s).get(n, "")
            if played and not everything and _play_text(text):     # every page's Play Movie: the same film
                known[(s, n)] = {"plays": {"title": played, "chapter": 1}, "assumed": "play"}
                log("  [%d/%d] %s button %d: Play again: title %d (not pressed)" % (i, len(tasks), s, n, played))
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
    result = []
    for s in routes:
        page = {"domain": s[0], "page": s[1], "program": s[2],
                "image": "page_%s_%d_%d.png" % s if s in c.reading else None, "passed_by": passed_by(s),
                "heading": c.heading(s),
                "route": [{"page": x[1], "program": x[2], "button": n} for x, n in routes[s]], "buttons": []}
        for b in _state_buttons(pages, s):
            e = {"n": b["n"], "rect": b["rect"], "cmd": b["cmd"], "text": c.words(s).get(b["n"], "")}
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
        result.append(page)
    c.pool.shutdown(wait=False)
    c.more.shutdown(wait=False)
    out = {"drive": drive, "titles": titles, "start": list(start), "minutes": round((time.time() - t0) / 60, 1),
           "pages": result, "chapters": {t["title"]: t["chapters"] for t in structure.get("titles", [])}}
    out["found"] = menu_titles(out)
    out["found"]["pictures"] = title_pictures(out, out_dir)
    with open(os.path.join(out_dir, "crawl.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    with open(os.path.join(out_dir, "crawl.txt"), "w", encoding="utf-8") as fh:
        fh.write(crawl_text(out) + "\n")
    return out


# --------------------------------------------------------------------------
# 5. What the menus say about the titles
# --------------------------------------------------------------------------

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
    # a list's number before the name ("4. J. Jonah Jameson", "2) Leaving..."); not a number
    # that's part of it ("3 Blind Mice", "Stuart Little 2")
    text = re.sub(r"^\s*\d{1,2}[.)]\s+", "", text)
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
HEADING_KINDS = (("Music", r"\bmusic\s*videos?\b"), ("DeletedScene", r"\bdeleted\s*scenes?\b"),
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
    chapters = {int(t): n for t, n in (out.get("chapters") or {}).items()}
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
        kind = heading_kind(p.get("heading"))
        for w in reversed(way):
            if kind or HUB_RE.search(w) or MAIN_MENU_RE.search(w):
                break
            kind = heading_kind(w)
        singles = {b["plays"]["title"] for b in p["buttons"] if b.get("plays")}
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
                promo = _ad_text(via) or _ad_text(b["text"]) or reel
                plays.append(dict(b["plays"], text=b["text"], promo=promo, kind=kind, via=via, play_all=whole))
                # an ad button's run of titles: ads, all of them (The Sorcerer's Apprentice's Sneak
                # Peeks: 66, then 70, 74, 78, 84, 88, 92, 94). Not another button's: FastPlay's sneak
                # peeks and film are what their own buttons say; a Play All's, its own titles
                if promo or whole:
                    for t in run:
                        plays.append({"title": t, "chapter": 1, "seconds": None, "text": b["text"], "promo": promo,
                                      "kind": kind, "via": via, "after": True, "play_all": whole})
                for o in b["plays"].get("others") or []:
                    plays.append(dict(o, text=b["text"], promo=promo, kind=kind, via=via, other=True))
    if not plays:
        return {"feature": None, "extras": [], "promos": []}
    icons = _icon_digits([x["text"] for x in plays if not x.get("after") and not x.get("other")])
    by = {}
    for x in plays:
        by.setdefault(x["title"], []).append(x)
    feature = max(by, key=lambda t: (len({x["chapter"] for x in by[t]}), by[t][0]["seconds"] or 0))
    extras, promos, promo_names, play_alls = [], [], {}, []
    for t, xs in sorted(by.items()):
        if t == feature:
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
        name = nice_name(_unicon(LISTED_RE.sub("", text), icons)) if _wordy(text) > 0 else ""
        kind = next((x["kind"] for x in xs if x.get("kind")), None)
        other = all(x.get("other") for x in xs)     # (only by another route: a commentary version?)
        if other and name:
            name += " (another version)"
        extras.append(dict({"title": t, "name": name, "seconds": xs[0]["seconds"], "listed": listed, "kind": kind},
                           **({"other": True} if other else {})))
    # the feature's own button, when it says more than Play ("Signature Edition": another cut of
    # the film may be on the disc, and is named the same way)
    # (from its start: a scene button's words name the scene)
    ftext = max([x["text"] for x in by[feature] if x.get("chapter") in (1, None)] or [""], key=_wordy)
    feature_name = nice_name(ftext) if _wordy(ftext) > 0 and not _play_text(ftext) and \
        not PLAY_ALL_RE.search(ftext) else ""
    return {"feature": feature, "feature_name": feature_name, "extras": extras, "promos": promos,
            "promo_names": promo_names, "play_alls": play_alls}


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
    text = max((x["text"] for x in xs), key=_wordy)
    # its own words, unless they're only the ad words ("PREVIEWS"): an ad can say what it is
    # ("Spider-Man: Friend or Foe Video Game Preview", Spider-Man 3)
    rest = re.sub(r"\b(previews?|sneak|peeks?|coming|soon|trailers?|ads?|advertisements?)\b", " ", text, flags=re.I)
    own = nice_name(text) if _wordy(text) > 0 and _wordy(rest) > 0 else ""
    return "%s - %s" % (lead, own) if own else lead


def readable(name, is_word):
    """Whether OCR read a name rather than the lettering's backdrop: most of its words (two in
    three) are words ("Beye olticak Peeks" isn't), one of them three letters or more ("2 Oh"
    is a scrap). Numbers count as read."""
    words = re.findall(r"[^\W_]+", name)
    real = [w for w in words if not w.isdigit() and w.lower() not in SMALL_WORDS and is_word(w)]
    good = len(real) + sum(1 for w in words if w.isdigit() or w.lower() in SMALL_WORDS)
    # and its longer words half real: scraps ("Rei Mec ai ay") don't carry a name on their own
    # ("Rei Mec ai ay cummantie scien", Enchanted's OCR of an animated button)
    longer = [w for w in words if len(w) >= 4 and not w.isdigit()]
    return any(len(w) >= 3 for w in real) and 3 * good >= 2 * len(words) and \
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
    toks = list(re.finditer(r"[A-Za-z][A-Za-z'-]*", text))
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
        if len(w) >= 4 and unread(w):
            near = {x.lower(): x for x in words if _close(w, x)}
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


def correct_names(found, people, is_word, title_fix=None):
    """found (menu_titles()) with its names put right where they can be: an extra's against the
    film's people (credit_fix); an ad's against the films it may advertise (title_fix: a known
    title, closely, "Ghost Riderm." for Ghost Rider). What was read is kept ("read"), to show."""
    if not found:
        return found
    extras = []
    for e in found.get("extras") or []:
        new = credit_fix(e["name"], people, is_word) if e.get("name") and people else e.get("name")
        extras.append(dict(e, name=new, read=e["name"]) if new != e.get("name") else e)
    promo_names, promo_read = {}, dict(found.get("promo_read") or {})
    for t, n in (found.get("promo_names") or {}).items():
        lead, _, own = n.partition(" - ")
        fixed = title_fix(own) if own and title_fix else own
        promo_names[t] = "%s - %s" % (lead, fixed) if fixed else n
        if fixed != own:
            promo_read[t] = own
    return dict(found, extras=extras, promo_names=promo_names, promo_read=promo_read)


def plain_names(found, is_word):
    """found (menu_titles()) with the names that aren't readable dropped: the extra keeps a plain
    name. As found when there's no dictionary (is_word None)."""
    if not found or not is_word:
        return found
    known = is_word
    is_word = lambda w: w.lower() in EXTRAS_VOCAB or known(w)      # (and extras' own words)
    extras = [dict(e, name=unmarked(e["name"], is_word)) for e in found.get("extras") or []]
    extras = [dict(e, name=e["name"] if e["name"] and readable(e["name"], is_word) else "") for e in extras]
    # an ad's own words, too: unreadable, it's just "Previews" (the ad words, always readable)
    promo_names = {t: unmarked(n, is_word) for t, n in (found.get("promo_names") or {}).items()}
    promo_names = {t: n if " - " not in n or readable(n.split(" - ", 1)[1], is_word) else n.split(" - ", 1)[0]
                   for t, n in promo_names.items()}
    fname = unmarked(found.get("feature_name") or "", is_word)
    return dict(found, extras=extras, promo_names=promo_names,
                feature_name=fname if fname and readable(fname, is_word) else "")


def title_pictures(out, out_dir):
    """For each title a menu button plays, the button to show for it: {title: {"image": the
    page's picture (a path), "rect": the button's [x0, y0, x1, y1] (the disc's 720x480), "text":
    its words as read}}. Only pages read have a picture; of a title's buttons, the one read
    best (the film: its Play button sooner than a scene's; nothing read, the first)."""
    best = {}
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
                        best[t2] = ((0, True, False, -b["n"]), {"image": os.path.join(out_dir, p["image"]),
                                                                "rect": list(b["rect"]), "text": b.get("text") or ""})
            # (a scrap read off a logo counts for nothing: then the first button, Play before FastPlay)
            # (and one that says it's an ad over one saying nothing: The Sorcerer's Apprentice's title
            # 66, FastPlay's first sneak peek too, but "Peeks" names it, "Ploy" doesn't)
            rank = (score if score >= 6 else 0, _ad_text(b.get("text")), b["plays"].get("chapter") == 1, -b["n"])
            if t not in best or rank > best[t][0]:
                best[t] = (rank, {"image": os.path.join(out_dir, p["image"]), "rect": list(b["rect"]),
                                  "text": b.get("text") or ""})
    return {t: v for t, (_, v) in sorted(best.items())}


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
    named = {"Title %d" % e["title"]: e["name"] for e in found.get("extras") or []}
    kinds = {"Title %d" % e["title"]: e.get("kind") for e in found.get("extras") or []}
    others = {"Title %d" % e["title"] for e in found.get("extras") or [] if e.get("other")}
    # (a name put right: as read, to show: correct_names)
    read_as = {"Title %d" % e["title"]: e["read"] for e in found.get("extras") or [] if e.get("read")}
    read_as.update({"Title %s" % t: r for t, r in (found.get("promo_read") or {}).items()})
    said = lambda tid, note: note + (" (read as \"%s\")" % read_as[tid] if tid in read_as else "")
    feature = "Title %d" % found["feature"] if found.get("feature") else None
    promos = {"Title %d" % n for n in found.get("promos") or []}
    promo_names = {"Title %d" % int(n): name for n, name in (found.get("promo_names") or {}).items()}
    played = lambda tid: tid in named or tid == feature or tid in promos
    # the feature named by its button only beside another cut ("Signature Edition")
    cuts = any(t["id"] in named and t.get("role") in ("feature", "version") for t in titles)
    # a page's Play All: the scan says what it plays (left unlabelled, never an ad)
    play_alls = {"Title %d" % n for n in found.get("play_alls") or []}
    # copies the menus play, of titles they don't: labelled in their twin's place
    twins = {}
    for t in titles:
        m = re.match(r"same video as (.+?)\s*$", t.get("note") or "") if t.get("role") == "duplicate" else None
        if m and played(t["id"]) and not played(m.group(1)):
            twins[t["id"]] = m.group(1)
    out = {}
    for t in titles:
        tid = t["id"]
        offered = played(tid)
        if tid in keep or tid in play_alls or tid in twins.values() or (t.get("role") in COPY_ROLES and tid not in twins) or \
                (t.get("role") in HIDDEN_ROLES and not offered):
            continue
        if tid == feature:
            out[tid] = {"type": "MainMovie", "title": (cuts and found.get("feature_name")) or None,
                        "note": "the menus: the feature"}
        elif tid in named and t.get("role") in ("feature", "version"):
            # another cut of the film, offered beside it (The Iron Giant's "Signature Edition" and
            # "Original Edition"): a main movie too, not an extra
            out[tid] = {"type": "MainMovie", "title": named[tid] or None, "note": "the menus: another cut of the film"}
        elif tid in named:
            # its page's heading says ("MUSIC VIDEOS"); or the film's own trailer: TheDiscDB's
            # Trailer (ads were promos already, by AD_RE)
            kind = kinds.get(tid) or ("Trailer" if re.search(r"\btrailer\b", named[tid] or "", re.I) else "Extra")
            out[tid] = {"type": kind, "title": named[tid] or None,
                        "note": said(tid, "the menus: another version of an extra (a setting picks it): worth a look"
                                     if tid in others else "the menus: an extra")}
        elif tid in promos:
            out[tid] = {"type": "Promo", "title": promo_names.get(tid), "note": said(tid, "the menus: an ad")}
        else:
            out[tid] = {"type": "Promo", "title": None, "note": "the menus: nothing plays it"}
    return out


def found_text(found):
    """menu_titles(), readable."""
    lines = []
    if found["feature"]:
        lines.append("Feature:  title %d" % found["feature"])
    for e in found["extras"]:
        check = ""
        if e["listed"]:
            ok = abs(e["listed"] - (e["seconds"] or 0)) <= max(10, 0.03 * e["listed"])
            check = "  (menu says %s, plays %s%s)" % (_clock(e["listed"]), _clock(e["seconds"]), "" if ok else ": doesn't fit")
        kind = "  [%s]" % e["kind"] if e.get("kind") else ""
        lines.append("Extra:    title %d  %s%s%s" % (e["title"], e["name"] or "(no name read)", kind, check))
    if found["promos"]:
        lines.append("Trailers: title%s %s" % ("s" if len(found["promos"]) > 1 else "", ", ".join(map(str, found["promos"]))))
    if found.get("play_alls"):
        lines.append("Play all: title%s %s" % ("s" if len(found["play_alls"]) > 1 else "",
                                               ", ".join(map(str, found["play_alls"]))))
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
                does = "plays title %d (%s) from chapter %s%s" % (
                    pl["title"], _clock(pl["seconds"]), pl["chapter"],
                    ", as the first Play button did (not pressed)" if pl.get("assumed") == "play"
                    else ", the feature the scenes point to (not pressed)" if pl.get("assumed")
                    else ", the disc's program" if pl.get("engine")
                    else "" if pl.get("known") else ", pressed")
                if b.get("before"):
                    does += ", after title%s %s" % ("s" if len(b["before"]) > 1 else "", ", ".join(map(str, b["before"])))
                for o in pl.get("others") or []:
                    does += "; or title %d (%s), by another route" % (o["title"], _clock(o.get("seconds")))
                if pl.get("after"):
                    does += ", then title%s %s" % ("s" if len(pl["after"]) > 1 else "", ", ".join(map(str, pl["after"])))
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
    result = crawl(root, vlc, s, out, everything=a.all)
    result["found"] = plain_names(result["found"], _dictionary())
    print()
    print(crawl_text(result))
    print("crawl written to %s (%.1f minutes)" % (os.path.join(out, "crawl.json"), result["minutes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
