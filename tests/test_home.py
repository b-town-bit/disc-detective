"""Where things live, and moving data over from the old name (disc_identify)."""
import os

from disc_detective import core


def test_old_folder_moves_over(tmp_path, monkeypatch):
    user = tmp_path / "user2"
    old = user / ".disc_identify"
    (old / "imdb").mkdir(parents=True)
    (old / "library.sqlite").write_bytes(b"lib")
    (old / "disc_ripper.json").write_text('{"tmdb_key": "abc"}')
    monkeypatch.delenv("DISC_DETECTIVE_HOME")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setattr(core, "LEGACY_HOME", str(old))
    home = core.data_home()
    assert home == os.path.join(str(user), ".disc_detective")
    assert not old.exists()
    assert open(os.path.join(home, "library.sqlite"), "rb").read() == b"lib"
    assert os.path.exists(os.path.join(home, "settings.json"))
    assert core.default_library() == os.path.join(home, "library.sqlite")


def test_old_environment_names_still_work(monkeypatch, tmp_path):
    monkeypatch.setenv("DISC_IDENTIFY_DB", str(tmp_path / "x.sqlite"))
    assert core.default_library() == str(tmp_path / "x.sqlite")
    monkeypatch.setenv("DISC_DETECTIVE_DB", str(tmp_path / "y.sqlite"))
    assert core.default_library() == str(tmp_path / "y.sqlite")


def test_discs_to_submit_to_thediscdb(tmp_path):
    """Your library's discs that TheDiscDB lacks, best first: confirmed ones (ready), then
    probable, then best guesses; TheDiscDB's own discs and unidentified ones are left out."""
    import sqlite3
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    rows = [("A" * 32, "identified (TheDiscDB)", "Known Film (2001)", None),
            ("B" * 32, "probable: Probable Film (2002) [IMDb]", "Probable Film (2002)", None),
            ("C" * 32, "best guess: Guess Film (2003) [IMDb]; check it", "Guess Film (2003)", None),
            ("D" * 32, "probable: Wrong (1999) [IMDb]", "Wrong (1999)", "Confirmed Film (2004)"),
            ("E" * 32, "unidentified; needs a label or manual search", None, None),
            ("F" * 32, "probable: Added Since (2005) [IMDb]", "Added Since (2005)", None)]
    with con:
        for h, status, guess, truth in rows:
            con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                        "VALUES (?, 'Blu-ray', 'LABEL', ?, ?, ?, '2026', '2026')", (h, status, guess, truth))
            con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, '00800.mpls', 'feature', 6000)", (h,))
    index = sqlite3.connect(":memory:")
    index.row_factory = sqlite3.Row
    index.executescript(core.DISCDB_INDEX_SCHEMA)
    index.execute("INSERT INTO discs (hash, title, titles) VALUES (?, 'Added Since', '[]')", ("F" * 32,))
    got = core.submission_candidates(con, index)
    assert [(r["title"], r["ready"]) for r in got] == [
        ("Confirmed Film (2004)", "ready"), ("Probable Film (2002)", "check"), ("Guess Film (2003)", "confirm")]


def test_the_packet_checklist_helps_without_the_case():
    pk = {"truth": "Braveheart (1995)", "content_hash": "F" * 32, "imdb_id": "tt0112573", "mastered": "2018-03",
          "makemkv_log": "disc01.txt", "makemkv_disc_name": "Braveheart", "real_matched": 1, "real_titles": 1,
          "labels": []}
    text = core.packet_checklist(pk, core.parse_truth(pk["truth"]))
    assert "UPC (required)" in text and "ASIN (optional)" in text and "amazon.com/dp/" in text
    assert "Blu-ray.com" in text and "(2018-03)" in text


