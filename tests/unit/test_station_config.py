"""[stations.<slug>] configuration: a second station beside the default one (#70)."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest

from autodj.config import (
    MAX_STATIONS,
    load_config,
    station_relay_password_variable,
    unknown_config_keys,
)
from autodj.doctor_repair import plan_unknown_key_repairs

BASE = """
[library]
music_dir = "music"

[playback]
crossfade_seconds = 7

[stream]
station_name = "AutoDJ"
max_listeners = 30

[stream.relays.main]
type = "icecast"
host = "radio.example.net"
mount = "/live"
password = "default-secret"
"""

NIGHTOWL = """
[stations.nightowl]
autostart = true

[stations.nightowl.stream]
station_name = "Night Owl Radio"
bitrate = 192
continuous = true
max_listeners = 20

[stations.nightowl.recording]
folder = "recordings/nightowl"
"""


def _write(tmp_path: Path, text: str, local: str | None = None) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(text, encoding="utf-8")
    if local is not None:
        (tmp_path / "config.local.toml").write_text(local, encoding="utf-8")
    return config


def _load(tmp_path: Path, text: str, local: str | None = None, environ=None):
    return load_config(_write(tmp_path, text, local), environ=environ or {})


def test_a_single_station_config_has_no_other_stations(tmp_path: Path) -> None:
    cfg = _load(tmp_path, BASE)
    assert cfg.stations == ()
    assert cfg.stream.station_name == "AutoDJ"


def test_a_station_starts_from_the_defaults_not_the_root_sections(tmp_path: Path) -> None:
    cfg = _load(tmp_path, BASE + NIGHTOWL)
    (spec,) = cfg.stations
    assert (spec.slug, spec.autostart, spec.name) == ("nightowl", True, "Night Owl Radio")
    station = spec.cfg
    assert (station.stream.bitrate, station.stream.max_listeners) == (192, 20)
    assert station.stream.continuous is True
    # Not inherited: the default station's crossfade, relays and webhooks.
    assert station.playback.crossfade_seconds == 3.0
    assert station.stream.relays == {}
    assert station.webhooks == ()
    assert station.recording.folder == Path("recordings/nightowl")
    # Unchanged for the default station.
    assert cfg.playback.crossfade_seconds == 7.0
    assert set(cfg.stream.relays) == {"main"}


def test_station_sections_are_separate_objects_and_shared_ones_are_the_roots(
    tmp_path: Path,
) -> None:
    cfg = _load(tmp_path, BASE + NIGHTOWL)
    station = cfg.stations[0].cfg
    for name in ("library", "index", "model", "huggingface", "server"):
        assert getattr(station, name) is getattr(cfg, name), name
    for name in ("playback", "djmix", "transitions", "replaygain", "stream", "recording"):
        assert getattr(station, name) is not getattr(cfg, name), name
    station.playback.crossfade_seconds = 1.0
    station.stream.station_name = "Changed"
    assert cfg.playback.crossfade_seconds == 7.0
    assert cfg.stream.station_name == "AutoDJ"
    assert station.stations == ()


def test_autostart_defaults_to_on_and_must_be_a_boolean(tmp_path: Path) -> None:
    text = BASE + NIGHTOWL.replace("autostart = true\n", "")
    assert _load(tmp_path, text).stations[0].autostart is True
    off = BASE + NIGHTOWL.replace("autostart = true", "autostart = false")
    assert _load(tmp_path, off).stations[0].autostart is False
    with pytest.raises(TypeError, match=r"\[stations.nightowl\] autostart"):
        _load(tmp_path, BASE + NIGHTOWL.replace("autostart = true", 'autostart = "yes"'))


def test_secrets_merge_from_the_local_file(tmp_path: Path) -> None:
    text = (
        BASE
        + NIGHTOWL
        + """
[stations.nightowl.stream.relays.owl]
type = "icecast"
host = "radio.example.net"
mount = "/nightowl"
"""
    )
    local = """
[stations.nightowl.stream]
listener_user = "owl"
listener_password = "hoot hoot"

[stations.nightowl.stream.relays.owl]
password = "owl-secret"

