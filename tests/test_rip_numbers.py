"""MakeMKV's title numbers belong to one scan: a rip must get the title it planned, not a number's."""
import os
import types

import pytest

pytest.importorskip("tkinter")
from disc_detective import app as dd_app, core, makemkv as mk  # noqa: E402

PLAY_ALL = "00001.mpls"
CLIP = "00004.mpls"
EXTRA = "00015.mpls"
# what MakeMKV lists at its default minimum length, and at 0 (Chuck S2 D2's numbers shift like this)
DEFAULT = [PLAY_ALL, EXTRA]
EVERYTHING = [CLIP, PLAY_ALL, EXTRA]


def reading(titles, saving=False):
    """A scan's robot-mode text."""
    lines = []
    for i, src in enumerate(titles):
        lines.append('MSG:3307,0,2,"File %s was added as title #%d","File %%1 was added as title #%%2","%s","%d"' % (src, i, src, i))
    for i, src in enumerate(titles):
        lines += ['TINFO:%d,9,0,"0:43:00"' % i, 'TINFO:%d,16,0,"%s"' % (i, src)]
    if saving:
        lines.append('MSG:5014,131072,2,"Saving 1 titles into directory x","Saving %1 titles into directory %2","1","x"')
    return "\n".join(lines) + "\n"


def dvd_reading(numbers):
    lines = ['MSG:3028,0,3,"Title #%d was added (5 cell(s), 0:43:00)","Title #%%1 was added (%%2 cell(s), %%3)","%d","5","0:43:00"' % (n, n)
             for n in numbers]
    return "\n".join(lines) + "\n"


class FakeMakeMKV:
    """makemkvcon: numbers its titles as `titles` says; a rip writes the file its number means."""

    def __init__(self, titles, no_minimum=None, fail=None):
        self.titles, self.no_minimum, self.reads, self.rips, self.short_reads, self.short_rips = titles, no_minimum or titles, 0, [], 0, 0
        self.fail = fail                 # a rip's closing messages, leaving a partial file behind
        test = self

        class Proc:
            returncode = None

            def __init__(self, cmd, **kw):
                self.number, self.work, self.stopped = int(cmd[-2]), cmd[-1], False
                self.listed = test.no_minimum if "--minlength=0" in cmd else test.titles
                test.short_rips += "--minlength=0" in cmd
                test.rips.append(self.number)
                self.stdout = self.lines()

            def lines(self):
                for line in reading(self.listed, saving=True).splitlines(True):
                    yield line
                if self.stopped:
                    return
                with open(os.path.join(self.work, "t%02d.mkv" % self.number), "w") as fh:
                    fh.write(self.listed[self.number])
                if test.fail:
                    yield from test.fail
                    return
                yield 'MSG:5036,260,1,"Copy complete. 1 titles saved.","Copy complete. %1 titles saved.","1"\n'

            def terminate(self):
                self.stopped = True

            def wait(self):
                self.returncode = 1 if self.stopped else 0

        self.Popen = Proc

    def run(self, cmd, **kw):
        self.reads += 1
        short = "--minlength=0" in cmd
        self.short_reads += short
        return types.SimpleNamespace(stdout=reading(self.no_minimum if short else self.titles))


