"""Repeat rules: which recently heard tracks, artists and titles hold back a pick.

Four independent rules decide whether a candidate may play yet:

- **Track**: the exact file played recently.
- **Artist**: any artist credited on the candidate was credited on a recent
  track.
- **Album**: the candidate has an album name recently played.
- **Title**: the candidate's song title, after :func:`title_key` folds away
  case, punctuation, accents and version notes such as "(Live)", matches a
  recent title.  So "Jingle Bells", "Jingle Bells (Live)" and
  "JINGLE BELLS - Remastered 2009" are one song.

Two more rules use files in the index folder, each turned on in Settings:

- **Related artist**: an artist linked in ``related-artists.toml`` to an
  artist credited on a recent track (see :class:`RelatedArtists`).
- **Related track**: a track grouped in ``related-tracks.toml`` with a
  recent track, such as a song and one that samples it (see
  :class:`RelatedTracks`).

Each rule looks back a number of tracks, a number of minutes, or both;
a candidate is held back when either one catches it.

Two more rules keep a song from playing at the same time of day, day after
day (daypart and hourpart separation, #129), both off unless set:

- **Same daypart**: the song played in the daypart the pick starts in
  (such as morning drive, 6 to 10) today or on one of the days before.
- **Same quarter hour**: one of the song's last few plays started in the
  quarter of the hour the pick starts in.

They relax with the artist, album and title rules when nothing else fits.

With **Count versions as one song** on, the files of one song (the same
lead artist and :func:`title_key`, such as a radio edit and a remix) are one
song when picking (see :class:`SongVersions`): the track rule holds back
every version of a song that played, and the pickers give the song the same
chance as a song with one file.

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
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any

from autodj.repeat_settings import DEFAULT_DAYPART_START_HOURS

logger = logging.getLogger(__name__)

#: The longest minute window a repeat rule accepts: one week.
MAX_REPEAT_MINUTES = 10080.0

#: The rules that relax together when no candidate passes them, for log lines.
RELAXED_RULES = (
    "the artist, related artist, album, title, related track, daypart and quarter-hour rules"
)

#: The shortest a play is taken to last when working out how many saved plays cover the
#: daypart and quarter-hour rules' days.  Liners are not saved as plays, so songs are longer.
SHORTEST_PLAY_SECONDS = 60.0

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


_VERSION_WORD = (
    r"(?:remix(?:es|ed)?|rmx|mix(?:ed)?|edit|live|re-?master(?:ed)?|version|demo|acoustic"
    r"|unplugged|mono|stereo|extended|instrumental|single|radio|club|dub|explicit|clean"
    r"|bonus|deluxe|anniversary|re-?recorded|reprise|karaoke|session|take|original|cut"
    r"|(?:19|20)\d\d)"
)
_VERSION_NOTE = re.compile(
    rf"\s*(?:{_VERSION_WORD}(?:[\s-]+{_VERSION_WORD})*"
    r"|live\s+(?:at|from|in)\s+.+|.+\s+(?:remix|mix|edit))\s*",
    re.IGNORECASE,
)
_CREDIT_NOTE = re.compile(r"^\s*(?:feat\b|ft\b|featuring\b|with\b|w/|from\b|prod\b)", re.IGNORECASE)
_BRACKETED = re.compile(r"[(\[{]([^)\]}]*)[)\]}]")
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+")
_INLINE_FEAT = re.compile(r"\s(?:feat\.?|featuring)\s.*$", re.IGNORECASE)


def _is_note(text: str) -> bool:
    """True when bracketed or dash-suffixed *text* names a version or a credit."""
    return bool(_CREDIT_NOTE.match(text) or _VERSION_NOTE.fullmatch(text))


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
    r"[(\[]\s*(?:(?:feat\.?|ft\.?|featuring|with)\s+|w/\s*)([^)\]]*)[)\]]", re.IGNORECASE
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


#: The largest related artists file AutoDJ reads, in bytes.
MAX_RELATED_ARTISTS_BYTES = 1024 * 1024


class RelatedArtistsFileTooLargeError(ValueError):
    """The related artists file is over :data:`MAX_RELATED_ARTISTS_BYTES`."""


def load_related_artists(path: str | Path | None) -> RelatedArtists:
    """Read and validate the related artists file at *path*.

    The size is checked before reading, so a huge file is never loaded.

    Raises:
        FileNotFoundError: *path* is set but there is no such file.
        RelatedArtistsFileTooLargeError: The file is over
            :data:`MAX_RELATED_ARTISTS_BYTES`.
        ValueError: The file is not valid TOML (the cause is the
            :class:`tomllib.TOMLDecodeError`) or not a valid related
            artists file (see :func:`parse_related_artists`).
    """
    if not path:
        return RelatedArtists()
    file = Path(path)
    data = _read_related_file(file, "related artists", RelatedArtistsFileTooLargeError)
    return parse_related_artists(data, str(file))


def _read_related_file(file: Path, kind: str, too_large: type[ValueError]) -> dict[str, Any]:
    """Parse the TOML at *file*, checking its size first so a huge file is never read."""
    if not file.is_file():
        raise FileNotFoundError(f"{kind} file not found: {file}")
    if file.stat().st_size > MAX_RELATED_ARTISTS_BYTES:
        raise too_large(f"{kind} file is larger than 1 MiB: {file}")
    try:
        with file.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{file}: not valid TOML: {exc}") from exc


# ---------------------------------------------------------------------------
# Related tracks
# ---------------------------------------------------------------------------

#: A track as the related tracks file names it: (artist key, title key).
TrackKey = tuple[str, str]


@dataclass(frozen=True)
class RelatedTracks:
    """Groups of tracks that sound alike on air, such as a song and one that samples it.

    Every track in a group holds back every other track in it, both ways.
    Groups are not joined: a track in two groups holds back the tracks of
    both, but those tracks do not hold each other back.

    Attributes:
        links: Track key -> the track keys it holds back.
        names: Track key -> "Artist - Title" as written, for log messages.
    """

    links: Mapping[TrackKey, frozenset[TrackKey]] = field(default_factory=dict)
    names: Mapping[TrackKey, str] = field(default_factory=dict)

    def related_to(self, key: TrackKey) -> frozenset[TrackKey]:
        """The track keys a recent track with *key* holds back, besides itself."""
        return self.links.get(key, frozenset())

    def name(self, key: TrackKey) -> str:
        """The "Artist - Title" *key* was written as."""
        return self.names.get(key, " - ".join(key))


def related_track_key(text: str) -> TrackKey | None:
    """The key of "Artist - Title" (split at the first " - "), or ``None`` when it is not one."""
    artist, separator, title = text.partition(" - ")
    key = (artist_key(artist), title_key(title))
    return key if separator and all(key) else None


def parse_related_tracks(data: Mapping[str, Any], source: str) -> RelatedTracks:
    """Validate a related tracks table (the parsed TOML file).

    The ``[related]`` table maps a track to a list of tracks; the track and
    its list are one group, and every track in a group holds back every
    other.  Each track is written "Artist - Title" with the lead artist::

        [related]
        "Rick James - Super Freak" = ["MC Hammer - U Can't Touch This"]

    Case, punctuation, a leading "The", featured artists and version notes
    such as "(Radio Edit)" do not matter, as in the artist and title rules.

    Raises:
        ValueError: An unknown key, a value of the wrong type, or a track
            not written "Artist - Title".  The message names *source*.
    """
    unknown = set(data) - {"related"}
    if unknown:
        raise ValueError(f"{source}: unknown keys {sorted(unknown)}; use [related]")
    related = data.get("related", {})
    if not isinstance(related, Mapping):
        raise ValueError(f'{source}: [related] must be a table of "Artist - Title" = [tracks]')
    links: dict[TrackKey, set[TrackKey]] = {}
    names: dict[TrackKey, str] = {}
    for track, others in related.items():
        if not isinstance(others, list) or not all(isinstance(other, str) for other in others):
            raise ValueError(
                f"{source}: [related] {track!r} must be a list of tracks, "
                f'for example ["MC Hammer - U Can\'t Touch This"]'
            )
        group: list[TrackKey] = []
        for text in (track, *others):
            key = related_track_key(text)
            if key is None:
                raise ValueError(f'{source}: [related] {text!r} is not written "Artist - Title"')
            names.setdefault(key, text)
            group.append(key)
        for key in group:
            links.setdefault(key, set()).update(other for other in group if other != key)
    return RelatedTracks({key: frozenset(values) for key, values in links.items() if values}, names)


class RelatedTracksFileTooLargeError(ValueError):
    """The related tracks file is over 1 MiB, the same limit as related artists."""


def load_related_tracks(path: str | Path | None) -> RelatedTracks:
    """Read and validate the related tracks file at *path*.

    The size is checked before reading, so a huge file is never loaded.

    Raises:
        FileNotFoundError: *path* is set but there is no such file.
        RelatedTracksFileTooLargeError: The file is over 1 MiB.
        ValueError: The file is not valid TOML (the cause is the
            :class:`tomllib.TOMLDecodeError`) or not a valid related
            tracks file (see :func:`parse_related_tracks`).
    """
    if not path:
        return RelatedTracks()
    file = Path(path)
    data = _read_related_file(file, "related tracks", RelatedTracksFileTooLargeError)
    return parse_related_tracks(data, str(file))


# ---------------------------------------------------------------------------
# Rules and history
# ---------------------------------------------------------------------------


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
        artist_window: Picks before an artist may play again.
        artist_minutes: The same, in minutes.
        title_window: Picks before the same song title may play again.
        title_minutes: The same, in minutes.
        related: Links between artists, and single acts.
        related_artist_window: Picks before an artist related to a recent
            one may play; ``None`` uses ``artist_window``.
        related_artist_minutes: The same, in minutes; ``None`` uses
            ``artist_minutes``.
        related_tracks: Groups of tracks that hold each other back.
        related_track_window: Picks before a track related to a recent
            one may play.
        related_track_minutes: The same, in minutes.
        versions_as_one_song: Count a song's versions as one song (see
            :class:`SongVersions`).
        daypart_days: Days before today whose same daypart holds a song
            back; 0 turns the rule off.
        daypart_starts: The station hours the dayparts start at, in
            order.  The last daypart runs on to the first start of the
            next day.
        hourpart_plays: A song's latest plays whose quarter of the hour
            holds it back; 0 turns the rule off.
    """

    track_window: int = 50
    track_minutes: float = 0.0
    artist_window: int = 8
    artist_minutes: float = 0.0
    title_window: int = 0
    title_minutes: float = 0.0
    related: RelatedArtists = field(default_factory=RelatedArtists)
    album_window: int = 0
    album_minutes: float = 0.0
    related_artist_window: int | None = None
    related_artist_minutes: float | None = None
    related_tracks: RelatedTracks = field(default_factory=RelatedTracks)
    related_track_window: int = 0
    related_track_minutes: float = 0.0
    versions_as_one_song: bool = False
    daypart_days: int = 0
    daypart_starts: tuple[int, ...] = DEFAULT_DAYPART_START_HOURS
    hourpart_plays: int = 0

    @property
    def related_artist_limits(self) -> tuple[int, float]:
        """The related artist rule's track count and minutes."""
        return (
            self.artist_window
            if self.related_artist_window is None
            else self.related_artist_window,
            self.artist_minutes
            if self.related_artist_minutes is None
            else self.related_artist_minutes,
        )

    @classmethod
    def from_playback(
        cls,
        playback: Any,
        *,
        track_window: int | None = None,
        related: RelatedArtists | None = None,
        related_tracks: RelatedTracks | None = None,
    ) -> RepeatRules:
        """Build the rules from a ``[playback]`` section.

        Args:
            playback: A :class:`~autodj.config.PlaybackConfig`.
            track_window: Use this track count instead of the configured
                count (the player shrinks it to fit a small library).
            related: The loaded related artists.
            related_tracks: The loaded related tracks.
        """
        settings = playback.repeat_rules

        def values(rule: Any) -> tuple[int, float]:
            return rule.tracks, rule.minutes

        artist_window, artist_minutes = values(settings.artist)
        album_window, album_minutes = values(settings.album)
        title_window, title_minutes = values(settings.title)
        configured_track_window, track_minutes = values(settings.track)
        related_artist_window, related_artist_minutes = values(settings.related_artist)
        related_track_window, related_track_minutes = values(settings.related_track)
        use_tracks = related_tracks is not None and settings.use_related_tracks
        return cls(
            track_window=int(configured_track_window if track_window is None else track_window),
            track_minutes=track_minutes,
            artist_window=artist_window,
            artist_minutes=artist_minutes,
            title_window=title_window,
            title_minutes=title_minutes,
            related=(
                related
                if related is not None and settings.use_related_artists
                else RelatedArtists()
            ),
            album_window=album_window,
            album_minutes=album_minutes,
            related_artist_window=related_artist_window,
            related_artist_minutes=related_artist_minutes,
            related_tracks=related_tracks if use_tracks and related_tracks else RelatedTracks(),
            related_track_window=related_track_window if use_tracks else 0,
            related_track_minutes=related_track_minutes if use_tracks else 0.0,
            versions_as_one_song=settings.versions_as_one_song,
            daypart_days=settings.daypart.days,
            daypart_starts=tuple(settings.daypart.start_hours),
            hourpart_plays=settings.hourpart.plays,
        )

    @property
    def keep_tracks(self) -> int:
        """How many recent tracks the artist, album, title and related rules can look at."""
        related_window = self.related_artist_limits[0] if self.related.links else 0
        return max(
            0,
            self.artist_window,
            self.album_window,
            self.title_window,
            related_window,
            self.related_track_window if self.related_tracks.links else 0,
        )

    @property
    def keep_seconds(self) -> float:
        """How many seconds of history any rule can look at.

        The daypart rule looks at the days it covers, and two more for the
        daypart that runs past midnight and a clock change; the
        quarter-hour rule at :data:`HOURPART_DAYS`.
        """
        return max(self.minute_seconds, self.time_of_day_seconds)

    @property
    def minute_seconds(self) -> float:
        """How many seconds of history the minute windows look at (0 when none is set)."""
        related_minutes = self.related_artist_limits[1] if self.related.links else 0.0
        return (
            max(
                self.track_minutes,
                self.artist_minutes,
                self.album_minutes,
                self.title_minutes,
                related_minutes,
                self.related_track_minutes if self.related_tracks.links else 0.0,
            )
            * 60.0
        )

    @property
    def time_of_day_seconds(self) -> float:
        """How many seconds of history the daypart and quarter-hour rules look at (0 when off)."""
        seconds = 0.0
        if self.daypart_days:
            seconds = (self.daypart_days + 2) * 86400.0
        if self.hourpart_plays:
            seconds = max(seconds, HOURPART_DAYS * 86400.0)
        return seconds


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


