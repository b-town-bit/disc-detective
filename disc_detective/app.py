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
             "clips": t.get("clip_list"), "episode_index": t.get("episode_index")}
            for t in rep.get("titles") or []]
    pairs = di.map_makemkv(mk_titles, ours, rep["format"])
    names = {}
    for n in rep.get("plex_names") or []:
        src, _, name = n.partition(" -> ")
        names[src.strip()] = name.strip()
    # TheDiscDB knows exactly what each title is: real extra names, exact episodes.
    hit = (rep.get("discdb") or {}).get("hit") or {}
    known = {str(t.get("source") or "").lower(): t.get("item") or {} for t in hit.get("titles") or []}
    cand = (rep.get("candidates") or [None])[0] if di.is_probable(rep.get("candidates")) else None
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
        self.plan_lbl = ttk.Label(info, text="", style="Plan.TLabel", wraplength=520, justify="left")
        self.plan_lbl.pack(anchor="w", pady=(8, 0))
        self.dest_lbl = ttk.Label(info, text="", style="Sub.TLabel", wraplength=520, justify="left")
        self.dest_lbl.pack(anchor="w")
        self.strip = ttk.Frame(info)    # short-film collections: one small poster per film

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
        self.title_lbl.config(text="Ready to scan")
        self.facts_lbl.config(text=self.source_name())
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
            self.pic_cap.config(text="%s · %s" % (art.get("what", "picture"), cap) if cap else "")
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
                ttk.Label(cell, text="%s\n%s (%s)" % (it["title_id"], re.sub(r"^.*?:\s*", "", it["name"]),
                                                        it.get("year") or ""),
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
        self.title_lbl.config(text="Investigating…")
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
            self.set_status("Scan failed · %s · details in the log (Scan ▾ > Open logs folder)" % self.source_name())
            return
        self.rep = rep
        self.set_busy(False)
        self._build_rip_menu()
        top = (rep.get("candidates") or [None])[0]
        title = top["name"] if rep.get("kind") == "collection" and top else rep.get("best_guess")
        self.title_lbl.config(text=(title or "Unidentified disc").split(" | ")[0])
        facts = rep.get("facts") or {}
        kind = {"tv": "TV", "movie": "Movie", "collection": "Collection"}.get(rep["kind"], "Disc")
        bits = ["%s %s" % (rep["format"], kind)]
        if facts.get("episode_count") and rep["kind"] != "collection":
            bits.append("%d episodes" % facts["episode_count"])
        if facts.get("feature_minutes"):
            bits.append("%d min" % facts["feature_minutes"])
        bits.append("identified by %s" % rep.get("identified_by", "?"))
        self.facts_lbl.config(text="  ·  ".join(bits))
        self.show_plan_summary()
        self.show_picture(rep)
        solved = str(rep.get("status", "")).startswith(("identified", "probable"))
        self.set_status("%s · %s · fingerprint %s" % ("Case closed" if solved else "Still a mystery: check the "
                                                      "answer before ripping", self.source_name(), rep["content_hash"][:8]))

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
        head = "Will rip %d films" % roles.count("feature")
    elif "feature" in roles or "longest" in roles:
        f = next(p for p in plan if p["role"] in ("feature", "longest"))
        head = "Will rip the %s (%s)" % ("feature" if f["role"] == "feature" else "longest title", f["length"])
    else:
        head = "Will rip %d title%s" % (len(plan), "" if len(plan) == 1 else "s")
    if extras and not custom:
        head += " + %d extra%s" % (extras, "" if extras == 1 else "s")
    return head


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
        body = ttk.Frame(top)
        body.pack(fill="both", expand=True, padx=12)
        names = {}
        everything = {t["id"] for t in rows}
        for p in plan_rip(rep, preview_titles(rep), True, selected=everything):
            names[p["title_id"]] = p["dest"].split("/")[-1]
        for col, head in enumerate(("", "Title", "", "Length", "Saves as")):
            ttk.Label(body, text=head, style="Sub.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 10))
        self.vars = {}
        for r, t in enumerate(rows, 1):
            v = tk.BooleanVar(value=t["id"] in current)
            self.vars[t["id"]] = v
            label = ("Episode %d" % t["episode_index"]) if t.get("episode_index") else t["role"].title()
            ttk.Checkbutton(body, variable=v).grid(row=r, column=0, sticky="w")
            ttk.Label(body, text=label).grid(row=r, column=1, sticky="w", padx=(0, 10))
            ttk.Label(body, text=t["id"], style="Sub.TLabel").grid(row=r, column=2, sticky="w", padx=(0, 10))
            ttk.Label(body, text=di.hms(t["duration"])).grid(row=r, column=3, sticky="e", padx=(0, 10))
            ttk.Label(body, text=names.get(t["id"], ""), style="Sub.TLabel").grid(row=r, column=4, sticky="w")
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=12, pady=12)
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

    SIZES = {"discdb": "about 4 MB", "imdb": "about 250 MB, then a few minutes to index"}

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
