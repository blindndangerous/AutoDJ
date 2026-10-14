"""The performance complement for DMCA mode (the US statutory webcasting licence).

General information, not legal advice.  In any three hours of actual plays,
counting the song about to play:

- at most 3 songs from one album, and at most 2 of them in a row;
- at most 4 songs by one artist, and at most 3 of them in a row;
- at most 4 songs from one box set or compilation, and at most 3 in a row.

Artists are the keys :class:`~autodj.repeat_rules.TrackKeyCache` gives, so
every artist a credit names counts, featured ones ("feat." in the artist or
the title) included.  Albums are compared by
:func:`~autodj.repeat_rules.album_key`, so two artists' albums that share a
name count as one album: stricter than needed, never looser.

A box set or compilation is detected from the album name: albums whose
names end in a disc or volume number ("Woodstock (Disc 2)", "CD 1",
"Vol. 3", "Disc 1 of 4") count as one set, named without the number.  The
index has no album-artist or compilation tag, and a one-disc compilation
needs none: it is one album, and the album limit is the stricter one.  A
box set tagged with one album name for every disc counts as one album,
again the stricter limit.

The module is pure: the player builds a :class:`Complement` from its play
history for each pick, and the queue projects the history to a queued
song's expected start with :func:`projected_history`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from autodj.repeat_rules import (
    PlayRecord,
    TrackKeyCache,
    TrackKeys,
    artist_key,
    featured_in_title,
)

#: The window every limit counts in: three hours.
WINDOW_SECONDS = 3 * 60 * 60

ALBUM_LIMIT, ALBUM_IN_A_ROW = 3, 2
ARTIST_LIMIT, ARTIST_IN_A_ROW = 4, 3
SET_LIMIT, SET_IN_A_ROW = 4, 3

_DISC_NUMBER = re.compile(r"\s+(?:disc|disk|cd|volume|vol)\s*\d+(?:\s+of\s+\d+)?$")


def set_key(album: str) -> str:
    """The box set an album key belongs to, or ``""`` when its name has no disc number."""
    stripped = _DISC_NUMBER.sub("", album)
    return stripped if stripped != album else ""


def _run(values: Sequence[str]) -> tuple[str, int]:
    """The last of *values* and how many times in a row it ends them (``("", 0)`` if unset)."""
    if not values or not values[-1]:
        return "", 0
    last, count = values[-1], 0
    for value in reversed(values):
        if value != last:
            break
        count += 1
    return last, count


def _artist_name(track: Any, key: str) -> str:
    """The written name of artist *key* on *track*, or the key when no credit is exactly it."""
    credits = [track.artist or "", *featured_in_title(track.title or "")]
    return next((credit for credit in credits if artist_key(credit) == key), key)


@dataclass(frozen=True)
class Complement:
    """What the plays in one three-hour window allow next.

    Build it with :func:`complement`; :meth:`broken` then checks a
    candidate with a few dictionary lookups, so a pick can check the whole
    library.
    """

    keys: TrackKeyCache
    albums: Mapping[str, int]
    artists: Mapping[str, int]
    sets: Mapping[str, int]
    album_run: tuple[str, int]
    set_run: tuple[str, int]
    artist_runs: Mapping[str, int]

    def broken(self, track: Any) -> tuple[str, ...]:
        """The limits *track* would go over if it played next, in words; empty when none."""
        keys = self.keys.keys(track)
        album, box = keys.album, set_key(keys.album)
        found: list[str] = []
        if album and self.albums.get(album, 0) >= ALBUM_LIMIT:
            found.append(f"more than {ALBUM_LIMIT} songs from the album “{track.album}” in 3 hours")
        if album and self.album_run[0] == album and self.album_run[1] >= ALBUM_IN_A_ROW:
            found.append(
                f"more than {ALBUM_IN_A_ROW} songs in a row from the album “{track.album}”"
            )
        for artist in sorted(keys.artists):
            if self.artists.get(artist, 0) >= ARTIST_LIMIT:
                found.append(
                    f"more than {ARTIST_LIMIT} songs by {_artist_name(track, artist)} in 3 hours"
                )
            if self.artist_runs.get(artist, 0) >= ARTIST_IN_A_ROW:
                found.append(
                    f"more than {ARTIST_IN_A_ROW} songs in a row by {_artist_name(track, artist)}"
                )
        part_of = f"the box set or compilation that “{track.album}” is part of"
        if box and self.sets.get(box, 0) >= SET_LIMIT:
            found.append(f"more than {SET_LIMIT} songs from {part_of} in 3 hours")
        if box and self.set_run[0] == box and self.set_run[1] >= SET_IN_A_ROW:
            found.append(f"more than {SET_IN_A_ROW} songs in a row from {part_of}")
        return tuple(found)

    @cached_property
    def _blocked(self) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        """The album, artist and box set keys one more play of would go over a limit."""
        albums = {album for album, count in self.albums.items() if count >= ALBUM_LIMIT}
        if self.album_run[1] >= ALBUM_IN_A_ROW:
            albums.add(self.album_run[0])
        artists = {artist for artist, count in self.artists.items() if count >= ARTIST_LIMIT}
        artists |= {artist for artist, run in self.artist_runs.items() if run >= ARTIST_IN_A_ROW}
        sets = {box for box, count in self.sets.items() if count >= SET_LIMIT}
        if self.set_run[1] >= SET_IN_A_ROW:
            sets.add(self.set_run[0])
        return frozenset(albums), frozenset(artists), frozenset(sets)

    @property
    def blocks_nothing(self) -> bool:
        """True when every track keeps every limit, so a pick need not check them."""
        return not any(self._blocked)

    def allows(self, track: Any) -> bool:
        """True when *track* keeps every limit: :meth:`broken` would find none.

        A pick checks every track in the library, so this only looks the
        track's keys up in the few keys that are at a limit, and not even
        that while none is.
        """
        albums, artists, sets = self._blocked
        if not (albums or artists or sets):
            return True
        keys = self.keys.keys(track)
        album = keys.album
        if album and album in albums:
            return False
        if not keys.artists.isdisjoint(artists):
            return False
        return not (sets and album and set_key(album) in sets)


def complement(history: Sequence[PlayRecord], when: float, keys: TrackKeyCache) -> Complement:
    """What *history* (oldest first) allows to start at *when*.

    Only plays that started less than three hours before *when*, and not
    after it, count; "in a row" means the plays straight before it.  A track
    with no album or no artist counts toward no album or artist.
    """
    recent: list[TrackKeys] = [
        keys.keys(play) for play in history if 0 <= when - play.started < WINDOW_SECONDS
    ]
    artist_runs: dict[str, int] = {}
    for artist in recent[-1].artists if recent else ():
        run = 0
        for track in reversed(recent):
            if artist not in track.artists:
                break
            run += 1
        artist_runs[artist] = run
    return Complement(
        keys=keys,
        albums=Counter(track.album for track in recent if track.album),
        artists=Counter(artist for track in recent for artist in track.artists),
        sets=Counter(box for track in recent if (box := set_key(track.album))),
        album_run=_run([track.album for track in recent]),
        set_run=_run([set_key(track.album) for track in recent]),
        artist_runs=artist_runs,
    )


def relaxed[T](
    tiers: Iterable[Sequence[T]], limits: Callable[[T], tuple[str, ...]]
) -> list[tuple[T, tuple[str, ...]]]:
    """The candidates to choose among when the first choice goes over a limit.

    *tiers* are candidate lists from narrowest to widest (such as: tracks
    the repeat rules allow, then any track not played recently, then any
    track).  The first tier with tracks that keep every limit gives them,
    each with ``()``.  When no tier has one, music still plays: the tracks
    of the widest tier that break the fewest limits are given, each with
    the limits it breaks.  Empty only when every tier is.
    """
    widest: list[tuple[T, tuple[str, ...]]] = []
    for tier in tiers:
        scored = [(track, limits(track)) for track in tier]
        if safe := [pair for pair in scored if not pair[1]]:
            return safe
        widest = scored or widest
    fewest = min((len(broken) for _, broken in widest), default=0)
    return [pair for pair in widest if len(pair[1]) == fewest]


def projected_history(
    history: Sequence[PlayRecord],
    now: float,
    playing: Any | None,
    waiting: Sequence[Any],
    *,
    length: Callable[[Any], float] | None = None,
) -> tuple[list[PlayRecord], float]:
    """The history as it will stand when a song queued after *waiting* starts, and that time.

    Args:
        history: The plays so far, oldest first.
        now: The time now.
        playing: The track playing, which ends its length after its
            recorded start (it counts as starting now when it is not the
            latest play); ``None`` when the queued song starts now.
        waiting: Tracks that play first, each for its length.
        length: How long a track plays, in seconds; its file length when
            ``None``.  Music speed makes music play shorter or longer.
    """
    played = length if length is not None else (lambda entry: float(entry.length))
    records = list(history)
    when = now
    if playing is not None:
        if records and records[-1].path == playing.path:
            started = records[-1].started
        else:
            started = now
            records.append(PlayRecord.of(playing, now))
        when = max(now, started + max(0.0, played(playing)))
    for entry in waiting:
        records.append(PlayRecord.of(entry, when))
        when += max(0.0, played(entry))
    return records, when


__all__ = [
    "ALBUM_IN_A_ROW",
    "ALBUM_LIMIT",
    "ARTIST_IN_A_ROW",
    "ARTIST_LIMIT",
    "SET_IN_A_ROW",
    "SET_LIMIT",
    "WINDOW_SECONDS",
    "Complement",
    "complement",
    "projected_history",
    "relaxed",
    "set_key",
]
