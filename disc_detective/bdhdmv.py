"""A Blu-ray's HDMV menus from its own files, nothing played: the hdmv-menus library (when installed) runs
the disc's programs to find every menu it can reach and what each button does; here each page is drawn,
each button's words read off its own picture, and the whole put in the DVD crawl's shape (menus.crawl's
"pages"), so one set of rules names the titles (menus.menu_titles). Replaces the VLC crawl for a disc whose
menus are HDMV, when the library is there; the crawl stays for when it isn't.

Decryption: the user's own MakeMKV library (libmmbd, the libaacs interface), as VLC uses it here; this
ships, fetches and searches for nothing else."""
import json
import os
import re

from . import bdmenus, menus

SIZE = (720, 480)               # the pages' size as the rules and Edit titles have them (a DVD's)
BACKGROUND = (0, 0, 0, 255)     # what a page and a button are drawn over, to read and to show


def available():
    """Whether the hdmv-menus library is installed."""
    try:
        import hdmv_menus  # noqa: F401
        return True
    except ImportError:
        return False


def aacs_library():
    """The user's libaacs-compatible library (MakeMKV's libmmbd, where MakeMKV or VLC keeps it), or None."""
    for d in bdmenus.decryption_dirs():
        for name in ("libmmbd64.dll", "libmmbd.dll", "libmmbd.so.0", "libmmbd.dylib"):
            if os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
    return None


def _rgb(picture):
    """A picture (hdmv_menus.Picture) over the background, as an RGB Pillow image."""
    from PIL import Image
    img = picture.to_pil()
    back = Image.new("RGBA", img.size, BACKGROUND)
    back.alpha_composite(img)
    return back.convert("RGB")


# (a button's name's lines, and the ways to read its picture alone: the DVD's and the Blu-ray's alike)
NAME_SHARE, LIT_SHARE = menus.NAME_SHARE, menus.LIT_SHARE
name_lines, _share = menus.name_lines, menus._share


def _lit(picture, selected):
    """A button's picture over the background, and where its highlight changes it (a mask, white; None
    without a selected picture its size)."""
    img = _rgb(picture)
    if selected is not None and (selected.width, selected.height) == (picture.width, picture.height):
        return img, menus._diff_mask(img, _rgb(selected))
    return img, None


def name_boxes(picture, selected, lines):
    """Of a page's lines (read whole: [(text, box, sure)], its own pixels), a button's name's boxes: in
    its box, and what its highlight changes; without a highlight to go by, its tallest lettering
    (name_lines). Its box's other lines, a heading over its name, a description under it, are the
    page's (Order of the Phoenix's "Additional Footage" over "Additional Scenes")."""
    if picture is None or not lines:
        return []
    x0, y0 = picture.x, picture.y
    mine = [(t, (a - x0, b - y0, c - x0, d - y0), sure, (a, b, c, d)) for t, (a, b, c, d), sure in lines
            if x0 <= (a + c) / 2 <= x0 + picture.width and y0 <= (b + d) / 2 <= y0 + picture.height]
    _, lit = _lit(picture, selected)
    if lit is not None:
        changed = [x[3] for x in mine if _share(lit, x[1]) >= LIT_SHARE]
        if changed:
            return changed
    names = name_lines([x[:3] for x in mine])
    return [x[3] for x in mine if x[0] in names]


def _box(menu, page, b):
    """A button's box on the 720x480 page, by its picture: its normal one, else its selected (a marker
    shown only when it's selected); None with neither."""
    pic = menu.button_image(page, b) or menu.button_image(page, b, "selected")
    if pic is None:
        return None
    sx, sy = SIZE[0] / menu.composition["width"], SIZE[1] / menu.composition["height"]
    return [round(pic.x * sx), round(pic.y * sy), round((pic.x + pic.width) * sx), round((pic.y + pic.height) * sy)]