def test_discs_whose_thediscdb_entry_could_be_labelled_better():
    """TheDiscDB has the disc but leaves titles unlabelled: real extras are listed; pieces of the
    film (seamless branching), play-alls of named scenes and films it already names are not."""
    import sqlite3
    import json
    con = core.library_open(":memory:")
    h = "G" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, first_seen, last_seen) "
                    "VALUES (?, 'Blu-ray', 'GLADIATOR', 'identified (TheDiscDB)', 'Gladiator (2000) | 4K', '2026', '2026')", (h,))
        for tid, role, dur, clips in [("00389.mpls", "feature", 9300, "00498,00499,00500"),
                                      ("00610.mpls", "feature", 26000, "00498,00700"),       # a maximizer
                                      ("00356.mpls", "extra", 1030, "00498"),                # a piece of the film
                                      ("00212.mpls", "extra", 720, "01310,01311"),           # play-all of named scenes
                                      ("00400.mpls", "extra", 600, "00800"),                 # a real, unnamed extra
                                      ("00401.mpls", "extra", 60, "00801")]:                 # too short to bother
            con.execute("INSERT INTO titles (hash, title_id, role, duration, clips) VALUES (?,?,?,?,?)", (h, tid, role, dur, clips))
    index = sqlite3.connect(":memory:")
    index.row_factory = sqlite3.Row
    index.executescript(core.DISCDB_INDEX_SCHEMA)
    titles = [{"source": "00389.mpls", "item": {"title": "Gladiator", "type": "MainMovie"}},
              {"source": "01310.m2ts", "item": {"title": "Scene", "type": "DeletedScene"}},
              {"source": "00400.mpls", "item": {}}]
    index.execute("INSERT INTO discs (hash, title, release, disc, titles) VALUES (?, 'Gladiator', '4K', '4K UHD Disc', ?)",
                  (h, json.dumps(titles)))
    got = core.improvement_candidates(con, index)
    assert len(got) == 1 and got[0]["title"] == "Gladiator (2000)"
    assert got[0]["titles"] == {"feature": [], "episode": [], "extra": ["00400.mpls"]} and got[0]["what"] == "1 extra"


def test_your_labels_survive_a_rescan_and_feed_packets_and_improvements(tmp_path):
    """Labels you give a disc's titles stay through rescans (the titles are rewritten), fill
    in a submission packet, and show where they'd fill a gap in TheDiscDB or differ from it."""
    import json
    import sqlite3
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "H" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'Blu-ray', 'FILM', 'identified (TheDiscDB)', 'Film (2001)', 'Film (2001)', '2026', '2026')", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration, clips) VALUES (?, '00800.mpls', 'feature', 6000, '00001')", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration, clips) VALUES (?, '00010.mpls', 'extra', 90, '00010')", (h,))
    core.set_label(con, h, "00010.mpls", "Trailer", "Theatrical Trailer")
    core.set_label(con, h, "00800.mpls", "MainMovie", "Film: Director's Cut")
    with con:                                        # a rescan rewrites the titles
        con.execute("DELETE FROM titles WHERE hash=?", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration, clips) VALUES (?, '00800.mpls', 'feature', 6000, '00001')", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration, clips) VALUES (?, '00010.mpls', 'extra', 90, '00010')", (h,))
    assert core.get_labels(con, h)["00010.mpls"] == {"type": "Trailer", "title": "Theatrical Trailer",
                                                      "season": None, "episode": None}
    index = sqlite3.connect(":memory:")
    index.row_factory = sqlite3.Row
    index.executescript(core.DISCDB_INDEX_SCHEMA)
    index.execute("INSERT INTO discs (hash, title, release, disc, titles) VALUES (?, 'Film', 'BD', 'Disc', ?)",
                  (h, json.dumps([{"source": "00800.mpls", "item": {"title": "Film", "type": "MainMovie"}}])))
    got = core.improvement_candidates(con, index)[0]
    assert got["yours"] == ["00010.mpls"] and got["differs"] == ["00800.mpls"]   # a short title, but yours
    assert "1 labelled by you" in got["what"] and "1 where your label differs" in got["what"]
    log = "\n".join(['CINFO:2,0,"Film"', 'TINFO:0,9,0,"1:40:00"', 'TINFO:0,16,0,"00800.mpls"', 'TINFO:0,27,0,"t00.mkv"',
                     'TINFO:1,9,0,"0:01:30"', 'TINFO:1,16,0,"00010.mpls"', 'TINFO:1,27,0,"t01.mkv"'])
    pk = core.build_packet(con, h, log, str(tmp_path / "packet"))
    lab = {l["source"]: l for l in pk["labels"]}
    assert (lab["00010.mpls"]["type"], lab["00010.mpls"]["title"], lab["00010.mpls"]["note"]) == (
        "Trailer", "Theatrical Trailer", "your label")
    core.set_label(con, h, "00010.mpls", None, "")    # cleared
    assert "00010.mpls" not in core.get_labels(con, h)


