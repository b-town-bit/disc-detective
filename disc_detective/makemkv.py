"""MakeMKV, the rip engine: everything particular to driving makemkvcon. The title it's asked for,
what its messages mean, the stopped backup for its decryption file, and the sting. Another engine
would get a file like this one, with its own pipeline; the app only asks for what it needs."""
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time

from .core import find_child, logger, parse_makemkv_log, parse_mpls, read_file, u16, u32

NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # keep makemkvcon from flashing a console
# MakeMKV's switches for every run: robot output, and --noscan: it opens only the drive it's
# given, not every drive in the machine (a sensitive drive left alone while another is read)
MAKEMKV_SWITCHES = ["-r", "--noscan", "--cache=1"]
# Every title on the disc, whatever minimum length MakeMKV is set to
NO_MINIMUM = ("--minlength=0",)
# What can go wrong setting up a sting: the rip goes on without it
STING_ERRORS = (OSError, RuntimeError, ValueError, IndexError, struct.error, subprocess.SubprocessError)


def makemkv_source(drive):
    """MakeMKV's name for the disc: the drive by its own name ("dev:E:"), or a backup folder."""
    d = drive.rstrip("\\/")
    if re.fullmatch(r"[A-Za-z]:", d):
        return "dev:%s" % d.upper()
    if os.name != "nt" and d.startswith("/dev/"):
        return "dev:%s" % d
    return "file:%s" % os.path.abspath(drive)


def sting_root(drive):
    """The folder holding this source's BDMV, when its playlists can be read (a Linux or Mac drive
    named by its device has none to hand: None)."""
    d = drive.rstrip("\\/")
    if re.fullmatch(r"[A-Za-z]:", d):
        return d.upper() + os.sep
    return None if os.name != "nt" and d.startswith("/dev/") else os.path.abspath(drive)


