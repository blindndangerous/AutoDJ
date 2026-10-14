"""The formats a station streams in: MP3, and AAC and Opus beside it (#50).

Each format names its ffmpeg encoder and container, its HTTP media type,
whether ICY song titles can be interleaved into it, and how to find the
first whole frame or page in a run of encoded bytes, so a listener who
joins mid-stream starts on a boundary its player can decode.

An Ogg stream (Opus) cannot be joined mid-stream on its own: a player
needs the stream's header pages first.  :func:`ogg_header_end` finds where
they end, so the stream output can keep them and send them to each new
listener before the recent audio.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from autodj.config import AAC_BITRATES, OPUS_BITRATES, STREAM_BITRATES
from autodj.icy import mp3_frame_offset

#: Ogg page header: "OggS", version, type, granule, serial, sequence, CRC, segments.
_OGG_HEADER = 27
_OGG_BOS = 0x02


def _adts_frame_size(data: bytes, at: int) -> int:
    """The length of the ADTS frame whose header starts at *at*, or 0 if none does."""
    if at + 7 > len(data):
        return 0
    if data[at] != 0xFF or (data[at + 1] & 0xF6) != 0xF0:
        return 0  # sync word, then MPEG-2/4 with layer 0
    if (data[at + 2] >> 2) & 0x0F > 12:
        return 0  # no such sample rate
    size = ((data[at + 3] & 0x03) << 11) | (data[at + 4] << 3) | (data[at + 5] >> 5)
    return size if size >= 7 else 0


def adts_frame_offset(data: bytes) -> int:
    """Return the offset of the first ADTS (AAC) frame confirmed by the next one.

    Args:
        data: The buffer to scan.

    Returns:
        The index of the first frame header whose following frame header
        is also valid (or which reaches exactly the end of *data*), or -1.
    """
    for at in range(len(data) - 6):
        size = _adts_frame_size(data, at)
        if size and (at + size == len(data) or _adts_frame_size(data, at + size)):
            return at
    return -1


def ogg_page_size(data: bytes, at: int) -> int:
    """The length of the whole Ogg page starting at *at*, or 0 if none starts there.

    A page whose header is complete but whose body runs past the end of
    *data* still has its length returned; callers check it fits.
    """
    if not data.startswith(b"OggS", at) or at + _OGG_HEADER > len(data) or data[at + 4] != 0:
        return 0
    segments = data[at + 26]
    table_end = at + _OGG_HEADER + segments
    if table_end > len(data):
        return 0
    return _OGG_HEADER + segments + sum(data[at + _OGG_HEADER : table_end])


def ogg_page_offset(data: bytes) -> int:
    """Return the offset of the first whole Ogg page confirmed by the next one.

    Args:
        data: The buffer to scan.

    Returns:
        The index of the first page that fits in *data* and is followed by
        another page (or ends exactly at the end of *data*), or -1.
    """
    at = data.find(b"OggS")
    while at >= 0:
        size = ogg_page_size(data, at)
        end = at + size
        if size and (end == len(data) or (end < len(data) and ogg_page_size(data, end))):
            return at
        at = data.find(b"OggS", at + 1)
    return -1


def ogg_starts_stream(data: bytes, at: int) -> bool:
    """Whether the Ogg page at *at* is the first of its stream (beginning of stream)."""
    return bool(data[at + 5] & _OGG_BOS)


def ogg_header_end(data: bytes) -> int:
    """Where the header pages at the start of an Ogg stream end, or -1 if not yet known.

    Header pages (the codec's identification and comment packets) carry
    granule position 0; the first audio page has a later one.

    Args:
        data: The stream's bytes from its very first page.

    Returns:
        The length of the header pages, once the first page after them
        has begun; -1 while *data* is still all header (or not a stream).
    """
    at = 0
    while at + _OGG_HEADER <= len(data):
        size = ogg_page_size(data, at)
        if not size:
            return -1
        (granule,) = struct.unpack_from("<q", data, at + 6)
        if granule != 0:
            return at if at else -1
        at += size
    return -1


@dataclass(frozen=True)
class StreamFormat:
    """One format a station can stream in.

    Attributes:
        name: The short name, also the address's extension (``stream.aac``).
        label: The name people read (``"AAC"``).
        media_type: The HTTP ``Content-Type``.
        encoder: ffmpeg's output options before the bitrate.
        container: ffmpeg's output options after it.
        bitrates: The bitrates it may run at, in kbps.
        icy: Whether ICY song titles can be put into it.
        paged: An Ogg stream: listeners need its header pages first.
        sync: Finds the first whole frame or page in a run of bytes.
    """

    name: str
    label: str
    media_type: str
    encoder: tuple[str, ...]
    container: tuple[str, ...]
    bitrates: tuple[int, ...]
    icy: bool
    paged: bool
    sync: Callable[[bytes], int]


MP3 = StreamFormat(
    name="mp3",
    label="MP3",
    media_type="audio/mpeg",
    encoder=("-c:a", "libmp3lame"),
    container=("-f", "mp3"),
    bitrates=STREAM_BITRATES,
    icy=True,
    paged=False,
    sync=mp3_frame_offset,
)
AAC = StreamFormat(
    name="aac",
    label="AAC",
    media_type="audio/aac",
    # ffmpeg's own AAC-LC encoder, in every build; ADTS frames can be
    # joined mid-stream, and players read ICY titles from it.
    encoder=("-c:a", "aac"),
    container=("-f", "adts"),
    bitrates=AAC_BITRATES,
    icy=True,
    paged=False,
    sync=adts_frame_offset,
)
OPUS = StreamFormat(
    name="opus",
    label="Opus",
    media_type="audio/ogg",
    # Opus runs at 48 kHz; ffmpeg resamples the 44.1 kHz mix.  Short Ogg
    # pages let a new listener join close to the live edge.
    encoder=("-c:a", "libopus", "-ar", "48000"),
    container=("-f", "ogg", "-page_duration", "200000"),
    bitrates=OPUS_BITRATES,
    icy=False,
    paged=True,
    sync=ogg_page_offset,
)

#: Every format, by name; the MP3 stream is always on, the others are extra mounts.
FORMATS: dict[str, StreamFormat] = {fmt.name: fmt for fmt in (MP3, AAC, OPUS)}
#: The formats a station can serve beside its MP3 stream.
EXTRA_FORMATS: tuple[StreamFormat, ...] = (AAC, OPUS)
