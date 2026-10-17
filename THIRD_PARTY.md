# Data sources and other software

Disc Detective's own code is MIT licensed (see `LICENSE`). It uses data and tools
from these projects, each under its own terms.

## TheDiscDB
Disc fingerprints, title lists and artwork from [TheDiscDB](https://thediscdb.com)
([github.com/TheDiscDb/data](https://github.com/TheDiscDb/data)).

> MIT License, Copyright (c) 2021 Luke Foust

The weekly "discdb-index" release in this repository is a derived index of that
data (fingerprints, large-file sizes, title lists, IMDb ids; no images) and
carries TheDiscDB's license file. Pictures are fetched from TheDiscDB's
repository when a disc needs one. Please contribute discs back through
[thediscdb.com/contribute](https://thediscdb.com/contribute); `disc-detective --packet`
prepares the details.

## IMDb
Title data from the [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/),
used under IMDb's terms for personal and non-commercial use. Each user's copy is
downloaded directly from IMDb to their own computer. This project never
redistributes IMDb data, and IMDb images are not used.

## Pillow
Images are converted with [Pillow](https://python-pillow.org) (MIT-CMU license), installed
as a dependency. ffmpeg is used instead if Pillow is missing.

## TMDB (optional)
With a TMDB key, posters and a fallback search come from [TMDB](https://www.themoviedb.org).

> This product uses the TMDB API but is not endorsed or certified by TMDB.

## MakeMKV (optional)
Ripping uses [MakeMKV](https://www.makemkv.com)'s command-line tool, which you
install yourself. Disc Detective is not affiliated with MakeMKV.

## Plex
File and folder names follow [Plex's naming guide](https://support.plex.tv/articles/naming-and-organizing-your-movie-media-files/).
Disc Detective is not affiliated with Plex.
