"""A Blu-ray's menus from its own files: which program runs them, and a studio framework's menu
project naming the disc's playlists (Sony's BD-J, Muppets from Space's Blu-ray)."""
import struct

from disc_detective import bdmenus


def entry(name, playlist, fps, seconds):
    return name.encode() + struct.pack(">Iff", playlist, fps, seconds) + b"\x00\x00"


def write_disc(tmp_path, entries):
    jar = tmp_path / "BDMV" / "JAR" / "03001"
    jar.mkdir(parents=True)
    body = b"\x00\x12Muppets_From_Space\x00\x00" + b"".join(
        struct.pack(">H", len(n)) + entry(n, p, f, s) for n, p, f, s in entries)
    (jar / "bluray-project.bin").write_bytes(body)
    # index.bdmv: first play and top menu a BD-J object "00000", one title an HDMV movie object 3
    bdj = lambda: bytes([0x80, 0, 0, 0, 0x80, 0]) + b"00000" + b"\x00"
    hdmv = bytes([0x40, 0, 0, 0, 0x40, 0, 0, 3, 0, 0, 0, 0])
    idx = 40
    indexes = struct.pack(">I", 0) + bdj() + bdj() + struct.pack(">H", 1) + hdmv
    head = b"INDX0200" + struct.pack(">II", idx, 0) + b"\x00" * (idx - 16)
    (tmp_path / "BDMV" / "index.bdmv").write_bytes(head + indexes)
    return str(tmp_path)


FILM, VIDEO = 23.976, 29.97
DISC = [("TMPL Main Menu", 3, FILM, 59823.6), ("FPL_MainFeature", 4, FILM, 5292.3),
        ("FPL_MainFeature_RegA", 762, FILM, 5292.3), ("SF_Cardshark_Serenade", 745, VIDEO, 23.0),
        ("SF_Eyes_Wide_Shut", 751, VIDEO, 9.9), ("SF_Theatrical_Trailer", 756, VIDEO, 141.1),
        ("SF_Smurfs", 802, FILM, 51.1), ("SF_Previews", 763, FILM, 116.2), ("SF_Play_All", 664, VIDEO, 366.2),
        ("PL_SPE_EASTER_EGG_7669", 2, FILM, 60.1), ("SF_Teaser_Trailer", 757, VIDEO, 1.0),
        ("SF_Gonzo_Disaster", 741, VIDEO, 4.0), ("SF_Music_Video", 758, VIDEO, 124.0)]


def titles():
    t = lambda pid, secs, role: {"id": "%05d.mpls" % pid, "duration": secs, "role": role}
    return [t(3, 59883.6, "loop"), t(4, 5292.3, "feature"), t(762, 5292.3, "duplicate"), t(745, 23.0, "junk"),
            t(751, 9.9, "junk"), t(756, 141.1, "extra"), t(802, 51.1, "junk"), t(763, 116.2, "junk"),
            t(664, 366.2, "extra"), t(2, 60.1, "junk"), t(757, 59.1, "junk"), t(741, 24.8, "junk"),
            t(758, 124.0, "extra")]


def test_the_program_that_runs_a_blu_rays_menus(tmp_path):
    root = write_disc(tmp_path, DISC)
    p = bdmenus.menu_program(root)
    assert p["first_play"] == p["top_menu"] == ("bdj", "00000") and p["titles"] == [("hdmv", 3)]
    assert bdmenus.menu_program(str(tmp_path / "nowhere")) is None
    # the project's own name, first in its file: the film's (a search term for a label saying nothing)
    assert bdmenus.project_title(root) == "Muppets From Space"
    assert bdmenus.project_title(str(tmp_path / "nowhere")) is None


def index(first, top):
    """index.bdmv with this first play and top menu: "bdj" or "hdmv"."""
    obj = {"bdj": bytes([0x80, 0, 0, 0, 0x80, 0]) + b"00000" + b"\x00",
           "hdmv": bytes([0x40, 0, 0, 0, 0x40, 0, 0, 3, 0, 0, 0, 0])}
    return b"INDX0300" + struct.pack(">II", 40, 0) + b"\x00" * 24 + struct.pack(">I", 0) + obj[first] + obj[top] + struct.pack(">H", 0)


