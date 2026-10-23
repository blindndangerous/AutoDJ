"""[stream.relays.<name>] configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodj.config import (
    RelayConfig,
    StreamConfig,
    load_config,
    move_station_tables,
    relay_password_variable,
)
from autodj.relay_settings import load_relays
from autodj.stations import StationPaths

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


@pytest.mark.parametrize("name", ["", " has space", "x" * 33, "dot.name"])
def test_bad_relay_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="names may use only"):
        StreamConfig.from_dict({"relays": {name: ICECAST}})


def test_relay_list_is_rejected_with_a_hint() -> None:
    with pytest.raises(TypeError, match=r"\[stream\.relays\.main\]"):
        StreamConfig.from_dict({"relays": [ICECAST]})


def test_too_many_relays() -> None:
    with pytest.raises(ValueError, match="at most 8"):
        StreamConfig.from_dict({"relays": {f"r{i}": ICECAST for i in range(9)}})


def test_legacy_relay_secrets_and_environment_are_migrated(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_bytes(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'.encode()
        + b'[stream.relays.main]\ntype = "icecast"\nhost = "radio.example.com"\nmount = "/live"\n'
        b'[stream.relays.backup-sc]\ntype = "shoutcast1"\nhost = "sc.example.com"\n'
    )
    (tmp_path / "config.local.toml").write_bytes(
        b'[stream.relays.main]\npassword = "from-local"\n'
        b'[stream.relays.backup-sc]\npassword = "local-sc"\n'
    )
    path = tmp_path / "config.toml"
    env = {"AUTODJ_STREAM_RELAY_BACKUP_SC_PASSWORD": "from-env"}
    cfg = load_config(path, environ=env)
    assert move_station_tables(cfg)
    cfg = load_config(path, environ={})
    relays = load_relays(StationPaths.default(cfg).relays, {}, "default")
    assert relays["main"].password == "from-local"
    assert relays["main"].host == "radio.example.com"
    assert relays["backup-sc"].password == "from-env"
    # A legacy variable is captured for one-time migration, never a runtime override.
    reloaded = load_config(path, environ={"AUTODJ_STREAM_RELAY_BACKUP_SC_PASSWORD": "later"})
    assert move_station_tables(reloaded) is False
    assert (
        load_relays(StationPaths.default(reloaded).relays, {}, "default")["backup-sc"].password
        == "from-env"
    )


def test_environment_password_alone_does_not_create_a_relay(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n[stream]\nenabled = false\n',
        encoding="utf-8",
    )
    env = {relay_password_variable("ghost"): "pw"}
    assert (
        load_config(tmp_path / "config.toml", environ=env).default_station.cfg.stream.relays == {}
    )


@pytest.mark.parametrize(("first", "second"), [("a-b", "a_b"), ("Main", "main")])
def test_relays_reading_one_set_password_variable_are_refused(
    tmp_path: Path, first: str, second: str
) -> None:
    """One server's source password must never be sent to another."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        f"""
[stream.relays.{first}]
type = "icecast"
host = "one.example.net"
mount = "/live"
password = "placeholder-one"

[stream.relays.{second}]
type = "icecast"
host = "two.example.org"
mount = "/live"
password = "placeholder-two"
""",
        encoding="utf-8",
    )
    variable = "AUTODJ_STREAM_RELAY_" + first.upper().replace("-", "_") + "_PASSWORD"
    with pytest.raises(ValueError, match=f"would both read {variable}, which is set"):
        move_station_tables(load_config(path, environ={variable: "secret-for-one"}))
    # Unset, the variable sends nothing: each relay keeps its own TOML password.
    cfg = load_config(path, environ={})
    move_station_tables(cfg)
    cfg = load_config(path, environ={})
    relays = load_relays(StationPaths.default(cfg).relays, {}, "default")
    assert relays[first].password == "placeholder-one"
    assert relays[second].password == "placeholder-two"


def test_password_variable_name() -> None:
    assert relay_password_variable("backup-sc") == "AUTODJ_STATION_DEFAULT_RELAY_BACKUP_SC_PASSWORD"
    assert relay_password_variable("Stream madness") == (
        "AUTODJ_STATION_DEFAULT_RELAY_NAME_SHA256_"
        "16DDB67590CA8DD1B551666647C75FEDC901ECD7A718195DCF3D6D96984604D1_PASSWORD"
    )


def test_relay_name_accepts_internal_spaces_but_not_path_or_edge_spaces() -> None:
    assert RelayConfig.from_table("Stream madness", ICECAST).name == "Stream madness"
    for name in (" Stream", "Stream ", "stream/relay", "   "):
        with pytest.raises(ValueError, match="names may use"):
            RelayConfig.from_table(name, ICECAST)


def test_a_host_with_a_port_points_at_the_port_key() -> None:
    with pytest.raises(ValueError, match=r"must not include a port.*stream\.relays\.main\.port"):
        RelayConfig.from_table("main", {**ICECAST, "host": "radio.example.com:8000"})


@pytest.mark.parametrize("host", ["::1", "fe80::1", "2001:db8::5"])
def test_an_ipv6_address_is_a_valid_host(host: str) -> None:
    assert RelayConfig.from_table("main", {**ICECAST, "host": host}).host == host


@pytest.mark.parametrize("key", ["format", "name"])
def test_unknown_relay_keys_warn_and_preserve_valid_settings(key, caplog):
    relay = StreamConfig.from_dict({"relays": {"main": {**ICECAST, key: "private-value"}}}).relays[
        "main"
    ]
    assert relay.name == "main"
    assert relay.host == ICECAST["host"]
    assert relay.password == ICECAST["password"]
    assert "Ignoring unknown config keys in [stream.relays.main]" in caplog.text
    assert key in caplog.text
    assert "private-value" not in caplog.text
