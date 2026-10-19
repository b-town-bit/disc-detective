"""Test data built from scratch: fake discs, a fake TheDiscDB, a fake IMDb, a fake GitHub.
Nothing here comes from a real disc, so the suite runs anywhere."""
import base64
import gzip
import hashlib
import http.server
import json
import os
import struct
import threading
import time
import urllib.parse

POSTER_JPG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAA8ACgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDOtLW3ktJrm5nliSN0QCOIOSWDHuwx939ad5Wlf8/l5/4CL/8AHKIv+QHdf9fMP/oMtUq4z6RJtvUu+VpX/P5ef+Ai/wDxyjytK/5/Lz/wEX/45VKilcrlff8AL/It3drbx2kNzbTyypI7oRJEEIKhT2Y5+9+lFOl/5Adr/wBfM3/oMVFDCF7ahF/yA7r/AK+Yf/QZapVdi/5Ad1/18w/+gy1SoYR3f9dEFFFFIouy/wDIDtf+vmb/ANBiool/5Adr/wBfM3/oMVFNkw2+/wDMIv8AkB3X/XzD/wCgy1Sq7F/yA7r/AK+Yf/QZapUMI7v+uiCiiikUXZf+QHa/9fM3/oMVFEv/ACA7X/r5m/8AQYqKbJht9/5ktlbXF1o13HbQSzOLiElY0LEDbLzgVF/Y+q/9A28/78N/hVKii6FyyTbTLv8AY+q/9A28/wC/Df4Uf2Pqv/QNvP8Avw3+FUqKNB2l3/D/AIJpXttcWujWkdzBLC5uJiFkQqSNsXODRWbRQwirKzP/2Q==")
BOX_JPG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCABQADwDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDNtLW3ktJrm6nliSN0jAjiDklgx7sMfd/Wl8rSv+f29/8AARf/AI5RF/yArv8A6+Yf/QZapV72rb1OAu+VpX/P7e/+Ai//AByjytK/5/b3/wABF/8AjlUqKfK+4X8i75Wlf8/t7/4CL/8AHKPK0r/n9vf/AAEX/wCOVSoo5X3C/kaVrZ6bdXcNtHfXYeZ1jUtaLgEnHP7ys2ruif8AId0//r5j/wDQhVKkr3auD2LsX/ICu/8Ar5h/9BlqlV2L/kBXf/XzD/6DLVKnHdg+gUUUVQgooooAu6J/yHdP/wCvmP8A9CFUqu6J/wAh3T/+vmP/ANCFUqlfEx9C7F/yArv/AK+Yf/QZapVdi/5AV3/18w/+gy1SojuwfQKKKKoQUUUUAXdE/wCQ7p//AF8x/wDoQqlV3RP+Q7p//XzH/wChCqVSviY+hdi/5AV3/wBfMP8A6DLVKrsX/ICu/wDr5h/9BlqlRHdg+gUUUVQgooooAu6J/wAh3T/+vmP/ANCFUqu6J/yHdP8A+vmP/wBCFUqlfEx9C7F/yArv/r5h/wDQZapVctLq3jtJra6gllSR0kBjlCEFQw7qc/e/Sl83Sv8Anyvf/Atf/jdLVN6AUqKu+bpX/Ple/wDgWv8A8bo83Sv+fK9/8C1/+N0+Z9gt5lKirvm6V/z5Xv8A4Fr/APG6PN0r/nyvf/Atf/jdHM+wW8w0T/kO6f8A9fMf/oQqlWla3mm2t3Dcx2N2XhdZFDXa4JBzz+7rNpK927A9j//Z")
THUMB_JPG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAkAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwB1tbwvbSz3E0kao6oAkYcksGPcj+7S+Xp3/P1df+Ay/wDxdEf/ACCLj/rvF/6DJVWuc8PRJaFry9O/5+rr/wABl/8Ai6PL07/n6uv/AAGX/wCLqrRQLmXYteXp3/P1df8AgMv/AMXR5enf8/V1/wCAy/8AxdVaKA5l2LXl6d/z9XX/AIDL/wDF0lzbwpbRT280kiu7IQ8YQgqFPYn+9VarUn/IIt/+u8v/AKDHQPRp6BH/AMgi4/67xf8AoMlVavWkE1xpdylvDJKwmiJCKWONsnpUf9maj/z4XX/flv8ACiwOLaVkVaKtf2ZqP/Phdf8Aflv8KP7M1H/nwuv+/Lf4UWYuSXYq0Va/szUf+fC6/wC/Lf4Uf2ZqP/Phdf8Aflv8KLMOSXYq1ak/5BFv/wBd5f8A0GOj+zNR/wCfC6/78t/hUl3BNb6XbJcQyRMZpSA6lTjbH60DUWk7oo0UUUiAooooAKKKKACiiigD/9k=")


