"""The translations' files, by area (disc_detective/i18n.py): each area's strings gathered from the code
into its template, a language's catalogs started from those, kept up to date with them, and made into
what the app reads. Run from the repository's root, with Babel installed (pip install -e ".[dev]"):

    python release/messages.py extract       # the code's strings -> disc_detective/locale/<area>.pot
    python release/messages.py new de        # a new language: locale/de/LC_MESSAGES/<area>.po
    python release/messages.py update        # every language's .po brought up to date with the templates
    python release/messages.py compile       # every .po -> its .mo (what the app reads)

Areas: interface (what the app and the command line show) and log. A string reaches an area's template
by the function it's written in: the keywords below, one list for both Babel and the tests."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "disc_detective")
LOCALE = os.path.join(PKG, "locale")


def shown(path):
    """A path to print: from the project's folder, else whole (another drive has no way there from it: a
    test's temporary folder on C:, GitHub's Windows runners checking the project out on D:)."""
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path

# Babel's keywords for each area (name:singular,plural for a counted one). Said goes to both: a message
# shown on the screen and written to the log, each in its own area's language.
KEYWORDS = {
    "interface": ["_", "ui", "ui_n:1,2", "N_", "Said"],
    "log": ["log", "log_n:1,2", "N_log", "Said"],
}


def version():
    with open(os.path.join(PKG, "core.py"), encoding="utf-8") as fh:
        return re.search(r'^VERSION = "([^"]+)"', fh.read(), re.M).group(1)


def babel(*args):
    from babel.messages.frontend import CommandLineInterface
    CommandLineInterface().run(["pybabel", "-q", *args])


def template(area):
    return os.path.join(LOCALE, "%s.pot" % area)


def languages():
    if not os.path.isdir(LOCALE):
        return []
    return sorted(d for d in os.listdir(LOCALE) if os.path.isdir(os.path.join(LOCALE, d, "LC_MESSAGES")))


def _without_date(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [ln for ln in fh if not ln.startswith('"POT-Creation-Date:')]
    except OSError:
        return None


def wanted(msgid):
    """A string to translate: not blank ("  ", an indent put before a log line, is gathered with it)."""
    return bool((msgid[0] if isinstance(msgid, tuple) else msgid).strip())


def _keep_wanted(path):
    from babel.messages.pofile import read_po, write_po
    with open(path, "rb") as fh:
        cat = read_po(fh)
    for m in list(cat):
        if m.id and not wanted(m.id):
            cat.delete(m.id)
    with open(path, "wb") as fh:
        write_po(fh, cat, include_lineno=False)


def extract():
    """Each area's template from the code; one whose strings haven't changed is left as it is (its date too),
    so a template changes in a commit only when its strings do."""
    os.makedirs(LOCALE, exist_ok=True)
    for area, keywords in KEYWORDS.items():
        out = template(area)
        new = out + ".new"
        args = ["extract", "--no-default-keywords", "--add-comments=TRANSLATORS:", "--add-location=file",
                "--project=Disc Detective", "--version=" + version(), "--copyright-holder=Brady Field",
                "--msgid-bugs-address=https://github.com/b-town-bit/disc-detective/issues", "-o", new]
        for k in keywords:
            args += ["-k", k]
        babel(*args, os.path.relpath(PKG, ROOT))
        _keep_wanted(new)
        if _without_date(new) == _without_date(out):
            os.remove(new)
        else:
            os.replace(new, out)
            print("updated", shown(out))


def new(lang):
    for area in KEYWORDS:
        babel("init", "-D", area, "-i", template(area), "-d", LOCALE, "-l", lang)
        print("started", shown(os.path.join(LOCALE, lang, "LC_MESSAGES", area + ".po")))


def update():
    for lang in languages():
        for area in KEYWORDS:
            if os.path.exists(os.path.join(LOCALE, lang, "LC_MESSAGES", area + ".po")):
                babel("update", "-D", area, "-i", template(area), "-d", LOCALE, "-l", lang)


def compile_all():
    for lang in languages():
        for area in KEYWORDS:
            if os.path.exists(os.path.join(LOCALE, lang, "LC_MESSAGES", area + ".po")):
                babel("compile", "-D", area, "-d", LOCALE, "-l", lang, "--statistics")


def main(argv):
    if argv[:1] == ["extract"]:
        extract()
    elif argv[:1] == ["new"] and len(argv) == 2:
        new(argv[1])
    elif argv[:1] == ["update"]:
        update()
    elif argv[:1] == ["compile"]:
        compile_all()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
