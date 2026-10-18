"""Disc structure: which titles are episodes, play-alls, duplicates, versions, extras."""
from disc_detective.core import classify
from disc_detective import core


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


def test_dvd_decoy_titles():
    """Shaped like the real disc: the film three times over (title sets sharing video), decoys
    that scramble its cells, and one sneaky decoy of the exact same length that's almost in order."""
    import random
    # 23 cells; a gap after cell 10 (the layer break) so even the real film has one jump
    cells = [(i * 1000 + (500 if i > 10 else 0), i * 1000 + 999 + (500 if i > 10 else 0)) for i in range(23)]
    rnd = random.Random(4)

    def title(i, order, dur):
        vts = 9 + i // 4                                  # copies live in different title sets
        return {"id": "Title %d" % i, "group": "VTS %d" % vts, "duration": dur, "chapters": 17,
                "segments": tuple((vts,) + cells[k] for k in order), "audio": [{"lang": "en", "kind": ""}] * 3,
                "subs": [], "video": "NTSC 16:9"}
    real = list(range(23))
    sneaky = list(range(23))
    sneaky[3:5] = [4, 3]                                  # same length, almost in order
    ts = [title(9, sneaky, 4805.5)]
    for i in range(10, 22):
        if i in (12, 17, 20):
            ts.append(title(i, real, 4805.5))
        else:
            order = list(range(23))
            rnd.shuffle(order)
            ts.append(title(i, order, 4400 + 31 * i))
    ts.append(title(30, [0], 30))
    kind, ob = classify(ts, "DVD")
    assert kind == "movie"
    assert ob == {"decoys": 10, "copies": 2, "picked": "Title 12", "sure": True}
    roles = {t["id"]: t["role"] for t in ts}
    assert roles["Title 12"] == "feature" and roles["Title 9"] == "decoy"
    assert roles["Title 17"] == roles["Title 20"] == "duplicate"


def test_ordinary_movie_is_not_called_protected():
    t = lambda i, d, ch: {"id": "Title %d" % i, "group": "VTS 1", "duration": d, "chapters": ch,
                         "segments": ((1, i * 10, i * 10 + 9),), "audio": [], "subs": [], "video": ""}
    ts = [t(1, 5400, 28), t(2, 5400, 28), t(3, 600, 2)]           # feature + full-screen version
    kind, ob = classify(ts, "DVD")
    assert kind == "movie" and not ob


def test_discdb_main_movie_decides_the_feature():
    """WALL·E UCE: four film-length playlists, TheDiscDB names one main movie. The near-identical
    ones are alternate versions, a 6.5 h maximizer is set aside, and the named copy of a
    duplicated short (Burn·E) is the one kept."""
    def t(i, mins, role, note="", clips=None):
        return {"id": i, "duration": mins * 60, "role": role, "note": note, "clip_list": clips}
    titles = [t("00081.mpls", 98, "feature", clips="20000"), t("00089.mpls", 97, "feature"),
              t("01000.mpls", 385, "feature"), t("00038.mpls", 7.5, "extra", clips="20003"),
              t("00065.mpls", 7.5, "duplicate", "same video as 00038.mpls", clips="20003"),
              t("00039.mpls", 7.5, "duplicate", "same video as 00038.mpls", clips="20003"),
              t("00050.mpls", 60, "feature")]
    hit = {"titles": [{"source": "00081.mpls", "item": {"type": "MainMovie", "title": "Wall·E"}},
                      {"source": "00065.mpls", "item": {"type": "Extra", "title": "Burn·E"}},
                      {"source": "20003.m2ts", "item": {}}, {"source": "00089.mpls", "item": {}}]}
    core.discdb_roles(titles, hit)
    role = {x["id"]: x["role"] for x in titles}
    assert role == {"00081.mpls": "feature", "00089.mpls": "version", "01000.mpls": "junk",
                    "00038.mpls": "duplicate", "00065.mpls": "extra", "00039.mpls": "duplicate",
                    "00050.mpls": "extra"}
    assert titles[5]["note"] == "same video as 00065.mpls"
    assert core.discdb_roles(titles, {"titles": [{"source": "00081.mpls", "item": {}}]}) == []   # no main named


