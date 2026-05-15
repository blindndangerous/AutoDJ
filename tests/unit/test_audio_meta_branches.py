"""Branch coverage tests for audio_meta helpers (ReplayGain + lyrics)."""

from __future__ import annotations

from types import SimpleNamespace

from autodj.audio_meta import (
    _decode_mp4_atom,
    _lyrics_from_mp4,
    _lyrics_from_uslt,
    _lyrics_from_vorbis,
    _parse_gain_string,
    _rg_from_id3,
    _rg_from_mp4,
    _rg_from_vorbis,
    parse_lrc,
)


class TestParseGainString:
    def test_returns_none_when_no_number(self) -> None:
        assert _parse_gain_string("no number here") is None

    def test_parses_negative_db(self) -> None:
        assert _parse_gain_string("-6.5 dB") == -6.5

    def test_parses_plain_number(self) -> None:
        assert _parse_gain_string("3") == 3.0


class TestRgFromVorbis:
    def test_no_tags_returns_none(self) -> None:
        m = SimpleNamespace()  # no .get
        assert _rg_from_vorbis(m) == (None, None)

    def test_index_error_returns_none(self) -> None:
        # m.get returns object that raises IndexError on [0]
        class Boom:
            def get(self, k):
                if k == "replaygain_track_gain":
                    return _Raises()
                return None

        class _Raises:
            def __bool__(self):
                return True

            def __getitem__(self, i):
                raise IndexError("x")

        assert _rg_from_vorbis(Boom()) == (None, None)

    def test_returns_gain_and_peak(self) -> None:
        m = SimpleNamespace(
            get=lambda k: {
                "replaygain_track_gain": ["-6.5 dB"],
                "replaygain_track_peak": ["0.95"],
            }.get(k)
        )
        gain, peak = _rg_from_vorbis(m)
        assert gain == "-6.5 dB"
        assert peak == "0.95"


class TestRgFromId3:
    def test_no_tags_returns_none(self) -> None:
        m = SimpleNamespace(tags=None)
        assert _rg_from_id3(m) == (None, None)

    def test_no_tags_attr_returns_none(self) -> None:
        m = SimpleNamespace()  # no .tags
        assert _rg_from_id3(m) == (None, None)

    def test_returns_frame_text(self) -> None:
        frame = SimpleNamespace(text=["-3.0 dB"])
        peak_frame = SimpleNamespace(text=["0.7"])
        tags = SimpleNamespace(
            get=lambda k: {
                "TXXX:replaygain_track_gain": frame,
                "TXXX:replaygain_track_peak": peak_frame,
            }.get(k),
        )
        m = SimpleNamespace(tags=tags)
        gain, peak = _rg_from_id3(m)
        assert gain == "-3.0 dB"
        assert peak == "0.7"

    def test_attr_error_continues(self) -> None:
        # frame raises AttributeError on .text access
        class Frame:
            @property
            def text(self):
                raise AttributeError("nope")

            def __bool__(self):
                return True

        tags = SimpleNamespace(get=lambda k: Frame() if "gain" in k.lower() else None)
        m = SimpleNamespace(tags=tags)
        assert _rg_from_id3(m) == (None, None)


class TestRgFromMp4:
    def test_no_tags_returns_none(self) -> None:
        m = SimpleNamespace(tags=None)
        assert _rg_from_mp4(m) == (None, None)

    def test_decodes_bytes(self) -> None:
        tags = {"----:com.apple.iTunes:replaygain_track_gain": [b"-4.0 dB"]}
        # Need a tags object with .keys() and []
        m = SimpleNamespace(tags=tags)
        gain, _peak = _rg_from_mp4(m)
        assert gain == "-4.0 dB"

    def test_index_error_swallowed(self) -> None:
        class BadList:
            def __getitem__(self, i):
                raise IndexError("x")

        tags = {"replaygain_track_gain": BadList()}
        m = SimpleNamespace(tags=tags)
        assert _rg_from_mp4(m) == (None, None)


class TestDecodeMp4Atom:
    def test_bytes_decoded(self) -> None:
        assert _decode_mp4_atom(b"hello") == "hello"

    def test_str_passes_through(self) -> None:
        assert _decode_mp4_atom("x") == "x"

    def test_int_stringified(self) -> None:
        assert _decode_mp4_atom(42) == "42"


