"""Unit tests for autodj.config.

Tests cover config loading, validation, default values, and error handling.
All tests are pure — no filesystem I/O beyond temp files provided by fixtures.
"""

import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest

from autodj.config import (
    ENVIRONMENT_OVERLAY,
    AutoDJConfig,
    DjMixConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
    ServerConfig,
    StationConfig,
    _deep_merge,
    canonicalize_allowed_origin,
    is_loopback_bind,
    load_config,
    move_station_tables,
    validate_server_exposure,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_autodj_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for variable in ENVIRONMENT_OVERLAY:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def minimal_toml(tmp_path: Path) -> Path:
    """A config file with only required keys."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[library]\nmusic_dir = "Z:/Music"\n'
        '[index]\nindex_dir = "index"\nmodel_dir = "models"\n'
        "[playback]\ncrossfade_seconds = 3.0\nrepeat_rules = { track = { tracks = 50 } }\n"
        '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def full_toml(tmp_path: Path) -> Path:
    """A config file with all optional keys populated."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[library]\n"
        'music_dir = "Z:/Music"\n'
        'beets_db = "C:/beets/library.db"\n'
        'supported_formats = ["mp3", "flac"]\n'
        "[index]\n"
        'index_dir = "Z:/autodj-index"\n'
        'model_dir = "models"\n'
        "[playback]\n"
        "crossfade_seconds = 5.0\n"
        "repeat_rules = { track = { tracks = 100 } }\n"
        "[model]\n"
        'name = "OpenMuQ/MuQ-MuLan-large"\n'
        'manual_path = "C:/models/MuQ"\n',
        encoding="utf-8",
    )
    return cfg


def load_migrated_config(path: Path):
    shutil.rmtree(path.parent / "index", ignore_errors=True)
    text = path.read_text(encoding="utf-8")
    index_dir = (path.parent / "index").as_posix()
    if re.search(r"(?m)^index\s*=", text):
        pass
    elif re.search(r"(?m)^\[index\]\s*$", text) is None:
        text += f'\n[index]\nindex_dir = "{index_dir}"\n'
    elif re.search(r"(?m)^index_dir\s*=", text):
        text = re.sub(r"(?m)^index_dir\s*=.*$", f'index_dir = "{index_dir}"', text, count=1)
    else:
        text = re.sub(r"(?m)^\[index\]\s*$", f'[index]\nindex_dir = "{index_dir}"', text, count=1)
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path, environ={})
    if move_station_tables(cfg):
        cfg = load_config(path, environ={})
    return cfg


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_loads_minimal_config(self, minimal_toml: Path) -> None:
        cfg = load_config(minimal_toml)
        assert isinstance(cfg, AutoDJConfig)
        assert cfg.model.revision == "main"

    def test_raises_if_file_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.toml")

    def test_raises_if_not_toml(self, tmp_path: Path) -> None:
        bad = tmp_path / "config.toml"
        bad.write_text("this is not valid toml ][", encoding="utf-8")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(bad)

    def test_loads_full_config(self, full_toml: Path) -> None:
        cfg = load_migrated_config(full_toml)
        assert cfg.playback.crossfade_seconds == 5.0
        assert cfg.playback.repeat_rules.track.tracks == 100
        assert cfg.model.name == "OpenMuQ/MuQ-MuLan-large"
        assert cfg.model.manual_path == Path("C:/models/MuQ")

    def test_loads_ordinary_server_toml(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            "[server]\n"
            'host = "192.168.1.10"\n'
            "port = 9000\n"
            f'access_token = "{"s" * 32}"\n'
            'allowed_hosts = ["Radio.Local", "radio.local"]\n'
            'allowed_origins = ["HTTPS://Radio.Local:443/"]\n'
            "session_ttl_seconds = 3600\n"
            "liner_upload_max_mib = 25\n",
            encoding="utf-8",
        )

        cfg = load_migrated_config(cfg_path)

        assert cfg.server == ServerConfig(
            host="192.168.1.10",
            port=9000,
            access_token="s" * 32,
            allowed_hosts=["radio.local"],
            allowed_origins=["https://radio.local"],
            session_ttl_seconds=3600,
        )
        assert cfg.station.liner_upload_max_mib == 25


