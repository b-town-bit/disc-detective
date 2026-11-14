"""TheDiscDB's contribute form, as the submit guide (core.submission_steps) was last matched to it.

Their site's source is public (github.com/TheDiscDb/web): the files behind its contribute pages, the form
controls they use and the UPC rule are recorded here (discdb_form.json), each by its git version, with their
commit at the time. A change to any of them may mean the guide's steps need one too, and a release.

  DISC_DETECTIVE_WATCH_DISCDB=1 pytest tests/test_discdb_form.py -s
      compare their files now with the ones recorded (needs the internet; GITHUB_TOKEN, if set, is used for
      GitHub's rate limit). Fails listing each file changed, added or gone, and a link to everything since.
  python tests/test_discdb_form.py --update
      record their files as they are now: once the guide matches their form again.
"""
import json
import os
import sys
import urllib.request

import pytest

RECORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discdb_form.json")
REPO = "TheDiscDb/web"
# what the guide follows: every contribute page (box sets too, for when the guide gets there), the form's own
# controls, and the rules their form checks values by (the UPC's)
WATCHED = ("code/TheDiscDb.Client/Pages/Contribute/",
           "code/TheDiscDb.Client/Controls/AsinInput.", "code/TheDiscDb.Client/Controls/ReleaseDateInput.",
           "code/TheDiscDb.Client/Controls/SlugInput.", "code/TheDiscDb.Client/Controls/ContributionNamingSuggestion.",
           "code/TheDiscDb.Client/Controls/HelpPopover.", "code/TheDiscDb.Core/Validation/")
WATCH = os.environ.get("DISC_DETECTIVE_WATCH_DISCDB")      # (read now: the isolation fixture clears the environment)
TOKEN = os.environ.get("GITHUB_TOKEN")


def watched(paths):
    """Their files the guide follows ({path: version}), styling left out."""
    return {p: v for p, v in paths.items() if p.startswith(WATCHED) and not p.endswith(".css")}


def form_changes(recorded, now):
    """What's different in their form's files since they were recorded: {"changed", "added", "gone"}, each a sorted
    list of paths."""
    return {"changed": sorted(p for p in recorded if p in now and now[p] != recorded[p]),
            "added": sorted(p for p in now if p not in recorded),
            "gone": sorted(p for p in recorded if p not in now)}


def github(path):
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (REPO, path), headers=dict(
        {"Accept": "application/vnd.github+json", "User-Agent": "disc-detective-tests"},
        **({"Authorization": "Bearer " + TOKEN} if TOKEN else {})))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def their_form():
    """Their main branch now: (its commit, {path: version} of the files the guide follows)."""
    commit = github("commits/main")["sha"]
    tree = github("git/trees/%s?recursive=1" % commit)
    assert not tree.get("truncated"), "GitHub cut their file list short: check by hand"
    return commit, watched({t["path"]: t["sha"] for t in tree["tree"] if t["type"] == "blob"})


def report(changes, since):
    lines = ["TheDiscDB's contribute form changed since the guide was matched to it (commit %s):" % since[:7]]
    for kind in ("changed", "added", "gone"):
        lines += ["  %-8s %s" % (kind, p) for p in changes[kind]]
    lines.append("Everything since: https://github.com/%s/compare/%s...main" % (REPO, since))
    lines.append("Walk their form, fix core.submission_steps to match, then: python tests/test_discdb_form.py --update")
    return "\n".join(lines)


def test_what_counts_as_a_change_to_their_form():
    """A file of theirs changed, added or gone, among those the guide follows; their styling, and pages it doesn't
    follow, aren't."""
    recorded = {"code/TheDiscDb.Client/Pages/Contribute/AddDisc.razor": "a",
                "code/TheDiscDb.Client/Pages/Contribute/IdentifyTitle.razor": "b"}
    now = watched({"code/TheDiscDb.Client/Pages/Contribute/AddDisc.razor": "a2",
                   "code/TheDiscDb.Client/Pages/Contribute/NewStep.razor": "c",
                   "code/TheDiscDb.Client/Pages/Contribute/AddDisc.razor.css": "d",
                   "code/TheDiscDb.Client/Pages/Home.razor": "e",
                   "code/TheDiscDb.Core/Validation/UpcAttribute.cs": "f"})
    changes = form_changes(recorded, now)
    assert changes == {"changed": ["code/TheDiscDb.Client/Pages/Contribute/AddDisc.razor"],
                       "added": ["code/TheDiscDb.Client/Pages/Contribute/NewStep.razor",
                                 "code/TheDiscDb.Core/Validation/UpcAttribute.cs"],
                       "gone": ["code/TheDiscDb.Client/Pages/Contribute/IdentifyTitle.razor"]}
    assert "compare/abc1234...main" in report(changes, "abc1234")
    assert form_changes(recorded, recorded) == {"changed": [], "added": [], "gone": []}


def test_the_record_covers_their_form():
    """The record holds the pages the guide walks through: the release page, the disc, the log, the titles."""
    with open(RECORD, encoding="utf-8") as fh:
        rec = json.load(fh)
    assert rec["repo"] == REPO and len(rec["commit"]) == 40
    for page in ("ReleaseDetailInput.razor", "ReleaseDetailFormBindings.cs", "AddDisc.razor", "DiscUpload.razor",
                 "IdentifyDiscItems.razor", "IdentifyTitle.razor", "DiscScanner.cs"):
        assert "code/TheDiscDb.Client/Pages/Contribute/" + page in rec["files"]
    assert "code/TheDiscDb.Core/Validation/UpcAttribute.cs" in rec["files"]


@pytest.mark.dev
@pytest.mark.skipif(not WATCH, reason="set DISC_DETECTIVE_WATCH_DISCDB=1 (needs the internet)")
def test_their_form_is_as_the_guide_was_matched_to_it():
    with open(RECORD, encoding="utf-8") as fh:
        rec = json.load(fh)
    commit, now = their_form()
    changes = form_changes(rec["files"], now)
    assert not any(changes.values()), report(changes, rec["commit"])


if __name__ == "__main__" and sys.argv[1:] == ["--update"]:
    import datetime
    commit, files = their_form()
    with open(RECORD, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"repo": REPO, "commit": commit, "matched": datetime.date.today().isoformat(), "files": files},
                  fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("Recorded %d files of TheDiscDB's form at %s" % (len(files), commit[:7]))
