"""Themed shows: a named song filter with optional branding, kept in ``shows.json``.

A show such as "Remembering the 2000s" is defined once in Settings > Shows
and run two ways: started now for a length, or by a "Start show" event in
the Schedule with an end time.  While it runs, the station's own pick mode
picks only songs that pass its filter (see :mod:`autodj.show_runtime`).

The filter is a form -- a first-release year range and genres to include
or leave out -- plus an optional beets query (:mod:`autodj.beets_query`),
all of which must match.  The year is beets' ``original_year``, falling
back to ``year`` when beets has no first-release year; a song with
neither is left out of year filters, and the check counts the fallbacks.

This module is pure: models, the file, the filter and the pool-size
check.  It never touches the player.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from autodj.beets_query import (
    FIRST_RELEASE_YEAR,
    Query,
    QueryError,
    first_release_year,
    parse_query,
)
from autodj.fsutil import atomic_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Literal[1] = 1
FILE_NAME = "shows.json"
MAX_SHOWS = 100
NAME_MAX = 100
GENRE_MAX = 100
MAX_GENRES = 50
QUERY_MAX = 500
MAX_MINUTES = 1440
#: Songs listed by name where a report would otherwise grow without bound.
EXAMPLES = 20
#: A song length to plan with when the library does not know any.
DEFAULT_SONG_SECONDS = 210.0

#: The fields AutoDJ's own library holds for every song, beets or not.
LIBRARY_FIELDS = ("title", "artist", "album", "genre", "year", "bpm", "length")

# Words for each field, used in error messages that name the field.
FIELD_LABELS = {
    "name": "Name",
    "year_from": "First-release year from",
    "year_to": "First-release year to",
    "genres_include": "Genres to include",
    "genres_exclude": "Genres to leave out",
    "query": "Beets query",
    "clockwheel": "Clockwheel",
    "intro": "Intro liner",
    "outro": "Outro liner",
    "liner_folder": "Show liner folder",
    "start": "Start",
    "end": "End",
    "default_minutes": "Length when started now",
    "minutes": "Length",
}


class ShowError(ValueError):
    """A rejected show or start, naming the field at fault (``""`` for none)."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name


Name = Annotated[
    StrictStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)
]
Genre = Annotated[
    StrictStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=GENRE_MAX)
]
CatalogId = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
SourceId = Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Timing = Literal["soft", "hard"]


