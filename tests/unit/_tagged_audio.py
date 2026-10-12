"""Tiny real audio files, made in a test's temporary folder, for tag tests.

Nothing here is committed as a binary: soundfile writes a tenth of a second
of silence as FLAC, MP3, Ogg Vorbis, Ogg Opus or WAV, and the M4A is a
hand-built MP4 holding only the atoms mutagen needs.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

# Suffix -> (soundfile format, subtype, sample rate).
_SOUNDFILE = {
    ".flac": ("FLAC", "PCM_16", 44100),
    ".mp3": ("MP3", "MPEG_LAYER_III", 44100),
    ".ogg": ("OGG", "VORBIS", 44100),
    ".opus": ("OGG", "OPUS", 48000),
    ".wav": ("WAV", "PCM_16", 44100),
}
SUFFIXES = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav")


def _atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + name + payload


def _full_atom(name: bytes, payload: bytes) -> bytes:
    return _atom(name, b"\x00\x00\x00\x00" + payload)


def mp4_bytes() -> bytes:
    """A one-second M4A with one sound track and no samples worth playing."""
    mvhd = _full_atom(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 1000) + b"\x00" * 80)
    mdhd = _full_atom(b"mdhd", struct.pack(">IIIIHH", 0, 0, 44100, 44100, 0x55C4, 0))
    hdlr = _full_atom(b"hdlr", struct.pack(">I4s12x", 0, b"soun") + b"\x00")
    moov = _atom(b"moov", mvhd + _atom(b"trak", _atom(b"mdia", mdhd + hdlr)))
    ftyp = _atom(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")
    return ftyp + moov + _atom(b"mdat", b"\x00" * 64)


def make_audio(folder: Path, suffix: str, stem: str = "song") -> Path:
    """Write a tiny untagged *suffix* file in *folder*; skip when soundfile cannot."""
    path = folder / f"{stem}{suffix}"
    if suffix == ".m4a":
        path.write_bytes(mp4_bytes())
        return path
    fmt, subtype, rate = _SOUNDFILE[suffix]
    if subtype not in sf.available_subtypes(fmt):
        pytest.skip(f"this libsndfile cannot write {suffix}")
    sf.write(path, np.zeros((rate // 10, 1), dtype=np.float32), rate, format=fmt, subtype=subtype)
    return path
