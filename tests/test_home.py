"""Where things live, and moving data over from the old name (disc_identify)."""
import os
import re

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


def test_discs_to_submit_to_thediscdb(tmp_path, monkeypatch):
    """Your library's discs that TheDiscDB lacks. Ready to submit: confirmed, the lab's reading of every title (none
    read short) and titles labelled. The rest say what they need, probable answers before best guesses; TheDiscDB's
    own discs and unidentified ones are left out."""
    import sqlite3
    logs = tmp_path / "makemkv_logs"
    logs.mkdir()
    monkeypatch.setattr(core, "makemkv_log_store", lambda h: str(logs / ("%s-all.txt" % h)))
    monkeypatch.setattr(core, "makemkv_old_log_store", lambda h: str(logs / ("%s.txt" % h)))
    reading = 'TINFO:0,9,0,"1:40:00"\nTINFO:0,16,0,"00800.mpls"\nTINFO:0,27,0,"t00.mkv"\n'
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

    def d_row(rows):
        return next(r for r in rows if r["hash"] == "D" * 32)
    got = core.submission_candidates(con, index)
    assert [(r["title"], r["needs"]) for r in got] == [
        ("Confirmed Film (2004)", ["lab", "film"]), ("Probable Film (2002)", ["check", "lab", "film"]),
        ("Guess Film (2003)", ["confirm", "lab", "film"])]
    # the lab's reading and an extra labelled, not the film: not yet
    (logs / ("%s-all.txt" % ("D" * 32))).write_text(reading + 'TINFO:1,9,0,"0:05:00"\nTINFO:1,16,0,"00801.mpls"\n'
                                                    'TINFO:2,9,0,"0:04:00"\nTINFO:2,16,0,"00802.mpls"\n')
    with con:
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, '00801.mpls', 'extra', 300)", ("D" * 32,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, '00802.mpls', 'extra', 240)", ("D" * 32,))
    core.set_label(con, "D" * 32, "00801.mpls", "Featurette", "Making Of")
    got = core.submission_candidates(con, index)
    assert got[0]["needs"] == ["film"] and got[0]["why"] == "needs the film labelled"
    # the film labelled: ready, though an extra isn't (extras aren't needed on a film's disc)
    core.set_label(con, "D" * 32, "00800.mpls", "MainMovie", "Confirmed Film")
    got = core.submission_candidates(con, index)
    assert (got[0]["title"], got[0]["ready"], got[0]["needs"], got[0]["labelled"]) == ("Confirmed Film (2004)", "ready", [], 2)
    assert got[0]["why"] == "confirmed by you, 2 titles labelled (1 extra not), lab report on file"
    # a bonus disc (the scan made everything an extra): each extra of two minutes or more, not a short clip
    with con:
        con.execute("UPDATE titles SET role='extra' WHERE hash=? AND title_id='00800.mpls'", ("D" * 32,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, '00803.mpls', 'extra', 30)", ("D" * 32,))
        con.execute("DELETE FROM labels WHERE hash=? AND title_id='00800.mpls'", ("D" * 32,))
    got = core.submission_candidates(con, index)
    assert d_row(got)["needs"] == ["extras"] and d_row(got)["why"] == "needs 2 extras labelled"
    # a TV disc: every episode, with its season and number
    with con:
        con.execute("UPDATE titles SET role='episode' WHERE hash=? AND title_id IN ('00800.mpls', '00802.mpls')", ("D" * 32,))
    core.set_label(con, "D" * 32, "00800.mpls", "Episode", "Pilot", 1, 1)
    core.set_label(con, "D" * 32, "00802.mpls", None, "Second", None, None)
    got = core.submission_candidates(con, index)
    assert d_row(got)["needs"] == ["episodes"] and d_row(got)["why"] == "needs 1 episode labelled"
    core.set_label(con, "D" * 32, "00802.mpls", "Episode", "Second", 1, 2)
    core.set_label(con, "D" * 32, "00800.mpls", "Episode", "Pilot", 1, 1)
    assert d_row(core.submission_candidates(con, index))["needs"] == []
    with con:
        con.execute("UPDATE titles SET role='feature' WHERE hash=? AND title_id='00800.mpls'", ("D" * 32,))
        con.execute("UPDATE titles SET role='extra' WHERE hash=? AND title_id='00802.mpls'", ("D" * 32,))
    core.set_label(con, "D" * 32, "00800.mpls", "MainMovie", "Confirmed Film")
    core.set_label(con, "D" * 32, "00802.mpls")
    # a reading with the film well short (a dirty disc), or one kept at MakeMKV's minimum length: not ready
    (logs / ("%s-all.txt" % ("D" * 32))).write_text(reading.replace("1:40:00", "1:10:00"))
    assert core.submission_candidates(con, index)[0]["needs"] == ["lab-short"]
    (logs / ("%s-all.txt" % ("D" * 32))).unlink()
    (logs / ("%s.txt" % ("D" * 32))).write_text(reading)
    assert core.submission_candidates(con, index)[0]["needs"] == ["lab-old"]
    assert "needs a fresh lab report" in core.submission_candidates(con, index)[0]["why"]


def test_the_packet_checklist_helps_without_the_case():
    pk = {"truth": "Braveheart (1995)", "content_hash": "F" * 32, "imdb_id": "tt0112573", "mastered": "2018-03",
          "makemkv_log": "disc01.txt", "makemkv_disc_name": "Braveheart", "real_matched": 1, "real_titles": 1,
          "labels": []}
    text = core.packet_checklist(pk, core.parse_truth(pk["truth"]))
    assert "UPC/EAN (required)" in text and "ASIN (required" in text and "amazon.com/dp/" in text
    assert "Blu-ray.com" in text and "(2018-03)" in text
    # the form's fields in its order, the edition named as TheDiscDB's are ("2011 Blu-ray", "2011-blu-ray")
    order = ["ASIN", "UPC/EAN", "Release Date", "Front Image", "Release Title", "Release Slug", "Locale", "Region Code"]
    assert [text.index("- [ ] " + f) for f in order] == sorted(text.index("- [ ] " + f) for f in order)
    assert "**2018 Blu-ray**" in text and "**2018-blu-ray**" in text and "A, B or C" in text
    dvd = core.packet_checklist(dict(pk, format="DVD"), core.parse_truth(pk["truth"]))
    assert "**2018 DVD**" in dvd and "**2018-dvd**" in dvd and "1 (US and Canada)" in dvd
    # the site's pages in order: the disc to hand (their form scans it), the edition found, the disc added,
    # its log, its titles, then submitted
    steps = ["## Before you start", "## 1.", "## 2.", "## 3. Add the disc", "## 4. Disc log", "## 5.", "## 6."]
    assert [dvd.index(s) for s in steps] == sorted(dvd.index(s) for s in steps)
    assert "disc in the drive" in dvd and "IFO files" in dvd and "AACS key file" in text
    assert "amazon.com/s?k=Braveheart+DVD" in dvd and "section=dvdmovies" in dvd and "section=bluraymovies" in text
    assert "not the film's poster" in dvd and "fingerprint" not in dvd
    # one disc alone: named by its format, not the film (shown already); a set's by its number
    assert "Disc Title" in dvd and "**DVD**" in dvd and "**dvd**" in dvd and "the drive itself" in dvd
    assert "**Blu-ray**" in text and "**blu-ray**" in text and "BDMV inside" in text
    set_disc = core.packet_checklist(dict(pk, format="DVD", truth="Braveheart (1995) | Disc 2"),
                                     core.parse_truth("Braveheart (1995) | Disc 2"))
    assert "**Disc 2**" in set_disc and "**disc-2**" in set_disc
    assert "Manual Upload: **disc01.txt**" in dvd and "Description is optional" in dvd


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
    assert got[0]["edits"] == []                    # (found by the scan, not labelled by you: nothing to suggest yet)


def test_your_labels_to_suggest_on_thediscdb_title_by_title():
    """A disc TheDiscDB has: your labels it lacks or has otherwise, one edit per title, by its name there (a playlist
    of one clip it lists as that clip: Half-Blood Prince's 00200.mpls is its 00001.m2ts), with its length there and
    its own page (the disc's address, then the file: .../discs/movie/00001/m2ts). One it already has as you do: none."""
    import sqlite3
    import json
    con = core.library_open(":memory:")
    h = "H" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, truth, first_seen, last_seen) VALUES "
                    "(?, 'Blu-ray', 'HP6', 'identified (TheDiscDB)', 'Harry Potter and the Half-Blood Prince (2009)', "
                    "'2026', '2026')", (h,))
        for tid, role, dur, clips in [("00100.mpls", "feature", 9210, "00000"), ("00200.mpls", "extra", 207, "00001"),
                                      ("00202.mpls", "extra", 168, "00003"), ("00122.mpls", "extra", 604, "00680")]:
            con.execute("INSERT INTO titles (hash, title_id, role, duration, clips) VALUES (?,?,?,?,?)", (h, tid, role, dur, clips))
    for tid, kind, name in [("00100.mpls", "MainMovie", "Harry Potter and the Half-Blood Prince"),
                            ("00200.mpls", "Featurette", "Close-Up with the Cast"), ("00202.mpls", "Extra", None),
                            ("00122.mpls", "Trailer", "Theatrical Trailer")]:
        core.set_label(con, h, tid, kind, name)
    index = sqlite3.connect(":memory:")
    index.row_factory = sqlite3.Row
    index.executescript(core.DISCDB_INDEX_SCHEMA)
    titles = [{"source": "00100.mpls", "duration": "2:33:30",
               "item": {"title": "Harry Potter and the Half-Blood Prince", "type": "MainMovie"}},
              {"source": "00001.m2ts", "duration": "0:03:27", "item": {}},
              {"source": "00003.m2ts", "duration": "0:02:48", "item": {}},
              {"source": "00122.mpls", "duration": "0:10:04", "item": {"title": "Trailer", "type": "Extra"}}]
    index.execute("INSERT INTO discs (hash, kind, title, year, release, slug, disc, titles) VALUES "
                  "(?, 'Movie', 'Harry Potter and the Half-Blood Prince', 2009, '2009 Special Edition', "
                  "'2009-special-edition', 'Movie', ?)", (h, json.dumps(titles)))
    got = core.improvement_candidates(con, index)
    edits = {e["source"]: e for e in got[0]["edits"]}
    # (not the film: it has it as you do; nor 00003, a type with no name, which their form won't take)
    assert sorted(edits) == ["00001.m2ts", "00122.mpls"]
    e = edits["00001.m2ts"]
    assert (e["our_title"], e["duration"], e["type"], e["title"], e["fix"]) == (
        "00200.mpls", "0:03:27", "Featurette", "Close-Up with the Cast", False)
    assert e["url"] == ("https://thediscdb.com/movie/harry-potter-and-the-half-blood-prince-2009/releases/"
                        "2009-special-edition/discs/movie/00001/m2ts/edit")
    assert edits["00122.mpls"]["fix"] and edits["00122.mpls"]["was"] == {"type": "Extra", "title": "Trailer"}
    # submitted: dated; a month on, still not in TheDiscDB's data, worth a look; your label changed since: due again
    core.set_suggested(con, h, "00200.mpls", edits["00001.m2ts"])
    with con:
        con.execute("UPDATE suggested SET at='2026-01-01T00:00:00'")
    e = {x["source"]: x for x in core.improvement_candidates(con, index)[0]["edits"]}["00001.m2ts"]
    assert e["submitted"] == "2026-01-01T00:00:00" and e["stale"]
    core.set_label(con, h, "00200.mpls", "Featurette", "Close-Up With the Cast, Again")
    e = {x["source"]: x for x in core.improvement_candidates(con, index)[0]["edits"]}["00001.m2ts"]
    assert e["submitted"] is None


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