def split_genres(value: Any) -> list[str]:
    """The genres in one genre tag: beets joins several with commas or semicolons."""
    if value is None:
        return []
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    for separator in (";", "\0"):
        text = text.replace(separator, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _unique(values: Iterable[str]) -> list[str]:
    """*values* with case-insensitive duplicates dropped, first spelling kept."""
    seen: set[str] = set()
    kept = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            kept.append(value)
    return kept


class LinerFolder(BaseModel):
    """A folder inside a registered voice liner folder: its source ID and the path in it."""

    model_config = ConfigDict(extra="forbid")

    source: SourceId
    folder: Annotated[StrictStr, StringConstraints(max_length=1024)] = ""

    @field_validator("folder")
    @classmethod
    def _inside(cls, value: str) -> str:
        from autodj.clockwheel_store import relative_path

        return relative_path(value, label="The folder", allow_empty=True)


class ShowSpec(BaseModel):
    """The editable part of a show, as the API receives it."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    year_from: StrictInt | None = Field(default=None, ge=1, le=9999)
    year_to: StrictInt | None = Field(default=None, ge=1, le=9999)
    genres_include: list[Genre] = Field(default_factory=list, max_length=MAX_GENRES)
    genres_exclude: list[Genre] = Field(default_factory=list, max_length=MAX_GENRES)
    query: Annotated[StrictStr, StringConstraints(strip_whitespace=True, max_length=QUERY_MAX)] = ""
    clockwheel: StrictStr | None = None
    intro: CatalogId | None = None
    outro: CatalogId | None = None
    liner_folder: LinerFolder | None = None
    start: Timing = "soft"
    end: Timing = "soft"
    default_minutes: StrictInt = Field(default=60, ge=1, le=MAX_MINUTES)

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Check the parts that depend on each other, and the query's syntax."""
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ShowError("year_to", "The year range ends before it starts")
        self.genres_include = _unique(self.genres_include)
        self.genres_exclude = _unique(self.genres_exclude)
        both = {g.casefold() for g in self.genres_include} & {
            g.casefold() for g in self.genres_exclude
        }
        if both:
            raise ShowError(
                "genres_exclude",
                f"{sorted(both)[0]} is both included and left out; choose one",
            )
        try:
            parse_query(self.query)
        except QueryError as exc:
            raise ShowError("query", str(exc)) from None
        if self.clockwheel is not None:
            from autodj.clockwheel_store import validate_name

            try:
                validate_name(self.clockwheel)
            except ValueError as exc:
                raise ShowError("clockwheel", str(exc)) from None
        return self

    def track_filter(self) -> TrackFilter:
        """The song filter this show describes."""
        return TrackFilter(
            self.year_from,
            self.year_to,
            frozenset(g.casefold() for g in self.genres_include),
            frozenset(g.casefold() for g in self.genres_exclude),
            parse_query(self.query),
        )


class Show(ShowSpec):
    """A stored show: the spec plus its server-assigned ID."""

    id: Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ClockState(BaseModel):
    """The station's clockwheel before a show switched it: name and place."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr | None = None
    cursor: StrictInt = 0


class Resume(BaseModel):
    """How the station picked songs before the first of a run of shows started."""

    model_config = ConfigDict(extra="forbid")

    pick_mode: StrictStr
    transition_mode: StrictStr
    clock: ClockState | None = None


class ActiveShow(BaseModel):
    """The show on air, kept so a restart carries on with it until its end."""

    model_config = ConfigDict(extra="forbid")

    show_id: Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    run_id: Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    name: StrictStr
    started_at: datetime
    ends_at: datetime
    resume: Resume | None = None


class ShowsFile(BaseModel):
    """The on-disk shows document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    shows: list[Show] = Field(max_length=MAX_SHOWS)
    active: ActiveShow | None = None

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len({show.id for show in self.shows}) != len(self.shows):
            raise ValueError("duplicate show ID")
        if len({show.name.casefold() for show in self.shows}) != len(self.shows):
            raise ValueError("duplicate show name")
        return self


def field_error(exc: ValidationError) -> tuple[str, str]:
    """Return the field and a message naming it for the first error in *exc*."""
    error = exc.errors()[0]
    original = (error.get("ctx") or {}).get("error")
    if isinstance(original, ShowError):
        return original.field, str(original)
    loc = [str(part) for part in error["loc"]]
    if not loc:
        return "", str(error["msg"])
    if error["type"] == "extra_forbidden":
        return loc[0], f"Unknown field {loc[0]}"
    if loc[0] == "name":
        return "name", f"Enter a name of up to {NAME_MAX} characters"
    label = FIELD_LABELS.get(loc[0], loc[0])
    if loc[0] in ("year_from", "year_to"):
        return loc[0], f"{label} must be a year from 1 to 9999"
    if loc[0] in ("default_minutes", "minutes"):
        return loc[0], f"{label} must be a whole number of minutes from 1 to {MAX_MINUTES}"
    if loc[0] in ("intro", "outro"):
        return loc[0], f"{label}: choose one of the voice liners"
    if loc[0] == "liner_folder" and isinstance(original, ValueError):
        return loc[0], f"{label}: {original}"
    return loc[0], f"{label}: {error['msg']}"


def parse_spec(body: Any) -> ShowSpec:
    """Validate a request body as a show, raising :class:`ShowError` naming the field."""
    try:
        return ShowSpec.model_validate(body)
    except ValidationError as exc:
        raise ShowError(*field_error(exc)) from exc


# ----------------------------------------------------------------------
# The file
# ----------------------------------------------------------------------


class ShowStore:
    """``shows.json``: the shows and the one on air, written atomically.

    A file that cannot be read is set aside as ``shows.json.bad-<id>`` and
    the station starts with no shows, as the schedule does.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _keep_bad_copy(self, reason: str) -> None:
        bad = self.path.with_name(f"{self.path.name}.bad-{uuid.uuid4().hex[:8]}")
        try:
            os.replace(self.path, bad)
        except OSError:
            logger.exception("Could not set aside the invalid shows file %s", self.path)
            return
        logger.error("Shows file %s is invalid (%s); set it aside as %s", self.path, reason, bad)

    def load(self) -> ShowsFile:
        """Read the file; a missing file is no shows, a bad one is set aside."""
        empty = ShowsFile(schema_version=SCHEMA_VERSION, shows=[])
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return empty
        except OSError as exc:
            logger.error("Could not read the shows file %s: %s", self.path, exc)
            return empty
        try:
            return ShowsFile.model_validate_json(data)
        except ValueError as exc:
            self._keep_bad_copy(str(exc).splitlines()[0])
            return empty

    def save(self, document: ShowsFile) -> None:
        """Write *document* atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, document.model_dump_json(indent=2) + "\n")


