#!/usr/bin/env python3
r"""
Disc Detective - figure out which movie or show is on a DVD or Blu-ray, and what
every title on it is, without reading any video.

The investigation, cheapest clue first:

  1. Fingerprint      TheDiscDB ContentHash (MD5 of stream file sizes). Instant.
  2. Database lookup  TheDiscDB online (GraphQL), then the downloaded TheDiscDB
                      index (or a local clone of github.com/TheDiscDb/data).
                      A hit usually closes the case.
  3. Disc structure   DVD IFO / Blu-ray MPLS parsing: titles, durations,
                      chapters, audio/subtitle tracks, commentary flags.
                      Classifies movie / TV / short-film collection, finds
                      episodes, play-all titles, duplicates, alternate
                      versions, extras and junk.
  4. Disc metadata    Volume label (show / season / disc number, sequel numbers,
                      truncation-aware), DVD provider ID, mastering date,
                      Blu-ray META/DL title XML, embedded cover art, optional
                      OCR of the cover with tesseract.
  5. Candidates       IMDb's non-commercial datasets, downloaded once per user and
                      indexed locally (no API key). Ranks by name, runtime,
                      release year vs mastering date and popularity; suggests
                      Plex names with the IMDb id. TMDB (optional key) is a fallback.
  6. Picture          The disc's own art, TheDiscDB box art / poster, or TMDB.

Usage:
  disc-detective D:\
  disc-detective D:\ --json > result.json
  disc-detective /path/to/backup --label PSYCH    (label override)
  disc-detective --list                           (your library of scanned discs)
  disc-detective --set-truth 3EBA9FFF "Psych S1 D2" (confirm a disc; instant next time)
  disc-detective --update-data                    (refresh IMDb + TheDiscDB data now)
  disc-detective --packet 3EBA9FFF --makemkv-log disc02.txt   (TheDiscDB submission)
  disc-detective D:\ --keep-evidence              (dev: keep IFOs/playlists for --corpus)
  disc-detective --corpus                         (dev: re-parse and score kept discs)

(Without installing: python -m disc_detective ...  With no arguments it opens the app.)

Everything lives in ~/.disc_detective (env DISC_DETECTIVE_HOME): your library of
scanned discs, the IMDb and TheDiscDB data, the picture cache, app settings.

Optional tools, used when found: Pillow or ffmpeg (pictures), tesseract (OCR).
"""
import argparse
import datetime as _dt
import difflib
import glob
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "0.3"
USER_AGENT = "disc-detective/" + VERSION
PROJECT_REPO = "b-town-bit/disc-detective"   # GitHub repo that publishes the TheDiscDB index
LEGACY_HOME = os.path.join(os.path.expanduser("~"), ".disc_identify")     # name before the rename


def env(name, default=None):
    """DISC_DETECTIVE_<name>, or the old DISC_IDENTIFY_<name> from before the rename."""
    return os.environ.get("DISC_DETECTIVE_" + name) or os.environ.get("DISC_IDENTIFY_" + name) or default


def data_home():
    """~/.disc_detective (env DISC_DETECTIVE_HOME). Moves an old ~/.disc_detective over once."""
    if os.environ.get("DISC_DETECTIVE_HOME"):
        return os.environ["DISC_DETECTIVE_HOME"]
    home = os.path.join(os.path.expanduser("~"), ".disc_detective")
    if not os.path.exists(home) and os.path.isdir(LEGACY_HOME):
        try:
            os.rename(LEGACY_HOME, home)             # same parent folder: instant, keeps the IMDb data
        except OSError:
            return LEGACY_HOME                       # in use / no permission: keep using it
    old_settings = os.path.join(home, "disc_ripper.json")
    if os.path.exists(old_settings) and not os.path.exists(os.path.join(home, "settings.json")):
        try:
            os.replace(old_settings, os.path.join(home, "settings.json"))
        except OSError:
            pass
    return home
DISCDB_GRAPHQL = "https://thediscdb.com/graphql"
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
TMDB_CREDIT = "This product uses the TMDB API but is not endorsed or certified by TMDB."
DISCDB_RAW = "https://raw.githubusercontent.com/TheDiscDb/data/main/data/"
# A slim index of TheDiscDB (fingerprints, big-file sizes, title lists, IMDb ids; no images),
# rebuilt weekly by a GitHub Action and published as a release asset. Override with env
# DISC_DETECTIVE_DISCDB_URL (e.g. your fork, or file:///... for testing).
DISCDB_INDEX_URL = env("DISCDB_URL") or \
    "https://github.com/%s/releases/download/discdb-index/discdb_index.sqlite.gz" % PROJECT_REPO
DISCDB_INDEX_DB = "discdb_index.sqlite"
DISCDB_INDEX_VERSION = 1
DISCDB_MAX_AGE_DAYS = 7
DISCDB_NOTICE = ("Disc data from TheDiscDB (thediscdb.com, github.com/TheDiscDb/data), "
                 "MIT License, Copyright (c) 2021 Luke Foust.")
progress = None   # optional hook: progress(label, done_bytes, total_bytes) for GUIs
ART_POSTER = (200, 300)  # main picture box (w, h) in the GUI: posters fill it, wide stills fit its width
ART_THUMB = (64, 96)     # the row of film posters under a collection's plan
ART_PICK = (40, 60)      # beside each guess in "Not right?" and the type-ahead list

# Classification thresholds, in seconds.
JUNK_MAX = 120          # shorter than this: menus, logos, warnings
EPISODE_MIN = 18 * 60   # sitcom with ads removed is ~20 min
EPISODE_MAX = 70 * 60   # an hour drama is ~42-48 min; specials run long
FEATURE_MIN = 70 * 60
SAME_LENGTH_TOL = 2     # seconds


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _natural(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]


def hms(seconds):
    seconds = int(round(seconds or 0))
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def find_child(parent, name):
    """Case-insensitive child lookup (discs vary between upper and lower case)."""
    try:
        for entry in os.listdir(parent):
            if entry.upper() == name.upper():
                return os.path.join(parent, entry)
    except OSError:
        pass
    return None


def read_file(path):
    with open(path, "rb") as fh:
        return fh.read()


def u8(b, o):
    return b[o]


def u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def lang(b):
    s = b.decode("latin-1", "replace").strip("\0 \xff")
    return s if s.isalpha() else ""


# --------------------------------------------------------------------------
# 1. fingerprint
# --------------------------------------------------------------------------

def detect_format(root):
    bdmv = find_child(root, "BDMV")
    if bdmv and find_child(bdmv, "STREAM"):
        return "Blu-ray", bdmv
    vts = find_child(root, "VIDEO_TS")
    if vts:
        return "DVD", vts
    return None, None


def content_hash(sizes):
    """TheDiscDB ContentHash: MD5 over each file size as little-endian int64."""
    md5 = hashlib.md5()
    for size in sizes:
        md5.update(struct.pack("<q", size))
    return md5.hexdigest().upper()


def fingerprint(fmt, base):
    if fmt == "Blu-ray":
        d = find_child(base, "STREAM")
        files = [f for f in os.listdir(d) if f.lower().endswith(".m2ts")]
    else:
        d = base
        files = os.listdir(d)
    files = sorted((f for f in files if os.path.isfile(os.path.join(d, f))), key=str.upper)
    stats = [(f, os.stat(os.path.join(d, f))) for f in files]
    return {
        "hash": content_hash(st.st_size for _, st in stats),
        "files": [(f, st.st_size) for f, st in stats],
        "newest_mtime": max((st.st_mtime for _, st in stats), default=None),
    }


# --------------------------------------------------------------------------
# 2. TheDiscDB lookup
# --------------------------------------------------------------------------

DISCDB_QUERY = """
query ($hash: String) {
  mediaItems(where: { releases: { some: { discs: { some: { contentHash: { eq: $hash } } } } } }) {
    nodes {
      title year type
      releases {
        slug title year locale regionCode
        discs { index name format contentHash
          titles { index duration sourceFile segmentMap displaySize
            item { title type season episode } } } } } }
}"""


