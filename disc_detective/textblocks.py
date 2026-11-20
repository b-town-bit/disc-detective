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
import statistics

import numpy as np

from PIL import Image

from . import menus

SPLIT_LETTERS = 2.0         # letters' width: a gap this wide or wider inside a line splits it
JOIN_BELOW = 1.0            # empty rows under a line, in line heights: fewer, the same text's next line
INLINE = 0.5                # share of the shorter line's ink within the other's span: one above the other in line
BOX_LINES = 3               # a box holding this many lines or fewer holds a name, not a section
BOX_JOIN = 1.4              # empty rows up to this many line heights: one text, when one such box holds both
TILT_MIN = 3                # pixels a line rises across: tilted (with TILT_SLOPE)
TILT_SLOPE = 0.026          # rows per column: tilted (1.5 degrees)
SAME_SLANT = 0.05           # rows per column apart at most: lines at one slant (slant_order)
HOLD_SHARE = 0.08           # of a slanted line's lettering in a button's box: that button's (slant_holders)
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
HEADING_BIGGER = 1.3        # lettering this many times the items' by two of its three size measures, in another
                            # style: a heading by its look
ROW_JOIN = 1.0              # OCR's lines on one row with less ink than this many letters between them: one line
HEADING_GAP = 0.5           # empty rows over such a line, in the smaller line's heights: fewer, no heading's start

CONJUNCTIONS = (r"and|or|nor|but|of|the|a|an|with|for|to|from|in|on|at|by|about|after|around|as|before|behind|"
                r"between|during|into|like|over|through|under|until|upon|within|without|&")


def _mid_in(ik, r):
    return r[0] <= (ik["left"] + ik["right"]) / 2 <= r[2] and r[1] <= (ik["top"] + ik["bottom"]) / 2 <= r[3]


def _holds(r, ik):
    """A button's box holds a line: its middle in the box, and half its width at least -- a marker's narrow box a
    heading crosses doesn't (Chuck's episodes: ">>" 19 pixels wide under "EPISODES", 103; the marker's name the
    line beside it)."""
    return _mid_in(ik, r) and min(r[2], ik["right"]) - max(r[0], ik["left"]) >= 0.5 * (ik["right"] - ik["left"])


def _holder(rects, ik):
    """The button whose box holds a line: a slanted line's own (slant_holders: held_by, where it's one of these), else
    the first whose box holds its middle (_holds)."""
    n = ik.get("held_by")
    if n is not None and n in rects:
        return n
    return next((m for m, r in rects.items() if _holds(r, ik)), None)


def slant_order(items):
    """A name's lines [(text, ink)] in reading order on a page drawn at a slant: by where each one's top crosses the
    same column (band), not its upright box's top -- a longer line under a shorter one reaches higher at its far end,
    Muppets from Space's main menu "SPECIAL / FEATURES" read "FEATURES SPECIAL"; lines level along it, left to right.
    Upright lines, as they came; and lines of several slants, a logo's words each at its own (Disney's "Sleeping
    Beauty", The Jungle Book 2's "THE" over "JUNGLE"): only a name all at one slant is a page drawn so."""
    slopes = [ik.get("k") for _, ik in items]
    if len(items) < 2 or not all(slopes) or max(slopes) - min(slopes) > SAME_SLANT:
        return list(items)
    ref = sum((ik["left"] + ik["right"]) / 2 for _, ik in items) / len(items)
    top = lambda ik: band(ik, ref)[0]
    rows = []
    for item in sorted(items, key=lambda it: top(it[1])):
        h = item[1].get("straight") or item[1]["h"]
        if rows and top(item[1]) - top(rows[-1][0][1]) < 0.5 * h:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [it for row in rows for it in sorted(row, key=lambda it: it[1]["left"])]


# a line's slope by its own letters (the user's, 2026-11-18: whether a line slopes, and by how much): its ink sorted
# into letters, a baseline and a top line through their bottoms and tops, descenders and capitals left out
LETTER_GROW = 0.25       # of a line's height, its outline grown by, up and down, to hold its letters
LETTERS_LEAST = 3        # letters at least, to judge a line's slope by (joined script reads as one or two shapes)
LETTER_OFF = 0.25        # of a letter's height: further from the fit than this, not on it (a descender, a capital)
RELIABLE_SHARE = 0.5     # of its letters on the baseline, at least: its measure to be trusted
RELIABLE_AGREE = 0.035   # rows per column (2 degrees): its baseline and top line this near each other, at most
RELIABLE_SPREAD = 0.12   # of a letter's height, its letters' scatter off the baseline, at most
FLAT = 0.0175            # rows per column (1 degree): a baseline and top line both within this, the line is level


def _ink_mask(gray):
    """The crop's lettering: Otsu's threshold, the smaller side the ink (lettering has fewer pixels than round it)."""
    hist = gray.histogram()
    total = sum(hist)
    s_all = sum(i * h for i, h in enumerate(hist))
    best, w0, s0, cut = -1, 0, 0, 128
    for t in range(256):
        w0 += hist[t]
        if not w0 or w0 == total:
            continue
        s0 += t * hist[t]
        m0, m1 = s0 / w0, (s_all - s0) / (total - w0)
        between = w0 * (total - w0) * (m0 - m1) ** 2
        if between > best:
            best, cut = between, t
    px = gray.load()
    dark = sum(hist[:cut + 1])
    ink_dark = dark < total - dark
    return lambda x, y: (px[x, y] <= cut) == ink_dark


def _letters(image, corners, grow=LETTER_GROW):
    """A line's lettering as its separate shapes (8-connected ink inside its outline, grown by `grow` of its height):
    [(middle x, top, bottom, height)] in the crop's pixels, specks (under a fifth of the median height) left out."""
    from PIL import ImageDraw
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = corners
    gy0, gy1 = grow * (y3 - y0), grow * (y2 - y1)
    quad = [(x0, y0 - gy0), (x1, y1 - gy1), (x2, y2 + gy1), (x3, y3 + gy0)]
    qx, qy = [p[0] for p in quad], [p[1] for p in quad]
    box = (max(0, int(min(qx))), max(0, int(min(qy))), min(image.width, int(max(qx)) + 1), min(image.height, int(max(qy)) + 1))
    gray = image.convert("L").crop(box)
    keep = Image.new("1", gray.size, 0)
    ImageDraw.Draw(keep).polygon([(x - box[0], y - box[1]) for x, y in quad], fill=1)
    kp = keep.load()
    on0 = _ink_mask(gray)
    W, H = gray.size
    ink = [[bool(kp[x, y]) and on0(x, y) for x in range(W)] for y in range(H)]
    seen = [[False] * W for _ in range(H)]
    shapes = []
    for y in range(H):
        for x in range(W):
            if ink[y][x] and not seen[y][x]:
                stack, pts = [(x, y)], []
                seen[y][x] = True
                while stack:
                    a, b = stack.pop()
                    pts.append((a, b))
                    for da in (-1, 0, 1):
                        for db in (-1, 0, 1):
                            c, d = a + da, b + db
                            if 0 <= c < W and 0 <= d < H and ink[d][c] and not seen[d][c]:
                                seen[d][c] = True
                                stack.append((c, d))
                if len(pts) >= 6:
                    px, py = [p[0] for p in pts], [p[1] for p in pts]
                    # (touching the crop's edge: a neighbour's or the background's, not this line's)
                    if min(px) == 0 or min(py) == 0 or max(px) == W - 1 or max(py) == H - 1:
                        continue
                    shapes.append(((min(px) + max(px)) / 2, min(py), max(py), max(py) - min(py) + 1, max(px) - min(px) + 1))
    if not shapes:
        return []
    mh = statistics.median(s[3] for s in shapes)
    wide = max(1.0, (max(p[0] for p in quad) - min(p[0] for p in quad)))
    return [s for s in shapes if s[3] >= 0.2 * mh and s[4] < 0.6 * wide]


