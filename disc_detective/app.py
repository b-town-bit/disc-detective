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
import csv
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
from . import theme as th

APP = "Disc Detective"
SETTINGS = os.path.join(di.data_home(), "settings.json")
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
RIP_ROLES = ("feature", "episode")
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
        for dirpath, dirs, _ in os.walk(base) if os.path.isdir(base) else []:
            if any(d.upper() in ("VIDEO_TS", "BDMV") for d in dirs):
                roots.append(dirpath)
                dirs[:] = []
            elif dirpath.count(os.sep) - base.count(os.sep) >= 2:
                dirs[:] = []
    return sorted(set(roots))


def drive_models():
    """{"D:": "HL-DT-ST BD-RE WH16NS40", ...}: each optical drive's make and model."""
    out = {}
    try:
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_CDROMDrive | Select-Object Drive, Caption | ConvertTo-Json"],
                               capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
            rows = json.loads(r.stdout or "[]")
            for row in rows if isinstance(rows, list) else [rows]:
                if row.get("Drive") and row.get("Caption"):
                    out[row["Drive"].rstrip("\\/").upper()] = " ".join(row["Caption"].split())
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

def makemkv_source(exe, drive):
    """Find MakeMKV's disc index for a drive letter / mount via its drive list."""
    out = subprocess.run([exe, "-r", "--cache=1", "info", "disc:9999"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW).stdout
    want = drive.rstrip("\\/").upper()
    for line in out.splitlines():
        if line.startswith("DRV:"):
            f = next(csv.reader([line[4:]]))
            if len(f) >= 7 and f[6] and f[6].rstrip("\\/").upper() in (want, want.split(os.sep)[-1]):
                return "disc:%s" % f[0]
    return "disc:0"


EXTRA_FOLDERS = {"Featurette": "Featurettes", "DeletedScene": "Deleted Scenes", "Trailer": "Trailers",
                 "Interview": "Interviews", "BehindTheScenes": "Behind The Scenes", "Short": "Shorts",
                 "Scene": "Scenes"}


def clean(s):
    return re.sub(r'[<>"/\\|?*]', "", re.sub(r"\s*:\s*", " - ", s or "")).strip()


def plan_rip(rep, mk_titles, include_extras, selected=None):
    """Which MakeMKV titles to rip and what to call them (Plex layout)."""
    ours = [{"title_id": t["id"], "role": t["role"], "duration": t["duration"],
             "clips": t.get("clip_list"), "episode_index": t.get("episode_index"), "edition": t.get("edition"),
             "interactive": t.get("interactive"), "special_name": t.get("special_name")}
            for t in rep.get("titles") or []]
    pairs = di.map_makemkv(mk_titles, ours, rep["format"])
    names = {}
    for n in rep.get("plex_names") or []:
        src, _, name = n.partition(" -> ")
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
    roles = RIP_ROLES + (("extra",) if include_extras or rep.get("extras_disc") else ())
    mine = rep.get("labels") or {}           # your labels (Label titles): they win over TheDiscDB's
    plan = []
    for m in mk_titles:
        t, _ = pairs.get(m["index"], (None, None))
        item = known.get((m["source"] or "").lower()) or {}
        if not item and t and t.get("clips") and "," not in t["clips"]:
            item = known.get(t["clips"].lower() + ".m2ts") or {}      # playlist of one clip
        if not item and m.get("dvd_title"):                         # TheDiscDB numbers DVD titles "01"
            item = known.get("%02d" % m["dvd_title"]) or known.get(str(m["dvd_title"])) or {}
        role = t["role"] if t else {"MainMovie": "feature", "Episode": "episode"}.get(item.get("type"), "extra" if item else None)
        my = mine.get(t["title_id"]) if t else None
        if my and (my.get("type") or my.get("title")):
            item = dict(item, **{k: v for k, v in my.items() if v})
            if my.get("type"):
                role = di.LABEL_ROLES.get(my["type"], "extra")
        if selected is not None:
            if not t or t["title_id"] not in selected:
                continue
        elif role not in roles or (t and t.get("interactive")):    # a menu game: only if ticked
            continue
        if role == "episode" and item.get("season") and item.get("episode"):
            dest = "%s/Season %02d/%s - s%02de%02d - %s.mkv" % (base, int(item["season"]), show, int(item["season"]),
                                                               int(item["episode"]), clean(item.get("title")))
        elif t and t["title_id"] in names and not (my and role == "extra"):
            dest = names[t["title_id"]]
        elif role == "extra":
            folder = EXTRA_FOLDERS.get(item.get("type"), "Other")
            label = clean(item.get("title")) or clean((t or {}).get("special_name")) or \
                "%s - %s" % (show, (t["title_id"] if t else m["source"]).replace(".", "_"))
            dest = "%s/%s/%s.mkv" % (base, folder, label)
        elif role == "episode":
            dest = "%s/Episode %02d.mkv" % (base, (t or {}).get("episode_index") or m["index"])
        elif role in ("feature", "longest"):
            dest = "%s/%s.mkv" % (base, show)
        elif role == "version" and t and t.get("edition") and "{imdb-" in base:
            # a ticked widescreen / full-screen / 4K copy: another Plex edition of the same film
            dest = "%s/%s {edition-%s}.mkv" % (base, show, clean(t["edition"]))
        else:   # hand-picked play-alls, alternate versions, duplicates
            dest = "%s/Other/%s - %s.mkv" % (base, show, (t["title_id"] if t else m["source"]).replace(".", "_"))
        plan.append({"mk": m["index"], "title_id": t["title_id"] if t else m["source"], "role": role,
                     "length": m["duration_text"], "dest": dest})
    if not plan and mk_titles and selected is None:   # nothing classified: the longest title (never a loop)
        real = [x for x in mk_titles if (pairs.get(x["index"], (None, None))[0] or {}).get("role")
                not in ("loop", "junk", "duplicate", "decoy", "play-all", "segment")] or mk_titles
        m = max(real, key=lambda x: x["duration"] or 0)
        plan.append({"mk": m["index"], "title_id": m["source"] or "title %d" % m["index"], "role": "longest",
                     "length": m["duration_text"], "dest": "%s/%s.mkv" % (base, show)})
    return plan


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
    if os.path.exists(path) and item.get("role") in ("feature", "version", "longest") and "{edition-" not in path:
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
    def add_command(self, label="", command=None, state="normal", image=None, compound=None, **_):
        self.entries.append({"type": "command", "label": label, "command": command, "state": state, "image": image or ""})

    def add_radiobutton(self, label="", value=None, variable=None, command=None, state="normal", **_):
        self.entries.append({"type": "radiobutton", "label": label, "value": value, "variable": variable,
                             "command": command, "state": state, "image": ""})

    def add_checkbutton(self, label="", variable=None, command=None, state="normal", **_):
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
    def tk_popup(self, x, y, *_):
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
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        x = min(x, sw - w - 4)
        if y + h > sh - 40:                 # no room below: open upwards
            y = max(0, y - h - 30)
        top.geometry("+%d+%d" % (max(0, x), y))
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


def centre_over(top, parent):
    """Put a dialog in the middle of the main window, kept on the screen."""
    top._placed = True
    top.update_idletasks()
    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
    sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    top.geometry("+%d+%d" % (max(0, min(x, sw - w - 8)), max(0, min(y, sh - h - 60))))


class NotesWindow:
    """Scan ▾ > Case notes: the technical log of this session, live, beside the main window."""

    def __init__(self, parent, app):
        self.app = app
        top = self.top = new_dialog(parent)
        top.title("%s · case notes" % APP)
        # over the main window (a window beside it can land off the screen)
        parent.update_idletasks()
        w = max(480, min(760, parent.winfo_width() - 40))
        h = max(320, min(520, parent.winfo_height() - 60))
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        top.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))
        top._placed = True
        f = ttk.Frame(top, padding=8)
        f.pack(fill="both", expand=True)
        self.text = tk.Text(f, wrap="none", font=("Consolas" if os.name == "nt" else "Courier", 9),
                            height=20, width=100, relief="flat", borderwidth=0)
        ys = ttk.Scrollbar(f, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(f, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Follow new lines", variable=self.follow).pack(side="left")
        ttk.Button(bar, text="Close", command=top.destroy).pack(side="right")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Copy all", command=self.copy).pack(side="right", padx=(0, 6))
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
    SCAN_TEXT = "  Scan Disc"
    RIP_TEXT = " Rip"

    MIN_STATUS_MS = 700       # each step of the story stays up at least this long

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self._story = collections.deque()   # status lines (and the result) waiting their turn
        self._story_job = None
        self._notes = collections.deque(maxlen=5000)   # the technical log, for the case notes window
        self.notes_win = None
        self.settings = load_settings()
        self.rep = None
        self.selected = None          # None = default (feature / episodes [+ extras])
        self.proc = None
        self.cancelled = False
        self.busy = False
        self.ripping = False
        self.last_out = None
        root.title(APP)
        try:
            self._icon = tk.PhotoImage(file=ICON)
            root.iconphoto(True, self._icon)
        except (tk.TclError, OSError):
            pass
        # as big as the design wants, but never taller or wider than the screen (with room for the
        # taskbar and title bar): a window taller than the screen hides the footer
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1000, sw - 40), min(780, sh - 110)
        root.minsize(min(760, w), min(520, h))
        root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 2 - 30)))
        self._style()
        self.source = tk.StringVar(value=self.settings.get("drive", ""))
        self.outdir = tk.StringVar(value=self.settings.get("outdir", os.path.join(os.path.expanduser("~"), "Videos", "Rips")))
        self.extras = tk.BooleanVar(value=self.settings.get("extras", False))
        self._drive_models = {}
        self._build()
        self.refresh_drives(initial=True)
        self.find_drive_models()
        di.status_hook = lambda msg: self.post(self.tell, msg)   # identifier steps -> status line, in turn
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
        try:
            self._scan_icon = tk.PhotoImage(file=ICON).subsample(3)
        except (tk.TclError, OSError):
            self._scan_icon = None
        self.scan_btn = ttk.Button(inner, text=self.SCAN_TEXT, style="Scan.TButton", command=self.scan,
                                   **({"image": self._scan_icon, "compound": "left"} if self._scan_icon else {}))
        self.scan_btn.pack(side="left")
        # the source: a drop-down of the disc drives (and a folder), with each drive's make and model
        self.drive_btn = ttk.Button(inner, text="", style="Pill.TButton", command=self.drive_popup, compound="center")
        self.drive_btn.pack(side="right")
        self.drive_menu = ThemedMenu(self.root)
        ttk.Label(inner, text="SOURCE", style="BarCap.TLabel").pack(side="right", padx=(0, 8))
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
        self.standing_lbl = ttk.Label(fin, text="No case open", style="FootStatus.TLabel")
        self.standing_lbl.pack(side="left")
        for k, (text, cmd, icon) in enumerate((("About / credits", self.about, "about"),
                                               ("Open logs folder", self.open_logs, "logs"),
                                               ("Case notes (technical log)", self.open_notes, "notes"),
                                               ("Settings", self.open_settings, "settings"))):
            if k:
                ttk.Label(links, text="|", style="FootSep.TLabel").pack(side="left", padx=8)
            th.link(links, text, cmd, style="FootLink.TLabel", icon=icon).pack(side="left")
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
        self.title_lbl = ttk.Label(name_row, text="Insert a disc and press Scan Disc", style="Name.TLabel",
                                   wraplength=520, justify="left")
        self.title_lbl.pack(side="left", anchor="sw")
        self.year_lbl = ttk.Label(name_row, text="", style="Year.TLabel")
        # the year's figures centred on the title's letters (on its last line, when it wraps)
        tf = tkfont.Font(font=ttk.Style().lookup("Name.TLabel", "font"))
        yf = tkfont.Font(font=ttk.Style().lookup("Year.TLabel", "font"))
        lift = round((tf.metrics("descent") + th.cap_height(tf) / 2) - (yf.metrics("descent") + th.cap_height(yf) / 2))
        self.year_lbl.pack(side="left", anchor="sw", padx=(10, 0), pady=(0, max(0, lift)))
        if lift < 0:
            self.title_lbl.pack_configure(pady=(0, -lift))
        facts = ttk.Frame(info, style="Desk.TFrame")
        facts.pack(anchor="w", pady=(2, 10))
        self.kind_lbl = ttk.Label(facts, text="", style="Kind.TLabel")
        self.kind_lbl.pack(side="left")
        self._fact_dots = [ttk.Label(facts, text="\u2022", style="DeskDot.TLabel") for _ in range(2)]
        self.facts_lbl = ttk.Label(facts, text="", style="Fact.TLabel")
        self.source_lbl = ttk.Label(facts, text="", style="Source.TLabel")
        self._facts_row = facts
        # the step of the investigation and its progress bar, just under the title
        # (shown only while there's something to say)
        self.status_strip = strip = th.frame(info, th.VELLUM_200, border=th.BRASS_300, radius=5, outer=th.PARCHMENT)
        sin = ttk.Frame(strip, style="Bar.TFrame", padding=(10, 3))
        sin.pack(fill="x")
        self.status_dot = th.dot(sin, th.VELLUM_200, th.BRASS_400)
        self.status_dot.pack(side="left", padx=(0, 8))
        self.progress = ttk.Progressbar(sin, mode="determinate", maximum=1000, length=220)
        self.status = ttk.Label(sin, text="", style="BarStatus.TLabel")
        self.status.pack(side="left", fill="x", expand=True)
        self.set_facts("", "Scan Disc reads the disc and opens a case file on it", "")

        # the details card: what the disc says, and whether that's the answer
        self.details_card = th.frame(info, th.CARD, border=th.BRASS_400, radius=7, shadow=3)
        self.details = ttk.Frame(self.details_card, style="Card.TFrame", padding=(16, 8, 16, 4))
        self.details.pack(fill="x")
        th.rule(self.details_card, th.BRASS_200).pack(fill="x", padx=16)
        self.match_row = ttk.Frame(self.details_card, style="Card.TFrame", padding=(16, 6, 16, 6))
        self.match_row.pack(fill="x")
        self.match_note = ttk.Label(self.match_row, text="", style="Warn.TLabel")
        tick = th.check(th.INK)
        self.confirm_btn = ttk.Button(self.match_row, text="That's right", style="Small.TButton", command=self.confirm,
                                      image=(tick, "disabled", tick), compound="left")
        self.confirmed_lbl = ttk.Label(self.match_row, text="In your library", style="Ok.TLabel",
                                       image=th.check(), compound="left")
        self.match_btn = ttk.Button(self.match_row, text="Not right?", style="Small.TButton", command=self.match_menu,
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
        # what each ticked title is, when the titles are films of their own (a disc of short films)
        # or a bonus disc's features: poster and name, for the titles being ripped only
        self.films_box = ttk.Frame(rc, style="Card.TFrame")
        self.strip = ttk.Frame(self.films_box, style="Card.TFrame")
        self.strip.pack(anchor="w")
        self._film_imgs = []
        lab = self._path_row = ttk.Frame(rc, style="Card.TFrame")
        lab.pack(fill="x", pady=(8, 2))
        ttk.Label(lab, text="TARGET PATH", style="CardCap.TLabel").pack(side="left")
        th.link(lab, "Change folder", self.pick_out, style="CardLink.TLabel").pack(side="right")
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
        ttk.Label(a, text="AUDIO", style="InsetCap.TLabel").pack(anchor="w")
        self.audio_lbl = ttk.Label(a, text="", style="InsetVal.TLabel", wraplength=190, justify="left")
        self.audio_lbl.pack(anchor="w", pady=(2, 0))
        a.bind("<Configure>", lambda ev: self.audio_lbl.config(wraplength=max(120, ev.width - 20)))
        e = th.frame(minis, th.VELLUM_100, border=th.BRASS_300, pad=5, radius=5, outer=th.CARD)
        e.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        etop = ttk.Frame(e, style="Inset.TFrame")
        etop.pack(fill="x")
        ttk.Label(etop, text="EXTRACTION ENGINE", style="InsetCap.TLabel").pack(side="left")
        self.engine_lbl = ttk.Label(e, text="", style="InsetOk.TLabel")
        self.engine_lbl.pack(anchor="w", pady=(2, 0))
        th.rule(rc, th.BRASS_200).pack(fill="x", pady=(9, 7))
        bottom = ttk.Frame(rc, style="Card.TFrame")
        bottom.pack(fill="x")
        self.rip_note = ttk.Label(bottom, text="", style="CardSub.TLabel")
        self.rip_note.pack(side="left")
        self.rip_btn = SplitButton(bottom, self.RIP_TEXT, self.rip_or_cancel, "Rip.TButton", "RipArrow.TButton",
                                   frame_style="Card.TFrame", image=th.button_image("rip"))
        self.rip_btn.pack(side="right")
        self.rip_btn.set_state(False)
        self._build_rip_menu()
        self.root.bind("<Configure>", self._rewrap)

    def _rewrap(self, event=None):
        if event is None or event.widget is self.root:
            width = event.width if event else self.root.winfo_width()
            w = max(300, width - 100 - (self.pic_col.winfo_reqwidth() + 26 if self.pic_col.winfo_ismapped() else 0))
            self.title_lbl.config(wraplength=max(240, w - 90))
            self.plan_lbl.config(wraplength=max(200, w - 190))

    def set_dest(self, path):
        self.dest_full = path
        self._fit_dest()

    def _fit_dest(self):
        """One line; a path too long for the box ends in an ellipsis."""
        text = self.dest_full
        width = self.dest_lbl.winfo_width() - 4
        if width > 20:
            measure = tkfont.Font(root=self.root, font=ttk.Style().lookup("Path.TLabel", "font") or "TkDefaultFont").measure
            if measure(text) > width:
                lo, hi = 0, len(text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if measure(text[:mid].rstrip() + "…") <= width:
                        lo = mid
                    else:
                        hi = mid - 1
                text = text[:lo].rstrip() + "…"
        if str(self.dest_lbl.cget("text")) != text:
            self.dest_lbl.config(text=text)

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
        self._build_scan_menu(optical_drives())     # fresh: a disc may have gone in since
        b = self.drive_btn
        try:
            self.drive_menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            self.drive_menu.grab_release()

    def _build_scan_menu(self, drives):
        m = self.drive_menu
        m.delete(0, "end")
        for d in drives:
            label = d
            try:
                vol = di.volume_label(d)
                if vol:
                    label = "%s   %s" % (d, vol)
            except Exception:
                pass
            m.add_radiobutton(label=label, value=d, variable=self.source, command=self.source_changed)
        if not drives:
            m.add_command(label="No disc drives found", state="disabled")
        cur = self.source.get()
        if cur and cur not in drives:
            m.add_radiobutton(label="Folder: %s" % short_path(cur, 60), value=cur, variable=self.source,
                              command=self.source_changed)
        m.add_separator()
        m.add_command(label="Choose folder…", command=self.pick_source)
        m.add_command(label="Refresh drives", command=self.refresh_drives)

    def about(self):
        if getattr(self, "about_win", None) is not None and self.about_win.top.winfo_exists():
            self.about_win.top.lift()
            return
        self.about_win = AboutDialog(self.root)

    def _build_rip_menu(self):
        m = self.rip_btn.menu
        m.delete(0, "end")
        m.add_command(label="Rip to: %s…" % short_path(self.outdir.get()), command=self.pick_out)
        if not (self.rep or {}).get("extras_disc"):     # a bonus disc rips its extras already
            m.add_checkbutton(label="Include extras", variable=self.extras, command=self.options_changed)
        m.add_command(label="Choose titles…", command=self.choose_titles,
                      state="normal" if self.rep else "disabled")
        m.add_command(label="Label titles…", command=self.label_titles,
                      state="normal" if self.rep else "disabled")
        if self.selected is not None:
            m.add_command(label="Back to default titles", command=self.reset_titles)
        m.add_separator()
        m.add_command(label="Open rip folder", command=self.open_out)

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
        text, colour = {"none": ("No case open", th.INK_SUBTLE), "open": ("Open case", th.WAX),
                        "closed": ("Case closed", th.GREEN_DOT)}[state]
        self.standing_lbl.config(text=text)
        th.set_dot(self.case_dot, colour)

    def set_status(self, text):
        """A passing message (a step of the investigation, what to do next, a rip's progress) on
        the strip under the title; the strip folds away when there's nothing to say."""
        self.status.config(text=text)
        showing = bool(self.status_strip.winfo_manager())
        if (text or self.progress.winfo_manager()) and not showing:
            self.status_strip.pack(fill="x", pady=(4, 4), before=self._facts_row)
        elif not text and not self.progress.winfo_manager() and showing:
            self.status_strip.pack_forget()
        t = text.lower()
        colour = th.GREEN_DOT if t.startswith(("case closed", "confirmed", "done", "disc data updated")) else \
            th.WAX if ("fail" in t or "couldn't" in t) else \
            th.AMBER_DOT if t.startswith(("best guess", "still a mystery", "cancel")) else th.BRASS_400
        th.set_dot(self.status_dot, colour)

    def tell(self, item):
        """Queue a status line (or a callable, run when its turn comes: the scan's result)."""
        self._story.append(item)
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
        self.set_status(item)
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
            self.progress.pack(side="right", padx=(12, 0), before=self.status)
            self.set_status(self.status.cget("text"))      # make sure the strip is showing
            self.progress.config(mode="indeterminate" if indeterminate else "determinate", maximum=1000, value=0)
            if indeterminate:
                self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.set_status(self.status.cget("text"))      # folds away if nothing's left to say

    def set_busy(self, busy, ripping=False):
        self.busy, self.ripping = busy, ripping
        self.scan_btn.config(state="disabled" if busy else "normal")
        self.drive_btn.config(state="disabled" if busy else "normal")
        if ripping:
            self.rip_btn.set_text("Cancel")
            self.rip_btn.set_state(True, arrow_enabled=False)
        else:
            self.rip_btn.set_text(self.RIP_TEXT)
            # no MakeMKV: Rip is greyed out, but ▾ still opens Choose titles and the rip options
            self.rip_btn.set_state(bool(self.rep) and not busy and bool(self.makemkv_exe()),
                                   arrow_enabled=bool(self.rep) and not busy)

    def remember(self):
        self.settings.update(drive=self.source.get(), outdir=self.outdir.get(), extras=self.extras.get())
        save_settings(self.settings)

    def source_name(self):
        src = self.source.get()
        return src if src else "no drive selected"

    # ---- source selection (Scan menu) ----
    def refresh_drives(self, initial=False):
        drives = optical_drives()
        cur = self.source.get()
        if drives and (not cur or (cur not in drives and not os.path.isdir(cur))):
            self.source.set(drives[0])
        self._build_scan_menu(drives)
        self.show_drive()
        if initial or not self.rep:
            self.set_status("" if self.source.get() else "No drive selected: pick one from Source")

    def pick_source(self):
        d = filedialog.askdirectory(title="Choose a disc or a full-disc backup folder")
        if d:
            self.source.set(os.path.normpath(d))
            self.refresh_drives()
            self.source_changed()

    def source_changed(self):
        self.remember()
        self.rep = None
        self.selected = None
        self.set_busy(False)
        self._build_rip_menu()
        self.clear_case("Ready to scan")
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
        text = "%s%s" % (where or "no drive", ("  " + model) if model else "")
        font = tkfont.Font(font=ttk.Style().lookup("Pill.TButton", "font") or "TkDefaultFont")
        side = round(22 * max(1.0, float(self.root.tk.call("tk", "scaling")) / 1.3333))   # room for the dot / chevron
        img = th.caret(th.BRASS_700, width=font.measure(text) + 2 * side, height=font.metrics("linespace"),
                       dot=th.AMBER_DOT if src and os.path.isdir(src) else th.INK_SUBTLE)
        self.drive_btn.config(text=text, image=(img, "disabled", img))

    def find_drive_models(self):
        """Drive makes and models, looked up once in the background (Windows: from the system)."""
        def work():
            found = drive_models()
            self.post(lambda: (self._drive_models.update(found), self.show_drive()))
        threading.Thread(target=work, daemon=True).start()

    # ---- settings ----
    def scan_args(self):
        """Identifier settings from the Settings dialog (a plain dict: safe to read off the Tk thread)."""
        st = self.settings
        return di.default_args(tmdb_key=st.get("tmdb_key") or None, gemini_key=st.get("gemini_key") or None,
                               no_art=not st.get("pictures", True),
                               no_discdb_online=not st.get("discdb_online", True),
                               episode_order=st.get("episode_order", "aired"))

    def makemkv_exe(self):
        exe = self.settings.get("makemkv")
        return exe if exe and os.path.isfile(exe) else di.find_makemkvcon()

    def open_logs(self):
        path = di.log_dir()
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def open_settings(self):
        SettingsDialog(self.root, self)

    # ---- disc data downloads (first run, Settings > Update now) ----
    def start_data_update(self, which, force, ui):
        """Run di.update_data in the background; `ui` gets progress()/message()/finished() on the Tk thread."""
        self.data_ui = ui
        self.set_busy(True)
        args = self.scan_args()

        def work():
            try:
                res = di.update_data(args, which, force=force,
                                     log=lambda m: (di.logger.info(m), self.post(ui.message, m)))
                di.logger.info("disc data updated: %s", res)
                self.post(self._data_done, ui, res, None)
            except Exception as exc:
                di.logger.exception("disc data update failed")
                self.post(self._data_done, ui, None, "%s: %s" % (type(exc).__name__, exc))
        threading.Thread(target=work, daemon=True).start()

    def _data_done(self, ui, res, err):
        self.data_ui = None
        self.set_busy(False)
        self.show_progress(False)
        ui.finished(res, err)
        self.set_status("Disc data updated" if not err else "Disc data update failed: %s" % err)

    def _on_progress(self, label, done, total):
        ui = getattr(self, "data_ui", None)
        if ui is not None:
            ui.progress(label, done, total)
        elif total:                            # a scan fetching data on its own
            self.show_progress(True)
            self.progress.config(value=1000 * done / total)
            self.set_status("Downloading %s: %d of %d MB" % (label, done // 2**20, total // 2**20))

    def maybe_first_run(self):
        args = self.scan_args()
        missing = [k for k, info in (("discdb", di.discdb_index_info(args.discdb_dir or di.default_discdb_dir())),
                                     ("imdb", di.imdb_index_info(args.imdb_dir))) if not info]
        if missing and not self.settings.get("skip_first_run"):
            FirstRunDialog(self.root, self, missing)

    # ---- picture ----
    def _photo(self, path):
        try:
            img = tk.PhotoImage(file=path)
        except (tk.TclError, TypeError):
            return None
        self._imgs.append(img)
        return img

    # ---- choosing the match ----
    DETAIL_NAMES = {"IMDb": "IMDb Reference", "TheDiscDB": "TheDiscDB Entry", "Disc label": "Disc Volume Label",
                    "Disc title": "Disc Title", "Mastered": "Mastering Date", "Fingerprint": "Disc Fingerprint",
                    "Box set": "Box Set"}

    def show_details(self, rep):
        for w in self.details.winfo_children():
            w.destroy()
        if not rep:
            self.details_card.pack_forget()
            return
        self.details.columnconfigure(1, weight=1)
        for r, (k, v) in enumerate(detail_rows(rep)):
            key = ttk.Label(self.details, text=self.DETAIL_NAMES.get(k, k), style="CardKey.TLabel")
            if k == "IMDb":
                cell = th.link(self.details, v, lambda v=v: webbrowser.open("https://www.imdb.com/title/%s/" % v),
                               style="CardLink.TLabel", icon="external", right=True)
                text, lift = cell, 0
            elif k == "Fingerprint":
                cell = ttk.Frame(self.details, style="Card.TFrame")
                tag = th.frame(cell, th.VELLUM_200, border=th.BRASS_300, radius=4, outer=th.CARD)
                tag.pack(side="left")
                text = ttk.Label(tag, text=v, style="Badge.TLabel")
                text.pack()
                full = rep.get("content_hash", v)
                th.link(cell, "", lambda full=full: (self.root.clipboard_clear(), self.root.clipboard_append(full),
                                                     self.set_status("Fingerprint copied: %s" % full)),
                        style="CardLink.TLabel", icon="copy").pack(side="left", padx=(8, 0))
                tag.update_idletasks()
                lift = (tag.winfo_reqheight() - text.winfo_reqheight()) // 2   # the tag's border and padding
            else:
                cell = ttk.Label(self.details, text=v, style="CardMonoBold.TLabel" if k == "Disc label" else "CardMono.TLabel",
                                 wraplength=420, justify="left")
                text, lift = cell, 0
            # sit the label's text on the same baseline as the value's text
            gap = self._baseline(cell, text, lift) - self._baseline(key, key)
            key.grid(row=r, column=0, sticky="nw", padx=(0, 22), pady=(3 + max(0, gap), 3))
            cell.grid(row=r, column=1, sticky="nw", pady=(3 + max(0, -gap), 3))
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
            self.confirmed_lbl.config(text="In your library")
            self.confirmed_lbl.pack(side="left")
        elif (rep.get("discdb") or {}).get("hit"):
            # TheDiscDB knows this exact disc: nothing to confirm (Not right? still fixes a bad entry)
            self.confirmed_lbl.config(text="TheDiscDB")
            self.confirmed_lbl.pack(side="left")
        elif self.confirmable(rep):
            sure = (rep.get("match") or {}).get("sure")
            self.match_note.config(text="Probable match" if sure else "Best guess: check it",
                                   style="Ok.TLabel" if sure else "Warn.TLabel")
            self.match_note.pack(side="left", padx=(0, 10))
            self.confirm_btn.pack(side="left")
        else:
            self.match_note.config(text="Not identified yet", style="Warn.TLabel")
            self.match_note.pack(side="left")
        self.match_btn.config(text="Not right?" if have else "Choose the match")
        self.match_btn.pack(side="right")
        if not self.details_card.winfo_manager():
            self.details_card.pack(fill="x", anchor="w", **self._before_rip())

    def _before_rip(self):
        return {"before": self.rip_card} if self.rip_card.winfo_manager() else {}

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
            truth = di.truth_for_collection(cand["name"], [(i["title_id"], i["imdb_id"]) for i in cand["items"]])
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
            di.logger.exception("saving the answer failed")
            messagebox.showerror(APP, "Couldn't save the answer: %s" % exc)
            return
        # The answer is the one on screen: nothing to investigate again, just mark it as yours
        rep["personal_hit"] = {"truth": truth, "notes": "", "saved": None}
        rep["status"] = di.status(rep, args.tmdb_key)
        rep["best_guess"], rep["identified_by"] = di.best_guess(rep)
        self.scan_done(rep, None)
        self.set_case("closed")
        self.set_status("Confirmed: saved to your case files")

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
            m.add_command(label="Your answer: %s" % di.strip_tags(rep["personal_hit"]["truth"]), state="disabled")
            m.add_command(label="Forget my answer and investigate again", command=lambda: self.use_answer(None))
            m.add_separator()
        if rep.get("kind") == "collection" and top and top.get("kind") == "collection":
            m.add_command(label="Change the films…", command=lambda: CollectionDialog(self.root, self))
            m.add_command(label="It's one film or TV show instead…", command=lambda: MatchDialog(self.root, self))
            m.add_separator()
        others = [c for c in cands if c.get("kind") != "collection" and (c is not top or not sure)]
        if others:
            m.add_command(label="Other matches:" if sure else "Possible matches:", state="disabled")
            kinds = {"movie": "film", "tv": "TV series"}
            for c in others[:6]:
                img = self._menu_image(c)
                m.add_command(label="  %s (%s) · %s" % (c["name"], c.get("year") or "?", kinds.get(c["kind"], c["kind"])),
                              command=lambda c=c: self.choose(c), **({"image": img, "compound": "left"} if img else {}))
            m.add_separator()
        if rep.get("disc_images"):
            m.add_command(label="Ask an AI about the disc's picture…", command=lambda: AskAIDialog(self.root, self))
        m.add_command(label="Enter it myself…", command=lambda: MatchDialog(
            self.root, self, title=(top or {}).get("name") or (rep.get("best_guess") or "").split(" | ")[0],
            year=(top or {}).get("year"), tv=rep.get("kind") == "tv",
            season=lp.get("season"), disc=lp.get("disc")))
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
            self.use_answer(di.truth_for_collection(cand["name"], [(i["title_id"], i["imdb_id"]) for i in cand["items"]]))
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
            di.logger.exception("saving the answer failed")
            messagebox.showerror(APP, "Couldn't save the answer: %s" % exc)
            return
        self.scan()

    def picture_menu(self, event=None):
        m = self.pic_menu
        m.delete(0, "end")
        if self.pic_path:
            m.add_command(label="Copy picture", command=lambda: copy_image(self.pic_path) or
                          self.set_status("Couldn't copy the picture here: use Save picture…"))
            m.add_command(label="Save picture…", command=self.save_picture)
        if (self.rep or {}).get("disc_images"):
            m.add_separator()
            m.add_command(label="Ask an AI about the disc's picture…", command=lambda: AskAIDialog(self.root, self))
        if m.index("end") is None:
            return
        try:
            m.tk_popup(event.x_root, event.y_root) if event else m.tk_popup(*self.root.winfo_pointerxy())
        finally:
            m.grab_release()

    def save_picture(self):
        dest = filedialog.asksaveasfilename(title="Save the picture", defaultextension=".png",
                                            initialfile="picture.png", filetypes=[("PNG image", "*.png")])
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
            self.pic_cap.config(text=picture_source(rep, art), wraplength=img.width() + 10, justify="center")
            self.pic_col.pack(side="left", anchor="n", padx=(0, 26), before=self.info)
        elif rep.get("art_note"):            # tell the user why there's no picture
            self.pic_lbl.config(text=rep["art_note"], image="", wraplength=170, justify="left", padding=10)
            self.pic_cap.config(text="NO PICTURE ON FILE")
            self.pic_col.pack(side="left", anchor="n", padx=(0, 26), before=self.info)
        self._rewrap()

    # ---- rip options (Rip menu) ----
    def pick_out(self):
        d = filedialog.askdirectory(title="Rip to", initialdir=self.outdir.get())
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
        if not self.rep:
            return
        TitlePicker(self.root, self)

    def label_titles(self):
        if self.rep:
            LabelDialog(self.root, self, self.rep["content_hash"], display_title(self.rep))

    def labels_changed(self, h):
        """Labels saved for a disc: if it's the one on the desk, its rip plan follows them now."""
        if self.rep and self.rep.get("content_hash") == h:
            con = di.library_open(self.scan_args().db)
            try:
                self.rep["labels"] = di.get_labels(con, h)
            finally:
                con.close()
            di.apply_labels(self.rep.get("titles") or [], self.rep["labels"])
            di.labels_over_guess(self.rep)
            self.show_details(self.rep)
            self.selected = None
            self._build_rip_menu()
            self.show_plan_summary()

    # ---- Scan ----
    def scan(self):
        drive = self.source.get().strip()
        if not drive or not os.path.isdir(drive):
            messagebox.showwarning(APP, "No disc found at %s.\n\nInsert a disc, or pick another drive or a "
                                        "folder from the Source menu (top right)." % self.source_name())
            return
        self.remember()
        self.rep = None
        self.selected = None
        self._build_rip_menu()
        self.clear_case("Investigating…")
        self.set_case("open")
        self.set_busy(True)
        self.show_progress(True, indeterminate=True)
        self.story_reset()
        if self.notes_win is None or not self.notes_win.top.winfo_exists():
            self._notes.clear()      # case notes closed: opening them later shows just this scan
        di.logger.info("%s %s ----", SCAN_DIVIDER, drive)
        self.tell("Arriving at the scene…")
        self.run_bg(self._scan_worker, drive)

    def _scan_worker(self, drive):
        try:
            import tempfile
            args = self.scan_args()
            args.library = di.library_open(args.db)
            with tempfile.TemporaryDirectory(prefix="disc_detective_") as tmp:
                args.tmp_out = tmp
                rep = di.identify(drive, args)
            args.library.close()
            self.post(self.tell, lambda: self.scan_done(rep, None))   # once the story has been told
        except SystemExit as exc:
            di.logger.warning("scan of %s: %s", drive, exc)
            self.post(self.tell, lambda: self.scan_done(None, str(exc)))
        except Exception as exc:   # show it rather than dying silently in a thread
            di.logger.exception("scan of %s failed", drive)
            msg = "%s: %s" % (type(exc).__name__, exc)
            self.post(self.tell, lambda: self.scan_done(None, msg))

    def scan_done(self, rep, err):
        self.show_progress(False)
        self.set_busy(False)
        if err:
            self.clear_case("Couldn't read this disc", err)
            self.set_case("none")
            self.set_status("Scan failed: details in the case notes")
            return
        self.rep = rep
        self.set_busy(False)
        self._build_rip_menu()
        name, year = split_title(display_title(rep))
        self.title_lbl.config(text=name)
        self.year_lbl.config(text=year)
        self.case_lbl.config(text="CASE FILE #%s" % rep["content_hash"][:8])
        facts = rep.get("facts") or {}
        kind = {"tv": "TV", "movie": "Movie", "collection": "Collection"}.get(rep["kind"], "Disc")
        if rep.get("extras_disc"):
            kind = "Special Features"            # the film's extras disc: no film on it
        mid = []
        if rep.get("extras_disc") and (rep.get("boxset") or {}).get("disc"):
            mid.append(rep["boxset"]["disc"])     # which disc of the set: "Indiana Jones Bonus Disc"
        if facts.get("episode_count") and rep["kind"] != "collection":
            mid.append("%d episodes" % facts["episode_count"])
        if facts.get("feature_minutes"):
            mid.append("%d min" % facts["feature_minutes"])
        m = rep.get("match")
        guess = bool(m and not m.get("sure") and not rep.get("personal_hit") and not (rep.get("discdb") or {}).get("hit"))
        by = "Best guess" if guess else ("Identified by %s" % rep.get("identified_by", "?")) if m or rep.get("personal_hit") \
            else "Unidentified"
        self.set_facts("%s %s" % (rep["format"], kind), "  \u2022  ".join(mid), by, guess=guess or not m)
        self.show_details(rep)
        self.show_plan_summary()
        self.show_picture(rep)
        self.show_match_button(rep)
        st = str(rep.get("status", ""))
        closed = st.startswith(("identified", "probable"))
        self.set_case("closed" if closed else "open")
        if closed:
            msg = ""
        elif st.startswith("best guess"):
            msg = "Best guess: confirm it, or pick another with Not right?"
        elif rep.get("disc_images"):
            msg = "Still a mystery: Choose the match (an AI can read the disc's picture for you)"
        else:
            msg = "Still a mystery: Choose the match to say what it is"
        self.set_status(msg)

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
        if badge:
            self.rip_tag.pack(side="right")
        else:
            self.rip_tag.pack_forget()
        self.audio_lbl.config(text=audio_summary(self.rep, plan))
        have_mkv = bool(self.makemkv_exe())
        self.engine_lbl.config(text="MakeMKV found" if have_mkv else "MakeMKV not found",
                               style="InsetOk.TLabel" if have_mkv else "InsetWarn.TLabel",
                               image=th.check() if have_mkv else "", compound="left")
        self.rip_note.config(text=("Ready to rip" if plan else "Nothing selected: use the arrow beside Rip > Choose titles")
                             if have_mkv else "install MakeMKV to rip (or set it in Settings)")
        th.set_dot(self.rip_dot, th.BRASS_600 if plan and have_mkv else th.INK_SUBTLE)
        if self.rep:
            self.rip_card.pack(fill="x", anchor="w", pady=(10, 0))
        self.rip_btn.set_state(bool(plan) and not self.busy and have_mkv,
                               arrow_enabled=bool(self.rep) and not self.busy)

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
                    tk.Frame(cell, width=pw, height=ph, background=th.VELLUM_100, highlightthickness=1,
                             highlightbackground=th.BRASS_300).pack(anchor="w")
                ttk.Label(cell, text=e["name"], style="CardStrip.TLabel", wraplength=wrap,
                          justify="left").pack(anchor="w", pady=(3, 0))
        self.films_box.pack(fill="x", pady=(10, 0), before=self._path_row)

    def rip_or_cancel(self):
        if self.ripping:
            self.cancel()
        else:
            self.rip()

    def rip(self):
        if not self.rep:
            return
        exe = self.makemkv_exe()
        if not exe:
            messagebox.showerror(APP, "MakeMKV's command-line tool (makemkvcon) wasn't found.\n\n"
                                      "Install MakeMKV, or add its folder to PATH.")
            return
        out = self.outdir.get().strip()
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP, "Can't use the rip folder:\n%s" % exc)
            return
        self.remember()
        self.cancelled = False
        self.set_busy(True, ripping=True)
        self.show_progress(True)
        self.set_status("Sending the disc to the lab (MakeMKV)…")
        # read every setting here, on the Tk thread; the worker only gets plain values
        opts = {"extras": bool(self.extras.get()),
                "selected": set(self.selected) if self.selected is not None else None}
        self.run_bg(self._rip_worker, exe, self.source.get(), out, self.rep, opts)

    def _rip_worker(self, exe, drive, out, rep, opts):
        try:
            source = makemkv_source(exe, drive)
            info = subprocess.run([exe, "-r", "--cache=1", "info", source], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=900,
                                  creationflags=NO_WINDOW).stdout
            mk = di.parse_makemkv_log(info)
            if not mk["titles"]:
                raise RuntimeError("MakeMKV found no titles on %s" % source)
            store = di.makemkv_log_store(rep["content_hash"])        # reused by --packet later
            os.makedirs(os.path.dirname(store), exist_ok=True)
            with open(store, "w", encoding="utf-8", newline="") as fh:
                fh.write(info)
            plan = self.current_plan(mk["titles"], extras=opts["extras"], selected=opts["selected"])
            di.logger.info("rip %s from %s (%s) into %s: %d titles", rep["content_hash"][:8], drive, source, out, len(plan))
            for item in plan:
                di.logger.info("  MakeMKV title %s (%s, %s) -> %s", item["mk"], item["title_id"], item["length"], item["dest"])
            done = []
            for i, item in enumerate(plan):
                if self.cancelled:
                    break
                self.post(self.set_status, "Bagging the evidence, %d of %d: %s" % (i + 1, len(plan), item["dest"].split("/")[-1]))
                work = os.path.join(out, ".ripping_%s_%d" % (rep["content_hash"][:8], item["mk"]))
                shutil.rmtree(work, ignore_errors=True)
                os.makedirs(work)
                ok = self._run_mkv(exe, source, item["mk"], work, i, len(plan))
                files = [f for f in os.listdir(work) if f.lower().endswith(".mkv")]
                if ok and files and not self.cancelled:
                    dest = rip_path(os.path.join(out, *item["dest"].split("/")), item, rep)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(os.path.join(work, max(files, key=lambda f: os.path.getsize(os.path.join(work, f)))), dest)
                    done.append(dest)
                    di.logger.info("  ripped %s", dest)
                elif not self.cancelled:
                    di.logger.warning("MakeMKV title %s did not rip (%s)", item["mk"],
                                      "no file written" if ok else "MakeMKV reported a failure")
                shutil.rmtree(work, ignore_errors=True)
            di.logger.info("rip finished: %d of %d titles%s", len(done), len(plan), " (cancelled)" if self.cancelled else "")
            self.post(self.rip_done, done, len(plan), None)
        except Exception as exc:
            di.logger.exception("rip failed")
            self.post(self.rip_done, [], 0, "%s: %s" % (type(exc).__name__, exc))

    def _run_mkv(self, exe, source, idx, work, n, total):
        cmd = [exe, "-r", "--progress=-same", "mkv", source, str(idx), work]
        di.logger.info("  run: %s", subprocess.list2cmdline(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        ok = False
        for line in self.proc.stdout:
            if line.startswith("PRGV:"):
                try:
                    cur, tot, mx = (int(x) for x in line[5:].split(","))
                    frac = (tot / mx) if mx else 0
                    self.post(self.progress.configure, {"value": int(1000 * (n + frac) / total)})
                except ValueError:
                    pass
            elif line.startswith("MSG:"):
                di.logger.info("  makemkv: %s", line.strip()[4:])   # MakeMKV's own messages, for the record
                if re.search(r'"Copy complete|titles saved', line):
                    ok = True
        self.proc.wait()
        rc, self.proc = self.proc.returncode, None
        if rc and not ok:
            di.logger.warning("makemkvcon exited with code %s", rc)
        return ok or rc == 0

    def rip_done(self, done, planned, err):
        self.show_progress(False)
        self.set_busy(False)
        if done:
            self.last_out = os.path.dirname(done[0])
        if err:
            self.set_status("Rip failed · details in the log (Open logs folder, at the bottom)")
            messagebox.showerror(APP, err + "\n\nDetails are in errors.log (Open logs folder, at the bottom of the window).")
        elif self.cancelled:
            self.set_status("Cancelled after %d of %d titles" % (len(done), planned))
        else:
            self.set_status("Done: %d of %d titles ripped · Open rip folder from the arrow beside Rip" % (len(done), planned))

    def cancel(self):
        self.cancelled = True
        if self.proc:
            self.proc.terminate()
        self.set_status("Cancelling…")

    def on_close(self):
        if self.busy and not messagebox.askyesno(APP, "A job is still running. Quit anyway?"):
            return
        self.cancel()
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
            t += "  ·  Specials"                    # Plex's season 0 (a special on its own disc)
        elif m["kind"] == "tv" and m.get("season"):
            t += "  ·  Season %s%s" % (m["season"], (", Disc %s" % m["disc"]) if m.get("disc") else "")
        return t
    guess = di.strip_tags(rep.get("best_guess") or "").split(" | ")[0]
    return guess or "Unidentified disc"


def split_title(text):
    """"Chicken Run (2000)" -> ("Chicken Run", "(2000)"); anything after the year stays with it."""
    m = re.match(r"^(.*?)\s+(\(\d{4}\).*)$", text or "")
    return (m.group(1), m.group(2)) if m else (text, "")


ART_SOURCE_NAMES = {"TMDB": "TMDB", "TheDiscDB": "DISCDB", "disc": "DISC", "IMDb": "IMDB"}


def picture_source(rep, art):
    """The line under the picture: where it comes from, with the film's id there
    ("SOURCE: TMDb ID #11943", "SOURCE: Disc Art")."""
    src = art.get("source")
    m = rep.get("match") or {}
    if src == "disc":
        line = "Disc Art"
    elif src == "TMDB":
        line = art.get("ref") or "TMDb"
    elif src == "TheDiscDB" and art.get("what") == "box set cover":
        line = art.get("ref") or "TheDiscDB"
    elif src == "TheDiscDB":       # TheDiscDB files films by their IMDb id
        line = "TheDiscDB \u00b7 %s" % m["imdb_id"] if m.get("imdb_id") else "TheDiscDB"
    else:
        line = src or ""
    return ("SOURCE: %s" % line) if line else ""


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
            out.append({"title_id": p["title_id"], "name": name + (" \u00b7 copy" if it.get("copy") else ""),
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
        return "%s%s" % (label, " (%d CHAPTERS)" % ch if ch and ch > 1 else "")
    return "%d TITLES" % len(plan)


AUDIO_NAMES = {"en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian", "ja": "Japanese",
               "pt": "Portuguese", "nl": "Dutch", "zh": "Chinese", "ko": "Korean", "ru": "Russian", "th": "Thai",
               "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
               "hu": "Hungarian", "tr": "Turkish", "he": "Hebrew", "ar": "Arabic", "hi": "Hindi"}


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
        name = AUDIO_NAMES.get(a.get("lang"), (a.get("lang") or "?").upper())
        if a.get("kind") == "commentary":
            name += " commentary"
        out.append("%s (%s)" % (name, " ".join(x for x in (a.get("codec"), ch) if x)))
    return "  \u2022  ".join(out[:3]) + ("  +%d more" % (len(out) - 3) if len(out) > 3 else "")


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
        rows.append(("TheDiscDB", "%s, in the %s release" % (disc, rel) if disc and rel else (disc or rel or "")))
    bs = rep.get("boxset")
    if bs and bs.get("title"):
        name = "%s (%s)" % (bs["title"], bs["year"]) if bs.get("year") else bs["title"]
        if bs.get("number") and bs.get("count"):
            name += " \u00b7 disc %s of %s" % (bs["number"], bs["count"])
        rows.append(("Box set", name))
    if rep.get("label"):
        rows.append(("Disc label", rep["label"]))
    names = (rep.get("bd_meta") or {}).get("names") or []
    if names and names[0] != rep.get("label"):
        rows.append(("Disc title", names[0]))
    po = rep.get("playlist_obfuscation")
    if isinstance(po, dict):
        rows.append(("Protection", "%d decoy titles · %s %s" % (
            po["decoys"], "the real film is" if po["sure"] else "best guess for the real film:", po["picked"])))
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
        return "No titles chosen to rip" if custom else "Nothing to rip on this disc"
    roles = [p["role"] for p in plan]
    eps = [p for p in plan if p["role"] == "episode"]
    extras = sum(1 for r in roles if r == "extra")
    if custom:
        head = "Will rip %d chosen title%s" % (len(plan), "" if len(plan) == 1 else "s")
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
        head = "Will rip %d episode%s%s" % (len(eps), "" if len(eps) == 1 else "s", span)
    elif roles.count("feature") > 1:
        eds = [(re.search(r"\{edition-([^}]*)\}", p["dest"]), p["length"]) for p in plan if p["role"] == "feature"]
        if all(m for m, _ in eds):     # one film in several editions (theatrical + extended, say)
            head = "Will rip %d editions: %s" % (len(eds), ", ".join("%s (%s)" % (m.group(1), n) for m, n in eds))
        else:
            head = "Will rip %d films" % roles.count("feature")
    elif all(r == "extra" for r in roles):         # a film's special-features disc
        head = "Will rip %d special feature%s" % (len(plan), "" if len(plan) == 1 else "s")
        return head
    elif "feature" in roles or "longest" in roles:
        f = next(p for p in plan if p["role"] in ("feature", "longest"))
        head = "Will rip the %s (%s)" % ("feature" if f["role"] == "feature" else "longest title", f["length"])
    else:
        head = "Will rip %d title%s" % (len(plan), "" if len(plan) == 1 else "s")
    if extras and not custom:
        head += " + %d extra%s" % (extras, "" if extras == 1 else "s")
    return head


class ScrollArea(ttk.Frame):
    """A list that grows with its rows up to a share of the screen, then scrolls, so a long
    list never pushes a window's buttons off screen. Put rows in .inner."""

    def __init__(self, parent, max_frac=0.55):
        super().__init__(parent)
        bg = ttk.Style(self).lookup("TFrame", "background") or None
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, yscrollincrement=20,
                                **({"background": bg} if bg else {}))
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.max_h = int(self.winfo_screenheight() * max_frac)
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

    def fit(self):
        self.update_idletasks()
        w, h = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        self.canvas.configure(width=w, height=min(h, self.max_h), scrollregion=(0, 0, w, h))
        if h > self.max_h:
            self.bar.pack(side="right", fill="y")
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
    """Rip menu > Choose titles: tick exactly which titles to rip."""

    def __init__(self, root, app):
        self.app = app
        rep = app.rep
        self.top = top = new_dialog(root, grab=True)
        top.title("Choose titles")
        top.transient(root)
        ttk.Label(top, text="Tick the titles to rip.", style="Sub.TLabel").pack(anchor="w", padx=12, pady=(12, 6))
        default = {p["title_id"] for p in plan_rip(rep, preview_titles(rep), app.extras.get())}
        self.current = set(app.selected if app.selected is not None else default)
        self.default = default
        order = {"feature": 0, "episode": 1, "extra": 2, "version": 3, "play-all": 4, "duplicate": 5,
                 "promo": 6, "segment": 7}
        rows = [t for t in rep["titles"] if t["role"] in order]
        self.names = {}
        for p in plan_rip(rep, preview_titles(rep), True, selected={t["id"] for t in rows}):
            self.names[p["title_id"]] = p["dest"].split("/")[-1]

        def place(t):
            """Episodes by season and episode as they'll be named (then by title); the rest by title."""
            code = re.search(r"\bs(\d+)e(\d+)", self.names.get(t["id"], ""), re.I) if t["role"] == "episode" else None
            ep = (int(code.group(1)), int(code.group(2))) if code else (9999, t.get("episode_index") or 9999)
            return (order[t["role"]], ep, di._natural(t["id"]))
        self.rows = sorted(rows, key=place)
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", padx=12, pady=12)     # packed first: always on screen
        hidden = [t for t in self.rows if t["role"] in self.HIDDEN]
        self.show_all = tk.BooleanVar(value=False)
        if hidden:
            ttk.Checkbutton(top, variable=self.show_all, command=self.refill,
                            text="Show %d more: duplicates, play-alls, promos and pieces of the film" % len(hidden)
                            ).pack(side="bottom", anchor="w", padx=12)
        self.area = ScrollArea(top)
        self.area.pack(fill="both", expand=True, padx=12)
        self.vars = {}
        self.refill()
        ttk.Button(btns, text="OK", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right", padx=6)
        ttk.Button(btns, text="Default", command=self.to_default).pack(side="left")

    # repeats of other titles, ads, the film's pieces: rarely wanted, so out of the way
    HIDDEN = ("play-all", "duplicate", "promo", "segment")
    ROLE_TEXT = {"promo": "Promo", "segment": "Piece of the film", "play-all": "Play-all", "duplicate": "Duplicate"}

    def refill(self):
        for k, v in self.vars.items():               # keep ticks across showing / hiding
            (self.current.add if v.get() else self.current.discard)(k)
        body = self.area.inner
        for w in body.winfo_children():
            w.destroy()
        for col, head in enumerate(("", "Title", "", "Length", "Video", "Saves as")):
            ttk.Label(body, text=head, style="Sub.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 10))
        self.vars = {}
        shown = [t for t in self.rows if self.show_all.get() or t["role"] not in self.HIDDEN
                 or t["id"] in self.current]
        for r, t in enumerate(shown, 1):
            v = tk.BooleanVar(value=t["id"] in self.current)
            self.vars[t["id"]] = v
            if t["role"] == "episode":
                label = self.episode_text(t)
            else:
                label = self.ROLE_TEXT.get(t["role"], t["role"].title())
            if t["role"] == "version" and t.get("edition"):
                label = "Version: %s" % t["edition"]
            if t["role"] == "duplicate" and t.get("note"):
                label = "Duplicate (%s)" % t["note"]
            ttk.Checkbutton(body, variable=v).grid(row=r, column=0, sticky="w")
            ttk.Label(body, text=label).grid(row=r, column=1, sticky="w", padx=(0, 10))
            ttk.Label(body, text=t["id"], style="Sub.TLabel").grid(row=r, column=2, sticky="w", padx=(0, 10))
            ttk.Label(body, text=di.hms(t["duration"])).grid(row=r, column=3, sticky="e", padx=(0, 10))
            ttk.Label(body, text=t.get("video") or "", style="Sub.TLabel").grid(row=r, column=4, sticky="w",
                                                                                padx=(0, 10))
            ttk.Label(body, text=self.names.get(t["id"], ""), style="Sub.TLabel").grid(row=r, column=5, sticky="w")

    def episode_text(self, t):
        """"Episode 11": its number in the season as it will be named (your label, or the guess);
        a two-part title "Episode 11-12". Just "Episode" when there's no number to go by."""
        code = re.search(r"\bs\d+e(\d+)((?:-e\d+)*)\b", self.names.get(t["id"], ""), re.I)
        if not code:
            return "Episode"
        nums = [int(code.group(1))] + [int(x) for x in re.findall(r"\d+", code.group(2) or "")]
        return "Episode %d" % nums[0] + ("-%d" % nums[-1] if len(nums) > 1 else "")

    def to_default(self):
        self.current = set(self.default)
        for k, v in self.vars.items():
            v.set(k in self.default)

    def ok(self):
        chosen = (self.current - set(self.vars)) | {k for k, v in self.vars.items() if v.get()}
        default = {p["title_id"] for p in plan_rip(self.app.rep, preview_titles(self.app.rep), self.app.extras.get())}
        self.app.selected = None if chosen == default else chosen
        self.top.destroy()
        self.app.options_changed()


def human_count(n):
    return format(n, ",") if n is not None else "?"


class DataProgress:
    """A progress bar + line of text inside a dialog, fed by App.start_data_update."""

    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        self.bar = ttk.Progressbar(self.frame, mode="determinate", maximum=1000, length=360)
        self.bar.pack(fill="x")
        self.text = ttk.Label(self.frame, text="", style="Sub.TLabel", wraplength=380, justify="left")
        self.text.pack(anchor="w", pady=(3, 0))
        self._spinning = False

    def progress(self, label, done, total):
        if self._spinning:
            self.bar.stop()
            self.bar.config(mode="determinate")
            self._spinning = False
        if total:
            self.bar.config(value=1000 * done / total)
            self.text.config(text="Downloading %s: %d of %d MB" % (label, done // 2**20, max(1, total // 2**20)))
        else:
            self.text.config(text="Downloading %s: %d MB" % (label, done // 2**20))

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

    SIZES = {"discdb": "about 4 MB", "imdb": "about 300 MB, then a few minutes to index"}

    def __init__(self, root, app, missing):
        self.app, self.missing = app, missing
        self.top = top = new_dialog(root)
        top.title("Get disc data")
        top.transient(root)
        top.resizable(False, False)
        body = ttk.Frame(top, padding=18)
        body.pack(fill="both")
        ttk.Label(body, text="Get disc data", style="Title.TLabel").pack(anchor="w")
        parts = {"discdb": "TheDiscDB (fingerprints of known discs)", "imdb": "IMDb's public title data"}
        ttk.Label(body, wraplength=400, justify="left", text=(
            "Disc Detective recognizes discs with:\n" +
            "".join("   •  %s, %s\n" % (parts[k], self.SIZES[k]) for k in missing) +
            "\nThis is a one-time download. After that the data refreshes itself, "
            "and scans work offline.")).pack(anchor="w", pady=(8, 12))
        self.prog = DataProgress(body)
        self.btns = ttk.Frame(body)
        self.btns.pack(fill="x")
        self.go = ttk.Button(self.btns, text="Download now", command=self.start)
        self.go.pack(side="right")
        self.later = ttk.Button(self.btns, text="Later", command=top.destroy)
        self.later.pack(side="right", padx=6)
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.after(50, lambda: (top.lift(), self.go.focus_set()))

    def start(self):
        self.go.config(state="disabled")
        self.later.config(state="disabled")
        self.prog.frame.pack(fill="x", before=self.btns, pady=(0, 12))
        self.prog.message("Starting…")
        self.app.start_data_update(self.missing, False, self)

    def progress(self, *a):
        self.prog.progress(*a)

    def message(self, text):
        self.prog.message(text)

    def finished(self, res, err):
        self.prog.stop()
        self.prog.bar.config(mode="determinate", value=1000 if not err else 0)
        if err:
            self.prog.text.config(text="Couldn't finish: %s\nYou can retry from Settings." % err)
            self.later.config(text="Close", state="normal")
        else:
            self.prog.text.config(text="Ready. Insert a disc and press Scan Disc.")
            self.later.config(text="Close", state="normal")
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
        di.logger.exception("copying the picture failed")
    return False


class AskAIDialog:
    """No API key needed: show the disc's own pictures, copy one and the question into any chat
    AI, then look its answer up in IMDb (the answer is checked like a typed one)."""

    def __init__(self, root, app):
        self.app, rep = app, app.rep
        self.images = rep.get("disc_images") or []
        self.k = 0
        self.top = top = new_dialog(root)
        top.title("Ask an AI about the disc's picture")
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
        ttk.Label(right, text="Chat AIs recognise covers and title art well. Copy the picture and the "
                              "question, paste both into one, then put its answer below.",
                  style="Sub.TLabel", wraplength=330, justify="left").pack(anchor="w")
        self.question = di.ai_question(rep)
        q = tk.Text(right, width=50, height=5, wrap="word", relief="flat", padx=6, pady=4, font="TkDefaultFont")
        q.insert("1.0", self.question)
        q.config(state="disabled")
        q.pack(anchor="w", pady=(8, 0))
        copy = ttk.Frame(right)
        copy.pack(anchor="w", pady=(8, 0))
        self.copy_pic_btn = ttk.Button(copy, text="Copy picture", command=self.copy_picture)
        self.copy_pic_btn.pack(side="left")
        ttk.Button(copy, text="Copy question", command=self.copy_question).pack(side="left", padx=(6, 0))
        ttk.Button(copy, text="Save picture\u2026", command=self.save_picture).pack(side="left", padx=(6, 0))
        sites = ttk.Frame(right)
        sites.pack(anchor="w", pady=(8, 0))
        ttk.Label(sites, text="Open:", style="Cap.TLabel").pack(side="left", padx=(0, 6))
        for name, url in AI_SITES:
            ttk.Button(sites, text=name, style="Small.TButton",
                       command=lambda n=name, u=url: self.open_site(n, u)).pack(side="left", padx=(0, 4))
        self.key = (app.settings or {}).get("gemini_key")
        if self.key:                     # a key in Settings: Gemini can be asked from here directly
            self.ask_btn = ttk.Button(sites, text="Ask Gemini for me", command=self.ask_gemini)
            self.ask_btn.pack(side="left", padx=(10, 0))
        self.note = ttk.Label(right, text="", style="Cap.TLabel")
        self.note.pack(anchor="w", pady=(6, 0))
        ans = ttk.Frame(right)
        ans.pack(anchor="w", pady=(10, 0))
        ttk.Label(ans, text="Its answer").pack(side="left", padx=(0, 8))
        self.answer = tk.StringVar()
        e = ttk.Entry(ans, textvariable=self.answer, width=30)
        e.pack(side="left")
        btns = ttk.Frame(f)
        btns.grid(row=1, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Look it up", command=self.look_up).pack(side="right")
        ttk.Button(btns, text="Close", command=top.destroy).pack(side="right", padx=6)
        top.bind("<Return>", lambda ev: self.look_up())
        top.bind("<Escape>", lambda ev: top.destroy())
        self.show(0)
        top.after(50, lambda: (top.lift(), e.focus_set()))

    def current(self):
        return self.images[self.k] if self.images else None

    def show(self, step):
        if not self.images:
            self.pic.config(text="No pictures on this disc", image="")
            return
        self.k = (self.k + step) % len(self.images)
        img = self.app._photo(self.current()["image"])
        self.pic.config(image=img or "", text="" if img else "(can't show it)")
        self.count.config(text="%d of %d" % (self.k + 1, len(self.images)))

    def copy_picture(self):
        ok = bool(self.current()) and copy_image(self.current()["full"])
        self.note.config(text="Picture copied: paste it into the chat" if ok else
                         "Couldn't copy the picture here: use Save picture\u2026 and attach the file")

    def open_site(self, name, url):
        if "{q}" in url:
            webbrowser.open(url.replace("{q}", urllib.parse.quote(self.question)))
            self.note.config(text="The question is filled in: paste the picture there and send")
        else:
            webbrowser.open(url)
            self.copy_question()
            self.note.config(text="Question copied: paste it and the picture into %s" % name)

    def ask_gemini(self):
        cur = self.current()
        if not cur:
            return
        self.ask_btn.config(state="disabled")
        self.note.config(text="Asking Gemini\u2026")
        q, key = di.ai_question(self.app.rep, as_json=True), self.key

        def work():
            try:
                text, _ = di.gemini_ask(key, q, cur["full"])
                a = di.parse_ai_answer(text)
                msg = None if a else "Gemini couldn't tell: %s" % text[:80]
            except Exception as exc:
                a, msg = None, str(exc)

            def done():
                if not self.top.winfo_exists():
                    return
                self.ask_btn.config(state="normal")
                if a:
                    self.answer.set("%s (%s)" % (a["title"], a["year"]) if a.get("year") else a["title"])
                    self.note.config(text="Gemini's answer is below: Look it up to check it against IMDb")
                else:
                    self.note.config(text=msg)
            self.app.post(done)
        threading.Thread(target=work, daemon=True).start()

    def copy_question(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(self.question)
        self.note.config(text="Question copied")

    def save_picture(self):
        cur = self.current()
        if not cur:
            return
        dest = filedialog.asksaveasfilename(parent=self.top, title="Save the disc's picture", defaultextension=".png",
                                            initialfile="disc_picture.png", filetypes=[("PNG image", "*.png")])
        if dest:
            shutil.copyfile(cur["full"], dest)
            self.note.config(text="Saved: attach %s in the chat" % os.path.basename(dest))

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

    KINDS = {"movie": "film", "tvMovie": "TV film", "video": "video", "tvSeries": "TV series",
             "tvMiniSeries": "miniseries", "tvSpecial": "TV special", "short": "short", "tvShort": "TV short"}

    def __init__(self, root, app, title="", year=None, tv=False, season=None, disc=None, picked=None, on_pick=None):
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
        top.title("Which film is this?" if on_pick else "What's on this disc?")
        top.transient(root)
        top.resizable(False, False)
        f = ttk.Frame(top, padding=16)
        f.pack(fill="both")
        self.title = tk.StringVar(value=title or "")
        self.year = tk.StringVar(value=str(year or ""))
        self.tv = tk.BooleanVar(value=bool(tv))
        self.season = tk.StringVar(value=str(season or ""))
        self.disc = tk.StringVar(value=str(disc or ""))
        ttk.Label(f, text="Title").grid(row=0, column=0, sticky="w", padx=(0, 10))
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
        ttk.Label(f, text="Year").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.year, width=6).grid(row=3, column=1, sticky="w", pady=(6, 0))
        e.bind("<Down>", lambda ev: (self.results.focus_set(), self.results.selection_set("0"),
                                     self.results.focus("0")) if self.matches else None)
        self.title.trace_add("write", lambda *a: self.typed())
        kinds = ttk.Frame(f)
        kinds.grid(row=4, column=1, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Radiobutton(kinds, text="Film", variable=self.tv, value=False, command=self.toggle).pack(side="left")
        ttk.Radiobutton(kinds, text="TV show", variable=self.tv, value=True, command=self.toggle).pack(side="left", padx=(12, 0))
        self.tv_row = ttk.Frame(f)
        ttk.Label(self.tv_row, text="Season").pack(side="left")
        ttk.Entry(self.tv_row, textvariable=self.season, width=4).pack(side="left", padx=(6, 14))
        ttk.Label(self.tv_row, text="Disc").pack(side="left")
        ttk.Entry(self.tv_row, textvariable=self.disc, width=4).pack(side="left", padx=(6, 0))
        self.tv_row.grid(row=5, column=1, columnspan=3, sticky="w", pady=(6, 0))
        self.note = ttk.Label(f, text=("Pick the right entry from IMDb above, or just type the title and year. "
                                       if self.con else "IMDb data isn't downloaded, so type the title and year. ") +
                              "Disc Detective remembers it for this disc.",
                              style="Sub.TLabel", wraplength=380, justify="left")
        self.note.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        btns = ttk.Frame(f)
        btns.grid(row=7, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Use this", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.close).pack(side="right", padx=6)
        top.bind("<Return>", lambda e: self.ok())
        top.bind("<Escape>", lambda e: self.close())
        top.protocol("WM_DELETE_WINDOW", self.close)
        self.toggle()
        if on_pick:                     # one film of a collection: no film/TV or season choice
            kinds.grid_remove()
            self.tv_row.grid_remove()
            self.note.config(text="Pick the film from the IMDb list.")
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
            bits = [self.KINDS.get(c["type"], c["type"])]
            if c.get("runtime"):
                bits.append("%d min" % c["runtime"])
            if c.get("votes"):
                bits.append("%s votes" % format(c["votes"], ","))
            img = self._thumbs.get(c["imdb_id"])
            self.results.insert("", "end", iid=str(i), text="  %s (%s)  ·  %s" % (
                c["name"], c.get("year") or "?", "  ·  ".join(bits)), **({"image": img} if img else {}))
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
        if not want or not self.app.settings.get("pictures", True):
            return
        self._gen += 1
        gen, args = self._gen, self.app.scan_args()      # settings read here, on the Tk thread

        def work():
            try:
                found = di.thumbs_for(want, args)
            except Exception:
                di.logger.exception("posters for the type-ahead list")
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
        self.tv.set(c["kind"] == "tv")
        self.toggle()
        self.picked = c
        self.show_pick()

    def show_pick(self):
        self.pick_lbl.config(text=("Using IMDb %s" % self.picked["imdb_id"]) if self.picked else "")

    def close(self):
        if self.con is not None:
            self.con.close()
            self.con = None
        self.top.destroy()

    def toggle(self):
        (self.tv_row.grid if self.tv.get() and not self.on_pick else self.tv_row.grid_remove)()

    def truth(self):
        if self.picked:
            if self.tv.get() and not self.season.get().strip().isdigit():
                return None, "Enter the season number."
            p = dict(self.picked, kind="tv" if self.tv.get() else "movie")
            s, d = self.season.get().strip(), self.disc.get().strip()
            return di.truth_for(p, int(s) if self.tv.get() else None,
                                int(d) if self.tv.get() and d.isdigit() else None), None
        title = self.title.get().strip()
        year = self.year.get().strip()
        if not title:
            return None, "Enter the title."
        if year and not re.fullmatch(r"(18|19|20)\d\d", year):
            return None, "The year should look like 1998."
        t = "%s (%s)" % (title, year) if year else title
        if self.tv.get():
            s, d = self.season.get().strip(), self.disc.get().strip()
            if not s.isdigit():
                return None, "Enter the season number."
            t += " S%d" % int(s) + (" D%d" % int(d) if d.isdigit() else "")
        return t, None

    def ok(self):
        if self.on_pick:
            if not self.picked:
                self.note.config(text="Pick one of the IMDb results above.", foreground="#b00020")
                return
            picked = dict(self.picked)
            self.close()
            self.on_pick(picked)
            return
        t, err = self.truth()
        if err:
            self.note.config(text=err, foreground="#b00020")
            return
        self.close()
        self.app.use_answer(t)


class CollectionDialog:
    """Not right? > Change the films: which film each title is, title by title."""

    def __init__(self, root, app):
        self.app = app
        rep = app.rep
        cand = rep["candidates"][0]
        self.name = cand["name"]
        self.assign = {it["title_id"]: {"imdb_id": it["imdb_id"], "name": it["name"], "year": it.get("year")}
                       for it in cand["items"]}
        self.rows = [t for t in rep["titles"] if t["duration"] >= 10 * 60
                     and t["role"] in ("feature", "version", "extra", "episode")]
        self.rows.sort(key=lambda t: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", t["id"])])
        self.versions = {v["title_id"]: v["same_as"] for v in cand.get("versions") or []}
        self.thumbs = {}
        self.top = top = new_dialog(root)
        top.title("Which film is each title?")
        top.transient(root)
        top.resizable(False, False)
        f = ttk.Frame(top, padding=14)
        f.pack(fill="both")
        namerow = ttk.Frame(f)
        namerow.pack(fill="x", pady=(0, 10))
        ttk.Label(namerow, text="Collection name").pack(side="left", padx=(0, 8))
        self.name_var = tk.StringVar(value=di.strip_tags(self.name))
        ttk.Entry(namerow, textvariable=self.name_var, width=44).pack(side="left")
        ttk.Label(f, text="Each title on the disc and the film it is. Change any that are wrong.",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x", pady=(10, 0))         # packed first: always on screen
        self.note = ttk.Label(f, text="", style="Sub.TLabel")
        self.note.pack(side="bottom", anchor="w", pady=(8, 0))
        self.area = ScrollArea(f, max_frac=0.5)
        self.area.pack(fill="both", expand=True)
        self.grid = self.area.inner
        ttk.Button(btns, text="Save", command=self.save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right", padx=6)
        self.draw()
        self.fetch_thumbs()

    def draw(self):
        for w in self.grid.winfo_children():
            w.destroy()
        for r, t in enumerate(self.rows):
            a = self.assign.get(t["id"])
            img = self.thumbs.get(a["imdb_id"]) if a else None
            if img:
                pic = ttk.Label(self.grid, image=img)
            else:                                   # keep the column the width of a poster
                pic = ttk.Frame(self.grid, width=di.ART_PICK[0], height=di.ART_PICK[1])
            pic.grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Label(self.grid, text="%s  ·  %s" % (t["id"], di.hms(t["duration"])), style="Sub.TLabel").grid(
                row=r, column=1, sticky="w", padx=(0, 12))
            if a:
                what = "%s (%s)" % (a["name"], a.get("year") or "?")
            elif t["id"] in self.versions and self.versions[t["id"]] in self.assign:
                what = "second copy of %s" % self.versions[t["id"]]
            else:
                what = "not a film (extra)"
            ttk.Label(self.grid, text=what, width=42).grid(row=r, column=2, sticky="w")
            ttk.Button(self.grid, text="Change…", style="Small.TButton",
                       command=lambda t=t: self.change(t)).grid(row=r, column=3, padx=(8, 4))
            if a:
                ttk.Button(self.grid, text="Not a film", style="Small.TButton",
                           command=lambda t=t: self.clear(t)).grid(row=r, column=4)

    def change(self, t):
        a = self.assign.get(t["id"]) or {}
        MatchDialog(self.top, self.app, title=a.get("name", ""), picked=dict(a, kind="movie") if a else None,
                    on_pick=lambda c, t=t: self.set(t, c))

    def set(self, t, c):
        self.assign[t["id"]] = {"imdb_id": c["imdb_id"], "name": c["name"], "year": c.get("year")}
        self.draw()
        self.fetch_thumbs()

    def clear(self, t):
        self.assign.pop(t["id"], None)
        self.draw()

    def fetch_thumbs(self):
        want = [dict(a) for a in self.assign.values() if a["imdb_id"] not in self.thumbs]
        if not want or not self.app.settings.get("pictures", True):
            return
        args = self.app.scan_args()

        def work():
            try:
                found = di.thumbs_for(want, args)
            except Exception:
                di.logger.exception("posters for the collection editor")
                return
            self.app.post(self.show_thumbs, found)
        threading.Thread(target=work, daemon=True).start()

    def show_thumbs(self, found):
        if not self.top.winfo_exists():
            return
        for imdb_id, path in found.items():
            try:
                self.thumbs[imdb_id] = tk.PhotoImage(file=path)
            except tk.TclError:
                pass
        self.draw()

    def films(self):
        order = [t["id"] for t in self.rows]
        return [(tid, self.assign[tid]["imdb_id"]) for tid in sorted(self.assign, key=order.index)
                if tid in order]

    def save(self):
        films = self.films()
        if not films:
            self.note.config(text="Choose at least one film, or use Not right? > It's one film or TV show instead.",
                             foreground="#b00020")
            return
        name = self.name_var.get().strip() or self.name
        self.top.destroy()
        self.app.use_answer(di.truth_for_collection(name, films))


class AboutDialog:
    """About / credits: the version, and the data and software Disc Detective stands on."""

    CREDITS = (
        ("Disc identification", "Disc fingerprints, title lists and box art from TheDiscDB (MIT License). "
                                "Titles from IMDb's non-commercial datasets, downloaded to your own computer "
                                "for personal use under IMDb's terms.",
         (("thediscdb.com", "https://thediscdb.com"),
          ("IMDb datasets", "https://developer.imdb.com/non-commercial-datasets/"))),
        ("TV episodes", "Episode lists and DVD orders from TVmaze, under CC BY-SA 4.0.",
         (("tvmaze.com", "https://www.tvmaze.com"),)),
        ("Pictures", "Posters and box art from the disc itself, TheDiscDB and, with a key of your own, TMDB. "
                     + di.TMDB_CREDIT, (("themoviedb.org", "https://www.themoviedb.org"),)),
        ("AI detection assistance", "Optional, with a Gemini key of your own: Google's Gemini looks at a "
                                    "disc's pictures, and IMDb checks its answer.", ()),
        ("Ripping and naming", "Rips with MakeMKV, which you install yourself; files are named the way Plex "
                               "likes. Disc Detective isn't affiliated with either.",
         (("makemkv.com", "https://www.makemkv.com"),)),
        ("Type and icons", "Detective Serif (a cut of Playfair Display), Courier Prime and Inter, under the "
                           "SIL Open Font License 1.1. Icons from Heroicons (MIT License).", ()),
    )

    def __init__(self, root):
        self.top = top = new_dialog(root)
        top.title("About %s" % APP)
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
        ttk.Label(names, text="VERSION %s \u00b7 MIT LICENSE" % di.VERSION, style="Case.TLabel").pack(anchor="w")
        ttk.Label(names, text=APP, style="Name.TLabel").pack(anchor="w")
        ttk.Label(body, text="Credits and sources", style="Kind.TLabel").pack(anchor="w", pady=(14, 6))
        card = th.frame(body, th.CARD, border=th.BRASS_400, radius=7, shadow=3)
        card.pack(fill="x")
        inner = ttk.Frame(card, style="Card.TFrame", padding=(18, 12, 18, 12))
        inner.pack(fill="x")
        inner.columnconfigure(0, weight=1, uniform="c")
        inner.columnconfigure(2, weight=1, uniform="c")
        rows = (len(self.CREDITS) + 1) // 2
        for k, (cap, text, links) in enumerate(self.CREDITS):      # two columns, so it stays short
            col, r = (k // rows) * 2, (k % rows) * 2
            if r:
                th.rule(inner, th.BRASS_200).grid(row=r - 1, column=col, sticky="ew", pady=8)
            cell = ttk.Frame(inner, style="Card.TFrame")
            cell.grid(row=r, column=col, sticky="nw")
            ttk.Label(cell, text=cap.upper(), style="CardCap.TLabel").pack(anchor="w")
            ttk.Label(cell, text=text, style="Card.TLabel", wraplength=300, justify="left").pack(anchor="w", pady=(2, 0))
            if links:
                row = ttk.Frame(cell, style="Card.TFrame")
                row.pack(anchor="w", pady=(3, 0))
                for j, (label, url) in enumerate(links):
                    th.link(row, label, lambda url=url: webbrowser.open(url), style="CardLink.TLabel",
                            icon="external", right=True).pack(side="left", padx=(0 if j == 0 else 14, 0))
        tk.Frame(inner, background=th.BRASS_200, width=1).grid(row=0, column=1, rowspan=rows * 2 - 1, sticky="ns", padx=18)
        foot = ttk.Frame(body, style="Desk.TFrame")
        foot.pack(fill="x", pady=(14, 0))
        th.link(foot, "Full notices and licenses",
                lambda: webbrowser.open("https://github.com/%s/blob/main/THIRD_PARTY.md" % di.PROJECT_REPO),
                style="DeskLink.TLabel", icon="external", right=True).pack(side="left")
        ttk.Button(foot, text="Close", command=top.destroy).pack(side="right")
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
        name = AUDIO_NAMES.get(a.get("lang"), (a.get("lang") or "?").upper())
        parts.append(" ".join(x for x in (name, a.get("codec"), ch) if x) + (" +%d" % (len(aud) - 1) if len(aud) > 1 else ""))
    if t.get("subs"):
        parts.append("%d subtitle%s" % (len(t["subs"]), "" if len(t["subs"]) == 1 else "s"))
    return "  \u00b7  ".join(parts)


class LabelDialog:
    """Label titles: say what each title of a disc in your library is, in TheDiscDB's terms.
    Your labels stay across rescans; submission packets use them, and Help TheDiscDB lists the
    discs where they'd fill a gap in TheDiscDB (or differ from it)."""

    ROLE_NAMES = {"feature": "the film", "version": "another version", "episode": "episode", "extra": "extra",
                  "play-all": "play-all", "duplicate": "duplicate", "segment": "piece of the film", "promo": "promo"}
    ORDER = {"feature": 0, "version": 1, "episode": 2, "extra": 3, "play-all": 4, "duplicate": 5, "segment": 6,
             "promo": 7}
    MIN_SECONDS = 60

    def __init__(self, root, app, h, name, on_save=None):
        self.app, self.h, self.on_save = app, h, on_save
        args = app.scan_args()
        self.db = args.db
        con = di.library_open(self.db)
        titles = [dict(r) for r in con.execute("SELECT * FROM titles WHERE hash=?", (h,))]
        mine = di.get_labels(con, h)
        self.series = di.series_of(con, h)        # for episode names from season and number
        con.close()
        self.order = args.episode_order
        self.online = not args.offline
        index = di.discdb_index_open(args.discdb_dir or di.default_discdb_dir(), allow_download=False)
        hit = di.index_hit(index, h) if index is not None else None
        items = {str(t.get("source") or "").lower(): t.get("item") or {} for t in (hit or {}).get("titles") or []}
        rows = sorted((t for t in titles if t["role"] in self.ORDER and (t["duration"] or 0) >= self.MIN_SECONDS),
                      key=lambda t: (self.ORDER[t["role"]], t.get("episode_index") or 0, di._natural(t["title_id"])))
        self.top = top = new_dialog(root)
        top.title("Label titles")
        top.transient(root)
        top.configure(background=th.PARCHMENT)
        th.color_title_bar(top)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(18, 14, 18, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=name, style="Title.TLabel", background=th.PARCHMENT).pack(anchor="w")
        ttk.Label(body, text="Say what each title is, in TheDiscDB's terms. For an episode or an episode's deleted "
                             "scene, its season and number are enough: the name is filled in. Promo is for ads (coming soon, other "
                             "releases): not ripped unless you tick it. Your labels stay with this disc (fingerprint %s) "
                             "through rescans, and rips and submission packets use them." % h[:8],
                  style="DeskSub.TLabel", wraplength=720, justify="left").pack(anchor="w", pady=(2, 10))
        btns = ttk.Frame(body, style="Desk.TFrame")
        btns.pack(side="bottom", fill="x", pady=(10, 0))           # first: always on screen
        ttk.Button(btns, text="Save", command=self.save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right", padx=(0, 8))
        if not rows:
            ttk.Label(body, text="No titles recorded for this disc yet: scan it once first.",
                      style="DeskSub.TLabel").pack(anchor="w")
        area = ScrollArea(body)
        area.pack(fill="both", expand=True)
        g = area.inner
        for c, head in enumerate(("Title", "Length", "Disc Detective read", "TheDiscDB says", "Type", "Name", "S", "E")):
            ttk.Label(g, text=head.upper(), style="Cap.TLabel").grid(row=0, column=c, sticky="w", padx=(0, 10), pady=(0, 4))
        self.vars = {}
        for r, t in enumerate(rows, 1):
            tid = t["title_id"]
            clips = (t["clips"] or "").split(",") if t["clips"] else []
            keys = [tid.lower()] + ([clips[0].lower() + ".m2ts"] if len(clips) == 1 else [])
            m = re.fullmatch(r"Title (\d+)", tid)
            if m:
                keys += ["%02d" % int(m.group(1)), m.group(1)]
            item = next((items[k] for k in keys if items.get(k)), {})
            theirs = " \u00b7 ".join(x for x in (item.get("type"), item.get("title")) if x) or ("\u2014" if hit else "not in TheDiscDB")
            cell = ttk.Frame(g)
            cell.grid(row=r, column=0, sticky="w", padx=(0, 10), pady=2)
            ttk.Label(cell, text=tid).pack(anchor="w")
            tr = tracks_text(t.get("tracks"))
            if tr:
                ttk.Label(cell, text=tr, style="Sub.TLabel").pack(anchor="w")
            ttk.Label(g, text=di.hms(t["duration"])).grid(row=r, column=1, sticky="w", padx=(0, 10))
            ttk.Label(g, text=self.ROLE_NAMES.get(t["role"], t["role"]), style="Sub.TLabel").grid(
                row=r, column=2, sticky="w", padx=(0, 10))
            ttk.Label(g, text=theirs, style="Sub.TLabel", wraplength=200, justify="left").grid(
                row=r, column=3, sticky="w", padx=(0, 10))
            my = mine.get(tid) or {}
            v = {"type": tk.StringVar(value=my.get("type") or ""), "title": tk.StringVar(value=my.get("title") or ""),
                 "season": tk.StringVar(value=str(my["season"]) if my.get("season") else ""),
                 "episode": tk.StringVar(value=str(my["episode"]) if my.get("episode") else "")}
            v["auto"] = None                          # the name we filled in (yours replaces it)
            ttk.Combobox(g, textvariable=v["type"], values=("",) + di.LABEL_TYPES, width=12,
                         state="readonly").grid(row=r, column=4, sticky="w", padx=(0, 6))
            ttk.Entry(g, textvariable=v["title"], width=28).grid(row=r, column=5, sticky="we", padx=(0, 6))
            se = ttk.Entry(g, textvariable=v["season"], width=3)
            se.grid(row=r, column=6, sticky="w", padx=(0, 4))
            ep = ttk.Entry(g, textvariable=v["episode"], width=3)
            ep.grid(row=r, column=7, sticky="w")
            for w in (se, ep):
                w.bind("<FocusOut>", lambda e, tid=tid: self.fill_episode(tid))
            v["type"].trace_add("write", lambda *a, tid=tid: self.fill_episode(tid))
            self.vars[tid] = v
        top.bind("<Escape>", lambda e: top.destroy())
        centre_over(top, root)

    # types named from the episode they belong to: an episode is its title; a deleted scene
    # "<episode title> Deleted Scene" (several in one Deleted Scenes folder need their own names)
    NAMED = {"Episode": "%s", "DeletedScene": "%s Deleted Scene"}

    def named(self, kind, episode_title):
        return self.NAMED.get(kind, "%s") % episode_title if episode_title else None

    def fill_episode(self, tid):
        """An episode with a season and number: its title from TVmaze or IMDb, unless you've typed
        one (a number given with no type makes it an Episode)."""
        v = self.vars[tid]
        season, episode = v["season"].get().strip(), v["episode"].get().strip()
        if not (season.isdigit() and episode.isdigit()) or v["type"].get() not in ("",) + tuple(self.NAMED):
            return
        if not v["type"].get():
            v["type"].set("Episode")
        kind = v["type"].get()
        if v["title"].get().strip() and v["title"].get() != v["auto"]:
            return                                # a name you typed stays
        key = (season, episode)
        v["want"] = key

        def work():
            name = self.named(kind, di.episode_name(self.series, season, episode, self.order, online=self.online))

            def done():
                if self.top.winfo_exists() and v.get("want") == key and name and \
                        (not v["title"].get().strip() or v["title"].get() == v["auto"]):
                    v["auto"] = name
                    v["title"].set(name)
            self.app.post(done)
        threading.Thread(target=work, daemon=True).start()

    def save(self):
        con = di.library_open(self.db)
        try:
            for tid, v in self.vars.items():
                if v["type"].get() in self.NAMED and not v["title"].get().strip() \
                        and v["season"].get().strip().isdigit() and v["episode"].get().strip().isdigit():
                    v["title"].set(self.named(v["type"].get(), di.episode_name(
                        self.series, v["season"].get(), v["episode"].get(), self.order, online=self.online)) or "")
                di.set_label(con, self.h, tid, v["type"].get() or None, v["title"].get(),
                             v["season"].get() or None, v["episode"].get() or None)
        finally:
            con.close()
        self.top.destroy()
        self.app.labels_changed(self.h)
        if self.on_save:
            self.on_save()


class HelpDiscDBDialog:
    """Settings > Disc data > Help TheDiscDB: the discs in your library TheDiscDB doesn't have yet
    (submit them), and the ones it has with titles worth labelling (suggest edits). Submitting
    needs your own GitHub account, so this only gathers what you need."""

    SUBMIT_COLS = (("title", "Disc", 300), ("format", "Format", 70), ("label", "Volume label", 190),
                   ("ready", "Status", 90))
    IMPROVE_COLS = (("title", "Disc", 250), ("disc", "TheDiscDB entry", 230), ("what", "Unlabelled there", 170))
    READY = {"ready": "Ready", "check": "Check title", "confirm": "Confirm first", "rescan": "Rescan first"}

    def __init__(self, root, app):
        self.app, self.root = app, root
        self.top = top = new_dialog(root)
        top.title("Help TheDiscDB")
        top.transient(root)
        top.configure(background=th.PARCHMENT)
        th.color_title_bar(top)
        body = ttk.Frame(top, style="Desk.TFrame", padding=(18, 14, 18, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Help TheDiscDB", style="Title.TLabel", background=th.PARCHMENT).pack(anchor="w")
        ttk.Label(body, text="Discs from your library that TheDiscDB is missing, or could label better. "
                             "Submitting and suggesting edits need your own GitHub account on thediscdb.com.",
                  style="DeskSub.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(2, 10))
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill="both", expand=True)
        self.lists = {}
        for key, cols in (("submit", self.SUBMIT_COLS), ("improve", self.IMPROVE_COLS)):
            page = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(page, text="")
            box = ttk.Frame(page)
            box.pack(fill="both", expand=True)
            tv = ttk.Treeview(box, columns=[c for c, _, _ in cols], show="headings", height=12, selectmode="browse")
            for c, head, w in cols:
                tv.heading(c, text=head, anchor="w")
                tv.column(c, width=w, anchor="w", stretch=c in ("title", "disc"))
            ys = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=ys.set)
            tv.pack(side="left", fill="both", expand=True)
            ys.pack(side="right", fill="y")
            tv.bind("<<TreeviewSelect>>", lambda e, k=key: self.selected_changed(k))
            note = ttk.Label(page, text="", style="Sub.TLabel", wraplength=640, justify="left")
            note.pack(anchor="w", pady=(8, 6))
            btns = ttk.Frame(page)
            btns.pack(fill="x")
            self.lists[key] = {"tv": tv, "note": note, "rows": {}, "btns": btns, "page": page}
        s_btns, i_btns = self.lists["submit"]["btns"], self.lists["improve"]["btns"]
        self.copy_s = ttk.Button(s_btns, text="Copy fingerprint", style="Small.TButton",
                                 command=lambda: self.copy("submit"))
        self.copy_s.pack(side="left")
        self.packet_btn = ttk.Button(s_btns, text="Build packet…", style="Small.TButton", command=self.build_packet)
        self.packet_btn.pack(side="left", padx=(8, 0))
        self.label_s = ttk.Button(s_btns, text="Label titles…", style="Small.TButton",
                                  command=lambda: self.label("submit"))
        self.label_s.pack(side="left", padx=(8, 0))
        th.link(s_btns, "thediscdb.com/contribute", lambda: webbrowser.open("https://thediscdb.com/contribute"),
                style="Link.TLabel", icon="external", right=True).pack(side="right")
        self.copy_i = ttk.Button(i_btns, text="Copy fingerprint", style="Small.TButton",
                                 command=lambda: self.copy("improve"))
        self.copy_i.pack(side="left")
        self.page_btn = ttk.Button(i_btns, text="Open its TheDiscDB page", style="Small.TButton",
                                   command=self.open_page)
        self.page_btn.pack(side="left", padx=(8, 0))
        self.label_i = ttk.Button(i_btns, text="Label titles…", style="Small.TButton",
                                  command=lambda: self.label("improve"))
        self.label_i.pack(side="left", padx=(8, 0))
        foot = ttk.Frame(body, style="Desk.TFrame")
        foot.pack(fill="x", pady=(10, 0))
        self.status = ttk.Label(foot, text="Looking through your library…", style="DeskSub.TLabel")
        self.status.pack(side="left")
        ttk.Button(foot, text="Close", command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())
        for k in self.lists:
            self.selected_changed(k)
        top.update_idletasks()
        top.minsize(top.winfo_reqwidth(), min(top.winfo_reqheight(), top.winfo_screenheight() - 120))
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
                di.logger.exception("Help TheDiscDB lists")
                self.app.post(lambda: self.show([], [], check_online, str(exc)))
        threading.Thread(target=work, daemon=True).start()
        if check_online:
            self.status.config(text="Checking thediscdb.com for newer entries…")

    def show(self, sub, imp, checked_online, err):
        if not self.top.winfo_exists():
            return
        self.fill("submit", sub, lambda r: (r["title"], r["format"] or "", r["label"] or "", self.READY.get(r["ready"], r["ready"])))
        self.fill("improve", imp, lambda r: (r["title"], "%s, %s" % (r["disc"], r["release"]) if r.get("disc") else
                                             r.get("release") or "", r["what"]))
        self.tabs.tab(0, text="NOT IN THEDISCDB (%d)" % len(sub))
        self.tabs.tab(1, text="COULD BE LABELLED (%d)" % len(imp))
        if err:
            self.status.config(text="Couldn't read your library: %s" % err)
        elif checked_online or self.app.scan_args().no_discdb_online:
            self.status.config(text="Up to date with %s." % ("thediscdb.com" if checked_online
                                                             else "your downloaded TheDiscDB data"))
        else:
            self.load(online=True)              # now the slower check, for anything added since

    def fill(self, key, rows, cells):
        l = self.lists[key]
        keep = (l["tv"].selection() or [None])[0]
        l["tv"].delete(*l["tv"].get_children())
        l["rows"] = {}
        for r in rows:
            iid = r["hash"]
            l["tv"].insert("", "end", iid=iid, values=cells(r))
            l["rows"][iid] = r
        if keep in l["rows"]:
            l["tv"].selection_set(keep)
        self.selected_changed(key)

    def current(self, key):
        l = self.lists[key]
        sel = l["tv"].selection()
        return l["rows"].get(sel[0]) if sel else None

    def selected_changed(self, key):
        r = self.current(key)
        l = self.lists[key]
        if key == "submit":
            for b in (self.copy_s, self.packet_btn, self.label_s):
                b.config(state="normal" if r else "disabled")
            if not r:
                text = "Pick a disc to see what's needed. Discs you've confirmed are ready to submit."
            else:
                text = "%s. Fingerprint %s%s. A packet needs MakeMKV to read the disc: %s." % (
                    r["why"][0].upper() + r["why"][1:], r["hash"][:8],
                    (", mastered %s" % r["mastered"]) if r.get("mastered") else "",
                    "a MakeMKV log is saved for it" if r["makemkv_log"] else "put the disc in the drive first")
        else:
            self.copy_i.config(state="normal" if r else "disabled")
            self.label_i.config(state="normal" if r else "disabled")
            self.page_btn.config(state="normal" if r and r.get("page") else "disabled")
            if not r:
                text = "Pick a disc. Its unlabelled titles are ones your scan found that TheDiscDB hasn't named."
            else:
                t = r["titles"]
                ids = t["feature"] + t["episode"] + t["extra"]
                text = "Unlabelled on TheDiscDB: %s. Watch them, then use Suggest edits on the disc's page." % (
                    ", ".join(ids[:8]) + (" and %d more" % (len(ids) - 8) if len(ids) > 8 else ""))
        l["note"].config(text=text)

    # ---- actions ----
    def copy(self, key):
        r = self.current(key)
        if r:
            self.top.clipboard_clear()
            self.top.clipboard_append(r["hash"])
            self.status.config(text="Fingerprint copied: %s" % r["hash"])

    def label(self, key):
        r = self.current(key)
        if r:
            LabelDialog(self.top, self.app, r["hash"], r["title"], on_save=lambda: self.load(online=False))

    def open_page(self):
        r = self.current("improve")
        if r and r.get("page"):
            webbrowser.open(r["page"])

    def build_packet(self):
        r = self.current("submit")
        if not r:
            return
        h = r["hash"]
        stored = di.makemkv_log_store(h)
        in_drive = bool(self.app.rep and self.app.rep.get("content_hash") == h)
        exe = self.app.makemkv_exe()
        if not os.path.exists(stored) and not (in_drive and exe):
            messagebox.showinfo("Help TheDiscDB", "A packet needs MakeMKV's reading of the disc.\n\n" + (
                "Put this disc in the drive, scan it, then build the packet." if exe else
                "Install MakeMKV (or set it in Settings), then scan this disc and build the packet."),
                parent=self.top)
            return
        args = self.app.scan_args()
        out = os.path.join(os.path.dirname(args.db), "packets", "thediscdb_%s" % h[:8])
        drive = self.app.source.get()
        self.packet_btn.config(state="disabled")
        self.status.config(text="Building the packet%s…" % ("" if os.path.exists(stored) else
                                                            " (MakeMKV is reading the disc; this can take a minute)"))

        def work():
            try:
                if os.path.exists(stored):
                    with open(stored, encoding="utf-8", errors="replace") as fh:
                        log_text = fh.read()
                else:
                    log_text = subprocess.run([exe, "-r", "--cache=1", "info", makemkv_source(exe, drive)],
                                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                                              timeout=900, creationflags=NO_WINDOW).stdout
                    if "TINFO:" not in log_text:
                        raise RuntimeError("MakeMKV returned no titles (is the disc in the drive?)")
                    os.makedirs(os.path.dirname(stored), exist_ok=True)
                    with open(stored, "w", encoding="utf-8") as fh:          # next time: no need to read again
                        fh.write(log_text)
                con = di.library_open(args.db)
                pk = di.build_packet(con, h, log_text, out)
                con.close()
                self.app.post(lambda: self.packet_done(out, pk, None))
            except (Exception, SystemExit) as exc:
                di.logger.exception("packet for %s", h[:8])
                self.app.post(lambda: self.packet_done(out, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def packet_done(self, out, pk, err):
        if not self.top.winfo_exists():
            return
        self.packet_btn.config(state="normal")
        if err:
            self.status.config(text="Couldn't build the packet: %s" % err)
            return
        self.status.config(text="Packet ready: %d of %d titles matched. Open checklist.md in it and follow it." % (
            pk["real_matched"], pk["real_titles"]))
        if os.name == "nt":
            os.startfile(out)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", out])


class SettingsDialog:
    """Scan ▾ > Settings: disc data, pictures / TMDB key, MakeMKV."""

    def __init__(self, root, app):
        self.app = app
        st = app.settings
        self.top = top = new_dialog(root)
        top.title("%s settings" % APP)
        top.transient(root)
        th.color_title_bar(top)
        top.resizable(False, False)
        outer = ttk.Frame(top, padding=(16, 14, 16, 16))
        outer.pack(fill="both", expand=True)
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        f, r = None, 0

        def tab(name):
            """Start a new tab; the rows that follow go on it."""
            nonlocal f, r
            f = ttk.Frame(self.tabs, padding=(14, 12, 14, 14))
            f.columnconfigure(1, weight=1)
            self.tabs.add(f, text=name.upper())
            r = 0

        def head(text):
            nonlocal r
            ttk.Label(f, text=text, style="Plan.TLabel").grid(row=r, column=0, columnspan=3, sticky="w",
                                                              pady=(0 if r == 0 else 14, 4))
            r += 1

        tab("Disc data")
        head("Disc data")
        self.status = {}
        for key, name in (("discdb", "TheDiscDB"), ("imdb", "IMDb")):
            ttk.Label(f, text=name).grid(row=r, column=0, sticky="w", padx=(0, 12))
            self.status[key] = ttk.Label(f, text="", style="Sub.TLabel")
            self.status[key].grid(row=r, column=1, sticky="w")
            ttk.Button(f, text="Update now", style="Small.TButton",
                       command=lambda k=key: self.update(k)).grid(row=r, column=2, sticky="e", pady=3)
            r += 1
        self.online = tk.BooleanVar(value=st.get("discdb_online", True))
        ttk.Checkbutton(f, text="Also check thediscdb.com when scanning (newest additions)",
                        variable=self.online).grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        r += 1
        self.prog = DataProgress(f)
        self.prog_row = r
        r += 1
        head("Help TheDiscDB")
        ttk.Label(f, text="Discs from your library that TheDiscDB is missing, or could label better.",
                  style="Sub.TLabel", wraplength=300, justify="left").grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Show discs…", style="Small.TButton",
                   command=lambda: HelpDiscDBDialog(self.top, self.app)).grid(row=r, column=2, sticky="e")
        r += 1

        tab("Pictures")
        head("Pictures")
        self.pictures = tk.BooleanVar(value=st.get("pictures", True))
        ttk.Checkbutton(f, text="Show posters and box art", variable=self.pictures).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(f, text="TMDB key").grid(row=r, column=0, sticky="w", padx=(0, 12), pady=(6, 0))
        self.key = tk.StringVar(value=st.get("tmdb_key", ""))
        ttk.Entry(f, textvariable=self.key, show="•", width=38).grid(row=r, column=1, sticky="we", pady=(6, 0))
        ttk.Button(f, text="Test", command=self.test_key).grid(row=r, column=2, sticky="e", padx=(6, 0), pady=(6, 0))
        r += 1
        self.key_note = ttk.Label(f, text="Optional: posters TheDiscDB doesn't have, and a fallback search. "
                                         "Get a free key at themoviedb.org", style="Sub.TLabel",
                                  cursor="hand2", wraplength=380, justify="left")
        self.key_note.grid(row=r, column=1, columnspan=2, sticky="w")
        self.key_note.bind("<Button-1>", lambda e: webbrowser.open("https://www.themoviedb.org/settings/api"))
        r += 1

        head("AI detection assistance")
        ttk.Label(f, text="Gemini key").grid(row=r, column=0, sticky="w", padx=(0, 12))
        self.gkey = tk.StringVar(value=st.get("gemini_key", ""))
        ttk.Entry(f, textvariable=self.gkey, show="•", width=38).grid(row=r, column=1, sticky="we")
        ttk.Button(f, text="Test", command=self.test_gemini).grid(row=r, column=2, sticky="e", padx=(6, 0))
        r += 1
        self.gkey_note = ttk.Label(f, text="Optional: when nothing identifies a disc that has pictures of its "
                                          "own, Gemini looks at them and IMDb checks its answer. The picture and the "
                                          "disc's clues go to Google (on the free tier Google may use them to improve "
                                          "its products). Get a free key at Google AI Studio",
                                   style="Sub.TLabel", cursor="hand2", wraplength=380, justify="left")
        self.gkey_note.grid(row=r, column=1, columnspan=2, sticky="w")
        self.gkey_note.bind("<Button-1>", lambda e: webbrowser.open("https://aistudio.google.com/apikey"))
        r += 1

        tab("Ripping")
        head("TV episodes")
        self.order = tk.StringVar(value=st.get("episode_order", "aired"))
        for value, text in (("aired", "Name files in aired order (Plex's default)"),
                            ("dvd", "Name files in DVD order (set the show to DVD order in Plex)")):
            ttk.Radiobutton(f, text=text, value=value, variable=self.order).grid(
                row=r, column=0, columnspan=3, sticky="w")
            r += 1
        ttk.Label(f, text="Episodes are matched to the disc in DVD order either way. Episode lists "
                          "from TVmaze (tvmaze.com); applies to the next scan.",
                  style="Sub.TLabel", wraplength=420, justify="left").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        head("MakeMKV")
        self.mkv = tk.StringVar(value=st.get("makemkv", ""))
        ttk.Entry(f, textvariable=self.mkv, width=38).grid(row=r, column=0, columnspan=2, sticky="we")
        ttk.Button(f, text="Browse…", command=self.browse).grid(row=r, column=2, sticky="e", padx=(6, 0))
        r += 1
        self.mkv_note = ttk.Label(f, text="", style="Sub.TLabel")
        self.mkv_note.grid(row=r, column=0, columnspan=3, sticky="w")
        self.mkv.trace_add("write", lambda *a: self.refresh_mkv())
        r += 1

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Close", command=self.close).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", self.close)
        centre_over(top, root)
        self.refresh_status()
        self.refresh_mkv()
        for var in (self.online, self.pictures, self.key, self.gkey, self.mkv, self.order):   # saved as you go
            var.trace_add("write", lambda *a: self.save())

    def refresh_status(self):
        args = self.app.scan_args()
        d = di.discdb_index_info(args.discdb_dir or di.default_discdb_dir())
        self.status["discdb"].config(text="%s discs · built %s" % (human_count(_n(d.get("discs"))), d.get("built", "")[:10])
                                     if d else "not downloaded yet")
        i = di.imdb_index_info(args.imdb_dir)
        self.status["imdb"].config(text=("%s titles · built %s%s" % (human_count(i["titles"]), i["built"],
                                                                    "" if i["current"] else " (rebuild needed)"))
                                   if i else "not downloaded yet")

    def refresh_mkv(self):
        p = self.mkv.get().strip()
        if p:
            self.mkv_note.config(text="Using this file" if os.path.isfile(p) else "File not found")
        else:
            found = di.find_makemkvcon()
            self.mkv_note.config(text=("Found automatically: %s" % found) if found else
                                 "Not found. Install MakeMKV, or browse to makemkvcon.")

    def browse(self):
        p = filedialog.askopenfilename(title="Find makemkvcon", parent=self.top)
        if p:
            self.mkv.set(os.path.normpath(p))

    def test_key(self):
        key = self.key.get().strip()
        if not key:
            self.key_note.config(text="Enter a key first.")
            return
        self.key_note.config(text="Testing…")

        def work():
            try:
                di.tmdb_get(key, "/configuration")
                msg = "Key works."
            except Exception as exc:
                msg = "That key didn't work: %s" % exc
            self.app.post(lambda: self.top.winfo_exists() and self.key_note.config(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def test_gemini(self):
        key = self.gkey.get().strip()
        if not key:
            self.gkey_note.config(text="Enter a key first.")
            return
        self.gkey_note.config(text="Testing…")

        def work():
            try:
                text, model = di.gemini_ask(key, "Reply with the single word OK.")
                msg = "Key works (%s)." % model
            except Exception as exc:
                msg = "That key didn't work: %s" % exc
            self.app.post(lambda: self.top.winfo_exists() and self.gkey_note.config(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def update(self, which):
        if self.app.busy:
            return
        self.save()
        self.prog.frame.grid(row=self.prog_row, column=0, columnspan=3, sticky="we", pady=(8, 0))
        self.prog.message("Starting…")
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
        self.prog.text.config(text=("Update failed: %s" % err) if err else "Up to date.")
        self.refresh_status()

    def save(self):
        st = self.app.settings
        st["discdb_online"] = bool(self.online.get())
        st["pictures"] = bool(self.pictures.get())
        st["tmdb_key"] = self.key.get().strip()
        st["gemini_key"] = self.gkey.get().strip()
        st["makemkv"] = self.mkv.get().strip()
        st["episode_order"] = self.order.get()
        save_settings(st)

    def close(self):
        self.save()
        if self.app.data_ui is self:     # keep the download going; report to the status line
            self.app.data_ui = None
        self.top.destroy()
        if self.app.rep and not self.app.busy:   # MakeMKV may have just been set: refresh Rip
            self.app.show_plan_summary()


def _n(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _log_uncaught(kind):
    def hook(*exc):
        di.logger.error("uncaught error (%s)", kind, exc_info=exc[-3:] if len(exc) >= 3 else None)
    return hook


def main():
    try:
        di.setup_logging()
    except OSError:
        pass
    di.logger.info("Disc Detective %s started", di.VERSION)
    sys.excepthook = _log_uncaught("main")
    threading.excepthook = lambda a: di.logger.error("uncaught error in a background thread",
                                                     exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    root = tk.Tk()

    def tk_error(exc, val, tb):               # errors in button handlers etc.
        di.logger.error("error in the app", exc_info=(exc, val, tb))
        messagebox.showerror(APP, "%s: %s\n\nDetails are in errors.log (Open logs folder, at the bottom of the window)."
                             % (exc.__name__, val))
    root.report_callback_exception = tk_error
    App(root)
    root.mainloop()
    di.logger.info("Disc Detective closed")


if __name__ == "__main__":
    main()
