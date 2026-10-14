"""Spoken track landmarks (#60): a song's structure in plain words.

A sighted DJ reads a song's shape from its waveform: where the intro ends,
where the drop hits, where the singing starts and where the outro begins.
This module gives the same picture as a short list of named moments that a
screen reader can say, such as "Drop, 1 minute 35 seconds, bar 49".

It merges what AutoDJ already knows about a song:

- its cue points in the catalog (Start, Intro, Outro and Segue), set by
  analysis, synced lyrics, DJ software or by hand in Track info.  Without a
  catalog record, the intro and outro of the DJ analysis stand in for them;
- the DJ analysis (``dj_meta.db``): the beat grid, for bar numbers, and the
  detected and imported cues (drops, breakdowns, build-ups, 32-beat
  phrases, and hot or memory cues named in DJ software);
- its synced lyrics: where the singing starts.

Marks less than :data:`MERGE_S` apart are one landmark.  Of two marks of
the same kind the one set by hand wins over DJ software, then lyrics, then
analysis.  A phrase or an unnamed cue that falls on another landmark adds
nothing and is dropped, and so is the end of the intro where the singing
starts.

Bars count from the first beat of the grid, as
:func:`autodj.dj_meta.nearest_phrase_boundary` does, so bar 1 is the
first detected beat.  A song with no grid has no bar numbers.

The route is a station route (``/api/landmarks`` and
``/api/stations/<slug>/landmarks``): the analysis cache and the lyrics are
read through that station's player.  Answers are kept in a small cache
until the song's cue points, analysis or lyrics change.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from statistics import median
from typing import Any

from fastapi import HTTPException, Query

from autodj.dj_meta import MARKER_CUE_TYPES, Cue, DjMeta
from autodj.station_routes import StationDep, StationRouter
from autodj.vocals import first_vocal_s

logger = logging.getLogger(__name__)

#: Marks closer together than this, in seconds, are one landmark.
MERGE_S = 1.0
#: A grid shorter than this many beats gives no bar numbers.
MIN_GRID_BEATS = 8
#: Longest name kept from a cue label set in DJ software.
MAX_LABEL = 60
#: An outro or Start this close to an end of the song is no landmark.
EDGE_S = 0.5

#: The plain name of each kind of landmark.
NAMES: dict[str, str] = {
    "start": "Start",
    "intro": "Intro",
    "intro_end": "Intro ends",
    "vocals": "Vocals",
    "build": "Build-up",
    "drop": "Drop",
    "breakdown": "Breakdown",
    "outro": "Outro",
    "segue": "Segue",
    "cue": "Cue",
    "phrase": "Phrase",
}

# Which kind names a landmark where several fall together, most telling first.
_KIND_ORDER = (
    "drop",
    "breakdown",
    "build",
    "vocals",
    "intro",
    "outro",
    "intro_end",
    "segue",
    "start",
    "cue",
    "phrase",
)
# Kinds that are not part of the song's structure: still stops for the
# landmark keys, never in the structure list or "what is coming".
_MINOR = frozenset({"phrase", "cue"})
# Who set a mark, highest first.
_PRIORITY = {"user": 4, "imported": 3, "lyrics": 2, "analysis": 1}
# DJ programs whose cues the import reads (autodj.dj_cues_import).
_DJ_SOFTWARE = frozenset({"mixxx", "rekordbox", "serato", "traktor"})
# Analysis cues that only time a mix, not a section: never landmarks.
_MIX_ONLY = frozenset({"first_downbeat", "outro_downbeat"})
_DROP_LABEL = re.compile(r"\bdrop\b", re.IGNORECASE)
_BREAKDOWN_LABEL = re.compile(r"\bbreak(?:down)?\b", re.IGNORECASE)
_BUILD_LABEL = re.compile(r"\bbuild(?:[ -]?up)?\b", re.IGNORECASE)


@dataclass
class _Mark:
    """One mark from one source, before marks that fall together merge."""

    time_s: float
    kind: str
    source: str
    name: str = ""
    end_s: float | None = None


@dataclass
class Landmark:
    """A named moment in a song.

    Attributes:
        time_s: Seconds from the start of the file.
        kind: Its main kind, a key of :data:`NAMES`.
        kinds: Every kind that falls here, main kind first.
        name: What it is called, in plain words ("Drop", "Drop and vocals",
            "Verse 2" for a named cue).
        source: Who set the main mark: ``user``, ``imported`` (DJ
            software), ``lyrics`` or ``analysis``.
        major: Part of the song's structure.  Phrases and unnamed cues are
            not: they are stops for the landmark keys only.
        bar: Bar number from the beat grid, or ``None`` without one.
        end_s: Where an intro or outro ends, else ``None``.
        end_bar: The bar of *end_s*.
    """

    time_s: float
    kind: str
    kinds: list[str]
    name: str
    source: str
    major: bool
    bar: int | None = None
    end_s: float | None = None
    end_bar: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """The landmark as the route answers it."""
        return {
            "time_s": round(self.time_s, 2),
            "kind": self.kind,
            "kinds": list(self.kinds),
            "name": self.name,
            "source": self.source,
            "major": self.major,
            "bar": self.bar,
            "end_s": None if self.end_s is None else round(self.end_s, 2),
            "end_bar": self.end_bar,
        }


@dataclass
class TrackFacts:
    """What :func:`build_landmarks` reads about one song.

    Attributes:
        length: Length in seconds; ``0`` when unknown.
        points: Catalog cue points (``start``, ``intro``, ``outro``,
            ``segue``...), or ``None`` without a catalog record, in which
            case the analysis intro and outro are used.
        point_sources: Who set each catalog point.
        meta: The DJ analysis, or ``None`` when the song has none.
        sung: Synced-lyric spans (:func:`autodj.vocals.sung_spans`).
    """

    length: float = 0.0
    points: Mapping[str, Any] | None = None
    point_sources: Mapping[str, str] = field(default_factory=dict)
    meta: DjMeta | None = None
    sung: Sequence[tuple[float, float]] = ()


def bar_number(beats: Sequence[float], time_s: float) -> int | None:
    """The bar *time_s* falls in, counted from the grid's first beat.

    Args:
        beats: Beat times in seconds, ascending.
        time_s: The moment, in seconds.

    Returns:
        1 for the first four beats, 2 for the next four and so on.  A
        moment just before the first beat (less than a bar) is in bar 1.
        ``None`` without a usable grid, or well before it starts.
    """
    if len(beats) < MIN_GRID_BEATS or time_s < 0:
        return None
    index = bisect_right(beats, time_s + 0.05) - 1
    if index < 0:
        period = median(b - a for a, b in pairwise(beats))
        return 1 if beats[0] - time_s <= 4 * period else None
    return index // 4 + 1


def _source(raw: str | None) -> str:
    """The landmark source for a catalog point source or a cue source."""
    if raw in ("user", "lyrics"):
        return raw
    if raw == "imported" or raw in _DJ_SOFTWARE:
        return "imported"
    return "analysis"


def _label(text: Any) -> str:
    """A cue label as a name: one line, trimmed, at most :data:`MAX_LABEL` long."""
    words = " ".join(str(text or "").split())
    return words[:MAX_LABEL].rstrip()


def _valid_time(value: Any, length: float) -> float | None:
    """*value* as a time in the song, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    time_s = float(value)
    if time_s != time_s or time_s < 0 or (length > 0 and time_s > length):
        return None
    return time_s


