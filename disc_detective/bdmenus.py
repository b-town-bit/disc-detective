"""A Blu-ray's menus from its own files, before anything is played (no VLC, no Java): which
program runs its menus (index.bdmv: HDMV commands or a BD-J application), and, for a studio
menu framework whose project file names the disc's playlists, what each title is.

Sony Pictures' BD-J framework ("radius", Muppets from Space's Blu-ray) keeps its menu project in
BDMV/JAR/<folder>/bluray-project.bin: each playlist by name ("FPL_MainFeature", "SF_Cardshark_
Serenade", "SF_Theatrical_Trailer"), its number, frame rate and running time. Its names are the
menus' own words, exact: no picture to read. (BD-J menus need Java to run; this needs nothing.)
The menus' AV streams are encrypted (AACS); these files aren't."""
import os
import re
import struct

from . import tools

PROJECT_NAMES = ("bluray-project.bin",)
FRAMEWORK_FILES = (("Sony", "bluray-project.bin"), ("Warner", "menu_base.prop"))
ENTRY_RE = re.compile(rb"(?:F?PL_|SF_|TMPL )[ -~]{2,60}")
LENGTH_FIT = 0.02        # a name's running time within this of its playlist's (and 2 s) is its


def _u16(d, o):
    return struct.unpack(">H", d[o:o + 2])[0]


def _u32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


def menu_program(root):
    """What runs a Blu-ray's menus (index.bdmv): {"first_play", "top_menu": ("bdj", "00000") or
    ("hdmv", movie object), "titles": [...]}; None without one."""
    path = os.path.join(root, "BDMV", "index.bdmv")
    try:
        with open(path, "rb") as fh:
            d = fh.read()
    except OSError:
        return None
    if d[:4] != b"INDX":
        return None

    def obj(b):
        kind = b[0] >> 6
        if kind == 1:
            return ("hdmv", _u16(b, 6))
        if kind == 2:
            return ("bdj", b[6:11].decode("ascii", "replace"))
        return (None, None)
    p = _u32(d, 8) + 4
    first, top = obj(d[p:p + 12]), obj(d[p + 12:p + 24])
    n = _u16(d, p + 24)
    titles = [obj(d[p + 26 + 12 * i: p + 38 + 12 * i]) for i in range(n)]
    return {"first_play": first, "top_menu": top, "titles": titles}


def _framework(root):
    """The studio menu framework whose files a disc has under BDMV/JAR, or None."""
    jar = os.path.join(root, "BDMV", "JAR")
    if os.path.isfile(os.path.join(jar, "onQClient.cfg")):
        return "onQ"
    try:
        folders = sorted(os.listdir(jar))
    except OSError:
        return None
    for folder in folders:
        for framework, name in FRAMEWORK_FILES:
            if os.path.isfile(os.path.join(jar, folder, name)):
                return framework
    return None


def menu_style(root):
    """What kind of menus a Blu-ray has, off its files in a moment: {"menu": "BD-J", "HDMV" or None,
    "framework": "Sony", "Warner", "onQ" or None}. A framework's files make it BD-J, whatever starts
    it; else its first play and top menu say: Java in either makes it BD-J, though other titles may
    be HDMV (Muppets from Space's)."""
    framework = _framework(root)
    try:
        program = menu_program(root) or {}
    except (struct.error, IndexError):       # (a broken index: as good as none)
        program = {}
    runs = {(program.get(k) or (None,))[0] for k in ("first_play", "top_menu")}
    menu = "BD-J" if framework or "bdj" in runs else "HDMV" if "hdmv" in runs else None
    return {"menu": menu, "framework": framework}


def crawlable(style):
    """Whether a crawl can work a disc of this style (menu_style): HDMV's. A Java menu no reader of ours
    knows is left alone: Arrow's sat on its first play for four minutes, to nothing."""
    return bool(style) and style.get("menu") == "HDMV"


def style_line(style):
    """A disc's menu style for the log: "BD-J, onQ"."""
    style = style or {}
    return ", ".join(x for x in (style.get("menu") or "no menu program found", style.get("framework")) if x)


