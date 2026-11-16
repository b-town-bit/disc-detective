"""A menu page's text grouped as a reader sees it, the same for a DVD's and a Blu-ray's page: which lines are each
button's name (page_names). Worked out with the user (2026-11-15; the experiment and its benches were
temp/text_blocks.py, temp/bench_blocks.py, temp/bench_blocks_bd.py: Blu-ray 90 -> 113 of 121 buttons, DVD 198 -> 199
of 284 labelled, none worse but a colon kept).

Measured off the lettering's own pixels, not OCR's padded boxes (ink): each line's real edges, its letters' width,
its slope (RapidOCR's corners) straightened.

  across: a gap of SPLIT_LETTERS letters' width or more splits a line OCR read whole, its parts in one button's box
          joined again (rejoin); a change of lettering colour inside a line is cut too, only so each piece is
          measured on its own -- the pieces stay one title (inline text: descriptions start on their own line)
  down:   lines mostly in line (INLINE of the shorter's width) join when the empty rows between are fewer than a line
          height (JOIN_BELOW), or up to BOX_JOIN line heights in one button's small box; a measurably bigger gap ends
          the block
  boxes:  a stacked column's first and last boxes overflow (the first over the heading, the last over the page's
          foot): trimmed to their nearest text block (trim_overflow)
  roles:  by style (size three ways, colour two ways, capitals): the page's item style, headings over items, a colon
          tying its line to the one item after it, descriptions under items, a play-all alone a name of its own; one
          block per button (the first whole one); siblings' names start alike in their boxes (placement)."""
import re

from . import menus

SPLIT_LETTERS = 2.0         # letters' width: a gap this wide or wider inside a line splits it
JOIN_BELOW = 1.0            # empty rows under a line, in line heights: fewer, the same text's next line
INLINE = 0.5                # share of the shorter line's ink within the other's span: one above the other in line
BOX_LINES = 3               # a box holding this many lines or fewer holds a name, not a section
BOX_JOIN = 1.4              # empty rows up to this many line heights: one text, when one such box holds both
TILT_MIN = 3                # pixels a line rises across: tilted (with TILT_SLOPE)
TILT_SLOPE = 0.026          # rows per column: tilted (1.5 degrees)
SIZE_HINT = 0.15            # lettering this much smaller by two of its three measures: another style
SATURATION_APART = 0.12     # as tinted give or take this: the same colour (gold isn't grey)
DARK = 70                   # lettering this dark in every channel: near black, any two alike
SENTENCE_WORDS = 7          # words: reads as a description
COLUMN_MIN = 3              # buttons stacked: a column, for placement's vote
PLACE_ACROSS = 0.08         # siblings' names starting within this share of the box across: agreed
PLACE_DOWN = 0.12           # ... down, as a share of the box
PLACE_DOWN_LINES = 0.75     # ... down, in line heights (a box of another height)
DOWN_TALLER = 0.25          # a box this much taller or shorter than its siblings': measured down in line heights
TRIM_MIN = 2                # buttons stacked: a column whose first and last boxes are trimmed
HEADING_ALONE = 1.25        # a lone line at a last box's top, lettering this much taller: a heading, passed over

CONJUNCTIONS = (r"and|or|nor|but|of|the|a|an|with|for|to|from|in|on|at|by|about|after|around|as|before|behind|"
                r"between|during|into|like|over|through|under|until|upon|within|without|&")


def _mid_in(ik, r):
    return r[0] <= (ik["left"] + ik["right"]) / 2 <= r[2] and r[1] <= (ik["top"] + ik["bottom"]) / 2 <= r[3]


def _flat(image):
    return list((getattr(image, "get_flattened_data", None) or image.getdata)())   # (Pillow 12.1 renamed it)


def otsu(values):
    """The grey level best splitting values (0-255) in two (Otsu's)."""
    hist = [0] * 256
    for v in values:
        hist[v] += 1
    total, sum_all = len(values), sum(i * h for i, h in enumerate(hist))
    best, cut, w0, sum0 = -1, 128, 0, 0
    for t in range(256):
        w0 += hist[t]
        if w0 == 0 or w0 == total:
            continue
        sum0 += t * hist[t]
        m0, m1 = sum0 / w0, (sum_all - sum0) / (total - w0)
        between = w0 * (total - w0) * (m0 - m1) ** 2
        if between > best:
            best, cut = between, t
    return cut


