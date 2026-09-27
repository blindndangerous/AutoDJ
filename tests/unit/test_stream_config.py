"""[stream] configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodj.config import StreamConfig, load_config, parse_env_bool


def test_defaults() -> None:
    cfg = StreamConfig()
    assert (
        cfg.enabled,
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
        {"station_name": ""},
        {"enabled": "yes"},
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
