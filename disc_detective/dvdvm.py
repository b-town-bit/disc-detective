"""A DVD player's command engine, run on the disc's own data: what a menu button does, found
without playing the disc.

A DVD's menus are a little program. Every button carries commands ("set register 4 to 2, then
go to menu page 8"), and every menu page has commands of its own that run on the way in and at
its end ("if register 4 is 2, play title 10"). A player keeps 16 registers the disc can set and
24 of its own (language, region, which title is playing...) and runs those commands as you press.
This module does the same, from the IFO files and the menus' button tables (menus.dvd_structure):
start the disc as VLC does (its first-play program's settings, title 1's, then its Title menu), reach the
menu it settles on, and from there press any button and follow where it goes, to a menu page or
to a title. The command set, its bit layout and its arithmetic follow the DVD spec as libdvdnav
(VLC's DVD library) implements it, so the answers are what VLC would do.

What it can't follow faithfully (a random number, a timer, resuming a title never played) it
says so ("unsure"), and the crawl presses that button in VLC instead.
"""
import os
import sys

from .menus import SECTOR, _menu_pgcs, _pgc, _u16, _u32

MAX_COMMANDS = 20000        # a runaway disc program (a loop): give up, unsure
MAX_PGCS = 200
LONG_TITLE = 20 * 60        # starting the disc, a title this long is its feature, playing by itself
FLEETING = 2.0              # a menu page shorter than this, held by no still, can't be used: it plays on


class Unsure(Exception):
    """The engine can't follow this faithfully (random numbers, timers, a loop...)."""


class _AtTitle(Exception):
    """Starting the disc as VLC does: the first-play program has reached a title."""


def bits(cmd, start, count):
    """Bits of an 8-byte command, libdvdnav's way: start counts from the least significant bit
    (63 is the first byte's top bit), and count bits are taken going down from there."""
    return (int.from_bytes(cmd, "big") >> (start - count + 1)) & ((1 << count) - 1)


# link sub-instructions (LinkSIns), by their 5-bit code
LINKS = {0: "nolink", 1: "top_cell", 2: "next_cell", 3: "prev_cell", 5: "top_program", 6: "next_program",
         7: "prev_program", 9: "top_pgc", 10: "next_pgc", 11: "prev_pgc", 12: "up_pgc", 13: "tail_pgc",
         16: "resume"}


# --------------------------------------------------------------------------
# the disc: everything the commands can reach
# --------------------------------------------------------------------------

class Disc:
    """The disc's programs: the first-play PGC, the menu PGCs of the VMG and of each title set
    (their first language unit), each title set's title PGCs and chapter map, the title table,
    and the menus' buttons (from menus.dvd_structure: pages and their programs' buttons)."""

    buttons_at = {}                         # (when each program's buttons come on: none known)

    def __init__(self, root, structure):
        vt = os.path.join(root, "VIDEO_TS") if os.path.isdir(os.path.join(root, "VIDEO_TS")) else root
        files = {f.upper(): os.path.join(vt, f) for f in os.listdir(vt)}
        with open(files["VIDEO_TS.IFO"], "rb") as fh:
            vmg = fh.read()
        fp = _u32(vmg, 0x84)
        self.first_play = _pgc(vmg, fp) if fp else None
        self.menus = {"vmgm": {n: (typ, entry, p) for n, typ, entry, p in _menu_pgcs(vmg, _u32(vmg, 0xC8))}}
        self.titles = [{"title": t["title"], "vts": t["vts"], "ttn": t["ttn"]} for t in structure.get("titles", [])]
        self.title_pgcs, self.ptt = {}, {}
        for t in self.titles:
            v = t["vts"]
            if v in self.title_pgcs or "VTS_%02d_0.IFO" % v not in files:
                continue
            with open(files["VTS_%02d_0.IFO" % v], "rb") as fh:
                ifo = fh.read()
            self.menus[v] = {n: (typ, entry, p) for n, typ, entry, p in _menu_pgcs(ifo, _u32(ifo, 0xD0))}
            pgci = _u32(ifo, 0xCC) * SECTOR
            self.title_pgcs[v] = {i + 1: _pgc(ifo, pgci + _u32(ifo, pgci + 8 + 8 * i + 4))
                                  for i in range(_u16(ifo, pgci))}
            ptt = _u32(ifo, 0xC8) * SECTOR
            n = _u16(ifo, ptt)
            end = ptt + _u32(ifo, ptt + 4) + 1
            offs = [ptt + _u32(ifo, ptt + 8 + 4 * i) for i in range(n)] + [end]
            self.ptt[v] = {i + 1: [(_u16(ifo, o), _u16(ifo, o + 2)) for o in range(offs[i], offs[i + 1] - 3, 4)]
                           for i in range(n)}
        # buttons by (domain, pgc): each program's
        # (and the seconds into each program that its buttons come on)
        self.buttons, self.buttons_at = {}, {}
        for d, pages in [("vmgm", structure.get("vmgm", []))] + [(int(v), pages) for v, pages in
                                                                  structure.get("vtsm", {}).items()]:
            for p in pages:
                self.buttons[(d, p["pgc"])] = p.get("program_buttons") or [p["buttons"]]
                self.buttons_at[(d, p["pgc"])] = p.get("buttons_at") or []

    def title_of(self, vts, ttn):
        return next((t["title"] for t in self.titles if (t["vts"], t["ttn"]) == (vts, ttn)), None)

    def where_in_title(self, vts, pgcn, pgn):
        """(title, chapter) for a title set's PGC and program: the chapter map's last entry at or
        before it."""
        best = None
        for ttn, chapters in self.ptt.get(vts, {}).items():
            for ch, (pg, pn) in enumerate(chapters, 1):
                if pg == pgcn and pn <= pgn and (best is None or (best[0] == ttn and pn >= best[2])):
                    best = (ttn, ch, pn)
        return (self.title_of(vts, best[0]), best[1]) if best else (None, None)

    def menu_pgc(self, domain, kind):
        """A domain's menu PGC of a kind (root, title, chapter...): the entry PGC for it."""
        return next((n for n, (typ, entry, _) in self.menus.get(domain, {}).items() if entry and typ == kind), None)


