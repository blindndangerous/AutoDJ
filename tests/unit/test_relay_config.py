"""[stream.relays.<name>] configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodj.config import RelayConfig, StreamConfig, load_config, relay_password_variable

ICECAST = {"type": "icecast", "host": "radio.example.com", "mount": "/live", "password": "pw"}


def test_icecast_defaults() -> None:
    relay = StreamConfig.from_dict({"relays": {"main": ICECAST}}).relays["main"]
    assert (relay.name, relay.port, relay.user, relay.bitrate, relay.public, relay.enabled) == (
        "main",
        8000,
        "source",
        None,
        False,
        True,
    )
    assert relay.stream_id is None


def test_shoutcast2_defaults_to_stream_one() -> None:
    data = {"type": "shoutcast2", "host": "10.0.0.5", "port": 8010, "password": "pw"}
    assert RelayConfig.from_table("sc", data).stream_id == 1


def test_password_is_not_in_repr() -> None:
    relay = RelayConfig.from_table("main", {**ICECAST, "password": "hunter2-secret"})
    assert "hunter2-secret" not in repr(relay)
    assert "hunter2-secret" not in repr(StreamConfig(relays={"main": relay}))


@pytest.mark.parametrize(
    "data",
    [
        {**ICECAST, "type": "rtmp"},
        {**ICECAST, "host": "http://radio.example.com"},
        {**ICECAST, "host": "radio.example.com/live"},
        {**ICECAST, "host": "radio.example.com:8000"},
        {**ICECAST, "host": "10.0.0.5:8000"},
        {**ICECAST, "host": "[::1]"},
        {**ICECAST, "host": ""},
        {**ICECAST, "password": ""},
        {**ICECAST, "password": "bad\r\npass"},
        {**ICECAST, "port": 0},
        {**ICECAST, "port": "8000"},
        {**ICECAST, "port": True},
        {**ICECAST, "mount": "live"},
        {**ICECAST, "mount": "/live stream"},
        {**ICECAST, "user": "a:b"},
        {**ICECAST, "stream_id": 2},
        {**ICECAST, "bitrate": 64},
        {**ICECAST, "bitrate": True},
        {**ICECAST, "public": "yes"},
        {**ICECAST, "enabled": 1},
        {**ICECAST, "format": "mp3"},
        {**ICECAST, "name": "other"},
        {"type": "icecast", "host": "h", "password": "pw"},
        {"type": "shoutcast1", "host": "h", "password": "pw", "mount": "/live"},
        {"type": "shoutcast1", "host": "h", "password": "pw", "user": "source"},
        {"type": "shoutcast1", "host": "h", "password": "pw", "stream_id": 1},
        {"type": "shoutcast1", "host": "h", "password": "pw", "port": 65535},
        {"type": "shoutcast2", "host": "h", "password": "pw", "stream_id": 0},
        {"type": "shoutcast2", "host": "h", "password": "pw", "stream_id": "1"},
        {"type": "icecast", "host": "h", "mount": "/live"},
        "not a table",
    ],
)
def test_invalid_relays_are_rejected(data: object) -> None:
    with pytest.raises((ValueError, TypeError, KeyError)):
        StreamConfig.from_dict({"relays": {"main": data}})


@pytest.mark.parametrize("name", ["", "has space", "x" * 33, "dot.name"])
def test_bad_relay_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="names may use only"):
        StreamConfig.from_dict({"relays": {name: ICECAST}})


def test_relay_list_is_rejected_with_a_hint() -> None:
    with pytest.raises(TypeError, match=r"\[stream\.relays\.main\]"):
        StreamConfig.from_dict({"relays": [ICECAST]})


def test_too_many_relays() -> None:
    with pytest.raises(ValueError, match="at most 8"):
        StreamConfig.from_dict({"relays": {f"r{i}": ICECAST for i in range(9)}})


def test_password_from_config_local_toml_and_environment(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_bytes(
        b'[stream.relays.main]\ntype = "icecast"\nhost = "radio.example.com"\nmount = "/live"\n'
        b'[stream.relays.backup-sc]\ntype = "shoutcast1"\nhost = "sc.example.com"\n'
    )
    (tmp_path / "config.local.toml").write_bytes(
        b'[stream.relays.main]\npassword = "from-local"\n'
        b'[stream.relays.backup-sc]\npassword = "local-sc"\n'
    )
    cfg = load_config(tmp_path / "config.toml", environ={})
    assert cfg.stream.relays["main"].password == "from-local"
    assert cfg.stream.relays["main"].host == "radio.example.com"

    env = {relay_password_variable("backup-sc"): "from-env"}
    cfg = load_config(tmp_path / "config.toml", environ=env)
    assert cfg.stream.relays["backup-sc"].password == "from-env"
    assert cfg.stream.relays["main"].password == "from-local"
    assert "environment" in cfg.config_sources


def test_environment_password_alone_does_not_create_a_relay(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_bytes(b"[stream]\nenabled = false\n")
    env = {relay_password_variable("ghost"): "pw"}
    assert load_config(tmp_path / "config.toml", environ=env).stream.relays == {}


def test_password_variable_name() -> None:
    assert relay_password_variable("backup-sc") == "AUTODJ_STREAM_RELAY_BACKUP_SC_PASSWORD"


def test_a_host_with_a_port_points_at_the_port_key() -> None:
    with pytest.raises(ValueError, match=r"must not include a port.*stream\.relays\.main\.port"):
        RelayConfig.from_table("main", {**ICECAST, "host": "radio.example.com:8000"})


@pytest.mark.parametrize("host", ["::1", "fe80::1", "2001:db8::5"])
def test_an_ipv6_address_is_a_valid_host(host: str) -> None:
    assert RelayConfig.from_table("main", {**ICECAST, "host": host}).host == host
