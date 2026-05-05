"""Tests for autodj.audio_meta — ReplayGain, cover art, LRC lyrics."""

from __future__ import annotations

import pytest

from autodj.audio_meta import (
    LyricLine,
    ReplayGain,
    current_lyric,
    load_lrc_for,
    parse_lrc,
    replaygain_multiplier,
)

# ---------------------------------------------------------------------------
# ReplayGain
# ---------------------------------------------------------------------------


class TestReplayGainMultiplier:
    def test_none_returns_unity(self) -> None:
        assert replaygain_multiplier(None) == 1.0

    def test_negative_gain_attenuates(self) -> None:
        rg = ReplayGain(track_gain_db=-6.0, track_peak=0.5)
        m = replaygain_multiplier(rg, target_db=-14.0)
        # +6 dB above original RG reference (-18 → -14 = +4 dB),
        # then -6 dB track gain → -2 dB net → ~0.794×
        assert 0.5 < m < 1.5

    def test_clip_safe_caps_gain(self) -> None:
        # Track gain of +6 dB but peak is already at 1.0 — should clamp
        rg = ReplayGain(track_gain_db=+6.0, track_peak=1.0)
        m = replaygain_multiplier(rg, target_db=-14.0, max_clip_safe_gain=1.0)
        assert m <= 1.0

    def test_zero_peak_is_safe(self) -> None:
        rg = ReplayGain(track_gain_db=0.0, track_peak=0.0)
        m = replaygain_multiplier(rg)
        assert m >= 0.0

    def test_target_db_louder_than_default(self) -> None:
        rg = ReplayGain(track_gain_db=-6.0, track_peak=0.5)
        m_quiet = replaygain_multiplier(rg, target_db=-18.0)
        m_loud = replaygain_multiplier(rg, target_db=-10.0)
        assert m_loud > m_quiet


# ---------------------------------------------------------------------------
# LRC parsing
# ---------------------------------------------------------------------------


class TestParseLRC:
    def test_simple_timestamps(self) -> None:
        text = "[00:05.50]first line\n[00:12.30]second line"
        lines = parse_lrc(text)
        assert len(lines) == 2
        assert lines[0].time_s == pytest.approx(5.5)
        assert lines[0].text == "first line"
        assert lines[1].time_s == pytest.approx(12.3)
        assert lines[1].text == "second line"

    def test_metadata_tags_skipped(self) -> None:
        text = "[ar:Some Artist]\n[ti:Track Title]\n[00:01.00]actual lyric"
        lines = parse_lrc(text)
        assert len(lines) == 1
        assert lines[0].text == "actual lyric"

    def test_multiple_timestamps_per_line(self) -> None:
        text = "[00:10.00][00:25.00]repeated chorus"
        lines = parse_lrc(text)
        assert len(lines) == 2
        assert all(ll.text == "repeated chorus" for ll in lines)
        assert lines[0].time_s == pytest.approx(10.0)
        assert lines[1].time_s == pytest.approx(25.0)

    def test_minutes_over_60(self) -> None:
        text = "[03:45.00]long song"
        lines = parse_lrc(text)
        assert lines[0].time_s == pytest.approx(225.0)

    def test_sorted_by_time(self) -> None:
        text = "[00:30.00]later\n[00:10.00]earlier"
        lines = parse_lrc(text)
        assert lines[0].time_s < lines[1].time_s

    def test_empty_string(self) -> None:
        assert parse_lrc("") == []

    def test_garbage_input_returns_empty(self) -> None:
        assert parse_lrc("not an LRC file at all") == []


# ---------------------------------------------------------------------------
# load_lrc_for sidecar lookup
# ---------------------------------------------------------------------------


class TestLoadLrcFor:
    def test_loads_sibling_lrc(self, tmp_path) -> None:
        audio = tmp_path / "song.flac"
        audio.write_bytes(b"")  # placeholder
        lrc = tmp_path / "song.lrc"
        lrc.write_text("[00:01.00]hello", encoding="utf-8")
        lines = load_lrc_for(audio)
        assert len(lines) == 1
        assert lines[0].text == "hello"

    def test_no_sidecar_returns_empty(self, tmp_path) -> None:
        audio = tmp_path / "song.flac"
        audio.write_bytes(b"")
        assert load_lrc_for(audio) == []

    def test_unreadable_lrc_returns_empty(self, tmp_path) -> None:
        # Pass a directory as the audio path — .lrc sibling doesn't exist
        assert load_lrc_for(tmp_path / "nonexistent.flac") == []


