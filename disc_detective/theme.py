"""The look: a Baker Street study. Parchment and brass, ink-dark text, Prussian blue for the
main action, sealing-wax red for case numbers (tools/ui has the design it follows).

Tk can't do gradients, soft shadows or rounded corners, so those become flat colours and
crisp 1px borders. Fonts ship with the app (OFL; see fonts/OFL-*.txt) and are loaded for
this process only: nothing is installed on the computer. "Detective Serif" is Playfair
Display, cut down to Latin and renamed as its licence asks of modified versions."""
import os
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")

# palette (names from the prototype's Tailwind config)
VELLUM_50 = "#fdfbf7"
VELLUM_100 = "#faf7f0"
VELLUM_200 = "#f4efe4"
VELLUM_300 = "#eae2d0"
PARCHMENT = "#f7f4ea"
CARD = "#fffdf9"
BRASS_200 = "#ece1c4"
BRASS_300 = "#decb9c"
BRASS_400 = "#c8ad69"
BRASS_600 = "#996515"
BRASS_700 = "#754b0e"
BRASS_800 = "#533409"
PRUSSIAN_600 = "#234a7d"
PRUSSIAN_700 = "#1b365d"
PRUSSIAN_800 = "#152a48"
WAX = "#8b0000"
INK = "#2c241b"
INK_LIGHT = "#594d3f"
INK_MUTED = "#7d6e5d"
INK_SUBTLE = "#a59784"
GREEN = "#065f46"
GREEN_DOT = "#059669"
AMBER_DOT = "#b8860b"
MAHOGANY = "#3a271d"
TITLE_TEXT = "#f5ebd7"

SERIF, MONO, SANS = "Georgia", "Courier New", "Segoe UI"     # replaced by the bundled fonts once loaded


def load_fonts(root):
    """Make the bundled fonts usable in this process, then pick each family (falling back to
    common system fonts where loading isn't possible)."""
    global SERIF, MONO, SANS
    files = [os.path.join(FONT_DIR, f) for f in sorted(os.listdir(FONT_DIR)) if f.lower().endswith(".ttf")] \
        if os.path.isdir(FONT_DIR) else []
    try:
        if os.name == "nt":
            import ctypes
            for f in files:
                ctypes.windll.gdi32.AddFontResourceExW(f, 0x10, 0)    # FR_PRIVATE: this process only
        elif sys.platform != "darwin":
            import ctypes
            import ctypes.util
            lib = ctypes.util.find_library("fontconfig")
            if lib:
                fc = ctypes.CDLL(lib)
                for f in files:
                    fc.FcConfigAppFontAddFile(None, f.encode())       # this process's font list only
    except Exception:
        pass
    have = {f.lower() for f in tkfont.families(root)}

    def pick(*names):
        return next((n for n in names if n.lower() in have), names[-1])
    SERIF = pick("Detective Serif", "Playfair Display", "Georgia", "Times New Roman", "serif")
    MONO = pick("Courier Prime", "Courier New", "Courier")
    SANS = pick("Inter", "Segoe UI", "Helvetica")


def ctypes_fontconfig_available():
    try:
        import ctypes.util
        return sys.platform != "darwin" and os.name != "nt" and bool(ctypes.util.find_library("fontconfig"))
    except Exception:
        return False


def color_title_bar(root):
    """Windows 11: tint the native title bar mahogany (older Windows just keeps its own)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()

        def rgb(h):
            r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
            return ctypes.c_int(r | (g << 8) | (b << 16))
        for attr, colour in ((35, MAHOGANY), (36, TITLE_TEXT), (34, BRASS_600)):   # caption, text, border
            c = rgb(colour)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c), ctypes.sizeof(c))
    except Exception:
        pass


_IMAGES = []          # Tk drops images that nothing references
_ROUND_STYLES = {}
ROUND = True          # rounded corners drawn with Pillow; off (square borders) without it

# ---- high-DPI screens ----
# With the app DPI-aware (Windows stops stretching it, so text is sharp), Tk draws pixels as
# pixels: fonts follow the screen (they're in points) but spacing given in pixels would shrink.
# SCALE is the screen's size over 96 DPI (1.5 at 150%); distances the app gives are scaled by it
# in one place, Tk's option handling. A size worked out from real pixels (a window's width) is
# marked real() so it isn't scaled twice.
SCALE = 1.0
SCALED_OPTIONS = {"padx", "pady", "ipadx", "ipady", "padding", "wraplength", "length", "minsize", "pad"}


class real(int):
    """A distance already in screen pixels (from winfo_width and the like): left as it is."""


def px(n):
    """A distance in the design's pixels (96 DPI) as screen pixels."""
    return real(round(n * SCALE)) if SCALE != 1.0 else n


def _scaled(v):
    if isinstance(v, real) or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return px(v)
    if isinstance(v, (tuple, list)):
        return type(v)(_scaled(x) for x in v)
    if isinstance(v, str) and v.replace(" ", "").isdigit():
        return " ".join(str(px(int(x))) for x in v.split())
    return v


_PATCHED = False


def _install_scaling():
    """Scale the distance options of every widget, pack, grid and place call (once per process)."""
    global _PATCHED
    if _PATCHED:
        return
    original = tk.Misc._options

    def _options(self, cnf, kw=None):
        if SCALE != 1.0:
            cnf = tk._cnfmerge((cnf, kw)) if kw else tk._cnfmerge(cnf)
            cnf = {k: (_scaled(v) if k.rstrip("_") in SCALED_OPTIONS else v) for k, v in cnf.items()}
            return original(self, cnf)
        return original(self, cnf, kw)
    tk.Misc._options = _options
    _PATCHED = True


def _rounded(w, h, r, fill, border=None, bw=1, shadow=0, outer=None, corners="all"):
    """A PhotoImage of a rounded rectangle, anti-aliased (drawn at 4x and shrunk): fill and a
    1px border, an optional soft shadow under it, corners square on one side for split buttons
    (corners "left" / "right" = which side is rounded). Outside it: `outer`, or transparent."""
    key = (w, h, r, fill, border, bw, shadow, outer, corners)
    if key not in _PNG_CACHE:            # drawn once per run; each Tk gets its own PhotoImage of it
        _PNG_CACHE[key] = _draw_rounded(w, h, r, fill, border, bw, shadow, outer, corners)
    ph = tk.PhotoImage(data=_PNG_CACHE[key], format="png")
    _IMAGES.append(ph)
    return ph


