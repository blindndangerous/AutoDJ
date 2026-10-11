"""Repeat rules: which recently heard tracks, artists and titles hold back a pick.

Three rules decide whether a candidate may play yet:

- **Track**: the exact file played recently.
- **Artist**: any artist credited on the candidate was credited on a recent
  track, or is related to one (see :class:`RelatedArtists`).  The album
  rule rides along with it, as it always has.
- **Title**: the candidate's song title, after :func:`title_key` folds away
  case, punctuation, accents and version notes such as "(Live)", matches a
  recent title.  So "Jingle Bells", "Jingle Bells (Live)" and
  "JINGLE BELLS - Remastered 2009" are one song.

Each rule looks back a number of tracks, a number of minutes, or both;
a candidate is held back when either one catches it.

Splitting artist credits
------------------------
:class:`ArtistSplitter` turns one credit such as
"Calvin Harris feat. Rihanna" into the artists it names.  The full credit
always counts as one artist.  Then:

1. ``feat.``, ``ft.``, ``featuring``, ``vs.``, ``versus``, a semicolon and a
   NUL (the separator of multi-value tags) always split.  A "(feat. X)" or
   "(with X)" note in the artist or the title is a featured credit too.
2. ``&``, ``and``, ``+``, ``x``, ``×``, ``with``, ``/`` and commas only
   split when every part looks like a separately credited artist: it has
   two or more words ("Dua Lipa"), or it is the whole artist credit of
   another track in the library ("Rihanna").  Parts that start with
   "his", "her" or "their" ("and His Orchestra") and "friends" are
   dropped first.  A short built-in list of single acts
   (:data:`SINGLE_ACTS`), plus any a user adds, is never split.

So "Simon & Garfunkel", "Earth, Wind & Fire" and "Mumford & Sons" stay one
artist (one-word parts), "Calvin Harris & Dua Lipa" counts for both, and
"Tom Petty and the Heartbreakers" also counts for Tom Petty.  The limits:
a duo of one-word names splits only when the library has solo tracks by
each ("Rihanna & Drake"), and a band name made of two multi-word parts
splits even though it is one act; add it to ``single_acts`` in the
related artists file.  Splitting only ever adds artists to a credit, so a
wrong split holds back a few more tracks; it never lets one through.
"""

from __future__ import annotations

import logging
import math
import re
import tomllib
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The longest minute window a repeat rule accepts: one week.
MAX_REPEAT_MINUTES = 10080.0

# ---------------------------------------------------------------------------
# Text keys
# ---------------------------------------------------------------------------

# Removed outright, so "Don't" is "dont" and "Anderson .Paak" is "anderson paak".
_DROPPED = re.compile(r"[’‘'`´.]")
_NON_WORD = re.compile(r"[\W_]+")


def _fold(text: str) -> str:
    """Casefold *text* and strip its accents ("Beyoncé" -> "beyonce")."""
    if text.isascii():  # most tags: nothing to decompose
        return text.lower()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _words(text: str) -> list[str]:
    """Return the folded words of *text*; ``&`` and ``+`` read as "and"."""
    folded = _fold(text).replace("&", " and ").replace("+", " and ")
    return _NON_WORD.sub(" ", _DROPPED.sub("", folded)).split()


@lru_cache(maxsize=65536)
def artist_key(name: str) -> str:
    """Return the comparison key for one artist name.

    Case, accents, punctuation and a leading "The" are ignored, and ``&``
    reads as "and", so "The Beatles", "beatles" and "Beatles!" match.
    """
    words = _words(name)
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def album_key(album: str) -> str:
    """Return the comparison key for an album name (case, accents, punctuation)."""
    return " ".join(_words(album))


_VERSION_NOTE = re.compile(
    r"\b(?:remix(?:es|ed)?|rmx|mix(?:ed)?|edit|live|re-?master(?:ed)?|version|demo|acoustic"
    r"|unplugged|mono|stereo|extended|instrumental|single|radio|club|dub|explicit|clean"
    r"|bonus|deluxe|anniversary|re-?recorded|reprise|karaoke|session|take|original|cut"
    r"|(?:19|20)\d\d)\b",
    re.IGNORECASE,
)
_CREDIT_NOTE = re.compile(r"^\s*(?:feat\b|ft\b|featuring\b|with\b|from\b|prod\b)", re.IGNORECASE)
_BRACKETED = re.compile(r"[(\[{]([^)\]}]*)[)\]}]")
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+")
_INLINE_FEAT = re.compile(r"\s(?:feat\.?|featuring)\s.*$", re.IGNORECASE)