def _theil_sen(pts):
    pairs = [(b[1] - a[1]) / (b[0] - a[0]) for i, a in enumerate(pts) for b in pts[i + 1:] if abs(b[0] - a[0]) > 1e-6]
    if not pairs:
        return None, None
    k = statistics.median(pairs)
    c = statistics.median(p[1] - k * p[0] for p in pts)
    return k, c


def letter_lines(image, corners):
    """A line's baseline and top line through its letters' bottoms and tops, robustly: a first fit, then the letters
    off it by more than LETTER_OFF of the median letter height left out (descenders below, ascenders and capitals above),
    fitted again. {"base", "top", "n" (letters), "kept" (on the baseline), "spread" (their scatter, of a letter's
    height)} or None for fewer than LETTERS_LEAST letters (joined script reads as one or two shapes)."""
    shapes = _letters(image, corners)
    if len(shapes) < LETTERS_LEAST:
        return None
    mh = statistics.median(s[3] for s in shapes)
    out = {"n": len(shapes)}
    for edge, at, side in (("base", 2, 1), ("top", 1, -1)):
        pts = [(s[0], s[at]) for s in shapes]
        k, c = _theil_sen(pts)
        if k is None:
            return None
        for _ in range(2):
            # (the line's own letters: none further from it than OFF of a letter's height, on its far side)
            kept = [p for p in pts if side * (p[1] - (k * p[0] + c)) <= LETTER_OFF * mh and -side * (p[1] - (k * p[0] + c)) <= LETTER_OFF * mh] \
                or pts
            k, c = _theil_sen(kept) if len(kept) >= 2 else (k, c)
        out[edge] = k
        if edge == "base":
            out["kept"] = len(kept)
            out["spread"] = statistics.pstdev([p[1] - (k * p[0] + c) for p in kept]) / mh if len(kept) > 1 else 0.0
    return out


def line_slope(image, corners, box_k):
    """(slope, how) of a line (corners: RapidOCR's, the page's pixels; box_k: their slope): by its letters where
    they can tell ("measured": WALL-E's menu -16.2 degrees, Harry Potter's name buttons -4.6, their boxes -2), level
    where its baseline and top line are both flat though its box tilts ("flat": Wallace & Gromit's bouncy "Cracking
    Contraptions"), else its box's ("box": joined script, Ride 'em Cowboy's; 3-D lettering)."""
    m = letter_lines(image, corners)
    if m:
        ok = m["kept"] >= RELIABLE_SHARE * m["n"] and abs(m["base"] - m["top"]) <= RELIABLE_AGREE and m["spread"] <= RELIABLE_SPREAD
        if ok:
            return (m["base"] + m["top"]) / 2, "measured"
        if abs(m["base"]) <= FLAT and abs(m["top"]) <= FLAT:
            return 0.0, "flat"
    return box_k, "box"


def clipped_area(poly, box):
    """The area of a convex polygon [(x, y)] inside an upright box (x0, y0, x1, y1): Sutherland-Hodgman, then the
    shoelace."""
    pts = list(poly)
    for edge in range(4):
        inside = [lambda p: p[0] >= box[0], lambda p: p[0] <= box[2], lambda p: p[1] >= box[1], lambda p: p[1] <= box[3]][edge]
        at = [lambda a, b: (box[0], a[1] + (b[1] - a[1]) * (box[0] - a[0]) / ((b[0] - a[0]) or 1e-9)),
              lambda a, b: (box[2], a[1] + (b[1] - a[1]) * (box[2] - a[0]) / ((b[0] - a[0]) or 1e-9)),
              lambda a, b: (a[0] + (b[0] - a[0]) * (box[1] - a[1]) / ((b[1] - a[1]) or 1e-9), box[1]),
              lambda a, b: (a[0] + (b[0] - a[0]) * (box[3] - a[1]) / ((b[1] - a[1]) or 1e-9), box[3])][edge]
        out = []
        for k in range(len(pts)):
            a, b = pts[k - 1], pts[k]
            if inside(b):
                if not inside(a):
                    out.append(at(a, b))
                out.append(b)
            elif inside(a):
                out.append(at(a, b))
        pts = out
        if not pts:
            return 0.0
    return abs(sum(pts[k - 1][0] * pts[k][1] - pts[k][0] * pts[k - 1][1] for k in range(len(pts)))) / 2


def slant_holders(lines, rects, every=False, shapes=None):
    """A slanted line's button (held_by in its ink), by its lettering's own band (band) over the buttons' boxes, not
    its upright box's middle: the box it covers most of, if that's HOLD_SHARE of it at least -- a page drawn at a
    slant has its lines' upright boxes far bigger than their words, overlapping, their middles off every button's
    (WALL-E's main menu, PLAY and SCENE SELECTION each by the start of its own line, b1's and b2's: the user's).
    Upright lines, and slanted ones no box covers so much of, as they were. every: upright lines too -- a slanted
    region turned level (menus._page_names), its buttons still by each line's start, as they were on the slant."""
    for _, ik in lines:
        if not ik.get("k") and not every:
            continue
        cols = range(int(ik["left"]), int(ik["right"]) + 1)
        area = sum(b - t for t, b in (band(ik, x) for x in cols)) or 1
        best, most = None, 0.0
        for n, r in rects.items():
            if shapes and n in shapes and not ik.get("k"):  # (a button's true shape, turned with its region)
                got = clipped_area(shapes[n], (ik["left"], ik["top"], ik["right"] + 1, ik["bottom"] + 1))
            else:
                got = 0.0
                for x in range(max(int(ik["left"]), int(r[0])), min(int(ik["right"]), int(r[2])) + 1):
                    t, b = band(ik, x)
                    got += max(0.0, min(b, r[3]) - max(t, r[1]))
            if got > most:
                best, most = n, got
        if best is not None and most >= HOLD_SHARE * area:
            ik["held_by"] = best
    return lines


def _flat(image):
    return list((getattr(image, "get_flattened_data", None) or image.getdata)())   # (Pillow 12.1 renamed it)


