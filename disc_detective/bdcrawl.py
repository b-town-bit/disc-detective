"""A TV Blu-ray's episode page, read and pressed: which button plays which title. VLC plays the disc
with no window; its pop-up menu is opened, the page of episodes found, each button's words read and
each button pressed, and where playback lands says what it plays. HDMV menus decide that as they run
(every PlayPL takes its playlist from a register, on Chuck), so it's cheaper to watch than to work out.

Many discs have no playlist per episode: a button jumps into the Play All at the episode's clip, so
the time it lands on names the clip. Others play a playlist of their own, known by its length. A
button is pressed only once its marker has been read on it, and what two buttons both land on is
nobody's: a wrong episode is worse than none. Returns readings as menus.tv_titles does, so the show's
episode list is searched as it is for a DVD. The bar's other page of titles, its special features, is
walked by its marker: what's a button there is what the marker lands on. Each plays a playlist of its
own, known by its length. While one plays the pop-up doesn't answer: VLC's top menu brings the page back
as it was, up by itself. Where the page's own Play All has as many clips as it has buttons, its first
and last are pressed, and if they land where its order says, the order names the rest. Otherwise every
button's pressed."""
import os
import re
import time

from . import bdmenus, menus
from .core import find_child, parse_mpls, read_file

MARK = "[>»]"
# "7) Chuck Versus the Fat Lady", "1o) ..." (OCR's zero), ">>9) ...". Several to a line when columns
# run together: "Chuck Versus the Gravitron >> 1o) Chuck Versus the DeLorean"
NUMBER = r"\d[\dOo]?\s*[).:]"
SPLIT = re.compile(r"(?:%s\s*)*(?<![\w])%s" % (MARK, NUMBER))
ROW = re.compile(r"^\W*?(%s\s*)*(\d[\dOo]?)\s*[).:]\s*(.+)$" % MARK)
MARKED = re.compile(r"^\W*?%s" % MARK)
# the bar's words (on Chuck: Languages, Episodes, Special Features), never an episode's button
BAR = re.compile(r"^\W*(languages?|audio|subtitles?|setup|episodes?|special|features?|special features|play|play all|"
                 r"main menu|x|resume|scenes?)\W*$", re.I)
BAR_EPISODES = re.compile(r"episodes?", re.I)
BAR_EXTRAS = re.compile(r"special|features?|bonus|extras?", re.I)
# FLAGGED FOR REMOVAL unless a disc needs it: "|warnerblu" was here. "visit us" covers it on Chuck's pages.
# A studio's name is no one show's, and there are only a handful, so a list of them could yet be of use.
LINK = menus.LINK
NOT_A_NAME = re.compile(r"%s|visit us|blu-ray disc" % LINK.pattern, re.I)
KEYS = ("down", "right")          # what's tried from each button (the way back's learned on the way)
COLUMN_GAP = 100                  # pixels between the middles of a column's buttons
BACK = {"down": "up", "up": "down", "right": "left", "left": "right"}
EXTRAS_LIMIT = 600                # seconds for a page of special features
EVERY_WAY_LIMIT = 180             # seconds more for the buttons that didn't turn up, each way from each
UNDER_A_BUTTON = 45               # pixels below a button's first line that its words can run to
SAME_LENGTH = 0.1                 # seconds: a playlist's own length, as VLC reports it
STEP_WAIT = 4                     # seconds for the marker to answer an arrow
PAGE_BACK = 12                    # seconds for the page to come back by itself, or first come up
MENU_TRIES = 6                    # goes at the pop-up before giving up on the page
BAR_WAIT = 4                      # seconds for the pop-up to come up
SAME_ROW = 12                     # pixels between lines of one row
START_NEAR = 30                   # seconds from a clip's start a button may land, and be that clip's
SETTLE = 8                        # seconds for a seek on the disc to be seen
WAIT_PLAYING = 240                # seconds for the intros before the menu can be asked for
TOP_MENU_AFTER = 120              # seconds with nothing else played: the top menu is where it stays