def _is_note(text: str) -> bool:
    """True when bracketed or dash-suffixed *text* names a version or a credit."""
    return bool(_CREDIT_NOTE.match(text) or _VERSION_NOTE.search(text))


@lru_cache(maxsize=65536)
def title_key(title: str) -> str:
    """Return the comparison key for a song title, ignoring its version.

    Case, accents and punctuation are ignored, and ``&`` reads as "and".
    Bracketed notes that name a version, a year or a credit -- "(Live)",
    "[Radio Edit]", "(feat. Someone)", "(From Frozen)" -- are dropped, as is
    a trailing " - " part that does ("- 2011 Remaster", "- Live at
    Wembley"), and an inline "feat. ..." to the end.  Other brackets keep
    their words: "(I Can't Get No) Satisfaction" stays as it is.  A title
    made only of a note, such as "(Live)", keeps its words.
    """

    def bracket(match: re.Match[str]) -> str:
        inner = match.group(1)
        return " " if _is_note(inner) else f" {inner} "

    text = _BRACKETED.sub(bracket, title)
    while dashes := list(_DASH_SUFFIX.finditer(text)):
        last = dashes[-1]
        if not _is_note(text[last.end() :]):
            break
        text = text[: last.start()]
    text = _INLINE_FEAT.sub("", text)
    key = " ".join(_words(text))
    return key or " ".join(_words(title))


# ---------------------------------------------------------------------------
# Artist credits
# ---------------------------------------------------------------------------

#: Well-known single acts whose names look like several artists.  The
#: two-word rule already keeps most of them whole; listing them also keeps
#: them whole when the library happens to hold an artist named like a part.
SINGLE_ACTS: tuple[str, ...] = (
    "Simon & Garfunkel",
    "Earth, Wind & Fire",
    "Mumford & Sons",
    "Hall & Oates",
    "Crosby, Stills, Nash & Young",
    "Crosby, Stills & Nash",
    "Emerson, Lake & Palmer",
    "Blood, Sweat & Tears",
    "Peter, Paul and Mary",
    "Tyler, the Creator",
    "Kool & the Gang",
    "Florence + the Machine",
    "Of Monsters and Men",
    "Above & Beyond",
)

_FEAT_BRACKET = re.compile(
    r"[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]*)[)\]]", re.IGNORECASE
)
_STRONG_SPLIT = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring|vs\.?|versus)\s+|\s*[;\x00]\s*", re.IGNORECASE
)
_JOINER = re.compile(r"\s*(?:,|&|\+|/|×)\s*|\s+(?:and|x|with)\s+(?=[^\s,&+/×])", re.IGNORECASE)
_GENERIC_LEADS = frozenset({"his", "her", "their", "my", "our", "its"})
_GENERIC_PARTS = frozenset({"friends", "others", "co", "company"})


def featured_in_title(title: str) -> list[str]:
    """Return the credits named by "(feat. X)" style notes or an inline "feat. X" in *title*."""
    found = [match.group(1) for match in _FEAT_BRACKET.finditer(title)]
    inline = re.search(r"\s(?:feat\.?|featuring)\s+(.+)$", _FEAT_BRACKET.sub(" ", title), re.I)
    if inline:
        found.append(inline.group(1))
    return [credit.strip() for credit in found if credit.strip()]