def _cue_mark(cue: Cue, length: float) -> _Mark | None:
    """The mark a cue gives, or ``None`` for a cue that is no landmark."""
    time_s = _valid_time(cue.time_s, length)
    if time_s is None or cue.type in _MIX_ONLY or cue.type in MARKER_CUE_TYPES:
        return None
    source = _source(cue.source)
    label = _label(cue.label)
    if cue.type in ("drop", "breakdown", "build", "phrase"):
        name = label if source != "analysis" and label else ""
        return _Mark(time_s, cue.type, source, name)
    if source == "analysis":
        return None
    # A hot or memory cue: its label may say what it is ("Drop 2").
    for kind, pattern in (
        ("drop", _DROP_LABEL),
        ("breakdown", _BREAKDOWN_LABEL),
        ("build", _BUILD_LABEL),
    ):
        if pattern.search(label):
            return _Mark(time_s, kind, source, label)
    if cue.type not in ("user", "cue", "hotcue", "memory"):
        label = label or _label(cue.type.replace("_", " ").capitalize())
    return _Mark(time_s, "cue", source, label)


def _section_marks(facts: TrackFacts) -> list[_Mark]:
    """Start, intro, outro and segue, from the cue points or the analysis."""
    length = facts.length
    meta = facts.meta if facts.meta is not None and facts.meta.analysed else None
    if facts.points is not None:
        points = facts.points
        sources = {name: _source(facts.point_sources.get(name)) for name in points}
    elif meta is not None:
        points = {
            "start": meta.intro_start_s,
            "intro": meta.intro_end_s or None,
            "outro": meta.outro_start_s or None,
        }
        sources = dict.fromkeys(points, "analysis")
    else:
        return []
    start = _valid_time(points.get("start"), length) or 0.0
    intro = _valid_time(points.get("intro"), length)
    outro = _valid_time(points.get("outro"), length)
    segue = _valid_time(points.get("segue"), length)
    end = _valid_time(points.get("end"), length)
    end = end if end is not None else (length or None)
    marks: list[_Mark] = []
    if intro is not None and intro > start + EDGE_S:
        marks.append(_Mark(start, "intro", sources.get("intro", "analysis"), end_s=intro))
        marks.append(_Mark(intro, "intro_end", sources.get("intro", "analysis")))
    elif start > EDGE_S:
        marks.append(_Mark(start, "start", sources.get("start", "analysis")))
    if outro is not None and (end is None or outro < end - EDGE_S):
        marks.append(_Mark(outro, "outro", sources.get("outro", "analysis"), end_s=end))
    if segue is not None and (end is None or segue < end - EDGE_S):
        marks.append(_Mark(segue, "segue", sources.get("segue", "analysis")))
    return marks