def _pieces(text, box):
    """One OCR line as its buttons (columns read as one line), each with its share of the box."""
    starts = [m.start() for m in SPLIT.finditer(text)]
    if not starts:
        return [(text, box)]
    if starts[0] and not re.search(r"\w", text[:starts[0]]):
        starts[0] = 0
    if starts[0]:
        starts.insert(0, 0)
    width = (box[2] - box[0]) / max(len(text), 1)
    ends = starts[1:] + [len(text)]
    return [(text[a:b].strip(), (box[0] + a * width, box[1], box[0] + b * width, box[3])) for a, b in zip(starts, ends)]


def page_rows(lines):
    """A page's buttons from OCR's lines ([(text, box, how sure)]): [{"number", "name", "box",
    "marked"}], top to bottom and left to right. Numbered lines where the page numbers them; a name
    wrapped onto the line below joins its row. No numbers: the lines around a marker, if there is one."""
    pieces = [p for text, box, _ in sorted(lines or [], key=lambda x: (x[1][1], x[1][0])) for p in _pieces(text, box)]
    rows, rest = [], []
    for text, box in pieces:
        m = ROW.match(text)
        if m and re.search(r"[A-Za-z]{3}", m.group(3)):
            rows.append({"number": int(m.group(2).replace("O", "0").replace("o", "0")), "name": m.group(3).strip(" -"),
                         "box": box, "marked": bool(MARKED.match(text))})
        else:
            rest.append((text, box))
    if not rows and any(MARKED.match(t) for t, _ in rest):
        for text, box in rest:
            name = re.sub(r"^\W*", "", text)
            if re.search(r"[A-Za-z]{3}", name) and not BAR.match(name):
                rows.append({"number": None, "name": name, "box": box, "marked": bool(MARKED.match(text))})
        return rows
    for text, box in rest:
        if not BAR.match(text):
            for r in rows:
                if -SAME_ROW / 2 <= box[1] - r["box"][3] < SAME_ROW and box[0] < r["box"][2] and box[2] > r["box"][0]:
                    r["name"] += " " + text.strip()
                    break
    return rows


def key_towards(here, there):
    """The arrow from one button's box towards another's."""
    if abs(here[1] - there[1]) < SAME_ROW:
        return "right" if there[0] > here[0] else "left"
    return "down" if there[1] > here[1] else "up"


def nearest(rows, box):
    """The row where this box is (a page read again: its lines may run together differently)."""
    return min(rows, key=lambda r: abs(r["box"][0] - box[0]) + abs(r["box"][1] - box[1]), default=None)


def play_alls(root, seconds):
    """Each playlist of several clips as long as this: [(clip, its start, its length)] of the first."""
    pl_dir = find_child(find_child(root, "BDMV"), "PLAYLIST") if find_child(root, "BDMV") else None
    for fn in sorted(os.listdir(pl_dir) if pl_dir else []):
        if not fn.lower().endswith(".mpls"):
            continue
        try:
            p = parse_mpls(read_file(os.path.join(pl_dir, fn)))
        except (OSError, ValueError, IndexError):
            continue
        if len(p["items"]) >= 2 and abs(p["duration"] - seconds) <= 2:
            items, at = [], 0.0
            for clip, t_in, t_out in p["items"]:
                items.append((clip, at, (t_out - t_in) / 45000.0))
                at += (t_out - t_in) / 45000.0
            return items
    return []


def clip_at(items, seconds):
    """The clip of a Play All's that starts at about this time (a button leads to its start):
    (clip, length, its start), or None (somewhere inside one: a scene, not an episode)."""
    for clip, start, length in items:
        if abs(seconds - start) <= START_NEAR:
            return clip, length, start
    return None