def project_title(root):
    """The name a menu project gives itself, its film's as a rule (Sony's: "Muppets_From_Space"
    first in bluray-project.bin): "Muppets From Space"; None without one. For a disc whose
    label says nothing (LOGICAL_VOLUME_ID) or in shorthand (HP4), a name to search by."""
    jar = os.path.join(root, "BDMV", "JAR")
    try:
        folders = sorted(os.listdir(jar))
    except OSError:
        return None
    for f in folders:
        for name in PROJECT_NAMES:
            try:
                with open(os.path.join(jar, f, name), "rb") as fh:
                    d = fh.read(256)
            except OSError:
                continue
            n = _u16(d, 0) if len(d) >= 2 else 0
            text = d[2:2 + n].decode("ascii", "replace") if 2 < n < 120 else ""
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_ '&:.,!-]+", text) and re.search(r"[A-Za-z]{3}", text):
                return re.sub(r"\s+", " ", text.replace("_", " ")).strip()
    return None


def _props(path):
    """A menu file's lines ("episode01.playlistNumber=51") as {"episode01": {"playlistNumber": "51"}}."""
    out = {}
    with open(path, encoding="latin1") as fh:
        for line in fh:
            m = re.match(r"\s*(\w+)\.(\w+)=(.*)$", line.rstrip())
            if m:
                out.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    return out


def _wb_folders(root):
    """Warner's BD-J framework (com.wb.bdj; Arrow S1 D1's): the folder of its English menu files, whose
    images.txt names its sheets, or the first with any; and the one with its states.prop. (None, None)."""
    jar = os.path.join(root, "BDMV", "JAR")
    try:
        folders = [os.path.join(jar, f) for f in sorted(os.listdir(jar))]
    except OSError:
        return None, None
    menus_at = [f for f in folders if os.path.isfile(os.path.join(f, "menu_base.prop"))]
    states = next((f for f in folders if os.path.isfile(os.path.join(f, "states.prop"))), None)

    def english(folder):
        try:
            with open(os.path.join(folder, "images.txt"), encoding="latin1") as fh:
                return "_eng" in fh.read()
        except OSError:
            return False
    return next((f for f in menus_at if english(f)), menus_at[0] if menus_at else None), states


def _sheet_reader(folder, read):
    """A menu image, by its name: (its words, as read (a picture to its words) reads them, "" without
    read or the image; and the picture itself, over black, or None), from its piece of a sheet of
    images, as images.txt has it ("name,sheet,left,top,right,bottom")."""
    pieces, sheets = {}, {}
    try:
        with open(os.path.join(folder, "images.txt"), encoding="latin1") as fh:
            for line in fh:
                p = line.strip().split(",")
                if len(p) == 6 and all(x.isdigit() for x in p[2:]):
                    pieces[p[0]] = (p[1], tuple(int(x) for x in p[2:]))
    except OSError:
        pass

    def picture(name):
        if name not in pieces:
            return None
        sheet, box = pieces[name]
        try:
            from PIL import Image
            if sheet not in sheets:
                sheets[sheet] = Image.open(os.path.join(folder, sheet)).convert("RGBA")
            piece = sheets[sheet].crop(box)
            flat = Image.new("RGBA", piece.size, (0, 0, 0, 255))
            flat.alpha_composite(piece)
            return flat.convert("RGB")
        except (OSError, ValueError, ImportError):
            return None

    def words(name):
        image = picture(name) if read else None
        if image is None:
            return ""
        try:
            return read(image.resize((image.width * 2, image.height * 2))).strip()
        except (OSError, ValueError):
            return ""
    return words, picture


