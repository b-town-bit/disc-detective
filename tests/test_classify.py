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


def test_a_films_extras_are_not_a_tv_season():
    """Robin Hood: Prince of Thieves: one 155-min film plus four episode-length extras (19, 32,
    50 and 60 min, 480i and 1080p). Episode-length titles that don't match each other aren't a season."""
    def t(i, mins, h, aspect):
        return {"id": i, "duration": mins * 60.0, "chapters": 5, "role": None, "note": "", "group": "playlists",
                "segments": ((i, 0, 45000 * mins * 60),), "audio": [], "subs": [], "video": "",
                "video_info": {"height": h, "aspect": aspect}}
    film = t("00033.mpls", 155, 1080, "16:9")
    extras = [t("00047.mpls", 32, 480, "4:3"), t("00049.mpls", 19.5, 480, "4:3"),
              t("00123.mpls", 60, 1080, "16:9"), t("00132.mpls", 50, 1080, "16:9")]
    kind, _ = classify([film] + extras, "Blu-ray")
    assert kind == "movie" and film["role"] == "feature" and all(x["role"] == "extra" for x in extras)
    season = [t("%05d.mpls" % k, 44 + k % 3, 1080, "16:9") for k in range(5)]          # a real season
    kind, _ = classify(season, "Blu-ray")
    assert kind == "tv"


def test_a_long_title_of_still_frames_is_not_the_film():
    """Sword in the Stone DVD: an interactive feature claims 103 minutes but has ~565 MB of video
    (0.9 Mbit/s); the 80-minute film is the feature. TheDiscDB's "01" is our Title 1."""
    def dvd(i, secs, sectors, cells):
        step = sectors // cells
        return {"id": i, "duration": secs, "chapters": 18, "role": None, "note": "", "group": "VTS 1",
                "segments": tuple((1, k * step, k * step + step - 1) for k in range(cells)), "audio": [], "subs": [],
                "video": "NTSC 16:9", "video_info": {"standard": "NTSC", "aspect": "16:9"}}
    film = dvd("Title 1", 4773, 1967442, 25)
    game = dvd("Title 34", 6204, 275948, 259)
    classify([film, game], "DVD")
    assert film["role"] == "feature" and game["role"] == "extra" and game.get("interactive")
    assert core.dvd_mbps(film) > 5 and core.dvd_mbps(game) < 1.5
    t = [dict(film, role="feature"), dict(game, role="feature")]
    core.discdb_roles(t, {"titles": [{"source": "01", "item": {"type": "MainMovie", "title": "The Sword in the Stone"}}]})
    assert t[0]["discdb_title"] == "The Sword in the Stone" and t[1]["role"] != "feature"


def test_discdb_main_movie_that_we_called_a_duplicate():
    """Aladdin 4K UCE: 00099 and 00800 play the same film; we kept 00099 and called 00800 its
    duplicate, but TheDiscDB names 00800. It becomes the feature; 00099 the duplicate."""
    def t(i, role, note=""):
        return {"id": i, "duration": 5441, "role": role, "note": note, "clip_list": "00902,00903"}
    titles = [t("00099.mpls", "feature"), t("00731.mpls", "duplicate", "same video as 00099.mpls"),
              t("00800.mpls", "duplicate", "same video as 00099.mpls"),
              t("00730.mpls", "version", "same length as 00099.mpls")]
    core.discdb_roles(titles, {"titles": [{"source": "00800.mpls", "item": {"type": "MainMovie", "title": "Aladdin"}}]})
    role = {x["id"]: (x["role"], x["note"]) for x in titles}
    assert role["00800.mpls"][0] == "feature" and role["00099.mpls"] == ("duplicate", "same video as 00800.mpls")
    assert role["00731.mpls"] == ("duplicate", "same video as 00800.mpls") and "00800" in role["00730.mpls"][1]


