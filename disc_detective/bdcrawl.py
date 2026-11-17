"""A TV Blu-ray's titles from its menu's own files, nothing played (bdmenus.wb_menu: Warner's BD-J menus list
each episode, playlist and extra in plain text -- Arrow Season 1 Disc 1): the readings a menu crawl gives, each
saying exactly which episode it is.

(VLC's crawl of a Blu-ray's pop-up menu, pressing its buttons and watching where playback landed, was here till
2026-11-17: the hdmv-menus library reads HDMV menus from the disc's files instead -- bdhdmv -- Chuck's discs, its
last, better so. In git's history if it's ever wanted.)"""
import os

from . import bdmenus, i18n, menus
from .kinds import Type


def from_files(root, label, titles, log=print, read=None, out_dir=None):
    """A TV disc's titles from its menu's own files (bdmenus.wb_menu), nothing played: what crawl gives,
    its readings saying exactly which episode each is. None for a disc with no such files. read: a
    picture to its words, for the extras' names and to check the episodes' (RapidOCR's, by default).
    out_dir: each title's own button saved there, to look at in Edit titles."""
    if read is None:
        read = lambda image: " ".join(t for t, _, _ in (menus.rapid_lines(image) or []))
    menu = bdmenus.wb_menu(root, label, read)
    if not menu:
        return None
    lengths = {t["id"]: t.get("duration") for t in titles}
    readings = []
    plain = dict(page=["bd", 0, 0], rect=None, image=None, switch=False, only_cut=False, commentaries=False)

    def button_picture(reading, entry):
        """The reading with its button's picture, saved, and what it says on it."""
        image = menu["picture"](entry["button"]) if out_dir and entry.get("button") else None
        if image is not None:
            name = "button_%s.png" % reading["title"].replace(".", "_")
            try:
                os.makedirs(out_dir, exist_ok=True)
                image.save(os.path.join(out_dir, name))
            except OSError:
                return reading
            reading.update(image=name, rect=[0, 0, image.width, image.height], alone=True, on_button=entry.get("read") or "")
        return reading
    for e in menu["episodes"]:
        if e["playlist"] not in lengths:
            log(i18n.log("the menu's episode {number} plays {playlist}, not among the disc's titles", number=e["number"], playlist=e["playlist"]))
            continue
        readings.append(button_picture(dict(plain, title=e["playlist"], text=("%d) %s" % (e["number"], e["read"])).strip(), number=e["number"],
                                            seconds=lengths[e["playlist"]], bonus=False, kind=None, only_kind=False, place=[e["number"]]), e))
    if not readings:
        return None
    for i, x in enumerate(menu["extras"]):
        if x["playlist"] not in lengths:
            continue
        kind = menus.heading_kind(x["under"]) or menus.heading_kind(x["read"])
        if x["of_episode"] is not None:         # (an episode's own: its scissor button's)
            kind = kind or Type.DELETED_SCENE
            text = {Type.DELETED_SCENE: "Deleted Scenes"}.get(kind) or x["read"] or x["under"]
        else:
            text = x["read"] or x["under"]
        if text:
            readings.append(button_picture(dict(plain, title=x["playlist"], text=text, seconds=lengths[x["playlist"]], bonus=True, kind=kind,
                                                only_kind=x["of_episode"] is not None, of_episode=x["of_episode"], place=[100 + i]), x))
    log(i18n.log("the disc's menu files list {count} of its titles, nothing played", count=len(readings)))
    return {"tv": {"readings": readings, "exact": True, "season": menu["season"], "disc": menu["disc"], "played": [r["title"] for r in readings],
                   "promos": [], "promo_said": {}, "play_alls": [],
                   "opening": [], "commentary_versions": []}, "pages": [], "found": {}, "suspects": len(readings), "watch": {}}