def wb_menu(root, label, read=None):
    """What Warner's BD-J framework's plain text files say of this disc (its menu covers the whole set):
    {"season", "disc", "episodes": [{"number", "playlist", "read"}], "extras": [{"playlist", "read", "under",
    "of_episode"}], "picture": a menu image by its name}, or None for another kind of disc, or one its
    files don't list by this label. "playlist" as "00051.mpls". "read": a button's words as read(picture)
    reads them off the menu's sheets, its art ("" without read); "button": the name of its image, or
    None; "under": the words of the button that leads to its page. A "scissor" button beside an episode
    ties a page's extra to that episode (of_episode)."""
    folder, shared = _wb_folders(root)
    if not folder or not label:
        return None
    try:
        props = _props(os.path.join(folder, "menu_base.prop"))
        states = _props(os.path.join(shared, "states.prop")) if shared else {}
    except OSError:
        return None
    words, picture = _sheet_reader(folder, read)

    def own_image(o):
        """A button's image, as it looks selected, where it has one."""
        names = (o.get("images") or "").split(",")
        return names[1] if len(names) > 1 else names[0]
    image_words = lambda o: words(own_image(o))
    episodes = []
    for name, o in props.items():
        m = re.match(r"episode(\d+)$", name)
        if m and o.get("type") == "EpisodeButton" and o.get("discName") == label and (o.get("playlistNumber") or "").isdigit():
            number = int(o.get("episodeStringName") or m.group(1))
            said = re.sub(r"\b0*%d\b" % number, " ", image_words(o))
            episodes.append({"number": number, "playlist": "%05d.mpls" % int(o["playlistNumber"]),
                             "read": re.sub(r"\s+", " ", said).strip(), "button": own_image(o) or None})
    if not episodes:
        return None
    own = {n for n, o in states.items() if o.get("discName") == label}
    pressed = lambda o: re.match(r"ecController\.playEC,(\w+),(\d+)$", o.get("onEnterPressed") or "")
    scissors = {}
    for name, o in props.items():
        m, e = pressed(o), re.match(r"episode(\d+)scis$", name)
        if m and e and m.group(1) in own:
            scissors[int(m.group(2))] = int(e.group(1))
    # the button that opens each page, for what it says of what's on it
    opens = {}
    for o in props.values():
        m = re.match(r"menuController\.activateMenu,(\w+)$", o.get("onEnterPressed") or "")
        if m:
            opens[m.group(1)] = o
    pages = {}
    for page, o in props.items():
        pending = [x for x in (o.get("children") or "").split(",") if x]
        seen = set()
        while pending:
            child = pending.pop()
            if child not in seen:
                seen.add(child)
                pages.setdefault(child, page)
                pending.extend(x for x in (props.get(child, {}).get("children") or "").split(",") if x)
    extras, listed = [], set()

    def opener(name):
        """The button that opens the nearest page holding this object, or None."""
        at, hops = pages.get(name), 0
        while at and hops < 8:
            if at in opens:
                return opens[at]
            at, hops = pages.get(at), hops + 1
        return None
    for name, o in props.items():
        m = pressed(o)
        if not m or m.group(1) not in own or re.match(r"episode\d+scis$", name) or int(m.group(2)) in listed:
            continue
        listed.add(int(m.group(2)))
        by = opener(name)
        extras.append({"playlist": "%05d.mpls" % int(m.group(2)), "read": image_words(o), "under": image_words(by) if by else "",
                       "of_episode": scissors.get(int(m.group(2))), "button": own_image(o) or None})
    for pid, number in scissors.items():
        if pid not in listed:
            extras.append({"playlist": "%05d.mpls" % pid, "read": "", "under": "", "of_episode": number, "button": None})
    season = re.search(r"_S(\d+)(?:_|$)", label, re.I)
    try:
        number = (_props(os.path.join(shared, "disc_set.prop")).get(label) or {}).get("discNum") if shared else None
    except OSError:
        number = None
    return {"season": int(season.group(1)) if season else None, "disc": int(number) if (number or "").isdigit() else None,
            "episodes": sorted(episodes, key=lambda e: e["number"]),
            "extras": extras, "picture": picture}


