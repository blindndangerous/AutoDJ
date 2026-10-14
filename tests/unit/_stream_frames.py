"""Hand-built AAC (ADTS) frames and Ogg pages for the stream format tests."""

from __future__ import annotations

import struct


def adts_frame(payload: bytes = b"\x21" * 20) -> bytes:
    """One ADTS frame: AAC-LC, 44.1 kHz, stereo, no CRC, then *payload*."""
    length = 7 + len(payload)
    header = bytes(
        [
            0xFF,
            0xF1,
            (1 << 6) | (4 << 2),
            (2 << 6) | ((length >> 11) & 0x03),
            (length >> 3) & 0xFF,
            ((length & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )
    return header + payload


def ogg_page(granule: int, payload: bytes, *, first: bool = False, sequence: int = 0) -> bytes:
    """One Ogg page holding *payload* (under 255 bytes); its CRC is left at 0."""
    assert len(payload) < 255
    header = struct.pack(
        "<4sBBqIIIB",
        b"OggS",
        0,
        0x02 if first else 0x00,
        granule,
        1234,
        sequence,
        0,
        1,
    )
    return header + bytes([len(payload)]) + payload


def ogg_stream(audio_pages: int = 3) -> tuple[bytes, bytes]:
    """An Ogg Opus-like stream: (its two header pages, the header and *audio_pages* more)."""
    header = ogg_page(0, b"OpusHead" + b"\x01" * 11, first=True) + ogg_page(
        0, b"OpusTags" + b"\x00" * 8, sequence=1
    )
    audio = b"".join(
        ogg_page(960 * (index + 1), b"\xfc" * 40, sequence=index + 2)
        for index in range(audio_pages)
    )
    return header, header + audio