class TestLyricsFromVorbis:
    def test_no_get_method_returns_empty(self) -> None:
        # SimpleNamespace has no .get — branch line 517
        m = SimpleNamespace()
        assert _lyrics_from_vorbis(m) == ""

    def test_index_error_continues(self) -> None:
        class _BadList(list):
            def __getitem__(self, i):  # type: ignore[override]
                raise IndexError("x")

        class Boom:
            def get(self, k):
                if k == "lyrics":
                    bad = _BadList()
                    bad.append(1)  # non-empty so truthy
                    return bad
                return None

        result = _lyrics_from_vorbis(Boom())
        assert result == ""

    def test_returns_first(self) -> None:
        m = SimpleNamespace(get=lambda k: ["the lyrics"] if k == "lyrics" else None)
        assert _lyrics_from_vorbis(m) == "the lyrics"


class TestLyricsFromUslt:
    def test_no_keys_returns_empty(self) -> None:
        # tags has no .keys() attr
        tags = object()
        assert _lyrics_from_uslt(tags) == ""

    def test_keys_attribute_error_continues(self) -> None:
        class Tags:
            keys = "not callable"  # hasattr True but calling raises

        # Whatever -- exercise the try/except
        result = _lyrics_from_uslt(Tags())
        assert result == ""

    def test_uslt_frame_text_returned(self) -> None:
        frame = SimpleNamespace(text="my song lyrics")

        class Tags:
            def keys(self):
                return ["USLT::eng"]

            def __getitem__(self, k):
                return frame

        assert _lyrics_from_uslt(Tags()) == "my song lyrics"

    def test_uslt_no_text_skipped(self) -> None:
        frame = SimpleNamespace(text=None)

        class Tags:
            def keys(self):
                return ["USLT::eng"]

            def __getitem__(self, k):
                return frame

        assert _lyrics_from_uslt(Tags()) == ""


class TestLyricsFromMp4:
    def test_keyerror_continues(self) -> None:
        tags = {}  # no \xa9lyr key
        assert _lyrics_from_mp4(tags) == ""

    def test_bytes_decoded(self) -> None:
        tags = {"\xa9lyr": [b"\xc2\xa9 lyrics here"]}
        out = _lyrics_from_mp4(tags)
        assert "lyrics here" in out

    def test_str_returned(self) -> None:
        tags = {"\xa9lyr": ["plain string"]}
        assert _lyrics_from_mp4(tags) == "plain string"

    def test_non_list_value(self) -> None:
        tags = {"\xa9lyr": "raw"}
        # First evaluates as raw[0] = 'r' (str slicing) -> isinstance(str) -> returns "r"
        assert _lyrics_from_mp4(tags) in ("r", "raw")


class TestParseLrcMalformed:
    def test_invalid_seconds_skipped(self) -> None:
        # Timestamp with non-numeric seconds part ([01:xx.yy])
        # The regex ([0-9]+):([0-9.]+) won't match xx.yy so the line just won't be parsed.
        # Use a parseable-by-regex but ValueError-on-int line
        # Force ValueError: regex captures (\d+) and ([\d.]+).  Use a value that has too many dots.
        text = "[01:1.2.3]hello\n[02:30.5]world"
        result = parse_lrc(text)
        # 01:1.2.3 -> float("1.2.3") raises ValueError -> skipped
        # only the second line should be present
        assert any(line.text == "world" for line in result)


# ---------------------------------------------------------------------------
# autodj.audio_meta  (_id3_get defensive branches)
# ---------------------------------------------------------------------------


class TestAudioMetaDefensive:
    def test_id3_get_returns_none_when_tags_missing(self) -> None:
        from autodj.audio_meta import _id3_get

        m = SimpleNamespace(tags=None)
        assert _id3_get(m, "TIT2") is None

    def test_id3_get_returns_none_when_value_is_none(self) -> None:
        from autodj.audio_meta import _id3_get

        class _Tags:
            def __getitem__(self, key: str) -> None:
                return None

        m = SimpleNamespace(tags=_Tags())
        assert _id3_get(m, "TIT2") is None

    def test_id3_get_returns_none_on_keyerror(self) -> None:
        from autodj.audio_meta import _id3_get

        class _Tags:
            def __getitem__(self, key: str) -> str:
                raise KeyError(key)

        m = SimpleNamespace(tags=_Tags())
        assert _id3_get(m, "TIT2") is None