def _merge(group: list[_Mark], beats: Sequence[float]) -> Landmark:
    """One landmark from the marks that fall together."""
    best: dict[str, _Mark] = {}
    for mark in group:
        held = best.get(mark.kind)
        if held is None or _PRIORITY[mark.source] > _PRIORITY[held.source]:
            best[mark.kind] = mark
    if len(best) > 1:
        best.pop("phrase", None)
    if len(best) > 1 and "cue" in best and not best["cue"].name:
        best.pop("cue")
    if "vocals" in best:
        # The singing starting says the intro is over.
        best.pop("intro_end", None)
    kinds = [kind for kind in _KIND_ORDER if kind in best]
    first = best[kinds[0]]
    names = [best[kind].name or NAMES[kind] for kind in kinds]
    name = " and ".join([names[0], *(n[:1].lower() + n[1:] for n in names[1:])])
    end_s = next((best[k].end_s for k in kinds if best[k].end_s is not None), None)
    return Landmark(
        time_s=first.time_s,
        kind=kinds[0],
        kinds=kinds,
        name=name,
        source=first.source,
        major=any(kind not in _MINOR or best[kind].name for kind in kinds),
        bar=bar_number(beats, first.time_s),
        end_s=end_s,
        end_bar=None if end_s is None else bar_number(beats, end_s),
    )


def build_landmarks(facts: TrackFacts) -> list[Landmark]:
    """The landmarks of one song, in time order.

    Args:
        facts: What is known about the song.

    Returns:
        Its landmarks; empty when nothing is known.
    """
    meta = facts.meta if facts.meta is not None and facts.meta.analysed else None
    marks = _section_marks(facts)
    if meta is not None:
        marks.extend(m for cue in meta.cues if (m := _cue_mark(cue, facts.length)) is not None)
    vocals = first_vocal_s(facts.sung)
    if vocals is not None and _valid_time(vocals, facts.length) is not None:
        marks.append(_Mark(float(vocals), "vocals", "lyrics"))
    beats = list(meta.beats) if meta is not None else []
    marks.sort(key=lambda m: (m.time_s, _KIND_ORDER.index(m.kind)))
    groups: list[list[_Mark]] = []
    for mark in marks:
        if groups and mark.time_s - groups[-1][0].time_s < MERGE_S:
            groups[-1].append(mark)
        else:
            groups.append([mark])
    return [_merge(group, beats) for group in groups]


class LandmarkCache:
    """Landmark answers by path, kept while the song's facts are unchanged.

    Each answer is stored with the facts it was built from: the catalog's
    records counter (any cue point saved anywhere changes it) and the
    analysis and lyric objects themselves, which the DJ-meta cache and the
    vocal cache hand out unchanged until they change.  The objects are kept
    with the answer, so a compared ``id`` is never reused.
    """

    def __init__(self, size: int = 128) -> None:
        """Keep at most *size* answers, dropping the least recently used."""
        self._size = size
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[tuple, dict]] = OrderedDict()

    def get(self, path: str, token: tuple) -> dict | None:
        """The answer stored for *path*, when it was built from *token*."""
        with self._lock:
            held = self._items.get(path)
            if held is None or not _same(held[0], token):
                return None
            self._items.move_to_end(path)
            return held[1]

    def put(self, path: str, token: tuple, answer: dict) -> None:
        """Store *answer* for *path*, built from *token*."""
        with self._lock:
            self._items[path] = (token, answer)
            self._items.move_to_end(path)
            while len(self._items) > self._size:
                self._items.popitem(last=False)


