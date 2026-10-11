"""Export the play log for royalty reporting and station logs.

Two formats: a general CSV with one row per play, and a SoundExchange
Report of Use for US webcasters (see :func:`soundexchange_report`).

The play log is the ``play_history`` table in the library catalog, which
keeps every completed play without expiry.  One row is written per play
that started inside the requested range:

- ``start`` and ``end``: ISO 8601 with the local UTC offset, to the second.
- ``duration_played``: whole seconds from the start of this play to the
  start of the next one, capped at the track length.  A crossfade's
  overlap therefore counts toward the incoming song, and a skipped song
  shows how long it played before the next one started.
- ``artist`` and ``title`` as shown when it played; ``album`` from the
  catalog record.
- ``isrc`` and ``label``, read from the file's tags at export time, so no
  re-index is needed.
- ``kind``: ``song`` or ``liner``.  The mix bus logs only the tracks it
  starts, so liners mixed over a transition are not in the log, and
  nothing yet marks a file as an ad or a station ID.
- ``path``: relative to the music or liner folder that holds the file,
  with ``/`` separators; absolute when no folder holds it.
- ``listeners``: in stream mode, the most stream listeners connected at
  once during the play (counted when it starts and whenever one joins);
  otherwise 1, the person at the computer or browser.  Empty for plays
  logged before AutoDJ counted listeners.

Cells starting with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return
get a leading ``'`` so a spreadsheet shows them as text rather than
running them as formulas.  The text is UTF-8 without a byte order mark:
reporting services and scripts read it as is, and a BOM would end up in
the first column name.
"""

from __future__ import annotations

import csv
import io
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import PurePath

from autodj.audio_meta import read_reporting_tags
from autodj.catalog import LibraryCatalog, _under

COLUMNS = (
    "start",
    "end",
    "duration_played",
    "artist",
    "title",
    "album",
    "isrc",
    "label",
    "kind",
    "path",
    "listeners",
)
# What a royalty report needs for every song.
REPORTING_FIELDS = ("artist", "title", "album", "isrc", "label", "listeners")
MISSING_COLUMNS = ("artist", "title", "path", "missing", "plays")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Play:
    """One exported play."""

    start: datetime
    end: datetime
    artist: str
    title: str
    album: str
    isrc: str
    label: str
    kind: str
    path: str
    listeners: int | None = None

    @property
    def duration(self) -> int:
        return round((self.end - self.start).total_seconds())

    def missing(self) -> list[str]:
        """Return the reporting fields this play has no value for."""
        return [
            name
            for name in REPORTING_FIELDS
            if getattr(self, name) is None or getattr(self, name) == ""
        ]


def parse_bound(text: str, *, end: bool = False) -> datetime:
    """Parse one end of an export range.

    Accepts ``YYYY-MM-DD`` or an ISO 8601 date and time; one without an
    offset is in the local time zone.  A date given as the *end* means the
    whole of that day, so the bound is the following midnight.

    Raises:
        ValueError: *text* is neither.
    """
    value = text.strip()
    try:
        day = date.fromisoformat(value)
    except ValueError:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f"{text!r} is not a date (YYYY-MM-DD) or an ISO 8601 date and time"
            ) from None
    else:
        moment = datetime.combine(day + timedelta(days=1) if end else day, time())
    return moment if moment.tzinfo else moment.astimezone()


