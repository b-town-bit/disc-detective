"""page_layout on kept Blu-ray menu pages, against answer keys put right by hand from the screenshots.

  DISC_DETECTIVE_LAYOUT_CASES  a folder of cases made by temp/layout_capture.py (default: temp/layout_cases, when it
                               exists): each has the page's OCR lines (read once), its buttons and highlights, the
                               page picture, and "expect" (headings, and each button's title, description, section).

Each case is its own test, so one page can be put right at a time without the others slipping."""
import glob
import json
import os

import pytest

from disc_detective import menus

pytestmark = pytest.mark.dev
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.environ.get("DISC_DETECTIVE_LAYOUT_CASES") or os.path.join(HERE, "temp", "layout_cases")
FILES = sorted(glob.glob(os.path.join(CASES, "*.json")))


def answer(lay, buttons):
    """What a layout says, as an answer key has it: lines in reading order (top to bottom) within each part."""
    lines = lay["lines"]
    order = sorted(range(len(lines)), key=lambda i: (lines[i]["box"][1], lines[i]["box"][0]))
    text = lambda n, role: " ".join(lines[i]["text"] for i in order if lines[i]["button"] == n and lines[i]["role"] == role)
    section = lambda n: next((lines[l["section"]]["text"] for l in lines if l["button"] == n and l["role"] == "title"
                              and l["section"] is not None), None)
    return {"headings": [l["text"] for l in lines if l["role"] == "heading"],
            "buttons": {str(b["n"]): {"title": text(b["n"], "title"), "description": text(b["n"], "description"),
                                      "section": section(b["n"])} for b in buttons if text(b["n"], "title")}}


@pytest.mark.skipif(not FILES, reason="no layout cases (temp/layout_capture.py makes them)")
@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.splitext(os.path.basename(p))[0])
def test_page_layout(path):
    from PIL import Image

    case = json.load(open(path, encoding="utf-8"))
    image = case["image"] if os.path.isabs(case["image"]) else os.path.join(HERE, case["image"])
    lines = [(t, tuple(b), s) for t, b, s in case["lines"]]
    buttons = [dict(b, box=tuple(b["box"]), lit=tuple(b["lit"]) if b["lit"] else None) for b in case["buttons"]]
    got = answer(menus.page_layout(lines, buttons, tuple(case["size"]), Image.open(image).convert("RGB")), buttons)
    want = case["expect"]
    wrong = []
    if sorted(got["headings"]) != sorted(want["headings"]):
        wrong.append("headings: missing %r, extra %r" % (sorted(set(want["headings"]) - set(got["headings"])),
                                                          sorted(set(got["headings"]) - set(want["headings"]))))
    for n in sorted(set(want["buttons"]) | set(got["buttons"]), key=int):
        w, g = want["buttons"].get(n), got["buttons"].get(n)
        if w is None or g is None:
            wrong.append("b%s: %s" % (n, "not a title in the key: %r" % g["title"] if w is None else "no title, want %r" % w["title"]))
            continue
        for k in ("title", "description", "section"):
            if w[k] != g[k]:
                wrong.append("b%s %s: got %r, want %r" % (n, k, g[k], w[k]))
    assert not wrong, "%d wrong:\n  " % len(wrong) + "\n  ".join(wrong)
