"""[stations.<slug>] configuration: a second station beside the default one (#70)."""

from __future__ import annotations

import json
import logging
import shutil
import tomllib
from pathlib import Path

import pytest

from autodj.config import (
    MAX_STATIONS,
    load_config,
    move_station_tables,
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
    shutil.rmtree(tmp_path / "index", ignore_errors=True)
    if "[index]" not in text:
        text += f'\n[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
    config = tmp_path / "config.toml"
    config.write_text(text, encoding="utf-8")
    if local is not None:
        (tmp_path / "config.local.toml").write_text(local, encoding="utf-8")
    return config


def _load(tmp_path: Path, text: str, local: str | None = None, environ=None):
    path = _write(tmp_path, text, local)
    cfg = load_config(path, environ=environ)
    if move_station_tables(cfg):
        cfg = load_config(path, environ=environ)
    return cfg


def test_a_single_station_config_has_no_other_stations(tmp_path: Path) -> None:
    cfg = _load(tmp_path, BASE)
    assert cfg.stations == ()
    assert cfg.stream.station_name == "AutoDJ"


def test_loader_does_not_read_legacy_station_tables_without_startup_migration(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, BASE + NIGHTOWL)
    cfg = load_config(path, environ={})
    assert cfg.stations == ()
    assert cfg.default_station is not None
    assert cfg.default_station.cfg.playback.crossfade_seconds == 3.0
    assert cfg.playback.crossfade_seconds == 3.0


@pytest.mark.parametrize("relative_index", [None, "index", "index # station"])
def test_default_station_migration_uses_definition_schema_and_saved_timezone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_index: str | None,
) -> None:
    text = BASE.replace("[stream]\n", '[stream]\nenabled = true\ntrusted_proxies = ["10.0.0.1"]\n')
    text += '\n[server]\nstation_timezone = "UTC"\nliner_upload_max_mib = 20\n'
    if relative_index is not None:
        monkeypatch.chdir(tmp_path)
        text += f'\n[index]\nindex_dir = "{relative_index}"\n'
    path = _write(tmp_path, text)
    cfg = load_config(path, environ={})
    catalog_path = cfg.index.library_dir / "library.db"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(catalog_path)) as db:
        db.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute(
            "INSERT INTO catalog_settings VALUES ('station_timezone', 'America/Los_Angeles')"
        )
        db.commit()
    assert move_station_tables(cfg)
    migrated = load_config(path, environ={})
    assert migrated.default_station is not None
    assert migrated.default_station.defined_in == "web"
    station = migrated.default_station.cfg.station
    assert (station.output, station.timezone, station.liner_upload_max_mib) == (
        "stream",
        "America/Los_Angeles",
        20,
    )
    assert migrated.server.trusted_proxies == ["10.0.0.1"]
    from autodj.stations import StationPaths

    document = json.loads(StationPaths.default(migrated).definition.read_text("utf-8"))
    assert document["station"]["station"] == {
        "output": "stream",
        "timezone": "America/Los_Angeles",
        "liner_upload_max_mib": 20,
    }
    assert "enabled" not in document["station"]["stream"]


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
    from autodj.stations import StationPaths

    relays = json.loads(StationPaths.default(cfg).relays.read_text("utf-8"))["relays"]
    assert [item["name"] for item in relays] == ["main"]


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
    from autodj.relay_settings import load_relays
    from autodj.stations import StationPaths

    relays = load_relays(StationPaths.for_station(cfg, "nightowl").relays, {}, "nightowl")
    assert relays["owl"].password == "owl-secret"
    assert [hook.url for hook in station.webhooks] == ["https://hooks.example.net/nightowl"]
    # The default station keeps its own credentials and webhooks.
    assert cfg.stream.listener_password == ""
    assert cfg.webhooks == ()


def test_a_station_named_only_in_the_local_file_is_refused(tmp_path: Path) -> None:
    # A typo there would start a public station of its own and leave the
    # real one without the password meant for it.
    local = '[stations.nigthowl.stream]\nlistener_user = "owl"\nlistener_password = "hoot"\n'
    with pytest.raises(ValueError, match=r"\[stations.nigthowl\] is in config.local.toml"):
        _load(tmp_path, BASE + NIGHTOWL, local)
    with pytest.raises(ValueError, match=r"\[stations.nigthowl\]"):
        _load(tmp_path, BASE, local)


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
    cfg = _load(tmp_path, BASE + "\n[stations.default.stream]\nbitrate = 128\n")
    assert cfg.default_station is not None
    assert cfg.default_station.cfg.stream.bitrate == 128


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