def title_of(titles, clip=None, seconds=None):
    """The scan's title for a clip, or for a playlist of this length if only one is: its id, or None."""
    if clip:
        return next((t["id"] for t in titles if (t.get("clip_list") or "").split(",") == [clip]), None)
    same = [t["id"] for t in titles if abs((t.get("duration") or 0) - seconds) <= SAME_LENGTH]
    return same[0] if len(same) == 1 else None


def bar_places(lines, height=480):
    """Where the pop-up's words sit along its bar, from its left: (the page of episodes' place, its
    special features' place), None where it isn't there. The pop-up opens at its first item, and
    a button's two lines ("Special" over "Features") are one item."""
    low = sorted((l for l in lines or [] if l[1][1] > height * 0.7), key=lambda l: l[1][0])
    items = []
    for text, box, _ in low:
        if items and box[0] < items[-1][1][2] and box[2] > items[-1][1][0]:
            items[-1] = (items[-1][0] + " " + text, (min(box[0], items[-1][1][0]), box[1], max(box[2], items[-1][1][2]), box[3]))
        else:
            items.append((text, box))
    place = lambda rx: next((i for i, (t, _) in enumerate(items) if rx.search(t)), None)
    return place(BAR_EPISODES), place(BAR_EXTRAS)


def marked_line(lines):
    """The line the marker's on, its words and its box (the marker may start it, or sit in a line
    that's run on from the column beside it): (text, box), or None."""
    for text, box, _ in sorted(lines or [], key=lambda l: (l[1][1], l[1][0])):
        m = re.search(r"%s\s*%s|^\W*?%s" % (MARK, MARK, MARK), text)
        if m:
            width = (box[2] - box[0]) / max(len(text), 1)
            x0 = box[0] + m.start() * width
            return text[m.end():].strip(" -"), (x0, box[1], box[2], box[3])
    return None


def same_button(a, b):
    """Two readings of one button's first line (the marker widens it, so by its middle)."""
    return abs(a[1] - b[1]) < 14 and abs((a[0] + a[2]) - (b[0] + b[2])) / 2 < 90


def plain_caps(text):
    """OCR's readings of small capitals as ordinary ones: "TylEr" and "ARRIVES" as "Tyler", "Arrives"."""
    return re.sub(r"[A-Za-z]+", lambda m: m.group().capitalize() if re.search(r"[A-Z]", m.group()[1:]) else m.group(), text)


def button_words(button, buttons, lines, height=480):
    """A button's words: its first line and those under it, down to the next button in its column
    (the line that says what's in it, "Casey and Sarah running toward Buy More."), not the bar's."""
    box = button["box"]
    under = [b["box"][1] for b in buttons if b is not button and b["box"][1] > box[1] + 5
             and b["box"][0] < box[2] and b["box"][2] > box[0]]
    stop = min(under + [height * 0.75]) - 6        # (a line read a little higher than with its marker on)
    got = []
    for text, b, _ in sorted(lines or [], key=lambda l: (l[1][1], l[1][0])):
        if box[1] + 5 < b[1] < stop and b[0] < box[2] and b[2] > box[0] and not NOT_A_NAME.search(text) and not BAR.match(text):
            got.append(text.strip())
    words = button["text"]
    for text in got[:3]:
        words += (" " if len(text) <= 8 else " - ") + text        # ("Sc. 8", wrapped from its name)
    return plain_caps(words.rstrip(" ."))


def free_lines(lines, buttons, height=480):
    """A page's lines without its bar, and of those the ones that are no button's or a button's words
    ("Emmett interviews Chuck"): (free, page)."""
    def under(b, box):
        return b["box"][1] + 5 < box[1] < b["box"][1] + UNDER_A_BUTTON and box[0] < b["box"][2] and box[2] > b["box"][0]
    page = [l for l in lines or [] if l[1][1] < height * 0.75 and not BAR.match(l[0])]
    return [l for l in page if not any(same_button(b["box"], l[1]) or under(b, l[1]) for b in buttons)], page


