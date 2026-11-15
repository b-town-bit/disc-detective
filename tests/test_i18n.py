"""Text by area, each its own language: the interface, the log, and the words looked for on a
disc (by the disc's language)."""
import json
import logging
import os
import re
import struct

import pytest

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
    di.logger.warning(...)), a log= callback or a crawler's self.log given a string with letters, or one
    %-formatted or joined to one."""
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
        if not (isinstance(n, ast.Call) and n.args):
            continue
        own_log = (isinstance(n.func, ast.Name) and n.func.id == "log") or (      # (a log= callback, a crawler's own)
            isinstance(n.func, ast.Attribute) and n.func.attr == "log" and ast.unparse(n.func.value) != "i18n")
        if logger_call(n.func) or own_log:
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


def _messages():
    """release/messages.py (the translations' files: Babel's keywords for each area, its commands)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(APP_PY)), "release", "messages.py")
    spec = importlib.util.spec_from_file_location("messages", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _extracted(messages):
    """{area: {msgid}} as Babel gathers it from the code now (a counted string by its singular)."""
    from babel.messages.extract import extract_from_dir
    from babel.messages.frontend import parse_keywords
    found = {}
    for area, keywords in messages.KEYWORDS.items():
        found[area] = {m[0] if isinstance(m, tuple) else m
                       for _f, _ln, m, _c, _ctx in extract_from_dir(messages.PKG, keywords=parse_keywords(keywords))
                       if messages.wanted(m)}
    return found


def test_babel_gathers_each_string_into_its_areas_template():
    """Every string written in a translating call (_, ui, ui_n, N_, i18n.log, log_n, N_log, Said) is in its area's
    template as Babel gathers it: none missed by a way of writing the call Babel doesn't see; a log line not
    in the interface's, a Said in both. And the templates in the repository are the code's (run
    `python release/messages.py extract` after changing a string)."""
    import ast
    pytest.importorskip("babel")
    from babel.messages.pofile import read_po
    messages = _messages()
    found = _extracted(messages)
    areas = {"_": ("interface",), "ui": ("interface",), "ui_n": ("interface",), "N_": ("interface",),
             "log": ("log",), "log_n": ("log",), "N_log": ("log",), "Said": ("interface", "log")}
    for name in sorted(os.listdir(messages.PKG)):
        if not name.endswith(".py"):
            continue
        for n in ast.walk(ast.parse(open(os.path.join(messages.PKG, name), encoding="utf-8").read())):
            if not (isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str) and messages.wanted(n.args[0].value)):
                continue
            fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for area in areas.get(fn, ()):
                assert n.args[0].value in found[area], "%s:%d not in %s" % (name, n.lineno, area)
    assert "reading the disc's menus (nothing else named it)" in found["log"]
    assert "reading the disc's menus (nothing else named it)" not in found["interface"]
    assert "Looking through the mug book" in found["interface"]
    assert "downloading IMDb dataset {name} ..." in found["interface"] & found["log"]
    for area in messages.KEYWORDS:
        with open(messages.template(area), "rb") as fh:
            have = {m.id[0] if isinstance(m.id, tuple) else m.id for m in read_po(fh) if m.id}
        assert have == found[area], "%s.pot is out of date: python release/messages.py extract" % area


def placeholders(text):
    import string
    return {f for _t, f, _s, _c in string.Formatter().parse(text) if f is not None}


def test_a_language_from_its_template_to_the_screen(tmp_path, monkeypatch):
    """A new language started from the templates, a line translated, compiled, and shown: the commands a
    translator runs (release/messages.py new, compile), end to end."""
    import shutil
    pytest.importorskip("babel")
    from babel.messages.pofile import read_po, write_po
    messages = _messages()
    locale = tmp_path / "locale"
    locale.mkdir()
    for area in messages.KEYWORDS:
        shutil.copy(messages.template(area), str(locale))
    monkeypatch.setattr(messages, "LOCALE", str(locale))
    messages.new("de")
    po = locale / "de" / "LC_MESSAGES" / "interface.po"
    with open(str(po), "rb") as fh:
        cat = read_po(fh)
    cat["Looking through the mug book"].string = "Im Verbrecheralbum blättern"
    with open(str(po), "wb") as fh:
        write_po(fh, cat)
    assert catalog_problems(messages) == ["de/interface.mo is out of date: python release/messages.py compile",
                                          "de/log.mo is out of date: python release/messages.py compile"]
    messages.compile_all()
    assert catalog_problems(messages) == []
    cat["{n} episode"].string = ("{n} Folge", "")               # (a form left empty: the English, compiled)
    cat["Saved: attach {file} in the chat"].string = "Gespeichert: {datei} anhängen"
    with open(str(po), "wb") as fh:
        write_po(fh, cat)
    assert catalog_problems(messages) == ["de/interface.po: a placeholder not in 'Saved: attach {file} in the chat'",
                                          "de/interface.mo is out of date: python release/messages.py compile"]
    cat["Saved: attach {file} in the chat"].string = "Gespeichert: {file} anhängen"
    with open(str(po), "wb") as fh:
        write_po(fh, cat)
    messages.compile_all()
    assert catalog_problems(messages) == []
    monkeypatch.setattr(i18n, "LOCALE_DIR", str(locale))
    try:
        i18n.set_language("interface", "de")
        assert i18n.ui("Looking through the mug book") == "Im Verbrecheralbum blättern"
    finally:
        i18n.set_language("interface", None)


