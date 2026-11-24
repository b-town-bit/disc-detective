"""A film named by what it shows at its ends: the MPA certificate card, the end credits, the opening credits.

For a disc nothing else has named for sure (the credits witness, after the menus'): its main title's frames are
read, never the whole of it. A disc reads one place at a time, so everything here is one straight walk or a few
seeks, never parallel reads.

1. The MPA card: the Motion Picture Association's globe found by its outline in the last 5-120 s (small frames,
   nothing read until it's there), its certificate number and the copyright year beside it read. The number
   names one film whose year agrees: that's the film. A number some other film's (a reused card: Shanghai Noon's
   shows The Sixth Sense's) is dropped; its year is kept.
2. The end credits: found from the end (a crawl near it, back chapter by chapter, halved), read until the
   close window holds one film; else the window: every film the leader isn't clearly ahead of.
3. The opening credits, when the end left no winner: read straight from the start until the window holds one
   film, or a certificate number (older films show it under their titles) names one with its year.

Names are matched to the IMDb credits index (core.IMDB_CREDITS_DB), weighed by the heading above them; a near
tie between films of a series is settled by their full casts (TMDB, with your key) when that's clear. A winner
needs a clear lead; a pick on a name or two that other films share too is no winner. Ties go to the better
known film in the window's order, never as a winner.

Built from the 2026-11 spike over a Plex library of 333 films: 311 right winners, 12 right in the window,
0 wrong winners."""
import collections
import json
import os
import re
import sqlite3
import subprocess
import time
import unicodedata

from . import core, menus, tools

MPA_FILE = "mpa_certs.json"                 # (beside the IMDb index: built with it, mpa_build)

# ---- reading a frame ------------------------------------------------------------------------------------------