def beside_names(menu, page, lines, ids=None):
    """{button id: (name, its box on the 720x480 page, how sure)}: buttons too small to hold their names,
    a marker or a pointer (Chuck's ">>"), each given the name beside it as the page's layout has them:
    the DVD's way (menus._tiny_names), off the page's lines read whole (its own pixels). ids: the
    buttons that do something (play, or turn a page); the rest, decoration, own no name: on Chuck's
    episode page each name is a button of its own that does nothing, beside the marker that plays it."""
    from PIL import Image
    if not lines:
        return {}
    rects = {b["id"]: tuple(r) for b in menu.buttons(page) for r in [_box(menu, page, b)]
             if r and (ids is None or b["id"] in ids)}
    w, h = menu.composition["width"], menu.composition["height"]
    frame = Image.new("RGB", (w, h))                    # (its size, for the scale)
    # (the buttons with no words in their boxes: those to name from beside them, whatever their width)
    sx, sy = w / SIZE[0], h / SIZE[1]
    holds = lambda r: any(r[0] * sx <= (a + c) / 2 <= r[2] * sx and r[1] * sy <= (b + d) / 2 <= r[3] * sy
                          for _, (a, b, c, d), _ in lines)
    empty = [list(r) for r in rects.values() if not holds(r)]
    found = menus._tiny_names(frame, [list(r) for r in rects.values()], SIZE, lines=lines, empty=empty)
    out = {bid: found[r] for bid, r in rects.items() if r in found}
    # first, where its highlight shows: the line level with it, nearest beside it (Chuck's ">>", in a
    # box as tall as its row and the page's heading over it: the episode's name, not "EPISODES")
    others = [(r[0] * sx, r[1] * sy, r[2] * sx, r[3] * sy) for r in rects.values() if holds(r)]
    for b in menu.buttons(page):
        r = rects.get(b["id"])
        if r is None or list(r) not in empty:
            continue
        pic, sel = menu.button_image(page, b), menu.button_image(page, b, "selected")
        if pic is None or sel is None:
            continue
        _, lit = _lit(pic, sel)
        lit_box = lit.getbbox() if lit is not None else None
        if not lit_box:
            continue
        hit = menus.level_name((lit_box[0] + pic.x, lit_box[1] + pic.y, lit_box[2] + pic.x, lit_box[3] + pic.y), lines, others)
        if hit:
            t, (a, bb, c, d), sure = hit
            out[b["id"]] = (t, (a / sx, bb / sy, c / sx, d / sy), sure)
    return out


def button_words(picture, read=None, selected=None, page=None, is_word=None):
    """A button's name: name_lines' (selected: its picture selected, for the lines its highlight
    changes), with how sure RapidOCR was of it (its least sure line's; None when read some other way,
    or nothing was). First its own lines of its page's reading (page: [(text, box, sure)], the page's
    pixels, read once at its own size): read in the page, as it shows ("Harry and Cho Under the
    Mistletoe"; alone, "Undr"). Then, while under menus.READ_CONFIDENT, its picture alone (nothing
    behind it), another way each time (_ways); the surest kept; the others' words too, surest first,
    for when the surest isn't words (The Choreography of Magic, surest as "Iagic"): (words, sure,
    others). is_word: a dictionary; a page's reading with a word of four letters or more that isn't
    one has its button read alone too, however sure ("The Mmirror Explosion": alone, "Mirror")."""
    if picture is None:
        return "", None, []
    img, lit = _lit(picture, selected)
    x0, y0 = picture.x, picture.y
    if read is not None:                # (another reader's: the button's picture, all it was given)
        return menus.Words(read(img), (x0, y0, x0 + picture.width, y0 + picture.height)), None, []
    tried = []

    def take(lines, mask, scale=1):
        names = name_lines(lines, mask)
        kept = [b for t, b, _ in lines if t in names]
        sure = min((c for t, _, c in lines if t in names), default=None)
        if sure is not None:            # (and where: its name's lines, on the page, its pixels)
            where = (x0 + min(b[0] for b in kept) / scale, y0 + min(b[1] for b in kept) / scale,
                     x0 + max(b[2] for b in kept) / scale, y0 + max(b[3] for b in kept) / scale)
            tried.append((menus.Words(re.sub(r"\s+", " ", " ".join(names)).strip(), where), sure))
        return sure is not None and sure >= menus.READ_CONFIDENT
    if page is not None:                # (the page's lines whose middle is in its box, in its own pixels)
        mine = [(t, (a - x0, b - y0, c - x0, d - y0), sure) for t, (a, b, c, d), sure in page
                if x0 <= (a + c) / 2 <= x0 + picture.width and y0 <= (b + d) / 2 <= y0 + picture.height]
        doubt = is_word is not None and any(
            len(w) >= 4 and w.lower() not in menus.EXTRAS_VOCAB and not is_word(w)
            for t, _, _ in mine for w in re.findall(r"[A-Za-z]+", t))
        if take(mine, lit) and not doubt:
            return tried[0][0], tried[0][1], []
    tried += menus.read_alone(img, lit, (x0, y0))
    if not tried:
        return "", None, []
    tried.sort(key=lambda x: -x[1])
    others = [t for t, _ in tried[1:] if t and t != tried[0][0]]
    return tried[0][0], tried[0][1], list(dict.fromkeys(others))