def test_a_shorter_copy_made_of_the_films_pieces_is_not_another_film():
    """Finding Nemo: the feature is logos + film body + credits; a PiP mode plays only the 88-min
    body. Same film (a version), not a second 'edition'."""
    def bd(i, items, **vi):
        return {"id": i, "duration": sum(o - a for _, a, o in items) / 45000.0, "chapters": 30, "role": None,
                "note": "", "group": "playlists", "segments": tuple(items), "audio": [], "subs": [],
                "video_info": dict({"height": 1080, "aspect": "16:9"}, **vi), "video": "1080p 16:9"}
    logo, body, credits = ("00872", 0, 45000 * 60), ("00873", 0, 45000 * 5266), ("00876", 0, 45000 * 720)
    film = bd("00800.mpls", [logo, body, credits])
    pip = bd("00822.mpls", [body], pip=True)
    other = bd("00900.mpls", [("00990", 0, 45000 * 5400)])                 # a genuinely different film
    classify([film, pip, other], "Blu-ray")
    assert film["role"] == "feature" and pip["role"] == "version" and pip["edition"] == "Picture-in-Picture"
    assert other["role"] == "feature"


def test_a_season_disc_with_a_two_hour_finale_is_reread_as_episodes():
    """SMALLVILLE_SEASON_10_DISC_6: the 84-minute finale made the disc look like a film."""
    titles = [{"id": "Title 1", "duration": 7579, "role": "play-all", "note": ""},
              {"id": "Title 2", "duration": 2539, "role": "extra", "note": ""},
              {"id": "Title 3", "duration": 5040, "role": "feature", "note": ""},
              {"id": "Title 5", "duration": 1183, "role": "extra", "note": ""},
              {"id": "Title 6", "duration": 1011, "role": "extra", "note": ""}]
    assert core.as_tv(titles)
    roles = {t["id"]: t["role"] for t in titles}
    assert roles == {"Title 1": "play-all", "Title 2": "episode", "Title 3": "episode",
                     "Title 5": "extra", "Title 6": "extra"}
    assert "two episodes" in titles[2]["note"]
    core.number_episodes(titles)
    assert [t.get("episode_index") for t in titles] == [None, 1, 2, None, None]


def test_shorts_a_little_under_eighteen_minutes_are_still_episodes():
    """THE_LITTLE_RASCALS_D1: ten 1930s shorts of 17-25 min and a play-all whose cells don't
    line up with them. The 17:34 short is an episode too, so the play-all is found by length."""
    lens = [(0, 25, 1), (0, 18, 51), (0, 20, 13), (0, 17, 34), (0, 20, 56), (0, 20, 48), (0, 20, 31),
            (0, 20, 7), (0, 19, 43), (0, 20, 25)]
    ts = [T(i + 2, 1, d, 4, [(1000 + i, 1000 + i)]) for i, d in enumerate(lens)]
    total = sum(h * 3600 + m * 60 + s for h, m, s in lens)
    ts.insert(0, T(1, 1, (0, 0, total), 34, [(5000, 5001)]))
    ts.append(T(12, 1, (0, 12, 0), 2, [(6000, 6001)]))          # a 12-minute featurette stays an extra
    kind, _ = classify(ts, "DVD")
    assert kind == "tv" and len(episodes(ts)) == 10
    assert ts[0]["role"] == "play-all" and ts[-1]["role"] == "extra"


def test_a_double_episode_inside_the_play_all_is_not_another_play_all():
    """LAST_MAN_STANDING_S1_D1: the play-all plays a 43-minute title and six 21-minute ones.
    The 43-minute title is two episodes, though its length equals two of the others."""
    eps = [T(i + 5, 3, (0, 21, 30 + i), 5, [(100 * (i + 2), 100 * (i + 2) + 99)]) for i in range(6)]
    dbl = T(4, 3, (0, 42, 57), 9, [(100, 199)])
    whole = T(3, 3, (2, 52, 21), 39, [(100, 199)] + [(100 * (i + 2), 100 * (i + 2) + 99) for i in range(6)])
    ts = [whole, dbl] + eps
    kind, _ = classify(ts, "DVD")
    assert kind == "tv" and dbl["role"] == "episode" and whole["role"] == "play-all"
    assert len(episodes(ts)) == 7