# --------------------------------------------------------------------------
# the player: registers, where it is, and the commands
# --------------------------------------------------------------------------

def default_sprm():
    """A player's own registers as libdvdnav starts them (English, region 1, highlight button 1)."""
    s = [0] * 24
    s[0] = (ord("e") << 8) | ord("n")       # menu language
    s[1], s[2], s[3] = 15, 62, 1             # audio stream (none yet), subpicture (none), angle
    s[4], s[5], s[7] = 1, 1, 1               # title, title-set title, chapter
    s[8] = 1 << 10                           # highlighted button
    s[12], s[13] = (ord("U") << 8) | ord("S"), 15   # parental country, level
    s[14], s[15] = 0x100, 0x7CFC             # video preference, audio capabilities
    s[16] = s[18] = (ord("e") << 8) | ord("n")      # preferred audio / subtitle language
    s[20] = 1                                # region 1
    return s


class Player:
    """One run of the disc's program. Its state (registers and place) can be copied, to press
    several buttons from the same menu."""

    def __init__(self, disc):
        self.disc = disc
        self.gprm = [0] * 16
        self.sprm = default_sprm()
        self.domain, self.pgcn, self.pgn = "fp", 0, 1    # domain: "fp", "vmgm", a VTS number (its menus), ("tt", vts)
        self.cell = None                                  # the cell playing (None: its program's first)
        self.resume = None                                # where LinkRSM returns to (after a CallSS)
        self.count = 0
        self.intro = 0.0                                  # seconds of menu played since the press
        self.fosl = 0                                     # the forced button of the menu video showing

    def copy(self):
        p = Player(self.disc)
        p.gprm, p.sprm, p.fosl = list(self.gprm), list(self.sprm), self.fosl
        p.domain, p.pgcn, p.pgn, p.resume, p.cell = self.domain, self.pgcn, self.pgn, self.resume, self.cell
        return p

    def key(self):
        """Where it is and what it holds: two runs with the same key behave the same."""
        return (self.domain, self.pgcn, self.pgn, tuple(self.gprm), self.sprm[8], self.resume)

    # ---- registers and conditions ----
    def reg(self, r):
        return self.sprm[r & 0x1F] if r & 0x80 else self.gprm[r & 0x0F]

    def reg_or_data(self, cmd, imm, start):
        return bits(cmd, start, 16) if imm else self.reg(bits(cmd, start - 8, 8))

    def reg_or_data_2(self, cmd, imm, start):
        return bits(cmd, start - 1, 7) if imm else self.gprm[bits(cmd, start - 4, 4)]

    @staticmethod
    def compare(op, a, b):
        return {1: bool(a & b), 2: a == b, 3: a != b, 4: a >= b, 5: a > b, 6: a <= b, 7: a < b}[op]

    def if1(self, cmd):
        op = bits(cmd, 54, 3)
        return self.compare(op, self.reg(bits(cmd, 39, 8)), self.reg_or_data(cmd, bits(cmd, 55, 1), 31)) if op else True

    def if2(self, cmd):
        op = bits(cmd, 54, 3)
        return self.compare(op, self.reg(bits(cmd, 15, 8)), self.reg(bits(cmd, 7, 8))) if op else True

    def if3(self, cmd):
        op = bits(cmd, 54, 3)
        # (a general register only: 4 bits; WALL-E's commentary switch, "if GPRM1 == 1")
        return self.compare(op, self.gprm[bits(cmd, 43, 4)], self.reg_or_data(cmd, bits(cmd, 55, 1), 15)) if op else True

    def if4(self, cmd):
        op = bits(cmd, 54, 3)
        return self.compare(op, self.reg(bits(cmd, 51, 4)), self.reg_or_data(cmd, bits(cmd, 55, 1), 31)) if op else True

    def if5(self, cmd):
        op = bits(cmd, 54, 3)
        if not op:
            return True
        if bits(cmd, 60, 1):
            return self.compare(op, self.reg(bits(cmd, 31, 8)), self.reg(bits(cmd, 23, 8)))
        return self.compare(op, self.reg(bits(cmd, 39, 8)), self.reg(bits(cmd, 31, 8)))

    def set_op(self, op, reg, reg2, data):
        g = self.gprm
        if op == 1:
            g[reg] = data
        elif op == 2:                         # swap
            g[reg2], g[reg] = g[reg], data
        elif op == 3:
            g[reg] = min(g[reg] + data, 0xFFFF)
        elif op == 4:
            g[reg] = max(g[reg] - data, 0)
        elif op == 5:
            g[reg] = min(g[reg] * data, 0xFFFF)
        elif op == 6:
            g[reg] = g[reg] // data if data else 0xFFFF
        elif op == 7:
            g[reg] = g[reg] % data if data else 0xFFFF
        elif op == 8:
            raise Unsure("a random number")
        elif op == 9:
            g[reg] &= data
        elif op == 10:
            g[reg] |= data
        elif op == 11:
            g[reg] ^= data

    # ---- one command: (goto line or 0, link or None) ----
    def link_subins(self, cmd, cond):
        op = bits(cmd, 4, 5)
        if not cond or op > 16:
            return None
        return {"op": LINKS.get(op, "nolink"), "button": bits(cmd, 15, 6)}

    def link_ins(self, cmd, cond):
        op = bits(cmd, 51, 4)
        if op == 1:
            return self.link_subins(cmd, cond)
        if not cond:
            return None
        if op == 4:
            return {"op": "pgcn", "n": bits(cmd, 14, 15)}
        if op == 5:
            return {"op": "pttn", "n": bits(cmd, 9, 10), "button": bits(cmd, 15, 6)}
        if op == 6:
            return {"op": "pgn", "n": bits(cmd, 6, 7), "button": bits(cmd, 15, 6)}
        if op == 7:
            return {"op": "cn", "n": bits(cmd, 7, 8), "button": bits(cmd, 15, 6)}
        return None

    def jump_ins(self, cmd, cond):
        if not cond:
            return None
        op = bits(cmd, 51, 4)
        if op == 1:
            return {"op": "exit"}
        if op == 2:
            return {"op": "jump_tt", "title": bits(cmd, 22, 7)}
        if op == 3:
            return {"op": "jump_vts_tt", "ttn": bits(cmd, 22, 7)}
        if op == 5:
            return {"op": "jump_vts_ptt", "ttn": bits(cmd, 22, 7), "ptt": bits(cmd, 41, 10)}
        if op in (6, 8):
            call = op == 8
            kind = bits(cmd, 23, 2)
            if kind == 0:
                return {"op": "first_play", "call": call}
            if kind == 1:
                return {"op": "vmgm_menu", "menu": bits(cmd, 19, 4), "call": call}
            if kind == 2:                     # (a call: the menu of the title set playing)
                return {"op": "vtsm", "vts": None if call else bits(cmd, 31, 8),
                        "ttn": None if call else bits(cmd, 39, 8), "menu": bits(cmd, 19, 4), "call": call}
            return {"op": "vmgm_pgcn", "n": bits(cmd, 46, 15), "call": call}
        return None

    def system_set(self, cmd, cond):
        op = bits(cmd, 59, 4)
        if op == 1:                           # stream numbers: audio, subpicture, angle
            for i in (1, 2, 3):
                if bits(cmd, 63 - (2 + i) * 8, 1):
                    data = self.reg_or_data_2(cmd, bits(cmd, 60, 1), 47 - i * 8)
                    if cond:
                        self.sprm[i] = data
        elif op == 2:                         # navigation timer: not followed (time-outs)
            pass
        elif op == 3:                         # a register's mode (a counter ticks with time)
            data = self.reg_or_data(cmd, bits(cmd, 60, 1), 47)
            if cond:
                self.gprm[bits(cmd, 19, 4)] = data
                if bits(cmd, 23, 1):
                    raise Unsure("a register counting time")
        elif op == 6:                         # highlighted button
            data = self.reg_or_data(cmd, bits(cmd, 60, 1), 31)
            if cond:
                self.sprm[8] = data
        return self.link_ins(cmd, cond) if bits(cmd, 51, 4) else None

    def execute(self, cmd):
        """One command: (line to go to, or 0 for the next; a link, or None)."""
        kind = bits(cmd, 63, 3)
        if kind == 0:                         # special: nop, goto, break, parental level
            cond = self.if1(cmd)
            op = bits(cmd, 51, 4)
            if op == 1 and cond:
                return bits(cmd, 7, 8), None
            if op == 2 and cond:
                return 100000, None           # break: past the end
            return 0, None
        if kind == 1:                         # link, or jump / call
            if bits(cmd, 60, 1):
                return 0, self.jump_ins(cmd, self.if2(cmd))
            return 0, self.link_ins(cmd, self.if1(cmd))
        if kind == 2:
            return 0, self.system_set(cmd, self.if2(cmd))
        if kind == 3:                         # set, maybe then link
            cond = self.if3(cmd)
            if cond:
                self.set_op(bits(cmd, 59, 4), bits(cmd, 35, 4), bits(cmd, 19, 4),
                            self.reg_or_data(cmd, bits(cmd, 60, 1), 31))
            return 0, (self.link_ins(cmd, cond) if bits(cmd, 51, 4) else None)
        if kind == 4:                         # set; compare; link sub-instruction
            self.set_op(bits(cmd, 59, 4), bits(cmd, 51, 4), 0, self.reg_or_data(cmd, bits(cmd, 60, 1), 47))
            return 0, self.link_subins(cmd, self.if4(cmd))
        if kind in (5, 6):                    # compare; set; link sub-instruction (always, for 6)
            cond = self.if5(cmd)
            if cond:
                self.set_op(bits(cmd, 59, 4), bits(cmd, 51, 4), 0, self.reg_or_data(cmd, bits(cmd, 60, 1), 47))
            return 0, self.link_subins(cmd, cond if kind == 5 else True)
        raise Unsure("an unknown command %s" % bytes(cmd).hex())

    def run(self, cmds):
        """A command list (pre, post, a cell's or a button's): the link it ends in, or None."""
        i = 0
        while i < len(cmds):
            self.count += 1
            if self.count > MAX_COMMANDS:
                raise Unsure("the disc's program loops")
            line, link = self.execute(cmds[i])
            if link:
                return link
            i = line - 1 if line else i + 1
        return None