def styled(tmp_path, first="hdmv", top="hdmv", files=()):
    for name in files:
        path = tmp_path / "BDMV" / "JAR" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    (tmp_path / "BDMV").mkdir(exist_ok=True)
    (tmp_path / "BDMV" / "index.bdmv").write_bytes(index(first, top))
    return bdmenus.menu_style(str(tmp_path))


def test_a_blu_rays_style_from_its_files(tmp_path):
    assert styled(tmp_path) == {"menu": "HDMV", "framework": None}
    assert styled(tmp_path, "hdmv", "bdj") == {"menu": "BD-J", "framework": None}
    assert styled(tmp_path, "bdj", "hdmv") == {"menu": "BD-J", "framework": None}
    assert bdmenus.menu_style(str(tmp_path / "nowhere")) == {"menu": None, "framework": None}
    (tmp_path / "BDMV" / "index.bdmv").write_bytes(b"INDX")      # (broken: not a failed scan)
    assert bdmenus.menu_style(str(tmp_path)) == {"menu": None, "framework": None}


def test_a_frameworks_files_make_a_disc_java_whatever_starts_it(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    assert styled(a, files=["03001/bluray-project.bin"]) == {"menu": "BD-J", "framework": "Sony"}
    b = tmp_path / "b"
    b.mkdir()
    assert styled(b, files=["15631/menu_base.prop"]) == {"menu": "BD-J", "framework": "Warner"}
    c = tmp_path / "c"
    c.mkdir()
    assert styled(c, files=["onQClient.cfg", "00003/playlists.xml"]) == {"menu": "BD-J", "framework": "onQ"}
    d = tmp_path / "d"
    d.mkdir()
    assert styled(d, files=["00003/playlists.xml"])["framework"] is None     # (a common name: says nothing alone)


def test_only_hdmv_can_be_crawled():
    assert bdmenus.crawlable({"menu": "HDMV", "framework": None})
    for style in ({"menu": "BD-J", "framework": "onQ"}, {"menu": "BD-J", "framework": "Warner"}, {"menu": "BD-J", "framework": None},
                  {"menu": None, "framework": None}, None):
        assert not bdmenus.crawlable(style)


def test_a_styles_line_in_the_log():
    assert bdmenus.style_line({"menu": "BD-J", "framework": "onQ"}) == "BD-J, onQ"
    assert bdmenus.style_line({"menu": "HDMV", "framework": None}) == "HDMV"
    assert bdmenus.style_line({"menu": None, "framework": None}) == "no menu program found"


def test_a_menu_project_names_the_discs_playlists(tmp_path):
    """Muppets from Space: the film, its outtakes by name (the scan, by length, took them for
    junk), its own trailer, an Easter egg; an ad for another film at a film's frame rate among
    extras at video's (an outtake named like a film at video's stays an outtake); the previews;
    a Play All a play-all; a menu template and the film's region copies left be; a trailer whose
    time in the project can't be one's ("SF_Teaser_Trailer", 1 s for 59) yours to confirm; the
    music video Music, named by its song."""
    root = write_disc(tmp_path, DISC)
    entries = bdmenus.menu_project(root)
    assert [e["playlist"] for e in entries][:3] == ["00003.mpls", "00004.mpls", "00762.mpls"]
    assert entries[3] == {"name": "SF_Cardshark_Serenade", "playlist": "00745.mpls", "fps": 29.97, "seconds": 23.0}
    known = {"Smurfs": "The Smurfs", "Eyes Wide Shut": "Eyes Wide Shut"}.get
    labels = bdmenus.project_labels(titles(), entries, film="Muppets from Space", known_film=known)
    got = {k: (v["type"], v["title"]) for k, v in labels.items()}
    assert got == {"00004.mpls": ("MainMovie", None), "00745.mpls": ("Extra", "Cardshark Serenade"),
                   "00751.mpls": ("Extra", "Eyes Wide Shut"), "00756.mpls": ("Trailer", "Theatrical Trailer"),
                   "00802.mpls": ("Promo", "Previews - The Smurfs"), "00763.mpls": ("Promo", "Previews"),
                   "00002.mpls": ("Extra", "Easter Egg"), "00757.mpls": ("Trailer", "Teaser Trailer"),
                   "00758.mpls": ("Music", "Music Video")}      # (Gonzo's 4 s for 24.8: an outtake, not trusted)
    assert not labels["00757.mpls"]["exact"] and "1 s in the project, 59.1 s on the disc" in labels["00757.mpls"]["note"]
    assert bdmenus.project_play_alls(titles(), entries) == ["00664.mpls"]
    # "This Elevator is Going Down" plays its parts 1, 2 and 3, clip for clip: they're pieces of
    # it (the Play All's), not extras of their own; nor is a whole that plays something unnamed
    t = lambda pid, clips, secs: {"id": pid, "clip_list": clips, "duration": secs}
    ts = [t("00746.mpls", "00554,00553,00535", 40.8), t("00809.mpls", "00554", 11.2), t("00808.mpls", "00553", 14.9),
          t("00840.mpls", "00535", 14.7), t("00745.mpls", "00534", 23.0), t("00900.mpls", "00534,00777", 30.0)]
    named = {k: {} for k in ("00746.mpls", "00809.mpls", "00808.mpls", "00840.mpls", "00745.mpls", "00900.mpls")}
    assert bdmenus.project_pieces(ts, named) == {"00809.mpls": "00746.mpls", "00808.mpls": "00746.mpls",
                                                 "00840.mpls": "00746.mpls"}
    # all at one frame rate: no ads by name alone
    same = [dict(e, fps=VIDEO) for e in entries]
    assert bdmenus.project_labels(titles(), same, "Muppets from Space", known)["00802.mpls"]["type"] == "Extra"
    assert bdmenus.menu_project(str(tmp_path / "nowhere")) == []


def class_file(strings):
    """A Java class file of one method loading strings in order (ldc_w each), as a compiled
    menu page does."""
    pool, index = [], {}

    def utf8(s):
        if ("u", s) not in index:
            data = s.encode("utf-8")
            pool.append(b"\x01" + struct.pack(">H", len(data)) + data)
            index[("u", s)] = len(pool)
        return index[("u", s)]

    def string(s):
        u = utf8(s)
        if ("s", s) not in index:
            pool.append(b"\x08" + struct.pack(">H", u))
            index[("s", s)] = len(pool)
        return index[("s", s)]
    refs = [string(s) for s in strings]
    code_name, name, desc = utf8("Code"), utf8("init"), utf8("()V")
    pool.append(b"\x07" + struct.pack(">H", utf8("dp")))
    this = len(pool)
    code = b"".join(b"\x13" + struct.pack(">H", r) + b"\x57" for r in refs) + b"\xb1"   # ldc_w, pop; return
    attr = struct.pack(">HHI", 1, 1, len(code)) + code + struct.pack(">HH", 0, 0)
    method = struct.pack(">HHHH", 1, name, desc, 1) + struct.pack(">HI", code_name, len(attr)) + attr
    return (b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, 50, len(pool) + 1) + b"".join(pool)
            + struct.pack(">HHHHH", 0x21, this, 0, 0, 0) + struct.pack(">H", 1) + method + struct.pack(">H", 0))


