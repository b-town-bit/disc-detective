"""Text by area, each its own language: the interface, the log, and the words looked for on a
disc (by the disc's language)."""
import json
import logging
import os
import re
import struct

from disc_detective import core, i18n

REAL_LOG = core.log          # (before conftest's stand-in: the real one, for the test of it)


def write_mo(path, entries, plural="nplurals=2; plural=(n != 1);"):
    """A gettext catalog (.mo) of entries: {msgid: msgstr}, or {(singular, plural): [forms]}."""
    cat = {"": "Content-Type: text/plain; charset=UTF-8\nPlural-Forms: %s\n" % plural}
    for k, v in entries.items():
        cat["\0".join(k) if isinstance(k, tuple) else k] = "\0".join(v) if isinstance(v, list) else v
    keys = sorted(cat)
    ids = b"".join(k.encode() + b"\0" for k in keys)
    strs = b"".join(cat[k].encode() + b"\0" for k in keys)
    n = len(keys)
    start = 28 + 16 * n
    table_ids, table_strs, o = [], [], 0
    for k in keys:
        table_ids.append((len(k.encode()), start + o))
        o += len(k.encode()) + 1
    o = 0
    for k in keys:
        table_strs.append((len(cat[k].encode()), start + len(ids) + o))
        o += len(cat[k].encode()) + 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<7I", 0x950412de, 0, n, 28, 28 + 8 * n, 0, 0))
        for length, off in table_ids + table_strs:
            fh.write(struct.pack("<2I", length, off))
        fh.write(ids + strs)


def test_each_area_its_own_language(tmp_path, monkeypatch, caplog):
    """A German interface, the log still English (a dev comparing it with bug reports); then
    the log German too. A string a catalog doesn't have stays English; counts follow the
    language's own plural forms; {placeholders} are filled after translating."""
    locale = tmp_path / "locale"
    write_mo(str(locale / "de" / "LC_MESSAGES" / "interface.mo"),
             {"Looking through the mug book": "Im Verbrecheralbum blättern",
              ("{n} title", "{n} titles"): ["{n} Titel", "{n} Titel"],
              "Saved {what}": "{what} gespeichert"})
    write_mo(str(locale / "de" / "LC_MESSAGES" / "log.mo"),
             {"reading the disc's menus (nothing else named it)": "die Menüs der Disc lesen"})
    monkeypatch.setattr(i18n, "LOCALE_DIR", str(locale))
    shown = []
    monkeypatch.setattr(core, "status_hook", shown.append)
    try:
        i18n.set_language("interface", "de")
        i18n.set_language("log", "en")
        with caplog.at_level(logging.INFO, logger="disc_detective"):
            core.step("menus")
        assert shown == ["Im Verbrecheralbum blättern…"]
        assert "reading the disc's menus (nothing else named it)" in caplog.text     # (the log: English)
        assert i18n.ui_n("{n} title", "{n} titles", 3) == "3 Titel"
        assert i18n.ui("Saved {what}", what="Rush Hour") == "Rush Hour gespeichert"
        assert i18n.ui("Not right?") == "Not right?"                                 # (not translated: English)
        i18n.set_language("log", "de")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="disc_detective"):
            core.step("menus")
        assert "die Menüs der Disc lesen" in caplog.text
    finally:
        for area in i18n.DOMAINS:
            i18n.set_language(area, None)
    assert i18n.ui("Looking through the mug book") == "Looking through the mug book"


def test_a_progress_message_in_each_areas_language(tmp_path, monkeypatch, caplog, capsys):
    """One of core's progress messages (core.log, Said): the screen in the interface's language, the log in
    the log's (English, unless set); the command line's say() in the interface's; a plain str as it is."""
    locale = tmp_path / "locale"
    write_mo(str(locale / "de" / "LC_MESSAGES" / "interface.mo"),
             {"downloading IMDb dataset {name} ...": "IMDb-Datensatz {name} wird geladen ..."})
    monkeypatch.setattr(i18n, "LOCALE_DIR", str(locale))
    shown = []
    monkeypatch.setattr(core, "status_hook", lambda msg, tone="info": shown.append(msg))
    monkeypatch.setattr(core, "log", REAL_LOG)
    try:
        i18n.set_language("interface", "de")
        i18n.set_language("log", "en")
        msg = i18n.Said("downloading IMDb dataset {name} ...", name="title.basics")
        assert msg == "downloading IMDb dataset title.basics ..."                    # (as a str: the English)
        # (a placeholder may have any name, the message's own parameter's too)
        assert i18n.log("failed: {msg}", msg="x") == i18n.ui("failed: {msg}", msg="x") == "failed: x"
        with caplog.at_level(logging.INFO, logger="disc_detective"):
            core.log(msg)
            core.log("plain words")
        assert shown == ["IMDb-Datensatz title.basics wird geladen ...", "plain words"]
        assert "downloading IMDb dataset title.basics ..." in caplog.text and "Datensatz" not in caplog.text
        core.say(msg)
        assert capsys.readouterr().out == "IMDb-Datensatz title.basics wird geladen ...\n"
    finally:
        for area in i18n.DOMAINS:
            i18n.set_language(area, None)


APP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "disc_detective", "app.py")
UI_KEYWORDS = {"text", "label", "title", "message", "detail"}
UI_CALLS = {"set_status": 1, "tell": 1, "set_outcome": 1, "set_background": 1, "detect_outcome": 1, "show_outcome": 1,
            "clear_case": 2, "Tooltip": 2, "link": 2, "showinfo": 2, "showerror": 2, "showwarning": 2, "askyesno": 2}