# ---------------------------------------------------------------- IMDb
def build_imdb(d):
    """IMDb's TSV layout with the titles the tests look for, plus look-alike decoys. Fake ids."""
    os.makedirs(d, exist_ok=True)
    B = ["tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres"]
    E = ["tconst\tparentTconst\tseasonNumber\tepisodeNumber"]
    R = ["tconst\taverageRating\tnumVotes"]
    n = [9000000]

    def add(ttype, title, year, runtime, votes, end="\\N", orig=None):
        n[0] += 1
        t = "tt%07d" % n[0]
        B.append("\t".join([t, ttype, title, orig or title, "0", str(year), str(end),
                            str(runtime) if runtime else "\\N", "Drama"]))
        if votes:
            R.append("%s\t7.0\t%d" % (t, votes))
        return t

    def series(title, year, runtime, votes, seasons):
        s = add("tvSeries", title, year, runtime, votes, end=year + 5)
        for sn, names in seasons.items():
            for i, nm in enumerate(names, 1):
                if isinstance(nm, tuple):            # (episode number, name), e.g. (0, "Unaired Pilot")
                    i, nm = nm
                e = add("tvEpisode", nm, year + sn - 1, runtime, 0)
                E.append("%s\t%s\t%d\t%d" % (e, s, sn, i))
        return s

    add("movie", "Maverick", 1994, 127, 120000)
    add("movie", "Top Gun: Maverick", 2022, 130, 700000)
    add("tvSeries", "Maverick", 1957, 60, 6000, end=1962)
    add("movie", "Anastasia", 1997, 94, 160000)
    add("movie", "Anastasia", 1956, 105, 15000)
    add("movie", "Gone in 60 Seconds", 2000, 118, 300000)
    add("movie", "Gone in 60 Seconds", 1974, 97, 9000)
    add("movie", "The Prince of Egypt", 1998, 99, 140000)
    add("movie", "Shrek", 2001, 90, 750000)
    add("movie", "Shrek 2", 2004, 93, 500000)
    add("movie", "An American Tailor", 1984, 75, 900)
    add("movie", "An American Tail", 1986, 80, 90000)
    add("movie", "An American Tail: Fievel Goes West", 1991, 75, 40000)
    add("video", "Pocahontas", 1994, 45, 257)                   # unrelated look-alikes, released first
    add("movie", "Pocahontas", 1995, 81, 220681)
    add("movie", "Pocahontas: The Legend", 1995, 102, 900)
    add("video", "Pocahontas 2: Journey to a New World", 1998, 73, 19608)
    add("movie", "Robin Hood: Prince of Thieves", 1991, 143, 224828)
    add("movie", "Robin Hood: Prince of Thieves", 2009, 142, 621)        # an obscure namesake
    add("video", "An American Tail: The Treasure of Manhattan Island", 1998, 83, 4000)
    add("video", "An American Tail: The Mystery of the Night Monster", 2000, 75, 3000)
    add("tvSeries", "Fievel's American Tails", 1992, 22, 2000, end=1992)
    add("movie", "Toy Story", 1995, 81, 1100000)
    add("movie", "Toy Story 2", 1999, 92, 650000)
    add("movie", "Toy Story 3", 2010, 103, 900000)
    add("movie", "Rocky", 1976, 120, 650000)
    add("movie", "Rocky II", 1979, 119, 230000)
    add("movie", "Rocky III", 1982, 99, 220000)
    add("movie", "Apollo 13", 1995, 140, 320000)
    add("short", "A Grand Day Out", 1989, 23, 30000, orig="A Grand Day Out with Wallace and Gromit")
    add("short", "Wallace & Gromit: The Wrong Trousers", 1993, 30, 60000, orig="The Wrong Trousers")
    add("short", "Wallace & Gromit: A Close Shave", 1995, 30, 45000, orig="A Close Shave")
    add("short", "Wallace & Gromit: A Matter of Loaf and Death", 2008, 30, 25000)
    add("short", "Wallace & Gromit: Shopper 13", 2003, 1, 300)
    add("movie", "Wallace & Gromit: The Curse of the Were-Rabbit", 2005, 85, 130000)
    add("tvSeries", "Wallace & Gromit in A Close Shave", 1998, 30, 13)
    add("tvSeries", "Wallace & Gromit in A Grand Day Out", 1998, 23, 12)
    add("tvSeries", "Wallace and Gromit's World of Invention", 2010, 30, 1259)
    add("short", "Gromit Unleashed", 2013, 8, 15)
    series("Psych", 2006, 44, 120000, {1: ["Pilot", "Spellingg Bee", "Speak Now or Forever Hold Your Piece",
                                           "Woman Seeking Dead Husband", "9 Lives", "Weekend Warriors"]})
    series("Justified", 2010, 44, 150000, {1: ["Fire in the Hole", "Riverbrook", "Fixer", "Long in the Tooth",
                                               "The Lord of War and Thunder"]})
    series("Justified: City Primeval", 2023, 50, 20000, {1: ["City Primeval"]})
    add("movie", "Justified", 1999, 90, 40)
    series("Smallville", 2001, 44, 140000, {1: [(0, "Unaired Pilot"), (1, "Pilot"), (2, "Metamorphosis"),
                                                (3, "Hothead"), (4, "X-Ray"), (5, "Cool")]})
    add("movie", "Chicken Run", 2000, 84, 230000)
    add("movie", "Hercules", 1997, 93, 260000)
    add("movie", "The Black Cauldron", 1985, 80, 46161)
    add("movie", "Booty Call", 1997, 79, 10234)
    add("movie", "The Nightmare Before Christmas", 1993, 76, 417395)   # N B C: B C after its 1st word
    add("movie", "The Emperor's New Groove", 2000, 78, 253750)
    add("movie", "Kronk's New Groove", 2005, 75, 18736)
    add("movie", "No Greater Glory", 1934, 74, 604)
    add("movie", "Blood Car", 2007, 82, 2873)
    add("movie", "Big City", 2012, 80, 9000)                 # after the mastering date
    add("movie", "Tarzan", 1999, 88, 272960)                 # TZN = TarZaN
    add("movie", "Psycho Cop Returns", 1993, 80, 1851)       # PSYCH: only a partial name match
    add("movie", "Torrid Zone", 1940, 88, 1609)              # T Z initials
    add("movie", "Tarzan Escapes", 1936, 89, 3865)           # two words: not a consonant code
    add("movie", "Nightmare", 1956, 80, 3113)                # N..G..E, but little known
    add("movie", "Hercules", 2014, 98, 160000)
    add("movie", "Hercules", 1958, 107, 9000)
    add("tvSeries", "Hercules", 1998, 22, 15000, end=1999)
    add("movie", "Chicken Run: Dawn of the Nugget", 2023, 101, 30000)
    for name, rows in (("title.basics.tsv.gz", B), ("title.episode.tsv.gz", E), ("title.ratings.tsv.gz", R)):
        # newline="\n": IMDb's files use \n; text mode on Windows would write \r\n
        with gzip.open(os.path.join(d, name), "wt", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(rows) + "\n")
    return d