def test_a_promo_goes_to_thediscdb_as_a_trailer_by_its_films_name(tmp_path):
    """The Swan Princess's DVD: its ads are Promos here, "Previews - Daddy Day Care" (the one namer's), and
    Trailers there by the film alone, as their other films' trailers are ("Daddy Day Care"): so its packet
    says, and a TheDiscDB entry naming it so isn't one your label differs from. An ad named by nothing more
    keeps its name; other labels go as they are."""
    import json
    import sqlite3
    assert core.discdb_label("Promo", "Previews - Daddy Day Care") == ("Trailer", "Daddy Day Care")
    assert core.discdb_label("Promo", "Sneak Peeks - Hitch") == ("Trailer", "Hitch")
    assert core.discdb_label("Promo", "Previews") == ("Trailer", "Previews")
    assert core.discdb_label("Extra", "Previews - Behind the Scenes") == ("Extra", "Previews - Behind the Scenes")
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "S" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'DVD', 'SWAN', 'identified', 'The Swan Princess (1994)', 'The Swan Princess (1994)', "
                    "'2026', '2026')", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, 'Title 1', 'feature', 5330)", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, 'Title 9', 'promo', 139)", (h,))
    core.set_label(con, h, "Title 1", "MainMovie", "The Swan Princess")
    core.set_label(con, h, "Title 9", "Promo", "Previews - Daddy Day Care")
    log = "\n".join(['CINFO:2,0,"Swan Princess"', 'TINFO:0,9,0,"1:28:50"', 'TINFO:0,24,0,"1"', 'TINFO:0,27,0,"t00.mkv"',
                     'TINFO:1,9,0,"0:02:19"', 'TINFO:1,24,0,"9"', 'TINFO:1,27,0,"t01.mkv"'])
    pk = core.build_packet(con, h, log, str(tmp_path / "packet"))
    lab = {l["our_title"]: l for l in pk["labels"]}
    assert (lab["Title 9"]["type"], lab["Title 9"]["title"]) == ("Trailer", "Daddy Day Care")
    assert ("09", "Trailer", "Daddy Day Care", "") in pk["steps"][5]["titles"]
    index = sqlite3.connect(":memory:")
    index.row_factory = sqlite3.Row
    index.executescript(core.DISCDB_INDEX_SCHEMA)
    index.execute("INSERT INTO discs (hash, title, release, disc, titles) VALUES (?, 'The Swan Princess', 'SE', 'DVD', ?)",
                  (h, json.dumps([{"source": "01", "item": {"title": "The Swan Princess", "type": "MainMovie"}},
                                  {"source": "09", "item": {"title": "Daddy Day Care", "type": "Trailer"}}])))
    assert core.improvement_candidates(con, index) == []                 # (nothing of yours it lacks or has otherwise)