def test_a_broken_translation_shows_the_english(tmp_path, monkeypatch):
    """A translation naming a placeholder the English doesn't have (or a stray brace) shows the English,
    filled in, rather than stopping a scan with an error."""
    locale = tmp_path / "locale"
    write_mo(str(locale / "de" / "LC_MESSAGES" / "interface.mo"),
             {"Saved {what}": "{was} gespeichert", "{n} title": "{n} Titel {",
              ("{n} title", "{n} titles"): ["{anzahl} Titel", "{anzahl} Titel"]})
    monkeypatch.setattr(i18n, "LOCALE_DIR", str(locale))
    try:
        i18n.set_language("interface", "de")
        assert i18n.ui("Saved {what}", what="Rush Hour") == "Saved Rush Hour"
        assert i18n.ui_n("{n} title", "{n} titles", 3) == "3 titles"
    finally:
        i18n.set_language("interface", None)


def catalog_problems(messages):
    """What's wrong with the languages' catalogs under messages.LOCALE: a translation naming a placeholder its
    English doesn't have (leaving one out is fine: "one title"), or a .mo not made from its .po as it is now."""
    import gettext
    from babel.messages.pofile import read_po
    problems = []
    for lang in messages.languages():
        for area in messages.KEYWORDS:
            po = os.path.join(messages.LOCALE, lang, "LC_MESSAGES", area + ".po")
            if not os.path.exists(po):
                continue
            with open(po, "rb") as fh:
                cat = read_po(fh)
            want = {}
            for m in cat:
                if not m.id or m.fuzzy:
                    continue
                ids = m.id if isinstance(m.id, tuple) else (m.id,)
                strs = m.string if isinstance(m.string, tuple) else (m.string,)
                english = set().union(*(placeholders(i) for i in ids))
                if any(not placeholders(t) <= english for t in strs):
                    problems.append("%s/%s.po: a placeholder not in %r" % (lang, area, ids[0]))
                if isinstance(m.id, tuple):     # (as Babel compiles a counted one: a form left empty, the English)
                    want.update({(ids[0], k): t or ids[min(k, 1)] for k, t in enumerate(strs)})
                elif strs[0]:
                    want[ids[0]] = strs[0]
            try:
                with open(po[:-3] + ".mo", "rb") as fh:
                    have = gettext.GNUTranslations(fh)._catalog
            except OSError:
                have = None
            if have is not None:
                have.pop("", None)
            if have != want:
                problems.append("%s/%s.mo is out of date: python release/messages.py compile" % (lang, area))
    return problems


def test_each_translation_keeps_its_placeholders_and_is_compiled():
    """Every language's catalogs in the repository: placeholders kept, each .mo made from its .po as it is."""
    pytest.importorskip("babel")
    assert catalog_problems(_messages()) == []


def test_the_log_english_and_the_interface_the_systems_unless_set(tmp_path, monkeypatch):
    """With no language set: the interface in the system's (a German system, German), the log in English
    all the same; on Windows, which says its language elsewhere, its display language (one gettext knows)."""
    locale = tmp_path / "locale"
    for area in i18n.DOMAINS:
        write_mo(str(locale / "de" / "LC_MESSAGES" / ("%s.mo" % area)), {"Not right?": "Nicht richtig?"})
    monkeypatch.setattr(i18n, "LOCALE_DIR", str(locale))
    for v in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("LANGUAGE", "de_DE")
    try:
        for area in i18n.DOMAINS:
            i18n.set_language(area, None)
        assert i18n.ui("Not right?") == "Nicht richtig?"
        assert i18n.log("Not right?") == "Not right?"
        i18n.set_language("log", "de")
        assert i18n.log("Not right?") == "Nicht richtig?"
        assert i18n.system_languages() is None                       # (gettext's own, from the variable)
        monkeypatch.delenv("LANGUAGE")
        found = i18n.system_languages()
        assert found is None or (len(found) == 1 and re.fullmatch(r"[a-z]{2,3}(_[A-Za-z0-9]+)*", found[0]))
    finally:
        for area in i18n.DOMAINS:
            i18n.set_language(area, None)
