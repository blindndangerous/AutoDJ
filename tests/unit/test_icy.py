"""ICY metadata framing and MP3 frame sync."""

from __future__ import annotations

from autodj.icy import IcyInterleaver, encode_metadata, format_stream_title, mp3_frame_offset


def test_title_format_and_escaping() -> None:
    assert format_stream_title("Daft Punk", "One More Time") == "Daft Punk - One More Time"
    assert format_stream_title("", "Solo") == "Solo"
    assert format_stream_title("Artist", "") == "Artist"
    assert format_stream_title("Guns N' Roses", "a;b\x07c") == "Guns N’ Roses - a,bc"


def test_title_format_all_control_characters() -> None:
    assert format_stream_title("\x01\x02", "\x03") == ""


def test_metadata_block_layout() -> None:
    block = encode_metadata("Artist - Title")
    payload = b"StreamTitle='Artist - Title';"
    assert block[0] == (len(block) - 1) // 16
    assert (len(block) - 1) % 16 == 0
    assert block[1 : 1 + len(payload)] == payload
    assert set(block[1 + len(payload) :]) <= {0}
    assert encode_metadata("") == b"\x00"


def test_long_non_ascii_title_is_truncated_on_char_boundary() -> None:
    block = encode_metadata("é" * 5000)
    assert len(block) - 1 <= 4080
    body = block[1:].rstrip(b"\x00")
    body.decode("utf-8")
    assert body.endswith(b"';")


def test_interleaver_inserts_every_metaint_bytes() -> None:
    icy = IcyInterleaver(metaint=10)
    out = icy.feed(b"A" * 25, "T1")
    first = encode_metadata("T1")
    assert out[:10] == b"A" * 10
    assert out[10 : 10 + len(first)] == first
    rest = out[10 + len(first) :]
    assert rest[:10] == b"A" * 10
    assert rest[10:11] == b"\x00"
    assert rest[11:] == b"A" * 5
    more = icy.feed(b"B" * 5, "T2")
    assert more[:5] == b"B" * 5
    assert more[5:] == encode_metadata("T2")


def test_interleaver_data_ends_exactly_on_metaint_boundary() -> None:
    icy = IcyInterleaver(metaint=5)
    out = icy.feed(b"A" * 5, "T1")
    first = encode_metadata("T1")
    assert out == b"A" * 5 + first
    more = icy.feed(b"B" * 5, "T1")
    assert more == b"B" * 5 + b"\x00"


def _frame(bitrate_index: int = 9) -> bytes:
    header = bytes([0xFF, 0xFB, (bitrate_index << 4) | 0x00, 0x00])
    size = 144 * 128_000 // 44_100
    return header + b"\x00" * (size - 4)


def test_frame_offset_finds_real_frame() -> None:
    data = b"\x12\xff\x00" + _frame() + _frame()
    assert mp3_frame_offset(data) == 3


def test_frame_offset_rejects_false_sync() -> None:
    assert mp3_frame_offset(b"\xff\xfb\xf0\x00" + b"\x00" * 10) == -1
    assert mp3_frame_offset(b"") == -1


def test_frame_offset_rejects_bad_bitrate_index() -> None:
    header = bytes([0xFF, 0xFB, (0x0F << 4) | 0x00, 0x00])
    assert mp3_frame_offset(header + b"\x00" * 10) == -1


def test_frame_offset_rejects_bad_sample_rate_index() -> None:
    header = bytes([0xFF, 0xFB, (9 << 4) | (0x03 << 2), 0x00])
    assert mp3_frame_offset(header + b"\x00" * 10) == -1


def test_frame_offset_rejects_truncated_header_at_end() -> None:
    assert mp3_frame_offset(b"\x00\xff\xfb") == -1


def test_frame_offset_rejects_when_next_header_is_truncated() -> None:
    header = bytes([0xFF, 0xFB, (9 << 4) | 0x00, 0x00])
    size = 144 * 128_000 // 44_100
    # Only 2 bytes follow where the next frame header would start, so the
    # second header check can't read a full 4-byte header and must fail
    # without also matching the "ends exactly at len(data)" branch.
    data = header + b"\x00" * (size - 4) + b"\x00\x00"
    assert mp3_frame_offset(data) == -1