def test_the_menus_own_words_from_its_code(tmp_path):
    """Muppets from Space: the outtakes page's code, each button's text once per state, then
    the title it plays; in English and German, the English one the words of its ids. Its full
    titles ("Expectorating\nExtraterrestrials": its own line breaks), not the project's
    shorthand; a page of another film's ids, left in Sony's framework, isn't this disc's; a
    page giving two texts one title isn't trusted."""
    import zipfile
    root = write_disc(tmp_path, DISC)
    page = lambda texts: [x for t, i in texts for x in (t, "font2", "text_1", t, "font2", "text_2", i)]
    english = page([("Cardshark Serenade", "SF_Cardshark_Serenade"), ("Expectorating\nExtraterrestrials", "SF_Eyes_Wide_Shut"),
                    ("Theatrical Trailer", "SF_Theatrical_Trailer")])
    german = page([("Falschspieler-\nSerenade", "SF_Cardshark_Serenade"), ("Augen weit zu", "SF_Eyes_Wide_Shut"),
                   ("Kinotrailer", "SF_Theatrical_Trailer")])
    other = page([("The Making of You Got Served", "SF_The Making"), ("3RUN", "SF_3RUN")])
    unsure = page([("Boop-Kiss!", "SF_Cardshark_Serenade"), ("Rizzo", "SF_Cardshark_Serenade")])
    assert bdmenus.button_texts(english)[0] == ("Cardshark Serenade", "SF_Cardshark_Serenade")
    with zipfile.ZipFile(tmp_path / "BDMV" / "JAR" / "00000.jar", "w") as z:
        for name, seq in (("dp.class", english), ("do.class", german), ("cg.class", other), ("dr.class", unsure)):
            z.writestr(name, class_file(seq))
    texts = bdmenus.menu_texts(root, bdmenus.menu_project(root))
    assert texts == {"00745.mpls": "Cardshark Serenade", "00751.mpls": "Expectorating Extraterrestrials",
                     "00756.mpls": "Theatrical Trailer"}
    labels = bdmenus.project_labels(titles(), bdmenus.menu_project(root), "Muppets from Space", None, texts)
    assert labels["00751.mpls"]["title"] == "Expectorating Extraterrestrials"
    assert labels["00745.mpls"]["note"] == 'its menu button reads "Cardshark Serenade"'
    assert labels["00002.mpls"]["title"] == "Easter Egg"                   # (no button text: the project's)
    # the music video's button: Music, its name the song's
    sung = bdmenus.project_labels(titles(), bdmenus.menu_project(root), "Muppets from Space", None,
                                  {"00758.mpls": "Music Video: “Shining Star” by The Dust Brothers"})
    assert (sung["00758.mpls"]["type"], sung["00758.mpls"]["title"]) == ("Music", "“Shining Star” by The Dust Brothers")


