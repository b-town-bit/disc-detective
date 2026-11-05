"""Episodes only a Play All plays: our own playlists of them, in a backup folder or a sting for a disc."""
import os
import struct
import types

import pytest

pytest.importorskip("tkinter")
from disc_detective import app as dd_app, core, makemkv as mk  # noqa: E402

VIDEO = bytes([9, 1, 0, 0x10, 0x11, 0, 0, 0, 0, 0]) + bytes([5, 0x1B, 0x61, 0, 0, 0])   # stream entry, then attributes
AUDIO = bytes([9, 1, 0, 0x11, 0x00, 0, 0, 0, 0, 0]) + bytes([5, 0x81, 0x61, 0x65, 0x6E, 0x67])
MENU = bytes([9, 2, 0, 0, 0x14, 0, 0, 0, 0, 0]) + bytes([5, 0x90, 0x65, 0x6E, 0x67, 0])


def item(clip, seconds=2500, menu=True, stream_type=1, angles=False):
    video = VIDEO if stream_type == 1 else bytes([9, stream_type]) + VIDEO[2:]
    entries = video + AUDIO + (MENU if menu else b"")
    stn = struct.pack(">HH", 12 + len(entries) + 2, 0) + bytes([1, 1, 0, 1 if menu else 0, 0, 0, 0]) + b"\0" * 5 + entries
    head = clip.encode() + b"M2TS" + bytes([0, 0x10 if angles else 5, 0]) + struct.pack(">II", 1000, 1000 + seconds * 45000)
    body = head + b"\0" * 8 + b"\0" + b"\0\0\0" + stn
    return struct.pack(">H", len(body)) + body


def mpls(clips, marks_per_item=2, **kw):
    """A playlist of these clips: a real one's layout, with a sub path for the pop-up menu."""
    items = b"".join(item(c, **kw) for c in clips)
    sub = struct.pack(">I", 4) + b"\0\3\0\0"
    body = b"\0\0" + struct.pack(">HH", len(clips), 1) + items + sub
    playlist = struct.pack(">I", len(body)) + body
    marks = b"".join(bytes([0, 1]) + struct.pack(">H", k) + struct.pack(">I", 1000 + m * 450000) + b"\xff\xff" + b"\0" * 4
                     for k in range(len(clips)) for m in range(marks_per_item))
    mark_body = struct.pack(">H", len(clips) * marks_per_item) + marks
    head = b"MPLS0200" + struct.pack(">III", 40, 40 + len(playlist), 0) + b"\0" * 20
    return head + playlist + struct.pack(">I", len(mark_body)) + mark_body


def make_disc(root, playlists):
    for sub in ("BDMV/PLAYLIST", "BDMV/STREAM", "BDMV/CLIPINF", "AACS"):
        (root / sub).mkdir(parents=True)
    (root / "BDMV/index.bdmv").write_bytes(b"index")
    (root / "BDMV/MovieObject.bdmv").write_bytes(b"objects")
    (root / "AACS/keep.txt").write_text("the disc's")
    (root / "BDMV/STREAM/00016.m2ts").write_text("video")
    for name, data in playlists.items():
        (root / "BDMV/PLAYLIST" / name).write_bytes(data)
    return str(root)


def test_a_playlist_of_one_item_keeps_its_streams_and_chapters_and_loses_the_menu():
    made = core.parse_mpls(mk.episode_playlist(mpls(["00016", "00007", "00008"]), 1))
    assert [c for c, _, _ in made["items"]] == ["00007"]
    assert made["chapters"] == 2 and made["item_chapters"] == [2]
    assert round(made["duration"]) == 2500 and made["video"]["codec"] == "H.264"
    assert [a["lang"] for a in made["video"].get("audio", [])] == ["en"]