def test_a_packet_is_the_log_and_the_forms_pages_walked_through(tmp_path):
    """The packet's one file is the one their form takes, MakeMKV's log (an older packet's other files gone); what
    goes in the form comes a page at a time in its order (submission_steps), the titles inline by their Playlist
    number: your labelled titles only, in MakeMKV's order, an episode's season and episode beside it."""
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "S" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'Blu-ray', 'FILM', 'x', 'Film (2001)', 'Film (2001)', '2026', '2026')", (h,))
        for tid, role, dur in (("00800.mpls", "feature", 6000), ("00010.mpls", "extra", 312), ("00011.mpls", "extra", 200)):
            con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, ?, ?, ?)", (h, tid, role, dur))
    core.set_label(con, h, "00800.mpls", "MainMovie", "")            # named after the film
    core.set_label(con, h, "00010.mpls", "Extra", "Gag Reel: Take 2?")
    t = lambda i, src, dur, ch, size, segs: [
        'TINFO:%d,8,0,"%d"' % (i, ch), 'TINFO:%d,9,0,"%s"' % (i, dur), 'TINFO:%d,10,0,"%s"' % (i, size),
        'TINFO:%d,16,0,"%s"' % (i, src), 'TINFO:%d,25,0,"1"' % i, 'TINFO:%d,26,0,"%s"' % (i, segs),
        'TINFO:%d,27,0,"t%02d.mkv"' % (i, i)]
    log = "\n".join(['CINFO:2,0,"Film"'] + t(0, "00800.mpls", "1:40:00", 28, "23.2 GB", "26") +
                    t(1, "00010.mpls", "0:05:12", 1, "144.2 MB", "10") + t(2, "00011.mpls", "0:03:20", 1, "90 MB", "11"))
    out = tmp_path / "packet"
    out.mkdir()
    for old in ("labels.json", "checklist.md", "disc01-summary.txt"):
        (out / old).write_text("an older packet's")
    pk = core.build_packet(con, h, log, str(out), root="E:\\")
    assert sorted(p.name for p in out.iterdir()) == ["disc01.txt"]
    assert (out / "disc01.txt").read_text(encoding="utf-8") == log and pk["log_path"] == str(out / "disc01.txt")
    steps = {s["head"]: s for s in pk["steps"]}
    assert list(steps) == ["Before you start", "Identify the release", "Release details", "Add the disc", "Disc log",
                           "Identify the titles", "Review and submit"]
    assert steps["Identify the titles"]["titles"] == [("00800.mpls", "MainMovie", "Film", ""),
                                                      ("00010.mpls", "Extra", "Gag Reel: Take 2?", "")]
    assert "Leave the other 1 title blank" in steps["Identify the titles"]["after"][0]
    assert steps["Disc log"]["file"] == pk["log_path"] and steps["Disc log"]["fields"][0][1] == "disc01.txt"
    fields = {f[0]: f[1] for f in steps["Add the disc"]["fields"]}
    assert fields["Select Disc Root Folder"] == "E:\\" and fields["Disc Title"] == "Blu-ray"
    assert fields["Disc Slug"] == "blu-ray" and fields["Disc Format"] == "Blu-ray"
    # what's to copy: what their form has typed in, or pasted in its pickers (the log's whole path); not a drop-down's
    copy = {f[0]: f[3] for s in pk["steps"] for f in s["fields"]}
    assert copy["Disc Title"] == "Blu-ray" and copy["Select Disc Root Folder"] == "E:\\"
    assert copy["Manual Upload"] == pk["log_path"] and copy["Disc Format"] is None and copy["Region Code"] is None
    # no TMDB id known: none asked for, their search by the title finds the film
    assert "TMDB ID" not in copy and copy["Search for"]

    # an episode, from a DVD: its title number, and season and episode
    ep = dict(pk, labels=[{"sent": True, "title": "About Last Knight", "source": 7, "type": "Episode", "season": 2,
                           "episode": 6, "role": "episode", "our_title": "Title 7"}])
    titles = {s["head"]: s for s in core.submission_steps(ep, core.parse_truth("Galavant S2"))}["Identify the titles"]
    assert titles["titles"] == [("07", "Episode", "About Last Knight", "S02E06")]
    assert "Episode order follows the disc's play-all" in titles["after"][0]
    # the edition the disc names itself (found as it's verified: release_edition): the release's name, the edition
    # looked for, the mastering year only a floor
    sp = dict(pk, mastered="2008-02", edition={"name": "Special Edition", "from": "its own name",
                                               "text": "Swan Princess (Special Edition)"})
    rel = {s["head"]: s for s in core.submission_steps(sp, None)}["Release details"]
    f = {x[0]: x for x in rel["fields"]}
    assert f["Release Title (required)"][1] == "2008 Special Edition" and "mastered" in f["Release Title (required)"][2]
    assert f["Release Slug (required)"][1] == "2008-special-edition" and f["Release Slug (required)"][3] is None
    first = {s["head"]: s for s in core.submission_steps(sp, None)}["Before you start"]
    assert any("calls itself the Special Edition" in n for n in first["notes"])
    assert "Special+Edition" in first["links"][0][1] and "says Special Edition" in first["after"][0]
    # the edition's ASIN, UPC and date, typed in before you start (as copied: the listing's address, a spaced
    # barcode, Amazon's date): given back in their form's terms, the release's name from the date's year
    assert [k for k, _, _ in first["inputs"]] == ["asin", "upc", "date"]
    sp["release"] = {"asin": "https://www.amazon.com/Swan-Princess/dp/B002CAWNHE/ref=sr_1_1", "upc": "0 43396 32266 0",
                     "date": "2009-08-18"}
    f = {x[0]: x for x in {s["head"]: s for s in core.submission_steps(sp, None)}["Release details"]["fields"]}
    assert f["ASIN (required)"][3] == "B002CAWNHE" and f["UPC/EAN (required)"][3] == "043396322660"
    assert f["Release Date (required)"][3] == "August 18, 2009"
    assert f["Release Title (required)"][1] == "2009 Special Edition" and "mastered" not in f["Release Title (required)"][2]
    assert f["Release Slug (required)"][1] == "2009-special-edition"
    # what isn't one yet: nothing to copy, and where to put it
    sp["release"] = {"asin": "xx", "upc": "43396322660", "date": "2009"}
    f = {x[0]: x for x in {s["head"]: s for s in core.submission_steps(sp, None)}["Release details"]["fields"]}
    assert not any(f[k][3] for k in ("ASIN (required)", "UPC/EAN (required)", "Release Date (required)"))
    assert "Before you start" in f["UPC/EAN (required)"][2]