def test_scans_store_each_titles_tracks():
    t = {"video_info": {"codec": "AVC", "height": 1080, "audio": ["x"]},
         "audio": [{"lang": "en", "codec": "DTS-HD MA", "channels": 6, "kind": ""}], "subs": [{"lang": "en", "kind": ""}]}
    import json
    got = json.loads(core.title_tracks(t))
    assert got["video"] == {"codec": "AVC", "height": 1080} and got["audio"][0]["codec"] == "DTS-HD MA"
    assert core.title_tracks({"audio": [], "subs": []}) is None


def test_labels_change_what_the_scan_says_a_title_is():
    """You watched the disc: a title the scan called an extra is an episode; the deleted scene you
    labelled was the one the scan had as a duplicate, so its twin becomes the copy; an ad is a
    promo. Clearing a label puts the scan's reading back."""
    titles = [{"id": "Title 5", "role": "extra", "note": None},
              {"id": "Title 6", "role": "extra", "note": None},
              {"id": "Title 7", "role": "duplicate", "note": "same video as Title 6"},
              {"id": "Title 9", "role": "extra", "note": None}]
    labels = {"Title 5": {"type": "Episode", "title": "Pilot", "season": 1, "episode": 1},
              "Title 7": {"type": "DeletedScene", "title": "Pilot Deleted Scene", "season": 1, "episode": 1},
              "Title 9": {"type": "Promo", "title": "Coming Soon to DVD"}}
    core.apply_labels(titles, labels)
    roles = {t["id"]: (t["role"], t["note"]) for t in titles}
    assert roles["Title 5"] == ("episode", "your label: Episode")
    assert roles["Title 7"] == ("extra", "your label: DeletedScene")
    assert roles["Title 6"] == ("duplicate", "same video as Title 7")
    assert roles["Title 9"][0] == "promo"
    core.apply_labels(titles, {})                       # all cleared
    assert [(t["role"], t["note"]) for t in titles] == [("extra", None), ("extra", None),
                                                         ("duplicate", "same video as Title 6"), ("extra", None)]


def test_duplicates_carry_no_guessed_episode_name():
    """Psych S8, the last disc: the scan guessed S08E07 for the 48-minute title by length. You
    labelled its copy S08E10, so the copy counts and the first becomes the duplicate: a duplicate
    needs no name, so the guess goes; a labelled title takes your name. Other guesses stay, and
    clearing your labels brings the guess back."""
    rep = {"titles": [{"id": "Title 13", "role": "episode"}, {"id": "Title 14", "role": "duplicate",
                                                               "note": "same video as Title 13"},
                      {"id": "Title 15", "role": "episode"}, {"id": "Title 2", "role": "episode"}],
           "plex_names": ["Title 13 -> Psych (2006) {imdb-tt0491738}/Season 08/Psych (2006) - s08e07 - Truck.mkv",
                          "Title 15 -> Psych (2006) {imdb-tt0491738}/Season 08/Psych (2006) - s08e08-e09 - X.mkv",
                          "Title 2 -> Psych (2006) {imdb-tt0491738}/Season 08/Psych (2006) - s08e06 - Y.mkv"],
           "labels": {"Title 14": {"type": "Episode", "title": "The Break Up", "season": 8, "episode": 10},
                      "Title 15": {"type": "Extra", "title": "Psych: The Musical"}}}
    core.apply_labels(rep["titles"], rep["labels"])
    dropped = core.labels_over_guess(rep)
    assert len(dropped) == 2 and [n.split(" -> ")[0] for n in rep["plex_names"]] == ["Title 2"]
    rep["labels"] = {}
    core.apply_labels(rep["titles"], rep["labels"])
    core.labels_over_guess(rep)
    assert len(rep["plex_names"]) == 3
