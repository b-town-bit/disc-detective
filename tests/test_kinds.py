"""Roles and label types as named values (disc_detective/kinds.py): stored and shown as the strings they were,
and the code naming them by name, so a misspelling fails where it's written."""
import ast
import json
import os
import sqlite3

from disc_detective import app, core
from disc_detective.kinds import TITLE_SEP, LineRole, Role, Type, join_title, no_mark, title_parts

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "disc_detective")


def test_a_kind_is_its_old_string():
    """Written, formatted, stored and compared just as the plain string was: case files and TheDiscDB's
    types read the same, on every Python we support (3.11 changed how a str enum formats)."""
    r = Role.PLAY_ALL
    assert r == "play-all" and Type("MainMovie") is Type.MAIN_MOVIE and Role("play-all") is r
    assert str(r) == "%s" % r == "{}".format(r) == f"{r}" == "play-all"
    assert json.dumps({"role": r, Type.PROMO: 1}) == '{"role": "play-all", "Promo": 1}'
    assert {"play-all": 1}[r] == 1 and {r: 1}["play-all"] == 1
    db = sqlite3.connect(":memory:")
    db.execute("create table t (x)")
    db.execute("insert into t values (?)", (Type.DELETED_SCENE,))
    assert db.execute("select x, typeof(x) from t").fetchone() == ("DeletedScene", "text")


def test_the_tables_name_every_kind():
    """Every type has its words and its place in Edit titles' order; every role its words in the report and
    the title picker; TheDiscDB's types are ours less Promo, our own (its data may have one more, not offered:
    BehindTheScenes, given words and a folder, a plain string)."""
    assert set(core.LABEL_TYPES) == set(Type) and set(core.DISCDB_ITEM_TYPES) == set(Type) - {Type.PROMO}
    assert set(app.FOUND_TYPES) == set(Type) and set(Type) <= set(app.TYPE_WORDS)   # (and TheDiscDB's BehindTheScenes)
    assert set(core.ROLE_WORDS) == set(app.TitlePicker.ROLE_TEXT) == set(Role)


def _role_side(e):
    s = ast.unparse(e)
    return s.endswith("['role']") or s.endswith("get('role')") or s in ("role", "scan_role")


def _type_side(e):
    s = ast.unparse(e)
    return s.endswith("['type']") or s.endswith("get('type')") or s == "kind"


def _strings(e):
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return [e.value]
    if isinstance(e, (ast.Tuple, ast.List, ast.Set)):
        return [v for x in e.elts for v in _strings(x)]
    return []


def spelt_out(path):
    """Roles and types written as strings where the code tells them apart or sets them: a title's role
    compared with "feature" or set to it, a label's type compared with "MainMovie" (or a menu line's role
    with "heading"), instead of Role.FEATURE, Type.MAIN_MOVIE, LineRole.HEADING."""
    roles, types, lines = {m.value for m in Role}, {m.value for m in Type}, {m.value for m in LineRole}
    found = []
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        said = []
        if isinstance(n, ast.Compare):
            sides = [n.left] + n.comparators
            for side in sides:
                if _role_side(side):
                    said += [(v, roles | lines) for other in sides for v in _strings(other)]
                if _type_side(side):
                    said += [(v, types) for other in sides for v in _strings(other)]
        elif isinstance(n, ast.Assign) and any(_role_side(t) for t in n.targets):
            said += [(v, roles) for v in _strings(n.value)]
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in ("role", "type"):
                    said += [(s, roles | lines if k.value == "role" else types) for s in _strings(v)]
        elif isinstance(n, ast.keyword) and n.arg in ("role", "type"):
            said += [(s, roles if n.arg == "role" else types) for s in _strings(n.value)]
        found += ["%s:%d %r" % (os.path.basename(path), n.lineno, v) for v, kinds in said if v in kinds]
    return found


def test_the_code_names_roles_and_types_by_name():
    """No role or type spelt out as a string where it's compared or set (Role.FEATURE, not "feature")."""
    found = []
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py") and name != "kinds.py":
            found += spelt_out(os.path.join(PKG, name))
    assert found == []


def test_a_titles_description_rides_after_its_mark_and_comes_apart_again():
    stored = join_title("The Hidden Secrets of Harry Potter", "Take a thrilling look back.")
    assert stored == "The Hidden Secrets of Harry Potter" + TITLE_SEP + "Take a thrilling look back."
    assert title_parts(stored) == ("The Hidden Secrets of Harry Potter", "Take a thrilling look back.")
    assert join_title("Deleted Scenes", "") == "Deleted Scenes" and title_parts("Deleted Scenes") == ("Deleted Scenes", "")
    assert title_parts(None) == ("", "") and title_parts("Name ‖") == ("Name", "")


def test_ocr_never_makes_the_mark():
    assert no_mark("MEN IN BLACK ‖") == "MEN IN BLACK II"


def test_a_labels_description_is_stored_with_its_name_and_read_apart(tmp_path):
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "D" * 32
    core.set_label(con, h, "00012.mpls", "Featurette", "The Hidden Secrets ‖ A look back.")
    core.set_label(con, h, "00013.mpls", "Featurette", "Conjuring a Scene ‖ ")
    got = core.get_labels(con, h)
    assert (got["00012.mpls"]["title"], got["00012.mpls"]["description"]) == ("The Hidden Secrets", "A look back.")
    assert got["00012.mpls"]["text"] == "The Hidden Secrets" + TITLE_SEP + "A look back."
    # (none after its mark: a label as it always was, its name alone)
    assert got["00013.mpls"] == {"type": "Featurette", "title": "Conjuring a Scene", "season": None, "episode": None}