# ---------------------------------------------------------------------------
# current_lyric
# ---------------------------------------------------------------------------


class TestCurrentLyric:
    def test_returns_active_line(self) -> None:
        lines = [
            LyricLine(time_s=0.0, text="intro"),
            LyricLine(time_s=10.0, text="verse"),
            LyricLine(time_s=30.0, text="chorus"),
        ]
        assert current_lyric(lines, 5.0).text == "intro"
        assert current_lyric(lines, 15.0).text == "verse"
        assert current_lyric(lines, 35.0).text == "chorus"

    def test_before_first_returns_none(self) -> None:
        lines = [LyricLine(time_s=10.0, text="late")]
        assert current_lyric(lines, 5.0) is None

    def test_empty_list_returns_none(self) -> None:
        assert current_lyric([], 10.0) is None

    def test_exactly_at_time_returns_that_line(self) -> None:
        lines = [LyricLine(time_s=10.0, text="exact")]
        assert current_lyric(lines, 10.0).text == "exact"


# ---------------------------------------------------------------------------
# read_replaygain
# ---------------------------------------------------------------------------


class _FakeMutagenVorbis:
    """Stand-in for a mutagen FLAC/Vorbis file: dict-like .get + .tags."""

    def __init__(self, tags: dict[str, list[str]], pictures: list | None = None) -> None:
        self._tags = tags
        self.tags = tags
        self.pictures = pictures or []

    def get(self, key: str) -> list[str] | None:
        return self._tags.get(key)


class _FakeID3Frame:
    def __init__(self, text: list[str], data: bytes = b"", mime: str = "image/jpeg") -> None:
        self.text = text
        self.data = data
        self.mime = mime


class _FakeID3Tags(dict):
    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key, default)


class _FakeMutagenID3:
    def __init__(self, tags: _FakeID3Tags) -> None:
        self.tags = tags
        self.pictures = []


def _patch_mutagen_file(monkeypatch, fake_obj):
    """Patch the import inside read_replaygain / read_cover_art."""
    import sys

    fake_module = type(sys)("mutagen")
    fake_module.File = lambda _path: fake_obj  # type: ignore[attr-defined]
    fake_module.MutagenError = type("MutagenError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mutagen", fake_module)