def _started(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def _relative(path: str, sources: Sequence[dict]) -> str:
    """Return *path* relative to the outermost source folder that holds it."""
    for source in sources:  # sorted by path, so an outer folder comes first
        root = source["path"]
        if not _under(path, root, source["recursive"]):
            continue
        relative = os.path.relpath(path, root)
        if os.name == "nt":  # pragma: no cover -- Windows only
            # The catalog keeps paths lower-cased on Windows; ask the file
            # system for the real spelling, keeping it only if it agrees.
            try:
                real = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
            except (OSError, ValueError):
                real = relative
            if os.path.normcase(real) == os.path.normcase(relative):
                relative = real
        return PurePath(relative).as_posix()
    return PurePath(path).as_posix()


def plays_between(
    catalog: LibraryCatalog,
    start: datetime,
    end: datetime,
    *,
    read_tags: Callable[[str], tuple[str, str]] = read_reporting_tags,
) -> list[Play]:
    """Return the plays that started at or after *start* and before *end*.

    Raises:
        ValueError: *end* is not after *start*.
    """
    if end <= start:
        raise ValueError("The end of the range must be after its start.")
    sources = catalog.sources()
    liner_sources = [source for source in sources if source["kind"] == "liner"]
    rows = catalog.play_log()
    starts = [_started(row["played_at"]) for row in rows]
    files: dict[str, tuple[str, str, str]] = {}
    plays = []
    for index, row in enumerate(rows):
        started = starts[index]
        if started is None or not start <= started < end:
            continue
        length = float(row["duration"])
        following = starts[index + 1] if index + 1 < len(starts) else None
        if following is not None and following >= started:
            length = min(length, (following - started).total_seconds())
        path = row["path"] or ""
        if path and path not in files:
            isrc, label = read_tags(path)
            files[path] = (_relative(path, sources), isrc, label)
        relative, isrc, label = files.get(path, ("", "", ""))
        # A play with no catalog record is a liner when a liner folder holds it.
        kind = row["kind"] or (
            "liner"
            if any(_under(path, s["path"], s["recursive"]) for s in liner_sources)
            else "music"
        )
        plays.append(
            Play(
                start=started,
                end=started + timedelta(seconds=round(length)),
                artist=row["artist"],
                title=row["title"],
                album=row["album"] or "",
                isrc=isrc,
                label=label,
                kind="liner" if kind == "liner" else "song",
                path=relative,
                listeners=row["listeners"],
            )
        )
    return plays


def _cell(value: object) -> str:
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _csv(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows([_cell(value) for value in row] for row in rows)
    return out.getvalue()


def _local(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


def plays_csv(plays: Iterable[Play]) -> str:
    """Return *plays* as CSV text with :data:`COLUMNS` as the header row."""
    return _csv(
        COLUMNS,
        (
            (
                _local(p.start),
                _local(p.end),
                p.duration,
                p.artist,
                p.title,
                p.album,
                p.isrc,
                p.label,
                p.kind,
                p.path,
                "" if p.listeners is None else p.listeners,
            )
            for p in plays
        ),
    )


def missing_fields(plays: Iterable[Play]) -> list[tuple[str, str, str, str, int]]:
    """Return each song lacking a reporting field, once, sorted by artist and title.

    Each item is ``(artist, title, path, missing, plays)``; ``missing``
    names the empty fields, separated by ``"; "``.  Liners are left out.
    """
    counts: dict[tuple[str, str, str], int] = {}
    lacking: dict[tuple[str, str, str], str] = {}
    for play in plays:
        missing = play.missing()
        if play.kind != "song" or not missing:
            continue
        key = (play.artist, play.title, play.path)
        counts[key] = counts.get(key, 0) + 1
        lacking.setdefault(key, "; ".join(missing))
    return sorted(
        (
            (artist, title, path, lacking[artist, title, path], count)
            for (artist, title, path), count in counts.items()
        ),
        key=lambda item: (item[0].casefold(), item[1].casefold(), item[2]),
    )


def missing_csv(plays: Iterable[Play]) -> str:
    """Return :func:`missing_fields` as CSV text with :data:`MISSING_COLUMNS`."""
    return _csv(MISSING_COLUMNS, missing_fields(plays))


# The columns of SoundExchange's Report of Use templates for commercial
# and noncommercial webcasters, in their order.
SOUNDEXCHANGE_COLUMNS = (
    "NAME_OF_SERVICE",
    "TRANSMISSION_CATEGORY",
    "FEATURED_ARTIST",
    "SOUND_RECORDING_TITLE",
    "ISRC",
    "ALBUM_TITLE",
    "MARKETING_LABEL",
    "ACTUAL_TOTAL_PERFORMANCES",
    "AGGREGATE_TUNING_HOURS",
    "CHANNEL_OR_PROGRAM_NAME",
    "PLAY_FREQUENCY",
)
# The template's transmission category codes; D to G are reserved.
TRANSMISSION_CATEGORIES = "ABCHIJKL"
_NOT_ALLOWED = re.compile(r"[|^\x00-\x1f\x7f]")


def _ascii(text: str) -> str:
    """Return *text* as one line of printable ASCII with no ``|`` or ``^``.

    Accents are dropped (``Björk`` becomes ``Bjork``); any other character
    outside ASCII becomes ``?``.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    plain = plain.encode("ascii", "replace").decode("ascii")
    return " ".join(_NOT_ALLOWED.sub(" ", plain).split())


def _text_field(text: str) -> str:
    value = _ascii(text)
    return f"^{value}^" if value else ""


def soundexchange_report(
    plays: Iterable[Play], service: str, category: str = "A", channel: str = ""
) -> str:
    """Return a SoundExchange Report of Use for *plays*.

    Only songs are reported: liners, station IDs and ads are not sound
    recordings.  There is one line per recording (same artist, title,
    ISRC, album and label), in :data:`SOUNDEXCHANGE_COLUMNS` order:

    - ACTUAL_TOTAL_PERFORMANCES adds up each play's listeners, and is left
      empty when a play of that recording has no count.
    - PLAY_FREQUENCY is the number of plays.
    - AGGREGATE_TUNING_HOURS is left empty; CHANNEL_OR_PROGRAM_NAME is
      *channel*.

    The layout is the one 37 CFR 370.4 sets for a file without headers:
    ASCII, ``|`` between fields, ``^`` around text and nothing around
    numbers, no header row, and empty fields still delimited.

    Raises:
        ValueError: *service* is empty, or *category* is not a code in
            :data:`TRANSMISSION_CATEGORIES`.
    """
    name = _text_field(service)
    if not name:
        raise ValueError("A SoundExchange report needs the name of the service.")
    code = category.strip().upper()
    if len(code) != 1 or code not in TRANSMISSION_CATEGORIES:
        raise ValueError(
            "The transmission category must be one of " + ", ".join(TRANSMISSION_CATEGORIES) + "."
        )
    totals: dict[tuple[str, str, str, str, str], list[int | None]] = {}
    for play in plays:
        if play.kind != "song":
            continue
        key = (play.artist, play.title, play.isrc, play.album, play.label)
        totals.setdefault(key, []).append(play.listeners)
    lines = []
    for (artist, title, isrc, album, label), counts in sorted(
        totals.items(), key=lambda item: (item[0][0].casefold(), item[0][1].casefold(), item[0])
    ):
        known = None if None in counts else sum(c for c in counts if c is not None)
        fields = [
            name,
            _text_field(code),
            _text_field(artist),
            _text_field(title),
            _text_field(isrc),
            _text_field(album),
            _text_field(label),
            "" if known is None else str(known),
            "",
            _text_field(channel),
            str(len(counts)),
        ]
        lines.append("|".join(fields) + "\r\n")
    return "".join(lines)


def soundexchange_filename(service: str, start: datetime, last_day: datetime) -> str:
    """Return the file name 37 CFR 370.4 asks for: service, then the dates, ``.txt``."""
    stem = re.sub(r"[^A-Za-z0-9]", "", _ascii(service)) or "Service"
    return f"{stem}{start:%Y%m%d}-{last_day:%Y%m%d}.txt"


def summary(plays: Sequence[Play]) -> str:
    """Return one sentence saying how many plays and incomplete songs there are."""
    count = len(plays)
    lacking = len(missing_fields(plays))
    text = f"{count} play{'' if count == 1 else 's'} exported."
    if lacking:
        text += (
            f" {lacking} song{' lacks' if lacking == 1 else 's lack'} ISRC, label,"
            " listener count or other reporting fields."
        )
    return text


__all__ = [
    "COLUMNS",
    "MISSING_COLUMNS",
    "REPORTING_FIELDS",
    "SOUNDEXCHANGE_COLUMNS",
    "TRANSMISSION_CATEGORIES",
    "Play",
    "missing_csv",
    "missing_fields",
    "parse_bound",
    "plays_between",
    "plays_csv",
    "soundexchange_filename",
    "soundexchange_report",
    "summary",
]