_PNG_CACHE = {}


def _draw_rounded(w, h, r, fill, border, bw, shadow, outer, corners):
    import base64
    import io
    from PIL import Image, ImageDraw, ImageFilter
    k = 4
    W, H = (w + 2 * shadow) * k, (h + 2 * shadow) * k
    img = Image.new("RGBA", (W, H), outer + "ff" if outer else (0, 0, 0, 0))

    def rect(d, box, rad, colour):
        d.rounded_rectangle(box, rad, fill=colour)
        x0, y0, x1, y1 = box
        if corners == "left":                    # square right-hand corners
            d.rectangle((x1 - rad, y0, x1, y1), fill=colour)
        elif corners == "right":
            d.rectangle((x0, y0, x0 + rad, y1), fill=colour)
    if shadow:
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rect(ImageDraw.Draw(sh), (shadow * k, shadow * k + k, W - shadow * k - 1, H - shadow * k + k - 1),
             r * k, (44, 36, 27, 46))
        sh = sh.filter(ImageFilter.GaussianBlur(shadow * k / 2.2))
        img = Image.alpha_composite(img, sh)
    d = ImageDraw.Draw(img)
    box = (shadow * k, shadow * k, W - shadow * k - 1, H - shadow * k - 1)
    if border:
        rect(d, box, r * k, border)
        box = (box[0] + bw * k, box[1] + bw * k, box[2] - bw * k, box[3] - bw * k)
    rect(d, box, max(0, (r - bw)) * k, fill)
    img = img.resize((w + 2 * shadow, h + 2 * shadow), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _check_img(checked, disabled=False, size=14):
    """A rounded check box: empty, or brass with a light tick in it."""
    import base64
    import io
    from PIL import Image, ImageDraw
    size = px(size)
    k = 4 * size / 14.0                   # the drawing is laid out for 14px; scaled with it
    S = int(size * 4)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    edge = INK_SUBTLE if disabled else (BRASS_700 if checked else BRASS_400)
    fill = (VELLUM_200 if disabled else BRASS_600) if checked else CARD
    d.rounded_rectangle((0, 0, S - 1, S - 1), 3 * k, fill=edge)
    d.rounded_rectangle((k, k, S - 1 - k, S - 1 - k), 2 * k, fill=fill)
    k = int(round(k))
    if checked:
        d.line([(S * 0.27, S * 0.52), (S * 0.43, S * 0.68), (S * 0.74, S * 0.34)], fill=VELLUM_50 if not disabled else INK_SUBTLE,
               width=int(1.8 * k), joint="curve")
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    ph = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
    _IMAGES.append(ph)
    return ph


def checkbox_style(st):
    """Check boxes with a tick, not clam's cross (a cross reads as "off")."""
    off, on = _check_img(False), _check_img(True)
    off_d, on_d = _check_img(False, True), _check_img(True, True)
    st.element_create("Tick.indicator", "image", off, ("disabled", "selected", on_d), ("disabled", off_d),
                      ("selected", on), sticky="")
    st.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Tick.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Checkbutton.label", {"sticky": "nswe"})]})]})])
    st.configure("TCheckbutton", padding=_scaled((0, 2)), indicatormargin=0)
    st.map("TCheckbutton", background=[("active", VELLUM_100)])


def round_frame_style(bg, border=None, radius=6, shadow=0, outer=PARCHMENT):
    """A ttk frame style whose background is a stretched rounded-rectangle image (9-slice:
    the corners keep their size, the edges and middle stretch)."""
    key = (bg, border, radius, shadow, outer)
    if key in _ROUND_STYLES:
        return _ROUND_STYLES[key]
    st = ttk.Style()
    n = "R%d" % len(_ROUND_STYLES)
    radius, shadow = px(radius), px(shadow)  # drawn at the screen's size
    size = 2 * (radius + shadow) + 240     # a big middle: the stretch tiles it, and each tile is a draw
    img = _rounded(size, size, radius, bg, border, shadow=shadow)
    edge = radius + shadow + 2
    st.element_create(n + ".bg", "image", img, border=edge, sticky="nsew", width=2 * edge, height=2 * edge)
    st.layout(n + ".TFrame", [(n + ".bg", {"sticky": "nsew"})])
    st.configure(n + ".TFrame", background=outer)
    _ROUND_STYLES[key] = (n + ".TFrame", real(shadow + (1 if border else 0) + max(2, radius // 3)))
    return _ROUND_STYLES[key]


def round_button(name, bg, fg, border, active, pressed, disabled_bg, disabled_fg, font, padding, radius=5,
                 corners="all", disabled_border=None, outer=VELLUM_100):
    """A ttk button style drawn from rounded images, one per state. `outer`: the colour it sits
    on, which shows past the rounded corners."""
    st = ttk.Style()
    radius = px(radius)
    padding = _scaled(padding)
    size = 2 * radius + 120          # (as for the frames: fewer tiles to draw)
    imgs = [_rounded(size, size, radius, c, b, corners=corners) for c, b in
            ((bg, border), (active, border), (pressed, border), (disabled_bg, disabled_border or disabled_bg))]
    el = name.replace(".", "_") + ".border"
    st.element_create(el, "image", imgs[0], ("disabled", imgs[3]), ("pressed", imgs[2]), ("active", imgs[1]),
                      border=radius + 1, sticky="nsew", width=2 * radius + 2, height=2 * radius + 2)
    st.layout(name, [(el, {"sticky": "nsew", "children": [
        ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    st.configure(name, foreground=fg, font=font, padding=padding, focuscolor=bg, background=outer)
    st.map(name, foreground=[("disabled", disabled_fg)], background=[("disabled", outer), ("active", outer),
                                                                     ("pressed", outer)])


def setup(root):
    """Fonts, then every ttk style the app and its dialogs use."""
    global SCALE
    _IMAGES.clear()                  # images and styles belong to one Tk: a new one starts afresh
    _ROUND_STYLES.clear()
    _ICONS.clear()
    try:
        SCALE = round(max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72)), 2)
    except (tk.TclError, ValueError):
        SCALE = 1.0
    if SCALE < 1.05:
        SCALE = 1.0
    _install_scaling()
    load_fonts(root)
    st = ttk.Style(root)
    st.theme_use("clam")
    root.configure(background=PARCHMENT)
    for opt, val in (("*Menu.background", CARD), ("*Menu.foreground", INK), ("*Menu.activeBackground", BRASS_200),
                     ("*Menu.activeForeground", INK), ("*Menu.font", (SANS, 9)), ("*Toplevel.background", VELLUM_100),
                     ("*Text.background", CARD), ("*Text.foreground", INK), ("*Text.selectBackground", BRASS_300),
                     ("*TCombobox*Listbox.background", CARD)):
        root.option_add(opt, val)
    base = (SANS, 9)
    st.configure(".", background=VELLUM_100, foreground=INK, font=base, bordercolor=BRASS_400,
                 lightcolor=VELLUM_100, darkcolor=VELLUM_100, troughcolor=VELLUM_300, focuscolor=BRASS_600,
                 selectbackground=BRASS_300, selectforeground=INK, fieldbackground=CARD, insertcolor=INK)
    st.configure("TFrame", background=VELLUM_100)
    st.configure("TLabel", background=VELLUM_100, foreground=INK)
    st.configure("TCheckbutton", background=VELLUM_100, foreground=INK)
    st.map("TCheckbutton", background=[("active", VELLUM_100)], indicatorcolor=[("selected", BRASS_600), ("!selected", CARD)])
    st.configure("TRadiobutton", background=VELLUM_100, foreground=INK)
    st.map("TRadiobutton", background=[("active", VELLUM_100)], indicatorcolor=[("selected", BRASS_600), ("!selected", CARD)])
    st.configure("TEntry", fieldbackground=CARD, bordercolor=BRASS_400, lightcolor=CARD, darkcolor=CARD, padding=_scaled(3))
    st.map("TEntry", bordercolor=[("focus", BRASS_600)], lightcolor=[("focus", BRASS_200)])
    st.configure("TScrollbar", background=BRASS_300, troughcolor=VELLUM_200, bordercolor=VELLUM_200,
                 arrowcolor=BRASS_700, lightcolor=BRASS_300, darkcolor=BRASS_300, arrowsize=px(14))
    st.configure("Horizontal.TProgressbar", background=BRASS_600, troughcolor=VELLUM_300, bordercolor=BRASS_400,
                 lightcolor=BRASS_600, darkcolor=BRASS_600, thickness=6)
    st.configure("TNotebook", background=VELLUM_100, bordercolor=BRASS_300, lightcolor=VELLUM_100,
                 darkcolor=VELLUM_100, tabmargins=_scaled((0, 4, 0, 0)))   # room above for the selected tab to grow
    st.configure("TNotebook.Tab", background=VELLUM_200, foreground=INK_MUTED, bordercolor=BRASS_300,
                 lightcolor=VELLUM_200, darkcolor=VELLUM_200, padding=_scaled((14, 5)), font=(MONO, 9, "bold"))
    st.map("TNotebook.Tab", background=[("selected", VELLUM_100), ("active", VELLUM_50)],
           foreground=[("selected", BRASS_800), ("active", INK)],
           lightcolor=[("selected", VELLUM_100)],
           padding=[("selected", _scaled((14, 5)))],          # clam shrinks the selected tab; keep it the same size
           expand=[("selected", _scaled((0, 4, 0, 0)))])     # then grow it upwards only, so no tab edge moves sideways
    st.configure("TCombobox", fieldbackground=CARD, background=VELLUM_100, bordercolor=BRASS_400,
                 lightcolor=CARD, darkcolor=CARD, arrowcolor=BRASS_700, padding=_scaled(2), arrowsize=px(12))
    st.map("TCombobox", fieldbackground=[("readonly", CARD)], background=[("active", VELLUM_200)],
           selectbackground=[("readonly", CARD)], selectforeground=[("readonly", INK)],
           bordercolor=[("focus", BRASS_600)])
    st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK, bordercolor=BRASS_400,
                 lightcolor=CARD, darkcolor=CARD)
    st.map("Treeview", background=[("selected", BRASS_200)], foreground=[("selected", INK)])
    st.configure("Picks.Treeview", rowheight=px(66))

    def button(name, bg, fg, border, active, font, padding, pressed=None):
        st.configure(name, background=bg, foreground=fg, bordercolor=border, lightcolor=bg, darkcolor=bg,
                     font=font, padding=_scaled(padding), focusthickness=0, focuscolor=bg, relief="solid", borderwidth=1)
        st.map(name, background=[("disabled", VELLUM_200), ("pressed", pressed or active), ("active", active)],
               foreground=[("disabled", INK_SUBTLE)], lightcolor=[("active", active)], darkcolor=[("active", active)],
               bordercolor=[("disabled", BRASS_300)])
    button("TButton", VELLUM_50, INK, BRASS_400, VELLUM_200, (SANS, 9), (10, 3))
    button("Small.TButton", VELLUM_50, INK, BRASS_400, "#ffffff", (MONO, 9), (8, 2))
    button("Scan.TButton", "#fcfaf5", INK, BRASS_400, VELLUM_200, (SERIF, 11, "bold"), (16, 7))
    button("ScanArrow.TButton", "#fcfaf5", BRASS_700, BRASS_400, VELLUM_200, (SANS, 10), (6, 7))
    button("Rip.TButton", PRUSSIAN_700, VELLUM_50, PRUSSIAN_800, PRUSSIAN_600, (SERIF, 10, "bold"), (14, 5))
    button("RipArrow.TButton", PRUSSIAN_700, BRASS_200, PRUSSIAN_800, PRUSSIAN_600, (SANS, 9), (5, 5))
    for name in ("Rip.TButton", "RipArrow.TButton"):
        st.map(name, background=[("disabled", "#8193ad"), ("active", PRUSSIAN_600)], foreground=[("disabled", "#dde3ec")],
               bordercolor=[("disabled", "#8193ad")], lightcolor=[("disabled", "#8193ad"), ("active", PRUSSIAN_600)],
               darkcolor=[("disabled", "#8193ad"), ("active", PRUSSIAN_600)])

    global ROUND
    try:
        import PIL  # noqa: F401
        ROUND = True
    except ImportError:
        ROUND = False
    if ROUND:
        round_button("TButton", VELLUM_50, INK, BRASS_400, VELLUM_200, BRASS_200, VELLUM_200, INK_SUBTLE,
                     (SANS, 9), (8, 1))
        round_button("Small.TButton", VELLUM_50, INK, BRASS_400, "#ffffff", BRASS_200, VELLUM_200, INK_SUBTLE,
                     (MONO, 9), (6, 0), radius=4, outer=CARD)
        round_button("Scan.TButton", "#fdfbf6", INK, BRASS_400, VELLUM_200, BRASS_200, VELLUM_200, INK_SUBTLE,
                     (SERIF, 11, "bold"), (10, 1), radius=6, outer=VELLUM_200)
        round_button("Pill.TButton", "#fffdf8", PRUSSIAN_700, BRASS_400, "#ffffff", BRASS_200, VELLUM_200, INK_SUBTLE,
                     (MONO, 9, "bold"), (6, 1), radius=6, outer=VELLUM_200)
        round_button("Rip.TButton", PRUSSIAN_700, VELLUM_50, PRUSSIAN_800, PRUSSIAN_600, PRUSSIAN_800, "#8193ad",
                     "#dde3ec", (SERIF, 10, "bold"), (10, 2), radius=5, corners="left", outer=CARD)
        round_button("RipArrow.TButton", PRUSSIAN_700, BRASS_200, PRUSSIAN_800, PRUSSIAN_600, PRUSSIAN_800, "#8193ad",
                     "#dde3ec", (SANS, 9), (2, 2), radius=5, corners="right", outer=CARD)
        checkbox_style(st)
    else:
        button("Pill.TButton", "#fffdf8", PRUSSIAN_700, BRASS_400, "#ffffff", (MONO, 9, "bold"), (10, 4))

    def label(name, bg=VELLUM_100, fg=INK, font=base, **kw):
        if "padding" in kw:
            kw["padding"] = _scaled(kw["padding"])
        st.configure(name, background=bg, foreground=fg, font=font, **kw)
    # dialogs (shared names the dialogs already use)
    label("Plan.TLabel", font=(SERIF, 11, "bold"))
    label("Sub.TLabel", fg=INK_MUTED)
    label("Cap.TLabel", fg=INK_MUTED, font=(MONO, 8))
    label("Detail.TLabel", font=(MONO, 8))
    label("Title.TLabel", font=(SERIF, 15, "bold"))
    label("Info.TLabel", font=(SANS, 10))
    label("Link.TLabel", fg=PRUSSIAN_700, font=(MONO, 9, "bold", "underline"))
    # the main window's parchment desk
    for prefix, bg in (("Desk", PARCHMENT), ("Card", CARD), ("Bar", VELLUM_200), ("Foot", VELLUM_300), ("Inset", VELLUM_100)):
        st.configure("%s.TFrame" % prefix, background=bg)
        label("%s.TLabel" % prefix, bg=bg)
        label("%sSub.TLabel" % prefix, bg=bg, fg=INK_MUTED)
        label("%sCap.TLabel" % prefix, bg=bg, fg=INK_MUTED, font=(MONO, 8, "bold"))
        label("%sLink.TLabel" % prefix, bg=bg, fg=PRUSSIAN_700, font=(MONO, 8, "bold", "underline"))
        label("%sMono.TLabel" % prefix, bg=bg, fg=INK, font=(MONO, 9))
        label("%sMonoBold.TLabel" % prefix, bg=bg, fg=INK, font=(MONO, 9, "bold"))
        label("%sKey.TLabel" % prefix, bg=bg, fg=INK_MUTED, font=(SERIF, 10, "italic"))
        label("%sDot.TLabel" % prefix, bg=bg, fg=BRASS_400, font=(MONO, 9))
    label("Banner.TLabel", bg=PARCHMENT, fg=BRASS_700, font=(MONO, 8, "bold"))
    label("BannerNote.TLabel", bg=PARCHMENT, fg=INK_MUTED, font=(SERIF, 8, "italic"))
    label("Case.TLabel", bg=PARCHMENT, fg=WAX, font=(MONO, 10))
    label("Name.TLabel", bg=PARCHMENT, fg=INK, font=(SERIF, 21, "bold"))
    label("Year.TLabel", bg=PARCHMENT, fg=INK_MUTED, font=(SANS, 16))
    label("Kind.TLabel", bg=PARCHMENT, fg=BRASS_700, font=(MONO, 9, "bold"))
    label("Fact.TLabel", bg=PARCHMENT, fg=INK_LIGHT, font=(MONO, 9))
    label("Source.TLabel", bg=PARCHMENT, fg=PRUSSIAN_700, font=(MONO, 9, "italic"))
    label("Guess.TLabel", bg=PARCHMENT, fg=WAX, font=(MONO, 9, "italic"))
    label("DeskSub.TLabel", bg=PARCHMENT, fg=INK_MUTED, font=(SANS, 9))
    label("Poster.TLabel", bg=CARD)
    label("PosterCap.TLabel", bg=BRASS_200, fg=INK_MUTED, font=(MONO, 8))
    label("StripCap.TLabel", bg=PARCHMENT, fg=INK_MUTED, font=(MONO, 8))
    label("CardStrip.TLabel", bg=CARD, fg=INK_MUTED, font=(MONO, 8))
    label("PosterBadge.TLabel", bg=VELLUM_50, fg=BRASS_800, font=(MONO, 7, "bold"), padding=(4, 2, 6, 2))
    label("Ok.TLabel", bg=CARD, fg=GREEN, font=(SANS, 9, "bold"))
    label("Warn.TLabel", bg=CARD, fg=WAX, font=(SANS, 9, "bold"))
    label("Head.TLabel", bg=CARD, fg=INK, font=(SERIF, 11, "bold"))
    label("HeadTime.TLabel", bg=CARD, fg=BRASS_700, font=(MONO, 10))
    label("Badge.TLabel", bg=VELLUM_200, fg=BRASS_800, font=(MONO, 9), padding=(5, 0))
    label("Path.TLabel", bg=VELLUM_50, fg=INK, font=(MONO, 9))
    label("InsetVal.TLabel", bg=VELLUM_100, fg=INK, font=(SERIF, 10, "bold"))
    label("InsetOk.TLabel", bg=VELLUM_100, fg=GREEN, font=(SANS, 9, "bold"))
    label("InsetWarn.TLabel", bg=VELLUM_100, fg=WAX, font=(SANS, 9, "bold"))
    st.configure("Pill.TFrame", background="#fffdf8")
    label("PillKey.TLabel", bg="#fffdf8", fg=INK_MUTED, font=(MONO, 8))
    label("PillVal.TLabel", bg="#fffdf8", fg=PRUSSIAN_700, font=(MONO, 9, "bold"))
    label("PillSize.TLabel", bg="#fffdf8", fg=BRASS_700, font=(MONO, 9, "bold"))
    label("PillSep.TLabel", bg="#fffdf8", fg=BRASS_400, font=(MONO, 9))
    label("FootStatus.TLabel", bg=VELLUM_300, fg=INK, font=(MONO, 9, "bold"))
    label("BarStatus.TLabel", bg=VELLUM_200, fg=INK, font=(MONO, 9))
    label("BarPct.TLabel", bg=VELLUM_200, fg=BRASS_700, font=(MONO, 9, "bold"))
    label("FootLink.TLabel", bg=VELLUM_300, fg=INK, font=(MONO, 8))
    label("FootSep.TLabel", bg=VELLUM_300, fg=BRASS_400, font=(MONO, 8))
    return st


def frame(parent, bg, border=None, pad=0, thick=1, radius=6, shadow=0, outer=PARCHMENT):
    """A panel: a rounded rectangle (a ttk frame with an image background) where Pillow can
    draw one, else a plain Tk frame with a 1px coloured border. `outer` is the colour around it
    (it shows past the rounded corners); pad is the room inside, past the border."""
    if ROUND and radius:
        style, edge = round_frame_style(bg, border, radius, shadow, outer)
        return ttk.Frame(parent, style=style, padding=real(edge + px(pad)))
    return tk.Frame(parent, background=bg, highlightthickness=thick if border else 0,
                    highlightbackground=border or bg, highlightcolor=border or bg, padx=pad, pady=pad)


def rule(parent, colour=BRASS_300):
    return tk.Frame(parent, background=colour, height=1)


def dot(parent, bg, colour, size=8):
    """A small round status light."""
    size = px(size)
    c = tk.Canvas(parent, width=size, height=size, background=bg, highlightthickness=0, bd=0)
    c.create_oval(0, 0, size - 1, size - 1, fill=colour, outline=colour)
    return c


def dot_image(colour, bg, size=8):
    """A small round light as an image (for a button), anti-aliased where Pillow is there."""
    size = px(size)
    if ROUND:
        return _rounded(size, size, size / 2, colour, outer=bg)
    img = tk.PhotoImage(width=size, height=size)
    img.put(colour, to=(1, 1, size - 1, size - 1))
    _IMAGES.append(img)
    return img


def set_dot(c, colour):
    """Light a dot in one colour (a pulse on it stops)."""
    stop_pulse(c)
    c.itemconfigure(1, fill=colour, outline=colour)


def _blend(a, b, t):
    """The colour t of the way from a to b ("#rrggbb")."""
    ca, cb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


def pulse_dot(c, colour, toward, period=1600, step=50):
    """Breathe a dot between colour and toward (its background, say): something's under way.
    Runs until set_dot (or stop_pulse) lights it plainly again."""
    import math
    if getattr(c, "_pulse", None) and c._pulse_colours == (colour, toward):
        return                                   # already breathing so
    stop_pulse(c)
    c._pulse_colours = (colour, toward)
    t0 = time.time()

    def tick():
        if not c.winfo_exists():
            return
        t = (1 - math.cos(2 * math.pi * ((time.time() - t0) * 1000 % period) / period)) / 2 * 0.75
        shade = _blend(colour, toward, t)
        c.itemconfigure(1, fill=shade, outline=shade)
        c._pulse = c.after(step, tick)
    tick()


def stop_pulse(c):
    job = getattr(c, "_pulse", None)
    if job:
        c.after_cancel(job)
    c._pulse = None


def shield(parent, bg, size=16):
    """A small shield with a check in it (the "verified" stamp); set_shield turns it plain."""
    size = px(size)
    w, h = size - 2, size
    c = tk.Canvas(parent, width=w, height=h, background=bg, highlightthickness=0, bd=0)
    c.create_polygon(w / 2, 1, w - 1.5, 3, w - 1.5, h * 0.52, w / 2, h - 1, 1.5, h * 0.52, 1.5, 3,
                     fill=bg, outline=BRASS_700, width=1.4, tags="body")
    c.create_line(w * 0.3, h * 0.5, w * 0.46, h * 0.66, w * 0.72, h * 0.34, fill=BRASS_700, width=1.8,
                  capstyle="round", joinstyle="round", tags="check")
    return c


def set_shield(c, verified):
    c.itemconfigure("check", state="normal" if verified else "hidden")
    c.itemconfigure("body", outline=BRASS_700 if verified else INK_SUBTLE)


ICON_DIR = os.path.join(HERE, "icons")
_ICONS = {}


def _png_photo(path, size):
    """An image file as a PhotoImage resized (Pillow) to `size` px square, or None."""
    import base64
    import io
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGBA").resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
    except Exception:
        return None


def scaled_photo(path):
    """A picture file (a poster, box art) at the screen's size: the file's pixels are the
    design's, so on a high-DPI screen it's enlarged by SCALE (Pillow), else shown as it is."""
    if SCALE != 1.0:
        import base64
        import io
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("RGBA")
                im = im.resize((round(im.width * SCALE), round(im.height * SCALE)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
        except Exception:
            pass
    return tk.PhotoImage(file=path)


GAUGE_DARK, GAUGE_BRIGHT = "#6b430c", "#f0c24e"     # the fill: dark brass, warming to gold at its head


def _photo(img):
    import base64
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")


class Gauge(tk.Canvas):
    """A progress bar in the study's brass: a rounded trough, its fill dark at the start and
    bright at its head; "indeterminate", a bright run slides along it. Takes what the app asks
    of a ttk.Progressbar (configure value / mode / maximum, start, stop)."""

    def __init__(self, parent, bg, height=10, length=200, mode="determinate", maximum=100, value=0, follow=None):
        """follow: called with fraction() (None while indeterminate) whenever that changes (a
        percentage beside the bar, say)."""
        self._h = px(height)
        super().__init__(parent, height=self._h, width=px(length), background=bg, highlightthickness=0, bd=0)
        self._bg, self._mode, self._max, self._value = bg, mode, float(maximum), float(value)
        self._follow, self._told = follow, object()
        self._job = None
        self._pos = 0.0
        self._imgs = {}
        self._trough = self.create_image(0, 0, anchor="nw")
        self._fill = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", lambda e: self._draw())

    # ---- ttk.Progressbar's calls ----
    def configure(self, cnf=None, **kw):
        kw = dict(cnf or {}, **kw)
        if "mode" in kw:
            self._mode = kw.pop("mode")
        if "maximum" in kw:
            self._max = float(kw.pop("maximum")) or 1.0
        if "value" in kw:
            self._value = float(kw.pop("value"))
        if kw:
            super().configure(**kw)
        self._draw()
        if self._follow and self.fraction() != self._told:
            self._told = self.fraction()
            self._follow(self._told)
    config = configure

    def cget(self, key):
        return {"mode": self._mode, "maximum": self._max, "value": self._value}.get(key) if key in (
            "mode", "maximum", "value") else super().cget(key)

    def fraction(self):
        return max(0.0, min(1.0, self._value / self._max)) if self._mode == "determinate" else None

    SWEEP = 2.5           # seconds for the indeterminate run to cross the trough

    def start(self, interval=12):
        """Set the run sliding (interval: ttk's, ignored: it moves by the clock, SWEEP a pass)."""
        self.stop()
        t0 = time.time() - self._pos * self.SWEEP

        def tick():
            self._pos = ((time.time() - t0) / self.SWEEP) % 1.0
            self._draw()
            self._job = self.after(20, tick)
        tick()

    def stop(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None

    # ---- drawing ----
    def _img(self, key, make):
        if key not in self._imgs:
            if len(self._imgs) > 40:              # (widths come and go as the window resizes)
                self._imgs.clear()
            self._imgs[key] = _photo(make())
        return self._imgs[key]

    def _pill_mask(self, w, h):
        """A pill's shape (anti-aliased ends) as an L-mode mask."""
        from PIL import Image, ImageDraw
        k = 4
        end = Image.new("L", (h * k, h * k), 0)
        ImageDraw.Draw(end).ellipse((0, 0, h * k - 1, h * k - 1), fill=255)
        end = end.resize((h, h), Image.LANCZOS)
        mask = Image.new("L", (w, h), 255)
        r = h // 2
        mask.paste(end.crop((0, 0, r, h)), (0, 0))
        mask.paste(end.crop((h - r, 0, h, h)), (w - r, 0))
        return mask

    def _make_trough(self, w, h):
        from PIL import Image
        img = Image.new("RGBA", (w, h), self._bg)
        edge = Image.new("RGBA", (w, h), BRASS_300)
        img.paste(edge, (0, 0), self._pill_mask(w, h))
        inner = Image.new("RGBA", (w - 2, h - 2), VELLUM_300)
        img.paste(inner, (1, 1), self._pill_mask(w - 2, h - 2))
        return img

    def _make_fill(self, w, h):
        """The fill, w wide: dark brass to gold along it, a soft sheen across its top half."""
        from PIL import Image, ImageOps
        ramp = Image.linear_gradient("L").rotate(90, expand=True)         # dark at the left, bright at the right
        if ramp.getpixel((0, 128)) > ramp.getpixel((255, 128)):
            ramp = ramp.transpose(Image.FLIP_LEFT_RIGHT)
        body = ImageOps.colorize(ramp.resize((w, h)), GAUGE_DARK, GAUGE_BRIGHT).convert("RGBA")
        sheen = Image.linear_gradient("L").resize((w, h)).point(lambda v: max(0, 70 - v))   # top: lighter
        body = Image.composite(Image.new("RGBA", (w, h), "#fff6dc"), body, sheen)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(body, (0, 0), self._pill_mask(w, h))
        return out

    def _draw(self):
        w, h = self.winfo_width(), self._h
        if w < h * 2:
            return
        self.itemconfigure(self._trough, image=self._img(("trough", w, h), lambda: self._make_trough(w, h)))
        inset, fh = 2, h - 4
        room = w - 2 * inset
        if self._mode == "determinate":
            fw = round(room * self.fraction())
            x = inset
        else:                                     # a run a quarter as long, sliding through and round:
            run = max(fh * 2, room // 4)          # it grows out of the left end, shrinks into the right
            start = inset - run + round((room + run) * self._pos)
            x = max(inset, start)
            fw = min(start + run, inset + room) - x
        if fw < fh:                               # too short to be a pill: nothing yet
            self.itemconfigure(self._fill, state="hidden")
            return
        img = self._img(("fill", fw, fh), lambda: self._make_fill(fw, fh))
        self.itemconfigure(self._fill, image=img, state="normal")
        self.coords(self._fill, x, inset)


class ScrollBar(tk.Canvas):
    """A scrollbar in the study's colours, as modern apps have them: a thin rounded pill, no
    arrows and no trough; pale brass at rest, so it keeps out of the way, full brass and a little
    wider under the pointer or while it's dragged. Takes what the app asks of a ttk.Scrollbar
    (command, set, get), so it drops in for one; the wheel over it scrolls what it scrolls."""

    THIN, WIDE, GAP, LEAST = 7, 10, 3, 28    # px: at rest, under the pointer, either side, shortest

    def __init__(self, parent, orient="vertical", command=None, bg=None):
        self._bg = bg or ttk.Style().lookup("TFrame", "background") or VELLUM_100
        self._v = orient == "vertical"
        side = px(self.WIDE + 2 * self.GAP)
        super().__init__(parent, background=self._bg, highlightthickness=0, bd=0,
                         **({"width": side} if self._v else {"height": side}))
        self._cmd = command
        self._lo, self._hi = 0.0, 1.0
        self._hot, self._grab, self._acc = False, None, 0.0
        self._imgs = {}
        self._rest, self._lit = self._resting(self._bg), BRASS_600
        self._thumb = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(seq, self._wheel)

    # ---- ttk.Scrollbar's calls ----
    def set(self, lo, hi):
        self._lo, self._hi = float(lo), float(hi)
        self._draw()

    def get(self):
        return self._lo, self._hi

    def configure(self, cnf=None, **kw):
        kw = dict(cnf or {}, **kw)
        if "command" in kw:
            self._cmd = kw.pop("command")
        if kw:
            super().configure(**kw)
    config = configure

    def recolour(self, bg):
        """On another background (the desk's parchment): its resting brass mixed to suit."""
        self._bg, self._rest = bg, self._resting(bg)
        super().configure(background=bg)
        self._imgs.clear()
        self._draw()

    def _resting(self, bg):
        """The thumb at rest: brass, three quarters of the way from the background (any colour
        Tk knows, a system one included)."""
        r, g, b = (v >> 8 for v in self.winfo_rgb(bg))
        return _blend("#%02x%02x%02x" % (r, g, b), BRASS_400, 0.75)

    # ---- where the thumb is ----
    def _length(self):
        return self.winfo_height() if self._v else self.winfo_width()

    def _span(self):
        """(start, length) of the thumb along the bar, in pixels; None: nothing to scroll."""
        room = self._length() - 2 * px(self.GAP)
        share = self._hi - self._lo
        if room <= 0 or share >= 1.0:
            return None
        n = max(min(px(self.LEAST), room), round(room * share))
        start = round((room - n) * self._lo / (1.0 - share)) if share < 1.0 else 0
        return px(self.GAP) + max(0, min(start, room - n)), n

    # ---- the pointer ----
    def _hover(self, on):
        self._hot = on
        self._draw()

    def _at(self, e):
        return e.y if self._v else e.x

    def _press(self, e):
        span = self._span()
        if not span or not self._cmd:
            return
        start, n = span
        if start <= self._at(e) <= start + n:
            self._grab = self._at(e) - start            # dragged by where it was taken hold of
            self._draw()
        else:                                           # the bar beside it: a page that way
            self._cmd("scroll", -1 if self._at(e) < start else 1, "pages")

    def _move(self, e):
        span = self._span()
        if self._grab is None or not span or not self._cmd:
            return
        room = self._length() - 2 * px(self.GAP) - span[1]
        if room > 0:
            frac = (self._at(e) - self._grab - px(self.GAP)) / room
            self._cmd("moveto", max(0.0, min(1.0, frac)) * (1.0 - (self._hi - self._lo)))

    def _release(self, e):
        self._grab = None
        self._hot = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        self._draw()

    def _wheel(self, e):
        if not self._cmd:
            return "break"
        if getattr(e, "num", None) in (4, 5):
            units = -3 if e.num == 4 else 3
        else:
            self._acc += -float(getattr(e, "delta", 0) or 0) / 40.0   # 120 a notch: 3 rows
            units = int(self._acc)
            self._acc -= units
        if units:
            self._cmd("scroll", units, "units")
        return "break"

    # ---- drawing ----
    def _pill(self, w, h, colour):
        """A pill w by h (anti-aliased: drawn four times over and shrunk)."""
        from PIL import Image, ImageDraw
        k = 4
        img = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((0, 0, w * k - 1, h * k - 1), min(w, h) * k // 2, fill=colour)
        return img.resize((w, h), Image.LANCZOS)

    def _draw(self):
        span = self._span()
        if not span:
            self.itemconfigure(self._thumb, state="hidden")
            return
        start, n = span
        lit = self._hot or self._grab is not None
        t = px(self.WIDE if lit else self.THIN)
        colour = self._lit if lit else self._rest
        w, h = (t, n) if self._v else (n, t)
        key = (w, h, colour)
        if key not in self._imgs:
            if len(self._imgs) > 40:              # (lengths come and go as windows resize)
                self._imgs.clear()
            self._imgs[key] = _photo(self._pill(w, h, colour))
        across = (self.winfo_width() if self._v else self.winfo_height()) - t
        x, y = (across // 2, start) if self._v else (start, across // 2)
        self.itemconfigure(self._thumb, image=self._imgs[key], state="normal")
        self.coords(self._thumb, x, y)


ART_DIR = os.path.join(HERE, "art")


def watermark(name, height, bg=PARCHMENT):
    """The desk's faint corner art ("right": the magnifier) `height` screen pixels tall, laid on the desk's colour (Tk has no see-through widgets: a label of it over the
    bare parchment looks the same). None without Pillow, or the file."""
    import base64
    import io
    try:
        from PIL import Image
        with Image.open(os.path.join(ART_DIR, "watermark_%s.png" % name)) as im:
            im = im.convert("RGBA")
            im = im.resize((max(1, round(im.width * height / im.height)), max(1, height)), Image.LANCZOS)
        out = Image.new("RGBA", im.size, bg)
        out.alpha_composite(im)
        buf = io.BytesIO()
        out.convert("RGB").save(buf, "PNG")
        return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
    except Exception:
        return None


def icon(name):
    """A small icon (PNG; Tk 8.6 reads PNG and GIF itself): the @2x one on a high-DPI screen."""
    if name in _ICONS:
        return _ICONS[name]
    img = None
    try:
        big = os.path.join(ICON_DIR, name + "@2x.png")
        if SCALE != 1.0 and os.path.exists(big):
            # a high-DPI screen: the double-size icon, brought to the screen's size (14px x SCALE)
            img = _png_photo(big, px(14))
        if img is None:
            for f in ((name + "@2x.png") if SCALE >= 1.9 else None, name + ".png"):
                if f and os.path.exists(os.path.join(ICON_DIR, f)):
                    img = tk.PhotoImage(file=os.path.join(ICON_DIR, f))
                    break
    except (tk.TclError, ValueError):
        img = None
    _ICONS[name] = img
    return img


def _mark(kind):
    """A 12px tick, dot or blank for the themed menus (radio and check entries)."""
    key = "_mark_" + kind
    if key in _ICONS:
        return _ICONS[key]
    import base64
    import io
    n = px(12)
    try:
        from PIL import Image, ImageDraw
        k, S = 4, n * 4
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if kind == "tick":
            d.line([(S * 0.18, S * 0.52), (S * 0.4, S * 0.74), (S * 0.82, S * 0.28)], fill=BRASS_700,
                   width=int(1.8 * k * n / 12), joint="curve")
        elif kind == "dot":
            d.ellipse((S * 0.3, S * 0.3, S * 0.7, S * 0.7), fill=BRASS_700)
        img = img.resize((n, n), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        ph = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
    except Exception:
        ph = tk.PhotoImage(width=n, height=n)
        if kind != "blank":
            ph.put(BRASS_700, to=(4, 4, 8, 8))
    _ICONS[key] = ph
    return ph


def icon_tick():
    return _mark("tick")


def icon_dot():
    return _mark("dot")


def icon_blank():
    return _mark("blank")


def button_image(name):
    """An icon for a ttk button, kept as it is when the button is disabled (Tk would grey it
    with a dither pattern)."""
    img = icon(name)
    return (img, "disabled", img) if img else None


def link(parent, text, command, style="DeskLink.TLabel", icon=None, right=False):
    """Clickable text (underlined, or the style's look), optionally with an icon before it
    (right=True: after it)."""
    img = globals()["icon"](icon) if icon else None
    lbl = ttk.Label(parent, text=text, style=style, cursor="hand2",
                    **({"image": img, "compound": ("right" if right else "left") if text else "image"} if img else {}))
    lbl.bind("<Button-1>", lambda e: command())
    return lbl


class Tooltip:
    """A small note that appears under a widget while the pointer rests on it.
    `text` is a function, so the note can change (or be empty, meaning no note)."""

    def __init__(self, widget, text, delay=450):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip = self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, event=None):
        self.hide()
        self.job = self.widget.after(self.delay, self.show)

    def show(self):
        self.job = None
        text = self.text()
        if not text or self.tip:
            return
        w = self.widget
        tip = self.tip = tk.Toplevel(w)
        tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        box = tk.Frame(tip, bg=BRASS_600, padx=1, pady=1)
        box.pack()
        tk.Label(box, text=text, bg=VELLUM_50, fg=INK, font=(MONO, 9), padx=6, pady=3,
                 justify="left", wraplength=real(max(px(300), w.winfo_screenwidth() // 2))).pack()
        tip.update_idletasks()
        x = w.winfo_rootx()
        y = w.winfo_rooty() + w.winfo_height() + 4
        x = max(0, min(x, w.winfo_screenwidth() - tip.winfo_reqwidth() - 8))
        tip.wm_geometry("+%d+%d" % (x, y))

    def hide(self, event=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


_BUNDLED = {"detective serif": "DetectiveSerif", "inter": "Inter", "courier prime": "CourierPrime"}


def cap_height(font):
    """How tall capital letters and figures are in a Tk font, in pixels: read from the bundled
    font file where Pillow is there, else a usual share of the font's ascent."""
    from tkinter import font as tkfont
    f = font if isinstance(font, tkfont.Font) else tkfont.Font(font=font)
    a = f.actual()
    size = a["size"]
    px = -size if size < 0 else size * float(f._tk.call("tk", "scaling"))
    base = _BUNDLED.get(str(a["family"]).lower())
    if base:
        style = "Bold" if a["weight"] == "bold" else ("Italic" if a["slant"] == "italic" else "Regular")
        path = os.path.join(FONT_DIR, "%s-%s.ttf" % (base, style))
        try:
            from PIL import ImageFont
            box = ImageFont.truetype(path, max(1, round(px))).getbbox("H", anchor="ls")
            return -box[1]
        except Exception:
            pass
    return round(f.metrics("ascent") * 0.7)


def caret(colour=BRASS_700, width=0, dot=None, height=0, gap=0):
    """A small drop-down chevron (the same everywhere, unlike the ▾ character, which the bundled
    fonts don't have). With `width`: an image that wide, chevron at the right and an optional
    `dot` light at the left, for a button whose text sits centred over it (compound="center")."""
    key = ("_caret", colour, width, dot, height, gap)
    if key in _ICONS:
        return _ICONS[key]
    import base64
    import io
    scale = max(1.0, float(tk._default_root.tk.call("tk", "scaling")) / 1.3333) if tk._default_root else 1.0
    cw, ch = round(9 * scale), round(5 * scale)           # the chevron itself
    W, H = max(width, cw + 2 + round(gap * scale)), max(height, ch + 4)
    try:
        from PIL import Image, ImageDraw
        k = 4
        img = Image.new("RGBA", (W * k, H * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x0, y0 = (W - cw - 1) * k, (H - ch) / 2 * k
        d.line([(x0, y0), (x0 + cw * k / 2, y0 + ch * k), (x0 + cw * k, y0)], fill=colour,
               width=max(4, round(1.5 * scale * k)), joint="curve")
        if dot:
            s = round(8 * scale)
            d.ellipse((0, (H - s) / 2 * k, s * k, (H + s) / 2 * k), fill=dot)
        img = img.resize((W, H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        ph = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
    except Exception:
        ph = tk.PhotoImage(width=W, height=H)
        for i in range(ch):
            ph.put(colour, to=(W - cw - 1 + i, (H - ch) // 2 + i, W - 1 - i, (H - ch) // 2 + i + 1))
    _ICONS[key] = ph
    return ph


def caret_image(colour=BRASS_700, **kw):
    """caret() for a ttk button, kept as it is when the button is disabled."""
    img = caret(colour, **kw)
    return (img, "disabled", img)


def check(colour=GREEN, size=12, gap=3):
    """A crisp drawn tick (Heroicons' check) to sit before a label's text (compound="left"); the
    ✓ character isn't in the bundled fonts, so Windows borrows a rough one from another font."""
    key = ("_check", colour, size, gap)
    if key in _ICONS:
        return _ICONS[key]
    import base64
    import io
    scale = max(1.0, float(tk._default_root.tk.call("tk", "scaling")) / 1.3333) if tk._default_root else 1.0
    s, g = round(size * scale), round(gap * scale)
    try:
        from PIL import Image, ImageDraw
        k = 4
        img = Image.new("RGBA", ((s + g) * k, s * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        u = s * k / 24.0                         # the 24-unit Heroicons grid
        pts = [(4 * u, 14 * u), (9 * u, 19 * u), (20 * u, 8 * u)]     # a touch low, to sit on the text's middle
        d.line(pts, fill=colour, width=max(4, round(3.6 * u)), joint="curve")
        r = 1.8 * u                              # round the ends, as stroke-linecap="round"
        for x, y in (pts[0], pts[2]):
            d.ellipse((x - r, y - r, x + r, y + r), fill=colour)
        img = img.resize((s + g, s), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        ph = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"), format="png")
    except Exception:
        ph = tk.PhotoImage(width=s + g, height=s)
        for i in range(3):
            ph.put(colour, to=(2 + i, s // 2 + i, 4 + i, s // 2 + i + 2))
        for i in range(6):
            ph.put(colour, to=(5 + i, s // 2 + 2 - i, 7 + i, s // 2 + 4 - i))
    _ICONS[key] = ph
    return ph
