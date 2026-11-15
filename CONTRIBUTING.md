# Contributing

Thanks for wanting to help. Bug reports, disc reports and ideas are welcome as issues.

## Code

Disc Detective is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE),
owned by Brady Field. Before a pull request can be merged, its author signs the
[contributor agreement](CLA.md) once: a bot asks you on your first pull request, and you
sign by commenting on it with the sentence it gives you. It covers all your later
contributions too.

The agreement lets the project keep the freedom to license Disc Detective in other ways in
the future. Please read it before you sign.

## Building the download-and-run package

`release/build.py` builds it for the system it runs on (PyInstaller; one folder, zipped), gathers
the licence of every package it bundles into `licenses/`, and runs the built program's
self-check (`--self-check`: its modules, files, RapidOCR, OpenCV and a window). In a Python 3.14
environment, from the repository's root:

```
pip install ".[windows-ocr]" pyinstaller     # on macOS or Linux: pip install . pyinstaller
python release/build.py
```

The zip lands in `dist/`. CI's **Build** workflow does the same on all three systems, and a
version tag makes a draft release with the three zips.

Between releases, `VERSION` in `disc_detective/core.py` names the next one as a development
version (`0.4.dev0`), so a copy from source never passes for a release. To release, set it to the
release's own (`0.4`), merge that, then tag `v0.4` on main; a tag that doesn't match `VERSION`
stops the build. After publishing, set it to the next development version (`0.5.dev0`).

A package added to the app that ships
no licence file stops the build: put its licence's text in `release/licenses` and name it in
`NO_FILE` in `release/build.py`.

## Looking after Linux or macOS

Disc Detective is built and tested by hand on Windows. On Linux and macOS the automated tests
pass, but nobody here can try it with a real disc drive, MakeMKV or VLC, so those two are
previews. If you use one of them and would like to look after it, open an issue and say so.
It would mean:

- trying each release on your system with a real drive (a DVD and a Blu-ray, a scan and a rip);
- taking that system's bug reports (they're labelled `linux` or `macos`): reproducing them and
  saying what you find, or fixing them;
- reviewing pull requests that change how the app finds drives, MakeMKV or VLC on your system.

The CI run's `screenshots-Linux` and `screenshots-macOS` artifacts show how the app's windows
look on each system, which helps with layout problems.

## Translating

Disc Detective's text comes in two parts, and each has its own files:

- **interface**: everything the app and the command line show. This is the part to translate first.
- **log**: the technical log behind *Case notes*. It stays in English unless someone sets a log
  language, because bug reports need a log the developers can read. Translating it is welcome but
  optional.

The words the app looks for on a disc's menus ("Play", "Scene Selection") are not part of these
files. Those depend on the disc's language, not the app's, and they'll get their own lists later.

### Starting a language

Install the developer tools (`pip install -e ".[dev]"`), then, from the repository's root:

```
python release/messages.py new de        # your language's code: de, fr, pt_BR...
```

That creates `disc_detective/locale/de/LC_MESSAGES/interface.po` and `log.po`. Translate them in
any .po editor (Poedit, for example) or a text editor. Each `msgid` is the English; your
translation goes in the `msgstr` under it. An empty `msgstr` shows the English, so you can
translate a bit at a time.

To see your work in the app:

```
python release/messages.py compile       # makes the .mo files the app reads
LANGUAGE=de python -m disc_detective     # PowerShell: $env:LANGUAGE = "de"; python -m disc_detective
```

Without `LANGUAGE`, the app uses the system's language. To try the log in your language as well,
add `"log_language": "de"` to `settings.json` in the app's data folder.

### What to keep as it is

- **Placeholders in braces**, such as `{title}`, `{n}` or `{seconds:.0f}`. Copy them exactly. You can
  move them anywhere in the sentence, and a singular form may leave one out ("one title" instead of
  "{n} title"). A placeholder that isn't in the English won't work: the app shows the English
  instead, and the tests fail.
- **Plurals.** A counted message has a `msgstr[0]`, `msgstr[1]` and so on, one for each form your
  language uses. `new` sets the number of forms for your language.
- **Names.** Leave the app's name, the names of sites and programs (TheDiscDB, IMDb, MakeMKV, VLC,
  Plex) and Plex's folder names (Featurettes, Behind The Scenes) in English. Plex only recognises
  those folder names in English. Most of them aren't in the files anyway.
- **Length.** Keep it short. Many lines sit on buttons or in narrow columns.

### The detective wording

Much of the interface talks like a detective ("Dusting for fingerprints", "Looking through the mug
book"). Keep the theme if it works in your language, or use plain wording if it doesn't. That's
up to you. Being clear matters more than the joke.

### When the English changes

When code adds or changes a string, the person changing it runs `python release/messages.py
extract`, which updates the templates (`disc_detective/locale/interface.pot` and `log.pot`). A test
fails until they do. To bring your language up to date with the templates afterwards:

```
python release/messages.py update        # new strings come in empty; changed ones are marked fuzzy
python release/messages.py compile
```

A string marked fuzzy has had its English changed since you translated it. The app shows the
English until you check your translation and remove the fuzzy mark.

Commit the `.po` and `.mo` files together. A test checks that each `.mo` matches its `.po`.

In the code, interface text goes through `_()` (`ui()` in `core.py`), or `ui_n()` when it's
counted. Log lines go through `i18n.log()` or `i18n.log_n()`. Text written in one place and shown
in another is marked with `N_()` for the interface or `N_log()` for the log. A message for both
uses `i18n.Said`. Babel gathers the strings by these names, so a string written any other way
won't reach translators. Babel also skips a translating call wrapped in extra parentheses inside
another `log(...)` call, and a test catches it. A comment starting `# TRANSLATORS:` on the line
before a string is passed on to translators.

## Before you open a pull request

- Run the tests: `pip install -e ".[dev]"`, then `pytest -q`.
- Changed or added a string? Run `python release/messages.py extract`.
- Keep personal disc data out of the repository (your discs' logs, menu pictures, packets).
- Never add decryption keys, key lists or decryption libraries, or links to them.