def test_an_editions_details_as_typed_or_copied():
    """The ASIN, UPC and release date as they're found: the listing's address or the id, a barcode spaced as it's
    printed, a date as Amazon or anyone writes it."""
    for t in ("B002CAWNHE", "b002cawnhe", "https://www.amazon.com/dp/B002CAWNHE?th=1",
              "https://www.amazon.com/Swan-Princess/dp/B002CAWNHE/ref=sr_1_1"):
        assert core.clean_asin(t) == "B002CAWNHE"
    assert core.clean_asin("xx") is None and core.clean_asin(None) is None
    assert core.clean_upc("0 43396 32266 0") == "043396322660" and core.clean_upc("4006680012345") == "4006680012345"
    assert core.clean_upc("43396322660") is None                    # (a digit short: the leading 0 lost)
    for t in ("August 18, 2009", "Aug 18,2009", "2009-08-18", "08/18/2009", " 8/18/2009 ", "18 August 2009"):
        assert str(core.release_date(t)) == "2009-08-18"
    assert core.release_date("2009") is None and core.release_date(None) is None


def test_the_edition_a_disc_names_itself(tmp_path, monkeypatch):
    """Worked out as the disc's verified for submission, never stored, in order: its own name (a DVD's text data,
    a Blu-ray's metadata title), its volume label (the one recorded, for a backup folder), then a cover banner read
    cleanly and of edition words alone. A title's edition (a menu button's) isn't the disc's."""
    assert core.edition_in("Swan Princess (Special Edition)") == "Special Edition"
    assert core.edition_in("IRON_GIANT_SIGNATURE_EDITION") == "Signature Edition"
    assert core.edition_in("BLADE_RUNNER_DIRECTORS_CUT") == "Director's Cut"
    assert core.edition_in("25TH_ANNIVERSARY_EDITION") == "25th Anniversary Edition"
    assert core.edition_in("DVD_VIDEO") is None and core.edition_in(None) is None
    # a cover's banner: read cleanly, packaging words alone (Spider-Man's, over its title); unsure, or with a name
    # in it, not one; a banner over two lines, one
    spidey = [("WIDESCREEN SPECIAL EDITION", (215, 53, 504, 66), 0.99), ("SPIDER-MAN", (215, 73, 505, 96), 0.95)]
    assert core.banner_edition(spidey) == ("WIDESCREEN SPECIAL EDITION", "Special Edition")
    assert core.banner_edition([("WIDESCREEN SPECIAL EDITION", (0, 0, 1, 1), 0.8)]) is None
    assert core.banner_edition([("SPIDER-MAN SPECIAL EDITION", (0, 0, 1, 1), 0.99)]) is None
    assert core.banner_edition([("COLLECTOR'S", (0, 0, 1, 1), 0.95), ("EDITION", (0, 2, 1, 3), 0.97)]) == (
        "COLLECTOR'S EDITION", "Collector's Edition")
    assert core.banner_edition([("SPIDER-MAN", (0, 0, 1, 1), 0.99)]) is None
    # a DVD naming itself in its text data (VIDEO_TS.IFO's TXTDT)
    vts = tmp_path / "dvd" / "VIDEO_TS"
    vts.mkdir(parents=True)
    ifo = bytearray(4096)
    ifo[:12] = b"DVDVIDEO-VMG"
    ifo[0xD4:0xD8] = (1).to_bytes(4, "big")
    text = b"Swan Princess (Special Edition)\tSwan Princess (Special Edition)\t"
    ifo[2048 + 0x103: 2048 + 0x103 + len(text)] = text
    (vts / "VIDEO_TS.IFO").write_bytes(bytes(ifo))
    monkeypatch.setattr(core, "volume_label", lambda root: None)       # (a folder has none)
    assert core.release_edition(str(tmp_path / "dvd")) == {
        "name": "Special Edition", "from": "its own name", "text": "Swan Princess (Special Edition)"}
    # none in its own name: the label recorded for it; none there either, its cover (none here: none at all)
    (vts / "VIDEO_TS.IFO").write_bytes(bytes(4096))
    assert core.release_edition(str(tmp_path / "dvd"), "HOLES_COLLECTORS_EDITION") == {
        "name": "Collector's Edition", "from": "its volume label", "text": "HOLES_COLLECTORS_EDITION"}
    assert core.release_edition(str(tmp_path / "dvd"), "HOLES") is None
    # a Blu-ray's metadata title
    dl = tmp_path / "bd" / "BDMV" / "META" / "DL"
    dl.mkdir(parents=True)
    (tmp_path / "bd" / "BDMV" / "STREAM").mkdir()
    (dl / "bdmt_eng.xml").write_text('<disclib xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo><di:title>'
                                     '<di:name>The Iron Giant: Signature Edition</di:name></di:title></di:discinfo>'
                                     '</disclib>', encoding="utf-8")
    assert core.release_edition(str(tmp_path / "bd"))["name"] == "Signature Edition"
    # in a packet: read from the disc where it's verified, when asked (the app's Submit)
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "E" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'Blu-ray', 'IRON', 'x', 'The Iron Giant (1999)', 'The Iron Giant (1999)', '2026', '2026')",
                    (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, '00800.mpls', 'feature', 5200)", (h,))
    log = "\n".join(['CINFO:2,0,"IRON"', 'TINFO:0,9,0,"1:26:40"', 'TINFO:0,16,0,"00800.mpls"', 'TINFO:0,27,0,"t00.mkv"'])
    pk = core.build_packet(con, h, log, str(tmp_path / "p"), root=str(tmp_path / "bd"), find_edition=True)
    assert pk["edition"]["name"] == "Signature Edition"
    assert core.build_packet(con, h, log, str(tmp_path / "p"), root=str(tmp_path / "bd"))["edition"] is None