# ---------------------------------------------------------------- discs
def _mpls(items, chapters_per_item):
    pi = b""
    for clip, tin, tout in items:
        body = clip.encode() + b"M2TS" + b"\x00\x01" + b"\x00" + struct.pack(">II", tin, tout) + b"\x00" * 12
        pi += struct.pack(">H", len(body)) + body
    plist = struct.pack(">HHH", 0, len(items), 0) + pi
    plist = struct.pack(">I", len(plist)) + plist
    marks, n = b"", 0
    for idx, (clip, tin, tout) in enumerate(items):
        step = (tout - tin) // chapters_per_item
        for c in range(chapters_per_item):
            marks += struct.pack(">BBHIHI", 0, 1, idx, tin + c * step, 0xFFFF, 0)
            n += 1
    marks += struct.pack(">BBHIHI", 0, 2, 0, 0, 0xFFFF, 0)
    markblk = struct.pack(">IH", 2 + 14 * (n + 1), n + 1) + marks
    head = b"MPLS0200" + struct.pack(">III", 40, 40 + len(plist), 0) + b"\x00" * 20
    return head + plist + markblk


T45 = 45000
BYTES_PER_SECOND = 20_000   # fake stream sizes stay small (Windows reserves the full size of a file)


def _bluray(root, playlists, clips, meta_xml=None, thumb=None, seed=0):
    for sub in ("PLAYLIST", "STREAM", "META/DL"):
        os.makedirs(os.path.join(root, "BDMV", *sub.split("/")), exist_ok=True)
    for name, (items, ch) in playlists.items():
        with open(os.path.join(root, "BDMV", "PLAYLIST", name), "wb") as fh:
            fh.write(_mpls(items, ch))
    for clip, seconds in clips.items():                      # sparse files: sizes only, no disk used
        with open(os.path.join(root, "BDMV", "STREAM", clip + ".m2ts"), "wb") as fh:
            fh.truncate(int(seconds * BYTES_PER_SECOND) + 192 * int(clip) + seed)
    if meta_xml:
        with open(os.path.join(root, "BDMV", "META", "DL", "bdmt_eng.xml"), "w", encoding="utf-8") as fh:
            fh.write(meta_xml)
    if thumb:
        with open(os.path.join(root, "BDMV", "META", "DL", "cover_640.jpg"), "wb") as fh:
            fh.write(thumb)
    return root