#: The most recent track starts the artist, album and title rules remember.
HISTORY_LIMIT = 50000


def prune_history(history: Any, rules: RepeatRules, now: float, minimum_seconds: float = 0) -> None:
    """Drop records no rule can look at any more from the left of *history* (a deque).

    At most :data:`HISTORY_LIMIT` records are kept, so a pick that copies
    and walks the history stays quick even with week-long windows.
    """
    keep, seconds = rules.keep_tracks, max(rules.keep_seconds, minimum_seconds)
    history_limit = max(HISTORY_LIMIT, rules.keep_tracks)
    while history and (
        len(history) > history_limit
        or (len(history) > keep and now - history[0].started >= seconds)
    ):
        history.popleft()


def plays_to_load(rules: RepeatRules, minimum_seconds: float = 0) -> int:
    """How many saved plays to load at startup so every window starts full.

    The track window's count, the artist and title windows' counts, and,
    when any rule counts minutes, up to :data:`HISTORY_LIMIT` plays.  When
    only the daypart and quarter-hour rules look back in time, as many
    plays as their days can hold at :data:`SHORTEST_PLAY_SECONDS` each.
    """
    counted = min(rules.keep_tracks, HISTORY_LIMIT)
    if max(rules.minute_seconds, minimum_seconds) > 0:
        needed = HISTORY_LIMIT
    elif rules.time_of_day_seconds:
        needed = min(
            HISTORY_LIMIT,
            max(counted, math.ceil(rules.time_of_day_seconds / SHORTEST_PLAY_SECONDS)),
        )
    else:
        needed = counted
    return max(rules.track_window, needed)