class ArtistSplitter:
    """Split artist credits into the artists they name (see the module notes).

    Args:
        known: Artist keys (:func:`artist_key`) of whole credits in the
            library.  A one-word part counts as an artist only when it is
            one of these.
        single_acts: Extra credits never to split, on top of
            :data:`SINGLE_ACTS`.
    """

    def __init__(self, known: Iterable[str] = (), single_acts: Iterable[str] = ()) -> None:
        self.known = frozenset(known)
        self.single_acts = frozenset(artist_key(name) for name in (*SINGLE_ACTS, *single_acts))
        self._cache: dict[str, frozenset[str]] = {}

    def _looks_like_artist(self, part: str) -> bool:
        return len(_words(part)) >= 2 or artist_key(part) in self.known

    def _segment(self, segment: str) -> set[str]:
        """Keys for one segment between strong separators: itself, plus its parts."""
        key = artist_key(segment)
        if not key:
            return set()
        keys = {key}
        if key in self.single_acts:
            return keys
        parts = [part.strip() for part in _JOINER.split(segment)]
        named = [
            part
            for part in parts
            if part
            and _words(part)
            and _words(part)[0] not in _GENERIC_LEADS
            and artist_key(part) not in _GENERIC_PARTS
        ]
        if len(parts) > 1 and all(parts) and all(self._looks_like_artist(p) for p in named):
            keys.update(artist_key(part) for part in named)
        return keys

    def split(self, credit: str) -> frozenset[str]:
        """Return the artist keys *credit* names, the full credit among them."""
        cached = self._cache.get(credit)
        if cached is not None:
            return cached
        keys: set[str] = set()
        whole = artist_key(credit)
        if whole:
            keys.add(whole)
        featured = [match.group(1) for match in _FEAT_BRACKET.finditer(credit)]
        main = _FEAT_BRACKET.sub(" ", credit)
        for segment in (*_STRONG_SPLIT.split(main), *featured):
            keys |= self._segment(segment.strip())
        result = frozenset(keys)
        self._cache[credit] = result
        return result

    def credited(self, artist: str, title: str) -> frozenset[str]:
        """Artist keys of a track: its artist credit plus "feat." credits in its title."""
        keys = set(self.split(artist)) if artist else set()
        for credit in featured_in_title(title) if title else ():
            keys |= self.split(credit)
        return frozenset(keys)


# ---------------------------------------------------------------------------
# Related artists
# ---------------------------------------------------------------------------

#: The optional starter list (``related_artists_starter = true``): side
#: projects, duos and aliases of one artist, in the same one-way form as
#: the user file.  Each key holds back the artists listed under it.
STARTER_RELATED_ARTISTS: dict[str, tuple[str, ...]] = {
    "Bruno Mars": ("Silk Sonic",),
    "Anderson .Paak": ("Silk Sonic", "NxWorries"),
    "Silk Sonic": ("Bruno Mars", "Anderson .Paak"),
    "NxWorries": ("Anderson .Paak",),
    "Donald Glover": ("Childish Gambino",),
    "Childish Gambino": ("Donald Glover",),
    "Danger Mouse": ("Gnarls Barkley", "Broken Bells"),
    "CeeLo Green": ("Gnarls Barkley",),
    "Gnarls Barkley": ("CeeLo Green", "Danger Mouse"),
    "Killer Mike": ("Run the Jewels",),
    "El-P": ("Run the Jewels",),
    "Run the Jewels": ("Killer Mike", "El-P"),
    "MF DOOM": ("Madvillain", "DANGERDOOM"),
    "Madlib": ("Madvillain",),
    "Madvillain": ("MF DOOM", "Madlib"),
    "Damon Albarn": ("Gorillaz", "Blur"),
    "Gorillaz": ("Damon Albarn",),
    "Jack White": ("The White Stripes", "The Raconteurs", "The Dead Weather"),
    "The White Stripes": ("Jack White",),
    "Thom Yorke": ("Radiohead", "Atoms for Peace", "The Smile"),
    "Justin Vernon": ("Bon Iver",),
    "Bon Iver": ("Justin Vernon",),
    "Aphex Twin": ("AFX", "Polygon Window"),
    "AFX": ("Aphex Twin",),
}