def test_a_packet_from_a_reading_with_titles_short_says_so(tmp_path):
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "G" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'DVD', 'SHOW', 'x', 'Show S2', 'Show S2', '2026', '2026')", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, 'Title 2', 'episode', 1290)", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, 'Title 7', 'episode', 1287)", (h,))
    core.set_label(con, h, "Title 7", "Episode", "About Last Knight", 2, 6)
    log = lambda seven: "\n".join(['CINFO:2,0,"Show"', 'TINFO:0,9,0,"0:21:30"', 'TINFO:0,24,0,"2"', 'TINFO:0,27,0,"t00.mkv"',
                                   'TINFO:1,9,0,"%s"' % seven, 'TINFO:1,24,0,"7"', 'TINFO:1,27,0,"t01.mkv"'])
    out = tmp_path / "packet"
    pk = core.build_packet(con, h, log("0:20:21"), str(out))        # (Galavant S2's, dirty)
    assert pk["read_short"] == [{"id": "Title 7", "seconds": 66, "length": 1287}]
    lab = {l["our_title"]: l for l in pk["labels"]}
    assert lab["Title 7"]["note"] == "your label; read 66 seconds short: part of it couldn't be read"
    assert lab["Title 2"]["note"] != "" and "short" not in lab["Title 2"]["note"]
    text = core.packet_checklist(pk)
    assert "Don't submit this reading yet" in text and "Title 7, 66 seconds short" in text
    assert text.index("Don't submit") < text.index("## 1. Identify")
    assert pk["steps"][0]["warn"] and "Don't submit" in pk["steps"][0]["warn"][0]     # (the guide's first page)
    # cleaned: nothing said
    pk = core.build_packet(con, h, log("0:21:14"), str(out))
    assert pk["read_short"] == []
    assert "Don't submit" not in core.packet_checklist(pk)
    assert pk["every_title"] and "build this again" not in core.packet_checklist(pk)


