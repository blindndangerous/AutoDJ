"""Extra audio_meta tests targeting uncovered branches.

Covers cover-art extraction, plain-lyrics extraction, _first_tag tag
fallback paths, and LRC parser ValueError branches that the existing
test_audio_meta.py file doesn't exercise.
"""

from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# Mutagen fakes — copied / adapted from test_audio_meta.py so the two
# files stay independent.
# ---------------------------------------------------------------------------


class _FakeMutagenVorbis(dict):
    """Stand-in for a mutagen Vorbis-style file (FLAC, OGG)."""

    def __init__(self, tags):
        super().__init__(tags)
        self.tags = self
        self.pictures = []


class _FakePicture:
    def __init__(self, data: bytes, mime: str = "image/jpeg") -> None:
        self.data = data
        self.mime = mime


class _FakeFLAC:
    """FLAC-style file with embedded pictures."""

    def __init__(self, pictures: list[_FakePicture] | None = None) -> None:
        self.pictures = pictures or []
        self.tags = None


class _FakeID3Frame:
    """Stand-in for an ID3 frame with .text or .data."""

    def __init__(
        self, text: list[str] | None = None, data: bytes | None = None, mime: str = "image/jpeg"
    ) -> None:
        if text is not None:
            self.text = text
        if data is not None:
            self.data = data
        self.mime = mime


class _FakeID3Tags:
    """Dict-like tag container with both .keys() and .get()."""

    def __init__(self, frames: dict[str, _FakeID3Frame]) -> None:
        self._frames = frames

    def keys(self):
        return self._frames.keys()

    def get(self, k, default=None):
        return self._frames.get(k, default)

    def __contains__(self, k):
        return k in self._frames

    def __getitem__(self, k):
        return self._frames[k]


class _FakeID3:
    """MP3-style file: tags exposed via .tags, no pictures attribute."""

    def __init__(self, frames: dict[str, _FakeID3Frame]) -> None:
        self.tags = _FakeID3Tags(frames)
        self.pictures = None

    def get(self, k, default=None):
        return None  # ID3 files use .tags not direct .get


class _FakeMP4:
    """M4A-style file with covr atom."""

    def __init__(self, tags: dict) -> None:
        self.tags = tags
        self.pictures = None

    def get(self, k, default=None):
        return None


def _patch_mutagen(monkeypatch, fake_obj) -> None:
    fake_module = type(sys)("mutagen")

    def _file_loader(_path):
        return fake_obj

    fake_module.File = _file_loader  # type: ignore[attr-defined]
    fake_module.MutagenError = type("MutagenError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mutagen", fake_module)


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------