[[stations.nightowl.webhooks]]
url = "https://hooks.example.net/nightowl"
"""
    cfg = _load(tmp_path, text, local)
    station = cfg.stations[0].cfg
    assert (station.stream.listener_user, station.stream.listener_password) == (
        "owl",
        "hoot hoot",
    )
    assert station.stream.relays["owl"].password == "owl-secret"
    assert [hook.url for hook in station.webhooks] == ["https://hooks.example.net/nightowl"]
    # The default station keeps its own credentials and webhooks.
    assert cfg.stream.listener_password == ""
    assert cfg.webhooks == ()


def test_config_toml_refuses_station_webhooks(tmp_path: Path) -> None:
    text = BASE + NIGHTOWL + '\n[[stations.nightowl.webhooks]]\nurl = "https://example.net/x"\n'
    with pytest.raises(ValueError, match=r"\[stations.nightowl\] webhooks belong only"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    "slug", ["Night", "-owl", "owl-", "night_owl", "a" * 33, '"night.owl"', '"../x"', '""']
)
def test_invalid_station_names_are_refused(tmp_path: Path, slug: str) -> None:
    text = BASE + f'\n[stations.{slug}.recording]\nfolder = "elsewhere"\n'
    with pytest.raises(ValueError, match="station name uses lower-case letters"):
        _load(tmp_path, text)


def test_the_default_name_is_reserved(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\[stations.default\] is reserved"):
        _load(tmp_path, BASE + "\n[stations.default.stream]\nbitrate = 128\n")


def test_stations_must_be_tables(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="named tables"):
        _load(tmp_path, "stations = 1\n" + BASE)
    with pytest.raises(TypeError, match=r"\[stations.nightowl\] must be a table"):
        _load(tmp_path, BASE + "\n[stations]\nnightowl = 3\n")


def test_at_most_eight_other_stations(tmp_path: Path) -> None:
    def tables(count: int) -> str:
        return "".join(
            f'\n[stations.s{n}.recording]\nfolder = "recordings/s{n}"\n' for n in range(count)
        )

    assert len(_load(tmp_path, BASE + tables(MAX_STATIONS)).stations) == MAX_STATIONS
    with pytest.raises(ValueError, match="at most 8"):
        _load(tmp_path, BASE + tables(MAX_STATIONS + 1))


def test_validation_errors_name_the_station(tmp_path: Path) -> None:
    bad = BASE + NIGHTOWL + "\n[stations.nightowl.playback]\ncrossfade_seconds = 99\n"
    with pytest.raises(ValueError, match=r"\[stations.nightowl\] playback.crossfade_seconds"):
        _load(tmp_path, bad)
    relay = BASE + NIGHTOWL + '\n[stations.nightowl.stream.relays.x]\ntype = "icecast"\n'
    with pytest.raises(KeyError, match=r"\[stations.nightowl\] config \[stream.relays.x\]"):
        _load(tmp_path, relay)
    repeat = BASE + NIGHTOWL + "\n[stations.nightowl.playback.repeat_rules.track]\ntracks = -1\n"
    with pytest.raises(ValueError, match=r"\[stations.nightowl\]"):
        _load(tmp_path, repeat)


def test_recording_folders_must_differ(tmp_path: Path) -> None:
    # Unset, both would record into the default "recordings" folder.
    with pytest.raises(ValueError, match="default and nightowl use the same recording folder"):
        _load(tmp_path, BASE + "\n[stations.nightowl]\n")
    two = (
        BASE
        + NIGHTOWL
        + '\n[stations.daybreak.recording]\nfolder = "recordings/../recordings/nightowl"\n'
    )
    with pytest.raises(ValueError, match="nightowl and daybreak use the same recording folder"):
        _load(tmp_path, two)


def test_relay_targets_must_differ_across_stations(tmp_path: Path) -> None:
    text = (
        BASE
        + NIGHTOWL
        + """
[stations.nightowl.stream.relays.copy]
type = "icecast"
host = "RADIO.example.net"
mount = "/live"
password = "pw"
"""
    )
    with pytest.raises(ValueError, match="default and nightowl both relay to"):
        _load(tmp_path, text)
    other_mount = text.replace(
        'mount = "/live"\npassword = "pw"', 'mount = "/owl"\npassword = "pw"'
    )
    assert set(_load(tmp_path, other_mount).stations[0].cfg.stream.relays) == {"copy"}


def test_unknown_station_keys_are_warned_about_and_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = (
        BASE
        + NIGHTOWL.replace("autostart = true", 'autostart = true\ncolour = "blue"')
        + """
[stations.nightowl.server]
port = 1