@dataclass(frozen=True)
class RelatedArtists:
    """One-way links between artists, and credits never to split.

    Attributes:
        links: Artist key -> the keys it holds back.  "Bruno Mars" ->
            "Silk Sonic" means a recent Bruno Mars track holds back Silk
            Sonic; Silk Sonic holds back Bruno Mars only if it is listed
            too.  Links are not followed further: A -> B and B -> C does
            not make A hold back C.
        names: Artist key -> the name as written, for log messages.
        single_acts: Credits from the file's ``single_acts`` list.
    """

    links: Mapping[str, frozenset[str]] = field(default_factory=dict)
    names: Mapping[str, str] = field(default_factory=dict)
    single_acts: tuple[str, ...] = ()

    def related_to(self, key: str) -> frozenset[str]:
        """The artist keys a recent track by *key* holds back, besides itself."""
        return self.links.get(key, frozenset())

    def name(self, key: str) -> str:
        """The name *key* was written as, or *key* itself."""
        return self.names.get(key, key)

    def merged(self, other: RelatedArtists) -> RelatedArtists:
        """Both sets of links, joined artist by artist."""
        links = {key: set(values) for key, values in self.links.items()}
        for key, values in other.links.items():
            links.setdefault(key, set()).update(values)
        return RelatedArtists(
            {key: frozenset(values) for key, values in links.items()},
            {**self.names, **other.names},
            (*self.single_acts, *other.single_acts),
        )


def parse_related_artists(data: Mapping[str, Any], source: str) -> RelatedArtists:
    """Validate a related artists table (the parsed TOML file).

    The file holds an optional ``single_acts`` list of credits never to
    split, and a ``[related]`` table mapping an artist to a list of the
    artists it holds back::

        single_acts = ["Edward Sharpe and the Magnetic Zeros"]

        [related]
        "Bruno Mars" = ["Silk Sonic"]
        "Anderson .Paak" = ["Silk Sonic", "NxWorries"]

    Raises:
        ValueError: An unknown key, a value of the wrong type, or an empty
            artist name.  The message names *source*.
    """
    unknown = set(data) - {"related", "single_acts"}
    if unknown:
        raise ValueError(f"{source}: unknown keys {sorted(unknown)}; use [related] and single_acts")
    single_acts = data.get("single_acts", [])
    if not isinstance(single_acts, list) or not all(
        isinstance(name, str) and artist_key(name) for name in single_acts
    ):
        raise ValueError(f"{source}: single_acts must be a list of artist names")
    related = data.get("related", {})
    if not isinstance(related, Mapping):
        raise ValueError(f"{source}: [related] must be a table of artist = [artists]")
    links: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for artist, others in related.items():
        key = artist_key(artist)
        if not key:
            raise ValueError(f"{source}: [related] has an empty artist name {artist!r}")
        if not isinstance(others, list) or not all(
            isinstance(other, str) and artist_key(other) for other in others
        ):
            raise ValueError(
                f"{source}: [related] {artist!r} must be a list of artist names, "
                f'for example ["Silk Sonic"]'
            )
        names.setdefault(key, artist)
        targets = links.setdefault(key, set())
        for other in others:
            other_key = artist_key(other)
            names.setdefault(other_key, other)
            if other_key != key:
                targets.add(other_key)
    return RelatedArtists(
        {key: frozenset(values) for key, values in links.items()},
        names,
        tuple(single_acts),
    )


def starter_related_artists() -> RelatedArtists:
    """The built-in starter list as :class:`RelatedArtists`."""
    return parse_related_artists(
        {"related": {artist: list(others) for artist, others in STARTER_RELATED_ARTISTS.items()}},
        "starter list",
    )


def load_related_artists(path: str | Path | None, *, starter: bool = False) -> RelatedArtists:
    """Read the related artists file at *path*, plus the starter list when *starter*.

    Raises:
        FileNotFoundError: *path* is set but there is no such file.
        ValueError: The file is not valid TOML or not a valid related
            artists file (see :func:`parse_related_artists`).
    """
    related = starter_related_artists() if starter else RelatedArtists()
    if not path:
        return related
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"playback.related_artists_file not found: {file}")
    try:
        with file.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{file}: not valid TOML: {exc}") from exc
    return related.merged(parse_related_artists(data, str(file)))


def load_related_artists_or_warn(
    path: str | Path | None, *, starter: bool = False
) -> tuple[RelatedArtists, str | None]:
    """Like :func:`load_related_artists`, but a bad file never stops AutoDJ.

    Returns:
        The related artists and ``None``; or, when the file is missing,
        unreadable or invalid, the starter list (when *starter*) or no
        links, and a sentence naming the file and the problem.
    """
    try:
        return load_related_artists(path, starter=starter), None
    except (OSError, ValueError) as exc:  # tomllib.TOMLDecodeError is a ValueError
        fallback = starter_related_artists() if starter else RelatedArtists()
        return fallback, f"playback.related_artists_file {path} was not used: {exc}"