def test_two_main_movies_become_plex_editions():
    """The Iron Giant Signature Edition: TheDiscDB names both cuts; each gets its own Plex edition
    so the second rip doesn't overwrite the first."""
    titles = [{"id": "00099.mpls", "duration": 5398, "role": "feature", "note": ""},
              {"id": "00100.mpls", "duration": 5199, "role": "feature", "note": ""}]
    hit = {"titles": [{"source": "00099.mpls", "item": {"type": "MainMovie", "title": "The Iron Giant: Signature Edition"}},
                      {"source": "00100.mpls", "item": {"type": "MainMovie", "title": "The Iron Giant: Theatrical Edition"}}]}
    core.discdb_roles(titles, hit)
    eds = core.editions(titles, "The Iron Giant")
    assert eds == {"00099.mpls": "Signature Edition", "00100.mpls": "Theatrical Edition"}
    names = core.plex_names({"kind": "movie", "name": "The Iron Giant", "year": 1999, "imdb_id": "tt0129167"},
                            sources=list(eds), editions=eds)
    assert len({n.split(" -> ")[1] for n in names}) == 2
    assert names[0].endswith("The Iron Giant (1999) {imdb-tt0129167}/The Iron Giant (1999) {edition-Theatrical Edition}.mkv")
    # no names from TheDiscDB: fall back to running time; one feature: no edition at all
    for t in titles:
        t.pop("discdb_title")
    assert core.editions(titles, "The Iron Giant") == {"00100.mpls": "87 Minute Cut", "00099.mpls": "90 Minute Cut"}
    assert core.editions(titles[:1], "The Iron Giant") == {}
    assert core.edition_name("Wall·E", "WALL·E") is None


def _title(i, secs, video, **vi):
    return {"id": i, "duration": secs, "chapters": 28, "role": None, "note": "", "audio": [], "subs": [],
            "group": "x", "segments": (i,), "video": video, "video_info": vi}


def test_widescreen_beats_full_screen_at_the_same_length():
    """A DVD with both versions: the 16:9 one is the feature even if the 4:3 one runs a hair
    longer; the 4:3 one is a 'Fullscreen' version (an edition if you tick it)."""
    full = _title("Title 2", 5400.4, "NTSC 4:3", standard="NTSC", aspect="4:3", letterboxed=False)
    wide = _title("Title 1", 5400.0, "NTSC 16:9", standard="NTSC", aspect="16:9", letterboxed=False)
    kind, _ = classify([full, wide], "DVD")
    assert kind == "movie" and wide["role"] == "feature" and full["role"] == "version"
    assert full["edition"] == "Fullscreen" and "fullscreen version of Title 1" in full["note"]


def test_version_labels():
    hd = {"height": 1080, "aspect": "16:9"}
    assert core.version_label({"height": 2160, "aspect": "16:9", "hdr": "HDR10"}, hd) == "4K"
    assert core.version_label(hd, {"height": 2160}) == "1080p"
    assert core.version_label(dict(hd, **{"3d": True}), hd) == "3D"
    assert core.version_label({"standard": "NTSC", "aspect": "4:3"}, {"standard": "NTSC", "aspect": "16:9"}) == "Fullscreen"
    assert core.version_label({"standard": "NTSC", "aspect": "4:3", "letterboxed": True},
                              {"standard": "NTSC", "aspect": "16:9"}) is None      # letterboxed: also widescreen
    assert core.version_label(hd, hd) is None
    assert core.video_text({"height": 2160, "scan": "p", "hdr": "HDR10", "aspect": "16:9", "codec": "HEVC"}) == \
        "2160p HDR10 16:9 HEVC"


def test_blu_ray_video_attributes():
    """PlayItem STN: one primary video stream, HEVC 2160p 23.976 HDR10; CLPI: 16:9."""
    item = bytes(32)                                       # clip name .. still time, no angles
    stn = bytearray(14)                                    # reserved, stream counts, reserved
    stn[2] = 1                                             # one primary video stream
    stn += bytes([9]) + bytes(9)                           # its stream entry
    stn += bytes([5, 0x24, (8 << 4) | 1, 1 << 4, 0, 0])    # attributes: HEVC, 2160p/23.976, HDR10
    v = core._stn_video(item + bytes(2) + bytes(stn))
    assert (v["height"], v["scan"], v["fps"], v["codec"], v["hdr"]) == (2160, "p", "23.976", "HEVC", "HDR10")
    clpi = bytearray(b"HDMV0300" + bytes(64))
    pi = 40
    clpi[12:16] = pi.to_bytes(4, "big")
    prog = bytes(4) + bytes([0, 1]) + bytes(4) + bytes(2) + bytes([1, 0]) + bytes(2) + bytes([4, 0x24, 0x81, 3 << 4, 0])
    clpi[pi:pi + len(prog)] = prog
    assert core.clpi_aspect(bytes(clpi)) == "16:9"