def english_in_ui(path=APP_PY, keywords=UI_KEYWORDS, calls=UI_CALLS, functions=None, names=()):
    """Words written straight into the interface, not put through _(): a string with letters (or one
    %-formatted) given as a widget's text, a window's title, a status line, a message box's words (or, for
    the command line, functions: only in those, its printed lines and --help). names: words that stay."""
    import ast
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    if functions:
        tree = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions],
                          type_ignores=[])
    found = []

    def check(node, where):
        if isinstance(node, (ast.IfExp,)):
            check(node.body, where)
            check(node.orelse, where)
        elif isinstance(node, ast.Lambda):
            check(node.body, where)
        elif isinstance(node, ast.BoolOp):
            for v in node.values:
                check(v, where)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            check(node.left, where)
            check(node.right, where)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant):
            check(node.left, where)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r"[A-Za-z]{2,}", node.value)                 and node.value not in names:
            found.append("%d %s: %r" % (node.lineno, where, node.value[:60]))

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for k in n.keywords:
            if k.arg in keywords:
                check(k.value, k.arg)
        if fn in calls:
            first = 1 if fn in ("Tooltip", "link") else 0      # (after the widget it's for)
            for a in n.args[first:first + calls[fn]]:
                check(a, fn)
    return found


def test_the_interface_puts_its_words_through_translation():
    """Every word app.py shows goes through _() (or ui_n, or N_ where it's written and _ where it's shown):
    nothing in English straight onto the screen. Names stay as they are (the app's, a program's, a site's),
    as constants or data, not as string literals here."""
    assert english_in_ui() == []


CLI_FUNCTIONS = {"print_report", "list_library", "list_submission_candidates", "list_improvement_candidates",
                 "packet_checklist", "make_parser", "_main", "set_field", "import_samples", "resolve_hash"}


def test_the_command_line_puts_its_words_through_translation():
    """The command line is interface too: its report, its lists, the submission checklist and --help say
    their words through ui() (core's own name for it: core uses _ as a throwaway name)."""
    core_py = os.path.join(os.path.dirname(APP_PY), "core.py")
    found = english_in_ui(core_py, keywords={"help"}, functions=CLI_FUNCTIONS, names={"TheDiscDB"},
                          calls={"print": 1, "p": 1, "top": 2, "more": 1, "fact": 2, "error": 1, "append": 1,
                                 "SystemExit": 1})
    assert found == []


def english_in_log(path):
    """Log lines written in English straight to the log, not through i18n.log: a logger call (logger.info(...),
    di.logger.warning(...)) or a log= callback given a string with letters, or one %-formatted or joined to one."""
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    levels = {"info", "warning", "error", "exception", "debug", "critical"}

    def logger_call(f):
        if isinstance(f, ast.IfExp):
            return logger_call(f.body) and logger_call(f.orelse)
        return isinstance(f, ast.Attribute) and f.attr in levels and "logger" in ast.unparse(f.value)

    def english(node):
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and bool(re.search(r"[A-Za-z]{2,}", node.value))
        if isinstance(node, ast.BinOp):
            return english(node.left) or english(node.right)
        if isinstance(node, ast.IfExp):
            return english(node.body) or english(node.orelse)
        if isinstance(node, ast.JoinedStr):
            return True
        return False
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args and (logger_call(n.func) or (isinstance(n.func, ast.Name)
                                                                            and n.func.id == "log")):
            if english(n.args[0]):
                found.append("%s:%d" % (os.path.basename(path), n.lineno))
    return found


def test_the_log_puts_its_words_through_translation():
    """Every log line goes through i18n.log (English unless the log's language is set: bug reports stay readable)."""
    here = os.path.dirname(APP_PY)
    found = []
    for name in ("app.py", "core.py", "menus.py", "makemkv.py", "bdcrawl.py", "bdhdmv.py", "tools.py"):
        found += english_in_log(os.path.join(here, name))
    assert found == []


def test_a_function_never_hides_the_translator():
    """No function in the interface's modules names a variable _: it would hide _() all through it."""
    import ast
    for name in ("app.py", "tools.py"):
        tree = ast.parse(open(os.path.join(os.path.dirname(APP_PY), name), encoding="utf-8").read())
        for f in ast.walk(tree):
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                names = {n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                names |= {a.arg for a in ast.walk(f) if isinstance(a, ast.arg)}
                assert "_" not in names, "%s, line %d" % (name, f.lineno)


def test_disc_words_by_the_discs_language(tmp_path, monkeypatch):
    """The words looked for on a disc's menus, by its language: a German disc's own list; a
    language without one, English's; none at all yet, nothing (the code's own lists stand)."""
    words = tmp_path / "disc_words"
    words.mkdir()
    (words / "en.json").write_text(json.dumps({"nav": ["play", "menu"]}), encoding="utf-8")
    (words / "de.json").write_text(json.dumps({"nav": ["wiedergabe", "hauptmenü"]}), encoding="utf-8")
    monkeypatch.setattr(i18n, "DISC_WORDS_DIR", str(words))
    i18n.disc_words.cache_clear()
    try:
        assert i18n.disc_words("de")["nav"] == ["wiedergabe", "hauptmenü"]
        assert i18n.disc_words("fr")["nav"] == ["play", "menu"]
        monkeypatch.setattr(i18n, "DISC_WORDS_DIR", str(tmp_path / "none"))
        i18n.disc_words.cache_clear()
        assert i18n.disc_words("de") == {}
    finally:
        i18n.disc_words.cache_clear()
