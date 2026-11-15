"""Text in other languages, by area, each its own files and its own language:

- interface: what the app shows (status lines, the investigation's steps, dialogs, menus);
- log: what the log says, for whoever runs the app from source and debugs it (its language can
  differ from the interface's: a German interface, an English log to compare bug reports with);
- disc words: the words looked for on a disc's menus ("Play", "Scene Selection", "Theatrical
  Trailer"), by the disc's language, not the interface's (a German disc says "Hauptmenü"
  whatever the app's language): word lists, not translations of one another.

The interface and the log are gettext domains: locale/<lang>/LC_MESSAGES/interface.mo, log.mo
(made from .po files by release/messages.py, which also gathers each area's strings from the code
into its template, locale/interface.pot and locale/log.pot: see CONTRIBUTING's "Translating"). Without them, or for a string they don't have, the
English stands. Strings in the code are written in English, marked where they're defined with
N_() (N_log() for the log) when they're shown elsewhere (core.STEPS), and put through ui() / log() where they're shown,
{named} placeholders filled in after translating (a translation can move them)."""
import functools
import gettext
import json
import os

LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
DISC_WORDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "disc_words")
DOMAINS = ("interface", "log")

# each area's language when none is set: the interface the system's, the log English (bug reports stay
# readable to whoever reads them)
DEFAULTS = {"interface": None, "log": "en"}
_languages = dict(DEFAULTS)                  # None: the system's language, else English


def set_language(domain, lang):
    """Show one area in a language ("de"); None for the area's default (DEFAULTS)."""
    if domain not in DOMAINS:
        raise ValueError("no such area: %s" % domain)
    _languages[domain] = lang or DEFAULTS[domain]
    _catalog.cache_clear()


def system_languages():
    """The system's languages, most wanted first ("de_DE"): gettext's own environment variables when
    they're set (Linux, macOS from a terminal); otherwise Windows' display language, which Windows doesn't
    put in them. None: gettext's own answer (English, with none)."""
    if any(os.environ.get(v) for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")):
        return None
    if os.name == "nt":
        try:
            import ctypes
            import locale
            code = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            return [code] if code else None
        except (AttributeError, OSError):
            return None
    return None


@functools.lru_cache(maxsize=None)
def _catalog(domain):
    lang = _languages[domain]
    return gettext.translation(domain, LOCALE_DIR, languages=[lang] if lang else system_languages(),
                               fallback=True)


def _fill(text, kw, english):
    """text with its {placeholders} filled; a translation that names one the English doesn't have (or
    breaks a brace) gives the English instead, rather than an error in the middle of a scan."""
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return english.format(**kw)


def ui(msg, /, **kw):
    """msg as the interface shows it: translated, {placeholders} filled from kw."""
    return _fill(_catalog("interface").gettext(msg), kw, msg)


def ui_n(singular, plural, n, /, **kw):
    """One or several ("1 title" / "{n} titles"), as the interface's language counts; n is a
    placeholder too."""
    return _fill(_catalog("interface").ngettext(singular, plural, n), dict(kw, n=n), singular if n == 1 else plural)


def log(msg, /, **kw):
    """msg as the log says it: translated, {placeholders} filled from kw."""
    return _fill(_catalog("log").gettext(msg), kw, msg)


def log_n(singular, plural, n, /, **kw):
    """One or several, as the log's language counts; n is a placeholder too."""
    return _fill(_catalog("log").ngettext(singular, plural, n), dict(kw, n=n), singular if n == 1 else plural)


def N_(msg):
    """A string to translate where it's shown, not where it's written (marked for the interface's template)."""
    return msg


def N_log(msg):
    """N_ for the log: a line written down here, translated where it's logged (the log's template)."""
    return msg


class Said(str):
    """A message bound for both the screen and the log, each in its own area's language: as a str, the
    English (filled in); .ui() as the interface says it, .log() as the log does. For messages handed to a
    callback that may print them, log them, or both (core's progress messages)."""

    def __new__(cls, msg, /, **kw):
        self = str.__new__(cls, _fill(msg, kw, msg))
        self.msg, self.kw = msg, kw
        return self

    def ui(self):
        return ui(self.msg, **self.kw)

    def log(self):
        return log(self.msg, **self.kw)


def for_ui(text):
    """text as the interface shows it: a Said translated, anything else as it is."""
    return text.ui() if isinstance(text, Said) else text


def for_log(text):
    """text as the log says it: a Said translated, anything else as it is."""
    return text.log() if isinstance(text, Said) else text


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