def test_a_blu_ray_playlist_watched_in_ffplay(monkeypatch, tmp_path):
    """Edit titles' play link on a Blu-ray: ffmpeg's ffplay plays the playlist by its own number
    ("00745.mpls": 745; no VLC title list to look up, an outtake of 23 s too), with MakeMKV's
    libmmbd on its search path to decrypt it; one window at a time, closed as VLC's are."""
    import subprocess
    from disc_detective import menus
    ran = []

    class Player:
        def __init__(self, cmd, **kw):
            self.cmd, self.env, self.open = cmd, kw.get("env") or {}, True
            ran.append(self)

        def poll(self):
            return None if self.open else 1

        def kill(self):
            self.open = False
    monkeypatch.setattr(subprocess, "Popen", Player)
    monkeypatch.setattr(bdmenus, "decryption_dirs", lambda: [r"C:\Program Files (x86)\MakeMKV"])
    ffplay = str(tmp_path / "ffplay.exe")
    assert bdmenus.watch_playlist("E:\\", "00745.mpls", ffplay, title="Cardshark Serenade")
    cmd = ran[0].cmd
    assert cmd[0] == ffplay and cmd[cmd.index("-playlist") + 1] == "745" and cmd[-1] == "bluray:E:"
    assert "-sn" in cmd and "-x" not in cmd                       # (subtitles off; no picture known: its size)
    assert ran[0].env["PATH"].startswith(r"C:\Program Files (x86)\MakeMKV")
    # an SD title (480 lines) at least 720 tall, its own shape; an HD one its own size
    assert bdmenus.watch_playlist("E:\\", "00746.mpls", ffplay, video={"height": 480, "aspect": "4:3"})
    cmd = ran[1].cmd
    assert (cmd[cmd.index("-x") + 1], cmd[cmd.index("-y") + 1]) == ("960", "720")
    assert bdmenus.watch_size({"height": 480, "aspect": "16:9"}) == (1280, 720)
    assert bdmenus.watch_size({"height": 1080, "aspect": "16:9"}) is None
    assert [p.open for p in ran] == [False, True]                 # (the one before: closed)
    menus.stop_watching()
    assert not ran[1].open
    assert not bdmenus.watch_playlist("E:\\", "00745.mpls", None)  # (no ffplay: no link)
    (tmp_path / "ffplay.exe").write_bytes(b"")
    assert bdmenus.find_ffplay(str(tmp_path)) == ffplay