class TestReadReplayGainVorbis:
    def test_lowercase_keys(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        fake = _FakeMutagenVorbis(
            {
                "replaygain_track_gain": ["-6.50 dB"],
                "replaygain_track_peak": ["0.95"],
            }
        )
        _patch_mutagen_file(monkeypatch, fake)
        rg = read_replaygain(tmp_path / "x.flac")
        assert rg is not None
        assert rg.track_gain_db == -6.5
        assert rg.track_peak == 0.95

    def test_uppercase_keys(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        fake = _FakeMutagenVorbis(
            {
                "REPLAYGAIN_TRACK_GAIN": ["-3.0 dB"],
                "REPLAYGAIN_TRACK_PEAK": ["0.8"],
            }
        )
        _patch_mutagen_file(monkeypatch, fake)
        rg = read_replaygain(tmp_path / "x.flac")
        assert rg.track_gain_db == -3.0
        assert rg.track_peak == 0.8

    def test_no_peak_defaults_to_one(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        fake = _FakeMutagenVorbis({"replaygain_track_gain": ["-2.0 dB"]})
        _patch_mutagen_file(monkeypatch, fake)
        rg = read_replaygain(tmp_path / "x.flac")
        assert rg.track_peak == 1.0

    def test_unparseable_gain_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        fake = _FakeMutagenVorbis({"replaygain_track_gain": ["banana"]})
        _patch_mutagen_file(monkeypatch, fake)
        assert read_replaygain(tmp_path / "x.flac") is None

    def test_no_tags_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        fake = _FakeMutagenVorbis({})
        _patch_mutagen_file(monkeypatch, fake)
        assert read_replaygain(tmp_path / "x.flac") is None

    def test_mutagen_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        _patch_mutagen_file(monkeypatch, None)
        assert read_replaygain(tmp_path / "x.flac") is None

    def test_mutagen_raises(self, monkeypatch, tmp_path) -> None:
        import sys

        from autodj.audio_meta import read_replaygain

        fake_module = type(sys)("mutagen")

        def _raise(_):
            raise OSError("bad file")

        fake_module.File = _raise  # type: ignore[attr-defined]
        fake_module.MutagenError = type("MutagenError", (Exception,), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mutagen", fake_module)
        assert read_replaygain(tmp_path / "x.flac") is None

    def test_mutagen_unimportable(self, monkeypatch, tmp_path) -> None:

        from autodj.audio_meta import read_replaygain

        # Block import by setting the module to a sentinel that raises
        # on attribute access
        class _Block:
            def __getattr__(self, _):
                raise ImportError("blocked")

        # Actual unimport: remove from sys.modules and add finder that raises.
        # Easier: patch _the function's import with monkeypatch_dict trick.
        # Instead patch builtins.__import__:
        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "mutagen":
                raise ImportError("blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        assert read_replaygain(tmp_path / "x.flac") is None


class TestReadReplayGainID3:
    def test_id3_txxx_frame(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        tags = _FakeID3Tags(
            {
                "TXXX:replaygain_track_gain": _FakeID3Frame(["-4.5 dB"]),
                "TXXX:replaygain_track_peak": _FakeID3Frame(["0.7"]),
            }
        )
        # Empty .get(...) for the Vorbis path
        fake = _FakeMutagenID3(tags)
        # The Vorbis dict-style .get on _FakeMutagenID3 doesn't exist,
        # but the code only calls m.get(...) when hasattr(m, 'get').
        _patch_mutagen_file(monkeypatch, fake)
        rg = read_replaygain(tmp_path / "x.mp3")
        assert rg.track_gain_db == -4.5
        assert rg.track_peak == 0.7

    def test_id3_uppercase_frame(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        tags = _FakeID3Tags(
            {
                "TXXX:REPLAYGAIN_TRACK_GAIN": _FakeID3Frame(["-2.0 dB"]),
                "TXXX:REPLAYGAIN_TRACK_PEAK": _FakeID3Frame(["0.99"]),
            }
        )
        fake = _FakeMutagenID3(tags)
        _patch_mutagen_file(monkeypatch, fake)
        rg = read_replaygain(tmp_path / "x.mp3")
        assert rg.track_gain_db == -2.0


class TestReadReplayGainMP4:
    def test_m4a_freeform_atoms(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        tags = _FakeID3Tags(
            {
                "----:com.apple.iTunes:replaygain_track_gain": [b"-7.0 dB"],
                "----:com.apple.iTunes:replaygain_track_peak": [b"0.92"],
            }
        )
        fake = _FakeMutagenID3(tags)
        _patch_mutagen_file(monkeypatch, fake)
        rg = read_replaygain(tmp_path / "x.m4a")
        assert rg.track_gain_db == -7.0
        assert rg.track_peak == 0.92

    def test_m4a_str_values(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_replaygain

        tags = _FakeID3Tags(
            {
                "----:com.apple.iTunes:replaygain_track_gain": ["-3.0 dB"],
            }
        )
        fake = _FakeMutagenID3(tags)
        _patch_mutagen_file(monkeypatch, fake)
        rg = read_replaygain(tmp_path / "x.m4a")
        assert rg.track_gain_db == -3.0


# ---------------------------------------------------------------------------
# read_cover_art
# ---------------------------------------------------------------------------


class _FakePicture:
    def __init__(self, data: bytes, mime: str = "image/jpeg") -> None:
        self.data = data
        self.mime = mime


class _FakeMP4Cover(bytes):
    """MP4 covr atom — bytes subclass with .imageformat attribute."""

    def __new__(cls, data: bytes, imageformat: int = 13):
        obj = super().__new__(cls, data)
        obj.imageformat = imageformat  # type: ignore[attr-defined]
        return obj


class TestReadCoverArt:
    def test_flac_pictures(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        fake = _FakeMutagenVorbis({}, pictures=[_FakePicture(b"JPEGDATA", "image/jpeg")])
        _patch_mutagen_file(monkeypatch, fake)
        art = read_cover_art(tmp_path / "x.flac")
        assert art.data == b"JPEGDATA"
        assert art.mime_type == "image/jpeg"

    def test_id3_apic(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        tags = _FakeID3Tags({"APIC:": _FakeID3Frame([], data=b"PNGDATA", mime="image/png")})
        fake = _FakeMutagenID3(tags)
        _patch_mutagen_file(monkeypatch, fake)
        art = read_cover_art(tmp_path / "x.mp3")
        assert art.data == b"PNGDATA"
        assert art.mime_type == "image/png"

    def test_mp4_covr_jpeg(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        tags = _FakeID3Tags({"covr": [_FakeMP4Cover(b"JPEG", imageformat=13)]})
        fake = _FakeMutagenID3(tags)
        _patch_mutagen_file(monkeypatch, fake)
        art = read_cover_art(tmp_path / "x.m4a")
        assert art.data == b"JPEG"
        assert art.mime_type == "image/jpeg"

    def test_mp4_covr_png(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        tags = _FakeID3Tags({"covr": [_FakeMP4Cover(b"PNG", imageformat=14)]})
        fake = _FakeMutagenID3(tags)
        _patch_mutagen_file(monkeypatch, fake)
        art = read_cover_art(tmp_path / "x.m4a")
        assert art.mime_type == "image/png"

    def test_no_art_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        fake = _FakeMutagenVorbis({}, pictures=[])
        _patch_mutagen_file(monkeypatch, fake)
        assert read_cover_art(tmp_path / "x.flac") is None

    def test_mutagen_none_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        _patch_mutagen_file(monkeypatch, None)
        assert read_cover_art(tmp_path / "x.flac") is None

    def test_mutagen_raises(self, monkeypatch, tmp_path) -> None:
        import sys

        from autodj.audio_meta import read_cover_art

        fake_module = type(sys)("mutagen")

        def _raise(_):
            raise OSError("bad")

        fake_module.File = _raise  # type: ignore[attr-defined]
        fake_module.MutagenError = type("MutagenError", (Exception,), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mutagen", fake_module)
        assert read_cover_art(tmp_path / "x.flac") is None


# ---------------------------------------------------------------------------
# read_file_tags — ID3 / Vorbis / MP4 metadata fallback
# ---------------------------------------------------------------------------


class _FakeInfo:
    def __init__(self, length: float = 180.0):
        self.length = length


class _FakeVorbisFile:
    """FLAC-like — dict-style .get + .info."""

    def __init__(self, tags: dict[str, list[str]], length: float = 180.0):
        self._tags = tags
        self.tags = tags
        self.info = _FakeInfo(length)
        self.pictures: list = []

    def get(self, key, default=None):
        return self._tags.get(key, default)


class TestReadFileTags:
    def test_vorbis_full(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_file_tags

        fake = _FakeVorbisFile(
            {
                "title": ["Song"],
                "artist": ["Artist"],
                "album": ["Album"],
                "genre": ["Trip-Hop"],
                "bpm": ["95"],
                "date": ["1994-02-01"],
            },
            length=240.0,
        )
        _patch_mutagen_file(monkeypatch, fake)
        tags = read_file_tags(tmp_path / "x.flac")
        assert tags.title == "Song"
        assert tags.artist == "Artist"
        assert tags.album == "Album"
        assert tags.genre == "Trip-Hop"
        assert tags.bpm == 95.0
        assert tags.year == 1994
        assert tags.length == 240.0

    def test_vorbis_partial(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_file_tags

        fake = _FakeVorbisFile({"title": ["Solo"]}, length=120.0)
        _patch_mutagen_file(monkeypatch, fake)
        tags = read_file_tags(tmp_path / "x.flac")
        assert tags.title == "Solo"
        assert tags.artist == ""
        assert tags.bpm == 0.0
        assert tags.year == 0
        assert tags.length == 120.0

    def test_bad_bpm_silently_zero(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_file_tags

        fake = _FakeVorbisFile({"bpm": ["banana"]})
        _patch_mutagen_file(monkeypatch, fake)
        assert read_file_tags(tmp_path / "x.flac").bpm == 0.0

    def test_bad_year_silently_zero(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_file_tags

        fake = _FakeVorbisFile({"date": ["xxxx"]})
        _patch_mutagen_file(monkeypatch, fake)
        assert read_file_tags(tmp_path / "x.flac").year == 0

    def test_mutagen_unavailable(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_file_tags

        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "mutagen":
                raise ImportError
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        tags = read_file_tags(tmp_path / "x.flac")
        assert tags.title == ""

    def test_mutagen_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_file_tags

        _patch_mutagen_file(monkeypatch, None)
        tags = read_file_tags(tmp_path / "x.flac")
        assert tags == tags.__class__()

    def test_mutagen_raises(self, monkeypatch, tmp_path) -> None:
        import sys

        from autodj.audio_meta import read_file_tags

        fake_module = type(sys)("mutagen")

        def _raise(_):
            raise OSError("bad")

        fake_module.File = _raise  # type: ignore[attr-defined]
        fake_module.MutagenError = type("MutagenError", (Exception,), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mutagen", fake_module)
        assert read_file_tags(tmp_path / "x.flac").title == ""

    def test_id3_frame_with_text(self, monkeypatch, tmp_path) -> None:
        """ID3 path — m.tags[k] returns a frame with .text."""
        from autodj.audio_meta import read_file_tags

        class _FakeID3File:
            def __init__(self) -> None:
                self.tags = _FakeID3Tags(
                    {
                        "TIT2": _FakeID3Frame(["MP3 Title"]),
                        "TPE1": _FakeID3Frame(["MP3 Artist"]),
                    }
                )
                self.info = _FakeInfo(150.0)

        _patch_mutagen_file(monkeypatch, _FakeID3File())
        tags = read_file_tags(tmp_path / "x.mp3")
        assert tags.title == "MP3 Title"
        assert tags.artist == "MP3 Artist"
        assert tags.length == 150.0

    def test_mp4_bytes_and_int(self, monkeypatch, tmp_path) -> None:
        """MP4 atom — bytes decode + int tmpo."""
        from autodj.audio_meta import read_file_tags

        class _FakeMP4File:
            def __init__(self) -> None:
                self.tags = _FakeID3Tags(
                    {
                        "\xa9nam": [b"MP4 Title"],
                        "tmpo": [128],
                    }
                )
                self.info = _FakeInfo(200.0)

        _patch_mutagen_file(monkeypatch, _FakeMP4File())
        tags = read_file_tags(tmp_path / "x.m4a")
        assert tags.title == "MP4 Title"
        assert tags.bpm == 128.0


# ---------------------------------------------------------------------------
# read_plain_lyrics — USLT / Vorbis / MP4 fallback
# ---------------------------------------------------------------------------


class TestReadPlainLyrics:
    def test_returns_empty_when_no_tags(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        _patch_mutagen_file(monkeypatch, None)
        assert read_plain_lyrics(tmp_path / "x.flac") == ""

    def test_vorbis_lyrics_tag(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        fake = _FakeMutagenVorbis({"lyrics": ["line one\nline two"]})
        _patch_mutagen_file(monkeypatch, fake)
        out = read_plain_lyrics(tmp_path / "x.flac")
        assert "line one" in out
        assert "line two" in out

    def test_uppercase_lyrics_tag(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        fake = _FakeMutagenVorbis({"LYRICS": ["upper case"]})
        _patch_mutagen_file(monkeypatch, fake)
        assert read_plain_lyrics(tmp_path / "x.flac") == "upper case"

    def test_id3_uslt(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        class _USLTFrame:
            def __init__(self, text: str) -> None:
                self.text = text

        tags = _FakeID3Tags({"USLT::eng": _USLTFrame("hello world")})

        class _FakeID3File:
            def __init__(self) -> None:
                self.tags = tags

            def get(self, _key, default=None):
                return default

        _patch_mutagen_file(monkeypatch, _FakeID3File())
        assert read_plain_lyrics(tmp_path / "x.mp3") == "hello world"

    def test_mp4_lyr_atom_bytes(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        tags = _FakeID3Tags({"\xa9lyr": [b"mp4 lyrics here"]})

        class _FakeMP4File:
            def __init__(self) -> None:
                self.tags = tags

            def get(self, _key, default=None):
                return default

        _patch_mutagen_file(monkeypatch, _FakeMP4File())
        assert read_plain_lyrics(tmp_path / "x.m4a") == "mp4 lyrics here"

    def test_no_module_returns_empty(self, tmp_path, monkeypatch) -> None:
        import sys

        from autodj.audio_meta import read_plain_lyrics

        # Hide mutagen so the function early-exits.
        monkeypatch.setitem(sys.modules, "mutagen", None)
        assert read_plain_lyrics(tmp_path / "x.flac") == ""