def _otsu_array(arr):
    """otsu() of a uint8 array (NumPy): the same histogram, the same scan of thresholds."""
    hist = np.bincount(arr.ravel(), minlength=256).tolist()
    total = int(arr.size)
    sum_all = sum(i * h for i, h in enumerate(hist))
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
    a = np.asarray(gray.crop((x0, y0, x1, y1)), dtype=np.uint8)
    cut = _otsu_array(a)
    dark = int((a <= cut).sum())
    on = (a > cut) if dark >= a.size / 2 else (a <= cut)
    h, w = on.shape
    rows = on.sum(axis=1).tolist()
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
    cols = on[top:bot + 1].sum(axis=0).tolist()
    lit_cols = [c for c, n in enumerate(cols) if n]
    if not lit_cols:
        return None
    left, right = lit_cols[0], lit_cols[-1]
    gaps, run = [], 0
    for c in range(left, right + 1):
        if cols[c] == 0:
            run += 1
        elif run:
            gaps.append((x0 + c - run, run))
            run = 0
    hgt = bot - top + 1
    letters = max(1, len(re.sub(r"\s", "", text)))
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


def _rgb(image, box):
    """A box's pixels as an (n, 3) int64 array, row by row (as getdata has them)."""
    crop = image.crop(box)
    if crop.mode != "RGB":
        crop = crop.convert("RGB")
    return np.asarray(crop, dtype=np.int64).reshape(-1, 3)