def page_headings(menu, page, buttons, drawn, lines):
    """A page's words that aren't its buttons', its heading line, whether it has several: (words, line,
    mixed), a DVD's way (menus.page_words), off its lines as it was read whole (lines: its own pixels, not
    read again). buttons: those the crawl kept (their names where they were read: "words"); a button not
    read, its name's lines by its highlight (name_boxes; the rest of its box is the page's). Its heading
    line among the lines the kept buttons don't hold (decoration's box may hold it: Chuck's first-row name
    button, "EPISODES" in it); its words, those no button on it holds, the bar's tabs' too ("SPECIAL
    FEATURES": the library can't tell what they do, "ended"; left in, every episode on the page is a bonus
    feature), its heading line always among them."""
    sx, sy = SIZE[0] / drawn.width, SIZE[1] / drawn.height
    by = {x["id"]: x for x in menu.buttons(page)}
    kept = {b["n"] for b in buttons}

    def unread(b):
        x = by[b["n"]]
        boxes = name_boxes(menu.button_image(page, x), menu.button_image(page, x, "selected"), lines) if lines else []
        return [[a * sx, c * sy, e * sx, f * sy] for a, c, e, f in boxes]
    names = [b["text"] for b in buttons]
    _, line, mixed = menus.page_words(drawn, menus.name_rects(buttons, unread), SIZE, names, lines=lines)
    rest = [{"n": x["id"], "rect": r} for x in menu.buttons(page) if x["id"] not in kept for r in [_box(menu, page, x)] if r]
    words = menus.page_words(drawn, [r for b in rest for r in (unread(b) or [b["rect"]])] + menus.name_rects(buttons, unread),
                             SIZE, names, lines=lines)[0]
    words = words if not line or line.lower() in words.lower() else ("%s %s" % (line, words)).strip()
    return words, line, mixed


def _keep_page(menu, page, acts, drawn, out_dir, image):
    """Test data (a developer's copy, menus.keeping_test_data): a page as it was read, so the naming
    can be tried again without the disc (temp/bench_bd.py): drawn at its own size, and every button's
    normal and selected pictures with their places, in out_dir/bd/ (acts: the ids of those that do
    something the crawl kept; the rest decoration)."""
    keep = os.path.join(out_dir, "bd")
    stem = os.path.splitext(image)[0]
    try:
        os.makedirs(keep, exist_ok=True)
        drawn.save(os.path.join(keep, stem + ".png"))
        info = {"size": [drawn.width, drawn.height], "buttons": []}
        for b in menu.buttons(page):
            entry = {"id": b["id"], "acts": b["id"] in acts}
            for state in ("normal", "selected"):
                pic = menu.button_image(page, b, state)
                if pic is not None:
                    name = "%s_b%d_%s.png" % (stem, b["id"], state)
                    _rgb(pic).save(os.path.join(keep, name))
                    entry[state] = {"image": name, "x": pic.x, "y": pic.y, "width": pic.width, "height": pic.height}
            info["buttons"].append(entry)
        with open(os.path.join(keep, stem + ".json"), "w", encoding="utf-8") as fh:
            json.dump(info, fh)
    except OSError:
        pass