def test_a_log_label_is_saved_in_the_station_definition(tmp_path: Path) -> None:
    text = BASE + NIGHTOWL + '\n[stations.nightowl.station]\nlog_label = "nor"\n'
    cfg = _load(tmp_path, text)
    (owl,) = cfg.stations
    assert owl.cfg.station.log_label == "nor"
    assert cfg.station.log_label == ""
    definition = tmp_path / "index" / "stations" / "nightowl" / "station.json"
    document = json.loads(definition.read_text(encoding="utf-8"))
    assert document["station"]["station"]["log_label"] == "nor"
    reloaded = load_config(cfg.config_path, environ={})
    assert reloaded.stations[0].cfg.station.log_label == "nor"


@pytest.mark.parametrize(
    ("label", "reason"),
    [("nor", "already used by station nightowl"), ("NightOwl", "ID of station nightowl")],
)
def test_log_labels_must_tell_stations_apart(tmp_path: Path, label: str, reason: str) -> None:
    text = (
        BASE
        + NIGHTOWL
        + '\n[stations.nightowl.station]\nlog_label = "nor"\n'
        + '\n[stations.daybreak.recording]\nfolder = "recordings/daybreak"\n'
        + f'\n[stations.daybreak.station]\nlog_label = "{label}"\n'
    )
    with pytest.raises(ValueError, match=reason):
        _load(tmp_path, text)


def test_a_hand_edited_duplicate_log_label_skips_that_station(tmp_path: Path) -> None:
    text = (
        BASE
        + NIGHTOWL
        + '\n[stations.nightowl.station]\nlog_label = "nor"\n'
        + '\n[stations.daybreak.recording]\nfolder = "recordings/daybreak"\n'
    )
    cfg = _load(tmp_path, text)
    assert [spec.slug for spec in cfg.stations] == ["nightowl", "daybreak"]
    definition = tmp_path / "index" / "stations" / "daybreak" / "station.json"
    document = json.loads(definition.read_text(encoding="utf-8"))
    document["station"].setdefault("station", {})["log_label"] = "nor"
    definition.write_text(json.dumps(document), encoding="utf-8")
    reloaded = load_config(cfg.config_path, environ={})
    assert [spec.slug for spec in reloaded.stations] == ["nightowl"]
    ((path, reason),) = reloaded.skipped_definitions
    assert path == str(definition)
    assert "already used by station nightowl" in reason


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
    cfg = _load(tmp_path, other_mount)
    from autodj.stations import StationPaths

    relays = json.loads(StationPaths.for_station(cfg, "nightowl").relays.read_text("utf-8"))[
        "relays"
    ]
    assert [item["name"] for item in relays] == ["copy"]


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


def test_server_wide_stream_keys_migrate_to_shared_server_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = BASE.replace("[stream]\n", '[stream]\ntrusted_proxies = ["10.0.0.1"]\n') + (
        NIGHTOWL.replace(
            "bitrate = 192", 'bitrate = 192\nenabled = false\ntrusted_proxies = ["192.0.2.9"]'
        )
    )
    with caplog.at_level(logging.WARNING, logger="autodj.config"):
        cfg = _load(tmp_path, text)
    assert cfg.server.trusted_proxies == ["10.0.0.1"]
    assert cfg.stations[0].cfg.station.output == "stream"


def test_station_relay_passwords_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setenv(variable, "from-env")
    cfg = _load(tmp_path, text)
    from autodj.relay_settings import load_relays
    from autodj.stations import StationPaths

    relays = load_relays(StationPaths.for_station(cfg, "nightowl").relays, {}, "nightowl")
    assert relays["back-up"].password == "from-env"
    # Only relays named in the TOML are looked up, and only for their station.
    ghost = station_relay_password_variable("nightowl", "ghost")
    cfg = _load(tmp_path, BASE + NIGHTOWL)
    monkeypatch.setenv(ghost, "pw")
    from autodj.relay_settings import load_relays
    from autodj.stations import StationPaths

    assert load_relays(StationPaths.for_station(cfg, "nightowl").relays, {}, "nightowl") == {}