def test_a_blu_ray_title_is_watched_in_ffplay_even_when_the_stakeout_saw_it(monkeypatch):
    """Edit titles' play link on a Blu-ray: ffplay, by the playlist's own number, whenever it's installed, the
    stakeout having seen it play or not; VLC (by its number for the title) only without ffplay. A DVD's: VLC."""
    from disc_detective import app as dd_app, menus
    played = []
    monkeypatch.setattr(bdmenus, "watch_playlist", lambda src, tid, ff, **k: played.append(("ffplay", tid)) or True)
    monkeypatch.setattr(bdmenus, "watch_vlc", lambda folder, src, n: played.append(("vlc", n)) or True)
    monkeypatch.setattr(menus, "watch_title", lambda folder, src, n: played.append(("vlc dvd", n)) or True)

    class Desk:
        app = type("A", (), {"busy": False, "mkv_scan": None})()
        rep = {"path": "E:\\", "titles": [], "menu_raw": {"watch": {"00010.mpls": {"title": 3}}}}
        state, vlc, ffplay = {}, "C:/VLC", "C:/ffmpeg/ffplay.exe"
        seen_playing = dd_app.TitlesDialog.seen_playing

        def show_outcome(self, *a):
            played.append(("said", a[0]))
    desk = Desk()
    dd_app.TitlesDialog.watch(desk, "00010.mpls")              # (seen by the stakeout: ffplay all the same)
    dd_app.TitlesDialog.watch(desk, "00011.mpls")
    desk.ffplay = None
    dd_app.TitlesDialog.watch(desk, "00010.mpls")              # (no ffplay: VLC, by its number for it)
    dd_app.TitlesDialog.watch(desk, "Title 4")
    assert played == [("ffplay", "00010.mpls"), ("ffplay", "00011.mpls"), ("vlc", 3), ("vlc dvd", 4)]


def test_a_warner_film_disc_names_its_titles_from_its_menu_files(tmp_path):
    """Warner's BD-J framework on a film disc (Half-Blood Prince's): states.prop names the film's playlist, and
    each special feature's button the clip it plays and how long it runs; its art's words (read) are its name,
    else the name in the file, in words. Through project_labels: the film, and each feature an extra by its
    button's words, known (exact)."""
    jar = tmp_path / "BDMV" / "JAR"
    (jar / "04518").mkdir(parents=True)
    (jar / "15631").mkdir()
    (jar / "04518" / "images.txt").write_text("sf_millennium_sel_png_eng,sheet.png,0,0,10,10\n")
    (jar / "04518" / "menu_base.prop").write_text(
        "millennium.name=TheMillenniumBridge\r\nmillennium.type=Button\r\n"
        "millennium.image=sf_millennium_nor_png,sf_millennium_sel_png_eng\r\n"
        "millennium.videoStream=200,206000000000\r\n"
        "millennium.class=com.wb.bdj.menu.SpecialFeatureClipButtonHelper,millennium_preview,special_features\r\n"
        "orphanage.name=WoolsOrphanage\r\norphanage.type=Button\r\norphanage.videoStream=205,169000000000\r\n"
        "orphanage.class=com.wb.bdj.menu.SpecialFeatureClipButtonHelper,orphanage_preview,special_features\r\n"
        "inmovie_on.videoStream=100,9210000000000\r\ninmovie_on.class=com.wb.bdj.menu.IMEEngineButtonHelper\r\n")
    (jar / "15631" / "states.prop").write_text("main_movie.streamIds=100\nmain_movie.duration=9210000000000\n")
    entries, texts = bdmenus.wb_film(str(tmp_path))
    assert [(e["name"], e["playlist"], e["seconds"]) for e in entries] == [
        ("FPL_MainFeature", "00100.mpls", 9210.0), ("SF_The_Millennium_Bridge", "00200.mpls", 206.0),
        ("SF_Wools_Orphanage", "00205.mpls", 169.0)]
    assert texts == {}                                   # (nothing to read the art with)
    titles = [{"id": "00100.mpls", "role": "feature", "duration": 9210}, {"id": "00200.mpls", "role": "extra", "duration": 207},
              {"id": "00205.mpls", "role": "extra", "duration": 168}]
    labels = bdmenus.project_labels(titles, entries, texts={"00205.mpls": "Wool's Orphanage"})
    assert labels["00100.mpls"]["type"] == "MainMovie" and labels["00100.mpls"]["exact"]
    assert (labels["00205.mpls"]["title"], labels["00205.mpls"]["exact"]) == ("Wool's Orphanage", True)
    assert (labels["00200.mpls"]["title"], labels["00200.mpls"]["exact"]) == ("The Millennium Bridge", False)
    assert bdmenus.wb_film(str(tmp_path / "none")) == ([], {})