def test_a_packet_from_the_reading_at_makemkvs_minimum_says_to_build_it_again(tmp_path):
    """TheDiscDB's logs list every title however short (Harry Potter 1's Blu-ray: 5 titles at MakeMKV's 2-minute
    minimum, while 9 are labelled); a packet from a reading kept at the minimum is written, but says so first."""
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "F" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'DVD', 'FILM', 'x', 'Film (2001)', 'Film (2001)', '2026', '2026')", (h,))
        con.execute("INSERT INTO titles (hash, title_id, role, duration) VALUES (?, 'Title 1', 'feature', 6000)", (h,))
    log = "\n".join(['CINFO:2,0,"Film"', 'TINFO:0,9,0,"1:40:00"', 'TINFO:0,24,0,"1"', 'TINFO:0,27,0,"t00.mkv"'])
    out = tmp_path / "packet"
    core.set_disc_tmdb(con, h, ("movie", 671))
    pk = core.build_packet(con, h, log, str(out), every_title=False)
    text = core.packet_checklist(pk)
    assert pk["every_title"] is False and "put the disc in the drive and build this again" in text
    assert text.index("build this again") < text.index("## 1. Identify")
    assert "- [ ] TMDB ID: **671**" in text                         # (what the form's first page takes)


def test_the_reading_kept_is_a_new_file_beside_the_old_ones(monkeypatch):
    """Readings kept before every run had no minimum numbered titles differently: the new one has a name of its own,
    so no rip takes an old one."""
    monkeypatch.setattr(core, "default_library", lambda: os.path.join("home", "library.sqlite"))
    assert core.makemkv_log_store("ABC") == os.path.join("home", "makemkv_logs", "ABC-all.txt")
    assert core.makemkv_old_log_store("ABC") == os.path.join("home", "makemkv_logs", "ABC.txt")


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