class TestDefaultAndEnvironmentConfig:
    @pytest.mark.parametrize(
        "section",
        [
            "library",
            "index",
            "playback",
            "model",
            "huggingface",
            "replaygain",
            "djmix",
            "transitions",
            "server",
        ],
    )
    def test_known_section_must_be_a_table(self, tmp_path: Path, section: str) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(f"{section} = []\n", encoding="utf-8")

        with pytest.raises(TypeError, match=rf"{section} section must be a table"):
            load_migrated_config(config_path)

    def test_missing_implicit_config_uses_validated_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None, environ={})
        assert cfg.library.music_dir is None
        assert cfg.index.index_dir == Path("index")
        assert cfg.index.model_dir == Path("models")
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 8080
        assert cfg.config_path is None
        assert cfg.config_sources == ("defaults",)

    def test_explicit_missing_config_remains_strict(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config(tmp_path / "missing.toml", environ={})

    def test_precedence_defaults_toml_local_environment(self, tmp_path: Path) -> None:
        base = tmp_path / "config.toml"
        base.write_text(
            '[library]\nmusic_dir = "toml-music"\n'
            '[index]\nindex_dir = "toml-index"\nmodel_dir = "toml-models"\n'
            '[server]\nhost = "127.0.0.2"\nport = 8081\n'
            'access_token = "test-token-0123456789abcdef0123456789"\n',
            encoding="utf-8",
        )
        (tmp_path / "config.local.toml").write_text(
            '[library]\nmusic_dir = "local-music"\n[server]\nport = 8082\n',
            encoding="utf-8",
        )
        cfg = load_config(
            base,
            environ={
                "AUTODJ_LIBRARY_MUSIC_DIR": "env-music",
                "AUTODJ_INDEX_DIR": "env-index",
                "AUTODJ_MODEL_DIR": "env-models",
                "AUTODJ_HOST": "127.0.0.3",
                "AUTODJ_PORT": "8083",
                "AUTODJ_ACCESS_TOKEN": "env-token-0123456789abcdef0123456789",
                "AUTODJ_HUGGINGFACE_TOKEN": "hf-token-0123456789abcdef0123456789",
            },
        )
        assert cfg.library.music_dir == Path("env-music")
        assert cfg.index.index_dir == Path("env-index")
        assert cfg.index.model_dir == Path("env-models")
        assert (cfg.server.host, cfg.server.port) == ("127.0.0.3", 8083)
        assert cfg.server.access_token == "env-token-0123456789abcdef0123456789"
        assert cfg.huggingface.token == "hf-token-0123456789abcdef0123456789"
        assert cfg.config_sources == (
            "defaults",
            str(base),
            str(tmp_path / "config.local.toml"),
            "environment",
        )

    @pytest.mark.parametrize("value", ["", "eight", "0", "65536"])
    def test_invalid_environment_port_uses_server_validator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match=r"AUTODJ_PORT|port"):
            load_config(None, environ={"AUTODJ_PORT": value})

    def test_environment_contract_is_complete(self) -> None:
        assert set(ENVIRONMENT_OVERLAY) == {
            "AUTODJ_LIBRARY_MUSIC_DIR",
            "AUTODJ_INDEX_DIR",
            "AUTODJ_MODEL_DIR",
            "AUTODJ_HOST",
            "AUTODJ_PORT",
            "AUTODJ_ACCESS_TOKEN",
            "AUTODJ_LAN",
            "AUTODJ_HUGGINGFACE_TOKEN",
        }

    def test_station_environment_is_slug_scoped_and_old_names_are_migration_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        old = load_config(
            None,
            environ={"AUTODJ_STREAM_BITRATE": "256", "AUTODJ_STREAM_ENABLED": "on"},
        )
        assert old.default_station is not None
        assert old.default_station.cfg.stream.bitrate == 320
        assert old.default_station.cfg.station.output == "browser"
        assert move_station_tables(old)
        migrated = load_config(None, environ={})
        assert migrated.default_station is not None
        assert migrated.default_station.cfg.stream.bitrate == 256
        assert migrated.default_station.cfg.station.output == "stream"
        from autodj.stations import StationPaths

        definition = StationPaths.default(migrated).definition
        document = json.loads(definition.read_text("utf-8"))
        document["station"]["stream"]["bitrate"] = 128
        document["station"]["station"]["output"] = "server"
        definition.write_text(json.dumps(document), encoding="utf-8")
        later = load_config(
            None,
            environ={"AUTODJ_STREAM_BITRATE": "256", "AUTODJ_STREAM_ENABLED": "on"},
        )
        assert move_station_tables(later) is False
        later = load_config(None, environ={})
        assert later.default_station is not None
        assert later.default_station.cfg.stream.bitrate == 128
        assert later.default_station.cfg.station.output == "server"

        scoped = load_config(
            None,
            environ={"AUTODJ_STATION_DEFAULT_STREAM_BITRATE": "192"},
        )
        assert scoped.default_station is not None
        assert scoped.default_station.cfg.stream.bitrate == 192


# ---------------------------------------------------------------------------
# LibraryConfig
# ---------------------------------------------------------------------------


class TestLibraryConfig:
    def test_music_dir_not_required(self) -> None:
        assert LibraryConfig.from_dict({}).music_dir is None

    def test_default_formats(self) -> None:
        lib = LibraryConfig.from_dict({"music_dir": "Z:/Music"})
        assert "mp3" in lib.supported_formats
        assert "flac" in lib.supported_formats
        assert "m4a" in lib.supported_formats

    def test_custom_formats(self) -> None:
        lib = LibraryConfig.from_dict({"music_dir": "Z:/Music", "supported_formats": ["flac"]})
        assert lib.supported_formats == ["flac"]

    def test_beets_db_optional(self) -> None:
        lib = LibraryConfig.from_dict({"music_dir": "Z:/Music"})
        assert lib.beets_db is None

    def test_beets_db_as_path(self) -> None:
        lib = LibraryConfig.from_dict({"music_dir": "Z:/Music", "beets_db": "C:/beets/library.db"})
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
        idx = IndexConfig.from_dict({"index_dir": "Z:/autodj-index", "model_dir": "Z:/models"})
        assert idx.index_dir == Path("Z:/autodj-index")

    def test_the_library_shared_files_and_stations_have_their_own_folders(self, caplog) -> None:
        idx = IndexConfig.from_dict({"index_dir": "srv/index", "name": "workout"})
        assert idx.library_dir == Path("srv/index/libraries/main")
        assert idx.shared_dir == Path("srv/index/shared")
        assert idx.stations_dir == Path("srv/index/stations")
        # Named indexes are gone: the old key is only warned about.
        assert "name" in caplog.text