def test_a_run_the_menus_offer_names_it_and_its_titles_are_its_pieces():
    """The rule on any disc: what the menus offer names it. Muppets from Space's previews, Smurfs and Zoo, a title
    each, but its menu offers only the run of them ("Previews"): that's labelled, the two its pieces, not labelled;
    with no button words known (nothing says what's offered), each as the project names it."""
    entries = [{"name": "FPL_MainFeature", "playlist": "00004.mpls", "seconds": 5292.0, "fps": "23.976"},
               {"name": "SF_Previews", "playlist": "00800.mpls", "seconds": 116.0, "fps": "23.976"},
               {"name": "SF_Smurfs", "playlist": "00801.mpls", "seconds": 60.0, "fps": "23.976"},
               {"name": "SF_Zoo", "playlist": "00802.mpls", "seconds": 56.0, "fps": "23.976"}]
    titles = [{"id": "00004.mpls", "role": "feature", "duration": 5292.0, "clip_list": "00004"},
              {"id": "00800.mpls", "role": "play-all", "duration": 116.0, "clip_list": "00801,00802"},
              {"id": "00801.mpls", "role": "junk", "duration": 60.0, "clip_list": "00801"},
              {"id": "00802.mpls", "role": "junk", "duration": 56.0, "clip_list": "00802"}]
    labels = bdmenus.project_labels(titles, entries, texts={"00800.mpls": "Previews", "00004.mpls": "Play Movie"})
    assert labels["00800.mpls"]["type"] == "Promo"
    assert "00801.mpls" not in labels and "00802.mpls" not in labels
    labels = bdmenus.project_labels(titles, entries)
    assert "00801.mpls" in labels and "00802.mpls" in labels


# ---- an episode only a Play All plays: its clip played whole, decrypted as it's read ----

def clip_disc(tmp_path, units):
    """A disc folder with one clip, 00016.m2ts, of the given aligned units ([bytes of UNIT])."""
    stream = tmp_path / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00016.m2ts").write_bytes(b"".join(units))
    return str(tmp_path)


def unit(first, fill):
    return bytes([first]) + bytes([fill]) * (bdmenus.UNIT - 1)


class FakeAacs:
    """MakeMKV's library stood in for: "decrypts" a unit by clearing its copy bits and flipping its body."""
    opened, closed = [], []

    def __init__(self, library, root):
        FakeAacs.opened.append((library, root))

    def decrypt(self, u):
        return bytes([u[0] & ~bdmenus.ENCRYPTED & 0xFF]) + bytes(b ^ 0xFF for b in u[1:])

    def close(self):
        FakeAacs.closed.append(True)


class FakePlayer:
    """ffplay stood in for: keeps what it's fed; its window open till killed (or, with ends_after, till that many
    bytes)."""

    def __init__(self, ends_after=None):
        self.fed, self.code, self.ends_after, self.command = bytearray(), None, ends_after, None
        outer = self

        class In:
            def write(self, b):
                outer.fed += b
                if outer.ends_after is not None and len(outer.fed) >= outer.ends_after:
                    outer.code = 0

            def close(self):
                pass
        self.stdin = In()

    def __call__(self, command, **kw):
        self.command = command
        return self

    def poll(self):
        return self.code

    def kill(self):
        self.code = -9


def test_a_clips_units_are_decrypted_in_order_into_the_player_and_the_disc_closed(tmp_path):
    FakeAacs.opened, FakeAacs.closed = [], []
    units = [unit(0xC0, 1), unit(0x00, 2), unit(0xC0, 3)]
    root = clip_disc(tmp_path, units)
    player = FakePlayer()
    w = bdmenus.ClipWatch(["ffplay", "-i", "pipe:0"], bdmenus.clip_path(root, "00016.m2ts"), root, "libmmbd64.dll",
                          opener=FakeAacs, popen=player).start()
    w._thread.join(timeout=5)
    assert bytes(player.fed) == FakeAacs.decrypt(None, units[0]) + units[1] + FakeAacs.decrypt(None, units[2])
    assert FakeAacs.opened == [("libmmbd64.dll", root)] and FakeAacs.closed == [True]
    assert w.failed is None