def test_two_relays_sharing_one_set_variable_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        BASE
        + NIGHTOWL
        + '\n[stations.a-relay-b.recording]\nfolder = "r1"\n'
        + '[stations.a-relay-b.stream.relays.c]\ntype = "shoutcast1"\nhost = "h1"\n'
        + '\n[stations.a.recording]\nfolder = "r2"\n'
        + '[stations.a.stream.relays.b-relay-c]\ntype = "shoutcast1"\nhost = "h2"\n'
    )
    variable = "AUTODJ_STATION_A_RELAY_B_RELAY_C_PASSWORD"
    monkeypatch.setenv(variable, "from-env")
    with pytest.raises(ValueError, match=f"would both read {variable}, which is set"):
        _load(tmp_path, text)
    # Unset, the variable sends nothing: each relay keeps its own TOML password.
    monkeypatch.delenv(variable)
    text = text.replace('host = "h1"\n', 'host = "h1"\npassword = "p1"\n')
    text = text.replace('host = "h2"\n', 'host = "h2"\npassword = "p2"\n')
    cfg = _load(tmp_path, text)
    from autodj.relay_settings import load_relays
    from autodj.stations import StationPaths

    passwords = {
        slug: [
            relay.password
            for relay in load_relays(StationPaths.for_station(cfg, slug).relays, {}, slug).values()
        ]
        for slug in ("a-relay-b", "a")
    }
    assert passwords["a-relay-b"] == ["p1"] and passwords["a"] == ["p2"]


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


def test_the_configuration_examples_load(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "config.toml"
    config.write_text((root / "config.toml.example").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config.local.toml").write_text(
        (root / "config.local.toml.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    cfg = load_config(config, environ={})
    assert cfg.server.host == "127.0.0.1"
    assert cfg.index.index_dir == Path("/srv/autodj/index")
    assert cfg.default_station is not None
    assert cfg.default_station.cfg.station.output == "browser"


def test_a_station_without_a_name_is_called_by_its_slug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    unnamed = BASE + NIGHTOWL.replace('station_name = "Night Owl Radio"\n', "")
    (spec,) = _load(tmp_path, unnamed).stations
    # Not "AutoDJ", the default station's name.
    assert spec.name == spec.cfg.stream.station_name == "nightowl"
    named = _load(tmp_path, BASE + NIGHTOWL).stations[0]
    assert named.name == "Night Owl Radio"

    same = BASE + NIGHTOWL.replace('"Night Owl Radio"', '"autodj "')
    with caplog.at_level(logging.WARNING, logger="autodj.config"):
        assert _load(tmp_path, same).stations[0].name == "autodj "
    assert any(
        "Stations default and nightowl are both called" in record.getMessage()
        for record in caplog.records
    )


def test_migration_drops_the_old_track_length_limit(tmp_path: Path, caplog) -> None:
    """#391: tracks of any length play; a saved limit is removed, not warned about forever."""
    from autodj.stations import StationPaths

    text = BASE.replace(
        "crossfade_seconds = 7\n", "crossfade_seconds = 7\nserver_max_track_minutes = 15.0\n"
    )
    cfg = _load(tmp_path, text, environ={})
    definition = StationPaths.default(cfg).definition
    document = json.loads(definition.read_text("utf-8"))
    assert document["station"]["playback"] == {"crossfade_seconds": 7}
    # A station file saved with the limit is cleaned the next time too.
    document["station"]["playback"]["server_max_track_minutes"] = 30
    definition.write_text(json.dumps(document), encoding="utf-8")
    assert move_station_tables(load_config(tmp_path / "config.toml", environ={}))
    caplog.clear()
    reloaded = load_config(tmp_path / "config.toml", environ={})
    assert (
        "server_max_track_minutes"
        not in json.loads(definition.read_text("utf-8"))["station"]["playback"]
    )
    assert reloaded.playback.crossfade_seconds == 7
    assert "server_max_track_minutes" not in caplog.text
