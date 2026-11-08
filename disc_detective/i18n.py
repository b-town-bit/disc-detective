"""Text in other languages, by area, each its own files and its own language:

- interface: what the app shows (status lines, the investigation's steps, dialogs, menus);
- log: what the log says, for whoever runs the app from source and debugs it (its language can
  differ from the interface's: a German interface, an English log to compare bug reports with);
- disc words: the words looked for on a disc's menus ("Play", "Scene Selection", "Theatrical
  Trailer"), by the disc's language, not the interface's (a German disc says "Hauptmenü"
  whatever the app's language): word lists, not translations of one another.

The interface and the log are gettext domains: locale/<lang>/LC_MESSAGES/interface.mo, log.mo
(made from .po files with msgfmt or pybabel). Without them, or for a string they don't have, the
English stands. Strings in the code are written in English, marked where they're defined with
N_() when they're shown elsewhere (core.STEPS), and put through ui() / log() where they're shown,
{named} placeholders filled in after translating (a translation can move them)."""
import functools
import gettext
import json
import os

LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
DISC_WORDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "disc_words")
DOMAINS = ("interface", "log")

_languages = {d: None for d in DOMAINS}      # None: the system's language, else English


def set_language(domain, lang):
    """Show one area in a language ("de"); None for the system's (falling back to English)."""
    if domain not in DOMAINS:
        raise ValueError("no such area: %s" % domain)
    _languages[domain] = lang or None
    _catalog.cache_clear()


@functools.lru_cache(maxsize=None)
def _catalog(domain):
    lang = _languages[domain]
    return gettext.translation(domain, LOCALE_DIR, languages=[lang] if lang else None, fallback=True)


def _fill(text, kw):
    return text.format(**kw) if kw else text


def ui(msg, **kw):
    """msg as the interface shows it: translated, {placeholders} filled from kw."""
    return _fill(_catalog("interface").gettext(msg), kw)


def ui_n(singular, plural, n, **kw):
    """One or several ("1 title" / "{n} titles"), as the interface's language counts; n is a
    placeholder too."""
    return _fill(_catalog("interface").ngettext(singular, plural, n), dict(kw, n=n))


def log(msg, **kw):
    """msg as the log says it: translated, {placeholders} filled from kw."""
    return _fill(_catalog("log").gettext(msg), kw)


def N_(msg):
    """A string to translate where it's shown, not where it's written (marked for extraction)."""
    return msg


@functools.lru_cache(maxsize=None)
def disc_words(lang="en"):
    """The words looked for on a disc's menus in its language ({list name: [words]}), from
    disc_words/<lang>.json; English's for a language without a list of its own (or {} when there's
    none at all yet: the code's own lists stand)."""
    for code in (lang, "en"):
        path = os.path.join(DISC_WORDS_DIR, "%s.json" % code)
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return {}