def menu_project(root):
    """The playlists a disc's menu project names (Sony's BD-J framework: bluray-project.bin
    under BDMV/JAR): [{"name": "SF_Cardshark_Serenade", "playlist": "00745.mpls", "fps", "seconds"}],
    in the file's order, each playlist once (its first name). [] for a disc without one."""
    jar = os.path.join(root, "BDMV", "JAR")
    try:
        folders = sorted(os.listdir(jar))
    except OSError:
        return []
    out, seen = [], set()
    for f in folders:
        for name in PROJECT_NAMES:
            path = os.path.join(jar, f, name)
            try:
                with open(path, "rb") as fh:
                    d = fh.read()
            except OSError:
                continue
            for m in ENTRY_RE.finditer(d):
                i = m.end()
                if i + 12 > len(d):
                    continue
                pid = _u32(d, i)
                fps, secs = struct.unpack(">f", d[i + 4:i + 8])[0], struct.unpack(">f", d[i + 8:i + 12])[0]
                # (a name followed by a playlist's number, a video frame rate and a time: an entry)
                if not (0 < pid < 100000 and 20 <= fps <= 61 and 0 < secs < 200000):
                    continue
                pl = "%05d.mpls" % pid
                if pl in seen:
                    continue
                seen.add(pl)
                out.append({"name": m.group().decode("ascii"), "playlist": pl, "fps": round(fps, 3),
                            "seconds": round(secs, 1)})
    return out


def project_pieces(titles, labels):
    """Named titles that are pieces of another named one: {piece: whole}. A whole plays nothing
    but the pieces' own clips, each exactly (Muppets from Space's "This Elevator is Going Down":
    "This Elevator 1", "2" and "3" back to back; its Play All strings the pieces together, its
    menu lists the whole). Ripping both saves it twice: the pieces are pieces."""
    by = {t["id"]: t for t in titles}
    clips = lambda t: [c for c in re.split(r"[,\s]+", (t or {}).get("clip_list") or (t or {}).get("clips") or "") if c]
    single = {}                         # {its one clip: the named title that's only that}
    for tid in labels:
        cs = clips(by.get(tid))
        if len(cs) == 1:
            single.setdefault(cs[0], tid)
    out = {}
    for tid in labels:
        cs = clips(by.get(tid))
        parts = [single.get(c) for c in cs]
        if len(cs) < 2 or not all(parts) or tid in parts:
            continue
        # (and as long as they are together: the same stretch of each clip)
        if abs(sum(by[p].get("duration") or 0 for p in parts) - (by[tid].get("duration") or 0)) <= 1.0:
            for p in parts:
                out[p] = tid
    return out


def project_play_alls(titles, entries):
    """The titles the menu project calls a Play All ("SF_Play_All": Muppets from Space's outtakes,
    one after another), its running time its playlist's: play-alls, not extras of their own."""
    by = {t["id"]: t for t in titles}
    return [e["playlist"] for e in entries if re.search(r"play[ _]?all", e["name"], re.I) and e["playlist"] in by
            and abs((by[e["playlist"]].get("duration") or 0) - e["seconds"]) <= max(2.0, LENGTH_FIT * e["seconds"])]


# ---- the menus' own words, in their Java code (Sony's framework: a table per menu page and
# language, each button's text and the title it plays) ----

def _class_strings(b):
    """The string constants each method of a Java class file loads, in order (ldc, ldc_w):
    [[...], ...]. [] for what isn't a class file this reads."""
    if b[:4] != b"\xca\xfe\xba\xbe":
        return []
    n, o, pool, i = _u16(b, 8), 10, {}, 1
    while i < n:
        tag = b[o]
        if tag == 1:
            ln = _u16(b, o + 1)
            pool[i] = ("utf8", b[o + 3:o + 3 + ln].decode("utf-8", "replace"))
            o += 3 + ln
        elif tag in (3, 4):
            o += 5
        elif tag in (5, 6):
            o += 9
            i += 1
        elif tag in (7, 8, 16, 19, 20):
            pool[i] = ("string", _u16(b, o + 1)) if tag == 8 else None
            o += 3
        elif tag in (9, 10, 11, 12, 17, 18):
            o += 5
        elif tag == 15:
            o += 4
        else:
            return []
        i += 1
    o += 6
    o += 2 + 2 * _u16(b, o)                       # (its interfaces)

    def members(o, codes):
        count = _u16(b, o)
        o += 2
        for _ in range(count):
            na = _u16(b, o + 6)
            o += 8
            for _ in range(na):
                an, al = _u16(b, o), _u32(b, o + 2)
                if (pool.get(an) or ("", ""))[1] == "Code":
                    codes.append(b[o + 14:o + 14 + _u32(b, o + 10)])
                o += 6 + al
        return o
    codes = []
    o = members(o, [])                            # fields (no code)
    members(o, codes)
    out = []
    for code in codes:
        seq = []
        for k in range(len(code) - 1):
            idx = code[k + 1] if code[k] == 0x12 else _u16(code, k + 1) if code[k] == 0x13 and k + 2 < len(code) else None
            e = pool.get(idx) if idx else None
            if e and e[0] == "string" and (pool.get(e[1]) or ("",))[0] == "utf8":
                seq.append(pool[e[1]][1])
        out.append(seq)
    return out