# --------------------------------------------------------------------------
# following the program: where a link leads, until the disc waits or plays
# --------------------------------------------------------------------------

class Engine:
    """Runs the disc's program: start() reaches the first menu that waits for a viewer; press()
    a button there and follow it to where the disc next waits, or the title it plays."""

    def __init__(self, disc, short_title=60):
        self.disc = disc
        self.short = short_title              # a title this short (a warning, an outtake) plays through
        self.menu_key = False                 # starting the disc, a viewer had to press Menu
        self.to_title = False                 # starting the disc: stop as the first title begins

    # ---- where things are ----
    def pgc(self, p):
        if isinstance(p.domain, tuple):
            return self.disc.title_pgcs.get(p.domain[1], {}).get(p.pgcn)
        if p.domain == "fp":
            return self.disc.first_play
        m = self.disc.menus.get(p.domain, {}).get(p.pgcn)
        return m[2] if m else None

    def buttons(self, p):
        progs = self.disc.buttons.get((p.domain, p.pgcn)) or []
        return progs[p.pgn - 1] if 0 < p.pgn <= len(progs) else []

    # ---- the runs ----
    def start(self):
        """Opening the disc as VLC does ("start directly in menu", its default): the disc's
        first-play program up to the first title it plays (its registers set, the title not
        played), then title 1 (its pre commands only: libdvdnav runs them, a jump in them
        ignored), then the Title menu, else the Root menu, as called from there. (player,
        outcome): the menu it waits on. A player plays the start through instead: Father of the
        Bride's 6 minutes of trailers, which VLC never shows, and its main menu then highlights
        another button (title 1's commands set the register it reads)."""
        p = Player(self.disc)
        self.menu_key = False
        if self.disc.first_play:
            self.to_title = True
            try:
                self.enter(p, "fp", 0, through=True)
            except _AtTitle:
                pass
            finally:
                self.to_title = False
        t = next((t for t in self.disc.titles if t["title"] == 1), None)
        chapters = self.disc.ptt.get(t["vts"], {}).get(t["ttn"]) if t else None
        pg = self.disc.title_pgcs.get(t["vts"], {}).get(chapters[0][0]) if chapters else None
        if pg is None:                        # (no title 1: the first-play program's way, played through)
            return self._start_played()
        pgcn, pgn = chapters[0]
        p.domain, p.pgcn, p.pgn, p.cell = ("tt", t["vts"]), pgcn, pgn, None
        p.sprm[4], p.sprm[5], p.sprm[6], p.sprm[7] = 1, t["ttn"], pgcn, 1
        try:
            p.run(pg["pre"])
        except Unsure:
            return self._start_played()
        p.resume = (p.domain, pgcn, pgn)
        out = {"unsure": "no title menu"}
        if self.disc.menu_pgc("vmgm", "title"):
            out = self.follow(p.copy(), {"op": "vmgm_menu", "menu": 2, "call": True}, through=True)
        if "menu" not in out:
            self.menu_key = False
            out = self.follow(p.copy(), {"op": "vtsm", "menu": 3, "call": True}, through=True)
        if "menu" not in out:
            return self._start_played()
        if self.menu_key:                     # it only got to a menu by a viewer pressing Menu
            out["menu_key"] = True
        return out["player"], out

    def _start_played(self):
        """The disc started as a player does: its first-play program played through to the menu
        it waits on. No first-play program: the title menu."""
        p = Player(self.disc)
        self.menu_key = False
        if self.disc.first_play:
            out = self.enter(p, "fp", 0, through=True)
        else:
            out = self.follow(p, {"op": "vmgm_menu", "menu": 2}, through=True)
        if self.menu_key:
            out["menu_key"] = True
        return out.get("player") or p, out

    def press(self, player, n):
        """Press button n of the menu the player waits on (a copy is used): the outcome.
        {"menu": (domain, pgc, program), "player": ...} where it waits next, or {"title", "chapter",
        "before": [short titles first], "player": ...} what plays, or {"unsure": why}."""
        p = player.copy()
        b = next((b for b in self.buttons(p) if b["n"] == n), None)
        if not b or not b.get("raw"):
            return {"unsure": "no such button"}
        p.sprm[8] = n << 10
        try:
            link = p.run([bytes.fromhex(b["raw"])])
            if not link:
                return {"menu": (p.domain, p.pgcn, p.pgn), "player": p, "stays": True}
            out = self.follow(p, link, through=False)
        except Unsure as e:
            return {"unsure": str(e)}
        if "title" in out and out.get("player") is not None:
            into = []
            out["after"] = self._after(out["player"], out["title"], into=into)
            if into:
                out["into"] = into[0]
        return out

    def _after(self, p, first, limit=20, into=None):
        """The titles that play on, one after another, after the one a button plays: up to a
        menu, or a film (The Sorcerer's Apprentice's Sneak Peeks: 66, then 70 and 74; FastPlay:
        those, then the film)."""
        out, q = [], p.copy()
        for _ in range(limit):
            try:
                res = self._play_out(q, self.pgc(q), False, [])
            except (Unsure, RecursionError):
                break
            if "title" not in res or res["title"] in out + [first]:
                break
            if (res.get("seconds") or 0) >= LONG_TITLE:
                # (a film ends the run, not part of it: noted in into, Wallace & Gromit's Play
                # All going on from title 1 into title 6, The Wrong Trousers)
                if into is not None:
                    into.append(res["title"])
                break
            out.append(res["title"])
            q = res["player"]
        return out

    def enter(self, p, domain, pgcn, pgn=1, through=False, played=None, pre=True):
        try:
            return self._enter(p, domain, pgcn, pgn, through, played if played is not None else [], pre)
        except Unsure as e:
            return {"unsure": str(e)}
        except RecursionError:                # a chain of links too long to follow: a loop, or near enough
            return {"unsure": "the disc's program loops"}

    def follow(self, p, link, through, played=None):
        played = played if played is not None else []
        try:
            return self._link(p, link, through, played)
        except Unsure as e:
            return {"unsure": str(e)}
        except RecursionError:
            return {"unsure": "the disc's program loops"}

    def _enter(self, p, domain, pgcn, pgn, through, played, pre, cell=None):
        """Into a PGC (at a program, or a cell): its pre commands, then it plays: a title (the
        answer, or played through if short or if starting the disc), or a menu page: it waits
        there if it has buttons, else it plays to its end (cell commands, then post commands).
        Starting the disc, a long title is the disc playing its feature by itself: a viewer
        presses Menu, and so does the engine (menu_key: the outcome says so)."""
        p.count += 1
        if p.count > MAX_COMMANDS or len(played) > MAX_PGCS:
            raise Unsure("the disc's program loops")
        p.domain, p.pgcn, p.pgn, p.cell = domain, pgcn, pgn, cell
        pg = self.pgc(p)
        if pg is None:
            raise Unsure("a PGC that isn't there (%s %s)" % (domain, pgcn))
        if pre and pgn == 1 and cell is None:
            link = p.run(pg["pre"])
            if link:
                return self._link(p, link, through, played)
        if isinstance(domain, tuple):
            p.fosl = 0                        # (a title's video forces no button)
        elif pg.get("cells"):                 # (a page of commands only plays nothing: no cell change)
            self._forced(p)
        if isinstance(domain, tuple):         # a title
            if self.to_title:
                raise _AtTitle()
            title, chapter = self.disc.where_in_title(domain[1], pgcn, pgn)
            p.sprm[4] = title or p.sprm[4]
            p.sprm[5] = next((t["ttn"] for t in self.disc.titles if t["title"] == title), p.sprm[5])
            p.sprm[6], p.sprm[7] = pgcn, chapter or 1
            seconds = pg["seconds"]
            if not through and seconds >= self.short:
                return {"title": title, "chapter": chapter, "before": [x["title"] for x in played],
                        "seconds": seconds, "player": p}
            if through and seconds >= LONG_TITLE:       # the disc plays its feature: press Menu
                self.menu_key = True
                return self._link(p, {"op": "vtsm", "menu": 3, "call": True}, through, played)
            played.append({"title": title, "chapter": chapter, "seconds": seconds})
            return self._play_out(p, pg, through, played)
        if self.buttons(p) and not self._fleeting(pg):    # a menu that waits for the viewer
            return self._waits(p, played)
        if self.buttons(p):
            # a page gone before anyone could press it: it plays on (WALL-E's start: 0.4 s of 36
            # specks of buttons, a trap; a player passes it, to the warnings and the menu)
            q = p.copy()
            try:
                out = self._play_out(q, pg, through, list(played))
            except (Unsure, RecursionError):
                return self._waits(p, played)
            if out.get("menu") == (p.domain, p.pgcn, p.pgn) or "stopped" in out:
                return self._waits(p, played)     # (it comes back to itself, or ends: it's the page)
            p.__dict__.update(q.__dict__)
            out["player"] = p
            return out
        return self._play_out(p, pg, through, played)

    def _fleeting(self, pg):
        """A menu page shown too briefly to use: no still picture held, its cells together under
        FLEETING seconds (an animated menu loops for longer; a held page has a still)."""
        cells = pg.get("cells") or []
        return bool(cells) and not any(c.get("still") for c in cells) and \
            0 < sum(c.get("seconds") or 0 for c in cells) < FLEETING

    @staticmethod
    def _forced(p):
        """A cell change: libdvdnav forces the highlight to the forced button of the navigation
        packet it read last, so of the video before the change, not the one coming (its "not
        really the right place to do this"). Coming to a page, the page left decides: Enchanted's
        pages force button 1, and back on its main menu, which asks for the button last used, VLC
        highlights 1; a page's own forced button counts only at its next cell change."""
        if p.fosl:
            p.sprm[8] = p.fosl << 10
        p.fosl = 0                            # (the coming video's: known as its cells play)

    def _waits(self, p, played):
        """The outcome for a menu that waits for the viewer, with its highlighted button, as
        libdvdnav picks it: the one SPRM 8 holds (the page's commands, or a forced button: _forced)
        if the page has it, else button 1. The page's own forced button is the one forced at the
        next cell change, a press away from here included."""
        btns = self.buttons(p)
        n = p.sprm[8] >> 10
        if not any(b["n"] == n for b in btns):
            n = 1
        p.sprm[8] = n << 10
        p.fosl = next((b["n"] for b in btns if b.get("forced")), 0)
        at = self.disc.buttons_at.get((p.domain, p.pgcn)) or []
        starts = (self.pgc(p) or {}).get("program_cells") or [1]
        if 0 < p.pgn <= min(len(at), len(starts)) and p.cell in (None, starts[p.pgn - 1]):
            p.intro += at[p.pgn - 1]            # (from the program's start: till its buttons come on)
        out = {"menu": (p.domain, p.pgcn, p.pgn), "player": p, "button": n, "intro": p.intro}
        if played:
            out["played"] = played
        return out

    def _play_out(self, p, pg, through, played):
        """A PGC played to its end from its current program: each cell's command, then the post
        commands; with no link, a title goes on to its next PGC; a menu just stops."""
        cells = pg["cells"]
        starts = pg.get("program_cells") or [1]
        first = p.cell or (starts[p.pgn - 1] if 0 < p.pgn <= len(starts) else 1)
        for n in range(first, len(cells) + 1):
            cell = cells[n - 1]
            p.cell = n
            if n != first:                    # the cell before has played (a menu's intro, say)
                p.intro += cells[n - 2].get("seconds") or 0
                if not isinstance(p.domain, tuple):
                    q = self._program_of(pg, n - 1)
                    progs = self.disc.buttons.get((p.domain, p.pgcn)) or []
                    p.fosl = next((b["n"] for b in (progs[q - 1] if q <= len(progs) else []) if b.get("forced")), 0)
                    self._forced(p)
            if n != first and n in starts and not isinstance(p.domain, tuple):
                # a menu's next program: if it has buttons, the disc waits there for the viewer
                # (The Sorcerer's Apprentice: Tinker Bell, then the FastPlay chooser, one page)
                p.pgn = starts.index(n) + 1
                if self.buttons(p):
                    return self._waits(p, played)
            if cell.get("still") == 255 and not isinstance(p.domain, tuple):
                return {"stopped": "a still picture with no buttons, waiting", "player": p}
            if cell.get("cmd") and cell["cmd"] <= len(pg["cell_cmds"]):
                link = p.run([pg["cell_cmds"][cell["cmd"] - 1]])
                if link:
                    return self._link(p, link, through, played)
        link = p.run(pg["post"])
        if link:
            return self._link(p, link, through, played)
        if isinstance(p.domain, tuple) and pg.get("next"):
            return self._enter(p, p.domain, pg["next"], 1, through, played, True)
        return self._settled_after(p, played, "the disc stops")

    def _settled_after(self, p, played, why):
        if played:                            # what played was the answer (an outtake, then nothing)
            main = longest(played)
            return {"title": main["title"], "chapter": main["chapter"], "seconds": main["seconds"],
                    "before": [x["title"] for x in played[:played.index(main)]], "player": p, "then": why}
        return {"stopped": why, "player": p}

    def _link(self, p, link, through, played):
        """Where a link (or jump, or call) goes."""
        op = link["op"]
        if link.get("button"):
            p.sprm[8] = link["button"] << 10
        dom = p.domain
        pg = self.pgc(p)
        menu_dom = None if isinstance(dom, tuple) or dom == "fp" else dom
        if op == "nolink":
            return {"menu": (dom, p.pgcn, p.pgn), "player": p, "stays": True} if self.buttons(p) else \
                {"stopped": "nothing more to do", "player": p}
        if op in ("top_pgc",):
            return self._enter(p, dom, p.pgcn, 1, through, played, True)
        if op == "top_program":
            return self._enter(p, dom, p.pgcn, p.pgn, through, played, False)
        if op == "top_cell":
            return self._enter(p, dom, p.pgcn, p.pgn, through, played, False, cell=p.cell)
        if op in ("next_program", "prev_program"):
            n = p.pgn + (1 if op == "next_program" else -1)
            if n < 1 or n > (pg or {}).get("programs", 0):
                return self._play_out(p, pg, through, played)
            return self._enter(p, dom, p.pgcn, n, through, played, False)
        if op in ("next_cell", "prev_cell"):
            n = (p.cell or 1) + (1 if op == "next_cell" else -1)
            if n < 1 or n > len((pg or {}).get("cells", [])):
                return self._play_out(p, pg, through, played) if op == "next_cell" else \
                    self._enter(p, dom, p.pgcn, p.pgn, through, played, False)
            return self._enter(p, dom, p.pgcn, self._program_of(pg, n), through, played, False, cell=n)
        if op == "tail_pgc":                  # the page's end: its post commands
            link2 = p.run(pg["post"]) if pg else None
            if link2:
                return self._link(p, link2, through, played)
            return self._settled_after(p, played, "the disc stops")
        if op in ("next_pgc", "prev_pgc", "up_pgc"):
            n = (pg or {}).get({"next_pgc": "next", "prev_pgc": "prev", "up_pgc": "up"}[op])
            if not n:
                return {"stopped": "no %s PGC" % op[:-4], "player": p}
            return self._enter(p, dom, n, 1, through, played, True)
        if op == "resume":
            if not p.resume:
                raise Unsure("resuming a title that was never played")
            (rdom, rpgcn, rpgn) = p.resume
            p.resume = None
            return self._enter(p, rdom, rpgcn, rpgn, through, played, False)
        if op == "pgcn":
            return self._enter(p, dom, link["n"], 1, through, played, True)
        if op == "pgn":
            return self._enter(p, dom, p.pgcn, link["n"], through, played, False)
        if op == "cn":                        # a cell of this PGC: playing on from it
            return self._enter(p, dom, p.pgcn, self._program_of(pg, link["n"]), through, played, False,
                               cell=link["n"])
        if op == "pttn":                      # a chapter of the title playing
            if not isinstance(dom, tuple):
                raise Unsure("a chapter link outside a title")
            ttn = p.sprm[5]
            pgcn, pgn = self.disc.ptt[dom[1]][ttn][link["n"] - 1]
            # like LinkPGN and LinkCN, no pre commands (libdvdnav: play_PG). The Sorcerer's
            # Apprentice's film checks a flag in them, clears it and links to its chapter 1:
            # run again, they'd find it cleared and call the intro again, forever
            return self._enter(p, dom, pgcn, pgn, through, played, False)
        if op == "exit":
            return {"stopped": "the disc's program ends", "player": p}
        if op == "jump_tt":
            t = next((t for t in self.disc.titles if t["title"] == link["title"]), None)
            if not t:
                raise Unsure("title %d isn't on the disc" % link["title"])
            return self._title(p, t["vts"], t["ttn"], 1, through, played)
        if op in ("jump_vts_tt", "jump_vts_ptt"):
            vts = dom[1] if isinstance(dom, tuple) else dom
            if not isinstance(vts, int):
                raise Unsure("a title-set jump from outside one")
            return self._title(p, vts, link["ttn"], link.get("ptt", 1), through, played)
        if op in ("first_play", "vmgm_menu", "vmgm_pgcn", "vtsm"):
            if link.get("call"):              # a call: the title playing can be resumed later
                if isinstance(dom, tuple):
                    p.resume = (dom, p.pgcn, p.pgn)
            if op == "first_play":
                return self._enter(p, "fp", 0, 1, through, played, True)
            if op == "vmgm_pgcn":
                return self._enter(p, "vmgm", link["n"], 1, through, played, True)
            if op == "vmgm_menu":
                n = self.disc.menu_pgc("vmgm", MENU_KIND.get(link["menu"]))
                if not n:
                    raise Unsure("the disc has no such menu")
                return self._enter(p, "vmgm", n, 1, through, played, True)
            vts = link.get("vts") or (dom[1] if isinstance(dom, tuple) else dom if isinstance(dom, int) else None)
            if link.get("ttn") and not link.get("call"):
                p.sprm[5] = link["ttn"]
            if not isinstance(vts, int):
                raise Unsure("a title-set menu with no title set")
            n = self.disc.menu_pgc(vts, MENU_KIND.get(link["menu"]))
            if not n:
                raise Unsure("title set %d has no such menu" % vts)
            return self._enter(p, vts, n, 1, through, played, True)
        raise Unsure("a link the engine doesn't follow (%s)" % op)

    @staticmethod
    def _program_of(pg, cell):
        """The program a cell of a PGC belongs to."""
        starts = (pg or {}).get("program_cells") or [1]
        return max((i + 1 for i, c in enumerate(starts) if c <= cell), default=1)

    def _title(self, p, vts, ttn, ptt, through, played):
        chapters = self.disc.ptt.get(vts, {}).get(ttn)
        if not chapters:
            raise Unsure("title %d of title set %d isn't there" % (ttn, vts))
        pgcn, pgn = chapters[min(ptt, len(chapters)) - 1]
        p.sprm[5] = ttn
        return self._enter(p, ("tt", vts), pgcn, pgn, through, played, pgn == 1)


