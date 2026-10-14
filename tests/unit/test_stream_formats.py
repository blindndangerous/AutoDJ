"""Frame and page finding for the AAC and Opus streams (#50)."""

from __future__ import annotations

from autodj.config import AAC_BITRATES, OPUS_BITRATES, STREAM_BITRATES
from autodj.stream_formats import (
    AAC,
    EXTRA_FORMATS,
    FORMATS,
    MP3,
    OPUS,
    adts_frame_offset,
    ogg_header_end,
    ogg_page_offset,
    ogg_page_size,
    ogg_starts_stream,
)
from tests.unit._stream_frames import adts_frame, ogg_page, ogg_stream


def test_every_format_names_its_address_type_and_bitrates() -> None:
    assert list(FORMATS) == ["mp3", "aac", "opus"]
    assert EXTRA_FORMATS == (AAC, OPUS)
    assert (MP3.media_type, AAC.media_type, OPUS.media_type) == (
        "audio/mpeg",
        "audio/aac",
        "audio/ogg",
    )
    assert (MP3.bitrates, AAC.bitrates, OPUS.bitrates) == (
        STREAM_BITRATES,
        AAC_BITRATES,
        OPUS_BITRATES,
    )
    # Song titles ride in MP3 and AAC; an Ogg stream cannot carry ICY blocks.
    assert (MP3.icy, AAC.icy, OPUS.icy) == (True, True, False)
    assert [fmt.paged for fmt in (MP3, AAC, OPUS)] == [False, False, True]


def test_adts_offset_skips_partial_bytes_to_a_confirmed_frame() -> None:
    frame = adts_frame()
    data = frame[5:] + frame + frame
    assert adts_frame_offset(data) == len(frame) - 5
    # One whole frame that ends the buffer counts too.
    assert adts_frame_offset(frame) == 0


def test_adts_offset_refuses_lookalikes() -> None:
    frame = adts_frame()
    assert adts_frame_offset(b"") == -1
    assert adts_frame_offset(b"\xff\xf1\x00") == -1
    # A sync word whose length points at no next frame is not trusted.
    assert adts_frame_offset(frame + b"\x00" * 30) == -1
    # Nor one whose next header is cut off by the end of the buffer.
    assert adts_frame_offset(frame + b"\xff\xf1") == -1
    # A layer that is not 0, or a sample rate index past 12.
    assert adts_frame_offset(b"\xff\xf3" + frame[2:] + frame) == len(frame)
    bad_rate = frame[:2] + bytes([frame[2] | (15 << 2)]) + frame[3:]
    assert adts_frame_offset(bad_rate + b"\x00" * 3) == -1
    # A length shorter than its own header.
    short = frame[:3] + b"\x80\x00\x1f" + frame[6:]
    assert adts_frame_offset(short) == -1


def test_ogg_page_size_and_offset() -> None:
    header, stream = ogg_stream()
    first = ogg_page(0, b"OpusHead" + b"\x01" * 11, first=True)
    assert ogg_page_size(stream, 0) == len(first)
    assert ogg_page_size(stream, 1) == 0
    # A truncated header or segment table is no page.
    assert ogg_page_size(stream[:20], 0) == 0
    assert ogg_page_size(b"OggS\x00" + b"\x00" * 21 + b"\x05", 0) == 0
    # A page with another version is not one.
    assert ogg_page_size(b"OggS\x01" + stream[5:], 0) == 0
    # Joining mid-page finds the next whole page, confirmed by the one after.
    assert ogg_page_offset(stream[3:]) == len(first) - 3
    assert ogg_page_offset(stream) == 0
    # The last page counts when it ends the buffer exactly.
    last = ogg_page(960, b"\xfc" * 40)
    assert ogg_page_offset(last) == 0
    # A page cut off by the end of the buffer is not trusted on its own.
    assert ogg_page_offset(last[:-5]) == -1
    assert ogg_page_offset(b"no pages here") == -1
    # "OggS" inside audio that is not followed by a page is skipped.
    assert ogg_page_offset(b"OggS\x00junk" + header) == 9


def test_ogg_header_end_waits_for_the_first_audio_page() -> None:
    header, stream = ogg_stream()
    assert ogg_header_end(stream) == len(header)
    # Still all header: not known yet.
    assert ogg_header_end(header) == -1
    assert ogg_header_end(header[:-3]) == -1
    # A stream that starts with audio has no header to keep.
    assert ogg_header_end(stream[len(header) :]) == -1
    assert ogg_header_end(b"not ogg at all, not ogg at all") == -1


def test_ogg_starts_stream_reads_the_first_page_flag() -> None:
    header, stream = ogg_stream()
    assert ogg_starts_stream(stream, 0)
    assert not ogg_starts_stream(stream, len(header))
