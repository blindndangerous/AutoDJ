"""ICY (SHOUTcast) metadata framing and MP3 frame sync helpers."""

from __future__ import annotations

import unicodedata

METAINT = 16000
_MAX_META = 4080
_BITRATES_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_SAMPLE_RATES = (44100, 48000, 32000, 0)


def format_stream_title(artist: str, title: str) -> str:
    """Return a safe ``Artist - Title`` string for ICY metadata.

    Args:
        artist: The track artist, or an empty string if unknown.
        title: The track title, or an empty string if unknown.

    Returns:
        ``"Artist - Title"`` when both are non-empty, otherwise whichever
        one is non-empty (or ``""`` if neither is). Control characters are
        stripped, apostrophes become U+2019, and semicolons become commas.
    """
    cleaned = [_strip_control(part).strip() for part in (artist, title)]
    text = " - ".join(part for part in cleaned if part)
    return text.replace("'", "’").replace(";", ",")


def _strip_control(text: str) -> str:
    """Return *text* with Unicode control characters removed.

    Args:
        text: The text to clean.

    Returns:
        *text* with every character in a Unicode "C" category (control,
        format, surrogate, private-use, unassigned) removed.
    """
    return "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")


def encode_metadata(title: str) -> bytes:
    """Encode one ICY metadata block for *title* (empty means no change).

    Args:
        title: The stream title to encode, or an empty string to signal
            that the title has not changed.

    Returns:
        A length-prefixed, NUL-padded ICY metadata block. An empty title
        encodes as a single zero byte.
    """
    if not title:
        return b"\x00"
    prefix, suffix = b"StreamTitle='", b"';"
    room = _MAX_META - len(prefix) - len(suffix)
    body = title.encode("utf-8")
    if len(body) > room:
        body = body[:room].decode("utf-8", errors="ignore").encode("utf-8")
    payload = prefix + body + suffix
    padded_len = -(-len(payload) // 16) * 16
    return bytes([padded_len // 16]) + payload.ljust(padded_len, b"\x00")


class IcyInterleaver:
    """Insert ICY metadata blocks into an MP3 byte stream for one listener."""

    def __init__(self, metaint: int = METAINT) -> None:
        """Start a fresh interleaver.

        Args:
            metaint: The number of audio bytes between metadata blocks.
        """
        self._metaint = metaint
        self._until_meta = metaint
        self._sent_title: str | None = None

    def feed(self, data: bytes, title: str) -> bytes:
        """Return *data* with metadata inserted at every metaint boundary.

        Args:
            data: The next chunk of audio bytes for this listener.
            title: The current stream title.

        Returns:
            *data* with an ICY metadata block spliced in every time the
            listener has received ``metaint`` audio bytes since the last
            block. The block carries the title only when it changed since
            the last block sent to this listener, otherwise a single zero
            byte.
        """
        out = bytearray()
        view = memoryview(data)
        while view:
            take = min(self._until_meta, len(view))
            out += view[:take]
            view = view[take:]
            self._until_meta -= take
            if self._until_meta == 0:
                if title != self._sent_title:
                    out += encode_metadata(title)
                    self._sent_title = title
                else:
                    out += b"\x00"
                self._until_meta = self._metaint
        return bytes(out)


def _header_ok(data: bytes, i: int) -> int:
    """Return the frame size at offset *i* if it is a valid MP3 header.

    Args:
        data: The buffer to inspect.
        i: The offset of the candidate frame header.

    Returns:
        The frame size in bytes if `data[i:i + 4]` is a valid MPEG-1 Layer
        III frame header, otherwise 0.
    """
    if i + 4 > len(data):
        return 0
    b1, b2 = data[i + 1], data[i + 2]
    if data[i] != 0xFF or (b1 & 0xE0) != 0xE0 or (b1 & 0x06) != 0x02:
        return 0
    bitrate = _BITRATES_KBPS[b2 >> 4]
    rate = _SAMPLE_RATES[(b2 >> 2) & 0x03]
    if not bitrate or not rate:
        return 0
    padding = (b2 >> 1) & 0x01
    return 144 * bitrate * 1000 // rate + padding


def mp3_frame_offset(data: bytes) -> int:
    """Return the offset of the first MP3 frame confirmed by the next one.

    Args:
        data: The buffer to scan for a frame sync.

    Returns:
        The index of the first valid MPEG-1 Layer III frame header whose
        following frame header is also valid (or which reaches exactly the
        end of *data*), or -1 if none is found.
    """
    for i in range(len(data) - 3):
        size = _header_ok(data, i)
        if size and (i + size == len(data) or _header_ok(data, i + size)):
            return i
    return -1