def rip(tmp_path, monkeypatch, saved, now, ids=(PLAY_ALL,), no_minimum=None, on_disc=(), fail=None):
    """Rip these titles with `saved` as the saved reading and MakeMKV numbering as `now`.
    on_disc: the scan's titles, (id, seconds). fail: each rip's closing messages, instead of success."""
    store = tmp_path / "store" / "reading.txt"
    if saved is not None:
        store.parent.mkdir()
        store.write_text(saved)
    mkv = FakeMakeMKV(now, no_minimum, fail)
    monkeypatch.setattr(dd_app.subprocess, "Popen", mkv.Popen)
    monkeypatch.setattr(dd_app.subprocess, "run", mkv.run)
    monkeypatch.setattr(dd_app, "NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(dd_app.di, "makemkv_log_store", lambda h: str(store))
    monkeypatch.setattr(dd_app, "rip_path", lambda path, item, rep: path)
    monkeypatch.setattr(mk, "sting_root", lambda d: None)      # (not whatever disc is in the real drive)
    out, results = tmp_path / "out", []
    out.mkdir()

    def plan(mk_titles, extras=None, selected=None):
        return [{"mk": m["index"], "title_id": m["source"], "role": "extra", "length": "0:43:00", "dest": m["source"] + ".mkv"}
                for m in mk_titles if m["source"] in ids]

    fake = types.SimpleNamespace(cancelled=False, post=lambda f, *a: f(*a),
                                 set_status=lambda *a: None, current_plan=plan, lab_short=lambda rep: [],
                                 progress=types.SimpleNamespace(configure=lambda *a: None), disc_gone=lambda d: False,
                                 rip_done=lambda done, planned, err, gone=False: results.append((done, planned, err)))
    rep = {"content_hash": "ABCDEF12", "titles": [{"id": i, "duration": s} for i, s in on_disc]}
    dd_app.App._rip_worker(fake, "makemkvcon", "E:\\", str(out), rep, {"extras": False, "selected": None})
    done, planned, err = results[0]
    assert err is None
    mkv.app = fake
    return {os.path.basename(p): open(p).read() for p in done}, planned, mkv, store


def test_same_numbers_rip_as_planned_with_no_second_reading(tmp_path, monkeypatch):
    got, planned, mkv, _ = rip(tmp_path, monkeypatch, reading(DEFAULT), DEFAULT)
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and planned == 1
    assert mkv.rips == [0] and mkv.reads == 0


def test_numbers_moved_rips_the_planned_title_not_the_old_number(tmp_path, monkeypatch):
    """The minimum length changed in MakeMKV's GUI after the reading: number 0 is another title now."""
    got, planned, mkv, store = rip(tmp_path, monkeypatch, reading(DEFAULT), EVERYTHING)
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and planned == 1      # never the 50-second clip
    assert mkv.rips == [0, 1] and mkv.reads == 1
    assert mk.makemkv_added(store.read_text()) == mk.makemkv_added(reading(DEFAULT))    # packets' reading kept


def test_later_titles_use_the_new_numbers_straight_away(tmp_path, monkeypatch):
    got, planned, mkv, _ = rip(tmp_path, monkeypatch, reading(DEFAULT), EVERYTHING, ids=(PLAY_ALL, EXTRA))
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL, EXTRA + ".mkv": EXTRA} and planned == 2
    assert mkv.rips == [0, 1, 2] and mkv.reads == 1        # one wasted scan, then none


def test_a_title_no_longer_listed_is_left_out_not_replaced(tmp_path, monkeypatch):
    got, planned, mkv, _ = rip(tmp_path, monkeypatch, reading(EVERYTHING), [EXTRA], ids=(CLIP,))
    assert got == {} and planned == 1
    assert mkv.reads == 1


def test_a_reading_without_messages_rips_as_before(tmp_path, monkeypatch):
    """A saved log without the scan's messages can't be compared: the number's all there is."""
    plain = "".join(l for l in reading(DEFAULT).splitlines(True) if not l.startswith("MSG:"))
    got, _, mkv, _ = rip(tmp_path, monkeypatch, plain, DEFAULT)
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and mkv.reads == 0


def test_no_saved_reading_reads_once_and_saves_it(tmp_path, monkeypatch):
    got, _, mkv, store = rip(tmp_path, monkeypatch, None, DEFAULT)
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and mkv.reads == 1 and store.exists()


SCAN = ((PLAY_ALL, 15415), (CLIP, 50), (EXTRA, 340))


