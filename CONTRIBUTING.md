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

## Before you open a pull request

- Run the tests: `pip install -e ".[dev]"`, then `pytest -q`.
- Keep personal disc data out of the repository (your discs' logs, menu pictures, packets).
- Never add decryption keys, key lists or decryption libraries, or links to them.