@dataclass(frozen=True, slots=True)
class TrackKeys:
    """The keys the artist, album and title rules compare for one track.

    Attributes:
        artists: Every artist key the track credits (see
            :meth:`ArtistSplitter.credited`).
        album: :func:`album_key` of the album, or ``""``.
        title: :func:`title_key` of the title, or ``""``.
        song: :func:`song_key` of the track, or ``None``.
    """

    artists: frozenset[str]
    album: str
    title: str
    song: TrackKey | None = None


def song_key(artist: str, title: str) -> TrackKey | None:
    """The song a file is a version of: its lead artist's key and its :func:`title_key`.

    The lead artist is the credit before any "feat." or "(with X)" note, so
    "Mariah Carey feat. ODB - Fantasy (Remix)" and "Mariah Carey - Fantasy"
    are one song, while two artists' songs with one title are two.
    ``None`` when the artist or title is missing: such a file is a song of
    its own.
    """
    return _song(artist, title_key(title) if _text(title) else "")


def _song(artist: str, title: str) -> TrackKey | None:
    """:func:`song_key` with the title already folded by :func:`title_key`."""
    lead = artist_key(_STRONG_SPLIT.split(_FEAT_BRACKET.sub(" ", _text(artist)))[0])
    return (lead, title) if lead and title else None


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
        # Compared one by one: no tuple is built for the lookup a pick makes per track.
        if cached is not None and cached[0] == artist and cached[1] == album and cached[2] == title:
            return cached[3]
        folded = title_key(title) if title else ""
        keys = TrackKeys(
            self.splitter.credited(artist, title),
            album_key(album) if album else "",
            folded,
            _song(artist, folded),
        )
        self._keys[track.path] = (artist, album, title, keys)
        return keys


