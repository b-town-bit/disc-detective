# Data sources and other software

Disc Detective's own code is MIT licensed (see `LICENSE`). It uses data and tools
from these projects, each under its own terms.

## TheDiscDB
Disc fingerprints, title lists and artwork from [TheDiscDB](https://thediscdb.com)
([github.com/TheDiscDb/data](https://github.com/TheDiscDb/data)).

> MIT License, Copyright (c) 2021 Luke Foust

The weekly "discdb-index" release in this repository is a derived index of that
data (fingerprints, large-file sizes, title lists, IMDb ids, and fingerprints of each
release's box art for recognising a disc by its cover; no images) and
carries TheDiscDB's license file. Pictures are fetched from TheDiscDB's
repository when a disc needs one. Please contribute discs back through
[thediscdb.com/contribute](https://thediscdb.com/contribute); `disc-detective --packet`
prepares the details.

## IMDb
Title data from the [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/),
used under IMDb's terms for personal and non-commercial use. Each user's copy is
downloaded directly from IMDb to their own computer. This project never
redistributes IMDb data, and IMDb images are not used.

## TVmaze
Episode lists and DVD episode orders from [TVmaze](https://www.tvmaze.com), used under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Answers are fetched
from TVmaze's public API on each user's computer and cached there; the trimmed sample
in `tests/data/tvmaze.json` is TVmaze data under the same license.

## Pillow
Images are converted with [Pillow](https://python-pillow.org) (MIT-CMU license), installed
as a dependency. ffmpeg is used instead if Pillow is missing.

## TMDB (optional)
With a TMDB key, posters and a fallback search come from [TMDB](https://www.themoviedb.org).

> This product uses the TMDB API but is not endorsed or certified by TMDB.

## Gemini (optional)
With a Gemini API key of your own, a disc nothing else identifies can have its own pictures
looked at by Google's Gemini; the picture and the disc's clues are sent to Google under
Google's terms. Its answer is always checked against IMDb.

## Fonts
The app's typefaces are bundled in `disc_detective/fonts`, each under the
[SIL Open Font License 1.1](https://openfontlicense.org) (license files beside them):
[Playfair Display](https://github.com/clauseggers/Playfair) (Copyright 2017 The Playfair
Display Project Authors), [Courier Prime](https://quoteunquoteapps.com/courierprime/)
(Copyright 2015 The Courier Prime Project Authors) and [Inter](https://rsms.me/inter/)
(Copyright 2020 The Inter Project Authors). They are static, Latin-only cuts of the
Google Fonts releases, loaded for the app only and never installed. Playfair Display's
name is reserved by its license, so the modified cut is named "Detective Serif".

## Heroicons
The small icons in `disc_detective/icons` are rendered from [Heroicons](https://heroicons.com)
(MIT License, Copyright (c) Tailwind Labs, Inc.) by `tools/make_icons.py`.

## MakeMKV (optional)
Ripping uses [MakeMKV](https://www.makemkv.com)'s command-line tool, which you
install yourself. Disc Detective is not affiliated with MakeMKV.

## Plex
File and folder names follow [Plex's naming guide](https://support.plex.tv/articles/naming-and-organizing-your-movie-media-files/).
Disc Detective is not affiliated with Plex.