# ---------------------------------------------------------------------------
# Rules and history
# ---------------------------------------------------------------------------


def _number(settings: Any, name: str, default: float | None) -> Any:
    """``settings.name`` when it is a number, else *default* (an older settings object)."""
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return value


def _text(value: Any) -> str:
    """*value* when it is text, else an empty string (a tag that is not set)."""
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class RepeatRules:
    """How far back each repeat rule looks.

    A ``*_window`` counts tracks and a ``*_minutes`` counts minutes; 0
    turns that half off.  When both are set the rule holds a candidate
    back if either one catches it.

    Attributes:
        track_window: Picks before the same file may play again.
        track_minutes: Minutes before the same file may play again.
        artist_window: Picks before an artist (or a related one, or the
            same album) may play again.
        artist_minutes: The same, in minutes.
        title_window: Picks before the same song title may play again.
        title_minutes: The same, in minutes.
        related: Links between artists, and single acts.
    """

    track_window: int = 500
    track_minutes: float = 0.0
    artist_window: int = 3
    artist_minutes: float = 0.0
    title_window: int = 3
    title_minutes: float = 0.0
    related: RelatedArtists = field(default_factory=RelatedArtists)

    @classmethod
    def from_playback(
        cls,
        playback: Any,
        *,
        track_window: int | None = None,
        related: RelatedArtists | None = None,
    ) -> RepeatRules:
        """Build the rules from a ``[playback]`` section.

        Args:
            playback: A :class:`~autodj.config.PlaybackConfig`.  On an
                older settings object, a newer setting that is missing or
                not a number reads as its default.
            track_window: Use this instead of ``no_repeat_window`` (the
                player shrinks it to fit a small library).
            related: The loaded related artists.
        """
        artist_window = int(playback.artist_repeat_window)
        title_window = _number(playback, "title_repeat_window", None)
        return cls(
            track_window=int(playback.no_repeat_window if track_window is None else track_window),
            track_minutes=float(_number(playback, "no_repeat_minutes", 0.0)),
            artist_window=artist_window,
            artist_minutes=float(_number(playback, "artist_repeat_minutes", 0.0)),
            title_window=artist_window if title_window is None else int(title_window),
            title_minutes=float(_number(playback, "title_repeat_minutes", 0.0)),
            related=related if related is not None else RelatedArtists(),
        )

    @property
    def keep_tracks(self) -> int:
        """How many recent tracks the artist and title rules can look at."""
        return max(0, self.artist_window, self.title_window)

    @property
    def keep_seconds(self) -> float:
        """How many seconds of history any minute window can look at."""
        return max(self.track_minutes, self.artist_minutes, self.title_minutes) * 60.0


@dataclass(frozen=True)
class PlayRecord:
    """One track start, as the repeat rules remember it.

    The raw tags are kept, not keys, so a change to the related artists or
    to the library's known artists applies to tracks already played; a
    :class:`TrackKeyCache` works the keys out once per track.  *started*
    is wall-clock seconds (``time.time()``), so saved plays from before a
    restart compare with new ones.
    """

    path: str
    artist: str
    album: str
    title: str
    started: float

    @classmethod
    def of(cls, entry: Any, started: float) -> PlayRecord:
        """Record *entry* (an :class:`~autodj.indexer.IndexEntry`) starting at *started*."""
        return cls(
            entry.path,
            _text(entry.artist),
            _text(entry.album),
            _text(entry.title),
            started,
        )