def _key_cache(keys: TrackKeyCache | ArtistSplitter | None) -> TrackKeyCache:
    """*keys* as a :class:`TrackKeyCache` (a bare splitter gets a fresh cache)."""
    return keys if isinstance(keys, TrackKeyCache) else TrackKeyCache(keys)


class SongVersions:
    """The files of each song that has more than one, for Count versions as one song.

    A song is the :func:`song_key` of its files; a file without one, or the
    only file of its song, is a song of its own.  Paths are the library's
    entry paths, compared as given.

    Args:
        entries: The library's tracks (``path``, ``artist``, ``album``, ``title``).
        keys: Looks up each track's keys; a bare splitter gets a fresh cache.
    """

    def __init__(
        self, entries: Iterable[Any], keys: TrackKeyCache | ArtistSplitter | None = None
    ) -> None:
        cache = _key_cache(keys)
        songs: dict[TrackKey, list[str]] = {}
        for entry in entries:
            song = cache.keys(entry).song
            if song is not None:
                songs.setdefault(song, []).append(entry.path)
        self._versions: dict[str, tuple[str, ...]] = {}
        for paths in songs.values():
            if len(paths) > 1:
                versions = tuple(paths)
                for path in versions:
                    self._versions[path] = versions

    def __bool__(self) -> bool:
        """True when some song has more than one version."""
        return bool(self._versions)

    def versions(self, path: str) -> tuple[str, ...]:
        """Every file of *path*'s song, *path* included."""
        return self._versions.get(path, (path,))

    def song(self, path: str) -> str:
        """Names *path*'s song: the path of its first version."""
        return self.versions(path)[0]

    def with_versions(self, paths: Iterable[str]) -> list[str]:
        """*paths*, each followed by the other versions of its song, without repeats."""
        seen: set[str] = set()
        result: list[str] = []
        for path in paths:
            for version in self.versions(path):
                if version not in seen:
                    seen.add(version)
                    result.append(version)
        return result

    def choice(self, pool: Sequence[Any], rng: Any) -> Any:
        """A random entry of *pool*, each song as likely as any other.

        A song with three versions in *pool* is as likely as a song with
        one; then one of its versions is picked.  *rng* is anything with
        ``choice`` (``random`` or a :class:`random.Random`).
        """
        if not self._versions:
            return rng.choice(pool)
        songs: dict[str, list[Any]] = {}
        for entry in pool:
            songs.setdefault(self.song(entry.path), []).append(entry)
        return rng.choice(rng.choice(list(songs.values())))

    def last_played(self, played: Callable[[str], Any]) -> Callable[[str], Any]:
        """*played* for songs: the latest play of any version of a path's song, or ``None``."""

        def song_played(path: str) -> Any:
            stamps = [
                stamp for version in self.versions(path) if (stamp := played(version)) is not None
            ]
            return max(stamps) if stamps else None

        return song_played