def _same(held: tuple, token: tuple) -> bool:
    """Whether two tokens match: plain values by value, objects by identity."""
    if len(held) != len(token):
        return False
    return all(
        a is b or (isinstance(a, int | float | str) and type(a) is type(b) and a == b)
        for a, b in zip(held, token, strict=True)
    )


def _player_meta(player: Any, path: str) -> DjMeta | None:
    """The DJ analysis of *path* from *player*'s cache, without analysing it."""
    try:
        player._ensure_dj_cache()
        cache = getattr(player, "_dj_cache", None)
        meta = cache.get(path) if cache is not None else None
    except Exception:  # a broken cache must never break Track info
        logger.debug("Landmarks: no DJ analysis for %s", path, exc_info=True)
        return None
    return meta if isinstance(meta, DjMeta) and meta.analysed else None


def _player_sung(player: Any, path: str) -> tuple[tuple[float, float], ...]:
    """Synced-lyric spans of *path*, empty when it has none or they cannot be read."""
    try:
        spans = player._sung_spans(path)
        return spans if isinstance(spans, tuple) else tuple(spans)
    except Exception:  # an unreadable tag must never break Track info
        logger.debug("Landmarks: no lyric timing for %s", path, exc_info=True)
        return ()


def _cache_for(player: Any) -> LandmarkCache:
    """The station's answer cache, kept on its player."""
    cache = getattr(player, "__dict__", {}).get("_landmark_cache")
    if not isinstance(cache, LandmarkCache):
        cache = LandmarkCache()
        player.__dict__["_landmark_cache"] = cache
    return cache


def station_landmarks(bridge: Any, path: str) -> dict:
    """The route's answer for *path* on the station of *bridge*.

    Raises:
        LookupError: *path* is not a song in the library.
    """
    player = bridge.player
    library = getattr(bridge, "library", None)
    record = None
    version: int | None = None
    if library is not None:
        if library.entry_for_path(path) is None:
            raise LookupError(path)
        record = library.catalog.record_for_path(path)
        if record is None:
            raise LookupError(path)
        path = record["path"]
        length, bpm, kind = float(record["length"] or 0), record["bpm"], record["kind"]
        version = int(library.catalog.change_versions()["records"])
    else:
        entry = bridge.entry_for_path(path)
        if entry is None:
            raise LookupError(path)
        path, length, bpm, kind = entry.path, float(entry.length or 0), entry.bpm, "music"
    music = kind == "music"
    meta = _player_meta(player, path) if music else None
    sung = _player_sung(player, path) if music else ()
    token = (version, length, kind, meta, sung)
    cache = _cache_for(player)
    if (answer := cache.get(path, token)) is not None:
        return answer
    facts = TrackFacts(
        length=length,
        points=record["points"] if record is not None else None,
        point_sources=record["point_sources"] if record is not None else {},
        meta=meta,
        sung=sung,
    )
    found = build_landmarks(facts) if music else []
    answer = {
        "path": path,
        "kind": kind,
        "length": round(length, 2),
        "bpm": round(float(bpm), 2) if bpm and bpm > 0 else None,
        "analysed": meta is not None,
        "bars": bool(meta is not None and len(meta.beats) >= MIN_GRID_BEATS),
        "lyrics": bool(sung),
        "landmarks": [landmark.as_dict() for landmark in found],
    }
    cache.put(path, token, answer)
    return answer


def install_landmarks(router: StationRouter) -> None:
    """Add the landmarks route; it reads through the station its URL names."""

    @router.get("/landmarks")
    async def landmarks(
        station: StationDep,
        path: str = Query(..., min_length=1, max_length=4096),
    ) -> dict:
        """A song's landmarks: intro, vocals, drops, breakdowns, outro and the rest."""
        try:
            return await asyncio.to_thread(station_landmarks, station.bridge, path)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Recording not in library") from exc