MENU_ID_RE = re.compile(r"^(?:F?PL_|SF_)")
MENU_NOISE_RE = re.compile(r"^(?:font\d*|text_\d+|[a-z0-9]+(?:_[a-z0-9]+)+|[a-z]{1,8}\d*)$")


def button_texts(seq):
    """A menu page's buttons, from the strings its code loads: [(text, the title id it plays)],
    in the page's order. Each button's text comes once per state (normal, selected...), then
    the id of what it plays ("Cardshark Serenade" ... "SF_Cardshark_Serenade"); the id last
    before the next text is its."""
    pairs, text = [], None
    for s in seq:
        if MENU_ID_RE.match(s):
            if text is not None:
                if pairs and pairs[-1][0] == text:
                    pairs[-1] = (text, s)
                else:
                    pairs.append((text, s))
        elif s.strip() and not MENU_NOISE_RE.match(s):
            text = s
    return pairs


def _key(name):
    """A title id to compare by: "SF_Excuse Me_Pepe" and "SF_ExcuseMe_Pepe" alike."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def menu_texts(root, entries):
    """The menus' own words for the titles a menu project names (entries: menu_project()),
    {playlist: text}, from the menu application's Java code (BDMV/JAR/*.jar): the page tables
    whose ids are this disc's (another film's left in the framework aren't), each id one
    text; of a page's languages, the one whose words are most its ids' (English, on Muppets
    from Space: "Expectorating\\nExtraterrestrials" for SF_Expectorating). Lines joined:
    "Expectorating Extraterrestrials". {} when there's none."""
    import zipfile
    ids = {_key(e["name"]): e["playlist"] for e in entries}
    named = {e["playlist"]: e["name"] for e in entries}
    jar = os.path.join(root, "BDMV", "JAR")
    try:
        jars = [os.path.join(jar, f) for f in sorted(os.listdir(jar)) if f.lower().endswith(".jar")]
    except OSError:
        return {}
    words = lambda s: set(re.findall(r"[a-z0-9]{3,}", s.lower().replace("_", " ")))
    best = {}                                      # {the page's ids, as a set: (how English it is, {playlist: text})}
    for path in jars:
        try:
            z = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile):
            continue
        for name in z.namelist():
            if not name.endswith(".class"):
                continue
            try:
                seqs = _class_strings(z.read(name))
            except (ValueError, IndexError, struct.error):
                continue
            for seq in seqs:
                pairs = [(t, ids[_key(i)]) for t, i in button_texts(seq) if _key(i) in ids]
                plays = [p for _, p in pairs]
                if len(pairs) < 2 or len(set(plays)) != len(plays):      # (each title once: else unsure)
                    continue
                fit = sum(len(words(t) & words(named[p])) for t, p in pairs) / len(pairs)
                page = frozenset(plays)
                if page not in best or fit > best[page][0]:
                    best[page] = (fit, {p: t for t, p in pairs})
    out = {}
    for fit, texts in best.values():
        if fit <= 0:
            continue
        for p, t in texts.items():
            lines = [x.strip() for x in t.split("\n") if x.strip()]
            joined = ""
            for x in lines:
                joined = joined + x if joined.endswith("-") or not joined else joined + " " + x
            # (a space the menu's text keeps inside its quotes: '"Shining Star "')
            joined = re.sub(r'"\s*([^"]*?)\s*"', r'"\1"', joined)
            out.setdefault(p, joined)
    return out