def test_a_chain_pointing_past_the_end_of_the_ifo_is_skipped():
    """A broken program chain used to throw away the whole title set (all lengths 0)."""
    import struct
    d = bytearray(4 * 2048)
    d[:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">I", d, 0xC8, 1)          # title table at sector 1
    struct.pack_into(">I", d, 0xCC, 2)          # chain table at sector 2
    struct.pack_into(">HHI", d, 2048, 1, 0, 11)
    struct.pack_into(">I", d, 2048 + 8, 12)
    struct.pack_into(">HH", d, 2048 + 12, 1, 1)
    pg = 2 * 2048
    struct.pack_into(">HHI", d, pg, 2, 0, 0)
    struct.pack_into(">I", d, pg + 8 + 4, 24)                 # chain 1: inside the file
    struct.pack_into(">I", d, pg + 16 + 4, 10 ** 6)           # chain 2: far past the end
    d[pg + 24 + 4:pg + 24 + 8] = bytes([0x00, 0x21, 0x30, 0xC0])   # 0:21:30 NTSC
    v = core.parse_vts(bytes(d))
    assert round(v["pgcs"][0]["duration"]) == 21 * 60 + 30 and v["pgcs"][1]["duration"] == 0


def test_firefly_bluray_two_hour_pilot_and_a_lower_resolution_featurette():
    """FIREFLYUS_D1: Serenity (86:42) has a clip of its own, so it isn't a play-all of the next
    two episodes just because the lengths add up; it's a double-length episode. The 28-minute
    480i making-of is a featurette, not an episode."""
    hd, sd = {"height": 1080}, {"height": 480}

    def P(n, secs, clips, ch, vi):
        return {"id": "%05d.mpls" % n, "group": "playlists", "duration": secs, "chapters": ch,
                "segments": tuple((c, 0, int(secs * 45000)) for c in clips), "audio": [], "subs": [],
                "video": "", "video_info": vi}
    ts = [P(1, 5202, ["00001"], 21, hd), P(2, 2564, ["00002"], 13, hd), P(3, 2635, ["00003"], 13, hd),
          P(4, 2639, ["00004"], 13, hd), P(6, 1720, ["00007"], 2, sd)]
    kind, _ = classify(ts, "Blu-ray")
    assert kind == "tv"
    assert [t["role"] for t in ts] == ["episode", "episode", "episode", "episode", "extra"]
    for t, nm in zip(ts, ["FEATURE_EP_Pilot", "FEATURE_EP_01", "FEATURE_EP_02", "FEATURE_EP_03", "TRL_BUFFY"]):
        t["authoring_name"] = nm
    core.apply_authoring_names(ts)
    assert ts[-1]["role"] == "extra" and "trailer" in ts[-1]["note"]


def test_authoring_names_are_read_in_playlist_order(tmp_path):
    jar = tmp_path / "BDMV" / "JAR" / "10000"
    jar.mkdir(parents=True)
    (jar / "PLsInfo.txt").write_text("# Playlist name:FEATURE_EP_Pilot\np1=1\n# Playlist name:EP9 WarStories\r\n"
                                     "# Playlist name:Menu\n")
    assert core.authoring_names(str(tmp_path / "BDMV")) == ["FEATURE_EP_Pilot", "EP9 WarStories", "Menu"]


def test_discdb_series_episodes_are_episodes_whatever_their_length():
    """SHERLOCK_THE_ABOMINABLE_BRIDE: one 93-minute special, which TheDiscDB lists as an
    Episode (season 0, episode 9). It looked like a film to the structure."""
    ts = [{"id": "00800.mpls", "role": "feature", "duration": 5588, "note": ""},
          {"id": "00250.mpls", "role": "extra", "duration": 1794, "note": ""}]
    hit = {"type": "series", "titles": [
        {"source": "00800.mpls", "item": {"title": "The Abominable Bride", "type": "Episode", "season": "0", "episode": "9"}},
        {"source": "00250.mpls", "item": {"title": "Mark Gatiss - A Study in Sherlock", "type": "Featurette"}}]}
    changed = core.discdb_roles(ts, hit)
    assert ts[0]["role"] == "episode" and ts[0]["episode_index"] == 1 and ts[1]["role"] == "extra"
    assert changed == [("00800.mpls", "feature", "episode")]