def test_its_layout_has_no_sub_paths_or_menu_streams_and_joins_nothing():
    data = mk.episode_playlist(mpls(["00016", "00007"]), 0)
    pl_at, mark_at, ext_at = struct.unpack(">III", data[8:20])
    assert ext_at == 0 and pl_at == 40
    assert struct.unpack(">HH", data[pl_at + 6:pl_at + 10]) == (1, 0)
    body = data[pl_at + 12:]
    assert body[10] & 0xF == 1
    stn = body[32:]
    assert stn[7] == 0 and struct.unpack(">H", stn[:2])[0] + 2 == mark_at - (pl_at + 12) - 32


@pytest.mark.parametrize("kw", [{"angles": True}, {"stream_type": 2}])
def test_what_it_cant_carry_over_is_refused(kw):
    with pytest.raises(ValueError):
        mk.episode_playlist(mpls(["00016", "00007"], **kw), 0)


def test_only_titles_inside_a_longer_playlist_are_found(tmp_path):
    root = make_disc(tmp_path, {"00001.mpls": mpls(["00016", "00007"]), "00002.mpls": mpls(["00040"]),
                                "00003.mpls": mpls(["00050", "00050", "00051"]), "bad.mpls": b"junk"})
    found = mk.inside_play_alls(root, {"00007.m2ts", "00040.m2ts", "00050.m2ts", "00051.m2ts", "00001.mpls", "00099.m2ts"})
    assert found == {"00007.m2ts": ("00001.mpls", 1), "00051.m2ts": ("00003.mpls", 2)}


def test_our_numbers_are_free_ones_and_a_scan_clears_what_a_crash_left(tmp_path):
    disc = {"00001.mpls": mpls(["00016", "00007"]), "00101.mpls": mpls(["00900"])}
    root = make_disc(tmp_path, disc)
    own = mk.add_episode_playlists(root, root, {"00016.m2ts": ("00001.mpls", 0), "00007.m2ts": ("00001.mpls", 1)})
    assert own == {"00102.mpls": "00007.m2ts", "00103.mpls": "00016.m2ts"}
    core.parse_bluray(os.path.join(root, "BDMV"))
    assert sorted(os.listdir(os.path.join(root, "BDMV", "PLAYLIST"))) == sorted(disc)
    assert not os.path.exists(os.path.join(root, mk.OWN_PLAYLISTS))


def test_clearing_never_removes_anything_but_playlists_it_noted(tmp_path):
    root = make_disc(tmp_path, {"00001.mpls": mpls(["00016", "00007"])})
    (tmp_path / mk.OWN_PLAYLISTS).write_text("../index.bdmv\n00001.mpls.bak\n")
    mk.clear_own_playlists(root)
    assert (tmp_path / "BDMV/index.bdmv").exists() and (tmp_path / "BDMV/PLAYLIST/00001.mpls").exists()


def test_the_sting_links_the_rest_of_the_disc_and_removing_it_leaves_the_disc_alone(tmp_path):
    disc = tmp_path / "disc"
    disc.mkdir()
    root = make_disc(disc, {"00001.mpls": mpls(["00016", "00007"])})
    work = tmp_path / "work"
    work.mkdir()
    att = tmp_path / "discatt.dat"
    att.write_text("from a backup")
    made = mk.build_sting(root, str(work), str(att))
    assert open(os.path.join(made, "AACS", "keep.txt")).read() == "the disc's"           # through a link
    assert open(os.path.join(made, "BDMV", "STREAM", "00016.m2ts")).read() == "video"
    assert open(os.path.join(made, "discatt.dat")).read() == "from a backup"
    assert open(os.path.join(made, "BDMV", "index.bdmv"), "rb").read() == b"index"
    mk.add_episode_playlists(root, made, {"00007.m2ts": ("00001.mpls", 1)}, note=False)
    assert os.listdir(os.path.join(root, "BDMV", "PLAYLIST")) == ["00001.mpls"]          # the disc: not written to
    mk.remove_sting(str(work))
    assert not work.exists()
    assert (disc / "AACS/keep.txt").read_text() == "the disc's" and (disc / "BDMV/STREAM/00016.m2ts").exists()


