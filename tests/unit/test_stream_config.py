"""[stream] configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodj.config import StreamConfig, load_config, parse_env_bool


def test_defaults() -> None:
    cfg = StreamConfig()
    assert cfg.max_listeners_per_address == 0
    assert "listener_password" not in repr(cfg)
    assert (
        cfg.enabled,
        cfg.continuous,
        cfg.bitrate,
        cfg.idle_grace_seconds,
        cfg.max_listeners,
        cfg.station_name,
    ) == (
        False,
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
        {"trusted_proxies": ["not-an-address"]},
        {"listener_password": "secret"},
        {"listener_user": "radio"},
        {"listener_user": "ra:dio", "listener_password": "secret"},
        {"station_name": ""},
        {"enabled": "yes"},
        {"continuous": "true"},
        {"unknown": 1},
    ],
)
def test_invalid_values_rejected(data: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        StreamConfig.from_dict(data)


def test_toml_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[stream]\nenabled = true\nbitrate = 192\n", encoding="utf-8")
    cfg = load_config(
        path, environ={"AUTODJ_STREAM_BITRATE": "256", "AUTODJ_STREAM_ENABLED": "off"}
    )
    assert cfg.stream.bitrate == 256
    assert cfg.stream.enabled is False


def test_private_listener_credentials_and_proxy_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[stream]\nmax_listeners_per_address = 0\ntrusted_proxies = ["192.0.2.0/24"]\n',
        encoding="utf-8",
    )
    (tmp_path / "config.local.toml").write_text(
        '[stream]\nlistener_user = "radio"\nlistener_password = "secret"\n', encoding="utf-8"
    )
    stream = load_config(path).stream
    assert stream.max_listeners_per_address == 0
    assert stream.trusted_proxies == ["192.0.2.0/24"]
    assert (stream.listener_user, stream.listener_password) == ("radio", "secret")


def test_continuous_config_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[stream]\ncontinuous = true\n", encoding="utf-8")
    cfg = load_config(path, environ={"AUTODJ_STREAM_CONTINUOUS": "off"})
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
