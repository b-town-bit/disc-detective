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
import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import core as di

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
                 "Interview": "Interviews", "BehindTheScenes": "Behind The Scenes"}


def clean(s):
    return re.sub(r'[<>"/\\|?*]', "", re.sub(r"\s*:\s*", " - ", s or "")).strip()


def plan_rip(rep, mk_titles, include_extras, selected=None):
    """Which MakeMKV titles to rip and what to call them (Plex layout)."""
    ours = [{"title_id": t["id"], "role": t["role"], "duration": t["duration"],
             "clips": t.get("clip_list"), "episode_index": t.get("episode_index"), "edition": t.get("edition")}
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
    roles = RIP_ROLES + (("extra",) if include_extras else ())
    plan = []
    for m in mk_titles:
        t, _ = pairs.get(m["index"], (None, None))
        item = known.get((m["source"] or "").lower()) or {}
        if not item and t and t.get("clips") and "," not in t["clips"]:
            item = known.get(t["clips"].lower() + ".m2ts") or {}      # playlist of one clip
        role = t["role"] if t else {"MainMovie": "feature", "Episode": "episode"}.get(item.get("type"), "extra" if item else None)
        if selected is not None:
            if not t or t["title_id"] not in selected:
                continue
        elif role not in roles:
            continue
        if role == "episode" and item.get("season") and item.get("episode"):
            dest = "%s/Season %02d/%s - s%02de%02d - %s.mkv" % (base, int(item["season"]), show, int(item["season"]),
                                                               int(item["episode"]), clean(item.get("title")))
        elif t and t["title_id"] in names:
            dest = names[t["title_id"]]
        elif role == "extra":
            folder = EXTRA_FOLDERS.get(item.get("type"), "Other")
            label = clean(item.get("title")) or "%s - %s" % (show, (t["title_id"] if t else m["source"]).replace(".", "_"))
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
    if not plan and mk_titles:   # nothing classified: fall back to the longest title
        m = max(mk_titles, key=lambda x: x["duration"] or 0)
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

class SplitButton(ttk.Frame):
    """A big button with a small ▾ menu attached (click = default action)."""

    def __init__(self, parent, text, command):
        super().__init__(parent)
        self.button = ttk.Button(self, text=text, style="Big.TButton", command=command)
        self.button.pack(side="left", fill="y")
        self.menu = tk.Menu(self, tearoff=False)
        # a narrow plain button that pops the menu (themes force a wide minimum on Menubuttons)
        self.arrow = ttk.Button(self, text="▾", style="Arrow.TButton", width=2, command=self.popup)
        self.arrow.pack(side="left", fill="y")

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


class App:
    SCAN_TEXT = "◎  Scan Disc"
    RIP_TEXT = "⬇  Rip"

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
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
        root.minsize(560, 0)
        self._style()
        self.source = tk.StringVar(value=self.settings.get("drive", ""))
        self.outdir = tk.StringVar(value=self.settings.get("outdir", os.path.join(os.path.expanduser("~"), "Videos", "Rips")))
        self.extras = tk.BooleanVar(value=self.settings.get("extras", False))
        self._build()
        self.refresh_drives(initial=True)
        di.status_hook = lambda msg: self.post(self.set_status, msg)   # identifier progress -> status line
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
        st = ttk.Style()
        if "vista" in st.theme_names():
            st.theme_use("vista")
        elif "clam" in st.theme_names():
            st.theme_use("clam")
            st.configure("Horizontal.TProgressbar", background="#2e7d32", troughcolor="#d9d9d9")
        st.configure("Big.TButton", font=("Segoe UI", 13, "bold"), padding=(22, 16))
        st.configure("Arrow.TButton", font=("Segoe UI", 12), padding=(2, 16))
        st.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        st.configure("Info.TLabel", font=("Segoe UI", 10))
        st.configure("Plan.TLabel", font=("Segoe UI", 10, "bold"))
        st.configure("Sub.TLabel", foreground="#666666")
        st.configure("Cap.TLabel", foreground="#777777", font=("Segoe UI", 8))
        st.configure("Small.TButton", font=("Segoe UI", 9), padding=(6, 1))
        st.configure("Detail.TLabel", font=("Segoe UI", 8))
        st.configure("Picks.Treeview", rowheight=di.ART_PICK[1] + 6)

    def _build(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=14, pady=(14, 8))
        self.scan_btn = SplitButton(top, self.SCAN_TEXT, self.scan)
        self.scan_btn.pack(side="left")
        self.rip_btn = SplitButton(top, self.RIP_TEXT, self.rip_or_cancel)
        self.rip_btn.pack(side="left", padx=(12, 0))
        self.rip_btn.set_state(False)
        self._build_rip_menu()

        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=16, pady=(4, 0))
        # picture column (poster / box art / disc art); hidden when there is nothing to show
        self.pic_col = ttk.Frame(row)
        self.pic_lbl = ttk.Label(self.pic_col)
        self.pic_lbl.pack(anchor="n")
        self.pic_cap = ttk.Label(self.pic_col, text="", style="Cap.TLabel")
        self.pic_cap.pack(anchor="n", pady=(3, 0))
        self._imgs = []                 # Tk drops images that nothing references
        info = self.info = ttk.Frame(row)
        info.pack(side="left", fill="x", expand=True, anchor="n")
        self.title_lbl = ttk.Label(info, text="Insert a disc and press Scan Disc", style="Title.TLabel")
        self.title_lbl.pack(anchor="w")
        self.facts_lbl = ttk.Label(info, text="", style="Info.TLabel")
        self.facts_lbl.pack(anchor="w", pady=(2, 0))
        self.details = ttk.Frame(info)          # small label / value table under the facts
        self.details.pack(anchor="w", pady=(4, 0))
        # "Not right?": other matches, typing the answer in, or confirming this one
        self.match_row = ttk.Frame(info)
        self.confirm_btn = ttk.Button(self.match_row, text="✓  That's right", style="Small.TButton", command=self.confirm)
        self.confirmed_lbl = ttk.Label(self.match_row, text="✓ In your library", style="Cap.TLabel")
        self.match_btn = ttk.Button(self.match_row, text="Not right? ▾", style="Small.TButton", command=self.match_menu)
        self.match_popup = tk.Menu(self.root, tearoff=0)
        self.plan_lbl = ttk.Label(info, text="", style="Plan.TLabel", wraplength=520, justify="left")
        self.plan_lbl.pack(anchor="w", pady=(8, 0))
        self.dest_lbl = ttk.Label(info, text="", style="Sub.TLabel", wraplength=520, justify="left")
        self.dest_lbl.pack(anchor="w")
        self.strip = ttk.Frame(info)    # short-film collections: each film's poster and name

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=14, pady=(10, 12))
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.status = ttk.Label(bottom, text="", style="Sub.TLabel")
        self.status.pack(anchor="w")
        self.root.bind("<Configure>", self._rewrap)

    def _rewrap(self, event=None):
        if event is None or event.widget is self.root:
            w = max(300, (event.width if event else self.root.winfo_width()) - 40
                    - (self.pic_col.winfo_reqwidth() + 14 if self.pic_col.winfo_ismapped() else 0))
            self.plan_lbl.config(wraplength=w)
            self.dest_lbl.config(wraplength=w)

    def _build_scan_menu(self, drives):
        m = self.scan_btn.menu
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
            m.add_radiobutton(label="Folder: %s" % cur, value=cur, variable=self.source, command=self.source_changed)
        m.add_separator()
        m.add_command(label="Choose folder…", command=self.pick_source)
        m.add_command(label="Refresh drives", command=self.refresh_drives)
        m.add_separator()
        m.add_command(label="Settings…", command=self.open_settings)
        m.add_command(label="Open logs folder", command=self.open_logs)
        m.add_command(label="About / credits", command=self.about)

    def about(self):
        messagebox.showinfo(APP, "Disc Detective %s · MIT License\n\n" % di.VERSION +
                            "Disc identification: TheDiscDB (thediscdb.com) and IMDb's non-commercial "
                            "datasets (imdb.com), with personal use per their terms.\n\n"
                            "Rips with MakeMKV (makemkv.com).\n\n"
                            "Posters and box art: the disc itself, TheDiscDB and, with a key, TMDB.\n"
                            + di.TMDB_CREDIT)

    def _build_rip_menu(self):
        m = self.rip_btn.menu
        m.delete(0, "end")
        m.add_command(label="Rip to: %s…" % short_path(self.outdir.get()), command=self.pick_out)
        m.add_checkbutton(label="Include extras", variable=self.extras, command=self.options_changed)
        m.add_command(label="Choose titles…", command=self.choose_titles,
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
    def set_status(self, text):
        self.status.config(text=text)

    def show_progress(self, on, indeterminate=False):
        if on:
            self.progress.pack(fill="x", before=self.status, pady=(0, 4))
            self.progress.config(mode="indeterminate" if indeterminate else "determinate", maximum=1000, value=0)
            if indeterminate:
                self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def set_busy(self, busy, ripping=False):
        self.busy, self.ripping = busy, ripping
        self.scan_btn.set_state(not busy)
        if ripping:
            self.rip_btn.set_text("■  Cancel")
            self.rip_btn.set_state(True, arrow_enabled=False)
        else:
            self.rip_btn.set_text(self.RIP_TEXT)
            self.rip_btn.set_state(bool(self.rep) and not busy)

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
        if initial or not self.rep:
            self.set_status("Source: %s" % self.source_name())

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
        self.clear_picture()
        self.show_match_button(None)
        self.title_lbl.config(text="Ready to scan")
        self.facts_lbl.config(text=self.source_name())
        self.show_details(None)
        self.plan_lbl.config(text="")
        self.dest_lbl.config(text="")
        self.set_status("Source: %s" % self.source_name())

    # ---- settings ----
    def scan_args(self):
        """Identifier settings from the Settings dialog (a plain dict: safe to read off the Tk thread)."""
        st = self.settings
        return di.default_args(tmdb_key=st.get("tmdb_key") or None, no_art=not st.get("pictures", True),
                               no_discdb_online=not st.get("discdb_online", True))

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
    def show_details(self, rep):
        for w in self.details.winfo_children():
            w.destroy()
        for r, (k, v) in enumerate(detail_rows(rep) if rep else []):
            ttk.Label(self.details, text=k, style="Cap.TLabel").grid(row=r, column=0, sticky="nw", padx=(0, 10))
            ttk.Label(self.details, text=v, style="Detail.TLabel").grid(row=r, column=1, sticky="nw")

    def show_match_button(self, rep):
        for w in (self.confirm_btn, self.confirmed_lbl, self.match_btn):
            w.pack_forget()
        if rep is None:
            self.match_row.pack_forget()
            return
        have = rep.get("match") or rep.get("personal_hit") or (rep.get("discdb") or {}).get("hit")
        if rep.get("personal_hit"):
            self.confirmed_lbl.pack(side="left", padx=(0, 10))
        elif self.confirmable(rep):
            self.confirm_btn.pack(side="left", padx=(0, 6))
        self.match_btn.config(text="Not right? ▾" if have else "Choose the match ▾")
        self.match_btn.pack(side="left")
        self.match_row.pack(anchor="w", pady=(6, 0), before=self.plan_lbl)

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
        self.choose(rep["candidates"][0], confirm=True)

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
        m.add_command(label="Enter it myself…", command=lambda: MatchDialog(
            self.root, self, title=(top or {}).get("name") or rep.get("best_guess", "").split(" | ")[0],
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

    def clear_picture(self):
        self.pic_col.pack_forget()
        self.pic_lbl.config(image="", text="")
        for w in self.strip.winfo_children():
            w.destroy()
        self.strip.pack_forget()
        self._imgs = []
        self._rewrap()

    def show_picture(self, rep):
        self.clear_picture()
        art = rep.get("art") or {}
        img = self._photo(art["image"]) if art.get("image") else None
        if img:
            self.pic_lbl.config(image=img)
            cap = {"disc": "from the disc", "TheDiscDB": "TheDiscDB", "TMDB": "TMDB"}.get(art.get("source"), "")
            what = "poster of the top guess" if art.get("guess") else art.get("what", "picture")
            self.pic_cap.config(text="%s · %s" % (what, cap) if cap else what)
            self.pic_col.pack(side="left", anchor="n", padx=(0, 14), before=self.info)
        elif rep.get("art_note"):            # tell the user why there's no picture
            self.pic_lbl.config(text=rep["art_note"], style="Cap.TLabel", wraplength=140, justify="left")
            self.pic_cap.config(text="")
            self.pic_col.pack(side="left", anchor="n", padx=(0, 14), before=self.info)
        c = (rep.get("candidates") or [None])[0]
        if rep.get("kind") == "collection" and c and c.get("items"):
            for k, it in enumerate(c["items"]):
                cell = ttk.Frame(self.strip)
                cell.grid(row=k // 4, column=k % 4, sticky="nw", padx=(0, 10), pady=(0, 6))
                thumb = self._photo((it.get("art") or {}).get("image")) if it.get("art") else None
                if thumb:
                    ttk.Label(cell, image=thumb).pack(anchor="w")
                ttk.Label(cell, text="%s (%s)" % (re.sub(r"^.*?:\s*", "", it["name"]), it.get("year") or "?"),
                          style="Cap.TLabel", wraplength=110, justify="left").pack(anchor="w")
            self.strip.pack(anchor="w", pady=(10, 0))
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

    # ---- Scan ----
    def scan(self):
        drive = self.source.get().strip()
        if not drive or not os.path.isdir(drive):
            messagebox.showwarning(APP, "No disc found at %s.\n\nInsert a disc, or pick another drive or a "
                                        "folder from the ▾ menu next to Scan Disc." % self.source_name())
            return
        self.remember()
        self.rep = None
        self.selected = None
        self._build_rip_menu()
        self.clear_picture()
        self.show_match_button(None)
        self.title_lbl.config(text="Investigating…")
        self.show_details(None)
        self.facts_lbl.config(text=drive)
        self.plan_lbl.config(text="")
        self.dest_lbl.config(text="")
        self.set_busy(True)
        self.show_progress(True, indeterminate=True)
        self.set_status("Dusting for fingerprints (no video is read)…")
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
            self.post(self.scan_done, rep, None)
        except SystemExit as exc:
            di.logger.warning("scan of %s: %s", drive, exc)
            self.post(self.scan_done, None, str(exc))
        except Exception as exc:   # show it rather than dying silently in a thread
            di.logger.exception("scan of %s failed", drive)
            self.post(self.scan_done, None, "%s: %s" % (type(exc).__name__, exc))

    def scan_done(self, rep, err):
        self.show_progress(False)
        self.set_busy(False)
        if err:
            self.title_lbl.config(text="Couldn't read this disc")
            self.facts_lbl.config(text=err)
            self.show_details(None)
            self.set_status("Scan failed · %s · details in the log (Scan ▾ > Open logs folder)" % self.source_name())
            return
        self.rep = rep
        self.set_busy(False)
        self._build_rip_menu()
        self.title_lbl.config(text=display_title(rep))
        self.show_details(rep)
        facts = rep.get("facts") or {}
        kind = {"tv": "TV", "movie": "Movie", "collection": "Collection"}.get(rep["kind"], "Disc")
        bits = ["%s %s" % (rep["format"], kind)]
        if facts.get("episode_count") and rep["kind"] != "collection":
            bits.append("%d episodes" % facts["episode_count"])
        if facts.get("feature_minutes"):
            bits.append("%d min" % facts["feature_minutes"])
        m = rep.get("match")
        if m and not m.get("sure") and not rep.get("personal_hit") and not (rep.get("discdb") or {}).get("hit"):
            bits.append("best guess")
        else:
            bits.append("identified by %s" % rep.get("identified_by", "?"))
        self.facts_lbl.config(text="  ·  ".join(bits))
        self.show_plan_summary()
        self.show_picture(rep)
        self.show_match_button(rep)
        st = str(rep.get("status", ""))
        if st.startswith(("identified", "probable")):
            msg = "Case closed"
        elif st.startswith("best guess"):
            msg = "Best guess: confirm it with ✓ That's right, or change it with Not right? ▾"
        else:
            msg = "Still a mystery: Choose the match ▾ to say what it is"
        self.set_status("%s · %s" % (msg, self.source_name()))

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
            self.dest_lbl.config(text="into %s" % os.path.join(self.outdir.get(), *common))
        else:
            self.dest_lbl.config(text="")
        self.rip_btn.set_state(bool(plan) and not self.busy)

    # ---- Rip ----
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
        self.set_status("Asking MakeMKV for the disc's titles…")
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
                self.post(self.set_status, "Ripping %d of %d: %s" % (i + 1, len(plan), item["dest"].split("/")[-1]))
                work = os.path.join(out, ".ripping_%s_%d" % (rep["content_hash"][:8], item["mk"]))
                shutil.rmtree(work, ignore_errors=True)
                os.makedirs(work)
                ok = self._run_mkv(exe, source, item["mk"], work, i, len(plan))
                files = [f for f in os.listdir(work) if f.lower().endswith(".mkv")]
                if ok and files and not self.cancelled:
                    dest = unique_path(os.path.join(out, *item["dest"].split("/")))
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
            self.set_status("Rip failed · details in the log (Scan ▾ > Open logs folder)")
            messagebox.showerror(APP, err + "\n\nDetails are in errors.log (Scan ▾ > Open logs folder).")
        elif self.cancelled:
            self.set_status("Cancelled after %d of %d titles" % (len(done), planned))
        else:
            self.set_status("Done: %d of %d titles ripped · ▾ Open rip folder" % (len(done), planned))

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
        self.root.destroy()


def display_title(rep):
    """The big title: just what it is. Name (year), and season / disc for TV."""
    top = (rep.get("candidates") or [None])[0]
    if rep.get("kind") == "collection" and top:
        return top["name"]
    m = rep.get("match")
    if m:
        t = "%s (%s)" % (m["name"], m["year"]) if m.get("year") else m["name"]
        if m["kind"] == "tv" and m.get("season"):
            t += "  ·  Season %s%s" % (m["season"], (", Disc %s" % m["disc"]) if m.get("disc") else "")
        return t
    guess = di.strip_tags(rep.get("best_guess") or "").split(" | ")[0]
    return guess or "Unidentified disc"


def detail_rows(rep):
    """The small print under the title, as (label, value) rows: ids and what the disc itself says."""
    rows = []
    m = rep.get("match") or {}
    if m.get("imdb_id"):
        rows.append(("IMDb", m["imdb_id"]))
    hit = (rep.get("discdb") or {}).get("hit")
    if hit:
        rows.append(("TheDiscDB", " · ".join(x for x in (hit.get("release"), hit.get("disc")) if x)))
    if rep.get("label"):
        rows.append(("Disc label", rep["label"]))
    names = (rep.get("bd_meta") or {}).get("names") or []
    if names and names[0] != rep.get("label"):
        rows.append(("Disc title", names[0]))
    po = rep.get("playlist_obfuscation")
    if isinstance(po, dict):
        rows.append(("Protection", "%d decoy titles · %s %s" % (
            po["decoys"], "the real film is" if po["sure"] else "best guess for the real film:", po["picked"])))
    if rep.get("mastered"):
        rows.append(("Mastered", rep["mastered"]))
    rows.append(("Fingerprint", rep.get("content_hash", "?")[:8]))
    return rows


def short_path(p, n=48):
    return p if len(p) <= n else p[:n // 2 - 2] + "\u2026" + p[-(n // 2 - 1):]


def describe_plan(plan, custom):
    """One plain sentence: what Rip will do."""
    if not plan:
        return "Nothing selected to rip."
    roles = [p["role"] for p in plan]
    eps = [p for p in plan if p["role"] == "episode"]
    extras = sum(1 for r in roles if r == "extra")
    if custom:
        head = "Will rip %d chosen title%s" % (len(plan), "" if len(plan) == 1 else "s")
    elif eps:
        codes = [re.search(r"s(\d+)e(\d+)", p["dest"], re.I) for p in eps]
        codes = ["S%sE%s" % (c.group(1), c.group(2)) for c in codes if c]
        span = (" (%s–%s)" % (codes[0], codes[-1]) if len(codes) > 1 else " (%s)" % codes[0]) if codes else ""
        head = "Will rip %d episode%s%s" % (len(eps), "" if len(eps) == 1 else "s", span)
    elif roles.count("feature") > 1:
        eds = [(re.search(r"\{edition-([^}]*)\}", p["dest"]), p["length"]) for p in plan if p["role"] == "feature"]
        if all(m for m, _ in eds):     # one film in several editions (theatrical + extended, say)
            head = "Will rip %d editions: %s" % (len(eds), ", ".join("%s (%s)" % (m.group(1), n) for m, n in eds))
        else:
            head = "Will rip %d films" % roles.count("feature")
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
        # The wheel scrolls the list wherever the pointer is in this window: bound on the window
        # (every widget's events pass through it), not on the list, whose rows would hide it.
        top = self.winfo_toplevel()
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(seq, self.roll, add="+")

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
            return
        delta = getattr(e, "delta", 0) or 0
        if getattr(e, "num", None) == 4 or delta > 0:
            step = -1
        elif getattr(e, "num", None) == 5 or delta < 0:
            step = 1
        else:
            return
        lines = max(1, abs(delta) // 120) if delta else 1      # Windows: 120 a notch; mac: small deltas
        self.canvas.yview_scroll(step * 3 * lines, "units")
        return "break"


class TitlePicker:
    """Rip menu > Choose titles: tick exactly which titles to rip."""

    def __init__(self, root, app):
        self.app = app
        rep = app.rep
        self.top = top = tk.Toplevel(root)
        top.title("Choose titles")
        top.transient(root)
        top.grab_set()
        ttk.Label(top, text="Tick the titles to rip. Play-alls and duplicates repeat other titles.",
                  style="Sub.TLabel").pack(anchor="w", padx=12, pady=(12, 6))
        default = {p["title_id"] for p in plan_rip(rep, preview_titles(rep), app.extras.get())}
        current = app.selected if app.selected is not None else default
        order = {"feature": 0, "episode": 1, "extra": 2, "version": 3, "play-all": 4, "duplicate": 5}
        rows = sorted((t for t in rep["titles"] if t["role"] in order),
                      key=lambda t: (order[t["role"]], t.get("episode_index") or 0, di._natural(t["id"])))
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", padx=12, pady=12)     # packed first: always on screen
        self.area = ScrollArea(top)
        self.area.pack(fill="both", expand=True, padx=12)
        body = self.area.inner
        names = {}
        everything = {t["id"] for t in rows}
        for p in plan_rip(rep, preview_titles(rep), True, selected=everything):
            names[p["title_id"]] = p["dest"].split("/")[-1]
        for col, head in enumerate(("", "Title", "", "Length", "Video", "Saves as")):
            ttk.Label(body, text=head, style="Sub.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 10))
        self.vars = {}
        for r, t in enumerate(rows, 1):
            v = tk.BooleanVar(value=t["id"] in current)
            self.vars[t["id"]] = v
            label = ("Episode %d" % t["episode_index"]) if t.get("episode_index") else t["role"].title()
            if t["role"] == "version" and t.get("edition"):
                label = "Version: %s" % t["edition"]
            ttk.Checkbutton(body, variable=v).grid(row=r, column=0, sticky="w")
            ttk.Label(body, text=label).grid(row=r, column=1, sticky="w", padx=(0, 10))
            ttk.Label(body, text=t["id"], style="Sub.TLabel").grid(row=r, column=2, sticky="w", padx=(0, 10))
            ttk.Label(body, text=di.hms(t["duration"])).grid(row=r, column=3, sticky="e", padx=(0, 10))
            ttk.Label(body, text=t.get("video") or "", style="Sub.TLabel").grid(row=r, column=4, sticky="w",
                                                                                padx=(0, 10))
            ttk.Label(body, text=names.get(t["id"], ""), style="Sub.TLabel").grid(row=r, column=5, sticky="w")
        ttk.Button(btns, text="OK", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right", padx=6)
        ttk.Button(btns, text="Default", command=lambda: [v.set(k in default) for k, v in self.vars.items()]
                   ).pack(side="left")

    def ok(self):
        chosen = {k for k, v in self.vars.items() if v.get()}
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
        self.top = top = tk.Toplevel(root)
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
            self.prog.text.config(text="Couldn't finish: %s\nYou can retry from Scan ▾ > Settings." % err)
            self.later.config(text="Close", state="normal")
        else:
            self.prog.text.config(text="Ready. Insert a disc and press Scan Disc.")
            self.later.config(text="Close", state="normal")
            self.top.after(1500, self.top.destroy)

    def close(self):
        if not self.app.data_ui:       # don't orphan a running download's progress
            self.top.destroy()


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
        self.top = top = tk.Toplevel(root)
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
        self.top = top = tk.Toplevel(root)
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


class SettingsDialog:
    """Scan ▾ > Settings: disc data, pictures / TMDB key, MakeMKV."""

    def __init__(self, root, app):
        self.app = app
        st = app.settings
        self.top = top = tk.Toplevel(root)
        top.title("%s settings" % APP)
        top.transient(root)
        top.resizable(False, False)
        f = ttk.Frame(top, padding=16)
        f.pack(fill="both")
        f.columnconfigure(1, weight=1)
        r = 0

        def head(text):
            nonlocal r
            ttk.Label(f, text=text, style="Plan.TLabel").grid(row=r, column=0, columnspan=3, sticky="w",
                                                              pady=(0 if r == 0 else 14, 4))
            r += 1

        head("Disc data")
        self.status = {}
        for key, name in (("discdb", "TheDiscDB"), ("imdb", "IMDb")):
            ttk.Label(f, text=name).grid(row=r, column=0, sticky="w", padx=(0, 12))
            self.status[key] = ttk.Label(f, text="", style="Sub.TLabel")
            self.status[key].grid(row=r, column=1, sticky="w")
            ttk.Button(f, text="Update now", command=lambda k=key: self.update(k)).grid(row=r, column=2, sticky="e")
            r += 1
        self.online = tk.BooleanVar(value=st.get("discdb_online", True))
        ttk.Checkbutton(f, text="Also check thediscdb.com when scanning (newest additions)",
                        variable=self.online).grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        r += 1
        self.prog = DataProgress(f)
        self.prog_row = r
        r += 1

        head("Pictures")
        self.pictures = tk.BooleanVar(value=st.get("pictures", True))
        ttk.Checkbutton(f, text="Show posters and box art", variable=self.pictures).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(f, text="TMDB key").grid(row=r, column=0, sticky="w", padx=(0, 12), pady=(6, 0))
        self.key = tk.StringVar(value=st.get("tmdb_key", ""))
        ttk.Entry(f, textvariable=self.key, show="•", width=38).grid(row=r, column=1, sticky="we", pady=(6, 0))
        ttk.Button(f, text="Test", command=self.test_key).grid(row=r, column=2, sticky="e", pady=(6, 0))
        r += 1
        self.key_note = ttk.Label(f, text="Optional: posters TheDiscDB doesn't have, and a fallback search. "
                                         "Get a free key at themoviedb.org", style="Sub.TLabel",
                                  cursor="hand2", wraplength=380, justify="left")
        self.key_note.grid(row=r, column=1, columnspan=2, sticky="w")
        self.key_note.bind("<Button-1>", lambda e: webbrowser.open("https://www.themoviedb.org/settings/api"))
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

        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Close", command=self.close).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_status()
        self.refresh_mkv()
        for var in (self.online, self.pictures, self.key, self.mkv):   # saved as you go, not only on Close
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
        st["makemkv"] = self.mkv.get().strip()
        save_settings(st)

    def close(self):
        self.save()
        if self.app.data_ui is self:     # keep the download going; report to the status line
            self.app.data_ui = None
        self.top.destroy()


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
        messagebox.showerror(APP, "%s: %s\n\nDetails are in errors.log (Scan ▾ > Open logs folder)."
                             % (exc.__name__, val))
    root.report_callback_exception = tk_error
    App(root)
    root.mainloop()
    di.logger.info("Disc Detective closed")


if __name__ == "__main__":
    main()