def test_drive_numbers_and_our_playlists_go_by_their_titles():
    text = ('DRV:0,2,999,12,"BD-RE LG","DISC","D:"\nDRV:1,2,999,12,"BD-RE PIONEER","CHUCK","E:"\nDRV:2,256,999,0,"","",""\n')
    assert mk.makemkv_drive_number(text, "E:\\") == 1 and mk.makemkv_drive_number(text, "F:") is None
    reading = 'TINFO:0,9,0,"0:43:00"\nTINFO:0,16,0,"00101.mpls"\nTINFO:1,9,0,"0:05:00"\nTINFO:1,16,0,"00015.mpls"\n'
    assert [t["source"] for t in mk.named_titles(reading, {"00101.mpls": "00016.m2ts"})] == ["00016.m2ts", "00015.mpls"]


# The whole rip, with a stand-in for makemkvcon: lists what's in the folder it's given.
class FakeMakeMKV:
    def __init__(self, root, backup_fails=False):
        self.root, self.backup_fails, self.calls, self.seen_at_rip = root, backup_fails, [], []
        test = self

        class Proc:
            returncode = None

            def __init__(self, cmd, **kw):
                self.cmd = cmd
                test.calls.append(cmd)
                self.stdout = self.lines() if "mkv" in cmd else None
                if "backup" in cmd and not test.backup_fails:
                    with open(os.path.join(cmd[-1], "discatt.dat"), "w") as fh:
                        fh.write("key data")

            def lines(self):
                folder = self.cmd[-3].split(":", 1)[1]
                titles = test.listing(folder)
                test.seen_at_rip.append(folder)
                with open(os.path.join(self.cmd[-1], "t.mkv"), "w") as fh:
                    fh.write(titles[int(self.cmd[-2])])
                yield 'MSG:5036,260,1,"Copy complete. 1 titles saved.","Copy complete. %1 titles saved.","1"\n'

            def poll(self):
                return None if "backup" in self.cmd and not test.backup_fails else 0

            def terminate(self):
                self.returncode = 1

            def wait(self, timeout=None):
                self.returncode = 0

        self.Popen = Proc

    def listing(self, folder):
        return sorted(f for f in os.listdir(os.path.join(folder, "BDMV", "PLAYLIST")) if f.endswith(".mpls"))

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[-1] == "disc:9999":
            return types.SimpleNamespace(stdout='DRV:1,2,999,12,"BD-RE","CHUCK","E:"\n')
        folder = cmd[-1].split(":", 1)[1] if cmd[-1].startswith("file:") else self.root
        lines = []
        for i, name in enumerate(self.listing(folder)):
            lines += ['TINFO:%d,9,0,"0:43:00"' % i, 'TINFO:%d,16,0,"%s"' % (i, name)]
        return types.SimpleNamespace(stdout="\n".join(lines) + "\n")