def page_heading(lines, buttons, frame=None, height=480):
    """A page's heading: of its free lines that aren't a link off the disc, the one with the most signs,
    at least two (menus.heading_signs); or None. A button not found has one at most: its column's top."""
    free, page = free_lines(lines, buttons, height)
    return menus.best_heading([l for l in free if not LINK.search(l[0])], page, frame)


def several_headings(lines, buttons, frame=None, height=480):
    """Whether a page has several headings, as a DVD's (menus.several_headings)."""
    free, page = free_lines(lines, buttons, height)
    return menus.several_headings(free, page, frame, [b["text"] for b in buttons])


def page_order(buttons):
    """A page's buttons in reading order: down its left column, then the next."""
    middle = lambda b: (b["box"][0] + b["box"][2]) / 2
    columns = []
    for b in sorted(buttons, key=middle):
        if columns and middle(b) - middle(columns[-1][-1]) < COLUMN_GAP:
            columns[-1].append(b)
        else:
            columns.append([b])
    return [b for c in columns for b in sorted(c, key=lambda x: x["box"][1])]


def extras_play_all(titles, count):
    """The clips of a Play All of this many clips, each a title of its own: [title id], or None."""
    for t in titles:
        clips = (t.get("clip_list") or "").split(",")
        if len(clips) == count >= 2:
            ids = [title_of(titles, clip=c) for c in clips]
            if all(ids):
                return ids
    return None


def readings_of(pressed):
    """What was pressed as readings (menus.tv_titles'): a title two buttons both land on is left out."""
    counts = {}
    for p in pressed:
        counts[p["title"]] = counts.get(p["title"], 0) + 1
    out = []
    for p in pressed:
        if counts[p["title"]] > 1:
            continue
        number = "%d) " % p["number"] if p.get("number") is not None else ""
        out.append({"title": p["title"], "text": number + p["name"], "seconds": p["seconds"], "bonus": False, "kind": None,
                    "page": ["bd", 0, 0], "rect": [round(x) for x in p["box"]], "place": [p["order"]], "image": p.get("image"),
                    "switch": False, "only_kind": False, "only_cut": False, "commentaries": False})
        if p.get("extra"):
            out[-1].update(bonus=True, kind=p.get("kind"), own_name=True)
    return out