def norm(s):
    """A row's words for matching: lower case, accents dropped (Renée -> renee), punctuation between letters a
    space (Stock-Poynton, O'Brien, WATTS,JIM), digits and other marks gone."""
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", re.sub(r"[-'’`.,/&;:]", " ", s)).split()


def _cv2():
    import cv2
    return cv2


def _rgb(f):
    from PIL import Image
    return Image.fromarray(_cv2().cvtColor(f, _cv2().COLOR_BGR2RGB))


def rows_boxes(f):
    """The rows read in a frame, top to bottom, words left to right: [(text, (x0, y0, x1, y1))]."""
    lines = sorted(menus.rapid_lines(_rgb(f)) or [], key=lambda x: (x[1][1] + x[1][3]) / 2)
    rows = []
    for text, (x0, y0, x1, y1), _ in lines:
        c, h = (y0 + y1) / 2, y1 - y0
        if rows and abs(c - rows[-1][0]) < h * 0.5:
            r = rows[-1]
            r[1].append((x0, text))
            r[2] = (min(r[2][0], x0), min(r[2][1], y0), max(r[2][2], x1), max(r[2][3], y1))
        else:
            rows.append([c, [(x0, text)], (x0, y0, x1, y1)])
    return [(" ".join(t for _, t in sorted(r[1])), r[2]) for r in rows]


def rows_of(f):
    return [t for t, _ in rows_boxes(f)]


def text_boxes(f, h):
    """Where text is in a frame (nothing read), as fractions of a 960-wide copy: [(x0, y0, x1, y1)]."""
    cv2 = _cv2()
    small = cv2.resize(f, (960, h // 2))
    got = menus.rapid_boxes(small) or []
    H = small.shape[0]
    return [(x0 / 960, y0 / H, x1 / 960, y1 / H) for x0, y0, x1, y1 in got]


def lines_of(f, h):
    """text_boxes' lines a crawl is made of: a line's height, not a big title's."""
    return [b for b in text_boxes(f, h) if b[3] - b[1] < 0.08 and b[2] - b[0] > 0.02]


# ---- the title's frames ---------------------------------------------------------------------------------------

class FileSource:
    """A video file's frames, by FFmpeg (a rip, a folder's main title): shown at its display shape (a DVD's
    squeezed 720x480 widened), 1920 wide; small frames 960 wide."""

    def __init__(self, path):
        self.path = path
        self.ffmpeg = tools.which("ffmpeg") or "ffmpeg"
        p = json.loads(subprocess.run(
            [tools.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,sample_aspect_ratio",
             "-select_streams", "v:0", "-show_chapters", "-of", "json", path],
            capture_output=True, text=True, **core.NO_CONSOLE).stdout)
        self.dur = float(p["format"]["duration"])
        W, H = p["streams"][0]["width"], p["streams"][0]["height"]
        self.chapters = [float(c["start_time"]) for c in p.get("chapters", [])]
        sar = p["streams"][0].get("sample_aspect_ratio") or "1:1"
        try:
            a, b = (int(x) for x in sar.split(":"))
            sar = a / b if a and b else 1.0
        except ValueError:
            sar = 1.0
        self.w, self.h = 1920, int(round(H * 1920 / (W * sar) / 2)) * 2
        self.grabs, self.grab_s = 0, 0.0
        self.keyframes = False      # (a disc's: the picture at the entry point before a time; a file's always exact)

    def frame(self, t, small=False):
        import numpy as np
        s = time.time()
        self.grabs += 1
        w, h = (960, int(round(self.h * 960 / self.w / 2)) * 2) if small else (self.w, self.h)
        out = subprocess.run([self.ffmpeg, "-v", "error", "-ss", "%.3f" % t, "-i", self.path, "-map", "0:v:0",
                              "-frames:v", "1", "-vf", "scale=%d:%d" % (w, h), "-f", "rawvideo", "-pix_fmt", "bgr24",
                              "pipe:"], capture_output=True, **core.NO_CONSOLE).stdout
        self.grab_s += time.time() - s
        if len(out) < w * h * 3:
            return None
        return np.frombuffer(out[:w * h * 3], np.uint8).reshape(h, w, 3)


DISC_AHEAD = 0.7                    # (s past the wanted time, read on to the entry point beyond it)
DISC_READ_MIN, DISC_READ_MAX = 2 * 2 ** 20, 12 * 2 ** 20   # (bytes read for a frame: at least, and when the map ends)
DISC_KEY_READ = 3 * 2 ** 19         # (bytes read for a keyframe alone: the entry point's picture; too few, the
                                    #  exact way's read from there instead)


class DiscSource(FileSource):
    """A Blu-ray's playlist (a drive or a folder: "00800.mpls" or 800), its frames read off the disc itself: a time
    to its clip and the entry point at or before it (the clip's EP_map), the bytes from there to just past it read
    and decrypted by hdmv-menus' read_clip (the user's own MakeMKV library, the disc unlocked once: disc, when the
    menus have it open already), FFmpeg decoding them on to the time. One seek and about a second of video a
    frame; nothing ripped."""

    def __init__(self, root, playlist, disc=None):
        from . import bdhdmv
        bdmv = core.find_child(root, "BDMV") or os.path.join(root, "BDMV")
        name = "%05d.mpls" % playlist if isinstance(playlist, int) else os.path.basename(playlist)
        pl = core.parse_mpls(core.read_file(os.path.join(core.find_child(bdmv, "PLAYLIST") or
                                                         os.path.join(bdmv, "PLAYLIST"), name)))
        clipinf = core.find_child(bdmv, "CLIPINF") or os.path.join(bdmv, "CLIPINF")
        self.path, self.ffmpeg = "%s %s" % (root, name), tools.which("ffmpeg") or "ffmpeg"
        self.items, self.starts, acc = [], [], 0.0
        self.maps, aspect = {}, None
        for clip, t_in, t_out in pl["items"]:
            self.items.append((clip, t_in / 45000.0))
            self.starts.append(acc)
            acc += max(0, t_out - t_in) / 45000.0
            if clip not in self.maps:
                data = core.read_file(os.path.join(clipinf, clip + ".clpi"))
                self.maps[clip] = core.clpi_entry_points(data)
                aspect = aspect or core.clpi_aspect(data)
        self.dur, self.chapters = pl["duration"], pl["chapter_times"]
        self.w, self.h = 1920, 1440 if aspect == "4:3" else 1080
        if disc is None:
            import hdmv_menus
            disc = hdmv_menus.Disc(root, aacs=bdhdmv.aacs_library())
        self.disc = disc
        self.grabs, self.grab_s, self.read_bytes = 0, 0.0, 0
        self.keyframes = False      # (on: a frame is the entry point's picture at or before its time, up to about a
                                    #  second early -- a third of the reading, for a straight read's steps)

    def where(self, t):
        """(clip, the entry point's byte offset at or before playlist time t, seconds on from it to t, the offset to
        read to)."""
        import bisect
        i = max(0, bisect.bisect_right(self.starts, t) - 1)
        clip, t_in = self.items[i]
        ct = t_in + t - self.starts[i]
        m = self.maps[clip]
        k = max(0, bisect.bisect_right([p for p, _ in m], ct) - 1)
        p0, off = m[k] if m else (ct, 0)
        end = next((o for p, o in m[k + 1:] if p >= ct + DISC_AHEAD), off + DISC_READ_MAX)
        return clip, off, max(0.0, ct - p0), max(end, off + DISC_READ_MIN)

    def frame(self, t, small=False):
        import numpy as np
        s = time.time()
        self.grabs += 1
        w, h = (960, self.h // 2) if small else (self.w, self.h)
        clip, off, delta, end = self.where(min(max(0.0, t), self.dur - 0.05))
        out = self._decode(clip, off, DISC_KEY_READ, 0.0, w, h) if self.keyframes else b""
        if len(out) < w * h * 3:
            out = self._decode(clip, off, end - off, 0.0 if self.keyframes else delta, w, h)
        self.grab_s += time.time() - s
        if len(out) < w * h * 3:
            return None
        return np.frombuffer(out[:w * h * 3], np.uint8).reshape(h, w, 3)

    def _decode(self, clip, off, length, delta, w, h):
        """A clip's bytes from off read and decoded on delta s: the picture's raw bytes, or b""."""
        try:
            data = self.disc.read_clip(clip, off, length)
        except (OSError, ValueError):
            return b""
        self.read_bytes += len(data)
        return subprocess.run([self.ffmpeg, "-v", "error", "-f", "mpegts", "-i", "pipe:", "-ss", "%.3f" % delta,
                               "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=%d:%d" % (w, h), "-f", "rawvideo",
                               "-pix_fmt", "bgr24", "pipe:"], input=data, capture_output=True,
                              **core.NO_CONSOLE).stdout if data else b""


# ---- the data: IMDb's credits, the MPA numbers, TMDB's casts --------------------------------------------------

class Index:
    """The IMDb index and its credits (read only), with what's been looked up kept. One thread's."""

    def __init__(self, imdb_dir):
        self.ti = sqlite3.connect(os.path.join(imdb_dir, core.IMDB_DB))
        self.cc = sqlite3.connect(os.path.join(imdb_dir, core.IMDB_CREDITS_DB))
        self.has_kf = bool(self.cc.execute("select 1 from sqlite_master where name='known_for'").fetchone())
        self._people, self._chars, self._roles, self._plays, self._named, self._charhits = {}, {}, {}, {}, {}, {}
        self._votes, self._titles = {}, {}

    def close(self):
        self.ti.close()
        self.cc.close()

    def title(self, t):
        """(title, year, type, runtime) of an IMDb title id."""
        if t not in self._titles:
            self._titles[t] = self.ti.execute("select title, year, type, runtime from titles where id=?", (t,)).fetchone() \
                or ("tt%07d" % t, None, None, None)
        return self._titles[t]

    def votes(self, t):
        if t not in self._votes:
            r = self.ti.execute("select votes from ratings where id=?", (t,)).fetchone()
            self._votes[t] = r[0] if r else 0
        return self._votes[t]

    def person(self, words):
        """The people named exactly these words."""
        k = " ".join(words)
        if k not in self._people:
            q = " ".join('"%s"' % w for w in words)
            self._people[k] = [p for p, n in self.cc.execute(
                "select p.id, p.name from people_fts f join people p on p.id=f.rowid where people_fts match ? limit 200", (q,))
                if norm(n) == words]
        return self._people[k]

    def characters(self, words):
        """Credits whose character is these words: [(title, person)]."""
        k = " ".join(words)
        if k not in self._chars:
            q = " ".join('"%s"' % w for w in words)
            self._chars[k] = [(t, p) for t, p, c in self.cc.execute(
                "select c.title, c.person, c.characters from characters_fts f join credits c on c.rowid=f.rowid "
                "where characters_fts match ? limit 300", (q,)) if norm(c or "") == words]
        return self._chars[k]

    def roles(self, p):
        """(title -> this person's IMDb categories there, "known" for a known-for title; their professions)."""
        if p not in self._roles:
            r = collections.defaultdict(set)
            for t, j in self.cc.execute("select title, job from credits where person=?", (p,)):
                r[t].add(j)
            profs = set()
            if self.has_kf:
                for (t,) in self.cc.execute("select title from known_for where person=?", (p,)):
                    r[t].add("known")
                row = self.cc.execute("select profs from professions where person=?", (p,)).fetchone()
                profs = set(((row or [""])[0] or "").split(","))
            self._roles[p] = (dict(r), profs - {""})
        return self._roles[p]

    def plays(self, p):
        """title -> the words of the characters this person plays there."""
        if p not in self._plays:
            out = collections.defaultdict(set)
            for t, c in self.cc.execute("select title, characters from credits where person=? and characters is not null", (p,)):
                out[t] |= {x for x in norm(c) if len(x) >= 3 and x not in PAIR_STOP}
            self._plays[p] = dict(out)
        return self._plays[p]

    def name_of(self, p):
        if p not in self._named:
            r = self.cc.execute("select name from people where id=?", (p,)).fetchone()
            self._named[p] = norm(r[0]) if r else []
        return self._named[p]

    def character_credits(self, cw):
        k = " ".join(cw)
        if k not in self._charhits:
            q = " ".join('"%s"' % x for x in cw)
            self._charhits[k] = [(t, pp, c) for t, pp, c in self.cc.execute(
                "select c.title, c.person, c.characters from characters_fts f join credits c on c.rowid=f.rowid "
                "where characters_fts match ? limit 400", (q,))]
        return self._charhits[k]

    def names_person(self, row):
        """Does this row name a person in the IMDb index: the whole row, or its first or last 2-3 words (a job title
        before a name, a character beside one)? In-film text (captions, signs, headlines, screens) mostly doesn't."""
        for part in re.split(r",|&|/|\band\b", row, flags=re.I):
            w = norm(part)
            spans = {tuple(w)} | {tuple(w[:k]) for k in (2, 3)} | {tuple(w[-k:]) for k in (2, 3)}
            if any(2 <= len(sp) <= 5 and self.person(list(sp)) for sp in spans):
                return True
        return False


class Casts:
    """Films' full casts and crews from TMDB (by IMDb id, with your key), kept in a file once fetched (film names
    and their people's names only). Without a key: empty, and the steps that use them stand aside."""

    def __init__(self, key, path):
        self.key, self.path = key, path
        try:
            with open(path, encoding="utf-8") as fh:
                self.kept = json.load(fh)
        except (OSError, ValueError):
            self.kept = {}

    def _fetch(self, t):
        k = str(t)
        if k not in self.kept:
            if not self.key:
                return {"cast": [], "names": [], "chars": []}
            try:
                f = core.tmdb_get(self.key, "/find/tt%07d" % t, external_source="imdb_id")
                found = (f.get("movie_results") or []) + (f.get("tv_results") or [])
                kind = "movie" if f.get("movie_results") else "tv"
                c = core.tmdb_get(self.key, "/%s/%d/credits" % (kind, found[0]["id"])) if found else {}
            except Exception:
                return {"cast": [], "names": [], "chars": []}           # (not kept: tried again next time)
            cast = sorted({" ".join(norm(x["name"])) for x in c.get("cast") or []} - {""})
            names = {" ".join(norm(x["name"])) for x in (c.get("cast") or []) + (c.get("crew") or [])} - {""}
            chars = set()
            for x in c.get("cast") or []:
                for part in re.split(r"/|\(|\)", x.get("character") or ""):
                    w = [y for y in norm(part) if y not in PAIR_STOP]
                    if len(w) >= 2 or w and len(w[0]) >= 5:
                        chars.add(" ".join(w))
            self.kept[k] = {"cast": cast, "names": sorted(names), "chars": sorted(chars)}
            try:
                tmp = self.path + ".new"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.kept, fh)
                os.replace(tmp, self.path)
            except OSError:
                pass
        return self.kept[k]

    def cast(self, t):
        return set(self._fetch(t)["cast"])

    def credits(self, t):
        d = self._fetch(t)
        return set(d["names"]) | {"char " + c for c in d["chars"]}


def mpa_load(imdb_dir):
    """The MPA certificate numbers (mpa_build): {"42055": [IMDb ids]}, or {} when there's none yet."""
    try:
        with open(os.path.join(imdb_dir, MPA_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# ---- the rules ------------------------------------------------------------------------------------------------

LOW_TYPES = {"video", "tvShort", "tvSpecial", "short", "videoGame", "tvEpisode", "tvMiniSeries", "tvSeries"}
CAST_WORDS = {"cast", "casting", "starring", "characters"}
CAST_JOBS = {"actor", "actress", "self"}
JOB_WORDS = set("""produced producer producers coproducer coproducers directed director directors written writer
writers screenplay story music composed composer conducted edited editor editors photography cinematography
cinematographer designer designers design costume costumes makeup casting supervisor supervisors effects
production manager managers associate executive featuring performed sound mixer mixers decorator decorators
stunt stunts coordinator coordinators camera operator operators assistant assistants animation animators
choreography choreographer orchestrated orchestrations narrated narrator lyrics songs""".split())
PAIR_STOP = {"the", "and", "mr", "mrs", "miss", "dr", "his", "her", "man", "woman", "old", "young", "little", "big",
             "voice", "uncredited", "himself", "herself", "also", "with", "from", "for", "boy", "girl", "lady"}
ROLE_MATCH = 1.5          # (a credit its heading confirms)
PAIR_BONUS = 1.0          # (a character beside the actor who played it there)
RICH_ROWS = 3             # (name rows that make a still card a credits card)
BACK_STEP, BACK_MOST, BACK_FILM = 15, 240, 3
CARD_GAP = 3              # (cards before the crawl: a frame every this many s)
NAME_STEP, NAME_MISS = 4, 8
HUNT_SPAN = 900           # (how far back earlier credits are looked for)
HARD_MOST = 200           # (frames read at most, even with no cast read)
EARLY = 6                 # (crawl frames before the early look back)
CAST_MIN = 3              # (cast points before reading may stop: about two actors)
SMALL_CAST = 3            # (a whole cast this small: one actor credited is its cast read)
CAST_AGREE = 1            # (the leader's cast points for the cast to agree)
CLOSE_CONFIDENT = 5       # (a close race: its leader's people points first)
CREW_W = 0.1              # (crew names' share in the cast ranking)
LEAD_SHARE = 0.5          # (a clear lead: at least this share of the leader's score, and 2)
OBSCURE = 1000            # (IMDb votes below which a leader doesn't end the reading early)
POP_X = 10                # (a tie step's winner this many times less popular than one close behind: no win)
THIN = 2                  # (a pick on this many names or fewer, other films having them too: more evidence needed)
CONTENDERS = 8            # (films listed when there's no clear winner)
YEAR_OFF = 0.5            # (another year than the copyright's: this much less)
STRONG = 4                # (a leader below this, on restoration credits: not the film's)
SIBLING_SHARE = 0.6
RESTORE_WORDS = {"restoration", "restored", "restore", "preserved", "preservation", "remastered", "remastering",
                 "archive", "archives", "vaults", "vault", "librarian", "library", "scan", "scanning", "colorist",
                 "conform", "transfer", "lab", "laboratory", "laboratories", "elements", "negative"}
ROMAN = {"M": 1000, "D": 500, "C": 100, "L": 50, "X": 10, "V": 5, "I": 1}

# the MPA card
LOGO_MIN, LOGO_WEAK = 0.6, 0.2         # (a sure logo; a maybe, read in full only if nothing better)
LOGO_WEAK_READ = 2                      # (the likeliest maybes read in full: 3 saved Taken 3, at 1-3 grabs on every film with no card)
LOGO_START, LOGO_USUAL, LOGO_FAR, LOGO_SPAN = 25, 45, 90, 120   # (s from the end: start, the usual stretch, the far walk)
LOGO_SCALES = [0.08 * 1.13 ** i for i in range(11)]
RASTER_W = 0.9                          # (a picture match counts a little under an outline match)
MPA_ROW = re.compile(r"motion\s*picture\s*ass|m\.?\s?p\.?\s?a\.?\s?a\b|association\s+of\s+am", re.I)
NUM_ROW = re.compile(r"(?:\bn[o0]\b|\bn\.|#|certi\w*|cert|approved)\.?\s*(?:n[o0]\.?)?\s*#?\s*([\dOoIlSsBZ]{4,6})\b", re.I)
NOT_NUM = re.compile(r"\bah[a-z]{0,2}\b|humane|op\.|\bd\.\d|isbn|\bfilm\s*no", re.I)
JUNK_C = re.compile(r"logo|used with|courtesy|permission|character|trademark|music|song|record|publish|photo|\bmap\b|"
                    r"images|lyric|book|poem|registered", re.I)
RATING_CARD = re.compile(r"been rated|rated by|rating admin|classification|parental|suitable|filmratings|mpaa\.org", re.I)
NEIGHBOURS = re.compile(r"dolby|dts|sdds|panavision|kodak|technicolor|deluxe|eastman|fujifilm|jurisdiction|a\.?f\.?l", re.I)
COMPANY = re.compile(r"\b(inc|llc|l\.l\.c|ltd|limited|corporation|corp|company|pictures|studios|productions|"
                     r"enterprises|entertainment|films|investments|gmbh)\b", re.I)
DIGITS = str.maketrans("OoIlLSsBZD", "0011155820")

# the opening
FRONT_STEP = 2            # (a small frame every this many s)
FRONT_CAP = 360           # (the opening read this far at most)
FRONT_QUIET = 5           # (picture frames in a row past chapter 2, names read: the cards are over)


def roman(tok):
    """A Roman numeral's value, or None (written the standard way only)."""
    if not tok or not re.fullmatch(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", tok):
        return None
    return sum(ROMAN[c] if i + 1 == len(tok) or ROMAN[c] >= ROMAN[tok[i + 1]] else -ROMAN[c] for i, c in enumerate(tok))


def digits(tok):
    """A token that holds 2+ digits, its look-alike letters as digits."""
    return tok.translate(DIGITS) if len(re.findall(r"\d", tok)) >= 2 else None


def row_years(text):
    """The film years a copyright row holds (look-alike letters as digits, Roman numerals of any case)."""
    out = set()
    for tok in re.findall(r"(?<![\w])[12IlL][\dOoIlLSsBZD]{3}(?![\w])", text):   # ("I997", "L999", "2OOO")
        d = tok.translate(DIGITS)
        if d and d.isdigit() and 1900 <= int(d) <= 2035:
            out.add(int(d))
    for tok in re.findall(r"(?<![A-Za-z0-9])[O0]?([MDCLXVImdclxvi][MDCLXVImdclxvi1]+)(?![A-Za-z0-9])", text):
        t = tok.replace("l", "I").replace("1", "I").upper()   # ("MCMXC11", "MMIli": I as 1 or l; "OMM": the (c) as O)
        if len(t) < 4 and not t.startswith("MM"):
            continue                                            # (a short one only if MM...: 2000-2003)
        v = roman(t)
        if v and 1900 <= v <= 2035:
            out.add(v)
    return out


def copyright_row(text):
    low = text.lower()
    return bool(("©" in text or "copyright" in low or "copyrignt" in low or re.search(r"\(c\)|all rights reserved", low)
                 or re.search(r"(?:^|\s)[@CGEO0]\s?[12Il][\dOo]{3}\b", text)) and not JUNK_C.search(text))


def company_row(text):
    """A year beside a company name, its (c) mark lost ("2008 DISNEY ENTERPRISES, INC.")."""
    return bool(COMPANY.search(text) and not JUNK_C.search(text)
                and not re.search(r"based on|motion picture$|\(\d{4}-\d{4}\)", text, re.I))


def copyright_years(texts):
    """{year: weight} from rows: a year by a (c) mark or 'copyright' (the latest such: songs credit older ones);
    a Roman numeral year only when there's no marked one."""
    marked, bare = set(), set()
    for text in texts:
        low = text.lower()
        mark = "©" in text or "copyright" in low or re.search(r"\(c\)|all rights reserved", low)
        for y in re.findall(r"(?<!\d)(19\d\d|20[0-3]\d)(?!\d)", text):
            if mark:
                marked.add(int(y))
        for tok in re.findall(r"\b[MDCLXVI]{4,}\b", text):
            v = roman(tok)
            if v and 1900 <= v <= 2035:
                (marked if mark else bare).add(v)
    if marked:
        return {max(marked): 1.0}
    return {max(bare): 0.7} if bare else {}


def nums_near(rows):
    """The numbers in text read beside a found logo: every run of 4-6 digits (look-alike letters as digits, 2 real
    digits at least), whatever's around it; years left out. The table and the year decide which is the card's."""
    out = []
    for r in rows:
        for tok in re.findall(r"(?<![0-9A-Za-z])[0-9OoIlLSsBZD]{4,6}(?![0-9A-Za-z])", r):
            d = digits(tok)
            if d and d.isdigit() and not (len(d) == 4 and 1900 <= int(d) <= 2035):
                out.append(str(int(d)))
    return out


def nums_in(rows):
    """Certificate numbers in rows: "No. 42055", "Certificate No. 29589", "Approved No. 7029", or a row of just
    4-6 digits (not a year)."""
    out = []
    for r in rows:
        if NOT_NUM.search(r):
            continue
        for m in NUM_ROW.finditer(r):
            d = digits(m.group(1))
            if d and d.isdigit():
                out.append(str(int(d)))
        m = re.fullmatch(r"\s*\W?\s*([\dOoIlSsBZ]{4,6})\s*\W?\s*", r)
        d = digits(m.group(1)) if m else None
        if d and d.isdigit() and not (len(d) == 4 and re.fullmatch(r"(19|20)\d\d", d)):
            out.append(str(int(d)))
    return out


def head_of(w):
    """The role a row's words head (or name with it): "cast", a title.principals category, "crew", or None."""
    ws = set(w)
    if ws & CAST_WORDS or ws & {"featuring", "starring"}:
        return "cast"
    if ws & {"casting"}:
        return "casting_director"
    if ws & {"photography", "cinematography", "cinematographer"}:
        return "cinematographer"
    if ws & {"assistant", "assistants", "art", "unit", "second", "first"}:
        return "crew"
    if ws & {"directed", "director"}:
        return "director"
    if ws & {"written", "writer", "writers", "screenplay", "story", "novel"}:
        return "writer"
    if ws & {"music", "composed", "composer"}:
        return "composer"
    if ws & {"edited", "editor", "editors", "editing"}:
        return "editor"
    if ws & {"produced", "producer", "producers", "coproducer", "coproducers", "executive"}:
        return "producer"
    if "design" in ws and "production" in ws or "designer" in ws and "production" in ws:
        return "production_designer"
    if ws & JOB_WORDS:
        return "crew"
    return None


def agrees(h, jobs, profs):
    """A person's credit there fits the heading above it."""
    if h == "cast":
        return bool(jobs & CAST_JOBS) or "known" in jobs and bool(profs & {"actor", "actress"})
    if h == "crew":
        return bool(jobs - CAST_JOBS - {"known"}) or "known" in jobs and bool(profs - {"actor", "actress", "self"})
    if h == "composer":
        return "composer" in jobs or "known" in jobs and bool(profs & {"composer", "music_department", "soundtrack"})
    return h in jobs or "known" in jobs and h in profs


def edits(a, b, most=2):
    """Edit distance of two words, up to most (most + 1 beyond it)."""
    if abs(len(a) - len(b)) > most:
        return most + 1
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        if min(cur) > most:
            return most + 1
        prev = cur
    return prev[-1]


def near_word(x, ws):
    """x is one of these words, or (4 letters or more) one letter off one."""
    return x in ws or len(x) >= 4 and any(len(y) >= 4 and edits(x, y, 1) <= 1 for y in ws)


def near_name(read, real):
    """A name read with a slip: as many words, a letter off in all (two in a long name); first letters kept."""
    if len(read) != len(real) or not real:
        return False
    d = sum(edits(a, b) for a, b in zip(read, real))
    return 0 < d <= (2 if sum(map(len, real)) >= 12 else 1) and all(a[:1] == b[:1] or len(a) >= 5 for a, b in zip(read, real))


def clear(a, b):
    """A lead of a over b is clear: at least max(2, half of a)."""
    return a - b >= max(2, LEAD_SHARE * a)


def restoration_share(texts):
    """The share of rows read that are restoration / archive work (and whether a restoration heading came first)."""
    rows = [norm(t) for t in texts if norm(t)]
    hit = [r for r in rows if set(r) & RESTORE_WORDS]
    first = next((k for k, r in enumerate(rows) if set(r) & {"restoration", "restored", "preserved"}), None)
    return (len(hit) / len(rows) if rows else 0.0), first is not None and first <= 3


def same_card(a, b):
    """Two frames' text boxes the same (one card, even over a moving picture): as many boxes, each with one
    within 2% of it in the other."""
    if not a or not b or abs(len(a) - len(b)) > max(1, len(a) // 4):
        return False
    near = lambda x, y: all(abs(x[i] - y[i]) < 0.02 for i in range(4))
    return sum(1 for x in a if any(near(x, y) for y in b)) >= 0.75 * max(len(a), len(b))


# The MPA's mark as a plain drawing of our own (no picture from a film, nothing of the logo's inside): an outer
# ellipse, a narrower inner one, a band across the middle, a plain ring in the middle. Its sizes were measured against a
# card's logo with its reel blacked out (fitted 0.94 alike). Matched masked (find_logo): only its lines and a
# margin of dark round them count, the open areas not, so the logo's own inside (its reel) doesn't need drawing,
# and a plain area scores nothing. On 412 labelled end-card frames: 77 of 92 cards at LOGO_MIN, a median 0.88.
LOGO_SHAPE = dict(asp=0.427, ow=0.461, oh=0.436, ot=0.061, iw=0.334, it=0.075, bt=0.058, r=0.441)
LOGO_MARGIN = 0.08        # (the mask's margin round the lines, of the mark's height)
LOGO_ASPECT = (1.5, 3.2)  # (an outline's width to height to be checked: some cards' logos are rounder, 1.8)


def draw_logo(w=200, p=LOGO_SHAPE):
    """The mark, w wide: grey, white lines on black (drawn 8 times bigger and shrunk, for smooth curves)."""
    import numpy as np
    cv2 = _cv2()
    h, S = int(round(w * p["asp"])), 8
    big = np.zeros((h * S, w * S), np.uint8)
    cx, cy = w / 2 * S, h / 2 * S
    P = lambda x, y: (int(x), int(y))
    T = lambda f: max(1, int(f * h * S))
    cv2.ellipse(big, P(cx, cy), P(w * p["ow"] * S, h * p["oh"] * S), 0, 0, 360, 255, T(p["ot"]), cv2.LINE_AA)
    cv2.ellipse(big, P(cx, cy), P(w * p["iw"] * S, h * p["oh"] * S), 0, 0, 360, 255, T(p["it"]), cv2.LINE_AA)
    xs = w * p["ow"] * S
    cv2.line(big, P(cx - xs, cy), P(cx + xs, cy), 255, T(p["bt"]), cv2.LINE_AA)        # (the band across the middle)
    r = h * p["r"] * S
    cv2.circle(big, P(cx, cy), int(r), 0, -1, cv2.LINE_AA)               # (inside the ring: open, not drawn)
    cv2.circle(big, P(cx, cy), int(r), 255, T(p["ot"]), cv2.LINE_AA)
    return cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA)


_logos = None


def _logo_templates():
    """The mark's picture (cropped to it), its mask (its lines and a margin round them) and its outline, made once."""
    global _logos
    if _logos is None:
        import numpy as np
        cv2 = _cv2()
        g = draw_logo(200)
        _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cs, _ = cv2.findContours(b, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(cs, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(c)
        k = max(3, int(round(g.shape[0] * LOGO_MARGIN)) | 1)
        m = cv2.dilate((g > 60).astype(np.uint8) * 255, np.ones((k, k), np.uint8))
        tg, tm = g[y:y + h, x:x + w], m[y:y + h, x:x + w]
        c = c - [x, y]                              # (its outline in the cropped picture's own coordinates)
        _logos = [(tg, tm, c, cv2.fitEllipse(c))]
    return _logos


BOX_ASPECT = (1.0, 2.4)          # (a boxed card's box: width over height, and how solid a blob it is)
BOX_FILL = 0.8


def _boxes(g):
    """Light box-like blobs (thin dark lines closed over first: a boxed mark's ellipse cuts its box): [(x, y, w, h)]."""
    import numpy as np
    cv2 = _cv2()
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    b = cv2.morphologyEx(b, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    cs, _ = cv2.findContours(b, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cs:
        x, y, w, h = cv2.boundingRect(c)
        if w >= 40 and BOX_ASPECT[0] <= w / h <= BOX_ASPECT[1] and w <= g.shape[1] * 0.45                 and cv2.contourArea(c) >= BOX_FILL * w * h:
            out.append((x, y, w, h))
    return out


def _masked(img, tpl, mask):
    """The best masked match of tpl in img (normalised correlation over the mask only; a plain area: 0), and where."""
    import numpy as np
    cv2 = _cv2()
    r = np.nan_to_num(cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED, mask=mask), nan=0.0, posinf=0.0, neginf=0.0)
    _, mx, _, loc = cv2.minMaxLoc(r)
    return float(mx), loc




def _mapped(g, c, tpl, slack=3):
    """A candidate matched in the drawing's own frame: an ellipse fitted to its outline, and the drawing's outer
    ellipse mapped onto it -- centre, angle, both axes (a logo tilted for style, Up's; a rounder or squeezed one) --
    its region warped back to the drawing's size and matched masked. The better of the two ways the fit may pair
    its axes with its angle."""
    import math
    import numpy as np
    cv2 = _cv2()
    tg, tm, _, (tctr, tax, tang) = tpl
    (cc, cax, cang) = cv2.fitEllipse(c)
    rot = lambda a: np.array([[math.cos(math.radians(a)), -math.sin(math.radians(a))],
                              [math.sin(math.radians(a)), math.cos(math.radians(a))]])
    H, W = tg.shape
    best = 0.0
    for ang, ax in ((cang, cax), (cang + 90, cax[::-1])):
        A = rot(ang) @ np.diag([ax[0] / tax[0], ax[1] / tax[1]]) @ rot(-tang)          # (drawing -> frame)
        t = np.array(cc) - A @ (np.array(tctr) + slack)
        M = np.hstack([A, t.reshape(2, 1)])
        warped = cv2.warpAffine(g, M, (W + 2 * slack, H + 2 * slack), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                borderMode=cv2.BORDER_REPLICATE)
        best = max(best, _masked(warped, tg, tm)[0], _masked(255 - warped, tg, tm)[0])
    return best


EDGE_ABOVE, EDGE_BELOW = 1.3, 2.0        # (a card's own extent round its logo, for the edge rule: its number above, name below)
CARD_ABOVE, CARD_BELOW, CARD_SIDE = 1.5, 2.5, 1.0   # (round a logo, where its card's number may be: in logo heights
                                                     #  above and below, widths to each side; crop_rows reads it)
PART_MIN = 0.4                      # (a logo cut by the frame's edge: its visible part this alike, worth a step)


def card_side(box):
    """Where a card runs off the frame, from its logo's box (fractions): "top", "bottom" or None."""
    x, y, w, h = box
    if y - EDGE_ABOVE * h < 0:
        return "top"
    if y + EDGE_BELOW * h > 1:
        return "bottom"
    return None


def logo_cut(img):
    """A logo cut by the frame's top or bottom (a scrolling card caught leaving or arriving): an outline touching
    that edge, the visible part of the drawing matched masked to it. ("top" or "bottom", score) or (None, 0)."""
    cv2 = _cv2()
    tg, tm = _logo_templates()[0][:2]
    W = 640
    h0 = int(img.shape[0] * W / img.shape[1])
    g = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (W, h0), interpolation=cv2.INTER_AREA)
    best = (None, 0.0)
    for inv in (False, True):
        _, b = cv2.threshold(g, 0, 255, (cv2.THRESH_BINARY_INV if inv else cv2.THRESH_BINARY) + cv2.THRESH_OTSU)
        cs, _ = cv2.findContours(b, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cs:
            x, y, w, h = cv2.boundingRect(c)
            top, bottom = y <= 1, y + h >= h0 - 1
            if top == bottom or w < 18 or w > W * 0.45:
                continue
            full = int(round(w * tg.shape[0] / tg.shape[1]))          # (the whole logo's height at this width)
            if not (0.25 * full <= h < 0.95 * full):
                continue
            T = cv2.resize(tg, (w, full), interpolation=cv2.INTER_AREA)
            M = cv2.resize(tm, (w, full), interpolation=cv2.INTER_NEAREST)
            T, M = (T[full - h:], M[full - h:]) if top else (T[:h], M[:h])  # (the part on screen)
            roi = g[y:y + h, max(0, x - 2):x + w + 2]
            if roi.shape[1] < w:
                continue
            r = max(_masked(roi, T, M)[0], _masked(255 - roi, T, M)[0])
            if r > best[1]:
                best = ("top" if top else "bottom", r)
    return best




def _outlines(g, ox, oy, W, h0, tpls, best):
    """The outline route over a grey picture g (the frame, or a box cut out of it at ox, oy): outlines shaped like
    the mark, each matched masked at its box's size and with the drawing's ellipse mapped onto it. best: (score,
    box as fractions of the frame), the better kept."""
    cv2 = _cv2()
    for inv in (False, True):
        _, b = cv2.threshold(g, 0, 255, (cv2.THRESH_BINARY_INV if inv else cv2.THRESH_BINARY) + cv2.THRESH_OTSU)
        cs, _ = cv2.findContours(b, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cs:
            x, y, w, h = cv2.boundingRect(c)
            if w < 18 or h < 7 or not (LOGO_ASPECT[0] <= w / h <= LOGO_ASPECT[1]) or w > W * 0.45:
                continue
            if min(cv2.matchShapes(c, tc, cv2.CONTOURS_MATCH_I1, 0) for _, _, tc, _ in tpls) >= 0.35:
                continue
            roi = g[max(0, y - 2):y + h + 2, max(0, x - 2):x + w + 2]
            if roi.shape[0] < h or roi.shape[1] < w:
                roi = cv2.copyMakeBorder(roi, 2, 2, 2, 2, cv2.BORDER_REPLICATE)
            for tpl in tpls:
                tg, tm = tpl[0], tpl[1]
                T = cv2.resize(tg, (w, h), interpolation=cv2.INTER_AREA)
                M = cv2.resize(tm, (w, h), interpolation=cv2.INTER_NEAREST)
                r = max(_masked(roi, T, M)[0], _masked(255 - roi, T, M)[0])
                if len(c) >= 5:                    # (and in the drawing's own frame: their ellipse mapped to ours)
                    r = max(r, _mapped(g, c, tpl))
                if r > best[0]:
                    best = (r, ((x + ox) / W, (y + oy) / h0, w / W, h / h0))
    return best


BAND_DARK, BAND_ROWS = 0.7, 0.08   # (a solid band in a box: rows this share dark, this share of its height thick at least)


def _free_box(g, x, y, w, h, m=8):
    """A light box with the mark in dark lines (a boxed card), freed for the outline route: the box and a margin,
    the outside painted the box's own colour (nothing outside joins the ellipse's tips), and solid dark bands across
    it painted out too (a number strip sitting on the ellipse's top); a thin line (the mark's band) stays.
    (patch, its x, its y in g)"""
    import numpy as np
    cv2 = _cv2()
    X0, Y0, X1, Y1 = max(0, x - m), max(0, y - m), min(g.shape[1], x + w + m), min(g.shape[0], y + h + m)
    patch = g[Y0:Y1, X0:X1].copy()
    bx, by = x - X0, y - Y0
    inside = np.zeros_like(patch)
    inside[by:by + h, bx:bx + w] = 255
    light = int(np.median(patch[inside > 0]))
    patch[inside == 0] = light
    t, _ = cv2.threshold(patch[by:by + h, bx:bx + w], 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = (patch[by:by + h, bx:bx + w] < t).mean(axis=1)
    run, need = [], max(4, int(BAND_ROWS * h))
    for i, v in enumerate(list(dark) + [0.0]):
        if v >= BAND_DARK:
            run.append(i)
            continue
        if len(run) >= need:
            patch[max(0, by + run[0] - 1):by + run[-1] + 2, bx:bx + w] = light
        run = []
    return patch, X0, Y0


def find_logo(img):
    """The MPA globe in a frame: outlines shaped like it (size-free), each checked by one masked match at the
    size its outline gives, light on dark or dark on light, and again with the drawing's ellipse mapped onto the
    one fitted to its outline (tilt, squeeze); light boxes freed of their surroundings and
    checked the same way (the boxed card); failing both, a plain match over sizes. (score,
    (x, y, w, h) as fractions of the frame) or (0, None)."""
    cv2 = _cv2()
    tpls = _logo_templates()
    W = 640
    h0 = int(img.shape[0] * W / img.shape[1])
    g = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (W, h0), interpolation=cv2.INTER_AREA)
    best = _outlines(g, 0, 0, W, h0, tpls, (0.0, None))
    for x, y, w, h in _boxes(g):                 # (a boxed card: the mark in dark lines on a light box)
        patch, X0, Y0 = _free_box(g, x, y, w, h)   # (the box freed: its outside and solid bands painted its colour)
        best = _outlines(patch, X0, Y0, W, h0, tpls, best)
    if best[0] < LOGO_MIN:                       # (no clear one -- the logo joined to its number, say: over sizes too)
        tg, tm = tpls[0][0], tpls[0][1]
        for sc in LOGO_SCALES:
            tw = int(W * sc)
            th = int(tg.shape[0] * tw / tg.shape[1])
            if th >= h0 or tw < 12:
                continue
            T = cv2.resize(tg, (tw, th), interpolation=cv2.INTER_AREA)
            r = cv2.matchTemplate(g, T, cv2.TM_CCOEFF_NORMED)      # (unmasked here: a whole frame masked is slow)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx * RASTER_W > best[0]:
                best = (float(mx) * RASTER_W, (loc[0] / W, loc[1] / h0, tw / W, th / h0))
    return best


fmt = lambda t: "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60)


# ---- one title, read -------------------------------------------------------------------------------------------

class Scan:
    """One title's identification by its credits (scan()): the card, the end, the opening. index: an Index;
    certs: mpa_load's table; casts: a Casts (or None: no full-cast tie step); log: a function of one string."""

    def __init__(self, index, certs, casts=None, log=None):
        self.ix, self.certs, self.casts = index, certs or {}, casts
        self.log = log or (lambda m: None)

    # (the film's state, fresh each scan)
    def _reset(self, src):
        self.src = src
        self.sparse, self.first_crawl, self.head = [], None, None
        self.cast, self.cast_pts, self.frames = collections.Counter(), {}, []
        self.stopped, self.decided, self.years, self._yr = None, None, {}, {}
        self.why_of, self.tie_log = {}, []
        self.cert = [None, None, []]
        self.walk_f = {}

    def _cast(self, t):
        return self.casts.cast(t) if self.casts else set()

    def _credits(self, t):
        return self.casts.credits(t) if self.casts else set()

    def known(self, t):
        return self.ix.votes(t) >= OBSCURE

    # -- looking at a frame
    def scrolling(self, t, dt=0.5):
        """(is it a crawl at t, its speed in frame heights a second, the frame at t)."""
        src = self.src
        a = src.frame(t)
        if a is None:
            return False, 0.0, a
        A = [l for l in lines_of(a, src.h) if l[2] - l[0] > 0.05]     # (names, not a scale's numbers)
        if len(A) < 3:
            return False, 0.0, a
        b = src.frame(t + dt)
        if b is None:
            return False, 0.0, a
        B = [l for l in lines_of(b, src.h) if l[2] - l[0] > 0.05]
        dys = [la[1] - lb[1] for la in A for lb in B
               if abs(la[0] - lb[0]) < 0.012 and abs(la[2] - lb[2]) < 0.012 and 0.003 < la[1] - lb[1] < 0.3]
        best = max(((sum(1 for e in dys if abs(e - d) < 0.006), d) for d in dys), default=(0, 0.0))
        return best[0] >= 3, best[1] / dt, a

    def crawl(self, t):
        """scrolling(t), and its frame read as names: three rows or more of 2-4 words (a pan over a scale moves,
        but reads as numbers)."""
        on, speed, a = self.scrolling(t)
        if on:
            on = sum(1 for r in rows_of(a) if sum(1 for x in norm(r) if len(x) >= 2) >= 2) >= 3
        return on, speed, a

    def probe(self, t):
        """What t is: ("crawl", speed), ("card", 0) a still card of credits (RICH_ROWS name rows or more, or a cast
        heading), ("sparse", 0) a card of a name or two (kept in self.sparse: searched past), or ("film", 0)."""
        on, sp, a = self.scrolling(t)
        if on:
            if self.first_crawl is None or t < self.first_crawl:
                self.first_crawl = t
            return "crawl", sp
        if a is None or len(lines_of(a, self.src.h)) < 1:
            return "film", 0.0
        rows = [r for r in rows_of(a) if norm(r)]
        names = [r for r in rows if sum(1 for x in norm(r) if len(x) >= 2) >= 2]
        known = [r for r in names if self.ix.names_person(r)]
        jobs = [r for r in rows if set(norm(r)) & JOB_WORDS]
        heading = any(set(norm(r)) & CAST_WORDS for r in rows)
        if heading and (known or names) or len(names) >= RICH_ROWS and (len(known) >= 2 or jobs):
            return "card", 0.0
        names = known if known or not jobs else [r for r in names if r not in jobs]
        if names:
            if not any(abs(t - x) < 1 for x, _ in self.sparse):
                self.sparse.append((t, rows))
            return "sparse", 0.0
        return "film", 0.0

    def credits_at(self, t):
        """Is t in the credits proper (a crawl or a full card); a sparse card is passed over, not the start."""
        kind, sp = self.probe(t)
        return kind in ("crawl", "card"), sp

    # -- the score
    def credit_points(self, pids, h):
        """title -> the points a name read gives it: 1 a credit; half a documentary's "self" credit, a known-for
        title with no heading to confirm it, or a credit the heading above contradicts (a namesake, most likely)."""
        out = {}
        for p in pids:
            titles, profs = self.ix.roles(p)
            for t, jobs in titles.items():
                real = jobs - {"known"}
                selfonly = bool(real) and real <= {"self"}
                w = 0.5 if selfonly or not real else 1.0
                if h is not None:
                    if agrees(h, jobs, profs):
                        w = 0.5 if selfonly else ROLE_MATCH if real else 1.0
                    else:
                        w *= 0.5
                out[t] = max(out.get(t, 0), w)
                if real & {"actor", "actress"} or h == "cast" and agrees(h, jobs, profs):
                    self.cast_pts[t] = max(self.cast_pts.get(t, 0), w)
        return out

    def fuzzy_pairs(self, w):
        """A row of no exact person: a character (1-2 words, at either end) read exactly, the rest its actor in a
        film with a slip. [(title, person, character words, actor words)]."""
        out = []
        for n, side in ((1, "start"), (2, "start"), (1, "end"), (2, "end")):
            if len(w) - n < 2 or len(w) - n > 3:
                continue
            cw, aw = (w[:n], w[n:]) if side == "start" else (w[-n:], w[:-n])
            if any(len(x) < 3 or x in PAIR_STOP for x in cw):
                continue
            for t, pp, c in self.ix.character_credits(cw):
                if set(cw) <= set(norm(c or "")) and near_name(aw, self.ix.name_of(pp)):
                    out.append((t, pp, cw, aw))
        return out

    def cast_read(self):
        top = self.cast.most_common(1)
        if not top or top[0][1] < 1.0:
            return False
        return top[0][1] >= CAST_MIN or 0 < len(self._cast(top[0][0])) <= SMALL_CAST

    def year_points(self, year):
        """The copyright year read: its film's year 1.5 points, a year later 0.75 (finished one year, out the next)."""
        if not year or not self.years:
            return 0.0
        return max(1.5 * w if year == y else 0.75 * w if year == y + 1 else 0.0 for y, w in self.years.items())

    def year_factor(self, t):
        if not self.years:
            return 1.0
        y = self.ix.title(t)[1]
        return max(1.0 if y and y in (c, c + 1) else 1.0 - YEAR_OFF * w for c, w in self.years.items())

    def year_view(self, score, n=30):
        return collections.Counter({t: s * self.year_factor(t) for t, s in score.most_common(n)}) if self.years else score

    def top_by(self, c, n):
        """The n films with most points, ties at the cut kept (up to 3n), the more popular first among equals."""
        if not c:
            return []
        cut = c.most_common(n)[-1][1]
        return sorted((t for t in c if c[t] >= cut), key=lambda t: (-c[t], -self.ix.votes(t)))[:3 * n]

    def by_weight(self, out):
        """Ranking order: weight, then people, then IMDb votes (exact ties: the more popular first)."""
        return sorted(out, key=lambda x: (x[0], x[1], self.ix.votes(int(x[6][2:]))), reverse=True)

    def _weigh(self, t, dur, base, people):
        title, year, typ, rt = self.ix.title(t)
        m = dur / 60
        fits = rt and abs(rt - m) <= 4
        w = base * self.year_factor(t) * (0.5 if not fits and (typ in LOW_TYPES or re.search(
            r"making|behind the|documentary", title or "", re.I)) else 1.0)
        w += self.year_points(year)
        if fits:
            w += 1.5
        elif rt and 0 < m - rt <= 75:
            w += 1.0 - (m - rt) / 75
        return (w, people, title, year, typ, rt, "tt%07d" % t)

    def rank(self, score, why, dur):
        """The ranking on all names: their points (the year weighed in), the year and running time."""
        self.why_of = {}
        out = []
        for t in self.top_by(score, 30):
            x = self._weigh(t, dur, score[t], score[t])
            self.why_of[x[2]] = sorted(why[t])
            out.append(x)
        return self.by_weight(out)

    def rank_cast(self, score, why, dur):
        """The ranking by the cast: its points (the year weighed in), crew a small share, the year and running time."""
        self.why_of = {}
        out = []
        for t in set(self.top_by(score, 30)) | set(self.top_by(self.cast, 10)):
            c = self.cast.get(t, 0)
            x = self._weigh(t, dur, c + CREW_W * max(score[t] - c, 0), score[t])
            self.why_of[x[2]] = sorted(why[t])
            out.append(x)
        return self.by_weight(out)

    def people_of(self, x):
        return {k for k in self.why_of.get(x[2], []) if not k.startswith(("char ", "pair "))}

    def window_of(self, r):
        """The close window of a ranking: every film the leader isn't clear of; a leader on a name or two that
        other films have too keeps them (more evidence needed); at most CONTENDERS, in ranking order."""
        if not r:
            return []
        win = [x for x in r if not clear(r[0][0], x[0])]
        named = self.people_of(r[0])
        if len(named) <= THIN:
            win += [x for x in r[1:] if named and named <= self.people_of(x) and x not in win]
        return self.by_weight(win)[:CONTENDERS]

    def close_window(self, score, why, dur):
        """(ranking, window) now: the cast-led ranking once cast is read, else all names."""
        r = self.rank_cast(score, why, dur) if self.cast_read() else self.rank(score, why, dur)
        return r, self.window_of(r)

    def low(self, t):
        """A film the ranking halves: a video, short, special, episode, or a making-of / documentary by its title."""
        title, _, typ, _ = self.ix.title(t)
        return typ in LOW_TYPES or bool(re.search(r"making|behind the|documentary", title or "", re.I))

    def siblings(self, score, why):
        """The leader and the runner-up are films of one series: most of the runner-up's names (3 or more) are the
        leader's too, so reading on (and the casts) must settle it."""
        top = score.most_common(2)
        if len(top) < 2 or len(why[top[1][0]]) < 3:
            return False
        a, b = why[top[0][0]], why[top[1][0]]
        return len(a & b) >= SIBLING_SHARE * len(b) and len(b) >= SIBLING_SHARE * len(a)

    def tie_break(self, score, rows, n=3, clean=False):
        """The top films' full casts against every row read: each film's names no other of them has. The film
        with 3 such names or more and twice the next one's, else None (or a far better known film close behind)."""
        if not self.casts or not self.casts.key:
            return None
        top = [t for t, _ in score.most_common(n)]
        dur = self.src.dur

        def fits(t):
            rt = self.ix.title(t)[3]
            return bool(rt) and abs(rt - dur / 60) <= 4
        top = [t for t in top if not self.low(t) or fits(t)] or top   # (a making-of shares the film's people)
        casts = {t: {x for x in self._credits(t) if x.startswith("char ")} | self._cast(t) for t in top}
        allp = set().union(*casts.values())
        read, own = set(), {t: set() for t in top}
        for text in rows:
            w = norm(text)
            here = set()
            for a in range(len(w)):
                for b in range(a + 2, min(a + 4, len(w)) + 1):
                    if " ".join(w[a:b]) in allp:
                        read.add(" ".join(w[a:b]))
                        here.add(" ".join(w[a:b]))
            for a in range(len(w)):          # (characters: 2 words or more; one only beside that film's own actor)
                for b in range(a + 1, min(a + 4, len(w)) + 1):
                    k = "char " + " ".join(w[a:b])
                    if k not in allp:
                        continue
                    if b - a >= 2:
                        read.add(k)
                    else:
                        for t in top:
                            if k in casts[t] and here & casts[t]:
                                own[t].add(k)
        only = {t: (read | own[t]) & casts[t] - set().union(*(casts[o] for o in top if o != t)) for t in top}
        ranked = sorted(top, key=lambda t: (-len(only[t]), -self.ix.votes(t)))
        self.tie_log.append([[self.ix.title(t)[0], len(only[t])] for t in ranked])
        a, b = len(only[ranked[0]]), (len(only[ranked[1]]) if len(ranked) > 1 else 0)
        if any(self.ix.votes(t) >= POP_X * max(self.ix.votes(ranked[0]), 1) and len(only[t]) * 2 >= a for t in ranked[1:]):
            return None                       # (far less popular than a film close behind it: left to the ranking)
        if clean:                             # (films of one series: a clean win, or 5 to 1)
            return ranked[0] if a >= 3 and (b == 0 or a >= 5 * b) else None
        return ranked[0] if a >= 3 and a >= 2 * b else None

    # -- reading frames
    def read_crawl(self, start, speed, quiet=4, most=40, cards_until=None, state=None, until=None, early=False, back=None):
        """Frames read from start every gap s (a screen's worth of the crawl), each row's names scored, until the
        close window holds one film or a stop (back: read backwards every NAME_STEP s down to back, while frames
        name people). (score, why, frames read, gap)"""
        src, ix = self.src, self.ix
        gap = min(max(0.6 / speed if speed > 0 else 3, 1.5), 8)
        if cards_until:
            most += int((cards_until - start) / CARD_GAP)
        first = not state and back is None
        if first:
            state.update(seen=set(), score=collections.Counter(), why=collections.defaultdict(set),
                         pers=collections.Counter(), pend={})
        seen, score, why, pers, pend = state["seen"], state["score"], state["why"], state["pers"], state["pend"]
        end = (until or src.dur) - 1
        t, held, n, last, same, last_set, miss = start, 0, 0, None, 0, None, 0
        if back is not None:
            state["first"] = None
        pre = [(ts, rows) for ts, rows in sorted(self.sparse) if ts < start - 1] if first else []
        while (back is not None and t >= back and miss < NAME_MISS and n < HARD_MOST) or back is None and (pre or t < end) \
                and (n < most or not self.cast_read() and not early) and n < HARD_MOST:
            if pre:
                here_t, rows = pre.pop(0)
                f, boxed, self.head = None, [(r, (0, 0, 0, 0)) for r in rows], None
            else:
                here_t, f = t, src.frame(t)
                n += 1
                if f is None:
                    break
                boxed = rows_boxes(f)
                if back is not None:
                    self.head = None             # (read backwards: a heading only heads its own frame)
            rowlog, named = [], False
            as_next = {i for i in range(len(boxed) - 1) if norm(boxed[i + 1][0])[:1] == ["as"] and len(norm(boxed[i + 1][0])) >= 2}
            for i_row, (text, _) in enumerate(boxed):
                w = norm(text)
                here, got, spans = set(), [], []
                rh = head_of(w)
                if not rh and i_row in as_next:  # (a name, "as" its character below it: cast)
                    rh = "cast"
                if rh:
                    self.head = rh
                for a in range(len(w)):
                    for b in range(a + 2, min(a + 4, len(w)) + 1):
                        k = " ".join(w[a:b])
                        found = ix.person(w[a:b])
                        here.update(found)
                        if found:
                            spans.append((a, b))
                            named = True
                        if k in seen or not found:
                            continue
                        seen.add(k)
                        got.append(k)
                        self.cast_pts.clear()
                        for tt, wt in self.credit_points(found, self.head).items():
                            score[tt] += wt
                            why[tt].add(k)
                            pers[tt] += wt
                            self.cast[tt] += self.cast_pts.get(tt, 0)
                            for kk in pend.pop(tt, []):      # (its characters read before: now they count)
                                score[tt] += 1
                                why[tt].add(kk)
                                self.cast[tt] += 1
                pk = "pair " + " ".join(w)
                if spans and not (rh and rh != "cast") and pk not in seen:
                    rest = {x for i, x in enumerate(w) if not any(a <= i < b for a, b in spans) and len(x) >= 3 and x not in PAIR_STOP}
                    for tt in ({t_ for p in here for t_, ws in ix.plays(p).items() if any(near_word(x, ws) for x in rest)}
                               if rest else ()):
                        seen.add(pk)
                        got.append(pk)
                        score[tt] += PAIR_BONUS
                        why[tt].add(pk)
                        self.cast[tt] += PAIR_BONUS
                if not spans and not rh and 3 <= len(w) <= 5 and pk not in seen:
                    for tt, pp, cw, aw in self.fuzzy_pairs(w):      # (a misread actor beside the character they played)
                        seen.add(pk)
                        got.append("fuzzy " + " ".join(w))
                        score[tt] += 1.0 + PAIR_BONUS
                        why[tt].add("fuzzy " + " ".join(aw))
                        pers[tt] += 1.0
                        self.cast[tt] += 1.0 + PAIR_BONUS
                        for kk in pend.pop(tt, []):
                            score[tt] += 1
                            why[tt].add(kk)
                            self.cast[tt] += 1
                if not (rh and rh != "cast"):    # (a job heading is no character)
                    for a in range(len(w)):
                        for b in range(a + 1, min(a + 4, len(w)) + 1):
                            k = "char " + " ".join(w[a:b])
                            if k in seen or any(a < y and x < b for x, y in spans):
                                continue
                            hits = {tt for tt, p in ix.characters(w[a:b]) if b - a >= 2 or p in here}
                            if hits:
                                seen.add(k)
                                got.append(k)
                                for tt in hits:
                                    if not pers[tt]:   # (a character counts once one of the film's people does)
                                        pend.setdefault(tt, []).append(k)
                                    else:
                                        score[tt] += 1
                                        why[tt].add(k)
                                        self.cast[tt] += 1
                if w:
                    rowlog.append({"text": text, "got": got})
            if back is not None:
                if named:
                    state["first"], miss = here_t, 0
                else:
                    miss += 1
            self.frames.append({"t": fmt(here_t), "rows": rowlog, "sparse": f is None})
            if f is None:                         # (a sparse card's rows: scored, nothing to stop on yet)
                continue
            _, win = self.close_window(score, why, src.dur)
            lid = int(win[0][6][2:]) if win else None
            one = len(win) == 1
            held = held + 1 if one and lid == last else 0
            last = lid if one else None
            wset = frozenset(int(x[6][2:]) for x in win)
            same = same + 1 if wset == last_set else 0
            last_set = wset
            ready = n >= 3 and not (cards_until and t < cards_until) and self.cast_read()
            if one and held >= quiet and ready and self.known(lid) and self.cast.get(lid, 0) >= CAST_AGREE:
                self.stopped = "the window held one film %d frames" % held
                break
            if ready and 1 < len(win) <= 5 and same >= quiet and self.cast.get(lid, 0) >= CLOSE_CONFIDENT:
                won = self.tie_break(collections.Counter({int(x[6][2:]): x[0] for x in win}), self.texts(), n=len(win))
                if won is not None and self.known(won):     # (the full casts narrow the window to one)
                    self.decided, self.stopped = won, "tie-break"
                    break
            t += -NAME_STEP if back is not None else CARD_GAP if cards_until and t < cards_until else gap
        if back is None:
            state["next"], state["more"] = t, self.stopped is None and t < end
        return score, why, n, gap

    def texts(self):
        return [x["text"] for fr in self.frames for x in fr["rows"]]

    # -- the MPA card
    def crop_rows(self, t, box, f=None):
        """The text read all round the logo (a card's number may be above it, above its box, below its name or
        beside it), on the full frame at t (or f, already grabbed): the lines nearest the logo first."""
        f = self.src.frame(t) if f is None else f
        if f is None:
            return [], None
        H, W = f.shape[:2]
        x, y, w, h = box
        x0, x1 = int(max(0, (x - CARD_SIDE * w) * W)), int(min(W, (x + w + CARD_SIDE * w) * W))
        y0, y1 = int(max(0, (y - CARD_ABOVE * h) * H)), int(min(H, (y + h + CARD_BELOW * h) * H))
        cx, cy = (x + w / 2) * W - x0, (y + h / 2) * H - y0
        got = menus.rapid_lines(_rgb(f[y0:y1, x0:x1])) or []
        near = lambda q: ((q[1][0] + q[1][2]) / 2 - cx) ** 2 / (w * W) ** 2 + ((q[1][1] + q[1][3]) / 2 - cy) ** 2 / (h * H) ** 2
        return [t_ for t_, _, _ in sorted(got, key=near)], f

    def card_whole(self, d, side):
        """A card cut by the frame's edge, at d s from the end: the frame 1-3 s away where it's whole -- earlier when
        it's at the top (the credits move up: it was lower), later at the bottom. (d, score, box) or None."""
        src = self.src
        for dd in ((d + 1, d + 2, d + 3) if side == "top" else (d - 1, d - 2, d - 3)):
            if not 0 < dd < src.dur:
                continue
            f = self.walk_f.get(dd)
            if f is None:
                f = src.frame(src.dur - dd, small=True)
                if f is None:
                    continue
                self.walk_f[dd] = f
            sc, box = find_logo(f)
            if sc >= LOGO_MIN and not card_side(box):
                return dd, sc, box
        return None

    def card_read(self):
        """The MPA card by its logo: from LOGO_START s before the end outward (2 s steps, small frames only
        shape-checked; pictures there: coarser, and on out to LOGO_FAR / LOGO_SPAN); on a logo, the crop around it
        read (the rating card skipped), the number voted over that frame and the ones a second or two either
        side; the copyright years from the card frame's full read and the frames nearest it (the © line sits
        below or near the card). (number or None, {year: 1.0}, numbers read, secs from the end)"""
        src = self.src
        order, k = [], 0
        while True:
            a, b = LOGO_START - 2 * k, LOGO_START + 2 * k
            if a < 5 and b > LOGO_USUAL:
                break
            for d in ([a] if k == 0 else [a, b]):
                if 5 <= d <= LOGO_USUAL and d < src.dur:
                    order.append(d)
            k += 1
        self.walk_f = {}
        weak = []

        def walk():
            blank, scene, coarse = 0, False, False
            for i, d in enumerate(order):
                if coarse and (d - LOGO_START) % 4:
                    continue
                f = src.frame(src.dur - d, small=True)
                if f is None:
                    continue
                self.walk_f[d] = f
                sc, box = find_logo(f)
                if sc >= LOGO_MIN:
                    yield d, sc, box
                    continue
                side, part = logo_cut(f)
                if part >= PART_MIN:              # (a logo cut by the edge: where its card is whole)
                    got = self.card_whole(d, side)
                    if got:
                        yield got
                        continue
                if sc >= LOGO_WEAK:
                    weak.append((sc, d, box))
                if i < 5 or d >= LOGO_START and not scene:
                    texty = len(lines_of(f, src.h)) >= 2
                    blank = blank + 1 if not texty and i < 5 else blank
                    if i == 4 and blank == 5:
                        scene = coarse = True     # (no text by the usual card: a scene; the budget still walked, coarser)
                    if not texty and d >= LOGO_START:
                        scene = True              # (a picture past the usual card: the credits end earlier)
            if scene:
                seen_text = False
                for j, d in enumerate(range(LOGO_USUAL + 4, (LOGO_FAR if coarse else LOGO_SPAN) + 1, 6 if coarse else 3)):
                    if d >= src.dur:
                        break
                    f = src.frame(src.dur - d, small=True)
                    if f is None:
                        continue
                    self.walk_f[d] = f
                    if not seen_text:
                        seen_text = len(lines_of(f, src.h)) >= 2
                        if not seen_text and j >= (99 if coarse else 7):
                            break
                    sc, box = find_logo(f)
                    if sc >= LOGO_MIN:
                        yield d, sc, box
                    elif sc >= LOGO_WEAK:
                        weak.append((sc, d, box))
            for sc, d, box in sorted(weak, reverse=True)[:LOGO_WEAK_READ]:   # (no clear logo: the likeliest weak ones, read in full)
                yield d, sc, ("weak", box)

        for d, sc, box in walk():
            if isinstance(box, tuple) and box and box[0] == "weak":
                # (a weak match: maybe the logo, maybe something else -- the numbers near it first, those the table
                #  knows; then the whole screen's labelled ones ("No. ..."), as when its position is wrong)
                wbox, box = box[1], None
                full = src.frame(src.dur - d)
                if full is None:
                    continue
                near = [n for n in nums_near(self.crop_rows(src.dur - d, wbox, f=full)[0]) if self.cert_lookup([n])[0]]                     if wbox else []
                rows = rows_of(full)
                strict = any(MPA_ROW.search(r) or NEIGHBOURS.search(r) or NUM_ROW.search(r) and not NOT_NUM.search(r)
                             for r in rows)              # (the card, its neighbours, or its number row)
                if not near and not strict:
                    continue
                if any(RATING_CARD.search(r) for r in rows) and not near:
                    continue
                reads = near + (nums_in(rows) if strict else [])
            else:
                rows, full = self.crop_rows(src.dur - d, box)
                if any(RATING_CARD.search(r) for r in rows):
                    continue                               # (the rating card: its logo, no number)
                reads = nums_near(rows)
            if not reads and box is not None and card_side(box):
                got = self.card_whole(d, card_side(box))  # (its card off the frame, its number with it)
                if got:
                    d, sc, box = got
                    rows, full = self.crop_rows(src.dur - d, box)
                    reads = nums_near(rows)
            if not reads and box is not None:
                continue                                   # (a clear logo with no number by it: on)
            for dd in (d - 1, d + 1, d - 2, d + 2):        # (a second or two either side: the vote)
                top = collections.Counter(reads).most_common(1)[0][1] if reads else 0
                if len(reads) >= 3 and top >= 2 or top >= 3:
                    break
                if box is None:
                    g = src.frame(src.dur - dd)
                    reads += nums_in(rows_of(g)) if g is not None else []
                    continue
                g = src.frame(src.dur - dd, small=True)
                sc2, box2 = find_logo(g) if g is not None else (0, None)
                if sc2 >= LOGO_WEAK:
                    rr, _ = self.crop_rows(src.dur - dd, box2)
                    reads += nums_near(rr)
            num = collections.Counter(reads).most_common(1)[0][0] if reads else None
            years, loose = set(), set()
            for dd in (d, d - 4, d + 4, d - 8, d + 8, d - 12, d + 12, d - 16, d + 16, d - 20, d + 20, d - 24, d + 24):
                if dd < 1 or dd > src.dur:
                    continue
                fr = full if dd == d else src.frame(src.dur - dd)
                if fr is None or dd != d and not lines_of(fr, src.h):
                    continue
                for r in rows_of(fr):
                    if copyright_row(r):
                        years |= row_years(r)
                    elif company_row(r):
                        loose |= row_years(r)
                if years:
                    break
            years = years or loose                          # (no copyright line: a year beside a company name)
            return num, {y: 1.0 for y in years}, reads, d
        return None, {}, [], None

    def cert_lookup(self, nums):
        """The first number read (or a reading of it: O/I/l as digits, then core.number_readings) the table knows:
        (its films, the number), or (None, None)."""
        for raw in nums:
            cand = [raw.translate(str.maketrans("OoIlSsBZ", "00115582"))]
            cand += [x for x in core.number_readings(raw) if x.isdigit()]
            for c in cand:
                if c.isdigit() and str(int(c)) in self.certs:
                    return set(self.certs[str(int(c))]), str(int(c))
        return None, None

    def number_pick(self):
        """The film the number names, taken at its word: one film (of several, the film type with the most votes),
        its year the copyright year or the one after; a year is required. (film, None) or (None, why not)."""
        if not self.cert[0]:
            return None, "no number" if not self.cert[2] else "not in the table"
        if not self.years:
            return None, "no year read"
        films = [(t,) + self.ix.title(t)[1:3] for t in self.cert[0]]
        ys = [int(y) for y in self.years]
        fit = [f for f in films if any(f[1] in (y, y + 1) for y in ys)]
        if not fit:
            return None, "year disagrees (%s vs %s)" % (ys, [f[1] for f in films])
        fit.sort(key=lambda f: (f[2] != "movie", -self.ix.votes(f[0])))
        return fit[0][0], None

    # -- the end credits' start
    def walk_crawl(self, a, b, dt):
        """Two walk frames dt s apart (a earlier): a crawl between them (rows moving up together), read as names."""
        A = [l for l in lines_of(a, self.src.h) if l[2] - l[0] > 0.05]
        B = [l for l in lines_of(b, self.src.h) if l[2] - l[0] > 0.05]
        if len(A) < 3 or len(B) < 3:
            return False, 0.0
        dys = [la[1] - lb[1] for la in A for lb in B
               if abs(la[0] - lb[0]) < 0.012 and abs(la[2] - lb[2]) < 0.012 and 0.003 * dt < la[1] - lb[1] < 0.3 * dt]
        tol = 0.006 * max(1.0, dt / 2)
        best = max(((sum(1 for e in dys if abs(e - d) < tol), d) for d in dys), default=(0, 0.0))
        if best[0] < 3:
            return False, 0.0
        return sum(1 for r in rows_of(a) if sum(1 for x in norm(r) if len(x) >= 2) >= 2) >= 3, best[1] / dt

    def head_start(self):
        """What the card walk already saw: the earliest walk pair that's a crawl (its time and speed), and the
        seconds-from-end it showed as no text. (hi or None, speed, {seconds: no text})"""
        if not self.walk_f:
            return None, 0.0, set()
        blank = {d for d, f in self.walk_f.items() if len([l for l in lines_of(f, self.src.h) if l[2] - l[0] > 0.05]) < 3}
        for d in sorted(self.walk_f, reverse=True):
            e = next((x for x in (d - 2, d - 3, d - 4) if x in self.walk_f), None)
            if e is None or d in blank or e in blank:
                continue
            on, sp = self.walk_crawl(self.walk_f[d], self.walk_f[e], d - e)
            if on:
                self.log("the card walk saw a crawl %d s before the end" % d)
                return self.src.dur - d, sp, blank
        return None, 0.0, blank

    def back_over_breaks(self, start, speed):
        """End credits broken by cards, gaps or sections: step back from the stretch found every BACK_STEP s
        (BACK_MOST at most) while frames are credits, past up to BACK_FILM-1 frames that aren't; then halve to the
        first credits frame. (start of it all, its speed)"""
        src = self.src
        first, miss, t, floor = start, 0, start - BACK_STEP, max(src.dur * 0.80, start - BACK_MOST)
        while t >= floor and miss < BACK_FILM:
            kind, sp = self.probe(t)
            if kind in ("crawl", "card"):
                first, miss = t, 0
                speed = sp or speed
            elif kind == "film":
                miss += 1
            t -= BACK_STEP
        if first == start:
            return start, speed
        lo, hi = max(first - BACK_STEP, floor), first
        while hi - lo > 3:
            mid = (lo + hi) / 2
            on, sp = self.credits_at(mid)
            if on:
                hi, speed = mid, sp or speed
            else:
                lo = mid
        return hi, speed

    def find_start(self):
        """The credits' start: a crawl near the end (the card walk's, else probes 15 s, 30 s ... from it), then back
        chapter by chapter while a chapter's start is credits (two without: they begin after the later), halved,
        then back over breaks. (start or None, speed)"""
        src = self.src
        floor = src.dur * 0.80
        hi, speed, blank = self.head_start()
        for back in () if hi is not None else (15, 30, 45, 60, 75, 90, 105, 120, 180, 300, 600, 1200, 2400):
            t = src.dur - back
            if t < floor:
                break
            if any(abs(back - d) <= 1 for d in blank):     # (the card walk saw no text here: no crawl to find)
                continue
            on, speed, _ = self.crawl(t)
            if on:
                hi = t
                break
        if hi is None:
            return None, 0.0
        lo, film_marks = None, 0
        for m in reversed([c for c in src.chapters if floor <= c < hi - 2]):
            kind, sp = self.probe(m + 2)
            if kind == "sparse":
                continue
            if kind in ("crawl", "card"):
                hi, speed, film_marks, lo = m + 2, sp or speed, 0, None
            else:
                film_marks += 1
                lo = lo if lo is not None else m + 2
                if film_marks >= 2:
                    break
        if lo is None:                                      # (no chapter marks to stand on: back in doubling steps)
            step = 30
            while hi - step >= floor:
                on, sp = self.credits_at(hi - step)
                if not on:
                    lo = hi - step
                    break
                hi, speed, step = hi - step, sp or speed, step * 2
            lo = lo if lo is not None else floor
        while hi - lo > 2:
            mid = (lo + hi) / 2
            on, sp = self.credits_at(mid)
            if on:
                hi, speed = mid, sp or speed
            else:
                lo = mid
        return self.back_over_breaks(hi, speed)

    def earlier_credits(self, before, speed):
        """Credits before `before`, past scenes or cards between (every BACK_STEP s, HUNT_SPAN back at most, not
        before the film's last quarter): where that earlier stretch starts, or None."""
        t, floor = before - BACK_STEP, max(self.src.dur * 0.75, before - HUNT_SPAN)
        while t >= floor:
            kind, sp = self.probe(t)
            if kind in ("crawl", "card"):
                return self.back_over_breaks(t, sp or speed)
            t -= BACK_STEP
        return None, speed

    # -- the scans
    def end_scan(self):
        """The end credits, read: (ranking, window, why it stopped); a window of one is the winner."""
        src = self.src
        start, speed = self.find_start()
        if start is None:
            self.log("no end credits found")
            return [], [], "no end credits"
        stretch = self.first_crawl if self.first_crawl and self.first_crawl > start else start
        self.log("the end credits start at %s" % fmt(start))
        st = {}
        cu = stretch if start < stretch else None
        score, why, n, gap = self.read_crawl(start, speed, cards_until=cu, state=st, most=EARLY, early=True)
        back = start
        if st["more"] and not self.cast_read():     # (no cast in the crawl's first frames: the names just before it first)
            self.read_crawl(start - NAME_STEP, speed, state=st, back=max(src.dur * 0.75, start - HUNT_SPAN))
            if st["first"] is not None:
                back = st["first"]
        if self.stopped is None and st["next"] < src.dur - 1:     # (on through the crawl)
            t1 = st["next"]
            self.read_crawl(t1, speed, state=st, cards_until=cu if cu and t1 < cu else None)
        while not self.cast_read() and self.stopped is None:      # (no cast yet: credits earlier, past scenes between)
            self.read_crawl(back - NAME_STEP, speed, state=st, back=max(src.dur * 0.75, back - HUNT_SPAN))
            s1 = st["first"]
            if s1 is not None and s1 < back:
                back = s1
                continue
            if self.stopped is not None:
                break
            s2, sp2 = self.earlier_credits(back, speed)
            if s2 is None or s2 >= back:
                break
            self.read_crawl(s2, sp2 or speed, state=st, until=back)
            back = s2
        texts = self.texts()
        if not self.years:
            self.years.update(copyright_years(texts))
        r = self.rank_cast(score, why, src.dur) if self.cast_read() else self.rank(score, why, src.dur)
        near = len(r) > 1 and not clear(r[0][0], r[1][0])
        if near and self.decided is None:              # (a close race after reading: the full casts)
            won = self.tie_break(collections.Counter({int(x[6][2:]): x[0] for x in r[:5]}), texts, n=5,
                                 clean=self.siblings(self.year_view(score), why))
            if won is not None:
                self.decided = won
        share, headed = restoration_share(texts)
        lead = score.most_common(1)[0][1] if score else 0
        if (share >= 0.15 or headed) and lead < STRONG and self.decided is None:
            self.log("the end credits are a restoration's, not the film's")
            return [], [], "restoration credits"
        if self.decided is not None and r and self.tie_log:     # (the tie step's pick stands only if no film it
            won = self.ix.title(self.decided)[0]                 #  weighed ranks clearly above it)
            weighed = {x[0] for x in self.tie_log[-1]}
            ww = next((x[0] for x in r if x[2] == won), None)
            if any(x[2] in weighed and x[2] != won and (ww is None or clear(x[0], ww)) for x in r):
                self.decided = None
        if self.decided is not None:
            won = [x for x in r if int(x[6][2:]) == self.decided]
            if not won:                             # (not in the final ranking: its own entry)
                title, year, typ, rt = self.ix.title(self.decided)
                won = [(99.0, score[self.decided], title, year, typ, rt, "tt%07d" % self.decided)]
            return won + [x for x in r if x not in won], won, self.stopped or "the full casts"
        return r, self.window_of(r), self.stopped or "the credits read"

    def front_scan(self):
        """The opening read straight from the start: a small frame every FRONT_STEP s, the any-size text check, a
        new card read in full into the same scoring and window; stops on the window holding one film for 3 cards, a
        certificate number naming one film with its year agreeing (or 2 of that film's names read), or the cards over (names read, past chapter 2,
        FRONT_QUIET picture frames in a row); FRONT_CAP s at most. (ranking, window, why it stopped)"""
        src = self.src
        src.keyframes = True              # (a straight read's steps: a disc's entry points will do, a third the reading)
        try:
            return self._front_scan()
        finally:
            src.keyframes = False

    def _front_scan(self):
        src = self.src
        self.cast, self.sparse, self.head, self.decided, self.stopped = collections.Counter(), [], None, None, None
        self.years, self.cert = {}, [None, None, []]
        ch2 = next((c for c in src.chapters if c > 5), None)
        st, t, quiet, last_boxes, held, last, nums, seen_names = {}, 0.0, 0, None, 0, None, [], False
        score, why = collections.Counter(), collections.defaultdict(set)
        stop = None
        while t <= min(FRONT_CAP, src.dur * 0.5):
            f = src.frame(t, small=True)
            if f is None:
                t += FRONT_STEP
                continue
            boxes = [b for b in text_boxes(f, src.h) if b[2] - b[0] > 0.02]
            if not boxes:
                quiet += 1
                if seen_names and quiet >= FRONT_QUIET and (ch2 is None or t > ch2):
                    stop = "the cards are over"
                    break
                t += FRONT_STEP
                continue
            quiet = 0
            if same_card(boxes, last_boxes):
                t += FRONT_STEP
                continue
            last_boxes = boxes
            if not any(norm(x) for x in rows_of(_cv2().resize(f, (src.w, src.h)))):   # (the small frame read first,
                t += FRONT_STEP                                                         #  no grab: scenery, not a card)
                continue
            n0 = len(self.frames)
            score, why, _, _ = self.read_crawl(t, 0.3, state=st, most=1, early=True)   # (the card read in full, scored)
            seen_names = seen_names or sum(score.values()) > 0
            if self.cert[0]:              # (a number read before, no year to back it: 2 of its film's names do)
                named = [x for x in self.cert[0] if len([k for k in why[x] if not k.startswith("char ")]) >= 2]
                if named:
                    self.decided, stop = named[0], "the number %s and its film's names" % self.cert[1]
                    break
            rows = [x["text"] for fr in self.frames[n0:] for x in fr["rows"]]
            for row in rows:
                if copyright_row(row):
                    for y in row_years(row):
                        self.years[y] = 1.0
            got = nums_in(rows)
            if got:
                nums += got
                self.cert[0], self.cert[1] = self.cert_lookup(got)
                self.cert[2] = nums
                pick, _ = self.number_pick()
                if pick:
                    self.decided, stop = pick, "the number %s" % self.cert[1]
                    break
            _, win = self.close_window(score, why, src.dur)
            lid = int(win[0][6][2:]) if len(win) == 1 else None
            held = held + 1 if lid is not None and lid == last else 0
            last = lid
            if lid is not None and held >= 3 and self.known(lid) and self.cast_read():
                stop = "the window held one film %d cards" % held
                break
            t += FRONT_STEP
        r, win = self.close_window(score, why, src.dur)
        if self.decided is not None:
            title, year, typ, rt = self.ix.title(self.decided)
            win = [x for x in r if int(x[6][2:]) == self.decided][:1] or \
                [(99.0, score[self.decided], title, year, typ, rt, "tt%07d" % self.decided)]
        return r, win, stop or ("the cap" if t > FRONT_CAP else "the opening's end")

    def _film(self, x, by):
        return {"imdb_id": x[6], "name": x[2], "year": x[3], "type": x[4], "runtime": x[5], "score": round(x[0], 1),
                "names": len(self.people_of(x)), "by": by}

    def scan(self, src):
        """Identify a title by its credits: {"winner": a film or None, "window": [films], "by": "the MPA card" /
        "the end credits" / "the opening credits" / None, "number", "years", "why", "grabs", "seconds", "steps"}.
        A film: {"imdb_id", "name", "year", "type", "runtime", "score", "names", "by"}."""
        t0 = time.time()
        self._reset(src)
        steps = []

        def done(winner, window, by, why):
            return {"winner": winner, "window": window, "by": by, "number": self.cert[1], "years": sorted(self.years),
                    "why": why, "grabs": src.grabs, "seconds": round(time.time() - t0, 1), "steps": steps}

        # 1. the MPA card
        num, ys, reads, card_at = self.card_read()
        self.years.update(ys)
        self.cert = [None, None, reads]
        votes = collections.Counter(reads)
        pick, why_not = None, "no number"
        for cand in sorted(votes, key=lambda n: (-votes[n], reads.index(n))):   # (the most read, then the nearest)
            self.cert[0], self.cert[1] = self.cert_lookup([cand])
            pick, why_not = self.number_pick()
            if pick:
                break
        if not pick and num:
            self.cert[0], self.cert[1] = self.cert_lookup([num])
        steps.append("the MPA card: %s" % ("number %s, %s" % (self.cert[1] or num, "the film" if pick else why_not)
                                           if num else why_not))
        self.log(steps[-1])
        if pick:
            title, year, typ, rt = self.ix.title(pick)
            film = self._film((99.0, 0, title, year, typ, rt, "tt%07d" % pick), "the MPA card")
            return done(film, [film], "the MPA card", "its certificate number %s (%s)" % (self.cert[1], year))
        self.cert[0] = None                       # (a number its year doesn't back: dropped; its year kept)
        card_years = dict(self.years)

        # 2. the end credits
        r, win, why = self.end_scan()
        steps.append("the end credits: %s, %s" % (why, "one film" if len(win) == 1 else "%d close" % len(win)))
        self.log(steps[-1])
        end_win = [self._film(x, "the end credits") for x in win]
        if len(end_win) == 1:
            return done(end_win[0], end_win, "the end credits", "its end credits (%s)" % why)

        # 3. the opening credits
        r, win, why = self.front_scan()
        steps.append("the opening credits: %s, %s" % (why, "one film" if len(win) == 1 else "%d close" % len(win)))
        self.log(steps[-1])
        self.years = card_years or self.years
        front_win = [self._film(x, "the opening credits") for x in win]
        if len(front_win) == 1:
            return done(front_win[0], front_win, "the opening credits", "its opening credits (%s)" % why)
        # (no winner: the films both windows hold first, then the opening's, then the end's)
        ends = {x["imdb_id"] for x in end_win}
        both = [x for x in front_win if x["imdb_id"] in ends]
        merged = both + [x for x in front_win if x not in both] + \
            [x for x in end_win if x["imdb_id"] not in {y["imdb_id"] for y in front_win}]
        return done(None, merged[:CONTENDERS], None, "no clear winner")


# ---- the MPA numbers' table -------------------------------------------------------------------------------------

MPA_LIST = "https://raw.githubusercontent.com/Richienb/mpaa-list/HEAD/readme.md"   # (CC0: number, year, title)
MPA_QUERY = "SELECT ?cert ?imdb WHERE { ?item wdt:P14671 ?cert ; wdt:P345 ?imdb . }"   # (CC0: number, IMDb id)
_NUM_WORDS = dict(zip("one two three four five six seven eight nine ten eleven twelve".split(), map(str, range(1, 13))))
_RANK = {"movie": 0, "tvMovie": 1, "video": 2, "tvSpecial": 3, "short": 4}


def _mpa_name(t):
    """A title for matching: "Great American Escape, The" = "the great american escape", "Three Ninjas" = "3 ninjas"."""
    t = (t or "").lower()
    m = re.match(r"(.*), (the|a|an)$", t)
    if m:
        t = m.group(2) + " " + m.group(1)
    return " ".join(_NUM_WORDS.get(x, x) for x in re.sub(r"[^a-z0-9 ]", " ", t.replace("&", "and")).split())


def mpa_fetch(timeout=60):
    """Both sources, fetched: ([(number, "tt…")] from Wikidata's P14671, [(number, title, year)] from the list).
    Only the queries are sent (the app's name for a user agent)."""
    import urllib.parse
    import urllib.request
    ua = core.USER_AGENT + " (https://github.com/b-town-bit/disc-detective)"
    url = core.WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({"query": MPA_QUERY, "format": "json"})
    req = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json", "User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rows = json.load(r).get("results", {}).get("bindings") or []
    wd = [(x["cert"]["value"], x["imdb"]["value"]) for x in rows if "cert" in x and "imdb" in x]
    with urllib.request.urlopen(urllib.request.Request(MPA_LIST, headers={"User-Agent": ua}), timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
    lst = []
    for line in text.splitlines():
        p = line.strip().strip("|").split("|")
        if len(p) == 3:
            lst.append((p[0], p[2], p[1]))
    return wd, lst


def mpa_build(imdb_dir, wikidata_rows, list_rows, out_path=None):
    """The MPA certificate numbers' table (MPA_FILE, beside the IMDb index): Wikidata's by IMDb id, the list's
    matched to the IMDb index by title (or an alternate title) and year: a feature film first, then TV films,
    videos, specials, shorts; its year, a year or two after (a list's year is often the rating's), or one before.
    A title naming more than 3 films is left out. out_path: elsewhere than beside the index (a test).
    Returns (numbers, numbers naming 2 films or more)."""
    ti = sqlite3.connect(os.path.join(imdb_dir, core.IMDB_DB))
    try:
        by, typ = collections.defaultdict(set), {}
        for i, title, alt, year, ty in ti.execute("select id, title, alt, year, type from titles where type in "
                                                  "('movie','video','tvMovie','tvSpecial','short') and year is not null"):
            typ[i] = ty
            for t in [title] + [a for a in (alt or "").split("|") if a]:
                by[(_mpa_name(t), year)].add(i)
    finally:
        ti.close()
    out = collections.defaultdict(set)
    for cert, imdb in wikidata_rows:
        if re.fullmatch(r"tt\d+", imdb or "") and str(cert).strip().isdigit():
            out[str(int(cert))].add(int(imdb[2:]))
    for cert, title, year in list_rows:
        if not (str(cert).strip().isdigit() and str(year).strip().isdigit()):
            continue
        y, hits = int(year), None
        for best in range(5):
            for yy in (y, y + 1, y + 2, y - 1):
                got = {i for i in by.get((_mpa_name(title), yy), ()) if _RANK.get(typ.get(i), 9) == best}
                if got:
                    hits = got
                    break
            if hits:
                break
        if hits and len(hits) <= 3:
            out[str(int(cert))] |= hits
    path = out_path or os.path.join(imdb_dir, MPA_FILE)
    with open(path + ".new", "w", encoding="utf-8") as fh:
        json.dump({k: sorted(v) for k, v in out.items()}, fh)
    os.replace(path + ".new", path)
    return len(out), sum(1 for v in out.values() if len(v) > 1)


def mpa_stale(imdb_dir):
    """Whether the table wants building: there's none, or the IMDb index is newer."""
    path, index = os.path.join(imdb_dir, MPA_FILE), os.path.join(imdb_dir, core.IMDB_DB)
    if not os.path.exists(index):
        return False
    return not os.path.exists(path) or os.path.getmtime(index) > os.path.getmtime(path)


def mpa_refresh(imdb_dir, log=None, force=False):
    """Fetch both sources and build the table, if it's stale (or force); a failure raises and keeps the one there
    (tried again next time)."""
    log = log or (lambda m: None)
    if not force and not mpa_stale(imdb_dir):
        return False
    log(core.Said("fetching the MPA certificate numbers ..."))
    numbers, shared = mpa_build(imdb_dir, *mpa_fetch())
    log(core.Said("MPA certificate numbers: {count}", count=numbers))
    return True