def discdb_online(content_hash_value, timeout=15):
    body = json.dumps({"query": DISCDB_QUERY, "variables": {"hash": content_hash_value}}).encode()
    req = urllib.request.Request(DISCDB_GRAPHQL, data=body, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
    for node in data["data"]["mediaItems"]["nodes"]:
        for rel in node.get("releases") or []:
            for disc in rel.get("discs") or []:
                if (disc.get("contentHash") or "").upper() == content_hash_value:
                    return {
                        "source": "thediscdb.com",
                        "title": node.get("title"), "year": node.get("year"), "type": node.get("type"),
                        "release": rel.get("title") or rel.get("slug"), "slug": rel.get("slug"),
                        "disc": disc.get("name"), "format": disc.get("format"),
                        "titles": [{
                            "source": t.get("sourceFile"), "duration": t.get("duration"),
                            "size": t.get("displaySize"),
                            "item": (t.get("item") or {}),
                        } for t in disc.get("titles") or []],
                    }
    return None


HASH_RE = re.compile(r'"ContentHash"\s*:\s*"([0-9A-Fa-f]{32})"')
HSH_RE = re.compile(r"^HSH:\d+,([^,]*),.*,(\d+)\s*$")


def discdb_local_index(repo):
    """Build (and cache) hash -> disc json path, plus big-file size -> disc paths."""
    data = os.path.join(repo, "data")
    cache = os.path.join(repo, ".disc_detective_index.json")
    stamp = os.path.getmtime(os.path.join(repo, ".git")) if os.path.exists(os.path.join(repo, ".git")) else 0
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                idx = json.load(fh)
            if idx.get("stamp") == stamp and idx.get("version") == VERSION:
                return idx
        except (OSError, ValueError):
            pass
    hashes, sizes, imdb = {}, {}, {}
    for dirpath, _, filenames in os.walk(data):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if fn == "metadata.json" and "cover.jpg" in filenames:
                try:
                    with open(path, encoding="utf-8-sig") as fh:
                        ext = (json.load(fh).get("ExternalIds") or {})
                    if ext.get("Imdb"):
                        imdb[ext["Imdb"]] = os.path.relpath(dirpath, data)
                except (OSError, ValueError, AttributeError):
                    pass
            elif re.match(r"disc\d+\.json$", fn):
                with open(path, encoding="utf-8-sig", errors="replace") as fh:
                    m = HASH_RE.search(fh.read(4096))
                if m:
                    hashes[m.group(1).upper()] = os.path.relpath(path, data)
            elif re.match(r"disc\d+\.txt$", fn):
                rel = os.path.relpath(path, data)[:-4] + ".json"
                with open(path, encoding="utf-8-sig", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("HSH:"):
                            m = HSH_RE.match(line.strip())
                            if m and int(m.group(2)) >= 50_000_000:
                                sizes.setdefault(m.group(2), []).append(rel)
    idx = {"version": VERSION, "stamp": stamp, "hashes": hashes, "sizes": sizes, "imdb": imdb}
    try:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(idx, fh)
    except OSError:
        pass
    return idx


def describe_local_disc(repo, rel):
    data = os.path.join(repo, "data")
    with open(os.path.join(data, rel), encoding="utf-8-sig") as fh:
        disc = json.load(fh)
    parts = rel.replace("\\", "/").split("/")
    release = {}
    rel_json = os.path.join(data, os.path.dirname(rel), "release.json")
    if os.path.exists(rel_json):
        with open(rel_json, encoding="utf-8-sig") as fh:
            release = json.load(fh)
    return {
        "source": "local TheDiscDB clone",
        "title": parts[1] if len(parts) > 1 else rel,
        "type": parts[0] if parts else None,
        "release": release.get("Title") or (parts[2] if len(parts) > 2 else None),
        "disc": disc.get("Name"), "format": disc.get("Format"),
        "titles": [{
            "source": t.get("SourceFile"), "duration": t.get("Duration"), "size": t.get("DisplaySize"),
            "item": {k.lower(): v for k, v in (t.get("Item") or {}).items() if k != "Chapters"},
        } for t in disc.get("Titles") or []],
        "path": rel,
    }


def discdb_lookup(fp, repo=None, online=True, index=None):
    """Online TheDiscDB first (always current), then the downloaded index, then a local clone."""
    result = {"hit": None, "near": [], "errors": []}
    if online:
        try:
            result["hit"] = discdb_online(fp["hash"])
        except Exception as exc:  # network down, schema change, etc.
            result["errors"].append("online lookup failed: %s" % exc)
    if result["hit"] is not None and index is not None and not result["hit"].get("path"):
        known = index_hit(index, fp["hash"])      # exact folder on GitHub, for the box art
        if known:
            result["hit"].update(path=known["path"], front=known["front"])
    if result["hit"] is None and index is not None:
        result["hit"] = index_hit(index, fp["hash"])
        if result["hit"] is None and not repo:
            result["near"] = index_near(index, fp)
    if result["hit"] is None and repo:
        idx = discdb_local_index(repo)
        rel = idx["hashes"].get(fp["hash"])
        if rel:
            result["hit"] = describe_local_disc(repo, rel)
        else:
            # near match: discs sharing several large, exact file sizes. Ignore sizes that
            # are common across the database (standard 1 GB VOB splits, padding files).
            votes = {}
            big = sorted({s for _, s in fp["files"] if s >= 50_000_000})
            useful = [s for s in big if 0 < len(idx["sizes"].get(str(s), [])) <= 3]
            for s in useful:
                for rel in set(idx["sizes"].get(str(s), [])):
                    votes[rel] = votes.get(rel, 0) + 1
            for rel, n in sorted(votes.items(), key=lambda kv: -kv[1])[:3]:
                if n >= 2:
                    result["near"].append({"path": rel, "shared_large_files": n, "of": len(big)})
    return result


# --------------------------------------------------------------------------
# 2b. TheDiscDB slim index (built by the publishing Action, downloaded by everyone)
# --------------------------------------------------------------------------

DISCDB_INDEX_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE discs (hash TEXT PRIMARY KEY, path TEXT, kind TEXT, title TEXT, year INT, release TEXT,
                    slug TEXT, disc TEXT, format TEXT, front INT, titles TEXT);
CREATE TABLE sizes (size INT, path TEXT);
CREATE TABLE posters (imdb TEXT PRIMARY KEY, folder TEXT, title TEXT, year INT);
"""


def _git_paths(repo, *patterns):
    """Paths git tracks (works in a sparse, blobless checkout where images aren't on disk)."""
    try:
        out = subprocess.run(["git", "-C", repo, "ls-files", "--", *patterns], capture_output=True,
                             text=True, encoding="utf-8", timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(out.stdout.splitlines()) if out.returncode == 0 else None


def discdb_build_index(repo, out_path, commit=None, log=print):
    """TheDiscDB data checkout -> slim SQLite index. Images need not be checked out."""
    import sqlite3
    data = os.path.join(repo, "data")
    images = _git_paths(repo, "data/**/front.jpg", "data/**/cover.jpg") \
        if os.path.exists(os.path.join(repo, ".git")) else None
    has = (lambda rel: ("data/" + rel) in images) if images is not None \
        else (lambda rel: os.path.exists(os.path.join(data, rel)))
    tmp = out_path + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(DISCDB_INDEX_SCHEMA)
    meta_cache, n_discs, n_sizes = {}, 0, 0

    def title_meta(folder):
        if folder not in meta_cache:
            m = {}
            try:
                with open(os.path.join(data, folder, "metadata.json"), encoding="utf-8-sig") as fh:
                    m = json.load(fh)
            except (OSError, ValueError):
                pass
            meta_cache[folder] = m
        return meta_cache[folder]

    for dirpath, dirs, files in os.walk(data):
        dirs.sort()
        rel_dir = os.path.relpath(dirpath, data).replace(os.sep, "/")
        parts = rel_dir.split("/")
        if len(parts) == 2 and parts[0] in ("movie", "series") and "metadata.json" in files:
            m = title_meta(rel_dir)
            imdb = (m.get("ExternalIds") or {}).get("Imdb")
            if imdb and has(rel_dir + "/cover.jpg"):
                con.execute("INSERT OR IGNORE INTO posters VALUES (?,?,?,?)",
                            (imdb, rel_dir, m.get("Title"), _int(m.get("Year"))))
        if len(parts) != 3 or parts[0] not in ("movie", "series"):
            continue
        folder = "/".join(parts[:2])
        m = title_meta(folder)
        release = {}
        if "release.json" in files:
            try:
                with open(os.path.join(dirpath, "release.json"), encoding="utf-8-sig") as fh:
                    release = json.load(fh)
            except (OSError, ValueError):
                pass
        front = 1 if has(rel_dir + "/front.jpg") else 0
        for fn in sorted(files):
            path = rel_dir + "/" + fn
            if re.match(r"disc\d+\.json$", fn):
                try:
                    with open(os.path.join(dirpath, fn), encoding="utf-8-sig") as fh:
                        d = json.load(fh)
                except (OSError, ValueError):
                    continue
                h = (d.get("ContentHash") or "").upper()
                if not re.fullmatch(r"[0-9A-F]{32}", h):
                    continue
                titles = [{"source": t.get("SourceFile"), "duration": t.get("Duration"),
                           "size": t.get("DisplaySize"),
                           "item": {k.lower(): v for k, v in (t.get("Item") or {}).items() if k != "Chapters"}}
                          for t in d.get("Titles") or []]
                title = m.get("Title") or re.sub(r"\s*\(\d{4}\)$", "", parts[1])
                year = _int(m.get("Year")) or _int((re.search(r"\((\d{4})\)$", parts[1]) or [None, None])[1])
                con.execute("INSERT OR IGNORE INTO discs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (h, path, parts[0], title, year, release.get("Title") or parts[2],
                             release.get("Slug") or parts[2], d.get("Name"), d.get("Format"), front,
                             json.dumps(titles, separators=(",", ":"))))
                n_discs += 1
            elif re.match(r"disc\d+\.txt$", fn):
                jpath = path[:-4] + ".json"
                rows = set()
                with open(os.path.join(dirpath, fn), encoding="utf-8-sig", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("HSH:"):
                            mm = HSH_RE.match(line.strip())
                            if mm and int(mm.group(2)) >= 50_000_000:
                                rows.add((int(mm.group(2)), jpath))
                con.executemany("INSERT INTO sizes VALUES (?,?)", sorted(rows))
                n_sizes += len(rows)
    con.execute("CREATE INDEX ix_sizes ON sizes(size)")
    built = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_posters = con.execute("SELECT COUNT(*) FROM posters").fetchone()[0]
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("version", str(DISCDB_INDEX_VERSION)), ("built", built), ("commit", commit or ""),
        ("discs", str(n_discs)), ("posters", str(n_posters)), ("notice", DISCDB_NOTICE)])
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, out_path)
    log("TheDiscDB index: %d discs, %d large-file sizes, %d posters -> %s" % (n_discs, n_sizes, n_posters, out_path))
    return out_path


def default_discdb_dir():
    return env("DISCDB_DIR") or os.path.join(data_home(), "discdb")


def _stream(resp, dest, label):
    """Copy an HTTP response to dest, reporting progress through the `progress` hook."""
    total = _int(resp.headers.get("Content-Length")) if getattr(resp, "headers", None) else None
    done = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress:
                progress(label, done, total)
    return done


def discdb_index_download(discdb_dir, force=False, log=print):
    """Fetch the published index when missing or older than a week (ETag: unchanged = no download)."""
    import gzip, sqlite3
    os.makedirs(discdb_dir, exist_ok=True)
    path = os.path.join(discdb_dir, DISCDB_INDEX_DB)
    etag_file = path + ".etag"
    if os.path.exists(path) and not force:
        age = (_dt.datetime.now().timestamp() - os.path.getmtime(path)) / 86400
        if age < DISCDB_MAX_AGE_DAYS:
            return False
    headers = {"User-Agent": USER_AGENT}
    if os.path.exists(path) and os.path.exists(etag_file):
        with open(etag_file, encoding="utf-8") as fh:
            headers["If-None-Match"] = fh.read().strip()
    log("checking for a newer TheDiscDB index ..." if os.path.exists(path) else "downloading the TheDiscDB index ...")
    try:
        resp = urllib.request.urlopen(urllib.request.Request(DISCDB_INDEX_URL, headers=headers), timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:                    # unchanged since our copy
            os.utime(path)
            return False
        raise
    gz = path + ".gz.part"
    with resp:
        _stream(resp, gz, "TheDiscDB index")
        etag = resp.headers.get("ETag") if getattr(resp, "headers", None) else None
    part = path + ".part"
    with gzip.open(gz, "rb") as src, open(part, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    os.remove(gz)
    con = sqlite3.connect(part)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()
    if _int(meta.get("version")) != DISCDB_INDEX_VERSION:
        os.remove(part)
        raise RuntimeError("TheDiscDB index format %s needs a different version of this program" % meta.get("version"))
    os.replace(part, path)
    if etag:
        with open(etag_file, "w", encoding="utf-8") as fh:
            fh.write(etag)
    log("TheDiscDB index: %s discs, built %s" % (meta.get("discs"), meta.get("built")))
    return True


def discdb_index_open(discdb_dir, allow_download=True, force=False, log=print, errors=None):
    """sqlite connection to the index (downloading/refreshing it first when allowed), or None."""
    import sqlite3
    path = os.path.join(discdb_dir, DISCDB_INDEX_DB)
    if allow_download:
        try:
            discdb_index_download(discdb_dir, force=force, log=log)
        except Exception as exc:              # offline / not published yet: use what we have
            if errors is not None:
                errors.append("index download failed: %s" % exc)
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def discdb_index_info(discdb_dir):
    """For settings screens: what the local index is and when it was built."""
    import sqlite3
    path = os.path.join(discdb_dir, DISCDB_INDEX_DB)
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        con.close()
    except sqlite3.Error:
        return None
    meta["checked"] = _dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    meta["path"] = path
    return meta


def index_hit(con, h):
    r = con.execute("SELECT * FROM discs WHERE hash=?", (h,)).fetchone()
    if not r:
        return None
    built = (con.execute("SELECT value FROM meta WHERE key='built'").fetchone() or [""])[0]
    return {"source": "TheDiscDB index (%s)" % built[:10], "title": r["title"], "year": r["year"],
            "type": r["kind"], "release": r["release"], "slug": r["slug"], "disc": r["disc"],
            "format": r["format"], "titles": json.loads(r["titles"] or "[]"), "path": r["path"],
            "front": bool(r["front"])}


def index_near(con, fp):
    votes = {}
    big = sorted({s for _, s in fp["files"] if s >= 50_000_000})
    for s in big:
        paths = [r[0] for r in con.execute("SELECT DISTINCT path FROM sizes WHERE size=?", (s,))]
        if 0 < len(paths) <= 3:           # common sizes (1 GB VOB splits, padding) prove nothing
            for p in paths:
                votes[p] = votes.get(p, 0) + 1
    return [{"path": p, "shared_large_files": n, "of": len(big)}
            for p, n in sorted(votes.items(), key=lambda kv: -kv[1])[:3] if n >= 2]


# --------------------------------------------------------------------------
# 3a. DVD structure
# --------------------------------------------------------------------------

AUDIO_CODING = {0: "AC3", 2: "MPEG1", 3: "MPEG2", 4: "LPCM", 6: "DTS"}
AUDIO_EXT = {3: "commentary", 4: "commentary"}
SUB_EXT = {5: "captions", 6: "captions", 7: "captions", 13: "commentary", 14: "commentary", 15: "commentary"}


def bcd(b):
    return (b >> 4) * 10 + (b & 0x0F)


def dvd_time(b, o):
    h, m, s, f = b[o], b[o + 1], b[o + 2], b[o + 3]
    rate = 25.0 if (f >> 6) == 1 else 29.97
    return bcd(h) * 3600 + bcd(m) * 60 + bcd(s) + bcd(f & 0x3F) / rate


def parse_vts(data):
    """Return per-VTS attributes plus PGC durations and cell ranges."""
    va = u16(data, 0x200)
    std = {0: "NTSC", 1: "PAL"}.get((va >> 12) & 3, "?")
    aspect = {0: "4:3", 3: "16:9"}.get((va >> 10) & 3, "?")
    letterboxed = bool(va & 0x0004)
    audio = []
    for i in range(min(u16(data, 0x202), 8)):
        a = data[0x204 + i * 8: 0x204 + i * 8 + 8]
        audio.append({
            "lang": lang(a[2:4]),
            "codec": AUDIO_CODING.get(a[0] >> 5, "?"),
            "channels": (a[1] & 7) + 1,
            "kind": AUDIO_EXT.get(a[5], ""),
        })
    subs = []
    for i in range(min(u16(data, 0x254), 32)):
        s = data[0x256 + i * 6: 0x256 + i * 6 + 6]
        subs.append({"lang": lang(s[2:4]), "kind": SUB_EXT.get(s[5], "")})

    pgci = u32(data, 0xCC) * 2048
    pgcs = []
    for i in range(u16(data, pgci)):
        off = pgci + u32(data, pgci + 8 + i * 8 + 4)
        if off + 0xEA > len(data):
            # a chain pointing past the end of the file (protection or bad authoring): skip it,
            # don't lose the whole title set over it
            pgcs.append({"duration": 0.0, "programs": 0, "cells": []})
            continue
        dur = dvd_time(data, off + 4)
        ncells = data[off + 3]
        cp = u16(data, off + 0xE8)   # cell playback information table
        cells = []
        for c in range(ncells):
            co = off + cp + c * 24
            if co + 24 <= len(data):
                cells.append((u32(data, co + 8), u32(data, co + 20)))
        pgcs.append({"duration": dur, "programs": data[off + 2], "cells": cells})

    ptt = u32(data, 0xC8) * 2048
    ntitles = u16(data, ptt)
    end = ptt + u32(data, ptt + 4) + 1
    offs = [ptt + u32(data, ptt + 8 + i * 4) for i in range(ntitles)] + [end]
    title_pgcs = []
    for i in range(ntitles):
        seen = []
        for o in range(offs[i], offs[i + 1], 4):
            pgcn = u16(data, o)
            if 1 <= pgcn <= len(pgcs) and pgcn not in seen:
                seen.append(pgcn)
        title_pgcs.append(seen)
    return {"standard": std, "aspect": aspect, "letterboxed": letterboxed,
            "audio": audio, "subs": subs, "pgcs": pgcs, "title_pgcs": title_pgcs}


def parse_dvd(vts_dir):
    ifo = find_child(vts_dir, "VIDEO_TS.IFO")
    try:
        vmg = read_file(ifo)
    except PermissionError:
        # Some drives refuse reads until the disc is authenticated; the backup copy often works.
        bup = find_child(vts_dir, "VIDEO_TS.BUP")
        if not bup:
            raise
        vmg = read_file(bup)
    if vmg[:12] != b"DVDVIDEO-VMG":
        raise ValueError("VIDEO_TS.IFO is not a DVD-Video VMG")
    provider = vmg[0x40:0x60].decode("latin-1", "replace").strip("\0 ")
    tt = u32(vmg, 0xC4) * 2048
    vts_cache, titles = {}, []
    for i in range(u16(vmg, tt)):
        e = tt + 8 + i * 12
        nch, vtsn, ttn = u16(vmg, e + 2), vmg[e + 6], vmg[e + 7]
        if vtsn not in vts_cache:
            path = find_child(vts_dir, "VTS_%02d_0.IFO" % vtsn)
            try:
                vts_cache[vtsn] = parse_vts(read_file(path)) if path else None
            except (OSError, struct.error, IndexError):
                vts_cache[vtsn] = None
        v = vts_cache[vtsn]
        vi = {"standard": v["standard"], "aspect": v["aspect"], "letterboxed": v["letterboxed"]} if v else {}
        dur, cells = 0.0, []
        if v and ttn <= len(v["title_pgcs"]):
            for pgcn in v["title_pgcs"][ttn - 1]:
                dur += v["pgcs"][pgcn - 1]["duration"]
                cells += v["pgcs"][pgcn - 1]["cells"]
        titles.append({
            "id": "Title %d" % (i + 1), "source": "VTS %d / title %d" % (vtsn, ttn),
            "group": "VTS %d" % vtsn, "duration": dur, "chapters": nch,
            "segments": tuple((vtsn,) + c for c in cells),
            "audio": v["audio"] if v else [], "subs": v["subs"] if v else [],
            "video": video_text(vi) if v else "?",
            "video_info": vi,
        })
    return {"provider": provider, "titles": titles}


# --------------------------------------------------------------------------
# 3b. Blu-ray structure
# --------------------------------------------------------------------------

def parse_mpls(data):
    if data[:4] != b"MPLS":
        raise ValueError("not an MPLS playlist")
    pl, marks = u32(data, 8), u32(data, 12)
    n_items = u16(data, pl + 6)
    o = pl + 10
    items, video = [], None
    for _ in range(n_items):
        length = u16(data, o)
        clip = data[o + 2:o + 7].decode("latin-1")
        t_in, t_out = u32(data, o + 14), u32(data, o + 18)
        items.append((clip, t_in, t_out))
        if video is None:
            try:
                video = _stn_video(data[o + 2:o + 2 + length])
            except (IndexError, struct.error):
                video = {}
        o += length + 2
    n_marks = u16(data, marks + 4)
    per_item = [0] * len(items)
    for i in range(n_marks):
        m = marks + 6 + i * 14
        if data[m + 1] == 1:
            ref = u16(data, m + 2)
            if ref < len(per_item):
                per_item[ref] += 1
    duration = sum(max(0, t_out - t_in) for _, t_in, t_out in items) / 45000.0
    video = video or {}
    try:
        if _mpls_3d(data):
            video["3d"] = True
    except (IndexError, struct.error):
        pass
    return {"items": items, "chapters": sum(per_item), "item_chapters": per_item, "duration": duration,
            "video": video}


BD_VIDEO_FORMAT = {1: 480, 2: 576, 3: 480, 4: 1080, 5: 720, 6: 1080, 7: 576, 8: 2160}
BD_INTERLACED = {1, 2, 4}
BD_RATE = {1: "23.976", 2: "24", 3: "25", 4: "29.97", 6: "50", 7: "59.94"}
BD_CODEC = {0x01: "MPEG-1", 0x02: "MPEG-2", 0x1B: "H.264", 0x20: "H.264", 0x24: "HEVC", 0xEA: "VC-1"}
BD_RANGE = {1: "HDR10", 2: "Dolby Vision"}


BD_AUDIO = {0x80: "LPCM", 0x81: "AC3", 0x82: "DTS", 0x83: "TrueHD", 0x84: "DD+", 0x85: "DTS-HD",
            0x86: "DTS-HD MA", 0xA1: "DD+", 0xA2: "DTS-HD"}
BD_CHANNELS = {1: 1, 3: 2, 6: 6, 12: 8}
ISO639_2 = {"eng": "en", "spa": "es", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "ita": "it",
            "jpn": "ja", "por": "pt", "nld": "nl", "dut": "nl", "zho": "zh", "chi": "zh", "kor": "ko",
            "rus": "ru", "ces": "cs", "cze": "cs", "ron": "ro", "rum": "ro", "tha": "th", "pol": "pl",
            "hun": "hu", "tur": "tr", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi", "ell": "el",
            "gre": "el", "heb": "he", "ara": "ar", "hin": "hi", "ind": "id", "ukr": "uk", "vie": "vi"}


def _bd_lang(b):
    code = b.decode("latin-1", "replace").strip("\0 ").lower()
    return ISO639_2.get(code, code or None)


def _stn_video(item):
    """Primary video of a PlayItem (its bytes after the length field): resolution, rate, codec,
    HDR (UHD discs), 3D (MVC). From the STN table."""
    q = 32                                    # clip name .. still time
    if item[10] & 0x10:                       # multi-angle: skip the extra angles
        q += 2 + (item[32] - 1) * 10
    stn = item[q + 2:]
    nv, na, npg, e = stn[2], stn[3], stn[4], 14
    if not nv:
        return {}

    def streams(n):
        nonlocal e
        out = []
        for _ in range(n):
            e += 1 + stn[e]                   # stream entry
            out.append(bytes(stn[e + 1:e + 1 + stn[e]]))
            e += 1 + stn[e]
        return out
    vids, auds, pgs = streams(nv), streams(na), streams(npg)
    attr = vids[0]
    fmt, rate = attr[1] >> 4, attr[1] & 15
    out = {"codec": BD_CODEC.get(attr[0], "?"), "height": BD_VIDEO_FORMAT.get(fmt),
           "scan": "i" if fmt in BD_INTERLACED else "p", "fps": BD_RATE.get(rate),
           "audio": [{"lang": _bd_lang(a[2:5]), "codec": BD_AUDIO.get(a[0], "?"),
                      "channels": BD_CHANNELS.get(a[1] >> 4, 0), "kind": ""} for a in auds if len(a) >= 5],
           "subs": [{"lang": _bd_lang(g[1:4]), "kind": ""} for g in pgs if len(g) >= 4 and g[0] == 0x90]}
    if attr[0] == 0x24 and len(attr) > 2:
        out["hdr"] = BD_RANGE.get(attr[2] >> 4)
    if attr[0] == 0x20:                       # MVC: 3D
        out["3d"] = True
    if stn[7]:                                # secondary video: picture-in-picture (commentary mode)
        out["pip"] = True
    return out


def _mpls_3d(data):
    """A 3D playlist carries an STN_SS table in its extension data (ID 2/1)."""
    ext = u32(data, 16)
    if not ext or ext + 12 > len(data) or u32(data, ext) == 0:
        return False
    n = data[ext + 11]
    for i in range(n):
        e = ext + 12 + i * 12
        if u16(data, e) == 2 and u16(data, e + 2) == 1:
            return True
    return False


def clpi_aspect(data):
    """Frame shape of a clip's video (4:3 / 16:9) from its CLPI ProgramInfo, or None."""
    if data[:4] != b"HDMV":
        return None
    p = u32(data, 12)                         # ProgramInfo
    if data[p + 5] < 1:
        return None
    o = p + 6
    nstreams = data[o + 6]
    o += 8
    for _ in range(nstreams):
        o += 2                                # PID
        ln, coding = data[o], data[o + 1]
        if coding in BD_CODEC:                # video: [format|rate][aspect|...]
            return {2: "4:3", 3: "16:9"}.get(data[o + 3] >> 4)
        o += 1 + ln
    return None


def video_text(v):
    """'1080p 16:9 H.264', '2160p HDR10 HEVC', 'NTSC 4:3' ... for the title list."""
    if not v:
        return ""
    if v.get("standard"):
        return "%s %s%s" % (v["standard"], v.get("aspect") or "?", " letterboxed" if v.get("letterboxed") else "")
    bits = ["%s%s" % (v["height"], v.get("scan", "")) if v.get("height") else "",
            "3D" if v.get("3d") else "", v.get("hdr") or "", v.get("aspect") or "", v.get("codec") or ""]
    text = " ".join(b for b in bits if b and b != "?")
    return text + (" + picture-in-picture" if v.get("pip") else "")


def video_rank(v):
    """Which of two copies of a film is the one to keep: higher resolution, then widescreen."""
    v = v or {}
    wide = v.get("aspect") == "16:9" or bool(v.get("letterboxed"))
    height = v.get("height") or (576 if v.get("standard") == "PAL" else 480 if v.get("standard") else 0)
    return (height, bool(v.get("hdr")), wide)


def version_label(v, other):
    """How this copy differs from the other one: 'Fullscreen', 'Widescreen', '4K', '1080p', '3D'."""
    v, other = v or {}, other or {}
    wide = lambda x: x.get("aspect") == "16:9" or bool(x.get("letterboxed"))
    if bool(v.get("3d")) != bool(other.get("3d")):
        return "3D" if v.get("3d") else "2D"
    if bool(v.get("pip")) != bool(other.get("pip")) and v.get("pip"):
        return "Picture-in-Picture"
    if (v.get("height") or 0) != (other.get("height") or 0) and v.get("height"):
        return "4K" if v["height"] >= 2160 else "%dp" % v["height"]
    if v.get("aspect") and other.get("aspect") and wide(v) != wide(other):
        return "Widescreen" if wide(v) else "Fullscreen"
    return None


def authoring_names(bdmv):
    """Playlist names from an authoring tool's PLsInfo.txt, in playlist order, or []."""
    jar = find_child(bdmv, "JAR")
    for path in sorted(glob.glob(os.path.join(jar, "*", "PLsInfo.txt"))) if jar else []:
        try:
            text = read_file(path).decode("latin-1")
        except OSError:
            continue
        found = [x.strip() for x in re.findall(r"^#\s*Playlist name:[ \t]*(.*?)[ \t]*\r?$", text, re.M)]
        if found:
            return found
    return []


AUTHORING_ROLES = [(r"^(TRL|TRAILER)", "extra", "trailer"),
                   (r"^(VAM|BONUS|EXTRA|FEATURETTE|MAKING|BTS|DELETED)", "extra", "bonus material"),
                   (r"FEATURE_EP|^EP_?\d|EPISODE", "episode", "an episode")]


def apply_authoring_names(titles):
    """Trust the authoring names where they're unambiguous: a playlist named as a trailer or
    a featurette is an extra, one named as an episode is an episode (unless it's a play-all)."""
    for t in titles:
        nm = (t.get("authoring_name") or "").upper()
        for pat, role, what in AUTHORING_ROLES:
            if nm and re.search(pat, nm):
                if role == "episode" and t["role"] in ("play-all", "duplicate", "junk", "loop"):
                    break
                if t["role"] in ("episode", "extra", "version", "feature") and t["role"] != role:
                    t["role"] = role
                t["note"] = ("authoring name %s: %s" % (t["authoring_name"], what)) + \
                    ("; " + t["note"] if t.get("note") and "authoring" not in t["note"] else "")
                break


def parse_bluray(bdmv):
    titles = []
    pl_dir = find_child(bdmv, "PLAYLIST")
    clip_dir = find_child(bdmv, "CLIPINF")
    aspects = {}

    def clip_aspect(clip):
        if clip not in aspects:
            path = find_child(clip_dir, clip + ".clpi") if clip_dir else None
            try:
                aspects[clip] = clpi_aspect(read_file(path)) if path else None
            except (OSError, IndexError, struct.error):
                aspects[clip] = None
        return aspects[clip]
    for fn in sorted(os.listdir(pl_dir) if pl_dir else []):
        if not fn.lower().endswith(".mpls"):
            continue
        try:
            p = parse_mpls(read_file(os.path.join(pl_dir, fn)))
        except (OSError, ValueError, struct.error, IndexError):
            continue
        counts = {}
        for c, _, _ in p["items"]:
            counts[c] = counts.get(c, 0) + 1
        top_clip, top_n = max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)
        loop = top_n >= 4 and top_n * 2 >= len(p["items"])   # one clip repeated: menu background loop
        v = dict(p["video"])
        bd_audio, bd_subs = v.pop("audio", []), v.pop("subs", [])
        if p["items"]:
            v["aspect"] = clip_aspect(p["items"][0][0])
        titles.append({
            "id": fn, "source": fn, "group": "playlists", "video_info": v,
            "loop": ("clip %s repeated %d times" % (top_clip, top_n)) if loop else None,
            "duration": p["duration"], "chapters": p["chapters"], "item_chapters": p["item_chapters"],
            "segments": tuple(p["items"]),
            "clips": "+".join(c for c, _, _ in p["items"][:6]) + ("..." if len(p["items"]) > 6 else ""),
            "clip_list": ",".join(dict.fromkeys(c for c, _, _ in p["items"])),
            "audio": bd_audio, "subs": bd_subs, "video": video_text(v),
        })
    # Discs authored with Panasonic's tools may carry BDMV/JAR/*/PLsInfo.txt, which names
    # every playlist in order (FEATURE_EP_Pilot, VAM_Making, TRL_BUFFY, Fox_Logo...).
    names = authoring_names(bdmv)
    n_files = len([f for f in os.listdir(pl_dir) if f.lower().endswith(".mpls")]) if pl_dir else 0
    if names and len(names) == n_files:
        # one name per playlist file, in order: 00001.mpls is the first (paired by number, so
        # a playlist we couldn't read doesn't shift the rest)
        files = sorted(f for f in os.listdir(pl_dir) if f.lower().endswith(".mpls"))
        by_file = dict(zip(files, names))
        for t in titles:
            if t["id"] in by_file:
                t["authoring_name"] = by_file[t["id"]]
    # Some TV discs have no per-episode playlists: one play-all strings the episode clips
    # together and the menu jumps into it. Expose each episode-length clip as its own title.
    have = {t["segments"] for t in titles}
    virtual = []
    for t in titles:
        items = list(t["segments"])
        clips = {c for c, _, _ in items}
        lens = [(o - i) / 45000.0 for _, i, o in items]
        if len(items) >= 2 and len(clips) == len(items) and all(EPISODE_MIN <= x <= EPISODE_MAX for x in lens):
            for k, item in enumerate(items):
                if (item,) in have:
                    continue
                have.add((item,))
                virtual.append({
                    # named like MakeMKV / TheDiscDB name stream-file titles
                    "id": "%s.m2ts" % item[0], "source": "%s item %d" % (t["id"], k + 1),
                    "group": "playlists", "loop": None, "duration": lens[k],
                    "chapters": t.get("item_chapters", [0] * len(items))[k],
                    "segments": (item,), "clips": item[0], "clip_list": item[0], "audio": [], "subs": [],
                    "video": t["video"], "video_info": t.get("video_info"),
                })
    titles += virtual
    meta = {"names": [], "toc": [], "thumbnails": []}
    dl = find_child(find_child(bdmv, "META") or "", "DL") if find_child(bdmv, "META") else None
    for xml_path in sorted(glob.glob(os.path.join(dl, "*.xml"))) if dl else []:
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag == "name" and el.text:
                meta["names"].append(el.text.strip())
            elif tag == "titleName" and el.text:
                meta["toc"].append((el.get("titleNumber"), el.text.strip()))
            elif tag == "thumbnail" and el.get("href"):
                meta["thumbnails"].append(os.path.join(dl, el.get("href")))
    if dl:
        for img in glob.glob(os.path.join(dl, "*.jpg")) + glob.glob(os.path.join(dl, "*.JPG")):
            if img not in meta["thumbnails"]:
                meta["thumbnails"].append(img)
    return {"titles": titles, "meta": meta}


# --------------------------------------------------------------------------
# 3c. classification
# --------------------------------------------------------------------------

def classify(titles, fmt):
    """Label each title: junk / duplicate / play-all / version / feature / episode / extra."""
    for t in titles:
        t["role"], t["note"] = None, ""
        if t.get("loop"):
            t["role"], t["note"] = "loop", "menu loop: " + t["loop"]
        elif t["duration"] < JUNK_MAX:
            t["role"] = "junk"
    real = [t for t in titles if t["role"] is None and t["segments"]]

    # exact duplicates: identical underlying video segments
    first_by_sig = {}
    for t in real:
        sig = t["segments"]
        if sig in first_by_sig:
            t["role"], t["note"] = "duplicate", "same video as %s" % first_by_sig[sig]["id"]
        else:
            first_by_sig[sig] = t
    uniq = [t for t in real if t["role"] is None]

    # play-all: segments are the concatenation of 2+ other titles' segments
    seg_owner = {}
    for t in uniq:
        if len(t["segments"]) >= 1:
            seg_owner.setdefault(t["segments"][0], []).append(t)
    for t in sorted(uniq, key=lambda x: -x["duration"]):
        parts, pos, segs = [], 0, t["segments"]
        while pos < len(segs):
            match = None
            for cand in seg_owner.get(segs[pos], []):
                if cand is t or cand["role"] == "play-all":
                    continue
                n = len(cand["segments"])
                if segs[pos:pos + n] == cand["segments"] and (match is None or n > len(match["segments"])):
                    match = cand
            if not match:
                break
            parts.append(match)
            pos += len(match["segments"])
        if pos == len(segs) and len(parts) >= 2:
            t["role"] = "play-all"
            t["note"] = "plays " + ", ".join(p["id"] for p in parts)
            for k, part in enumerate(parts):
                part["in_play_all"] = True
                part.setdefault("play_order", (t["id"], k))
    # duration fallback for play-alls whose cells don't line up (some DVD authoring):
    # a title as long as a run of 2+ consecutive episode-length titles in its group.
    def num(t):
        return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", t["id"])]
    uniq = [t for t in uniq if t["role"] is None]
    # the shortest episode: 18 minutes, or a bit less when the disc's episodes run short
    # (1930s shorts of 17-25 minutes); never below 10, so featurettes stay extras
    ep_lengths = sorted(t["duration"] for t in uniq if EPISODE_MIN <= t["duration"] <= EPISODE_MAX)
    ep_min = EPISODE_MIN
    if len(ep_lengths) >= 3:
        ep_min = max(10 * 60, min(EPISODE_MIN, 0.8 * ep_lengths[(len(ep_lengths) - 1) // 2]))
    for t in sorted(uniq, key=lambda x: -x["duration"]):
        # a piece of a confirmed play-all is content (a double episode), not another play-all.
        # Only DVDs need this: a Blu-ray play-all always reuses the episodes' own clips, so a
        # playlist with a clip of its own (Firefly's two-hour Serenity) isn't one by length.
        if fmt != "DVD" or t["duration"] < 2 * EPISODE_MIN or t.get("in_play_all"):
            continue
        pool = sorted((x for x in uniq if x is not t and x["role"] is None and x["group"] == t["group"]
                       and ep_min <= x["duration"] <= EPISODE_MAX), key=num)
        for i in range(len(pool)):
            total = 0.0
            for j in range(i, len(pool)):
                total += pool[j]["duration"]
                n = j - i + 1
                if n >= 2 and abs(total - t["duration"]) <= SAME_LENGTH_TOL * n + 2:
                    t["role"] = "play-all"
                    t["note"] = "length equals %s" % ", ".join(x["id"] for x in pool[i:j + 1])
                    for part in pool[i:j + 1]:
                        part["in_play_all"] = True
                    break
                if total > t["duration"] + 10:
                    break
            if t["role"]:
                break
    uniq = [t for t in uniq if t["role"] is None]

    for t in uniq:                     # hours of the same few clips over and over: a background loop
        segs = t.get("segments") or ()
        nclips = len({x[0] for x in segs}) if fmt == "Blu-ray" else 0
        if t["role"] is None and t["duration"] >= FEATURE_MIN and nclips and len(segs) >= 10 * nclips:
            t["role"], t["note"] = "loop", "repeats %d clips %d times (a background or slideshow loop)" % (
                nclips, len(segs))
    uniq = [t for t in uniq if t["role"] != "loop"]
    if fmt == "DVD":                   # a "film" with almost no video: an interactive game / slideshow
        rates = [r for r in (dvd_mbps(t) for t in uniq if t["duration"] >= FEATURE_MIN) if r]
        top_rate = max(rates, default=0)
        for t in uniq:
            rate = dvd_mbps(t)
            # far below the disc's real film-length video (relative: a whole disc can be low-rate)
            if t["role"] is None and t["duration"] >= FEATURE_MIN and rate is not None \
                    and rate < 1.5 and rate < 0.25 * top_rate:
                t["interactive"] = True
                t["role"], t["note"] = "extra", ("interactive feature or slideshow, not a film: %.1f Mbit/s of "
                                                 "video for its length (mostly still frames)" % rate)
        uniq = [t for t in uniq if t["role"] is None]
    long_ = [t for t in uniq if t["duration"] >= FEATURE_MIN]
    eps = [t for t in uniq if ep_min <= t["duration"] <= EPISODE_MAX]

    # Same length to the frame and same chapters, but different video: alternate versions
    # (widescreen/full screen, censored/uncut). Different TV episodes are often within a
    # second or two of each other, so episodes only collapse on a near-exact match.
    def same_length(a, b, tol):
        return abs(a["duration"] - b["duration"]) <= tol and a["chapters"] == b["chapters"]

    if len(eps) >= 2 and (not long_ or len(season_like(eps)) >= 3 * len(long_)):
        kind = "tv"
        primaries = []
        for t in sorted(eps, key=lambda x: x["id"]):
            # a title the play-all includes is its own episode, however close in length
            twin = None if t.get("in_play_all") else next(
                (p for p in primaries if same_length(p, t, 0.1) and not p.get("in_play_all")), None)
            if twin:
                t["role"], t["note"] = "version", "identical length to %s: likely an alternate version" % twin["id"]
            else:
                t["role"] = "episode"
                primaries.append(t)
        typical = sorted(t["duration"] for t in primaries)[(len(primaries) - 1) // 2] if primaries else 0
        for t in long_:
            if typical and t["duration"] <= 2.3 * typical and t["duration"] <= 150 * 60:
                # about twice an episode: a two-hour pilot or finale (Firefly's Serenity)
                t["role"], t["note"] = "episode", "double length: likely two episodes or a two-hour episode"
            else:
                t["role"], t["note"] = "extra", "long title on an episode disc (special or feature)"
        if fmt == "Blu-ray":
            # an episode-length title in lower resolution than the episodes is a featurette
            hs = sorted((t.get("video_info") or {}).get("height") or 0 for t in titles if t["role"] == "episode")
            top = hs[len(hs) // 2] if hs else 0
            for t in titles:
                h = (t.get("video_info") or {}).get("height") or 0
                if t["role"] == "episode" and top and h and h < top:
                    t["role"], t["note"] = "extra", "lower resolution than the episodes (%d vs %d lines): a featurette" % (h, top)
        # When a play-all covers the episodes of one title set, an episode-length title sitting
        # outside it, in a different title set, is usually a bonus (pilot, other show, featurette).
        covered = [t for t in titles if t.get("in_play_all") and t["role"] == "episode"]
        play_alls = [t for t in titles if t["role"] == "play-all"]
        if len(play_alls) == 1 and len(covered) >= 3:   # one "play everything" title
            groups = {t["group"] for t in covered}
            for t in titles:
                if t["role"] == "episode" and not t.get("in_play_all") and t["group"] not in groups:
                    t["role"], t["note"] = "extra", "episode-length but outside the play-all (bonus episode?)"
    elif long_:
        kind = "movie"
        long_.sort(key=lambda x: -x["duration"])
        close = lambda a, b: same_length(a, b, SAME_LENGTH_TOL) or abs(a["duration"] - b["duration"]) <= 5
        # Copies of the film at the same length (widescreen + full screen, 4K + 1080p): keep the
        # best picture as the feature: higher resolution, then widescreen, then no PiP overlay.
        def same_film(t, ref):
            """Same film with a different opening/ending (or minus one): localized credits, a
            different studio logo, a picture-in-picture mode. Not different scenes mid-film."""
            share, where = shared_video(t, ref)
            if where is None and share >= 0.99 and t["duration"] <= ref["duration"]:
                # t plays nothing but ref's pieces: ref without its logos / credits, or a PiP mode
                # over the film's main clip. Part of the same film however much shorter.
                return t["duration"] >= 0.6 * ref["duration"]
            if where is None:
                share = max(share, shared_video(ref, t)[0]) if share >= 0.85 else 0
            return share >= 0.85 and where in ("start", "end", None) and abs(t["duration"] - ref["duration"]) <= 240
        twins = [t for t in long_ if close(long_[0], t) or same_film(t, long_[0])]
        num = lambda t: int(re.sub(r"\D", "", t["id"]) or 0)
        # DVD players start with the lowest title (usually the original-language one)
        first_lang = lambda t: (t.get("audio") or [{}])[0].get("lang")
        main = max(twins, key=lambda t: video_rank(t.get("video_info"))
                   + (not (t.get("video_info") or {}).get("pip"), first_lang(t) in (None, "en"),
                      -num(t) if fmt == "DVD" else t["duration"]))
        main["role"] = "feature"
        for t in long_:
            if t is main:
                continue
            shared, where = shared_video(t, main)
            gap = t["duration"] - main["duration"]
            what = version_label(t.get("video_info"), main.get("video_info"))
            if same_film(t, main) and what:
                t["role"], t["edition"] = "version", what
                t["note"] = "%s version of %s (%s)" % (what.lower(), main["id"], t["video"])
                continue
            if same_film(t, main):
                # The same film with its own opening or end credits: localized title cards / credits.
                # (Different scenes in the middle is another cut, e.g. an extended edition: kept.)
                t["role"], t["edition"] = "version", "Alternate %s" % ("Credits" if where == "end" else "Titles")
                lang_t, lang_m = first_lang(t), first_lang(main)
                language = LANG_NAMES.get(lang_t, lang_t) if lang_t and lang_t != lang_m else None
                t["note"] = ("same film as %s except the %s (%d s %s): %s"
                             % (main["id"], "end credits" if where == "end" else "opening titles",
                                abs(round(gap)), "longer" if gap > 0 else "shorter",
                                "the %s version (%s audio first)" % (language, language) if language
                                else "likely another language's version"))
                if language:
                    t["edition"] = language
                continue
            if close(main, t):
                t["role"] = "version"
                what = version_label(t.get("video_info"), main.get("video_info"))
                if what:
                    t["edition"] = what
                    t["note"] = "%s version of %s (%s vs %s)" % (what.lower(), main["id"], t["video"], main["video"])
                else:
                    t["note"] = "same length as %s%s" % (main["id"], (" (%s vs %s)" % (main["video"], t["video"]))
                                                         if main["video"] != t["video"] else "")
            else:
                t["role"] = "feature"
        for t in eps:
            t["role"] = "extra"
    else:
        kind = "unknown"
    for t in titles:
        if t["role"] is None:
            t["role"] = "extra"

    # Blu-ray playlist obfuscation: many unique playlists of near-identical feature length
    features = [t for t in titles if t["role"] in ("feature", "version")]
    obfuscated = fmt == "Blu-ray" and len(features) >= 6
    if fmt == "DVD" and kind == "movie":
        obfuscated = dvd_decoys(features) or obfuscated
    return kind, obfuscated


LANG_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
              "ja": "Japanese", "pt": "Portuguese", "nl": "Dutch", "zh": "Chinese", "ko": "Korean",
              "ru": "Russian", "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
              "fi": "Finnish", "cs": "Czech", "hu": "Hungarian", "tr": "Turkish", "th": "Thai"}


def dvd_mbps(t):
    """Average data rate of a DVD title from its cells' sector ranges (Mbit/s), or None. Real
    video runs 3-10; a title that plays a few stills for an hour shows well under 1."""
    segs = [x for x in t.get("segments") or () if len(x) == 3 and isinstance(x[1], int)]
    if not segs or not t.get("duration"):
        return None
    sectors = sum(b - a + 1 for _, a, b in segs)
    return sectors * 2048 * 8 / t["duration"] / 1e6


def season_like(eps):
    """The episode-length titles that look like one season: similar length (within 25% of the
    typical one) and the same picture format. A film's extras (a 32-min 4:3 making-of, an hour-long
    1080p documentary) are episode-length too, but they don't match each other."""
    if len(eps) < 2:
        return eps
    durs = sorted(t["duration"] for t in eps)
    mid = durs[len(durs) // 2]
    fmt = lambda t: ((t.get("video_info") or {}).get("height"), (t.get("video_info") or {}).get("aspect"),
                     (t.get("video_info") or {}).get("standard"))
    from collections import Counter
    common = Counter(fmt(t) for t in eps).most_common(1)[0][0]
    return [t for t in eps if abs(t["duration"] - mid) <= 0.25 * mid and fmt(t) == common]


def shared_video(a, b):
    """How much of title a plays the same pieces of video as b (0..1, weighted by size), and
    where a's own pieces sit: 'start', 'end' or 'middle'. DVD pieces are cell sector ranges,
    Blu-ray pieces are clip in/out times."""
    sa, sb = list(a.get("segments") or ()), set(b.get("segments") or ())
    size = lambda seg: (seg[2] - seg[1] + 1) if len(seg) == 3 and isinstance(seg[1], int) else 1
    total = sum(size(x) for x in sa)
    if not total or not sb:
        return 0.0, None
    own = [i for i, x in enumerate(sa) if x not in sb]
    share = 1 - sum(size(sa[i]) for i in own) / total
    if not own:
        return share, None
    where = "end" if min(own) >= len(sa) * 0.8 else "start" if max(own) < len(sa) * 0.2 else "middle"
    return share, where


def _in_order(t):
    """How much of a title plays its pieces in disc order (1.0 = straight through). Decoy titles
    reuse the real film's cells in a scrambled order; the real one runs them sequentially."""
    cells = [seg[1:] for seg in t.get("segments") or ()]
    if len(cells) < 2:
        return 0.5
    return sum(1 for a, b in zip(cells, cells[1:]) if b[0] == a[1] + 1) / (len(cells) - 1)


def dvd_decoys(features):
    """DVD title obfuscation (many 2009-2012 Disney / Sony / Lionsgate DVDs): dozens of
    feature-length titles with the same chapter count. Decoys reuse the film's cells in a
    scrambled order; the real film runs them in order and is often there several times over
    (title sets pointing at the same video). Keep one real copy; mark copies and decoys so
    nobody rips 40 titles. Returns what was found, or None."""
    from collections import Counter
    if len(features) < 6:
        return None
    chapters, n = Counter(t["chapters"] for t in features).most_common(1)[0]
    if n < 0.6 * len(features):
        return None
    num = lambda t: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", t["id"])]
    top = max(_in_order(t) for t in features)
    group = sorted((t for t in features if _in_order(t) >= top - 1e-9), key=num)   # the most in-order
    same_film = max(t["duration"] for t in group) - min(t["duration"] for t in group) <= 1.0
    sure = top >= 0.9 and same_film and len(group) < len(features)
    best = group[0]
    copies = 0
    for t in features:
        if t is best:
            continue
        if sure and t in group:
            t["role"], t["note"] = "duplicate", "another copy of the film (%s)" % best["id"]
            copies += 1
        else:
            t["role"], t["note"] = "decoy", "decoy title (copy protection)"
    best["role"] = "feature"
    best["note"] = ("the real film: plays in order, unlike the %d decoys" % (len(features) - 1 - copies)
                    if sure else "picked from %d look-alike titles (couldn't tell which is real: check it)"
                    % len(features))
    return {"decoys": len(features) - 1 - copies, "copies": copies, "picked": best["id"], "sure": sure}


# --------------------------------------------------------------------------
# 4. disc metadata: label, provider, date, cover art
# --------------------------------------------------------------------------

def as_movie(titles):
    """The disc turned out to be a film: reread the structure that way. Blu-ray features are
    often several clips played back to back, which on their own look exactly like a TV
    disc's play-all of episodes. Returns False if nothing on the disc is film-length."""
    playable = [t for t in titles if t.get("role") not in ("junk", "loop", "duplicate") and t.get("segments")]
    if not playable:
        return False
    main = max(playable, key=lambda t: t["duration"])
    if main["duration"] < FEATURE_MIN * 0.75:
        return False
    whole = set(main["segments"])
    for t in titles:
        t.pop("episode_index", None)
        if t is main:
            continue
        if t.get("role") in ("junk", "loop", "duplicate"):
            continue
        if t.get("segments") and set(t["segments"]) <= whole:
            t["role"], t["note"] = "part", "part of the feature (%s)" % main["id"]
        elif abs(t["duration"] - main["duration"]) <= 5:
            t["role"], t["note"] = "version", "same length as %s" % main["id"]
        elif t.get("role") in ("episode", "feature", "extra"):
            t["role"], t["note"] = "extra", ""
    main["role"] = "feature"
    main["note"] = "the film" + (" (plays %s)" % ", ".join(t["id"] for t in titles if t.get("role") == "part")
                                 if any(t.get("role") == "part" for t in titles) else "")
    return True


def as_tv(titles):
    """The label says this is a season's disc (SMALLVILLE_SEASON_10_DISC_6) but the structure
    read it as a film, usually because one title is a double-length episode (a two-hour
    finale). Reread it as episodes: every title of at least 40% of the longest (and at least
    18 minutes) is an episode, shorter ones stay extras; play-alls and copies keep their role.
    Returns False if nothing on the disc looks like an episode."""
    keep = ("junk", "loop", "duplicate", "play-all", "decoy")
    cands = [t for t in titles if t.get("role") not in keep and not t.get("interactive")]
    if not cands:
        return False
    longest = max(t["duration"] for t in cands)
    eps = [t for t in cands if t["duration"] >= max(18 * 60, 0.4 * longest)]
    if not eps:
        return False
    for t in cands:
        if t in eps:
            t["role"], t["note"] = "episode", ""
            t.pop("edition", None)
        elif t.get("role") in ("feature", "version", "part"):
            t["role"], t["note"] = "extra", ""
    typical = sorted(t["duration"] for t in eps)[(len(eps) - 1) // 2]
    for t in eps:
        if t["duration"] >= 1.7 * typical:
            t["note"] = "double length: likely two episodes"
    return True


def as_tv_possible(titles):
    """Would as_tv find episodes here? (without changing anything)"""
    import copy
    return as_tv(copy.deepcopy(titles))


def number_episodes(titles):
    """Episode order: follow the play-all when there is one (file numbers can be interleaved)."""
    for t in titles:
        t.pop("episode_index", None)
    eps_sorted = sorted((t for t in titles if t["role"] == "episode"),
                        key=lambda t: (t.get("play_order") or ("~", 0), _natural(t["id"])))
    for n, t in enumerate(eps_sorted, 1):
        t["episode_index"] = n


def movie_facts(titles, facts):
    f = {k: v for k, v in facts.items() if k not in ("episode_minutes", "episode_count")}
    feats = [t["duration"] for t in titles if t.get("role") == "feature"]
    if feats:
        f["feature_minutes"] = round(max(feats) / 60)
    return f


def volume_label(root):
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(261)
            drive = os.path.splitdrive(os.path.abspath(root))[0] + "\\"
            if ctypes.windll.kernel32.GetVolumeInformationW(drive, buf, 261, None, None, None, None, 0):
                if os.path.abspath(root).rstrip("\\") == drive.rstrip("\\"):
                    return buf.value or None
        except Exception:
            pass
        return None
    real = os.path.realpath(root)
    if sys.platform == "darwin" and real.startswith("/Volumes/"):
        return real.split("/")[2]
    if shutil.which("findmnt"):
        try:
            src = subprocess.run(["findmnt", "-no", "SOURCE", "--target", real],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            mnt = subprocess.run(["findmnt", "-no", "TARGET", "--target", real],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if src.startswith("/dev/") and mnt == real:
                out = subprocess.run(["lsblk", "-no", "LABEL", src], capture_output=True,
                                     text=True, timeout=5).stdout.strip()
                return out or None
        except Exception:
            pass
    return None


REGIONS = {"US", "USA", "UK", "CA", "CAN", "EUR", "EU", "AUS", "AU", "NZ", "JPN", "JP", "NA"}
NUMBER_WORDS = {w: i for i, w in enumerate(
    "ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE THIRTEEN FOURTEEN FIFTEEN "
    "SIXTEEN SEVENTEEN EIGHTEEN NINETEEN TWENTY".split(), 1)}

NOISE = {"DVD", "DVD9", "DVD5", "BD", "BLURAY", "BLU", "RAY", "UHD", "4K", "HD", "WS", "FS",
         "WIDESCREEN", "FULLSCREEN", "FULL", "SCREEN", "NTSC", "PAL", "R1", "R2", "R4", "AC3", "DTS",
         "SE", "CE", "SPECIAL", "COLLECTORS", "EDITION", "ED", "DISC", "DISK", "SIDE", "VIDEO",
         "US", "USA", "UK", "CA", "ENG", "EN", "MOVIE", "FEATURE", "COMPLETE", "SERIES", "SEASON",
         "FILM", "TV", "VOL", "VOLUME", "PART", "DIGITAL", "COPY", "MAIN"}


def parse_label(label):
    """Split a volume label into a search query plus season / disc hints."""
    if not label:
        return None
    raw = label.strip()
    s = re.sub(r"[_.\-:;,!/|()\[\]]+", " ", raw.upper())
    s = re.sub(r"([A-Z])(\d)", r"\1 \2", s) if re.search(r"\b(?:S|D|SEASON|SERIES|DISC|DISK|BD|DVD|DIM)\d", s) else s
    info = {"raw": raw, "season": None, "disc": None, "side": None, "year": None}
    # SEASON_TWO, SERIES_THREE: spelled-out numbers after a season word
    s = re.sub(r"\b(SEASON|SERIES)\s+(%s)\b" % "|".join(NUMBER_WORDS),
               lambda m: "%s %d" % (m.group(1), NUMBER_WORDS[m.group(2)]), s)
    # "Series" is the British "season" on a TV disc: one with a disc number, or with the show's
    # name before it (SHERLOCK_SERIES_3). At the start with no disc it's a title (SERIES_7_THE_CONTENDERS).
    has_disc = re.search(r"\b(?:D|DISC|DISK|BD|DVD|DIM)\s*\d", s)
    m = re.search(r"\bSERIES\s*\d", s)
    series_is_title = bool(m and not has_disc and not s[:m.start()].strip())
    pats = [
        (r"\bS(?:EASON)?\s*(\d{1,2})\s*D(?:ISC|ISK)?\s*(\d{1,2})\b", ("season", "disc")),
        (r"\bSEASON\s*(\d{1,2})\b" if series_is_title else r"\b(?:SEASON|SERIES)\s*(\d{1,2})\b",
         ("season",)),                                            # British discs: SERIES_3
        (r"\bSE?\s*(\d{1,2})\b(?=\s|$)", ("season",)),
        # PUSHING_DAISIES_S1_BD01_DIM2 is disc 2: every disc of that set says BD01, DIM counts
        (r"\bDIM\s*(\d{1,2})\b", ("disc",)),
        (r"\b(?:D(?:ISC|ISK|VD)?|BD)\s*(\d{1,2})\b", ("disc",)),     # D1, DISC_1, DVD3, BD01
        (r"\bSIDE\s*([AB12])\b", ("side",)),
    ]
    first_mark = None
    for pat, keys in pats:
        m = re.search(pat, s)
        if m:
            for k, g in zip(keys, m.groups()):
                if info[k] is None:
                    info[k] = g if k == "side" else int(g)
            if keys != ("side",):
                first_mark = m.start() if first_mark is None else min(first_mark, m.start())
            s = s[:m.start()] + " " * (m.end() - m.start()) + s[m.end():]
    # PUSHING_DAISIES_S1_BD01_DIM1: the title comes before the season / disc marks; what
    # follows them is authoring codes
    if first_mark is not None and s[:first_mark].strip() and s[first_mark:].strip():
        s = s[:first_mark]
    m = re.search(r"\b(19[2-9]\d|20[0-4]\d)\b", s)
    if m and s[:m.start()].strip():       # a leading number is part of the title (2001, 1917)
        info["year"] = int(m.group(1))
        s = s.replace(m.group(1), " ")
    run_together = not re.search(r"[\s_.\-:]", raw)
    if run_together:
        # GONE60SECONDS -> GONE 60 SECONDS, SHREK2 -> SHREK 2. Noise words are only
        # meaningful as separate tokens, so don't filter fragments of one word (SE7EN).
        s = re.sub(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])", " ", s)
        words = s.split()
        info["alt_query"] = raw.capitalize() if re.search(r"\d", raw) else None
    else:
        words = s.split()
        # POCAHONTAS_USA_DES, TITLE_UK_SE: after a region code comes release / edition codes
        cut = next((i for i, w in enumerate(words) if i >= 1 and w in REGIONS), None)
        if cut is not None:
            words = words[:cut]
        words = [w for i, w in enumerate(words) if w not in NOISE
                 or (w == "SERIES" and i + 1 < len(words)
                     and (words[i + 1] == "OF" or series_is_title and words[i + 1].isdigit()))]
    # drop a trailing authoring/production code (PRINCE_OF_EGYPT_UPB2) when a real title precedes it
    if len(words) >= 3 and re.fullmatch(r"(?=.*[A-Z])(?=.*\d)[A-Z0-9]{3,6}", words[-1]):
        words = words[:-1]
    # SISTER_ACT2: a number stuck to the last word is a sequel number (Sister Act 2)
    if not run_together and len(words) >= 2:
        m = re.fullmatch(r"([A-Z]{2,})(\d{1,2})", words[-1])
        if m:
            words = words[:-1] + [m.group(1), m.group(2)]
    info["query"] = " ".join(w.capitalize() for w in words)
    # one more try without a short trailing code (..._DES): searched only alongside the full query
    if not run_together and len(words) >= 2 and re.fullmatch(r"[A-Z]{2,3}", words[-1]) \
            and words[-1] not in ROMAN:
        info["short_query"] = " ".join(w.capitalize() for w in words[:-1])
    # Old DVD labels are often cut to 8-11 characters with no separators.
    info["maybe_truncated"] = bool(words) and len(raw) <= 11 and "_" not in raw and " " not in raw
    return info


def mastering_date(fp):
    if not fp.get("newest_mtime"):
        return None
    d = _dt.datetime.fromtimestamp(fp["newest_mtime"], _dt.timezone.utc)
    return d.strftime("%Y-%m") if 1996 <= d.year <= _dt.datetime.now().year else None


def extract_cover(root, fmt, outdir, bd_meta=None):
    """Save embedded cover art. DVD JACKET_P stills need ffmpeg (MPEG program stream)."""
    os.makedirs(outdir, exist_ok=True)
    if fmt == "DVD":
        jp = find_child(root, "JACKET_P")
        if not jp:
            return None
        cands = sorted(glob.glob(os.path.join(jp, "*.MP2")) + glob.glob(os.path.join(jp, "*.mp2")),
                       key=os.path.getsize, reverse=True)
        if not cands or not shutil.which("ffmpeg"):
            return {"found": cands[0] if cands else None, "image": None,
                    "note": "install ffmpeg to decode" if cands else ""}
        out = os.path.join(outdir, "cover.png")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "mpeg", "-i", cands[0],
                        "-frames:v", "1", out], capture_output=True, timeout=30)
        return {"found": cands[0], "image": out if os.path.exists(out) else None, "note": ""}
    thumbs = (bd_meta or {}).get("thumbnails") or []
    thumbs = [t for t in thumbs if os.path.isfile(t)]
    if not thumbs:
        return None
    best = max(thumbs, key=os.path.getsize)
    out = os.path.join(outdir, "cover" + os.path.splitext(best)[1].lower())
    if os.path.exists(out):
        os.chmod(out, stat.S_IWRITE | stat.S_IREAD)
    shutil.copyfile(best, out)
    return {"found": best, "image": out, "note": ""}


def ocr(image):
    """Classic OCR of cover art. Only confident, word-like tokens are kept: stylized
    logo lettering usually defeats tesseract, and junk text makes bad search terms."""
    if not image or not shutil.which("tesseract"):
        return None
    words = []
    for psm in ("11", "3"):
        try:
            tsv = subprocess.run(["tesseract", image, "stdout", "--psm", psm, "tsv"],
                                 capture_output=True, text=True, timeout=60).stdout
        except Exception:
            return None
        for line in tsv.splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) < 12:
                continue
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            w = cols[11].strip(" .,:;!?\"'()[]")
            if conf >= 80 and len(w) >= 3 and w.isalpha() and re.search(r"[aeiouyAEIOUY]", w):
                if w.lower() not in (x.lower() for x in words):
                    words.append(w)
    return " ".join(words) if len(words) >= 2 else None


# --------------------------------------------------------------------------
# 5a. IMDb (default source): official non-commercial datasets, local index
# --------------------------------------------------------------------------
# IMDb publishes daily TSV dumps for personal, non-commercial use. Each install
# downloads its own copy (nothing is redistributed), then every lookup is local,
# instant and needs no API key. IMDb ids are what Plex uses ({imdb-tt...}).

IMDB_BASE = "https://datasets.imdbws.com/"
IMDB_FILES = ("title.basics.tsv.gz", "title.episode.tsv.gz", "title.ratings.tsv.gz")
IMDB_DB = "imdb_index.sqlite"
IMDB_MAX_AGE_DAYS = 30
IMDB_MOVIE_TYPES = ("movie", "tvMovie", "video")
IMDB_TV_TYPES = ("tvSeries", "tvMiniSeries")
IMDB_SHORT_TYPES = ("short", "tvShort")
IMDB_KEEP_TYPES = IMDB_MOVIE_TYPES + IMDB_TV_TYPES + ("tvSpecial",)
IMDB_SHORT_MIN_RUNTIME = 5      # shorts are indexed for short-film collections; skip clips
IMDB_SHORT_MIN_VOTES = 20       # ... and the obscure ones (there are about a million shorts)
IMDB_INDEX_VERSION = 2          # bump to rebuild existing indexes when the schema changes


def default_imdb_dir():
    return env("IMDB") or os.path.join(data_home(), "imdb")


def norm_title(s):
    s = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return s[3:] if s.startswith("the") and len(s) > 6 else s


def tt(n):
    return "tt%07d" % n


def imdb_download(imdb_dir, force=False, log=print):
    os.makedirs(imdb_dir, exist_ok=True)
    fresh = True
    for name in IMDB_FILES:
        path = os.path.join(imdb_dir, name)
        age = (_dt.datetime.now().timestamp() - os.path.getmtime(path)) / 86400 if os.path.exists(path) else None
        if age is not None and age < IMDB_MAX_AGE_DAYS and not force:
            continue
        fresh = False
        log("downloading IMDb dataset %s ..." % name)
        req = urllib.request.Request(IMDB_BASE + name, headers={"User-Agent": USER_AGENT})
        tmp = path + ".part"
        with urllib.request.urlopen(req, timeout=60) as r:
            _stream(r, tmp, "IMDb " + name.split(".")[1])
        os.replace(tmp, path)
    return fresh


def imdb_index_info(imdb_dir):
    """For settings screens: is the IMDb index built, and when."""
    import sqlite3
    path = os.path.join(imdb_dir, IMDB_DB)
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path)
        titles = con.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
        version = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()
    except sqlite3.Error:
        return None
    return {"built": _dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d"),
            "titles": titles, "current": version >= IMDB_INDEX_VERSION, "path": path}


def update_data(args, which=("discdb", "imdb"), force=False, log=print):
    """Download/refresh the disc data now (first-run screens, Settings > Update now)."""
    done = {}
    if "discdb" in which:
        discdb_index_download(getattr(args, "discdb_dir", None) or default_discdb_dir(), force=force, log=log)
        done["discdb"] = discdb_index_info(getattr(args, "discdb_dir", None) or default_discdb_dir())
    if "imdb" in which:
        con = imdb_open(args.imdb_dir, allow_download=True, force=force, log=log)
        if con is not None:
            con.close()
        done["imdb"] = imdb_index_info(args.imdb_dir)
    return done


def _tsv(path):
    import gzip
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        header = fh.readline().rstrip("\r\n").split("\t")      # tolerate \r\n (a copy saved on Windows)
        for line in fh:
            yield dict(zip(header, line.rstrip("\r\n").split("\t")))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def imdb_build(imdb_dir, log=print):
    """Build the local SQLite index from the TSV dumps (one-time, a few minutes)."""
    import sqlite3
    db_path = os.path.join(imdb_dir, IMDB_DB)
    tmp = db_path + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE titles (id INTEGER PRIMARY KEY, type TEXT, title TEXT, year INT, end_year INT,
                             runtime INT, norm TEXT, alt TEXT);
        CREATE TABLE ep_titles (id INTEGER PRIMARY KEY, title TEXT, runtime INT, year INT);
        CREATE TABLE ep_map (id INTEGER PRIMARY KEY, parent INT, season INT, episode INT);
        CREATE TABLE ratings (id INTEGER PRIMARY KEY, votes INT);
    """)
    log("indexing IMDb titles (one-time, takes a few minutes) ...")
    keep, eps = [], []
    for row in _tsv(os.path.join(imdb_dir, "title.basics.tsv.gz")):
        ttype = row.get("titleType")
        if row.get("isAdult") == "1":
            continue
        n = _int(row["tconst"][2:])
        if ttype == "tvEpisode":
            eps.append((n, row["primaryTitle"], _int(row["runtimeMinutes"]), _int(row["startYear"])))
        elif ttype in IMDB_KEEP_TYPES or (ttype in IMDB_SHORT_TYPES and
                                          (_int(row["runtimeMinutes"]) or 0) >= IMDB_SHORT_MIN_RUNTIME):
            # originalTitle too: "Wallace & Gromit: The Wrong Trousers" is "The Wrong Trousers" originally
            alt = row.get("originalTitle") if row.get("originalTitle") != row["primaryTitle"] else None
            keep.append((n, ttype, row["primaryTitle"], _int(row["startYear"]), _int(row["endYear"]),
                         _int(row["runtimeMinutes"]), norm_title(row["primaryTitle"]), alt))
        if len(keep) >= 50000:
            con.executemany("INSERT INTO titles VALUES (?,?,?,?,?,?,?,?)", keep); keep = []
        if len(eps) >= 50000:
            con.executemany("INSERT INTO ep_titles VALUES (?,?,?,?)", eps); eps = []
    con.executemany("INSERT INTO titles VALUES (?,?,?,?,?,?,?,?)", keep)
    con.executemany("INSERT INTO ep_titles VALUES (?,?,?,?)", eps)
    batch = []
    for row in _tsv(os.path.join(imdb_dir, "title.episode.tsv.gz")):
        batch.append((_int(row["tconst"][2:]), _int(row["parentTconst"][2:]),
                      _int(row["seasonNumber"]), _int(row["episodeNumber"])))
        if len(batch) >= 50000:
            con.executemany("INSERT INTO ep_map VALUES (?,?,?,?)", batch); batch = []
    con.executemany("INSERT INTO ep_map VALUES (?,?,?,?)", batch)
    batch = []
    for row in _tsv(os.path.join(imdb_dir, "title.ratings.tsv.gz")):
        batch.append((_int(row["tconst"][2:]), _int(row["numVotes"])))
        if len(batch) >= 50000:
            con.executemany("INSERT INTO ratings VALUES (?,?)", batch); batch = []
    con.executemany("INSERT INTO ratings VALUES (?,?)", batch)
    con.execute("DELETE FROM titles WHERE type IN (%s) AND COALESCE((SELECT votes FROM ratings r "
                "WHERE r.id = titles.id), 0) < ?" % ",".join("?" * len(IMDB_SHORT_TYPES)),
                (*IMDB_SHORT_TYPES, IMDB_SHORT_MIN_VOTES))
    con.executescript("""
        CREATE TABLE episodes AS
            SELECT m.parent AS parent, m.season AS season, m.episode AS episode, m.id AS id,
                   e.title AS title, e.runtime AS runtime, e.year AS year
            FROM ep_map m JOIN ep_titles e ON e.id = m.id
            WHERE m.parent IN (SELECT id FROM titles);
        DROP TABLE ep_map; DROP TABLE ep_titles;
        CREATE INDEX ix_ep ON episodes(parent, season, episode);
        CREATE INDEX ix_norm ON titles(norm);
    """)
    try:
        con.executescript("""
            CREATE VIRTUAL TABLE titles_fts USING fts5(title, alt, content='titles', content_rowid='id');
            INSERT INTO titles_fts(rowid, title, alt) SELECT id, title, alt FROM titles;
        """)
    except sqlite3.OperationalError:
        log("note: this Python's SQLite lacks FTS5; word search falls back to prefix matching")
    con.execute("PRAGMA user_version = %d" % IMDB_INDEX_VERSION)
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, db_path)
    return db_path


def imdb_open(imdb_dir, allow_download=True, force=False, log=print):
    """Return a sqlite connection to the local IMDb index, downloading/building if needed."""
    import sqlite3
    db_path = os.path.join(imdb_dir, IMDB_DB)
    have_files = all(os.path.exists(os.path.join(imdb_dir, f)) for f in IMDB_FILES)
    if allow_download:
        imdb_download(imdb_dir, force=force, log=log)
        have_files = True
    if not os.path.exists(db_path) and not have_files:
        return None
    newest = max((os.path.getmtime(os.path.join(imdb_dir, f)) for f in IMDB_FILES
                  if os.path.exists(os.path.join(imdb_dir, f))), default=0)
    stale = False
    if os.path.exists(db_path):
        c = sqlite3.connect(db_path)
        stale = c.execute("PRAGMA user_version").fetchone()[0] < IMDB_INDEX_VERSION
        c.close()
        if stale and not have_files:
            log("note: IMDb index is from an older version; run once without --offline to rebuild it")
            stale = False
        elif stale:
            log("IMDb index is from an older version; rebuilding ...")
    if not os.path.exists(db_path) or stale or (have_files and os.path.getmtime(db_path) < newest):
        imdb_build(imdb_dir, log=log)
    return sqlite3.connect(db_path)


def _fts_query(q, prefix_last):
    toks = re.findall(r"[a-z0-9]+", q.lower())
    toks = [t for t in toks if t not in ("the", "a", "an", "of", "and", "in")] or toks
    if not toks:
        return None
    parts = ['"%s"' % t for t in toks]
    if prefix_last:
        parts[-1] += "*"
    return " AND ".join(parts)


def imdb_search(con, query, truncated, types, limit=40):
    """Candidate rows: exact normalized title, prefix (truncated labels), and word match."""
    n = norm_title(query)
    qmarks = ",".join("?" * len(types))
    base = ("SELECT t.id, t.type, t.title, t.year, t.end_year, t.runtime, COALESCE(r.votes,0) "
            "FROM titles t LEFT JOIN ratings r ON r.id = t.id WHERE t.type IN (%s) " % qmarks)
    rows = {}
    if n:
        for r in con.execute(base + "AND t.norm = ? ORDER BY 7 DESC LIMIT ?", (*types, n, limit)):
            rows[r[0]] = r
        if truncated or len(n) >= 6:
            for r in con.execute(base + "AND t.norm >= ? AND t.norm < ? ORDER BY 7 DESC LIMIT ?",
                                 (*types, n, n + "{", limit)):
                rows.setdefault(r[0], r)
    fq = _fts_query(query, prefix_last=True)
    if fq:
        try:
            sql = ("SELECT t.id, t.type, t.title, t.year, t.end_year, t.runtime, COALESCE(r.votes,0) "
                   "FROM titles_fts f JOIN titles t ON t.id = f.rowid LEFT JOIN ratings r ON r.id = t.id "
                   "WHERE titles_fts MATCH ? AND t.type IN (%s) ORDER BY 7 DESC LIMIT ?" % qmarks)
            for r in con.execute(sql, (fq, *types, limit)):
                rows.setdefault(r[0], r)
        except Exception:
            pass   # no FTS5 in this SQLite build
    return list(rows.values())


def imdb_candidates(con, queries, kind, facts):
    """Rank IMDb titles with the same explainable signals as TMDB, plus popularity."""
    import math
    types = IMDB_TV_TYPES if kind == "tv" else IMDB_MOVIE_TYPES if kind == "movie" else IMDB_KEEP_TYPES
    master_year = int(facts["mastered"][:4]) if facts.get("mastered") else None
    best = {}
    for query, truncated in queries:
        for (n, ttype, title, year, end_year, runtime, votes) in imdb_search(con, query, truncated, types):
            ns = name_score(query, title, truncated)
            if ns < 0.6:
                continue
            score = ns * 50
            why = ["name %.0f%%" % (ns * 100)]
            if master_year and year:
                if year <= master_year:
                    score += 10
                else:
                    score -= 40
                    why.append("released after the disc was mastered")
            target = facts.get("episode_minutes") if ttype in IMDB_TV_TYPES else facts.get("feature_minutes")
            if target and runtime:
                diff = abs(runtime - target)
                score += max(0, 30 - diff * 6) if ttype not in IMDB_TV_TYPES else max(0, 20 - diff * 3)
                why.append("runtime %d vs disc %d min" % (runtime, target))
            if facts.get("truth_year") and year:
                score += 40 if year == facts["truth_year"] else -40
            pop = min(15, 3 * math.log10(votes + 1))
            score += pop
            if votes:
                why.append("%s votes" % format(votes, ","))
            cand = {"kind": "tv" if ttype in IMDB_TV_TYPES else "movie", "imdb_id": tt(n), "_n": n,
                    "name": title, "year": year, "type": ttype, "runtime": runtime, "score": round(score, 1),
                    "why": "; ".join(why), "source": "IMDb", "name_score": ns, "votes": votes}
            if n not in best or best[n]["score"] < cand["score"]:
                best[n] = cand
    # Sequel numbers: AN_AMERICAN_TAIL_2 is "An American Tail: Fievel Goes West". Order the
    # franchise by release year and favour the Nth film; runtime and year still have to agree.
    for query, truncated in queries:
        m = re.match(r"^(.*?\S)\s+(\d|II|III|IV|V|VI|VII|VIII|IX)$", (query or "").strip(), re.I)
        if not m or kind == "tv":
            continue
        base, num = m.group(1), m.group(2).upper()
        n = int(num) if num.isdigit() else ROMAN.get(num)
        if not n or n < 2:
            continue
        words = lambda x: re.findall(r"[a-z0-9]+", x.lower().replace("&", "and"))
        bw = words(base)
        pool = imdb_search(con, base, True, IMDB_MOVIE_TYPES, limit=60)
        # A title that carries the number ("Pocahontas 2: Journey to a New World", "Rocky II")
        # beats counting look-alikes by year (several unrelated "Pocahontas" films came out in 1995).
        marks = {str(n), {v: k for k, v in ROMAN.items()}.get(n, "").lower()} - {""}
        numbered = [r for r in pool if words(r[2])[:len(bw)] == bw and len(words(r[2])) > len(bw)
                    and words(r[2])[len(bw)] in marks]
        if numbered:
            pick = max(numbered, key=lambda r: r[6])
            why = ["titled \"%s %s\u2026\"" % (base, n)]
            score = 50 + 25
        else:
            family = [r for r in pool if words(r[2])[:len(bw)] == bw and r[3] and r[6] >= 100]
            family.sort(key=lambda r: (r[3], -r[6]))
            if len(family) < n:
                continue
            pick = family[n - 1]
            why = ["film #%d of %d titled \"%s\u2026\" by release year" % (n, len(family), base)]
            score = 0.85 * 50 + 25
        (fid, ttype, title, year, _, runtime, votes) = pick
        if master_year and year:
            score += 10 if year <= master_year else -40
        target = facts.get("feature_minutes")
        if target and runtime:
            score += max(0, 30 - abs(runtime - target) * 6)
            why.append("runtime %d vs disc %d min" % (runtime, target))
        score += min(15, 3 * math.log10(votes + 1))
        why.append("%s votes" % format(votes, ","))
        cand = {"kind": "movie", "imdb_id": tt(fid), "_n": fid, "name": title, "year": year, "type": ttype,
                "score": round(score, 1), "why": "; ".join(why), "source": "IMDb"}
        if fid not in best or best[fid]["score"] < cand["score"]:
            best[fid] = cand
    # Several titles of exactly the same name (a film and an obscure namesake): the far
    # better-known one is almost always meant.
    votes = {n: (con.execute("SELECT votes FROM ratings WHERE id=?", (n,)).fetchone() or [0])[0] for n in best}
    groups = {}
    for n, c in best.items():
        groups.setdefault(re.sub(r"[^a-z0-9]", "", c["name"].lower()), []).append(n)
    for ns in groups.values():
        if len(ns) < 2:
            continue
        ns.sort(key=lambda n: -votes[n])
        if votes[ns[0]] >= 1000 and votes[ns[0]] >= 20 * max(votes[ns[1]], 1):
            c = best[ns[0]]
            c["score"] = round(c["score"] + 10, 1)
            c["why"] += "; far better known than other titles of that name"
    out = sorted(best.values(), key=lambda c: -c["score"])[:5]
    return out


ROMAN = {"II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9}


def _title_num(t):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", t["id"])]


def collection_name(query):
    """"Wallace And Gromit" -> "Wallace & Gromit Collection" (the plan already says how many films)."""
    name = re.sub(r"\bAnd\b", "&", (query or "").strip())
    return name if re.search(r"\bcollection\b", name, re.I) else "%s Collection" % name


def imdb_collection(con, queries, titles, facts):
    """A disc of separate short films (Wallace & Gromit, Pixar shorts, cartoon collections) looks
    like a TV disc: several 20-35 minute titles. When the label names a franchise with short
    films on IMDb, pair each title with a short: release order follows disc order, runtimes must
    agree. Returns one candidate whose items name every title, or None."""
    words = lambda x: set(re.findall(r"[a-z0-9]+", (x or "").lower().replace("&", " and ")))
    stop = {"the", "a", "an", "of", "and", "in", "collection", "shorts", "short", "films", "adventures"}
    allp = sorted((t for t in titles if t.get("role") in ("episode", "version")), key=_title_num)
    # a second copy of a film (same length to the second) must not use up a film of its own
    pieces, twins = [], []
    for t in allp:
        twin = next((p for p in pieces if abs(p["duration"] - t["duration"]) <= 1.5), None)
        (twins.append((t, twin)) if twin else pieces.append(t))
    if len(pieces) < 2:
        return None
    master_year = int(facts["mastered"][:4]) if facts.get("mastered") else None
    best = None
    for query, _ in queries:
        need = words(query) - stop
        if not need:
            continue
        rows = {}
        for r in imdb_search(con, " ".join(sorted(need, key=query.lower().find)), False,
                             IMDB_SHORT_TYPES + IMDB_MOVIE_TYPES, limit=200):
            (n, ttype, title, year, _, runtime, votes) = r
            alt = con.execute("SELECT alt FROM titles WHERE id=?", (n,)).fetchone()[0]
            if (ttype in IMDB_SHORT_TYPES or (runtime or 99) <= 45) and runtime and year \
                    and need <= (words(title) | words(alt)) and votes >= IMDB_SHORT_MIN_VOTES \
                    and (not master_year or year <= master_year):
                rows[n] = r
        shorts = sorted(rows.values(), key=lambda r: (r[3], -r[6]))
        if len(shorts) < 2:
            continue
        fits = lambda t, r: abs(t["duration"] / 60 - r[5]) <= max(3, 0.15 * r[5])
        # longest order-preserving pairing (disc order vs release order), closest runtimes on ties
        P, S = len(pieces), len(shorts)
        dp = [[(0, 0.0)] * (S + 1) for _ in range(P + 1)]
        for i in range(P - 1, -1, -1):
            for j in range(S - 1, -1, -1):
                opts = [dp[i + 1][j], dp[i][j + 1]]
                if fits(pieces[i], shorts[j]):
                    m, d = dp[i + 1][j + 1]
                    opts.append((m + 1, d - abs(pieces[i]["duration"] / 60 - shorts[j][5])))
                dp[i][j] = max(opts)
        i = j = 0
        pairs = []
        while i < P and j < S:
            if fits(pieces[i], shorts[j]) and dp[i][j] == (dp[i + 1][j + 1][0] + 1, dp[i + 1][j + 1][1]
                                                           - abs(pieces[i]["duration"] / 60 - shorts[j][5])):
                pairs.append((pieces[i], shorts[j])); i += 1; j += 1
            elif dp[i][j] == dp[i + 1][j]:
                i += 1
            else:
                j += 1
        if len(pairs) < 2 or (best and len(pairs) <= len(best["items"])):
            continue
        matched = {t["id"] for t, _ in pairs}
        items = [{"title_id": t["id"], "imdb_id": tt(r[0]), "name": r[2], "year": r[3], "runtime": r[5],
                  "disc_minutes": round(t["duration"] / 60, 1)} for t, r in pairs]
        versions = [{"title_id": t["id"], "same_as": tw["id"]} for t, tw in twins if tw["id"] in matched]
        unmatched = [t["id"] for t in pieces if t["id"] not in matched] + \
                    [t["id"] for t, tw in twins if tw["id"] not in matched]
        same_rt = len({it["runtime"] for it in items}) < len(items)
        why = ["%d of %d episode-length titles pair with \"%s\" short films in release order" %
               (len(pairs), len(allp), query)]
        if same_rt:
            why.append("some films share a runtime, so their order is assumed from the disc")
        if versions:
            why.append("%s: same length as another title (second version?)" %
                       ", ".join(v["title_id"] for v in versions))
        score = 60 + 10 * len(pairs) - 10 * len(unmatched)
        best = {"kind": "collection", "imdb_id": None, "name": collection_name(query),
                "year": min(it["year"] for it in items), "type": "collection", "score": round(score, 1),
                "why": "; ".join(why), "source": "IMDb", "items": items, "versions": versions,
                "unmatched": unmatched}
    return best


def _initials(title):
    w = re.findall(r"[A-Za-z0-9']+", title or "")
    if w and w[0].lower() in ("the", "a", "an"):
        w = w[1:]
    return "".join(x[0].upper() for x in w)


def _one_word(title):
    """A one-word title (leading article dropped) in upper case, else '' (for TZN = TarZaN)."""
    t = re.sub(r"^(the|a|an)\s+", "", (title or "").strip(), flags=re.I)
    return t.upper() if re.fullmatch(r"[A-Za-z]+", t) else ""


def imdb_initials(con, label, facts):
    """Last resort for code-like labels (BCS0NNW1): studios often start them with the title's
    initials (B C = The Black Cauldron). Sometimes the code skips the first word (NGE0NNW1 =
    The Emperor's New Groove), so initials starting at the second word count too, ranked below
    titles that start with them. Films with the feature's length (+-4 min) released by the
    mastering date, best known first. Only a clear favourite becomes a guess."""
    import math
    raw = re.sub(r"[^A-Za-z0-9]", "", label or "")
    run = re.match(r"[A-Za-z]{2,}", raw)
    minutes = facts.get("feature_minutes")
    if not run or not re.search(r"\d", raw) or not minutes:
        return []
    # only real codes: a short letter run, no word breaks (SISTER_ACT2 is words, not initials)
    if len(run.group(0)) > 4 or re.search(r"[\s_.\-]", label or ""):
        return []
    master_year = int(facts["mastered"][:4]) if facts.get("mastered") else 9999
    rows = con.execute(
        "SELECT t.id, t.type, t.title, t.year, t.runtime, COALESCE(r.votes, 0) FROM titles t "
        "LEFT JOIN ratings r ON r.id = t.id WHERE t.type IN ('movie', 'video', 'tvMovie') "
        "AND t.runtime BETWEEN ? AND ? AND t.year <= ? AND COALESCE(r.votes, 0) >= 500",
        (minutes - 4, minutes + 4, master_year)).fetchall()
    letters = run.group(0).upper()

    def scored(hits, want, tier):
        hits.sort(key=lambda r: -r[5])
        out = []
        for i, (n, ttype, title, year, runtime, votes) in enumerate(hits[:5]):
            nxt = hits[i + 1][5] if i + 1 < len(hits) else 0
            clear = i == 0 and votes >= 5000 and votes >= 3 * max(nxt, 1)
            score = (30 + min(15, 3 * math.log10(votes + 1)) + (10 if clear else 0)
                     + 2 * (len(want) - 2) + (6 if tier == 0 else 0))
            where = {0: "starts with the initials", 1: "has, after its first word, the initials",
                     2: "spells out the letters"}[tier]
            why = "label %s: %s %s %s; %d min like the disc; %s votes%s" % (
                label, title, where, " ".join(want), runtime, format(votes, ","),
                " (%.0fx the next)" % (votes / max(nxt, 1)) if clear and nxt else "")
            out.append({"kind": "movie", "imdb_id": tt(n), "_n": n, "name": title, "year": year,
                        "type": ttype, "runtime": runtime, "score": round(score, 1), "why": why,
                        "source": "IMDb"})
        return out

    out = []
    for k in range(len(letters), 1, -1):           # most specific first: BCS, then BC
        want = letters[:k]
        tiers = {0: [], 1: []}                     # 0: title starts with them, 1: from its 2nd word
        for r in rows:
            ini = _initials(r[2])
            if ini.startswith(want):
                tiers[0].append(r)
            elif ini[1:].startswith(want):
                tiers[1].append(r)
        if k == len(letters) >= 3 and not tiers[0] and not tiers[1]:
            # no title has all these initials. One-word titles get shortened to their consonants
            # instead (TZN0NNW1 = TarZaN: the word's first and last letters, some in between).
            # Those compete with the shorter initials below (NG = Emperor's New Groove still
            # beats a little-known film called Nightmare); the better-known film wins.
            skel = re.compile("^" + ".*".join(want) + "$")
            out += scored([r for r in rows if _one_word(r[2]) and skel.match(_one_word(r[2]))],
                          want, 2)
        if tiers[0] or tiers[1]:
            for tier, hits in tiers.items():
                out += scored(hits, want, tier)
            break
    out.sort(key=lambda c: -c["score"])
    return out[:5]


def imdb_season(con, cand, season, disc, n_eps, lengths=None, fmt=None, hints=None, before=None):
    """Episode guess from IMDb's list (aired order). With the disc's episode lengths it is
    placed the same way as TVmaze's (double-length titles, a shorter last disc)."""
    rows = con.execute("SELECT episode, title, id FROM episodes WHERE parent=? AND season=? "
                       "ORDER BY episode", (cand["_n"], season)).fetchall()
    rts = dict(con.execute("SELECT episode, runtime FROM episodes WHERE parent=? AND season=?",
                           (cand["_n"], season)).fetchall())
    rows = [r for r in rows if r[0] is not None]
    # an episode 0 inside a season is an unaired pilot or special (Smallville S1E00), not the
    # first episode on disc 1; keep it only when the disc holds exactly the whole season with it
    if any(r[0] == 0 for r in rows) and not (len(rows) == n_eps and disc in (None, 1)):
        rows = [r for r in rows if r[0] != 0]
    info = {"episodes_in_season": len(rows)}
    if not rows or not n_eps:
        return info
    if lengths and len(lengths) == n_eps and not (len(rows) == n_eps and disc in (None, 1)):
        seq = [{"season": season, "number": e, "name": t, "runtime": rts.get(e)} for e, t, _ in rows]
        g = tvmaze_season_guess({"aired": seq, "dvd": None}, season, disc or 1, lengths, "aired", fmt, hints, before)
        if g:
            g["source"] = "IMDb aired order"
            g["note"] = g["note"].replace("TVmaze aired order, named in aired order", "IMDb aired order")
            return g
    if len(rows) == n_eps and (disc in (None, 1)):
        chosen, info["note"] = rows, "disc holds the complete season (%d episodes)" % n_eps
    elif disc:
        start = (disc - 1) * n_eps
        chosen = rows[start:start + n_eps]
        info["note"] = "assumes %d episodes on every earlier disc; confirm by subtitle matching" % n_eps
    else:
        return info
    info["guess"] = ["S%02dE%02d %s" % (season, e, t) for e, t, _ in chosen]
    info["ids"] = [tt(i) for _, _, i in chosen]
    return info


def season_from_dates(first_year, mastered):
    """The season a label-less disc must hold, from {season: year its first episode aired}:
    the only season there is, or the only one out by the mastering date. (None, None) if
    that doesn't settle it."""
    if len(first_year) == 1:
        return next(iter(first_year)), "the show has only one season"
    master = int(mastered[:4]) if mastered else None
    out = [sn for sn, y in first_year.items() if master and y and y <= master]
    if len(out) == 1:
        return out[0], "the only season out when the disc was mastered (%s)" % mastered
    return None, None


def plex_names(cand, season=None, episode_guess=None, sources=None, editions=None):
    """Plex-style names with the IMDb id hint Plex's agents understand."""
    if not cand or not cand.get("imdb_id"):
        return None
    safe = lambda s: re.sub(r'[<>"/\\|?*]', "", re.sub(r"\s*:\s*", " - ", s or "")).strip()
    base = "%s (%s) {imdb-%s}" % (safe(cand["name"]), cand.get("year") or "", cand["imdb_id"])
    sources = list(sources or [])
    tag = lambda i: ("%-14s -> " % sources[i]) if i < len(sources) else ""
    if cand["kind"] == "movie":
        if editions:          # one folder, one file per edition: Plex shows them as versions of the film
            return [tag(i) + base + "/" + base.split(" {")[0] + " {edition-%s}.mkv" % safe(editions[src])
                    for i, src in enumerate(sources)]
        return [tag(0) + base + "/" + base.split(" {")[0] + ".mkv"]
    names = []
    for i, g in enumerate((episode_guess or {}).get("guess", [])):
        code, _, title = g.partition(" ")
        names.append(tag(i) + "%s/Season %02d/%s (%s) - %s - %s.mkv" % (
            base, season or 0, safe(cand["name"]), cand.get("year") or "", code.lower(), safe(title)))
    return names or [base + ("/Season %02d/" % season if season is not None else "/")]


# --------------------------------------------------------------------------
# 5a. Artwork: something to look at when confirming a match
# --------------------------------------------------------------------------
# Order: the disc's own art (this exact release), TheDiscDB box art / poster, TMDB poster
# (needs the optional key). IMDb's datasets have no images and its pages may not be scraped.
# Tk shows PNG/GIF only, so pictures are converted (Pillow if installed, else ffmpeg) and
# cached beside the library: a disc seen before shows its picture instantly, offline.

def default_art_dir(db_path=None):
    return os.path.join(os.path.dirname(os.path.abspath(db_path or default_library())), "art")


def image_converter():
    """What can turn a JPEG into something Tk shows: "Pillow", "ffmpeg" or None."""
    try:
        import PIL  # noqa: F401
        return "Pillow"
    except ImportError:
        return "ffmpeg" if shutil.which("ffmpeg") else None


def to_png(src, dest, box):
    """Scale an image to fit box=(w, h) and save as PNG. Returns dest, or None if no converter."""
    bw, bh = box
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                f = min(bw / im.width, bh / im.height)
                im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS).save(dest, "PNG")
            return dest
        except Exception:
            return None
    ff = shutil.which("ffmpeg")
    if ff:
        subprocess.run([ff, "-loglevel", "error", "-y", "-i", src, "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease" % (bw, bh),
                        "-frames:v", "1", dest], capture_output=True, timeout=30)
        return dest if os.path.exists(dest) and os.path.getsize(dest) else None
    return None


def _download(url, dest, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if len(data) < 500 or data[:1] not in (b"\xff", b"\x89", b"G", b"R"):   # jpeg/png/gif/webp
        raise ValueError("not an image")
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def _art_entry(art_dir, key, src, source, what, box, credit=None):
    """Convert src into the cache as <key>.png (+ sidecar json). None if it can't be shown."""
    png = os.path.join(art_dir, key + ".png")
    if not to_png(src, png, box):
        return None
    entry = {"image": png, "source": source, "what": what}
    if credit:
        entry["credit"] = credit
    with open(os.path.join(art_dir, key + ".json"), "w", encoding="utf-8") as fh:
        json.dump(entry, fh)
    return entry


def _art_cached(art_dir, key):
    png, side = os.path.join(art_dir, key + ".png"), os.path.join(art_dir, key + ".json")
    if os.path.exists(png) and os.path.exists(side):
        try:
            with open(side, encoding="utf-8") as fh:
                e = json.load(fh)
            e["image"] = png
            return e
        except (OSError, ValueError):
            pass
    return None


def _raw(path):
    return DISCDB_RAW + urllib.parse.quote(path, safe="/")


def poster_from_tmdb(poster_path, key_name, art_dir, box=ART_POSTER, errors=None):
    """A TMDB poster we already have the path of (from a TMDB search result)."""
    name = "%s_%dx%d" % (key_name, box[0], box[1])
    hit = _art_cached(art_dir, name)
    if hit or not poster_path:
        return hit
    try:
        orig = os.path.join(art_dir, key_name + ".jpg")
        if not os.path.exists(orig):
            _download(TMDB_IMG + poster_path, orig)
        return _art_entry(art_dir, name, orig, "TMDB", "poster", box, credit=TMDB_CREDIT)
    except Exception as exc:
        if errors is not None:
            errors.append("TMDB poster: %s" % exc)
    return None


def poster_for_imdb(imdb_id, art_dir, repo=None, key=None, online=True, box=ART_POSTER, errors=None, index=None):
    """A poster for an IMDb id: TheDiscDB's cover.jpg (local clone, or via the index), then TMDB."""
    if not imdb_id:
        return None
    name = "%s_%dx%d" % (imdb_id, box[0], box[1])
    hit = _art_cached(art_dir, name)
    if hit:
        return hit
    if repo:
        try:
            rel = discdb_local_index(repo).get("imdb", {}).get(imdb_id)
            if rel:
                e = _art_entry(art_dir, name, os.path.join(repo, "data", rel, "cover.jpg"),
                               "TheDiscDB", "poster", box)
                if e:
                    return e
        except OSError:
            pass
    if index is not None and online:
        r = index.execute("SELECT folder FROM posters WHERE imdb=?", (imdb_id,)).fetchone()
        if r:
            try:
                orig = os.path.join(art_dir, imdb_id + "_discdb.jpg")
                if not os.path.exists(orig):
                    _download(_raw(r[0] + "/cover.jpg"), orig)
                e = _art_entry(art_dir, name, orig, "TheDiscDB", "poster", box)
                if e:
                    return e
            except Exception as exc:
                if errors is not None:
                    errors.append("TheDiscDB poster: %s" % exc)
    if key and online:
        try:
            orig = os.path.join(art_dir, imdb_id + "_tmdb.jpg")
            if not os.path.exists(orig):
                found = tmdb_get(key, "/find/%s" % imdb_id, external_source="imdb_id")
                res = (found.get("movie_results") or []) + (found.get("tv_results") or [])
                path = next((r.get("poster_path") for r in res if r.get("poster_path")), None)
                if not path:
                    return None
                _download(TMDB_IMG + path, orig)
            return _art_entry(art_dir, name, orig, "TMDB", "poster", box, credit=TMDB_CREDIT)
        except Exception as exc:
            if errors is not None:
                errors.append("TMDB poster: %s" % exc)
    return None


def discdb_art(rep, art_dir, repo=None, online=True, errors=None, prefer="poster"):
    """From TheDiscDB for this disc: the title's poster, else this release's box art
    (prefer="box": the other way round)."""
    hit = (rep.get("discdb") or {}).get("hit")
    if not hit:
        return None
    name = rep["content_hash"] + ("_box" if prefer == "box" else "_discdb")
    cached = _art_cached(art_dir, name)
    if cached:
        return cached
    tries = []
    if hit.get("path") and repo:      # local clone: data/<type>/<Title (Year)>/<release>/discNN.json
        rel_dir = os.path.join(repo, "data", os.path.dirname(hit["path"]))
        tries += [(os.path.join(rel_dir, "front.jpg"), "box art"),
                  (os.path.join(os.path.dirname(rel_dir), "cover.jpg"), "poster")]
    elif hit.get("path") and online:  # downloaded index: same layout on GitHub
        rel_dir = os.path.dirname(hit["path"].replace("\\", "/"))
        if hit.get("front", True):
            tries.append((_raw(rel_dir + "/front.jpg"), "box art"))
        tries.append((_raw(os.path.dirname(rel_dir) + "/cover.jpg"), "poster"))
    elif online and hit.get("title") and hit.get("slug"):
        q = urllib.parse.quote
        folder = "%s/%s" % ({"series": "series"}.get(str(hit.get("type")).lower(), "movie"),
                            q("%s (%s)" % (hit["title"], hit["year"]) if hit.get("year") else hit["title"]))
        tries += [(DISCDB_RAW + folder + "/" + q(hit["slug"]) + "/front.jpg", "box art"),
                  (DISCDB_RAW + folder + "/cover.jpg", "poster")]
    if prefer != "box":
        tries.sort(key=lambda t: t[1] != "poster")      # stable: poster first, box art next
    for src, what in tries:
        try:
            if src.startswith("http"):
                tmp = os.path.join(art_dir, name + "_dl.jpg")
                _download(src, tmp)
                src = tmp
            if os.path.exists(src):
                e = _art_entry(art_dir, name, src, "TheDiscDB", what, ART_POSTER)
                if e:
                    return e
        except Exception as exc:
            if errors is not None:
                errors.append("TheDiscDB image: %s" % exc)
    return None


def thumb_for(cand, art_dir, repo=None, key=None, online=True, index=None, box=ART_PICK):
    """A small poster for one guess (IMDb id: TheDiscDB then TMDB; TMDB match: its own poster).
    Reuses downloads already cached for the full-size picture."""
    try:
        t = None
        if cand.get("imdb_id"):
            t = poster_for_imdb(cand["imdb_id"], art_dir, repo, key, online, box, None, index)
        if not t and cand.get("tmdb_id") and cand.get("poster_path") and online:
            t = poster_from_tmdb(cand["poster_path"], "tmdb_%s_%s" % (cand.get("kind"), cand["tmdb_id"]), art_dir, box)
        return (t or {}).get("image")
    except Exception:
        return None


def thumbs_for(cands, args):
    """Small posters for a list of guesses (the app's type-ahead), from a background thread:
    opens its own connections. Returns {imdb_id: png path}."""
    art_dir = getattr(args, "art_dir", None) or default_art_dir(getattr(args, "db", None))
    os.makedirs(art_dir, exist_ok=True)
    if image_converter() is None or getattr(args, "no_art", False):
        return {}
    online = not getattr(args, "offline", False)
    key = getattr(args, "tmdb_key", None) or os.environ.get("TMDB_API_KEY")
    index = discdb_index_open(getattr(args, "discdb_dir", None) or default_discdb_dir(), allow_download=False)
    try:
        return {c["imdb_id"]: p for c in cands if c.get("imdb_id")
                for p in [thumb_for(c, art_dir, getattr(args, "discdb_repo", None), key, online, index)] if p}
    finally:
        if index is not None:
            index.close()


def _disc_art(rep, art_dir):
    art = _art_cached(art_dir, rep["content_hash"] + "_disc")
    cover = rep.get("cover") or {}
    if not art and cover.get("image") and os.path.exists(cover["image"]):
        art = _art_entry(art_dir, rep["content_hash"] + "_disc", cover["image"], "disc", "disc art", ART_POSTER)
    return art


def tmdb_collection_poster(name, art_dir, key, errors=None):
    """TMDB groups films into collections ("Wallace & Gromit Collection"), each with its own poster."""
    q = re.sub(r"\bcollection\b", "", strip_tags(name), flags=re.I).strip(" -")
    if not q:
        return None
    try:
        res = tmdb_get(key, "/search/collection", query=q).get("results") or []
    except Exception as exc:
        if errors is not None:
            errors.append("TMDB collection: %s" % exc)
        return None
    best = None
    for r in res:
        n = re.sub(r"\bcollection\b", "", r.get("name") or "", flags=re.I).strip(" -")
        sc = name_score(q, n, False)
        if r.get("poster_path") and sc >= 0.7 and (not best or sc > best[0]):
            best = (sc, r)
    if not best:
        return None
    art = poster_from_tmdb(best[1]["poster_path"], "tmdbcoll_%s" % best[1]["id"], art_dir, errors=errors)
    return dict(art, what="collection poster") if art else None


# --- choosing a different picture -------------------------------------------

ART_CHOOSE = (100, 150)             # tiles for picking a picture (TheDiscDB submission, later)


def art_options(rep, args, index=None, tmdb_max=8):
    """Every picture we could show for this disc, best first: this release's box front and back,
    the film's poster, other releases' box art (TheDiscDB), TMDB posters (with a key), the disc's
    own artwork. Each: {src, source, what, label}; src is a URL or a local file. Nothing is
    downloaded here except TMDB's list of posters."""
    repo = getattr(args, "discdb_repo", None)
    online = not getattr(args, "offline", False)
    key = getattr(args, "tmdb_key", None) or os.environ.get("TMDB_API_KEY")
    opts, seen = [], set()

    def add(src, source, what, label):
        if src and src not in seen:
            seen.add(src)
            opts.append({"src": src, "source": source, "what": what, "label": label})

    def img(rel):                       # a TheDiscDB file: local clone or GitHub
        if repo:
            f = os.path.join(repo, "data", rel)
            return f if os.path.exists(f) else None
        return _raw(rel) if online else None

    cands = rep.get("candidates") or []
    c = adopt_guess(cands)
    imdbs = []
    if c and c.get("kind") == "collection":
        imdbs = [it["imdb_id"] for it in c.get("items") or [] if it.get("imdb_id")]
    elif c and c.get("imdb_id"):
        imdbs = [c["imdb_id"]]
    hit = (rep.get("discdb") or {}).get("hit") or {}
    rel_dir = os.path.dirname(hit["path"].replace("\\", "/")) if hit.get("path") else None
    films = []                          # TheDiscDB film folders: data/<type>/<Title (Year)>
    if rel_dir and c and c.get("kind") != "collection":
        films.append(os.path.dirname(rel_dir))
    for i in imdbs:
        if index is not None:
            r = index.execute("SELECT folder FROM posters WHERE imdb=?", (i,)).fetchone()
            if r and r[0] not in films:
                films.append(r[0])
        elif repo:
            rel = discdb_local_index(repo).get("imdb", {}).get(i)
            if rel and rel not in films:
                films.append(rel)
    if rel_dir:
        rel_name = hit.get("release") or "this release"
        add(img(rel_dir + "/front.jpg"), "TheDiscDB", "box art", "Box front (%s)" % rel_name)
        add(img(rel_dir + "/back.jpg"), "TheDiscDB", "box back", "Box back (%s)" % rel_name)
    for f in films:
        name = re.sub(r"^.*/", "", f)
        add(img(f + "/cover.jpg"), "TheDiscDB", "poster", "Poster: %s" % name)
    for f in films:                     # the same film's other releases
        others = []
        if index is not None:
            others = [(os.path.dirname(p), rel) for p, rel in index.execute(
                "SELECT path, release FROM discs WHERE path LIKE ? AND front=1", (f + "/%",))]
        elif repo:
            others = [(os.path.relpath(os.path.dirname(x), os.path.join(repo, "data")).replace(os.sep, "/"), None)
                      for x in glob.glob(os.path.join(repo, "data", f, "*", "front.jpg"))]
        for d, rel in sorted(set(others)):
            if d != rel_dir:
                add(img(d + "/front.jpg"), "TheDiscDB", "box art",
                    "Box front (%s)" % (rel or d.rsplit("/", 1)[-1].replace("-", " ")))
    if key and online and imdbs and not (c and c.get("kind") == "collection"):
        try:
            found = tmdb_get(key, "/find/%s" % imdbs[0], external_source="imdb_id")
            kind = "movie" if found.get("movie_results") else "tv"
            res = (found.get("movie_results") or found.get("tv_results") or [None])[0]
            if res:
                ims = tmdb_get(key, "/%s/%s/images" % (kind, res["id"]), include_image_language="en,null")
                posters = sorted(ims.get("posters") or [], key=lambda x: -(x.get("vote_average") or 0))
                for n, ptr in enumerate(posters[:tmdb_max], 1):
                    add(TMDB_IMG + ptr["file_path"], "TMDB", "poster", "TMDB poster %d" % n)
        except Exception as exc:
            logger.info("TMDB posters: %s", exc)
    cover = (rep.get("cover") or {}).get("image")
    if cover and os.path.exists(cover):
        add(os.path.abspath(cover), "disc", "disc art", "From the disc")
    return opts


def art_option_load(opt, art_dir, box=ART_POSTER):
    """Fetch (if needed) and convert one option at a size; None if it isn't there (404) or can't be shown."""
    import hashlib
    tag = hashlib.sha1(opt["src"].encode("utf-8")).hexdigest()[:16]
    name = "opt_%s_%dx%d" % (tag, box[0], box[1])
    hit = _art_cached(art_dir, name)
    if hit:
        return dict(hit, label=opt.get("label"))
    src = opt["src"]
    try:
        if src.startswith("http"):
            orig = os.path.join(art_dir, "opt_%s.jpg" % tag)
            if not os.path.exists(orig):
                _download(src, orig)
            src = orig
        if not os.path.exists(src):
            return None
        e = _art_entry(art_dir, name, src, opt["source"], opt["what"], box,
                       credit=TMDB_CREDIT if opt["source"] == "TMDB" else None)
        return dict(e, label=opt.get("label")) if e else None
    except Exception as exc:
        logger.info("picture %s: %s", opt.get("label"), exc)
        return None


def art_options_loaded(rep, args, box=ART_THUMB):
    """For the app's picture chooser (background thread): the options that actually load, each
    with a small image. Opens its own index connection."""
    art_dir = getattr(args, "art_dir", None) or default_art_dir(getattr(args, "db", None))
    os.makedirs(art_dir, exist_ok=True)
    if image_converter() is None:
        return []
    index = discdb_index_open(getattr(args, "discdb_dir", None) or default_discdb_dir(), allow_download=False)
    try:
        out = []
        for o in art_options(rep, args, index):
            e = art_option_load(o, art_dir, box)
            if e:
                out.append(dict(o, thumb=e["image"]))
        return out
    finally:
        if index is not None:
            index.close()


def find_artwork(rep, args, index=None):
    """Fill rep["art"] (main picture) and, for short-film collections, each film's thumbnail."""
    art_dir = getattr(args, "art_dir", None) or default_art_dir(getattr(args, "db", None))
    os.makedirs(art_dir, exist_ok=True)
    online = not getattr(args, "offline", False)
    repo = getattr(args, "discdb_repo", None)
    key = getattr(args, "tmdb_key", None) or os.environ.get("TMDB_API_KEY")
    errors = []
    rep["art"], rep["art_note"] = None, None
    if image_converter() is None:
        rep["art_note"] = "No picture: install Pillow (pip install pillow) or ffmpeg to show posters"
        return None
    cands = rep.get("candidates") or []
    c = cands[0] if cands else None
    collection = bool(c and c.get("kind") == "collection" and adopt_guess(cands))
    # TheDiscDB: the title's poster first (easiest to recognise), else this release's box art.
    # A collection keeps its box: its folder's poster is one film's, not the whole disc's.
    art = discdb_art(rep, art_dir, repo, online, errors, prefer="box" if collection else "poster")
    sure = is_probable(cands)
    if collection:
        for it in c["items"]:
            it["art"] = poster_for_imdb(it["imdb_id"], art_dir, repo, key, online, ART_THUMB, errors, index)
        # the big picture should be the whole disc, not one film of it
        if not art and key and online:
            art = tmdb_collection_poster(c["name"], art_dir, key, errors)
        art = art or _disc_art(rep, art_dir)
        if not art:
            first = c["items"][0]
            art = poster_for_imdb(first["imdb_id"], art_dir, repo, key, online, errors=errors, index=index)
            if art:
                art = dict(art, what="first film's poster")
    elif c and not art:
        # a confident match, or the top guess (labelled as one): a poster helps you check it either way
        poster = None
        if c.get("imdb_id"):
            poster = poster_for_imdb(c["imdb_id"], art_dir, repo, key, online, errors=errors, index=index)
        if not poster and c.get("tmdb_id") and online:
            poster = poster_from_tmdb(c.get("poster_path"), "tmdb_%s_%s" % (c.get("kind"), c["tmdb_id"]),
                                      art_dir, errors=errors)
        if poster and not sure:
            poster = dict(poster, guess=True)
        art = poster
    if not art:                          # last resort: the disc's own images (often a menu still)
        art = _disc_art(rep, art_dir)
    rep["art"] = art
    # small posters beside the other guesses: how most people recognise the right one
    if online or repo:
        for g in cands[:6]:
            if g.get("kind") != "collection":
                g["thumb"] = thumb_for(g, art_dir, repo, key, online, index)
    if errors:
        rep["art_errors"] = errors
    if not art:                          # say why, so a missing picture isn't a mystery
        if not c:
            rep["art_note"] = "No picture: nothing identified yet to look one up for"
        elif not online:
            rep["art_note"] = "No picture: offline"
        elif errors:
            rep["art_note"] = "No picture: " + errors[0]
        elif index is None and not repo and not key:
            rep["art_note"] = ("No picture: the TheDiscDB index hasn't downloaded yet "
                               "(Settings > TheDiscDB > Update now)")
        elif key:
            rep["art_note"] = "No picture: neither TheDiscDB nor TMDB has a poster for %s" % (
                c.get("imdb_id") or "this title")
        else:
            rep["art_note"] = ("No picture for this title in TheDiscDB; a TMDB key in Settings "
                               "adds more posters")
    return art


# --------------------------------------------------------------------------
# 5b. TMDB candidates (optional fallback, needs a key)
# --------------------------------------------------------------------------

def tmdb_get(key, path, **params):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if len(key) > 40:            # v4 read access token
        headers["Authorization"] = "Bearer " + key
    else:                        # v3 api key
        params["api_key"] = key
    url = TMDB_API + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        return json.load(r)


def name_score(query, name, truncated):
    q, n = query.lower(), (name or "").lower()
    if not q or not n:
        return 0.0
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s)
    if squash(n) == squash(q):
        return 1.0
    if truncated and squash(n).startswith(squash(q)):
        return 0.85   # truncated label: a prefix is plausible, but exact names win ties
    return difflib.SequenceMatcher(None, squash(q), squash(n)).ratio()


def tmdb_candidates(key, queries, kind, facts):
    """Rank movie or TV candidates with simple, explainable scores."""
    out, seen = [], set()
    master_year = int(facts["mastered"][:4]) if facts.get("mastered") else None
    for query, truncated in queries:
        if not query:
            continue
        search_q = query
        kinds = ["tv"] if kind == "tv" else ["movie"] if kind == "movie" else ["movie", "tv"]
        for k in kinds:
            res = tmdb_get(key, "/search/%s" % k, query=search_q).get("results", [])[:8]
            for r in res:
                ident = (k, r["id"])
                if ident in seen:
                    continue
                seen.add(ident)
                name = r.get("title") or r.get("name")
                date = r.get("release_date") or r.get("first_air_date") or ""
                year = int(date[:4]) if date[:4].isdigit() else None
                score = name_score(query, name, truncated) * 50
                why = ["name %.0f%%" % (name_score(query, name, truncated) * 100)]
                if master_year and year:
                    if year <= master_year:
                        score += 10
                    else:
                        score -= 40
                        why.append("released after the disc was mastered")
                # one details call gives the runtime and the IMDb id (Plex names, TheDiscDB posters)
                try:
                    det = tmdb_get(key, "/%s/%d" % (k, r["id"]), append_to_response="external_ids") or {}
                except Exception:
                    det = {}
                imdb_id = det.get("imdb_id") or (det.get("external_ids") or {}).get("imdb_id") or None
                if k == "movie" and facts.get("feature_minutes"):
                    rt = det.get("runtime") or 0
                    if rt:
                        diff = abs(rt - facts["feature_minutes"])
                        score += max(0, 30 - diff * 6)
                        why.append("runtime %d vs disc %d min" % (rt, facts["feature_minutes"]))
                if k == "tv" and facts.get("episode_minutes"):
                    rts = det.get("episode_run_time") or []
                    if rts:
                        diff = min(abs(x - facts["episode_minutes"]) for x in rts)
                        score += max(0, 20 - diff * 3)
                        why.append("episode length %s vs disc %d min" % (rts, facts["episode_minutes"]))
                out.append({"kind": k, "tmdb_id": r["id"], "name": name, "year": year,
                            "score": round(score, 1), "why": "; ".join(why),
                            "poster_path": r.get("poster_path") or det.get("poster_path"),
                            "imdb_id": imdb_id})
    out.sort(key=lambda c: -c["score"])
    return out[:5]


def tmdb_season_guess(key, cand, season, disc, n_eps):
    """If label gave season + disc, guess the episode range (assumes equal discs)."""
    try:
        s = tmdb_get(key, "/tv/%d/season/%d" % (cand["tmdb_id"], season))
    except Exception:
        return None
    eps = s.get("episodes") or []
    if not eps or not disc or not n_eps:
        return {"episodes_in_season": len(eps)}
    start = (disc - 1) * n_eps
    chosen = eps[start:start + n_eps]
    return {"episodes_in_season": len(eps),
            "guess": ["S%02dE%02d %s" % (season, e["episode_number"], e.get("name", "")) for e in chosen],
            "note": "assumes %d episodes on every earlier disc; confirm by subtitle matching" % n_eps}


# --------------------------------------------------------------------------
# 4c. TVmaze: episode lists, including DVD order (free, no key; data CC BY-SA, credit TVmaze)
# --------------------------------------------------------------------------
# A disc follows the DVD order, which for some shows isn't the broadcast order (Firefly's
# DVDs start with the two-hour "Serenity", aired last). TVmaze keeps both, links each DVD
# entry to the aired episode(s) it holds, and splits two-hour finales into their parts.

TVMAZE_API = "https://api.tvmaze.com"
TVMAZE_MAX_AGE = 14 * 86400          # episode lists change rarely; refresh every two weeks


def default_tvmaze_dir():
    return os.path.join(os.path.dirname(default_library()), "tvmaze")


def _tvmaze_fetch(path):
    req = urllib.request.Request(TVMAZE_API + path, headers={"User-Agent": USER_AGENT,
                                                             "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:      # /lookup redirects to the show
        return json.loads(r.read().decode("utf-8"))


def tvmaze_get(path, cache_dir=None, online=True):
    """GET from TVmaze with a file cache. Offline (or on an error) a cached copy is used."""
    cache = os.path.join(cache_dir or default_tvmaze_dir(),
                         re.sub(r"[^A-Za-z0-9]+", "_", path.strip("/")) + ".json")
    fresh = os.path.exists(cache) and time.time() - os.path.getmtime(cache) < TVMAZE_MAX_AGE
    if online and not fresh:
        try:
            data = _tvmaze_fetch(path)
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
        except Exception:
            pass
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def tvmaze_orders(imdb_id, cache_dir=None, online=True):
    """{"show": id, "aired": [episodes], "dvd": [DVD entries] or None} for an IMDb series id."""
    show = tvmaze_get("/lookup/shows?imdb=%s" % imdb_id, cache_dir, online)
    if not show or not show.get("id"):
        return None
    sid = show["id"]
    aired = tvmaze_get("/shows/%d/episodes?specials=1" % sid, cache_dir, online) or []
    dvd = None
    for lst in tvmaze_get("/shows/%d/alternatelists" % sid, cache_dir, online) or []:
        if lst.get("dvd_release"):
            dvd = tvmaze_get("/alternatelists/%d/alternateepisodes?embed=episodes" % lst["id"], cache_dir, online)
            break
    return {"show": sid, "name": show.get("name"), "aired": aired, "dvd": dvd or None}


def _ep_code(season, numbers):
    numbers = sorted(n for n in numbers if n is not None)
    if not numbers:
        return None
    return "S%02dE%02d" % (season, numbers[0]) + "".join("-E%02d" % n for n in numbers[1:])


def set_aside_specials(titles, specials, regular_runtimes):
    """Titles that are a season's special rather than an episode: SHERLOCK DISC 2's 55-minute
    unaired pilot and 32-minute "Unlocking Sherlock" are listed with those runtimes, while the
    episodes run 90. A title within a few minutes of a special, and well off the episodes'
    length, becomes an extra under the special's name. Returns the titles changed."""
    rts = sorted(r for r in regular_runtimes if r)
    typical = rts[(len(rts) - 1) // 2] if rts else 0
    changed, used = [], set()
    for t in [t for t in titles if t.get("role") == "episode"]:
        m = t["duration"] / 60.0
        if typical and abs(m - typical) <= 0.15 * typical:
            continue                                  # episode-length: an episode
        for sp in specials:
            rt = sp.get("runtime")
            # a special listed at the episodes' own length (often just the time slot) says nothing
            if not rt or (typical and abs(rt - typical) <= 0.15 * typical):
                continue
            if sp.get("name") not in used and abs(m - rt) <= max(2, 0.06 * rt):
                t["role"], t["special_name"] = "extra", sp["name"]
                t["note"] = "the special \"%s\" (%d min)" % (sp["name"], rt)
                used.add(sp["name"])
                changed.append(t)
                break
    if changed:
        number_episodes(titles)
    return changed


def place_disc(lengths, runtimes, start):
    """Where in the season a disc's episodes sit. lengths: the disc's episode titles in seconds,
    in disc order; runtimes: the season's listed runtimes in minutes. A title may hold two
    listed episodes (a two-part finale stored as one title). Starting near `start` (the
    same-count-per-disc guess), returns (first index, [episodes per title], cost) with the best
    match of relative lengths, or None. Listed runtimes are usually the time slot (60), so
    only the lengths relative to the disc's own typical episode are compared."""
    if not lengths or not runtimes:
        return None
    typ_disc = sorted(lengths)[(len(lengths) - 1) // 2]
    typ_list = sorted(r for r in runtimes if r)[len(runtimes) // 2] if any(runtimes) else 0
    if not typ_disc or not typ_list:
        return None
    rel = [x / typ_disc for x in lengths]
    q = [(r or typ_list) / typ_list for r in runtimes]

    def walk(s):
        cost, widths, j = 0.0, [], s
        for r in rel:
            if j >= len(q):
                return None
            one = abs(r - q[j])
            two = abs(r - q[j] - q[j + 1]) if j + 1 < len(q) else 99
            if two + 0.25 < one:              # clearly two episodes' worth
                widths.append(2); cost += two; j += 2
            else:
                widths.append(1); cost += one; j += 1
        return cost, widths

    best = None
    for s in range(len(q)):
        w = walk(s)
        if w is None:
            continue
        cost = w[0] + 0.05 * abs(s - start)   # equal fits: stay with the counting guess
        if best is None or cost < best[2]:
            best = (s, w[1], cost)
    return best


DVD_MINUTES = 180      # a dual-layer DVD usually holds about 3 hours of episodes


def _name_key(text):
    """Letters only, CamelCase split, without episode-number prefixes: 'EP10 HeartOfGold' ->
    'heartofgold'; 'FEATURE_EP_01' -> '' (no name in it)."""
    t = re.sub(r"(?i)^(FEATURE_)?EP(ISODE)?[ _]*\d*[ _]*", "", text or "")
    return re.sub(r"[^a-z]", "", t.lower())


def match_episode_names(hints, seq):
    """{title position: episode} where a disc name clearly names one episode: the name is in
    the episode's title (OBJECTINSPACE in Objects in Space) or spells it as an abbreviation
    (MESG = The Message). Ambiguous or reused matches are dropped."""
    out, used = {}, {}
    for i, h in enumerate(hints):
        k = _name_key(h)
        if len(k) < 4:
            continue
        hits = []
        for e in seq:
            n = re.sub(r"[^a-z]", "", (e.get("name") or "").lower())
            n2 = re.sub(r"^the", "", n)
            sub = re.compile(".*".join(map(re.escape, k)))
            if k in n or n2.startswith(k[:6]) and sub.search(n) or (sub.search(n2) and n2[:1] == k[:1]):
                hits.append(e)
        if len(hits) == 1:
            e = hits[0]
            if id(e) in used:                 # two names for one episode: trust neither
                out.pop(used[id(e)], None)
                continue
            used[id(e)] = i
            out[i] = e
    return out


def tvmaze_season_guess(orders, season, disc, lengths, name_order="aired", fmt=None, hints=None, before=None):
    """Episode guess for a disc from TVmaze: placed by DVD order when TVmaze has one, named
    by the order asked for (Plex uses aired order unless you change the show's setting).
    lengths: the disc's episode titles (seconds) in disc order; fmt "DVD" lets a short disc
    (a set's last) be told from a full one."""
    if not orders or not season or not lengths:
        return None
    regular = lambda e: e.get("season") == season and e.get("number") is not None
    if orders.get("dvd"):
        seq = [e for e in orders["dvd"] if regular(e)]
        src = "TVmaze DVD order"
    else:
        seq = [e for e in orders.get("aired") or [] if regular(e)]
        src = "TVmaze aired order"
    seq.sort(key=lambda e: e["number"])
    if not seq:
        return None
    runtimes = [e.get("runtime") for e in seq]
    typ = sorted(r for r in runtimes if r)[len([r for r in runtimes if r]) // 2] if any(runtimes) else 60
    units = [(r or typ) / typ for r in runtimes]            # a two-hour episode counts twice
    typ_disc = sorted(lengths)[(len(lengths) - 1) // 2]
    this_disc = sum(round(x / typ_disc) or 1 for x in lengths)
    per_disc = this_disc
    if fmt == "DVD":                    # a full DVD of hour-long episodes holds about 4
        per_disc = max(per_disc, int(DVD_MINUTES // max(1, typ_disc / 60)))
    notes = []
    known = before is not None
    first, before = 0, (before if known else (disc - 1) * per_disc if disc else 0)
    while first < len(seq) and sum(units[:first]) + 0.5 < before:
        first += 1
    if known:
        notes.append("the earlier discs of this set in your library hold %d episodes" % before)
    elif first + len(lengths) > len(seq) or (disc and disc > 1 and this_disc < per_disc
                                             and first + len(lengths) >= len(seq) - 1):
        last = max(0, len(seq) - len(lengths))
        if last != first:
            notes.append("a shorter last disc: the season's last episodes")
        first = last
    placed = place_disc(lengths, runtimes, first)
    if placed:
        if placed[0] != first:
            notes.append("placed by episode lengths")
        first, widths = placed[0], placed[1]
    else:
        widths = [1] * len(lengths)
    if first + sum(widths) > len(seq):   # a long title read as two at the season's end
        widths = [1] * len(lengths)
    def name(entries):
        if name_order == "dvd" or not orders.get("dvd"):
            code = _ep_code(season, [e["number"] for e in entries])
        else:                          # the aired episode(s) each DVD entry holds
            aired = [a for e in entries for a in (e.get("_embedded") or {}).get("episodes") or []]
            code = _ep_code(season, [a.get("number") for a in aired if a.get("season") == season]) \
                or _ep_code(season, [e["number"] for e in entries])
        return "%s %s" % (code, " / ".join(e.get("name") or "" for e in entries))

    placed_entries, j = [], first
    for w in widths:
        entries = seq[j:j + w]
        j += w
        if not entries:
            break
        placed_entries.append(entries)
    # The disc's own playlist names may spell the episode out (EP10 HeartOfGold, EP13_Mesg):
    # where one clearly matches an episode of the season, that beats counting and order
    by_name = {i: e for i, e in match_episode_names(hints or [], seq).items() if i < len(placed_entries)}
    if by_name:
        notes.append("%d matched by the disc's own playlist names" % len(by_name))
        # the rest take this disc's other episodes, in order, skipping the ones matched by name
        taken = {id(e) for e in by_name.values()}
        pool = [e for es in placed_entries for e in es if id(e) not in taken]
        out = []
        for i, es in enumerate(placed_entries):
            if i in by_name:
                out.append([by_name[i]])
            else:
                n = len(es)
                out.append(pool[:n] or es)
                pool = pool[n:]
        placed_entries = out
    guess = [name(entries) for entries in placed_entries]
    if 2 in widths:
        notes.append("a long title holds two episodes")
    named = "DVD order" if name_order == "dvd" and orders.get("dvd") else "aired order"
    return {"episodes_in_season": len(seq), "guess": guess, "source": src, "order": name_order,
            "has_dvd_order": bool(orders.get("dvd")),
            "note": "; ".join(["%s, named in %s" % (src, named)] + notes +
                              ([] if known else ["episodes on earlier discs are estimated; check the names"]))}


# --------------------------------------------------------------------------
# main pipeline
# --------------------------------------------------------------------------

def discdb_roles(titles, hit):
    """TheDiscDB names the disc's main movie, so trust it over our length-based guess: other
    film-length titles become alternate versions (close in length) or extras, and an overlong
    one (a maximizer or seamless 'everything' playlist) is set aside. Returns what changed."""
    items = {str(x.get("source") or "").lower(): x.get("item") or {} for x in hit.get("titles") or []}
    mains = {k for k, it in items.items() if it.get("type") == "MainMovie"}

    def keys(t):
        ks = [t["id"].lower()]
        m = re.match(r"title (\d+)$", ks[0])
        if m:                                         # TheDiscDB numbers DVD titles "01", "04" ...
            ks += ["%02d" % int(m.group(1)), m.group(1)]
        clips = t.get("clip_list") or ""
        if clips and "," not in clips:
            ks.append(clips.lower() + ".m2ts")       # playlist of one clip
        return ks

    if not mains:
        # a series disc: the titles TheDiscDB lists as episodes are the episodes, whatever their
        # length (SHERLOCK_THE_ABOMINABLE_BRIDE: one 93-minute special, S00E09)
        changed = []
        for t in titles:
            it = next((items[k] for k in keys(t) if items.get(k)), None)
            if it and it.get("type") == "Episode" and t["role"] != "episode":
                changed.append((t["id"], t["role"], "episode"))
                t["role"], t["discdb_title"] = "episode", it.get("title")
                t["note"] = "TheDiscDB: %s" % (it.get("title") or "episode")
            elif it and it.get("type") not in (None, "", "Episode") and t["role"] in ("episode", "feature", "version"):
                changed.append((t["id"], t["role"], "extra"))
                t["role"], t["note"] = "extra", "TheDiscDB: %s" % (it.get("title") or it.get("type"))
        if changed:
            number_episodes(titles)
        return changed
    main_len = [t["duration"] for t in titles if any(k in mains for k in keys(t)) and t.get("duration")]
    if not main_len:
        return []
    changed = []
    for t in titles:
        ks = keys(t)
        listed = next((items[k] for k in ks if items.get(k)), None)     # entries TheDiscDB named
        if listed and listed.get("type") == "MainMovie":
            t["discdb_title"] = listed.get("title")
            if t["role"] == "duplicate" and t.get("note", "").startswith("same video as "):
                # TheDiscDB names this copy: it becomes the feature, its twin the duplicate
                twin = next((x for x in titles if x["id"] == t["note"][len("same video as "):]), None)
                if twin:
                    changed.append((twin["id"], twin["role"], "duplicate"))
                    twin["role"], twin["note"] = "duplicate", "same video as %s" % t["id"]
                    for x in titles:
                        if x is not t and x.get("note") == "same video as %s" % twin["id"]:
                            x["note"] = "same video as %s" % t["id"]
                        elif x is not t and twin["id"] in (x.get("note") or ""):
                            x["note"] = x["note"].replace(twin["id"], t["id"])
            if t["role"] not in ("feature",):
                changed.append((t["id"], t["role"], "feature"))
                t["role"], t["note"] = "feature", "TheDiscDB: main movie"
            continue
        if listed and t["role"] == "duplicate" and t.get("note", "").startswith("same video as "):
            twin = next((x for x in titles if x["id"] == t["note"][len("same video as "):]), None)
            if twin and not any(items.get(k) for k in keys(twin)):      # keep the copy TheDiscDB names
                changed.append((t["id"], "duplicate", twin["role"]))
                t["role"], t["note"], twin["role"], twin["note"] = twin["role"], twin["note"], \
                    "duplicate", "same video as %s" % t["id"]
                for x in titles:                                       # other copies point at the named one
                    if x is not t and x.get("note") == "same video as %s" % twin["id"]:
                        x["note"] = "same video as %s" % t["id"]
            continue
        if t["role"] != "feature":
            continue
        d = t.get("duration") or 0
        if listed:
            role, note = "extra", "TheDiscDB: %s" % (listed.get("title") or listed.get("type") or "extra")
        elif any(abs(d - m) <= 0.1 * m for m in main_len):
            role, note = "version", "film-length, but TheDiscDB's main movie is another title: likely an alternate version"
            ref = min((x for x in titles if any(k in mains for k in keys(x))),
                      key=lambda x: abs((x.get("duration") or 0) - d))
            what = version_label(t.get("video_info"), ref.get("video_info"))
            if what:
                t["edition"] = what
                note = "%s version of %s (TheDiscDB's main movie)" % (what.lower(), ref["id"])
        elif d > 1.5 * max(main_len):
            role, note = "junk", "much longer than TheDiscDB's main movie (maximizer or all-in-one playlist)"
        else:
            role, note = "extra", "not TheDiscDB's main movie"
        changed.append((t["id"], t["role"], role))
        t["role"], t["note"] = role, note
    return changed


def edition_name(title, film):
    """'The Iron Giant: Signature Edition' -> 'Signature Edition' (None when it's just the film's name)."""
    if not title:
        return None
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    rest = title
    words = re.split(r"(\s*[:\-\u2013\u2014(]\s*)", title, maxsplit=1)
    if len(words) == 3 and norm(words[0]) == norm(film):
        rest = words[2].rstrip(") ")
    elif norm(title) == norm(film) or norm(title).startswith(norm(film)) and len(norm(title)) - len(norm(film)) < 3:
        return None
    rest = re.sub(r"[{}]", "", rest).strip()
    return rest or None


def editions(titles, film):
    """Two or more real features (theatrical + extended, say): give each a Plex edition so they
    don't overwrite each other: named from TheDiscDB when it knows, else by running time."""
    feats = [t for t in titles if t["role"] == "feature"]
    if len(feats) < 2:
        return {}
    out, seen = {}, set()
    for t in sorted(feats, key=lambda t: t.get("duration") or 0):
        others = [x for x in feats if x is not t]
        diff = version_label(t.get("video_info"), others[0].get("video_info")) if len(others) == 1 else None
        name = (edition_name(t.get("discdb_title"), film) or diff
                or "%d Minute Cut" % round((t.get("duration") or 0) / 60))
        if name.lower() in seen:
            name = "%s (%d min)" % (name, round((t.get("duration") or 0) / 60))
        seen.add(name.lower())
        out[t["id"]] = name
    return out


def identify(root, args):
    sample = load_sample(root)
    if sample:
        fmt = sample["format"]
        base = find_child(root, "BDMV" if fmt == "Blu-ray" else "VIDEO_TS") or root
        fp = {"hash": content_hash(sz for _, sz in sample["stream_files"]),
              "files": [tuple(x) for x in sample["stream_files"]],
              "newest_mtime": sample.get("newest_mtime")}
    else:
        fmt, base = detect_format(root)
        if not fmt:
            raise SystemExit("%s: no BDMV/STREAM or VIDEO_TS folder found" % root)
        fp = fingerprint(fmt, base)
    rep = {"path": root, "format": fmt, "tool_version": VERSION, "from_sample": bool(sample)}
    if sample and sample.get("truth"):
        rep["truth"] = sample["truth"]

    rep["content_hash"] = fp["hash"]
    rep["stream_files"] = len(fp["files"])
    rep["mastered"] = mastering_date(fp)

    # Your confirmed discs act as a private disc database (skipped when the input is a
    # disc rebuilt from evidence, so corpus scoring never grades a disc against its own answer).
    lib = getattr(args, "library", None)
    personal = personal_lookup(lib, fp["hash"]) if lib is not None and not sample else None
    rep["personal_hit"] = personal

    index_errors = []
    index = discdb_index_open(getattr(args, "discdb_dir", None) or default_discdb_dir(),
                              allow_download=not args.offline, force=getattr(args, "discdb_update", False),
                              log=log, errors=index_errors)
    db = discdb_lookup(fp, repo=args.discdb_repo, index=index,
                       online=not args.offline and not personal and not getattr(args, "no_discdb_online", False))
    db["errors"] += index_errors
    if index is None and not args.discdb_repo:
        db["errors"].append("no TheDiscDB index yet (it downloads automatically when online)")
    rep["discdb"] = db

    # structure
    bd_meta = None
    try:
        if fmt == "DVD":
            s = parse_dvd(base)
            rep["provider"] = s["provider"] or None
        else:
            s = parse_bluray(base)
            bd_meta = s["meta"]
            rep["bd_meta"] = {k: v for k, v in bd_meta.items() if k != "thumbnails"}
        titles = s["titles"]
        kind, obfuscated = classify(titles, fmt)
        if kind == "tv":
            apply_authoring_names(titles)
    except Exception as exc:
        titles, kind, obfuscated = [], "unknown", False
        rep["structure_error"] = str(exc)
    rep["kind"] = kind
    rep["playlist_obfuscation"] = obfuscated
    rep["discdb_roles"] = discdb_roles(titles, (db.get("hit") or {}))
    number_episodes(titles)
    rep["titles"] = [{k: v for k, v in t.items() if k not in ("segments", "in_play_all", "item_chapters", "play_order")}
                     for t in titles]

    eps = [t for t in titles if t["role"] == "episode"]
    feats = [t for t in titles if t["role"] == "feature"]
    facts = {"mastered": rep["mastered"]}
    if eps:
        facts["episode_minutes"] = round(sorted(t["duration"] for t in eps)[len(eps) // 2] / 60)
        facts["episode_count"] = len(eps)
    if feats:
        facts["feature_minutes"] = round(max(t["duration"] for t in feats) / 60)
    commentary = any(a.get("kind") == "commentary" for t in titles if t["role"] in ("episode", "feature")
                     for a in t["audio"])
    facts["commentary_flagged"] = commentary
    rep["facts"] = facts

    # metadata
    label = args.label or (sample.get("label") if sample else volume_label(root))
    rep["label"] = label
    rep["label_parsed"] = parse_label(label)
    cover = extract_cover(root, fmt, args.out or args.tmp_out, bd_meta) if not args.no_cover else None
    if cover:
        cover["saved"] = bool(args.out)
    rep["cover"] = cover
    rep["cover_ocr"] = ocr(cover["image"]) if cover and cover.get("image") else None

    # candidates
    queries = []
    lp = rep["label_parsed"]
    if bd_meta and bd_meta["names"]:
        bl = parse_label(bd_meta["names"][0])
        bl["maybe_truncated"] = False   # disc metadata titles are full names, not clipped labels
        queries.append((bl["query"], False))
        lp = lp or bl
        if lp is not bl:
            for k in ("season", "disc"):
                lp[k] = lp.get(k) or bl.get(k)
    if lp and lp.get("query"):
        queries.append((lp["query"], lp["maybe_truncated"]))
    if lp and lp.get("alt_query"):
        queries.append((lp["alt_query"], lp["maybe_truncated"]))
    if rep["cover_ocr"]:
        queries.append((rep["cover_ocr"][:60], False))
    # A confirmed answer (your library or TheDiscDB) still goes through IMDb, so the rip
    # gets proper Plex names with the IMDb id.
    anchor = personal["truth"] if personal else None
    if not anchor and db["hit"]:
        h = db["hit"]
        anchor = h.get("title") or ""
        if h.get("year") and not re.search(r"\(\d{4}\)", anchor):
            anchor += " (%s)" % h["year"]
        seasons = [str((t.get("item") or {}).get("season")) for t in h.get("titles") or []
                   if (t.get("item") or {}).get("season")]
        if seasons:
            anchor += " S%s" % max(set(seasons), key=seasons.count)
    if anchor:
        pt = parse_truth(anchor)
        queries = [(pt["title"], False)]
        lp = dict(lp or {"raw": None, "side": None, "year": None},
                  season=pt["season"] if pt["season"] is not None else (lp or {}).get("season"),
                  disc=pt["disc"] or (lp or {}).get("disc"), query=pt["title"], maybe_truncated=False)
        facts["truth_year"] = pt["year"]
        if pt["title"] and db["hit"] and kind == "unknown":
            kind = "tv" if pt["season"] is not None else "movie"
        if db["hit"] and str(db["hit"].get("type", "")).lower() in ("series", "tv", "show") and kind != "tv":
            kind = rep["kind"] = "tv"           # TheDiscDB files it under a series (a special's disc)
            eps = [t for t in titles if t["role"] == "episode"]
            facts = {k: v for k, v in facts.items() if k != "feature_minutes"}
            if eps:
                facts["episode_minutes"] = round(sorted(t["duration"] for t in eps)[(len(eps) - 1) // 2] / 60)
                facts["episode_count"] = len(eps)
            rep["facts"] = facts
        hit_is_movie = db["hit"] and str(db["hit"].get("type", "")).lower() == "movie"
        if kind == "tv" and not pt["season"] and not pt["films"] and (hit_is_movie or personal) and as_movie(titles):
            kind = "movie"                     # your answer / TheDiscDB say film: the "episodes" are reels
            facts = movie_facts(titles, facts)
            rep["facts"] = facts
            rep["kind"] = kind
            eps = []
    # another disc of the same set (shared menu files) that you confirmed as a TV show: this one
    # is the same show, even if its structure looks like a film (Psych S1 D1's extended pilot)
    rep["siblings"] = find_siblings(lib, fp, fp["hash"])
    # shared studio boilerplate (logos, warnings) also links unrelated discs, so the sibling's
    # show has to fit this disc's own label too (PSYCH and PSYCH; not ROBIN_HOOD_POT and Chuck)
    own = (lp or {}).get("query") or ""

    def fits(x):
        t = parse_truth(x["known_as"])
        same_label = x.get("label") and x["label"] == rep.get("label")
        return t.get("season") and t.get("title") and (same_label or (own and name_score(own, t["title"], False) >= 0.85))
    sib_tv = next((parse_truth(x["known_as"]) for x in rep["siblings"] if x.get("confirmed") and fits(x)), None)
    if sib_tv and not anchor and sib_tv.get("title"):
        rep["sibling_hint"] = sib_tv["title"]
        rep["sibling_season"] = sib_tv.get("season")
        queries.insert(0, (sib_tv["title"], False))
        if kind == "movie" and as_tv(titles):
            kind = rep["kind"] = "tv"
            number_episodes(titles)
            eps = [t for t in titles if t["role"] == "episode"]
            facts = {k: v for k, v in facts.items() if k != "feature_minutes"}
            facts["episode_minutes"] = round(sorted(t["duration"] for t in eps)[(len(eps) - 1) // 2] / 60)
            facts["episode_count"] = len(eps)
            rep["facts"] = facts
    if kind == "unknown" and lp and lp.get("season"):
        kind = rep["kind"] = "tv"          # structure unreadable, but the label names a season
    if kind == "movie" and lp and lp.get("season") and not (anchor and pt.get("films")) and as_tv(titles):
        # the label (or your answer) names a season and disc: a TV disc whose two-hour
        # finale looked like a film
        kind = rep["kind"] = "tv"
        number_episodes(titles)
        eps = [t for t in titles if t["role"] == "episode"]
        facts = {k: v for k, v in facts.items() if k != "feature_minutes"}
        facts["episode_minutes"] = round(sorted(t["duration"] for t in eps)[(len(eps) - 1) // 2] / 60)
        facts["episode_count"] = len(eps)
        rep["facts"] = facts
    dedup = []
    for q, tr in queries:
        if q and q.lower() not in (x[0].lower() for x in dedup):
            dedup.append((q, tr))
    queries = dedup
    rep["label_parsed"] = lp
    rep["search_queries"] = [q for q, _ in queries]
    key = args.tmdb_key or os.environ.get("TMDB_API_KEY")
    rep["candidates"] = []
    season = lp.get("season") if lp else None
    disc_no = lp.get("disc") if lp else None
    if (db["hit"] is None or anchor) and queries and not getattr(args, "no_imdb", False):
        try:
            con = imdb_open(args.imdb_dir, allow_download=not args.offline,
                            force=getattr(args, "imdb_update", False), log=log)
            if con is None:
                rep["imdb_note"] = "IMDb data not downloaded yet (run once without --offline)"
            else:
                with con:
                    picked = imdb_by_id(con, pt["imdb_id"]) if anchor and pt.get("imdb_id") else None
                    if anchor and pt.get("films"):
                        picked = collection_from_ids(con, pt["films"], titles, anchor)
                    rep["candidates"] = [picked] if picked else imdb_candidates(con, queries, kind, facts)
                    if not rep["candidates"] and not anchor and lp and lp.get("short_query"):
                        # nothing for the whole label: try it without its short trailing code (_DES)
                        rep["candidates"] = imdb_candidates(con, [(lp["short_query"], False)], kind, facts)
                        rep["search_queries"].append(lp["short_query"])
                    if kind == "tv" and not anchor:
                        # a disc of short films looks like TV; a collection that explains the titles wins
                        # over a TV match it beats (tiny IMDb TV entries can look "confident")
                        coll = imdb_collection(con, queries, titles, facts)
                        top_tv = rep["candidates"][0]["score"] if rep["candidates"] else 0
                        if coll and (not is_probable(rep["candidates"]) or coll["score"] > top_tv):
                            rep["candidates"].insert(0, coll)
                    if kind == "tv" and not is_probable(rep["candidates"]) and not anchor:
                        longest = max((t["duration"] for t in titles
                                       if t.get("role") not in ("junk", "loop", "duplicate")), default=0)
                        if longest >= FEATURE_MIN * 0.75:
                            mf = dict(movie_facts(titles, facts), feature_minutes=round(longest / 60))
                            mc = imdb_candidates(con, queries, "movie", mf)
                            tv_best = rep["candidates"][0]["score"] if rep["candidates"] else 0
                            # the film has to be about as long as the disc's longest title: a 3½-hour
                            # play-all of shorts is not the 82-minute Little Rascals (1994)
                            fits = mc and mc[0].get("runtime") and \
                                abs(mc[0]["runtime"] - longest / 60) <= max(15, 0.2 * longest / 60)
                            if is_probable(mc) and fits and mc[0]["score"] > tv_best and as_movie(titles):
                                kind = rep["kind"] = "movie"
                                facts = movie_facts(titles, facts)
                                rep["facts"] = facts
                                rep["candidates"] = mc
                                eps = []
                    if kind == "movie" and not anchor and as_tv_possible(titles):
                        # check the other reading too: "PSYCH" is exactly a well-known series, while
                        # every film it finds is only a partial name match (Psycho Cop Returns)
                        tf = {k: v for k, v in facts.items() if k != "feature_minutes"}
                        tc = imdb_candidates(con, queries, "tv", tf)
                        top_m = rep["candidates"][0] if rep["candidates"] else None
                        if tc and tc[0].get("name_score", 0) >= 0.999 and tc[0].get("votes", 0) >= 10000 \
                                and (not top_m or top_m.get("name_score", 0) < 0.999) and as_tv(titles):
                            kind = rep["kind"] = "tv"
                            number_episodes(titles)
                            eps = [t for t in titles if t["role"] == "episode"]
                            facts = dict(tf, episode_minutes=round(
                                sorted(t["duration"] for t in eps)[(len(eps) - 1) // 2] / 60), episode_count=len(eps))
                            rep["facts"] = facts
                            rep["candidates"] = imdb_candidates(con, queries, "tv", facts)
                    if not rep["candidates"] and not anchor and kind == "movie" and rep.get("label"):
                        rep["candidates"] = imdb_initials(con, rep["label"], facts)   # BCS0NNW1-style codes
                    top = rep["candidates"][0] if rep["candidates"] else None
                    if top and top["kind"] == "tv" and not season and eps and top.get("_n"):
                        # no season on the label (FIREFLYUS_D1, SHERLOCK DISC 1): the show only
                        # ever had one, or only one had aired when the disc was mastered, or
                        # another disc of the same set you confirmed says which
                        first_year = {r[0]: r[1] for r in con.execute(
                            "SELECT season, MIN(year) FROM episodes WHERE parent=? AND episode > 0 "
                            "GROUP BY season", (top["_n"],)) if r[0]}
                        season, why = season_from_dates(first_year, rep.get("mastered"))
                        if not why and rep.get("sibling_season") and rep.get("sibling_hint") and \
                                name_score(rep["sibling_hint"], top["name"], False) >= 0.9:
                            season, why = rep["sibling_season"], "the season of another disc of this set"
                        if not why and disc_no and 1 in first_year:
                            # HOGANS_HEROES_D1: a disc number but no season. First-season sets
                            # often leave the season off (later ones say S2, S3...): assume 1
                            season, why = 1, "assumed: the label has a disc number but no season"
                        if why:
                            rep["season_note"] = why
                            rep["label_parsed"] = lp = dict(lp or {}, season=season)
                    if top and top["kind"] == "tv" and season and top.get("_n"):
                        sp_rows = con.execute("SELECT title, runtime, episode FROM episodes WHERE parent=? "
                                              "AND season=?", (top["_n"], season)).fetchall()
                        if set_aside_specials(titles, [{"name": r[0], "runtime": r[1]} for r in sp_rows if r[2] == 0],
                                              [r[1] for r in sp_rows if r[2]]):
                            eps = [t for t in titles if t["role"] == "episode"]
                    if top and top["kind"] == "tv" and season:
                        ordered = sorted(eps, key=lambda t: t.get("episode_index") or 0)
                        rep["episode_guess"] = imdb_season(
                            con, top, season, disc_no, len(eps), [t["duration"] for t in ordered], fmt,
                            [t.get("authoring_name") or "" for t in ordered],
                            episodes_before(lib, top["name"], season, disc_no, fp["hash"]))
                con.close()
        except Exception as exc:
            rep["candidates_error"] = "IMDb: %s" % exc
    if db["hit"] is None and key and queries and not is_probable(rep["candidates"]):
        try:
            tm = tmdb_candidates(key, queries, kind, facts)
            for c in tm:
                c["source"] = "TMDB"
            merged = {}
            for c in sorted(rep["candidates"] + tm, key=lambda c: -c["score"]):
                k = c.get("imdb_id") or ("tmdb", c.get("kind"), c.get("tmdb_id")) or id(c)
                if k not in merged:
                    merged[k] = c
                elif c.get("poster_path") and not merged[k].get("poster_path"):
                    merged[k]["poster_path"], merged[k]["tmdb_id"] = c["poster_path"], c.get("tmdb_id")
            rep["candidates"] = list(merged.values())[:5]
            top = rep["candidates"][0] if rep["candidates"] else None
            if top and top.get("source") == "TMDB" and top["kind"] == "tv" and season:
                rep["episode_guess"] = tmdb_season_guess(key, top, season, disc_no, len(eps))
        except Exception as exc:
            rep["candidates_error"] = "TMDB: %s" % exc
    top = rep["candidates"][0] if rep["candidates"] else None
    hit = db.get("hit") or {}
    if top and top["kind"] == "tv" and eps and hit.get("titles"):
        # TheDiscDB names each episode of this very disc (season and number): no guessing
        by_src = {str(x.get("source") or "").lower(): x.get("item") or {} for x in hit["titles"]}
        ordered = sorted(eps, key=lambda t: t.get("episode_index") or 0)
        items = [by_src.get(t["id"].lower()) or by_src.get("%02d" % int(t["id"].split()[-1]))
                 if t["id"].lower().startswith("title ") else by_src.get(t["id"].lower()) for t in ordered]
        if items and all(it and it.get("season") not in (None, "") and it.get("episode") for it in items):
            ss = {int(it["season"]) for it in items}
            if len(ss) == 1:
                season = ss.pop()
                rep["episode_guess"] = {"source": "TheDiscDB",
                                        "guess": ["S%02dE%02d %s" % (season, int(it["episode"]), it.get("title") or "")
                                                  for it in items],
                                        "note": "TheDiscDB names each episode on this disc" +
                                                ("; season 0 is where Plex keeps specials" if season == 0 else "")}
    if top and top["kind"] == "tv" and eps and season is None and not rep.get("episode_guess"):
        rep["episode_guess"] = {"note": "the label doesn't say which season, so the episodes can't be "
                                        "named yet: they rip as Episode 01, 02, ... in disc order"}
    if top and top["kind"] == "tv" and top.get("imdb_id") and season and eps \
            and not getattr(args, "no_tvmaze", False) and rep.get("episode_guess", {}).get("source") != "TheDiscDB":
        try:
            orders = tvmaze_orders(top["imdb_id"], getattr(args, "tvmaze_dir", None), online=not args.offline)
            if orders and rep.get("season_note"):
                # the season was inferred, not on the label: make sure TVmaze sees the show the same
                # way (THE_LITTLE_RASCALS_D1: IMDb has one season of 13; TVmaze 22 seasons of shorts
                # from 1922). If not, naming these episodes would be guesswork.
                aired = [e for e in orders.get("aired") or [] if e.get("number") is not None]
                tv_seasons = {e.get("season") for e in aired if e.get("season")}
                s_years = [int(e["airdate"][:4]) for e in aired if e.get("season") == season and e.get("airdate")]
                start = top.get("year")
                if ("only one season" in rep["season_note"] and len(tv_seasons) > 1) or \
                        (s_years and start and abs(min(s_years) - start) > 2):
                    rep["episode_guess"] = {"note": "the label doesn't say which season and IMDb and TVmaze "
                                                    "disagree on the show's seasons, so the episodes aren't "
                                                    "named: they rip as Episode 01, 02, ... in disc order"}
                    orders = None
                    season = None
            if orders:
                in_season = [e for e in orders.get("aired") or [] if e.get("season") == season]
                if set_aside_specials(titles, [e for e in in_season if e.get("number") is None],
                                      [e.get("runtime") for e in in_season if e.get("number") is not None]):
                    eps = [t for t in titles if t["role"] == "episode"]
            ordered = sorted(eps, key=lambda t: t.get("episode_index") or 0)
            lengths = [t["duration"] for t in ordered]
            g = tvmaze_season_guess(orders, season, disc_no, lengths, getattr(args, "episode_order", "aired"), fmt,
                                    hints=[t.get("authoring_name") or "" for t in ordered],
                                    before=episodes_before(lib, top["name"], season, disc_no, fp["hash"]))
            if g:
                rep["episode_guess"] = g
        except Exception as exc:
            rep["candidates_error"] = "TVmaze: %s" % exc
    if rep.get("episode_guess", {}).get("guess") and rep.get("season_note"):
        g = rep["episode_guess"]
        g["note"] = "season %s: %s; %s" % (season, rep["season_note"], g["note"])
    if adopt_guess(rep["candidates"]) and rep["candidates"][0]["kind"] == "collection":
        rep["kind"] = "collection"
        rep["plex_names"] = apply_collection(rep["candidates"][0], titles)
    elif adopt_guess(rep["candidates"]):
        eds = {}
        if rep["candidates"][0]["kind"] == "movie":
            srcs = [t["id"] for t in titles if t["role"] == "feature"][:1]
            eds = editions(titles, rep["candidates"][0]["name"])
            if eds:
                srcs = list(eds)
        else:
            srcs = [t["id"] for t in sorted((t for t in titles if t.get("episode_index")),
                                            key=lambda t: t["episode_index"])]
        rep["plex_names"] = plex_names(rep["candidates"][0], season, rep.get("episode_guess"), srcs,
                                       editions=eds)
    rep["titles"] = [{k: v for k, v in t.items() if k not in ("segments", "in_play_all", "item_chapters", "play_order")}
                     for t in titles]
    m = adopt_guess(rep["candidates"])
    rep["match"] = {"name": m["name"], "year": m.get("year"), "kind": m["kind"], "imdb_id": m.get("imdb_id"),
                    "sure": is_probable(rep["candidates"]), "source": m.get("source"),
                    "season": season if m["kind"] == "tv" else None,   # 0: specials
                    "disc": disc_no if m["kind"] == "tv" else None,
                    "films": len(m.get("items") or []) or None} if m else None
    for c in rep["candidates"]:
        c.pop("_n", None)
    rep["status"] = status(rep, key)
    rep["best_guess"], rep["identified_by"] = best_guess(rep)
    if not getattr(args, "no_art", False):
        try:
            find_artwork(rep, args, index)
        except Exception as exc:          # artwork is a nicety; never fail a scan over it
            rep["art"], rep["art_errors"] = None, ["%s: %s" % (type(exc).__name__, exc)]
    if index is not None:
        index.close()
    log_report(rep)
    if lib is not None and not sample and not getattr(args, "no_record", False):
        rep["evidence_stored"] = record_disc(lib, rep, fp, root, fmt, base, getattr(args, "keep_evidence", False))
        rep["recorded"] = True
        rep["library_path"] = os.path.abspath(args.db)
    return rep


def apply_collection(cand, titles):
    """Each short film is its own Plex movie; second copies become versions."""
    byid = {t["id"]: t for t in titles}
    names = []
    for k, it in enumerate(cand["items"], 1):
        t = byid[it["title_id"]]
        t["role"], t["note"] = "feature", "short film %d: %s (%s)" % (k, it["name"], it["year"])
        t.pop("episode_index", None)
        names += ["%-14s -> " % it["title_id"] + n.split(" -> ")[-1] for n in plex_names(
            {"kind": "movie", "name": it["name"], "year": it["year"], "imdb_id": it["imdb_id"]})]
    for v in cand.get("versions") or []:
        t = byid[v["title_id"]]
        t["role"], t["note"] = "version", "same length as %s: likely a second version" % v["same_as"]
        t.pop("episode_index", None)
    for tid in cand.get("unmatched") or []:
        byid[tid]["role"], byid[tid]["note"] = "extra", "episode-length, no matching short film"
        byid[tid].pop("episode_index", None)
    return names


def best_guess(rep):
    """Name the most likely title and which signal produced it (confirmed or not)."""
    if rep.get("personal_hit"):
        return strip_tags(rep["personal_hit"]["truth"]), "your library"
    hit = rep["discdb"]["hit"]
    if hit:
        return "%s | %s" % (hit.get("title"), hit.get("disc") or hit.get("release")), "TheDiscDB"
    c = rep.get("candidates") or []
    if rep["status"].startswith(("probable", "best guess")) and c and c[0]["kind"] == "collection":
        return "%s: %s" % (c[0]["name"], ", ".join("%s (%s)" % (i["name"], i["year"]) for i in c[0]["items"])), \
            "IMDb short films"
    if rep["status"].startswith(("probable", "best guess")) and c:
        g = "%s (%s)" % (c[0]["name"], c[0]["year"])
        lp = rep.get("label_parsed") or {}
        if c[0]["kind"] == "tv":
            g += "".join(" %s%s" % (k[0].upper(), lp[k]) for k in ("season", "disc") if lp.get(k))
        return g, ("%s ranking" if rep["status"].startswith("probable") else "%s best guess") % c[0].get("source", "TMDB")
    lp = rep.get("label_parsed") or {}
    tail = ""
    if lp.get("season"):
        tail += " S%s" % lp["season"]
    if lp.get("disc"):
        tail += " D%s" % lp["disc"]
    names = (rep.get("bd_meta") or {}).get("names") or []
    if names:
        already = re.search(r"season|series|disc|\bS\d+|\bD\d+", names[0], re.I)
        return names[0] + ("" if already else tail), "disc metadata (unconfirmed)"
    if rep.get("cover_ocr"):
        return rep["cover_ocr"], "cover OCR (unconfirmed)"
    if lp.get("query"):
        return lp["query"] + tail, "volume label (unconfirmed)"
    sib = next((x for x in rep.get("siblings") or [] if x.get("known_as")), None)
    if sib:
        return "same set as %s" % sib["known_as"], "sibling disc (unconfirmed)"
    if rep.get("cover") and rep["cover"].get("image"):
        return None, "cover art only (needs a look)"
    return None, "nothing yet"


def parse_truth(truth):
    """'Gone in 60 Seconds (2000)' / 'Psych S1 D2' -> title, year, season, disc."""
    t = truth or ""
    imdb = re.search(r"\{imdb-(tt\d+)\}", t)
    t = re.sub(r"\{imdb-tt\d+\}", " ", t)
    films = re.search(r"\{films:([^}]*)\}", t)
    t = re.sub(r"\{films:[^}]*\}", " ", t)
    films = [tuple(x.split("=", 1)) for x in films.group(1).split("|") if "=" in x] if films else []
    year = re.search(r"\((\d{4})\)", t)
    season = re.search(r"\b(?:S|season\s*)0*(\d+)\b", t, re.I)
    disc = re.search(r"\b(?:D|dis[ck]\s*)0*(\d+)\b", t, re.I)
    title = re.sub(r"\(\d{4}\)|\b(?:S|season\s*)\d+\b|\b(?:D|dis[ck]\s*)\d+\b", " ", t, flags=re.I)
    return {"title": " ".join(title.split()), "year": int(year.group(1)) if year else None,
            "season": None if films else (int(season.group(1)) if season else None),
            "disc": None if films else (int(disc.group(1)) if disc else None),
            "imdb_id": imdb.group(1) if imdb else None, "films": films}


def truth_verdict(guess, truth):
    """Compare a guessed title with the recorded truth. Years must agree when both have one;
    a prefix counts (truncated labels) unless the extra part is a number (Shrek vs Shrek 2)."""
    def split(x):
        x = x or ""
        year = re.search(r"\((\d{4})\)", x)
        core = re.split(r" \| ", x)[0]
        core = re.sub(r"\(\d{4}\)|\bS\d+\b|\bD\d+\b|\bseason\s*\d+|\bdis[ck]\s*\d+", " ", core, flags=re.I)
        core = re.sub(r"[^a-z0-9]", "", core.lower())
        return re.sub(r"^the", "", core), (year.group(1) if year else None)
    g, gy = split(guess)
    t, ty = split(truth)
    if not g:
        return "missed"
    if gy and ty and gy != ty:
        return "WRONG"
    same = g == t or any(long_.startswith(short) and not long_[len(short):][:1].isdigit()
                         for short, long_ in ((g, t), (t, g)))
    if not same:
        # small word dropped from a cramped label (GONE60SECONDS) - but numbers must agree
        digits = lambda x: re.findall(r"\d+", x)
        same = (difflib.SequenceMatcher(None, g, t).ratio() >= 0.85 and digits(g) == digits(t))
    if not same:
        return "WRONG"
    # season / disc: right show but missing or different season/disc is only partial
    def tags(x):
        x = re.sub(r"\bseason\s*(\d+)", r"S\1", x or "", flags=re.I)
        x = re.sub(r"\bdis[ck]\s*(\d+)", r"D\1", x, flags=re.I)
        return set(re.findall(r"\b([SD])0*(\d+)\b", x))
    want, have = tags(truth), tags(guess)
    if want - have:
        return "WRONG" if (have - want) else "partial"
    return "ok"


def is_split_size(size):
    """DVD VOBs are split just under 1 GiB; those sizes recur on unrelated discs."""
    return 1073741824 - 2 * 1024 * 1024 <= size <= 1073741824


logger = logging.getLogger("disc_detective")
status_hook = None   # front ends show progress messages here (the app's status line); default: stderr


def log(msg):
    """A progress message: to the log file, and to the screen."""
    logger.info(msg)
    if status_hook:
        status_hook(msg)
    else:
        print(msg, file=sys.stderr, flush=True)


def log_dir():
    return os.path.join(data_home(), "logs")


def setup_logging(directory=None, level=logging.INFO):
    """disc_detective.log (everything) and errors.log (warnings and errors, with tracebacks),
    1 MB each with 3 old copies kept. Safe to call more than once."""
    d = directory or log_dir()
    os.makedirs(d, exist_ok=True)
    for h in list(logger.handlers):
        if getattr(h, "_disc_detective", False):
            logger.removeHandler(h)
            h.close()
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    for name, lvl in (("disc_detective.log", level), ("errors.log", logging.WARNING)):
        h = logging.handlers.RotatingFileHandler(os.path.join(d, name), maxBytes=1_000_000, backupCount=3,
                                                 encoding="utf-8", delay=True)
        h.setLevel(lvl)
        h.setFormatter(fmt)
        h._disc_detective = True
        logger.addHandler(h)
    logger.setLevel(min(level, logging.INFO))
    logger.propagate = False
    return d


def log_report(rep):
    """One line per scan in the log, plus anything that went wrong along the way."""
    logger.info("scan %s  %s  label=%s  -> %s  [%s; %s]", rep.get("content_hash", "?")[:8], rep.get("format"),
                rep.get("label") or "-", rep.get("best_guess") or "?", rep.get("identified_by") or "-",
                rep.get("status") or "-")
    for e in (rep.get("discdb") or {}).get("errors") or []:
        logger.warning("TheDiscDB: %s", e)
    for k in ("candidates_error", "imdb_note"):
        if rep.get(k):
            logger.warning("%s", rep[k])
    for e in rep.get("art_errors") or []:
        logger.warning("picture: %s", e)
    a = rep.get("art")
    if a:
        logger.info("picture: %s from %s%s", a.get("what"), a.get("source"), " (top guess)" if a.get("guess") else "")
    note = rep.get("art_note")
    if note and not rep.get("art"):
        (logger.warning if re.search(r"Pillow|index|offline", note) else logger.info)("%s", note)


GUESS_MIN = 45   # below this, the top candidate isn't worth acting on: the disc stays unidentified


def adopt_guess(c):
    """The candidate to go ahead with: a confident match, or else a reasonable top guess
    (names, rip plan and picture follow it; "Not right?" corrects it)."""
    if not c:
        return None
    if is_probable(c):
        return c[0]
    return c[0] if c[0].get("score", 0) >= GUESS_MIN else None


def strip_tags(text):
    """Your answer without its machine tags: "Hercules (1997) {imdb-tt0119282}" -> "Hercules (1997)"."""
    return " ".join(re.sub(r"\{imdb-tt\d+\}|\{films:[^}]*\}", " ", text or "").split())


def is_probable(c):
    return bool(c) and (len(c) == 1 or c[0]["score"] - c[1]["score"] >= 15) and c[0]["score"] >= 60


def status(rep, key):
    if rep.get("personal_hit"):
        return "identified (your library)"
    if rep["discdb"]["hit"]:
        return "identified (TheDiscDB)"
    c = rep.get("candidates") or []
    if is_probable(c):
        return "probable: %s (%s) [%s]" % (c[0]["name"], c[0]["year"], c[0].get("source", ""))
    if adopt_guess(c):
        return "best guess: %s (%s) [%s]; check it" % (c[0]["name"], c[0]["year"], c[0].get("source", ""))
    if c:
        return "candidates found; confirm one"
    if rep.get("imdb_note") and rep.get("search_queries"):
        return "unidentified; have search terms (%s)" % rep["imdb_note"]
    if rep.get("search_queries") and not c:
        return "unidentified; search terms found no confident match"
    sib = next((x for x in rep.get("siblings") or [] if x.get("known_as")), None)
    if sib:
        return "unidentified; looks like the same set as %s (next disc in the set?)" % sib["known_as"]
    if rep.get("cover") and rep["cover"].get("image"):
        return "unidentified; the cover art likely shows the title (save it with --out DIR)" if not rep["cover"].get("saved") \
            else "unidentified; cover art saved - the title is likely on it (%s)" % rep["cover"]["image"]
    return "unidentified; needs a label or manual search"


# --------------------------------------------------------------------------
# library: one SQLite file records every disc you scan
# --------------------------------------------------------------------------
# discs/titles are the shipped feature: your confirmed truths identify a disc
# instantly next time, and other discs in the same set are spotted as siblings.
# evidence (IFO/playlist/metadata/cover bytes) is a dev/test aid, stored only with
# --keep-evidence, so --corpus can re-parse every disc after a code change.

SAMPLE_FILE = "sample.json"          # format of a disc materialized for re-parsing
EVIDENCE_MAX_FILE = 5 * 1024 * 1024
LIBRARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS discs (
    hash TEXT PRIMARY KEY, format TEXT, label TEXT, mastered TEXT, provider TEXT, kind TEXT,
    stream_files TEXT, newest_mtime REAL, root_entries TEXT,
    status TEXT, best_guess TEXT, identified_by TEXT, imdb_id TEXT,
    truth TEXT, notes TEXT, first_seen TEXT, last_seen TEXT, tool_version TEXT);
CREATE TABLE IF NOT EXISTS titles (
    hash TEXT, title_id TEXT, role TEXT, duration REAL, chapters INT, episode_index INT,
    note TEXT, name TEXT, clips TEXT, PRIMARY KEY (hash, title_id));
CREATE TABLE IF NOT EXISTS evidence (hash TEXT, path TEXT, data BLOB, PRIMARY KEY (hash, path));
"""


def default_library():
    return env("DB") or os.path.join(data_home(), "library.sqlite")


def library_open(path):
    import sqlite3
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(LIBRARY_SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(titles)")}
    if "clips" not in cols:                       # libraries made before clips were recorded
        con.execute("ALTER TABLE titles ADD COLUMN clips TEXT")
    return con


def now_iso():
    return _dt.datetime.now().isoformat(timespec="seconds")


def load_sample(root):
    """A disc materialized from the library (or an old sample folder) for re-parsing."""
    path = os.path.join(root, SAMPLE_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def collect_evidence(root, fmt, base):
    """Small identifying files worth keeping for re-parsing (never video)."""
    found = []

    def grab(src_dir, rel_dir, patterns):
        if not src_dir or not os.path.isdir(src_dir):
            return
        for pat in patterns:
            for f in sorted(glob.glob(os.path.join(src_dir, pat))):
                if os.path.isfile(f) and os.path.getsize(f) <= EVIDENCE_MAX_FILE:
                    found.append((os.path.join(rel_dir, os.path.basename(f)).replace("\\", "/"), f))

    if fmt == "DVD":
        grab(base, "VIDEO_TS", ["*.IFO", "*.ifo"])
        grab(find_child(root, "JACKET_P"), "JACKET_P", ["*"])
    else:
        grab(base, "BDMV", ["index.bdmv", "MovieObject.bdmv"])
        grab(find_child(base, "PLAYLIST"), "BDMV/PLAYLIST", ["*.mpls", "*.MPLS"])
        grab(find_child(base, "CLIPINF"), "BDMV/CLIPINF", ["*.clpi", "*.CLPI"])
        jar = find_child(base, "JAR")             # authoring playlist names (PLsInfo.txt)
        for d in sorted(glob.glob(os.path.join(jar, "*"))) if jar else []:
            if os.path.isdir(d):
                grab(d, "BDMV/JAR/" + os.path.basename(d), ["PLsInfo.txt"])
        meta = find_child(base, "META")
        for sub in (os.listdir(meta) if meta else []):
            grab(os.path.join(meta, sub), "BDMV/META/" + sub, ["*"])
    seen, out = set(), []
    for rel, f in found:
        if rel.lower() not in seen:
            seen.add(rel.lower())
            out.append((rel, f))
    return out


def record_disc(con, rep, fp, root, fmt, base, keep_evidence):
    """Upsert the disc and its titles; never overwrites your truth/notes."""
    try:
        root_entries = sorted(os.listdir(root))
    except OSError:
        root_entries = []
    top = (rep.get("candidates") or [{}])[0] if is_probable(rep.get("candidates")) else {}
    row = {
        "hash": fp["hash"], "format": fmt, "label": rep.get("label"), "mastered": rep.get("mastered"),
        "provider": rep.get("provider"), "kind": rep.get("kind"),
        "stream_files": json.dumps([list(x) for x in fp["files"]]), "newest_mtime": fp.get("newest_mtime"),
        "root_entries": json.dumps(root_entries), "status": rep.get("status"),
        "best_guess": rep.get("best_guess"), "identified_by": rep.get("identified_by"),
        "imdb_id": top.get("imdb_id"), "now": now_iso(), "tool_version": VERSION,
    }
    with con:
        con.execute("""
            INSERT INTO discs (hash, format, label, mastered, provider, kind, stream_files, newest_mtime,
                               root_entries, status, best_guess, identified_by, imdb_id,
                               first_seen, last_seen, tool_version)
            VALUES (:hash, :format, :label, :mastered, :provider, :kind, :stream_files, :newest_mtime,
                    :root_entries, :status, :best_guess, :identified_by, :imdb_id, :now, :now, :tool_version)
            ON CONFLICT(hash) DO UPDATE SET
                format=excluded.format, label=COALESCE(excluded.label, discs.label),
                mastered=excluded.mastered, provider=excluded.provider, kind=excluded.kind,
                stream_files=excluded.stream_files, newest_mtime=excluded.newest_mtime,
                root_entries=excluded.root_entries, status=excluded.status,
                best_guess=excluded.best_guess, identified_by=excluded.identified_by,
                imdb_id=COALESCE(excluded.imdb_id, discs.imdb_id),
                last_seen=excluded.last_seen, tool_version=excluded.tool_version""", row)
        names = {}
        for n in rep.get("plex_names") or []:
            if " -> " in n:
                src, _, name = n.partition(" -> ")
                names[src.strip()] = name.strip()
        con.execute("DELETE FROM titles WHERE hash=?", (fp["hash"],))
        con.executemany("INSERT INTO titles (hash, title_id, role, duration, chapters, episode_index, note, "
                        "name, clips) VALUES (?,?,?,?,?,?,?,?,?)", [
            (fp["hash"], t["id"], t["role"], t["duration"], t["chapters"], t.get("episode_index"),
             t.get("note") or None, names.get(t["id"]), t.get("clip_list")) for t in rep.get("titles") or []])
        stored = 0
        if keep_evidence:
            for rel, f in collect_evidence(root, fmt, base):
                try:
                    data = read_file(f)
                except OSError:
                    continue
                con.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?)", (fp["hash"], rel, data))
                stored += 1
    return stored


def episodes_before(con, show, season, disc, own_hash):
    """How many episodes the earlier discs of this set hold, from your library: every disc
    1..disc-1 of the same show and season has to be there (confirmed or scanned), else None.
    A double-length episode counts twice, as on the disc itself."""
    if con is None or not show or not season or not disc or disc <= 1:
        return None
    found = {}
    for r in con.execute("SELECT hash, truth, best_guess FROM discs WHERE hash != ?", (own_hash,)):
        t = parse_truth(r["truth"] or r["best_guess"] or "")
        if t.get("disc") and t["disc"] < disc and (t.get("season") or season) == season \
                and t.get("title") and name_score(show, t["title"], False) >= 0.9:
            found.setdefault(t["disc"], r["hash"])
    if set(found) != set(range(1, disc)):
        return None
    total = 0
    for h in found.values():
        lens = [x[0] for x in con.execute("SELECT duration FROM titles WHERE hash=? AND role='episode'", (h,))]
        if not lens:
            return None
        typ = sorted(lens)[(len(lens) - 1) // 2]
        total += sum(2 if x >= 1.7 * typ else 1 for x in lens)
    return total


def personal_lookup(con, h):
    r = con.execute("SELECT truth, notes, last_seen FROM discs WHERE hash=? AND truth IS NOT NULL "
                    "AND truth != ''", (h,)).fetchone()
    return {"truth": r["truth"], "notes": r["notes"] or "", "saved": r["last_seen"]} if r else None


def find_siblings(con, fp, own_hash):
    """Discs sharing exact multi-MB file sizes (reused menus, logos, trailers) come from the
    same authoring family - usually the same box set. Same mastering day backs it up."""
    if con is None:
        return []
    mine = {sz for _, sz in fp["files"] if sz >= 1_000_000 and not is_split_size(sz)}
    day = lambda t: _dt.datetime.fromtimestamp(t, _dt.timezone.utc).date() if t else None
    my_day = day(fp.get("newest_mtime"))
    out = []
    for r in con.execute("SELECT hash, label, truth, best_guess, stream_files, newest_mtime FROM discs "
                         "WHERE hash != ?", (own_hash,)):
        theirs = {sz for _, sz in json.loads(r["stream_files"] or "[]")
                  if sz >= 1_000_000 and not is_split_size(sz)}
        shared = mine & theirs
        if not shared:
            continue
        same_day = bool(my_day and my_day == day(r["newest_mtime"]))
        if len(shared) >= 2 or same_day:
            out.append({"hash": r["hash"], "known_as": r["truth"] or r["best_guess"], "label": r["label"],
                        "confirmed": bool(r["truth"]),
                        "shared_files": len(shared), "same_mastering_day": same_day})
    out.sort(key=lambda x: (-x["shared_files"], not x["same_mastering_day"]))
    return out[:5]


def materialize(con, h, dest):
    """Rebuild a disc's evidence as a folder the parser can read (no video needed)."""
    d = con.execute("SELECT * FROM discs WHERE hash=?", (h,)).fetchone()
    os.makedirs(dest, exist_ok=True)
    for r in con.execute("SELECT path, data FROM evidence WHERE hash=?", (h,)):
        p = os.path.join(dest, *r["path"].split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(r["data"])
    info = {"format": d["format"], "content_hash": h, "label": d["label"], "truth": d["truth"],
            "stream_files": json.loads(d["stream_files"] or "[]"), "newest_mtime": d["newest_mtime"]}
    with open(os.path.join(dest, SAMPLE_FILE), "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    return dest


def resolve_hash(con, prefix):
    rows = con.execute("SELECT hash FROM discs WHERE hash LIKE ?", (prefix.upper() + "%",)).fetchall()
    if len(rows) != 1:
        raise SystemExit("%s matches %d discs in the library" % (prefix, len(rows)))
    return rows[0]["hash"]


def truth_for(cand, season=None, disc=None):
    """How a chosen match is written as your answer: "Title (Year)", "S1 D2" for TV, and the
    exact IMDb entry when known ("{imdb-tt0119282}"), so the right one of several is used."""
    t = "%s (%s)" % (cand["name"], cand["year"]) if cand.get("year") else cand["name"]
    if cand.get("kind") == "tv" or season:
        t += "".join(" %s%s" % (k, v) for k, v in (("S", season), ("D", disc)) if v)
    if cand.get("imdb_id"):
        t += " {imdb-%s}" % cand["imdb_id"]
    return t


def truth_for_collection(name, films):
    """A short-film disc as your answer: its name plus which title is which film.
    films: [(title_id, imdb_id)]."""
    name = strip_tags(name).replace("{", "(").replace("}", ")")
    return "%s {films:%s}" % (name, "|".join("%s=%s" % f for f in films))


def collection_from_ids(con, films, titles, name):
    """Rebuild a collection candidate from your answer's exact pairing (no search)."""
    byid = {t["id"]: t for t in titles}
    items = []
    for tid, iid in films:
        t, c = byid.get(tid), imdb_by_id(con, iid)
        if t and c:
            items.append({"title_id": tid, "imdb_id": c["imdb_id"], "name": c["name"], "year": c["year"],
                          "runtime": c.get("runtime"), "disc_minutes": round(t["duration"] / 60, 1)})
    if not items:
        return None
    matched = {it["title_id"]: byid[it["title_id"]] for it in items}
    versions, unmatched = [], []
    for t in sorted((t for t in titles if t.get("role") in ("episode", "version", "feature")), key=_title_num):
        if t["id"] in matched:
            continue
        twin = next((m for m in matched.values() if abs(m["duration"] - t["duration"]) <= 1.5), None)
        (versions.append({"title_id": t["id"], "same_as": twin["id"]}) if twin else unmatched.append(t["id"]))
    return {"kind": "collection", "imdb_id": None, "name": strip_tags(name), "year": min(i["year"] or 9999 for i in items),
            "type": "collection", "score": 100.0, "why": "your answer (exact IMDb entries)", "source": "IMDb",
            "items": items, "versions": versions, "unmatched": unmatched}


def imdb_by_id(con, imdb_id):
    """The candidate for one exact IMDb entry (an answer you picked)."""
    n = _int((imdb_id or "")[2:])
    r = con.execute("SELECT t.id, t.type, t.title, t.year, t.runtime, COALESCE(r.votes, 0) FROM titles t "
                    "LEFT JOIN ratings r ON r.id = t.id WHERE t.id = ?", (n,)).fetchone() if n else None
    if not r:
        return None
    return {"kind": "tv" if r[1] in IMDB_TV_TYPES else "movie", "imdb_id": tt(r[0]), "_n": r[0], "name": r[2],
            "year": r[3], "type": r[1], "runtime": r[4], "score": 100.0, "why": "your answer (exact IMDb entry)",
            "source": "IMDb"}


def imdb_lookup(con, text, limit=8):
    """Type-ahead for "Enter it myself": films, TV and shorts whose title matches what was typed,
    closest name first, then the best known."""
    text = (text or "").strip()
    if len(text) < 2:
        return []
    rows = imdb_search(con, text, True, IMDB_KEEP_TYPES + IMDB_SHORT_TYPES, limit=60)
    out = []
    for (n, ttype, title, year, end_year, runtime, votes) in rows:
        out.append({"kind": "tv" if ttype in IMDB_TV_TYPES else "movie", "imdb_id": tt(n), "name": title,
                    "year": year, "type": ttype, "runtime": runtime, "votes": votes,
                    "_rank": (name_score(text, title, True) >= 0.85, votes)})
    out.sort(key=lambda c: c["_rank"], reverse=True)
    for c in out:
        c.pop("_rank")
    return out[:limit]


def set_truth(db_path, h, truth):
    """Save (or with truth=None, forget) your answer for a disc. The disc must have been scanned."""
    con = library_open(db_path)
    try:
        with con:
            con.execute("UPDATE discs SET truth=? WHERE hash=?", (truth or None, h))
    finally:
        con.close()
    logger.info("answer for %s: %s", h[:8], truth or "(forgotten)")


def set_field(con, prefix, field, value):
    h = resolve_hash(con, prefix)
    with con:
        con.execute("UPDATE discs SET %s=? WHERE hash=?" % field, (value or None, h))
    print("%s %s = %r" % (h[:8], field, value))


def list_library(con):
    rows = con.execute("SELECT d.*, (SELECT COUNT(*) FROM evidence e WHERE e.hash=d.hash) AS ev "
                       "FROM discs d ORDER BY last_seen DESC").fetchall()
    print("%-8s  %-7s  %-24s  %-34s  %-7s  %s" % ("hash", "format", "label", "truth / best guess", "evid.", "last seen"))
    print("-" * 110)
    for r in rows:
        shown = ("* " + r["truth"]) if r["truth"] else (r["best_guess"] or "-")
        print("%-8s  %-7s  %-24s  %-34s  %-7s  %s" % (r["hash"][:8], r["format"], (r["label"] or "")[:24],
              shown[:34], r["ev"] or "-", (r["last_seen"] or "")[:16]))
    print('\n%d discs (* = confirmed truth). Set one with --set-truth HASH "Title (Year)"' % len(rows))


def import_samples(con, folder):
    """Bring old sample folders (sample.json + files) into the library."""
    n = 0
    for name in sorted(os.listdir(folder)):
        d = os.path.join(folder, name)
        smp = load_sample(d)
        if not smp:
            continue
        h = smp.get("content_hash") or content_hash(sz for _, sz in smp["stream_files"])
        with con:
            con.execute("""INSERT INTO discs (hash, format, label, stream_files, newest_mtime, root_entries,
                           best_guess, status, truth, notes, first_seen, last_seen, tool_version)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(hash) DO UPDATE SET truth=COALESCE(discs.truth, excluded.truth),
                           notes=COALESCE(discs.notes, excluded.notes), label=COALESCE(discs.label, excluded.label)""",
                        (h, smp["format"], smp.get("label"), json.dumps(smp["stream_files"]),
                         smp.get("newest_mtime"), json.dumps(smp.get("root_entries", [])),
                         smp.get("last_guess"), smp.get("last_status"), smp.get("truth"),
                         smp.get("notes") or None, smp.get("saved") or now_iso(), smp.get("saved") or now_iso(),
                         smp.get("tool_version")))
            for dirpath, _, files in os.walk(d):
                for fn in files:
                    if fn == SAMPLE_FILE:
                        continue
                    p = os.path.join(dirpath, fn)
                    rel = os.path.relpath(p, d).replace(os.sep, "/")
                    con.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?)", (h, rel, read_file(p)))
        n += 1
    print("imported %d discs from %s" % (n, folder))


def corpus_summary(con, args):
    """Re-parse every disc that has stored evidence and score it against its truth."""
    import tempfile
    # Discs without evidence still re-test the fingerprint, database and label steps.
    hashes = [r["hash"] for r in con.execute("SELECT hash FROM discs ORDER BY hash")]
    rows = []
    with tempfile.TemporaryDirectory(prefix="disc_corpus_") as tmp:
        for h in hashes:
            d = materialize(con, h, os.path.join(tmp, h))
            a = argparse.Namespace(**vars(args))
            a.label, a.no_record, a.keep_evidence, a.no_art = None, True, False, True
            a.tmp_out = getattr(a, "tmp_out", None) or os.path.join(tmp, h + "_out")
            try:
                rep = identify(d, a)
            except Exception as exc:
                rows.append((h[:8], "?", "", "error", str(exc)[:34], "", None, "ERROR", None))
                continue
            truth = rep.get("truth")
            guess = rep.get("best_guess") or ""
            verdict = truth_verdict(guess, truth) if truth else "-"
            if verdict == "WRONG" and "unconfirmed" in (rep.get("identified_by") or ""):
                verdict = "missed"          # it only echoed the label back; it never claimed an answer
            # notes "features: Title 1" (or "features: 00099.mpls, 00100.mpls"): what the rip plan
            # should treat as the film(s); checks the structure reading, not just the name
            notes = (con.execute("SELECT notes FROM discs WHERE hash=?", (h,)).fetchone() or [None])[0] or ""
            want = re.search(r"features?:\s*([^;\n]+)", notes, re.I)
            plan = None
            if want:
                # "features: A, B" (two films); "A|B" = either copy will do (same video)
                groups = [{y.strip() for y in x.split("|")} for x in want.group(1).split(",") if x.strip()]
                got = sorted(t["id"] for t in rep.get("titles") or [] if t["role"] == "feature")
                ok = len(got) == len(groups) and all(g & set(got) for g in groups)
                plan = "plan ok" if ok else "plan WRONG (%s)" % ", ".join(got)
            rows.append((h[:8], rep["format"], (rep.get("label") or "")[:22], rep["kind"],
                         guess[:34], rep["identified_by"], truth, verdict, plan))
    p = print
    p("%-8s  %-7s  %-22s  %-6s  %-34s  %-28s  %s" % ("hash", "format", "label", "kind", "best guess", "identified by", "truth check"))
    p("-" * 128)
    for r in rows:
        p("%-8s  %-7s  %-22s  %-6s  %-34s  %-28s  %s%s" % (r[0], r[1], r[2], r[3], r[4], r[5], r[7] if r[6] else "-",
                                                         ", " + r[8] if r[8] else ""))
    by = {}
    for r in rows:
        by[r[5]] = by.get(r[5], 0) + 1
    p("")
    p("%d discs. By signal: %s" % (len(rows), ", ".join("%s %d" % kv for kv in sorted(by.items(), key=lambda kv: -kv[1]))))
    scored = [r for r in rows if r[6]]
    if scored:
        p("Truth check: %d ok, %d partial, %d wrong, %d missed (of %d with a recorded truth)" % (
            sum(r[7] == "ok" for r in scored), sum(r[7] == "partial" for r in scored),
            sum(r[7] == "WRONG" for r in scored), sum(r[7] == "missed" for r in scored), len(scored)))
    planned = [r for r in rows if r[8]]
    if planned:
        p("Rip plan check: %d ok, %d wrong (of %d with expected features in their notes)" % (
            sum(r[8] == "plan ok" for r in planned), sum(r[8] != "plan ok" for r in planned), len(planned)))
    if not rows:
        p("The library is empty. Scan discs (with --keep-evidence for full re-parsing) to build a corpus.")


# --------------------------------------------------------------------------
# TheDiscDB submission packet
# --------------------------------------------------------------------------
# TheDiscDB has no submission API: contributions go through its web form (with its
# own TMDB search) and are reviewed. The packet makes that form quick: the MakeMKV
# log it asks for, every MakeMKV title pre-labeled, and a checklist for the parts
# that need the physical box (UPC, cover photos, release date).

MAKEMKV_CANDIDATES = [
    r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
    r"C:\Program Files (x86)\MakeMKV\makemkvcon.exe",
    r"C:\Program Files\MakeMKV\makemkvcon64.exe",
    r"C:\Program Files\MakeMKV\makemkvcon.exe",
    "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
    "/usr/bin/makemkvcon", "/usr/local/bin/makemkvcon",
]
DISCDB_TYPES = {"feature": "MainMovie", "episode": "Episode", "extra": "Extra", "version": "MainMovie"}


def find_makemkvcon():
    for name in ("makemkvcon64", "makemkvcon"):
        p = shutil.which(name)
        if p:
            return p
    return next((p for p in MAKEMKV_CANDIDATES if os.path.isfile(p)), None)


def run_makemkv(drive="disc:0"):
    exe = find_makemkvcon()
    if not exe:
        raise SystemExit("makemkvcon not found; pass --makemkv-log FILE with a saved log instead")
    log("running %s -r info %s (MakeMKV reads the disc; this can take a minute) ..." % (exe, drive))
    out = subprocess.run([exe, "-r", "--cache=1", "info", drive], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=900)
    if "TINFO:" not in out.stdout:
        raise SystemExit("MakeMKV returned no titles (is the disc in %s?)" % drive)
    return out.stdout


def parse_makemkv_log(text):
    """MakeMKV robot-mode output ('makemkvcon -r info') -> disc name + titles."""
    import csv
    disc, titles = {}, {}
    for line in text.splitlines():
        tag, _, rest = line.partition(":")
        if tag not in ("CINFO", "TINFO"):
            continue
        try:
            fields = next(csv.reader([rest]))
        except (csv.Error, StopIteration):
            continue
        if tag == "CINFO" and len(fields) >= 3:
            disc[int(fields[0])] = fields[2]
        elif tag == "TINFO" and len(fields) >= 4:
            t = titles.setdefault(int(fields[0]), {"index": int(fields[0])})
            t[int(fields[1])] = fields[3]
    out = []
    for i in sorted(titles):
        t = titles[i]
        dur = t.get(9, "")
        parts = [int(x) for x in dur.split(":")] if re.fullmatch(r"\d+:\d\d:\d\d", dur or "") else None
        out.append({
            "index": i, "name": t.get(2, ""), "chapters": _int(t.get(8)),
            "duration": parts[0] * 3600 + parts[1] * 60 + parts[2] if parts else None, "duration_text": dur,
            "size": t.get(10, ""), "bytes": _int(t.get(11)), "source": t.get(16, ""),
            "dvd_title": _int(t.get(24)), "segments": t.get(26, ""), "output": t.get(27, ""),
        })
    return {"disc_name": disc.get(2, ""), "volume": disc.get(32, ""), "titles": out}


def map_makemkv(mk_titles, our_titles, fmt):
    """Pair each MakeMKV title with our classified title: Blu-ray by source file,
    DVD by original title number, then by duration as a fallback."""
    ours = {t["title_id"]: t for t in our_titles}
    used, pairs = set(), {}
    for m in mk_titles:
        key = None
        if fmt == "Blu-ray" and m["source"]:
            if m["source"] in ours:
                key = m["source"]
            elif m["source"].lower().endswith(".m2ts"):
                clip = m["source"][:-5]
                single = [t for t in our_titles if (t.get("clips") or "") == clip
                          and t["role"] not in ("junk", "loop") and t["title_id"] not in used]
                key = single[0]["title_id"] if single else None
        elif fmt == "DVD" and m["dvd_title"]:
            key = "Title %d" % m["dvd_title"] if ("Title %d" % m["dvd_title"]) in ours else None
        if key and key not in used:
            pairs[m["index"]] = (ours[key], "source" if key == m["source"] or fmt == "DVD" else "clip")
            used.add(key)
    for m in mk_titles:
        if m["index"] in pairs or m["duration"] is None:
            continue
        best = None
        for t in our_titles:
            if t["title_id"] in used or t["role"] in ("junk", "loop", "duplicate", "play-all"):
                continue
            d = abs((t["duration"] or 0) - m["duration"])
            if d <= 2 and (best is None or d < best[0]):
                best = (d, t)
        if best:
            pairs[m["index"]] = (best[1], "duration")
            used.add(best[1]["title_id"])
    return pairs


def _episode_parts(name):
    m = re.search(r" - s(\d+)e(\d+) - (.+?)\.mkv$", name or "", re.I)
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else (None, None, None)


def build_packet(con, prefix, log_text, out_dir, log_name=None):
    h = resolve_hash(con, prefix)
    d = con.execute("SELECT * FROM discs WHERE hash=?", (h,)).fetchone()
    our = [dict(r) for r in con.execute("SELECT * FROM titles WHERE hash=?", (h,))]
    if not our:
        raise SystemExit("no titles recorded for %s yet - scan the disc once first" % h[:8])
    mk = parse_makemkv_log(log_text)
    if not mk["titles"]:
        raise SystemExit("the MakeMKV log has no titles")
    pairs = map_makemkv(mk["titles"], our, d["format"])
    truth = d["truth"] or d["best_guess"] or ""
    pt = parse_truth(truth)
    labels = []
    for m in mk["titles"]:
        t, how = pairs.get(m["index"], (None, None))
        role = t["role"] if t else None
        entry = {"makemkv_title": m["index"], "output_file": m["output"], "source": m["source"] or m["dvd_title"],
                 "duration": m["duration_text"], "chapters": m["chapters"], "size": m["size"],
                 "our_title": t["title_id"] if t else None, "role": role, "matched_by": how,
                 "type": DISCDB_TYPES.get(role), "title": None, "season": None, "episode": None, "note": ""}
        if role in ("feature", "version"):
            entry["title"] = pt["title"]
            if role == "version":
                entry["note"] = "alternate version of the main movie - label it or leave it out"
        elif role == "episode":
            s, e, name = _episode_parts(t.get("name"))
            entry.update(season=s, episode=e, title=name)
            if s is None:
                entry["note"] = "episode %s on this disc - number not confirmed" % t.get("episode_index")
        elif role == "extra":
            entry["note"] = "choose Extra / Featurette / DeletedScene / Trailer / Interview / Other"
        elif role in ("play-all", "duplicate", "junk", "loop"):
            entry["note"] = "leave unlabeled (%s)" % role
        elif not t and (m["duration"] or 0) < JUNK_MAX:
            entry["note"] = "leave unlabeled (short clip)"
        elif not t:
            entry["note"] = "no match in this disc's scan - check the log is from this disc"
        labels.append(entry)
    matched = sum(1 for l in labels if l["our_title"])
    real = [l for l, m in zip(labels, mk["titles"]) if (m["duration"] or 0) >= JUNK_MAX]
    real_matched = sum(1 for l in real if l["our_title"])
    os.makedirs(out_dir, exist_ok=True)
    disc_no = pt["disc"] or 1
    log_file = log_name or "disc%02d.txt" % disc_no
    with open(os.path.join(out_dir, log_file), "w", encoding="utf-8", newline="") as fh:
        fh.write(log_text)
    packet = {"content_hash": h, "format": d["format"], "label": d["label"], "truth": truth,
              "imdb_id": d["imdb_id"], "mastered": d["mastered"], "makemkv_disc_name": mk["disc_name"],
              "makemkv_log": log_file, "matched": matched, "makemkv_titles": len(labels),
              "real_titles": len(real), "real_matched": real_matched, "labels": labels}
    with open(os.path.join(out_dir, "labels.json"), "w", encoding="utf-8") as fh:
        json.dump(packet, fh, indent=2)
    with open(os.path.join(out_dir, "checklist.md"), "w", encoding="utf-8") as fh:
        fh.write(packet_checklist(packet, pt))
    return packet


def packet_checklist(pk, pt):
    L = []
    w = L.append
    w("# TheDiscDB submission: %s" % (pk["truth"] or "unconfirmed disc"))
    w("")
    w("Contribute at https://thediscdb.com/contribute (sign in with GitHub). Disc fingerprint `%s`." % pk["content_hash"])
    w("")
    w("## 1. Identify the release (their form searches TMDB for you)")
    year = " (%s)" % pt["year"] if pt["year"] else ""
    w("- Search for: **%s**%s%s" % (pt["title"] or "?", year,
                                   (" - IMDb `%s` to confirm the right one" % pk["imdb_id"]) if pk["imdb_id"] else ""))
    if pt["season"]:
        w("- Season %s, disc %s" % (pt["season"], pt["disc"] or "?"))
    w("")
    w("## 2. Release details (from the box)")
    w("- [ ] UPC: the barcode number on the back of the case")
    w("- [ ] ASIN: from the Amazon listing for this edition, if there is one")
    w("- [ ] Release date: street date of this edition%s" % (
        (" (disc mastered %s, so on or after that)" % pk["mastered"]) if pk["mastered"] else ""))
    w("- [ ] Cover images: photos or scans of the front (and back) of the case")
    w("")
    w("## 3. Disc log")
    w("- [ ] Upload `%s` (MakeMKV log, disc name \"%s\")" % (pk["makemkv_log"], pk["makemkv_disc_name"]))
    w("- Optional: their browser disc scan or `thediscdb-scan` adds a disc manifest")
    w("")
    w("## 4. Label the titles (%d of %d titles over 2 minutes matched to this disc's scan)" % (
        pk["real_matched"], pk["real_titles"]))
    if pk["real_titles"] and pk["real_matched"] < pk["real_titles"] / 2:
        w("")
        w("> **Warning:** fewer than half the titles matched. Make sure the log is from this disc.")
    w("")
    if any(l["role"] == "episode" for l in pk["labels"]):
        w("> Episode order follows the disc's play-all; episode *numbers* assume the same number of episodes")
        w("> on every disc of the season. Check them against the case before submitting.")
        w("")
    w("| MakeMKV | Output file | Source | Length | Label as | Title | S/E | Note |")
    w("|---|---|---|---|---|---|---|---|")
    for l in pk["labels"]:
        se = "S%02dE%02d" % (l["season"], l["episode"]) if l["season"] and l["episode"] else ""
        w("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            l["makemkv_title"], l["output_file"], l["source"] or "", l["duration"], l["type"] or "-",
            (l["title"] or "").replace("|", "/"), se, l["note"]))
    w("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def print_report(rep):
    p = print
    p("=" * 72)
    p("%s  [%s]" % (rep["path"], rep["format"]))
    p("STATUS: %s" % rep["status"])
    if rep.get("best_guess"):
        p("GUESS:  %s  [%s]" % (rep["best_guess"], rep["identified_by"]))
    p("=" * 72)
    p("Fingerprint   %s  (%d stream files)" % (rep["content_hash"], rep["stream_files"]))
    if rep.get("personal_hit"):
        ph = rep["personal_hit"]
        p("Your library  HIT: %s%s" % (ph["truth"], ("  (%s)" % ph["notes"]) if ph.get("notes") else ""))
    db = rep["discdb"]
    if db["hit"]:
        h = db["hit"]
        p("TheDiscDB     HIT via %s" % h["source"])
        p("              %s | %s | %s" % (h.get("title"), h.get("release"), h.get("disc")))
        named = [t for t in h["titles"] if (t.get("item") or {}).get("title")]
        for t in named[:25]:
            it = t["item"]
            se = " S%sE%s" % (it.get("season"), it.get("episode")) if it.get("episode") else ""
            p("                %-12s %8s  %-9s %s%s" % (t["source"], t["duration"], it.get("type") or "",
                                                     it.get("title"), se))
    else:
        p("TheDiscDB     not found")
        for n in db["near"]:
            p("              near match: %s (%d of %d large files identical)" % (n["path"], n["shared_large_files"], n["of"]))
    for e in db["errors"]:
        p("              note: %s" % e)

    p("")
    p("Disc facts")
    p("  label       %s" % (rep["label"] or "(unknown; pass --label)"))
    lp = rep.get("label_parsed")
    if lp:
        bits = ["query '%s'" % lp.get("query")]
        for k in ("season", "disc", "side", "year"):
            if lp.get(k):
                bits.append("%s %s" % (k, lp[k]))
        if lp.get("maybe_truncated"):
            bits.append("possibly truncated, prefix search")
        p("              -> " + ", ".join(bits))
    if rep.get("provider"):
        p("  provider    %s" % rep["provider"])
    if rep.get("bd_meta") and (rep["bd_meta"]["names"] or rep["bd_meta"]["toc"]):
        p("  BD meta     %s" % "; ".join(rep["bd_meta"]["names"][:2]))
        for num, name in rep["bd_meta"]["toc"][:12]:
            p("              title %s: %s" % (num, name))
    p("  mastered    %s" % (rep["mastered"] or "?"))
    f = rep["facts"]
    kind = rep["kind"]
    if kind == "tv":
        p("  looks like  TV: %d episodes of ~%d min" % (f.get("episode_count", 0), f.get("episode_minutes", 0)))
    elif kind == "movie":
        p("  looks like  movie: ~%d min feature" % f.get("feature_minutes", 0))
    else:
        p("  looks like  unknown")
    if f.get("commentary_flagged"):
        p("  commentary  flagged in audio track attributes")
    po = rep.get("playlist_obfuscation")
    if isinstance(po, dict):
        p("  protection  %d decoy titles; %s %s" % (po["decoys"], "using" if po["sure"] else "guessing", po["picked"]))
    elif po:
        p("  warning     many feature-length playlists: possible playlist obfuscation")
    if rep.get("cover"):
        c = rep["cover"]
        where = c.get("image") if c.get("saved") else "found (save it with --out DIR)"
        p("  cover art   %s%s" % (where or c.get("found"), (" (" + c["note"] + ")") if c.get("note") else ""))
    if rep.get("art"):
        a = rep["art"]
        p("  picture     %s from %s: %s" % (a["what"], a["source"], a["image"]))
        if a.get("credit"):
            p("              %s" % a["credit"])
    if not rep.get("art") and rep.get("art_note"):
        p("  picture     %s" % rep["art_note"])
    for e in rep.get("art_errors") or []:
        p("  picture     (%s)" % e)
    if rep.get("cover_ocr"):
        p("  cover OCR   %s" % rep["cover_ocr"])
    elif rep.get("cover") and rep["cover"].get("image"):
        p("  cover OCR   no confident text (stylized artwork); view the image")
    for sib in rep.get("siblings") or []:
        p("  sibling     %s (%s): %d identical asset file%s%s" % (
            sib["known_as"] or "unknown disc", sib["hash"][:8], sib["shared_files"],
            "" if sib["shared_files"] == 1 else "s", ", same mastering day" if sib["same_mastering_day"] else ""))
    if rep.get("structure_error"):
        p("  structure   could not parse: %s" % rep["structure_error"])

    shown = [t for t in rep["titles"] if t["role"] not in ("junk", "loop")]
    loops = [t for t in rep["titles"] if t["role"] == "loop"]
    junk = len(rep["titles"]) - len(shown)
    if shown:
        p("")
        p("Titles (%d shown, %d short menu/logo clips hidden%s)" % (
            len(shown), junk - len(loops), (", %d menu loops hidden" % len(loops)) if loops else ""))
        for t in sorted(shown, key=lambda t: ({"feature": 0, "episode": 1, "version": 2, "play-all": 3,
                                                "duplicate": 4, "extra": 5}.get(t["role"], 6),
                                               t.get("episode_index", 0), _natural(t["id"]))):
            tracks = ""
            if t["audio"]:
                tracks = " audio " + ",".join((a["lang"] or "?") + ("*" if a["kind"] == "commentary" else "")
                                              for a in t["audio"])
            role = "episode %d" % t["episode_index"] if t.get("episode_index") else t["role"]
            p("  %-10s %-14s %8s %3d ch  %-18s%s%s" % (role, t["id"], hms(t["duration"]), t["chapters"],
                                                       t.get("video") or "", tracks,
                                                       ("  - " + t["note"]) if t["note"] else ""))
    if rep.get("candidates"):
        p("")
        p("Candidates")
        for c in rep["candidates"]:
            ident = c.get("imdb_id") or ("tmdb-%s" % c["tmdb_id"] if c.get("tmdb_id") else "")
            p("  %5.1f  %-5s %s (%s) %s  [%s: %s]" % (c["score"], c["kind"], c["name"], c["year"], ident,
                                                    c.get("source", ""), c["why"]))
    if rep.get("episode_guess", {}).get("guess"):
        g = rep["episode_guess"]
        p("  episode guess: " + ", ".join(g["guess"]))
        p("  (%s)" % g["note"])
    elif rep.get("episode_guess", {}).get("note"):
        p("  episodes: " + rep["episode_guess"]["note"])
    elif rep.get("search_queries") and not rep.get("candidates") and not rep["discdb"]["hit"]:
        p("")
        p("Search terms: %s" % " | ".join(q for q in rep["search_queries"] if q))
    if rep.get("plex_names"):
        p("")
        p("Plex naming")
        for n in rep["plex_names"][:12]:
            p("  " + n)
    if rep.get("candidates_error"):
        p("  lookup error: %s" % rep["candidates_error"])
    if rep.get("recorded"):
        ev = (" with %d evidence files" % rep["evidence_stored"]) if rep.get("evidence_stored") else ""
        if rep.get("personal_hit"):
            p("Updated in your library%s (%s)" % (ev, rep.get("library_path")))
        else:
            p("Recorded in your library%s (%s)" % (ev, rep.get("library_path")))
            p("Once you know what it is: --set-truth %s \"Title (Year)\"  or  \"Show S1 D2\"" % rep["content_hash"][:8])
    p("")


def make_parser():
    ap = argparse.ArgumentParser(prog="disc-detective", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="disc root(s) or full-disc backup folder(s)")
    ap.add_argument("--db", default=default_library(),
                    help="library database (default ~/.disc_detective/library.sqlite)")
    ap.add_argument("--no-record", action="store_true", help="don't record this scan in the library")
    ap.add_argument("--keep-evidence", action="store_true",
                    help="dev/test: also store IFOs, playlists and cover art so --corpus can re-parse the disc")
    ap.add_argument("--corpus", action="store_true", help="dev/test: re-parse every disc with stored evidence and score it")
    ap.add_argument("--list", action="store_true", help="list the discs in the library")
    ap.add_argument("--set-truth", nargs=2, metavar=("HASH", "TRUTH"), help='confirm a disc, e.g. 3EBA9FFF "Psych S1 D2"')
    ap.add_argument("--set-notes", nargs=2, metavar=("HASH", "NOTES"), help="attach notes to a disc")
    ap.add_argument("--import-samples", metavar="DIR", help="import old sample folders into the library")
    ap.add_argument("--discdb-dir", help="where the downloaded TheDiscDB index lives (default ~/.disc_detective/discdb)")
    ap.add_argument("--discdb-update", action="store_true", help="re-download the TheDiscDB index now")
    ap.add_argument("--update-data", action="store_true", help="download/refresh IMDb and TheDiscDB data now and exit")
    ap.add_argument("--no-discdb-online", action="store_true",
                    help="don't query thediscdb.com; use the downloaded index only")
    ap.add_argument("--build-discdb-index", metavar="REPO",
                    help="publisher: build the slim index from a TheDiscDB/data checkout and exit")
    ap.add_argument("--index-out", default=DISCDB_INDEX_DB, help="output file for --build-discdb-index")
    ap.add_argument("--commit", help="TheDiscDB/data commit recorded in the index (--build-discdb-index)")
    ap.add_argument("--packet", metavar="HASH", help="build a TheDiscDB submission packet for a disc in the library")
    ap.add_argument("--makemkv-log", metavar="FILE", help="saved 'makemkvcon -r info disc:0' output for --packet")
    ap.add_argument("--makemkv-drive", default="disc:0", help="drive for MakeMKV when no log is given (default disc:0)")
    ap.add_argument("--packet-dir", metavar="DIR", help="where to write the packet (default thediscdb_<hash>)")
    ap.add_argument("--discdb-repo", default=env("DISCDB"),
                    help="local clone of github.com/TheDiscDb/data: offline lookup and posters "
                         "(or env DISC_DETECTIVE_DISCDB)")
    ap.add_argument("--offline", action="store_true", help="skip online lookups")
    ap.add_argument("--no-tvmaze", action="store_true", help="don't look up episode lists on TVmaze")
    ap.add_argument("--tvmaze-dir", help="where TVmaze answers are cached (default ~/.disc_detective/tvmaze)")
    ap.add_argument("--episode-order", choices=("aired", "dvd"), default="aired",
                    help="name episode files in aired order (Plex's default) or DVD order")
    ap.add_argument("--imdb-dir", default=default_imdb_dir(),
                    help="where IMDb datasets and the local index live (default ~/.disc_detective/imdb)")
    ap.add_argument("--imdb-update", action="store_true", help="re-download the IMDb datasets now")
    ap.add_argument("--no-imdb", action="store_true", help="don't use IMDb")
    ap.add_argument("--tmdb-key", help="optional TMDB API key, used when IMDb isn't confident (or env TMDB_API_KEY)")
    ap.add_argument("--label", help="override the volume label")
    ap.add_argument("--out", help="save extracted cover art to this folder")
    ap.add_argument("--no-cover", action="store_true", help="skip cover art extraction")
    ap.add_argument("--no-art", action="store_true", help="don't look up a poster / box art")
    ap.add_argument("--art-dir", help="artwork cache (default: 'art' beside the library)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a report")
    return ap


def default_args(**overrides):
    """Settings as the command line would set them, for other front ends (the GUI)."""
    args = make_parser().parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    args.db = os.path.abspath(args.db)
    return args


def makemkv_log_store(h):
    return os.path.join(os.path.dirname(default_library()), "makemkv_logs", "%s.txt" % h)


def main():
    ap = make_parser()
    args = ap.parse_args()
    if args.build_discdb_index:
        discdb_build_index(args.build_discdb_index, os.path.abspath(args.index_out), args.commit, log=log)
        return
    try:
        setup_logging()
    except OSError:
        pass                                  # read-only home: run without log files
    logger.info("disc-detective %s: %s", VERSION, " ".join(sys.argv[1:]))
    try:
        _main(args, ap)
    except SystemExit:
        raise
    except Exception:
        logger.exception("command failed")
        raise


def _main(args, ap):
    import tempfile
    if args.offline:
        args.tmdb_key = None
        os.environ.pop("TMDB_API_KEY", None)
    if args.update_data:
        for k, v in update_data(args, force=True, log=log).items():
            log("%s: %s" % (k, v))
        return
    args.db = os.path.abspath(args.db)
    args.library = library_open(args.db)
    try:
        if args.import_samples:
            import_samples(args.library, args.import_samples)
        if args.set_truth:
            set_field(args.library, args.set_truth[0], "truth", args.set_truth[1])
        if args.set_notes:
            set_field(args.library, args.set_notes[0], "notes", args.set_notes[1])
        if args.list:
            list_library(args.library)
        if args.packet:
            h = resolve_hash(args.library, args.packet)
            stored = makemkv_log_store(h)
            if args.makemkv_log:
                with open(args.makemkv_log, encoding="utf-8", errors="replace") as fh:
                    log_text = fh.read()
            elif os.path.exists(stored):
                log("using the MakeMKV log saved when this disc was ripped")
                with open(stored, encoding="utf-8", errors="replace") as fh:
                    log_text = fh.read()
            else:
                log_text = run_makemkv(args.makemkv_drive)
            out_dir = args.packet_dir or "thediscdb_%s" % h[:8]
            pk = build_packet(args.library, h, log_text, out_dir)
            print("Packet written to %s: %d of %d titles over 2 minutes matched to the scan" % (
                os.path.abspath(out_dir), pk["real_matched"], pk["real_titles"]))
            print("Open checklist.md and follow it on https://thediscdb.com/contribute")
        with tempfile.TemporaryDirectory(prefix="disc_detective_") as tmp:
            args.tmp_out = tmp
            if args.corpus:
                corpus_summary(args.library, args)
            reports = []
            for path in args.paths:
                a = argparse.Namespace(**vars(args))
                if len(args.paths) > 1 and args.out:
                    a.out = os.path.join(args.out, re.sub(r"[^\w.-]+", "_", path).strip("_") or "disc")
                a.tmp_out = os.path.join(tmp, str(len(reports)))
                rep = identify(path, a)
                reports.append(rep)
                if not args.json:
                    print_report(rep)
            if args.json and reports:
                print(json.dumps(reports if len(reports) > 1 else reports[0], indent=2, default=str))
        if not (args.paths or args.corpus or args.list or args.set_truth or args.set_notes
                or args.import_samples or args.packet):
            ap.error("give a disc path, or one of --list, --corpus, --set-truth, --import-samples, --packet")
    finally:
        args.library.close()


if __name__ == "__main__":
    main()