def test_a_clip_stops_being_read_when_its_window_closes(tmp_path):
    FakeAacs.closed = []
    root = clip_disc(tmp_path, [unit(0xC0, k) for k in range(20)])
    player = FakePlayer(ends_after=3 * bdmenus.UNIT)
    w = bdmenus.ClipWatch(["ffplay"], bdmenus.clip_path(root, "00016"), root, "lib", opener=FakeAacs, popen=player).start()
    w._thread.join(timeout=5)
    assert len(player.fed) == 3 * bdmenus.UNIT and FakeAacs.closed == [True]


def test_killing_a_clips_watch_stops_its_player_and_closes_the_disc(tmp_path):
    import threading
    FakeAacs.closed = []
    gate = threading.Event()

    class Slow(FakeAacs):
        def decrypt(self, u):
            gate.wait(5)
            return super().decrypt(u)
    root = clip_disc(tmp_path, [unit(0xC0, k) for k in range(50)])
    player = FakePlayer()
    w = bdmenus.ClipWatch(["ffplay"], bdmenus.clip_path(root, "00016"), root, "lib", opener=Slow, popen=player).start()
    assert w.poll() is None
    gate.set()
    w.kill()
    assert player.code == -9 and FakeAacs.closed == [True] and w.poll() is not None


def test_a_disc_the_library_cant_open_says_why(tmp_path):
    root = clip_disc(tmp_path, [unit(0xC0, 1)])
    told = []

    def refuses(library, root):
        raise OSError("MakeMKV's library couldn't open the disc (error 7)")
    player = FakePlayer()
    w = bdmenus.ClipWatch(["ffplay"], bdmenus.clip_path(root, "00016"), root, "lib", opener=refuses, popen=player,
                          on_fail=told.append).start()
    w._thread.join(timeout=5)
    assert told == ["MakeMKV's library couldn't open the disc (error 7)"] and player.command is None


def test_watch_clip_plays_a_decrypted_folders_clip_as_it_is_and_says_what_it_lacks(tmp_path, monkeypatch):
    import subprocess
    from disc_detective import menus
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or FakePlayer())
    monkeypatch.setattr(menus, "_watching", None)
    root = clip_disc(tmp_path, [unit(0x00, 1), unit(0x00, 2)])
    assert bdmenus.watch_clip(root, "00016.m2ts", None) == "ffplay"
    assert bdmenus.watch_clip(root, "00099.m2ts", "ffplay.exe") == "file"
    assert bdmenus.watch_clip(root, "00016.m2ts", "ffplay.exe", title="S02E02") == "playing"
    assert started[-1][-2:] == ["-i", bdmenus.clip_path(root, "00016.m2ts")] and "S02E02" in started[-1]
    (tmp_path / "BDMV" / "STREAM" / "00016.m2ts").write_bytes(unit(0xC0, 1))
    monkeypatch.setattr(bdmenus, "mmbd_library", lambda: None)
    assert bdmenus.watch_clip(root, "00016.m2ts", "ffplay.exe") == "library"


def write_template(tmp_path, folder="00000"):
    """A menu template in plain text files, made up in the shape of Spider-Man's Blu-ray's: streams.xml naming
    the videos' playlists (a comment before its declaration, a commented-out entry), template.xml's pages, and
    dictionary.txt's words, a column per language (a quoted text over two lines, a stray Windows-1252 byte)."""
    jar = tmp_path / "BDMV" / "JAR" / folder
    jar.mkdir(parents=True)
    (jar / "streams.xml").write_text(
        "﻿<!-- Streams -->\n<?xml version=\"1.0\"?>\n<streams><playlist>\n<feature>00001</feature>\n"
        "<added-value>\n<behind_0>00345</behind_0>\n<behind_1>00306</behind_1>\n<tv_1>00331</tv_1>\n"
        "<SF_3>00317</SF_3> <!-- Gag Outtakes -->\n<t00>00009</t00>\n<!-- <old_1>00777</old_1> -->\n"
        "</added-value></playlist></streams>\n", encoding="utf-8")
    (jar / "template.xml").write_text(
        "<?xml version=\"1.0\"?>\n<template>\n<special_features>\n"
        "<button-text>sf_0,sf_1,sf_2,sf_3,sf_4</button-text>\n"
        "<!-- <button>#old_1</button> -->\n"
        "<button>GAME,AV_1,#SF_3,AV_2,#t00</button>\n<menus>\n<GAME><button>~6</button></GAME>\n"
        "<AV_1><button-text>behind_0,behind_1</button-text><button>#behind_0,#behind_1</button></AV_1>\n"
        "<AV_2><button-text>tv_1</button-text><button>#tv_1</button>\n"
        "<button-text menu-language=\"jpn\">tv_9</button-text><button menu-language=\"jpn\">#tv_9</button></AV_2>\n"
        "</menus>\n</special_features>\n</template>\n", encoding="utf-8")
    (jar / "dictionary.txt").write_bytes(
        "﻿Item Type\teng\tspa\nsf_0\tTRIVIA\tTRIVIA\nsf_1\t\"BEHIND THE SCENES\nOF SPIDER-MAN".encode() + b"\x99\"\tDETR\xc3\x81S\n"
        + "sf_2\tGAGS & OUTTAKES\tTOMAS\nsf_3\tTV SPOTS\tANUNCIOS\nsf_4\tPREVIEWS\tAVANCES\nbehind_0\tPLAY ALL\tTODO\n"
          "behind_1\t    COSTUME DESIGN \tVESTUARIO\ntv_1\tAMAZING\tASOMBROSO\ntv_9\tCINGULAR\tCINGULAR\n".encode())
    return str(tmp_path)