def test_picture_in_picture_mode_is_never_the_feature():
    """Warner's Maximum Movie Mode (Deathly Hallows Part 1): the film with a picture-in-picture
    track, 22 minutes longer because it pauses for featurettes, with its own copies of every other
    scene. The plain film is the feature; the PiP mode is an optional version, even though longer."""
    def bd(i, clips, pip=False):
        segs = tuple((c, s, e) for c, s, e in clips)
        v = {"codec": "H.264", "height": 1080, "scan": "p", "fps": "23.976", "aspect": "16:9"}
        if pip:
            v["pip"] = True
        return {"id": "%05d.mpls" % i, "group": "playlists", "duration": sum(e - s for _, s, e in clips) / 45000,
                "chapters": 30, "segments": segs, "video_info": v, "audio": [{"lang": "en", "kind": ""}],
                "subs": [], "video": core.video_text(v)}
    n = 45000 * 60                                    # a minute in 45 kHz ticks
    film = [("%05d" % (10 + k), k * 10 * n, (k + 1) * 10 * n - 1) for k in range(15)]          # 150 min
    mmm = [seg if k % 2 else ("%05d" % (60 + k), seg[1], seg[2] + 3 * n) for k, seg in enumerate(film)]
    ts = [bd(100, film), bd(200, mmm, pip=True)]
    kind, _ = classify(ts, "Blu-ray")
    roles = {t["id"]: (t["role"], t.get("edition")) for t in ts}
    assert kind == "movie" and roles["00100.mpls"] == ("feature", None)
    assert roles["00200.mpls"] == ("version", "Picture-in-Picture")


def test_a_box_set_disc_from_thediscdb_is_a_collection(imdb):
    """TheDiscDB files a double feature under one film but names both as main movies: two Plex
    movies, each found on IMDb by its name and length. Its own versions stay versions, and a
    mislabelled extra ("Play All Deleted Scenes") is an extra. One film's cuts are not a collection."""
    hit = {"title": "The Prince of Egypt", "year": 1998, "type": "movie", "titles": [
        {"source": "00001.mpls", "item": {"title": "The Prince of Egypt", "type": "MainMovie"}},
        {"source": "00002.mpls", "item": {"title": "Shrek 2", "type": "MainMovie"}},
        {"source": "00003.mpls", "item": {"title": "The Prince of Egypt (Sing-Along Version)", "type": "MainMovie"}},
        {"source": "00004.mpls", "item": {"title": "Play All Deleted Scenes", "type": "MainMovie"}}]}
    mk = lambda i, minutes: {"id": "%05d.mpls" % i, "role": "extra", "duration": minutes * 60, "chapters": 9}
    titles = [mk(1, 99), mk(2, 93), mk(3, 101), mk(4, 12)]
    core.discdb_roles(titles, hit)
    c = core.discdb_collection(imdb, titles, hit, {"mastered": "2010-01"}, name="Double Feature Collection")
    assert c and c["kind"] == "collection" and c["source"] == "TheDiscDB"
    assert [(i["title_id"], i["name"], i["year"]) for i in c["items"]] == [
        ("00001.mpls", "The Prince of Egypt", 1998), ("00002.mpls", "Shrek 2", 2004)]
    assert all(i["imdb_id"] for i in c["items"])
    assert c["versions"] == [{"title_id": "00003.mpls", "same_as": "00001.mpls"}]
    assert next(t for t in titles if t["id"] == "00004.mpls")["role"] == "extra"
    names = core.apply_collection(c, titles)
    assert any("Shrek 2 (2004) {imdb-tt" in n for n in names) and len(names) == 2
    # one film's two cuts: editions, not a collection
    cuts = {"title": "Apollo 13", "year": 1995, "type": "movie", "titles": [
        {"source": "00001.mpls", "item": {"title": "Apollo 13 (Theatrical Cut)", "type": "MainMovie"}},
        {"source": "00002.mpls", "item": {"title": "Apollo 13 (IMAX Version)", "type": "MainMovie"}}]}
    t2 = [mk(1, 140), mk(2, 116)]
    core.discdb_roles(t2, cuts)
    assert core.discdb_collection(imdb, t2, cuts, {"mastered": "2010-01"}) is None