def make_tv_bluray(root, with_thumb=True):
    """Justified S1 D1: 4 episodes, a play-all, a duplicate playlist, a logo and a featurette."""
    ep = lambda m, s: (m * 60 + s) * T45
    eps = [("00011", 0, ep(42, 31)), ("00012", 0, ep(43, 2)), ("00013", 0, ep(41, 58)), ("00014", 0, ep(42, 10))]
    pls = {"00000.mpls": ([("00001", 0, 10 * T45)], 1),
           "00001.mpls": ([eps[0]], 6), "00002.mpls": ([eps[1]], 6), "00003.mpls": ([eps[2]], 6),
           "00004.mpls": ([eps[3]], 6), "00005.mpls": (eps, 6), "00006.mpls": ([eps[1]], 6),
           "00020.mpls": ([("00030", 0, ep(9, 45))], 1)}
    clips = {c: t / T45 for c, _, t in eps}
    clips.update({"00001": 10, "00030": 585})
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<disclib xmlns="urn:BDA:bdmv;disclib" '
           'xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo><di:title><di:name>Justified: Season 1: Disc 1'
           '</di:name></di:title><di:description><di:thumbnail href="cover_640.jpg" size="640x360"/>'
           '</di:description></di:discinfo></disclib>')
    return _bluray(root, pls, clips, xml, THUMB_JPG if with_thumb else None)


def make_movie_bluray(root, minutes=99, seed=7):
    """A movie disc: the feature, an hour-long documentary, a trailer, a logo. No metadata, no art."""
    f = minutes * 60 * T45
    pls = {"00000.mpls": ([("00001", 0, 12 * T45)], 1), "00800.mpls": ([("00800", 0, f)], 24),
           "00011.mpls": ([("00011", 0, 3600 * T45)], 6), "00010.mpls": ([("00010", 0, 150 * T45)], 1)}
    return _bluray(root, pls, {"00001": 12, "00800": minutes * 60, "00011": 3600, "00010": 150}, seed=seed)


def make_reel_movie_bluray(root, name="Chicken Run"):
    """A film stored as four ~21-minute reels in one playlist: shaped exactly like a TV
    play-all, which is how Chicken Run fooled the episode logic."""
    reel = lambda m, s: (m * 60 + s) * T45
    reels = [("00101", 0, reel(21, 4)), ("00102", 0, reel(20, 51)), ("00103", 0, reel(21, 13)), ("00104", 0, reel(20, 49))]
    pls = {"00000.mpls": ([("00001", 0, 12 * T45)], 1), "00800.mpls": (reels, 6),
           "00010.mpls": ([("00010", 0, 140 * T45)], 1)}
    clips = {c: t / T45 for c, _, t in reels}
    clips.update({"00001": 12, "00010": 140})
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<disclib xmlns="urn:BDA:bdmv;disclib" '
           'xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo><di:title><di:name>%s</di:name></di:title>'
           '</di:discinfo></disclib>' % name)
    return _bluray(root, pls, clips, xml, seed=11)


def make_shorts_bluray(root):
    """Shaped like the Wallace & Gromit DVD: three short films (one of them twice), a play-all
    of the first two, an extra and a logo. Mastered 2005."""
    ln = lambda m, s: (m * 60 + s) * T45
    a, b, c, c2 = ("00011", 0, ln(24, 29)), ("00012", 0, ln(32, 3)), ("00013", 0, ln(31, 2)), ("00014", 0, ln(31, 2) + 22500)
    pls = {"00000.mpls": ([("00001", 0, 8 * T45)], 1),
           "00001.mpls": ([a], 4), "00002.mpls": ([b], 4), "00003.mpls": ([c], 5), "00004.mpls": ([c2], 4),
           "00005.mpls": ([a, b], 8), "00020.mpls": ([("00030", 0, ln(7, 54))], 2)}
    clips = {x[0]: x[2] / T45 for x in (a, b, c, c2)}
    clips.update({"00001": 8, "00030": 474})
    _bluray(root, pls, clips, seed=5)
    stamp = 1123200000                                   # 2005-08-05: what "mastered" reads
    for dp, _, fns in os.walk(root):
        for fn in fns:
            os.utime(os.path.join(dp, fn), (stamp, stamp))
    return root