def ink(gray, box, text):
    """A line's lettering in its OCR box: {top, bottom, left, right, h, core, letter, gaps, ...}, page pixels; None
    if none found. The box binarised by its own Otsu threshold, the lettering the fewer pixels; its rows: the run of
    rows with ink nearest the box's middle (a neighbour's lettering dipping into a padded box left out)."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(gray.width, x1), min(gray.height, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    px = _flat(gray.crop((x0, y0, x1, y1)))
    cut = otsu(px)
    dark = sum(1 for v in px if v <= cut)
    lit = (lambda v: v > cut) if dark >= len(px) / 2 else (lambda v: v <= cut)
    w, h = x1 - x0, y1 - y0
    on = [[lit(px[r * w + c]) for c in range(w)] for r in range(h)]
    rows = [sum(row) for row in on]
    need = max(2, 0.08 * max(rows))
    runs, start, quiet = [], None, 0
    for r, n in enumerate(rows + [0, 0, 0]):
        if r < h and n >= need:
            start = r if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet > 2:
                runs.append((start, r - quiet))
                start, quiet = None, 0
    if not runs:
        return None
    mid = h / 2
    top, bot = min(runs, key=lambda ab: 0 if ab[0] <= mid <= ab[1] else min(abs(ab[0] - mid), abs(ab[1] - mid)))
    cols = [sum(on[r][c] for r in range(top, bot + 1)) for c in range(w)]
    lit_cols = [c for c, n in enumerate(cols) if n]
    if not lit_cols:
        return None
    left, right = lit_cols[0], lit_cols[-1]
    gaps, run = [], 0                   # (empty column runs inside the ink: (where it starts, its width))
    for c in range(left, right + 1):
        if cols[c] == 0:
            run += 1
        elif run:
            gaps.append((x0 + c - run, run))
            run = 0
    hgt = bot - top + 1
    letters = max(1, len(re.sub(r"\s", "", text)))
    # (a letter's width: the ink's, less the gaps between words (a quarter of the lettering's height or more), over
    # the letters read; its size: from its top to its baseline, the last row with a third of the fullest row's ink)
    letter = max(1.0, (right - left + 1 - sum(g for _, g in gaps if g >= 0.25 * hgt)) / letters)
    full = max(rows[top:bot + 1])
    dense = [r for r in range(top, bot + 1) if rows[r] >= full / 3]
    core = dense[-1] - top + 1 if dense else hgt
    return {"top": y0 + top, "bottom": y0 + bot, "left": x0 + left, "right": x0 + right, "h": hgt, "core": core,
            "letter": letter, "gaps": gaps, "k": 0.0, "cy": y0 + (top + bot) / 2, "cx": x0 + (left + right) / 2,
            "straight": hgt}


def tilt(ik, k):
    """A line at slope k (rows per column, the OCR detector's): straightened when it slopes TILT_SLOPE or more and
    rises TILT_MIN pixels across -- its height less the rise, its size in proportion; a level one as it is."""
    rise = k * (ik["right"] - ik["left"])
    if abs(k) < TILT_SLOPE or abs(rise) < TILT_MIN:
        return ik
    straight = max(1, ik["h"] - int(round(abs(rise))))
    return dict(ik, k=k, straight=straight, h=straight, core=max(1, int(round(ik["core"] * straight / ik["h"]))))


def band(ik, x):
    """A line's straightened lettering at column x: (top, bottom), page pixels."""
    k = ik.get("k", 0.0)
    if not k:
        return ik["top"], ik["bottom"]
    mid = ik["cy"] + k * (x - ik["cx"])
    return mid - ik["straight"] / 2, mid + ik["straight"] / 2


def mid_top(ik):
    """A line's top at its middle (its height on the page, along its slope)."""
    return band(ik, (ik["left"] + ik["right"]) / 2)[0]


def column_gap(a, b):
    """The empty rows between a line and the one under it, both straightened, where they overlap across."""
    x = (max(a["left"], b["left"]) + min(a["right"], b["right"])) / 2
    return int(round(band(b, x)[0] - band(a, x)[1] - 1))


def pieces(ik):
    """A line's ink in words: [(left, right)], cut at gaps of a quarter of its height or more."""
    out, x = [], ik["left"]
    for gx, gw in ik["gaps"]:
        if gw >= 0.25 * ik["h"]:
            out.append((x, gx - 1))
            x = gx + gw
    out.append((x, ik["right"]))
    return out


def split_line(text, ik, cuts=None):
    """A line OCR read across two groups: its parts [(text, ink)], at gaps of SPLIT_LETTERS letters' width or more
    (or at cuts). Its words go to the parts by place where its word gaps can be told (OCR's spaces are its widest
    ink gaps), else shared out by their letters against each part's ink width."""
    if cuts is None:
        cuts = [(x, w) for x, w in ik["gaps"] if w >= SPLIT_LETTERS * ik["letter"]]
    words = text.split()
    if not cuts or len(words) < 2:
        return [(text, ik)]
    edges, x = [], ik["left"]
    for gx, gw in cuts:
        edges.append((x, gx - 1))
        x = gx + gw
    edges.append((x, ik["right"]))
    part = lambda t, a, b: (" ".join(t), dict(ik, left=a, right=b, gaps=[g for g in ik["gaps"] if a <= g[0] <= b],
                                                 split_from=text))
    spaces = sorted(sorted(ik["gaps"], key=lambda g: -g[1])[:len(words) - 1])
    if len(spaces) == len(words) - 1 and all(c in spaces for c in cuts):
        parts, k = [], 0
        for i, (a, b) in enumerate(edges):
            n = len(words) if i == len(edges) - 1 else spaces.index(cuts[i]) + 1
            if words[k:n]:
                parts.append(part(words[k:n], a, b))
            k = n
        if len(parts) > 1:
            return parts
    widths = [b - a + 1 for a, b in edges]
    total_w, total_l = sum(widths), sum(len(w) for w in words)
    parts, k = [], 0
    for i, (a, b) in enumerate(edges):
        if i == len(edges) - 1:
            take = words[k:]
        else:
            want = sum(widths[:i + 1]) / total_w * total_l
            best, n = None, k
            for j in range(k + 1, len(words) - (len(edges) - i - 1) + 1):
                d = abs(sum(len(w) for w in words[:j]) - want)
                if best is None or d < best:
                    best, n = d, j
            take, k = words[k:n], n
        if take:
            parts.append(part(take, a, b))
    return parts if len(parts) > 1 else [(text, ik)]


def background(image, ik, gap=2, width=3):
    """The colour behind a line: the middle colour of a thin ring just outside its box (OCR boxes hug the letters, so
    the box's own middle colour can be the lettering's: the Muppets' outlined capitals, fill or outline by turns)."""
    W, H = image.size
    l, t, r, b = int(ik["left"]), int(ik["top"]), int(ik["right"]), int(ik["bottom"])
    px = []
    for x0, y0, x1, y1 in ((l - gap - width, t - gap - width, r + gap + width, t - gap),
                           (l - gap - width, b + gap, r + gap + width, b + gap + width),
                           (l - gap - width, t - gap, l - gap, b + gap),
                           (r + gap, t - gap, r + gap + width, b + gap)):
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if x1 > x0 and y1 > y0:
            px.extend(_flat(image.crop((x0, y0, x1, y1))))
    if not px:
        return None
    return tuple(sorted(c[i] for c in px)[len(px) // 2] for i in range(3))


def ring_colour(image, ik):
    """A line's lettering colour against what's behind it: the fifth of its box's pixels furthest from its background
    (background), averaged. An outline lies between its fill and the backdrop, so the fill is read, every line alike."""
    box = (ik["left"], ik["top"], ik["right"] + 1, ik["bottom"] + 1)
    bg = background(image, ik)
    if bg is None:
        return menus.lettering_colour(image, box)
    px = _flat(image.crop(box))
    if not px:
        return None
    far = sorted(px, key=lambda c: -sum(abs(c[i] - bg[i]) for i in range(3)))[:max(1, len(px) // 5)]
    return tuple(sum(c[i] for c in far) / len(far) for i in range(3))


def style(image, text, ik):
    """A line's lettering style: its size three ways (capitals to baseline, a letter's width, OCR's box height), its
    colour two ways (menus.lettering_colour over its box; against its ring, ring_colour), all capitals or not."""
    letters = re.findall(r"[A-Za-z]", text)
    caps = len(letters) >= 3 and all(c.isupper() for c in letters)
    box = (ik["left"], ik["top"], ik["right"] + 1, ik["bottom"] + 1)
    return {"size": ik["core"], "width": ik["letter"], "box": ik.get("box_h") or ik["h"],
            "colour": menus.lettering_colour(image, box), "ring": ring_colour(image, ik),
            "caps": caps if len(letters) >= 3 else None}


def saturation(c):
    return (max(c) - min(c)) / max(max(c), 1)


def same_colour(a, b):
    """The same lettering colour: close in every channel, both near black, or the same mix (menus.same_lettering)
    and as tinted (Order of the Phoenix's gold "Trailing Tonks" over its grey description)."""
    if max(abs(x - y) for x, y in zip(a, b)) <= 25:
        return True
    if max(max(a), max(b)) <= DARK:
        return True
    return menus.same_lettering(a, b) and abs(saturation(a) - saturation(b)) <= SATURATION_APART


def colours_match(a, b):
    """Two lines' lettering one colour by either reading: against its ring (outlined lettering: the Muppets' cream
    titles, fill or outline by turns in their own boxes) or its own box (lettering whose outline is darker than a
    backdrop lighter than its fill: Great Mouse Detective's "Theatrical / Trailer*")."""
    if a.get("ring") and b.get("ring") and same_colour(a["ring"], b["ring"]):
        return True
    return a["colour"] is None or b["colour"] is None or same_colour(a["colour"], b["colour"])


def smaller(a, b):
    """Lettering a smaller than b: two of its three size measures SIZE_HINT or more smaller."""
    return sum(1 for k in ("size", "width", "box") if a[k] < (1 - SIZE_HINT) * b[k]) >= 2


def same_style(a, b):
    """Soft signs adding up: another colour is another style; so is lettering smaller (or bigger) by two of its
    three size measures; one measure only with capitals changing too (one alone is often the ink's error over a
    busy backdrop: Enchanted's songs 32, 18, 24)."""
    if not colours_match(a, b):
        return False
    ratio = lambda k: (min(a[k], b[k]) / max(a[k], b[k], 1e-6), a[k] < b[k])
    small = [ratio(k) for k in ("size", "width", "box")]
    for less in (True, False):
        if sum(1 for r, s in small if r < 1 - SIZE_HINT and s == less) >= 2:
            return False
    caps_differ = a["caps"] is not None and b["caps"] is not None and a["caps"] != b["caps"]
    return not (sum(1 for r, _ in small if r < 1 - SIZE_HINT) >= 1 and caps_differ)


def carries_on(above, below):
    """A sign the text carries on from one line to the next: the first ends in a conjunction, a preposition, an
    article or a comma ("Professor Lupin and / Sirius Black"), or the second starts with one in lowercase."""
    end = re.search(r"(?:\b(?:%s)|,)\s*$" % CONJUNCTIONS, above.strip(), re.I)
    start = re.match(r"\s*(?:%s)\b" % CONJUNCTIONS.replace("|&", ""), below)
    return bool(end or (start and below.strip()[:1].islower()))


def play_all_alone(text):
    """A line that is a play-all and nothing else (menus.PLAY_ALL_RE, OCR's variants: "Play Aly", "PLAY aul")."""
    m = menus.PLAY_ALL_RE.search(text)
    return bool(m) and not re.sub(r"[\W_]+", "", text[:m.start()] + text[m.end():])


def sentence_like(texts):
    """Lines that read as a description: a full stop at the end, SENTENCE_WORDS words or more, or two lines or more
    -- lines joined by a colon or a word that carries on counted as one ("Harry Potter: / The Magic of Editing")."""
    words = " ".join(texts).split()
    logical = 1 + sum(1 for a, b in zip(texts, texts[1:]) if not (a.rstrip().endswith(":") or carries_on(a, b)))
    return logical >= 2 or len(words) >= SENTENCE_WORDS or bool(re.search(r"[.!?]['\")]?$", " ".join(texts).strip()))


def one_button_title(g, st, lines):
    """In one button's block, the line whose style is its title's: of the block's runs of one style, top down, the
    first short one (not sentence_like) after which everything reads as description. "BEHIND THE STORY / Capturing
    the Stone / Producer David Heyman...": Capturing the Stone; "TRAILERS / Teaser Trailer": Teaser Trailer."""
    runs = []
    for i in g:
        last = runs[-1][-1] if runs else None
        if runs and (same_style(st[runs[-1][0]], st[i]) or (carries_on(lines[last][0], lines[i][0])
                                                            and colours_match(st[last], st[i]))):
            runs[-1].append(i)
        else:
            runs.append([i])
    said = [sentence_like([lines[i][0] for i in r]) for r in runs]
    for k, r in enumerate(runs):
        if not said[k] and all(said[k + 1:]):
            return r[0]
    return next((r[0] for k, r in enumerate(runs) if not said[k]), runs[0][0])


def aligned(a, b):
    """Two lines one above the other in line: INLINE of the shorter one's ink within the other's span (an indented
    second line counts: Friends' hanging indents; so do left, centred and right ones). (in line?, the share)."""
    over = min(a["right"], b["right"]) - max(a["left"], b["left"]) + 1
    short = min(a["right"] - a["left"], b["right"] - b["left"]) + 1
    share = over / short if short > 0 else 0
    return share >= INLINE, share


def blocks(lines, rects=None, trimmed=None):
    """[(text, ink)] -> its text blocks, [[index]]. Each line joins every line below it that's mostly in line with it
    and fewer than a line height (JOIN_BELOW) of empty rows down -- the nearest, and others only where they overlap
    its rows ("The Ghost and / Mr. Chicken", side by side under it); up to BOX_JOIN line heights when one button's
    small box (BOX_LINES lines or fewer, the disc's or trimmed) holds both: the Muppets' wrapped names."""
    order = sorted(range(len(lines)), key=lambda i: lines[i][1]["top"])
    parent = list(range(len(lines)))

    def owners(rs):
        of = {i: next((n for n, r in (rs or {}).items() if _mid_in(lines[i][1], r)), None) for i in range(len(lines))}
        count = {}
        for n in of.values():
            if n is not None:
                count[n] = count.get(n, 0) + 1
        return of, count
    box_of, held = owners(rects)
    tbox_of, theld = owners(trimmed) if trimmed else (box_of, held)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for k, i in enumerate(order):
        a = lines[i][1]
        below = []
        for j in order[k + 1:]:
            b = lines[j][1]
            if b["top"] <= a["top"] or not aligned(a, b)[0]:
                continue
            below.append((column_gap(a, b), j))
        if not below:
            continue
        nearest = min(g for g, _ in below)
        # (one nearest: on a tie, the one most in line with it -- a heading over two columns has a line under it in
        # each, Sorcerer's Stone's ADDITIONAL FOOTAGE over "Deleted Scenes" and "Scene 4")
        first = max((x for x in below if x[0] == nearest), key=lambda x: aligned(a, lines[x[1]][1])[1])[1]
        for gap, j in below:
            b = lines[j][1]
            ratio = gap / max(a["h"], b["h"])
            same_box = (box_of[i] is not None and box_of[i] == box_of[j] and held[box_of[i]] <= BOX_LINES) or \
                (tbox_of[i] is not None and tbox_of[i] == tbox_of[j] and theld[tbox_of[i]] <= BOX_LINES)
            joined = ratio < JOIN_BELOW or (same_box and ratio < BOX_JOIN and gap <= nearest)
            joined = joined and (j == first or gap <= -0.2 * min(a["h"], b["h"]))
            if joined:
                parent[find(j)] = find(i)
    groups = {}
    for i in order:
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def rejoin(lines, rects):
    """A line split in parts whose parts sit in one button's box: whole again (one button, one name: "PLAY | ALL" on
    Chuck's letter-spaced capitals). Parts in different boxes, or outside any, and colour cuts, stay apart."""
    owner = lambda ik: next((n for n, r in rects.items() if _mid_in(ik, r)), None)
    out, k = [], 0
    while k < len(lines):
        text, ik = lines[k]
        src = ik.get("split_from")
        if not src:
            out.append(lines[k])
            k += 1
            continue
        run = [lines[k]]
        while k + len(run) < len(lines) and lines[k + len(run)][1].get("split_from") == src:
            run.append(lines[k + len(run)])
        k += len(run)
        piece = [run[0]]
        for part in run[1:]:
            if owner(part[1]) is not None and owner(part[1]) == owner(piece[-1][1]) and not part[1].get("style_cut"):
                piece.append(part)
            else:
                out.append(_whole(piece, src))
                piece = [part]
        out.append(_whole(piece, src))
    return out


def _whole(piece, src):
    if len(piece) == 1:
        return piece[0]
    ik = dict(piece[0][1], right=piece[-1][1]["right"], gaps=[g for p in piece for g in p[1]["gaps"]])
    if " ".join(t for t, _ in piece) == src:
        ik.pop("split_from", None)
    return " ".join(t for t, _ in piece), ik


def _stacked(rects, n, r):
    """Button n's column: the boxes with its left and right edges, top down, each starting about where the one above
    ends (from the top one), [(button, box)], and every box with those edges."""
    w = r[2] - r[0]
    col = sorted(((m, q) for m, q in rects.items() if abs(q[0] - r[0]) <= 0.05 * w and abs(q[2] - r[2]) <= 0.05 * w),
                 key=lambda kv: kv[1][1])
    run = [col[0]]
    for m, q in col[1:]:
        if abs(q[1] - run[-1][1][3]) <= 0.25 * max(1, min(q[3] - q[1], run[-1][1][3] - run[-1][1][1])):
            run.append((m, q))
        else:
            break
    return run, col


def columns(rects):
    """({button: its stacked column of COLUMN_MIN or more, top down}, {the columns' first and last buttons})."""
    member, ends, seen = {}, set(), set()
    for n, r in sorted(rects.items(), key=lambda kv: (kv[1][0], kv[1][1])):
        if n in seen:
            continue
        run, col = _stacked(rects, n, r)
        seen.update(m for m, _ in col)
        if len(run) >= COLUMN_MIN:
            ends.update((run[0][0], run[-1][0]))
            for m, _ in run:
                member[m] = [x for x, _ in run]
    return member, ends


def trim_overflow(rects, lines, groups):
    """The user's observation: in a column of buttons, the middle ones fit their items, while the first can reach up
    over the heading and the last down over the page's foot (Sorcerer's Stone's Play All box over ADDITIONAL
    FOOTAGE, its Scene 3 box over ONLINE). In a column of TRIM_MIN or more, the first box is trimmed to the top of the
    text block nearest its bottom edge, the last to the bottom of the one nearest its top (nearest_block).
    {n: box} -> {n: box}."""
    out = dict(rects)
    seen = set()
    for n, r in sorted(rects.items(), key=lambda kv: (kv[1][0], kv[1][1])):
        if n in seen:
            continue
        run, col = _stacked(rects, n, r)
        seen.update(m for m, _ in col)
        if len(run) < TRIM_MIN or not lines or not groups:
            continue
        (m0, q0), (m1, q1) = run[0], run[-1]
        out[m0] = (q0[0], nearest_block(q0, lines, groups, from_bottom=True), q0[2], q0[3])
        out[m1] = (q1[0], q1[1], q1[2], nearest_block(q1, lines, groups, from_bottom=False))
    return out


def nearest_block(box, lines, groups, from_bottom):
    """The edge a first box (from_bottom) or a last box keeps: the top of the text block nearest its bottom edge, or
    the bottom of the one nearest its top edge, of the blocks with a line in the box; a heading alone in its block at
    a last box's top (one line, HEADING_ALONE taller than the box's others) passed over -- Goblet of Fire's
    "TRAILERS" over "THEATRICAL TRAILER", ONLINE below. The box's own edge when no block is in it."""
    mine = [g for g in ([i for i in g if _mid_in(lines[i][1], box)] for g in groups) if g]
    if not mine:
        return box[1] if from_bottom else box[3]
    if from_bottom:
        g = max(mine, key=lambda g: max(lines[i][1]["bottom"] for i in g))
        return max(box[1], min(lines[i][1]["top"] for i in g) - 1)
    mine.sort(key=lambda g: min(lines[i][1]["top"] for i in g))
    if len(mine) > 1 and len(mine[0]) == 1:
        tall = lambda i: lines[i][1].get("box_h") or lines[i][1]["h"]
        rest = sorted(tall(i) for g in mine[1:] for i in g)
        if tall(mine[0][0]) >= HEADING_ALONE * rest[len(rest) // 2]:
            mine = mine[1:]
    return min(box[3], max(lines[i][1]["bottom"] for i in mine[0]) + 1)


def stroke_colours(image, ik, bits):
    """Each word's lettering colour from its strokes alone: the pixels the line's own threshold marks as lettering
    (the fewer), the half of them furthest from it, averaged. [(r, g, b) or None] for bits [(left, right)]."""
    box = (ik["left"], ik["top"], ik["right"] + 1, ik["bottom"] + 1)
    rgb = image.crop(box)
    px = _flat(rgb)
    gray = [round(0.299 * r + 0.587 * g + 0.114 * b) for r, g, b in px]
    cut = otsu(gray)
    light = sum(1 for v in gray if v > cut) <= len(gray) / 2
    w = rgb.width
    out = []
    for left, right in bits:
        mine = [(gray[y * w + x], px[y * w + x]) for y in range(rgb.height) for x in range(left - box[0], right - box[0] + 1)
                if 0 <= x < w and ((gray[y * w + x] > cut) if light else (gray[y * w + x] <= cut))]
        if len(mine) < 4:
            out.append(None)
            continue
        mine.sort(key=lambda v: -v[0] if light else v[0])
        core = [c for _, c in mine[:max(2, len(mine) // 2)]]
        out.append(tuple(sum(c[i] for c in core) / len(core) for i in range(3)))
    return out


def colour_split(image, text, ik):
    """A line whose words change lettering colour part-way and keep to it ("Year 1 | Harry Potter and the Sorcerer's
    Stone", cream then grey): cut there so each piece's style is measured on its own (a blended line would blur the
    page's vote for its item style), the pieces one title again in the name (inline_whole: the user's, a description
    always starts on a line of its own)."""
    bits = pieces(ik)
    if len(bits) < 2:
        return [(text, ik)]
    cols = stroke_colours(image, ik, bits)
    if any(c is None for c in cols):
        return [(text, ik)]
    for k in range(1, len(bits)):
        left, right = cols[:k], cols[k:]
        if all(same_colour(left[0], c) for c in left) and all(same_colour(right[0], c) for c in right) \
                and not same_colour(left[-1], right[0]):
            gap = (bits[k - 1][1] + 1, bits[k][0] - bits[k - 1][1] - 1)
            parts = split_line(text, dict(ik, gaps=[g for g in ik["gaps"] if g != gap] + [gap]), cuts=[gap])
            if len(parts) != 2:
                return [(text, ik)]
            token = (ik["left"], ik["top"], text)
            return [(parts[0][0], dict(parts[0][1], inline=token)),
                    (parts[1][0], dict(parts[1][1], style_cut=True, inline=token))]
    return [(text, ik)]


def slopes(lines, corners):
    """Each line's slope (rows per column) by RapidOCR's four corners for it, its top and bottom edges averaged;
    None where it has none (lines read another time). corners: [(text, ((x, y) x4), sure)], menus.rapid_lines'."""
    slope = lambda pts: (((pts[1][1] - pts[0][1]) / max(pts[1][0] - pts[0][0], 1e-6)) +
                         ((pts[2][1] - pts[3][1]) / max(pts[2][0] - pts[3][0], 1e-6))) / 2
    out = []
    for k, (t, b, _) in enumerate(lines):
        hit = corners[k] if k < len(corners) and corners[k][0] == t else next(
            (c for c in corners if c[0] == t and abs(min(x for x, _ in c[1]) - b[0]) < 6
             and abs(min(y for _, y in c[1]) - b[1]) < 6), None)
        out.append(slope(hit[1]) if hit else None)
    return out


def measure(image, lines, corners=None):
    """A page's lines [(text, box, sure)] measured off their ink, straightened, split where they hold two groups
    (split_line) and where their colour changes (colour_split): [(text, ink)], each ink's "src" its line's index."""
    gray = image.convert("L")
    rgb = image.convert("RGB")
    ks = slopes(lines, corners) if corners else [None] * len(lines)
    got = []
    for k, (t, b, _) in enumerate(lines):
        ik = ink(gray, b, t)
        if not ik:
            continue
        if ks[k] is not None:
            ik = tilt(ik, ks[k])
        ik["src"] = k
        ik["box_h"] = b[3] - b[1]
        for part in split_line(t, ik):
            got += colour_split(rgb, *part)
    return got


def inline_whole(lines, names):
    """A name holding a piece of a line cut by colour (colour_split) gets the line's other pieces too, in reading
    order: inline text is one title."""
    pieces_of = {}
    for i, (_, ik) in enumerate(lines):
        if ik.get("inline"):
            pieces_of.setdefault(ik["inline"], []).append(i)
    for v in names.values():
        idx = list(v.get("idx") or [])
        add = [j for i in idx if lines[i][1].get("inline") for j in pieces_of[lines[i][1]["inline"]] if j not in idx]
        if add:
            idx = sorted(set(idx + add))
            v.update(name=" ".join(lines[i][0] for i in idx), lines=[lines[i] for i in idx], idx=idx)


def classify(image, lines, groups, rects, disc_rects=None):
    """Each button's name, description and heading from the blocks, by style (the user's rules, 2026-11-15):
      - the item style: the one most buttons have a line in;
      - a line in another style followed by items in their style: the heading of all of them, till the next one;
      - ending in a colon: the heading of the one item after it only (kept in the name: the namer may promote it);
      - a line in the items' style no box holds: the next item's, else the one before's;
      - a line in another style after an item, smaller, or no items after it: that item's description;
      - a line over a play-all in its box, more items after: a sub-heading (the play-all plays what it names);
      - a play-all on its own line: a name by itself;
      - one block per button: of those in the item style, the first wholly in its box, else the most in it.
    rects: the buttons' boxes, trimmed of overflow; disc_rects: as the disc has them (placement's).
    Returns ({button: {"name", "lines", "idx", "description", "heading"}}, the text no button holds)."""
    owner = {i: next((n for n, r in rects.items() if _mid_in(lines[i][1], r)), None) for i in range(len(lines))}
    st = {i: style(image, lines[i][0], lines[i][1]) for i in range(len(lines))}
    owned_all = [i for i in range(len(lines)) if owner[i] is not None]
    firsts = [min((i for i in owned_all if owner[i] == n), key=lambda i: lines[i][1]["top"])
              for n in {owner[i] for i in owned_all}]
    # (the item style: the one most buttons have a line in -- a heading in one button's box loses, Enchanted's "Comes
    # To Life"; tied (Goblet: every button a title and a description), the one first within its buttons)
    buttons_with = lambda a: len({owner[j] for j in owned_all if same_style(a, st[j])})
    first_in = lambda a: sum(1 for j in firsts if same_style(a, st[j]))
    page_ref = max((st[i] for i in owned_all), key=lambda a: (buttons_with(a), first_in(a)), default=None)
    per_button, headings, rest = {}, [], []
    for gi, g in enumerate(groups):
        g = sorted(g, key=lambda i: (lines[i][1]["top"], lines[i][1]["left"]))
        owned = [i for i in g if owner[i] is not None]
        if not owned:
            rest.append(" / ".join(lines[i][0] for i in g))
            continue
        if len({owner[i] for i in owned}) == 1:
            ref = st[one_button_title(g, st, lines)]
        elif page_ref is not None and any(same_style(st[i], page_ref) for i in owned):
            ref = page_ref
        else:
            own_firsts = [i for i in firsts if i in owned] or owned
            ref = max((st[i] for i in own_firsts), key=lambda a: sum(1 for j in own_firsts if same_style(a, st[j])))
        item = {i: same_style(st[i], ref) for i in g}
        # (a button with no line in the item style -- the selected one drawn brighter in a saved frame: Enchanted's
        # "Happy Working Song" -- its lines not in the style of text no button holds; none left, all its lines)
        free = [j for j in range(len(lines)) if owner[j] is None]
        for n in {owner[i] for i in owned}:
            mine = [i for i in owned if owner[i] == n]
            if not any(item[i] for i in mine):
                for i in [i for i in mine if not any(same_style(st[i], st[j]) for j in free)] or mine:
                    item[i] = True
        roles, heading, colon, bound = {}, [], [], None
        for pos, i in enumerate(g):
            after = g[pos + 1:]
            if bound is not None:       # (the line after an item's colon: that item's, whatever its style)
                roles[bound]["name"].append(i)
                bound = None
                continue
            prev = g[pos - 1] if pos else None
            if prev is not None and item[prev] and not item[i] and carries_on(lines[prev][0], lines[i][0]) \
                    and colours_match(st[prev], st[i]):
                item[i] = True          # (the text carries on into it, in the item's colour)
            nxt = after[0] if after else None
            if item[i] and owner[i] is not None and nxt is not None and item[nxt] and owner[nxt] == owner[i] \
                    and menus.PLAY_ALL_RE.search(lines[nxt][0]) and not menus.PLAY_ALL_RE.search(lines[i][0]) \
                    and not lines[i][0].rstrip().endswith(":") and not carries_on(lines[i][0], lines[nxt][0]) \
                    and any(item[j] and owner[j] not in (None, owner[i]) for j in after):
                # (a sub-heading in the items' own lettering, over a play-all in its box, more items after:
                # Sorcerer's Stone's "Deleted Scenes / Play All", then Scene 1...)
                heading = [i]
                headings.append(i)
                rest.append(lines[i][0])
                continue
            if item[i]:
                n = owner[i]
                if n is None and play_all_alone(lines[i][0]):
                    rest.append(lines[i][0])
                    continue
                if n is None:           # (no box holds it: the next item's, else the one before's)
                    n = next((owner[j] for j in after if item[j] and owner[j] is not None), None)
                    if n is None:
                        n = next((owner[j] for j in reversed(g[:pos]) if item[j] and owner[j] is not None), None)
                if n is None:
                    rest.append(lines[i][0])
                    continue
                roles.setdefault(n, {"name": [], "description": [], "heading": list(heading), "colon": list(colon)})
                roles[n]["name"].append(i)
                colon = []
                if lines[i][0].rstrip().endswith(":") and after:
                    bound = n           # (a colon: it goes with the one line after it)
                continue
            later_items = [j for j in after if item[j]]
            prev_item = next((owner[j] for j in reversed(g[:pos]) if item[j] and owner[j] is not None), None)
            if lines[i][0].rstrip().endswith(":") and later_items:
                colon.append(i)
            elif prev_item is not None and prev_item in roles and smaller(st[i], ref):
                roles[prev_item]["description"].append(i)
            elif later_items and (not roles or len({owner[j] for j in later_items if owner[j] is not None}) >= 1
                                  and not any(item[j] for j in g[:pos]) or len(later_items) >= 2):
                heading = [i]
                headings.append(i)
                rest.append(lines[i][0])
            elif prev_item is not None and prev_item in roles:
                roles[prev_item]["description"].append(i)
            else:
                rest.append(lines[i][0])
        for n, r in roles.items():
            per_button.setdefault(n, []).append((gi, r))
    for n in {owner[i] for i in owned_all}:          # (a button none of whose lines came out a name: its own lines)
        if not any(r["name"] for _, r in per_button.get(n, [])):
            mine = sorted((i for i in owned_all if owner[i] == n), key=lambda i: lines[i][1]["top"])
            per_button[n] = [(None, {"name": mine, "description": [], "heading": [], "colon": []})]
    names = {}
    for n, found in per_button.items():
        found = [x for x in found if x[1]["name"]] or found
        tall = lambda r: max((st[i]["size"] for i in r["name"]), default=0)
        matches_page = lambda r: page_ref is not None and any(same_style(st[i], page_ref) for i in r["name"])
        near_items = lambda r: abs(tall(r) - page_ref["size"]) if page_ref is not None else -tall(r)
        inside = lambda gi: (sum(1 for i in groups[gi] if owner[i] == n) / len(groups[gi])) if gi is not None else 1.0
        first = lambda r: min(r["heading"] + r["colon"] + r["name"] + r["description"], default=len(lines))
        # (one block per button, the user's: in the item style first (a heading alone in its block isn't a name);
        # wholly in its box, the first in reading order -- Goblet's b13, "THEATRICAL TRAILER" over the bigger
        # "Visit us at ..." block; partly, the most of it in the box; then lettering nearest the items' size)
        found.sort(key=lambda x: (not matches_page(x[1]), inside(x[0]) < 1.0,
                                  first(x[1]) if inside(x[0]) >= 1.0 else -inside(x[0]), near_items(x[1])))
        main = found[0][1]
        for _, r in found[1:]:
            rest.append(" / ".join(lines[i][0] for i in r["name"] + r["description"]))
        # (a play-all on its own line is a whole name by itself, as in playlists: the lines over it its heading)
        alone = [i for i in main["colon"] + main["name"] if play_all_alone(lines[i][0])]
        if alone and len(main["colon"] + main["name"]) > 1:
            over = [i for i in main["colon"] + main["name"] if lines[i][1]["top"] < lines[alone[0]][1]["top"]]
            main = dict(main, colon=[], name=[alone[0]], heading=over or main["heading"])
        idx = main["colon"] + main["name"]
        names[n] = {"name": " ".join(lines[i][0] for i in idx), "lines": [lines[i] for i in idx], "idx": idx,
                    "description": " ".join(lines[i][0] for i in main["description"]),
                    "heading": " ".join(lines[i][0] for i in main["heading"])}
    full = disc_rects or rects
    disc_owner = {i: next((n for n, r in full.items() if _mid_in(lines[i][1], r)), None) for i in range(len(lines))}
    placement(lines, full, disc_owner, names, {i: gi for gi, g in enumerate(groups) for i in g}, st, rects)
    inline_whole(lines, names)
    return names, rest


def placement(lines, rects, owner, names, group_of, st, trimmed=None):
    """A ranked sign from the page's layout: buttons laid out alike start their names in the same place in their
    boxes. Siblings: the button's stacked column (voted by its middle buttons), else buttons about as wide. Where
    their names start (left edge or middle across; top down, as a share of the box, or in line heights for a box of
    another height) counts on an axis where three in five agree. A button whose name doesn't start there, nor sits
    there by its middle or bottom (wholly in its box), while another of its lines in its siblings' lettering does:
    its name starts at that line -- unless a line it would drop ends in a colon or carries on into the next
    (DA502D7E's "Theatrical Trailer", not the description beside it; In Society's BONUS heading dropped)."""
    line_h = sorted(ik.get("box_h") or ik["h"] for _, ik in lines)[len(lines) // 2] if lines else 1

    def place(i, r):
        """(left across, middle across, down as a share of the box, down in line heights)."""
        ik = lines[i][1]
        w, h = max(r[2] - r[0], 1), max(r[3] - r[1], 1)
        top = mid_top(ik)
        return ((ik["left"] - r[0]) / w, ((ik["left"] + ik["right"]) / 2 - r[0]) / w, (top - r[1]) / h,
                (top - r[1]) / max(line_h, 1))

    def centre(idx, r):
        """[(middle down, as a share and in line heights), (bottom down, as a share and in line heights up)]."""
        top = min(lines[i][1]["top"] for i in idx)
        bottom = max(lines[i][1]["bottom"] for i in idx)
        h = max(r[3] - r[1], 1)
        return [(((top + bottom) / 2 - r[1]) / h, ((top + bottom) / 2 - r[1]) / max(line_h, 1)),
                ((bottom - r[1]) / h, (r[3] - bottom) / max(line_h, 1))]

    def agreed(values, tol):
        """The value most of them sit near, or None when fewer than three in five do."""
        best = max(values, key=lambda v: sum(1 for x in values if abs(x - v) <= tol))
        near = sum(1 for x in values if abs(x - best) <= tol)
        return best if near >= max(2, -(-3 * len(values) // 5)) else None

    starts = {n: place(v["idx"][0], rects[n]) for n, v in names.items() if n in rects and v.get("idx")}
    member, ends = columns(rects)
    trimmed = trimmed or rects
    in_trim = {m: place(names[m]["idx"][0], trimmed[m]) for m in starts if m in trimmed}
    changed = {}
    for n, v in names.items():
        if n not in starts:
            continue
        width = rects[n][2] - rects[n][0]
        mids = []
        if n in member:
            mates = [m for m in member[n] if m != n and m in starts]
            mids = [m for m in mates if m not in ends]
            by_mids = len(mids) >= 2
            sib_ids = mids if by_mids else mates
            sibs = [starts[m] if m not in ends or by_mids else in_trim.get(m, starts[m]) for m in sib_ids]
        else:
            by_mids = False
            sib_ids = [m for m in starts if m != n and m not in ends
                       and abs((rects[m][2] - rects[m][0]) - width) <= 0.25 * max(width, 1)]
            sibs = [starts[m] for m in sib_ids]
        if len(sibs) < 2:
            continue
        hs = sorted(rects[m][3] - rects[m][1] for m in sib_ids)
        mid_h = hs[len(hs) // 2]
        dk = 3 if mid_h and abs((rects[n][3] - rects[n][1]) - mid_h) > DOWN_TALLER * mid_h else 2
        tol_k = PLACE_DOWN_LINES if dk == 3 else PLACE_DOWN
        axes = [(k, agreed([x[k] for x in sibs], PLACE_ACROSS)) for k in range(2)] + \
            [(dk, agreed([x[dk] for x in sibs], tol_k))]
        axes = [(k, a) for k, a in axes if a is not None]
        across = [(k, a) for k, a in axes if k < 2]
        down = [(k, a) for k, a in axes if k >= 2]
        if not axes:
            continue

        def fits(spot):
            ok_across = not across or any(abs(spot[k] - a) <= PLACE_ACROSS for k, a in across)
            return ok_across and (not down or abs(spot[dk] - down[0][1]) <= tol_k)
        if fits(starts[n]):
            continue
        if down:
            # (or its middle or bottom where its siblings' names have theirs, wholly in its box: F96605BE's two-line
            # "BONUS / MATERIALS" ends where PLAY, SCENES and LANGUAGES end; lines reaching out of the box -- "State
            # Street / Original Version" over B7017D5D's "Director's Cut" -- aren't its own)
            boxes_of = {m: (trimmed[m] if m in ends and not by_mids and m in trimmed else rects[m]) for m in sib_ids} \
                if n in member else {m: rects[m] for m in sib_ids}
            ck = 0 if dk == 2 else 1
            ok_across = not across or any(abs(starts[n][k] - a) <= PLACE_ACROSS for k, a in across)
            mine_at = centre(v["idx"], rects[n])
            sib_at = [centre(names[m]["idx"], boxes_of[m]) for m in sib_ids]
            held = any(a is not None and abs(mine_at[w][ck] - a) <= tol_k
                       for w in range(2) for a in [agreed([x[w][ck] for x in sib_at], tol_k)])
            if ok_across and held and all(_mid_in(lines[i][1], rects[n]) for i in v["idx"]):
                continue
        mine = sorted((i for i in range(len(lines)) if owner.get(i) == n),
                      key=lambda i: (lines[i][1]["top"], lines[i][1]["left"]))
        sib_firsts = [names[m]["idx"][0] for m in sib_ids]
        anchor = next((i for i in mine if fits(place(i, rects[n])) and any(same_style(st[i], st[j]) for j in sib_firsts)),
                      None)
        if anchor is None:
            continue
        below = [i for i in v["idx"] if lines[i][1]["top"] > lines[anchor][1]["top"]
                 and group_of.get(i) == group_of.get(anchor)]
        dropped = [i for i in v["idx"] if i != anchor and i not in below]
        if dropped:
            seq = sorted(dropped + [anchor], key=lambda i: (lines[i][1]["top"], lines[i][1]["left"]))
            if any(lines[a][0].rstrip().endswith(":") or carries_on(lines[a][0], lines[b][0])
                   for a, b in zip(seq, seq[1:]) if a in dropped):
                continue
        changed[n] = [anchor] + below
    for n, idx in changed.items():
        names[n].update(name=" ".join(lines[i][0] for i in idx), lines=[lines[i] for i in idx], idx=idx)


def page_names(image, lines, rects, corners=None, boxes=None):
    """Each button's name lines on a menu page, by its text's grouping: {button: [(text, box, sure)]}, the page's
    own lines (whole, or the part of one split off) in reading order; a button with none left out.
    image: the page's picture; lines: the page read whole ([(text, box, sure)], its pixels); rects: {button: box}
    in the same pixels, the buttons that do something where that's known; boxes: every button's (a page's
    decorations too: Chamber of Secrets' Blu-ray, 25 of 54), for joining lines and parts of lines, else rects;
    corners: menus.rapid_lines' corners for the lines (their slopes), if there are any."""
    if not lines or not rects:
        return {}
    boxes = boxes or rects
    measured = rejoin(measure(image, lines, corners), boxes)
    groups = blocks(measured, boxes)
    trimmed = trim_overflow(rects, measured, groups)
    groups = blocks(measured, boxes, trimmed=trimmed)
    names, _ = classify(image.convert("RGB"), measured, groups, trimmed, rects)
    out = {}
    for n, v in names.items():
        got = []
        for text, ik in v.get("lines") or []:
            sure = lines[ik["src"]][2]
            got.append((text, (float(ik["left"]), float(ik["top"]), float(ik["right"] + 1), float(ik["bottom"] + 1)), sure))
        if got:
            out[n] = got
    return out
