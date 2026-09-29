"""Test doubles shared by several unit and integration test modules.

Kept out of the ``test_*.py`` modules so tests import helpers from here,
never from another test module.
"""

from __future__ import annotations

import base64
import struct
import threading
from pathlib import Path
from unittest.mock import MagicMock

import faiss
import numpy as np

from autodj.dj_meta import Cue
from autodj.indexer import FEATURE_DIM, IndexEntry
from autodj.similarity import SimilarityIndex


def write_serato_flac(path: Path) -> Cue:
    """Write a short silent FLAC at *path* with one Serato hot cue in its tags.

    Returns the cue :func:`autodj.dj_cues_import.import_from_serato_tags`
    should read back: 12.5 s, named "Drop".
    """
    import soundfile as sf
    from mutagen.flac import FLAC

    sf.write(path, np.zeros(4410, dtype="float32"), 44100)
    cue = b"\x00\x00" + struct.pack(">I", 12500) + b"\x00\xcc\x00\x00\x00\x00Drop\x00"
    payload = b"\x01\x01" + b"CUE\x00" + struct.pack(">I", len(cue)) + cue + b"\x00"
    body = b"\x01\x01" + base64.b64encode(payload).replace(b"=", b"A")
    wrapped = b"application/octet-stream\x00\x00Serato Markers2\x00" + body.ljust(470, b"\x00")
    tags = FLAC(path)
    tags["SERATO_MARKERS_V2"] = base64.b64encode(wrapped).decode("ascii").rstrip("=")
    tags.save()
    return Cue(time_s=12.5, type="user", label="Drop", source="serato", color="#cc0000")


def make_entry(i: int = 0) -> IndexEntry:
    """Build a deterministic ``IndexEntry`` keyed by *i*."""
    return IndexEntry(
        path=f"Z:/Music/song_{i}.flac",
        title=f"Song {i}",
        artist="Artist",
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2000,
        length=180.0,
        energy=0.05,
        key=0,
        mode=1,
        tempo_confidence=0.8,
    )


def make_cfg_mock() -> MagicMock:
    """Build a config mock carrying every setting the Player reads."""
    cfg = MagicMock()
    cfg.playback.no_repeat_window = 50
    cfg.playback.artist_repeat_window = 3
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.crossfade_eq_duck = False
    cfg.playback.crossfade_bass_cutoff_hz = 180.0
    cfg.playback.show_lyrics = True
    cfg.playback.prefetch_next_track = True
    cfg.playback.silence_trigger_crossfade = True
    cfg.playback.discovery_every = None
    cfg.playback.pick_top_k = 1
    cfg.playback.pick_temperature = 0.0
    cfg.playback.server_max_track_minutes = 15.0
    cfg.playback.import_external_cues = False
    cfg.replaygain.enabled = False
    cfg.replaygain.target_db = -14.0
    cfg.replaygain.max_clip_safe_gain = 1.0
    cfg.djmix.harmonic_mode = "off"
    cfg.djmix.beatmatch = False
    cfg.djmix.beatmatch_max_stretch = 0.08
    cfg.djmix.outro_intro_align = False
    cfg.djmix.phrase_align = False
    cfg.djmix.phrase_bars = 8
    cfg.djmix.filter_sweep = False
    cfg.djmix.filter_sweep_floor_hz = 250.0
    cfg.transitions.effect = "none"
    cfg.transitions.wet_mix = 1.0
    cfg.library.beets_db = None
    cfg.library.music_dir = None
    cfg.index.active_dir = None
    return cfg


def make_sim_index(n: int = 10, *, bpms: list[float] | None = None) -> SimilarityIndex:
    """Build a real FAISS-backed index of *n* deterministic tracks."""
    if bpms is not None and len(bpms) != n:
        raise ValueError("bpms must contain one value per track")
    rng = np.random.default_rng(42)
    vectors = rng.standard_normal((n, FEATURE_DIM)).astype(np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors /= norms
    fi = faiss.IndexFlatIP(FEATURE_DIM)
    fi.add(vectors)
    entries = [make_entry(i) for i in range(n)]
    if bpms is not None:
        for entry, bpm in zip(entries, bpms, strict=True):
            entry.bpm = bpm
    return SimilarityIndex(faiss_index=fi, entries=entries)


class FakeClock:
    """A mix-bus clock that only moves when the bus sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class FakeEncoder:
    """Echo PCM bytes back as 'encoded' bytes, one chunk per write."""

    def __init__(self, bitrate: int) -> None:
        self.bitrate = bitrate
        self._chunks: list[bytes] = []
        self._cond = threading.Condition()
        self._closed = False
        self.alive = True

    def write(self, pcm: bytes) -> None:
        with self._cond:
            self._chunks.append(pcm[:64])
            self._cond.notify_all()

    def read(self, n: int) -> bytes:
        with self._cond:
            while not self._chunks and not self._closed:
                self._cond.wait(0.05)
            return self._chunks.pop(0) if self._chunks else b""

    def close_input(self) -> None:
        pass

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self.alive = False
            self._cond.notify_all()
