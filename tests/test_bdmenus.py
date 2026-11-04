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
        ("PL_SPE_EASTER_EGG_7669", 2, FILM, 60.1), ("SF_Teaser_Trailer", 757, VIDEO, 1.0)]


def titles():
    t = lambda pid, secs, role: {"id": "%05d.mpls" % pid, "duration": secs, "role": role}
    return [t(3, 59883.6, "loop"), t(4, 5292.3, "feature"), t(762, 5292.3, "duplicate"), t(745, 23.0, "junk"),
            t(751, 9.9, "junk"), t(756, 141.1, "extra"), t(802, 51.1, "junk"), t(763, 116.2, "junk"),
            t(664, 366.2, "extra"), t(2, 60.1, "junk"), t(757, 59.1, "junk")]


def test_the_program_that_runs_a_blu_rays_menus(tmp_path):
    root = write_disc(tmp_path, DISC)
    p = bdmenus.menu_program(root)
    assert p["first_play"] == p["top_menu"] == ("bdj", "00000") and p["titles"] == [("hdmv", 3)]
    assert bdmenus.menu_program(str(tmp_path / "nowhere")) is None
    # the project's own name, first in its file: the film's (a search term for a label saying nothing)
    assert bdmenus.project_title(root) == "Muppets From Space"
    assert bdmenus.project_title(str(tmp_path / "nowhere")) is None


def test_a_menu_project_names_the_discs_playlists(tmp_path):
    """Muppets from Space: the film, its outtakes by name (the scan, by length, took them for
    junk), its own trailer, an Easter egg; an ad for another film at a film's frame rate among
    extras at video's (an outtake named like a film at video's stays an outtake); the previews;
    a Play All a play-all; a menu template and the film's region copies left be; a name whose
    time isn't its playlist's ("SF_Teaser_Trailer", 1 s for 59) not trusted."""
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
                   "00002.mpls": ("Extra", "Easter Egg")}
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