MENU_KIND = {2: "title", 3: "root", 4: "subpicture", 5: "audio", 6: "angle", 7: "chapter"}


def longest(played):
    """Of the short titles a press played before the disc went back to a menu, the one it was
    for: the longest (a clip, then a one-second bumper: the clip)."""
    return max(played, key=lambda x: x["seconds"] or 0)


# each link the program follows is a call deeper: a disc whose start runs through a dozen ads
# and a CallSS check (The Sorcerer's Apprentice) goes past Python's usual limit of 1000
DEEP = 20000


# menu states to explore (a page as the registers reach it): enough for Spider-Man's 55 pages of
# cast bios and features (~20,000 states, 2-3 s); 400 left most of them unexplored
MAX_MENUS = 20000


def explore(engine, max_menus=MAX_MENUS):
    """Start the disc, then press every button of every menu it can get to (each menu as the
    route there left the registers: the same page can answer differently after another route).
    Returns (where it starts, {(domain, pgc, program, button): [answers]}, the menus reached)."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, DEEP))
    try:
        return _explore(engine, max_menus)
    finally:
        sys.setrecursionlimit(limit)


def opening(engine, least=60):
    """The titles a player plays before the disc's menu, at least least seconds long (the
    warnings and logos are shorter): the previews on a player, which VLC (starting in the menu)
    never shows, and no menu button plays (Father of the Bride's 6 minutes of trailers,
    Jurassic World's preview reel)."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, DEEP))
    try:
        _, out = engine._start_played()
    except (Unsure, RecursionError):
        return []
    finally:
        sys.setrecursionlimit(limit)
    return [x["title"] for x in out.get("played", []) if x["title"] and (x["seconds"] or 0) >= least]


def _explore(engine, max_menus):
    p, out = engine.start()
    out0 = out
    found = {}
    if "menu" not in out:
        return answer(out), found, []
    # where the Menu key leads from the first page (back to it: a way past its intro animation)
    menu = engine.follow(p.copy(), {"op": "vtsm", "menu": 3, "call": False}, through=False)
    queue, seen, menus = [p], {p.key()}, [out["menu"]]
    while queue and len(seen) < max_menus:
        p = queue.pop(0)
        for b in engine.buttons(p):
            out = engine.press(p, b["n"])
            a = answer(out)
            k = (p.domain, p.pgcn, p.pgn, b["n"])
            if a not in found.setdefault(k, []):
                found[k].append(a)
            nxt = out.get("player")
            if "goes_to" in a and nxt is not None and nxt.key() not in seen:
                seen.add(nxt.key())
                queue.append(nxt)
                if a["goes_to"] not in menus:
                    menus.append(a["goes_to"])
    began = {"goes_to": menus[0]}
    if out0.get("button"):
        began["select"] = out0["button"]
    if out0.get("menu_key"):
        began["menu_key"] = True
    if menu.get("menu"):
        began["menu_goes_to"] = menu["menu"]
        if menu.get("button"):
            began["menu_select"] = menu["button"]
    return began, found, menus


def answer(out):
    """A press's outcome, plainly: {"plays": title, "chapter", "before": [...]} (a short title that
    played and then left the disc in a menu counts: an outtake), {"goes_to": (domain, pgc,
    program)}, {"stays": True}, {"stops": why}, or {"unsure": why}."""
    if "unsure" in out:
        return {"unsure": out["unsure"]}
    if "title" in out:
        a = {"plays": out["title"], "chapter": out["chapter"], "before": out.get("before", [])}
        if out.get("after"):
            a["after"] = out["after"]
        if out.get("into"):
            a["into"] = out["into"]
        return a
    if "menu" in out and out.get("played"):
        main = longest(out["played"])
        return {"plays": main["title"], "chapter": main["chapter"],
                "before": [x["title"] for x in out["played"][:out["played"].index(main)]]}
    if "menu" in out:
        if out.get("stays"):
            return {"stays": True}
        a = {"goes_to": out["menu"]}
        if out.get("button"):
            a["select"] = out["button"]         # the button highlighted there (crawl: no need to look)
            a["intro"] = round(out.get("intro") or 0, 1)   # seconds of menu before its buttons come on
        return a
    return {"stops": out.get("stopped", "")}