def nice(name):
    """A project name as a title: "SF_Cardshark_Serenade" -> "Cardshark Serenade"."""
    rest = re.sub(r"^(?:F?PL_|SF_)", "", name)
    rest = re.sub(r"\s+", " ", rest.replace("_", " ")).strip()
    return rest


def project_labels(titles, entries, film=None, known_film=None, texts=None):
    """The menu project's word on each title (the shape of core.get_labels: {"00745.mpls":
    {"type", "title", "note"}}), as Detect titles' labels are for a DVD: waiting for you in Edit
    titles. Only a name whose running time is its playlist's (LENGTH_FIT). The film
    ("FPL_MainFeature") a main movie (its region copies, the scan's duplicates, left be); a menu
    template, a Play All: nothing (the scan has them); "Previews", or a film other than this
    one (known_film: core.known_film; film, this disc's film's name): an ad; a trailer of this
    film: Trailer; the rest extras by their names (Muppets from Space's 26 outtakes, which the
    scan, by length alone, took for junk)."""
    from collections import Counter
    from .core import AD_RE
    by = {t["id"]: t for t in titles}
    plain = lambda s: " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))
    # (another film's name isn't enough: Muppets from Space's outtakes are called "Eyes Wide Shut"
    # and "The Return". Its ads are at a film's frame rate, its own extras not: Smurfs, Zoo at
    # 23.976 among outtakes at 29.97. All at one rate: no ads by name)
    rates = Counter(e["fps"] for e in entries if e["name"].startswith("SF_"))
    usual = rates.most_common(1)[0][0] if rates else None
    out = {}
    for e in entries:
        t = by.get(e["playlist"])
        if not t or abs((t.get("duration") or 0) - e["seconds"]) > max(2.0, LENGTH_FIT * e["seconds"]):
            continue
        # (the menu's own words when its code has them, texts: menu_texts(); else the project's name)
        name, word = nice(e["name"]), "the menus' project names it \"%s\"" % e["name"]
        exact = bool((texts or {}).get(e["playlist"]))     # (its button's words, its playlist: known)
        if exact:
            name, word = texts[e["playlist"]], "its menu button reads \"%s\"" % texts[e["playlist"]]
        if e["name"].startswith("TMPL") or re.search(r"\bmenu\b|\bplay all\b|\bbackground|\bbckgrnd", name, re.I):
            continue
        if e["name"].startswith("FPL_"):
            if t.get("role") not in ("duplicate", "loop"):
                out[t["id"]] = {"type": "MainMovie", "title": None, "note": word, "exact": True}
            continue
        if re.search(r"easter egg", name, re.I):
            out[t["id"]] = {"type": "Extra", "title": "Easter Egg", "note": word}
            continue
        if AD_RE.search(name) or re.fullmatch(r"SPHE|Sony.*", name):
            out[t["id"]] = {"type": "Promo", "title": "Previews", "note": word}
            continue
        if re.search(r"\btrailer\b|\bteaser\b", name, re.I):
            out[t["id"]] = {"type": "Trailer", "title": name, "note": word, "exact": exact}
            continue
        other = known_film(name) if known_film and usual and e["fps"] != usual else None
        if other and not (film and (plain(film) in plain(other) or plain(other) in plain(film))):
            out[t["id"]] = {"type": "Promo", "title": "Previews - %s" % other, "note": word + ": another film, an ad"}
            continue
        out[t["id"]] = {"type": "Extra", "title": name, "note": word, "exact": exact}
    return out


# ---- a title played to see what it is (Edit titles' play link): ffmpeg's ffplay, the playlist by
# its own number (no VLC: no title list to look up, no minute-long floor), MakeMKV's libmmbd
# decrypting (libbluray in ffmpeg finds it on the search path) ----

FFPLAY_GLOBS = (r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\*\bin\ffplay.exe",
                r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffplay.exe",
                r"C:\ffmpeg\bin\ffplay.exe", r"%ProgramFiles%\ffmpeg\bin\ffplay.exe",
                r"%USERPROFILE%\scoop\apps\ffmpeg\current\bin\ffplay.exe",
                r"C:\ProgramData\chocolatey\bin\ffplay.exe")