def _far_mean(px, ref):
    """The fifth of pixels px furthest from colour ref (summed channel differences; equally far ones in their order,
    a stable sort), averaged."""
    d = np.abs(px - np.asarray(ref, dtype=np.int64)).sum(axis=1)
    order = np.argsort(-d, kind="stable")
    far = px[order[:max(1, len(px) // 5)]]
    n = len(far)
    return tuple(int(v) / n for v in far.sum(axis=0))


def background(image, ik, gap=2, width=3):
    """The colour behind a line: the middle colour of a thin ring just outside its box (OCR boxes hug the letters, so
    the box's own middle colour can be the lettering's: the Muppets' outlined capitals, fill or outline by turns)."""
    W, H = image.size
    l, t, r, b = int(ik["left"]), int(ik["top"]), int(ik["right"]), int(ik["bottom"])
    parts = []
    for x0, y0, x1, y1 in ((l - gap - width, t - gap - width, r + gap + width, t - gap),
                           (l - gap - width, b + gap, r + gap + width, b + gap + width),
                           (l - gap - width, t - gap, l - gap, b + gap),
                           (r + gap, t - gap, r + gap + width, b + gap)):
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if x1 > x0 and y1 > y0:
            parts.append(_rgb(image, (x0, y0, x1, y1)))
    if not parts:
        return None
    px = np.concatenate(parts)
    s = np.sort(px, axis=0)
    return tuple(int(v) for v in s[len(px) // 2])


def ring_colour(image, ik):
    """A line's lettering colour against what's behind it: the fifth of its box's pixels furthest from its background
    (background), averaged. An outline lies between its fill and the backdrop, so the fill is read, every line alike."""
    box = (ik["left"], ik["top"], ik["right"] + 1, ik["bottom"] + 1)
    bg = background(image, ik)
    if bg is None:
        return menus.lettering_colour(image, box)
    px = _rgb(image, box)
    if not len(px):
        return None
    return _far_mean(px, bg)


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


def box_cut(lines, rects):
    """A line OCR read across buttons side by side (small slanted lettering, its gap between columns no wider than a
    word's: Spider-Man's TV SPOTS, "Blockbuster Countdown | Destiny Cutdown" read "Blockbustor Cutdown"): cut between
    each two buttons it runs across, as the buttons say where one name stops (the user's; the DVD way's row cut, The
    Swan Princess's "SCENE SELECTIONS SPECIAL FEATURES TRAILERS") -- at its widest ink gap between them (their middles
    across), however narrow: a marker beside each name, or a box round it. Buttons it runs across: those that do
    something (rects), the line's middle down within them, their middle across within its ink. [(text, ink)] -> the
    same, such lines in parts."""
    out = []
    for text, ik in lines:
        if ik.get("split_from") or len(text.split()) < 2:
            out.append((text, ik))
            continue
        mid = (ik["top"] + ik["bottom"]) / 2
        over = sorted((r for r in rects.values() if r[1] <= mid <= r[3] and ik["left"] < (r[0] + r[2]) / 2 < ik["right"]),
                      key=lambda r: (r[0] + r[2]) / 2)
        cuts = []
        for a, b in zip(over, over[1:]):
            lo, hi = (a[0] + a[2]) / 2, (b[0] + b[2]) / 2
            between = [g for g in ik["gaps"] if lo <= g[0] + g[1] / 2 <= hi]
            if between:
                g = max(between, key=lambda g: g[1])
                if g not in cuts:
                    cuts.append(g)
        out += split_line(text, ik, sorted(cuts)) if cuts else [(text, ik)]
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
    crop = image.crop(box)
    px = np.asarray(crop.convert("RGB") if crop.mode != "RGB" else crop, dtype=np.int64)
    h, w = px.shape[:2]
    r, g, b = px[..., 0].astype(np.float64), px[..., 1].astype(np.float64), px[..., 2].astype(np.float64)
    gray = np.round(0.299 * r + 0.587 * g + 0.114 * b).astype(np.int64)
    cut = _otsu_array(gray.astype(np.uint8))
    light = int((gray > cut).sum()) <= gray.size / 2
    sel = (gray > cut) if light else (gray <= cut)
    out = []
    for left, right in bits:
        c0, c1 = max(0, left - box[0]), min(w - 1, right - box[0])
        if c1 < c0:
            out.append(None)
            continue
        m = sel[:, c0:c1 + 1]
        gv = gray[:, c0:c1 + 1][m]
        cv = px[:, c0:c1 + 1][m]
        if len(gv) < 4:
            out.append(None)
            continue
        order = np.argsort(-gv if light else gv, kind="stable")
        core = cv[order[:max(2, len(gv) // 2)]]
        n = len(core)
        out.append(tuple(int(v) / n for v in core.sum(axis=0)))
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
        ik = ink(gray, b, t)            # (no lettering found in it: no line -- a backdrop's faint print read as
        if not ik:                      # words, DA502D7E's newspaper: "prim indrd", "to decraied")
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


def item_style(lines, st, owner):
    """The page's item style: the one most buttons have a line in -- a heading in one button's box loses, Enchanted's
    "Comes To Life"; tied (Goblet: every button a title and a description), the one first within its buttons. st: each
    line's style; owner: each line's button, or None."""
    owned_all = [i for i in range(len(lines)) if owner[i] is not None]
    firsts = [min((i for i in owned_all if owner[i] == n), key=lambda i: lines[i][1]["top"])
              for n in {owner[i] for i in owned_all}]
    buttons_with = lambda a: len({owner[j] for j in owned_all if same_style(a, st[j])})
    first_in = lambda a: sum(1 for j in firsts if same_style(a, st[j]))
    return max((st[i] for i in owned_all), key=lambda a: (buttons_with(a), first_in(a)), default=None)


def heading_look(a, ref):
    """Lettering a looks like a heading over items in ref's: another style, and HEADING_BIGGER as big by two of its
    three size measures, as smaller goes (Chuck S1 D3's ONLINE plate: 2.9, 1.3 and 1.65 times its items'). Not a
    wrapped name's second line measured a little bigger by one (The Water Horse's "Legend of the Deep", "Harry Potter"
    under "The Hidden Secrets of"), nor small print beside items whose capitals measure short (DA502D7E's newspaper)."""
    return ref is not None and not same_style(a, ref) and         sum(1 for k in ("size", "width", "box") if a[k] >= HEADING_BIGGER * ref[k]) >= 2


def split_at_headings(lines, groups, st, ref):
    """A heading starts a block, as empty rows do (the user's): each block split before every line in it but its first
    that looks like one (heading_look) with empty rows over it, HEADING_GAP of a line or more, under the line above it
    -- Chuck S1 D3's ONLINE, a line height under Sword Skills, joined to it and its small print by its own height. Not a
    logo's bigger lettering touching the smaller over it: the Abbott and Costello pickers' "HOLD THAT / GHOST", "One
    Night / TROPICS" (-26 to 4 px apart)."""
    out = []
    for g in groups:
        g = sorted(g, key=lambda i: (lines[i][1]["top"], lines[i][1]["left"]))
        out.append([g[0]])
        for i in g[1:]:
            if heading_look(st[i], ref):
                a, b = lines[i][1], None
                above = [j for j in out[-1] if aligned(lines[j][1], a)[0]] or out[-1]
                b = lines[max(above, key=lambda j: lines[j][1]["bottom"])][1]
                if column_gap(b, a) >= HEADING_GAP * min(a["h"], b["h"]):
                    out.append([])
            out[-1].append(i)
    return out


def _classify(image, lines, groups, rects, disc_rects=None, st=None, fixed=None):
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
    owner = {i: _holder(rects, lines[i][1]) for i in range(len(lines))}
    st = st or {i: style(image, lines[i][0], lines[i][1]) for i in range(len(lines))}
    owned_all = [i for i in range(len(lines)) if owner[i] is not None]
    firsts = [min((i for i in owned_all if owner[i] == n), key=lambda i: lines[i][1]["top"])
              for n in {owner[i] for i in owned_all}]
    page_ref = item_style(lines, st, owner)
    per_button, headings, rest = {}, [], []
    for gi, g in enumerate(groups):
        g = sorted(g, key=lambda i: (lines[i][1]["top"], lines[i][1]["left"]))
        owned = [i for i in g if owner[i] is not None]
        if not owned:
            rest.append(" / ".join(lines[i][0] for i in g))
            continue
        if len({owner[i] for i in owned}) == 1:
            # (one button's block: its title in the page's item style where it has a line in it -- such a line is
            # no heading inside its own button's block, Chuck S2 D1's "Chuck Versus the Cougars Sc. 33" over its
            # description, its comma no full stop; else by its runs of style, one_button_title)
            if page_ref is not None and any(same_style(st[i], page_ref) for i in owned):
                ref = page_ref
            else:
                ref = st[one_button_title(g, st, lines)]
        elif page_ref is not None and any(same_style(st[i], page_ref) for i in owned):
            ref = page_ref
        else:
            own_firsts = [i for i in firsts if i in owned] or owned
            ref = max((st[i] for i in own_firsts), key=lambda a: sum(1 for j in own_firsts if same_style(a, st[j])))
        if fixed and gi in fixed:       # (its title fixed by the settle loop: the block's lettering is its)
            ref = st[fixed[gi][0]]
        item = {i: same_style(st[i], ref) for i in g}
        # (a button with no line in the item style -- the selected one drawn brighter in a saved frame: Enchanted's
        # "Happy Working Song" -- its lines not in the style of text no button holds; none left, all its lines)
        free = [j for j in range(len(lines)) if owner[j] is None]
        for n in {owner[i] for i in owned}:
            mine = [i for i in owned if owner[i] == n]
            if not any(item[i] for i in mine):
                for i in [i for i in mine if not any(same_style(st[i], st[j]) for j in free)] or mine:
                    item[i] = True
        # (a line carrying on into an item under it, in its colour, is that item's first line, whatever its size
        # measures: Spider-Man 2's "Peter Parker and / Mary Jane Watson" -- a line ending in "and" is no heading)
        # (nor one touching it, no empty rows between, in its colour and its button's box: a heading has space under
        # it, as it has to start a block -- Abbott and Costello's "Bonus / Materials", "Bonus" all wide letters,
        # measured as other lettering)
        for a, b in reversed(list(zip(g, g[1:]))):
            touching = owner[a] is not None and owner[a] == owner[b] and \
                column_gap(lines[a][1], lines[b][1]) < HEADING_GAP * min(lines[a][1]["h"], lines[b][1]["h"])
            if not item[a] and item[b] and (carries_on(lines[a][0], lines[b][0]) or touching) and colours_match(st[a], st[b]):
                item[a] = True
        if fixed and gi in fixed:       # (the fixed title an item, the lines above it none)
            top = lines[fixed[gi][0]][1]["top"]
            for i in g:
                if i in fixed[gi]:
                    item[i] = True
                elif lines[i][1]["top"] < top:
                    item[i] = False
        roles, heading, colon, bound = {}, [], [], None
        since = 0                       # (a heading starts a block, as an empty line does: nothing after it is an
                                        # item's before it -- Chuck S1 D3's ONLINE, its small print not Sword Skills')

        def close(n, i):
            # (an item's text goes on only line by line: a line of its more than a line height of empty rows under
            # its last one is past the empty space ending it -- Sword Skills, not the "Blu-ray Disc." far under it)
            last = max(roles[n]["name"] + roles[n]["description"], key=lambda j: lines[j][1]["bottom"])
            a, b = lines[last][1], lines[i][1]
            return column_gap(a, b) < JOIN_BELOW * max(a["h"], b["h"])
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
                since = pos + 1
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
                        n = next((owner[j] for j in reversed(g[since:pos]) if item[j] and owner[j] is not None), None)
                        n = n if n in roles and close(n, i) else None
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
            prev_item = next((owner[j] for j in reversed(g[since:pos]) if item[j] and owner[j] is not None), None)
            if lines[i][0].rstrip().endswith(":") and later_items:
                colon.append(i)
            elif prev_item is not None and prev_item in roles and smaller(st[i], ref) and close(prev_item, i):
                roles[prev_item]["description"].append(i)
            elif heading_look(st[i], ref):
                # (a heading by its look, whatever follows it: Chuck S1 D3's ONLINE plate, the one item-like line under
                # it no button's)
                heading = [i]
                since = pos + 1
                headings.append(i)
                rest.append(lines[i][0])
            elif later_items and (not roles or len({owner[j] for j in later_items if owner[j] is not None}) >= 1
                                  and not any(item[j] for j in g[since:pos]) or len(later_items) >= 2):
                heading = [i]
                since = pos + 1
                headings.append(i)
                rest.append(lines[i][0])
            elif prev_item is not None and prev_item in roles and close(prev_item, i):
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
                    "description_lines": [lines[i] for i in main["description"]], "desc_idx": list(main["description"]),
                    "heading": " ".join(lines[i][0] for i in main["heading"])}
    full = disc_rects or rects
    disc_owner = {i: _holder(full, lines[i][1]) for i in range(len(lines))}
    placement(lines, full, disc_owner, names, {i: gi for gi, g in enumerate(groups) for i in g}, st, rects)
    inline_whole(lines, names)
    return names, rest


SETTLE_ROUNDS = 10      # rounds at most, the settle loop's (a state repeated ends it sooner, as a rule after one)
SETTLE_CYCLES = []      # pages whose settle loop came round to a state seen before (classify's own names kept)


def classify(image, lines, groups, rects, disc_rects=None, st=None):
    """Each button's name, description and heading from the blocks, by style (_classify, the user's rules), settled
    (_settle: a block's title fixed where its style group says another than classify's, classify again) and looked
    at once more by the page's style dictionary (restyle). The page's style groups worked out once for both. Returns
    ({button: {"name", "lines", "idx", "description", "heading"}}, the text no button holds)."""
    st = st or {i: style(image, lines[i][0], lines[i][1]) for i in range(len(lines))}
    sg = style_groups(st, len(lines)) if len(lines) >= 3 else None
    names, rest, verdicts = _settle(image, lines, groups, rects, disc_rects, st, sg)
    restyle(lines, groups, rects, names, st, sg, verdicts)
    return names, rest


def _state(names):
    return tuple(sorted((n, tuple(v.get("idx") or []), tuple(v.get("desc_idx") or [])) for n, v in names.items()))


def _settle(image, lines, groups, rects, disc_rects, st, sg):
    """The user's settle loop (2026-11-19): classify; then, in a block of one button whose title isn't sure and whose
    style group says another run of its lines is its title (_verdicts), that run fixed as the block's title -- grown
    through the hard rules first: a line above carrying on into it or ending in a colon (Spider-Man 2's "Peter Parker
    and" / "Mary Jane Watson", its first line in other lettering), lines it carries on into -- and classify run again,
    its own rules judging the rest against it (Order of the Phoenix's "Trailing Tonks", classify's heading over its
    sentence, the sentence its name). Each round's state kept: the same again, settled; one seen before, a cycle, and
    classify's own names kept (SETTLE_CYCLES). SETTLE_ROUNDS at most. Returns (names, rest, the verdicts for them
    or None): its last verdicts, for restyle to take as they are, where the names are still those they were worked
    out for (most pages: nothing to fix)."""
    first = _classify(image, lines, groups, rects, disc_rects, st=st)
    names, rest = first
    if len(lines) < 3:
        return names, rest, None
    seen, fixed = [_state(names)], {}
    block_of = {i: k for k, b in enumerate(groups) for i in b}
    # (classify's hard rules for a title: a colon, a line carrying on, and a line touching the one under it in its
    # colour, no empty rows between -- Abbott and Costello's "BONUS" / "MATERIALS", "BONUS" all wide letters)
    touching = lambda a, b: column_gap(lines[a][1], lines[b][1]) < HEADING_GAP * min(lines[a][1]["h"], lines[b][1]["h"]) \
        and colours_match(st[a], st[b])
    tied = lambda a, b: lines[a][0].rstrip().endswith(":") or carries_on(lines[a][0], lines[b][0]) or touching(a, b)
    v = None
    for _ in range(SETTLE_ROUNDS):
        v = _verdicts(lines, groups, rects, names, st, sg)
        if v is None:
            break
        word, confident, owner, order = v["word"], v["confident"], v["owner"], v["order"]
        new = dict(fixed)
        for n, nv in names.items():
            title = list(nv.get("idx") or [])
            if not title or confident.get(title[0]) or word(title[0]) == "title":
                continue
            gi = block_of.get(title[0])
            if gi is None or gi in new or len({owner[i] for i in groups[gi] if owner[i] is not None}) != 1:
                continue
            mine = sorted((i for i in groups[gi] if owner[i] in (n, None)), key=order)
            run = []
            for i in mine:
                if word(i) == "title":
                    run.append(i)
                elif run:
                    break
            while run and mine.index(run[0]) > 0 and tied(mine[mine.index(run[0]) - 1], run[0]):
                run.insert(0, mine[mine.index(run[0]) - 1])
            while run and mine.index(run[-1]) + 1 < len(mine) and tied(run[-1], mine[mine.index(run[-1]) + 1]):
                run.append(mine[mine.index(run[-1]) + 1])
            if run and run != title:
                new[gi] = run
        if new == fixed:
            return names, rest, v       # (nothing more to fix: its verdicts are these names')
        fixed = new
        names, rest = _classify(image, lines, groups, rects, disc_rects, st=st, fixed=fixed)
        state = _state(names)
        if state == seen[-1]:
            return names, rest, v       # (the same again: the verdicts were worked out for this state)
        if state in seen:               # (a cycle: classify's own names, and the page noted)
            SETTLE_CYCLES.append(tuple(t for t, _ in lines))
            return first[0], first[1], None
        seen.append(state)
    return names, rest, None


# ---- the page's style dictionary, a second look at what classify isn't sure of (the user's design, 2026-11-19;
# demo temp/style_demo.py, benched on 15 keyed pages: titles 291 -> 297, descriptions 299 -> 306, the bench's
# Blu-ray titles 312 -> 316, none lost, no time to speak of) ----

STYLE_MOST = 0.6        # of the pairs across two groups alike, at least: one style group (OCR's box height left out)
GROUP_FIRST = 0.6       # of a group's buttons in which its line comes first, at least: a title group
GROUP_AFTER = 0.6       # ... in which it comes after another group's: a description group (with SENTENCE_SHARE)
SENTENCE_SHARE = 0.3    # of its lines reading as sentences, at least: a description group (or smaller lettering)
GROUP_FREE = 0.6        # of its lines no button's, at least: another group (headings, the page's own text)
VERDICT_SHARE = 0.7     # of a group's placement evidence one way, at least: its word on its lines' role
VOTES_LEAST = 3         # confident lines, at least, for their roles alone to speak for their group (and 0.8 of them)


def style_groups(st, n):
    """The page's lines grouped by style, by agreement across a group, never by a chain of pairs: two groups join
    while STYLE_MOST of the pairs across them are alike (same_style, OCR's box height left out: noisy on short lines),
    the most alike first. st: each line's style; n: how many lines. [[line, ...], ...]."""
    alike = lambda a, b: same_style(dict(a, box=1), dict(b, box=1))
    ok = [[alike(st[i], st[j]) if i != j else True for j in range(n)] for i in range(n)]
    groups = [[i] for i in range(n)]
    while True:
        best, pair = STYLE_MOST, None
        for x in range(len(groups)):
            for y in range(x + 1, len(groups)):
                share = sum(ok[i][j] for i in groups[x] for j in groups[y]) / (len(groups[x]) * len(groups[y]))
                if share >= best and (pair is None or share > best):
                    best, pair = share, (x, y)
        if pair is None:
            return groups
        x, y = pair
        groups[x] += groups.pop(y)


def _sentence(text):
    return bool(re.search(r"[.!?]['\")]?\s*$", text)) or len(text.split()) >= 5


def restyle(lines, groups, rects, names, st, sg=None, verdicts=None):
    """A second look at classify's names, by the page's style groups (the user's design): lines that look alike do
    alike. First, each line classify named, CONFIDENT or not by how its own signs agree -- a title first of its
    button's lines, in the page's item lettering, short (under 5 words, no full stop); a description after its
    button's title, smaller than it, reading as a sentence (or a wrap). Then each style group's word on its lines,
    from two kinds of evidence apart -- where its own lines sit (a title group first in its buttons, a description
    group after another in them and reading as sentences or smaller, another group mostly no button's) and its
    confident lines' roles (an unsure line has no say: it can't poison its group) -- a word when they agree, or one
    is clear and the other silent, none on a conflict. Then, within a button, never a confident line, never past a
    hard rule (a colon, a line carrying on, an inline piece stays with its title; a cut never parts them, the run
    moved may hang together itself):
      a title's end -> its description   its group's word description (all of the title only if a title-word line
                                         of the button's above it takes its place)
      a description's start -> its title its group's word title
      an unused line -> its title        the button's, above its title or between a colon and the line after it
      unused lines -> its description    the button's, close under its last line -- and only where confident lines
                                         of its group are descriptions too: a backdrop's print read as words
                                         (DA502D7E's newspaper) has no say in it
    Order of the Phoenix's "The Hidden Secrets of / Harry Potter" over its sentence (classify had the sentence for
    its title), "Harry Potter: / The Magic of Editing" (its colon had bound the description's first line), the
    descriptions Azkaban's interviews lost. names: classify's, changed in place; sg: the page's style groups, if
    worked out already (style_groups); verdicts: _verdicts' for these names, if worked out already (_settle's).
    (Moves change the names; restyle's own verdicts are of the names before them, as they always were.)"""
    v = verdicts if verdicts is not None else _verdicts(lines, groups, rects, names, st, sg)
    if v is None:
        return
    order, word, confident, by_button, evidence, group_of = (v[k] for k in ("order", "word", "confident", "by_button",
                                                                            "evidence", "group_of"))

    def tied(a, b):
        """b can't be parted from a, the line before it: a colon, a line carrying on, an inline piece."""
        return lines[a][0].rstrip().endswith(":") or carries_on(lines[a][0], lines[b][0]) or \
            bool(lines[b][1].get("inline")) or bool(lines[a][1].get("inline"))
    _moves(lines, names, order, word, confident, by_button, evidence, group_of, tied)


def _verdicts(lines, groups, rects, names, st, sg=None):
    """restyle's and the settle loop's look at a page: each line classify named, confident or not; each style group's
    evidence; and word(line), its group's word on its role. {"order", "word", "confident", "by_button", "owner",
    "evidence", "group_of"}, or None for a page with too little on it."""
    if not names or len(lines) < 3:
        return None
    order = lambda i: (lines[i][1]["top"], lines[i][1]["left"])
    held = {i: _holder(rects, lines[i][1]) for i in range(len(lines))}
    owner = dict(held)                  # (a line no box holds, in a block only one button's lines are in: its)
    for b in groups:
        mine = {held[i] for i in b if held[i] is not None}
        if len(mine) == 1:
            for i in b:
                if owner[i] is None:
                    owner[i] = next(iter(mine))
    block_of = {i: k for k, b in enumerate(groups) for i in b}
    ref = item_style(lines, st, held)
    by_button = {}
    for i in sorted(owner, key=order):
        if owner[i] is not None:
            by_button.setdefault(owner[i], []).append(i)
    role, confident = {}, {}
    for n, v in names.items():
        title = list(v.get("idx") or [])
        for i in title:
            role[i] = "title"
            confident[i] = bool(by_button.get(n)) and by_button[n][0] == i and ref is not None and \
                same_style(st[i], ref) and not _sentence(lines[i][0])
        for k, i in enumerate(v.get("desc_idx") or []):
            role[i] = "description"
            above = [j for j in title if order(j) < order(i)]
            confident[i] = bool(above) and smaller(st[i], st[above[0]]) and (_sentence(lines[i][0]) or k > 0)
    sg = sg if sg is not None else style_groups(st, len(lines))
    group_of = {i: k for k, g in enumerate(sg) for i in g}
    evidence = []
    for k, g in enumerate(sg):
        place = None
        if len(g) >= 2:
            buttons = {owner[i] for i in g if owner[i] is not None}
            free = sum(1 for i in g if owner[i] is None) / len(g)
            first = sum(1 for n in buttons if group_of[by_button[n][0]] == k) / max(len(buttons), 1)
            after = sum(1 for n in buttons if group_of[by_button[n][0]] != k) / max(len(buttons), 1)
            wraps = lambda i: any(j != i and block_of.get(j) == block_of.get(i) and order(j) < order(i) for j in g)
            sentence = sum(1 for i in g if _sentence(lines[i][0]) or wraps(i)) / len(g)
            over = [st[by_button[n][0]]["size"] for n in buttons if group_of[by_button[n][0]] != k]
            small = bool(over) and statistics.median(st[i]["size"] for i in g) < 0.9 * statistics.median(over)
            if free >= GROUP_FREE:
                place = "other"
            elif len(buttons) >= 2 and first >= GROUP_FIRST:
                place = "title"
            elif len(buttons) >= 2 and after >= GROUP_AFTER and (sentence >= SENTENCE_SHARE or small):
                place = "description"
        votes = {}
        for i in g:
            if confident.get(i):
                votes[role[i]] = votes.get(role[i], 0) + 1
        evidence.append((votes, place))

    def word(i):
        """Its group's word on line i's role, or None."""
        votes, place = evidence[group_of[i]]
        top = max(votes, key=votes.get) if votes else None
        if top and place and top != place:
            return None                                 # (a conflict: no word)
        if place:
            return place
        if top and votes[top] >= VOTES_LEAST and votes[top] / sum(votes.values()) >= 0.8:
            return top
        return None
    return {"order": order, "word": word, "confident": confident, "by_button": by_button, "owner": owner,
            "evidence": evidence, "group_of": group_of}


def _moves(lines, names, order, word, confident, by_button, evidence, group_of, tied):
    """restyle's moves, by the page's verdicts (_verdicts): names changed in place."""
    used = {i for v in names.values() for i in list(v.get("idx") or []) + list(v.get("desc_idx") or [])}
    for n, v in names.items():
        title, desc = list(v.get("idx") or []), list(v.get("desc_idx") or [])
        if not title:
            continue
        mine = by_button.get(n, [])
        lead = [i for i in mine if i not in used and order(i) < order(title[0]) and word(i) == "title" and lines[i][0].strip()]
        between = lambda a, b: [m for m in mine if m not in used and order(a) < order(m) < order(b) and word(m) == "title"]
        before = (list(title), list(desc))
        cut = len(title)
        while cut > 0 and not confident.get(title[cut - 1]) and word(title[cut - 1]) == "description":
            cut -= 1
        while 0 < cut < len(title) and tied(title[cut - 1], title[cut]) and not (
                lines[title[cut - 1]][0].rstrip().endswith(":") and between(title[cut - 1], title[cut])):
            cut += 1
        if cut == 0 and not lead:
            cut = 1 if len(title) > 1 else len(title)
        if cut < len(title):
            mids = between(title[cut - 1], title[cut]) if cut else []
            used.update(mids)
            desc, title = title[cut:] + desc, title[:cut] + mids
        if not title and lead:
            title = lead
            used.update(lead)
        while desc and title and not confident.get(desc[0]) and word(desc[0]) == "title" and order(desc[0]) > order(title[-1]):
            title.append(desc.pop(0))
        last = (desc or title)[-1]
        for i in mine:
            if i in used or order(i) <= order(last) or word(i) != "description" or \
                    not evidence[group_of[i]][0].get("description"):
                continue
            a, b = lines[last][1], lines[i][1]
            if column_gap(a, b) >= JOIN_BELOW * max(a["h"], b["h"]) * 1.5:
                break
            desc.append(i)
            used.add(i)
            last = i
        if (title, desc) != before:
            used.update(title + desc)
            v.update(name=" ".join(lines[i][0] for i in title), lines=[lines[i] for i in title], idx=title,
                     description=" ".join(lines[i][0] for i in desc), description_lines=[lines[i] for i in desc],
                     desc_idx=desc)


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
        # (never its own description: a title traded for the line under it, Chuck S2 D1's b16, said twice)
        anchor = next((i for i in mine if i not in (v.get("desc_idx") or ()) and fits(place(i, rects[n]))
                       and any(same_style(st[i], st[j]) for j in sib_firsts)), None)
        if anchor is None:
            continue
        below = [i for i in v["idx"] if lines[i][1]["top"] > lines[anchor][1]["top"]
                 and group_of.get(i) == group_of.get(anchor)]
        dropped = [i for i in v["idx"] if i != anchor and i not in below]
        if dropped:
            seq = sorted(dropped + [anchor], key=lambda i: (lines[i][1]["top"], lines[i][1]["left"]))
            # (nor a line touching the next, in its colour: one title -- Buck Privates' "BONUS / MATERIALS")
            touch = lambda a, b: column_gap(lines[a][1], lines[b][1]) < HEADING_GAP * min(lines[a][1]["h"], lines[b][1]["h"]) \
                and colours_match(st[a], st[b])
            if any(lines[a][0].rstrip().endswith(":") or carries_on(lines[a][0], lines[b][0]) or touch(a, b)
                   for a, b in zip(seq, seq[1:]) if a in dropped):
                continue
        changed[n] = [anchor] + below
    for n, idx in changed.items():
        names[n].update(name=" ".join(lines[i][0] for i in idx), lines=[lines[i] for i in idx], idx=idx)


def _gap(a, b):
    """The empty distance between two boxes (0 if they touch or overlap)."""
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return (dx * dx + dy * dy) ** 0.5


def empty_buttons_blocks(lines, groups, rects, boxes, reach):
    """Each button with no text in its box (a pointer, a lamp, a picture) and its text, the user's: the text block
    whose line is nearest it, touching it or within reach -- only if no button's box holds that line, and no other
    such button is nearer it; never a line further off past that one: a button with no text near it has none
    (Spider-Man's hidden buttons). That block is its text, whole, whatever its lettering ("MEN IN / BLACK / II", a
    logo's sizes); several such buttons nearest lines of one block share it out, each line to the nearest of them
    (27F0BF0E's "THEATRICAL TRAILER / SNEAK PEEKS", an icon by each). {button: [index]}, in reading order."""
    whose = [_holder(rects, lines[i][1]) for i in range(len(lines))]
    empty = [n for n in rects if n not in whose]
    if not empty or not lines:
        return {}
    import statistics
    spot = lambda i: (lines[i][1]["left"], lines[i][1]["top"], lines[i][1]["right"], lines[i][1]["bottom"])
    # (held by a button that does something: a decoration's box holds no one's text -- Chuck's episodes page, its
    # names under decorations' boxes)
    held = [w is not None for w in whose]
    mid = lambda b: ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    gap = lambda n, i: _gap(rects[n], spot(i))
    nearest = {}
    for n in empty:
        least = min(gap(n, i) for i in range(len(lines)))
        if least <= reach:
            nearest[n] = [i for i in range(len(lines)) if gap(n, i) <= least + 1]
    # (lines tied for nearest -- touching it both: a column's first marker's box reaching up over the heading,
    # Chuck's) told apart by where its siblings' names sit from theirs (each with one nearest), else the most level
    offs = [(mid(spot(c[0]))[0] - mid(rects[n])[0], mid(spot(c[0]))[1] - mid(rects[n])[1]) for n, c in nearest.items() if len(c) == 1]
    dx, dy = (statistics.median(o[0] for o in offs), statistics.median(o[1] for o in offs)) if offs else (None, None)
    away = lambda n, i: (((mid(spot(i))[0] - mid(rects[n])[0] - dx) ** 2 + (mid(spot(i))[1] - mid(rects[n])[1] - dy) ** 2) ** 0.5
                         if dx is not None else abs(mid(spot(i))[1] - mid(rects[n])[1]))
    cost = lambda n, i: (gap(n, i), away(n, i))
    want = {}
    for n, ties in nearest.items():
        i = min(ties, key=lambda i: away(n, i))
        if not held[i]:
            want.setdefault(i, []).append(cost(n, i) + (n,))
    first = {min(claims)[2]: i for i, claims in want.items()}           # (a line: the nearest button's)
    block_of = {i: gi for gi, g in enumerate(groups) for i in g}
    out = {}
    for gi in {block_of[i] for i in first.values()}:
        mine = [n for n, i in first.items() if block_of[i] == gi]
        for i in groups[gi]:            # (each of its lines no box holds to the nearest of its buttons)
            if not held[i]:
                out.setdefault(min(mine, key=lambda n: cost(n, i)), []).append(i)
    return {n: sorted(idx, key=lambda i: (lines[i][1]["top"], lines[i][1]["left"])) for n, idx in out.items()}


def hull(points):
    """The convex hull of [(x, y)], clockwise from the leftmost (fewer than three points: as they are)."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) < 3:
        return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def line_shape(text, box, corners):
    """A line's four corners as RapidOCR found them (its slant), in its box's pixels; a piece of a line read whole
    (split off it: its own text not RapidOCR's) its stretch of that line's slanted band; none known, its box."""
    x0, y0, x1, y1 = box
    c = corner_of(corners, text, box)
    if c:
        return [tuple(p) for p in c[1]]
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    whole = next((c for c in corners or () if min(p[0] for p in c[1]) <= mx <= max(p[0] for p in c[1])
                  and min(p[1] for p in c[1]) <= my <= max(p[1] for p in c[1]) and text in c[0]), None)
    if whole:
        (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = [tuple(p) for p in whole[1]]
        top = lambda x: tly + (try_ - tly) * (x - tlx) / max(trx - tlx, 1e-6)
        bottom = lambda x: bly + (bry - bly) * (x - blx) / max(brx - blx, 1e-6)
        return [(x0, top(x0)), (x1, top(x1)), (x1, bottom(x1)), (x0, bottom(x0))]
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def reading_shape(lines, corners):
    """The one outline round a reading's lines (its name's and its description's), following their slant: the convex
    hull of their corners (line_shape) -- Spider-Man's Oscorp screens, WALL-E's tilted list, not an upright box."""
    return hull([p for t, b, _ in lines for p in line_shape(t, b, corners)])


def corner_of(corners, text, box):
    """A line's four corners among rapid_lines' (as slopes finds them: by its text, near its box's top left), or None."""
    return next((c for c in corners or () if c[0] == text and abs(min(x for x, _ in c[1]) - box[0]) < 6
                 and abs(min(y for _, y in c[1]) - box[1]) < 6), None)


def join_rows(image, lines, corners=None):
    """OCR's lines side by side on one row, their ink a word's gap apart (ROW_JOIN of their letters' width, measured
    as measure has them): one line (Chuck Season 2's "Chuck Versus" | "the Third Dimension", read apart, 19 px of ink
    between them). Not lines further apart (columns, a bar's buttons, an icon's scrap beside a name: "O Motion
    Gallery"), nor one starting more than a letter back inside the other (stacked lines, a logo's pieces). Benched
    2026-11-17 against OCR's boxes (6 names lost) and the lettering's height (Chuck's missed): none lost.
    [(text, box, sure)], rapid_lines' corners (or None) -> the same, joined left to right: the text with a space, the
    boxes' union, the least sure; the corners as they were, the joined line's added (the first's left, the last's
    right: slopes finds a line's by its text and place)."""
    lines = list(lines)
    corners = list(corners) if corners is not None else None
    ink = {}                            # (each line's ink: left, top, right, bottom, a letter's width)
    for _, ik in measure(image, lines, corners):
        k = ik["src"]
        was = ink.get(k)
        ink[k] = (ik["left"], ik["top"], ik["right"], ik["bottom"], ik["letter"]) if was is None else \
            (min(was[0], ik["left"]), min(was[1], ik["top"]), max(was[2], ik["right"]), max(was[3], ik["bottom"]), was[4])
    pairs = []
    for i, a in ink.items():
        for j, c in ink.items():
            if i == j or c[0] < a[0] or min(a[3], c[3]) - max(a[1], c[1]) < 0.6 * min(a[3] - a[1], c[3] - c[1]):
                continue
            gap, letter = c[0] - a[2], min(a[4], c[4])
            if -letter <= gap < ROW_JOIN * letter:
                pairs.append((gap, i, j))
    nxt = {}
    for _, i, j in sorted(pairs):       # (each line to its nearest neighbour on its right, once)
        if i not in nxt and j not in nxt.values():
            nxt[i] = j
    if not nxt:
        return lines, corners
    out = []
    for i in range(len(lines)):
        if i in nxt.values():
            continue
        chain = [i]
        while chain[-1] in nxt:
            chain.append(nxt[chain[-1]])
        if len(chain) == 1:
            out.append(lines[i])
            continue
        text = " ".join(lines[k][0] for k in chain)
        bs = [lines[k][1] for k in chain]
        out.append((text, (min(x[0] for x in bs), min(x[1] for x in bs), max(x[2] for x in bs), max(x[3] for x in bs)),
                    min(lines[k][2] for k in chain)))
        if corners is not None:
            c1, c2 = corner_of(corners, *lines[chain[0]][:2]), corner_of(corners, *lines[chain[-1]][:2])
            if c1 and c2:
                corners.append((text, (c1[1][0], c2[1][1], c2[1][2], c1[1][3]), out[-1][2]))
    return out, corners


def page_names(image, lines, rects, corners=None, boxes=None, reach=None, level=False, shapes=None, owners=None):
    """Each button's name lines on a menu page, by its text's grouping: {button: [(text, box, sure)]}, the page's
    own lines (whole, or the part of one split off) in reading order; a button with none left out.
    image: the page's picture; lines: the page read whole ([(text, box, sure)], its pixels); rects: {button: box}
    in the same pixels, the buttons that do something where that's known; boxes: every button's (a page's
    decorations too: Chamber of Secrets' Blu-ray, 25 of 54), for joining lines and parts of lines, else rects;
    corners: menus.rapid_lines' corners for the lines (their slopes), if there are any; reach: how far from a button
    with no text in its box its text's nearest line may be (empty_buttons_blocks), page pixels (default: 60 of a
    720-wide page). That block is its text, and what's read again for it, as any button's is: the user's."""
    if not lines or not rects:
        return {}
    boxes = boxes or rects
    lines, corners = join_rows(image, lines, corners)
    measured = slant_holders(box_cut(rejoin(measure(image, lines, corners), boxes), rects), rects, every=level,
                             shapes=shapes)
    for text, ik in measured:           # (owners: {a line's text: its button}, as worked out before -- on the page)
        if owners and owners.get(text) in rects:
            ik["held_by"] = owners[text]
    rgb = image.convert("RGB")
    st = {i: style(rgb, measured[i][0], measured[i][1]) for i in range(len(measured))}
    ref = item_style(measured, st, {i: _holder(rects, measured[i][1]) for i in range(len(measured))})
    groups = split_at_headings(measured, blocks(measured, boxes), st, ref)
    paired = empty_buttons_blocks(measured, groups, rects, boxes, 60 * image.width / 720 if reach is None else reach)
    trimmed = trim_overflow(rects, measured, groups)
    groups = split_at_headings(measured, blocks(measured, boxes, trimmed=trimmed), st, ref)
    names, _ = classify(rgb, measured, groups, trimmed, rects, st=st)
    for n, idx in paired.items():       # (a button with no text in its box: its block, whole)
        names[n] = {"name": " ".join(measured[i][0] for i in idx), "lines": [measured[i] for i in idx], "idx": idx}
    inline_whole(measured, names)
    out = {}
    for n, v in names.items():
        got = Named()
        for text, ik in slant_order(v.get("lines") or []):
            sure = lines[ik["src"]][2]
            got.append((text, (float(ik["left"]), float(ik["top"]), float(ik["right"] + 1), float(ik["bottom"] + 1)), sure))
        got.heading = v.get("heading") or ""
        got.description = [(text, (float(ik["left"]), float(ik["top"]), float(ik["right"] + 1), float(ik["bottom"] + 1)),
                            lines[ik["src"]][2]) for text, ik in slant_order(v.get("description_lines") or [])]
        if got:
            out[n] = got
    return out


class Named(list):
    """A button's name lines (page_names), the lines under it describing it (.description: as its own, [] if none),
    and the heading over it (.heading: "" if none) -- a Play All named
    by its menu takes its heading's name, the user's."""
    heading = ""
    description = ()
    alts = ()               # (its lines as read turned level, a page at a slant's: menus._page_names)