def test_a_discs_tmdb_id_is_found_from_its_imdb_id_and_kept(tmp_path, monkeypatch):
    """TheDiscDB's form finds a film by its TMDB id. From its IMDb id: TMDB's own lookup with a key; without one,
    Wikidata's (only the IMDb id sent); a show's is a series id; kept with the disc, in a library of any age."""
    import io
    import json
    import urllib.parse
    asked = []

    def wikidata(req, timeout=None):
        asked.append(req.full_url)
        imdb = re.search(r'P345 "(tt\d+)"', urllib.parse.unquote_plus(req.full_url)).group(1)
        rows = {"tt0241527": [{"movie": {"value": "671"}}], "tt0108778": [{"tv": {"value": "1668"}}]}.get(imdb, [])
        return io.BytesIO(json.dumps({"results": {"bindings": rows}}).encode())
    monkeypatch.setattr(core.urllib.request, "urlopen", wikidata)
    assert core.find_tmdb("tt0241527") == ("movie", 671)
    assert core.find_tmdb("tt0108778") == ("tv", 1668)
    assert core.find_tmdb("tt9999999") is None and core.find_tmdb("") is None
    assert all(u.startswith(core.WIKIDATA_SPARQL) for u in asked) and len(asked) == 3
    monkeypatch.setattr(core, "tmdb_get", lambda key, path, **kw: {"movie_results": [{"id": 671}]} if "0241527" in path else {})
    assert core.find_tmdb("tt0241527", key="k") == ("movie", 671) and core.find_tmdb("tt1", key="k") is None
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "C" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, first_seen, last_seen) VALUES (?, 'Blu-ray', 'X', '2026', '2026')", (h,))
    core.set_disc_tmdb(con, h, ("movie", 671))
    row = con.execute("SELECT * FROM discs WHERE hash=?", (h,)).fetchone()
    assert core.disc_tmdb(row) == ("movie", 671) and core.disc_tmdb({"tmdb": None}) is None


def test_a_copy_makemkv_lists_instead_carries_your_label(tmp_path, monkeypatch):
    """MakeMKV keeps one of a disc's duplicates: when it lists the copy (00801.mpls, "same video as 00800.mpls")
    and not the title you labelled, the copy carries your label, in the packet and for being ready (the film is
    labelled). A clip it lists that one of ours plays alone (00000.m2ts: 00800's) is a copy of that title."""
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "K" * 32
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, truth, first_seen, last_seen) VALUES "
                    "(?, 'Blu-ray', 'X', 'probable: x', 'Copy Film (2001)', '2026', '2026')", (h,))
        for tid, role, dur, clips, note in [("00800.mpls", "feature", 6000, "00000", None),
                                            ("00801.mpls", "duplicate", 6000, "00000", "same video as 00800.mpls"),
                                            ("00802.mpls", "extra", 300, "00002", None)]:
            con.execute("INSERT INTO titles (hash, title_id, role, duration, clips, note) VALUES (?,?,?,?,?,?)",
                        (h, tid, role, dur, clips, note))
    core.set_label(con, h, "00800.mpls", "MainMovie", "Copy Film")
    log = ('TINFO:0,9,0,"1:40:00"\nTINFO:0,16,0,"00801.mpls"\nTINFO:0,27,0,"t00.mkv"\n'
           'TINFO:1,9,0,"0:05:00"\nTINFO:1,16,0,"00802.mpls"\nTINFO:1,27,0,"t01.mkv"\n'
           'TINFO:2,9,0,"0:05:00"\nTINFO:2,16,0,"00002.m2ts"\nTINFO:2,27,0,"t02.mkv"\n')
    labels = core.packet_labels(con, h, log)[4]
    assert (labels[0]["our_title"], labels[0]["type"], labels[0]["title"], labels[0]["sent"]) == (
        "00801.mpls", "MainMovie", "Copy Film", True)
    assert labels[0]["note"] == "your label for 00800.mpls (MakeMKV lists this copy of it instead)"
    assert labels[2]["our_title"] is None and not labels[2]["sent"]
    assert labels[2]["note"] == "leave unlabeled (a copy of 00802.mpls)"
    # a type with no name: left out of the packet (their form needs a title), and not yet a labelled extra
    core.set_label(con, h, "00802.mpls", "Other")
    labels = core.packet_labels(con, h, log)[4]
    assert labels[1]["unnamed"] and not labels[1]["sent"] and "no name" in labels[1]["note"]
    core.set_label(con, h, "00802.mpls")
    ours = [dict(r) for r in con.execute("SELECT * FROM titles WHERE hash=?", (h,))]
    mine = core.get_labels(con, h)
    assert core.submission_labels(ours, mine, {"00801.mpls", "00802.mpls"}) == ({}, 1)   # (the film: by its copy)
    # both listed: the title has your label, its copy stays blank
    assert core.carried_labels(ours, mine, {"00800.mpls", "00801.mpls"})[1] == {}