[stations.nightowl.playback]
quiet_boost_db = 3
"""
    )
    with caplog.at_level(logging.WARNING, logger="autodj.config"):
        cfg = _load(tmp_path, text)
    assert "[stations.nightowl]: ['colour', 'server']" in caplog.text
    assert "[stations.nightowl.playback]: ['quiet_boost_db']" in caplog.text
    assert cfg.server.port == 8080
    raw = tomllib.loads(text)
    assert unknown_config_keys(raw) == {
        "stations.nightowl": ("colour",),
        "stations.nightowl.playback": ("quiet_boost_db",),
    }


def test_server_wide_stream_keys_in_a_station_are_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = BASE.replace("[stream]\n", '[stream]\ntrusted_proxies = ["10.0.0.1"]\n') + (
        NIGHTOWL.replace(
            "bitrate = 192", 'bitrate = 192\nenabled = false\ntrusted_proxies = ["192.0.2.9"]'
        )
    )
    with caplog.at_level(logging.WARNING, logger="autodj.config"):
        cfg = _load(tmp_path, text)
    stream = cfg.stations[0].cfg.stream
    assert stream.enabled is True
    assert stream.trusted_proxies == ["10.0.0.1"]
    assert "always streams" in caplog.text
    assert "applies server-wide" in caplog.text


def test_station_relay_passwords_come_from_the_environment(tmp_path: Path) -> None:
    text = (
        BASE
        + NIGHTOWL
        + """
[stations.nightowl.stream.relays.back-up]
type = "shoutcast1"
host = "10.0.0.5"
"""
    )
    variable = station_relay_password_variable("nightowl", "back-up")
    assert variable == "AUTODJ_STATION_NIGHTOWL_RELAY_BACK_UP_PASSWORD"
    cfg = _load(tmp_path, text, environ={variable: "from-env"})
    assert cfg.stations[0].cfg.stream.relays["back-up"].password == "from-env"
    assert "environment" in cfg.config_sources
    # Only relays named in the TOML are looked up, and only for their station.
    ghost = station_relay_password_variable("nightowl", "ghost")
    cfg = _load(tmp_path, BASE + NIGHTOWL, environ={ghost: "pw"})
    assert cfg.stations[0].cfg.stream.relays == {}


def test_two_relays_sharing_one_variable_are_refused(tmp_path: Path) -> None:
    text = (
        BASE
        + NIGHTOWL
        + '\n[stations.a-relay-b.recording]\nfolder = "r1"\n'
        + '[stations.a-relay-b.stream.relays.c]\ntype = "shoutcast1"\nhost = "h1"\n'
        + '\n[stations.a.recording]\nfolder = "r2"\n'
        + '[stations.a.stream.relays.b-relay-c]\ntype = "shoutcast1"\nhost = "h2"\n'
    )
    with pytest.raises(ValueError, match="would both read AUTODJ_STATION_A_RELAY_B_RELAY_C"):
        _load(tmp_path, text)


def test_doctor_repair_removes_an_unknown_station_key(tmp_path: Path) -> None:
    config = _write(
        tmp_path,
        BASE
        + NIGHTOWL
        + "\n[stations.nightowl.playback]\nquiet_boost_db = 3\nfade_in_seconds = 2\n",
    )
    (repair,) = plan_unknown_key_repairs(str(config))
    assert repair.keys_by_section == {"stations.nightowl.playback": ("quiet_boost_db",)}
    assert b"quiet_boost_db" not in repair.replacement
    assert b"fade_in_seconds = 2" in repair.replacement


def test_the_commented_examples_load(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]

    def uncomment(filename: str, start: str) -> str:
        text = (root / filename).read_text(encoding="utf-8")
        block = text[text.index(start) :]
        return "\n".join(line.removeprefix("#").removeprefix(" ") for line in block.splitlines())

    shared = uncomment("config.toml.example", "# [stations.nightowl]\n")
    local = uncomment("config.local.toml.example", "# [stations.nightowl.stream]\n")
    shared += '\n[stations.nightowl.stream.relays.main]\ntype = "shoutcast1"\nhost = "10.0.0.5"\n'
    cfg = _load(tmp_path, BASE + shared, local)
    station = cfg.stations[0].cfg
    assert cfg.stations[0].name == "Night Owl Radio"
    assert station.playback.loudness_processing == "smooth"
    assert station.stream.listener_user == "owl"
    assert station.stream.relays["main"].password == "that station's source password"
    assert len(station.webhooks) == 1