def _played_at(value: Any) -> float | None:
    """Seconds since the epoch for a saved ``played_at`` (ISO 8601), or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def records_from_plays(rows: Iterable[Mapping[str, Any]], now: float) -> list[PlayRecord]:
    """Turn saved plays (oldest first) into :class:`PlayRecord`, oldest first.

    Each row needs ``path``, ``artist``, ``album``, ``title`` and
    ``played_at`` (ISO 8601).  *now* is the wall clock (``time.time()``).
    A row with no path or an unreadable time is skipped, and so is one
    played after *now*: the clock was set back since, and a start in the
    future would hold its song back until the clock caught up.
    """
    records = []
    for row in rows:
        path = row.get("path")
        started = _played_at(row.get("played_at"))
        if not isinstance(path, str) or not path or started is None or started > now:
            continue
        records.append(
            PlayRecord(
                path,
                _text(row.get("artist")),
                _text(row.get("album")),
                _text(row.get("title")),
                started,
            )
        )
    return records


#: The most track starts the artist, album and title rules remember.  A
#: week of two-minute tracks fits; a larger window looks only this far back.
HISTORY_LIMIT = 5000


def prune_history(history: Any, rules: RepeatRules, now: float, minimum_seconds: float = 0) -> None:
    """Drop records no rule can look at any more from the left of *history* (a deque).

    At most :data:`HISTORY_LIMIT` records are kept, so a pick that copies
    and walks the history stays quick even with week-long windows.
    """
    keep, seconds = rules.keep_tracks, max(rules.keep_seconds, minimum_seconds)
    while history and (
        len(history) > HISTORY_LIMIT
        or (len(history) > keep and now - history[0].started >= seconds)
    ):
        history.popleft()


def plays_to_load(rules: RepeatRules, minimum_seconds: float = 0) -> int:
    """How many saved plays to load at startup so every window starts full.

    The track window's count, the artist and title windows' counts, and,
    when any rule counts minutes, up to :data:`HISTORY_LIMIT` plays.
    """
    needed = (
        HISTORY_LIMIT
        if max(rules.keep_seconds, minimum_seconds) > 0
        else min(rules.keep_tracks, HISTORY_LIMIT)
    )
    return max(rules.track_window, needed)


@dataclass(frozen=True, slots=True)
class TrackKeys:
    """The keys the artist, album and title rules compare for one track.

    Attributes:
        artists: Every artist key the track credits (see
            :meth:`ArtistSplitter.credited`).
        album: :func:`album_key` of the album, or ``""``.
        title: :func:`title_key` of the title, or ``""``.
    """

    artists: frozenset[str]
    album: str
    title: str


class TrackKeyCache:
    """Each track's :class:`TrackKeys`, worked out once and then looked up.

    A pick checks the whole library against the rules, so splitting
    credits and folding titles on every pick would cost far too much on a
    large library.  Tracks are remembered by path together with the tags
    their keys came from, so a retagged track gets new keys.  Make a new
    cache when the splitter's known artists change.

    Args:
        splitter: Splits artist credits.
    """

    def __init__(self, splitter: ArtistSplitter | None = None) -> None:
        self.splitter = splitter or ArtistSplitter()
        self._keys: dict[str, tuple[str, str, str, TrackKeys]] = {}

    def keys(self, track: Any) -> TrackKeys:
        """The keys of *track*: anything with ``path``, ``artist``, ``album`` and ``title``."""
        artist, album, title = _text(track.artist), _text(track.album), _text(track.title)
        cached = self._keys.get(track.path)
        if cached is not None and cached[:3] == (artist, album, title):
            return cached[3]
        keys = TrackKeys(
            self.splitter.credited(artist, title),
            album_key(album) if album else "",
            title_key(title) if title else "",
        )
        self._keys[track.path] = (artist, album, title, keys)
        return keys


def _key_cache(keys: TrackKeyCache | ArtistSplitter | None) -> TrackKeyCache:
    """*keys* as a :class:`TrackKeyCache` (a bare splitter gets a fresh cache)."""
    return keys if isinstance(keys, TrackKeyCache) else TrackKeyCache(keys)


#: Reasons a candidate is held back, in the order they are checked.
REASONS = ("same artist", "related artist", "same album", "same title")


class RepeatFilter:
    """Says whether a candidate passes the artist, album and title rules.

    Call it with an entry: ``True`` means the rules let it play.  Every
    candidate it holds back is remembered with its reason, for
    :meth:`summary`.  The track rule is not here: the picker's recent-path
    set carries it.

    Args:
        artists: Keys of artists credited on recent tracks.
        related: Keys held back through a related artist -> the recent
            artist key that holds it back.
        albums: Keys of recent albums.
        titles: Keys of recent titles.
        keys: Looks up each candidate's keys; a bare splitter gets a
            fresh cache.
        names: Looks up the written name of an artist key, for logs.
    """

    def __init__(
        self,
        artists: Collection[str] = (),
        related: Mapping[str, str] | None = None,
        albums: Collection[str] = (),
        titles: Collection[str] = (),
        keys: TrackKeyCache | ArtistSplitter | None = None,
        names: RelatedArtists | None = None,
    ) -> None:
        self.artists = frozenset(artists)
        self.related = dict(related or {})
        self._related_keys = frozenset(self.related)
        self.albums = frozenset(albums)
        self.titles = frozenset(titles)
        self._keys = _key_cache(keys)
        self._names = names or RelatedArtists()
        self.held_back: dict[str, str] = {}

    def __bool__(self) -> bool:
        """True when any rule can hold a candidate back."""
        return bool(self.artists or self.related or self.albums or self.titles)

    def reason(self, entry: Any) -> str | None:
        """Why *entry* is held back, as a short phrase, or ``None``."""
        if not self:
            return None
        keys = self._keys.keys(entry)
        if not keys.artists.isdisjoint(self.artists):
            return "same artist"
        if not keys.artists.isdisjoint(self._related_keys):
            return "related artist"
        if keys.album and keys.album in self.albums:
            return "same album"
        if keys.title and keys.title in self.titles:
            return "same title"
        return None

    def __call__(self, entry: Any) -> bool:
        """``True`` when *entry* may play; remembers why it may not."""
        why = self.reason(entry)
        if why is None:
            return True
        self.held_back[entry.path] = why
        return False

    def summary(self) -> str:
        """Count the candidates held back, by reason; empty when none were."""
        counts = Counter(self.held_back.values())
        return ", ".join(f"{counts[reason]} {reason}" for reason in REASONS if counts[reason])


def build_filter(
    history: Sequence[PlayRecord],
    rules: RepeatRules,
    now: float,
    keys: TrackKeyCache | ArtistSplitter,
) -> tuple[RepeatFilter, list[str]]:
    """Work out what *history* (oldest first) holds back at time *now*.

    *keys* looks up each record's keys; pass the player's
    :class:`TrackKeyCache` so tracks from the library are not split again.

    Returns:
        The artist, album and title filter, and the paths the track
        rule's minute window holds back (its count window is the
        player's recent-path deque).
    """
    cache = _key_cache(keys)
    count = len(history)
    artists: set[str] = set()
    albums: set[str] = set()
    titles: set[str] = set()
    paths: list[str] = []
    # No age is below -inf, so a rule without minutes counts tracks only.
    artist_seconds = rules.artist_minutes * 60.0 if rules.artist_minutes > 0 else -math.inf
    title_seconds = rules.title_minutes * 60.0 if rules.title_minutes > 0 else -math.inf
    track_seconds = rules.track_minutes * 60.0 if rules.track_minutes > 0 else -math.inf
    for position, record in enumerate(history):
        back = count - position  # 1 = the latest track
        age = now - record.started
        by_artist = back <= rules.artist_window or age < artist_seconds
        by_title = back <= rules.title_window or age < title_seconds
        if by_artist or by_title:
            track = cache.keys(record)
            if by_artist:
                artists |= track.artists
                albums.add(track.album)
            if by_title:
                titles.add(track.title)
        if age < track_seconds:
            paths.append(record.path)
    related: dict[str, str] = {}
    for key in artists:
        for other in rules.related.related_to(key):
            if other not in artists:
                related.setdefault(other, key)
    artists.discard("")
    albums.discard("")
    titles.discard("")
    return RepeatFilter(artists, related, albums, titles, cache, rules.related), paths


__all__ = [
    "HISTORY_LIMIT",
    "MAX_REPEAT_MINUTES",
    "REASONS",
    "SINGLE_ACTS",
    "STARTER_RELATED_ARTISTS",
    "ArtistSplitter",
    "PlayRecord",
    "RelatedArtists",
    "RepeatFilter",
    "RepeatRules",
    "TrackKeyCache",
    "TrackKeys",
    "album_key",
    "artist_key",
    "build_filter",
    "featured_in_title",
    "load_related_artists",
    "load_related_artists_or_warn",
    "parse_related_artists",
    "plays_to_load",
    "prune_history",
    "records_from_plays",
    "starter_related_artists",
    "title_key",
]