# ----------------------------------------------------------------------
# The filter
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TrackFilter:
    """The form and the query of one show; every part must match."""

    year_from: int | None = None
    year_to: int | None = None
    genres_include: frozenset[str] = frozenset()
    genres_exclude: frozenset[str] = frozenset()
    query: Query = field(default_factory=Query)

    def uses_year(self) -> bool:
        """Whether the filter reads the first-release year."""
        return (
            self.year_from is not None
            or self.year_to is not None
            or self.query.uses_first_release_year()
        )

    def fields(self) -> frozenset[str]:
        """The beets fields evaluating the filter reads."""
        names = set(self.query.fields())
        if self.uses_year():
            names.update((FIRST_RELEASE_YEAR, "year"))
        if self.genres_include or self.genres_exclude:
            names.add("genre")
        return frozenset(names)

    def evaluate(self, item: Mapping[str, Any]) -> tuple[bool, bool]:
        """Return whether *item* passes and whether its year came from the fallback."""
        year, fell_back = first_release_year(item)
        if self.year_from is not None or self.year_to is not None:
            if year is None:
                return False, False
            if self.year_from is not None and year < self.year_from:
                return False, False
            if self.year_to is not None and year > self.year_to:
                return False, False
        if self.genres_include or self.genres_exclude:
            genres = {genre.casefold() for genre in split_genres(item.get("genre"))}
            if self.genres_include and not genres & self.genres_include:
                return False, False
            if genres & self.genres_exclude:
                return False, False
        if not self.query.matches(item):
            return False, False
        return True, fell_back and self.uses_year()


def library_item(entry: Any) -> dict[str, Any]:
    """The fields AutoDJ's own library holds for *entry*, as a query reads them."""
    return {name: getattr(entry, name, None) for name in LIBRARY_FIELDS}


@dataclass(frozen=True)
class BeetsSource:
    """The live beets database a filter reads, and the folder its relative paths start in."""

    db_path: Path
    music_dir: Path | None = None

    def fields(self) -> frozenset[str]:
        """Every field beets holds for items, flexible attributes included."""
        from autodj.beets import item_fields

        return item_fields(self.db_path)

    def items(self, fields: Iterable[str]) -> dict[str, dict[str, Any]]:
        """The values of *fields* for every beets item, by AutoDJ's library path."""
        from autodj.beets import read_items
        from autodj.catalog import normalize_path

        found: dict[str, dict[str, Any]] = {}
        for path, values in read_items(self.db_path, fields):
            raw = str(path).replace("\\", "/")
            absolute = raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":")
            if not absolute and self.music_dir is not None:
                path = Path(self.music_dir) / raw.lstrip("/")
            found[normalize_path(path)] = values
        return found


