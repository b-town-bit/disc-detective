# Tests

```
pip install -e ".[dev]"
pytest                    # everything that needs no personal data (fake discs, fake IMDb / TheDiscDB / GitHub)
pytest -m "not gui"       # skip the app tests (they also skip themselves without a display)
```

Everything runs offline against data the tests build themselves (`fixtures.py`).
Each test gets an empty `DISC_DETECTIVE_HOME`, so your real library is never touched.

## Your own discs (optional, never committed)

Scan discs with `--keep-evidence`, confirm each with `--set-truth`, then:

```
DISC_DETECTIVE_DEV_LIBRARY=~/.disc_detective/library.sqlite pytest tests/test_dev.py -s
```

Each disc is re-investigated from its saved IFOs / playlists and graded against
your answer: ok, partial, WRONG or missed. A WRONG fails the test. Use
`DISC_DETECTIVE_DEV_IMDB` to grade with real IMDb data instead of the fixture.

`DISC_DETECTIVE_DISCDB=/path/to/TheDiscDb-data` checks the MakeMKV log parser
against every disc TheDiscDB has.