#: Reasons a candidate is held back, in the order they are checked.
REASONS = (
    "same artist",
    "related artist",
    "same album",
    "same title",
    "related track",
    "same daypart",
    "same quarter hour",
)


def _local_start(day: date, hour: int, zone: tzinfo) -> float:
    """Unix seconds of *hour* o'clock on *day* in *zone*."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=zone).timestamp()


def daypart_spans(
    at: float, starts: Sequence[int], days: int, zone: tzinfo = UTC
) -> list[tuple[float, float]]:
    """The daypart *at* falls in, today and on each of the *days* before, newest first.

    Each span is ``(start, end)`` in Unix seconds, the end not included.
    *starts* are the station hours the dayparts start at, in order; the
    last daypart runs to the first start of the next day, so with starts
    of 6 and 19 a song at 2 in the morning is in the evening daypart that
    began at 19 the day before.  *zone* is the station's time zone, so a
    daypart keeps its hours across a clock change.
    """
    local = datetime.fromtimestamp(at, zone)
    index = bisect_right(starts, local.hour) - 1
    day = local.date()
    if index < 0:  # Before the first start: still in the last daypart of the day before.
        index = len(starts) - 1
        day -= timedelta(days=1)
    spans = []
    for back in range(days + 1):
        first = day - timedelta(days=back)
        if index + 1 < len(starts):
            end = _local_start(first, starts[index + 1], zone)
        else:
            end = _local_start(first + timedelta(days=1), starts[0], zone)
        spans.append((_local_start(first, starts[index], zone), end))
    return spans


#: How many days back the quarter-hour rule looks for a song's latest plays.  A pattern
#: older than a week is not one a listener hears, and the bound keeps the history short.
HOURPART_DAYS = 7


def quarter_hour(at: float) -> int:
    """The quarter of the hour Unix time *at* falls in, 0 to 3.

    Read in UTC.  Every time zone in use is a whole number of quarter
    hours from UTC, so two times share a quarter in UTC exactly when they
    share it on the station's clock; the quarter's number may differ.
    """
    return int(at // 900) % 4


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
        related_tracks: Track keys held back through a related track ->
            the recent track key that holds it back.
        daypart: Songs (see :func:`song_identity`) that played in the
            pick's daypart today or on the days before.
        hourpart: Songs whose latest plays started in the pick's quarter
            of the hour.
        versions_as_one_song: Name songs by their :func:`song_key`, so
            every version of a song is held back with it.
    """

    def __init__(
        self,
        artists: Collection[str] = (),
        related: Mapping[str, str] | None = None,
        albums: Collection[str] = (),
        titles: Collection[str] = (),
        keys: TrackKeyCache | ArtistSplitter | None = None,
        names: RelatedArtists | None = None,
        related_tracks: Mapping[TrackKey, TrackKey] | None = None,
        *,
        daypart: Collection[str | TrackKey] = (),
        hourpart: Collection[str | TrackKey] = (),
        versions_as_one_song: bool = False,
    ) -> None:
        self.artists = frozenset(artists)
        self.related = dict(related or {})
        self._related_keys = frozenset(self.related)
        self.albums = frozenset(albums)
        self.titles = frozenset(titles)
        self.related_tracks = dict(related_tracks or {})
        # Titles first, so a candidate whose title matches nothing skips the artist pairs.
        self._related_titles = frozenset(title for _, title in self.related_tracks)
        self._keys = _key_cache(keys)
        self._names = names or RelatedArtists()
        self.daypart = frozenset(daypart)
        self.hourpart = frozenset(hourpart)
        self._versions = versions_as_one_song
        self._timed = bool(self.daypart or self.hourpart)
        self.held_back: dict[str, str] = {}

    def __bool__(self) -> bool:
        """True when any rule can hold a candidate back."""
        return bool(
            self.artists
            or self.related
            or self.albums
            or self.titles
            or self.related_tracks
            or self.daypart
            or self.hourpart
        )

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
        if keys.title in self._related_titles and any(
            (artist, keys.title) in self.related_tracks for artist in keys.artists
        ):
            return "related track"
        if self._timed:
            song = (keys.song or entry.path) if self._versions else entry.path
            if song in self.daypart:
                return "same daypart"
            if song in self.hourpart:
                return "same quarter hour"
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