# ---------------------------------------------------------------- TheDiscDB
def build_discdb_repo(root, movie_disc_hash, movie_disc_sizes):
    """A miniature TheDiscDb/data checkout: one movie (whose disc fingerprint we know),
    one series, posters and box art."""
    data = os.path.join(root, "data")

    def write(rel, obj):
        p = os.path.join(data, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if isinstance(obj, bytes):
            open(p, "wb").write(obj)
        elif isinstance(obj, str):
            open(p, "w", encoding="utf-8").write(obj)
        else:
            json.dump(obj, open(p, "w", encoding="utf-8"))

    m = "movie/Test Movie Mystery (2011)"
    write(m + "/metadata.json", {"Title": "Test Movie Mystery", "Year": 2011, "Type": "Movie",
                                 "ExternalIds": {"Imdb": "tt7000001", "Tmdb": "1"}})
    write(m + "/cover.jpg", POSTER_JPG)
    write(m + "/2012-blu-ray/release.json", {"Slug": "2012-blu-ray", "Title": "2012 Blu-ray", "Year": 2012})
    write(m + "/2012-blu-ray/front.jpg", BOX_JPG)
    write(m + "/2015-4k/front.jpg", BOX_JPG)              # another release's box (no disc of it)
    write(m + "/2012-blu-ray/disc01.json", {"Index": 1, "Name": "Blu-ray", "Format": "Blu-ray",
        "ContentHash": movie_disc_hash, "Titles": [
            {"SourceFile": "00800.mpls", "Duration": "1:39:00", "DisplaySize": "20 GB",
             "Item": {"Title": "Test Movie Mystery", "Type": "MainMovie", "Chapters": []}},
            {"SourceFile": "00010.mpls", "Duration": "0:02:30", "DisplaySize": "0.6 GB",
             "Item": {"Title": "Theatrical Trailer", "Type": "Trailer", "Chapters": []}}]})
    write(m + "/2012-blu-ray/disc01.txt", "".join(
        "HSH:%d,\"%s\",\"\",%d\n" % (i, name, size) for i, (name, size) in enumerate(movie_disc_sizes)))
    s = "series/Example Show (2004)"
    write(s + "/metadata.json", {"Title": "Example Show", "Year": 2004, "Type": "Series",
                                 "ExternalIds": {"Imdb": "tt7000002"}})
    write(s + "/cover.jpg", POSTER_JPG)
    write(s + "/s1-dvd/release.json", {"Slug": "s1-dvd", "Title": "Season 1 DVD"})
    write(s + "/s1-dvd/disc01.json", {"Name": "Disc 1", "Format": "DVD", "ContentHash": "A" * 32, "Titles": [
        {"SourceFile": "01", "Duration": "0:44:00", "Item": {"Title": "Pilot", "Type": "Episode",
                                                             "Season": "1", "Episode": "1"}}]})
    write("../LICENSE", "MIT License\n\nCopyright (c) 2021 Luke Foust\n")
    return root


# ---------------------------------------------------------------- fake GitHub
class FakeGitHub:
    """Release asset with ETag / 304 (like GitHub), raw files (like raw.githubusercontent.com),
    and IMDb dataset files."""

    def __init__(self, asset=None, raw_root=None, imdb_dir=None, slow=False):
        self.asset, self.raw_root, self.imdb_dir, self.slow = asset, raw_root, imdb_dir, slow
        self.log = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, etag=None):
                self.send_response(200)
                if etag:
                    self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                step = 32768 if outer.slow else max(1, len(body))
                for i in range(0, len(body), step):
                    self.wfile.write(body[i:i + step])
                    if outer.slow:
                        time.sleep(0.01)

            def do_GET(self):
                outer.log.append((self.path, self.headers.get("If-None-Match")))
                path = urllib.parse.unquote(self.path)
                if path.startswith("/dl/") and outer.asset:
                    body = open(outer.asset, "rb").read()
                    tag = '"%s"' % hashlib.sha1(body).hexdigest()
                    if self.headers.get("If-None-Match") == tag:
                        self.send_response(304)
                        self.end_headers()
                        return
                    return self._send(body, tag)
                for prefix, base in (("/raw/", outer.raw_root), ("/imdb/", outer.imdb_dir)):
                    if path.startswith(prefix) and base:
                        p = os.path.join(base, *path[len(prefix):].split("/"))
                        if os.path.isfile(p):
                            return self._send(open(p, "rb").read())
                self.send_response(404)
                self.end_headers()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