def rip(tmp_path, monkeypatch, drive, root, mkv, read_only=False):
    store = tmp_path / "store" / "reading.txt"
    monkeypatch.setattr(dd_app.subprocess, "Popen", mkv.Popen)
    monkeypatch.setattr(dd_app.subprocess, "run", mkv.run)
    monkeypatch.setattr(dd_app, "NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(dd_app.time, "sleep", lambda s: None)
    monkeypatch.setattr(dd_app.di, "makemkv_log_store", lambda h: str(store))
    monkeypatch.setattr(dd_app, "rip_path", lambda path, item, rep: path)
    monkeypatch.setattr(mk, "sting_root", lambda d: root)
    monkeypatch.setattr(dd_app, "preview_titles", lambda rep: [{"index": 0, "source": "00016.m2ts", "duration": 2500}])
    monkeypatch.setattr(mk, "sting_folder", lambda: str(tmp_path / "sting"))
    if read_only:
        def refuse(*a, **k):
            raise PermissionError("read-only")
        monkeypatch.setattr(mk, "add_episode_playlists", refuse)
    out, results, statuses = tmp_path / "out", [], []
    out.mkdir()

    def plan(mk_titles, extras=None, selected=None):
        return [{"mk": m["index"], "title_id": m["source"], "role": "episode", "length": "0:43:00", "dest": m["source"] + ".mkv"}
                for m in mk_titles if m["source"] == "00016.m2ts"]

    fake = types.SimpleNamespace(cancelled=False, post=lambda f, *a: f(*a),
                                 set_status=statuses.append, current_plan=plan, lab_short=lambda rep: [],
                                 progress=types.SimpleNamespace(configure=lambda *a: None), disc_gone=lambda d: False,
                                 rip_done=lambda done, planned, err, gone=False: results.append((done, planned, err)))
    rep = {"content_hash": "ABCDEF12", "titles": [{"id": "00016.m2ts", "duration": 2500}]}
    dd_app.App._rip_worker(fake, "makemkvcon", drive, str(out), rep, {"extras": False, "selected": None})
    done, planned, err = results[0]
    assert err is None
    return {os.path.basename(p): open(p).read() for p in done}, planned, fake, statuses


def test_a_disc_gets_a_sting_that_is_gone_afterwards(tmp_path, monkeypatch):
    disc = tmp_path / "disc"
    disc.mkdir()
    root = make_disc(disc, {"00001.mpls": mpls(["00016", "00007"]), "00002.mpls": mpls(["00200"], seconds=300)})
    mkv = FakeMakeMKV(root)
    got, planned, fake, statuses = rip(tmp_path, monkeypatch, "E:\\", root, mkv)
    assert got == {"00016.m2ts.mkv": "00101.mpls"} and planned == 1      # our playlist of it, under the title's name
    assert fake.rip_problem is None and "Setting up a sting…" in statuses
    assert any(c[-2:-1] == ["disc:1"] for c in mkv.calls if "backup" in c)
    assert mkv.seen_at_rip[0].endswith(os.path.join("sting", "disc"))
    # no minimum length, for its reading and its rip: MakeMKV's own setting can't hide our playlists
    assert all("--minlength=0" in c for c in mkv.calls if any(a.startswith("file:") for a in c))
    assert not (tmp_path / "sting").exists()                              # nothing of it left
    assert sorted(os.listdir(os.path.join(root, "BDMV", "PLAYLIST"))) == ["00001.mpls", "00002.mpls"]


def test_a_backup_folder_gets_our_playlists_for_the_rip_and_no_longer(tmp_path, monkeypatch):
    disc = tmp_path / "backup"
    disc.mkdir()
    root = make_disc(disc, {"00001.mpls": mpls(["00016", "00007"])})
    mkv = FakeMakeMKV(root)
    got, planned, fake, statuses = rip(tmp_path, monkeypatch, root, root, mkv)
    assert got == {"00016.m2ts.mkv": "00101.mpls"} and planned == 1
    assert not [c for c in mkv.calls if "backup" in c]                    # it has what it needs
    assert mkv.seen_at_rip == [root] and not (tmp_path / "sting").exists()
    assert os.listdir(os.path.join(root, "BDMV", "PLAYLIST")) == ["00001.mpls"]
    assert not os.path.exists(os.path.join(root, mk.OWN_PLAYLISTS))


def test_a_read_only_folder_rips_what_it_can_and_says_why_not_the_rest(tmp_path, monkeypatch):
    disc = tmp_path / "backup"
    disc.mkdir()
    root = make_disc(disc, {"00001.mpls": mpls(["00016", "00007"])})
    got, planned, fake, _ = rip(tmp_path, monkeypatch, root, root, FakeMakeMKV(root), read_only=True)
    assert got == {} and planned == 1 and "read-only" in fake.rip_problem


def test_no_decryption_file_falls_through_and_leaves_nothing(tmp_path, monkeypatch):
    disc = tmp_path / "disc"
    disc.mkdir()
    root = make_disc(disc, {"00001.mpls": mpls(["00016", "00007"])})
    got, planned, fake, _ = rip(tmp_path, monkeypatch, "E:\\", root, FakeMakeMKV(root, backup_fails=True))
    assert got == {} and planned == 1 and "decryption file" in fake.rip_problem
    assert not (tmp_path / "sting").exists()