def find_ffplay(setting=None):
    """ffmpeg's player, ffplay: the setting (the file, or its folder), the search path, or where
    ffmpeg installs (WinGet, scoop, chocolatey, C:\\ffmpeg); None without it."""
    import glob
    import shutil
    if setting:
        p = os.path.join(setting, "ffplay.exe") if os.path.isdir(setting) else setting
        if os.path.isfile(p):
            return p
    p = tools.which("ffplay")
    if p:
        return p
    for g in FFPLAY_GLOBS:
        hits = sorted(glob.glob(os.path.expandvars(g)))
        if hits:
            return hits[-1]
    return None


def decryption_dirs():
    """The folders holding MakeMKV's libmmbd (its AACS / BD+ in libbluray's place): MakeMKV's own,
    and VLC's, where it's often copied."""
    from . import core, menus
    dirs = []
    exe = core.find_makemkvcon()
    for d in ([os.path.dirname(exe)] if exe else []) + [r"C:\Program Files (x86)\MakeMKV", r"C:\Program Files\MakeMKV",
                                                         menus.find_vlc(None) or ""]:
        if d and d not in dirs and any(os.path.isfile(os.path.join(d, n)) for n in ("libmmbd.dll", "libmmbd64.dll")):
            dirs.append(d)
    return dirs


WATCH_MIN_HEIGHT = 720      # a standard-definition title's window at least this tall (480 lines: too small)


def watch_size(video):
    """The window to watch a title in (video: the scan's video_info): an SD one, 480 or 576 lines,
    at WATCH_MIN_HEIGHT and its own shape (960 x 720 for 4:3, 1280 x 720 for 16:9); None for an HD
    one (its own size) or one whose picture isn't known."""
    v = video or {}
    if not v.get("height") or v["height"] >= WATCH_MIN_HEIGHT:
        return None
    wide = v.get("aspect") == "16:9"
    return (round(WATCH_MIN_HEIGHT * (16 / 9 if wide else 4 / 3)), WATCH_MIN_HEIGHT)


def watch_vlc(folder, source, title):
    """Plays a title of a Blu-ray in VLC's own window, by VLC's number for it, as a stakeout saw it.
    One window at a time, as menus.watch_title's. False if there's no VLC player to open."""
    import subprocess
    from . import menus
    exe = menus.vlc_player(folder) if folder else None
    if not exe:
        return False
    menus.stop_watching()
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(decryption_dirs() + [env.get("PATH", "")])
    root = source.replace("\\", "/").rstrip("/")
    menus._watching = subprocess.Popen([exe, "bluray:///%s/#%d" % (root, title)], env=env, close_fds=True)
    return True


def watch_playlist(source, playlist, ffplay, title=None, video=None):
    """Plays one playlist of a Blu-ray (a drive or a folder) in ffplay's window of its own, to see
    what it is: by its number ("00745.mpls": 745), its menus left alone, its subtitles off (the
    first, ffplay's pick: German, on Muppets from Space), an SD picture big enough to see
    (watch_size). One window at a time (menus.watch_title's: the one before closed first, and
    before the drive is needed). False without ffplay."""
    import subprocess
    from . import menus
    if not ffplay:
        return False
    menus.stop_watching()
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(decryption_dirs() + [env.get("PATH", "")])
    num = int(re.match(r"\d+", os.path.basename(playlist)).group())
    root = source.rstrip("\\/") if re.fullmatch(r"[A-Za-z]:[\\/]?", source) else source
    flags = 0x08000000 if os.name == "nt" else 0           # (CREATE_NO_WINDOW: its picture, no console)
    size = watch_size(video)
    menus._watching = subprocess.Popen(
        [ffplay, "-hide_banner", "-loglevel", "error", "-window_title", title or playlist, "-sn"]
        + (["-x", str(size[0]), "-y", str(size[1])] if size else [])
        + ["-playlist", str(num), "-i", "bluray:" + root], env=env, creationflags=flags, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