def test_a_menu_template_in_plain_text_files_is_found_by_its_shape(tmp_path):
    """A menu template's files (streams.xml, template.xml, dictionary.txt) make a disc's style one, whoever made it;
    a folder missing one, or with the names but not their shape, isn't."""
    root = write_template(tmp_path / "a")
    (tmp_path / "a" / "BDMV" / "index.bdmv").write_bytes(index("hdmv", "bdj"))
    assert bdmenus.menu_style(root) == {"menu": "BD-J", "framework": "menu template"}
    b = tmp_path / "b" / "BDMV" / "JAR" / "00000"
    b.mkdir(parents=True)
    for name in bdmenus.TEMPLATE_FILES:
        (b / name).write_text("nothing of the kind")
    assert bdmenus.template_film(str(tmp_path / "b")) == ([], {})
    (tmp_path / "a" / "BDMV" / "JAR" / "00000" / "dictionary.txt").unlink()
    assert bdmenus.template_film(root) == ([], {})


def test_a_menu_templates_files_name_each_title_by_its_button(tmp_path):
    """Each page's buttons, their words' keys paired with what each does, in order: "#key" plays the video
    streams.xml gives that key, "AV_1" opens that page, whose button's words are its heading. Only the language
    with no menu-language of its own (English), what's commented out left out, the words recased; then through
    project_labels as any menu project's: the film, its Play All, a TV spot its own trailer, previews an ad."""
    root = write_template(tmp_path)
    entries, texts = bdmenus.template_film(root)
    assert [(e["name"], e["playlist"], e.get("under")) for e in entries] == [
        ("FPL_MainFeature", "00001.mpls", None),
        ("SF_Play_All_behind_0", "00345.mpls", "Behind the Scenes of Spider-Man"),
        ("SF_behind_1", "00306.mpls", "Behind the Scenes of Spider-Man"),
        ("SF_SF_3", "00317.mpls", ""), ("SF_tv_1", "00331.mpls", "TV Spots"), ("SF_t00", "00009.mpls", "")]
    assert texts["00306.mpls"] == "Costume Design" and texts["00317.mpls"] == "Gags & Outtakes"
    titles = [{"id": i, "duration": d, "role": r} for i, d, r in (
        ("00001.mpls", 7272, "feature"), ("00345.mpls", 1940, "extra"), ("00306.mpls", 508, "extra"),
        ("00317.mpls", 185, "extra"), ("00331.mpls", 33, "junk"), ("00009.mpls", 524, "extra"))]
    labels = bdmenus.project_labels(titles, entries, "Spider-Man", None, texts)
    assert labels["00001.mpls"]["type"] == "MainMovie"
    assert "00345.mpls" not in labels and bdmenus.project_play_alls(titles, entries) == ["00345.mpls"]
    assert (labels["00306.mpls"]["type"], labels["00306.mpls"]["title"], labels["00306.mpls"]["exact"]) == ("Extra", "Costume Design", True)
    assert (labels["00331.mpls"]["type"], labels["00331.mpls"]["title"]) == ("Trailer", "Amazing")
    assert labels["00009.mpls"]["type"] == "Promo"