def test_a_tv_special_disc(imdb):
    """RUDOLPH_THE_RED_NOSED_REINDEER: a 47-minute TV movie and its extras looks like a TV disc of
    two episodes, but no series has that name: the title whose length fits the special is it."""
    mk = lambda i, minutes: {"id": "Title %d" % i, "role": "episode", "duration": minutes * 60,
                             "episode_index": i, "chapters": 8}
    titles = [mk(10, 52.1), mk(11, 47.0), {"id": "Title 14", "role": "extra", "duration": 828}]
    c = core.special_on_disc(imdb, [("The Snowman Who Sang", False)], titles, {"mastered": "2018-09"})
    assert c and (c[0]["name"], c[0]["year"]) == ("The Snowman Who Sang", 1964)
    roles = {t["id"]: t["role"] for t in titles}
    assert roles == {"Title 10": "extra", "Title 11": "feature", "Title 14": "extra"}
    # the real disc: the special (52 min, English 5.1 + 2.0, Spanish, French) beside a 47-minute
    # commentary featurette (English stereo): the one with the full soundtracks is the special
    rich = [dict(mk(10, 52.1), audio=[{"lang": l, "channels": ch} for l, ch in (("en", 6), ("en", 2), ("es", 2), ("fr", 2))]),
            dict(mk(11, 47.0), audio=[{"lang": "en", "channels": 2}])]
    assert core.special_on_disc(imdb, [("The Snowman Who Sang", False)], rich, {"mastered": "2018-09"})
    assert [t["role"] for t in rich] == ["feature", "extra"]
    # the real disc: the special (52 min, English 5.1 + 2.0, Spanish, French) beside a 47-minute
    # commentary featurette (English stereo): the one with the full soundtracks is the special
    rich = [dict(mk(10, 52.1), audio=[{"lang": l, "channels": ch} for l, ch in (("en", 6), ("en", 2), ("es", 2), ("fr", 2))]),
            dict(mk(11, 47.0), audio=[{"lang": "en", "channels": 2}])]
    assert core.special_on_disc(imdb, [("The Snowman Who Sang", False)], rich, {"mastered": "2018-09"})
    assert [t["role"] for t in rich] == ["feature", "extra"]
    # nothing that fits: left alone
    other = [mk(10, 52.1), mk(11, 30.0)]
    assert core.special_on_disc(imdb, [("The Snowman Who Sang", False)], other, {"mastered": "2018-09"}) is None
    assert {t["role"] for t in other} == {"episode"}


def test_two_films_named_in_one_label(imdb):
    """LITTLE_DRUMMER_CRICKET_HEARTH: The Little Drummer Boy and Cricket on the Hearth. Each film
    explains part of the label and pairs with its own title by length (TV listings include ad
    breaks: Cricket is 60 minutes on IMDb, 49.5 on the disc)."""
    mk = lambda i, minutes: {"id": "Title %d" % i, "role": "episode", "duration": minutes * 60, "chapters": 6}
    titles = [mk(8, 49.5), mk(17, 25.7)]
    c = core.label_double_feature(imdb, "Tin Soldier Lantern Harbor", titles, {"mastered": "2018-08"})
    assert c and c["kind"] == "collection"
    assert [(i["title_id"], i["name"]) for i in c["items"]] == [("Title 8", "Lantern by the Harbor"),
                                                                 ("Title 17", "The Tin Soldier Waltz")]
    assert c["name"] == "The Tin Soldier Waltz & Lantern by the Harbor"          # in the label's order
    # a label that is one film's name isn't split up, and lengths have to fit
    assert core.label_double_feature(imdb, "Tin Soldier Lantern Harbor", [mk(8, 90), mk(17, 25.7)],
                                     {"mastered": "2018-08"}) is None


def test_a_pip_track_built_into_the_film_is_optional():
    """V for Vendetta (Warner's In-Movie Experience): the PiP video is multiplexed into the film's
    own clip, so every playlist of the film carries it. That's an optional track, not a PiP mode:
    the film is still the feature, and the optional track isn't mentioned."""
    v = {"codec": "VC-1", "height": 1080, "scan": "p", "fps": "23.976", "aspect": "16:9", "pip": True}
    n = 45000 * 60
    mk = lambda i: {"id": "%05d.mpls" % i, "group": "playlists", "duration": 132.5 * 60, "chapters": 32,
                    "segments": (("00003", 0, int(132.5 * n)),), "video_info": dict(v),
                    "audio": [{"lang": "en", "kind": ""}], "subs": [], "video": core.video_text(v)}
    ts = [mk(3), mk(39), mk(47)]
    kind, _ = classify(ts, "Blu-ray")
    feat = [t for t in ts if t["role"] == "feature"]
    assert kind == "movie" and len(feat) == 1
    assert "picture-in-picture" not in feat[0]["video"]           # optional: like a spare soundtrack
    assert not any(t.get("edition") == "Picture-in-Picture" for t in ts)