def _film(i, cells, audio=("en",)):
    """A title made of DVD cells (vts, first sector, last sector); 1 sector ~ 1 ms here."""
    return {"id": i, "duration": sum(b - a + 1 for _, a, b in cells) / 1000.0, "chapters": 20, "role": None,
            "note": "", "group": "VTS 6", "segments": tuple(cells), "video": "NTSC 16:9",
            "video_info": {"standard": "NTSC", "aspect": "16:9"},
            "audio": [{"lang": x, "kind": ""} for x in audio], "subs": []}


def test_translated_end_credits_are_versions_not_more_films():
    """Tangled DVD: three film titles, the same cells except the end credits. One feature (the
    first title); the others are versions. An extended cut (different scenes in the middle)
    stays a film of its own."""
    body = [(6, k * 600000, k * 600000 + 599999) for k in range(10)]      # 100 minutes of film
    t1 = _film("Title 1", body + [(6, 7000000, 7037787)])                 # credits ~38 s
    t2 = _film("Title 2", body + [(6, 7100000, 7140149)])                 # other credits ~40 s
    t3 = _film("Title 3", body + [(6, 7200000, 7240588)])
    kind, _ = classify([t3, t2, t1], "DVD")
    assert (t1["role"], t2["role"], t3["role"]) == ("feature", "version", "version")
    assert "end credits" in t2["note"] and t2["edition"] == "Alternate Credits"
    ext = _film("Title 4", body[:5] + [(6, 8000000, 8179999)] + body[5:] + [(6, 7000000, 7037787)])
    cut = _film("Title 1", body + [(6, 7000000, 7037787)])
    classify([cut, ext], "DVD")
    assert ext["role"] == "feature" and cut["role"] == "feature"         # 3 extra minutes mid-film


def test_another_language_playlist_is_named():
    body = [(6, k * 600000, k * 600000 + 599999) for k in range(10)]
    en = _film("Title 1", body + [(6, 7000000, 7037787)], audio=("en", "fr"))
    fr = _film("Title 2", body + [(6, 7100000, 7150000)], audio=("fr", "en"))
    classify([fr, en], "DVD")
    assert en["role"] == "feature" and fr["role"] == "version" and fr["edition"] == "French"


def test_blu_ray_audio_and_subtitle_languages():
    item = bytes(32)
    stn = bytearray(14)
    stn[2], stn[3], stn[4] = 1, 2, 1                       # video, two audio, one subtitle stream
    stn += bytes([9]) + bytes(9) + bytes([5, 0x1B, (6 << 4) | 1, 0, 0, 0])
    stn += bytes([9]) + bytes(9) + bytes([5, 0x86, (6 << 4) | 1]) + b"eng"
    stn += bytes([9]) + bytes(9) + bytes([5, 0x81, (3 << 4) | 1]) + b"spa"
    stn += bytes([9]) + bytes(9) + bytes([5, 0x90]) + b"fra" + bytes(1)
    v = core._stn_video(item + bytes(2) + bytes(stn))
    assert [(a["lang"], a["codec"], a["channels"]) for a in v["audio"]] == [("en", "DTS-HD MA", 6), ("es", "AC3", 2)]
    assert [x["lang"] for x in v["subs"]] == ["fr"]


def test_picture_in_picture_copies_and_long_loops():
    """WALL·E without TheDiscDB: the film is intro + body; a PiP copy plays just the body, another
    a different intro + body; a 6.5 h playlist cycles a handful of clips. One feature."""
    def bd(i, items, **vi):
        return {"id": i, "duration": sum(o - a for _, a, o in items) / 45000.0, "chapters": 30, "role": None,
                "note": "", "group": "playlists", "segments": tuple(items), "audio": [], "subs": [],
                "video_info": dict({"height": 1080, "aspect": "16:9"}, **vi), "video": "1080p 16:9"}
    intro, body = ("00071", 0, 45000 * 10), ("20000", 0, 45000 * 5880)
    film = bd("00081.mpls", [intro, body])
    pip1 = bd("00089.mpls", [body], pip=True)
    pip2 = bd("00095.mpls", [("20001", 0, 45000 * 20), body], pip=True)
    loop = bd("01000.mpls", [("5000%d" % (k % 7), 0, 45000 * 56) for k in range(408)])
    classify([film, pip1, pip2, loop], "Blu-ray")
    assert film["role"] == "feature" and loop["role"] == "loop"
    assert pip1["role"] == pip2["role"] == "version" and pip1["edition"] == "Picture-in-Picture"
