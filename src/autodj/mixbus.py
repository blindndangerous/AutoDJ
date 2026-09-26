"""Clock-paced stereo mix bus for server-side playback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry


@dataclass(frozen=True)
class RenderedTrack:
    """One track's audio as the mix bus will play it.

    Attributes:
        entry: The track this audio belongs to.
        audio: ``(frames, 2)`` float32 audio at 44100 Hz — the track's body
            plus any overlap mixed in from *next_entry*.
        next_entry: The track mixed into the tail, or ``None`` when this is
            the last track (or there was no room for a crossfade).
        next_start_offset: Samples of *next_entry*'s audio already played in
            the overlap (including any skipped intro), so the following
            render call knows where to continue without replaying them.
            Expressed in *next_entry*'s own, as-loaded-fresh timeline (i.e.
            already converted back out of any beat-match stretch).
        transition_fx: Name of the effect used for the overlap, or ``""``
            when no effect was applied.
        beatmatch_ratio: Measured stretch ratio applied to *next_entry*'s
            audio for the overlap (``len(stretched) / len(original)``), or
            ``1.0`` when it was not stretched (or there is no overlap).
    """

    entry: IndexEntry
    audio: np.ndarray
    next_entry: IndexEntry | None
    next_start_offset: int
    transition_fx: str
    beatmatch_ratio: float = 1.0
