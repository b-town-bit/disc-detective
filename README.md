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

The app then shows the poster or box art so you can confirm at a glance.

## Install

Needs Python 3.9+ with Tkinter (included in the python.org installer for Windows
and macOS).

```
pip install "git+https://github.com/b-town-bit/disc-detective"
```

That brings along its one dependency, **Pillow** (for posters and box art). To run
from a downloaded copy of the repo instead, `pip install pillow` once, then
double-click **Disc Detective.pyw** (Windows) or run `python -m disc_detective`.

Optional: **MakeMKV** for ripping,
**tesseract** for reading text off cover art, a free **TMDB** key (in Settings) for
extra posters.

On first launch it offers to download its data: the TheDiscDB index (about 4 MB)
and IMDb's public title data (about 250 MB, indexed locally). It keeps both up to
date on its own.

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
non-commercial datasets, optional posters from TMDB. See [THIRD_PARTY.md](THIRD_PARTY.md).

MIT License.