# ---------------------------------------------------------------------------
# PlaybackConfig
# ---------------------------------------------------------------------------


class TestPlaybackConfig:
    def test_hour_fit_defaults_bounds_and_unknown_key(self, caplog) -> None:
        defaults = PlaybackConfig.from_dict({})
        assert defaults.hour_fit is False
        assert defaults.hour_fit_window_minutes == 10
        fitted = PlaybackConfig.from_dict({"hour_fit": True, "hour_fit_window_minutes": 30})
        assert (fitted.hour_fit, fitted.hour_fit_window_minutes) == (True, 30)
        for value in (0, 31, True, 1.5):
            with pytest.raises(ValueError, match="hour_fit_window_minutes"):
                PlaybackConfig.from_dict({"hour_fit_window_minutes": value})
        with pytest.raises(ValueError, match="hour_fit must"):
            PlaybackConfig.from_dict({"hour_fit": "true"})
        PlaybackConfig.from_dict({"hour_fit_old": True})
        assert "hour_fit_old" in caplog.text

    def test_defaults(self) -> None:
        pb = PlaybackConfig.from_dict({})
        assert pb.crossfade_seconds == 3.0
        assert pb.music_speed_percent == 0.0
        for name, tracks in (("track", 50), ("artist", 8), ("album", 0), ("title", 0)):
            rule = getattr(pb.repeat_rules, name)
            assert (rule.tracks, rule.minutes) == (tracks, 0)
        assert pb.show_lyrics is True
        assert pb.prefetch_next_track is True
        assert pb.silence_trigger_crossfade is True

    @pytest.mark.parametrize("value", [-10, -1.23, 0, 1.23, 10])
    def test_music_speed_percent_accepts_range_and_hundredths(self, value: float) -> None:
        assert PlaybackConfig.from_dict({"music_speed_percent": value}).music_speed_percent == value

    @pytest.mark.parametrize("value", [-10.01, 10.01, 1.234, float("inf"), float("nan"), True])
    def test_music_speed_percent_rejects_invalid_values(self, value: float) -> None:
        with pytest.raises((TypeError, ValueError)):
            PlaybackConfig.from_dict({"music_speed_percent": value})

    @pytest.mark.parametrize("value", [0, -3])
    def test_discovery_every_below_one_is_refused(self, value: int) -> None:
        """0 made every pick raise ZeroDivisionError once Discovery was on."""
        with pytest.raises(ValueError, match="discovery_every must be 1 or more"):
            PlaybackConfig.from_dict({"discovery_every": value})
        assert PlaybackConfig.from_dict({"discovery_every": 1}).discovery_every == 1
        assert PlaybackConfig.from_dict({}).discovery_every is None

    def test_show_lyrics_false_round_trip(self) -> None:
        pb = PlaybackConfig.from_dict({"show_lyrics": False})
        assert pb.show_lyrics is False

    def test_prefetch_next_track_false(self) -> None:
        pb = PlaybackConfig.from_dict({"prefetch_next_track": False})
        assert pb.prefetch_next_track is False

    def test_silence_trigger_crossfade_false(self) -> None:
        pb = PlaybackConfig.from_dict({"silence_trigger_crossfade": False})
        assert pb.silence_trigger_crossfade is False

    def test_crossfade_zero_allowed(self) -> None:
        pb = PlaybackConfig.from_dict({"crossfade_seconds": 0.0})
        assert pb.crossfade_seconds == 0.0

    def test_crossfade_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            PlaybackConfig.from_dict({"crossfade_seconds": -1.0})

    @pytest.mark.parametrize("name", ["crossfade_seconds", "fade_in_seconds"])
    def test_fade_lengths_stop_at_the_settings_page_maximum(self, name: str) -> None:
        # The Settings page and the web routes allow 0 to 20 seconds.
        assert getattr(PlaybackConfig.from_dict({name: 20}), name) == 20.0
        with pytest.raises(ValueError, match="between 0 and 20"):
            PlaybackConfig.from_dict({name: 20.5})

    def test_fade_in_seconds_default(self) -> None:
        pb = PlaybackConfig.from_dict({})
        assert pb.fade_in_seconds == 3.0

    def test_fade_in_seconds_zero_allowed(self) -> None:
        pb = PlaybackConfig.from_dict({"fade_in_seconds": 0.0})
        assert pb.fade_in_seconds == 0.0

    def test_fade_in_seconds_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            PlaybackConfig.from_dict({"fade_in_seconds": -1.0})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("liners_every_n_songs", -1),
            ("liners_every_minutes", -1.0),
            ("liners_random_min_minutes", -1.0),
            ("liners_random_max_minutes", -1.0),
            ("pick_temperature", -0.1),
            ("pick_top_k", 0),
        ],
    )
    def test_out_of_range_values_are_refused_not_adjusted(self, key: str, value: float) -> None:
        with pytest.raises(ValueError, match=key):
            PlaybackConfig.from_dict({key: value})

    def test_matched_intro_without_liner_setting_defaults_off(self) -> None:
        assert PlaybackConfig.from_dict({}).matched_intro_only_without_liner is False
        assert (
            PlaybackConfig.from_dict(
                {"matched_intro_only_without_liner": True}
            ).matched_intro_only_without_liner
            is True
        )

    def test_the_old_track_length_limit_is_ignored(self, caplog) -> None:
        """Tracks of any length play now; an old config only gets a warning."""
        pb = PlaybackConfig.from_dict({"server_max_track_minutes": 15.0})
        assert not hasattr(pb, "server_max_track_minutes")
        assert "server_max_track_minutes" in caplog.text

    @pytest.mark.parametrize(
        "key",
        [
            "no_repeat_window",
            "no_repeat_minutes",
            "artist_repeat_window",
            "artist_repeat_minutes",
            "title_repeat_window",
            "title_repeat_minutes",
            "related_artists_file",
            "related_artists_starter",
        ],
    )
    def test_removed_repeat_key_is_ignored_with_warning(
        self, tmp_path: Path, key: str, caplog
    ) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(f"[playback]\n{key} = 0\n", encoding="utf-8")
        cfg = load_migrated_config(config_file)
        assert cfg.playback.repeat_rules.artist.tracks == 8
        assert key in caplog.text

    def test_key_notation_default(self) -> None:
        pb = PlaybackConfig.from_dict({})
        assert pb.key_notation == "camelot"
        assert pb.key_prefer_flats is False

    def test_key_notation_musical(self) -> None:
        pb = PlaybackConfig.from_dict({"key_notation": "musical"})
        assert pb.key_notation == "musical"

    def test_key_notation_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            PlaybackConfig.from_dict({"key_notation": "bogus"})

    def test_key_prefer_flats_round_trip(self) -> None:
        pb = PlaybackConfig.from_dict({"key_prefer_flats": True})
        assert pb.key_prefer_flats is True


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------