def test_a_wanted_short_title_reads_again_with_no_minimum(tmp_path, monkeypatch):
    """Muppets from Space's outtakes: labelled, under 2 minutes, not in MakeMKV's default reading."""
    got, planned, mkv, store = rip(tmp_path, monkeypatch, reading(DEFAULT), DEFAULT, ids=(PLAY_ALL, CLIP),
                                   no_minimum=EVERYTHING, on_disc=SCAN)
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL, CLIP + ".mkv": CLIP} and planned == 2
    assert mkv.short_reads == 1 and mkv.short_rips == 2 and mkv.rips == [0, 1]     # by the new numbers
    assert mk.makemkv_added(store.read_text()) == mk.makemkv_added(reading(DEFAULT))    # packets' reading kept


def test_no_short_title_wanted_keeps_the_saved_reading_alone(tmp_path, monkeypatch):
    got, _, mkv, _ = rip(tmp_path, monkeypatch, reading(DEFAULT), DEFAULT, on_disc=SCAN)
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and mkv.reads == 0 and mkv.short_rips == 0


def test_a_long_title_hidden_by_a_higher_minimum_reads_again_and_rips(tmp_path, monkeypatch):
    """MakeMKV's minimum length set above an episode's: it's listed once there's no minimum."""
    got, planned, mkv, _ = rip(tmp_path, monkeypatch, reading(DEFAULT), DEFAULT, ids=(PLAY_ALL, CLIP), no_minimum=EVERYTHING,
                               on_disc=((PLAY_ALL, 15415), (CLIP, 2521)))
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL, CLIP + ".mkv": CLIP} and planned == 2 and mkv.short_reads == 1 and mkv.short_rips == 2


def test_a_title_no_minimum_lists_either_counts_as_not_ripped(tmp_path, monkeypatch):
    """Chuck's episodes, with no way to reach them: asked once, and said so."""
    got, planned, mkv, _ = rip(tmp_path, monkeypatch, reading([PLAY_ALL]), [PLAY_ALL], ids=(PLAY_ALL, "00016.m2ts"),
                               on_disc=((PLAY_ALL, 15415), ("00016.m2ts", 2521)))
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and planned == 2 and mkv.reads == 1 and mkv.short_reads == 1


def test_a_short_title_never_listed_counts_as_not_ripped(tmp_path, monkeypatch):
    got, planned, mkv, _ = rip(tmp_path, monkeypatch, reading(DEFAULT), DEFAULT, ids=(PLAY_ALL, CLIP), on_disc=SCAN)
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and planned == 2 and mkv.short_reads == 1


def test_a_short_one_clip_playlist_listed_as_its_stream_file_keeps_its_identity():
    """Lengths alone couldn't tell several 8-second outtakes apart."""
    ours = [{"title_id": "00742.mpls", "role": "junk", "duration": 8, "clips": "00042"},
            {"title_id": "00749.mpls", "role": "junk", "duration": 8, "clips": "00049"},
            {"title_id": "00700.mpls", "role": "loop", "duration": 8, "clips": "00070"}]
    mk = [{"index": 0, "source": "00049.m2ts", "duration": 8}, {"index": 1, "source": "00070.m2ts", "duration": 8}]
    pairs = core.map_makemkv(mk, ours, "Blu-ray")
    assert pairs[0][0]["title_id"] == "00749.mpls" and pairs[0][1] == "clip"
    assert 1 not in pairs                                  # (a menu loop stays nobody's title)


READ_ERROR = ('MSG:2003,0,3,"Error \'Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR\' occurred while reading \'/VIDEO_TS/VTS_03_1.VOB\' at offset \'12787712\'",'
              '"Error \'%1\' occurred while reading \'%2\' at offset \'%3\'","Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR","/VIDEO_TS/VTS_03_1.VOB","12787712"\n')
NONE_SAVED = 'MSG:5037,260,2,"Copy complete. 0 titles saved, 1 failed.","Copy complete. %1 titles saved, %2 failed.","0","1"\n'