class Crawler:
    def __init__(self, v, titles, items, out_dir, log=print, sleep=time.sleep, clock=time.time):
        self.v, self.titles, self.items, self.out_dir = v, titles, items, out_dir
        self.log, self.sleep, self.clock = log, sleep, clock
        self.places = None                      # (the bar's page of episodes, its special features)

    def rows(self):
        return page_rows(menus.rapid_lines(self.v.frame()))

    def wait_playing(self, seconds):
        """Past the intros: VLC's title of this length playing (the disc's longest, its Play All, by the
        scan: VLC's own list fills in as it goes), or the top menu up. That title's number, 0 for the top
        menu, None if neither."""
        start = self.clock()
        while self.clock() - start < WAIT_PLAYING:
            self.sleep(0.5)
            if not self.v.frames:
                continue
            s = self.v.state()
            title = next((t["index"] for t in self.v.titles() if not t.get("menu") and abs(t["seconds"] - seconds) <= 2), None)
            if title is not None and s["title"] == title and s["time_ms"] > 4000:
                return title
            # (only if it's stayed up, as a disc starting up shows title 0 for a while)
            if s["title"] == 0 and self.clock() - start > TOP_MENU_AFTER:
                return 0
        return None

    def show_bar(self):
        """The pop-up fresh, at its first item, by what's on screen (a press can leave it up, and the
        same key closes it). A seek on the disc can hold it back a moment. Its lines, or None."""
        if any(x is not None for x in bar_places(menus.rapid_lines(self.v.frame()))):
            self.v.press("popup")
            self.sleep(0.8)
        self.v.press("popup")
        asked = self.clock()
        while self.clock() - asked < BAR_WAIT:
            self.sleep(0.5)
            lines = menus.rapid_lines(self.v.frame())
            if any(x is not None for x in bar_places(lines)):
                return lines
        return None

    def open_bar(self, place):
        """The pop-up, and across its bar to the item at this place: its lines, or None."""
        lines = self.show_bar()
        if lines is None:
            return None
        if self.places is None:
            self.places = bar_places(lines)
        for _ in range(self.places[place] or 0):
            self.v.press("right")
            self.sleep(0.6)
        self.v.press("activate")
        self.sleep(1.5)
        return lines

    def open_page(self):
        """The pop-up, and the bar across to its page of episodes: the page's rows. Read again if not
        there yet."""
        for _ in range(2):
            if self.open_bar(0) is None:
                continue
            for _ in range(2):
                rows = self.rows()
                if len(rows) >= 2 and all(r["number"] is not None for r in rows):
                    return rows
                self.sleep(1)
        return []

    def walk_to(self, target):
        """The marker on this button: True once it's been read there. The marker can lag a press."""
        for _ in range(10):
            rows = self.rows()
            mark = next((r for r in rows if r["marked"]), None)
            if not mark:
                self.sleep(0.5)                 # (once: read again before giving up)
                rows = self.rows()
                mark = next((r for r in rows if r["marked"]), None)
                if not mark:
                    return False
            if nearest(rows, target["box"]) is mark:
                return True
            self.v.press(key_towards(mark["box"], target["box"]))
            moved = self.clock()
            while self.clock() - moved < 2.5:
                self.sleep(0.25)
                now = next((r for r in self.rows() if r["marked"]), None)
                if now and abs(now["box"][0] - mark["box"][0]) + abs(now["box"][1] - mark["box"][1]) > SAME_ROW:
                    break
        return False

    def press(self):
        """Activate, and watch for where it lands: (VLC's title, seconds), or None if nothing moved."""
        was = self.v.state()
        pressed = self.clock()
        self.v.press("activate")
        while self.clock() - pressed < SETTLE:
            self.sleep(0.25)
            s = self.v.state()
            ran = self.clock() - pressed
            if s["title"] != was["title"] or abs(s["time_ms"] - was["time_ms"]) / 1000.0 > ran + 5:
                self.sleep(0.5)                 # (a moment for the new time to settle)
                s = self.v.state()
                return s["title"], s["time_ms"] / 1000.0
        return None

    def landed(self, title, seconds, vlc_titles, play_all):
        """What a landing plays: (the scan's title, its length, where it starts), or None."""
        if title == play_all:
            hit = clip_at(self.items, seconds)
            return (title_of(self.titles, clip=hit[0]), hit[1], hit[2]) if hit else None
        length = self.v.length_ms() / 1000.0
        return (title_of(self.titles, seconds=length), length, 0.0) if length > 0 else None

    def crawl(self, vlc_titles, play_all):
        """Each button of the page pressed in turn: [{"title", "number", "name", "seconds", "box", ...}]."""
        pressed, page, done, again, image = [], [], set(), set(), None
        for round_no in range(40):
            rows = self.open_page()
            if len(rows) > len(page):           # (the fullest reading of the page is its layout)
                page = rows
                image = "bd_page.png"
                self.v.frame().save(os.path.join(self.out_dir, image))
            # (one that didn't move goes last, once: playback may have been where it leads already)
            todo = sorted((r for i, r in enumerate(page) if i not in done), key=lambda r: page.index(r) in again)
            if not rows or not todo:
                break
            target = todo[0]
            i = page.index(target)
            self.log("[%d/%d] button %s %s" % (len(done) + 1, len(page), target["number"], target["name"]))
            done.add(i)
            if not self.walk_to(target):
                self.log("  its marker wasn't seen on it: not pressed")
                self.v.press("popup")           # (closed: the next round opens it afresh)
                self.sleep(0.5)
                continue
            read = nearest(self.rows(), target["box"]) or target     # (its words, with the marker on it)
            where = self.press()
            if not where:
                self.log("  nothing moved")
                if i not in again:
                    again.add(i)
                    done.discard(i)
                continue
            got = self.landed(where[0], where[1], vlc_titles, play_all)
            self.log("  title %s at %.0f s: %s" % (where[0], where[1], got[0] if got else "not one title's start"))
            if got and got[0]:
                name = read["name"] if len(read["name"]) >= len(target["name"]) else target["name"]
                pressed.append({"title": got[0], "number": target["number"], "name": name, "seconds": got[1],
                                "box": target["box"], "order": i, "image": image, "vlc": where[0]})
        return pressed

    def marked(self):
        lines = menus.rapid_lines(self.v.frame())
        return marked_line(lines), lines

    def step(self, key, here):
        """An arrow: the marker's new (text, box), or None if it stays or leaves the page (put back).
        It can lag a press, and a reading can miss it: off the page only if it's gone from several."""
        self.v.press(key)
        moved, gone = self.clock(), 0
        while self.clock() - moved < STEP_WAIT:
            self.sleep(0.3)
            mark = self.marked()[0]
            if mark and not same_button(mark[1], here):
                return mark
            gone = gone + 1 if not mark else 0
            if gone >= 3:
                self.v.press(BACK[key])         # (off the page, to the bar: back)
                self.sleep(0.6)
                return None
        return None

    def page_up(self):
        """The page of special features up, its marker read: (its text and box, the page's lines).
        After a scene it's up by itself, its bar gone (Chuck's: "Resume Episode"): no keys for that."""
        waited = self.clock()
        while self.clock() - waited < PAGE_BACK:
            mark, read = self.marked()
            if mark:
                return mark, read
            self.sleep(0.5)
        for _ in range(MENU_TRIES):
            self.open_bar(1)
            mark, read = self.marked()
            if mark:
                return mark, read
            self.sleep(3)                       # (the top menu takes a while to answer, after a scene)
        return None, None

    def walk(self, buttons, here, ways=KEYS, enough=None, limit=EXTRAS_LIMIT):
        """Every button of the page, by moving its marker. What's learned of the way between them goes
        in each button's "next". The first one tries up and left too, in case it wasn't at the top."""
        first = here
        tries = lambda b: ways + (("up", "left") if b is first else ())

        def known(mark):
            b = next((x for x in buttons if same_button(x["box"], mark[1])), None)
            if not b:
                b = {"text": mark[0], "box": mark[1], "next": {}, "n": len(buttons)}
                buttons.append(b)
            return b
        started = self.clock()
        while self.clock() - started < limit and not (enough and enough()):
            queue, seen, way = [(here, [])], {id(here)}, None
            while queue and way is None:
                b, keys = queue.pop(0)
                untried = [k for k in tries(b) if k not in b["next"]]
                if untried:
                    way = (keys, b, untried[0])
                for k, other in b["next"].items():
                    if other is not None and id(other) not in seen:
                        seen.add(id(other))
                        queue.append((other, keys + [k]))
            if way is None:
                return known
            keys, goal, key = way
            for k in keys:                      # (it may land elsewhere: wherever it's read)
                got = self.step(k, here["box"])
                if got:
                    here = known(got)
                else:
                    here["next"][k] = None
                    break
            if here is not goal:
                continue
            got = self.step(key, here["box"])
            was, here = here, known(got) if got else here
            was["next"][key] = here if got else None
            if got:
                here["next"].setdefault(BACK[key], was)
        return known

    def press_button(self, button, known, vlc_titles, play_all):
        """Bring the page up, take the marker to this button and press it: (what it plays, its length,
        where it starts, VLC's title), or None."""
        mark, _ = self.page_up()
        if not mark:
            return None
        here = known(mark)
        for _ in range(30):
            if here is button:
                break
            queue, seen, keys = [(here, [])], {id(here)}, None
            while queue and keys is None:
                b, way = queue.pop(0)
                if b is button:
                    keys = way
                for k, other in b["next"].items():
                    if other is not None and id(other) not in seen:
                        seen.add(id(other))
                        queue.append((other, way + [k]))
            if not keys:
                return None
            got = self.step(keys[0], here["box"])
            if got:
                here = known(got)
            else:
                here["next"][keys[0]] = None
        if here is not button:
            return None
        where = self.press()
        got = self.landed(where[0], where[1], vlc_titles, play_all) if where else None
        self.log("  title %s: %s" % (where[0] if where else "-", got[0] if got else "not one of the disc's titles"))
        if where and where[0] != play_all:
            self.v.menu()                       # (the pop-up doesn't answer while it plays)
            self.sleep(1)
        return got + (where[0],) if got and got[0] else None

    def extras(self, vlc_titles, play_all):
        """The bar's page of special features: its buttons found by the marker, and what each plays."""
        if (self.places or (None, None))[1] is None:
            return []
        self.open_bar(1)
        mark, lines = self.page_up()
        if not mark:
            return []
        image = "bd_extras.png"
        frame = self.v.frame()
        frame.save(os.path.join(self.out_dir, image))
        buttons = []
        here = {"text": mark[0], "box": mark[1], "next": {}, "n": 0}
        buttons.append(here)
        known = self.walk(buttons, here)
        scenes = lambda: page_order([b for b in buttons if not menus.PLAY_ALL_RE.search(b["text"]) and not LINK.search(b["text"])])
        real = scenes()
        if not extras_play_all(self.titles, len(real)):
            # (not what its Play All has: one may sit above where the marker came across, Chuck S2 D1's)
            self.log("special features: %d buttons, not what the page's Play All has: every way from each" % len(real))
            mark = self.marked()[0] or self.page_up()[0]
            if mark:
                self.walk(buttons, known(mark), KEYS + ("up", "left"), enough=lambda: extras_play_all(self.titles, len(scenes())),
                          limit=EVERY_WAY_LIMIT)
                real = scenes()
        self.log("special features: %d buttons" % len(real))
        kind = None if several_headings(lines, buttons, frame) else menus.heading_kind(page_heading(lines, buttons, frame))

        def entry(b, title, seconds, vlc=None):
            return {"title": title, "number": None, "name": button_words(b, buttons, lines), "seconds": seconds, "box": b["box"],
                    "order": 100 + b["n"], "image": image, "vlc": vlc, "extra": True, "kind": kind}

        heard = {}

        def hear(b):
            if id(b) not in heard:
                self.log("[%d/%d] button %s" % (len(heard) + 1, len(real), b["text"]))
                heard[id(b)] = self.press_button(b, known, vlc_titles, play_all)
            return heard[id(b)]

        order = extras_play_all(self.titles, len(real))
        if order and len(real) >= 2:
            ends = [(real[0], order[0]), (real[-1], order[-1])]
            if all((hear(b) or [None])[0] == want for b, want in ends):
                lengths = {t["id"]: t.get("duration") for t in self.titles}
                self.log("its two ends land where the page's Play All has them: its order names the rest")
                out = []
                first, last = heard[id(real[0])][3], heard[id(real[-1])][3]
                # (VLC's numbers run on through them, where the two ends say so: for watching)
                run = last - first == len(real) - 1
                for i, (b, want) in enumerate(zip(real, order)):
                    out.append(entry(b, want, lengths.get(want), first + i if run else None))
                return out
        return [entry(b, h[0], h[1], h[3]) for b, h in ((b, hear(b)) for b in real) if h]