class Rip:
    """One rip's dealings with makemkvcon. cancelled: asks whether the user called it off.
    numbers_changed and problem: what the last title's run found (see title())."""

    def __init__(self, exe, cancelled=lambda: False):
        self.exe, self.cancelled, self.proc = exe, cancelled, None
        self.numbers_changed, self.problem, self.exit_code = False, None, None

    def stop(self):
        if self.proc:
            self.proc.terminate()

    def read_titles(self, source, switches=()):
        """MakeMKV's reading of the disc, as it stands (its robot-mode text)."""
        info = subprocess.run([self.exe, *MAKEMKV_SWITCHES, *switches, "info", source], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900,
                              creationflags=NO_WINDOW).stdout
        if not parse_makemkv_log(info)["titles"]:
            raise RuntimeError("MakeMKV found no titles on %s" % source)
        return info

    def title(self, source, idx, work, expected=None, switches=(), progress=lambda fraction: None):
        """Rip title idx. expected: the titles of the reading idx came from (makemkv_added). This
        run scans the disc for itself: numbered differently, it stops before saving anything, since
        idx would be some other title, and says so in numbers_changed. MakeMKV's last word on
        anything going wrong stays in problem."""
        cmd = [self.exe, *MAKEMKV_SWITCHES, *switches, "--progress=-same", "mkv", source, str(idx), work]
        logger.info("  run: %s", subprocess.list2cmdline(cmd))
        self.numbers_changed, self.problem, self.exit_code = False, None, None
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        saved, scanned = None, []
        for line in self.proc.stdout:
            if expected and line.startswith("MSG:") and not self.numbers_changed:
                scanned.append(line)
                if line.startswith("MSG:5014,"):           # the scan's over, and it's about to save
                    now = makemkv_added("".join(scanned))
                    if now and now != expected:
                        self.numbers_changed = True
                        self.proc.terminate()
            if line.startswith("PRGV:"):
                try:
                    cur, tot, mx = (int(x) for x in line[5:].split(","))
                    progress((tot / mx) if mx else 0)
                except ValueError:
                    pass
            elif line.startswith("MSG:"):
                # MakeMKV's own messages, for the record: its words, not its robot-mode line ("5005,128,1,...,"%1 titles saved"")
                logger.info("  makemkv: %s", makemkv_message(line) or line.strip()[4:])
                count = makemkv_saved(line)
                saved = saved if count is None else count
                self.problem = makemkv_problem(line) or self.problem
        self.proc.wait()
        rc, self.proc = self.proc.returncode, None
        self.exit_code = rc
        if self.numbers_changed:
            return False
        if rc and not saved:             # (the rip says whether it's a problem: not for a disc taken out)
            logger.info("makemkvcon exited with code %s", rc)
        return saved > 0 if saved is not None else rc == 0   # (no count, as in another language: its exit code)

    def sting(self, drive, ids, starting=lambda: None):
        """Somewhere MakeMKV lists these titles from, when only a Play All plays them: our playlists
        in a backup folder, or a sting standing in for a disc. None when no such titles. Returns its
        name for MakeMKV, {our playlist: its title}, and what removes it all."""
        root = sting_root(drive)
        found = inside_play_alls(root, ids) if root else {}
        if not found:
            return None
        logger.info("only a Play All plays %s: setting up a sting", ", ".join(sorted(found)))
        starting()
        if makemkv_source(drive).startswith("file:"):
            undo = lambda: clear_own_playlists(root)
            try:
                return makemkv_source(drive), add_episode_playlists(root, root, found), undo
            except STING_ERRORS:
                undo()
                raise
        work = sting_folder()
        remove_sting(work)                         # (one a crash left behind)
        os.makedirs(work)
        undo = lambda: remove_sting(work)
        try:
            disc = build_sting(root, work, self.discatt(drive, os.path.join(work, "backup")))
            return "file:%s" % disc, add_episode_playlists(root, disc, found, note=False), undo
        except STING_ERRORS:
            undo()
            raise

    def discatt(self, drive, folder):
        """The decryption file MakeMKV writes at the very start of a backup: start one, and stop it
        once the file's done, before it copies any video. It stays on this computer, in the sting."""
        drives = subprocess.run([self.exe, *MAKEMKV_SWITCHES, "info", "disc:9999"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW).stdout
        number = makemkv_drive_number(drives, drive)
        if number is None:
            raise RuntimeError("MakeMKV doesn't list %s" % drive)
        os.makedirs(folder)
        path = os.path.join(folder, "discatt.dat")
        cmd = [self.exe, *MAKEMKV_SWITCHES, "backup", "disc:%d" % number, folder]
        logger.info("  run: %s", subprocess.list2cmdline(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        started, last, steady = time.time(), -1, 0
        try:
            while self.proc.poll() is None and steady < 4 and not self.cancelled() and time.time() - started < 300:
                time.sleep(0.5)
                size = os.path.getsize(path) if os.path.exists(path) else -1
                steady = steady + 1 if size > 0 and size == last else 0
                last = size
            finished = self.proc.poll() is not None
        finally:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(30)
                except subprocess.TimeoutExpired:
                    logger.warning("MakeMKV hasn't stopped its backup yet")
            self.proc = None
        if self.cancelled():
            raise RuntimeError("called off")
        if not (steady >= 4 or finished and os.path.exists(path) and os.path.getsize(path)):
            raise RuntimeError("MakeMKV didn't write its decryption file")
        return path


def makemkv_added(text):
    """What a MakeMKV scan's messages say it added, to tell whether two scans number their titles
    alike. Title numbers belong to a scan, not the disc: they move with MakeMKV's minimum title
    length, and between a disc and a backup of it. A Blu-ray's messages say each file's number; a
    DVD's only say which titles, in order (and some are later dropped or doubled, so they can't give
    the numbers themselves). [] when the text has no such messages."""
    import csv
    files, dvd = [], []
    for line in text.splitlines():
        if not line.startswith(("MSG:3307,", "MSG:3028,")):
            continue
        try:
            fields = next(csv.reader([line[4:]]))
        except (csv.Error, StopIteration):
            continue
        if fields[0] == "3307" and len(fields) >= 7:
            files.append((fields[-2].lower(), fields[-1]))
        elif fields[0] == "3028" and len(fields) >= 6:
            dvd.append(fields[5])
    return sorted(files) or dvd


def makemkv_message(line):
    """The words of one of MakeMKV's robot-mode messages (None: the line isn't one)."""
    import csv
    if not line.startswith("MSG:"):
        return None
    try:
        fields = next(csv.reader([line[4:].strip()]))
    except (csv.Error, StopIteration):
        return None
    return fields[3] if len(fields) > 3 else None


def makemkv_saved(line):
    """How many titles this message says MakeMKV saved (None: it doesn't say). "0 titles saved" is a
    failure that reads like success."""
    found = re.search(r"(\d+) titles? saved", makemkv_message(line) or "")
    return int(found.group(1)) if found else None


def makemkv_problem(line):
    """This message's words when it reports something going wrong, for showing the user. MakeMKV's
    summary ("0 titles saved, 1 failed") is left out: it says that, but not why."""
    text = makemkv_message(line)
    if not text or makemkv_saved(line) is not None:
        return None
    return text if re.search(r"fail|error|abort", text, re.I) else None


def makemkv_drive_number(text, drive):
    """The disc number MakeMKV gives a drive (its backup takes only that), from its drive list."""
    import csv
    want = drive.rstrip("\\/").upper()
    for line in text.splitlines():
        if not line.startswith("DRV:"):
            continue
        try:
            fields = next(csv.reader([line[4:]]))
        except (csv.Error, StopIteration):
            continue
        if len(fields) >= 7 and fields[6].rstrip("\\/").upper() == want:
            return int(fields[0])
    return None


def named_titles(text, own):
    """MakeMKV's titles from a reading, each of our playlists (own: its file -> the title it plays)
    going by that title's name, so a rip plan finds it."""
    titles = parse_makemkv_log(text)["titles"]
    for t in titles:
        t["source"] = own.get(t["source"], t["source"])
    return titles


# Some Blu-rays keep their episodes only inside a Play All. MakeMKV lists titles from playlists, so
# it can't list those, but it will list a playlist of ours that plays just one. A backup folder takes
# them as it is; a disc can't be written to, so a "sting" stands in for it: a temporary folder with
# copies of its small files, links to the rest, and the decryption file MakeMKV writes at the start
# of a backup. Nothing but the playlists is copied, and everything's removed after the rip.
OWN_PLAYLISTS = "disc_detective_playlists.txt"


def inside_play_alls(root, ids):
    """Which of these titles ("00016.m2ts") only a longer playlist plays: {title: (playlist, item)}."""
    pl_dir = find_child(find_child(root, "BDMV") or "", "PLAYLIST") if find_child(root, "BDMV") else None
    want, found = {i.lower(): i for i in ids if i.lower().endswith(".m2ts")}, {}
    for fn in sorted(os.listdir(pl_dir) if pl_dir and want else []):
        if not fn.lower().endswith(".mpls"):
            continue
        try:
            items = parse_mpls(read_file(os.path.join(pl_dir, fn)))["items"]
        except (OSError, ValueError, struct.error, IndexError):
            continue
        clips = [c for c, _, _ in items]
        for k, clip in enumerate(clips):
            title = want.get("%s.m2ts" % clip.lower())
            if title and len(clips) >= 2 and clips.count(clip) == 1 and title not in found:
                found[title] = (fn, k)
    return found


def episode_playlist(data, k):
    """A playlist of item k of this one: its streams, languages and chapters, with no join to the
    item before it and without the pop-up menu's streams (they live in sub paths it won't have)."""
    pl_at, mark_at = u32(data, 8), u32(data, 12)
    pos, body = pl_at + 10, None
    for i in range(u16(data, pl_at + 6)):
        length = u16(data, pos)
        if i == k:
            body = bytearray(data[pos + 2:pos + 2 + length])
        pos += 2 + length
    if body is None:
        raise ValueError("no item %d in the playlist" % k)
    if body[10] & 0x10:
        raise ValueError("a multi-angle item")
    body[10] = (body[10] & 0xF0) | 1
    stn = bytearray(body[32:])
    used = u16(stn, 0)
    p = 16
    for _ in range(sum(stn[4:7])):                       # video, audio and subtitles
        if p + 1 >= 2 + used or stn[p + 1] != 1:         # (1: in this clip)
            raise ValueError("a stream outside the clip")
        p += 1 + stn[p]
        p += 1 + stn[p]
    stn[7:11] = b"\0\0\0\0"                              # no menu or secondary streams
    stn[:2] = struct.pack(">H", p - 2)
    item = bytes(body[:32]) + bytes(stn[:p])
    head = bytes(data[:pl_at])
    body_out = b"\0\0" + struct.pack(">HH", 1, 0) + struct.pack(">H", len(item)) + item
    playlist = struct.pack(">I", len(body_out)) + body_out
    marks = []
    for i in range(u16(data, mark_at + 4)):
        mark = data[mark_at + 6 + 14 * i:mark_at + 6 + 14 * (i + 1)]
        if u16(mark, 2) == k:
            marks.append(mark[:2] + b"\0\0" + mark[4:])
    mark_body = struct.pack(">H", len(marks)) + b"".join(marks)
    out = bytearray(head + playlist + struct.pack(">I", len(mark_body)) + mark_body)
    out[8:20] = struct.pack(">III", len(head), len(head) + len(playlist), 0)
    return bytes(out)


def add_episode_playlists(root, dest, found, note=True):
    """Write a playlist per title of `found` (inside_play_alls of root) into dest's, under numbers
    neither uses: {its file: the title}. note: leave word of them in dest, for clear_own_playlists."""
    src_dir = find_child(find_child(root, "BDMV"), "PLAYLIST")
    dest_dir = find_child(find_child(dest, "BDMV"), "PLAYLIST")
    taken = {f.lower() for f in os.listdir(src_dir)} | {f.lower() for f in os.listdir(dest_dir)}
    made, number = [], 100
    for title, (fn, k) in sorted(found.items()):
        data = episode_playlist(read_file(os.path.join(src_dir, fn)), k)
        number += 1
        while "%05d.mpls" % number in taken:
            number += 1
        made.append(("%05d.mpls" % number, title, data))
    if note:
        with open(os.path.join(dest, OWN_PLAYLISTS), "w", encoding="utf-8") as fh:
            fh.write("".join(name + "\n" for name, _, _ in made))
    for name, _, data in made:
        with open(os.path.join(dest_dir, name), "wb") as fh:
            fh.write(data)
    return {name: title for name, title, _ in made}


def clear_own_playlists(root):
    """Remove the playlists we noted in this folder, and the note."""
    note = os.path.join(root, OWN_PLAYLISTS)
    try:
        with open(note, encoding="utf-8") as fh:
            names = [n.strip() for n in fh if re.fullmatch(r"\d{5}\.mpls", n.strip())]
    except OSError:
        return
    pl_dir = find_child(find_child(root, "BDMV") or "", "PLAYLIST") if find_child(root, "BDMV") else None
    try:
        for name in names if pl_dir else []:
            if os.path.exists(os.path.join(pl_dir, name)):
                os.remove(os.path.join(pl_dir, name))
        os.remove(note)
    except OSError as exc:
        logger.warning("couldn't clear our playlists from %s: %s", root, exc)


def sting_folder():
    """The sting's temporary folder, on this computer (links can't all reach a network drive)."""
    return os.path.join(tempfile.gettempdir(), "disc_detective_sting")


def _link_folder(target, link):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(target, link)      # (a symlink would need admin rights or Developer Mode)
    else:
        os.symlink(target, link)


def build_sting(root, work, discatt):
    """A folder MakeMKV can open as this disc: its playlists and start-up files copied, a link to
    every other folder, and the decryption file. Returns the folder."""
    disc = os.path.join(work, "disc")
    bdmv = find_child(root, "BDMV")
    os.makedirs(os.path.join(disc, "BDMV", "PLAYLIST"))
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path) and path != bdmv:
            _link_folder(path, os.path.join(disc, name))
    for name in os.listdir(bdmv):
        path = os.path.join(bdmv, name)
        if not os.path.isdir(path):
            shutil.copyfile(path, os.path.join(disc, "BDMV", name))
        elif name.upper() == "PLAYLIST":
            for pl in os.listdir(path):
                shutil.copyfile(os.path.join(path, pl), os.path.join(disc, "BDMV", "PLAYLIST", pl))
        else:
            _link_folder(path, os.path.join(disc, "BDMV", name))
    shutil.copyfile(discatt, os.path.join(disc, "discatt.dat"))
    return disc


def remove_sting(path):
    """Delete the sting's folder. Links are only unhooked: what they point at is never touched."""
    if not os.path.lexists(path):
        return
    with os.scandir(path) as entries:
        items = list(entries)
    for entry in items:
        info = os.lstat(entry.path)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:   # (0x400: a junction)
            os.rmdir(entry.path) if os.name == "nt" else os.unlink(entry.path)
        elif stat.S_ISDIR(info.st_mode):
            remove_sting(entry.path)
        else:
            os.chmod(entry.path, stat.S_IWRITE)
            os.remove(entry.path)
    os.rmdir(path)