def test_a_packet_leaves_the_pieces_of_a_title_you_named_unlabelled(tmp_path):
    """Muppets from Space's Blu-ray: "Pepe's Pick Up Pointers (Versions 1, 2 & 3)" plays three clips,
    each another title's only one, as long as they are together. Its pieces are left unlabelled, as
    TheDiscDB leaves copies (named, they'd be ripped twice), the note saying whose part each is; a
    title that isn't only pieces has none. And in the app (apply_labels): the pieces set aside, the
    scan's extra among them (the whole's named, they aren't), back as they were once its label is
    cleared; a piece named too keeps its name, and the whole is their Play All."""
    con = core.library_open(str(tmp_path / "lib.sqlite"))
    h = "D" * 32
    rows = [("00753.mpls", "junk", 43.1, "00541,00543,00542"), ("00811.mpls", "junk", 19.4, "00541"),
            ("00804.mpls", "junk", 13.8, "00543"), ("00805.mpls", "junk", 9.9, "00542"),
            ("00745.mpls", "junk", 23.0, "00534"), ("00900.mpls", "junk", 30.0, "00534,00777")]
    with con:
        con.execute("INSERT INTO discs (hash, format, label, status, best_guess, truth, first_seen, last_seen) "
                    "VALUES (?, 'Blu-ray', 'MUPPETS', 'x', 'Muppets from Space (1999)', 'Muppets from Space (1999)', '2026', '2026')",
                    (h,))
        for tid, role, secs, clips in rows:
            con.execute("INSERT INTO titles (hash, title_id, role, duration, clips) VALUES (?,?,?,?,?)", (h, tid, role, secs, clips))
    core.set_label(con, h, "00753.mpls", "Extra", "Pepe's Pick Up Pointers (Versions 1, 2 & 3)")
    core.set_label(con, h, "00900.mpls", "Extra", "Not only pieces")
    log = ['CINFO:2,0,"Muppets"']
    for i, (tid, _, secs, _) in enumerate(rows):
        log += ['TINFO:%d,9,0,"0:00:%02d"' % (i, secs), 'TINFO:%d,16,0,"%s"' % (i, tid), 'TINFO:%d,27,0,"t%02d.mkv"' % (i, i)]
    pk = core.build_packet(con, h, "\n".join(log), str(tmp_path / "packet"))
    lab = {l["source"]: (l["type"], l["title"]) for l in pk["labels"]}
    notes = {l["source"]: l["note"] for l in pk["labels"]}
    for k, pid in enumerate(("00811.mpls", "00804.mpls", "00805.mpls"), 1):
        assert lab[pid] == (None, None) and notes[pid] == "leave unlabeled (part %d of 3 of 00753.mpls, which plays it whole)" % k
    assert "part" not in notes["00745.mpls"]              # (one clip of a whole that plays something else too)
    titles = [{"id": tid, "role": role, "duration": secs, "clip_list": clips, "note": ""} for tid, role, secs, clips in rows]
    titles[3]["role"] = "extra"                            # (00805: kept by the scan)
    labels = core.get_labels(con, h)
    core.apply_labels(titles, labels)
    by = {t["id"]: t for t in titles}
    assert (by["00811.mpls"]["role"], by["00811.mpls"]["note"]) == (
        "segment", "part 1 of 3 of 00753.mpls (Pepe's Pick Up Pointers (Versions 1, 2 & 3)), which plays it whole")
    assert by["00804.mpls"]["role"] == "segment" and by["00805.mpls"]["role"] == "segment"
    assert by["00745.mpls"]["role"] == "junk"
    core.apply_labels(titles, {})                          # (its label cleared: as the scan had them)
    assert [by[p]["role"] for p in ("00811.mpls", "00804.mpls", "00805.mpls")] == ["junk", "junk", "extra"]
    both = dict(labels, **{"00811.mpls": {"type": "Extra", "title": "Version 1"}})
    core.apply_labels(titles, both)                        # (a piece named too: its own)
    assert by["00811.mpls"]["role"] == "extra" and by["00804.mpls"]["role"] == "segment"
    eps = {"00753.mpls": {"type": "Episode", "title": "Pilot"}}
    core.apply_labels(titles, eps)                         # (an episode: never a whole of pieces)
    assert by["00804.mpls"]["role"] == "junk"
