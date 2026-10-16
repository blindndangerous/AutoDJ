"""[stream] configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodj.config import StreamConfig, load_config, move_station_tables, parse_env_bool


def test_defaults() -> None:
    cfg = StreamConfig()
    assert cfg.max_listeners_per_address == 0
    assert "listener_password" not in repr(cfg)
    assert (
        cfg.continuous,
        cfg.bitrate,
        cfg.idle_grace_seconds,
        cfg.max_listeners,
        cfg.station_name,
    ) == (
        False,
        320,
        30.0,
        8,
        "AutoDJ",
    )


@pytest.mark.parametrize("bitrate", [128, 192, 256, 320])
def test_valid_bitrates(bitrate: int) -> None:
    assert StreamConfig.from_dict({"bitrate": bitrate}).bitrate == bitrate


@pytest.mark.parametrize(
    "data",
    [
        {"bitrate": 64},
        {"bitrate": "320"},
        {"idle_grace_seconds": -1},
        {"idle_grace_seconds": "thirty"},
        {"max_listeners": 0},
        {"max_listeners": "eight"},
        {"max_listeners_per_address": -1},
        {"max_listeners_per_address": True},
        {"listener_password": "secret"},
        {"listener_user": "radio"},
        {"listener_user": "ra:dio", "listener_password": "secret"},
        {"station_name": ""},
        {"continuous": "true"},
    ],
)
def test_invalid_values_rejected(data: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        StreamConfig.from_dict(data)


def test_toml_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        "[stream]\nenabled = true\nbitrate = 192\n",
        encoding="utf-8",
    )
    env = {"AUTODJ_STREAM_BITRATE": "256", "AUTODJ_STREAM_ENABLED": "off"}
    cfg = load_config(path, environ=env)
    assert cfg.stream.bitrate == 320  # legacy root settings are migration input, never fallback
    assert cfg.station.output == "browser"
    assert move_station_tables(cfg)
    migrated = load_config(path, environ={})
    assert migrated.stream.bitrate == 256
    assert migrated.station.output == "browser"


def test_private_listener_credentials_and_proxy_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        '[stream]\nmax_listeners_per_address = 0\ntrusted_proxies = ["192.0.2.0/24"]\n',
        encoding="utf-8",
    )
    (tmp_path / "config.local.toml").write_text(
        '[stream]\nlistener_user = "radio"\nlistener_password = "secret"\n', encoding="utf-8"
    )
    cfg = load_config(path, environ={})
    assert move_station_tables(cfg)
    cfg = load_config(path, environ={})
    stream = cfg.stream
    assert stream.max_listeners_per_address == 0
    assert cfg.server.trusted_proxies == ["192.0.2.0/24"]
    assert (stream.listener_user, stream.listener_password) == ("radio", "secret")


def test_continuous_config_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[stream]\ncontinuous = true\n", encoding="utf-8")
    path.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n[stream]\ncontinuous = true\n',
        encoding="utf-8",
    )
    cfg = load_config(path, environ={"AUTODJ_STATION_DEFAULT_STREAM_CONTINUOUS": "off"})
    assert cfg.stream.continuous is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("TRUE", True), ("on", True), ("no", False), ("0", False)],
)
def test_parse_env_bool(raw: str, expected: bool) -> None:
    assert parse_env_bool(raw) is expected


def test_parse_env_bool_rejects_other() -> None:
    with pytest.raises(ValueError):
        parse_env_bool("maybe")


@pytest.mark.parametrize("name", ["Radio\r\nX-Evil: 1", "Radio\x00", "Tab\there", "Bell\x07"])
def test_station_name_rejects_control_characters(name: str) -> None:
    with pytest.raises(ValueError, match="station_name"):
        StreamConfig(station_name=name)


def test_station_name_keeps_printable_unicode() -> None:
    assert StreamConfig(station_name="Радио 🎵 Café").station_name == "Радио 🎵 Café"


def test_unknown_stream_keys_warn_without_discarding_supported_settings(caplog):
    cfg = StreamConfig.from_dict({"unknown": 1, "bitrate": 192})
    assert cfg.bitrate == 192
    assert "Ignoring unknown config keys in [stream]: ['unknown']" in caplog.text


def test_extra_formats_are_off_and_statistics_kept_90_days_by_default() -> None:
    cfg = StreamConfig()
    assert (cfg.aac_bitrate, cfg.opus_bitrate, cfg.stats_days) == (0, 0, 90)


@pytest.mark.parametrize(
    ("key", "value"),
    [("aac_bitrate", 64), ("aac_bitrate", 256), ("opus_bitrate", 48), ("opus_bitrate", 192)],
)
def test_extra_format_bitrates(key: str, value: int) -> None:
    assert getattr(StreamConfig.from_dict({key: value}), key) == value


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"aac_bitrate": 320}, ValueError),
        ({"opus_bitrate": 256}, ValueError),
        ({"aac_bitrate": "96"}, TypeError),
        ({"opus_bitrate": True}, TypeError),
        ({"stats_days": -1}, ValueError),
        ({"stats_days": 3651}, ValueError),
        ({"stats_days": 1.5}, TypeError),
        ({"stats_days": False}, TypeError),
    ],
)
def test_bad_extra_format_and_statistics_settings(data: dict, error: type[Exception]) -> None:
    with pytest.raises(error):
        StreamConfig.from_dict(data)
