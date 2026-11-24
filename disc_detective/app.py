#!/usr/bin/env python3
"""
Disc Detective - the app. Two big buttons:

  Scan Disc  click: investigate the disc in the current drive (fingerprint,
             TheDiscDB, disc structure, label, IMDb) and show what it is.
             ▾ menu: another drive, a folder (disc backup), Settings, About.
  Rip        click: rip the feature or the episodes with makemkvcon, each file
             named for Plex. ▾ menu: rip folder, include extras, choose
             titles, open rip folder. While ripping it becomes Cancel.

  Default use: Scan Disc, check the answer, Rip.

Needs Python 3 with Tkinter (included in the python.org Windows installer).
MakeMKV is only needed for ripping.
"""
import collections
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import core as di
from . import i18n
from .i18n import N_, ui as _, ui_n
from .kinds import Role, Type, join_title, title_parts
from . import makemkv
from . import tools
from .makemkv import MAKEMKV_SWITCHES, makemkv_source
from . import theme as th

APP = "Disc Detective"
# the main window's title bar (Windows draws it in its own font: the subtitle can't be smaller)
WINDOW_TITLE = N_("Disc Detective — Optical Forensics · 221B")
SETTINGS = os.path.join(di.data_home(), "settings.json")
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
RIP_ROLES = (Role.FEATURE, Role.EPISODE)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # keep makemkvcon from flashing a console


# --------------------------------------------------------------------------
# drives and settings
# --------------------------------------------------------------------------

def optical_drives():
    """Optical drives on Windows; mounted discs on macOS / Linux."""
    if os.name == "nt":
        import ctypes
        import string
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        out = []
        for i, letter in enumerate(string.ascii_uppercase):
            root = "%s:\\" % letter
            if mask & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(root) == 5:   # DRIVE_CDROM
                out.append(root)
        return out
    roots = []
    for base in ("/Volumes", "/media", "/run/media", "/mnt"):
        for dirpath, dirs, _x in os.walk(base) if os.path.isdir(base) else []:
            if any(d.upper() in ("VIDEO_TS", "BDMV") for d in dirs):
                roots.append(dirpath)
                dirs[:] = []
            elif dirpath.count(os.sep) - base.count(os.sep) >= 2:
                dirs[:] = []
    return sorted(set(roots))


def drive_identity(drive):
    """An optical drive's make and model, asked of the drive itself ("PIONEER BD-RW BDR-XD07U"):
    the storage device query, which needs no disc and doesn't read one, so Windows' device
    inventory (WMI) needn't answer: it can stall after a drive has (Win32_CDROMDrive waiting
    minutes). None if the drive doesn't say. Windows only."""
    import ctypes
    import ctypes.wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = w.HANDLE
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD,
                                  ctypes.POINTER(w.DWORD), w.LPVOID]
    k.CloseHandle.argtypes = [w.HANDLE]
    # (no access asked: the device's own description only; shared, opened as it is)
    h = k.CreateFileW(r"\\.\%s" % drive.rstrip("\\/"), 0, 3, None, 3, 0, None)
    if h is None or h == w.HANDLE(-1).value:
        return None
    try:
        query = (ctypes.c_ulong * 3)(0, 0, 0)        # StorageDeviceProperty, PropertyStandardQuery
        buf = ctypes.create_string_buffer(1024)
        got = w.DWORD()
        if not k.DeviceIoControl(h, 0x2D1400, query, ctypes.sizeof(query), buf, 1024, ctypes.byref(got), None):
            return None                              # (IOCTL_STORAGE_QUERY_PROPERTY)
        raw = buf.raw

        def text(at):                                # (a STORAGE_DEVICE_DESCRIPTOR's strings, by offset)
            off = int.from_bytes(raw[at:at + 4], "little")
            return raw[off:raw.index(b"\0", off)].decode("ascii", "replace").strip() if 0 < off < len(raw) else ""
        vendor, product = text(12), text(16)
        if vendor.upper() in ("ATA", "SATA", "SCSI", "USB"):    # (the connection, not the make)
            vendor = ""
        return " ".join((vendor + " " + product).split()) or None
    finally:
        k.CloseHandle(h)


DRIVE_ASK_WAIT = 3.0     # seconds a drive has to say what it is


def drive_models(drives=None):
    """{"D:": "HL-DT-ST BD-RE WH16NS40", ...}: each optical drive's make and model. Windows: asked
    of each drive (drive_identity, DRIVE_ASK_WAIT each), Windows' device inventory only for any
    that didn't say."""
    out = {}
    if os.name == "nt":
        for d in optical_drives() if drives is None else drives:
            box = {}
            t = threading.Thread(target=lambda d=d: box.update(name=drive_identity(d)), daemon=True)
            t.start()
            t.join(DRIVE_ASK_WAIT)
            if box.get("name"):
                out[d.rstrip("\\/").upper()] = box["name"]
        if out and len(out) == len(optical_drives() if drives is None else drives):
            return out
    try:
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_CDROMDrive | Select-Object Drive, Caption | ConvertTo-Json"],
                               capture_output=True, text=True, errors="replace", timeout=20, creationflags=NO_WINDOW)
            rows = json.loads(r.stdout or "[]")
            for row in rows if isinstance(rows, list) else [rows]:
                if row.get("Drive") and row.get("Caption"):
                    out.setdefault(row["Drive"].rstrip("\\/").upper(), " ".join(row["Caption"].split()))
        else:
            for dev in sorted(os.listdir("/sys/block")) if os.path.isdir("/sys/block") else []:
                if dev.startswith("sr"):
                    bits = []
                    for f in ("vendor", "model"):
                        try:
                            with open("/sys/block/%s/device/%s" % (dev, f)) as fh:
                                bits.append(fh.read().strip())
                        except OSError:
                            pass
                    if bits:
                        out["/DEV/" + dev.upper()] = " ".join(bits)
    except Exception:
        pass
    return out


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def relaunch_command():
    """How to start the app again, as it was started: a packaged build's own program; else this Python,
    the app imported from its own package's folder (a .pyw, python -m, or an installed package alike)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-c", "import sys; sys.path.insert(0, %r); from disc_detective.app import main; main()" % pkg]


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# MakeMKV
# --------------------------------------------------------------------------

EXTRA_FOLDERS = {Type.FEATURETTE: "Featurettes", Type.DELETED_SCENE: "Deleted Scenes", Type.TRAILER: "Trailers",
                 Type.INTERVIEW: "Interviews", "BehindTheScenes": "Behind The Scenes", Type.SHORT: "Shorts",
                 Type.SCENE: "Scenes"}
# Plex's ending for an extra saved beside its episode ("-other" for the rest)
EXTRA_SUFFIXES = {Type.FEATURETTE: "featurette", Type.DELETED_SCENE: "deleted", Type.TRAILER: "trailer", Type.INTERVIEW: "interview",
                  "BehindTheScenes": "behindthescenes", Type.SHORT: "short", Type.SCENE: "scene"}


def clean(s):
    return re.sub(r'[<>"/\\|?*]', "", re.sub(r"\s*:\s*", " - ", s or "")).strip()


def grown(points, by):
    """A convex outline pushed out by `by` pixels from its middle, every corner along its own direction: clear of
    the text it goes round, as the upright box's gap is."""
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    out = []
    for x, y in points:
        d = max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5, 1e-6)
        out.append((x + (x - cx) / d * by, y + (y - cy) / d * by))
    return out


def anchored_titles(rep, number=False):
    """The disc's main movies and episodes, for the menus' search and pictures (the user's, 2026-11-19): {title:
    "main" or "episode"}, each one's button nearest the first menu read and shown. By its label where it has one
    (yours, TheDiscDB's: MainMovie, Episode), else by the scan (the film and its other cuts; a show's episodes).
    number: a DVD crawl's keys, its title numbers ("Title 3": 3); else the titles' own ids ("00030.mpls")."""
    labels = rep.get("labels") or {}
    out = {}
    for t in rep.get("titles") or []:
        kind = (labels.get(t["id"]) or {}).get("type")
        if kind:
            got = "main" if kind == Type.MAIN_MOVIE else "episode" if kind == Type.EPISODE else None
        else:
            got = "main" if t.get("role") in (Role.FEATURE, Role.VERSION) else "episode" if t.get("role") == Role.EPISODE else None
        if not got:
            continue
        key = t["id"]
        if number:
            m = re.match(r"Title (\d+)$", str(key))
            if not m:
                continue
            key = int(m.group(1))
        out[key] = got
    return out


def described(name, description):
    """An extra's file name with the words its menu prints under it, in brackets, as Plex shows it ("Sc. 22 (Big
    Mike talks to Herd)": the user's, 2026-11-17, tried in Plex): no full stop at its end; none, the name alone."""
    said = clean(description).rstrip(" .")
    return "%s (%s)" % (name, said) if name and said else name


def short_reads(short):
    """What the lab read short, said plainly: one title by its name and seconds, else how many."""
    if len(short) == 1:
        return ui_n("{title} reads {n} second short", "{title} reads {n} seconds short", short[0]["seconds"],
                    title=short[0]["id"])
    return ui_n("{n} title reads short", "{n} titles read short", len(short))


def lab_short_text(short):
    """The rip card's word on titles the lab read short ([] : none)."""
    return _("Lab: {what}: check the disc", what=short_reads(short))


def names_and(names):
    """A few names as one: "VLC", "VLC and MakeMKV", "VLC, MakeMKV and FFmpeg"."""
    names = list(names)
    if len(names) < 2:
        return "".join(names)
    return _("{names} and {last}", names=_(", ").join(names[:-1]), last=names[-1])


def episode_dest(base, show, season, episode, title):
    """Where a rip saves an episode, or several in one title: Plex's "s01e01-e02". (Two names joined
    with a slash, as a menu has them, would make a folder of the first.)"""
    code = di._ep_code(int(season), di.episode_numbers(episode)).lower()
    return "%s/Season %02d/%s - %s - %s.mkv" % (base, int(season), show, code,
                                                clean(re.sub(r"\s*/\s*", " & ", title or "")))


def menu_data(root, content_hash, vlc, log, t0=None, oldest=None):
    """A DVD's menu data (menus.read_structure): as read before (kept by content hash), else read
    off the disc (VLC unlocking the drive first, when it's there) and kept for next time. oldest:
    the earliest kept version that will do (saved_structure)."""
    from . import menus
    t0 = time.time() if t0 is None else t0
    structure = menus.saved_structure(content_hash, oldest=oldest)
    if structure is not None:        # (read on an earlier scan or stakeout: the same disc, the same data)
        log(i18n.log("the disc's menu data, as read before: {count} menu pages (set up in {seconds:.0f} s)", count=len(menus._pages(structure)), seconds=time.time() - t0))
        return structure
    structure = menus.read_structure(root, vlc, log=log)
    log(i18n.log("the disc's menu data read: {count} menu pages (set up in {seconds:.0f} s)", count=len(menus._pages(structure)), seconds=time.time() - t0))
    try:
        menus.save_structure(content_hash, structure)
    except OSError as e:
        log(i18n.log("couldn't keep the disc's menu data for next time ({error})", error=e))
    return structure


def menu_title_id(n):
    """A title as the menus' answer has it, as a scan's id: a DVD's number, a Blu-ray's id as it is."""
    return n if isinstance(n, str) and not n.isdigit() else "Title %d" % int(n)


def menu_title_number(tid):
    """A scan's id as the menus' answer has it: a DVD's number, a Blu-ray's id as it is."""
    m = re.fullmatch(r"Title (\d+)", tid)
    return int(m.group(1)) if m else tid


def menu_played(found):
    """The titles a disc's menu buttons play (menu_titles()), as title ids."""
    found = found or {}
    nums = {found.get("feature")} | {f["title"] for f in found.get("features") or []} | \
        {e["title"] for e in found.get("extras") or []} | set(found.get("promos") or []) | \
        set(found.get("play_alls") or []) | {e["title"] for e in found.get("episodes") or []}
    return {menu_title_id(n) for n in nums if n}


def menu_unread(found):
    """Of those, the extras whose buttons' words weren't read (a page passed by, its title known by
    another way, or its copy's), as title ids."""
    return {menu_title_id(e["title"]) for e in (found or {}).get("extras") or [] if not e.get("name")}


PLAY_ALL_FIT = 0.05     # a Play All the scan didn't see is as long as its page's titles, to this


def menu_play_alls(titles, found):
    """The menus' Play Alls (menu_titles' play_all_parts: the titles its page's other buttons
    play), what the scan says of them put right where they agree: a play-all whose page plays
    every title it holds and more (Wallace & Gromit's Shorts: title 1 holds 2 and 3, the disc going
    on to 7's twin after it), or a title the scan took for an extra as long as its page's titles
    together (a play-all whose pieces don't line up with its parts'). Changes titles' roles and
    notes; anything else stays as the scan has it."""
    by = {t["id"]: t for t in titles}
    for n, parts in ((found or {}).get("play_all_parts") or {}).items():
        t, ids = by.get(menu_title_id(n)), [menu_title_id(p) for p in parts]
        if not t or not all(i in by for i in ids):
            continue
        held = re.findall(r"Title \d+", t.get("note") or "") if t.get("role") == Role.PLAY_ALL else None
        total = sum(by[i].get("duration", 0) for i in ids)
        if held is not None and set(held) <= set(ids) and len(ids) > len(held) or \
                t.get("role") in (Role.EXTRA, None) and abs(total - t.get("duration", 0)) <= PLAY_ALL_FIT * total:
            t["role"], t["note"] = Role.PLAY_ALL, "plays " + ", ".join(ids)


NOT_IN_MENUS = "not in the disc's menus"       # (menus.NOT_IN_MENUS, without importing it at the start)


def naming_film(rep):
    """The disc's film, or its show, as the scan has it (its best guess): what the namer tells its own
    trailer from another's by (menus.name_extras), on every disc alike. None on a disc of several films
    (each one's trailer is the disc's own), or with no guess."""
    if rep.get("kind") == "collection":
        return None
    return di.parse_truth((rep.get("best_guess") or "").split(" | ")[0])["title"] or None


def detected_for(rep, found):
    """What the menus found (menus.menu_titles()) as labels for this scan's titles: the titles you
    or TheDiscDB named are left be."""
    from . import menus
    keep = set(rep.get("labels") or {}) | discdb_named(rep)
    di.prefer_played(rep, menu_played(found), menu_unread(found), keep,
                     feature=menu_title_id(found["feature"]) if found.get("feature") else None)
    film = naming_film(rep)
    if found.get("tv"):
        return menus.detected_tv_labels(found, rep.get("titles") or [], keep=keep, film=film)
    menu_play_alls(rep.get("titles") or [], found)
    if rep.get("extras_disc") and found.get("feature"):
        # none of the film on it: its longest title is one more extra, named as its button has it
        # (The Fellowship of the Ring's special features: A Passage to Middle-earth, 41 minutes)
        longest = {"title": found["feature"], "name": found.get("feature_name") or None, "seconds": None, "kind": None}
        found = dict(found, feature=None, feature_name=None, extras=[longest] + list(found.get("extras") or []))
    # (a disc of short films: the ones its answer has, each a film, as a film's own menu makes it)
    cands = rep.get("candidates") or []
    shorts = short_films(rep, found)
    if shorts and di.adopt_guess(cands):
        films = {it["title_id"] for it in cands[0]["items"]}
        pairs = di.films_by_headings(cands[0]["items"], shorts) or {}
        films = set(pairs.values()) or films
        found = dict(found, features=[{"title": menu_title_number(t), "name": shorts.get(t) or ""}
                                      for t in sorted(films, key=di._natural)])
    return menus.detected_labels(found, rep.get("titles") or [], keep=keep, film=film)


def short_films(rep, found):
    """A disc of short films (Wallace & Gromit's: each played from a Shorts page, no menu of its
    own), its answer a collection of them by length: the buttons' names of the titles as long as
    one of its films ({title id: name}), to pair them as a film's menu heading does. Release order
    put The Wrong Trousers on title 3, which the menu calls A Close Shave. {} for a disc whose
    films have menus of their own (films_by_menus' headings), or no such answer."""
    cands = rep.get("candidates") or []
    top = cands[0] if cands and di.adopt_guess(cands) else None
    if not top or top.get("kind") != "collection" or found.get("features") or len(top.get("items") or []) < 2:
        return {}
    runtimes = [it.get("runtime") for it in top["items"] if it.get("runtime")]
    minutes = {t["id"]: t.get("duration", 0) / 60 for t in rep.get("titles") or []}
    return {menu_title_id(e["title"]): e["name"] for e in found.get("extras") or [] if e.get("name") and
            any(abs(minutes.get(menu_title_id(e["title"]), 0) - r) <= di.TITLE_FILM_FIT for r in runtimes)}


def films_by_menus(rep, found):
    """A disc of several films, as its films' own menus say, as the disc's answer: their headings
    pairing films and titles differently from now (the lengths had it), or, for a disc you've
    answered for, whose film each extra is (the film's menu it's on: it rips with that film).
    None when that's nothing new."""
    cands = rep.get("candidates") or []
    top = cands[0] if cands and di.adopt_guess(cands) else None
    if not top or top.get("kind") != "collection" or not (found.get("features") or short_films(rep, found)):
        return None
    now = {it["imdb_id"]: it["title_id"] for it in top["items"]}
    pairs = di.films_by_headings(top["items"], {menu_title_id(f["title"]): f.get("name") for f in found["features"]}
                                 if found.get("features") else short_films(rep, found))
    film_of = {t: i for i, t in (pairs or now).items()}
    extras = {menu_title_id(e["title"]): film_of[menu_title_id(e["film"])] for e in found.get("extras") or []
              if e.get("film") and menu_title_id(e["film"]) in film_of}
    had = top.get("extras") or {}
    moved = bool(pairs) and pairs != now
    if not moved and not (rep.get("personal_hit") and any(had.get(t) != i for t, i in extras.items())):
        return None
    films = sorted(((t, i) for i, t in (pairs or now).items()), key=lambda p: di._natural(p[0]))
    name = di.strip_tags(top["name"])
    named = {it["imdb_id"]: it["name"] for it in top["items"]}
    if name == " & ".join(it["name"] for it in sorted(top["items"], key=lambda it: di._natural(it["title_id"]))):
        name = " & ".join(named[i] for _x, i in films)      # (named after its films: in their new order)
    return di.truth_for_collection(name, films, dict(had, **extras))


def collection_extras(rep, cand):
    """Whose film each extra of a disc of several films is, {title id: imdb id}: your answer's,
    and this session's Detect titles' (the film's menu it's on)."""
    by_title = {it["title_id"]: it["imdb_id"] for it in cand.get("items") or []}
    found = {menu_title_id(e["title"]): by_title[menu_title_id(e["film"])]
             for e in (rep.get("menu_found") or {}).get("extras") or []
             if e.get("film") and menu_title_id(e["film"]) in by_title}
    return dict(found, **(cand.get("extras") or {}))


def all_labels(rep):
    """Your labels, over what Detect titles read from the disc's menus (kept for this scan only)."""
    return dict(rep.get("detected") or {}, **(rep.get("labels") or {}))


def discdb_items(hit):
    """TheDiscDB's word on each title of a disc (its index hit): {source, lower case: item}."""
    return {str(t.get("source") or "").lower(): t.get("item") or {} for t in (hit or {}).get("titles") or []}


def discdb_item(items, t):
    """TheDiscDB's item for one of our titles: by its id ("Title 3", "03", "3") or its one clip."""
    tid = t["id"]
    keys = [tid.lower()] + ([t["clips"][0].lower() + ".m2ts"] if len(t.get("clips") or []) == 1 else [])
    m = re.fullmatch(r"Title (\d+)", tid)
    if m:
        keys += ["%02d" % int(m.group(1)), m.group(1)]
    return next((items[k] for k in keys if items.get(k)), {})


def scan_names(rep):
    """The scan's guessed episodes, from the names it would save them as: {title id: {"season",
    "episode", "title"}} ("Psych - s01e02 - The Spelling Bee.mkv")."""
    out = {}
    for n in (rep or {}).get("plex_names") or []:
        src, _x, dest = n.partition(" -> ")
        m = re.search(r"\bs(\d+)e(\d+)((?:-e\d+)*)(?: - (.+?))?\.\w+$", dest.strip(), re.I)
        if m:
            more = [int(x) for x in re.findall(r"\d+", m.group(3))]
            episode = di.episode_value(", ".join(map(str, [int(m.group(2))] + more)))
            out[src.strip()] = {"season": int(m.group(1)), "episode": episode, "title": m.group(4) or ""}
    return out


def found_by_menus(label):
    """Whether the menus' word on a title is worth having: the film, an ad, or an extra with its
    name read (an extra the menus couldn't name says nothing: the scan's guess stands)."""
    return bool(label) and (label.get("type") in (Type.MAIN_MOVIE, Type.PROMO) or bool((label.get("title") or "").strip()))


# the scan's reading in TheDiscDB's terms (a copy, a play-all or a piece of the film has none)
ROLE_TYPES = {Role.FEATURE: Type.MAIN_MOVIE, Role.VERSION: Type.MAIN_MOVIE, Role.EPISODE: Type.EPISODE, Role.EXTRA: Type.EXTRA,
              Role.PROMO: Type.PROMO}


# Edit titles' order of what the menus found: by type first
FOUND_TYPES = (Type.MAIN_MOVIE, Type.EPISODE, Type.DELETED_SCENE, Type.EXTRA, Type.FEATURETTE, Type.INTERVIEW, Type.TRAILER, Type.SHORT, Type.MUSIC,
               Type.SCENE, Type.OTHER, Type.PROMO)
# a type in plain words, for sentences (the types themselves are TheDiscDB's words: kept as they are where they're picked)
TYPE_WORDS = {Type.MAIN_MOVIE: N_("Feature"), Type.EPISODE: N_("Episode"), Type.DELETED_SCENE: N_("Deleted Scene"), Type.EXTRA: N_("Extra"),
              Type.FEATURETTE: N_("Featurette"), Type.INTERVIEW: N_("Interview"), Type.TRAILER: N_("Trailer"), Type.SHORT: N_("Short"),
              Type.MUSIC: N_("Music"), Type.SCENE: N_("Scene"), Type.OTHER: N_("Other"), Type.PROMO: N_("Promo"),
              "BehindTheScenes": N_("Behind The Scenes")}


def type_words(kind):
    """"Deleted Scene" for DeletedScene, in the interface's language."""
    return _(TYPE_WORDS[kind]) if kind in TYPE_WORDS else re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind)


def menus_on(settings, disc_format):
    """Whether a disc of this format may have its menus crawled, Detect titles on offer for it: Settings >
    Disc's own switch for each. Any Blu-ray with its switch on, whatever it is: "experimental" is
    for those it can't read yet, and a crawl that finds nothing leaves the scan as it was. Not a Blu-ray's
    menu files: those are part of any scan, and cost nothing."""
    return bool(settings.get({"DVD": "menu_detect", "Blu-ray": "bluray_menus"}.get(disc_format)))


def hidden_roles(include_extras):
    """The kinds of title the title lists keep under Show more: repeats, ads, the film's pieces, and
    extras too unless they're to be ripped (Include extras): the rest is there to browse."""
    return TitlePicker.HIDDEN + (() if include_extras else (Role.EXTRA,))


def under_more(role, kind, include_extras):
    """Whether Edit titles keeps a title under Show more, waiting there to be confirmed or not (never
    counted UNCONFIRMED): by what it's down as (kind: the menus' type, TheDiscDB's, yours), the film and
    episodes always in view, an ad never, an extra unless extras are ripped (Include extras); else, a
    repeat, a play-all, a piece of the film, a logo or a menu loop, by the scan's role (hidden_roles)."""
    if kind in (Type.MAIN_MOVIE, Type.EPISODE):
        return False
    if kind == Type.PROMO:
        return True
    if kind:
        return not include_extras
    # (and what the scan set aside as too short to matter, listed from MakeMKV's reading: logos, menu loops)
    return role in hidden_roles(include_extras) or role in (Role.JUNK, Role.LOOP, Role.DECOY)


def more_text(hidden):
    """The Show more tick's words, for these rows hidden."""
    extras = any(t["role"] == Role.EXTRA for t in hidden)
    if any(t["role"] in (Role.JUNK, Role.LOOP, Role.DECOY) for t in hidden):     # (from MakeMKV's reading: logos, menu loops)
        if extras:
            return _("Show {count} more: extras, duplicates, play-alls, promos, pieces and short clips", count=len(hidden))
        return _("Show {count} more: duplicates, play-alls, promos, pieces and short clips", count=len(hidden))
    if extras:
        return _("Show {count} more: extras, duplicates, play-alls, promos and pieces of the film", count=len(hidden))
    return _("Show {count} more: duplicates, play-alls, promos and pieces of the film", count=len(hidden))


def found_order(tid, was, detected, index=0):
    """Where a title goes in Edit titles: by type (the film, the episodes, the deleted scenes...); by
    menu page; by the season and episode it's down as (a deleted scene by its episode's); then button
    by button, so that checking them doesn't jump about the page. Episodes go straight by their numbers:
    the page a button was found on is no reason to put one out of order. Without numbers, after those
    that have them, as the scan has them (index)."""
    kind = was.get("type")
    rank = FOUND_TYPES.index(kind if kind in FOUND_TYPES else Type.OTHER)
    place = (detected.get(tid) or {}).get("place")
    page = [] if kind == Type.EPISODE or not place else list(place[:-1])
    numbers = di.episode_numbers(was.get("episode"))
    numbered = (0, di._int(was.get("season")) or 0, numbers[0]) if numbers else (1, 0, 0)
    return (rank, not place and kind != Type.EPISODE, page, numbered, index, place or [], di._natural(tid))


PIECE_RE = re.compile(r"^(?:part (\d+) of (\d+) of|a piece of) (\S+)")


def piece_text(t):
    """A piece of a named title, in a word ("Part 2 of 3 of 00753.mpls", "Piece of 00746.mpls"), or "":
    its note's (core.apply_labels, the menu project's pieces); not the film's own pieces."""
    m = PIECE_RE.match(t.get("note") or "") if t.get("role") == Role.SEGMENT else None
    if not m or m.group(3) == "the":
        return ""
    if m.group(1):
        return _("Part {part} of {parts} of {title}", part=m.group(1), parts=m.group(2), title=m.group(3))
    return _("Piece of {title}", title=m.group(3))


def title_word(t, mine, items, detected, guesses):
    """The best word on what a title is, and whose it is: yours, then TheDiscDB's, then the
    menus' (Detect titles), then the scan's guess. ({"type", "title", "season", "episode"}, source)
    Edit titles starts its rows from it; Choose titles shows it."""
    tid = t["id"]
    theirs = discdb_item(items, t)
    if mine.get(tid):
        return mine[tid], "yours"
    if theirs.get("type") or theirs.get("title"):
        return theirs, "discdb"
    if found_by_menus(detected.get(tid)):
        return detected[tid], "menus"
    if (detected.get(tid) or {}).get("note") == NOT_IN_MENUS:    # (no word from them: nothing to confirm)
        return detected[tid], "scan"
    return dict(guesses.get(tid) or {}, type=ROLE_TYPES.get(t["role"], "")), "scan"


def fit_label(lbl, text, style):
    """Text on one line in a label; too long for it, it ends in an ellipsis (a Tooltip can show
    the whole, when the label's text isn't it)."""
    width = lbl.winfo_width() - 4
    if width > 20:
        text = elide(text, width, tkfont.Font(root=lbl, font=ttk.Style().lookup(style, "font") or "TkDefaultFont").measure)
    if str(lbl.cget("text")) != text:
        lbl.config(text=text)


def fitted_label(parent, text, style="TLabel", width=30):
    """A label width characters wide whose text, too long for it, ends in an ellipsis (fit_label,
    as it's drawn), the whole of it in a tooltip then (The Wistful Widow of Wagon Gap (1947))."""
    lbl = ttk.Label(parent, text=text, style=style, width=width)
    lbl.bind("<Configure>", lambda e: fit_label(lbl, text, style))
    th.Tooltip(lbl, lambda: text if str(lbl.cget("text")) != text else "")
    return lbl


def tree_fonts():
    """The fonts a Treeview's cells and headings are drawn in."""
    style = ttk.Style()

    def font(name, fallback):
        set_to = style.lookup(name, "font")
        return tkfont.Font(font=set_to) if set_to else tkfont.nametofont(fallback)
    return font("Treeview", "TkDefaultFont"), font("Treeview.Heading", "TkHeadingFont")


def elide(text, width, measure):
    """text cut to fit width pixels (measure: a font's), ending in an ellipsis; as it is, if it fits."""
    if measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def fit_columns(tv, pad=24, most=600, full=None, fill=None):
    """A Treeview's columns each made as wide as its heading or its widest cell (the font's own measure: no guessing
    by characters), up to `most` pixels. With full ({row id: its cells, whole}), one column (fill: its name; the
    first, else) takes what's left of the table's width instead, and any cell too long for its column is cut short
    (fit_first): no sideways scroll."""
    body, head = tree_fonts()
    rows = [full[i] if full else tv.item(i, "values") for i in tv.get_children()]
    fill = fill or tv["columns"][0]
    for n, c in enumerate(tv["columns"]):
        if full and c == fill:
            continue
        widest = max([head.measure(tv.heading(c, "text"))] + [body.measure(str(v[n])) for v in rows if n < len(v)])
        tv.column(c, width=min(most, widest + th.real(th.px(pad))), minwidth=th.real(th.px(40)), stretch=False)
    if full:
        fit_first(tv, full, fill)


def fit_first(tv, full, fill=None):
    """A table's fill column (the first, unless named) as wide as the table leaves it (after the others), and each
    cell cut short with an ellipsis where it doesn't fit its column (full: {row id: its cells, whole}; a TreeTip
    shows the whole)."""
    cols = tv["columns"]
    fill = fill or cols[0]
    width = tv.winfo_width()
    if width <= 1:                                # (not drawn yet: as it asks to be)
        width = tv.winfo_reqwidth()
    rest = sum(tv.column(c, "width") for c in cols if c != fill)
    # (the table asks for its columns plus its own border, which differs by theme: that border measured, not guessed,
    # or a wider column asks for a wider table, a wider window, and round again with no end, as on Linux)
    border = max(0, tv.winfo_reqwidth() - rest - tv.column(fill, "width"))
    room = max(th.real(th.px(120)), width - rest - border)
    if tv.column(fill, "width") != room:
        tv.column(fill, width=room, minwidth=th.real(th.px(120)), stretch=True)
    measure = tree_fonts()[0].measure
    rooms = [tv.column(c, "width") - th.real(th.px(12)) for c in cols]
    for iid in tv.get_children():
        for n, c in enumerate(cols):
            if iid in full and n < len(full[iid]):
                text = elide(str(full[iid][n]), rooms[n], measure)
                if tv.set(iid, c) != text:
                    tv.set(iid, c, text)


class TreeTip(th.Tooltip):
    """A table's note under the pointer: text(row id, column name) for the cell the pointer rests on ("" none),
    asked again as it moves from cell to cell."""

    def __init__(self, tv, text):
        self.tv, self.at = tv, None
        super().__init__(tv, lambda: text(*self.at) if self.at and self.at[0] else "")
        tv.bind("<Motion>", self._moved, add="+")

    def _moved(self, e):
        col = self.tv.identify_column(e.x)
        n = int(col[1:]) - 1 if col.startswith("#") and col[1:].isdigit() else -1
        cols = self.tv["columns"]
        at = (self.tv.identify_row(e.y), cols[n] if 0 <= n < len(cols) else None)
        if at != self.at:
            self.at = at
            self._schedule()

    def show(self):
        super().show()
        if self.tip:                              # (by the pointer, not under the whole table)
            x, y = self.tv.winfo_pointerxy()
            self.tip.wm_geometry("+%d+%d" % (max(0, min(x, self.tv.winfo_screenwidth() - self.tip.winfo_reqwidth() - 8)),
                                             y + 18))


def menu_name(came_from):
    """A menu page's name, for its picture's heading: the button it's reached by and "Menu" ("Yearbook Character
    Clips Menu"; one already a menu's, as it is), else the menu the disc opens on: "Main Menu"."""
    words = " ".join((came_from or "").split())
    if not words:
        return _("Main Menu")
    if words.isupper():                           # (a button in capitals: "SPECIAL FEATURES")
        words = words.title()
    return words if re.search(r"\bmenu\s*$", words, re.I) else _("{button} Menu", button=words)


def menu_trail(trail):
    """The way to a menu page from the main menu, for its heading's tip: "Main Menu › Bonus Features › Odette's
    Friends › Jean-Bob" (each the button pressed for the next). "" for the main menu."""
    if not trail:
        return ""
    clean = lambda w: " ".join(w.split()).title() if w.isupper() else " ".join(w.split())
    return " › ".join([_("Main Menu")] + [clean(w) for w in trail])


SUSPECT_LINES = {     # (a lone suspect, the last one so far, k of n)
    "questioning": (N_("Questioning suspect…"), N_("Questioning suspect {k}…"), N_("Questioning suspect {k} of {n}…")),
    "interrogating": (N_("Interrogating suspect…"), N_("Interrogating suspect {k}…"),
                      N_("Interrogating suspect {k} of {n}…")),
}


def suspect_line(doing, k, n):
    """"Interrogating suspect 2 of 5…"; the last of them just "suspect 5…" (the total's often still
    growing: the crawl finds pages as it goes); a lone one, "Interrogating suspect…". doing: "questioning"
    or "interrogating"."""
    lone, last, of = SUSPECT_LINES[doing]
    if n <= 1:
        return _(lone)
    return _(last, k=k) if k >= n else _(of, k=k, n=n)


def time_left(seconds):
    """A wait still to go, in words: "under a minute left", "about 3 min left"; "" when not known."""
    if seconds is None:
        return ""
    if seconds < 50:
        return _("under a minute left")
    return _("about {minutes} min left", minutes=max(1, round(seconds / 60)))


def suspect_report(found):
    """Detect titles' answer, in the case's words: "The suspects named the film and 3 extras"
    (each button pressed: a suspect; a trailer of the film's own counts among the extras)."""
    n = len(found.get("extras") or [])
    eps = len(found.get("episodes") or [])
    named = [w for w in (_("the film") if found.get("feature") else "",
                         ui_n("{n} episode", "{n} episodes", eps) if eps else "",
                         ui_n("{n} extra", "{n} extras", n) if n else "") if w]
    if found.get("suspects", 0) == 1:
        return _("The suspect named {what}", what=names_and(named))
    return _("The suspects named {what}", what=names_and(named))


def discdb_named(rep):
    """The titles TheDiscDB names or types for this disc ("Title 3", or a Blu-ray's playlist)."""
    hit = (rep.get("discdb") or {}).get("hit") or {}
    out = set()
    for t in hit.get("titles") or []:
        item, src = t.get("item") or {}, str(t.get("source") or "")
        if item.get("title") or item.get("type"):
            out.add("Title %d" % int(src) if src.isdigit() else src)
    return out


def linked_episode(item):
    """The episode numbers a label ties an extra to, or None."""
    if item.get("type") in (Type.EPISODE, Type.MAIN_MOVIE) or not str(item.get("season")).isdigit():
        return None
    return di.episode_numbers(item.get("episode")) or None


def extra_dest(item, name, episodes, base, season):
    """Where a rip saves an extra, at the level Plex shows it: beside the episode it's tied to; else
    in its type's folder under the disc's season (season: None or 0, a disc of no one season), else
    under the show. One tied to an episode with no file known keeps its code, under that season."""
    numbers = linked_episode(item)
    if numbers:
        home = episodes.get((int(item["season"]), numbers[0]))
        if home:
            return linked_dest(home, name, item.get("type"))
        name = "%s - %s" % (di._ep_code(int(item["season"]), numbers).lower(), name)
        season = int(item["season"])
    level = "%s/Season %02d" % (base, season) if season else base
    return "%s/%s/%s.mkv" % (level, EXTRA_FOLDERS.get(item.get("type"), "Other"), name)


def episode_files(mine, known, names, base, show):
    """Where a rip saves each episode this disc knows of, ripped this time or not: {(season, number):
    its path}. Your labels and the menus' first, then TheDiscDB's, then the scan's guess."""
    out = {}

    def add(season, numbers, dest):
        for n in numbers:
            out.setdefault((season, n), dest)
    for item in list(mine.values()) + list(known.values()):
        numbers = di.episode_numbers(item.get("episode"))
        if item.get("type") == Type.EPISODE and numbers and str(item.get("season")).isdigit():
            add(int(item["season"]), numbers, episode_dest(base, show, item["season"], item["episode"], item.get("title")))
    for dest in names.values():
        code = re.search(r"\bs(\d+)e(\d+)((?:-e\d+)*)", dest, re.I)
        if code:
            add(int(code.group(1)), [int(code.group(2))] + [int(x) for x in re.findall(r"\d+", code.group(3))], dest)
    return out


def linked_dest(episode_file, name, kind):
    """An extra Plex shows under its episode: beside it, the episode's whole file name, the extra's
    own, then Plex's ending for its type. Plex doesn't take a dash in the extra's name, so none's
    kept ("Sc. 8-10 - Chuck with Jill": "Sc. 8 to 10 Chuck with Jill"), nor the episode's name
    again ("Pilot Deleted Scene": "Deleted Scene"), the file's saying that already."""
    def plain(text):
        text = re.sub(r"(?<=\d)\s*[-\u2013\u2014]\s*(?=\d)", " to ", text)
        return re.sub(r"\s+", " ", re.sub(r"[-\u2013\u2014]", " ", text)).strip()
    stem = episode_file[:-len(".mkv")]
    words = plain(name)
    episode = re.search(r"\bs\d+e\d+(?:-e\d+)* - (.+)$", stem, re.I)
    opening = plain(episode.group(1)) + " " if episode else None
    if opening and words.lower().startswith(opening.lower()) and words[len(opening):].strip():
        words = words[len(opening):].strip()
    return "%s - %s-%s.mkv" % (stem, words, EXTRA_SUFFIXES.get(kind, "other"))


def plan_rip(rep, mk_titles, include_extras, selected=None):
    """Which MakeMKV titles to rip and what to call them (Plex layout). Of the film's copies in
    other pictures, the best by default (higher resolution, then HDR, then widescreen: a
    fullscreen copy is left out); the others are yours to tick."""
    ours = [{"title_id": t["id"], "role": t["role"], "duration": t["duration"],
             "clips": t.get("clip_list"), "episode_index": t.get("episode_index"), "edition": t.get("edition"),
             "interactive": t.get("interactive"), "special_name": t.get("special_name"),
             "video_info": t.get("video_info")}
            for t in rep.get("titles") or []]
    pairs = di.map_makemkv(mk_titles, ours, rep["format"])
    names = {}
    for n in rep.get("plex_names") or []:
        src, _x, name = n.partition(" -> ")
        names[src.strip()] = name.strip()
    # TheDiscDB knows exactly what each title is: real extra names, exact episodes.
    hit = (rep.get("discdb") or {}).get("hit") or {}
    known = {str(t.get("source") or "").lower(): t.get("item") or {} for t in hit.get("titles") or []}
    cand = (rep.get("candidates") or [None])[0] if di.adopt_guess(rep.get("candidates")) else None
    if cand and cand.get("kind") == "collection" and cand.get("items"):
        first = cand["items"][0]        # extras of a short-film disc go with the first film
        base = "%s (%s) {imdb-%s}" % (clean(first["name"]), first.get("year") or "", first["imdb_id"])
        show = "%s (%s)" % (clean(first["name"]), first.get("year") or "")
    elif cand and cand.get("imdb_id"):
        base = "%s (%s) {imdb-%s}" % (clean(cand["name"]), cand.get("year") or "", cand["imdb_id"])
        show = "%s (%s)" % (clean(cand["name"]), cand.get("year") or "")
    else:
        guess = (rep.get("best_guess") or rep.get("label") or "Disc %s" % rep["content_hash"][:8]).split(" | ")[0]
        base = show = clean(guess) or "Disc"
    # a film's special-features disc has nothing but extras: those are what to rip
    roles = RIP_ROLES + ((Role.EXTRA,) if include_extras or rep.get("extras_disc") else ())
    mine = all_labels(rep)                   # your labels (Edit titles): they win over TheDiscDB's
    homes = extras_homes(rep, cand)
    if cand and cand.get("kind") == "collection":
        # each film of the set in its own folder (your label on it drops the scan's name for it:
        # Rush Hour & Rush Hour 2, both labelled, went into Rush Hour's)
        for it in cand.get("items") or []:
            if it.get("imdb_id"):
                homes[it["title_id"]] = ("%s (%s) {imdb-%s}" % (clean(it["name"]), it.get("year") or "", it["imdb_id"]),
                                         "%s (%s)" % (clean(it["name"]), it.get("year") or ""))
    base0, show0 = base, show
    episodes = episode_files(mine, known, names, base, show)
    # the disc's season, as far as it's said, not worked out (a bonus disc has none, or season 0)
    season_level = None if rep.get("season_note") else (rep.get("match") or {}).get("season")
    plan = []
    for m in mk_titles:
        t, _x = pairs.get(m["index"], (None, None))
        base, show = homes.get(t["title_id"], (base0, show0)) if t else (base0, show0)
        item = known.get((m["source"] or "").lower()) or {}
        if not item and t and t.get("clips") and "," not in t["clips"]:
            item = known.get(t["clips"].lower() + ".m2ts") or {}      # playlist of one clip
        if not item and m.get("dvd_title"):                         # TheDiscDB numbers DVD titles "01"
            item = known.get("%02d" % m["dvd_title"]) or known.get(str(m["dvd_title"])) or {}
        role = t["role"] if t else di.LABEL_ROLES.get(item.get("type"), Role.EXTRA) if item else None
        my = mine.get(t["title_id"]) if t else None
        if my and (my.get("type") or my.get("title")):
            item = dict(item, **{k: v for k, v in my.items() if v not in (None, "")})        # (season 0's a season)
            if my.get("type"):
                role = di.label_role(my["type"], t["role"] if t else None)
        if selected is not None:
            if not t or t["title_id"] not in selected:
                continue
        elif role not in roles or (t and t.get("interactive")):    # a menu game: only if ticked
            continue
        if role == Role.EPISODE and str(item.get("season")).isdigit() and di.episode_numbers(item.get("episode")):
            dest = episode_dest(base, show, item["season"], item["episode"], item.get("title"))
        elif role in (Role.PLAY_ALL, Role.PROMO) and item.get("title") and item.get("type") not in (None, "", Type.PROMO):
            # ticked by hand, but labelled (yours, or TheDiscDB's): saved as the label says
            dest = extra_dest(item, described(clean(item["title"]), item.get("description")), episodes, base, season_level)
        elif t and t["title_id"] in names and not (my and role == Role.EXTRA):
            dest = names[t["title_id"]]
        elif role == Role.EXTRA:
            label = described(clean(item.get("title")), item.get("description")) or clean((t or {}).get("special_name")) or \
                "%s - %s" % (show, (t["title_id"] if t else m["source"]).replace(".", "_"))
            dest = extra_dest(item, label, episodes, base, season_level)
        elif role == Role.EPISODE:
            dest = "%s/Episode %02d.mkv" % (base, (t or {}).get("episode_index") or m["index"])
        elif role in (Role.FEATURE, Role.VERSION, Role.LONGEST) and my and edition_label(my, base, show) and \
                not (cand and cand.get("kind") == "collection"):    # (a collection's: its films' names)
            # your label's name for a cut of the film: its Plex edition (The Iron Giant's
            # "Signature Edition" and "Original Edition", one folder, a version picker in Plex)
            dest = "%s/%s {edition-%s}.mkv" % (base, show, edition_label(my, base, show))
        elif role in (Role.FEATURE, Role.LONGEST):
            dest = "%s/%s.mkv" % (base, show)
        elif role == Role.VERSION and t and t.get("edition") and "{imdb-" in base:
            # a ticked widescreen / full-screen / 4K copy: another Plex edition of the same film
            dest = "%s/%s {edition-%s}.mkv" % (base, show, clean(t["edition"]))
        else:   # hand-picked play-alls, alternate versions, duplicates
            dest = "%s/Other/%s - %s.mkv" % (base, show, (t["title_id"] if t else m["source"]).replace(".", "_"))
        plan.append({"mk": m["index"], "title_id": t["title_id"] if t else m["source"], "role": role,
                     "length": m["duration_text"], "dest": dest, "full": fullscreen(t, item),
                     "picture": (t or {}).get("video_info"), "seconds": m.get("duration") or (t or {}).get("duration") or 0})
    copies = [p for p in plan if p["role"] in (Role.FEATURE, Role.VERSION, Role.LONGEST)]
    if selected is None and len(copies) > 1:
        # the better picture, by default: a copy of about the same length (the same film) in a
        # better one leaves this one for you to tick (Rush Hour 3's fullscreen; a 4K disc's 1080p
        # copy). Cuts in one picture are both kept (The Iron Giant's editions)
        rank = lambda p: (di.video_rank(p["picture"]), not p["full"])
        near = lambda a, b: min(a["seconds"], b["seconds"]) > 0 and \
            max(a["seconds"], b["seconds"]) <= 1.15 * min(a["seconds"], b["seconds"])
        worse = [p for p in copies if any(q is not p and near(p, q) and rank(q) > rank(p) for q in copies)]
        plan = [p for p in plan if p not in worse]
    for p in plan:
        del p["full"], p["picture"], p["seconds"]
    if not plan and mk_titles and selected is None:   # nothing classified: the longest title (never a loop)
        real = [x for x in mk_titles if (pairs.get(x["index"], (None, None))[0] or {}).get("role")
                not in (Role.LOOP, Role.JUNK, Role.DUPLICATE, Role.DECOY, Role.PLAY_ALL, Role.SEGMENT)] or mk_titles
        m = max(real, key=lambda x: x["duration"] or 0)
        plan.append({"mk": m["index"], "title_id": m["source"] or "title %d" % m["index"], "role": Role.LONGEST,
                     "length": m["duration_text"], "dest": "%s/%s.mkv" % (base0, show0)})
    return plan


def edition_label(my, base, show):
    """The edition your label names a film's title with ("Signature Edition"), or "": none for a
    label naming the film itself ("The Iron Giant"), or a film with no IMDb folder to put it in."""
    name = clean(my.get("title"))
    if my.get("type") != Type.MAIN_MOVIE or not name or "{imdb-" not in base:
        return ""
    plain = lambda s: re.sub(r"[^a-z0-9]", "", re.sub(r"\(\d{4}\)", "", s.lower()))
    return "" if plain(name) == plain(show) else name


FULLSCREEN_RE = re.compile(r"\bfull[\s-]*screen\b|\bpan[\s&-]*(?:and[\s-]*)?scan\b", re.I)


def fullscreen(t, item=None):
    """A copy of the film cropped to fill an old TV's screen: so labelled ("Fullscreen", "Pan &
    Scan"; yours, TheDiscDB's, or the edition the scan gave it), or a 4:3 picture, not letterboxed."""
    if FULLSCREEN_RE.search(" ".join(x or "" for x in ((item or {}).get("title"), (t or {}).get("edition")))):
        return True
    v = (t or {}).get("video_info") or {}
    return v.get("aspect") == "4:3" and not v.get("letterboxed")


def extras_homes(rep, cand):
    """A disc of several films: each extra whose film is known (your answer's {extras:...}, else
    this session's Detect titles: the film's menu it's on) and that film's Plex folder and name,
    {title id: (folder, name)}. The rest go with the first film."""
    if not cand or cand.get("kind") != "collection" or not cand.get("items"):
        return {}
    by_imdb = {it["imdb_id"]: it for it in cand["items"]}
    by_title = {it["title_id"]: it for it in cand["items"]}
    owners = {tid: by_imdb[i] for tid, i in (cand.get("extras") or {}).items() if i in by_imdb}
    for e in (rep.get("menu_found") or {}).get("extras") or []:
        film = by_title.get(menu_title_id(e["film"])) if e.get("film") else None
        if film:
            owners.setdefault(menu_title_id(e["title"]), film)
    return {tid: ("%s (%s) {imdb-%s}" % (clean(it["name"]), it.get("year") or "", it["imdb_id"]),
                  "%s (%s)" % (clean(it["name"]), it.get("year") or "")) for tid, it in owners.items()}


def preview_titles(rep):
    """The disc's own titles in MakeMKV's shape, so the rip plan can be previewed before MakeMKV runs."""
    out = []
    for i, t in enumerate(rep.get("titles") or []):
        m = re.match(r"Title (\d+)$", t["id"])
        out.append({"index": i, "source": "" if m else t["id"], "dvd_title": int(m.group(1)) if m else None,
                    "duration": t["duration"], "duration_text": di.hms(t["duration"]),
                    "clips": t.get("clip_list")})
    return out


def own_edition(t, fmt):
    """What this copy of a film is, on its own (the copy already in the folder isn't known):
    3D, 4K, a DVD's Fullscreen / Widescreen, a Blu-ray's resolution, else its running time."""
    v = (t or {}).get("video_info") or {}
    if v.get("3d"):
        return "3D"
    if (v.get("height") or 0) >= 2160:
        return "4K"
    if fmt == "DVD" and v.get("aspect"):
        return "Widescreen" if v["aspect"] == "16:9" or v.get("letterboxed") else "Fullscreen"
    if v.get("height"):
        return "%dp" % v["height"]
    return "%d Minute Cut" % round(((t or {}).get("duration") or 0) / 60) if t else None


def rip_path(path, item, rep):
    """Where a ripped title goes. A film whose file is already there (its other version, from
    another disc of the set) gets a Plex edition saying what this one is, so Plex shows one film
    with a version picker; anything else that's taken gets a (2)."""
    if os.path.exists(path) and item.get("role") in (Role.FEATURE, Role.VERSION, Role.LONGEST) and "{edition-" not in path:
        t = next((x for x in rep.get("titles") or [] if x["id"] == item.get("title_id")), None)
        ed = own_edition(t, rep.get("format"))
        if ed:
            stem, ext = os.path.splitext(path)
            path = "%s {edition-%s}%s" % (stem, ed, ext)
    return unique_path(path)


def unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s (%d)%s" % (stem, n, ext)):
        n += 1
    return "%s (%d)%s" % (stem, n, ext)


# --------------------------------------------------------------------------
# the window: two split buttons and a summary
# --------------------------------------------------------------------------

class ThemedMenu:
    """A drop-down menu in the app's own colours and fonts (Windows draws Tk's menus itself and
    ignores their colours). It takes the tk.Menu calls the app uses: add_command / radiobutton /
    checkbutton / separator, delete, index, type, entrycget, invoke, tk_popup, unpost."""

    headless = False                 # tests: build the entries, never show the window

    def __init__(self, master):
        self.master = master
        self.entries = []
        self.top = None
        self._rows = []
        self._hot = None

    # ---- the tk.Menu calls ----
    def add_command(self, label="", command=None, state="normal", image=None, compound=None, **_x):
        self.entries.append({"type": "command", "label": label, "command": command, "state": state, "image": image or ""})

    def add_radiobutton(self, label="", value=None, variable=None, command=None, state="normal", **_x):
        self.entries.append({"type": "radiobutton", "label": label, "value": value, "variable": variable,
                             "command": command, "state": state, "image": ""})

    def add_checkbutton(self, label="", variable=None, command=None, state="normal", **_x):
        self.entries.append({"type": "checkbutton", "label": label, "variable": variable, "command": command,
                             "state": state, "image": ""})

    def add_separator(self):
        self.entries.append({"type": "separator", "label": "", "state": "disabled", "image": ""})

    def delete(self, first, last=None):
        if last == "end":
            self.entries = self.entries[:first]
        else:
            last = first if last is None else last
            self.entries = self.entries[:first] + self.entries[last + 1:]

    def index(self, what):
        return (len(self.entries) - 1 if self.entries else None) if what == "end" else what

    def type(self, i):
        return self.entries[i]["type"]

    def entrycget(self, i, option):
        return self.entries[i].get(option, "")

    def invoke(self, i):
        e = self.entries[i]
        if e["state"] == "disabled" or e["type"] == "separator":
            return
        self.unpost()
        var = e.get("variable")
        if e["type"] == "radiobutton" and var is not None:
            var.set(e["value"])
        elif e["type"] == "checkbutton" and var is not None:
            var.set(not var.get())
        if e.get("command"):
            e["command"]()

    def grab_release(self):
        pass

    def unpost(self):
        if self.top is not None:
            try:
                self.top.grab_release()
                self.top.destroy()
            except tk.TclError:
                pass
        self.top = None

    # ---- showing it ----
    def tk_popup(self, x, y, *_x):
        self.unpost()
        if self.headless or not self.entries:
            return
        top = self.top = tk.Toplevel(self.master)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(background=th.BRASS_400)
        body = tk.Frame(top, background=th.CARD, padx=0, pady=4)
        body.pack(padx=1, pady=1)
        self._rows, self._hot = [], None
        tick, dot = th.icon_tick(), th.icon_dot()
        for i, e in enumerate(self.entries):
            if e["type"] == "separator":
                tk.Frame(body, background=th.BRASS_200, height=1).pack(fill="x", padx=8, pady=4)
                self._rows.append(None)
                continue
            off = e["state"] == "disabled"
            row = tk.Frame(body, background=th.CARD, padx=10, pady=3, cursor="" if off else "hand2")
            row.pack(fill="x")
            mark = None
            if e["type"] == "radiobutton":
                mark = dot if e["variable"] is not None and e["variable"].get() == e["value"] else ""
            elif e["type"] == "checkbutton":
                mark = tick if e["variable"] is not None and e["variable"].get() else ""
            if mark is not None:
                tk.Label(row, image=mark or th.icon_blank(), background=th.CARD).pack(side="left", padx=(0, 6))
            lbl = tk.Label(row, text=e["label"].strip(), background=th.CARD, anchor="w", justify="left",
                           foreground=th.INK_MUTED if off else th.INK,
                           font=(th.MONO, 8, "bold") if off else (th.SANS, 9),
                           **({"image": e["image"], "compound": "left", "padx": 0} if e.get("image") else {}))
            lbl.pack(side="left", fill="x", expand=True)
            self._rows.append(row)
            if not off:
                for w in (row, lbl, *row.winfo_children()):
                    w.bind("<Enter>", lambda ev, i=i: self._light(i))
                    w.bind("<ButtonRelease-1>", lambda ev, i=i: self.invoke(i))
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        left, up, right, down = work_area(top)
        x = min(x, right - w - 4)
        if y + h > down - 4:                # no room above the taskbar: open upwards
            y = max(up, y - h - 30)
        top.geometry("+%d+%d" % (max(left, x), y))
        top.deiconify()
        top.lift()
        top.bind("<Escape>", lambda e: self.unpost())
        top.bind("<Up>", lambda e: self._step(-1))
        top.bind("<Down>", lambda e: self._step(1))
        top.bind("<Return>", lambda e: self._hot is not None and self.invoke(self._hot))
        top.bind("<ButtonPress-1>", self._outside, add="+")
        top.bind("<FocusOut>", lambda e: self.top is not None and self.top.after(150, self._check_focus))
        try:
            top.focus_force()
            top.grab_set()                  # a click anywhere else in the app closes it
        except tk.TclError:
            pass

    def _outside(self, e):
        if self.top is None:
            return
        x, y = e.x_root, e.y_root
        t = self.top
        if not (t.winfo_rootx() <= x < t.winfo_rootx() + t.winfo_width()
                and t.winfo_rooty() <= y < t.winfo_rooty() + t.winfo_height()):
            self.unpost()

    def _check_focus(self):
        try:
            if self.top is not None and self.top.focus_get() is None:
                self.unpost()                # another program took the focus
        except (tk.TclError, KeyError):
            self.unpost()

    def _light(self, i):
        for k, row in enumerate(self._rows):
            if row is not None:
                bg = th.BRASS_200 if k == i else th.CARD
                row.configure(background=bg)
                for w in row.winfo_children():
                    w.configure(background=bg)
        self._hot = i

    def _step(self, d):
        live = [k for k, e in enumerate(self.entries) if e["type"] != "separator" and e["state"] != "disabled"]
        if not live:
            return
        k = live.index(self._hot) + d if self._hot in live else (0 if d > 0 else len(live) - 1)
        self._light(live[k % len(live)])


class SplitButton(ttk.Frame):
    """A button with a small ▾ menu attached (click = default action)."""

    def __init__(self, parent, text, command, style="Scan.TButton", arrow_style="ScanArrow.TButton",
                 frame_style="TFrame", image=None):
        super().__init__(parent, style=frame_style)
        self.button = ttk.Button(self, text=text, style=style, command=command,
                                 **({"image": image, "compound": "left"} if image else {}))
        self.button.pack(side="left", fill="y")
        self.menu = ThemedMenu(self)
        # a narrow plain button that pops the menu (themes force a wide minimum on Menubuttons)
        self.arrow = ttk.Button(self, style=arrow_style, command=self.popup,
                                image=th.caret_image("#ece1c4" if "Rip" in arrow_style else th.BRASS_700))
        self.arrow.pack(side="left", fill="y", padx=(1, 0))

    def popup(self):
        x = self.arrow.winfo_rootx()
        y = self.arrow.winfo_rooty() + self.arrow.winfo_height()
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def set_state(self, enabled, arrow_enabled=None):
        self.button.config(state="normal" if enabled else "disabled")
        self.arrow.config(state="normal" if (enabled if arrow_enabled is None else arrow_enabled) else "disabled")

    def set_text(self, text):
        self.button.config(text=text)


SCAN_DIVIDER = "---- scan of"


class NotesHandler(logging.Handler):
    """Copies the disc_detective log, DEBUG detail included, into the app (thread-safe: posted)."""

    def __init__(self, app):
        super().__init__(logging.DEBUG)
        self.app = app
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.app.post(self.app.add_note, self.format(record))
        except Exception:
            self.handleError(record)


def new_dialog(parent, grab=False):
    """A dialog window, kept hidden until it's built and in place (shown at the window manager's
    spot first and then moved, it would flash). It appears once its __init__ is done: centred
    over `parent` unless it placed itself (centre_over or its own geometry: mark top._placed)."""
    top = tk.Toplevel(parent)
    top.withdraw()
    top._placed, top._grab = False, grab

    def reveal():
        try:
            if not top.winfo_exists() or top.state() != "withdrawn":
                return
            if not top._placed:
                centre_over(top, parent)
            top.deiconify()
            th.color_title_bar(top)           # Windows gives a hidden window its frame only once it's shown
            if top._grab:
                top.grab_set()
        except tk.TclError:
            pass
    # a timer, not after_idle: building a dialog runs update_idletasks (the title-bar colour, its
    # own measuring), which would run an idle callback there and then, half built and unplaced
    top.after(1, reveal)
    return top


def work_area(widget):
    """(left, top, right, bottom) of the screen a window may use: on Windows, the screen less the
    taskbar (the app is DPI aware, so these are Tk's pixels); elsewhere, the screen less a margin."""
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes
            r = ctypes.wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
                return r.left, r.top, r.right, r.bottom
        except (AttributeError, OSError):
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight() - th.px(48)


# a window's frame round its contents: the title bar above, the border at the sides and below
FRAME_TOP, FRAME_EDGE = 32, 8


def keep_on_screen(top, x=None, y=None, w=None, h=None):
    """Move a window (or put one about to be shown at x, y, with contents w x h) so all of it,
    title bar to bottom edge, is inside the work area: above the taskbar. A window that grows
    after it's shown (Edit titles, filled in by Detect titles) is moved back up."""
    top.update_idletasks()
    if x is None:
        x, y = top.winfo_rootx(), top.winfo_rooty()
        w, h = top.winfo_width(), top.winfo_height()
    left, up, right, down = work_area(top)
    bar, edge = th.px(FRAME_TOP), th.px(FRAME_EDGE)
    nx = max(left + edge, min(x, right - w - edge))
    ny = max(up + bar, min(y, down - h - edge))
    if (nx, ny) != (x, y):
        # geometry places the frame's corner: the contents sit a title bar below, a border in
        top.geometry("+%d+%d" % (nx - edge, ny - bar))


def play_all_text(note):
    """What a play-all row says in Edit titles, from the scan's note ("plays Title 16, Title 17,
    Title 18"): "Play-all of Titles 16-18" (runs joined up, others listed)."""
    nums = [int(n) for n in re.findall(r"Title (\d+)", note or "")]
    if not nums:
        return _("Play-all")
    runs = []
    for n in nums:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    said = ", ".join("%d" % a if a == b else "%d–%d" % (a, b) for a, b in runs)
    return ui_n("Play-all of Title {numbers}", "Play-all of Titles {numbers}", len(nums), numbers=said)


def autohide(bar):
    """A scroll command for a gridded scrollbar that shows it only when there's more to scroll
    to (Case notes: no bars over a short log)."""
    def set_(lo, hi):
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            bar.grid_remove()
        else:
            bar.grid()
        bar.set(lo, hi)
    return set_


def centre_over(top, parent):
    """Put a dialog in the middle of the main window, kept on the screen (above the taskbar)."""
    top._placed = True
    top.update_idletasks()
    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    top.geometry("+%d+%d" % (x - th.px(FRAME_EDGE), y - th.px(FRAME_TOP)))
    keep_on_screen(top, x, y, w, h)


class NotesWindow:
    """Scan ▾ > Case notes: the technical log of this session, live, beside the main window."""

    def __init__(self, parent, app):
        self.app = app
        top = self.top = new_dialog(parent)
        top.title(_("{APP} · case notes", APP=APP))
        # over the main window (a window beside it can land off the screen)
        parent.update_idletasks()
        w = max(th.px(480), min(th.px(760), parent.winfo_width() - 40))
        h = max(th.px(320), min(th.px(520), parent.winfo_height() - 60))
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        top.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))
        keep_on_screen(top, x, y, w, h)
        top._placed = True
        f = ttk.Frame(top, padding=8)
        f.pack(fill="both", expand=True)
        self.text = tk.Text(f, wrap="none", font=("Consolas" if os.name == "nt" else "Courier", 9),
                            height=20, width=100, relief="flat", borderwidth=0)
        ys = th.ScrollBar(f, orient="vertical", command=self.text.yview)
        xs = th.ScrollBar(f, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=autohide(ys), xscrollcommand=autohide(xs))
        self.text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text=_("Follow new lines"), variable=self.follow).pack(side="left")
        ttk.Button(bar, text=_("Close"), command=top.destroy).pack(side="right")
        ttk.Button(bar, text=_("Clear"), command=self.clear).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text=_("Copy all"), command=self.copy).pack(side="right", padx=(0, 6))
        self.text.insert("end", "".join(self._spaced(x, i > 0) for i, x in enumerate(app._notes)))
        self.text.configure(state="disabled")
        self.text.see("end")

    @staticmethod
    def _spaced(line, after_something):
        """A blank line before each scan's divider, except at the very top."""
        return ("\n" if after_something and SCAN_DIVIDER in line else "") + line + "\n"

    def append(self, line):
        if not self.top.winfo_exists():
            return
        self.text.configure(state="normal")
        self.text.insert("end", self._spaced(line, self.text.index("end-1c") != "1.0"))
        self.text.configure(state="disabled")
        if self.follow.get():
            self.text.see("end")

    def clear(self):
        self.app._notes.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(self.text.get("1.0", "end-1c"))


class App:
    SCAN_TEXT = N_("Scan Disc")       # (shown with leading spaces: room beside its icon)
    RIP_TEXT = N_("Rip")
    # the empty desk's heading and the line under it (at the start, and when the source changes)
    EMPTY_TITLE, EMPTY_NOTE = N_("Insert a disc and press Scan Disc"), N_("Scan Disc reads the disc and opens a case file on it")

    MIN_STATUS_MS = 700       # each step of the story stays up at least this long

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self._story = collections.deque()   # status lines (and the result) waiting their turn
        self._story_job = None
        self._notes = collections.deque(maxlen=5000)   # the technical log, for the case notes window
        self.notes_win = None
        self.settings = load_settings()
        tools.choose(self.settings)          # (a copy of FFmpeg's or Tesseract's chosen in Settings)
        for area in i18n.DOMAINS:     # (each area its own language: "interface_language", "log_language")
            i18n.set_language(area, self.settings.get("%s_language" % area))
        self.rep = None
        self.selected = None          # None = default (feature / episodes [+ extras])
        self.titles_dialog = None     # Edit titles, while it's open (Detect titles fills it)
        self._menus_again = None      # a disc to identify again from its menus (Detect titles found several films)
        self._before = None           # the case on the desk before a scan cleared it (scan_done carries its menus over)
        self._stakeouts = {}          # {fingerprint: a stakeout's raw result}: this session's, reused by its scans
        # each drive as last asked, off the Tk thread (a stuck drive mustn't freeze the window):
        # {drive: ("ready", label) | ("empty", None) | ("busy", None)}; the asking, one at a time per drive
        self._drive_state, self._probes, self._drive_recheck, self._drives = {}, {}, None, []
        self._watch_asking = False    # (the watcher's question to the drive, not answered yet)
        self.detecting = False        # Detect titles under way
        self.detect_stop = threading.Event()  # (set: Detect titles called off, at the crawl's next step)
        self.mkv = None                # the rip under way, in MakeMKV's hands
        self.cancelled = False
        self.busy = False
        self.credits_asked = set()       # films whose credits were asked for this session (fetch_credits)
        self.ripping = False
        self.last_out = None
        self.mkv_scan = None          # MakeMKV reading a confirmed disc in the background (see prescan)
        self.lab_again = set()        # discs whose short titles have had a second reading since they went in
        self._answer_saved = None     # a disc whose corrected answer was saved, being rescanned
        root.title(_(WINDOW_TITLE))
        try:
            self._icon = tk.PhotoImage(file=ICON)
            root.iconphoto(True, self._icon)
        except (tk.TclError, OSError):
            pass
        # as big as the design wants, but never taller or wider than the screen above the taskbar
        # (title bar and border too): a window taller than that hides the footer under the taskbar
        self._style()                    # first: it works out the screen's scale the sizes below use
        left, up, right, down = work_area(root)
        bar, edge = th.px(FRAME_TOP), th.px(FRAME_EDGE)
        room_w, room_h = right - left - 2 * edge, down - up - bar - edge
        w, h = min(th.px(1000), room_w), min(th.px(780), room_h)
        root.minsize(min(th.px(760), w), min(th.px(520), h))
        root.geometry("%dx%d+%d+%d" % (w, h, left + (room_w - w) // 2, up + (room_h - h) // 2))
        self.source = tk.StringVar(value=self.settings.get("drive", ""))
        self.outdir = tk.StringVar(value=self.settings.get("outdir", os.path.join(os.path.expanduser("~"), "Videos", "Rips")))
        self.extras = tk.BooleanVar(value=self.settings.get("extras", False))
        self._drive_models = dict(self.settings.get("drive_models") or {})     # (as last found)
        self._build()
        self.refresh_drives(initial=True)
        self.find_drive_models()
        self.root.after(self.DRIVE_WATCH, self.watch_drive)
        di.status_hook = lambda msg, tone="info": self.post(self.tell, msg, tone)   # identifier steps -> status line, in turn
        for h in [h for h in di.logger.handlers if isinstance(h, NotesHandler)]:
            di.logger.removeHandler(h)                         # one app, one copy of the log
        self._notes_handler = NotesHandler(self)
        di.logger.addHandler(self._notes_handler)
        self.data_ui = None
        self._last_prog = 0.0

        def prog(label, done, total):                          # called from worker threads
            now = time.time()
            if now - self._last_prog >= 0.15 or (total and done >= total):
                self._last_prog = now
                self.post(self._on_progress, label, done, total)
        di.progress = prog
        root.after(400, self.maybe_first_run)
        root.after(600, self.resume_installs)
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- layout ----
    def _style(self):
        th.setup(self.root)
        th.color_title_bar(self.root)

    def _build(self):
        """The window, top to bottom: the toolbar (Scan Disc, the drive and the disc in it), the
        desk (the case: picture, title, details card, rip card) and the footer (status, links)."""
        root = self.root
        # ---- toolbar ----
        bar = th.frame(root, th.VELLUM_200, radius=0)
        bar.pack(fill="x")
        inner = ttk.Frame(bar, style="Bar.TFrame", padding=(18, 10))
        inner.pack(fill="x")
        # a plain glyph, as on Rip (the app's own icon is too detailed at button size)
        scan_icon = th.button_image("scan")
        self.scan_btn = ttk.Button(inner, text="  " + _(self.SCAN_TEXT), style="Scan.TButton", command=self.scan,
                                   **({"image": scan_icon, "compound": "left"} if scan_icon else {}))
        self.scan_btn.pack(side="left")
        # the source: a drop-down of the disc drives (and a folder), with each drive's make and model
        self.drive_btn = ttk.Button(inner, text="", style="Pill.TButton", command=self.drive_popup, compound="center")
        self.drive_btn.pack(side="right")
        self.drive_menu = ThemedMenu(self.root)
        ttk.Label(inner, text=_("SOURCE"), style="BarCap.TLabel").pack(side="right", padx=(0, 8))
        th.rule(root, th.BRASS_300).pack(fill="x")

        # ---- footer (packed before the desk so it keeps its place at the bottom) ----
        foot = th.frame(root, th.VELLUM_300, radius=0)
        foot.pack(side="bottom", fill="x")
        th.rule(root, th.BRASS_400).pack(side="bottom", fill="x")
        fin = ttk.Frame(foot, style="Foot.TFrame", padding=(14, 5))
        fin.pack(fill="x")
        links = ttk.Frame(fin, style="Foot.TFrame")
        links.pack(side="right")
        # the case's standing: no case / open (being investigated, or not solved yet) / closed
        self.case_dot = th.dot(fin, th.VELLUM_300, th.INK_SUBTLE)
        self.case_dot.pack(side="left", padx=(0, 8))
        # (only the room the links leave it: a long outcome ends in an ellipsis, whole on hover)
        self.standing_full = _("No case open")
        self.standing_lbl = ttk.Label(fin, text=self.standing_full, style="FootStatus.TLabel", width=1, anchor="w")
        self.standing_lbl.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.standing_lbl.bind("<Configure>", lambda ev: self._fit_standing())
        th.Tooltip(self.standing_lbl, lambda: self.standing_full
                   if self.standing_lbl.cget("text") != self.standing_full else "")
        for k, (text, cmd, icon) in enumerate(((_("About / credits"), self.about, "about"),
                                               (_("Open logs folder"), self.open_logs, "logs"),
                                               (_("Case notes (technical log)"), self.open_notes, "notes"),
                                               (_("Settings"), self.open_settings, "settings"))):
            if k:
                ttk.Label(links, text="|", style="FootSep.TLabel").pack(side="left", padx=8)
            lbl = th.link(links, text, cmd, style="FootLink.TLabel", icon=icon)
            lbl.pack(side="left")
            if icon == "about":
                self.about_link = lbl
        # (a newer version, seen with the weekly TheDiscDB index or by opening About: its icon there, no popup)
        self.update_tip = th.Tooltip(self.about_link, lambda: _("Version {version} is out", version=self.update["version"])
                                     if self.update else "")
        self.show_update()
        # ---- the desk (scrolls when a case is taller than the window) ----
        self.desk_scroll = DeskScroll(root)
        self.desk_scroll.pack(fill="both", expand=True)
        desk = ttk.Frame(self.desk_scroll.inner, style="Desk.TFrame", padding=(30, 14, 30, 12))
        desk.pack(fill="both", expand=True)
        row = ttk.Frame(desk, style="Desk.TFrame")
        row.pack(fill="both", expand=True)
        # picture column (poster / box art / disc art) in a brass mount; hidden when there's nothing
        self.pic_col = ttk.Frame(row, style="Desk.TFrame")
        # the brass mount runs on below the picture, behind the source line (as in the design)
        mount = th.frame(self.pic_col, th.BRASS_200, border=th.BRASS_300, radius=8, shadow=3)
        mount.pack(anchor="n")
        card = th.frame(mount, th.CARD, border=th.BRASS_600, pad=3, radius=4, outer=th.BRASS_200)
        card.pack(padx=9, pady=(9, 0))
        self.pic_lbl = ttk.Label(card, style="Poster.TLabel")
        self.pic_lbl.pack(anchor="n")
        self.pic_cap = ttk.Label(mount, text="", style="PosterCap.TLabel")
        self.pic_cap.pack(anchor="n", pady=(7, 8))
        self.pic_path = None            # the picture on show (full size where there is one)
        self.pic_menu = ThemedMenu(self.root)
        for w in (self.pic_lbl, self.pic_cap, card, mount):
            w.bind("<Button-3>", self.picture_menu)
            w.bind("<Button-2>" if sys.platform == "darwin" else "<Control-Button-1>", self.picture_menu)
        self._imgs = []                 # Tk drops images that nothing references
        info = self.info = ttk.Frame(row, style="Desk.TFrame")
        info.pack(side="left", fill="both", expand=True, anchor="n")
        self.case_lbl = ttk.Label(info, text="", style="Case.TLabel")
        self.case_lbl.pack(anchor="w")
        name_row = ttk.Frame(info, style="Desk.TFrame")
        name_row.pack(anchor="w", fill="x")
        self.title_lbl = ttk.Label(name_row, text=_(self.EMPTY_TITLE), style="Name.TLabel",
                                   wraplength=520, justify="left")
        self.title_lbl.pack(side="left", anchor="sw")
        self.year_lbl = ttk.Label(name_row, text="", style="Year.TLabel")
        # the year's figures centred on the title's letters (on its last line, when it wraps)
        tf = tkfont.Font(font=ttk.Style().lookup("Name.TLabel", "font"))
        yf = tkfont.Font(font=ttk.Style().lookup("Year.TLabel", "font"))
        lift = round((tf.metrics("descent") + th.cap_height(tf) / 2) - (yf.metrics("descent") + th.cap_height(yf) / 2))
        self.year_lbl.pack(side="left", anchor="sw", padx=(10, 0), pady=(0, th.real(max(0, lift))))
        if lift < 0:
            self.title_lbl.pack_configure(pady=(0, th.real(-lift)))
        facts = ttk.Frame(info, style="Desk.TFrame")
        facts.pack(anchor="w", pady=(2, 10))
        self.kind_lbl = ttk.Label(facts, text="", style="Kind.TLabel")
        self.kind_lbl.pack(side="left")
        self._fact_dots = [ttk.Label(facts, text="\u2022", style="DeskDot.TLabel") for _x in range(2)]
        self.facts_lbl = ttk.Label(facts, text="", style="Fact.TLabel")
        self.source_lbl = ttk.Label(facts, text="", style="Source.TLabel")
        self._facts_row = facts
        # the step of the investigation and its progress bar, above the rip card (or just under
        # the title while there's none); shown only while there's something to say
        self.status_strip = strip = th.frame(info, th.VELLUM_200, border=th.BRASS_300, radius=5, outer=th.PARCHMENT)
        sin = ttk.Frame(strip, style="Bar.TFrame", padding=(10, 3))
        sin.pack(fill="x")
        # the step on top (its light, its words, how far along), the gauge under it while there's one
        top = ttk.Frame(sin, style="Bar.TFrame")
        top.pack(fill="x")
        self.status_dot = th.dot(top, th.VELLUM_200, th.BRASS_400)
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_pct = ttk.Label(top, text="", style="BarPct.TLabel")
        self.status_pct.pack(side="right", padx=(12, 0))
        self.status = ttk.Label(top, text="", style="BarStatus.TLabel")
        self.status.pack(side="left", fill="x", expand=True)
        self.progress = th.Gauge(sin, th.VELLUM_200, maximum=1000,
                                 follow=lambda f: self.status_pct.config(text="" if f is None else _("{percent}%", percent=round(100 * f))))
        self.set_facts("", _(self.EMPTY_NOTE), "")

        # the details card: what the disc says, and whether that's the answer
        self.details_card = th.frame(info, th.CARD, border=th.BRASS_400, radius=7, shadow=3)
        self.details = ttk.Frame(self.details_card, style="Card.TFrame", padding=(16, 8, 16, 4))
        self.details.pack(fill="x")
        th.rule(self.details_card, th.BRASS_200).pack(fill="x", padx=16)
        self.match_row = ttk.Frame(self.details_card, style="Card.TFrame", padding=(16, 6, 16, 6))
        self.match_row.pack(fill="x")
        self.match_note = ttk.Label(self.match_row, text="", style="Warn.TLabel")
        tick = th.check(th.INK)
        self.confirm_btn = ttk.Button(self.match_row, text=_("That's right"), style="Small.TButton", command=self.confirm,
                                      image=(tick, "disabled", tick), compound="left")
        self.confirmed_lbl = ttk.Label(self.match_row, text=_("In your case files"), style="Ok.TLabel",
                                       image=th.check(), compound="left")
        self.match_btn = ttk.Button(self.match_row, text=_("Not right?"), style="Small.TButton", command=self.match_menu,
                                    image=th.caret_image(gap=5), compound="right")
        self.match_popup = ThemedMenu(self.root)

        # the rip card: what Rip will do, where it goes, and the Rip button
        self.rip_card = th.frame(info, th.CARD, border=th.BRASS_400, radius=7, shadow=3)
        rc = ttk.Frame(self.rip_card, style="Card.TFrame", padding=(16, 9, 16, 9))
        rc.pack(fill="x")
        head = ttk.Frame(rc, style="Card.TFrame")
        head.pack(fill="x")
        self.rip_dot = th.dot(head, th.CARD, th.BRASS_600, 9)
        self.rip_dot.pack(side="left", padx=(0, 8))
        self.plan_lbl = ttk.Label(head, text="", style="Head.TLabel", wraplength=420, justify="left")
        self.plan_lbl.pack(side="left")
        self.rip_tag = th.frame(head, th.VELLUM_200, border=th.BRASS_300, radius=4, outer=th.CARD)
        self.rip_badge = ttk.Label(self.rip_tag, text="", style="Badge.TLabel")
        self.rip_badge.pack()
        # beside it: titles the scan's own stakeout named, yours to confirm (a click opens Edit titles)
        self.confirm_tag = th.frame(head, th.AMBER_50, border=th.AMBER_300, radius=4, outer=th.CARD)
        self.confirm_badge = ttk.Label(self.confirm_tag, text="", style="Confirm.TLabel", cursor="hand2")
        self.confirm_badge.pack()
        self.confirm_badge.bind("<Button-1>", lambda e: self.edit_titles())
        th.Tooltip(self.confirm_badge, lambda: _("Titles the disc's menus named that you haven't confirmed yet: "
                                               "click to check them in Edit titles"))
        # what each ticked title is, when the titles are films of their own (a disc of short films)
        # or a bonus disc's features: poster and name, for the titles being ripped only
        self.films_box = ttk.Frame(rc, style="Card.TFrame")
        self.strip = ttk.Frame(self.films_box, style="Card.TFrame")
        self.strip.pack(anchor="w")
        self._film_imgs = []
        lab = self._path_row = ttk.Frame(rc, style="Card.TFrame")
        lab.pack(fill="x", pady=(8, 2))
        ttk.Label(lab, text=_("TARGET PATH"), style="CardCap.TLabel").pack(side="left")
        th.link(lab, _("Change folder"), self.pick_out, style="CardLink.TLabel").pack(side="right")
        pathbox = th.frame(rc, th.VELLUM_50, border=th.BRASS_300, pad=2, radius=4, outer=th.CARD)
        pathbox.pack(fill="x")
        self.dest_full = ""
        ttk.Label(pathbox, image=th.icon("logs"), style="Path.TLabel").pack(side="left", padx=(4, 0))
        self.dest_lbl = ttk.Label(pathbox, text="", style="Path.TLabel", anchor="w", width=1)
        self.dest_lbl.pack(side="left", fill="x", expand=True, padx=(6, 4))
        self.dest_lbl.bind("<Configure>", lambda ev: self._fit_dest())
        # the whole path, when the box had to cut it short
        self.dest_tip = th.Tooltip(self.dest_lbl, lambda: self.dest_full if self.dest_lbl.cget("text") != self.dest_full else "")
        minis = ttk.Frame(rc, style="Card.TFrame")
        minis.pack(fill="x", pady=(8, 0))
        minis.columnconfigure(0, weight=1, uniform="m")
        minis.columnconfigure(1, weight=1, uniform="m")
        a = th.frame(minis, th.VELLUM_100, border=th.BRASS_300, pad=5, radius=5, outer=th.CARD)
        a.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        ttk.Label(a, text=_("AUDIO"), style="InsetCap.TLabel").pack(anchor="w")
        self.audio_lbl = ttk.Label(a, text="", style="InsetVal.TLabel", wraplength=190, justify="left")
        self.audio_lbl.pack(anchor="w", pady=(2, 0))
        a.bind("<Configure>", lambda ev: self.audio_lbl.config(wraplength=th.real(max(th.px(120), ev.width - th.px(20)))))
        e = th.frame(minis, th.VELLUM_100, border=th.BRASS_300, pad=5, radius=5, outer=th.CARD)
        e.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        etop = ttk.Frame(e, style="Inset.TFrame")
        etop.pack(fill="x")
        ttk.Label(etop, text=_("EXTRACTION ENGINE"), style="InsetCap.TLabel").pack(side="left")
        self.engine_lbl = ttk.Label(e, text="", style="InsetOk.TLabel")
        self.engine_lbl.pack(anchor="w", pady=(2, 0))
        # VLC, for Detect titles (shown only while that's switched on in Settings)
        self.vlc_lbl = ttk.Label(e, text="", style="InsetOk.TLabel")
        th.rule(rc, th.BRASS_200).pack(fill="x", pady=(9, 7))
        bottom = ttk.Frame(rc, style="Card.TFrame")
        bottom.pack(fill="x")
        # the lab's report (MakeMKV's reading of the disc, made in the background once you've
        # confirmed it): its own light, breathing while the lab works
        self.bg_box = ttk.Frame(bottom, style="Card.TFrame")
        self.bg_dot = th.dot(self.bg_box, th.CARD, th.BRASS_400)
        self.bg_dot.pack(side="left", padx=(0, 6))
        self.bg_lbl = ttk.Label(self.bg_box, text="", style="CardSub.TLabel")
        self.bg_lbl.pack(side="left")
        self.rip_note = ttk.Label(bottom, text="", style="CardSub.TLabel")
        self.rip_note.pack(side="left")
        self.rip_btn = SplitButton(bottom, " " + _(self.RIP_TEXT), self.rip_or_cancel, "Rip.TButton", "RipArrow.TButton",
                                   frame_style="Card.TFrame", image=th.button_image("rip"))
        self.rip_btn.pack(side="right")
        self.rip_btn.set_state(False)
        self._build_rip_menu()
        self.root.bind("<Configure>", self._rewrap)
        # the empty desk's faint art (a magnifier, bottom right): before a case is open, sized by
        # the window alone; Tk can't see through a widget, so where something on the desk reaches
        # down into it (a short window), it's cut off below that, as if lying under it
        self._mark = tk.Label(root, bd=0, highlightthickness=0, padx=0, pady=0, anchor="s",
                              background=th.PARCHMENT)
        self._mark_img, self._marks_job = None, None
        self._desk_parts = (self.case_lbl, name_row, facts, self.status_strip, self.details_card,
                            self.rip_card, self.pic_col)
        for w in self._desk_parts + (self.desk_scroll,):
            for ev in ("<Configure>", "<Map>", "<Unmap>"):
                w.bind(ev, lambda e: self._marks_later(), add="+")

    # the art's size: up to this tall (design px) and most of the desk's height; none on a desk too
    # small for MARK_MIN, or when less than MARK_PEEK of it shows below what's over it
    MARK_MAX, MARK_MIN, MARK_PEEK = 460, 150, 40

    def _marks_later(self):
        if self._marks_job is None:
            self._marks_job = self.root.after(60, self._place_mark)

    def _place_mark(self):
        self._marks_job = None
        ds = self.desk_scroll
        if not ds.winfo_ismapped():
            return
        w, h = ds.winfo_width(), ds.winfo_height()
        size = min(round(0.8 * h), th.px(self.MARK_MAX), round(0.5 * w))
        if self.rep is not None or ds.scrolls() or size < th.px(self.MARK_MIN):
            self._mark.place_forget()
            return
        if self._mark_img is None or self._mark_img.height() != size:
            self._mark_img = th.watermark("right", size)
            if self._mark_img is None:
                return
        iw = self._mark_img.width()
        # its top: below anything on the desk over its corner (the status strip, in a short window)
        top = max([p.winfo_rooty() - ds.winfo_rooty() + p.winfo_height() for p in self._desk_parts
                   if p.winfo_ismapped() and p.winfo_rootx() + p.winfo_width() > ds.winfo_rootx() + w - iw]
                  + [h - size])
        if h - top < th.px(self.MARK_PEEK):
            self._mark.place_forget()
            return
        self._mark.configure(image=self._mark_img, width=iw, height=h - top)     # (its foot shown: anchor "s")
        self._mark.place(x=ds.winfo_rootx() - self.root.winfo_rootx() + w - iw,
                         y=ds.winfo_rooty() - self.root.winfo_rooty() + top)

    def _rewrap(self, event=None):
        if event is None or event.widget is self.root or event.widget is self.info:
            width = self.root.winfo_width() if event is None or event.widget is self.info else event.width
            # (the picture beside it as soon as it's packed: drawn a moment later, its width the same)
            w = max(th.px(300), width - th.px(100) - (self.pic_col.winfo_reqwidth() + th.px(26)
                                                      if self.pic_col.winfo_manager() else 0))
            # the title: as wide as its column is, laid out (the desk is as wide as the window shows
            # it), the year beside it; before that, as the window's width says
            col = self.info.winfo_width()
            room = col - self.year_lbl.winfo_reqwidth() - th.px(18) if col > th.px(200) else w - th.px(90)
            self.title_lbl.config(wraplength=th.real(max(th.px(240), room)))
            self.plan_lbl.config(wraplength=th.real(max(th.px(200), w - th.px(190))))

    def set_dest(self, path):
        self.dest_full = path
        self._fit_dest()

    def _fit_dest(self):
        fit_label(self.dest_lbl, self.dest_full, "Path.TLabel")

    def set_facts(self, kind, middle, source, guess=False):
        """The line under the title: KIND • facts • who identified it (empty parts left out)."""
        for w in (self.kind_lbl, self.facts_lbl, self.source_lbl, *self._fact_dots):
            w.pack_forget()
        parts = [(self.kind_lbl, kind.upper()), (self.facts_lbl, middle), (self.source_lbl, source)]
        shown = [(w, t) for w, t in parts if t]
        for k, (w, t) in enumerate(shown):
            if k:
                self._fact_dots[k - 1].pack(side="left", padx=7)
            w.config(text=t)
            w.pack(side="left")
        self.source_lbl.config(style="Guess.TLabel" if guess else "Source.TLabel")

    def drive_popup(self):
        drives = optical_drives()                     # (which drives there are: asks none of them)
        self._build_scan_menu(drives)
        self.check_drives(drives)                     # fresh: a disc may have gone in since
        b = self.drive_btn
        try:
            self.drive_menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            self.drive_menu.grab_release()

    def _build_scan_menu(self, drives):
        m = self.drive_menu
        m.delete(0, "end")
        # a drive being read (scan, stakeout, rip, the lab) isn't asked its label: the scan's will do
        in_use = self.drive_in_use()
        for d in drives:
            state, vol = self._drive_state.get(d, (None, None))
            if d == in_use:
                state, vol = "ready", (self.rep or {}).get("label")
            label = "%s   %s" % (d, vol) if vol else _("{drive}   Drive not ready", drive=d) if state == "busy" else d
            m.add_radiobutton(label=label, value=d, variable=self.source, command=self.source_changed,
                              state="disabled" if state == "busy" else "normal")
        if not drives:
            m.add_command(label=_("No disc drives found"), state="disabled")
        cur = self.source.get()
        if cur and cur not in drives:
            m.add_radiobutton(label=_("Folder: {folder}", folder=short_path(cur, 60)), value=cur, variable=self.source,
                              command=self.source_changed)
        m.add_separator()
        m.add_command(label=_("Choose folder…"), command=self.pick_source)
        m.add_command(label=_("Refresh drives"), command=self.refresh_drives)

    def about(self):
        if getattr(self, "about_win", None) is not None and self.about_win.top.winfo_exists():
            self.about_win.top.lift()
            return
        self.about_win = AboutDialog(self.root, self)

    def show_update(self):
        """The About link as a newer version's known (di.known_update) or not: its upgrade icon, highlighted."""
        self.update = di.known_update()
        self.about_link.configure(image=th.icon("upgrade" if self.update else "about"),
                                  style="FootLink%s.TLabel" % ("New" if self.update else ""))

    def _build_rip_menu(self):
        m = self.rip_btn.menu
        m.delete(0, "end")
        m.add_command(label=_("Rip to: {folder}…", folder=short_path(self.outdir.get())), command=self.pick_out)
        if not (self.rep or {}).get("extras_disc"):     # a bonus disc rips its extras already
            m.add_checkbutton(label=_("Include extras"), variable=self.extras, command=self.options_changed)
        m.add_command(label=_("Choose titles…"), command=self.choose_titles,
                      state="normal" if self.rep else "disabled")
        m.add_command(label=_("Edit titles…"), command=self.edit_titles,
                      state="normal" if self.rep else "disabled")
        if self.selected is not None:
            m.add_command(label=_("Back to default titles"), command=self.reset_titles)
        m.add_separator()
        m.add_command(label=_("Open rip folder"), command=self.open_out)
        if self.makemkv_app():
            # (not while the drive's in use: MakeMKV's window starts reading the disc as it opens)
            m.add_command(label=_("Open MakeMKV"), command=self.open_makemkv,
                          state="disabled" if self.busy or self.mkv_scan else "normal")

    # ---- thread plumbing: workers post callables, the Tk thread runs them ----
    def post(self, fn, *a):
        self.q.put((fn, a))

    def _poll(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                fn(*a)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def run_bg(self, target, *a):
        threading.Thread(target=target, args=a, daemon=True).start()

    # ---- small helpers ----
    def set_case(self, state):
        """The footer's standing of the case: "none", "open" (investigating, or unsolved) or "closed"."""
        text, colour = {"none": (_("No case open"), th.INK_SUBTLE), "open": (_("Open case"), th.WAX),
                        "closed": (_("Case closed"), th.GREEN_DOT)}[state]
        self.standing_full = text
        self._fit_standing()
        th.set_dot(self.case_dot, colour)

    def set_outcome(self, text, colour):
        """The last action's result in the footer, where the case's standing shows (green: it
        worked, red: it failed); the passing status strip folds away, its work done."""
        self.standing_full = text
        self._fit_standing()
        th.set_dot(self.case_dot, colour)
        self.set_status("")

    def set_background(self, text, colour=None):
        """The rip card's lab report light: text with a breathing brown light while the lab works
        (colour None), a steady colour once it's over (green: done); no text, gone."""
        if not text:
            th.stop_pulse(self.bg_dot)
            self.bg_box.pack_forget()
            return
        self.bg_lbl.config(text=text)
        if colour is None:
            th.pulse_dot(self.bg_dot, th.BRASS_400, th.CARD)
        else:
            th.set_dot(self.bg_dot, colour)
        if not self.bg_box.winfo_manager():
            self.bg_box.pack(side="left", padx=(0, 14), before=self.rip_note)

    def _fit_standing(self):
        fit_label(self.standing_lbl, self.standing_full, "FootStatus.TLabel")

    # the status line's light, by what a message is (not by its wording, which a translation changes)
    TONES = {"good": th.GREEN_DOT, "warn": th.AMBER_DOT, "bad": th.WAX, "info": th.BRASS_400}

    def set_status(self, text, tone="info"):
        """A passing message (a step of the investigation, what to do next, a rip's progress) on
        the strip just above the rip card (under the title while there's none); the strip folds
        away when there's nothing to say. tone: its light ("good", "warn", "bad", "info"), said by
        whoever says it, never read from its words (a translation changes them)."""
        self._status_tone = tone
        v = self.titles_dialog
        if self.detecting and v is not None and v.alive():
            v.show_status(text)               # Detect titles: its news shows where it was asked for
            return
        self.status.config(text=text)
        showing = bool(self.status_strip.winfo_manager())
        if text or self.progress.winfo_manager():
            anchor = self.rip_card if self.rip_card.winfo_manager() else self._facts_row
            row = self.status_strip.master.pack_slaves()
            # right above it (rows packed before the rip card since, like the match row, go above)
            if not showing or row.index(self.status_strip) + 1 != row.index(anchor):
                self.status_strip.pack(fill="x", pady=(4, 4) if anchor is self._facts_row else (10, 0), before=anchor)
        elif showing:
            self.status_strip.pack_forget()
        colour = self.TONES[tone]
        if colour == th.BRASS_400 and self.progress.winfo_manager():
            th.pulse_dot(self.status_dot, colour, th.VELLUM_200)       # under way: it breathes
        else:
            th.set_dot(self.status_dot, colour)

    def tell(self, item, tone="info"):
        """Queue a status line, with its tone (or a callable, run when its turn comes: the scan's result)."""
        self._story.append(item if callable(item) else (item, tone))
        if self._story_job is None:
            self._story_next()

    def _story_next(self):
        self._story_job = None
        if not self._story:
            return
        item = self._story.popleft()
        if callable(item):
            item()
            self._story_next()
            return
        self.set_status(*item)
        self._story_job = self.root.after(self.MIN_STATUS_MS, self._story_next)

    def story_reset(self):
        self._story.clear()
        if self._story_job is not None:
            self.root.after_cancel(self._story_job)
            self._story_job = None

    # ---- case notes: the technical log of this session ----
    def add_note(self, line):
        self._notes.append(line)
        if self.notes_win is not None:
            self.notes_win.append(line)

    def open_notes(self):
        if self.notes_win is not None and self.notes_win.top.winfo_exists():
            self.notes_win.top.deiconify()
            self.notes_win.top.lift()
            return
        self.notes_win = NotesWindow(self.root, self)

    def show_progress(self, on, indeterminate=False):
        if on:
            self.progress.pack(fill="x", pady=(4, 5))
            self.set_status(self.status.cget("text"), getattr(self, "_status_tone", "info"))      # make sure the strip is showing
            self.progress.config(mode="indeterminate" if indeterminate else "determinate", maximum=1000, value=0)
            if indeterminate:
                self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.status_pct.config(text="")
            self.set_status(self.status.cget("text"), getattr(self, "_status_tone", "info"))      # folds away if nothing's left to say

    def set_busy(self, busy, ripping=False):
        self.busy, self.ripping = busy, ripping
        if busy:                                # (the drive's needed: a title playing in VLC is closed)
            from . import menus
            menus.stop_watching()
        self.update_scan_btn()
        self.drive_btn.config(state="disabled" if busy else "normal")
        self.show_drive()                       # (its light: gold, breathing, while it's read)
        if ripping:
            self.rip_btn.set_text(_("Cancel"))
            self.rip_btn.set_state(True, arrow_enabled=False)
        else:
            self.rip_btn.set_text(" " + _(self.RIP_TEXT))
            # no MakeMKV: Rip is greyed out, but ▾ still opens Choose titles and the rip options
            self.rip_btn.set_state(bool(self.rep) and not busy and bool(self.makemkv_exe()),
                                   arrow_enabled=bool(self.rep) and not busy)

    def remember(self):
        self.settings.update(drive=self.source.get(), outdir=self.outdir.get(), extras=self.extras.get())
        save_settings(self.settings)

    def source_name(self):
        src = self.source.get()
        return src if src else _("no drive selected")

    # ---- source selection (Scan menu) ----
    def refresh_drives(self, initial=False):
        drives = self._drives = optical_drives()
        cur = self.source.get()
        if drives and (not cur or (cur not in drives and not os.path.isdir(cur))):
            self.source.set(drives[0])
        self._build_scan_menu(drives)
        self.show_drive()
        self.update_scan_btn()             # (a drive: off till it's said it has a disc)
        if initial or not self.rep:
            self.set_status("" if self.source.get() else _("No drive selected: pick one from Source"))
        self.check_drives(drives)

    DRIVE_WAIT = 3.0          # seconds a drive has to answer before it's "not ready"
    DRIVE_RECHECK = 5000      # ms between asking a drive that wasn't ready again

    def drive_in_use(self):
        """The drive the app itself is reading (a scan, stakeout, rip, the lab): never asked."""
        return self.source.get() if (self.busy or getattr(self, "mkv_scan", None)) else None

    def update_scan_btn(self):
        """Scan on only for a source that's ready (scan_ready), the desk not busy."""
        self.scan_btn.config(state="disabled" if getattr(self, "busy", False) or not self.scan_ready() else "normal")

    def scan_ready(self):
        """Whether Scan can go: a source picked; a drive that has said it has a disc, ready (not
        before it's answered: at startup, Scan waits for its word); a folder that's there. (While
        the app reads the drive itself, the busy desk has Scan off already.)"""
        src = self.source.get()
        if not src:
            return False
        if src in self._drives:
            return self._drive_state.get(src, (None,))[0] == "ready" or src == self.drive_in_use()
        return os.path.isdir(src)

    def drive_busy(self, d=None):
        d = self.source.get() if d is None else d
        return d != self.drive_in_use() and self._drive_state.get(d, (None,))[0] == "busy"

    def check_drives(self, drives):
        """Ask each drive whether it has a disc and what it's called, in the background: one that
        doesn't answer in DRIVE_WAIT (a USB drive stuck after a read: Windows lists it, but any
        question waits on it) is "not ready": greyed out, Scan with it, until it answers."""
        skip = self.drive_in_use()
        ask = [d for d in drives if d != skip]

        def work():
            found = {d: self._probe(d) for d in ask}
            self.post(lambda: self._drives_checked(found))
        threading.Thread(target=work, daemon=True).start()

    def _probe(self, d):
        """One drive's answer, waited on DRIVE_WAIT at most (off the Tk thread): (state, label,
        serial). A drive still stuck on the last question isn't asked another: that one's waited
        on again."""
        t = self._probes.get(d)
        if t is None or not t.is_alive():
            box = {}

            def ask():
                box["has"] = os.path.isdir(d)
                box["label"] = di.volume_label(d) if box["has"] else None
                box["serial"] = None
            t = threading.Thread(target=ask, daemon=True)
            t.box = box
            self._probes[d] = t
            t.start()
        t.join(self.DRIVE_WAIT)
        if t.is_alive():
            return ("busy", None, None)
        if not t.box.get("has"):
            return ("empty", None, None)
        return ("ready", t.box.get("label"), t.box.get("serial"))

    def ejected(self, path):
        """Whether a disc drive's disc is out, asked on the Tk thread (the scan's answer about to show): a drive just
        read answers at once, its disc spun up; a folder, or no drive, isn't one."""
        if not path:
            return False
        key = os.path.normcase(path.rstrip("\\/"))
        drives = [d for d in self._drives if os.path.normcase(d.rstrip("\\/")) == key]
        return bool(drives) and not os.path.isdir(drives[0])

    def disc_gone(self, drive):
        """Whether a drive's disc has been taken out (off the Tk thread: asks it, DRIVE_WAIT at
        most): what a scan, a stakeout or a rip that failed says, if so."""
        return drive in self._drives and self._probe(drive)[0] == "empty"

    def _drives_checked(self, found):
        self._watch_asking = False
        serials = {d: s[2] for d, s in found.items()}
        found = {d: s[:2] for d, s in found.items()}
        changed = any(self._drive_state.get(d) != s for d, s in found.items())
        self._drive_state.update(found)
        if changed:
            self._build_scan_menu(self._drives or list(found))
            self.show_drive()
            self.update_scan_btn()
        for d, (state, _x) in found.items():
            self._disc_check(d, state, serials.get(d))
        if any(s[0] == "busy" for s in found.values()) and self._drive_recheck is None:
            def again():
                self._drive_recheck = None
                self.check_drives(optical_drives())
            self._drive_recheck = self.root.after(self.DRIVE_RECHECK, again)

    DRIVE_WATCH = 3000        # ms between asking the source drive again (its disc taken out or changed)

    def watch_drive(self):
        """The source drive asked again every DRIVE_WATCH (not while the app reads it): a disc taken
        out or put in shows on its light, and the case on the desk goes with its disc
        (_disc_check). Drives plugged in or out show in the Source menu."""
        self.root.after(self.DRIVE_WATCH, self.watch_drive)
        drives = optical_drives()
        if drives != self._drives:
            self._drives = drives
            self._build_scan_menu(drives)
            self.show_drive()
        src = self.source.get()
        if src in drives and src != self.drive_in_use() and not self._watch_asking:
            self._watch_asking = True
            self.check_drives([src])

    def _disc_check(self, d, state, serial):
        """The case on the desk, its drive asked again: its disc taken out, or another in its place
        (another serial number: discs of one label told apart), and the desk is cleared, so
        nothing is done to the next disc with this one's answers."""
        rep = self.rep
        if not rep or self.busy or d == self.drive_in_use() or \
                os.path.normcase((rep.get("path") or "").rstrip("\\/")) != os.path.normcase(d.rstrip("\\/")):
            return
        if state == "empty":
            self.desk_cleared("")                 # (taken out when done with: nothing to say)

    OTHER_DISC = N_("A different disc is in the drive: Scan it")

    def gone_check(self, drive):
        """A scan step's progress (menus.waits_told's, off the Tk thread): the drive asked every
        DISC_CHECK whether its disc is still in; taken out, the scan ends there (SystemExit: "removed")."""
        asked = [time.time()]

        def check(stage=None, *a):
            if time.time() - asked[0] >= self.DISC_CHECK:
                asked[0] = time.time()
                if self.disc_gone(drive):
                    raise SystemExit("the disc was removed")
        return check

    def still_the_disc(self, rep):
        """Before a command that uses the case's disc (a rip, Detect titles; off the Tk thread): its
        fingerprint taken again (core.same_disc, quick). Another disc in the drive, or none: the desk
        is cleared and the command doesn't run (False). Nothing kept about a disc but its fingerprint."""
        path, h = rep.get("path"), rep.get("content_hash")
        if not path or not h or di.same_disc(path, h):
            return True
        gone = self.disc_gone(path)
        di.logger.info((i18n.log("{path}: not the disc scanned (none in the drive)", path=path) if gone else
                        i18n.log("{path}: not the disc scanned (another disc)", path=path)))
        self.post(self.wrong_disc, gone)
        return False

    def wrong_disc(self, gone):
        """A command stopped before it began, its disc not the case's: back to the blank desk."""
        self.show_progress(False)
        self.set_busy(False)
        self.detecting = False
        th.set_dot(self.rip_dot, th.INK_SUBTLE)
        self.desk_cleared("" if gone else _(self.OTHER_DISC))

    def desk_cleared(self, why):
        """The case taken off the desk (its disc gone): back to the blank desk, saying why if
        there's something to say (another disc in its place), not for a disc taken out when done."""
        from . import menus
        self.lab_again.clear()           # (cleaned, maybe, when it's back)
        di.logger.info(i18n.log("{why} (case {disc} closed)", why=why or i18n.log("the disc was removed"), disc=(self.rep or {}).get("content_hash", "?")[:8]))
        v = self.titles_dialog
        if v is not None and v.alive():
            v.close()
        menus.stop_watching()
        self.rep, self.selected, self._before = None, None, None
        self._build_rip_menu()
        self.clear_case(_(self.EMPTY_TITLE), _(self.EMPTY_NOTE))
        self.set_case("none")
        self.show_drive()
        self.set_status(why, "warn" if why else "info")

    def pick_source(self):
        d = filedialog.askdirectory(title=_("Choose a disc or a full-disc backup folder"))
        if d:
            self.source.set(os.path.normpath(d))
            self.refresh_drives()
            self.source_changed()

    def source_changed(self):
        self.remember()
        if self.source.get() in self._drives:     # (a drive picked: asked, Scan as it answers)
            self.check_drives([self.source.get()])
        self.rep = None
        self.selected = None
        self.set_busy(False)
        self._build_rip_menu()
        self.clear_case(_(self.EMPTY_TITLE), _(self.EMPTY_NOTE))
        self.set_case("none")
        self.show_drive()
        self.set_status("")

    def clear_case(self, title, note=""):
        """The desk with no case on it: just a heading and a line under it."""
        self.clear_picture()
        self.show_match_button(None)
        self.show_details(None)
        self.case_lbl.config(text="")
        self.title_lbl.config(text=title)
        self.year_lbl.config(text="")
        self.set_facts("", note, "")
        self.plan_lbl.config(text="")
        self.set_dest("")
        self.rip_card.pack_forget()
        self.show_rip_films([])

    # ---- the drive and the disc in it (toolbar pills) ----
    def show_drive(self):
        src = self.source.get()
        model = self._drive_models.get(src.rstrip("\\/").upper()) if src else None
        where = src if len(src) <= 8 else short_path(src, 30)
        light = self.drive_light()
        text = "%s%s" % (where or _("no drive"), "  " + _("Drive not ready") if light == "busy" else ("  " + model) if model else "")
        font = tkfont.Font(font=ttk.Style().lookup("Pill.TButton", "font") or "TkDefaultFont")
        side = round(22 * max(1.0, float(self.root.tk.call("tk", "scaling")) / 1.3333))   # room for the dot / chevron
        self._pill = dict(text=text, width=font.measure(text) + 2 * side, height=font.metrics("linespace"))
        if light == "reading":
            self._pulse_drive()
        else:
            self._stop_drive_pulse()
            self._light_pill(self.DRIVE_LIGHTS[light])

    # the drive's light: red, not ready (don't wait on it); gold, breathing, the app is reading it
    # (don't eject it); green, a disc in it, ready and free; grey, no disc (or not asked yet)
    DRIVE_LIGHTS = {"busy": th.WAX, "ready": th.GREEN_DOT, "none": th.INK_SUBTLE}
    PULSE_SHADES, PULSE_MS = 12, 1600

    def drive_light(self):
        src = self.source.get()
        if not src:
            return "none"
        if src == self.drive_in_use():
            return "reading"
        if src in self._drives:          # a drive: as last asked (never asked here, on the Tk thread)
            state = self._drive_state.get(src, (None,))[0]
            return state if state in ("busy", "ready") else "none"
        return "ready" if os.path.isdir(src) else "none"      # a folder

    def _light_pill(self, colour):
        p = self._pill
        img = th.caret(th.BRASS_700, width=p["width"], height=p["height"], dot=colour)
        self.drive_btn.config(text=p["text"], image=(img, "disabled", img))

    def _pulse_drive(self):
        """The drive's gold light breathing while the app reads the disc (a few shades, each drawn once)."""
        if getattr(self, "_drive_pulse", None):
            return
        import math
        t0 = time.time()
        shades = [th._blend(th.AMBER_DOT, th.VELLUM_200, 0.5 * k / (self.PULSE_SHADES - 1))
                  for k in range(self.PULSE_SHADES)]

        def tick():
            if self.drive_light() != "reading":
                self._drive_pulse = None
                self.show_drive()
                return
            t = (1 - math.cos(2 * math.pi * ((time.time() - t0) * 1000 % self.PULSE_MS) / self.PULSE_MS)) / 2
            self._light_pill(shades[round(t * (self.PULSE_SHADES - 1))])
            self._drive_pulse = self.root.after(self.PULSE_MS // (2 * self.PULSE_SHADES), tick)
        tick()

    def _stop_drive_pulse(self):
        job = getattr(self, "_drive_pulse", None)
        if job:
            self.root.after_cancel(job)
        self._drive_pulse = None

    def find_drive_models(self):
        """Drive makes and models, looked up once in the background (asked of each drive), and
        kept in the settings: shown from the start next time, whatever the drives are doing."""
        def work():
            found = drive_models()
            self.post(lambda: self._drive_models_found(found))
        threading.Thread(target=work, daemon=True).start()

    def _drive_models_found(self, found):
        self._drive_models.update(found)
        if found and self.settings.get("drive_models") != self._drive_models:
            self.settings["drive_models"] = dict(self._drive_models)
            save_settings(self.settings)
        self.show_drive()

    # ---- settings ----
    def scan_args(self):
        """Identifier settings from the Settings dialog (a plain dict: safe to read off the Tk thread)."""
        st = self.settings
        return di.default_args(tmdb_key=st.get("tmdb_key") or None, gemini_key=st.get("gemini_key") or None,
                               episode_order=st.get("episode_order", "aired"))

    def makemkv_exe(self):
        exe = self.settings.get("makemkv")
        return exe if exe and os.path.isfile(exe) else di.find_makemkvcon()

    def makemkv_app(self):
        """MakeMKV's own window (beside makemkvcon): makemkv.exe; on a Mac the MakeMKV.app the
        command-line tool lives in; or None."""
        exe = self.makemkv_exe()
        if not exe:
            return None
        here = os.path.dirname(exe)
        if sys.platform == "darwin" and ".app/" in exe:
            return exe[:exe.index(".app/") + 4]
        for name in ("makemkv.exe", "makemkv"):
            path = os.path.join(here, name)
            if os.path.isfile(path):
                return path
        return shutil.which("makemkv")

    def open_makemkv(self):
        app = self.makemkv_app()
        if not app or self.busy or self.mkv_scan:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", app])
        else:
            subprocess.Popen([app], cwd=os.path.dirname(app) or None)

    def open_logs(self):
        path = di.log_dir()
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def open_settings(self):
        SettingsDialog(self.root, self)

    RESTART_WAIT = 30         # seconds a restarted app waits for the one before it to close

    def restart_to_install(self, keys):
        """Install these tools in a fresh copy of the app: they're asked for in its settings, it's started,
        and this one closes, letting go of their files (a library it loaded: VLC's, MakeMKV's). Agreed to
        already: the new copy installs them first thing, before loading anything. Not mid-scan or mid-rip."""
        if self.busy or getattr(self, "detecting", False):
            messagebox.showinfo(APP, _("Wait for the case at hand to finish, then try again."), parent=self.root)
            return False
        self.settings["install_on_start"] = {"tools": list(keys), "after": os.getpid()}
        save_settings(self.settings)
        di.logger.info(i18n.log("restarting to install {programs}", programs=", ".join(keys)))
        try:
            subprocess.Popen(relaunch_command(), cwd=os.getcwd(), creationflags=NO_WINDOW)
        except OSError as exc:
            self.settings.pop("install_on_start", None)
            save_settings(self.settings)
            messagebox.showerror(APP, _("Couldn't start {APP} again: {exc}", APP=APP, exc=exc), parent=self.root)
            return False
        self.root.after(100, self.root.destroy)
        return True

    def resume_installs(self, waited=0.0):
        """At startup: tools left to install by the copy before (restart_to_install), once it has closed:
        Settings opened at them, each installed in turn."""
        job = self.settings.get("install_on_start")
        if not job:
            return
        if tools.still_running(job.get("after")) and waited < self.RESTART_WAIT:
            self.root.after(500, lambda: self.resume_installs(waited + 0.5))
            return
        self.settings.pop("install_on_start", None)
        save_settings(self.settings)
        keys = [k for k in job.get("tools") or [] if k in tools.BY_KEY]
        if not keys:
            return
        d = SettingsDialog(self.root, self)
        d.tabs.select(len(d.tabs.tabs()) - 1)          # (Integrations, where its programs are)
        d.queue = keys
        d.next_install()

    # ---- disc data downloads (first run, Settings > Update now) ----
    def start_data_update(self, which, force, ui):
        """Run di.update_data in the background; `ui` gets progress()/message()/finished() on the Tk thread."""
        self.data_ui = ui
        self.set_busy(True)
        args = self.scan_args()

        def work():
            try:
                res = di.update_data(args, which, force=force,
                                     log=lambda m: (di.logger.info(i18n.for_log(m)), self.post(ui.message, i18n.for_ui(m))))
                di.logger.info(i18n.log("disc data updated: {result}", result=res))
                self.post(self._data_done, ui, res, None)
            except Exception as exc:
                di.logger.exception(i18n.log("disc data update failed"))
                self.post(self._data_done, ui, None, "%s: %s" % (type(exc).__name__, exc))
        threading.Thread(target=work, daemon=True).start()

    def _data_done(self, ui, res, err):
        self.data_ui = None
        self.set_busy(False)
        self.show_progress(False)
        ui.finished(res, err)
        self.set_status(_("Disc data updated") if not err else _("Disc data update failed: {err}", err=err), "bad" if err else "good")

    def _on_progress(self, label, done, total):
        ui = getattr(self, "data_ui", None)
        if ui is not None:
            ui.progress(label, done, total)
        elif total:                            # a scan fetching data on its own
            self.show_progress(True)
            self.progress.config(value=1000 * done / total)
            self.set_status(_("Downloading {label}: {done} of {total} MB", label=label, done=done // 2**20, total=total // 2**20))

    def maybe_first_run(self):
        args = self.scan_args()
        credits = di.imdb_credits_info(args.imdb_dir)
        missing = [k for k, info in (("discdb", di.discdb_index_info(args.discdb_dir or di.default_discdb_dir())),
                                     ("imdb", di.imdb_index_info(args.imdb_dir) and credits)) if not info]
        if missing and not self.settings.get("skip_first_run"):
            FirstRunDialog(self.root, self, missing)
        elif credits:
            self.refresh_credits(args)

    def refresh_credits(self, args):
        """IMDb's credits, behind the IMDb index a scan has refreshed: built again out of the way, the old
        ones used till it's done (1.1 GB read, so never in the middle of a scan). Tried again next time
        if it fails. The thread, or None if there's nothing to do."""
        from . import credits
        stale = di.imdb_credits_stale(args.imdb_dir)
        mpa = credits.mpa_stale(args.imdb_dir) and not getattr(args, "offline", False)   # (fetched: not offline)
        if not stale and not mpa:
            return None

        def work():
            if stale:
                try:
                    di.imdb_credits_build(args.imdb_dir, log=di.logger.info)
                except Exception:
                    di.logger.exception(i18n.log("refreshing IMDb's credits failed"))
            try:                             # (the MPA numbers, beside them: credits.mpa_refresh)
                if mpa:
                    credits.mpa_refresh(args.imdb_dir, log=di.logger.info)
            except Exception:
                di.logger.exception(i18n.log("refreshing the MPA certificate numbers failed"))
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    # ---- picture ----
    def _photo(self, path):
        try:
            img = th.scaled_photo(path)            # at the screen's size on a high-DPI screen
        except (tk.TclError, TypeError):
            return None
        self._imgs.append(img)
        return img

    # ---- choosing the match ----
    DETAIL_NAMES = {"IMDb": N_("IMDb Reference"), "TheDiscDB": N_("TheDiscDB Entry"), "Disc label": N_("Disc Volume Label"),
                    "Disc title": N_("Disc Title"), "Mastered": N_("Mastering Date"), "Fingerprint": N_("Disc Fingerprint"),
                    "Box set": N_("Box Set"), "Protection": N_("Protection"), "Episodes": N_("Episodes")}

    def show_details(self, rep):
        rows = detail_rows(rep) if rep else None
        if rows and rows == getattr(self, "_detail_rows", None) and self.details_card.winfo_manager():
            return                                # nothing on the card changed (labels saved, say)
        self._detail_rows = rows
        if not rep:
            for w in self.details.winfo_children():
                w.destroy()
            self.details_card.pack_forget()
            return
        # the new rows are built out of sight and swapped in whole: measuring them (the baselines)
        # flushes drawing, which would otherwise show the card half-built for a moment
        old, box = self.details, ttk.Frame(self.details_card, style="Card.TFrame", padding=(16, 8, 16, 4))
        box.columnconfigure(1, weight=1)
        for r, (k, v) in enumerate(rows):
            key = ttk.Label(box, text=_(self.DETAIL_NAMES.get(k, k)), style="CardKey.TLabel")
            if k == "IMDb":
                cell = th.link(box, v, lambda v=v: webbrowser.open("https://www.imdb.com/title/%s/" % v),
                               style="CardLink.TLabel", icon="external", right=True)
                text, lift = cell, 0
            elif k == "Fingerprint":
                cell = ttk.Frame(box, style="Card.TFrame")
                tag = th.frame(cell, th.VELLUM_200, border=th.BRASS_300, radius=4, outer=th.CARD)
                tag.pack(side="left")
                text = ttk.Label(tag, text=v, style="Badge.TLabel")
                text.pack()
                full = rep.get("content_hash", v)
                # the card shows its first 8 characters: the whole one on hover, and copied whole (it's
                # the whole one that names the disc on TheDiscDB and in your case files)
                copy = th.link(cell, "", lambda full=full: (self.root.clipboard_clear(), self.root.clipboard_append(full),
                                                            self.set_status(_("Fingerprint copied: {full}", full=full))),
                               style="CardLink.TLabel", icon="copy")
                copy.pack(side="left", padx=(8, 0))
                self.fingerprint_tips = (th.Tooltip(text, lambda full=full: full),
                                         th.Tooltip(copy, lambda: _("Copy the whole fingerprint")))
                tag.update_idletasks()
                lift = (tag.winfo_reqheight() - text.winfo_reqheight()) // 2   # the tag's border and padding
            else:
                cell = ttk.Label(box, text=v, style="CardMonoBold.TLabel" if k == "Disc label" else "CardMono.TLabel",
                                 wraplength=420, justify="left")
                text, lift = cell, 0
            # sit the label's text on the same baseline as the value's text
            gap = self._baseline(cell, text, lift) - self._baseline(key, key)
            key.grid(row=r, column=0, sticky="nw", padx=(0, 22), pady=(th.real(th.px(3) + max(0, gap)), 3))
            cell.grid(row=r, column=1, sticky="nw", pady=(th.real(th.px(3) + max(0, -gap)), 3))
        box.pack(fill="x", before=old)
        old.destroy()
        self.details = box
        self.details_card.pack(fill="x", anchor="w", **self._before_rip())

    @staticmethod
    def _baseline(cell, text, lift=0):
        """How far down `cell` the baseline of `text`'s (first line of) writing sits. `text` is a
        ttk label inside cell (or cell itself), centred in it with `lift` pixels of frame around it."""
        font = tkfont.Font(font=ttk.Style().lookup(str(text.cget("style")), "font") or "TkDefaultFont")
        text.update_idletasks()
        h = text.winfo_reqheight()
        lines = max(1, str(text.cget("text")).count("\n") + 1)
        if text.cget("wraplength") and str(text.cget("wraplength")) not in ("0", ""):
            lines = max(lines, round((h - 2) / font.metrics("linespace")))
        top = (h - font.metrics("linespace") * lines) / 2
        return lift + top + font.metrics("ascent")

    def show_match_button(self, rep):
        for w in (self.confirm_btn, self.confirmed_lbl, self.match_btn, self.match_note):
            w.pack_forget()
        if rep is None:
            return
        have = rep.get("match") or rep.get("personal_hit") or (rep.get("discdb") or {}).get("hit")
        if rep.get("personal_hit"):
            self.confirmed_lbl.config(text=_("In your case files"))
            self.confirmed_lbl.pack(side="left")
        elif (rep.get("discdb") or {}).get("hit"):
            # TheDiscDB knows this exact disc: nothing to confirm (Not right? still fixes a bad entry)
            self.confirmed_lbl.config(text=_("TheDiscDB"))
            self.confirmed_lbl.pack(side="left")
        elif self.confirmable(rep):
            sure = (rep.get("match") or {}).get("sure")
            self.match_note.config(text=_("Probable match") if sure else _("Best guess: check it"),
                                   style="Ok.TLabel" if sure else "Warn.TLabel")
            self.match_note.pack(side="left", padx=(0, 10))
            self.confirm_btn.pack(side="left")
        else:
            self.match_note.config(text=_("Not identified yet"), style="Warn.TLabel")
            self.match_note.pack(side="left")
        self.match_btn.config(text=_("Not right?") if have else _("Choose the match"))
        self.match_btn.pack(side="right")
        if not self.details_card.winfo_manager():
            self.details_card.pack(fill="x", anchor="w", **self._before_rip())

    def _before_rip(self):
        """Where a card goes above the rip card: above the status strip too, when that's sitting
        just over the rip card (re-placing the details card after Detect titles put it under
        the strip, moving everything about)."""
        if not self.rip_card.winfo_manager():
            return {}
        row = self.rip_card.master.pack_slaves()
        strip = self.status_strip
        if strip.winfo_manager() and strip in row and row.index(strip) + 1 == row.index(self.rip_card):
            return {"before": strip}
        return {"before": self.rip_card}

    def confirmable(self, rep):
        top = (rep.get("candidates") or [None])[0]
        if top and top.get("kind") == "collection":
            return bool(di.adopt_guess(rep.get("candidates")) and all(i.get("imdb_id") for i in top["items"]))
        return bool(di.adopt_guess(rep.get("candidates")) and top)

    def confirm(self):
        """✓ That's right: remember the current answer for this disc (instant next time)."""
        rep = self.rep
        if not rep or self.busy or not self.confirmable(rep):
            return
        cand, lp = rep["candidates"][0], rep.get("label_parsed") or {}
        if cand.get("kind") == "collection":
            truth = di.truth_for_collection(cand["name"], [(i["title_id"], i["imdb_id"]) for i in cand["items"]],
                                            collection_extras(self.rep, cand))
        elif cand.get("kind") == "tv" and not lp.get("season"):
            self.choose(cand, confirm=True)          # TV with no season on the label: ask for it
            return
        else:
            truth = di.truth_for(cand, lp.get("season") if cand.get("kind") == "tv" else None,
                                 lp.get("disc") if cand.get("kind") == "tv" else None)
        args = self.scan_args()
        try:
            di.set_truth(args.db, rep["content_hash"], truth)
        except Exception as exc:
            di.logger.exception(i18n.log("saving the answer failed"))
            messagebox.showerror(APP, _("Couldn't save the answer: {exc}", exc=exc))
            return
        # The answer is the one on screen: nothing to investigate again, just mark it as yours
        rep["personal_hit"] = {"truth": truth, "notes": "", "saved": None}
        rep["status"] = di.status(rep, args.tmdb_key)
        rep["best_guess"], rep["identified_by"] = di.best_guess(rep)
        self.scan_done(rep, None)        # (in your case files now: the lab starts reading it)
        self.set_outcome(_("Saved to your case files"), th.GREEN_DOT)

    def match_menu(self):
        rep = self.rep
        if not rep or self.busy:
            return
        m = self.match_popup
        m.delete(0, "end")
        self._menu_imgs = []
        cands = rep.get("candidates") or []
        sure = bool(di.adopt_guess(cands))           # a match or top guess is in use
        top = cands[0] if cands else None
        lp = rep.get("label_parsed") or {}
        if rep.get("personal_hit"):
            m.add_command(label=_("Your answer: {answer}", answer=di.strip_tags(rep["personal_hit"]["truth"])), state="disabled")
            m.add_command(label=_("Forget my answer and investigate again"), command=lambda: self.use_answer(None))
            m.add_separator()
        if rep.get("kind") == "collection" and top and top.get("kind") == "collection":
            m.add_command(label=_("It's one film or TV show instead…"), command=lambda: MatchDialog(self.root, self))
            m.add_separator()
        others = [c for c in cands if c.get("kind") != "collection" and (c is not top or not sure)]
        if others:
            m.add_command(label=_("Other matches:") if sure else _("Possible matches:"), state="disabled")
            kinds = {"movie": N_("film"), "tv": N_("TV series")}
            for c in others[:6]:
                img = self._menu_image(c)
                m.add_command(label="  " + _("{name} ({year}) · {kind}", name=c["name"], year=c.get("year") or "?", kind=_(kinds.get(c["kind"], c["kind"]))),
                              command=lambda c=c: self.choose(c), **({"image": img, "compound": "left"} if img else {}))
            m.add_separator()
        if rep.get("disc_images"):
            m.add_command(label=_("Ask an AI about the disc's picture…"), command=lambda: AskAIDialog(self.root, self))
        m.add_command(label=_("Enter it myself…"), command=lambda: MatchDialog(
            self.root, self, title=(top or {}).get("name") or (rep.get("best_guess") or "").split(" | ")[0],
            year=(top or {}).get("year"), tv=rep.get("kind") == "tv",
            season=lp.get("season"), disc=lp.get("disc"), multi=rep.get("kind") == "collection"))
        btn = self.match_btn
        try:
            m.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
        finally:
            m.grab_release()

    def _menu_image(self, cand):
        if not cand.get("thumb"):
            return None
        try:
            img = tk.PhotoImage(file=cand["thumb"])
        except tk.TclError:
            return None
        self._menu_imgs.append(img)         # Tk drops images nothing references
        return img

    def choose(self, cand, confirm=False):
        lp = (self.rep or {}).get("label_parsed") or {}
        if cand.get("kind") == "collection":
            self.use_answer(di.truth_for_collection(cand["name"], [(i["title_id"], i["imdb_id"]) for i in cand["items"]],
                                                    collection_extras(self.rep, cand)))
            return
        if cand.get("kind") == "tv" and not (lp.get("season") and confirm):
            MatchDialog(self.root, self, title=cand["name"], year=cand.get("year"), tv=True,
                        season=lp.get("season"), disc=lp.get("disc"), picked=cand)   # TV needs season/disc
            return
        self.use_answer(di.truth_for(cand, lp.get("season") if cand.get("kind") == "tv" else None,
                                     lp.get("disc") if cand.get("kind") == "tv" else None))

    def use_answer(self, truth):
        """Save the answer for this disc (or forget it) and investigate again with it."""
        args = self.scan_args()
        try:
            di.set_truth(args.db, self.rep["content_hash"], truth)
        except Exception as exc:
            di.logger.exception(i18n.log("saving the answer failed"))
            messagebox.showerror(APP, _("Couldn't save the answer: {exc}", exc=exc))
            return
        h = self.rep["content_hash"]
        self.scan()
        self._answer_saved = h if truth else None      # corrected: the footer says it's saved, once rescanned

    def picture_menu(self, event=None):
        m = self.pic_menu
        m.delete(0, "end")
        if self.pic_path:
            m.add_command(label=_("Copy picture"), command=lambda: copy_image(self.pic_path) or
                          self.set_status(_("Couldn't copy the picture here: use Save picture…"), "bad"))
            m.add_command(label=_("Save picture…"), command=self.save_picture)
        if (self.rep or {}).get("disc_images"):
            m.add_separator()
            m.add_command(label=_("Ask an AI about the disc's picture…"), command=lambda: AskAIDialog(self.root, self))
        if m.index("end") is None:
            return
        try:
            m.tk_popup(event.x_root, event.y_root) if event else m.tk_popup(*self.root.winfo_pointerxy())
        finally:
            m.grab_release()

    def save_picture(self):
        dest = filedialog.asksaveasfilename(title=_("Save the picture"), defaultextension=".png",
                                            initialfile="picture.png", filetypes=[(_("PNG image"), "*.png")])
        if dest and self.pic_path:
            shutil.copyfile(self.pic_path, dest)

    def clear_picture(self):
        self.pic_path = None
        self.pic_col.pack_forget()
        self.pic_lbl.config(image="", text="")
        self.pic_cap.config(text="")
        self._imgs = []
        self._rewrap()

    def show_picture(self, rep):
        self.clear_picture()
        art = rep.get("art") or {}
        img = self._photo(art["image"]) if art.get("image") else None
        if img:
            self.pic_lbl.config(image=img, text="", padding=0)
            full = next((d["full"] for d in rep.get("disc_images") or [] if art.get("source") == "disc"), None)
            self.pic_path = full or art["image"]
            self.pic_cap.config(text=picture_source(rep, art), wraplength=th.real(img.width() + th.px(10)), justify="center")
            self.pic_col.pack(side="left", anchor="n", padx=(0, 26), before=self.info)
        elif rep.get("art_note"):            # tell the user why there's no picture
            self.pic_lbl.config(text=rep["art_note"], image="", wraplength=170, justify="left", padding=10)
            self.pic_cap.config(text=_("NO PICTURE ON FILE"))
            self.pic_col.pack(side="left", anchor="n", padx=(0, 26), before=self.info)
        self._rewrap()
        # (and once the picture's laid out, its size known: a long name, Mexican Hayride & Bud
        # Abbott Lou Costello Meet the Killer Boris Karloff, wrapped short of it, not under it)
        self.root.after_idle(self._rewrap)

    # ---- rip options (Rip menu) ----
    def pick_out(self):
        d = filedialog.askdirectory(title=_("Rip to"), initialdir=self.outdir.get())
        if d:
            self.outdir.set(os.path.normpath(d))
            self.options_changed()

    def options_changed(self):
        self.remember()
        self._build_rip_menu()
        if self.rep and not self.busy:
            self.show_plan_summary()

    def open_out(self):
        path = self.last_out or self.outdir.get()
        if not os.path.isdir(path):
            path = self.outdir.get()
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def reset_titles(self):
        self.selected = None
        self.options_changed()

    def choose_titles(self):
        """Rip ▾ > Choose titles: what to rip."""
        if self.rep:
            TitlePicker(self.root, self)

    def edit_titles(self):
        """Rip ▾ > Edit titles: what each title is (and Detect titles). One at a time: open
        already, it comes to the front."""
        v = self.titles_dialog
        if v is not None and v.alive():
            v.top.lift()
            v.top.focus_force()
        elif self.rep:
            TitlesDialog(self.root, self, self.rep["content_hash"], display_title(self.rep), rep=self.rep)

    def labels_changed(self, h):
        """Labels saved for a disc: if it's the one on the desk, its rip plan follows them now."""
        if self.rep and self.rep.get("content_hash") == h:
            con = di.library_open(self.scan_args().db)
            try:
                self.rep["labels"] = di.get_labels(con, h)
            finally:
                con.close()
            self.relabel()
            self.show_waiting()

    def relabel(self):
        """The disc's titles follow its labels (yours, then Detect titles') again: details, plan."""
        di.apply_labels(self.rep.get("titles") or [], all_labels(self.rep))
        di.labels_over_guess(self.rep)
        self.show_details(self.rep)
        self.selected = None
        self._build_rip_menu()
        self.show_plan_summary()

    # ---- Scan ----
    def scan(self):
        drive = self.source.get().strip()
        if self.drive_busy(drive):
            self.set_status(_("The drive isn't ready yet: give it a moment"))
            return
        state = self._drive_state.get(drive, (None,))[0]
        if drive in self._drives and state != "ready":
            self.set_status(_("No disc in the drive") if state == "empty" else
                            _("The drive hasn't answered yet: give it a moment"), "warn")
            return
        # (a drive isn't asked here, on the Tk thread: a stuck one would freeze the window. No disc
        # in it: the scan says so)
        if drive not in self._drives and (not drive or not os.path.isdir(drive)):
            messagebox.showwarning(APP, _("No disc found at {drive}.\n\nInsert a disc, or pick another drive or a "
                                        "folder from the Source menu (top right).", drive=self.source_name()))
            return
        self.remember()
        # the lab still reading this drive: it steps aside (one thing at a time on a drive); the
        # scan starts once MakeMKV's let go and the drive's settled
        lab = self.stop_prescan() if self.mkv_scan and self.mkv_scan["drive"] == drive else None
        self.set_background(None)    # a new case: the last one's lab report goes with it
        if self.rep is not None:     # (the same disc again: what its menus said carries over, scan_done)
            self._before = self.rep
        self.rep = None
        self.selected = None
        self._build_rip_menu()
        self.clear_case(_("Investigating…"))
        self.set_case("open")
        self.set_busy(True)
        self.show_progress(True, indeterminate=True)
        self.story_reset()
        if self.notes_win is None or not self.notes_win.top.winfo_exists():
            self._notes.clear()      # case notes closed: opening them later shows just this scan
        di.logger.info("%s %s ----", SCAN_DIVIDER, drive)
        self.tell(_("Arriving at the scene…"))
        self.run_bg(self._scan_worker, drive, lab)

    def _scan_worker(self, drive, lab=None):
        try:
            self.wait_for_lab(lab)
            import tempfile
            args = self.scan_args()
            args.library = di.library_open(args.db)
            with tempfile.TemporaryDirectory(prefix="disc_detective_") as tmp:
                args.tmp_out = tmp
                rep = di.identify(drive, args)
                pmap = self._program_map(drive, rep)
                self._menu_style(rep)
                rep = self._menus_seen(drive, args, rep)
                self._files_saved(args, rep)
                self._program_seen(rep, pmap)
                self._bd_menus_seen(drive, args, rep)
            args.library.close()
            # (taken out once the disc's been read, a step left out for it: no answer shown for a disc that isn't in)
            if self.disc_gone(drive):
                raise SystemExit("the disc was removed")
            self.post(self.tell, lambda: self.scan_done(rep, None))   # once the story has been told
        except SystemExit as exc:
            msg = str(exc)               # (exc is gone once the except ends: the message is kept)
            gone = self.disc_gone(drive)
            # (a disc taken out isn't a problem to report: the log has it, errors.log doesn't)
            (di.logger.info if gone else di.logger.warning)(i18n.log("scan of {drive}: {error}", drive=drive, error=exc))
            self.post(self.tell, lambda: self.scan_done(None, msg, gone))
        except Exception as exc:   # show it rather than dying silently in a thread
            msg = "%s: %s" % (type(exc).__name__, exc)
            gone = self.disc_gone(drive)     # (the disc taken out mid-scan: said so, plainly)
            # (a read failing for its disc taken out, "the device is not ready": the log has it, errors.log doesn't)
            if gone:
                di.logger.info(i18n.log("scan of {drive}: the disc was removed ({msg})", drive=drive, msg=msg))
            else:
                di.logger.exception(i18n.log("scan of {drive} failed", drive=drive))
            self.post(self.tell, lambda: self.scan_done(None, msg, gone))

    def _program_map(self, drive, rep):
        """What a DVD's program says its menus do (menus.program_map), off the Tk thread: its menu
        data as kept (a few seconds' read off the disc the first time, VLC unlocking the drive,
        then kept), the program run in memory. None for a Blu-ray, or a disc whose program can't
        be read (the scan goes on without it)."""
        from . import menus
        if not rep or rep.get("format") != "DVD" or not rep.get("content_hash"):
            return None
        di.step("routes")
        t0 = time.time()
        try:
            vlc = menus.find_vlc(self.settings.get("vlc") or None)
            # (VLC's waits told: the disc taken out meanwhile ends the scan there, SystemExit, not minutes on)
            with menus.waits_told(self.gone_check(drive)):
                structure = menu_data(drive, rep["content_hash"], vlc, lambda m: di.logger.info(i18n.log("menus: {line}", line=m.strip())),
                                      oldest=2)
            pmap = menus.program_map(drive, structure) if structure else None
        except Exception as exc:          # (a nicety: never fail a scan over it)
            di.logger.info(i18n.log("the disc's program couldn't be read ({name}: {error})", name=type(exc).__name__, error=exc))
            return None
        if pmap:
            di.logger.info(i18n.log("the disc's program: {played} titles its buttons play, {chained} only a chain reaches, {first} before "
                           "the menu ({time:.1f} s)", played=len(pmap["plays"]), chained=len(pmap["chained"]), first=len(pmap["opening"]), time=time.time() - t0))
        return pmap

    def _bd_menus_seen(self, drive, args, rep):
        """A Blu-ray's menus as its own files name its titles (bdmenus: Sony's BD-J framework's
        menu project, Muppets from Space): the menus' word on each, waiting for you in Edit titles
        as Detect titles' is on a DVD. Read off the disc's files in a moment; no VLC, no Java."""
        from . import bdmenus
        if not rep or rep.get("format") != "Blu-ray" or rep.get("kind") == "tv":
            return
        try:
            entries, texts = bdmenus.menu_project(drive), None
            if not entries:               # (Warner's framework: its plain text files, its buttons' art read)
                from . import menus
                entries, texts = bdmenus.wb_film(drive, lambda image: " ".join(
                    t for t, _x, _x in (menus.rapid_lines(image) or [])))
            if not entries:               # (a menu template in plain text files: Spider-Man's)
                entries, texts = bdmenus.template_film(drive)
            if not entries:
                return
            film = naming_film(rep)
            con = di.imdb_open(getattr(args, "imdb_dir", None) or di.default_imdb_dir(), allow_download=False,
                               log=lambda m: None)
            try:
                labels = bdmenus.project_labels(rep.get("titles") or [], entries, film,
                                                lambda n: di.known_film(con, n),
                                                texts if texts is not None else bdmenus.menu_texts(drive, entries))
            finally:
                con.close()
        except Exception as exc:          # (a nicety: never fail a scan over it)
            di.logger.info(i18n.log("the disc's menu project couldn't be read ({name}: {error})", name=type(exc).__name__, error=exc))
            return
        by = {t["id"]: t for t in rep.get("titles") or []}
        before = {tid: (t.get("role"), t.get("note")) for tid, t in by.items()}
        pieces = bdmenus.project_pieces(rep.get("titles") or [], labels)
        for whole in set(pieces.values()):
            parts = [p for p, w in pieces.items() if w == whole]
            if all((labels.get(p) or {}).get("exact") for p in parts):
                # (each piece a button of its own: each its own title, the whole their Play All)
                labels.pop(whole, None)
                by[whole]["role"], by[whole]["note"] = Role.PLAY_ALL, "plays %s (its menu's buttons play each)" % ", ".join(parts)
                for p in parts:
                    del pieces[p]
        for piece, whole in pieces.items():
            # (a piece of another named title: not ripped twice, nor its label waiting for you)
            labels.pop(piece, None)
            by[piece]["role"] = Role.SEGMENT
            by[piece]["note"] = "a piece of %s (%s), which plays it whole" % (whole, (labels.get(whole) or {}).get("title"))
        for pid in bdmenus.project_play_alls(rep.get("titles") or [], entries):
            if by[pid].get("role") in (Role.EXTRA, Role.JUNK, None):
                by[pid]["role"], by[pid]["note"] = Role.PLAY_ALL, "the menus' Play All (its menu project says)"
        # (a name off its menu button, linked to its playlist through the project: known, not a
        # guess; saved as yours are, for TheDiscDB too. What rests on a judgement (an ad by its
        # frame rate, a project's own shorthand) waits for you in Edit titles)
        mine = dict(rep.get("labels") or {})
        saved = []
        con = getattr(args, "library", None)
        h = rep.get("content_hash")
        # (what the project made of the scan's titles, a Play All, a piece, in your case files too: the scan
        # recorded them before it was read. Help TheDiscDB's titles come from there)
        moved = [(t["role"], t.get("note"), h, tid) for tid, t in by.items() if (t.get("role"), t.get("note")) != before[tid]]
        if moved and con is not None and h:
            with con:
                con.executemany("UPDATE titles SET role=?, note=? WHERE hash=? AND title_id=?", moved)
        for tid, lab in sorted(labels.items()):
            if lab.get("exact") and tid not in mine and con is not None and h:
                di.set_label(con, h, tid, lab["type"], join_title(lab.get("title"), lab.get("description")))
                mine[tid] = {"type": lab["type"], "title": lab.get("title"), "season": None, "episode": None}
                if lab.get("description"):
                    mine[tid].update(description=lab["description"], text=join_title(lab.get("title"), lab["description"]))
                saved.append(tid)
        if saved:
            rep["labels"] = mine
            di.logger.info(i18n.log("the disc's menus name {count} of its titles exactly (their buttons' words): saved", count=len(saved)))
        waiting = {tid: lab for tid, lab in labels.items() if tid not in mine}
        if labels:
            rep["detected"], rep["bd_project"] = labels, len(entries)
            rep["staked_out"] = bool(waiting)          # (titles waiting for you, if any)
            di.logger.info(i18n.log("the disc's menu project names {count} of its titles ({entries} entries; {waiting} waiting for you)", count=len(labels), entries=len(entries), waiting=len(waiting)))

    def _program_seen(self, rep, pmap):
        """The program map on the scan (core.apply_program_map): the title each button plays the
        original of its copies, a play-all's chain whole."""
        if not rep or not pmap:
            return
        before = {t["id"]: t.get("role") for t in rep.get("titles") or []}
        for line in di.apply_program_map(rep, pmap) or []:
            di.logger.info(line)              # (the film put right, or a doubt: in the case notes)
        moved = [t["id"] for t in rep.get("titles") or [] if before.get(t["id"]) != t.get("role")]
        if moved:
            di.logger.info(i18n.log("the disc's program put these titles right: {titles}", titles=", ".join(moved)))

    def _menus_seen(self, drive, args, rep):
        """The disc's menus, as the scan uses them (off the Tk thread): a stakeout this session
        of the same disc, kept, its titles named for the film as it is now; else, as Settings say
        (Detect titles during a scan), a stakeout: its pictures read for the disc's name
        (_menus_witness, reading those rather than a first look) and its titles named; else the
        first look of _menus_witness alone."""
        from . import menus
        h = rep.get("content_hash")
        before = self._before if self._before is not None and self._before.get("content_hash") == h else None
        raw = self._stakeouts.get(h)
        if raw is None:                  # (a Blu-ray whose files list its menu: part of any scan, nothing played)
            raw = self._menu_files(rep)
            if raw is not None:
                self._stakeouts[h] = raw
        if raw is not None:              # staked out already this session: kept (Detect titles makes a new one)
            args.title_phrases = menus.feature_phrases(raw, menus.menu_dir(h), di.word_checker(getattr(args, "imdb_dir", None)))
            rep = self._menus_witness(drive, args, rep, staked=True)
            rep["menu_raw"] = raw
            rep["menu_found"] = self._name_titles(raw, rep)
            rep = self._settle_by_menus(drive, args, rep, raw)
            if before is not None and before.get("staked_out"):
                rep["staked_out"] = True          # (its titles still wait for you in Edit titles)
            return rep
        vlc = menus.find_vlc(self.settings.get("vlc") or None)
        if not self.stakeout_in_scan(rep, vlc):
            return self._menus_witness(drive, args, rep)
        di.step("stakeout")
        try:
            raw = self._stakeouts[h] = self._stakeout(rep, vlc)
        except menus.Cancelled:          # (its disc taken out: the scan ends there, as a disc gone mid-scan)
            if self.disc_gone(drive):
                raise SystemExit("the disc was removed")
            return self._menus_witness(drive, args, rep)
        except menus.Unsupported:        # (why, in the log: the scan goes on)
            self.post(self.tell, _(self.UNSUPPORTED_STAKEOUT))
            return self._menus_witness(drive, args, rep)
        except menus.Lost as exc:        # (no menu answered: Psych's bonus disc has none. Not an error)
            di.logger.info(i18n.log("no menu answered on this disc ({error}): the scan goes on without one", error=exc))
            return self._menus_witness(drive, args, rep)
        except Exception:
            di.logger.exception(i18n.log("reading the menus failed: the scan goes on without them"))
            return self._menus_witness(drive, args, rep)
        # each film's own menu, read apart (a disc of several films): what names each title
        args.title_phrases = menus.feature_phrases(raw, menus.menu_dir(h), di.word_checker(getattr(args, "imdb_dir", None)))
        rep = self._menus_witness(drive, args, rep, staked=True)   # (reads the stakeout's pictures, kept)
        rep["menu_raw"] = raw
        rep["menu_found"] = self._name_titles(raw, rep)
        rep = self._settle_by_menus(drive, args, rep, raw)
        rep["staked_out"] = True
        return rep

    @staticmethod
    def _menu_style(rep):
        """A Blu-ray's kind of menus, in the log (bdmenus.menu_style), the disc named: a line that
        stands alone, to count which discs have which."""
        if rep.get("format") != "Blu-ray" or rep.get("from_sample") or not rep.get("path"):
            return
        from . import bdmenus
        di.logger.info(i18n.log("menus: {style}  [{disc} {label}]", style=bdmenus.style_line(bdmenus.menu_style(rep["path"])), disc=(rep.get("content_hash") or "")[:8], label=rep.get("label") or i18n.log("no label")))

    @staticmethod
    def _menu_files(rep):
        """A Blu-ray's titles from its menu's own files (bdcrawl.from_files), or None: not such a disc.
        Whatever the scan took it for: its episodes say it's a show. Off the Tk thread. No VLC, no Java,
        and never a reason for the scan to fail."""
        if rep.get("format") != "Blu-ray" or rep.get("from_sample") or not rep.get("path"):
            return None
        from . import bdcrawl
        try:
            from . import menus
            return bdcrawl.from_files(rep["path"], rep.get("label"), rep.get("titles") or [],
                                      lambda line: di.logger.info(i18n.log("menus: {line}", line=line.strip())),
                                      out_dir=menus.menu_dir(rep["content_hash"]))
        except Exception:
            di.logger.exception(i18n.log("reading the disc's menu files failed"))
            return None

    @staticmethod
    def _files_saved(args, rep):
        """What a disc's menu files say for certain, saved as Sony's menu project's is, with nothing to
        confirm: each title the files number and the show's list names, and those they tie to it, if
        the words on the menu agreed with that list or the show's already settled. Not a title only
        left out of them: that's for you to look at, Other for now."""
        tv = (rep.get("menu_raw") or {}).get("tv") or {}
        found = rep.get("menu_found") or {}
        con, h = getattr(args, "library", None), rep.get("content_hash")
        if not tv.get("exact") or not found.get("episodes") or con is None or not h:
            return
        if any(e.get("by") != "disc" for e in found["episodes"]):
            return
        read = any(re.sub(r"^\W*\d+\W*", "", r.get("text") or "") for r in tv["readings"] if r.get("number") is not None)
        if not (read or rep.get("personal_hit") or (rep.get("discdb") or {}).get("hit")):
            return
        mine = dict(rep.get("labels") or {})
        saved = 0
        for tid, lab in detected_for(rep, found).items():
            if tid not in mine and lab.get("type") and lab["type"] != Type.OTHER:
                di.set_label(con, h, tid, lab["type"], join_title(lab.get("title"), lab.get("description")),
                             lab.get("season"), lab.get("episode"))
                mine[tid] = {"type": lab["type"], "title": lab.get("title"), "season": lab.get("season"), "episode": lab.get("episode")}
                if lab.get("description"):
                    mine[tid].update(description=lab["description"], text=join_title(lab.get("title"), lab["description"]))
                saved += 1
        if saved:
            rep["labels"] = mine
            di.logger.info(i18n.log("the disc's menu files settle {saved} of its titles: saved", saved=saved))

    def stakeout_in_scan(self, rep, vlc):
        """Whether the scan goes on stakeout ahead of being asked: VLC to do it with (or a Blu-ray's menus
        read from its files, needing none), and its menus wanted."""
        # (a Blu-ray's menus read from its files alone, hdmv-menus' way: no VLC crawl to fall back to)
        able = self.reads_without_vlc(rep) if (rep or {}).get("format") == "Blu-ray" else bool(vlc)
        return able and self.menus_wanted(rep)

    @staticmethod
    def reads_without_vlc(rep):
        """Whether a disc's menus can be read with no VLC: a Blu-ray, the hdmv-menus library installed and
        MakeMKV's own library to open it with (its menus HDMV, or not, the stakeout itself sees)."""
        if not rep or rep.get("format") != "Blu-ray":
            return False
        if rep.get("bd_project"):            # (its menus in its own files: Sony's menu project)
            return True
        from . import bdhdmv
        return bdhdmv.available() and bdhdmv.aacs_library() is not None

    def scan_menus_mode(self, rep):
        """Settings > Disc: "never", or when the scan asks the menus of a disc like this."""
        return self.settings.get("detect_in_scan", "never") if menus_on(self.settings, rep.get("format")) else "never"

    def menus_wanted(self, rep):
        """Whether the scan asks a disc's menus (Settings > Disc, with Detect titles on): when
        the film isn't certain (not yours, not TheDiscDB's: unnamed, a guess, a second feature
        unnamed), or also on every disc whose titles aren't known (TheDiscDB's, your labels)."""
        mode = self.scan_menus_mode(rep)
        if mode == "never" or rep.get("from_sample") or not rep.get("path"):
            return False
        hit = (rep.get("discdb") or {}).get("hit")
        weak = not rep.get("personal_hit") and not hit and (
            str(rep.get("status", "")).startswith("unidentified") or not di.is_probable(rep.get("candidates"))
            or self.more_films(rep))
        known = bool(hit and hit.get("titles")) or bool(rep.get("labels"))
        # (a show's disc that doesn't say its season, yours too: its menus may)
        no_season = rep.get("kind") == "tv" and not known and not (rep.get("match") or {}).get("season")
        return weak or no_season or (mode == "unknown" and not known)

    def _menus_witness(self, drive, args, rep, staked=False):
        """Nothing named the disc (a number for a label, no cover art), or only a guess for a disc
        of two films or more (Abbott and Costello's: the label names the pair, not the films): what
        its menus show, read (menus.menu_phrases: a kept stakeout's pictures, else a look in VLC),
        and the disc identified again with that as cover art. Rush Hour / Rush Hour 2's double
        feature: its menu's posters. The scan's answer as it was when the menus say nothing new
        (for a guess: unless they make it several films, or sure)."""
        from . import menus
        vlc = menus.find_vlc(self.settings.get("vlc") or None)
        asked = self._menus_again == (rep or {}).get("content_hash")      # (Detect titles found several films)
        self._menus_again = None
        # (a Blu-ray's: its stakeout's pages only, drawn from its files, no VLC needed or looked through)
        blu = bool(rep) and rep.get("format") == "Blu-ray" and staked
        if not rep or not (blu or (vlc and rep.get("format") == "DVD")):
            return rep
        if blu:
            vlc = None
        unnamed = str(rep.get("status", "")).startswith("unidentified")
        several = not unnamed and self.more_films(rep)
        # (a stakeout just now: its pictures are there to read, for a guess too)
        guess = staked and not rep.get("personal_hit") and not (rep.get("discdb") or {}).get("hit") and \
            not di.is_probable(rep.get("candidates"))
        if not (unnamed or several or asked or guess):
            return rep
        di.step("menus")
        phrases = menus.menu_phrases(drive, vlc, rep.get("content_hash"), log=di.logger.info,
                                     is_word=di.word_checker(getattr(args, "imdb_dir", None)))
        di.logger.info((i18n.log("the menus show: {phrases}", phrases=" / ".join(phrases)) if phrases else
                        i18n.log("the menus show: nothing to read")))
        if phrases:
            args.menu_phrases = phrases
            again = di.identify(drive, args)
            top = (again.get("candidates") or [None])[0]
            if unnamed or (top and top.get("kind") == "collection" and di.adopt_guess(again["candidates"])) or \
                    (not di.is_probable(rep.get("candidates")) and di.is_probable(again.get("candidates"))):
                if not str(again.get("status", "")).startswith("unidentified"):
                    di.logger.info(i18n.log("named by its menus: {guess}", guess=again.get("best_guess")))
                rep = again
            else:
                di.logger.info(i18n.log("the menus didn't name more films: the scan's answer stands"))
        if self.more_films(rep):
            rep["more_films"] = True
            di.logger.info(i18n.log("its titles are {films_apart} films' worth of video, one named: another film?", films_apart=rep["films_apart"]))
        return rep

    @staticmethod
    def more_films(rep):
        """A disc named as one film (not yours, not TheDiscDB's) whose feature-length titles are
        two films' worth of video or more: not cuts of it (they'd share their video), another film
        too (Abbott and Costello's In the Navy and Hold That Ghost, 85.5 and 85.9 minutes)."""
        cands = rep.get("candidates") or []
        return rep.get("films_apart", 0) >= 2 and not rep.get("personal_hit") and \
            not (rep.get("discdb") or {}).get("hit") and not (cands and cands[0].get("kind") == "collection")

    def scan_done(self, rep, err, gone=False):
        self.show_progress(False)
        self.set_busy(False)
        self.show_update()               # (the scan may have refreshed the weekly index, and looked for a newer version)
        if not err and rep and self.ejected(rep.get("path")):
            err, gone = "the disc was removed", True       # (out while the story was told: nothing shown of it)
        if err and gone:                 # the disc taken out mid-scan: no error, just that
            di.logger.info(i18n.log("the disc was removed: scan aborted"))
            self.clear_case(_(self.EMPTY_TITLE), _(self.EMPTY_NOTE))
            self.set_case("none")
            self.set_status(_("The disc was removed: scan aborted"), "warn")
            return
        if err:
            self.clear_case(_("Couldn't read this disc"), err)
            self.set_case("none")
            self.set_status(_("Scan failed: details in the case notes"), "bad")
            return
        old = self.rep or self._before   # (a scan clears the desk first: the case before it)
        self._before = None
        if not rep.get("menu_found") and old is not rep and old and old.get("menu_found") and \
                old.get("content_hash") == rep.get("content_hash"):
            # the same disc scanned again (an answer saved): what its menus said still holds
            rep["menu_found"] = old["menu_found"]
            rep["menu_raw"] = old.get("menu_raw")
        if rep.get("menu_found"):                                 # (carried over, or this scan's stakeout)
            rep["detected"] = detected_for(rep, rep["menu_found"])
            di.apply_labels(rep.get("titles") or [], all_labels(rep))
            di.labels_over_guess(rep)
        elif rep.get("bd_project") and rep.get("detected"):        # (a Blu-ray's menu project's names)
            di.apply_labels(rep.get("titles") or [], all_labels(rep))
            di.labels_over_guess(rep)
        self.rep = rep
        d = self.titles_dialog            # Edit titles open on this disc: as the new scan has it
        if d is not None and d.alive() and d.rep is not None and d.h == rep.get("content_hash"):
            d.rescanned(rep)
        self.set_busy(False)
        self._build_rip_menu()
        name, year = split_title(display_title(rep))
        self.title_lbl.config(text=name)
        self.year_lbl.config(text=year)
        self.case_lbl.config(text=_("CASE FILE #{content_hash}", content_hash=rep["content_hash"][:8]))
        facts = rep.get("facts") or {}
        kind = {"tv": _("TV"), "movie": _("Movie"), "collection": _("Collection")}.get(rep["kind"], _("Disc"))
        if rep.get("extras_disc"):
            kind = _("Special Features")         # the film's or show's extras disc: none of it on there
        mid = []
        if rep.get("extras_disc") and (rep.get("boxset") or {}).get("disc"):
            mid.append(rep["boxset"]["disc"])     # which disc of the set: "Indiana Jones Bonus Disc"
        if facts.get("episode_count") and rep["kind"] != "collection":
            mid.append(ui_n("{n} episode", "{n} episodes", facts["episode_count"]))
        if facts.get("feature_minutes"):
            mid.append(_("{minutes} min", minutes=facts["feature_minutes"]))
        uhd, hdr = picture_format(rep)
        if uhd:
            mid.append("4K")                      # the picture, beside the runtime: 4K, HDR10 / Dolby Vision
        if hdr:
            mid.append(hdr)
        m = rep.get("match")
        guess = bool(m and not m.get("sure") and not rep.get("personal_hit") and not (rep.get("discdb") or {}).get("hit"))
        by = _("Best guess") if guess else _("Identified by {source}", source=rep.get("identified_by", "?")) \
            if m or rep.get("personal_hit") else _("Unidentified")
        self.set_facts(_("{format} {kind}", format=rep["format"], kind=kind), "  \u2022  ".join(mid), by, guess=guess or not m)
        self.show_details(rep)
        self.show_plan_summary()
        self.show_picture(rep)
        self.show_match_button(rep)
        st = str(rep.get("status", ""))
        closed = st.startswith(("identified", "probable"))
        self.set_case("closed" if closed else "open")
        tone = "warn"                            # (a guess, or no answer: yours to settle)
        if rep.get("more_films"):
            msg = (_("There's a second feature on this disc we couldn't name: Detect titles may find it, or add it "
                     "with Not right? > Enter it myself > Multi-film") if self.can_detect() else
                   _("There's a second feature on this disc we couldn't name: add it with Not right? > Enter it myself "
                     "> Multi-film"))
            tone = "info"
        elif closed:
            msg = ""
        elif st.startswith("best guess"):
            msg = _("Best guess: confirm it, or pick another with Not right?")
        elif rep.get("disc_images"):
            msg = _("Still a mystery: Choose the match (an AI can read the disc's picture for you)")
        else:
            msg = _("Still a mystery: Choose the match to say what it is")
        self.set_status(msg, tone)
        if self.titles_waiting():
            di.logger.info(i18n.log("the menus named {count} titles, waiting to be confirmed in Edit titles", count=len(self.titles_waiting())))
        self.show_waiting()
        if os.path.exists(di.makemkv_log_store(rep["content_hash"])):     # read by MakeMKV before
            self.set_background(_("Lab report on file"), th.GREEN_DOT)
        if self._answer_saved == rep["content_hash"]:        # rescanned after you corrected it
            self._answer_saved = None
            self.set_outcome(_("Saved to your case files"), th.GREEN_DOT)
        if rep.get("personal_hit"):      # in your case files: the lab reads it, if it hasn't yet
            self.prescan(rep)
        if rep.get("menu_found"):        # several films now: their menus say which title each is
            answer = films_by_menus(rep, rep["menu_found"])
            if answer:
                self.films_answer(rep, answer)

    def titles_waiting(self):
        """Titles the scan's own stakeout named that you haven't confirmed yet (Edit titles)."""
        rep = self.rep or {}
        if not rep.get("staked_out"):
            return []
        # (the rows Edit titles asks you to confirm: a title it lists in view, the menus' word on it, not
        # yours; not one under Show more, an ad or an extra that isn't being ripped: it asks nothing of you)
        roles = {t["id"]: t.get("role") for t in rep.get("titles") or [] if t.get("role") in TitlesDialog.ORDER}
        return [tid for tid, lab in (rep.get("detected") or {}).items() if tid in roles and tid not in (rep.get("labels") or {})
                and lab.get("note") != NOT_IN_MENUS and not under_more(roles[tid], lab.get("type"), self.extras.get())]

    def show_waiting(self):
        """The rip card's "3 UNCONFIRMED" pill: titles the scan's stakeout named that wait for you
        (gone once they're confirmed in Edit titles)."""
        waiting = self.titles_waiting()
        if waiting:
            self.confirm_badge.config(text=_("{count} UNCONFIRMED", count=len(waiting)))
            if not self.confirm_tag.winfo_manager():
                self.confirm_tag.pack(side="right", padx=(0, 6))
        else:
            self.confirm_tag.pack_forget()

    def films_answer(self, rep, answer):
        """Keep what a disc's films' menus say (films_by_menus) as its answer. Only whose film
        each extra is, the films as they were: saved as it is, the case as it is (no new scan:
        nothing it shows changes); the films put in another order: saved, the disc looked at
        again (their names, posters, rip plan follow). True when it's looked at again. Only a disc
        you've answered for is saved: a guess is put right as it stands, and saved when you
        confirm it (a wrong guess isn't made yours: Go to Mars & Mexicana)."""
        top = rep["candidates"][0]
        said = di.parse_truth(answer)
        if not rep.get("personal_hit"):
            by_imdb = {it["imdb_id"]: it for it in top["items"]}
            for tid, iid in said["films"]:
                if iid in by_imdb:
                    by_imdb[iid]["title_id"] = tid
            top["items"].sort(key=lambda it: di._natural(it["title_id"]))
            top["extras"] = said["extras"]
            top["name"] = di.strip_tags(answer)
            rep["plex_names"] = di.apply_collection(top, rep.get("titles") or [])
            rep["best_guess"], rep["identified_by"] = di.best_guess(rep)
            di.logger.info(i18n.log("the films' menus put the guess in order: {name} (not saved: a guess)", name=top["name"]))
            if rep is self.rep:
                name, year = split_title(display_title(rep))
                self.title_lbl.config(text=name)
                self.year_lbl.config(text=year)
                self.relabel()
            return False
        if dict(said["films"]) == {it["title_id"]: it["imdb_id"] for it in top["items"]}:
            di.set_truth(self.scan_args().db, rep["content_hash"], answer)
            top["extras"] = said["extras"]
            rep["personal_hit"]["truth"] = answer
            di.logger.info(i18n.log("answer for {disc}: {answer} (whose film each extra is: no new scan)", disc=rep["content_hash"][:8], answer=answer))
            return False
        self.use_answer(answer)
        return True

    def current_plan(self, mk_titles=None, extras=None, selected="current"):
        """Must run on the Tk thread unless extras/selected are passed in (Tk variables aren't thread-safe)."""
        titles = mk_titles if mk_titles is not None else preview_titles(self.rep)
        return plan_rip(self.rep, titles, self.extras.get() if extras is None else extras,
                        self.selected if selected == "current" else selected)

    def show_plan_summary(self):
        plan = self.current_plan()
        self.plan_lbl.config(text=describe_plan(plan, self.selected is not None))
        if plan:
            # the folder every file goes under (the movie / show folder, not a subfolder)
            dirs = [p["dest"].split("/")[:-1] for p in plan]
            common = []
            for parts in zip(*dirs):
                if len(set(parts)) != 1:
                    break
                common.append(parts[0])
            self.set_dest(os.path.join(self.outdir.get(), *common))
        else:
            self.set_dest(self.outdir.get())
        self.show_rip_films(plan)
        badge = plan_badge(self.rep, plan)
        self.rip_badge.config(text=badge)
        self.rip_tag.pack_forget()
        self.confirm_tag.pack_forget()
        if badge:
            self.rip_tag.pack(side="right")
        self.show_waiting()
        self.audio_lbl.config(text=audio_summary(self.rep, plan))
        have_mkv = bool(self.makemkv_exe())
        self.engine_lbl.config(text=_("MakeMKV linked") if have_mkv else _("MakeMKV not found"),
                               style="InsetOk.TLabel" if have_mkv else "InsetWarn.TLabel",
                               image=th.check() if have_mkv else "", compound="left")
        from . import menus
        have_vlc = menus_on(self.settings, (self.rep or {}).get("format")) and bool(menus.find_vlc(self.settings.get("vlc") or None))
        # (a Blu-ray whose menus are read from its files: no VLC needed, nothing to warn of)
        if menus_on(self.settings, (self.rep or {}).get("format")) and (have_vlc or not self.reads_without_vlc(self.rep)):
            self.vlc_lbl.config(text=_("VLC linked") if have_vlc else _("VLC not found"),
                                style="InsetOk.TLabel" if have_vlc else "InsetWarn.TLabel",
                                image=th.check() if have_vlc else "", compound="left")
            self.vlc_lbl.pack(anchor="w", pady=(2, 0))
        else:
            self.vlc_lbl.pack_forget()
        # (ready to rip needs no words: the Rip button says so; the lab's report shows here instead)
        self.rip_note.config(text=("" if plan else _("Nothing selected: use the arrow beside Rip > Choose titles"))
                             if have_mkv else _("install MakeMKV to rip (or set it in Settings)"))
        th.set_dot(self.rip_dot, th.BRASS_600 if plan and have_mkv else th.INK_SUBTLE)
        if self.rep:
            self.rip_card.pack(fill="x", anchor="w", pady=(10, 0))
        self.rip_btn.set_state(bool(plan) and not self.busy and have_mkv,
                               arrow_enabled=bool(self.rep) and not self.busy)

    def can_detect(self):
        """Detect titles is on offer (Edit titles): its format's switched on in Settings (a real disc,
        not a saved sample)."""
        rep = self.rep
        # (rep's path was a disc when it was scanned: not asked again, the drive may be busy)
        return bool(rep and menus_on(self.settings, rep.get("format")) and not rep.get("from_sample") and rep.get("path"))

    def detect_titles(self):
        """Read the disc's menus (VLC plays them and presses their buttons) for its feature and
        extras; every other title becomes a Promo. Kept for this scan only: titles you've
        labelled, and those TheDiscDB names, stay as they are."""
        from . import menus
        if self.busy or not self.can_detect():
            return
        vlc = menus.find_vlc(self.settings.get("vlc") or None)
        if self.rep.get("format") == "Blu-ray":
            if not self.reads_without_vlc(self.rep):     # (its menus read from its files alone: no VLC crawl)
                self.detect_outcome(_(self.NO_MAKEMKV_STAKEOUT), th.WAX)
                return
        elif not vlc:
            self.detect_outcome(_(self.NO_VLC_STAKEOUT), th.WAX)
            return
        # one thing at a time on the drive: the lab (MakeMKV) steps aside for the stakeout, and
        # picks up again once it's over
        lab = self.stop_prescan(_("The lab waits out the stakeout…"))
        self.detect_stop.clear()
        self.detecting = True
        self.set_busy(True)
        self.story_reset()
        v = self.titles_dialog
        if v is None or not v.alive():       # (Edit titles has a light of its own)
            self.show_progress(True, indeterminate=True)
        self._stakeout_t0 = time.time()      # (its time left told only once it's run a while: _crawl_estimate)
        self._crawl_phase = None
        # each line stays up a moment, as the scan's do: setting up (the drive, the menus' data),
        # then going on stakeout once VLC plays the menus
        self.tell(_("Setting up the stakeout…"))
        self.run_bg(self._detect_worker, self.rep, vlc, lab)

    def _detect_worker(self, rep, vlc, lab=None):
        from . import menus
        try:
            self.wait_for_lab(lab)       # MakeMKV gone from the drive, and the drive settled
            if not self.still_the_disc(rep):     # (another disc in the drive since the scan: not read)
                return
            if self._files_named(rep):           # (its menus in its own files: read again, no stakeout)
                return
            raw = self._stakeouts[rep["content_hash"]] = self._stakeout(rep, vlc)
            rep["menu_raw"] = raw
            self.post(self._crawl_estimate)      # (naming them: no time left told, whichever crawl it was)
            found = self._name_titles(raw, rep)
            # the answer in its turn, once the last suspect has had their moment
            self.post(self.tell, lambda: self.detect_done(rep, found, None))
        except menus.Cancelled:
            gone = self.disc_gone(rep["path"])
            di.logger.info(i18n.log("the disc was removed: stakeout aborted") if gone else i18n.log("Detect titles called off"))
            msg = self.GONE_STAKEOUT if gone else self.CANCELLED_STAKEOUT
            self.post(self.tell, lambda: self.detect_done(rep, None, msg))
        except menus.NoReader as exc:
            di.logger.warning("%s", exc)
            self.post(self.tell, lambda: self.detect_done(rep, None, self.NO_READER_STAKEOUT))
        except menus.NoVlc:
            self.post(self.tell, lambda: self.detect_done(rep, None, self.NO_VLC_STAKEOUT))
        except menus.Unsupported:
            self.post(self.tell, lambda: self.detect_done(rep, None, self.UNSUPPORTED_STAKEOUT))
        except menus.Lost as exc:    # (no menu answered: not an error, unless the disc went)
            di.logger.info(i18n.log("no menu answered on this disc ({error})", error=exc))
            msg = self.GONE_STAKEOUT if self.disc_gone(rep["path"]) else self.NO_MENU_STAKEOUT
            self.post(self.tell, lambda: self.detect_done(rep, None, msg))
        except Exception as exc:     # show it rather than dying silently in a thread
            di.logger.exception(i18n.log("reading the menus failed"))
            msg = "%s: %s" % (type(exc).__name__, exc)
            if self.disc_gone(rep["path"]):       # (taken out mid-stakeout: said so, plainly)
                msg = self.GONE_STAKEOUT
            self.post(self.tell, lambda: self.detect_done(rep, None, msg))

    def _files_named(self, rep):
        """Detect titles on a Blu-ray whose menus are in its own files (Sony's menu project: Muppets
        from Space): those read again and their names classified (_bd_menus_seen, as the scan does),
        in a moment, no menu crawled. False: not such a disc (off the Tk thread)."""
        from . import bdmenus
        if rep.get("format") != "Blu-ray" or not rep.get("path") or rep.get("kind") == "tv":
            return False
        try:
            if not bdmenus.menu_project(rep["path"]):
                return False
        except Exception:
            return False
        self.post(self.tell, _("Opening the case files…"))
        args = self.scan_args()
        args.library = di.library_open(args.db)
        try:
            self._bd_menus_seen(rep["path"], args, rep)
        finally:
            args.library.close()
        self.post(self.tell, lambda: self.files_done(rep))
        return True

    def files_done(self, rep):
        """Detect titles' answer from a disc's own menu files (_files_named): its titles relabelled, as a
        stakeout's are."""
        self.detecting = False
        if self.progress.winfo_manager():
            self.show_progress(False)
        self.set_busy(False)
        if rep is not self.rep:          # another disc was scanned meanwhile
            return
        d = self.titles_dialog
        if d is not None and d.rep is rep:
            self.post(d.detected)
        if rep.get("personal_hit"):      # the lab, stopped for it, picks up again
            self.prescan(rep)
        n = len(rep.get("detected") or {})
        if not n:
            self.show_plan_summary()
            self.detect_outcome(_("The disc's own files name none of its titles"), th.AMBER_DOT)
            return
        self.relabel()
        self.detect_outcome(ui_n("The disc's own files name {n} of its titles", "The disc's own files name {n} of its titles", n),
                            th.GREEN_DOT)

    def _crawl_progress(self):
        """What a crawl tells as it goes (menus.CRAWL_STAGES: a DVD's, a Blu-ray's, from VLC or the disc's own
        files alike), in the app's words, on the status line (posted: the crawl runs off the Tk thread)."""
        count = {"before": 0, "total": 0}     # (a player's presses, counted from 1 each round)

        def progress(stage, k=0, n=0):
            if stage == "setup":
                line = _("Opening the case files…")
            elif stage == "start":
                line = _("Going on stakeout…")
            elif stage == "page":        # each page read: a suspect questioned (quick)
                line = suspect_line("questioning", k, n)
            elif stage == "press":       # each button pressed: a suspect; a new round joins those heard
                if k == 1:
                    count["before"] = count["total"]
                count["total"] = count["before"] + n
                line = suspect_line("interrogating", count["before"] + k, count["total"])
            elif stage == "titles":      # the titles sought, k of n settled: for the time left (its words as they are)
                self.post(self._crawl_estimate, k, n)
                return
            elif stage == "finish":      # the search over: no time left told for the rest (naming, photos)
                self.post(self._crawl_estimate)
                return
            elif stage == "photos":      # a Blu-ray's menu pages over their video, for Edit titles
                self.post(self._crawl_estimate)
                line = _("Taking photos for evidence…")
            else:
                return
            self.post(self.tell, line)
        return progress

    ESTIMATE_AFTER = 120        # seconds of a stakeout before its time left is told (a quick one needs none)

    def _crawl_estimate(self, k=None, n=None):
        """The stakeout's time left (on the Tk thread), beside its bar (busy throughout): told once it has run
        ESTIMATE_AFTER, while it looks for its titles, at the rate they've been settled since it began to (k
        of the n sought: never counted back, though n grows as presses find more); gone once they're found
        (k None: naming them, photos). Edit titles' while it's up, else the main window's."""
        v = getattr(self, "titles_dialog", None)
        if v is not None and v.alive() and v.news_bar.winfo_manager():
            label = v.news_pct
        elif self.progress.winfo_manager():
            label = self.status_pct
        else:
            return
        now = time.time()
        if k is None or not n:
            self._crawl_phase = None
            label.config(text="")
            return
        began, f0, best = getattr(self, "_crawl_phase", None) or (now, k / n, 0.0)
        f = max(k / n, best)
        self._crawl_phase = (began, f0, f)
        left = None
        if now - getattr(self, "_stakeout_t0", now) >= self.ESTIMATE_AFTER and f0 < f < 1:
            left = (1 - f) * (now - began) / (f - f0)      # (at the rate it's gone since the titles were sought)
        label.config(text=time_left(left) if left is not None else "")

    def _stakeout(self, rep, vlc):
        """The stakeout itself (off the Tk thread): VLC plays the disc's menus, their buttons are
        pressed and read. What it finds as read, the film not known yet (menus.crawl's result:
        its pages and pictures, and what plays what); _name_titles names the titles for a film."""
        from . import menus
        t0 = time.time()
        di.logger.info(i18n.log("menus: setting up the stakeout on {path}", path=rep["path"]))
        # (called off: Cancelled at the crawl's next step, on its own thread, whichever crawl it is; and so is
        # one whose disc was taken out, the drive asked every DISC_CHECK at those steps: a player waiting on a
        # disc that's gone took minutes to give up, WAIT_PLAYING)
        worker = threading.get_ident()
        asked = [time.time()]

        def check():
            if threading.get_ident() != worker:
                return
            stop = getattr(self, "detect_stop", None)
            if stop is not None and stop.is_set():
                raise menus.Cancelled()
            if time.time() - asked[0] >= self.DISC_CHECK:
                asked[0] = time.time()
                if self.disc_gone(rep["path"]):
                    raise menus.Cancelled()

        def log(line):
            di.logger.info(i18n.log("menus: {line}", line=line.strip()))
            check()
        told = self._crawl_progress()

        def progress(stage, k=0, n=0):
            check()
            told(stage, k, n)
        root = rep["path"]
        if rep.get("format") == "Blu-ray":        # (its menus read from its own files, nothing played)
            from . import bdmenus
            raw = self._menu_files(rep)           # (unless its own files say)
            if raw is not None:
                return raw
            style = bdmenus.menu_style(root)
            if not bdmenus.crawlable(style):
                log(i18n.log("menu style not yet supported ({style})", style=bdmenus.style_line(style)))
                raise menus.Unsupported(bdmenus.style_line(style))
            if menus.rapid_missing():
                raise menus.NoReader("RapidOCR can't run here: pip install rapidocr onnxruntime, with a connection "
                                     "for its model the first time")
            # (HDMV menus read from the disc's own files by the hdmv-menus library: every menu, every button and
            # what it plays, nothing played. The one way: VLC's crawl of a Blu-ray's pop-up is gone, the user's,
            # 2026-11-17 -- Chuck's discs, its last, read better this way)
            from . import bdhdmv
            if not bdhdmv.available():
                raise menus.Unsupported("hdmv-menus isn't installed")
            try:
                got = bdhdmv.read(root, rep.get("titles") or [], menus.menu_dir(rep["content_hash"]), log, progress=progress,
                                  tv=rep.get("kind") == "tv",
                                  # (a quick crawl, each title's best button; every one, if Settings ask)
                                  scope="language" if self.settings.get("menu_every_title") else "titles",
                                  is_word=self._crawl_words(rep, self.scan_args()), anchored=anchored_titles(rep))
            except (menus.Cancelled, menus.Unsupported):
                raise                    # (called off; a Java menu after all)
            except Exception:
                if self.disc_gone(root):         # (it failed for its disc taken out)
                    raise menus.Cancelled()
                di.logger.exception(i18n.log("reading the HDMV menus failed"))
                raise menus.Lost("reading the HDMV menus failed")
            if got is None:
                raise menus.Lost("no menu reached from the first play")
            return got
        progress("setup")                # (its menu data read, or the copy kept)
        structure = menu_data(root, rep["content_hash"], vlc, log, t0)
        out = menus.menu_dir(rep["content_hash"])
        log(i18n.log("reading the menus of {root} with VLC {vlc}", root=root, vlc=vlc))
        args = self.scan_args()
        # (the IMDb index as a dictionary: which of Windows OCR's two readings has real words)
        result = menus.crawl(root, vlc, structure, out, log=log, is_word=self._crawl_words(rep, args),
                             tv=rep.get("kind") == "tv", every_title=bool(self.settings.get("menu_every_title")),
                             progress=progress, anchored=anchored_titles(rep, number=True))
        result["suspects"] = sum(1 for p in result["pages"] for b in p["buttons"] if "chain" in b)
        return result

    def _name_titles(self, raw, rep):
        """What a stakeout found (_stakeout), its titles named for the disc's film as the scan has
        it (rep's match): misreadings put right by the film's people, junk made of the backdrop
        dropped, other films' trailers taken for ads. Off the Tk thread (IMDb, the film's credits).
        Run again, no drive needed, when the film changes."""
        from . import menus
        args = self.scan_args()
        log = lambda line: di.logger.info(i18n.log("menus: {line}", line=line.strip()))
        titles, is_word, names = self._menu_dictionary(rep, args, log)
        if rep.get("kind") == "tv" or (raw.get("tv") or {}).get("exact"):
            return self._name_episodes(raw, rep, args, log, is_word, names, titles)
        found = raw["found"]
        match = rep.get("match") or {}
        # (and by the words read well elsewhere in the menus: "Iagic" by "Magic")
        ad_fix = self._ad_fix(rep, titles)
        found = menus.correct_names(found, names, is_word, ad_fix,
                                    seen=menus.disc_words(raw, is_word, names), film=naming_film(rep))
        found = menus.plain_names(found, is_word)
        # the well-known films trailers are named for (Spider-Man's xXx, Mr. Deeds...): for the namer to judge
        # other films' trailers by (menus.name_extras, when labelled: detected_for)
        if titles is not None:
            found = menus.known_films(found, lambda n: di.known_film(titles, n), naming_film(rep), ad_fix)
        for line in menus.found_text(found).splitlines():
            log(line)
        return dict(found, suspects=raw.get("suspects", 0))

    @staticmethod
    def _ad_fix(rep, titles):
        """How an ad's reading is put right as the film or show it's for, any disc's (core.title_fix: a reading and
        its other readings), or None without the IMDb index: up to a year past the disc's mastering, else its
        film's or show's year."""
        if titles is None:
            return None
        match = rep.get("match") or {}
        year = int(rep["mastered"][:4]) if str(rep.get("mastered") or "")[:4].isdigit() else match.get("year")
        return lambda t, also=(): di.title_fix(titles, t, year, also)

    @staticmethod
    def _menu_dictionary(rep, args, log):
        """What a stakeout's readings are put right by, a film's or a show's: (the IMDb index, its words
        (is_word: the titles' words and the people's), the people in the disc's credits [names])."""
        try:                             # names OCR made up out of the backdrop: plain names
            titles = di.imdb_open(args.imdb_dir, allow_download=False, log=lambda m: None)
            is_word = di.title_words(titles)
        except Exception:
            titles = is_word = None
        names, people = App._film_people(rep, args)
        if is_word and people:
            log(i18n.log("the show's credits: {count} words of names", count=len(people)) if rep.get("kind") == "tv" else i18n.log("the film's credits: {count} words of names", count=len(people)))
            is_word = (lambda known: lambda w: w.lower() in people or known(w))(is_word)
        return titles, is_word, names

    @staticmethod
    def _film_people(rep, args):
        """The film's or show's people (its characters, cast and crew: extras are named after them, "Hogarth
        Hughes"): names no title has, words too; and names to put misreadings right by. ([names], {their words}),
        none for a disc not known yet."""
        match = rep.get("match") or {}
        if not match.get("imdb_id"):
            return [], set()
        lib = di.library_open(args.db)
        try:
            credits = di.film_credits(lib, match.get("imdb_id"), args.tmdb_key, args.imdb_dir,
                                      online=not args.offline)
        finally:
            lib.close()
        names = [x for r in credits for x in r[:2] if x]
        return names, {w for n in names for w in re.findall(r"[^\W\d_]{2,}", n.lower())}

    @staticmethod
    def _crawl_words(rep, args):
        """The stakeout's dictionary (is_word, for any thread: di.word_checker), the film's or show's people's words
        with the titles' when the disc's known already -- "Nathaniel", Enchanted's, a word as the namer has it, not
        a misreading to read again (the user's). None without the IMDb index."""
        is_word = di.word_checker(args.imdb_dir)
        if is_word is None:
            return None
        try:
            people = App._film_people(rep, args)[1]
        except Exception:                # (the credits not to be had: the titles' words alone)
            di.logger.exception(i18n.log("the film's credits couldn't be read"))
            people = set()
        if not people:
            return is_word
        di.logger.info(i18n.log("the film's credits: {count} words of names", count=len(people)))
        return lambda w: w.lower() in people or is_word(w)

    def _name_episodes(self, raw, rep, args, log, is_word=None, names=(), titles=None):
        """A TV disc's stakeout: the menus' words read against the show's episode list; the rest, its
        bonus features, put right as a film's extras are (menus.fix_tv_readings: by the show's people,
        the button's other readings, the disc's words; is_word, names, titles: _menu_dictionary's), their
        trailers' well-known films looked up as a film's are (menus.known_films). Off the Tk thread (TVmaze)."""
        from . import menus
        tv = raw.get("tv") or menus.tv_titles(raw)
        # (in the order a Play All has them, where the disc has one)
        tv = dict(tv, readings=di.menu_play_order(tv["readings"], tv.get("play_order")))
        match = rep.get("match") or {}
        orders = di.show_episodes(match.get("imdb_id"), getattr(args, "tvmaze_dir", None),
                                  online=not args.offline, imdb_dir=args.imdb_dir)
        order = getattr(args, "episode_order", "aired")
        # (the disc's own files give each its number: the names only need fetching)
        got = di.numbered_episodes(tv["readings"], orders, tv.get("season") or match.get("season"), order) \
            if orders and tv.get("exact") else None
        got = got or (di.menu_episodes(tv["readings"], orders, match.get("season"), order) if orders else None)
        tv = menus.fix_tv_readings(tv, set((got or {}).get("picks") or {}), names, is_word, raw.get("also"),
                                   menus.disc_words(raw, is_word, names), raw.get("sure"))
        # (the titles the scan already has as episodes: what's too short for one, when the menus name none)
        found = menus.tv_found(tv, (got or {}).get("picks"),
                               [t.get("duration") for t in rep.get("titles") or [] if t.get("role") == Role.EPISODE])
        # (its ads' films, and its trailers' for other films, put right as a film's disc's are)
        ad_fix = self._ad_fix(rep, titles)
        found = menus.fix_promos(found, ad_fix)
        if titles is not None:
            found = menus.known_films(found, lambda n: di.known_film(titles, n), naming_film(rep), ad_fix)
        for line in menus.found_tv_text(found).splitlines():
            log(line)
        settles = self._tv_settles(rep, tv["readings"], got, args, tv.get("disc"))
        if settles:
            found["settles"] = settles
            log(i18n.log("the menus say: {settles}", settles=settles))
        return dict(found, suspects=raw.get("suspects", 0),
                    pictures=menus.tv_pictures(tv, menus.menu_dir(rep["content_hash"]),
                                               (raw.get("found") or {}).get("pictures"), pages=raw.get("pages")))

    @staticmethod
    def _tv_settles(rep, readings, got, args, disc=None):
        """What the menus' episodes change in the scan's answer, or None: {"season": n}, your answer
        naming the show but no season; {"answer": a truth}, nothing confirmed (a show they name
        better than the scan's guess, or a season the guess hasn't or has wrong). Yours and
        TheDiscDB's show stands, whatever the menus read. disc: the number its menu's files give it.
        Never saved: it's asked for again each scan."""
        match = rep.get("match") or {}
        named = sum(1 for p in (got or {"picks": {}})["picks"].values() if p["by"] in ("name", "disc"))
        if rep.get("personal_hit") or (rep.get("discdb") or {}).get("hit"):
            return {"season": got["season"]} if got and not match.get("season") else None
        if got and named and match.get("imdb_id"):             # the scan's show it is
            if got["season"] == match.get("season") and (not disc or disc == match.get("disc")):
                return None
            return {"answer": di.truth_for(match, got["season"], disc or match.get("disc"))}
        try:
            con = di.imdb_open(args.imdb_dir, allow_download=False, log=lambda m: None)
            show = di.shows_by_episodes(con, readings)
        except Exception:
            di.logger.exception(i18n.log("searching IMDb's episodes for the menus' names failed"))
            return None
        if show and show["imdb_id"] != match.get("imdb_id"):
            return {"answer": di.truth_for(show, show["season"], disc)}
        if show and show["season"] != match.get("season"):
            return {"answer": di.truth_for(match, show["season"], disc or match.get("disc"))}
        return None

    def _settle_by_menus(self, drive, args, rep, raw):
        """The disc identified again with what its menus' episodes settled, and its titles named for
        that. Off the Tk thread."""
        settles = (rep.get("menu_found") or {}).get("settles")
        if not settles:
            return rep
        di.step("menus")
        args.menu_answer, args.menu_season = settles.get("answer"), settles.get("season")
        again = di.identify(drive, args)
        args.menu_answer = args.menu_season = None
        again["menu_raw"] = raw
        again["menu_found"] = self._name_titles(raw, again)
        again["menu_found"].pop("settles", None)                # (once: it's been heard)
        return again

    GONE_STAKEOUT = N_("The disc was removed: stakeout aborted")
    DISC_CHECK = 3.0          # seconds between asking a stakeout's drive whether its disc is still in
    CANCELLED_STAKEOUT = N_("Stakeout called off")
    NO_MENU_STAKEOUT = N_("No menu answered: nothing to stake out on this disc")
    UNSUPPORTED_STAKEOUT = N_("Could not go on Stakeout")
    NO_READER_STAKEOUT = N_("No stakeout: RapidOCR isn't working here, details in the case notes")
    NO_VLC_STAKEOUT = N_("A stakeout needs VLC: install it, or set its folder in Settings")
    NO_MAKEMKV_STAKEOUT = N_("A Blu-ray's stakeout needs MakeMKV to open the disc: install it")

    def detect_outcome(self, text, colour):
        """How the stakeout went: in Edit titles, where it was asked for, while that's open; else
        in the footer."""
        v = self.titles_dialog
        if v is not None and v.alive():
            v.show_outcome(text, colour)
        else:
            self.set_outcome(text, colour)

    def detect_done(self, rep, found, err):
        self.detecting = False
        if self.progress.winfo_manager():
            self.show_progress(False)
        self.set_busy(False)
        if rep is not self.rep:          # another disc was scanned meanwhile
            return
        d = self.titles_dialog
        if d is not None and d.rep is rep:
            self.post(d.detected)         # (after the labels below are in)
        if rep.get("personal_hit"):      # the lab, stopped for the stakeout, picks up again
            self.prescan(rep)
        if found and found.get("settles"):
            # the show or the season, by its episodes: the scan again, the stakeout kept (_menus_seen)
            self.detect_outcome(suspect_report(found), th.GREEN_DOT)
            self.scan()
            return
        if found:
            rep["menu_found"] = found
            answer = films_by_menus(rep, found)
            if answer and self.films_answer(rep, answer):   # its films put in order: looked at again
                self.detect_outcome(suspect_report(found), th.GREEN_DOT)
                return
            top = (rep.get("candidates") or [None])[0]
            if len(found.get("features") or []) >= 2 and not rep.get("personal_hit") and \
                    not (rep.get("discdb") or {}).get("hit") and not (top and top.get("kind") == "collection"):
                # several films, one named (or none): the disc identified again, from the menus'
                # pictures just taken (they read better than a first look: Hold That Ghost's logo)
                self.detect_outcome(suspect_report(found), th.GREEN_DOT)
                self._menus_again = rep["content_hash"]
                self.scan()
                return
        labels = detected_for(rep, found) if found else {}
        if not labels:
            self.show_plan_summary()
            ends = (self.GONE_STAKEOUT, self.NO_MENU_STAKEOUT, self.NO_READER_STAKEOUT, self.UNSUPPORTED_STAKEOUT)
            self.detect_outcome(*(((_(err), th.WAX) if err in ends else
                                   (_("The stakeout couldn't go ahead: details in the case notes"), th.WAX)) if err else
                                  (_("The stakeout turned up nothing: titles unchanged"), th.AMBER_DOT)))
            return
        rep["detected"] = labels
        self.relabel()
        self.detect_outcome(suspect_report(found), th.GREEN_DOT)

    FILM_COLS = 5

    def show_rip_films(self, plan):
        """The strip in the rip card: each film being ripped, when the disc's films are each a title
        of their own (its poster and name). Extras are only counted in the summary above it."""
        for w in self.strip.winfo_children():
            w.destroy()
        for col in range(self.FILM_COLS):
            self.strip.columnconfigure(col, minsize=0, uniform="")
        self._film_imgs = []
        entries = rip_films(self.rep, plan) if self.rep else []
        if not entries:
            self.films_box.pack_forget()
            return
        if entries:
            # every film the same width: its poster, the caption wrapping just past it
            pw, ph = di.ART_THUMB
            wrap = pw + 26
            for col in range(min(self.FILM_COLS, len(entries))):
                self.strip.columnconfigure(col, minsize=wrap, uniform="films")
            for k, e in enumerate(entries):
                cell = ttk.Frame(self.strip, width=wrap, style="Card.TFrame")
                cell.grid(row=k // self.FILM_COLS, column=k % self.FILM_COLS, sticky="nw", padx=(0, 10), pady=(0, 6))
                thumb = self._photo(e["image"]) if e.get("image") else None
                if thumb:
                    self._film_imgs.append(thumb)
                    ttk.Label(cell, image=thumb, style="Card.TLabel").pack(anchor="w")
                else:                            # no poster: an empty frame of the same size
                    tk.Frame(cell, width=th.px(pw), height=th.px(ph), background=th.VELLUM_100, highlightthickness=1,
                             highlightbackground=th.BRASS_300).pack(anchor="w")
                ttk.Label(cell, text=e["name"], style="CardStrip.TLabel", wraplength=wrap,
                          justify="left").pack(anchor="w", pady=(3, 0))
        self.films_box.pack(fill="x", pady=(10, 0), before=self._path_row)

    def rip_or_cancel(self):
        if self.ripping:
            self.cancel()
        else:
            self.rip()

    # ---- MakeMKV's reading of the disc (its title list), ahead of time ----
    def prescan(self, rep):
        """For a disc in your case files (confirmed or corrected; after a scan or stakeout of one
        too, if the lab hasn't read it yet), with MakeMKV linked:
        MakeMKV reads the disc's titles in the background (minutes, on a protected disc) and the
        reading is saved with the disc. A rip then starts straight away, and a TheDiscDB packet
        can be built later without the disc. Nothing to do if it's saved already."""
        self.fetch_credits(rep)
        exe = self.makemkv_exe()
        if not exe or not rep or rep.get("from_sample") or self.mkv_scan or self.busy:   # (busy: the drive's in use)
            return
        if os.path.exists(di.makemkv_log_store(rep["content_hash"])):
            short = self.lab_short(rep)
            if not short or rep["content_hash"] in self.lab_again:
                di.logger.info(i18n.log("MakeMKV's reading of {disc} is saved already (from an earlier rip or reading)", disc=rep["content_hash"][:8]))
                self.set_background(*((lab_short_text(short), th.AMBER_DOT) if short else (_("Lab report on file"), th.GREEN_DOT)))
                return
            # a title it read short, on a disc back in the drive: cleaned, maybe. A fresh look, once
            di.logger.info(i18n.log("MakeMKV's saved reading of {disc} has titles short: reading it again", disc=rep["content_hash"][:8]))
            self.lab_again.add(rep["content_hash"])
        if (rep.get("discdb") or {}).get("hit"):
            # TheDiscDB has it: no packet to build, so the reading would only spare the rip its
            # own (which it does when it starts); the drive is left alone
            di.logger.info(i18n.log("TheDiscDB has {disc}: no background reading by MakeMKV", disc=rep["content_hash"][:8]))
            return
        job = {"hash": rep["content_hash"], "drive": self.source.get(), "proc": None, "done": threading.Event(),
               "stopped": False, "ok": False}
        self.mkv_scan = job
        self.show_drive()
        self.set_background(_("The lab is examining the disc…"))
        self._build_rip_menu()           # (Open MakeMKV waits while the lab has the drive)
        di.logger.info(i18n.log("MakeMKV reads {disc} in the background (saved for rips and TheDiscDB packets)", disc=rep["content_hash"][:8]))
        self.run_bg(self._prescan_worker, exe, self.source.get(), job)

    def lab_short(self, rep):
        """The titles MakeMKV's saved reading of a disc has well short of their length (core.lab_short_titles)."""
        try:
            with open(di.makemkv_log_store(rep["content_hash"]), encoding="utf-8", errors="replace") as fh:
                return di.lab_short_titles(fh.read(), rep.get("titles"), rep.get("format"))
        except OSError:
            return []

    def fetch_credits(self, rep):
        """The film's credits into your case files, once (di.film_credits: the IMDb credits index,
        else TMDB), in the background: the network only, not the drive."""
        imdb_id = ((rep or {}).get("match") or {}).get("imdb_id")
        if not imdb_id or imdb_id in self.credits_asked:
            return
        self.credits_asked.add(imdb_id)
        args = self.scan_args()

        def work():
            con = di.library_open(args.db)
            try:
                rows = di.film_credits(con, imdb_id, args.tmdb_key, args.imdb_dir, online=not args.offline)
                di.logger.info(i18n.log("credits of {imdb_id}: {count} in your case files", imdb_id=imdb_id, count=len(rows)))
            except Exception:
                di.logger.exception(i18n.log("fetching the credits of {imdb_id} failed", imdb_id=imdb_id))
            finally:
                con.close()
        threading.Thread(target=work, daemon=True).start()

    def _prescan_worker(self, exe, drive, job):
        try:
            source = makemkv_source(drive)
            if job["stopped"]:
                return
            if not di.same_disc(drive, job["hash"]):     # (its reading kept under the case's fingerprint)
                di.logger.info(i18n.log("MakeMKV's background reading stopped: not the disc scanned"))
                return
            job["proc"] = subprocess.Popen([exe, *MAKEMKV_SWITCHES, "info", source], stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                           creationflags=NO_WINDOW)
            info = job["proc"].communicate()[0]
            if job["stopped"] or not di.parse_makemkv_log(info)["titles"]:
                di.logger.info((i18n.log("MakeMKV's background reading stopped") if job["stopped"] else
                                i18n.log("MakeMKV's background reading found no titles")))
                return
            store = di.makemkv_log_store(job["hash"])
            os.makedirs(os.path.dirname(store), exist_ok=True)
            with open(store, "w", encoding="utf-8", newline="") as fh:
                fh.write(info)
            job["ok"] = True
            di.logger.info(i18n.log("MakeMKV's reading of {hash} saved", hash=job["hash"][:8]))
        except Exception:
            di.logger.exception(i18n.log("MakeMKV's background reading failed"))
        finally:
            job["done"].set()
            self.post(self._prescan_over, job)

    def _prescan_over(self, job):
        if self.mkv_scan is job:
            self.mkv_scan = None
            self._build_rip_menu()
            self.show_drive()
        # its report, on its case: (a scan since will say "on file" itself, once it's done)
        if not job["stopped"] and self.rep and self.rep.get("content_hash") == job["hash"]:
            short = self.lab_short(self.rep) if job["ok"] else []
            for s in short:
                di.logger.warning(i18n.log("MakeMKV reads {id} {seconds} seconds short of its {length}: part of it couldn't be read (a dirty or "
                                  "damaged disc?)", id=s["id"], seconds=s["seconds"], length=s["length"]))
            if short:
                self.lab_again.add(job["hash"])      # (not read again till it's been out of the drive)
            self.set_background(*((lab_short_text(short), th.AMBER_DOT) if short else
                                  (_("Lab report filed"), th.GREEN_DOT) if job["ok"] else
                                  (_("The lab couldn't read the disc"), th.AMBER_DOT)))

    DRIVE_SETTLE = 2.0       # seconds a drive gets to settle after MakeMKV lets go of it

    def wait_for_lab(self, job):
        """(Worker thread) Wait until the lab (MakeMKV's background reading) has let go of the drive:
        only one thing at a time reads it. Stopped, it's MakeMKV quitting (a moment), then a
        breath for the drive; still working, the whole reading."""
        if not job:
            return
        if job["stopped"]:
            job["done"].wait()
            time.sleep(self.DRIVE_SETTLE)
        elif not job["done"].is_set():
            di.logger.info(i18n.log("waiting for MakeMKV's background reading to finish with the drive"))
            self.post(self.tell, _("Waiting for the lab to finish with the disc…"))
            job["done"].wait()

    def stop_prescan(self, why=None):
        """Stop MakeMKV's background reading: something else needs the drive (a stakeout, a new
        scan), or the app's closing. why: said in the rip card meanwhile (it starts again after:
        a confirmed disc without a saved reading gets one). Returns the stopped job, for whoever
        needs the drive to wait_for_lab on (MakeMKV quits, the drive settles), or None."""
        job = self.mkv_scan
        if not job:
            return None
        job["stopped"] = True
        if job["proc"] and job["proc"].poll() is None:
            job["proc"].terminate()
        self.mkv_scan = None
        self._build_rip_menu()
        self.show_drive()
        di.logger.info((i18n.log("MakeMKV's background reading stopped ({why})", why=why) if why else
                        i18n.log("MakeMKV's background reading stopped")))
        self.set_background(why, th.BRASS_400)
        return job

    def rip(self):
        if not self.rep:
            return
        exe = self.makemkv_exe()
        if not exe:
            messagebox.showerror(APP, _("MakeMKV's command-line tool (makemkvcon) wasn't found.\n\n"
                                      "Install MakeMKV, or add its folder to PATH."))
            return
        out = self.outdir.get().strip()
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP, _("Can't use the rip folder:\n{exc}", exc=exc))
            return
        self.remember()
        self.cancelled = False
        self.set_busy(True, ripping=True)
        self.show_progress(True)
        th.pulse_dot(self.rip_dot, th.BRASS_600, th.CARD)     # the rip card's light breathes while it rips
        self.set_status(_("Sending the disc to the lab (MakeMKV)…"))
        # read every setting here, on the Tk thread; the worker only gets plain values
        job = self.mkv_scan if self.mkv_scan and self.mkv_scan["hash"] == self.rep["content_hash"] else None
        opts = {"extras": bool(self.extras.get()),
                "selected": set(self.selected) if self.selected is not None else None, "prescan": job}
        self.run_bg(self._rip_worker, exe, self.source.get(), out, self.rep, opts)

    def _rip_worker(self, exe, drive, out, rep, opts):
        plan, undo = [], None
        self.mkv = makemkv.Rip(exe, lambda: self.cancelled)
        try:
            # MakeMKV's title list: read once per disc and kept (a confirmed disc's was read in the
            # background, maybe still being read: wait for it rather than read it twice)
            job = opts.get("prescan")
            if job and not job["done"].is_set():
                di.logger.info(i18n.log("waiting for MakeMKV's background reading of the disc"))
                self.post(self.set_status, _("Waiting for the lab to finish examining the disc…"))
                while not job["done"].wait(0.5):
                    if self.cancelled:
                        self.post(self.rip_done, [], 0, None)
                        return
            if not self.still_the_disc(rep):     # (another disc in the drive since the scan: not ripped)
                return
            source = makemkv_source(drive)
            store = di.makemkv_log_store(rep["content_hash"])        # reused by --packet later
            mk, reading = {"titles": []}, ""
            if os.path.exists(store):
                with open(store, encoding="utf-8", errors="replace") as fh:
                    reading = fh.read()
                mk = di.parse_makemkv_log(reading)
                if mk["titles"]:
                    di.logger.info(i18n.log("using MakeMKV's saved reading of the disc"))
            if not mk["titles"]:
                reading = self.mkv.read_titles(source)
                mk = di.parse_makemkv_log(reading)
                os.makedirs(os.path.dirname(store), exist_ok=True)
                with open(store, "w", encoding="utf-8", newline="") as fh:
                    fh.write(reading)
            plan = self.current_plan(mk["titles"], extras=opts["extras"], selected=opts["selected"])
            # what you asked for, by the disc's own list, that MakeMKV doesn't list at all (with no
            # minimum length, every title it can reach): a sting for those
            wanted = {p["title_id"] for p in self.current_plan(preview_titles(rep), extras=opts["extras"], selected=opts["selected"])}
            missing = wanted - {p["title_id"] for p in plan}
            self.rip_problem = None
            own = {}                       # our playlists, once there are any: {its file: the title}
            if missing and not self.cancelled:
                try:
                    sting = self.mkv.sting(drive, missing, lambda: self.post(self.set_status, _("Setting up a sting…")))
                    if sting:
                        source, own, undo = sting
                        reading = self.mkv.read_titles(source)
                        plan = self.current_plan(makemkv.named_titles(reading, own), extras=opts["extras"], selected=opts["selected"])
                        missing = wanted - {p["title_id"] for p in plan}
                except makemkv.STING_ERRORS as exc:
                    # the rest of the rip goes ahead without them, from the disc as it was
                    di.logger.warning(i18n.log("the sting fell through: {error}", error=exc))
                    if undo:
                        undo()
                        undo = None
                    source, own = makemkv_source(drive), {}
                    if not self.cancelled:
                        self.rip_problem = _("The sting fell through: {error}", error=exc)
            if missing:                    # (the outcome says so: fewer ripped than asked for)
                di.logger.warning(i18n.log("MakeMKV doesn't list these, so they can't be ripped: {titles}", titles=", ".join(sorted(missing))))
            expected = makemkv.makemkv_added(reading)
            numbers = None                 # once re-read: which number each title has now
            di.logger.info(i18n.log("rip {disc} from {drive} ({source}) into {folder}: {count} titles", disc=rep["content_hash"][:8], drive=drive, source=source, folder=out, count=len(plan)))
            ripping = {item["title_id"] for item in plan}
            self.rip_short = [s for s in self.lab_short(rep) if s["id"] in ripping]
            for item in plan:
                di.logger.info("  " + i18n.log("MakeMKV title {number} ({title_id}, {length}) -> {dest}", number=item["mk"], title_id=item["title_id"], length=item["length"], dest=item["dest"]))
            done = []
            # One MakeMKV run per title: makemkvcon's mkv takes one title or "all", not a list, and
            # each run rescans the disc (minutes, on a protected one). A list is a requested
            # MakeMKV feature (forum.makemkv.com/forum/viewtopic.php?f=10&t=40891): once it's
            # there, one run for the whole plan scans the disc once.
            for i, item in enumerate(plan):
                if self.cancelled:
                    break
                number = item["mk"] if numbers is None else numbers.get(item["title_id"])
                if number is None:
                    di.logger.warning(i18n.log("MakeMKV doesn't list {title_id} now: not ripped", title_id=item["title_id"]))
                    continue
                self.post(self.set_status, _("Bagging the evidence, {number} of {total}: {file}", number=i + 1, total=len(plan), file=item["dest"].split("/")[-1]))
                work = os.path.join(out, ".ripping_%s_%d" % (rep["content_hash"][:8], item["mk"]))
                shutil.rmtree(work, ignore_errors=True)
                os.makedirs(work)
                progress = lambda fraction, i=i: self.post(self.progress.configure, {"value": int(1000 * (i + fraction) / len(plan))})
                ok = self.mkv.title(source, number, work, expected, progress=progress)
                if self.mkv.numbers_changed and numbers is None and not self.cancelled:
                    # numbered differently from the reading the plan used (MakeMKV's minimum title
                    # length changed, or this is a backup of the disc read): read it again, once, and
                    # ask for the same titles by their new numbers. The saved reading stays as it
                    # was: packets use it. A second difference stops the title rather than guess.
                    di.logger.warning(i18n.log("MakeMKV numbers this disc's titles differently from its saved reading: reading it again"))
                    self.post(self.set_status, _("The lab's numbering changed: examining the disc again…"))
                    fresh = self.mkv.read_titles(source)
                    expected = makemkv.makemkv_added(fresh)
                    again = self.current_plan(makemkv.named_titles(fresh, own), extras=opts["extras"], selected=opts["selected"])
                    numbers = {p["title_id"]: p["mk"] for p in again}
                    number = numbers.get(item["title_id"])
                    if number is None:
                        di.logger.warning(i18n.log("MakeMKV doesn't list {title_id} now: not ripped", title_id=item["title_id"]))
                    else:
                        shutil.rmtree(work, ignore_errors=True)
                        os.makedirs(work)
                        self.post(self.set_status, _("Bagging the evidence, {number} of {total}: {file}", number=i + 1, total=len(plan), file=item["dest"].split("/")[-1]))
                        ok = self.mkv.title(source, number, work, expected, progress=progress)
                files = [f for f in os.listdir(work) if f.lower().endswith(".mkv")]
                if ok and files and not self.cancelled:
                    dest = rip_path(os.path.join(out, *item["dest"].split("/")), item, rep)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(os.path.join(work, max(files, key=lambda f: os.path.getsize(os.path.join(work, f)))), dest)
                    done.append(dest)
                    di.logger.info("  " + i18n.log("ripped {dest}", dest=dest))
                elif not self.cancelled:
                    gone = self.disc_gone(drive)
                    why = ("numbered differently from the reading" if self.mkv.numbers_changed else "no file written" if ok
                           else "MakeMKV reported a failure, exit code %s" % getattr(self.mkv, "exit_code", "?"))
                    # (a disc taken out isn't a problem to report: the log has it, errors.log doesn't)
                    (di.logger.info if gone else di.logger.warning)((i18n.log("MakeMKV title {number} did not rip (the disc was removed)", number=number) if gone else
                         i18n.log("MakeMKV title {number} did not rip ({why})", number=number, why=why)))
                    if self.mkv.problem and not gone:
                        self.rip_problem = _("MakeMKV says: {problem}", problem=self.mkv.problem)
                    if gone:                      # (taken out mid-rip: the rest would only fail too)
                        shutil.rmtree(work, ignore_errors=True)
                        di.logger.info(i18n.log("the disc was removed: rip aborted"))
                        self.post(self.rip_done, done, len(plan), None, True)
                        return
                shutil.rmtree(work, ignore_errors=True)
            di.logger.info((i18n.log("rip finished: {ripped} of {total} titles (cancelled)", ripped=len(done), total=len(plan) + len(missing))
                            if self.cancelled else
                            i18n.log("rip finished: {ripped} of {total} titles", ripped=len(done), total=len(plan) + len(missing))))
            self.post(self.rip_done, done, len(plan) + len(missing), None)
        except Exception as exc:
            gone = self.disc_gone(drive)
            if gone:                     # (as a scan: its disc taken out isn't a problem to report)
                di.logger.info(i18n.log("rip stopped: the disc was removed ({name}: {error})", name=type(exc).__name__, error=exc))
            else:
                di.logger.exception(i18n.log("rip failed"))
            self.post(self.rip_done, [], len(plan), "%s: %s" % (type(exc).__name__, exc), gone)
        finally:
            if undo:
                try:
                    undo()
                except OSError as exc:
                    di.logger.warning(i18n.log("couldn't clean up after the sting: {error}", error=exc))

    def rip_done(self, done, planned, err, gone=False):
        """The rip's outcome, in the footer: green, all ripped; amber, called off or some didn't
        make it; red, it failed (or its disc was taken out: said so, no error to read)."""
        self.show_progress(False)
        self.set_busy(False)
        th.set_dot(self.rip_dot, th.BRASS_600 if self.rep else th.INK_SUBTLE)
        if done:
            self.last_out = os.path.dirname(done[0])
        if gone:
            self.set_outcome(ui_n("The disc was removed: rip aborted ({ripped} of {n} title ripped)", "The disc was removed: rip aborted ({ripped} of {n} titles ripped)", planned, ripped=len(done)),
                             th.WAX)
        elif err:
            self.set_outcome(_("The lab couldn't rip it: details in the logs"), th.WAX)
            messagebox.showerror(APP, err + _("\n\nDetails are in errors.log (Open logs folder, at the bottom of the window)."))
        elif self.cancelled:
            self.set_outcome(ui_n("Rip called off after {ripped} of {n} title", "Rip called off after {ripped} of {n} titles", planned, ripped=len(done)), th.AMBER_DOT)
        elif len(done) < planned and getattr(self, "rip_problem", None):
            self.set_outcome(ui_n("{ripped} of {n} title ripped. {problem}", "{ripped} of {n} titles ripped. {problem}", planned, ripped=len(done), problem=self.rip_problem), th.AMBER_DOT)
        elif len(done) < planned:
            self.set_outcome(ui_n("Evidence bagged: {ripped} of {n} title ripped (details in the logs)", "Evidence bagged: {ripped} of {n} titles ripped (details in the logs)", planned, ripped=len(done)),
                             th.AMBER_DOT)
        elif getattr(self, "rip_short", None):
            self.set_outcome((_("Evidence bagged, but {what}: compare it", what=short_reads(self.rip_short)) if len(self.rip_short) == 1 else
                              _("Evidence bagged, but {what}: compare them", what=short_reads(self.rip_short))), th.AMBER_DOT)
        else:
            self.set_outcome(ui_n("Evidence bagged: {ripped} of {n} title ripped", "Evidence bagged: {ripped} of {n} titles ripped", planned, ripped=len(done)), th.GREEN_DOT)

    def cancel(self):
        self.cancelled = True
        if self.mkv:
            self.mkv.stop()
        self.stop_detect()
        self.set_status(_("Cancelling…"), "warn")

    def stop_detect(self):
        """Detect titles called off: its crawl stops at its next step (menus.Cancelled, through its progress
        and its log). A call into the hdmv-menus library (opening a disc) finishes first, seconds at most."""
        if self.detecting:
            self.detect_stop.set()
            self.tell(_("Calling off the stakeout…"))

    def on_close(self):
        if self.busy and not messagebox.askyesno(APP, _("A job is still running. Quit anyway?")):
            return
        self.cancel()
        self.stop_prescan()
        from . import menus
        menus.stop_watching()                   # (a title playing in VLC: closed with the app)
        self.remember()
        di.logger.removeHandler(self._notes_handler)
        self.root.destroy()


def display_title(rep):
    """The big title: just what it is. Name (year), and season / disc for TV."""
    top = (rep.get("candidates") or [None])[0]
    if rep.get("kind") == "collection" and top:
        return top["name"]
    bs = rep.get("boxset")
    if rep.get("extras_disc") and bs and bs.get("title"):
        # a box set's bonus disc: the set (TheDiscDB files the disc under one of its films)
        return "%s (%s)" % (bs["title"], bs["year"]) if bs.get("year") else bs["title"]
    m = rep.get("match")
    if m:
        t = "%s (%s)" % (m["name"], m["year"]) if m.get("year") else m["name"]
        if m["kind"] == "tv" and m.get("season") == 0:
            t += "  ·  " + _("Specials")           # Plex's season 0 (a special on its own disc)
        elif m["kind"] == "tv" and m.get("season"):
            t += "  ·  " + (_("Season {season}, Disc {disc}", season=m["season"], disc=m["disc"]) if m.get("disc")
                            else _("Season {season}", season=m["season"]))
        return t
    guess = di.strip_tags(rep.get("best_guess") or "").split(" | ")[0]
    return guess or _("Unidentified disc")


def split_title(text):
    """"Chicken Run (2000)" -> ("Chicken Run", "(2000)"); anything after the year stays with it."""
    m = re.match(r"^(.*?)\s+(\(\d{4}\).*)$", text or "")
    return (m.group(1), m.group(2)) if m else (text, "")


ART_SOURCE_NAMES = {"TMDB": "TMDB", "TheDiscDB": "DISCDB", "disc": "DISC", "IMDb": "IMDB"}


def picture_source(rep, art):
    """The line under the picture: where it comes from, and the film's id there
    ("VERIFIED TMDb ASSET #11943"; no id for the disc's own: "VERIFIED Disc Art ASSET")."""
    src = art.get("source")
    ref = art.get("ref") or ""
    asset = None
    if src == "disc":
        where = _("Disc Art")
    elif src == "TMDB":            # its ref: "TMDb ID #11943"
        where, asset = "TMDb", (re.search(r"#\S+", ref) or [None])[0]
    elif src == "TheDiscDB" and art.get("what") == "box set cover":
        where, asset = "TheDiscDB", ref.partition("\u00b7")[2].strip() or None    # "TheDiscDB \u00b7 <set>"
    elif src == "TheDiscDB":       # (no id of its own: it files films by their IMDb id)
        where = "TheDiscDB"
    else:
        where = src or ""
    if not where:
        return ""
    return _("VERIFIED {source} ASSET {id}", source=where, id=asset) if asset else _("VERIFIED {source} ASSET", source=where)


def picture_format(rep):
    """(4K, HDR) for the disc's main video: whether its film (or episodes) are 2160 lines, and
    their HDR ("HDR10", "Dolby Vision") if any. A 4K disc's 1080p extras don't count."""
    main = [t for t in (rep or {}).get("titles") or [] if t.get("role") in (Role.FEATURE, Role.EPISODE)] or \
        [t for t in (rep or {}).get("titles") or [] if t.get("role") == Role.VERSION]
    vis = [t.get("video_info") or {} for t in main]
    uhd = any((v.get("height") or 0) >= 2160 for v in vis)
    hdr = next((v["hdr"] for v in vis if v.get("hdr")), None)
    return uhd, hdr


def rip_films(rep, plan):
    """The films a disc of short films is ripping, for the rip card: poster and "Name (year)",
    a second copy marked. Extras aren't listed (the summary counts them), and an ordinary disc
    gets nothing (its picture and title already say it)."""
    c = (rep.get("candidates") or [None])[0]
    if not (c and c.get("kind") == "collection" and di.adopt_guess(rep.get("candidates"))):
        return []
    items = {it["title_id"]: it for it in c.get("items") or []}
    for v in c.get("versions") or []:                       # a second copy of a film
        if v.get("same_as") in items:
            items.setdefault(v["title_id"], dict(items[v["same_as"]], copy=True))
    out = []
    for p in plan:
        it = items.get(p["title_id"])
        if it:
            name = "%s (%s)" % (re.sub(r"^.*?:\s*", "", it["name"]), it.get("year") or "?")
            out.append({"title_id": p["title_id"], "name": name + (" \u00b7 " + _("copy") if it.get("copy") else ""),
                        "image": (it.get("art") or {}).get("image"), "length": p.get("length")})
    return out


def plan_badge(rep, plan):
    """The rip card's tag: the one title it rips (and its chapters), or how many titles."""
    if not plan:
        return ""
    if len(plan) == 1:
        t = next((x for x in (rep or {}).get("titles") or [] if x["id"] == plan[0]["title_id"]), None)
        ch = (t or {}).get("chapters")
        label = plan[0]["title_id"].upper().replace("TITLE ", "TITLE #")
        return _("{title} ({n} CHAPTERS)", title=label, n=ch) if ch and ch > 1 else label
    return _("{n} TITLES", n=len(plan))


AUDIO_NAMES = {"en": N_("English"), "fr": N_("French"), "es": N_("Spanish"), "de": N_("German"), "it": N_("Italian"),
               "ja": N_("Japanese"), "pt": N_("Portuguese"), "nl": N_("Dutch"), "zh": N_("Chinese"), "ko": N_("Korean"),
               "ru": N_("Russian"), "th": N_("Thai"), "sv": N_("Swedish"), "da": N_("Danish"), "no": N_("Norwegian"),
               "fi": N_("Finnish"), "pl": N_("Polish"), "cs": N_("Czech"), "hu": N_("Hungarian"), "tr": N_("Turkish"),
               "he": N_("Hebrew"), "ar": N_("Arabic"), "hi": N_("Hindi")}


def audio_summary(rep, plan):
    """The main title's soundtracks, as the rip keeps them: "English (AC3 5.1) · French (AC3 2.0)"."""
    ids = [p["title_id"] for p in plan or []]
    t = next((x for x in (rep or {}).get("titles") or [] if x["id"] in ids), None)
    tracks = (t or {}).get("audio") or []
    if not tracks:
        return "\u2014"
    out = []
    for a in tracks:
        ch = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(a.get("channels"), str(a.get("channels") or ""))
        name = _(AUDIO_NAMES[a["lang"]]) if a.get("lang") in AUDIO_NAMES else (a.get("lang") or "?").upper()
        if a.get("kind") == "commentary":
            name = _("{language} commentary", language=name)
        out.append("%s (%s)" % (name, " ".join(x for x in (a.get("codec"), ch) if x)))
    return "  \u2022  ".join(out[:3]) + ("  " + _("+{count} more", count=len(out) - 3) if len(out) > 3 else "")


def detail_rows(rep):
    """The small print under the title, as (label, value) rows: ids and what the disc itself says."""
    rows = []
    m = rep.get("match") or {}
    if m.get("imdb_id"):
        rows.append(("IMDb", m["imdb_id"]))
    hit = (rep.get("discdb") or {}).get("hit")
    if hit:
        # this disc first, then the set TheDiscDB files it under: the same disc is often sold in
        # several releases (a Blu-ray that also comes in a 4K box), and TheDiscDB may list only one
        disc, rel = hit.get("disc"), hit.get("release")
        rows.append(("TheDiscDB", _("{disc}, in the {release} release", disc=disc, release=rel) if disc and rel
                     else (disc or rel or "")))
    bs = rep.get("boxset")
    if bs and bs.get("title"):
        name = "%s (%s)" % (bs["title"], bs["year"]) if bs.get("year") else bs["title"]
        if bs.get("number") and bs.get("count"):
            name += " \u00b7 " + _("disc {number} of {count}", number=bs["number"], count=bs["count"])
        rows.append(("Box set", name))
    if rep.get("label"):
        rows.append(("Disc label", rep["label"]))
    names = (rep.get("bd_meta") or {}).get("names") or []
    if names and names[0] != rep.get("label"):
        rows.append(("Disc title", names[0]))
    po = rep.get("playlist_obfuscation")
    if isinstance(po, dict):
        rows.append(("Protection", (ui_n("{n} decoy title · the real film is {title}", "{n} decoy titles · the real film is {title}",
                                         po["decoys"], title=po["picked"]) if po["sure"] else
                                    ui_n("{n} decoy title · best guess for the real film: {title}",
                                         "{n} decoy titles · best guess for the real film: {title}", po["decoys"],
                                         title=po["picked"]))))
    g = rep.get("episode_guess") or {}
    if g.get("source"):        # where the episode names came from, and in which order
        rows.append(("Episodes", g["note"].split(";")[0]))
    elif g.get("note"):        # no season on the label: why the episodes aren't named
        rows.append(("Episodes", g["note"]))
    if rep.get("mastered"):
        rows.append(("Mastered", rep["mastered"]))
    rows.append(("Fingerprint", rep.get("content_hash", "?")[:8]))
    return rows


def short_path(p, n=48):
    return p if len(p) <= n else p[:n // 2 - 2] + "\u2026" + p[-(n // 2 - 1):]


def describe_plan(plan, custom):
    """One plain sentence: what Rip will do."""
    if not plan:
        return _("No titles chosen to rip") if custom else _("Nothing to rip on this disc")
    roles = [p["role"] for p in plan]
    eps = [p for p in plan if p["role"] == Role.EPISODE]
    extras = sum(1 for r in roles if r == Role.EXTRA)
    if custom:
        head = ui_n("Will rip {n} chosen title", "Will rip {n} chosen titles", len(plan))
    elif eps:
        codes = [re.search(r"s(\d+)e(\d+)", p["dest"], re.I) for p in eps]
        codes = ["S%sE%s" % (c.group(1), c.group(2)) for c in codes if c]
        nums = [int(c.split("E")[1]) for c in codes]
        if len(codes) > 1 and nums == list(range(nums[0], nums[0] + len(nums))):
            span = " (%s–%s)" % (codes[0], codes[-1])
        elif codes:     # DVD order that isn't aired order (Firefly): list them as they are
            span = " (%s)" % ", ".join(codes[:5] + (["…"] if len(codes) > 5 else []))
        else:
            span = ""
        head = ui_n("Will rip {n} episode", "Will rip {n} episodes", len(eps)) + span
    elif roles.count(Role.FEATURE) > 1:
        eds = [(re.search(r"\{edition-([^}]*)\}", p["dest"]), p["length"]) for p in plan if p["role"] == Role.FEATURE]
        if all(m for m, _x in eds):     # one film in several editions (theatrical + extended, say)
            head = _("Will rip {n} editions: {editions}", n=len(eds),
                     editions=", ".join("%s (%s)" % (m.group(1), n) for m, n in eds))
        else:
            head = _("Will rip {n} films", n=roles.count("feature"))
    elif all(r == Role.EXTRA for r in roles):         # a film's special-features disc
        head = ui_n("Will rip {n} special feature", "Will rip {n} special features", len(plan))
        return head
    elif Role.FEATURE in roles or Role.LONGEST in roles:
        f = next(p for p in plan if p["role"] in (Role.FEATURE, Role.LONGEST))
        head = (_("Will rip the feature ({length})", length=f["length"]) if f["role"] == Role.FEATURE else
                _("Will rip the longest title ({length})", length=f["length"]))
    else:
        head = ui_n("Will rip {n} title", "Will rip {n} titles", len(plan))
    if extras and not custom:
        head += " + " + ui_n("{n} extra", "{n} extras", extras)
    return head


class ScrollArea(ttk.Frame):
    """A list that grows with its rows up to a share of the screen, then scrolls, so a long
    list never pushes a window's buttons off screen. Put rows in .inner."""

    def __init__(self, parent, max_frac=0.55):
        super().__init__(parent)
        bg = ttk.Style(self).lookup("TFrame", "background") or None
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, yscrollincrement=20,
                                **({"background": bg} if bg else {}))
        self.bar = th.ScrollBar(self, orient="vertical", command=self.canvas.yview, bg=bg)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        up, down = work_area(self)[1::2]
        self.max_h = int((down - up) * max_frac)      # (of the screen above the taskbar)
        self.inner.bind("<Configure>", lambda e: self.fit())
        # The wheel scrolls the list under the pointer, over its rows as well as its scrollbar.
        # One handler for the whole app (bind_all): on Windows wheel and trackpad events can go
        # to the window with the keyboard focus rather than the one under the pointer, so a
        # binding on the dialog alone misses them.
        self._acc = 0.0
        ScrollArea._areas.add(self)
        self.bind("<Destroy>", lambda e: e.widget is self and ScrollArea._areas.discard(self), add="+")
        root = self._root()
        if not getattr(root, "_dd_wheel", False):          # once per Tk root
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                root.bind_all(seq, ScrollArea._dispatch, add="+")
            try:
                # Tk 9: a trackpad sends <TouchpadScroll> instead of wheel events (Windows,
                # macOS); Tk's own scrollbars bind it, so without this only the bar scrolled
                root.bind_all("<TouchpadScroll>", ScrollArea._dispatch_touch, add="+")
            except tk.TclError:
                pass                                       # Tk 8.6: trackpads send wheel events
            root._dd_wheel = True

    _areas = set()

    @staticmethod
    def touch_dy(delta):
        """The vertical step of a <TouchpadScroll> event: %D packs horizontal into the high and
        vertical into the low 16 bits, each signed (65347 -> -189, a step up the page...)."""
        try:
            d = int(delta)
        except (TypeError, ValueError):
            return 0
        dy = d & 0xFFFF
        return dy - 0x10000 if dy >= 0x8000 else dy

    @staticmethod
    def _dispatch_touch(e):
        e.delta = ScrollArea.touch_dy(getattr(e, "delta", 0))
        e.num = None
        return ScrollArea._dispatch(e) if e.delta else None

    @staticmethod
    def _dispatch(e):
        """Send a wheel event to the scroll list under the pointer (or holding the widget)."""
        try:
            under = e.widget.winfo_containing(e.x_root, e.y_root) if (e.x_root or e.y_root) else None
        except (tk.TclError, AttributeError):
            under = None
        for w in (under, e.widget):
            name = str(w) if w is not None else ""
            for area in list(ScrollArea._areas):
                try:
                    if area.winfo_exists() and (name == str(area) or name.startswith(str(area) + ".")):
                        if name == str(area.bar):
                            return None              # the scrollbar scrolls itself
                        return area.roll(e)
                except tk.TclError:
                    ScrollArea._areas.discard(area)
        return None

    @staticmethod
    def wheel_scrolls(widget):
        """The wheel over a widget in a list (a dropdown) scrolls the list, as it does over the
        rest of it, rather than changing what the widget says."""
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, lambda e: (ScrollArea._dispatch(e), "break")[1])
        try:
            widget.bind("<TouchpadScroll>", lambda e: (ScrollArea._dispatch_touch(e), "break")[1])
        except tk.TclError:
            pass                                  # (before Tk 9: no such event)

    def fit(self):
        self.update_idletasks()
        w, h = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        self.canvas.configure(width=w, height=min(h, self.max_h), scrollregion=(0, 0, w, h))
        if h > self.max_h:
            self.bar.pack(side="right", fill="y")
            # (fewer rows: Show more unticked while scrolled down. The view stays where it was,
            # past the rows' end, blank; back to the end of them. The lists call this after a
            # refill: Tk resizes the rows' frame, and says so, only once it's in view again)
            last = h - self.max_h
            if self.canvas.canvasy(0) > last:
                self.canvas.yview_moveto(last / h)
        else:
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    def scrolls(self):
        return self.inner.winfo_reqheight() > self.max_h

    def roll(self, e):
        if not self.winfo_exists() or not self.scrolls():
            return None
        num = getattr(e, "num", None)
        if num in (4, 5):                                    # X11: one notch per button press
            units = -3 if num == 4 else 3
        else:
            try:
                delta = float(getattr(e, "delta", 0) or 0)
            except (TypeError, ValueError):
                return None
            if not delta:
                return None
            if self.tk.call("tk", "windowingsystem") == "aqua":
                units = -int(delta) or (-1 if delta > 0 else 1)  # macOS: delta is already lines
            else:
                # Windows: 120 a mouse notch (3 rows of 20 px); a trackpad sends many small
                # deltas, so add them up and scroll as they make whole rows
                self._acc += -delta / 40.0
                units = int(self._acc)
                self._acc -= units
                if not units:
                    return "break"
        self.canvas.yview_scroll(units, "units")
        return "break"


class DeskScroll(ScrollArea):
    """The main window's desk: as big as the window, scrolling (wheel or bar) only when the case
    on it is taller; its contents always take the window's full width."""

    def __init__(self, parent):
        super().__init__(parent)
        self.configure(style="Desk.TFrame")
        self.canvas.configure(background=th.PARCHMENT)
        self.bar.recolour(th.PARCHMENT)
        self.inner.configure(style="Desk.TFrame")
        self.item = self.canvas.find_all()[0]
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self.item, width=e.width), self.fit()))

    def fit(self):
        h, ch = self.inner.winfo_reqheight(), self.canvas.winfo_height()
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), max(h, ch)))
        if h > ch > 1:
            if not self.bar.winfo_manager():
                self.bar.pack(side="right", fill="y", before=self.canvas)
        else:
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    def scrolls(self):
        return self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1


class TitlePicker:
    """Rip menu > Choose titles: tick exactly which titles to rip. What each title is shows as
    it stands; Edit titles (Rip ▾ too) is where that's changed."""

    def __init__(self, root, app):
        self.app = app
        self.top = top = new_dialog(root, grab=True)
        top.title(_("Choose titles"))
        top.transient(root)
        ttk.Label(top, text=_("Tick the titles to rip."), style="Sub.TLabel").pack(anchor="w", padx=12, pady=(12, 6))
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", padx=12, pady=12)     # packed first: always on screen
        self.show_all = tk.BooleanVar(value=False)
        self.more = ttk.Checkbutton(top, variable=self.show_all, command=self.refill)
        self.area = ScrollArea(top, max_frac=0.45)     # (the rest scrolls: the window stays short)
        self.area.pack(fill="both", expand=True, padx=12)
        self.vars = {}
        self.current = None
        self.load()
        ttk.Button(btns, text=_("OK"), command=self.ok).pack(side="right")
        ttk.Button(btns, text=_("Cancel"), command=top.destroy).pack(side="right", padx=6)
        ttk.Button(btns, text=_("Default"), command=self.to_default).pack(side="left")

    ORDER = {Role.FEATURE: 0, Role.EPISODE: 1, Role.EXTRA: 2, Role.VERSION: 3, Role.PLAY_ALL: 4, Role.DUPLICATE: 5,
             Role.PROMO: 6, Role.SEGMENT: 7}
    # repeats of other titles, ads, the film's pieces: rarely wanted, so out of the way
    HIDDEN = (Role.PLAY_ALL, Role.DUPLICATE, Role.PROMO, Role.SEGMENT)
    ROLE_TEXT = {Role.PROMO: N_("Promo"), Role.SEGMENT: N_("Piece of the film"), Role.PLAY_ALL: N_("Play-all"),
                 Role.DUPLICATE: N_("Duplicate"), Role.FEATURE: N_("Feature"), Role.VERSION: N_("Version"), Role.EXTRA: N_("Extra"),
                 Role.EPISODE: N_("Episode"), Role.PART: N_("Part"), Role.LONGEST: N_("Longest"),
                 Role.DECOY: N_("Decoy"), Role.LOOP: N_("Loop"), Role.JUNK: N_("Junk")}

    def load(self):
        """The rows and their ticks, from the disc as it now stands (again after Edit titles: ticks
        you hadn't changed follow the new plan; ones you had stay)."""
        rep = self.app.rep
        old_default = getattr(self, "default", None)
        self.default = {p["title_id"] for p in plan_rip(rep, preview_titles(rep), self.app.extras.get())}
        if self.current is None:
            self.current = set(self.app.selected if self.app.selected is not None else self.default)
        else:
            for k, v in self.vars.items():
                (self.current.add if v.get() else self.current.discard)(k)
            if self.current == old_default:
                self.current = set(self.default)
        self.vars = {}
        rows = di.without_lost_copies([t for t in rep["titles"] if t["role"] in self.ORDER])
        self.names = {}
        for p in plan_rip(rep, preview_titles(rep), True, selected={t["id"] for t in rows}):
            self.names[p["title_id"]] = p["dest"].split("/")[-1]
        # what each title is, by the same word Edit titles starts from (yours, TheDiscDB's, the
        # menus', the scan's)
        items, guesses = discdb_items((rep.get("discdb") or {}).get("hit")), scan_names(rep)
        self.words = {t["id"]: title_word(t, rep.get("labels") or {}, items, rep.get("detected") or {}, guesses)[0]
                      for t in rows}

        def place(t):
            """Episodes by season and episode as they'll be named (then by title); the rest by title."""
            code = re.search(r"\bs(\d+)e(\d+)", self.names.get(t["id"], ""), re.I) if t["role"] == Role.EPISODE else None
            ep = (int(code.group(1)), int(code.group(2))) if code else (9999, t.get("episode_index") or 9999)
            return (self.ORDER[t["role"]], ep, di._natural(t["id"]))
        self.rows = sorted(rows, key=place)
        self.refill()

    def refill(self):
        for k, v in self.vars.items():               # keep ticks across showing / hiding
            (self.current.add if v.get() else self.current.discard)(k)
        body = self.area.inner
        for w in body.winfo_children():
            w.destroy()
        for col, head in enumerate(("", _("Title"), "", _("Length"), _("Video"), _("Saves as"))):
            ttk.Label(body, text=head, style="Sub.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 10))
        self.vars = {}
        roles = hidden_roles(self.app.extras.get())
        hidden = [t for t in self.rows if t["role"] in roles]
        shown = [t for t in self.rows if self.show_all.get() or t["role"] not in roles
                 or t["id"] in self.current]
        for r, t in enumerate(shown, 1):
            v = tk.BooleanVar(value=t["id"] in self.current)
            self.vars[t["id"]] = v
            ttk.Checkbutton(body, variable=v).grid(row=r, column=0, sticky="w")
            ttk.Label(body, text=self.what(t)).grid(row=r, column=1, sticky="w", padx=(0, 10))
            ttk.Label(body, text=t["id"], style="Sub.TLabel").grid(row=r, column=2, sticky="w", padx=(0, 10))
            ttk.Label(body, text=di.hms(t["duration"])).grid(row=r, column=3, sticky="e", padx=(0, 10))
            ttk.Label(body, text=t.get("video") or "", style="Sub.TLabel").grid(row=r, column=4, sticky="w",
                                                                                padx=(0, 10))
            ttk.Label(body, text=self.names.get(t["id"], ""), style="Sub.TLabel").grid(row=r, column=5, sticky="w")
        if hidden:
            self.more.config(text=more_text(hidden))
            self.more.pack(side="bottom", anchor="w", padx=12, before=self.area)
        else:
            self.more.pack_forget()
        self.area.fit()                         # (fewer rows: no word from Tk while scrolled past them)
        if self.top.state() != "withdrawn":    # grown (Show more): still on screen
            keep_on_screen(self.top)

    def what(self, t):
        """What a title is, by Edit titles' word on it, in plain words: its type and name
        ("Feature: Signature Edition", "Deleted Scene: Campfire", "Episode 2: The Spelling Bee");
        a copy or a play-all by what it repeats."""
        word = self.words.get(t["id"]) or {}
        twin = re.match(r"same video as (.+?)\s*$", t.get("note") or "") if t["role"] == Role.DUPLICATE else None
        if twin:
            return _("Copy of {title}", title=twin.group(1))
        if t["role"] == Role.PLAY_ALL and not word.get("type"):
            return play_all_text(t.get("note"))
        if word.get("type") == Type.EPISODE:
            kind = self.episode_text(t)
        elif word.get("type"):                    # in plain words: "Feature", "Deleted Scene"
            kind = type_words(word["type"])
        elif t["role"] == Role.EPISODE and not piece_text(t):
            kind = self.episode_text(t)
        else:
            kind = piece_text(t) or (_(self.ROLE_TEXT[t["role"]]) if t["role"] in self.ROLE_TEXT else t["role"].title())
        # (a second cut the scan named by its length or credits: "Extended")
        name = (word.get("title") or "").strip() or (t.get("edition") if t["role"] == Role.VERSION else "")
        return _("{kind}: {name}", kind=kind, name=name) if name else kind

    def episode_text(self, t):
        """"Episode 11": its number in the season as it will be named (your label, or the guess);
        a two-part title "Episode 11-12". Just "Episode" when there's no number to go by."""
        code = re.search(r"\bs\d+e(\d+)((?:-e\d+)*)\b", self.names.get(t["id"], ""), re.I)
        if not code:
            return _("Episode")
        nums = [int(code.group(1))] + [int(x) for x in re.findall(r"\d+", code.group(2) or "")]
        if len(nums) > 1:
            return _("Episode {first}-{last}", first=nums[0], last=nums[-1])
        return _("Episode {number}", number=nums[0])

    def to_default(self):
        self.current = set(self.default)
        for k, v in self.vars.items():
            v.set(k in self.default)

    def ok(self):
        chosen = (self.current - set(self.vars)) | {k for k, v in self.vars.items() if v.get()}
        self.app.selected = None if chosen == self.default else chosen
        self.top.destroy()
        self.app.options_changed()


class TitlesDialog:
    """Edit titles (Rip ▾, or Help TheDiscDB for a disc in your case files): what each
    title of a disc is, in TheDiscDB's terms. Each row starts from the best word on it (yours,
    then TheDiscDB's, then the menus' (Detect titles, here), then the scan's guess). A row you
    change is saved with the disc (through rescans, for rips and submission packets); what the
    menus found is saved only once confirmed (Confirm?, ticked for you when you change the row),
    and left be otherwise. With rep (the disc on the desk) its scan's guesses and Detect from
    menus come too."""

    ORDER = {Role.FEATURE: 0, Role.VERSION: 1, Role.EPISODE: 2, Role.EXTRA: 3, Role.PLAY_ALL: 4, Role.DUPLICATE: 5, Role.SEGMENT: 6,
             Role.PROMO: 7}
    HIDDEN = TitlePicker.HIDDEN
    ROLE_TYPES = ROLE_TYPES
    MIN_SECONDS = 60                  # (a disc from your case files: logos and menu loops aren't listed)

    def __init__(self, root, app, h, name, rep=None, on_save=None, on_close=None, review=False):
        # (review: Help TheDiscDB's look at a packet's titles, as Edit titles has them, MakeMKV's only, each
        # put right by its pencil, nothing to change otherwise; no Detect titles, nothing to watch)
        self.app, self.h, self.rep, self.on_save, self.on_close = app, h, rep, on_save, on_close
        self.review = review
        self.sent = set()                         # (review: the titles its packet labels)
        self.unnamed = set()                      # (and those you've typed but not named: left out till named)
        args = app.scan_args()
        self.db = args.db
        con = di.library_open(self.db)
        self.stored = {r["title_id"]: dict(r) for r in con.execute("SELECT * FROM titles WHERE hash=?", (h,))}
        # (the disc on the desk carries yours: read when it was scanned, kept up to date since)
        self.mine = rep["labels"] if rep is not None and "labels" in rep else di.get_labels(con, h)
        self.series = di.series_of(con, h)        # for episode names from season and number
        disc = con.execute("SELECT kind, format FROM discs WHERE hash=?", (h,)).fetchone()
        self.tv = (rep.get("kind") if rep is not None else disc and disc["kind"]) == "tv"
        self.format = (rep or {}).get("format") or (disc["format"] if disc else None)
        con.close()
        self.order = args.episode_order
        self.online = not args.offline
        if rep is not None:
            hit = (rep.get("discdb") or {}).get("hit")
        else:
            index = di.discdb_index_open(args.discdb_dir or di.default_discdb_dir(), allow_download=False)
            hit = di.index_hit(index, h) if index is not None else None
        self.items = discdb_items(hit)
        # a DVD on the desk: each title can be watched in VLC (its title numbers are the disc's)
        self.vlc = None
        if rep is not None and rep.get("format") in ("DVD", "Blu-ray") and rep.get("path") and not rep.get("from_sample"):
            from . import menus
            self.vlc = menus.find_vlc(app.settings.get("vlc") or None)
        # a Blu-ray's: in ffmpeg's ffplay, each playlist by its number (no VLC: no title list to
        # look up, and its short titles too)
        self.ffplay = None
        if rep is not None and rep.get("format") == "Blu-ray" and rep.get("path") and not rep.get("from_sample"):
            from . import bdmenus
            self.ffplay = bdmenus.find_ffplay(app.settings.get("ffplay") or None)
        # {title id: {"type", "title", "season", "episode", "from", "was": what it started as, "ok": confirmed}}
        self.state = {}
        self.locked = False                       # while Detect titles is under way
        self.unlocked = set()                     # rows with nothing to change, opened to change all the same (edit)
        self.clear_links = {}
        self.pictures = {}                        # {title id: the menu button that plays it} (load())
        self.viewer = None                        # MenuPicture, once one's been looked at
        self.guide = None                         # TypeGuide, once it's been asked for
        self.rows = []
        self.top = top = new_dialog(root, grab=on_close is not None)
        top.title(_("Titles for TheDiscDB") if review else _("Edit titles"))
        top.transient(root)
        top.configure(background=th.PARCHMENT)
        th.color_title_bar(top)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(18, 14, 18, 14))
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body, style="Desk.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=name, style="Title.TLabel", background=th.PARCHMENT).pack(side="left", anchor="w")
        self.detect_btn = None
        if rep is not None and app.can_detect() and not review:          # as Settings > Disc say
            self.detect_btn = ttk.Button(head, text=_("Detect titles"), style="Small.TButton", command=self.detect)
            self.detect_btn.pack(side="right")
        # Detect titles' news, just under the title, near the button, on a strip like the main
        # window's: a light (breathing while it's under way), a line, and a bar that moves
        self.news = th.frame(body, th.VELLUM_200, border=th.BRASS_300, radius=5, outer=th.PARCHMENT)
        nin = ttk.Frame(self.news, style="Bar.TFrame", padding=(10, 3))
        nin.pack(fill="x")
        ntop = ttk.Frame(nin, style="Bar.TFrame")                     # (the words on top, the bar under them)
        ntop.pack(fill="x")
        self.news_dot = th.dot(ntop, th.VELLUM_200, th.BRASS_400)
        self.news_dot.pack(side="left", padx=(0, 8))
        # (the time left, beside the words, while a long stakeout looks for its titles: App._crawl_estimate)
        self.news_pct = ttk.Label(ntop, text="", style="BarPct.TLabel")
        self.news_pct.pack(side="right", padx=(12, 0))
        self.news_bar = th.Gauge(nin, th.VELLUM_200, mode="indeterminate")
        self.news_lbl = ttk.Label(ntop, text="", style="BarStatus.TLabel")
        self.news_lbl.pack(side="left", fill="x", expand=True)
        self.intro = ttk.Label(body, text=(_("The titles this disc's packet labels (fingerprint {h}), as TheDiscDB will "
                                           "have them. To put a type or name right, use its pencil. To label another, "
                                           "put the disc in the drive and use Edit titles.", h=h[:8])) if review else
                               _("Change a title's type or name and it's saved to your case files with this "
                               "disc (fingerprint {h}), for rips and submission packets. To clear a saved title, use "
                               "its ✕. Promo is for ads.", h=h[:8]) +
                               (_(" What Detect titles finds is saved once you confirm it.") if self.detect_btn else ""),
                  style="DeskSub.TLabel", wraplength=720, justify="left")
        self.intro.pack(anchor="w", pady=(2, 10))
        # a disc of several films (the disc on the desk): which film each title is, and its name
        self.film_cells = {}
        self.coll_name = tk.StringVar(value="")
        self.namerow = ttk.Frame(body, style="Desk.TFrame")
        ttk.Label(self.namerow, text=_("Collection name"), style="DeskSub.TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(self.namerow, textvariable=self.coll_name, width=64).pack(side="left")
        self.films_of(rep)
        btns = ttk.Frame(body, style="Desk.TFrame")
        btns.pack(side="bottom", fill="x", pady=(10, 0))           # first: always on screen
        self.save_btn = ttk.Button(btns, text=_("Save"), command=self.save)
        self.save_btn.pack(side="right")
        ttk.Button(btns, text=_("Cancel"), command=self.close).pack(side="right", padx=(0, 8))
        self.show_all = tk.BooleanVar(value=False)
        self.more = ttk.Checkbutton(body, variable=self.show_all, command=self.refill)
        self.area = ScrollArea(body, max_frac=0.45)    # (the rest scrolls: the window stays short)
        self.area.pack(fill="both", expand=True)
        self.vars = {}
        self.load()
        self.refill()
        app.titles_dialog = self
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.bind("<Escape>", lambda e: self.close())
        centre_over(top, root)

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def close(self, ask=True):
        """The window closed: Cancel, Escape, its close box (ask: while Detect titles reads this disc's menus,
        whether to stop it, then stopped); Save's, asking nothing (the reading goes on, its labels kept)."""
        from . import menus
        if ask and self.app.detecting and self.app.rep is self.rep:
            # (Detect titles still reading this disc's menus: asked first, then called off with the window)
            if not messagebox.askyesno(APP, _("Detect titles is still reading this disc's menus. Stop it?"),
                                       parent=self.top):
                return
            self.app.stop_detect()
        menus.stop_watching()                   # (a title it played in VLC: closed with it)
        for w in (self.viewer, self.guide):
            if w is not None and w.alive():
                w.top.destroy()
        self.top.destroy()
        if self.app.titles_dialog is self:
            self.app.titles_dialog = None
        if self.on_close:
            self.on_close()

    def makemkv_listed(self):
        """The titles of this disc MakeMKV's saved reading has (a TheDiscDB packet's, matched to the scan's as
        the packet does), or None with no reading saved."""
        for path in (di.makemkv_log_store(self.h), di.makemkv_old_log_store(self.h)):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    mk = di.parse_makemkv_log(fh.read())
            except OSError:
                continue
            pairs = di.map_makemkv(mk["titles"], list(self.stored.values()), self.format)
            return {t["title_id"] for t, _x in pairs.values() if t}
        return None

    def packet_rows(self):
        """The disc's titles as its TheDiscDB packet has them (di.packet_labels, from the lab's reading): (the ones
        it labels, the ones it leaves blank that aren't a play-all, a copy, a piece or a loop). With no reading
        saved: the titles you've labelled, and nothing else."""
        for path in (di.makemkv_log_store(self.h), di.makemkv_old_log_store(self.h)):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            con = di.library_open(self.db)
            try:
                entries = di.packet_labels(con, self.h, text)[4]
            except SystemExit:                    # (a reading with no titles)
                entries = []
            finally:
                con.close()
            # (one you've given a type but no name: in view, to name, though the packet leaves it out)
            self.unnamed = {e["our_title"] for e in entries if e["our_title"] and e.get("unnamed")}
            sent = {e["our_title"] for e in entries if e["our_title"] and e["sent"]} | self.unnamed
            blank = {e["our_title"] for e in entries if e["our_title"] and not e["sent"] and
                     e["role"] not in (Role.PLAY_ALL, Role.DUPLICATE, Role.SEGMENT, Role.LOOP) and
                     not e["note"].startswith("leave unlabeled (part ")}
            return sent, blank
        return {tid for tid, my in self.mine.items() if tid in self.stored and (my["type"] or my["title"])}, set()

    # ---- what each title is ----
    def load(self):
        """The rows, and each row's best word (keeping what you've made yours here already)."""
        stored = self.stored
        if self.rep is not None:
            titles = [{"id": t["id"], "role": t["role"], "duration": t["duration"], "note": t.get("note"),
                       "clips": t.get("clip_list") or [], "episode_index": t.get("episode_index"),
                       "sub": tracks_text((stored.get(t["id"]) or {}).get("tracks")) or t.get("video") or ""}
                      for t in self.rep.get("titles") or [] if t["role"] in self.ORDER]
        elif self.review:
            # (Help TheDiscDB's: the titles the packet labels, and under Show more those it leaves blank that aren't
            # a play-all, a copy, a piece or a menu loop: one missing is labelled with the disc in the drive)
            sent, blank = self.packet_rows()
            self.sent = sent
            titles = [{"id": tid, "role": t["role"], "duration": t["duration"] or 0, "note": t.get("note"),
                       "clips": (t["clips"] or "").split(",") if t.get("clips") else [],
                       "episode_index": t.get("episode_index"), "sub": tracks_text(t.get("tracks"))}
                      for tid, t in stored.items() if tid in sent or tid in blank]
        else:
            # (from your case files: the titles MakeMKV's saved reading has, a packet's, however short; with
            # none saved, the longer ones the scan kept)
            listed = self.makemkv_listed()
            # (as the disc on the desk has them: your labels applied, a title you named an extra, its pieces set aside)
            known = [dict(t, id=t["title_id"], clip_list=t.get("clips"), duration=t["duration"] or 0) for t in stored.values()]
            di.apply_labels(known, self.mine)
            if listed is not None:
                keep = lambda t: t["id"] in listed and t["role"] in self.ORDER
            else:
                keep = lambda t: t["role"] in self.ORDER and t["duration"] >= self.MIN_SECONDS
            titles = [{"id": t["id"], "role": t["role"], "duration": t["duration"], "note": t.get("note"),
                       "clips": (t["clips"] or "").split(",") if t.get("clips") else [],
                       "episode_index": t.get("episode_index"), "sub": tracks_text(t.get("tracks"))}
                      for t in known if keep(t)]
        self.rows = sorted(di.without_lost_copies(titles), key=lambda t: (self.ORDER.get(t["role"], len(self.ORDER)), t.get("episode_index") or 0,
                                                                          di._natural(t["id"])))
        guesses = self.scan_names()
        # the menu pages Detect titles saw: for each title a button plays, that button (this
        # session's pictures: gone when the app closes, unless kept as test data)
        pics = (((self.rep or {}).get("menu_found") or {}).get("pictures")) or {}
        self.pictures = {menu_title_id(t): v for t, v in pics.items() if os.path.isfile(v.get("image") or "")}
        detected = (self.rep or {}).get("detected") or {}
        for t in self.rows:
            tid = t["id"]
            if self.state.get(tid) and (self.state[tid]["ok"] or self.changed(tid)):
                continue                          # changed or confirmed here: stays
            word, source = title_word(t, self.mine, self.items, detected, guesses)
            # (the title box: its name with its description after the mark, as stored, kinds.join_title)
            row = {"type": word.get("type") or "", "title": word.get("text") or join_title(word.get("title"), word.get("description")),
                   "season": str(word["season"]) if word.get("season") else "",
                   "episode": str(word["episode"]) if word.get("episode") else ""}
            self.state[tid] = dict(row, was=dict(row), ok=False, auto=None, **{"from": source})
            # (a disc whose own files name its titles, a BD-J menu's: one they don't name isn't in its menus, and
            # probably nothing to look into: no guess on it, its row blank, after the named ones)
            if source == "scan" and (self.rep or {}).get("bd_project") and t["role"] not in (Role.FEATURE, Role.VERSION, Role.EPISODE):
                blank = {k: "" for k in self.FIELDS}
                self.state[tid].update(blank, was=dict(blank))
        self.rows.sort(key=lambda t: (self.ORDER.get(t["role"], len(self.ORDER)),) + found_order(t["id"], self.state[t["id"]]["was"], detected,
                                                                            t.get("episode_index") or 0))

    FIELDS = ("type", "title", "season", "episode")

    found_by_menus = staticmethod(found_by_menus)

    def changed(self, tid):
        s = self.state[tid]
        return any(s[k].strip() != s["was"][k].strip() for k in self.FIELDS)

    def discdb_item(self, t):
        return discdb_item(self.items, t)

    def scan_names(self):
        return scan_names(self.rep)

    # ---- the table ----
    def refill(self):
        g = self.area.inner
        for w in g.winfo_children():
            w.destroy()
        heads = (N_("TITLE"), N_("LENGTH"), N_("TYPE"), N_("NAME"), N_("S"), N_("E")) + \
            ((N_("FILM"),) if self.collection else ()) + (N_("CONFIRM?"), "")
        self.cols = {h: c for c, h in enumerate(heads)}
        self.heads = {}
        for c, head in enumerate(heads):
            if head == "TYPE":                    # what each type is for: a click away
                self.heads[head] = th.link(g, _(head), self.show_types, style="Cap.TLabel", icon="help", right=True)
                th.Tooltip(self.heads[head], lambda: _("What each type is for"))
            else:
                self.heads[head] = ttk.Label(g, text=_(head) if head else "", style="Cap.TLabel")
            self.heads[head].grid(row=0, column=c, sticky="w", padx=(0, 8), pady=(0, 4))
        # in view: the film or the episodes (the extras too, when they're ripped); the rest under Show more,
        # confirmed there or not (under_more): each part in order, by type, then the menus' pages
        include = self.app.extras.get() or self.review      # (TheDiscDB's: its extras are the point)
        detected = (self.rep or {}).get("detected") or {}

        def order(t):
            # (a copy, or a Play All as the scan has it: nothing to do with it there, so last of all)
            key = found_order(t["id"], self.state[t["id"]]["was"], detected, t.get("episode_index") or 0)
            # (no word on it from anyone, the menus not naming it: after the named ones, nothing to look into)
            was = self.state[t["id"]]["was"]
            unnamed = self.state[t["id"]]["from"] == "scan" and not (was.get("type") or was.get("title"))
            return (self.inert(t, opened=False), unnamed) + key[:1] + (self.ORDER.get(t["role"], len(self.ORDER)),) + key[1:]
        if self.review:                           # (Help TheDiscDB's: what goes in labelled, the rest blank)
            under = lambda t: t["id"] not in self.sent
        else:
            under = lambda t: under_more(t["role"], self.state[t["id"]]["was"].get("type"), include)
        hidden = sorted((t for t in self.rows if under(t)), key=order)
        shown = sorted((t for t in self.rows if not under(t)), key=order) + (hidden if self.show_all.get() else [])
        self.shown = [t["id"] for t in shown]     # (the rows, in their order)
        self.vars, self.episode_boxes, self.clear_links = {}, {}, {}
        for r, t in enumerate(shown, 1):
            self.add_row(g, r, t)
        if hidden:
            self.more.config(text=(_("Show {count} more: titles that go in blank", count=len(hidden))) if self.review
                             else more_text(hidden))
            self.more.pack(side="bottom", anchor="w", pady=(6, 0), before=self.area)
        else:
            self.more.pack_forget()
        self.episode_heads()
        on_show = {t["id"] for t in shown}
        self.heads["CONFIRM?"].config(text=_("CONFIRM?") if on_show & set(self.found()) else "")
        state = "disabled" if self.locked else "normal"
        self.save_btn.config(state=state)
        self.more.config(state=state)
        self.area.fit()                         # (fewer rows: no word from Tk while scrolled past them)
        if self.top.state() != "withdrawn":    # grown (Detect titles' rows, Show more): still on screen
            keep_on_screen(self.top)

    def inert(self, t, opened=True):
        """Whether a row has nothing to change: a copy (only what it copies to say), a piece of a named
        title, or a Play All as the scan has it (the titles it plays). Not one opened to change all the
        same (edit; opened=False: as it was, for the rows' order)."""
        if opened and t["id"] in self.unlocked:
            return False
        copy = t["role"] == Role.DUPLICATE and re.match(r"same video as (.+?)\s*$", t.get("note") or "")
        return bool(copy) or bool(piece_text(t)) or (t["role"] == Role.PLAY_ALL and self.state[t["id"]]["from"] == "scan")

    def found(self):
        """The rows Detect titles has a word on (to confirm, or not)."""
        return [tid for tid, s in self.state.items() if s["from"] == "menus"]

    def add_row(self, g, r, t):
        tid, s, col = t["id"], self.state[t["id"]], self.cols
        cell = ttk.Frame(g)
        cell.grid(row=r, column=col["TITLE"], sticky="w", padx=(0, 8), pady=2)
        line = ttk.Frame(cell)
        line.pack(anchor="w")
        ttk.Label(line, text=tid).pack(side="left")
        pic = self.pictures.get(tid)
        if pic:                                   # the menu button that plays it, to look at
            see = th.link(line, "", lambda: self.show_picture(tid), style="DeskLink.TLabel", icon="picture")
            see.pack(side="left", padx=(6, 0))
            th.Tooltip(see, lambda: _("The menu button that plays it") + (
                _(": “{button_title}”", button_title=self.button_title(tid)) if self.button_title(tid) else ""))
        if (self.vlc and re.fullmatch(r"Title \d+", tid)) or (self.ffplay and re.fullmatch(r"\d+\.(mpls|m2ts)", tid)) or \
                (self.vlc and tid in self.seen_playing()):
            watch = th.link(line, "", lambda: self.watch(tid), style="DeskLink.TLabel", icon="play")
            watch.pack(side="left", padx=(6, 0))
            th.Tooltip(watch, lambda: _("Watch it in VLC") if tid in self.seen_playing() or tid.startswith("Title ") else _("Watch it"))
        if t.get("sub"):
            ttk.Label(cell, text=t["sub"], style="Sub.TLabel").pack(anchor="w")
        ttk.Label(g, text=di.hms(t["duration"])).grid(row=r, column=col["LENGTH"], sticky="w", padx=(0, 8))
        if self.review and tid not in self.sent:  # (goes in blank: labelled with the disc in the drive, not here)
            ttk.Label(g, text="—", style="Sub.TLabel").grid(row=r, column=col["TYPE"], sticky="w", padx=(0, 6))
            return
        if self.inert(t) and not self.review:     # a copy, a piece, a Play All: nothing to say of it but what it is
            twin = re.match(r"same video as (.+?)\s*$", t.get("note") or "") if t["role"] == Role.DUPLICATE else None
            said = _("Copy of {title}", title=twin.group(1)) if twin else piece_text(t) or play_all_text(t.get("note"))
            ttk.Label(g, text=said, style="Sub.TLabel").grid(row=r, column=col["TYPE"], columnspan=4, sticky="w", padx=(0, 6))
            # (to put right all the same: its type and name brought in)
            pen = th.link(g, "", lambda: self.unlock(tid), style="DeskLink.TLabel", icon="edit")
            pen.grid(row=r, column=col[""], sticky="w")
            th.Tooltip(pen, lambda: _("Edit it"))
            return
        if self.review and tid not in self.unlocked:     # (to look over: its pencil to put it right)
            ttk.Label(g, text={Type.PROMO: Type.TRAILER}.get(s["type"], s["type"]) or "—",
                      style="Sub.TLabel" if not s["type"] else "TLabel").grid(
                row=r, column=col["TYPE"], sticky="w", padx=(0, 6))
            if tid in self.unnamed and not s["title"]:   # (TheDiscDB's form needs one: its pencil to give it)
                ttk.Label(g, text=_("needs a name"), style="Sub.TLabel").grid(row=r, column=col["NAME"], sticky="w", padx=(0, 6))
            else:
                ttk.Label(g, text=s["title"] or "").grid(row=r, column=col["NAME"], sticky="w", padx=(0, 6))
            if self.tv and (s["season"] or s["episode"]):
                ttk.Label(g, text=s["season"] or "").grid(row=r, column=col["S"], sticky="w", padx=(0, 4))
                ttk.Label(g, text=s["episode"] or "").grid(row=r, column=col["E"], sticky="w", padx=(0, 8))
            pen = th.link(g, "", lambda: self.unlock(tid), style="DeskLink.TLabel", icon="edit")
            pen.grid(row=r, column=col[""], sticky="w")
            th.Tooltip(pen, lambda: _("Edit it"))
            return
        v = {k: tk.StringVar(value=s[k]) for k in ("type", "title", "season", "episode")}
        v["ok"] = tk.BooleanVar(value=s["ok"])
        off = self.locked                         # (Detect titles under way: hands off)
        kind = ttk.Combobox(g, textvariable=v["type"], values=("",) + di.LABEL_TYPES, width=12,
                            state="disabled" if off else "readonly")
        kind.grid(row=r, column=col["TYPE"], sticky="w", padx=(0, 6))
        ScrollArea.wheel_scrolls(kind)            # (not the type under the pointer)
        ttk.Entry(g, textvariable=v["title"], width=30, state="disabled" if off else "normal").grid(
            row=r, column=col["NAME"], sticky="we", padx=(0, 6))
        se = ttk.Entry(g, textvariable=v["season"], width=3, state="disabled" if off else "normal")
        ep = ttk.Entry(g, textvariable=v["episode"], width=5, state="disabled" if off else "normal")
        se.grid(row=r, column=col["S"], sticky="w", padx=(0, 4))
        ep.grid(row=r, column=col["E"], sticky="w", padx=(0, 8))
        self.episode_boxes[tid] = (se, ep)
        if self.collection:
            self.film_cells[tid] = ttk.Frame(g)
            self.film_cells[tid].grid(row=r, column=col["FILM"], sticky="w", padx=(0, 8))
            self.draw_film(tid)
        if s["from"] == "menus":                  # what the menus found: saved once confirmed
            ttk.Checkbutton(g, variable=v["ok"], command=lambda: self.confirmed(tid),
                            state="disabled" if off else "normal").grid(row=r, column=col["CONFIRM?"])
        self.vars[tid] = v
        # (its values, if any, cleared in a click)
        self.clear_links[tid] = clear = th.link(g, "", lambda: self.clear_row(tid), style="DeskLink.TLabel", icon="clear")
        clear.grid(row=r, column=col[""], sticky="w")
        th.Tooltip(clear, lambda: _("Clear it"))
        self.show_clear(tid)
        for w in (se, ep):
            w.bind("<FocusOut>", lambda e, tid=tid: self.fill_episode(tid))
        for k in ("type", "title", "season", "episode"):
            v[k].trace_add("write", lambda *a, tid=tid, k=k: self.edited(tid, k))
        self.show_episode(tid)

    def unlock(self, tid):
        """A row with nothing to change (a copy, a piece, a Play All) opened to change all the same: its
        type and name brought in, where it is."""
        if self.locked:
            return
        self.unlocked.add(tid)
        self.refill()

    def clear_row(self, tid):
        """A row's values all cleared (its type, name, season, episode), as if you'd emptied each."""
        v = self.vars.get(tid)
        if v is None or self.locked:
            return
        for k in ("type", "title", "season", "episode"):
            if v[k].get():
                v[k].set("")

    def show_clear(self, tid):
        """The row's clear link, while it has a value to clear."""
        v, link = self.vars.get(tid), self.clear_links.get(tid)
        if v is None or link is None:
            return
        if any(v[k].get().strip() for k in ("type", "title", "season", "episode")) and not self.locked:
            link.grid()
        else:
            link.grid_remove()

    def edited(self, tid, k):
        """A change to a row (saved, as it's changed). One of the menus' rows given a type or a
        name of your own is confirmed for you (not for the name we filled in from its episode)."""
        v, s = self.vars[tid], self.state[tid]
        s[k] = v[k].get()
        self.show_clear(tid)
        if k == "type":
            self.show_episode(tid)
            self.episode_heads()
            self.fill_episode(tid)
            if self.collection:                   # no longer a film: no film for it
                if s["type"] not in self.FILM_TYPES and tid in self.films:
                    named = self.coll_name.get().strip() != self.films_name(self.films)
                    del self.films[tid]
                    if not named and self.films:
                        self.coll_name.set(self.films_name(self.films))
                for t in self.film_cells:
                    self.draw_film(t)
        if s["from"] != "menus" or (k == "type" and not s["type"]) or \
                (k == "title" and s["auto"] is not None and s["title"] == s["auto"]):
            return
        self.confirm(tid)

    def confirm(self, tid, ok=True):
        self.state[tid]["ok"] = ok
        v = self.vars.get(tid)
        if v and v["ok"].get() != ok:
            v["ok"].set(ok)

    def confirmed(self, tid):
        self.confirm(tid, self.vars[tid]["ok"].get())

    # ---- a disc of several films: which film each title is ----
    FILM_TYPES = (Type.MAIN_MOVIE, Type.SHORT)

    def films_of(self, rep):
        """The disc's films as its scan has them (rep): which title each is, whose film each
        extra is (its menu: rips with it), and the disc's name (its row shown for several films)."""
        top_c = ((rep or {}).get("candidates") or [None])[0]
        self.collection = bool(rep is not None and top_c and top_c.get("kind") == "collection"
                               and di.adopt_guess(rep["candidates"]))
        self.films = {it["title_id"]: {"imdb_id": it["imdb_id"], "name": it["name"], "year": it.get("year")}
                      for it in top_c["items"]} if self.collection else {}
        self.films_was = dict(self.films)
        self.extras_of(rep)
        self.coll_was = di.strip_tags(top_c["name"]) if self.collection else ""
        self.coll_name.set(self.coll_was)
        if self.collection and not self.namerow.winfo_manager():
            self.namerow.pack(fill="x", pady=(0, 10), after=self.intro)
        elif not self.collection:
            self.namerow.pack_forget()

    def extras_of(self, rep):
        """Whose film each extra is (its film's menu: it rips with it), as the disc's answer and
        Detect titles say, kept as the film's title ({extra: film title}): a film corrected here
        (Change…, a swap), its extras go with the film its title is now. Shown under FILM."""
        top_c = ((rep or {}).get("candidates") or [None])[0]
        title_of = {f["imdb_id"]: tid for tid, f in self.films.items()}
        self.extra_of = {tid: title_of[i] for tid, i in collection_extras(rep, top_c).items()
                         if i in title_of} if self.collection and top_c else {}

    def extra_film(self, tid):
        """The film an extra goes with, as its film's title has it now (None: that title's no film now)."""
        return self.films.get(self.extra_of.get(tid))

    def rescanned(self, rep):
        """The disc on the desk scanned again while this is open (Detect titles put its films in
        order, say): its rows, films, menu pictures and name as the new scan has them. Rows you've
        changed here stay as you have them."""
        if not self.alive():
            return
        self.rep = rep
        if "labels" in rep:
            self.mine = rep["labels"]
        self.films_of(rep)
        self.load()
        self.refill()

    @staticmethod
    def films_name(films):
        """The name a disc of several films gets when nobody's named it: its films, title by title."""
        return " & ".join(films[t]["name"] for t in sorted(films, key=di._natural))

    def draw_film(self, tid):
        """A film's row: the film it is and Change… (Choose… for one with none yet)."""
        cell = self.film_cells.get(tid)
        if cell is None or not cell.winfo_exists():
            return
        for w in cell.winfo_children():
            w.destroy()
        if self.state[tid]["type"] not in self.FILM_TYPES and tid not in self.films:
            film = self.extra_film(tid)           # an extra on a film's menu: it rips with that film
            if film:
                fitted_label(cell, film["name"], "Sub.TLabel", 36).pack(side="left")
            return
        f = self.films.get(tid)
        fitted_label(cell, _("{name} ({year})", name=f["name"], year=f.get("year") or "?") if f else _("No film yet"),
                     "TLabel" if f else "Sub.TLabel", 30).pack(side="left")
        ttk.Button(cell, text=_("Change…") if f else _("Choose…"), style="Small.TButton",
                   state="disabled" if self.locked else "normal",
                   command=lambda: self.change_film(tid)).pack(side="left", padx=(6, 0))

    def change_film(self, tid):
        f = self.films.get(tid)
        MatchDialog(self.top, self.app, title=f["name"] if f else "", picked=dict(f, kind="movie") if f else None,
                    on_pick=lambda c: self.set_film(tid, c))

    def set_film(self, tid, c):
        """tid is film c now. A film another title had: the two swap (the usual mix-up)."""
        if not self.alive():
            return
        named = self.coll_name.get().strip() != self.films_name(self.films)      # (yours: kept)
        f = {"imdb_id": c["imdb_id"], "name": c["name"], "year": c.get("year")}
        other = next((k for k, v in self.films.items() if v["imdb_id"] == f["imdb_id"] and k != tid), None)
        if other is not None:
            if tid in self.films:
                self.films[other] = self.films[tid]
            else:
                del self.films[other]
            self.draw_film(other)
        self.films[tid] = f
        for t in self.film_cells:
            self.draw_film(t)
        if not named:                             # (a name from its films: theirs, in their new order)
            self.coll_name.set(self.films_name(self.films))

    def films_answer(self):
        """The disc's films, changed here, as your answer for it (None: unchanged, or none left)."""
        if not self.collection or not self.films:
            return None
        name = self.coll_name.get().strip() or self.coll_was
        if self.films == self.films_was and name == self.coll_was:
            return None
        # (each extra with the film its film's title is now: a film corrected here, its extras too)
        extras = {tid: self.extra_film(tid)["imdb_id"] for tid in self.extra_of if self.extra_film(tid)}
        return di.truth_for_collection(name, [(tid, self.films[tid]["imdb_id"])
                                              for tid in sorted(self.films, key=di._natural)], extras)

    # season and number: for an episode (or an episode's deleted scene) only
    NAMED = {Type.EPISODE: "%s", Type.DELETED_SCENE: "%s Deleted Scene"}

    def numbered(self, kind):
        """Whether a title of this type has a season and episode: an episode, or a TV disc's extra
        (an episode's own, if it's given one)."""
        return kind == Type.EPISODE or self.tv and kind not in ("", None, Type.MAIN_MOVIE, Type.PROMO)

    def show_episode(self, tid):
        se, ep = self.episode_boxes[tid]
        for w in (se, ep):
            if self.numbered(self.state[tid]["type"]):
                w.grid()
            else:
                w.grid_remove()

    def episode_heads(self):
        any_ep = any(self.numbered(self.state[tid]["type"]) for tid in self.vars)
        for h in ("S", "E"):
            self.heads[h].config(text=_(h) if any_ep else "")

    def named(self, kind, episode_title):
        return self.NAMED[kind] % episode_title if episode_title and kind in self.NAMED else None

    def fill_episode(self, tid):
        """An episode with a season and number: its title from TVmaze or IMDb, unless you've typed
        one."""
        v, s = self.vars[tid], self.state[tid]
        season, episode = s["season"].strip(), s["episode"].strip()
        if not (season.isdigit() and di.episode_numbers(episode)) or not self.numbered(s["type"]):
            return
        kind = s["type"]
        if s["title"].strip() and s["title"] != s["auto"]:
            return                                # a name you typed stays
        key = (season, episode)
        s["want"] = key

        def work():
            name = self.named(kind, di.episode_name(self.series, season, episode, self.order, online=self.online))

            def done():
                if self.alive() and s.get("want") == key and name and \
                        (not s["title"].strip() or s["title"] == s["auto"]):
                    s["auto"] = name
                    if self.vars.get(tid) is v:
                        v["title"].set(name)
                    else:
                        s["title"] = name
            self.app.post(done)
        threading.Thread(target=work, daemon=True).start()

    def seen_playing(self):
        """A Blu-ray's titles the stakeout saw played: {title id: {"title": VLC's number for it}}."""
        return ((self.rep or {}).get("menu_raw") or {}).get("watch") or {}

    def watch(self, tid):
        """The title, played in VLC's own window (not while the drive is being read)."""
        from . import menus
        if self.app.busy or getattr(self.app, "mkv_scan", None):
            self.show_outcome(_("The disc is busy: watch it once the scan or rip is done"), th.WAX)
            return
        # (a Blu-ray's playlist: ffplay, by its own number, whenever it's installed; else VLC, for a title
        # the stakeout saw it play, by VLC's number for it)
        place = self.seen_playing().get(tid)
        if tid.endswith(".m2ts"):
            self.watch_clip(tid)
            return
        if tid.endswith(".mpls") and (self.ffplay or not place):
            from . import bdmenus
            t = next((x for x in self.rep.get("titles") or [] if x["id"] == tid), {})
            name = title_parts((self.state.get(tid) or {}).get("title"))[0]
            if not bdmenus.watch_playlist(self.rep["path"], tid, self.ffplay, title=_("{tid}  {name}", tid=tid, name=name) if name else tid,
                                          video=t.get("video_info")):
                self.show_outcome(_("ffmpeg's player (ffplay) wasn't found: install ffmpeg to watch Blu-ray titles"), th.WAX)
            return
        if place:
            from . import bdmenus
            if not bdmenus.watch_vlc(self.vlc, self.rep["path"], place["title"]):
                self.show_outcome(_("VLC's player wasn't found beside it: reinstall VLC, or set its folder in Settings"), th.WAX)
            return
        if not menus.watch_title(self.vlc, self.rep["path"], int(tid.split()[1])):
            self.show_outcome(_("VLC's player wasn't found beside it: reinstall VLC, or set its folder in Settings"),
                              th.WAX)

    def watch_clip(self, tid):
        """A clip played whole in ffplay (bdmenus.watch_clip): an episode only a Play All plays, the disc's decrypted
        by the user's MakeMKV as it's read. As quick as a playlist's, so no word while it opens: only if it can't."""
        from . import bdmenus
        t = next((x for x in self.rep.get("titles") or [] if x["id"] == tid), {})
        name = title_parts((self.state.get(tid) or {}).get("title"))[0]
        shown = name or tid

        def failed(why):                    # (from its thread: in the window, if it's still open)
            self.app.post(lambda: self.alive() and self.show_outcome(
                _("Couldn't play {name}: {why}", name=shown, why=why), th.WAX))
        how = bdmenus.watch_clip(self.rep["path"], tid, self.ffplay, title=_("{tid}  {name}", tid=tid, name=name) if name else tid,
                                 video=t.get("video_info"), on_fail=failed)
        if how == "ffplay":
            self.show_outcome(_("ffmpeg's player (ffplay) wasn't found: install ffmpeg to watch Blu-ray titles"), th.WAX)
        elif how == "library":
            self.show_outcome(_("MakeMKV's decryption library wasn't found: reinstall MakeMKV to watch this episode"), th.WAX)
        elif how == "file":
            self.show_outcome(_("{name} isn't on the disc in the drive", name=shown), th.WAX)

    def button_title(self, tid):
        """The words on the menu button that plays tid: its name as put right, else as read; "" if none was."""
        said = ((self.rep or {}).get("detected") or {}).get(tid) or {}
        if said.get("read") and said.get("title"):
            return said["title"]
        return " ".join(((self.pictures.get(tid) or {}).get("text") or "").split())

    def show_picture(self, tid):
        """The menu page with the button that plays tid outlined (one window, reused)."""
        v = self.viewer
        if v is None or not v.alive():
            v = self.viewer = MenuPicture(self.top)
        pic = dict(self.pictures[tid])
        said = ((self.rep or {}).get("detected") or {}).get(tid) or {}
        if said.get("read") and said.get("title"):  # (its name put right: as read, and as put right)
            pic.update(read_as=said["read"], name=said["title"])
        v.show(tid, pic)

    def show_types(self):
        """What each type is for (one window, kept to the front)."""
        if self.guide is not None and self.guide.alive():
            self.guide.top.lift()
        else:
            self.guide = TypeGuide(self.top)

    # ---- Detect titles: its news shows here ----
    def detect(self):
        self.detect_btn.config(state="disabled")
        self.app.detect_titles()
        if self.app.busy:                         # under way: the table's locked till it's back
            self.locked = True
            self.refill()
        else:                                     # (didn't go: no VLC, or the desk is busy)
            self.detect_btn.config(state="normal")

    def show_status(self, text):
        """A step of the stakeout, with the light breathing."""
        self.news_lbl.config(text=text)
        if text:
            self.news.pack(fill="x", pady=(6, 2), before=self.intro)
            th.pulse_dot(self.news_dot, th.BRASS_400, th.VELLUM_200)
            if not self.news_bar.winfo_manager():
                self.news_bar.pack(fill="x", pady=(4, 5))
                self.news_bar.start(12)

    def show_outcome(self, text, colour):
        """How the stakeout went: a steady light (green: titles named)."""
        self.show_status(text)
        self.news_bar.stop()
        self.news_bar.pack_forget()
        self.news_pct.config(text="")
        th.set_dot(self.news_dot, colour)

    def detected(self):
        """Detect titles is back: rows not yours take the menus' word."""
        if not self.alive():
            return
        if self.detect_btn:
            self.detect_btn.config(state="normal")
        self.locked = False
        self.extras_of(self.rep)          # (whose film each extra is: the menus just said)
        self.load()
        self.refill()

    # ---- saving ----
    def save(self):
        con = di.library_open(self.db)
        try:
            for tid, s in self.state.items():
                # the menus' word once confirmed; any other row once changed (emptied: cleared)
                if not (s["ok"] if s["from"] == "menus" else self.changed(tid)):
                    continue
                if self.numbered(s["type"]) and not s["title"].strip() \
                        and s["season"].strip().isdigit() and di.episode_numbers(s["episode"]):
                    s["title"] = self.named(s["type"], di.episode_name(
                        self.series, s["season"], s["episode"], self.order, online=self.online)) or ""
                di.set_label(con, self.h, tid, s["type"] or None, s["title"],
                             (s["season"] or None) if self.numbered(s["type"]) else None,
                             (s["episode"] or None) if self.numbered(s["type"]) else None)
        finally:
            con.close()
        answer = self.films_answer()
        self.close(ask=False)
        if answer:                                # which film is which: the disc's answer, investigated again
            self.app.use_answer(answer)
        else:
            self.app.labels_changed(self.h)
        if self.on_save:
            self.on_save()


class TypeGuide:
    """Edit titles' guide to the types: what each is for, and where a rip saves it."""

    # (type, what it's for, the folder a rip saves it in)
    TYPES = ((Type.MAIN_MOVIE, N_("The film itself, in any cut (theatrical, extended, director's)."), N_("The film's")),
             (Type.EPISODE, N_("A TV episode. Give it its season and episode."), N_("Its season's")),
             (Type.EXTRA, N_("Bonus material no other type fits: making-ofs, panels, galleries."), "Other"),
             (Type.FEATURETTE, N_("Short behind-the-scenes pieces, gag reels."), "Featurettes"),
             (Type.DELETED_SCENE, N_("Deleted and extended scenes."), "Deleted Scenes"),
             (Type.TRAILER, N_("This film's own trailers and teasers."), "Trailers"),
             (Type.INTERVIEW, N_("Interviews with the cast and crew."), "Interviews"),
             (Type.SHORT, N_("Short films."), "Shorts"),
             (Type.MUSIC, N_("Music videos, songs, sing-alongs, concert clips."), "Other"),
             (Type.SCENE, N_("A single scene or shot from the film."), "Scenes"),
             (Type.OTHER, N_("Logos, warnings, disclaimers. Not ripped unless ticked."), "Other"),
             (Type.PROMO, N_("Ads for other releases: coming soon, sneak peeks. Not ripped unless ticked."), "Other"))
    PLEX_FOLDERS = {"Other", "Featurettes", "Deleted Scenes", "Trailers", "Interviews", "Shorts", "Scenes"}  # (Plex's names)

    def __init__(self, parent):
        self.top = top = new_dialog(parent)
        top.title(_("Title types"))
        top.transient(parent)
        top.configure(background=th.PARCHMENT)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(14, 12, 14, 12))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=_("Title types"), style="Title.TLabel", background=th.PARCHMENT).pack(anchor="w")
        ttk.Label(body, text=_("TheDiscDB's types, and the folder each is saved in when ripped. "
                             "Promo is Disc Detective's own: it goes to TheDiscDB as a Trailer."),
                  style="DeskSub.TLabel", wraplength=th.px(560), justify="left").pack(anchor="w", pady=(2, 10))
        g = ttk.Frame(body, style="Desk.TFrame")
        g.pack(fill="x")
        for c, head in enumerate((_("TYPE"), _("WHAT IT'S FOR"), _("SAVED IN"))):
            ttk.Label(g, text=head, style="DeskCap.TLabel").grid(row=0, column=c, sticky="w", padx=(0, 12), pady=(0, 4))
        for r, (kind, what, folder) in enumerate(self.TYPES, 1):
            ttk.Label(g, text=kind, style="DeskMonoBold.TLabel").grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=1)
            ttk.Label(g, text=_(what), style="Desk.TLabel", wraplength=th.px(340), justify="left").grid(
                row=r, column=1, sticky="nw", padx=(0, 12), pady=1)
            ttk.Label(g, text=folder if folder in self.PLEX_FOLDERS else _(folder), style="DeskSub.TLabel").grid(row=r, column=2, sticky="nw", pady=1)
        ttk.Label(body, text=_("On a TV disc, an extra given its episode saves beside that episode. The rest "
                             "save in their folder under the disc's season, or under the show if it has none."),
                  style="DeskSub.TLabel", wraplength=th.px(560), justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Label(body, text=_("A Play All takes the type of what it plays (Deleted Scenes: Play All is a "
                             "DeletedScene). It isn't ripped unless ticked, as what it plays is ripped on its own."),
                  style="DeskSub.TLabel", wraplength=th.px(560), justify="left").pack(anchor="w", pady=(10, 0))
        btns = ttk.Frame(body, style="Desk.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text=_("Close"), command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False


class MenuPicture:
    """Edit titles' picture of a menu page: the button that plays a title outlined, and what
    Detect titles read on it, for naming a title (or putting its name right) without going
    through the disc's menus in VLC."""

    FRAME = (720, 480)                    # the disc's own coordinates, the buttons' rectangles in
    PALER, DARKER = 0.55, 0.75            # the menu round the button: half its colour, a quarter darker

    def __init__(self, parent):
        self.top = top = new_dialog(parent)
        top.title(_("Menu button"))
        top.transient(parent)
        top.configure(background=th.PARCHMENT)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(14, 12, 14, 12))
        body.pack(fill="both", expand=True)
        # the page: the way to it from the main menu, one line, the whole on hover when it's cut short (each of The
        # Swan Princess's friends' pages, "Odette Reads" on each, told apart by the button before); set apart from
        # the help text, as what its button was read as is
        self.trail = ""
        self.path = ttk.Label(body, text="", style="DeskFind.TLabel", width=1)
        self.path.pack(anchor="w", fill="x")
        self.path.bind("<Configure>", lambda e: fit_label(self.path, self.trail, "DeskFind.TLabel"))
        th.Tooltip(self.path, lambda: self.trail if str(self.path.cget("text")) != self.trail else "")
        # what it was read as: the name, a link when it was put right (read as, put right as: pointed at,
        # or clicked), then how to look at it
        said = self.said_row = ttk.Frame(body, style="Desk.TFrame")
        said.pack(anchor="w", pady=(4, 0))
        # (only the words as read set apart: "Detect titles read it as" and its full stop as the help text is)
        self.said = ttk.Label(said, text="", style="DeskSub.TLabel")
        self.said.pack(side="left")
        self.words = ttk.Label(said, text="", style="DeskFind.TLabel")
        self.fixed = th.link(said, "", self._toggle_fix, style="DeskFindLink.TLabel")
        self.said_end = ttk.Label(said, text="", style="DeskSub.TLabel")
        th.Tooltip(self.fixed, lambda: self.fix_text)
        # (its description, read under its name: a line of its own, wrapped -- Order of the Phoenix's run long, and
        # beside the name on one line they ran off the window)
        self.described = ttk.Label(body, text="", style="DeskFind.TLabel", wraplength=640, justify="left")
        self.fix_note = ttk.Label(body, text="", style="DeskSub.TLabel", wraplength=640, justify="left")
        self.fix_text = ""
        self.read = ttk.Label(body, text="", style="DeskSub.TLabel", wraplength=640, justify="left")
        self.read.pack(anchor="w", pady=(2, 8))
        self.pic = ttk.Label(body, style="DeskSub.TLabel")
        self.pic.pack()
        self.pic.bind("<Motion>", self._pointer)
        self.pic.bind("<Leave>", lambda e: (setattr(self, "over", False), self._redraw()))
        self.pic.bind("<Button-1>", self._click)
        btns = ttk.Frame(body, style="Desk.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text=_("Close"), command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())
        self.photo = None

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def _said(self, lead, pic, words):
        """The first line: lead, a sentence with {words} in it (“Detect titles read it as {words}.”), the name
        in place of {words}; a name put right (pic's read_as, name) as a link there, its reading shown pointed
        at or clicked."""
        name, read_as = pic.get("name"), pic.get("read_as")
        self.fix_note.pack_forget()
        self.fixed.pack_forget()
        self.words.pack_forget()
        self.said_end.pack_forget()
        self.described.pack_forget()
        fixed = bool(name and read_as and name != read_as)
        self.fix_text = _("Read as “{read}”, corrected to “{name}”", read=read_as, name=name) if fixed else ""
        self.fix_note.config(text=self.fix_text + "." if fixed else "")
        if not (fixed or words):
            self.said.config(text="")
            return
        before, _x, after = lead.partition("{words}")      # (the name: its own label, a link when put right)
        self.said.config(text=before)
        shown = self.fixed if fixed else self.words
        # (set apart by its bold alone, no quotes round it: the user's)
        shown.config(text=name if fixed else words)
        shown.pack(side="left")
        self.said_end.config(text=after)
        self.said_end.pack(side="left")
        if pic.get("description"):
            self.described.config(text=pic["description"])
            self.described.pack(anchor="w", after=self.said_row)

    def _toggle_fix(self):
        """A click on the corrected name: its reading shown, or hidden again."""
        if self.fix_note.winfo_manager():
            self.fix_note.pack_forget()
        else:
            self.fix_note.pack(anchor="w", before=self.read)

    def show(self, tid, pic):
        """The page's picture: the rest of the menu dimmed, the button's area (a gold dashed box)
        as it is; the words its name was read from outlined in red only while the pointer is on
        the gold box, or after a click on it (another click hides them): for checking a reading,
        not needed to find the button."""
        import base64
        import io
        from PIL import Image, ImageDraw, ImageEnhance
        # (a picture kept before the way there was: its page's name, as its heading had it; the first menu's, Main Menu)
        self.trail = menu_trail(pic.get("page_trail")) or menu_name(pic.get("page_from"))
        fit_label(self.path, self.trail, "DeskFind.TLabel")
        # (what was read: the name; the words under it describing it on their own line, _said)
        words = (pic.get("text") or "").strip()
        if pic.get("alone"):                      # (only the button: the disc's own image of it)
            self._said(_("Read as {words}."), pic, words)
            self.read.config(text=_("Its button in the disc's menu, as the disc has it."))
            with Image.open(pic["image"]) as im:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "PNG")
            self.photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
            self.photos, self.hot, self.words_shown = None, (0, 0, -1, -1), False
            self.pinned = self.over = False
            self.pic.config(image=self.photo, cursor="")
            self.top.deiconify()
            self.top.lift()
            keep_on_screen(self.top)
            return
        button = list(pic.get("button") or pic["rect"])
        shown = list(pic["rect"])                 # (the words read, its description's too, or the button's area)
        # (and their outline following the lines' slant, where the reading kept it: else that box)
        shape = [tuple(p) for p in pic.get("shape") or []] if len(pic.get("shape") or []) >= 3 else None
        # (the picture: the page with this button selected (a Blu-ray's, a DVD subpicture page's), else the page read
        # without its subpicture, no highlight on any button -- never another button's highlight)
        self._said(_("Detect titles read it as {words}."), pic, words)
        said = [] if words else [_("Detect titles couldn't read its words.")]
        said.append(_("The gold dashed box is the button on the menu; point at it, or click it, to see the words it "
                      "was read from (in red).") if words else _("The gold dashed box is the button on the menu."))
        if pic.get("pressed"):
            said.append(_("Press the numbered buttons in order: the last is this title's."))
        self.read.config(text=" ".join(said))
        # (a Blu-ray's page photographed over its video, where it was: as a player shows it; else as it was read)
        from . import menus
        shot = menus.photo_of(pic["image"])
        with Image.open(shot if os.path.isfile(shot) else pic["image"]) as im:
            im = im.convert("RGB")
        sx, sy = im.width / self.FRAME[0], im.height / self.FRAME[1]
        # a spotlight: the rest of the menu a touch darker and paler, the button's area (and its
        # words) as they are, so the eye finds them
        dim = ImageEnhance.Brightness(ImageEnhance.Color(im).enhance(self.PALER)).enhance(self.DARKER)
        lit = Image.new("L", im.size, 0)
        for x0, y0, x1, y1 in ([button] if shape else [shown, button]):
            ImageDraw.Draw(lit).rectangle((x0 * sx, y0 * sy, x1 * sx, y1 * sy), fill=255)
        if shape:
            ImageDraw.Draw(lit).polygon([(x * sx, y * sy) for x, y in shape], fill=255)
        # (and the buttons on this page pressed first on the way to it, a setting coming back to the same menu:
        # WALL-E's commentary "ON", menus.pressed_first -- lit and numbered in the order pressed, no ring: the
        # red outline is only ever the words read, the user's)
        pressed = [q for q in pic.get("pressed") or [] if q.get("rect")]
        rings = []
        for q in pressed:
            qs = [tuple(v) for v in q.get("shape") or []]
            if len(qs) < 3:
                x0, y0, x1, y1 = q["rect"]
                qs = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            ImageDraw.Draw(lit).polygon([(x * sx, y * sy) for x, y in qs], fill=255)
            rings.append(qs)
        base = Image.composite(im, dim, lit).convert("RGBA")
        w = max(3, round(im.width / 180))
        # the button's area as the disc draws it: gold, dashed (a dark edge under it: seen on light
        # menus too)
        marks = Image.new("RGBA", im.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(marks)
        x0, y0, x1, y1 = button
        area = (x0 * sx - 2, y0 * sy - 2, x1 * sx + 2, y1 * sy + 2)
        thin = max(2, w // 2)
        dashed_box(draw, area, (0, 0, 0, 90), thin + 2, dash=round(8 * sx), gap=round(5 * sx))
        dashed_box(draw, area, tuple(int(th.BRASS_400[i:i + 2], 16) for i in (1, 3, 5)) + (235,), thin,
                   dash=round(8 * sx), gap=round(5 * sx))
        gap = w + 3
        red = tuple(int(th.WAX[i:i + 2], 16) for i in (1, 3, 5)) + (190,)
        steps = Image.new("RGBA", im.size, (0, 0, 0, 0))     # (the numbers, over everything)
        if rings:                       # (each pressed first numbered by its words, the title's last)
            ring_of = lambda qs: grown([(x * sx, y * sy) for x, y in qs], gap + w)
            mine = ring_of(shape) if shape else ring_of([(shown[0], shown[1]), (shown[2], shown[1]),
                                                           (shown[2], shown[3]), (shown[0], shown[3])])
            for n, r in enumerate([ring_of(qs) for qs in rings] + [mine], 1):
                step_badge(ImageDraw.Draw(steps), min(q[0] for q in r), min(q[1] for q in r), n, round(12 * sx),
                           red[:3] + (240,))
        plain = Image.alpha_composite(Image.alpha_composite(base, marks), steps)
        # and the words read, outlined clear of them: a gap, then the line, half see-through, so
        # the text under it (a neighbour's) still reads
        draw = ImageDraw.Draw(marks)
        if shape:                                 # (round the lines' own slant, a gap clear of them)
            ring = grown([(x * sx, y * sy) for x, y in shape], gap + w)
            draw.line(ring + ring[:1], fill=(255, 255, 255, 120), width=w + 2, joint="curve")
            draw.line(ring + ring[:1], fill=red, width=w, joint="curve")
        else:
            x0, y0, x1, y1 = shown
            box = (x0 * sx - gap - w, y0 * sy - gap - w, x1 * sx + gap + w, y1 * sy + gap + w)
            draw.rectangle(box, outline=(255, 255, 255, 120), width=w + 2)
            draw.rectangle(box, outline=red, width=w)
        read = Image.alpha_composite(Image.alpha_composite(base, marks), steps)
        k = th.SCALE * min(1.0, 900 / im.width)

        def photo(img):
            img = img.convert("RGB")
            if k != 1.0:
                img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
        self.photos = (photo(plain), photo(read))
        self.hot = tuple(v * k for v in area)     # (the gold box, in the shown picture's pixels)
        self.pinned = self.over = False
        self.words_shown = bool(words)
        self.photo = self.photos[0]
        self.pic.config(image=self.photo, cursor="")
        self.top.deiconify()
        self.top.lift()
        keep_on_screen(self.top)                  # (the picture makes it bigger than it opened)

    def _on_box(self, e):
        x0, y0, x1, y1 = getattr(self, "hot", (0, 0, -1, -1))
        return x0 <= e.x <= x1 and y0 <= e.y <= y1

    def _pointer(self, e):
        """Over the gold box: the words read, outlined; off it, as before (unless pinned)."""
        self.over = self._on_box(e)
        self._redraw()

    def _click(self, e):
        if self._on_box(e):
            self.pinned = not self.pinned
            self._redraw()

    def _redraw(self):
        if not getattr(self, "photos", None) or not self.words_shown:
            return
        want = self.photos[1] if (self.over or self.pinned) else self.photos[0]
        if want is not self.photo:
            self.photo = want
            self.pic.config(image=want)
        self.pic.config(cursor="hand2" if self.over else "")


def step_badge(draw, x, y, n, radius, colour):
    """A numbered badge centred on (x, y): the order buttons are pressed in (the picture window's)."""
    from PIL import ImageFont
    try:
        font = ImageFont.load_default(size=round(radius * 1.3))
    except TypeError:                   # (a Pillow older than 10.1: its one small font)
        font = ImageFont.load_default()
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=colour, outline=(255, 255, 255, 230),
                 width=max(1, radius // 6))
    draw.text((x, y), str(n), font=font, fill=(255, 255, 255, 255), anchor="mm")


def dashed_box(draw, box, colour, width, dash=8, gap=5):
    """A dashed rectangle (Pillow draws only solid ones): dash long strokes, gap apart."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    dash, gap = max(1, dash), max(1, gap)
    for a, b, fixed, across in ((x0, x1, y0, True), (x0, x1, y1, True), (y0, y1, x0, False), (y0, y1, x1, False)):
        p = a
        while p < b:
            q = min(p + dash, b)
            draw.line(((p, fixed), (q, fixed)) if across else ((fixed, p), (fixed, q)), fill=colour, width=width)
            p = q + gap


def human_count(n):
    return format(n, ",") if n is not None else "?"


class DataProgress:
    """A progress bar + line of text inside a dialog, fed by App.start_data_update."""

    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        # the words on top, the bar under them (as on the main window's strip)
        self.text = ttk.Label(self.frame, text="", style="Sub.TLabel", wraplength=380, justify="left")
        self.text.pack(anchor="w", pady=(0, 4))
        self.bar = th.Gauge(self.frame, ttk.Style().lookup("TFrame", "background") or th.VELLUM_100,
                            maximum=1000, length=360)
        self.bar.pack(fill="x")
        self._spinning = False

    def progress(self, label, done, total):
        if self._spinning:
            self.bar.stop()
            self.bar.config(mode="determinate")
            self._spinning = False
        if total:
            self.bar.config(value=1000 * done / total)
            self.text.config(text=_("Downloading {label}: {done} of {total} MB", label=label, done=done // 2**20, total=max(1, total // 2**20)))
        else:
            self.text.config(text=_("Downloading {label}: {done} MB", label=label, done=done // 2**20))

    def message(self, text):
        if "index" in text.lower() and "downloading" not in text.lower():
            self.bar.config(mode="indeterminate")
            self.bar.start(12)
            self._spinning = True
        self.text.config(text=text[0].upper() + text[1:] if text else "")

    def stop(self):
        self.bar.stop()
        self._spinning = False


class FirstRunDialog:
    """Shown at startup while the disc data hasn't been downloaded yet."""

    SIZES = {"discdb": N_("about 4 MB"), "imdb": N_("about 1.4 GB, then a few minutes to index")}

    def __init__(self, root, app, missing):
        self.app, self.missing = app, missing
        self.top = top = new_dialog(root)
        top.title(_("Get disc data"))
        top.transient(root)
        top.resizable(False, False)
        body = ttk.Frame(top, padding=18)
        body.pack(fill="both")
        ttk.Label(body, text=_("Get disc data"), style="Title.TLabel").pack(anchor="w")
        parts = {"discdb": _("TheDiscDB (fingerprints of known discs)"), "imdb": _("IMDb's public title and credits data")}
        ttk.Label(body, wraplength=400, justify="left", text=(
            _("Disc Detective recognizes discs with:\n") +
            "".join("   •  %s\n" % _("{what}, {size}", what=parts[k], size=_(self.SIZES[k])) for k in missing) +
            _("\nThis is a one-time download. After that the data refreshes itself, "
            "and scans work offline."))).pack(anchor="w", pady=(8, 12))
        self.prog = DataProgress(body)
        self.btns = ttk.Frame(body)
        self.btns.pack(fill="x")
        self.go = ttk.Button(self.btns, text=_("Download now"), command=self.start)
        self.go.pack(side="right")
        self.later = ttk.Button(self.btns, text=_("Later"), command=top.destroy)
        self.later.pack(side="right", padx=6)
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.after(50, lambda: (top.lift(), self.go.focus_set()))

    def start(self):
        self.go.config(state="disabled")
        self.later.config(state="disabled")
        self.prog.frame.pack(fill="x", before=self.btns, pady=(0, 12))
        self.prog.message(_("Starting…"))
        self.app.start_data_update(self.missing, False, self)

    def progress(self, *a):
        self.prog.progress(*a)

    def message(self, text):
        self.prog.message(text)

    def finished(self, res, err):
        self.prog.stop()
        self.prog.bar.config(mode="determinate", value=1000 if not err else 0)
        if err:
            self.prog.text.config(text=_("Couldn't finish: {err}\nYou can retry from Settings.", err=err))
            self.later.config(text=_("Close"), state="normal")
        else:
            self.prog.text.config(text=_("Ready. Insert a disc and press Scan Disc."))
            self.later.config(text=_("Close"), state="normal")
            self.top.after(1500, self.top.destroy)

    def close(self):
        if not self.app.data_ui:       # don't orphan a running download's progress
            self.top.destroy()


# the chat sites; {q} is the question, filled into the chat box where the site takes it from the link
AI_SITES = [("ChatGPT", "https://chatgpt.com/?q={q}"), ("Claude", "https://claude.ai/new?q={q}"),
            ("Gemini", "https://gemini.google.com/app")]


def copy_image(path):
    """Put a picture on the system clipboard, for pasting into a chat AI. True if it worked."""
    try:
        if sys.platform == "win32":
            import ctypes
            import io
            from ctypes import wintypes
            from PIL import Image
            with Image.open(path) as im:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "BMP")
            data = buf.getvalue()[14:]                  # a DIB is a BMP without its file header
            k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
            k32.GlobalAlloc.restype, k32.GlobalAlloc.argtypes = wintypes.HGLOBAL, [wintypes.UINT, ctypes.c_size_t]
            k32.GlobalLock.restype, k32.GlobalLock.argtypes = wintypes.LPVOID, [wintypes.HGLOBAL]
            k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            u32.OpenClipboard.argtypes = [wintypes.HWND]
            u32.SetClipboardData.restype, u32.SetClipboardData.argtypes = wintypes.HANDLE, [wintypes.UINT, wintypes.HANDLE]
            h = k32.GlobalAlloc(0x0002, len(data))      # GMEM_MOVEABLE
            ptr = k32.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.OpenClipboard(None):
                return False
            try:
                u32.EmptyClipboard()
                return bool(u32.SetClipboardData(8, h))  # CF_DIB; the clipboard owns h now
            finally:
                u32.CloseClipboard()
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e", 'set the clipboard to (read (POSIX file "%s") as \u00abclass PNGf\u00bb)'
                                % os.path.abspath(path)], capture_output=True, timeout=10)
            return r.returncode == 0
        for cmd in (["wl-copy", "--type", "image/png"], ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]):
            if shutil.which(cmd[0]):
                with open(path, "rb") as fh:            # these keep serving the clipboard: don't wait
                    subprocess.Popen(cmd, stdin=fh, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                return True
    except Exception:
        di.logger.exception(i18n.log("copying the picture failed"))
    return False


class AskAIDialog:
    """No API key needed: show the disc's own pictures, copy one and the question into any chat
    AI, then look its answer up in IMDb (the answer is checked like a typed one)."""

    def __init__(self, root, app):
        self.app, rep = app, app.rep
        self.images = rep.get("disc_images") or []
        self.k = 0
        self.top = top = new_dialog(root)
        top.title(_("Ask an AI about the disc's picture"))
        top.transient(root)
        top.resizable(False, False)
        f = ttk.Frame(top, padding=16)
        f.pack(fill="both")
        left = ttk.Frame(f)
        left.grid(row=0, column=0, sticky="n", padx=(0, 14))
        self.pic = ttk.Label(left)
        self.pic.pack()
        nav = ttk.Frame(left)
        nav.pack(pady=(4, 0))
        self.prev_btn = ttk.Button(nav, text="\u2039", width=3, style="Small.TButton", command=lambda: self.show(-1))
        self.count = ttk.Label(nav, text="", style="Cap.TLabel")
        self.next_btn = ttk.Button(nav, text="\u203a", width=3, style="Small.TButton", command=lambda: self.show(1))
        if len(self.images) > 1:
            self.prev_btn.pack(side="left")
            self.count.pack(side="left", padx=6)
            self.next_btn.pack(side="left")
        right = ttk.Frame(f)
        right.grid(row=0, column=1, sticky="nw")
        ttk.Label(right, text=_("Chat AIs recognise covers and title art well. Copy the picture and the "
                              "question, paste both into one, then put its answer below."),
                  style="Sub.TLabel", wraplength=330, justify="left").pack(anchor="w")
        self.question = di.ai_question(rep)
        q = tk.Text(right, width=50, height=5, wrap="word", relief="flat", padx=6, pady=4, font="TkDefaultFont")
        q.insert("1.0", self.question)
        q.config(state="disabled")
        q.pack(anchor="w", pady=(8, 0))
        copy = ttk.Frame(right)
        copy.pack(anchor="w", pady=(8, 0))
        self.copy_pic_btn = ttk.Button(copy, text=_("Copy picture"), command=self.copy_picture)
        self.copy_pic_btn.pack(side="left")
        ttk.Button(copy, text=_("Copy question"), command=self.copy_question).pack(side="left", padx=(6, 0))
        ttk.Button(copy, text=_("Save picture\u2026"), command=self.save_picture).pack(side="left", padx=(6, 0))
        sites = ttk.Frame(right)
        sites.pack(anchor="w", pady=(8, 0))
        ttk.Label(sites, text=_("Open:"), style="Cap.TLabel").pack(side="left", padx=(0, 6))
        for name, url in AI_SITES:
            ttk.Button(sites, text=name, style="Small.TButton",
                       command=lambda n=name, u=url: self.open_site(n, u)).pack(side="left", padx=(0, 4))
        self.key = (app.settings or {}).get("gemini_key")
        if self.key:                     # a key in Settings: Gemini can be asked from here directly
            self.ask_btn = ttk.Button(sites, text=_("Ask Gemini for me"), command=self.ask_gemini)
            self.ask_btn.pack(side="left", padx=(10, 0))
        self.note = ttk.Label(right, text="", style="Cap.TLabel")
        self.note.pack(anchor="w", pady=(6, 0))
        ans = ttk.Frame(right)
        ans.pack(anchor="w", pady=(10, 0))
        ttk.Label(ans, text=_("Its answer")).pack(side="left", padx=(0, 8))
        self.answer = tk.StringVar()
        e = ttk.Entry(ans, textvariable=self.answer, width=30)
        e.pack(side="left")
        btns = ttk.Frame(f)
        btns.grid(row=1, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text=_("Look it up"), command=self.look_up).pack(side="right")
        ttk.Button(btns, text=_("Close"), command=top.destroy).pack(side="right", padx=6)
        top.bind("<Return>", lambda ev: self.look_up())
        top.bind("<Escape>", lambda ev: top.destroy())
        self.show(0)
        top.after(50, lambda: (top.lift(), e.focus_set()))

    def current(self):
        return self.images[self.k] if self.images else None

    def show(self, step):
        if not self.images:
            self.pic.config(text=_("No pictures on this disc"), image="")
            return
        self.k = (self.k + step) % len(self.images)
        img = self.app._photo(self.current()["image"])
        self.pic.config(image=img or "", text="" if img else _("(can't show it)"))
        self.count.config(text=_("{number} of {total}", number=self.k + 1, total=len(self.images)))

    def copy_picture(self):
        ok = bool(self.current()) and copy_image(self.current()["full"])
        self.note.config(text=_("Picture copied: paste it into the chat") if ok else
                         _("Couldn't copy the picture here: use Save picture\u2026 and attach the file"))

    def open_site(self, name, url):
        if "{q}" in url:
            webbrowser.open(url.replace("{q}", urllib.parse.quote(self.question)))
            self.note.config(text=_("The question is filled in: paste the picture there and send"))
        else:
            webbrowser.open(url)
            self.copy_question()
            self.note.config(text=_("Question copied: paste it and the picture into {name}", name=name))

    def ask_gemini(self):
        cur = self.current()
        if not cur:
            return
        self.ask_btn.config(state="disabled")
        self.note.config(text=_("Asking Gemini\u2026"))
        q, key = di.ai_question(self.app.rep, as_json=True), self.key

        def work():
            try:
                text, _x = di.gemini_ask(key, q, cur["full"])
                a = di.parse_ai_answer(text)
                msg = None if a else _("Gemini couldn't tell: {answer}", answer=text[:80])
            except Exception as exc:
                a, msg = None, str(exc)

            def done():
                if not self.top.winfo_exists():
                    return
                self.ask_btn.config(state="normal")
                if a:
                    self.answer.set("%s (%s)" % (a["title"], a["year"]) if a.get("year") else a["title"])
                    self.note.config(text=_("Gemini's answer is below: Look it up to check it against IMDb"))
                else:
                    self.note.config(text=msg)
            self.app.post(done)
        threading.Thread(target=work, daemon=True).start()

    def copy_question(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(self.question)
        self.note.config(text=_("Question copied"))

    def save_picture(self):
        cur = self.current()
        if not cur:
            return
        dest = filedialog.asksaveasfilename(parent=self.top, title=_("Save the disc's picture"), defaultextension=".png",
                                            initialfile="disc_picture.png", filetypes=[(_("PNG image"), "*.png")])
        if dest:
            shutil.copyfile(cur["full"], dest)
            self.note.config(text=_("Saved: attach {file} in the chat", file=os.path.basename(dest)))

    def look_up(self):
        text = self.answer.get().strip().strip("*\"'\u201c\u201d. ")
        if not text:
            return
        m = re.match(r"^(.*?)\s*\((\d{4})[^)]*\)$", text)
        title, year = (m.group(1), int(m.group(2))) if m else (text, None)
        lp = self.app.rep.get("label_parsed") or {}
        self.top.destroy()
        MatchDialog(self.app.root, self.app, title=title, year=year, tv=self.app.rep.get("kind") == "tv",
                    season=lp.get("season"), disc=lp.get("disc"))


class MatchDialog:
    """Type the answer in: IMDb is searched as you type; pick a result to use that exact entry.
    Title / year / film-or-TV can also just be typed, for anything IMDb doesn't have."""

    KINDS = {"movie": N_("film"), "tvMovie": N_("TV film"), "video": N_("video"), "tvSeries": N_("TV series"),
             "tvMiniSeries": N_("miniseries"), "tvSpecial": N_("TV special"), "short": N_("short"), "tvShort": N_("TV short")}

    def __init__(self, root, app, title="", year=None, tv=False, season=None, disc=None, picked=None, on_pick=None,
                 multi=False):
        self.app = app
        self.on_pick = on_pick          # set: just pick one IMDb entry (one film of a collection)
        self.picked = picked if picked and picked.get("imdb_id") else None
        self._filling = False
        self._after = None
        try:
            args = app.scan_args()
            self.con = di.imdb_open(args.imdb_dir, allow_download=False, log=lambda m: None)
        except Exception:
            self.con = None
        self.top = top = new_dialog(root)
        top.title(_("Which film is this?") if on_pick else _("What's on this disc?"))
        top.transient(root)
        top.resizable(False, False)
        f = ttk.Frame(top, padding=16)
        f.pack(fill="both")
        self.title = tk.StringVar(value=title or "")
        self.year = tk.StringVar(value=str(year or ""))
        self.kind = tk.StringVar(value="multi" if multi and not on_pick else "tv" if tv else "film")
        self.season = tk.StringVar(value="" if season is None else str(season))
        self.disc = tk.StringVar(value=str(disc or ""))
        title_lbl = ttk.Label(f, text=_("Title"))
        title_lbl.grid(row=0, column=0, sticky="w", padx=(0, 10))
        e = ttk.Entry(f, textvariable=self.title, width=44)
        e.grid(row=0, column=1, columnspan=3, sticky="we")
        # results with a small poster each (posters fill in as they arrive)
        self.results = ttk.Treeview(f, show="tree", height=4, selectmode="browse", style="Picks.Treeview")
        self.results.column("#0", width=430)
        self.results.grid(row=1, column=1, columnspan=3, sticky="we", pady=(2, 0))
        self.results.bind("<<TreeviewSelect>>", lambda ev: self.pick())
        self.matches = []
        self._thumbs = {}            # imdb id -> PhotoImage (kept so Tk doesn't drop them)
        self._gen = 0
        self.pick_lbl = ttk.Label(f, text="", style="Sub.TLabel")
        self.pick_lbl.grid(row=2, column=1, columnspan=3, sticky="w")
        year_lbl = ttk.Label(f, text=_("Year"))
        year_lbl.grid(row=3, column=0, sticky="w", pady=(6, 0))
        year_e = ttk.Entry(f, textvariable=self.year, width=6)
        year_e.grid(row=3, column=1, sticky="w", pady=(6, 0))
        self.one_title = [title_lbl, e, self.results, self.pick_lbl, year_lbl, year_e]   # not for several films
        e.bind("<Down>", lambda ev: (self.results.focus_set(), self.results.selection_set("0"),
                                     self.results.focus("0")) if self.matches else None)
        self.title.trace_add("write", lambda *a: self.typed())
        kinds = ttk.Frame(f)
        kinds.grid(row=4, column=1, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Radiobutton(kinds, text=_("Film"), variable=self.kind, value="film", command=self.toggle).pack(side="left")
        ttk.Radiobutton(kinds, text=_("TV show"), variable=self.kind, value="tv", command=self.toggle).pack(side="left", padx=(12, 0))
        # a double feature (two films on one disc), a collection as Wallace & Gromit's shorts are
        ttk.Radiobutton(kinds, text=_("Multi-film"), variable=self.kind, value="multi",
                        command=self.toggle).pack(side="left", padx=(12, 0))
        self.tv_row = ttk.Frame(f)
        ttk.Label(self.tv_row, text=_("Season")).pack(side="left")
        ttk.Entry(self.tv_row, textvariable=self.season, width=4).pack(side="left", padx=(6, 14))
        ttk.Label(self.tv_row, text=_("Disc")).pack(side="left")
        ttk.Entry(self.tv_row, textvariable=self.disc, width=4).pack(side="left", padx=(6, 0))
        self.tv_row.grid(row=5, column=1, columnspan=3, sticky="w", pady=(6, 0))
        # Multi-film: the films on the disc, in no order (which title is which comes after)
        self.films = [] if on_pick else suspected_films(app.rep or {})
        self.film_thumbs = {}
        self.film_box = ttk.Frame(f)
        self.film_list = ttk.Frame(self.film_box)
        self.film_list.pack(fill="x")
        ttk.Button(self.film_box, text=_("Add a film…"), command=self.add_film).pack(anchor="w", pady=(6, 0))
        self.film_box.grid(row=5, column=0, columnspan=4, sticky="we", pady=(8, 0))
        self.draw_films()
        self.fetch_film_thumbs()
        self.hint = (_("Pick the right entry from IMDb above, or just type the title and year. Disc Detective "
                       "remembers it for this disc.") if self.con else
                     _("IMDb data isn't downloaded, so type the title and year. Disc Detective remembers it for this disc."))
        self.note = ttk.Label(f, text=self.hint, style="Sub.TLabel", wraplength=380, justify="left")
        self.note.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        btns = ttk.Frame(f)
        btns.grid(row=7, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(btns, text=_("Use this"), command=self.ok).pack(side="right")
        ttk.Button(btns, text=_("Cancel"), command=self.close).pack(side="right", padx=6)
        top.bind("<Return>", lambda e: self.ok())
        top.bind("<Escape>", lambda e: self.close())
        top.protocol("WM_DELETE_WINDOW", self.close)
        self.toggle()
        if on_pick:                     # one film of a collection: no film/TV or season choice
            kinds.grid_remove()
            self.tv_row.grid_remove()
            self.note.config(text=_("Pick the film from the IMDb list."))
        self.search()
        self.show_pick()
        top.after(50, lambda: (top.lift(), e.focus_set(), e.icursor("end")))

    # ---- IMDb type-ahead ----
    def typed(self):
        if self._filling:
            return
        self.picked = None                          # edited by hand: no longer that exact entry
        self.show_pick()
        if self._after:
            self.top.after_cancel(self._after)
        self._after = self.top.after(200, self.search)

    def search(self):
        self._after = None
        self.results.delete(*self.results.get_children())
        self.matches = di.imdb_lookup(self.con, self.title.get()) if self.con else []
        for i, c in enumerate(self.matches):
            bits = [_(self.KINDS[c["type"]]) if c["type"] in self.KINDS else c["type"]]
            if c.get("runtime"):
                bits.append(_("{minutes} min", minutes=c["runtime"]))
            if c.get("votes"):
                bits.append(ui_n("{votes} vote", "{votes} votes", c["votes"], votes=format(c["votes"], ",")))
            img = self._thumbs.get(c["imdb_id"])
            self.results.insert("", "end", iid=str(i), text="  " + _("{name} ({year})  ·  {details}", name=c["name"], year=c.get("year") or "?", details="  ·  ".join(bits)), **({"image": img} if img else {}))
        if self.picked:
            for i, c in enumerate(self.matches):
                if c["imdb_id"] == self.picked["imdb_id"]:
                    self._filling = True
                    self.results.selection_set(str(i))
                    self.results.see(str(i))
                    self._filling = False
        self.fetch_thumbs()

    def fetch_thumbs(self):
        """Posters for the listed results, in the background; each row updates when its arrives."""
        want = [c for c in self.matches if c["imdb_id"] not in self._thumbs]
        if not want:
            return
        self._gen += 1
        gen, args = self._gen, self.app.scan_args()      # settings read here, on the Tk thread

        def work():
            try:
                found = di.thumbs_for(want, args)
            except Exception:
                di.logger.exception(i18n.log("posters for the type-ahead list"))
                return
            self.app.post(self.show_thumbs, gen, found)
        threading.Thread(target=work, daemon=True).start()

    def show_thumbs(self, gen, found):
        if not self.top.winfo_exists():
            return
        for imdb_id, path in found.items():
            if imdb_id not in self._thumbs:
                try:
                    self._thumbs[imdb_id] = tk.PhotoImage(file=path)
                except tk.TclError:
                    continue
        for i, c in enumerate(self.matches):           # the list may have changed meanwhile: match by id
            img = self._thumbs.get(c["imdb_id"])
            if img and self.results.exists(str(i)):
                self.results.item(str(i), image=img)

    def pick(self):
        if self._filling:
            return
        sel = self.results.selection()
        if not sel:
            return
        c = self.matches[int(sel[0])]
        self._filling = True
        self.title.set(c["name"])
        self.year.set(str(c.get("year") or ""))
        self._filling = False
        self.kind.set("tv" if c["kind"] == "tv" else "film")
        self.toggle()
        self.picked = c
        self.show_pick()

    def show_pick(self):
        self.pick_lbl.config(text=(_("Using IMDb {imdb_id}", imdb_id=self.picked["imdb_id"])) if self.picked else "")

    def close(self):
        if self.con is not None:
            self.con.close()
            self.con = None
        self.top.destroy()

    def tv(self):
        return self.kind.get() == "tv"

    def toggle(self):
        (self.tv_row.grid if self.tv() and not self.on_pick else self.tv_row.grid_remove)()
        multi = self.kind.get() == "multi"
        for w in self.one_title:
            (w.grid_remove if multi else w.grid)()
        (self.film_box.grid if multi else self.film_box.grid_remove)()
        self.note.config(text=_("Add each film on the disc. Each is matched to a title by its length.") if multi
                         else self.hint, foreground="")

    # ---- Multi-film: the list of films ----
    def draw_films(self):
        """A row for each film: its poster and name, or "Film 2: not chosen yet"; Search… (Change…
        once chosen) and Remove."""
        for w in self.film_list.winfo_children():
            w.destroy()
        if not self.films:
            ttk.Label(self.film_list, text=_("No films yet."), style="Sub.TLabel").grid(row=0, column=0, sticky="w")
        for r, c in enumerate(self.films):
            img = self.film_thumbs.get(c["imdb_id"]) if c else None
            pic = ttk.Label(self.film_list, image=img) if img else \
                ttk.Frame(self.film_list, width=di.ART_PICK[0], height=di.ART_PICK[1])
            pic.grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            if c:
                what = "%s (%s)" % (c["name"], c.get("year") or "?") + \
                    ("  ·  " + _("{minutes} min", minutes=c["runtime"]) if c.get("runtime") else "")
                fitted_label(self.film_list, what, "TLabel", 40).grid(row=r, column=1, sticky="w")
            else:
                ttk.Label(self.film_list, text=_("Film {number}: not chosen yet", number=r + 1), width=40,
                          style="Sub.TLabel").grid(row=r, column=1, sticky="w")
            ttk.Button(self.film_list, text=_("Change…") if c else _("Search…"), style="Small.TButton",
                       command=lambda r=r: self.add_film(r)).grid(row=r, column=2, padx=(8, 0))
            ttk.Button(self.film_list, text=_("Remove"), style="Small.TButton",
                       command=lambda r=r: self.remove_film(r)).grid(row=r, column=3, padx=(4, 0))

    def add_film(self, at=None):
        """Search for a film: for the row at (its film, or an empty one), else one more."""
        c = self.films[at] if at is not None else None
        MatchDialog(self.top, self.app, title=c["name"] if c else "", picked=dict(c, kind="movie") if c else None,
                    on_pick=lambda f: self.got_film(f, at))

    def got_film(self, c, at=None):
        if not self.top.winfo_exists() or any(f and f["imdb_id"] == c["imdb_id"]
                                              for k, f in enumerate(self.films) if k != at):
            return                                  # (the disc's films are each one film)
        f = {"imdb_id": c["imdb_id"], "name": c["name"], "year": c.get("year"), "runtime": c.get("runtime")}
        if at is not None and at < len(self.films):
            self.films[at] = f
        else:
            self.films.append(f)
        self.draw_films()
        self.fetch_film_thumbs()

    def remove_film(self, at):
        del self.films[at]
        self.draw_films()

    def fetch_film_thumbs(self):
        want = [dict(c) for c in self.films if c and c["imdb_id"] not in self.film_thumbs]
        if not want:
            return
        args = self.app.scan_args()

        def work():
            try:
                found = di.thumbs_for(want, args)
            except Exception:
                di.logger.exception(i18n.log("posters for the film list"))
                return
            self.app.post(self.show_film_thumbs, found)
        threading.Thread(target=work, daemon=True).start()

    def show_film_thumbs(self, found):
        if not self.top.winfo_exists():
            return
        for imdb_id, path in found.items():
            try:
                self.film_thumbs[imdb_id] = tk.PhotoImage(file=path)
            except tk.TclError:
                pass
        self.draw_films()

    def truth(self):
        # (a season only if it says something: a whole series' bonus disc has none)
        s, d = self.season.get().strip(), self.disc.get().strip()
        if self.tv() and s and not s.isdigit():
            return None, _("The season should be a number.")
        if self.picked:
            p = dict(self.picked, kind="tv" if self.tv() else "movie")
            return di.truth_for(p, int(s) if self.tv() and s else None,
                                int(d) if self.tv() and d.isdigit() else None), None
        title = self.title.get().strip()
        year = self.year.get().strip()
        if not title:
            return None, _("Enter the title.")
        if year and not re.fullmatch(r"(18|19|20)\d\d", year):
            return None, _("The year should look like 1998.")
        t = "%s (%s)" % (title, year) if year else title
        if self.tv():
            t += (" S%d" % int(s) if s else "") + (" D%d" % int(d) if d.isdigit() else "")
        return t, None

    def ok(self):
        if self.on_pick:
            if not self.picked:
                self.note.config(text=_("Pick one of the IMDb results above."), foreground="#b00020")
                return
            picked = dict(self.picked)
            self.close()
            self.on_pick(picked)
            return
        if self.kind.get() == "multi":
            if None in self.films:
                self.note.config(text=_("Search for each film, or remove the ones that aren't on the disc."),
                                 foreground="#b00020")
                return
            if len(self.films) < 2:
                self.note.config(text=_("Add each film on the disc (at least two)."), foreground="#b00020")
                return
            answer = films_answer(self.app.rep, self.films)
            if not answer:
                self.note.config(text=_("None of the disc's titles is long enough to be a film."), foreground="#b00020")
                return
            self.close()
            self.app.use_answer(answer)
            return
        t, err = self.truth()
        if err:
            self.note.config(text=err, foreground="#b00020")
            return
        self.close()
        self.app.use_answer(t)


def suspected_films(rep):
    """Multi-film's list to start from, a place for each film: the disc's own collection's films;
    else one for each feature-length title of the disc (seen as copies of one film or not: two
    films can be taken for two editions), filled with a candidate film as long as one of them
    (core.several_films), the rest None: to search for."""
    cands = rep.get("candidates") or []
    mine = bool(cands) and cands[0].get("kind") == "collection"
    films = cands[0]["items"] if mine else [c for _x, c in di.several_films(rep.get("titles") or [], cands)]
    films = [{"imdb_id": c["imdb_id"], "name": c["name"], "year": c.get("year"), "runtime": c.get("runtime")}
             for c in films if c.get("imdb_id")]
    if mine:
        return films
    long_ = [t for t in rep.get("titles") or [] if t.get("duration", 0) >= di.SEVERAL_FILMS_MIN
             and t.get("role") in (Role.FEATURE, Role.VERSION, Role.LONGEST)]
    return films + [None] * max(0, len(long_) - len(films))


def films_answer(rep, films):
    """Multi-film's list as your answer for the disc: each film matched to a title by length
    (core.films_to_titles), named after them; Edit titles puts a mix-up right. None if no title
    of the disc is long enough to be one."""
    pairs, _x = di.films_to_titles(rep.get("titles") or [], films)
    if not pairs:
        return None
    return di.truth_for_collection(" & ".join(f["name"] for _x, f in pairs),
                                   [(t["id"], f["imdb_id"]) for t, f in pairs])


class AboutDialog:
    """About / credits: the version, and the data and software Disc Detective stands on."""

    # (heading, words, links, the source's own credit line: in its words, as its terms ask, untranslated)
    CREDITS = (
        (N_("Disc identification"), N_("Disc fingerprints, title lists and box art from TheDiscDB (MIT License). "
                                       "Titles from IMDb's non-commercial datasets, downloaded to your own computer "
                                       "for personal use under IMDb's terms."),
         (("thediscdb.com", "https://thediscdb.com"),
          (N_("IMDb datasets"), "https://developer.imdb.com/non-commercial-datasets/")), di.IMDB_CREDIT),
        (N_("TV episodes"), N_("Episode lists and DVD orders from TVmaze, under CC BY-SA 4.0."),
         (("tvmaze.com", "https://www.tvmaze.com"),), ""),
        (N_("Pictures"), N_("Posters and box art from the disc itself, TheDiscDB and, with a key of your own, TMDB."),
         (("themoviedb.org", "https://www.themoviedb.org"),), di.TMDB_CREDIT),
        (N_("AI detection assistance"), N_("Optional, with a Gemini key of your own: Google's Gemini looks at a "
                                           "disc's pictures, and IMDb checks its answer."), (), ""),
        (N_("Ripping and naming"), N_("Rips with MakeMKV, which you install yourself; files are named the way Plex "
                                      "likes. Disc Detective isn't affiliated with either."),
         (("makemkv.com", "https://www.makemkv.com"),), ""),
        (N_("Type and icons"), N_("Detective Serif (a cut of Playfair Display), Courier Prime and Inter, under the "
                                  "SIL Open Font License 1.1. Icons from Heroicons (MIT License)."), (), ""),
    )

    NEWS = {"checking": N_("Looking for a newer version…"), "done": N_("You have the newest version."),
            "failed": N_("Couldn't reach GitHub to look for a newer version."),
            "offline": N_("Working offline: not looking for a newer version.")}
    SYSTEMS = {"win32": "Windows", "darwin": "macOS"}

    def check(self):
        try:
            di.check_for_update()
            err = None
        except Exception as exc:                 # (offline, GitHub down: said so, and what was seen before kept)
            di.logger.info(i18n.log("newer version check: {error}", error=exc))
            err = str(exc)
        self.app.post(lambda: self.checked(err))

    def checked(self, err):
        self.app.show_update()
        if self.top.winfo_exists():
            self.show_news(di.known_update(), "failed" if err else "done")

    def show_news(self, rel, state):
        """A newer version (rel) and how to update to it, or where the looking for one is (state: NEWS)."""
        for w in self.news.winfo_children():
            w.destroy()
        if not rel:
            if state:
                ttk.Label(self.news, text=_(self.NEWS[state]), style="DeskSub.TLabel").pack(anchor="w")
            return
        ttk.Label(self.news, image=th.icon("upgrade"), text="  " + _("Version {version} is out (you have {current})", version=rel["version"], current=di.VERSION), compound="left", style="DeskFind.TLabel").pack(anchor="w")
        if getattr(sys, "frozen", False):
            how = _("Download the zip for {system} from its page, close {APP}, and put the new folder in place of this "
                    "one. Your case files and settings stay where they are.",
                    system=self.SYSTEMS.get(sys.platform, "Linux"), APP=APP)
        else:
            how = _("Update your copy (git pull, or pip install --upgrade), then start it again.")
        ttk.Label(self.news, text=how, style="DeskSub.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=(3, 0))
        if rel.get("url"):
            th.link(self.news, _("What's new, and the downloads"), lambda: webbrowser.open(rel["url"]),
                    style="DeskLink.TLabel", icon="external", right=True).pack(anchor="w", pady=(3, 0))

    LOGOS = {"Pictures": "tmdb"}       # (beside its credit: TMDB's terms ask for its logo, less prominent than ours)

    def __init__(self, root, app=None):
        self.app = app
        self.top = top = new_dialog(root)
        top.title(_("About {APP}", APP=APP))
        top.transient(root)
        top.resizable(False, False)
        top.configure(background=th.PARCHMENT)
        th.color_title_bar(top)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(22, 18, 22, 16))
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body, style="Desk.TFrame")
        head.pack(fill="x")
        try:
            self._icon = tk.PhotoImage(file=ICON).subsample(2)
            ttk.Label(head, image=self._icon, style="Desk.TLabel").pack(side="left", padx=(0, 12))
        except (tk.TclError, OSError):
            self._icon = None
        names = ttk.Frame(head, style="Desk.TFrame")
        names.pack(side="left", anchor="center")
        ttk.Label(names, text=_("VERSION {version} \u00b7 POLYFORM NONCOMMERCIAL", version=di.VERSION), style="Case.TLabel").pack(anchor="w")
        ttk.Label(names, text=APP, style="Name.TLabel").pack(anchor="w")
        # a newer version: looked for as About opens (the only check besides the weekly TheDiscDB index's)
        self.news = ttk.Frame(body, style="Desk.TFrame")
        self.news.pack(fill="x", pady=(10, 0))
        online = app is not None and not app.scan_args().offline
        self.show_news(di.known_update(), "checking" if online else "offline" if app is not None else None)
        if online:
            threading.Thread(target=self.check, daemon=True).start()
        ttk.Label(body, text=_("Credits and sources"), style="Kind.TLabel").pack(anchor="w", pady=(14, 6))
        card = th.frame(body, th.CARD, border=th.BRASS_400, radius=7, shadow=3)
        card.pack(fill="x")
        inner = ttk.Frame(card, style="Card.TFrame", padding=(18, 12, 18, 12))
        inner.pack(fill="x")
        inner.columnconfigure(0, weight=1, uniform="c")
        inner.columnconfigure(2, weight=1, uniform="c")
        rows = (len(self.CREDITS) + 1) // 2
        for k, (cap, text, links, credit) in enumerate(self.CREDITS):      # two columns, so it stays short
            col, r = (k // rows) * 2, (k % rows) * 2
            if r:
                th.rule(inner, th.BRASS_200).grid(row=r - 1, column=col, sticky="ew", pady=8)
            cell = ttk.Frame(inner, style="Card.TFrame")
            cell.grid(row=r, column=col, sticky="nw")
            ttk.Label(cell, text=_(cap).upper(), style="CardCap.TLabel").pack(anchor="w")
            ttk.Label(cell, text=_(text) + (" " + credit if credit else ""), style="Card.TLabel", wraplength=300, justify="left").pack(anchor="w", pady=(2, 0))
            if links:
                row = ttk.Frame(cell, style="Card.TFrame")
                row.pack(anchor="w", pady=(3, 0))
                for j, (label, url) in enumerate(links):
                    th.link(row, _(label), lambda url=url: webbrowser.open(url), style="CardLink.TLabel",
                            icon="external", right=True).pack(side="left", padx=(0 if j == 0 else 14, 0))
            logo = th.logo(self.LOGOS[cap], 16) if cap in self.LOGOS else None
            if logo is not None:
                self._logos = getattr(self, "_logos", []) + [logo]      # (kept, or Tk drops the picture)
                ttk.Label(cell, image=logo, style="Card.TLabel").pack(anchor="w", pady=(6, 0))
        tk.Frame(inner, background=th.BRASS_200, width=1).grid(row=0, column=1, rowspan=rows * 2 - 1, sticky="ns", padx=18)
        foot = ttk.Frame(body, style="Desk.TFrame")
        foot.pack(fill="x", pady=(14, 0))
        th.link(foot, _("Full notices and licenses"),
                lambda: webbrowser.open("https://github.com/%s/blob/main/THIRD_PARTY.md" % di.PROJECT_REPO),
                style="DeskLink.TLabel", icon="external", right=True).pack(side="left")
        ttk.Button(foot, text=_("Close"), command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())
        centre_over(top, root)


def tracks_text(tracks):
    """A stored title's tracks in a few words: "1080p · English DTS-HD MA 5.1 +2 · 3 subtitles"."""
    try:
        t = json.loads(tracks) if isinstance(tracks, str) else (tracks or {})
    except ValueError:
        return ""
    parts = []
    v = t.get("video") or {}
    if v.get("height"):
        parts.append("%s%s" % (v["height"], v.get("scan") or "p"))
    elif v.get("aspect"):
        parts.append(v["aspect"])
    aud = t.get("audio") or []
    if aud:
        a = aud[0]
        ch = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(a.get("channels"), "")
        name = _(AUDIO_NAMES[a["lang"]]) if a.get("lang") in AUDIO_NAMES else (a.get("lang") or "?").upper()
        parts.append(" ".join(x for x in (name, a.get("codec"), ch) if x) + (" +%d" % (len(aud) - 1) if len(aud) > 1 else ""))
    if t.get("subs"):
        parts.append(ui_n("{n} subtitle", "{n} subtitles", len(t["subs"])))
    return "  \u00b7  ".join(parts)


def edits_text(edits):
    """What a disc's edits for TheDiscDB come to: "3 new, 1 fix", those you've submitted apart ("2 submitted")."""
    due = [e for e in edits if not e.get("submitted")]
    fixes = sum(1 for e in due if e["fix"])
    return _(", ").join(x for x in (_("{n} new", n=len(due) - fixes) if len(due) > fixes else "",
                                    ui_n("{n} fix", "{n} fixes", fixes) if fixes else "",
                                    _("{n} submitted", n=len(edits) - len(due)) if len(edits) > len(due) else "") if x)


MONTHS = (N_("Jan"), N_("Feb"), N_("Mar"), N_("Apr"), N_("May"), N_("Jun"), N_("Jul"), N_("Aug"), N_("Sep"), N_("Oct"),
          N_("Nov"), N_("Dec"))     # (not strftime's: that's the system's locale, not the interface's)


def submitted_text(e):
    """An edit's word on its submission: "submitted Nov 13"; a month on with no word from TheDiscDB's data, to check."""
    if not e.get("submitted"):
        return ""
    try:
        when = di._dt.datetime.fromisoformat(e["submitted"])
        said = _("submitted {month} {day}", month=_(MONTHS[when.month - 1]), day=when.day)
    except ValueError:
        said = _("submitted")
    return said + (_(", not on TheDiscDB yet: check your suggestions there") if e.get("stale") else "")


class EditsDialog:
    """Help TheDiscDB > Ready to update > Suggest edits: your labels a disc on TheDiscDB lacks (or has otherwise),
    one per title, by its name there, for its own page's form (Edit Disc Item: Title and Type, Season / Episode):
    open the page, its pencil, enter them, Review Suggested Changes."""

    COLS = (("source", N_("Title there"), 110), ("length", N_("Length"), 70), ("type", N_("Type"), 110),
            ("title", N_("Title"), 300), ("se", N_("S/E"), 60), ("note", "", 160))

    def __init__(self, root, r, db=None, on_change=None):
        # (db: your case files, where what you submit is kept; on_change: told when it is)
        self.r, self.db, self.on_change = r, db, on_change
        self.top = top = new_dialog(root)
        top.title(_("Edits for TheDiscDB"))
        top.transient(root)
        top.configure(background=th.PARCHMENT)
        th.color_title_bar(top)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(18, 14, 18, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=r["title"], style="Title.TLabel", background=th.PARCHMENT).pack(anchor="w")
        ttk.Label(body, text=_("Each title has its own form on TheDiscDB (signed in with GitHub). Copy its title, open its "
                             "form, paste it and pick the type, then Review Suggested Changes. Once it's in, mark it "
                             "submitted: it goes from this list when TheDiscDB's data has it."),
                  style="DeskSub.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(2, 10))
        box = ttk.Frame(body)
        box.pack(fill="both", expand=True)
        self.tv = tv = ttk.Treeview(box, columns=[c for c, _x, _x in self.COLS], show="headings",
                                    height=min(12, max(3, len(r["edits"]))), selectmode="browse")
        for c, head, w in self.COLS:
            tv.heading(c, text=_(head) if head else "", anchor="w")
            tv.column(c, width=w, anchor="w", stretch=False)
        ys = th.ScrollBar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=ys.set)
        tv.grid(row=0, column=0, sticky="nsew")
        if len(r["edits"]) > 12:                  # (a scroll bar only for a list longer than the table)
            ys.grid(row=0, column=1, sticky="ns")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.full = {}
        for n, e in enumerate(r["edits"]):
            tv.insert("", "end", iid=str(n), values=self.cells(e))
        TreeTip(tv, lambda iid, col: self.tip(iid, col))
        tv.bind("<Double-1>", lambda e: self.open())
        tv.bind("<<TreeviewSelect>>", lambda e: self.picked())
        btns = ttk.Frame(body, style="Desk.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        self.copy_btn = ttk.Button(btns, text=_("Copy title"), style="Small.TButton", command=self.copy)
        self.copy_btn.pack(side="left")
        self.mark_btn = ttk.Button(btns, text=_("Mark submitted"), style="Small.TButton", command=self.mark)
        self.mark_btn.pack(side="left", padx=(8, 0))
        # (its form is on thediscdb.com: a link, to say so)
        self.form_link = th.link(btns, _("Open Title Form"), self.open, style="Link.TLabel", icon="external", right=True)
        self.form_link.pack(side="left", padx=(14, 0))
        ttk.Button(btns, text=_("Close"), command=top.destroy).pack(side="right")
        self.status = ttk.Label(body, text="", style="DeskSub.TLabel")
        self.status.pack(anchor="w", pady=(8, 0))
        top.bind("<Escape>", lambda e: top.destroy())
        fit_columns(tv, most=th.real(th.px(220)), full=self.full, fill="title")
        top.update_idletasks()
        top.minsize(top.winfo_reqwidth(), top.winfo_reqheight())
        tv.bind("<Configure>", lambda e: fit_first(tv, self.full, fill="title"), add="+")
        if tv.get_children():
            tv.selection_set(tv.get_children()[0])
        self.picked()
        centre_over(top, root)

    def cells(self, e):
        n = str(self.r["edits"].index(e))
        se = "S%sE%s" % (e["season"], e["episode"]) if e["season"] is not None and e["episode"] else ""
        note = submitted_text(e) or (_("there: {was}", was=" · ".join(x for x in (e["was"].get("type"), e["was"].get("title")) if x))
                                     if e["fix"] else "")
        self.full[n] = (e["source"], e["duration"] or "", e["type"] or "", e["title"] or "", se, note)
        return self.full[n]

    def tip(self, iid, col):
        whole = self.full.get(iid)
        cols = self.tv["columns"]
        if not whole or col not in cols:
            return ""
        text = str(whole[cols.index(col)])
        return text if self.tv.set(iid, col) != text else ""

    def current(self):
        sel = self.tv.selection()
        return self.r["edits"][int(sel[0])] if sel else None

    def picked(self):
        e = self.current()
        self.copy_btn.config(state="normal" if e and e.get("title") else "disabled")
        self.mark_btn.config(text=_("Not submitted") if e and e.get("submitted") else _("Mark submitted"),
                             state="normal" if e and self.db else "disabled")
        if e and e.get("url"):
            self.form_link.pack(side="left", padx=(14, 0), after=self.mark_btn)
        else:
            self.form_link.pack_forget()

    def mark(self):
        """The edit picked, submitted on TheDiscDB (kept till its data has it), or not after all."""
        e = self.current()
        if not e or not self.db:
            return
        con = di.library_open(self.db)
        try:
            di.set_suggested(con, self.r["hash"], e["our_title"], None if e.get("submitted") else e)
        finally:
            con.close()
        e["submitted"] = None if e.get("submitted") else di.now_iso()
        e["stale"] = False
        self.tv.item(str(self.r["edits"].index(e)), values=self.cells(e))
        fit_columns(self.tv, most=th.real(th.px(220)), full=self.full, fill="title")
        self.picked()
        self.status.config(text=(_("Marked submitted: {title}", title=e["title"] or e["source"])) if e["submitted"] else
                           _("No longer marked submitted: {title}", title=e["title"] or e["source"]))
        if self.on_change:
            self.on_change()

    def open(self):
        e = self.current()
        if e and e.get("url"):
            webbrowser.open(e["url"])

    def copy(self):
        e = self.current()
        if e and e.get("title"):
            self.top.clipboard_clear()
            self.top.clipboard_append(e["title"])
            self.status.config(text=_("Copied: {title}", title=e["title"]))


class HelpDiscDBDialog:
    """Settings > Data > Help TheDiscDB: the discs in your library TheDiscDB doesn't have yet
    (submit them), and the ones it has with titles worth labelling (suggest edits). Submitting
    needs your own GitHub account, so this only gathers what you need."""

    # (the disc's name first, taking what room the rest leave it: fit_columns)
    SUBMIT_COLS = (("title", N_("Disc"), 400), ("format", N_("Format"), 70), ("labelled", N_("Labelled"), 70))
    NEEDS_COLS = (("title", N_("Disc"), 400), ("format", N_("Format"), 70), ("needs", N_("Needs"), 200))
    IMPROVE_COLS = (("title", N_("Disc"), 400), ("disc", N_("TheDiscDB entry"), 230), ("what", N_("To suggest"), 120))
    # what a disc needs before it's submitted, and how to get it (in the steps' own order)
    # (a count's need: its singular and plural)
    NEEDS = {"scan": (N_("A scan"), N_("scan it again")),
             "check": (N_("Film checked"), N_("check the film it names and confirm it")),
             "confirm": (N_("Film confirmed"), N_("confirm the film, or pick another with Not right?")),
             "lab": (N_("Lab report"), N_("scan it with MakeMKV linked, and the lab reads every title")),
             "lab-old": (N_("Fresh lab report"), N_("scan it again with MakeMKV linked, and the lab reads every title "
                                                    "(its last reading left out the shortest)")),
             "lab-short": (N_("Clean reading"), N_("clean it and scan it again: the lab read titles short")),
             "film": (N_("Film labelled"), N_("label the film in Edit titles")),
             "episodes": ((N_("{n} episode labelled"), N_("{n} episodes labelled")),
                          N_("label its episodes, season and number, in Edit titles")),
             "extras": ((N_("{n} extra labelled"), N_("{n} extras labelled")),
                        N_("label its extras in Edit titles (a disc of nothing but extras)")),
             # (a disc TheDiscDB has: titles the scan found it leaves blank, yours to label, then to suggest there)
             "discdb-blank": ((N_("{n} title labelled"), N_("{n} titles labelled")),
                              N_("label the titles TheDiscDB leaves blank in Edit titles; "
                                 "then they're yours to suggest there, under Ready to update"))}

    def need_text(self, r, i, n):
        """One of a disc's needs, for its Needs column: its count filled in, capitalised if it's the first."""
        w = self.NEEDS[n][0]
        w = ui_n(w[0], w[1], r["missing"][n]) if isinstance(w, tuple) else _(w)
        return w if not i else w[0].lower() + w[1:] if not w[0].isdigit() else w

    def __init__(self, root, app):
        self.app, self.root = app, root
        self.releases = {}
        self.top = top = new_dialog(root)
        top.title(_("Help TheDiscDB"))
        top.transient(root)
        top.configure(background=th.PARCHMENT)
        th.color_title_bar(top)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(18, 14, 18, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=_("Help TheDiscDB"), style="Title.TLabel", background=th.PARCHMENT).pack(anchor="w")
        ttk.Label(body, text=_("Discs from your case files that TheDiscDB is missing, or could label better. "
                             "Submitting and suggesting edits need your own GitHub account on thediscdb.com."),
                  style="DeskSub.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(2, 10))
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill="both", expand=True)
        self.lists = {}
        for key, cols in (("submit", self.SUBMIT_COLS), ("needs", self.NEEDS_COLS), ("improve", self.IMPROVE_COLS)):
            page = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(page, text="")
            box = ttk.Frame(page)
            box.pack(fill="both", expand=True)
            tv = ttk.Treeview(box, columns=[c for c, _x, _x in cols], show="headings", height=12, selectmode="browse")
            for c, head, w in cols:
                tv.heading(c, text=_(head), anchor="w")
                tv.column(c, width=w, anchor="w", stretch=c == "title")
            # (each column as wide as what's in it, the disc's name the rest, cut short where it's longer: fit_columns)
            ys = th.ScrollBar(box, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=ys.set)
            tv.grid(row=0, column=0, sticky="nsew")
            ys.grid(row=0, column=1, sticky="ns")
            box.rowconfigure(0, weight=1)
            box.columnconfigure(0, weight=1)
            tv.bind("<<TreeviewSelect>>", lambda e, k=key: self.selected_changed(k))
            tv.bind("<Configure>", lambda e, k=key: fit_first(self.lists[k]["tv"], self.lists[k]["full"]), add="+")
            TreeTip(tv, lambda iid, col, k=key: self.cell_tip(k, iid, col))
            note = ttk.Label(page, text="", style="Sub.TLabel", wraplength=640, justify="left")
            note.pack(anchor="w", pady=(8, 6))
            btns = ttk.Frame(page)
            btns.pack(fill="x")
            self.lists[key] = {"tv": tv, "note": note, "rows": {}, "full": {}, "btns": btns, "page": page}
        s_btns, n_btns, i_btns = self.lists["submit"]["btns"], self.lists["needs"]["btns"], self.lists["improve"]["btns"]
        # (the log their form takes, and its pages walked through, TMDB id and edition included: SubmitGuide)
        self.packet_btn = ttk.Button(s_btns, text=_("Submit…"), style="Small.TButton", command=self.build_packet)
        self.packet_btn.pack(side="left")
        self.label_s = ttk.Button(s_btns, text=_("Label titles…"), style="Small.TButton",
                                  command=lambda: self.label("submit"))
        self.label_s.pack(side="left", padx=(8, 0))
        # (a fingerprint's only for a new disc's submission: Ready to submit's)
        self.page_btn = ttk.Button(i_btns, text=_("Open its TheDiscDB page"), style="Small.TButton",
                                   command=self.open_page)
        self.page_btn.pack(side="left")
        self.edits_btn = ttk.Button(i_btns, text=_("Suggest edits…"), style="Small.TButton", command=self.suggest)
        self.edits_btn.pack(side="left", padx=(8, 0))
        foot = ttk.Frame(body, style="Desk.TFrame")
        foot.pack(fill="x", pady=(10, 0))
        self.status = ttk.Label(foot, text=_("Looking through your case files…"), style="DeskSub.TLabel")
        self.status.pack(side="left")
        ttk.Button(foot, text=_("Close"), command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())
        for l in self.lists.values():             # (room for a note of two lines on every tab: nothing pushed off)
            l["note"].config(text="\n")
        top.update_idletasks()
        top.minsize(top.winfo_reqwidth(), min(top.winfo_reqheight(), top.winfo_screenheight() - 120))
        # (its size kept as it opens: the disc's name takes what room there is, not widening the window)
        top.geometry("%dx%d" % (top.winfo_reqwidth(), min(top.winfo_reqheight(), top.winfo_screenheight() - 120)))
        for k in self.lists:
            self.selected_changed(k)
        centre_over(top, root)
        self.load(online=False)

    # ---- filling the lists (index first: instant; then thediscdb.com: newest) ----
    def load(self, online):
        args = self.app.scan_args()
        check_online = online and not args.no_discdb_online

        def work():
            try:
                con = di.library_open(args.db)
                index = di.discdb_index_open(args.discdb_dir or di.default_discdb_dir(), allow_download=False)
                sub = di.submission_candidates(con, index, check_online)
                imp = di.improvement_candidates(con, index, check_online)
                con.close()
                self.app.post(lambda: self.show(sub, imp, check_online, None))
            except Exception as exc:
                di.logger.exception(i18n.log("Help TheDiscDB lists"))
                msg = str(exc)           # (exc is gone once the except ends)
                self.app.post(lambda: self.show([], [], check_online, msg))
        threading.Thread(target=work, daemon=True).start()
        if check_online:
            self.status.config(text=_("Checking thediscdb.com for newer entries…"))

    def show(self, sub, imp, checked_online, err):
        if not self.top.winfo_exists():
            return
        ready = [r for r in sub if not r["needs"]]
        needs = [r for r in sub if r["needs"]]
        self.fill("submit", ready, lambda r: (r["title"], r["format"] or "",
                                              "%d%s" % (r["labelled"], "  " + ui_n("({n} extra not)", "({n} extras not)",
                                                                                     r["open_extras"])
                                                        if r["open_extras"] else "")))
        # (a disc TheDiscDB has: your labels to suggest there; only titles the scan found, to label first)
        edit = [r for r in imp if r["edits"]]
        needs = needs + [dict(r, needs=["discdb-blank"], missing={"discdb-blank": r["unlabelled"]})
                         for r in imp if not r["edits"] and r["unlabelled"]]
        self.fill("needs", needs, lambda r: (r["title"], r["format"] or "",
                                             ", ".join(self.need_text(r, i, n) for i, n in enumerate(r["needs"]))))
        self.fill("improve", edit, lambda r: (r["title"], "%s, %s" % (r["disc"], r["release"])
                                              if r.get("disc") else r.get("release") or "", edits_text(r["edits"])))
        self.tabs.tab(0, text=_("READY TO SUBMIT ({count})", count=len(ready)))
        self.tabs.tab(1, text=_("NEEDS MORE INFO ({count})", count=len(needs)))
        self.tabs.tab(2, text=_("READY TO UPDATE ({count})", count=len(edit)))
        if err:
            self.status.config(text=_("Couldn't read your case files: {err}", err=err))
        elif checked_online or self.app.scan_args().no_discdb_online:
            self.status.config(text=_("Up to date with {where}.", where="thediscdb.com" if checked_online
                                                             else "your downloaded TheDiscDB data"))
        else:
            self.load(online=True)              # now the slower check, for anything added since

    def fill(self, key, rows, cells):
        l = self.lists[key]
        keep = (l["tv"].selection() or [None])[0]
        l["tv"].delete(*l["tv"].get_children())
        l["rows"], l["full"] = {}, {}
        for r in rows:
            iid = r["hash"]
            l["full"][iid] = cells(r)
            l["tv"].insert("", "end", iid=iid, values=l["full"][iid])
            l["rows"][iid] = r
        if keep in l["rows"]:
            l["tv"].selection_set(keep)
        fit_columns(l["tv"], most=th.real(th.px(220)), full=l["full"])   # (the rest cut short: the name has the room)
        self.selected_changed(key)

    def cell_tip(self, key, iid, col):
        """A cell's whole text, under the pointer, where it's cut short (a long name, say)."""
        l = self.lists[key]
        whole, cols = l["full"].get(iid), l["tv"]["columns"]
        if not whole or col not in cols or not l["tv"].exists(iid):
            return ""
        text = str(whole[cols.index(col)])
        return text if l["tv"].set(iid, col) != text else ""

    def current(self, key):
        l = self.lists[key]
        sel = l["tv"].selection()
        return l["rows"].get(sel[0]) if sel else None

    def selected_changed(self, key):
        r = self.current(key)
        l = self.lists[key]
        if key == "submit":
            for b in (self.packet_btn, self.label_s):
                b.config(state="normal" if r else "disabled")
            if not r:
                text = _("Pick a disc. These are confirmed, read by the lab and labelled: ready for TheDiscDB.")
            else:
                # (their form scans the disc as it's added: the disc, or a backup of it, to hand)
                why = r["why"][0].upper() + r["why"][1:]
                text = (_("{why}, mastered {date}. Their form scans the disc: have it in the drive.", why=why, date=r["mastered"])
                        if r.get("mastered") else _("{why}. Their form scans the disc: have it in the drive.", why=why))
        elif key == "needs":
            if not r:
                text = _("Pick a disc to see what it needs. Once it has it all, it's ready to submit.")
            else:
                # (each with the disc in the drive: a title can't be checked without it)
                text = _("To do: Put the disc in the drive and {steps}.",
                         steps=_("; then ").join(_(self.NEEDS[n][1]) for n in r["needs"]))
        else:
            self.edits_btn.config(state="normal" if r else "disabled")
            self.page_btn.config(state="normal" if r and r.get("page") else "disabled")
            if not r:
                text = _("Pick a disc. These are ones TheDiscDB has, with labels of yours it lacks or has otherwise.")
            else:
                text = _("Your labels for {edits} on TheDiscDB, each on its title's page there. Suggest edits lists them.",
                         edits=edits_text(r["edits"]))
        l["note"].config(text=text)

    # ---- actions ----
    def tmdb_lookup(self, r, done):
        """The disc's TMDB id, which TheDiscDB's form finds the film by, from its IMDb id (TMDB's lookup with your
        key: di.find_tmdb), kept; done(found, where it was looked for, error) after."""
        args = self.app.scan_args()
        key = self.tmdb_key()
        where = "TMDB"

        def work():
            try:
                found = di.find_tmdb(r["imdb_id"], key)
                err = None
            except Exception as exc:              # (offline, the service down: say so, and search instead)
                di.logger.warning(i18n.log("TMDB id of {imdb_id}: {error}", imdb_id=r["imdb_id"], error=exc))
                found, err = None, str(exc)
            if found:
                con = di.library_open(args.db)
                di.set_disc_tmdb(con, r["hash"], found)
                con.close()
                r["tmdb"] = found
            self.app.post(lambda: done(found, where, err))
        threading.Thread(target=work, daemon=True).start()
        return where

    def tmdb_key(self):
        return getattr(self.app.scan_args(), "tmdb_key", None) or os.environ.get("TMDB_API_KEY") or None

    def label(self, key):
        r = self.current(key)
        if r:
            TitlesDialog(self.top, self.app, r["hash"], r["title"], on_save=lambda: self.load(online=False), review=True)

    def open_page(self):
        r = self.current("improve")
        if r and r.get("page"):
            webbrowser.open(r["page"])

    def suggest(self):
        r = self.current("improve")
        if r:
            self.edits_dialog = EditsDialog(self.top, r, db=self.app.scan_args().db,
                                            on_change=lambda: self.load(online=False))

    def build_packet(self):
        """A packet for the disc picked, with the disc to hand: TheDiscDB's form scans it as it's added (its file
        list and IFO or AACS files), so it's looked for in the drives first (core.same_disc: one folder's
        listing); not in one, a backup folder of the whole disc will do."""
        r = self.current("submit")
        if not r:
            return
        self.packet_btn.config(state="disabled")
        self.status.config(text=_("Looking for the disc…"))

        def find():
            drive = next((d for d in optical_drives() if di.same_disc(d, r["hash"])), None)
            self.app.post(lambda: self.disc_found(r, drive))
        threading.Thread(target=find, daemon=True).start()

    def disc_found(self, r, drive):
        if not self.top.winfo_exists():
            return
        root = drive
        if drive is None:
            self.packet_btn.config(state="normal")
            if not messagebox.askyesno(_("Help TheDiscDB"), _("TheDiscDB's form scans the disc as you add it, and it isn't "
                                       "in a drive.\n\nUse a backup folder of the whole disc instead?"), parent=self.top):
                self.status.config(text=_("Put the disc in the drive, then Submit again."))
                return
            folder = filedialog.askdirectory(parent=self.top, title=_("The disc's backup folder"), mustexist=True)
            if not folder:
                self.status.config(text=_("Put the disc in the drive, then Submit again."))
                return
            if not di.same_disc(folder, r["hash"]):
                self.status.config(text=_("That folder isn't this disc: pick its backup, the folder holding {folder}.", folder="BDMV" if r.get("format") == "Blu-ray" else "VIDEO_TS"))
                return
            root = os.path.normpath(folder)
        self.packet_work(r, root)

    def packet_work(self, r, root=None):
        h = r["hash"]
        stored = di.makemkv_log_store(h)      # (the lab's reading of every title: each disc on the Ready list has one)
        args = self.app.scan_args()
        out = os.path.join(os.path.dirname(args.db), "packets", "thediscdb_%s" % h[:8])
        self.packet_btn.config(state="disabled")
        self.status.config(text=_("Writing the log…"))

        def work():
            try:
                with open(stored, encoding="utf-8", errors="replace") as fh:
                    log_text = fh.read()
                con = di.library_open(args.db)
                pk = di.build_packet(con, h, log_text, out, root=root, find_edition=True)
                con.close()
                self.app.post(lambda: self.packet_done(r, out, pk, None))
            except (Exception, SystemExit) as exc:
                di.logger.exception(i18n.log("packet for {disc}", disc=h[:8]))
                msg = str(exc)           # (exc is gone once the except ends)
                self.app.post(lambda: self.packet_done(r, out, None, msg))
        threading.Thread(target=work, daemon=True).start()

    def packet_done(self, r, out, pk, err):
        if not self.top.winfo_exists():
            return
        self.packet_btn.config(state="normal")
        if err:
            self.status.config(text=_("Couldn't write the log: {err}", err=err))
            return
        if pk.get("read_short"):
            self.status.config(text=_("Don't submit it yet: {problem}. Clean the disc, scan it again, then try again.", problem=short_reads(pk["read_short"])))
        else:
            self.status.config(text=ui_n("Log ready: {matched} of {n} title matched. The guide walks you through their form.", "Log ready: {matched} of {n} titles matched. The guide walks you through their form.", pk["real_titles"], matched=pk["real_matched"]))
        # (its TMDB id looked up only with your TMDB key; without one, their form's search by the title does)
        # (the edition's ASIN, UPC and date you type in, kept while this window's open: Submit again, still there)
        pk["release"] = self.releases.setdefault(r["hash"], {})
        pk["steps"] = di.submission_steps(pk, None)
        self.guide = SubmitGuide(self.top, pk, find_tmdb=(lambda done: self.tmdb_lookup(r, done))
                                 if r.get("imdb_id") and self.tmdb_key() else None)


def hint_lines(note):
    """A field's instructions a line a part ("the barcode's number: on the case; the disc never carries it" ->
    two lines), each begun with a capital; not inside brackets ("Disc 1" in a set; both films' names ...)."""
    parts, cur, depth = [], "", 0
    for k, ch in enumerate(note or ""):
        depth += (ch == "(") - (ch == ")")
        cur += ch
        if ch in ";." and not depth and (note[k + 1:k + 2] or " ").isspace():
            parts.append(cur)
            cur = ""
    parts = [p.strip().rstrip(";.") for p in parts + [cur] if p.strip()]
    return "\n".join(p[:1].upper() + p[1:] for p in parts)


class SubmitGuide:
    """Help TheDiscDB > Submit…: TheDiscDB's contribute form a page at a time, in its order (core.submission_steps),
    what goes in each of its fields beside it and the disc's titles inline, to type in or copy (the copy icon beside
    each one their form has you type, or paste in its picker; none for its drop-downs). The only file their form
    takes, the log, shown where it is. find_tmdb(done): the TMDB id looked up, where the guide has none yet (with
    your TMDB key: without one, no TMDB id, their search by the title)."""

    WRAP = 560

    def __init__(self, root, pk, find_tmdb=None):
        self.pk, self.steps, self.i, self.find_tmdb = pk, pk["steps"], 0, find_tmdb
        self.top = top = new_dialog(root)
        top.title(_("Submit to TheDiscDB"))
        top.transient(root)
        body = ttk.Frame(top, padding=(18, 14, 18, 14))
        body.pack(fill="both", expand=True)
        self.count = ttk.Label(body, text="", style="Sub.TLabel")
        self.count.pack(anchor="w")
        self.head = ttk.Label(body, text="", style="Title.TLabel")
        self.head.pack(anchor="w", pady=(0, 8))
        self.page = ttk.Frame(body)
        self.page.pack(fill="both", expand=True)
        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(12, 0))
        self.status = ttk.Label(foot, text="", style="Sub.TLabel")
        self.status.pack(side="left")
        ttk.Button(foot, text=_("Close"), command=top.destroy).pack(side="right")
        self.next_btn = ttk.Button(foot, text=_("Next"), command=lambda: self.show(self.i + 1))
        self.next_btn.pack(side="right", padx=(0, 8))
        self.back_btn = ttk.Button(foot, text=_("Back"), command=lambda: self.show(self.i - 1))
        self.back_btn.pack(side="right", padx=(0, 8))
        top.bind("<Escape>", lambda e: top.destroy())
        self.show(0)
        top.update_idletasks()
        top.minsize(th.real(th.px(self.WRAP + 120)), top.winfo_reqheight())
        centre_over(top, root)

    def show(self, i):
        if not 0 <= i < len(self.steps):
            self.top.destroy()
            return
        self.i, s = i, self.steps[i]
        for w in self.page.winfo_children():
            w.destroy()
        self.count.config(text=_("Before you start") if not i else _("Step {step} of {total}", step=i, total=len(self.steps) - 1))
        self.head.config(text=_(s["head"]))
        self.status.config(text="")
        wrap = th.px(self.WRAP)
        line = lambda text, **kw: ttk.Label(self.page, text=text, wraplength=wrap, justify="left", **kw).pack(
            anchor="w", pady=(0, 6))
        for w in s["warn"]:
            line(w, style="Warn.TLabel", background=ttk.Style().lookup("TFrame", "background"))
        for n in s["notes"]:
            line(n)
        if s["links"]:
            row = ttk.Frame(self.page)
            row.pack(anchor="w", pady=(0, 8))
            for k, (text, url) in enumerate(s["links"]):
                th.link(row, text, lambda u=url: webbrowser.open(u), style="Link.TLabel", icon="external",
                        right=True).pack(side="left", padx=(0 if not k else 14, 0))
        fields = s["fields"]
        if self.find_tmdb and s["head"] == "Identify the release" and not any(f[0] == "TMDB ID" for f in fields):
            fields = [("TMDB ID", "", "", None)] + fields       # (to look up: look_up_tmdb)
        if fields:
            grid = ttk.Frame(self.page)
            grid.pack(fill="x", pady=(0, 6))
            grid.columnconfigure(1, weight=1)
            for r, (name, value, note, copy) in enumerate(fields):
                ttk.Label(grid, text=name, style="Sub.TLabel").grid(row=r, column=0, sticky="nw", padx=(0, 14), pady=(0, 8))
                cell = ttk.Frame(grid)
                cell.grid(row=r, column=1, sticky="w", pady=(0, 8))
                if value:
                    row = ttk.Frame(cell)
                    row.pack(anchor="w")
                    ttk.Label(row, text=value, style="Plan.TLabel").pack(side="left")
                    if copy:
                        self.copier(row, copy).pack(side="left", padx=(6, 0))
                elif name == "TMDB ID" and self.find_tmdb:
                    # (not known yet: looked up from its IMDb id, here, then shown with the rest)
                    self.tmdb_link = th.link(cell, _("Find its TMDB id"), self.look_up_tmdb, style="Link.TLabel")
                    self.tmdb_link.pack(anchor="w")
                    continue
                if note:
                    ttk.Label(cell, text=hint_lines(note), style="Hint.TLabel", wraplength=th.px(self.WRAP - 160),
                              justify="left").pack(anchor="w", pady=(2, 0))
        if s["file"]:
            ttk.Button(self.page, text=_("Show the file"), style="Small.TButton",
                       command=lambda: self.show_file(s["file"])).pack(anchor="w", pady=(0, 8))
        if s["titles"]:
            area = ScrollArea(self.page, max_frac=0.4)
            area.pack(fill="both", expand=True, pady=(0, 8))
            episodes = any(t[3] for t in s["titles"])
            said = s.get("descriptions") or []      # (their optional Description: the words under its menu button)
            described = any(said)
            for c, head in ((0, _("Playlist")), (1, _("Type")), (2, _("Name"))) + (((4, _("S/E")),) if episodes else ()) + \
                    (((5, _("Description")),) if described else ()):
                ttk.Label(area.inner, text=head, style="Sub.TLabel").grid(row=0, column=c, sticky="w", padx=(0, 14))
            for r, (src, kind, name, se) in enumerate(s["titles"], 1):
                ttk.Label(area.inner, text=src).grid(row=r, column=0, sticky="w", padx=(0, 14))
                ttk.Label(area.inner, text=kind).grid(row=r, column=1, sticky="w", padx=(0, 14))   # (a drop-down)
                ttk.Label(area.inner, text=name).grid(row=r, column=2, sticky="w")
                self.copier(area.inner, name).grid(row=r, column=3, sticky="w", padx=(6, 14))
                if episodes:
                    ttk.Label(area.inner, text=se).grid(row=r, column=4, sticky="w", padx=(0, 14))
                words = said[r - 1] if r - 1 < len(said) else ""
                if words:
                    ttk.Label(area.inner, text=words, wraplength=th.px(260)).grid(row=r, column=5, sticky="w")
                    self.copier(area.inner, words).grid(row=r, column=6, sticky="w", padx=(6, 0))
        if s.get("inputs"):
            self.inputs(s["inputs"], wrap)
        for a in s["after"]:
            line(a, style="Sub.TLabel")
        self.back_btn.config(state="normal" if i else "disabled")
        self.next_btn.config(text=_("Done") if i == len(self.steps) - 1 else _("Next"))

    # what each box takes, as their form wants it (None: not that)
    CLEAN = {"asin": lambda t: di.clean_asin(t),
             "upc": lambda t: di.clean_upc(t),
             "date": lambda t: (lambda d: d and "%s %d, %d" % (d.strftime("%B"), d.day, d.year))(di.release_date(t))}
    BAD = {"asin": N_("10 letters and numbers, or the listing's address"), "upc": N_("12 or 13 digits"),
           "date": N_("a date: August 18, 2009, or 2009-08-18")}

    def inputs(self, boxes, wrap):
        """Boxes for what's found before their form's filled in (the edition's ASIN, UPC, release date): kept in the
        packet's "release" as typed, the steps made again from them as they change (core.submission_steps), each
        box saying what it read."""
        rel = self.pk.setdefault("release", {})
        grid = ttk.Frame(self.page)
        grid.pack(fill="x", pady=(4, 8))
        grid.columnconfigure(1, weight=1)
        self.boxes = {}
        for r, (key, name, note) in enumerate(boxes):
            ttk.Label(grid, text=name, style="Sub.TLabel").grid(row=2 * r, column=0, sticky="nw", padx=(0, 14))
            var = tk.StringVar(value=rel.get(key, ""))
            entry = ttk.Entry(grid, textvariable=var, width=34)
            entry.grid(row=2 * r, column=1, sticky="w")
            said = ttk.Label(grid, text="", wraplength=th.px(self.WRAP - 160), justify="left")
            said.grid(row=2 * r + 1, column=1, sticky="w", pady=(2, 8))
            self.boxes[key] = (var, said, note)
            var.trace_add("write", lambda *a, k=key: self.typed(k))
            self.typed(key, remake=False)

    def typed(self, key, remake=True):
        var, said, note = self.boxes[key]
        text = var.get().strip()
        self.pk["release"][key] = text
        got = self.CLEAN[key](text) if text else None
        said.config(text=(_("✓ {got}", got=got)) if got else _(self.BAD[key]) if text else hint_lines(note),
                    style="Warn.TLabel" if text and not got else "TLabel" if got else "Hint.TLabel",
                    background=ttk.Style().lookup("TFrame", "background"))
        if remake:
            self.steps = self.pk["steps"] = di.submission_steps(self.pk, None)

    def copier(self, parent, value):
        """The copy icon beside a value to go in their form: clicked, the value copied."""
        lbl = th.link(parent, "", lambda: self.copy(value), style="Link.TLabel", icon="copy")
        lbl.copies = value
        th.Tooltip(lbl, lambda: _("Copy"))
        return lbl

    def look_up_tmdb(self):
        """Identify the release's TMDB id, not known yet: looked up (find_tmdb), then the page again with it, and it
        copied; nowhere to be found, TMDB's own search for the title, in the browser."""
        self.tmdb_link.unbind("<Button-1>")
        where = self.find_tmdb(self.tmdb_found)
        self.status.config(text=_("Looking up its TMDB id on {where}…", where=where))

    def tmdb_found(self, found, where, err):
        if not self.top.winfo_exists():
            return
        if found:
            self.pk["tmdb"] = found
            self.steps = self.pk["steps"] = di.submission_steps(self.pk, None)
            self.show(self.i)
            self.copy(str(found[1]))
            return
        self.show(self.i)
        title = di.parse_truth(self.pk.get("truth") or "")["title"] or self.pk.get("truth") or ""
        self.status.config(text=_("{where} doesn't know its TMDB id ({error}): search TMDB for it.", where=where, error=err) if err else _("{where} doesn't know its TMDB id: search TMDB for it.", where=where))
        webbrowser.open("https://www.themoviedb.org/search?" + urllib.parse.urlencode({"query": title}))

    def copy(self, value):
        self.top.clipboard_clear()
        self.top.clipboard_append(value)
        self.status.config(text=_("Copied: {value}", value=value))

    def show_file(self, path):
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])


class SettingsDialog:
    """Scan ▾ > Settings: disc data, how discs are scanned, the programs and keys it works with."""

    def __init__(self, root, app):
        self.app = app
        st = app.settings
        self.top = top = new_dialog(root)
        top.title(_("{APP} settings", APP=APP))
        top.transient(root)
        th.color_title_bar(top)
        top.resizable(False, False)
        outer = ttk.Frame(top, padding=(16, 14, 16, 16))
        outer.pack(fill="both", expand=True)
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        f, r = None, 0

        def tab(name):
            """Start a new tab; the rows that follow go on it (scrolling, if the screen's too short
            for it: the Close button stays on screen)."""
            nonlocal f, r
            area = ScrollArea(self.tabs, max_frac=0.72)
            f = ttk.Frame(area.inner, padding=(14, 12, 14, 14))
            f.pack(fill="both", expand=True)
            f.columnconfigure(1, weight=1)
            self.tabs.add(area, text=_(name).upper())
            r = 0

        def head(text):
            nonlocal r
            ttk.Label(f, text=text, style="Plan.TLabel").grid(row=r, column=0, columnspan=3, sticky="w",
                                                              pady=(0 if r == 0 else 14, 4))
            r += 1

        tab(N_("Disc"))
        head(_("Ripping"))
        # (the rip menu's own tick, the same setting: either one changes both)
        ttk.Checkbutton(f, text=_("Include extras by default"), variable=self.app.extras,
                        command=self.app.options_changed).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        head(_("Detect titles"))
        self.detect = tk.BooleanVar(value=st.get("menu_detect", False))
        ttk.Checkbutton(f, text=_("Detect titles from a DVD's menus"), variable=self.detect).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.bluray_menus = tk.BooleanVar(value=st.get("bluray_menus", False))
        ttk.Checkbutton(f, text=_("Detect titles from a Blu-ray's menus (experimental)"), variable=self.bluray_menus).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(f, text=_("Adds Detect titles to Edit titles (the menu beside Rip): VLC plays the menus and presses their buttons "
                          "to find the film and its extras. Anything else is left out of the rip."),
                  style="Sub.TLabel", wraplength=420, justify="left").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        # Detect titles as part of the scan: its readings help name the disc, and its titles are named
        ttk.Label(f, text=_("During a scan (adds a few minutes):")).grid(row=r, column=0, columnspan=3, sticky="w",
                                                                       pady=(6, 0))
        r += 1
        self.in_scan = tk.StringVar(value=st.get("detect_in_scan", "never"))
        self.in_scan_btns = []
        for value, text in (("never", _("Don't detect titles")),
                            ("weak", _("Detect titles when the film isn't certain")),
                            ("unknown", _("Detect titles on every disc whose titles aren't known yet"))):
            b = ttk.Radiobutton(f, text=text, value=value, variable=self.in_scan)
            b.grid(row=r, column=0, columnspan=3, sticky="w", padx=(18, 0))
            self.in_scan_btns.append(b)
            r += 1
        ttk.Label(f, text=_("Known titles: named by TheDiscDB, or by you in Edit titles."),
                  style="Sub.TLabel", wraplength=420, justify="left").grid(row=r, column=0, columnspan=3, sticky="w",
                                                                           padx=(18, 0))
        r += 1
        self.every_title = tk.BooleanVar(value=st.get("menu_every_title", False))
        every = ttk.Checkbutton(f, text=_("Read menus for copies and Play Alls too (slower: a picture for every title)"),
                                variable=self.every_title)
        every.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.every_btn = every
        r += 1
        self.detect.trace_add("write", lambda *a: self.show_in_scan())
        self.bluray_menus.trace_add("write", lambda *a: self.show_in_scan())
        self.show_in_scan()

        head(_("TV episodes"))
        self.order = tk.StringVar(value=st.get("episode_order", "aired"))
        for value, text in (("aired", _("Name files in aired order (Plex's default)")),
                            ("dvd", _("Name files in DVD order (set the show to DVD order in Plex)"))):
            ttk.Radiobutton(f, text=text, value=value, variable=self.order).grid(
                row=r, column=0, columnspan=3, sticky="w")
            r += 1
        ttk.Label(f, text=_("Episodes are matched to the disc in DVD order either way. Episode lists "
                          "from TVmaze (tvmaze.com); applies to the next scan."),
                  style="Sub.TLabel", wraplength=420, justify="left").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        tab(N_("Data"))
        head(_("Disc data"))
        self.status, self.update_btns = {}, {}
        for key, name in (("discdb", "TheDiscDB"), ("imdb", "IMDb")):
            ttk.Label(f, text=name).grid(row=r, column=0, sticky="w", padx=(0, 12))
            self.status[key] = ttk.Label(f, text="", style="Sub.TLabel", wraplength=300, justify="left")
            self.status[key].grid(row=r, column=1, sticky="w")
            self.update_btns[key] = ttk.Button(f, text=_("Update now"), style="Small.TButton",
                                               command=lambda k=key: self.update(k))
            self.update_btns[key].grid(row=r, column=2, sticky="e", pady=3)
            r += 1
        self.prog = DataProgress(f)
        self.prog_row = r
        r += 1
        head(_("Help TheDiscDB"))
        ttk.Label(f, text=_("Discs from your case files that TheDiscDB is missing, or could label better."),
                  style="Sub.TLabel", wraplength=300, justify="left").grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text=_("Show discs…"), style="Small.TButton",
                   command=lambda: HelpDiscDBDialog(self.top, self.app)).grid(row=r, column=2, sticky="e")
        r += 1

        tab(N_("Integrations"))
        # the programs it works with: each one's standing, and its publisher's installer a press away (winget,
        # with your consent; its own window and licence), or its download page; or a copy of your own (Find…)
        head(_("Programs"))
        self.tool_rows, self.installing, self.queue, self.failed = {}, None, [], {}
        self.paths, self.tool_paths, self.tool_tips = {}, {}, {}
        for t in tools.LISTED:
            k = t["key"]
            self.paths[k] = tk.StringVar(value=st.get(k, ""))
            ttk.Label(f, text=t["name"]).grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=3)
            note = ttk.Label(f, text="", style="Sub.TLabel", wraplength=300, justify="left")
            note.grid(row=r, column=1, sticky="w", pady=3)
            # (a path: one line, ✓ before it, an ellipsis if it's too long; the whole of it on hover)
            note.bind("<Configure>", lambda e, k=k: self.fit_tool_path(k))
            self.tool_tips[k] = th.Tooltip(note, lambda k=k: (self.tool_paths.get(k) or ("", ""))[1])
            cell = ttk.Frame(f)
            cell.grid(row=r, column=2, sticky="ne", pady=3)
            btn = ttk.Button(cell, text=_("Install…"), style="Small.TButton", command=lambda k=k: self.get_tools([k]))
            btn.pack(side="left")
            pick = ttk.Button(cell, text=_("Find…"), style="Small.TButton", command=lambda k=k: self.pick_tool(k))
            pick.pack(side="left", padx=(4, 0))
            auto = ttk.Button(cell, text=_("Automatic"), style="Small.TButton", command=lambda k=k: self.paths[k].set(""))
            auto.pack(side="left", padx=(4, 0), after=pick)
            self.tool_rows[k] = (note, btn, pick, auto)
            r += 1
        self.mkv, self.vlc = self.paths["makemkv"], self.paths["vlc"]
        self.all_btn = ttk.Button(f, text=_("Install all missing…"), style="Small.TButton",
                                  command=lambda: self.get_tools(self.missing_tools()))
        self.all_btn.grid(row=r, column=1, columnspan=2, sticky="e", pady=(2, 0))
        r += 1

        head(_("Posters"))
        ttk.Label(f, text=_("TMDB key")).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=(6, 0))
        self.key = tk.StringVar(value=st.get("tmdb_key", ""))
        ttk.Entry(f, textvariable=self.key, show="•", width=38).grid(row=r, column=1, sticky="we", pady=(6, 0))
        ttk.Button(f, text=_("Test"), command=self.test_key).grid(row=r, column=2, sticky="e", padx=(6, 0), pady=(6, 0))
        r += 1
        self.key_note = ttk.Label(f, text=_("Optional: posters TheDiscDB doesn't have, and a fallback search. "
                                         "Get a free key at themoviedb.org"), style="Sub.TLabel",
                                  cursor="hand2", wraplength=380, justify="left")
        self.key_note.grid(row=r, column=1, columnspan=2, sticky="w")
        self.key_note.bind("<Button-1>", lambda e: webbrowser.open("https://www.themoviedb.org/settings/api"))
        r += 1

        head(_("AI Assistance"))
        ttk.Label(f, text=_("Gemini key")).grid(row=r, column=0, sticky="w", padx=(0, 12))
        self.gkey = tk.StringVar(value=st.get("gemini_key", ""))
        ttk.Entry(f, textvariable=self.gkey, show="•", width=38).grid(row=r, column=1, sticky="we")
        ttk.Button(f, text=_("Test"), command=self.test_gemini).grid(row=r, column=2, sticky="e", padx=(6, 0))
        r += 1
        self.gkey_note = ttk.Label(f, text=_("Optional: when nothing identifies a disc that has pictures of its "
                                          "own, Gemini looks at them and IMDb checks its answer. The picture and the "
                                          "disc's clues go to Google (on the free tier Google may use them to improve "
                                          "its products). Get a free key at Google AI Studio"),
                                   style="Sub.TLabel", cursor="hand2", wraplength=380, justify="left")
        self.gkey_note.grid(row=r, column=1, columnspan=2, sticky="w")
        self.gkey_note.bind("<Button-1>", lambda e: webbrowser.open("https://aistudio.google.com/apikey"))
        r += 1

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text=_("Close"), command=self.close).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", self.close)
        centre_over(top, root)
        self.refresh_status()
        self.refresh_tools()
        for var in self.paths.values():
            var.trace_add("write", lambda *a: self.refresh_tools())
        for var in (self.key, self.gkey, self.order, self.detect, self.bluray_menus, self.in_scan,
                    self.every_title, *self.paths.values()):   # saved as you go
            var.trace_add("write", lambda *a: self.save())

    def refresh_status(self):
        args = self.app.scan_args()
        d = di.discdb_index_info(args.discdb_dir or di.default_discdb_dir())
        self.status["discdb"].config(text=_("{count} discs · built {date}", count=human_count(_n(d.get("discs"))), date=d.get("built", "")[:10])
                                     if d else _("not downloaded yet"))
        i = di.imdb_index_info(args.imdb_dir)
        c = di.imdb_credits_info(args.imdb_dir)
        if i:
            people = _("{count} people", count=human_count(c["people"])) if c else _("no credits yet")
            text = _("{count} titles · {people} · built {date}", count=human_count(i["titles"]), people=people, date=i["built"])
            if not (i["current"] and c and c["current"]):
                text = _("{status} (rebuild needed)", status=text)
        self.status["imdb"].config(text=text if i else _("not downloaded yet"))

    def tool_settings(self):
        """Settings as they stand in this window (a path typed in, not yet saved, counts)."""
        return dict(self.app.settings, **{k: v.get() for k, v in self.paths.items()})

    def missing_tools(self):
        return [t["key"] for t in tools.LISTED if not tools.find(t["key"], self.tool_settings())]

    def chosen_tool(self, k):
        """A copy of a program chosen with Find…: (its path, whether it's there), or None."""
        p = self.paths[k].get().strip()
        if not p:
            return None
        if k == "vlc":
            from . import menus
            ok = menus.find_vlc(p) == p
        else:
            ok = os.path.isfile(p) and (k == "makemkv" or tools.runs(k, p))
        return p, ok

    def pick_tool(self, k):
        """Find…: a copy of your own, used instead of the one found (VLC: its folder)."""
        name = tools.BY_KEY[k]["name"]
        if k == "vlc":
            p = filedialog.askdirectory(title=_("Find VLC's folder"), parent=self.top)
        else:
            exe = {"makemkv": "makemkvcon", "ffmpeg": "ffmpeg"}.get(k, k)
            types = [(_("Programs"), "*.exe"), (_("All files"), "*")] if os.name == "nt" else [(_("All files"), "*")]
            p = filedialog.askopenfilename(title=_("Find {name} ({exe})", name=name, exe=exe), filetypes=types, parent=self.top)
        if p:
            self.paths[k].set(os.path.normpath(p))

    PATH_CHARS = 44           # a program's path, on one line: about as wide as the other rows' notes

    def fit_tool_path(self, k):
        shown = self.tool_paths.get(k)
        if shown:
            fit_label(self.tool_rows[k][0], shown[0], "Sub.TLabel")

    def tool_note(self, k, text=None, path=None, tip=None):
        """A program's note: a message, wrapped; or a path (found, or yours), ✓ and one line, its whole in a
        tooltip (tip: what it says, the path itself if not given)."""
        note = self.tool_rows[k][0]
        if path:
            self.tool_paths[k] = ("✓ " + path, tip or path)
            note.config(wraplength=0, width=self.PATH_CHARS)
            fit_label(note, self.tool_paths[k][0], "Sub.TLabel")
        else:
            self.tool_paths.pop(k, None)
            self.tool_tips[k].hide()
            note.config(text=text, wraplength=300, width=0)

    def refresh_tools(self):
        """Each program's line: where it was found, or the copy you chose; or what it's for, and Install
        (winget) or its download page. Find… for a copy of your own; Automatic, back to the one found."""
        if not getattr(self, "tool_rows", None):
            return
        winget = tools.winget()
        missing = []
        for t in tools.LISTED:
            k = t["key"]
            note, btn, pick, auto = self.tool_rows[k]
            chosen = self.chosen_tool(k)
            found = tools.find(k, self.tool_settings())
            pick.config(state="disabled" if self.installing else "normal")
            if chosen:
                auto.pack(side="left", padx=(4, 0), after=pick)
            else:
                auto.pack_forget()
            if self.installing == k:
                self.tool_note(k, _("Installing… its installer's window may be behind this one."))
                btn.pack_forget()
            elif chosen:
                if chosen[1]:
                    self.tool_note(k, path=chosen[0], tip=_("Set by you: {path}", path=chosen[0]))
                else:
                    self.tool_note(k, _("Not found where you set it: {path}", path=chosen[0]))
                btn.pack_forget()
            elif found and k not in self.failed:
                self.tool_note(k, path=found)
                btn.pack_forget()
            else:
                missing.append(k)
                why = self.failed.get(k)
                broken = tools.found_broken(k) if k in ("ffmpeg", "tesseract") else None
                if why:
                    text = _("Not installed: {why}.", why=why[:1].upper() + why[1:])
                elif broken:
                    text = _("Found at {path}, but it won't run: its install may not have finished.", path=broken)
                else:
                    text = _("Not installed: {why}.", why=_(t["for"]))
                self.tool_note(k, text)
                btn.config(text=_("Install…") if winget else _("Download page"), state="disabled" if self.installing else "normal")
                btn.pack(side="left", before=pick)
        if winget and len(missing) >= 2 and not self.installing:
            self.all_btn.grid()
        else:
            self.all_btn.grid_remove()

    def get_tools(self, keys):
        """Install these programs, after asking: each one's publisher and licence named, and where its installer
        comes from. One at a time, each installer's own window shown. No winget: the first one's download page."""
        if not keys or self.installing:
            return
        if not tools.winget():
            for k in keys[:1]:
                messagebox.showinfo(APP, _("{name}: {how_to_get}", name=tools.BY_KEY[k]["name"], how_to_get=tools.how_to_get(k)), parent=self.top)
                webbrowser.open(tools.BY_KEY[k]["page"])
            return
        listed = "\n".join(_("• {name}, from {publisher} ({license})", name=tools.BY_KEY[k]["name"],
                               publisher=tools.BY_KEY[k]["publisher"], license=tools.BY_KEY[k]["license"]) for k in keys)
        if not messagebox.askyesno(APP, _("Install {programs}?\n\n{listed}\n\nEach installer comes from its publisher, through winget (Microsoft's package "
                "manager; where each one comes from: {WINGET_REPO}). Its own window opens, with its licence for you to agree to, "
                "and Windows may ask for permission.", programs=names_and(tools.BY_KEY[k]["name"] for k in keys) if len(keys) <= 2 else ui_n("{n} program", "{n} programs", len(keys)), listed=listed, WINGET_REPO=tools.WINGET_REPO), parent=self.top):
            return
        # (a tool whose files this app has loaded, its library, can't be replaced while it runs: its installer
        # would stop partway, "Error opening file for writing". Asked to restart the app first)
        held = [k for k in keys if any(tools.loaded_from(d) for d in tools.folders_of(k, self.tool_settings()))]
        if held:
            if messagebox.askyesno(APP, _("{APP} has {programs}'s files open, so the installer couldn't replace them. Restart {APP} "
                                        "and install first thing?", APP=APP, programs=names_and(tools.BY_KEY[k]["name"] for k in held)),
                                   parent=self.top):
                self.app.restart_to_install(keys)
            return
        self.queue = list(keys)
        self.next_install()

    def next_install(self):
        if not self.queue:
            self.installing = None
            self.refresh_tools()
            return
        k = self.installing = self.queue.pop(0)
        self.failed.pop(k, None)
        self.refresh_tools()

        def work():
            ok, last = tools.install(k, lambda line: di.logger.info("%s", line))
            self.app.post(self.installed, k, ok, last)
        self.app.run_bg(work)

    def installed(self, k, ok, last):
        """One program's installer done: found now, or why not, in its line; then the next."""
        try:
            alive = bool(self.top.winfo_exists())
        except tk.TclError:
            alive = False
        found = tools.find(k, self.app.settings)
        di.logger.info("%s %s", tools.BY_KEY[k]["name"], ("installed: %s" % found) if ok and found else ("not installed (%s)" % last))
        if not alive:                     # (the window closed meanwhile: what's left isn't installed)
            self.installing, self.queue = None, []
            return
        # (the installer's word first: one that stopped partway can leave a copy that's there but broken)
        if not ok:
            self.failed[k] = last
        elif not found:
            self.failed[k] = _("installed, but not found where it's usually put")
        self.next_install()

    def test_key(self):
        key = self.key.get().strip()
        if not key:
            self.key_note.config(text=_("Enter a key first."))
            return
        self.key_note.config(text=_("Testing…"))

        def work():
            try:
                di.tmdb_get(key, "/configuration")
                msg = _("Key works.")
            except Exception as exc:
                msg = _("That key didn't work: {error}", error=exc)
            self.app.post(lambda: self.top.winfo_exists() and self.key_note.config(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def test_gemini(self):
        key = self.gkey.get().strip()
        if not key:
            self.gkey_note.config(text=_("Enter a key first."))
            return
        self.gkey_note.config(text=_("Testing…"))

        def work():
            try:
                text, model = di.gemini_ask(key, "Reply with the single word OK.")
                msg = _("Key works ({model}).", model=model)
            except Exception as exc:
                msg = _("That key didn't work: {error}", error=exc)
            self.app.post(lambda: self.top.winfo_exists() and self.gkey_note.config(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def update(self, which):
        if self.app.busy:
            return
        self.save()
        self.prog.frame.grid(row=self.prog_row, column=0, columnspan=3, sticky="we", pady=(8, 0))
        self.prog.message(_("Starting…"))
        self.app.start_data_update([which], True, self)

    def progress(self, *a):
        if self.top.winfo_exists():
            self.prog.progress(*a)

    def message(self, text):
        if self.top.winfo_exists():
            self.prog.message(text)

    def finished(self, res, err):
        if not self.top.winfo_exists():
            return
        self.prog.stop()
        self.prog.bar.config(mode="determinate", value=0 if err else 1000)
        self.prog.text.config(text=(_("Update failed: {err}", err=err)) if err else _("Up to date."))
        self.refresh_status()

    def save(self):
        st = self.app.settings
        st["tmdb_key"] = self.key.get().strip()
        st["gemini_key"] = self.gkey.get().strip()
        for k, var in self.paths.items():
            st[k] = var.get().strip()
        tools.choose(st)
        st["menu_detect"] = bool(self.detect.get())
        st["bluray_menus"] = bool(self.bluray_menus.get())
        st["episode_order"] = self.order.get()
        st["detect_in_scan"] = self.in_scan.get()
        st["menu_every_title"] = bool(self.every_title.get())
        save_settings(st)

    def show_in_scan(self):
        """Detect titles during a scan: a choice only while either format has it on; how much it reads,
        a DVD's crawl only."""
        for b in self.in_scan_btns:
            b.config(state="normal" if self.detect.get() or self.bluray_menus.get() else "disabled")
        self.every_btn.config(state="normal" if self.detect.get() else "disabled")

    def close(self):
        self.save()
        if self.app.data_ui is self:     # keep the download going; report to the status line
            self.app.data_ui = None
        self.top.destroy()
        if self.app.rep and not self.app.busy:   # MakeMKV or Detect titles may have just been set: refresh Rip
            self.app._build_rip_menu()
            self.app.show_plan_summary()


def _n(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _log_uncaught(kind):
    def hook(*exc):
        di.logger.error(i18n.log("uncaught error ({kind})", kind=kind), exc_info=exc[-3:] if len(exc) >= 3 else None)
    return hook


def dpi_aware():
    """Windows: tell it the app draws at the screen's own resolution, so it stops stretching the
    window (which blurs every letter on a screen scaled past 100%). Before the first window.
    System-aware, not per-monitor: Tk 8.6 doesn't redraw for a monitor of another scale."""
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)          # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()           # Windows 7 / 8
        except Exception:
            pass


def text_readers():
    """The OCR this Python has, best first (menus and cover art are read best by RapidOCR, which
    needs Python 3.14 or older: a session on 3.15 reads them worse, and the log's first line says so)."""
    import importlib.util
    found = lambda m: importlib.util.find_spec(m) is not None
    return [name for name, ok in (("RapidOCR", found("rapidocr") and found("onnxruntime")),
                                  ("Windows OCR", os.name == "nt" and found("winrt")),
                                  ("Tesseract", bool(tools.which("tesseract")))) if ok]


def main():
    tools.keep_off_path()                # (first: before anything loads a library through PATH)
    try:
        di.setup_logging()
    except OSError:
        pass
    di.logger.info(i18n.log("Disc Detective {version} started (Python {python}; text read by {readers})", version=di.VERSION, python=sys.version.split()[0], readers=", ".join(text_readers()) or i18n.log("nothing: no OCR installed")))
    sys.excepthook = _log_uncaught("main")
    threading.excepthook = lambda a: di.logger.error(i18n.log("uncaught error in a background thread"),
                                                     exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    dpi_aware()
    root = tk.Tk()
    # unseen until it's built at its proper size: tinting the title bar needs the window to exist,
    # and it would otherwise show at Tk's small default size, then jump to the real one
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass

    def tk_error(exc, val, tb):               # errors in button handlers etc.
        di.logger.error(i18n.log("error in the app"), exc_info=(exc, val, tb))
        messagebox.showerror(APP, _("{error}: {detail}\n\nDetails are in errors.log (Open logs folder, at the bottom of the window).", error=exc.__name__, detail=val))
    root.report_callback_exception = tk_error
    App(root)
    root.update_idletasks()
    try:
        root.attributes("-alpha", 1.0)
    except tk.TclError:
        pass
    root.mainloop()
    di.logger.info(i18n.log("Disc Detective closed"))


if __name__ == "__main__":
    main()