def test_a_failed_rip_isnt_filed_even_with_a_partial_file_and_a_clean_exit(tmp_path, monkeypatch):
    got, planned, mkv, _ = rip(tmp_path, monkeypatch, reading(DEFAULT), DEFAULT, fail=[READ_ERROR, NONE_SAVED])
    assert got == {} and planned == 1
    assert "MEDIUM ERROR" in mkv.app.rip_problem
    assert not [f for f in os.listdir(tmp_path / "out") if f.endswith(".mkv")]


def test_no_count_in_the_messages_falls_back_on_the_exit_code(tmp_path, monkeypatch):
    """As in another language: the file and a clean exit are all there is to go on."""
    got, _, mkv, _ = rip(tmp_path, monkeypatch, reading(DEFAULT), DEFAULT, fail=[])
    assert got == {PLAY_ALL + ".mkv": PLAY_ALL} and mkv.app.rip_problem is None


def test_makemkv_problem_and_count_read_its_messages():
    assert mk.makemkv_saved('MSG:5005,128,1,"1 titles saved","%1 titles saved","1"\n') == 1
    assert mk.makemkv_saved(NONE_SAVED) == 0
    assert mk.makemkv_saved(READ_ERROR) is None and mk.makemkv_saved('TINFO:0,9,0,"1 titles saved"') is None
    assert mk.makemkv_problem(READ_ERROR).startswith("Error 'Scsi error")
    assert mk.makemkv_problem(NONE_SAVED) is None            # says that it failed, not why
    assert mk.makemkv_problem('MSG:5010,0,0,"Failed to open disc","Failed to open disc"\n') == "Failed to open disc"
    assert mk.makemkv_problem('MSG:5011,0,0,"Operation successfully completed","Operation successfully completed"\n') is None
    assert mk.makemkv_problem('MSG:3016,0,1,"Title #00001.mpls was skipped","Title #%1 was skipped","00001.mpls"\n') is None


def outcome_of(monkeypatch, done, planned, problem):
    seen = []
    monkeypatch.setattr(dd_app.th, "set_dot", lambda *a: None)
    fake = types.SimpleNamespace(show_progress=lambda on: None, set_busy=lambda on: None, rip_dot=None, rep=None, cancelled=False,
                                 last_out=None, rip_short=[], rip_problem=problem, set_outcome=lambda text, colour: seen.append(text))
    dd_app.App.rip_done(fake, done, planned, None)
    return seen[0]


def test_the_outcome_shows_what_makemkv_said(monkeypatch):
    text = outcome_of(monkeypatch, [], 1, "MakeMKV says: Failed to open disc")
    assert text == "0 of 1 titles ripped. MakeMKV says: Failed to open disc"


def test_the_outcome_without_a_message_stays_as_it_was(monkeypatch):
    assert outcome_of(monkeypatch, [], 2, None) == "Evidence bagged: 0 of 2 titles ripped (details in the logs)"


def test_a_title_that_recovered_says_nothing_of_earlier_problems(monkeypatch):
    assert outcome_of(monkeypatch, ["a/b.mkv"], 1, "Failed to open disc") == "Evidence bagged: 1 of 1 titles ripped"


def test_added_titles_of_a_blu_ray_ignore_message_order():
    forward = reading(DEFAULT)
    backward = "".join(reversed(forward.splitlines(True)))
    assert mk.makemkv_added(forward) == mk.makemkv_added(backward) == [(PLAY_ALL, "0"), (EXTRA, "1")]
    assert mk.makemkv_added(reading(DEFAULT)) != mk.makemkv_added(reading(EVERYTHING))


def test_added_titles_of_a_dvd_keep_their_order():
    assert mk.makemkv_added(dvd_reading([1, 3, 4])) == ["1", "3", "4"]
    assert mk.makemkv_added(dvd_reading([1, 3, 4])) != mk.makemkv_added(dvd_reading([1, 2, 3, 4]))


def test_added_titles_of_text_without_them():
    assert mk.makemkv_added("") == [] and mk.makemkv_added('MSG:5011,0,0,"Done","Done"\n') == []
