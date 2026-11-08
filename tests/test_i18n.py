"""Text by area, each its own language: the interface, the log, and the words looked for on a
disc (by the disc's language)."""
import json
import logging
import os
import struct

from disc_detective import core, i18n


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
