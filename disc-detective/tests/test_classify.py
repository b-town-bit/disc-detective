"""Disc structure: which titles are episodes, play-alls, duplicates, versions, extras."""
from disc_detective.core import classify


def T(i, vts, dur, ch, segs, audio=1):
    h, m, s = dur
    return {"id": "Title %d" % i, "group": "VTS %d" % vts, "duration": h * 3600 + m * 60 + s, "chapters": ch,
            "segments": tuple((vts,) + x for x in segs), "audio": [{"lang": "en", "kind": ""}] * audio,
            "subs": [], "video": "NTSC 4:3"}


A, B, C, D, E, F, G = [(i * 100, i * 100 + 99) for i in range(1, 8)]


def episodes(ts):
    return [t["id"] for t in ts if t["role"] == "episode"]


def test_tv_disc_with_play_alls_and_duplicates():
    # duplicate episode titles, two play-alls covering pairs of episodes, short extras
    ts = [T(1, 1, (0, 43, 8), 5, [A], 2), T(2, 1, (0, 43, 8), 5, [A], 2), T(3, 1, (0, 43, 8), 5, [A], 2),
          T(4, 2, (0, 42, 46), 5, [B]), T(5, 2, (0, 42, 22), 5, [C]), T(6, 2, (1, 25, 8), 9, [B, C]),
          T(7, 3, (0, 43, 11), 5, [D], 2), T(8, 3, (0, 43, 11), 5, [D], 2), T(9, 3, (1, 26, 18), 9, [D, E], 2),
          T(10, 3, (0, 43, 7), 5, [E], 2), T(11, 3, (0, 43, 7), 5, [E], 2)] + \
         [T(12 + i, 4, (0, 0, 46 + i * 30), 2, [(900 + i, 950 + i)]) for i in range(4)]
    kind, _ = classify(ts, "DVD")
    assert kind == "tv" and len(episodes(ts)) == 5
    roles = {t["id"]: t["role"] for t in ts}
    assert roles["Title 6"] == roles["Title 9"] == "play-all"
    assert roles["Title 2"] == roles["Title 3"] == "duplicate"


def test_play_all_of_every_episode():
    ts = [T(1, 1, (2, 48, 42), 32, [A, B, C, D], 3), T(2, 1, (0, 42, 19), 8, [A], 3), T(3, 1, (0, 42, 19), 8, [A], 3),
          T(4, 1, (0, 41, 56), 8, [B], 3), T(5, 1, (0, 42, 6), 8, [C], 3), T(6, 1, (0, 42, 19), 9, [D], 3),
          T(7, 1, (0, 0, 0), 1, [])]
    kind, _ = classify(ts, "DVD")
    assert kind == "tv" and episodes(ts) == ["Title 2", "Title 4", "Title 5", "Title 6"]
    assert ts[0]["role"] == "play-all"


def test_movie_with_alternate_version():
    ts = [T(1, 5, (1, 34, 2), 37, [F], 4), T(2, 4, (1, 34, 2), 37, [G], 4), T(3, 3, (0, 0, 0), 2, [])]
    ts[0]["video"] = "NTSC 4:3 letterboxed"
    kind, _ = classify(ts, "DVD")
    assert kind == "movie"
    assert sorted(t["role"] for t in ts) == ["feature", "junk", "version"]


def test_play_all_whose_cells_do_not_line_up():
    ts = [T(1, 1, (0, 22, 1), 6, [A]), T(2, 1, (0, 21, 59), 6, [B]), T(3, 1, (0, 22, 3), 6, [C]),
          T(4, 1, (1, 6, 3), 18, [(5000, 5100)])]
    kind, _ = classify(ts, "DVD")
    assert kind == "tv" and len(episodes(ts)) == 3 and ts[3]["role"] == "play-all"