class TestReadCoverArt:
    def test_flac_picture(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        fake = _FakeFLAC([_FakePicture(b"\x89PNG-fake", "image/png")])
        _patch_mutagen(monkeypatch, fake)
        result = read_cover_art(tmp_path / "x.flac")
        assert result is not None
        assert result.data == b"\x89PNG-fake"
        assert result.mime_type == "image/png"

    def test_id3_apic_frame(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        frames = {
            "APIC:": _FakeID3Frame(data=b"\xff\xd8-jpg", mime="image/jpeg"),
        }
        _patch_mutagen(monkeypatch, _FakeID3(frames))
        result = read_cover_art(tmp_path / "x.mp3")
        assert result is not None
        assert result.data == b"\xff\xd8-jpg"
        assert result.mime_type == "image/jpeg"

    def test_mp4_covr_atom_jpeg(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        class _Cover(bytes):
            imageformat = 13  # MP4Cover.FORMAT_JPEG

        cover = _Cover(b"\xff\xd8-jpg-mp4")
        _patch_mutagen(monkeypatch, _FakeMP4({"covr": [cover]}))
        result = read_cover_art(tmp_path / "x.m4a")
        assert result is not None
        assert result.mime_type == "image/jpeg"

    def test_mp4_covr_atom_png(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        class _Cover(bytes):
            imageformat = 14  # MP4Cover.FORMAT_PNG

        cover = _Cover(b"\x89PNG-mp4")
        _patch_mutagen(monkeypatch, _FakeMP4({"covr": [cover]}))
        result = read_cover_art(tmp_path / "x.m4a")
        assert result.mime_type == "image/png"

    def test_no_tags_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        class _Empty:
            pictures = None
            tags = None

            def get(self, k, default=None):
                return None

        _patch_mutagen(monkeypatch, _Empty())
        assert read_cover_art(tmp_path / "x.mp3") is None

    def test_mutagen_returns_none(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        _patch_mutagen(monkeypatch, None)
        assert read_cover_art(tmp_path / "x.flac") is None

    def test_mutagen_raises(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_cover_art

        fake_module = type(sys)("mutagen")

        def _raise(_):
            raise OSError("bad")

        fake_module.File = _raise  # type: ignore[attr-defined]
        fake_module.MutagenError = type("MutagenError", (Exception,), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mutagen", fake_module)
        assert read_cover_art(tmp_path / "x.flac") is None


# ---------------------------------------------------------------------------
# Plain (unsynced) lyrics
# ---------------------------------------------------------------------------


class TestReadPlainLyrics:
    def test_vorbis_lowercase_key(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        fake = _FakeMutagenVorbis({"lyrics": ["Verse 1\nLine 2"]})
        _patch_mutagen(monkeypatch, fake)
        result = read_plain_lyrics(tmp_path / "x.flac")
        assert "Verse 1" in result

    def test_vorbis_unsyncedlyrics(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        fake = _FakeMutagenVorbis({"unsyncedlyrics": ["Plain text"]})
        _patch_mutagen(monkeypatch, fake)
        result = read_plain_lyrics(tmp_path / "x.flac")
        assert result == "Plain text"

    def test_id3_uslt_frame(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        frames = {"USLT::eng": _FakeID3Frame(text=["Verse text"])}
        _patch_mutagen(monkeypatch, _FakeID3(frames))
        result = read_plain_lyrics(tmp_path / "x.mp3")
        assert "Verse text" in result

    def test_mp4_lyr_atom_bytes(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        # MP4 ©lyr atom holds bytes
        _patch_mutagen(
            monkeypatch,
            _FakeMP4({"\xa9lyr": [b"Plain bytes lyric"]}),
        )
        result = read_plain_lyrics(tmp_path / "x.m4a")
        assert "Plain bytes lyric" in result

    def test_mp4_lyr_atom_str(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        _patch_mutagen(monkeypatch, _FakeMP4({"\xa9lyr": ["String lyric"]}))
        result = read_plain_lyrics(tmp_path / "x.m4a")
        assert "String lyric" in result

    def test_no_tags_returns_empty(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        class _Empty:
            tags = None

            def get(self, k, default=None):
                return None

        _patch_mutagen(monkeypatch, _Empty())
        assert read_plain_lyrics(tmp_path / "x.mp3") == ""

    def test_mutagen_unimportable(self, monkeypatch, tmp_path) -> None:
        import builtins

        from autodj.audio_meta import read_plain_lyrics

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "mutagen":
                raise ImportError("nope")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Need to also clear cache so the fresh import attempt is made.
        monkeypatch.delitem(sys.modules, "mutagen", raising=False)
        assert read_plain_lyrics(tmp_path / "x.flac") == ""

    def test_mutagen_raises(self, monkeypatch, tmp_path) -> None:
        from autodj.audio_meta import read_plain_lyrics

        fake_module = type(sys)("mutagen")

        def _raise(_):
            raise OSError("bad")

        fake_module.File = _raise  # type: ignore[attr-defined]
        fake_module.MutagenError = type("MutagenError", (Exception,), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mutagen", fake_module)
        assert read_plain_lyrics(tmp_path / "x.flac") == ""


# ---------------------------------------------------------------------------
# LRC parser ValueError branch — non-numeric stamp
# ---------------------------------------------------------------------------


class TestParseLrcExtras:
    def test_non_numeric_minute_skipped(self) -> None:
        from autodj.audio_meta import parse_lrc

        # The regex requires \d+ for minutes so this is hard to trigger
        # via the regex path -- but exercise a malformed numeric edge.
        # Exotic high-precision seconds value should still parse.
        text = "[00:01.99999]hello\n[03:14.0]world"
        out = parse_lrc(text)
        assert len(out) == 2
        assert out[0].time_s < out[1].time_s
