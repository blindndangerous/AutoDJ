"""Unit tests for autodj.config.

Tests cover config loading, validation, default values, and error handling.
All tests are pure — no filesystem I/O beyond temp files provided by fixtures.
"""

import tomllib
from pathlib import Path

import pytest

from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
    load_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_toml(tmp_path: Path) -> Path:
    """A config file with only required keys."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[library]\nmusic_dir = "Z:/Music"\n'
        '[index]\nindex_dir = "index"\nmodel_dir = "models"\n'
        '[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 50\n'
        '[model]\nname = "m-a-p/MERT-v1-330M"\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def full_toml(tmp_path: Path) -> Path:
    """A config file with all optional keys populated."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[library]\n'
        'music_dir = "Z:/Music"\n'
        'beets_db = "C:/beets/library.db"\n'
        'supported_formats = ["mp3", "flac"]\n'
        '[index]\n'
        'index_dir = "Z:/autodj-index"\n'
        'model_dir = "models"\n'
        '[playback]\n'
        'crossfade_seconds = 5.0\n'
        'no_repeat_window = 100\n'
        '[model]\n'
        'name = "m-a-p/MERT-v1-95M"\n'
        'manual_path = "C:/models/MERT"\n',
        encoding="utf-8",
    )
    return cfg


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_loads_minimal_config(self, minimal_toml: Path) -> None:
        cfg = load_config(minimal_toml)
        assert isinstance(cfg, AutoDJConfig)

    def test_raises_if_file_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.toml")

    def test_raises_if_not_toml(self, tmp_path: Path) -> None:
        bad = tmp_path / "config.toml"
        bad.write_text("this is not valid toml ][", encoding="utf-8")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(bad)

    def test_loads_full_config(self, full_toml: Path) -> None:
        cfg = load_config(full_toml)
        assert cfg.playback.crossfade_seconds == 5.0
        assert cfg.playback.no_repeat_window == 100
        assert cfg.model.name == "m-a-p/MERT-v1-95M"
        assert cfg.model.manual_path == Path("C:/models/MERT")


# ---------------------------------------------------------------------------
# LibraryConfig
# ---------------------------------------------------------------------------


class TestLibraryConfig:
    def test_music_dir_required(self) -> None:
        with pytest.raises((TypeError, KeyError, ValueError)):
            LibraryConfig.from_dict({})

    def test_default_formats(self) -> None:
        lib = LibraryConfig.from_dict({"music_dir": "Z:/Music"})
        assert "mp3" in lib.supported_formats
        assert "flac" in lib.supported_formats
        assert "m4a" in lib.supported_formats

    def test_custom_formats(self) -> None:
        lib = LibraryConfig.from_dict(
            {"music_dir": "Z:/Music", "supported_formats": ["flac"]}
        )
        assert lib.supported_formats == ["flac"]

    def test_beets_db_optional(self) -> None:
        lib = LibraryConfig.from_dict({"music_dir": "Z:/Music"})
        assert lib.beets_db is None

    def test_beets_db_as_path(self) -> None:
        lib = LibraryConfig.from_dict(
            {"music_dir": "Z:/Music", "beets_db": "C:/beets/library.db"}
        )
        assert lib.beets_db == Path("C:/beets/library.db")

    def test_music_dir_as_path(self) -> None:
        lib = LibraryConfig.from_dict({"music_dir": "Z:/Music"})
        assert isinstance(lib.music_dir, Path)


# ---------------------------------------------------------------------------
# IndexConfig
# ---------------------------------------------------------------------------


class TestIndexConfig:
    def test_defaults(self) -> None:
        idx = IndexConfig.from_dict({})
        assert idx.index_dir == Path("index")
        assert idx.model_dir == Path("models")

    def test_custom_paths(self) -> None:
        idx = IndexConfig.from_dict(
            {"index_dir": "Z:/autodj-index", "model_dir": "Z:/models"}
        )
        assert idx.index_dir == Path("Z:/autodj-index")


# ---------------------------------------------------------------------------
# PlaybackConfig
# ---------------------------------------------------------------------------


class TestPlaybackConfig:
    def test_defaults(self) -> None:
        pb = PlaybackConfig.from_dict({})
        assert pb.crossfade_seconds == 3.0
        assert pb.no_repeat_window == 50

    def test_crossfade_zero_allowed(self) -> None:
        pb = PlaybackConfig.from_dict({"crossfade_seconds": 0.0})
        assert pb.crossfade_seconds == 0.0

    def test_crossfade_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            PlaybackConfig.from_dict({"crossfade_seconds": -1.0})

    def test_no_repeat_window_zero_allowed(self) -> None:
        pb = PlaybackConfig.from_dict({"no_repeat_window": 0})
        assert pb.no_repeat_window == 0

    def test_no_repeat_window_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            PlaybackConfig.from_dict({"no_repeat_window": -5})


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------


class TestModelConfig:
    def test_default_model_name(self) -> None:
        mc = ModelConfig.from_dict({})
        assert mc.name == "m-a-p/MERT-v1-330M"

    def test_manual_path_none_by_default(self) -> None:
        mc = ModelConfig.from_dict({})
        assert mc.manual_path is None

    def test_manual_path_as_path(self) -> None:
        mc = ModelConfig.from_dict({"manual_path": "C:/models/MERT"})
        assert mc.manual_path == Path("C:/models/MERT")

    def test_custom_model_name(self) -> None:
        mc = ModelConfig.from_dict({"name": "m-a-p/MERT-v1-95M"})
        assert mc.name == "m-a-p/MERT-v1-95M"


# ---------------------------------------------------------------------------
# AutoDJConfig integration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HuggingFaceConfig
# ---------------------------------------------------------------------------


class TestHuggingFaceConfig:
    def test_token_none_by_default(self) -> None:
        hf = HuggingFaceConfig.from_dict({})
        assert hf.token is None

    def test_token_set(self) -> None:
        hf = HuggingFaceConfig.from_dict({"token": "hf_abc123"})
        assert hf.token == "hf_abc123"

    def test_empty_string_token_treated_as_none(self) -> None:
        hf = HuggingFaceConfig.from_dict({"token": ""})
        assert hf.token is None


# ---------------------------------------------------------------------------
# AutoDJConfig integration
# ---------------------------------------------------------------------------


class TestAutoDJConfig:
    def test_round_trips_all_sections(self, full_toml: Path) -> None:
        cfg = load_config(full_toml)
        assert isinstance(cfg.library, LibraryConfig)
        assert isinstance(cfg.index, IndexConfig)
        assert isinstance(cfg.playback, PlaybackConfig)
        assert isinstance(cfg.model, ModelConfig)
        assert isinstance(cfg.huggingface, HuggingFaceConfig)

    def test_config_dir_property(self, minimal_toml: Path) -> None:
        cfg = load_config(minimal_toml)
        assert cfg.config_path == minimal_toml

    def test_huggingface_token_defaults_none(self, minimal_toml: Path) -> None:
        cfg = load_config(minimal_toml)
        assert cfg.huggingface.token is None