class TestModelConfig:
    def test_default_model_name(self) -> None:
        mc = ModelConfig.from_dict({})
        assert mc.name == "OpenMuQ/MuQ-large-msd-iter"

    def test_manual_path_none_by_default(self) -> None:
        mc = ModelConfig.from_dict({})
        assert mc.manual_path is None

    def test_manual_path_as_path(self) -> None:
        mc = ModelConfig.from_dict({"manual_path": "C:/models/MuQ"})
        assert mc.manual_path == Path("C:/models/MuQ")

    def test_custom_model_name(self) -> None:
        mc = ModelConfig.from_dict({"name": "OpenMuQ/MuQ-MuLan-large"})
        assert mc.name == "OpenMuQ/MuQ-MuLan-large"


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

    def test_repr_redacts_token(self) -> None:
        token = "hf-secret-token"
        assert token not in repr(HuggingFaceConfig(token=token))


# ---------------------------------------------------------------------------
# AutoDJConfig integration
# ---------------------------------------------------------------------------


class TestDeepMergeAndOverlays:
    def test_deep_merge_result_does_not_alias_either_input(self) -> None:
        base = {"nested": {"retained": [1]}}
        overlay = {
            "nested": {"added": [2]},
            "replacement": {"value": [3]},
        }

        merged = _deep_merge(base, overlay)
        merged["nested"]["retained"].append(4)
        merged["nested"]["added"].append(5)
        merged["replacement"]["value"].append(6)

        assert base == {"nested": {"retained": [1]}}
        assert overlay == {
            "nested": {"added": [2]},
            "replacement": {"value": [3]},
        }

    def test_local_overlay_merges_recursively(self, tmp_path: Path) -> None:
        base = tmp_path / "config.toml"
        base.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            '[index]\nindex_dir = "index"\n'
            "[playback]\ncrossfade_seconds = 3.0\n",
            encoding="utf-8",
        )
        local = tmp_path / "config.local.toml"
        local.write_text(
            '[library]\nmusic_dir = "/srv/music"\n[playback]\ncrossfade_seconds = 5.0\n',
            encoding="utf-8",
        )
        cfg = load_migrated_config(base)
        assert str(cfg.library.music_dir).replace("\\", "/").endswith("/srv/music")
        assert cfg.playback.crossfade_seconds == 5.0

    @pytest.mark.parametrize(
        "overlay",
        [
            "[library]\npath_remap = {}\n",
            "[playback]\ncrossfade = 3.0\n",
            "[lyrics]\nenabled = true\n",
        ],
    )
    def test_local_overlay_unknown_keys_are_ignored(
        self, tmp_path: Path, overlay: str, caplog
    ) -> None:
        base = tmp_path / "config.toml"
        base.write_text('[library]\nmusic_dir = "Z:/Music"\n', encoding="utf-8")
        (tmp_path / "config.local.toml").write_text(overlay, encoding="utf-8")
        cfg = load_config(base)
        assert str(cfg.library.music_dir).replace("\\", "/").endswith("/Music")
        assert cfg.playback.crossfade_seconds == 3.0

    def test_unknown_nested_repeat_keys_are_ignored_but_known_values_validate(
        self, tmp_path: Path, caplog
    ) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[playback.repeat_rules]\nfuture = "secret"\n'
            '[playback.repeat_rules.artist]\ntracks = 12\nfuture = "secret"\n',
            encoding="utf-8",
        )
        cfg = load_migrated_config(config_file)
        assert cfg.playback.repeat_rules.artist.tracks == 12
        assert "future" in caplog.text
        assert "secret" not in caplog.text

        config_file.write_text(
            "[playback.repeat_rules.artist]\ntracks = -1\nfuture = true\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="tracks"):
            load_migrated_config(config_file)

    def test_non_table_presets_value_is_a_load_error(self, tmp_path: Path) -> None:
        base = tmp_path / "config.toml"
        base.write_text('presets = 5\n[library]\nmusic_dir = "Z:/Music"\n', encoding="utf-8")
        with pytest.raises(TypeError, match="presets section must be a table"):
            load_config(base)

    def test_config_toml_presets_loaded(self, tmp_path: Path) -> None:
        base = tmp_path / "config.toml"
        base.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n[index]\n[playback]\n'
            "[presets.evening]\nbpm_target = 100\nbpm_weight = 0.3\n",
            encoding="utf-8",
        )
        cfg = load_config(base)
        assert "evening" in cfg.presets

    def test_leftover_presets_toml_is_refused(self, tmp_path: Path) -> None:
        """Ignoring it would drop its presets without a word."""
        base = tmp_path / "config.toml"
        base.write_text('[library]\nmusic_dir = "Z:/Music"\n', encoding="utf-8")
        (tmp_path / "presets.toml").write_text("[chill]\nbpm_target = 75\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"presets\.toml is no longer read"):
            load_config(base)


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


class TestModelConfigRevision:
    def test_custom_revision_loads(self) -> None:
        assert ModelConfig.from_dict({"revision": "v1.2.3"}).revision == "v1.2.3"

    @pytest.mark.parametrize("value", [None, 1, True, ["main"]])
    def test_revision_must_be_string(self, value: object) -> None:
        with pytest.raises(ValueError, match=r"model\.revision"):
            ModelConfig.from_dict({"revision": value})

    @pytest.mark.parametrize("revision", [" main", "main ", "\tmain\n", "   "])
    def test_revision_rejects_whitespace_in_toml_and_direct_construction(
        self, revision: str
    ) -> None:
        with pytest.raises(ValueError, match=r"model\.revision"):
            ModelConfig.from_dict({"revision": revision})
        with pytest.raises(ValueError, match=r"model\.revision"):
            ModelConfig(revision=revision)


# ---------------------------------------------------------------------------
# DjMixConfig — harmonic_mode added with the harmonic combo box
# ---------------------------------------------------------------------------


class TestDjMixConfig:
    def test_default_harmonic_mode_off(self) -> None:
        from autodj.config import DjMixConfig

        d = DjMixConfig.from_dict({})
        assert d.harmonic_mode == "off"

    def test_explicit_harmonic_mode(self) -> None:
        from autodj.config import DjMixConfig

        d = DjMixConfig.from_dict({"harmonic_mode": "STRICT"})
        # Lowercased on load
        assert d.harmonic_mode == "strict"

    @pytest.mark.parametrize("value", [True, False, "same_key"])
    def test_invalid_harmonic_mode_is_rejected(self, value: object) -> None:
        """A Boolean left over from the old on/off setting must not load as a mode."""
        from autodj.config import DjMixConfig

        with pytest.raises(ValueError, match=r"djmix\.harmonic_mode"):
            DjMixConfig.from_dict({"harmonic_mode": value})

    def test_leftover_harmonic_mixing_key_is_ignored(self, caplog) -> None:
        from autodj.config import DjMixConfig

        config = DjMixConfig.from_dict({"harmonic_mixing": False, "harmonic_mode": "off"})
        assert config.harmonic_mode == "off"
        assert "harmonic_mixing" in caplog.text


def test_removed_weighted_liner_mode_is_rejected() -> None:
    """The removed Weighted rotation must stop startup, not load as a no-op."""
    from autodj.config import PlaybackConfig

    with pytest.raises(ValueError, match=r"playback\.liners_pick_mode"):
        PlaybackConfig.from_dict({"liners_pick_mode": "weighted"})


@pytest.mark.parametrize("value", [0.5, 60.0, -30.5, float("nan")])
def test_liner_duck_outside_minus_30_to_0_db_is_rejected(value: float) -> None:
    """A positive duck would multiply the music's volume under every liner."""
    from autodj.config import PlaybackConfig

    with pytest.raises(ValueError, match=r"playback\.liners_duck_db"):
        PlaybackConfig.from_dict({"liners_duck_db": value})


@pytest.mark.parametrize("value", [-30, 0, -12.5])
def test_liner_duck_range_ends_are_accepted(value: float) -> None:
    from autodj.config import PlaybackConfig

    assert PlaybackConfig.from_dict({"liners_duck_db": value}).liners_duck_db == value


def test_liner_duck_default_is_three_db() -> None:
    from autodj.config import PlaybackConfig

    assert PlaybackConfig.from_dict({}).liners_duck_db == -3.0


# ---------------------------------------------------------------------------
# PlaybackConfig.transition_mode (Mixxx-style crossfade alignment)
# ---------------------------------------------------------------------------


class TestPlaybackTransitionMode:
    def test_default_full_intro_outro(self) -> None:
        from autodj.config import PlaybackConfig

        p = PlaybackConfig.from_dict({})
        assert p.transition_mode == "full_intro_outro"

    def test_explicit_outro_fade(self) -> None:
        from autodj.config import PlaybackConfig

        p = PlaybackConfig.from_dict({"transition_mode": "outro_fade"})
        assert p.transition_mode == "outro_fade"

    def test_unknown_raises(self) -> None:
        import pytest

        from autodj.config import PlaybackConfig

        with pytest.raises(ValueError, match="transition_mode"):
            PlaybackConfig.from_dict({"transition_mode": "wat"})

    def test_all_documented_modes_load(self) -> None:
        from autodj.config import TRANSITION_MODES, PlaybackConfig

        for mode in TRANSITION_MODES:
            p = PlaybackConfig.from_dict({"transition_mode": mode})
            assert p.transition_mode == mode


def test_playback_pick_and_loudness_modes_validate_and_keep_legacy_default(caplog) -> None:
    import pytest

    from autodj.config import PlaybackConfig

    defaults = PlaybackConfig.from_dict({})
    assert defaults.pick_mode == "similarity"
    assert defaults.loudness_processing is None
    assert defaults.intro_lift_max_db == 4.0
    assert defaults.stereotool_input_trim_db == 0.0
    assert defaults.stereotool_agc_drive_db == 7.0
    assert defaults.stereotool_preset == "included"
    assert PlaybackConfig.from_dict({"quiet_boost_db": 11.0}).stereotool_agc_drive_db == 7.0
    from autodj.config import unknown_config_keys

    assert "quiet_boost_db" in caplog.text
    assert unknown_config_keys({"playback": {"quiet_boost_db": 11.0}}) == {}
    for value in (0, 12):
        assert (
            PlaybackConfig.from_dict({"stereotool_agc_drive_db": value}).stereotool_agc_drive_db
            == value
        )
    for value in (-0.1, 12.1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="stereotool_agc_drive_db"):
            PlaybackConfig.from_dict({"stereotool_agc_drive_db": value})
    for field, invalid in (
        ("intro_lift_max_db", 6.1),
        ("stereotool_input_trim_db", -12.1),
        ("stereotool_preset", "../escape.sts"),
    ):
        with pytest.raises(
            ValueError, match=field if field != "stereotool_preset" else "preset file name"
        ):
            PlaybackConfig.from_dict({field: invalid})
    for mode in ("similarity", "random", "least_recent", "clock"):
        assert PlaybackConfig.from_dict({"pick_mode": mode}).pick_mode == mode
    for mode in ("off", "replaygain", "smooth", "stereotool"):
        assert PlaybackConfig.from_dict({"loudness_processing": mode}).loudness_processing == mode
    with pytest.raises(ValueError, match="pick_mode"):
        PlaybackConfig.from_dict({"pick_mode": "unknown"})
    with pytest.raises(ValueError, match="loudness_processing"):
        PlaybackConfig.from_dict({"loudness_processing": "unknown"})


class TestServerConfig:
    def test_defaults_to_loopback_and_derives_policy(self) -> None:
        cfg = ServerConfig.from_dict({})

        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8080
        assert cfg.access_token is None
        assert cfg.insecure_lan is False
        assert cfg.trusted_proxies == []
        assert cfg.effective_allowed_hosts() == ["127.0.0.1"]
        assert cfg.effective_allowed_origins() == ["http://127.0.0.1:8080"]

    def test_output_and_monitoring_modes_validate(self) -> None:
        assert StationConfig.from_dict({}).output == "browser"
        assert StationConfig.from_dict({"output": "server"}).output == "server"
        assert StationConfig.from_dict(
            {"output": "stream", "server_audio_too": True}
        ).server_audio_too
        with pytest.raises(ValueError, match="server_audio_too requires"):
            StationConfig.from_dict({"output": "browser", "server_audio_too": True})
        with pytest.raises(ValueError, match=r"station\.output"):
            StationConfig.from_dict({"output": "invalid"})

    @pytest.mark.parametrize("limit", [0, 1025])
    def test_liner_upload_limit_is_station_local_and_bounded(self, limit: int) -> None:
        with pytest.raises(ValueError, match=r"station\.liner_upload_max_mib"):
            StationConfig.from_dict({"liner_upload_max_mib": limit})

    def test_custom_bind_and_default_port_drive_derived_policy(self) -> None:
        custom = ServerConfig.from_dict({"host": "127.0.0.2", "port": 9090})
        default_port = ServerConfig.from_dict({"host": "127.0.0.2", "port": 80})

        assert custom.effective_allowed_hosts() == ["127.0.0.2"]
        assert custom.effective_allowed_origins() == ["http://127.0.0.2:9090"]
        assert default_port.effective_allowed_origins() == ["http://127.0.0.2"]

    def test_canonicalizes_ipv6_origins_and_duplicates(self) -> None:
        cfg = ServerConfig.from_dict(
            {
                "allowed_hosts": ["RADIO.local.", "radio.local", "::1"],
                "allowed_origins": [
                    "HTTPS://Radio.Local:443/",
                    "https://radio.local",
                    "http://[2001:DB8::1]:80/",
                ],
            }
        )

        assert cfg.allowed_hosts == ["radio.local", "::1"]
        assert cfg.allowed_origins == [
            "https://radio.local",
            "http://[2001:db8::1]",
        ]

    @pytest.mark.parametrize(
        "origin",
        [
            "ftp://radio.local",
            "https://user:pass@radio.local",
            "https://radio.local/private",
            "https://radio.local?token=x",
            "https://radio.local#fragment",
            "https://radio.local?",
            "https://radio.local#",
            "https://radio.local?#",
            "https://radio.local/?",
            "https://radio.local/#",
            "https://radio.local:99999",
            "https://radio.local:",
            "https://radio.local:0",
            "https://[2001:db8::1",
            "https://[v1.foo]",
            "https://[not-ipv6]",
            "https://0.0.0.0",
            "https://*.example",
            "https://radio.local\x00",
        ],
    )
    def test_rejects_malformed_or_wildcard_origins(self, origin: str) -> None:
        with pytest.raises(ValueError, match="allowed_origins"):
            ServerConfig.from_dict({"allowed_origins": [origin]})

    @pytest.mark.parametrize(
        "host",
        [
            "",
            " ",
            "radio.local:8080",
            "user@radio.local",
            "[::1]",
            "radio/local",
            "*.example",
            "0.0.0.0",
            "::",
            "bad_host",
            "radio.local\x00",
        ],
    )
    def test_rejects_invalid_allowed_hosts(self, host: str) -> None:
        with pytest.raises(ValueError, match="allowed_hosts"):
            ServerConfig.from_dict({"allowed_hosts": [host]})

    @pytest.mark.parametrize(
        "host",
        [
            "fa\N{LATIN SMALL LETTER SHARP S}.de",
            "fa\N{ZERO WIDTH JOINER}ss.de",
            "b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.example",
        ],
    )
    def test_rejects_unicode_allowed_hosts_without_idna_aliasing(self, host: str) -> None:
        with pytest.raises(ValueError, match="ASCII"):
            ServerConfig.from_dict({"allowed_hosts": [host]})

    @pytest.mark.parametrize(
        "origin",
        [
            "https://fa\N{LATIN SMALL LETTER SHARP S}.de",
            "https://fa\N{ZERO WIDTH JOINER}ss.de",
        ],
    )
    def test_rejects_unicode_origin_hosts_without_idna_aliasing(self, origin: str) -> None:
        with pytest.raises(ValueError, match="ASCII"):
            ServerConfig.from_dict({"allowed_origins": [origin]})

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"port": True}, "server.port"),
            ({"port": "8080"}, "server.port"),
            ({"insecure_lan": 1}, "server.insecure_lan"),
            ({"allowed_hosts": "radio.local"}, "server.allowed_hosts"),
            ({"allowed_hosts": [1]}, "server.allowed_hosts"),
            ({"allowed_origins": "http://radio.local"}, "server.allowed_origins"),
            ({"session_ttl_seconds": 59}, "server.session_ttl_seconds"),
            ({"session_ttl_seconds": 31_536_001}, "server.session_ttl_seconds"),
        ],
    )
    def test_rejects_wrong_types_and_out_of_bounds_values(
        self, data: dict[str, object], message: str
    ) -> None:
        with pytest.raises((TypeError, ValueError), match=message):
            ServerConfig.from_dict(data)

    def test_unknown_upload_bytes_alias_is_ignored(self, caplog) -> None:
        cfg = ServerConfig.from_dict({"liner_upload_max_bytes": 1})
        assert not hasattr(cfg, "liner_upload_max_bytes")
        assert "liner_upload_max_bytes" in caplog.text

    def test_rejects_weak_token_without_echoing_it(self) -> None:
        weak = "do-not-print-me"

        with pytest.raises(ValueError) as raised:
            ServerConfig.from_dict({"access_token": weak})

        assert "at least 32 UTF-8 bytes" in str(raised.value)
        assert weak not in str(raised.value)

    def test_token_minimum_counts_utf8_bytes(self) -> None:
        cfg = ServerConfig.from_dict({"access_token": "\N{LOCK}" * 8})

        assert cfg.access_token == "\N{LOCK}" * 8

    def test_server_config_repr_redacts_access_token(self) -> None:
        token = "do-not-render-this-secret-value!"

        cfg = ServerConfig(access_token=token)

        assert token not in repr(cfg)

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.2", "::1"])
    def test_loopback_detection_never_needs_dns(self, host: str) -> None:
        assert is_loopback_bind(host)

    @pytest.mark.parametrize("host", ["radio.local", "0.0.0.0", "::", "bad host!", "0"])
    def test_non_loopback_detection(self, host: str) -> None:
        assert not is_loopback_bind(host)

    def test_zero_is_not_a_host_name(self) -> None:
        """Many resolvers read "0" as 0.0.0.0, so it must not pass as a name."""
        with pytest.raises(ValueError, match="invalid host"):
            ServerConfig(allowed_hosts=["0"])

    @pytest.mark.parametrize(
        ("kwargs", "error", "message"),
        [
            ({"access_token": 12345}, TypeError, "access_token must be a string"),
            ({"liner_upload_max_bytes": 1024}, TypeError, "unexpected keyword"),
        ],
    )
    def test_programmatic_values_are_validated(self, kwargs, error, message) -> None:
        with pytest.raises(error, match=message):
            ServerConfig(**kwargs)

    def test_server_section_must_be_a_table(self) -> None:
        with pytest.raises(TypeError, match="server section must be a table"):
            ServerConfig.from_dict(["127.0.0.1"])  # type: ignore[arg-type]

    def test_empty_explicit_host_list_is_refused_even_on_loopback(self) -> None:
        cfg = ServerConfig(allowed_hosts=[])
        with pytest.raises(ValueError, match="explicit allowed_hosts and allowed_origins"):
            validate_server_exposure(cfg)

    def test_wildcard_bind_derives_no_default_origin(self) -> None:
        cfg = ServerConfig(host="0.0.0.0", access_token="s" * 32, allowed_hosts=["radio.local"])
        assert cfg.effective_allowed_origins() == []

    def test_wildcard_requires_explicit_nonempty_policy_lists(self) -> None:
        cfg = ServerConfig(
            host="0.0.0.0",
            access_token="s" * 32,
            allowed_hosts=[],
            allowed_origins=[],
        )

        with pytest.raises(ValueError, match="wildcard binding requires"):
            validate_server_exposure(cfg)

    def test_lan_requires_token_or_insecure_acknowledgement(self) -> None:
        cfg = ServerConfig(
            host="192.168.1.10",
            allowed_hosts=["radio.local"],
            allowed_origins=["http://radio.local:8080"],
        )

        with pytest.raises(ValueError, match="LAN binding requires"):
            validate_server_exposure(cfg)

    def test_specific_lan_bind_derives_policy_when_authenticated(self) -> None:
        cfg = ServerConfig(host="192.168.1.10", access_token="s" * 32)

        validate_server_exposure(cfg)

        assert cfg.effective_allowed_hosts() == ["192.168.1.10"]
        assert cfg.effective_allowed_origins() == ["http://192.168.1.10:8080"]

    def test_specific_named_lan_bind_derives_policy_with_insecure_ack(self) -> None:
        cfg = ServerConfig(host="Radio.Local", insecure_lan=True)

        validate_server_exposure(cfg)

        assert cfg.effective_allowed_hosts() == ["radio.local"]
        assert cfg.effective_allowed_origins() == ["http://radio.local:8080"]

    def test_validation_rechecks_mutated_direct_configuration(self) -> None:
        cfg = ServerConfig()
        cfg.host = "0.0.0.0"
        cfg.access_token = "weak-secret"
        cfg.allowed_hosts = ["radio.local"]
        cfg.allowed_origins = ["http://radio.local:8080"]

        with pytest.raises(ValueError) as raised:
            validate_server_exposure(cfg)

        assert "weak-secret" not in str(raised.value)

    def test_origin_helper_rejects_non_string_without_value_echo(self) -> None:
        with pytest.raises(TypeError, match="allowed_origins"):
            canonicalize_allowed_origin(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Skip style, liner talk-ups and the vocal guard
# ---------------------------------------------------------------------------


class TestMixFeatureSettings:
    def test_defaults(self) -> None:
        playback = PlaybackConfig()
        assert playback.skip_style == "fade"
        assert playback.liners_talk_up is False
        assert DjMixConfig().vocal_guard is True

    @pytest.mark.parametrize("style", ["fade", "echo_out", "backspin", "loop_roll"])
    def test_every_skip_style_loads(self, style: str) -> None:
        assert PlaybackConfig.from_dict({"skip_style": style}).skip_style == style

    def test_an_unknown_skip_style_is_refused(self) -> None:
        with pytest.raises(ValueError, match="skip_style"):
            PlaybackConfig.from_dict({"skip_style": "scratch"})

    def test_switches_are_coerced_to_booleans(self) -> None:
        assert PlaybackConfig.from_dict({"liners_talk_up": 1}).liners_talk_up is True
        assert DjMixConfig.from_dict({"vocal_guard": 0}).vocal_guard is False