def from_files(root, label, titles, log=print, read=None, out_dir=None):
    """A TV disc's titles from its menu's own files (bdmenus.wb_menu), nothing played: what crawl gives,
    its readings saying exactly which episode each is. None for a disc with no such files. read: a
    picture to its words, for the extras' names and to check the episodes' (RapidOCR's, by default).
    out_dir: each title's own button saved there, to look at in Edit titles."""
    if read is None:
        read = lambda image: " ".join(t for t, _, _ in (menus.rapid_lines(image) or []))
    menu = bdmenus.wb_menu(root, label, read)
    if not menu:
        return None
    lengths = {t["id"]: t.get("duration") for t in titles}
    readings = []
    plain = dict(page=["bd", 0, 0], rect=None, image=None, switch=False, only_cut=False, commentaries=False)

    def button_picture(reading, entry):
        """The reading with its button's picture, saved, and what it says on it."""
        image = menu["picture"](entry["button"]) if out_dir and entry.get("button") else None
        if image is not None:
            name = "button_%s.png" % reading["title"].replace(".", "_")
            try:
                os.makedirs(out_dir, exist_ok=True)
                image.save(os.path.join(out_dir, name))
            except OSError:
                return reading
            reading.update(image=name, rect=[0, 0, image.width, image.height], alone=True, on_button=entry.get("read") or "")
        return reading
    for e in menu["episodes"]:
        if e["playlist"] not in lengths:
            log("the menu's episode %d plays %s, not among the disc's titles" % (e["number"], e["playlist"]))
            continue
        readings.append(button_picture(dict(plain, title=e["playlist"], text=("%d) %s" % (e["number"], e["read"])).strip(), number=e["number"],
                                            seconds=lengths[e["playlist"]], bonus=False, kind=None, only_kind=False, place=[e["number"]]), e))
    if not readings:
        return None
    for i, x in enumerate(menu["extras"]):
        if x["playlist"] not in lengths:
            continue
        kind = menus.heading_kind(x["under"]) or menus.heading_kind(x["read"])
        if x["of_episode"] is not None:         # (an episode's own: its scissor button's)
            kind = kind or "DeletedScene"
            text = {"DeletedScene": "Deleted Scenes"}.get(kind) or x["read"] or x["under"]
        else:
            text = x["read"] or x["under"]
        if text:
            readings.append(button_picture(dict(plain, title=x["playlist"], text=text, seconds=lengths[x["playlist"]], bonus=True, kind=kind,
                                                only_kind=x["of_episode"] is not None, of_episode=x["of_episode"], place=[100 + i]), x))
    log("the disc's menu files list %d of its titles, nothing played" % len(readings))
    return {"tv": {"readings": readings, "exact": True, "season": menu["season"], "disc": menu["disc"], "played": [r["title"] for r in readings],
                   "promos": [], "promo_names": {}, "play_alls": [],
                   "opening": [], "commentary_versions": []}, "pages": [], "found": {}, "suspects": len(readings), "watch": {}}


