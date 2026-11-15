"""The kinds of things the app sorts titles and menu lines into, as named values: a misspelt one fails
where it's written (Role.FETAURE) instead of quietly matching nothing ("featrue"), and the words shown
for them can change without breaking the code that tells them apart.

Each is a str with its old spelling as its value, so what's stored (case files, settings, TheDiscDB's
own types) reads and compares just as before: Role.PLAY_ALL == "play-all", and json.dumps writes
"play-all". What the interface calls them is elsewhere (app.TYPE_WORDS, core.ROLE_WORDS)."""
import enum


class _Named(str, enum.Enum):
    """A str enum written as its value everywhere ("%s", format, str), on every Python we support
    (3.11 changed how a str enum formats)."""
    __str__ = str.__str__
    __format__ = str.__format__


class Role(_Named):
    """What a scan says a title is."""
    FEATURE = "feature"          # the film
    VERSION = "version"          # another cut of the film
    LONGEST = "longest"          # the longest title, ripped when nothing on the disc was classified
    PART = "part"                # one of the clips a film plays back to back, with a title of its own
    EPISODE = "episode"
    EXTRA = "extra"
    PROMO = "promo"              # an ad for something else, or a logo or warning (label type Other)
    PLAY_ALL = "play-all"        # other titles played one after another
    SEGMENT = "segment"          # a piece of a seamlessly branched film (4K), with a playlist of its own
    DUPLICATE = "duplicate"      # the same video as another title
    DECOY = "decoy"              # a fake copy of the film (copy protection)
    LOOP = "loop"                # a menu's looping background
    JUNK = "junk"                # too short to be anything


class Type(_Named):
    """What you (or TheDiscDB) say a title is: TheDiscDB's types, and one of our own (PROMO)."""
    MAIN_MOVIE = "MainMovie"
    EPISODE = "Episode"
    EXTRA = "Extra"
    FEATURETTE = "Featurette"
    DELETED_SCENE = "DeletedScene"
    TRAILER = "Trailer"
    INTERVIEW = "Interview"
    SHORT = "Short"
    MUSIC = "Music"
    SCENE = "Scene"
    OTHER = "Other"              # logos, warnings, disclaimers
    PROMO = "Promo"              # Disc Detective's own: an ad for another release (TheDiscDB: Trailer)


class LineRole(_Named):
    """What a line of a menu page's words is (menus' reading of a page)."""
    HEADING = "heading"
    TITLE = "title"
    DESCRIPTION = "description"
    OTHER = "other"
