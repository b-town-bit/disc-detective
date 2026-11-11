<p align="center"><img src="disc_detective/icon.png" width="64" alt=""></p>

# Disc Detective

Put a disc in. Disc Detective works out **which movie or show it is**, and **what
every title on the disc is**: the feature, each episode in order, play-all
titles, duplicates, alternate versions and extras. It then names them for Plex.

It never reads the video, so a scan takes seconds.

## How it cracks the case

Cheapest clue first, stopping as soon as the answer is solid:

1. **Fingerprint.** The sizes of the disc's video files form a fingerprint
   (TheDiscDB's ContentHash). Every pressing of a disc has its own.
2. **TheDiscDB.** Thousands of discs are already catalogued there, down to which
   title is which episode. A match usually closes the case.
3. **Your library.** Discs you've scanned and confirmed are recognized instantly,
   even offline.
4. **The disc's structure.** Titles, lengths, chapters, audio and subtitle tracks
   tell a movie from a TV disc from a short-film collection, and find the
   episodes, play-alls, duplicates and extras.
5. **The disc's own labels.** Volume label (`SMALLVILLE_SEASON_10_DISC_1`,
   `AN_AMERICAN_TAIL_2`), Blu-ray title metadata, mastering date, cover art.
6. **IMDb.** Candidates ranked by name, runtime, release year and popularity,
   with the IMDb id Plex uses.
7. **TVmaze.** For TV discs, the season's episode list, in DVD order when the
   DVDs differ from broadcast (Firefly). Episode lengths place a disc in its season,
   so a short last disc or a two-part finale stored as one title gets the right
   episodes. Files are named in aired order (Plex's default) or DVD order (Settings).

The app then shows the poster or box art so you can confirm at a glance.

## Install

**Windows** is fully supported. **Linux** and **macOS** are previews: the tests run on both,
but nobody has tried them with a real disc drive, MakeMKV or VLC yet. If you use one of them,
[bug reports](../../issues/new/choose) help a lot, and so would someone to look after it (see
[Contributing](CONTRIBUTING.md#looking-after-linux-or-macos)).

Needs Python 3.9 to 3.14 with Tkinter (included in the python.org installer for Windows
and macOS). Python 3.15 isn't supported yet: some of what it uses doesn't run on it.

```
pip install "git+https://github.com/b-town-bit/disc-detective"
```

That brings along what it depends on: **Pillow** (for posters and box art), **RapidOCR**
(for reading disc menus) and **hdmv-menus** (for reading a Blu-ray's menus from its own files),
about 250 MB in all. RapidOCR fetches one of its models the first
time it reads a menu, so have a connection for that. To run from a downloaded copy of the
repo instead, `pip install pillow rapidocr onnxruntime hdmv-menus` once, then double-click
**Disc Detective.pyw** (Windows) or run `python -m disc_detective`.

Optional programs: **MakeMKV** for ripping (and reading a Blu-ray's menus), **VLC** for
Detect titles on DVDs, and **FFmpeg** for previewing a Blu-ray's titles. Settings >
Integrations > Programs shows which you have. On Windows, Install there runs each one's own
installer from its publisher, through winget, once you agree; elsewhere it gives the download
page. Find… points it at a copy of your own (a portable one, say); Automatic goes back to the
one it found. A free **TMDB** key (also in Settings) adds extra posters. Where RapidOCR can't
make out a DVD's menu buttons or cover art, Windows' own OCR has a go, then Tesseract if you
already have it installed:
`pip install "disc-detective[windows-ocr] @ git+https://github.com/b-town-bit/disc-detective"`
(or, from a copy of the repo, `pip install -e ".[windows-ocr]"`).

On first launch it offers to download its data: the TheDiscDB index (about 4 MB)
and IMDb's public title and credits data (about 1.4 GB, indexed locally). It keeps
both up to date on its own.

## Use

**The app:** press **Scan Disc**, check the answer, press **Rip** (or don't, and
rip with MakeMKV as usual). The ▾ menus hold the rest: other drives or folders,
Settings, which titles to rip, where to put them.

**The command line:**

```
disc-detective D:\                     investigate a disc (or a folder backup)
disc-detective D:\ --json              the full report as JSON
disc-detective --list                  every disc you've scanned
disc-detective --set-truth 3EBA9FFF "Psych S1 D2"     confirm a disc; instant next time
disc-detective --to-submit            list your discs TheDiscDB doesn't have yet
disc-detective --to-improve           list discs TheDiscDB has, with titles worth labelling
disc-detective --packet 3EBA9FFF --makemkv-log disc02.txt   help submit a disc to TheDiscDB
disc-detective --update-data           refresh IMDb and TheDiscDB data now
```

Your library, the downloaded data, cached pictures and settings live in
`~/.disc_detective` (set `DISC_DETECTIVE_HOME` to move it).

**Logs** are in `~/.disc_detective/logs` (in the app: Scan ▾ > Open logs folder):
`disc_detective.log` records every scan, download and rip, including MakeMKV's own
messages; `errors.log` has only warnings and errors, with full tracebacks.
Include `errors.log` when reporting a problem.

## Plex names

```
Movies/The Prince of Egypt (1998) {imdb-tt0120794}/The Prince of Egypt (1998).mkv
TV/Psych (2006) {imdb-tt0491738}/Season 01/Psych (2006) - s01e06 - Spellingg Bee.mkv
Movies/The Prince of Egypt (1998) {imdb-tt0120794}/Featurettes/…
```

## Publishing the TheDiscDB index (maintainers)

`.github/workflows/discdb-index.yml` rebuilds the index every Monday and attaches it
to the `discdb-index` release. The app downloads it from the repo named in
`PROJECT_REPO` (`disc_detective/core.py`), so a fork changes that line. Run the
workflow once from the Actions tab after the first push.

## Credits

Disc data from [TheDiscDB](https://thediscdb.com), title data from IMDb's
non-commercial datasets, episode lists from [TVmaze](https://www.tvmaze.com)
(CC BY-SA), optional posters from TMDB. See [THIRD_PARTY.md](THIRD_PARTY.md).

Information courtesy of IMDb (https://www.imdb.com). Used with permission.
This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB.

Copyright (c) 2026 Brady Field. Source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE): free to use, change and share for
non-commercial purposes. Versions released before 2026-11-07 were released under the MIT License.