def song_identity(record: Any, keys: TrackKeyCache, versions_as_one_song: bool) -> str | TrackKey:
    """What the daypart and quarter-hour rules call *record*'s song.

    Its path, or with Count versions as one song its :func:`song_key`
    (its path when it has none), so every version of a song counts.
    """
    if versions_as_one_song:
        return keys.keys(record).song or record.path
    return record.path


def _time_separation(
    history: Sequence[PlayRecord],
    rules: RepeatRules,
    at: float,
    zone: tzinfo,
    cache: TrackKeyCache,
) -> tuple[set[str | TrackKey], set[str | TrackKey]]:
    """The songs the daypart and quarter-hour rules hold back for a song starting at *at*.

    Worked out once per pick, walking *history* (oldest first) from the
    newest play back to the oldest either rule can see; each candidate is
    then a set lookup.  Like :func:`prune_history`, the walk takes the
    history to be in the order the plays started.
    """
    versions = rules.versions_as_one_song
    daypart: set[str | TrackKey] = set()
    if rules.daypart_days and rules.daypart_starts:
        spans = daypart_spans(at, rules.daypart_starts, rules.daypart_days, zone)
        oldest, latest = spans[-1][0], spans[0][1]
        for record in reversed(history):
            started = record.started
            if started < oldest:
                break
            if started < latest and any(begin <= started < end for begin, end in spans):
                daypart.add(song_identity(record, cache, versions))
    hourpart: set[str | TrackKey] = set()
    plays = rules.hourpart_plays
    if plays:
        quarter = quarter_hour(at)
        since = at - HOURPART_DAYS * 86400.0
        counted: dict[str | TrackKey, int] = {}
        for record in reversed(history):
            started = record.started
            if started < since:
                break
            song = (cache.keys(record).song or record.path) if versions else record.path
            seen = counted.get(song, 0)
            if seen < plays:
                counted[song] = seen + 1
                if int(started // 900) % 4 == quarter:
                    hourpart.add(song)
    return daypart, hourpart


def build_filter(
    history: Sequence[PlayRecord],
    rules: RepeatRules,
    now: float,
    keys: TrackKeyCache | ArtistSplitter,
    *,
    at: float | None = None,
    zone: tzinfo = UTC,
) -> tuple[RepeatFilter, list[str]]:
    """Work out what *history* (oldest first) holds back at time *now*.

    *keys* looks up each record's keys; pass the player's
    :class:`TrackKeyCache` so tracks from the library are not split again.
    The daypart and quarter-hour rules judge a song that starts at *at*
    (*now* when not given) on the station's clock in *zone*.

    Returns:
        The artist, album and title filter, and the paths the track
        rule's minute window holds back (its count window is the
        player's recent-path deque).
    """
    cache = _key_cache(keys)
    count = len(history)
    artists: set[str] = set()
    related_sources: set[str] = set()
    albums: set[str] = set()
    titles: set[str] = set()
    related_tracks: dict[TrackKey, TrackKey] = {}
    paths: list[str] = []

    def seconds(minutes: float) -> float:
        # No age is below -inf, so a rule without minutes counts tracks only.
        return minutes * 60.0 if minutes > 0 else -math.inf

    related_window, related_minutes = rules.related_artist_limits
    if not rules.related.links:
        related_window, related_minutes = 0, 0.0
    track_links = rules.related_tracks
    related_track_window = rules.related_track_window if track_links.links else 0
    artist_seconds = seconds(rules.artist_minutes)
    related_seconds = seconds(related_minutes)
    album_seconds = seconds(rules.album_minutes)
    title_seconds = seconds(rules.title_minutes)
    related_track_seconds = seconds(rules.related_track_minutes if track_links.links else 0.0)
    track_seconds = seconds(rules.track_minutes)
    for position, record in enumerate(history):
        back = count - position  # 1 = the latest track
        age = now - record.started
        by_artist = back <= rules.artist_window or age < artist_seconds
        by_related = back <= related_window or age < related_seconds
        by_album = back <= rules.album_window or age < album_seconds
        by_title = back <= rules.title_window or age < title_seconds
        by_related_track = back <= related_track_window or age < related_track_seconds
        if by_artist or by_related or by_album or by_title or by_related_track:
            track = cache.keys(record)
            if by_artist:
                artists |= track.artists
            if by_related:
                related_sources |= track.artists
            if by_album:
                albums.add(track.album)
            if by_title:
                titles.add(track.title)
            if by_related_track and track.title:
                for artist in track.artists:
                    for linked in track_links.related_to((artist, track.title)):
                        related_tracks.setdefault(linked, (artist, track.title))
        if age < track_seconds:
            paths.append(record.path)
    related: dict[str, str] = {}
    for key in related_sources:
        for other in rules.related.related_to(key):
            if other not in artists:
                related.setdefault(other, key)
    artists.discard("")
    albums.discard("")
    titles.discard("")
    daypart, hourpart = _time_separation(history, rules, now if at is None else at, zone, cache)
    repeat = RepeatFilter(
        artists,
        related,
        albums,
        titles,
        cache,
        rules.related,
        related_tracks=related_tracks,
        daypart=daypart,
        hourpart=hourpart,
        versions_as_one_song=rules.versions_as_one_song,
    )
    return repeat, paths


__all__ = [
    "HISTORY_LIMIT",
    "HOURPART_DAYS",
    "MAX_RELATED_ARTISTS_BYTES",
    "MAX_REPEAT_MINUTES",
    "REASONS",
    "RELAXED_RULES",
    "SHORTEST_PLAY_SECONDS",
    "SINGLE_ACTS",
    "ArtistSplitter",
    "PlayRecord",
    "RelatedArtists",
    "RelatedArtistsFileTooLargeError",
    "RelatedTracks",
    "RelatedTracksFileTooLargeError",
    "RepeatFilter",
    "RepeatRules",
    "SongVersions",
    "TrackKey",
    "TrackKeyCache",
    "TrackKeys",
    "album_key",
    "artist_key",
    "build_filter",
    "daypart_spans",
    "featured_in_title",
    "load_related_artists",
    "load_related_tracks",
    "parse_related_artists",
    "parse_related_tracks",
    "plays_to_load",
    "prune_history",
    "quarter_hour",
    "records_from_plays",
    "related_track_key",
    "song_identity",
    "song_key",
    "title_key",
]