def _routes(page_ids, links, first):
    """The way to each page of a menu from its first, by the buttons that turn to it: {page: [(page,
    button), ...]} (the menu's first page: [])."""
    goes = {}
    for x in links:
        o = x["outcome"]
        if o["kind"] == "page" and o.get("page") is not None and o["page"] != x["page"]:
            goes.setdefault(x["page"], []).append((x["button"], o["page"]))
    routes, queue = {first: []}, [first]
    while queue:
        here = queue.pop(0)
        for button, to in sorted(goes.get(here, [])):
            if to in page_ids and to not in routes:
                routes[to] = routes[here] + [(here, button)]
                queue.append(to)
    return routes


def _opening(plan_menus, title_for):
    """What a player plays from the first play before the first menu shows, nothing pressed (its
    warnings, its previews; the last, the playlist that carries the menu, not): a DVD's opening's like,
    each "plays" of the nearest menu's way there, while there's been no press. [title ids]"""
    out = []
    first = next((m for m in plan_menus if m["reached"] and m["path"][:1] == ["first play"]), None)
    for step in (first or {}).get("path", [])[1:-1]:
        hit = re.fullmatch(r"plays (\d+)", step)
        if not hit:
            break
        t = title_for(int(hit.group(1)))
        if t and t not in out:
            out.append(t)
    return out


TRIES = menus.TITLE_TRIES       # a title's pages read at most (scope "titles"), while each reading fails
SCOPES = ("titles", "language", "all")