def crawl(root, vlc_dir, titles, out_dir, log=print):
    """Read and press a Blu-ray's page of episodes: what menus.tv_titles gives for a DVD, under "tv".
    menus.Lost when no such page answered."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(list(bdmenus.decryption_dirs()) + [path])
    v = menus.Vlc(vlc_dir, highlight=True)
    try:
        v.open("bluray:///%s/" % root.replace("\\", "/").rstrip("/"))
        longest = max((t.get("duration") or 0) for t in titles)
        c = Crawler(v, titles, play_alls(root, longest), out_dir, log)
        playing = c.wait_playing(longest)
        if playing is None:
            raise menus.Lost("nothing started playing")
        vlc_titles = [t for t in v.titles() if not t.get("menu")]
        pressed = c.crawl(vlc_titles, playing)
        if not pressed:
            raise menus.Lost("no page of episodes answered")
        pressed += c.extras(vlc_titles, playing)
        readings = readings_of(pressed)
        return {"tv": {"readings": readings, "played": [p["title"] for p in pressed], "promos": [], "promo_names": {},
                       "play_alls": [], "opening": [],
                       "commentary_versions": []}, "pages": [], "found": {}, "suspects": len(pressed),
                # (what VLC can open by itself: it can't seek a Play All to an episode inside it)
                "watch": {p["title"]: {"title": p["vlc"]} for p in pressed if p.get("vlc") not in (None, playing)}}
    finally:
        v.close()
        os.environ["PATH"] = path