@dataclass(frozen=True)
class FilterResult:
    """What a filter matches in the library.

    Attributes:
        paths: The library paths of the matching songs.
        fallback: Matching songs whose year came from ``year`` because
            beets has no first-release year for them (only counted when
            the filter reads the year).
        errors: ``(field, message)`` for each problem, in words.
        beets: Whether the beets database was read.
        matched: The matching songs, for the pool-size check.
    """

    paths: frozenset[str] = frozenset()
    fallback: tuple[Any, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()
    beets: bool = False
    matched: tuple[Any, ...] = ()

    @property
    def count(self) -> int:
        return len(self.paths)


def count_text(count: int) -> str:
    """Say a match count: "1 song matches", "1,284 songs match"."""
    return f"{count:,} song matches" if count == 1 else f"{count:,} songs match"


def fallback_text(count: int) -> str:
    """Say how many matches used the release year."""
    if not count:
        return ""
    verb = "uses" if count == 1 else "use"
    return f"{count:,} {verb} the release year because the first-release year is missing"


def match_library(
    spec: ShowSpec,
    entries: Sequence[Any],
    beets: BeetsSource | None,
) -> FilterResult:
    """Evaluate *spec*'s filter against every song in *entries*.

    Songs are AutoDJ's library entries; beets values, when the database
    is configured, are laid over the library's own fields.  A query that
    names a field no song has is an error, since it can never match.
    """
    from autodj.catalog import normalize_path

    track_filter = spec.track_filter()
    fields = track_filter.fields()
    known = set(LIBRARY_FIELDS) | {FIRST_RELEASE_YEAR}
    overlay: dict[str, dict[str, Any]] = {}
    errors: list[tuple[str, str]] = []
    read_beets = False
    if beets is not None:
        try:
            known |= beets.fields()
            overlay = beets.items(fields)
            read_beets = True
        except (OSError, sqlite3.Error) as exc:
            errors.append(("query", f"The beets database could not be read: {exc}"))
    unknown = sorted(track_filter.query.named_fields() - known)
    if unknown:
        where = "the library or the beets database" if read_beets else "the library"
        errors.append(
            (
                "query",
                f"No song in {where} has a field named {unknown[0]}. Check the spelling; "
                "beets fields look like original_year or albumartist",
            )
        )
    if errors:
        return FilterResult(errors=tuple(errors), beets=read_beets)
    paths: set[str] = set()
    fallback: list[Any] = []
    matched: list[Any] = []
    for entry in entries:
        if getattr(entry, "is_silent", False):
            continue
        item = library_item(entry)
        extra = overlay.get(normalize_path(entry.path)) if overlay else None
        if extra:
            item.update(extra)
        passes, fell_back = track_filter.evaluate(item)
        if not passes:
            continue
        paths.add(entry.path)
        matched.append(entry)
        if fell_back:
            fallback.append(entry)
    return FilterResult(frozenset(paths), tuple(fallback), (), read_beets, tuple(matched))


def library_genres(entries: Iterable[Any]) -> list[str]:
    """Every genre in the library, sorted, each spelled as it first appears."""
    return sorted(
        _unique(genre for entry in entries for genre in split_genres(getattr(entry, "genre", ""))),
        key=str.casefold,
    )


# ----------------------------------------------------------------------
# Is the pool big enough?
# ----------------------------------------------------------------------


def _window(tracks: int, minutes: float, song_seconds: float) -> int:
    """A rule's reach in songs: its track count, or its minutes in songs."""
    by_minutes = math.ceil(minutes * 60 / song_seconds) if minutes > 0 else 0
    return max(int(tracks), by_minutes)


def pool_warning(
    songs: Sequence[Any],
    rules: Any,
    minutes: int,
    keys: Any = None,
    *,
    dmca: bool = False,
) -> str | None:
    """Warn in words when *songs* cannot fill *minutes* under the repeat rules.

    A rule that holds a song, artist, album or title back for more songs
    than the pool has of them stops the show once each has played once:
    after that every song is held back and the rules must relax.  So the
    songs that fit a rule are the distinct values it compares when it
    reaches at least that far, and unlimited otherwise.  In DMCA mode
    the three-hour limits (4 songs an artist, 3 an album) cap the songs
    that fit in any three hours of the show.

    Args:
        songs: The songs that pass the show's filter.
        rules: The station's :class:`~autodj.repeat_rules.RepeatRules`.
        minutes: How long the show runs.
        keys: A :class:`~autodj.repeat_rules.TrackKeyCache` that splits
            artist credits as the player does; a plain one by default.
        dmca: Whether DMCA mode is on.

    Returns:
        "about 30 songs needed; only 22 fit your artist rule", or ``None``
        when the pool is big enough.
    """
    from autodj.dmca import ALBUM_LIMIT, ARTIST_LIMIT, WINDOW_SECONDS
    from autodj.repeat_rules import TrackKeyCache

    keys = keys or TrackKeyCache()
    lengths = [float(song.length) for song in songs if float(song.length or 0) > 0]
    song_seconds = sum(lengths) / len(lengths) if lengths else DEFAULT_SONG_SECONDS
    needed = max(1, math.ceil(minutes * 60 / song_seconds))
    if not songs:
        return f"about {needed:,} songs needed; no song matches the show"
    track_keys = [keys.keys(song) for song in songs]
    artists: set[str] = set()
    albums: set[str] = set()
    titles: set[str] = set()
    no_album = no_title = 0
    for item in track_keys:
        artists |= item.artists or {""}
        if item.album:
            albums.add(item.album)
        else:
            no_album += 1
        if item.title:
            titles.add(item.title)
        else:
            no_title += 1
    candidates = [
        ("track", len(songs), _window(rules.track_window, rules.track_minutes, song_seconds)),
        ("artist", len(artists), _window(rules.artist_window, rules.artist_minutes, song_seconds)),
        (
            "album",
            len(albums) + no_album,
            _window(rules.album_window, rules.album_minutes, song_seconds),
        ),
        (
            "title",
            len(titles) + no_title,
            _window(rules.title_window, rules.title_minutes, song_seconds),
        ),
    ]
    shortest: tuple[int, str] | None = None
    for rule, distinct, reach in candidates:
        fits_less = reach > 0 and distinct <= reach and distinct < needed
        if fits_less and (shortest is None or distinct < shortest[0]):
            shortest = (distinct, f"your {rule} rule")
    if dmca:
        window = min(minutes * 60, WINDOW_SECONDS)
        needed_in_window = max(1, math.ceil(window / song_seconds))
        by_artist: dict[str, int] = {}
        by_album: dict[str, int] = {}
        for item in track_keys:
            for artist in item.artists or {""}:
                by_artist[artist] = by_artist.get(artist, 0) + 1
            if item.album:
                by_album[item.album] = by_album.get(item.album, 0) + 1
        fit_artist = sum(min(count, ARTIST_LIMIT) for count in by_artist.values())
        fit_album = sum(min(count, ALBUM_LIMIT) for count in by_album.values()) + no_album
        fit = min(fit_artist, fit_album, len(songs))
        if fit < needed_in_window and (shortest is None or fit < shortest[0]):
            shortest = (fit, "the DMCA limits")
            needed = needed_in_window
    if shortest is None:
        return None
    fit, rule = shortest
    return f"about {needed:,} songs needed; only {fit:,} fit {rule}"


# ----------------------------------------------------------------------
# Words
# ----------------------------------------------------------------------


def _join(words: Sequence[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def describe(spec: ShowSpec, liner_name: Callable[[str], str] | None = None) -> str:
    """Say what a show plays and how it starts and ends, for its row in the list."""
    parts: list[str] = []
    if spec.year_from is not None and spec.year_to is not None:
        parts.append(f"First released {spec.year_from} to {spec.year_to}")
    elif spec.year_from is not None:
        parts.append(f"First released in {spec.year_from} or later")
    elif spec.year_to is not None:
        parts.append(f"First released in {spec.year_to} or earlier")
    if spec.genres_include:
        parts.append(f"Genres {_join(spec.genres_include)}")
    if spec.genres_exclude:
        parts.append(f"Leaves out {_join(spec.genres_exclude)}")
    if spec.query:
        parts.append(f"Beets query {spec.query}")
    if not parts:
        parts.append("Every song")
    if spec.clockwheel:
        parts.append(f"Clockwheel {spec.clockwheel}")
    name = liner_name or (lambda value: value)
    if spec.intro:
        parts.append(f"Intro {name(spec.intro)}")
    if spec.outro:
        parts.append(f"Outro {name(spec.outro)}")
    if spec.liner_folder is not None:
        parts.append("Its own liner folder")
    parts.append(f"{spec.start.capitalize()} start, {spec.end} end")
    parts.append(f"{spec.default_minutes} minutes when started now")
    return ". ".join(parts) + "."