def read(root, titles, out_dir, log=print, aacs=None, read_words=None, progress=None, tv=False, scope="titles",
         is_word=None):
    """The disc's HDMV menus as a crawl's result (bdcrawl.crawl's: its pages as a DVD's, "found" by the
    shared rules), nothing played. None when the library isn't installed or the disc has no HDMV menus.
    aacs: a libaacs-compatible library (MakeMKV's, by default); read_words: a picture to its words
    (RapidOCR's, by default); progress: menus.CRAWL_STAGES, as a DVD's crawl tells them: "setup" (the disc
    opened, unlocked), "start", then "page", k, n for each menu page questioned. tv: a show's disc, whose scene buttons may name its
    episodes (a film's name only its scenes: not read, but in scope "titles", where they may be one).

    scope, the library's crawl plan (hdmv_menus.Disc.crawl): "titles", each title's best button read (the
    next only if its reading fails: TRIES at most), every button that plays one kept, read or not (a
    film's scenes say which is the film); "language", every menu reached in the player's language, each
    button that plays a title read, and each that turns to a page on the way to one (not each page's own
    tabs over again, nor the languages' going back to the first); "all", that of every menu, every
    language. is_word: a dictionary, for the readings worth a second look (button_words)."""
    if not available():
        return None
    import time
    import hdmv_menus
    from PIL import Image
    tell = progress or (lambda *a: None)
    os.makedirs(out_dir, exist_ok=True)
    aacs = aacs or aacs_library()
    menus.readings_begin()              # (how sure of each reading: this stakeout's, as a DVD's)
    tell("setup")                       # (opening it: its files read, an encrypted disc unlocked)
    disc = hdmv_menus.Disc(root, aacs=aacs)
    tell("start")
    plan = disc.crawl(scope)
    if not plan["menus"]:
        # (its first play gone on to a Java title: its menus are BD-J, an HDMV menu stream of its own
        # elsewhere notwithstanding; Enchanted's, two intros then title 1, its JAR 00000)
        java = next((e for e in disc.trace() if e.get("kind") == "bdj"), None)
        if java is not None:
            log("hdmv-menus: the first play goes on to Java (title %s, JAR %s): BD-J menus"
                % (java.get("title"), java.get("jar")))
            raise menus.Unsupported("BD-J (its first play goes on to Java)")
        log("hdmv-menus: no menu reached from the first play")
        return None
    lengths = {t["id"]: t.get("duration") for t in titles}
    title_for = lambda playlist: "%05d.mpls" % playlist if "%05d.mpls" % playlist in lengths else None
    opening = _opening(plan["menus"], title_for)
    # (what plays while a menu shows, the first play let run, nothing pressed: a pop-up's film, the menu in
    # its own stream; Chamber of Secrets' 00023 under menu 00157)
    under_menu = []
    for m in plan["menus"]:
        steps = m.get("path") or []
        if m.get("reached") and steps[:1] == ["first play"] and not any(" button " in x for x in steps):
            hit = re.fullmatch(r"plays (\d+)", steps[-1]) if len(steps) > 1 else None
            t = title_for(int(hit.group(1))) if hit else None
            if t and t not in [u["title"] for u in under_menu]:
                under_menu.append({"title": t, "seconds": lengths.get(t)})
    loaded = {}                         # (clip, pid): the menu, decoded, what its buttons do, its pages' routes
    for n_menu, cm in enumerate(plan["menus"], 1):
        clip, pid = cm["stream"]["clip"], cm["stream"]["pid"]
        try:
            menu = disc.menu(clip, pid)
        except (OSError, ValueError, RuntimeError) as exc:
            log("hdmv-menus: menu %s unreadable: %s" % (clip, exc))
            continue
        log("hdmv-menus: menu %s (%s, %d pages), %s" % (clip, cm["stream"]["lang"], len(menu.pages),
                                                     "reached by " + " > ".join(cm["path"]) if cm["reached"] else "not reached"))
        ids = [p["id"] for p in menu.pages]
        routes = _routes(set(ids), cm["links"], ids[0]) if ids else {}
        # (its depth: the presses on its way there, a menu inside menus; menus.menu_titles' nearest the root)
        loaded[(clip, pid)] = {"n": n_menu, "menu": menu, "routes": routes, "depth": sum(" button " in x for x in cm["path"]), "steps": {x for r in routes.values() for x in r},
                               "outcome": {(x["page"], x["button"]): x["outcome"] for x in cm["links"]}}

    def worth(m, page_id, b_id):
        """What a button does that's worth having: ("plays", title) or ("goes_to", page), else None; and
        whether a full crawl (not "titles") reads its words."""
        o = m["outcome"].get((page_id, b_id)) or {"kind": "ended"}
        if o["kind"] == "play" and title_for(o["playlist"]):
            return ("plays", title_for(o["playlist"])), tv or not o.get("mark") or scope == "all"
        if o["kind"] == "page" and o.get("page") is not None and o["page"] != page_id and \
                ((page_id, b_id) in m["steps"] or scope == "all"):
            return ("goes_to", o["page"]), True
        return None, False              # (decoration, a language, a tab again, a link within the film)

    pages_of = {k: {p["id"]: p for p in m["menu"].pages} for k, m in loaded.items()}
    got, started = {}, time.time()      # (clip, pid, page, button): (its words, how sure, its other readings)
    count = {"pages": [], "total": 0}   # (the pages questioned so far, in turn; how many there are to be)
    seen = {}                           # (clip, pid, page): (drawn at its own size, its lines read)
    near = {}                           # (clip, pid, page): beside_names, worked out once
    beside = {}                         # (clip, pid, page, button): where its name was read (720x480)
    boxes = {}                          # (clip, pid, page): its buttons' boxes (720x480)

    def questioned(key):
        """A page drawn at its own size, and read once, whole (its lines: each button's first reading)."""
        if key not in seen:
            page = pages_of[key[:2]][key[2]]
            img = _rgb(loaded[key[:2]]["menu"].draw(page, background=BACKGROUND))
            seen[key] = (img, (menus.rapid_lines(img) or []) if read_words is None else None)
        return seen[key]

    def look(key):
        """A button's words (button_words), read once; its page told, the first time one of its buttons is."""
        if key not in got:
            m = loaded[key[:2]]
            page = pages_of[key[:2]][key[2]]
            b = next(x for x in m["menu"].buttons(page) if x["id"] == key[3])
            if key[:3] not in count["pages"]:
                count["pages"].append(key[:3])
                k = len(count["pages"])
                n = max(count["total"], k)
                tell("page", k, n)
            drawn, lines = questioned(key[:3])
            menu = m["menu"]
            pic, sel = menu.button_image(page, b), menu.button_image(page, b, "selected")

            def near_it():
                if key[:3] not in near:
                    doing = {bid for (pid, bid), o in m["outcome"].items() if pid == key[2] and o["kind"] in ("play", "page")}
                    near[key[:3]] = beside_names(menu, page, lines, doing)
                return near[key[:3]].get(key[3])
            if lines == []:             # (no words on the whole page: its art in the video, nothing alone either)
                got[key] = ("", None, [])
            elif read_words is not None:     # (another reader's, the button's picture alone)
                got[key] = button_words(pic, read_words, sel)
            elif pic is None and sel is None:
                got[key] = ("", None, [])
            else:
                # the DVD's naming (menus.name_button), off the page as drawn: its own lines read whole, what
                # its highlight changes, its picture alone, the name beside it (beside_names: the line level
                # with its highlight first)
                own = pic or sel
                img, lit = _lit(own, sel if pic is not None else None)
                sx, sy = drawn.width / SIZE[0], drawn.height / SIZE[1]
                if key[:3] not in boxes:
                    boxes[key[:3]] = [r for x in menu.buttons(page) for r in [_box(menu, page, x)] if r]
                rect = _box(menu, page, b)
                got[key] = menus.name_button(
                    drawn, rect, boxes[key[:3]], SIZE, lines=lines,
                    lit=(lit, (own.x, own.y)) if lit is not None else None,
                    alone=lambda: menus.read_alone(img, lit, (own.x, own.y), (sx, sy)),
                    beside=near_it, is_word=is_word)
            # (where it was read, on the 720x480 page: what its picture outlines)
            where = menus.where_read(got[key][0])
            if where:
                beside[key] = where
        return got[key]


    if scope == "titles":
        # each title's buttons best first, as the library ranks them; the next read only while the
        # reading so far fails (no words, or not READ_CONFIDENT of them): the surest kept
        # (page by page, as each title's first button has it: a page questioned, then the next)
        # (a Play All the scan knows, none: what it plays says what it is; of copies of one video, one:
        # whichever's read is the one confirmed, the others its copies, as on a DVD)
        # (the scan's copies and Play Alls, as a DVD's crawl has them)
        play_alls, copies = menus._play_alls_of(titles, str), menus._copies_of(titles, str)
        video = {t["id"]: copies.get(t["id"], t["id"]) for t in titles}
        tries, videos = [], set()
        for t in plan["titles"]:
            tid = title_for(t["playlist"])
            if not tid or tid in play_alls or video.get(tid, tid) in videos:
                continue
            keys = [(b["clip"], b["pid"], b["page"], b["button"]) for b in t["buttons"] if (b["clip"], b["pid"]) in loaded]
            if keys:
                tries.append((video.get(tid, tid), keys[:TRIES]))
                videos.add(video.get(tid, tid))
        # the pages to read, a DVD's way (menus.plan_pages): the page naming the most titles still unnamed
        # first (no presses to get there: each page costs its reading), ties to the library's ranking (a
        # title's first button's page first), re-planned after each page; on a page, each of its titles'
        # buttons there read until one names it (menus.names_title, sure of it: another reading costs next to
        # nothing here); a title given up on after TRIES pages
        holds, best = {}, {}
        # (a button playing its title from the start, as a DVD's planner has them: a scene's names nothing)
        from_start = lambda key: not (loaded[key[:2]]["outcome"].get(key[2:]) or {}).get("mark")
        for vid, keys in tries:
            for i, key in enumerate(k for k in keys if from_start(k)):
                holds.setdefault(key[:3], {}).setdefault(vid, []).append(key)
                best[key[:3]] = min(best.get(key[:3], TRIES), i)
        order = sorted(holds, key=lambda pk: (loaded[pk[:2]]["n"], pk[2]))
        rank = lambda pk: best[pk] * len(order) + order.index(pk)
        found, tried, done = set(), {}, set()
        while True:
            left = {pk: {v for v in vs if v not in found and tried.get(v, 0) < TRIES} for pk, vs in holds.items()}
            todo = menus.plan_pages({pk: v for pk, v in left.items() if v}, {}, done, rank)
            count["total"] = len(count["pages"]) + len([pk for pk in todo if pk not in count["pages"]])
            if not todo:
                break
            pk = todo[0]
            done.add(pk)
            for vid in sorted(left[pk], key=str):
                for key in holds[pk][vid]:
                    text, sure, _ = look(key)
                    if menus.names_title(text, sure, need_sure=True):
                        found.add(vid)
                        break
                else:
                    tried[vid] = tried.get(vid, 0) + 1
    else:
        wanted = [(k + (p["id"], b["id"])) for k, m in loaded.items() for p in m["menu"].pages for b in m["menu"].buttons(p)
                  if worth(m, p["id"], b["id"])[1]]
        count["total"] = len({key[:3] for key in wanted})
        for key in wanted:
            look(key)
    # each page, its buttons that play a title or lead to a page, read or not
    pages, also = [], {}
    for key, m in sorted(loaded.items(), key=lambda kv: kv[1]["n"]):
        clip, menu, n_menu = key[0], m["menu"], m["n"]
        sx, sy = SIZE[0] / menu.composition["width"], SIZE[1] / menu.composition["height"]
        for page in menu.pages:
            buttons, read_here, scenes = [], 0, False
            for b in menu.buttons(page):
                it, _ = worth(m, page["id"], b["id"])
                if it is None:
                    continue
                o = m["outcome"][(page["id"], b["id"])]
                rect = _box(menu, page, b) or [0, 0, 0, 0]
                entry = {"n": b["id"], "rect": rect, "_b": b}
                # (its picture: the page drawn with it selected, the user's choice: the button read lit, as a player
                # shows it, a button hidden as the page opens too (a group shows one at a time, its default first:
                # Prisoner of Azkaban's "RESUME MOVIE", 69, under 77; Chuck's ">>"); nothing to it but a drawing)
                if it[0] == "plays" and menu.button_image(page, b, "selected") is not None:
                    entry["picture"] = "hdmv_%s_p%d_b%d.png" % (clip, page["id"], b["id"])
                if key + (page["id"], b["id"]) in beside:       # (where its name was read: what to outline)
                    entry["words"] = [round(v) for v in beside[key + (page["id"], b["id"])]]
                if it[0] == "plays":
                    entry["plays"] = {"title": it[1], "chapter": (o.get("mark") or 0) + 1, "seconds": lengths[it[1]], "engine": True}
                    scenes = scenes or bool(o.get("mark"))
                else:
                    entry["goes_to"] = {"page": it[1], "program": n_menu, "engine": True}
                text, how_sure, others = got.get(key + (page["id"], b["id"]), ("", None, []))
                read_here += (key + (page["id"], b["id"])) in got
                entry["text"] = str(text)
                if text and how_sure is not None:      # (another reader's too: noted as RapidOCR's are)
                    menus._note_sure(str(text), how_sure)
                if text and others:
                    also.setdefault(text, others)
                buttons.append(entry)
            # (a page with nothing read on it, in a quick crawl: kept only for its scenes, saying which is the film)
            if not buttons or (scope == "titles" and not read_here and not scenes):
                continue
            pages.append({"domain": "bd", "page": page["id"], "program": n_menu, "heading": "", "heading_line": None, "depth": m["depth"],
                          "mixed": False, "buttons": buttons, "menu": clip, "read": read_here,
                          "route": [{"page": p, "program": n_menu, "button": btn} for p, btn in m["routes"].get(page["id"], [])],
                          "image": "hdmv_%s_p%d.png" % (clip, page["id"]), "_page": page})
    # a page with no words read, whose titles have none anywhere: a menu drawn over its video, the art in
    # the film, the disc's files only its highlights (Order of the Phoenix's editing game): nothing to name
    # (words: letters, not a "?" read off a box of colour)
    worded = lambda t: bool(re.search(r"[A-Za-z]{2}", t or ""))
    named = {b["plays"]["title"] for p in pages for b in p["buttons"] if b.get("plays") and worded(b["text"])}
    # (not a page of scenes: numbers for words, but it says which title is the film)
    scenes = lambda p: any((b.get("plays") or {}).get("chapter", 1) > 1 for b in p["buttons"])
    for p in [p for p in pages if p["read"] and not scenes(p) and not any(worded(b["text"]) for b in p["buttons"])
              and not any(b.get("plays", {}).get("title") in named for b in p["buttons"])]:
        log("hdmv-menus: menu %s page %d: no words on its buttons, left out" % (p["menu"], p["page"]))
        pages.remove(p)
    for p in pages:
        k = next(k for k in loaded if k[0] == p["menu"] and loaded[k]["n"] == p["program"])
        page = p.pop("_page")
        drawn = seen[k + (page["id"],)][0] if k + (page["id"],) in seen else \
            _rgb(loaded[k]["menu"].draw(page, background=BACKGROUND))
        drawn.resize(SIZE, Image.LANCZOS).save(os.path.join(out_dir, p["image"]))
        for b in p["buttons"]:                  # (and with each button read that plays a title selected)
            if b.get("picture") and b.get("text"):
                lit = _rgb(loaded[k]["menu"].draw(page, selected=b["n"], background=BACKGROUND))
                lit.resize(SIZE, Image.LANCZOS).save(os.path.join(out_dir, b["picture"]))
            else:
                b.pop("picture", None)
        own = [b.pop("_b") for b in p["buttons"]]
        if menus.keeping_test_data():
            _keep_page(loaded[k]["menu"], page, {x["id"] for x in own}, drawn, out_dir, p["image"])
        if p.pop("read"):               # (its heading: only where its buttons were read)
            lines = seen[k + (page["id"],)][1] if k + (page["id"],) in seen else None
            p["heading"], p["heading_line"], p["mixed"] = page_headings(loaded[k]["menu"], page, p["buttons"], drawn, lines)
    log("hdmv-menus: %s crawl, %d buttons read in %.0f s" % (scope, len(got), time.time() - started))
    if not any(b.get("plays") for p in pages for b in p["buttons"]):
        log("hdmv-menus: no button plays one of the scan's titles")
        return None
    # (each title's chapters, as the scan counted them: a "trailer" in several, an ad reel)
    chapters = {t["id"]: t["chapters"] for t in titles if t.get("chapters")}
    out = {"drive": root, "pages": pages, "chapters": chapters, "reader": "rapidocr", "source": "hdmv-menus",
           "scope": scope, "sure": menus.readings_sure(), "also": also, "opening": opening,
           "scan_play_alls": sorted(menus._play_alls_of(titles, str)), "under_menu": under_menu,
           # (every line of the pages read whole, and how sure: the disc's own words, menus.disc_words)
           "page_lines": {t: round(c, 3) for _, lines in seen.values() for t, _, c in lines or []},
           "suspects": sum(1 for p in pages for b in p["buttons"] if b.get("plays")), "watch": {}}
    # (a show's: its readings as a DVD's are (menus.tv_titles), Play Alls, ads, kinds and all: Chuck's
    # PLAY ALL plays its first episode's own playlist, which its own row names)
    out["tv"] = menus.tv_titles(out)
    if tv:
        # what plays on after what (each button's play and the library's "then"), and the scan's Play All
        # playlists: the order of the episodes, by a Play All, as a DVD's (core.play_titles_order)
        from . import core
        runs = [[t] + [x for x in (title_for(p) for p in link.get("then") or []) if x]
                for m in plan["menus"] for link in m.get("links") or []
                for o in [link["outcome"]] if o.get("kind") == "play"
                for t in [title_for(o["playlist"])] if t]
        out["tv"]["play_order"] = core.play_titles_order(titles, runs)
    out["found"] = dict(menus.menu_titles(out), reader="rapidocr")
    return menus.finish_crawl(out, out_dir)
