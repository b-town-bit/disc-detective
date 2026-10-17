"""Tests on real data you keep outside the repo (skipped unless you point at it).

  DISC_DETECTIVE_DEV_LIBRARY  a library.sqlite whose discs were scanned with --keep-evidence
                              and confirmed with --set-truth: every disc is re-investigated
                              from its evidence and graded against your answer.
  DISC_DETECTIVE_DEV_IMDB     IMDb data for that run (default: the test fixture)
  DISC_DETECTIVE_DISCDB       a TheDiscDb/data checkout: the MakeMKV log parser is checked
                              against every disc TheDiscDB has.
"""
import json
import os
import re

import pytest

from disc_detective import core

pytestmark = pytest.mark.dev
# read now: the autouse isolation fixture clears these before each test runs
DEV_LIBRARY = os.environ.get("DISC_DETECTIVE_DEV_LIBRARY")
DEV_IMDB = os.environ.get("DISC_DETECTIVE_DEV_IMDB")
DISCDB = os.environ.get("DISC_DETECTIVE_DISCDB")


@pytest.mark.skipif(not DEV_LIBRARY, reason="set DISC_DETECTIVE_DEV_LIBRARY")
def test_my_discs(imdb_dir, capsys):
    con = core.library_open(DEV_LIBRARY)
    args = core.default_args(imdb_dir=DEV_IMDB or imdb_dir, offline=True, discdb_repo=DISCDB)
    core.corpus_summary(con, args)
    out = capsys.readouterr().out
    with capsys.disabled():
        print("\n" + out)
    m = re.search(r"Truth check: (\d+) ok, (\d+) partial, (\d+) wrong, (\d+) missed", out)
    assert m and int(m.group(3)) == 0, "a disc was identified as the wrong title"


@pytest.mark.skipif(not DISCDB, reason="set DISC_DETECTIVE_DISCDB")
def test_makemkv_parser_against_thediscdb():
    data = os.path.join(DISCDB, "data")
    checked = mismatched = 0
    for dp, _, fns in os.walk(data):
        for fn in fns:
            m = re.match(r"(disc\d+)\.txt$", fn)
            if not m or not os.path.exists(os.path.join(dp, m.group(1) + ".json")):
                continue
            text = open(os.path.join(dp, fn), encoding="utf-8-sig", errors="replace").read()
            if "TINFO:" not in text:
                continue
            disc = json.load(open(os.path.join(dp, m.group(1) + ".json"), encoding="utf-8-sig"))
            mk = {t["index"]: t for t in core.parse_makemkv_log(text)["titles"]}
            for t in disc.get("Titles") or []:
                ours = mk.get(t.get("Index"))
                checked += 1
                if not ours or (t.get("Duration") and ours["duration_text"] != t["Duration"]):
                    mismatched += 1
    assert checked > 1000 and mismatched / checked < 0.001
