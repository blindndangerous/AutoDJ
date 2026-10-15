"""Web station definitions and selected duplication state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodj.config import _build_station, load_config
from autodj.doctor import CheckStatus, run_doctor
from autodj.station_creation import CreationConflict, duplicate_table, stage_station
from autodj.stations import StationPaths


def _root(tmp_path: Path, extra: str = ""):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        '[stream]\nstation_name = "Main"\n'
        '[recording]\nfolder = "recordings"\n' + extra,
        encoding="utf-8",
    )
    return load_config(path, environ={})


def _definition(cfg, slug: str, table: dict) -> Path:
    path = StationPaths.for_station(cfg, slug).definition
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"version": 1, "created": "2026-01-01T00:00:00Z", "source": None, "station": table}
        ),
        encoding="utf-8",
    )
    return path


def test_name_flag_loads_the_selected_index_web_stations(tmp_path: Path) -> None:
    """``--name other`` reads web stations from index "other", not the configured one."""
    from autodj.cli import _apply_index_name

    cfg = _root(tmp_path, '[stations.fixed.recording]\nfolder = "rec-fixed"\n')
    _definition(cfg, "morning", {"autostart": False, "recording": {"folder": "rec-morning"}})
    cfg.index.name = "other"
    _definition(cfg, "owl", {"autostart": False, "recording": {"folder": "rec-owl"}})
    cfg = load_config(cfg.config_path, environ={})
    assert [spec.slug for spec in cfg.stations] == ["fixed", "morning"]

    assert _apply_index_name(cfg, "other") is True
    assert [spec.slug for spec in cfg.stations] == ["fixed", "owl"]
    assert StationPaths.for_station(cfg, "owl").definition.is_file()


def test_new_station_uses_defaults_and_loads_after_restart(tmp_path: Path) -> None:
    cfg = _root(tmp_path)
    table = duplicate_table(cfg, None, "new", "New station", False)
    _, folder, copied = stage_station(
        cfg, "new", None, table, copy_clockwheels=False, copy_schedule=False, copy_carts=False
    )
    assert copied == []
    assert [item.name for item in folder.iterdir()] == ["station.json"]
    document = json.loads((folder / "station.json").read_text(encoding="utf-8"))
    assert document["station"] == table
    reloaded = load_config(cfg.config_path, environ={})
    (station,) = reloaded.stations
    assert station.defined_in == "web"
    assert station.autostart is False
    assert (
        station.cfg.playback.crossfade_seconds
        == _build_station(
            "other", {"stream": {"station_name": "Other"}}, cfg
        ).cfg.playback.crossfade_seconds
    )
    assert station.cfg.recording.folder == Path("recordings/new")


def test_duplicate_copies_only_selected_station_assets(tmp_path: Path) -> None:
    extra = '\n[stations.owl]\nautostart = true\n[stations.owl.stream]\nstation_name = "Owl"\nlistener_user = "secret"\nlistener_password = "secret"\n[stations.owl.recording]\nfolder = "recordings/owl"\n'
    cfg = _root(tmp_path, extra)
    old = StationPaths.for_station(cfg, "owl")
    old.state_dir.mkdir(parents=True)
    old.web_state.write_text(
        json.dumps({"playback": {"stream_station_name": "Owl", "crossfade_seconds": 9}}),
        encoding="utf-8",
    )
    old.time_checks.write_text(
        '{"version": 1, "settings": {"enabled": true, "intro": "hi.wav", "outro": "",'
        ' "recorded_folder": "Clips"}, "pack_dates": {"old": {}}}',
        encoding="utf-8",
    )
    old.schedule.write_text(
        '{"schema_version": 1, "events": [], "runs": [{"old": true}]}', encoding="utf-8"
    )
    old.carts.write_text('{"slots": []}', encoding="utf-8")
    old.cart_uploads.mkdir()
    (old.cart_uploads / "id.wav").write_bytes(b"cart")
    old.clockwheels.mkdir()
    (old.clockwheels / "mix.json").write_text("{}", encoding="utf-8")
    old.relays.write_text('{"password": "secret"}', encoding="utf-8")
    table = duplicate_table(cfg, "owl", "new", "New", False)
    assert "listener_user" not in table["stream"]
    assert Path(table["recording"]["folder"]) == Path("recordings/new")
    _, folder, copied = stage_station(
        cfg, "new", "owl", table, copy_clockwheels=True, copy_schedule=True, copy_carts=True
    )
    assert set(copied) == {"saved settings", "schedule", "carts", "clockwheels"}
    assert json.loads((folder / "web_state.json").read_text())["playback"] == {
        "stream_station_name": "New",
        "crossfade_seconds": 9,
    }
    assert json.loads((folder / "schedule.json").read_text())["runs"] == []
    assert json.loads((folder / "time_checks.json").read_text()) == {
        "version": 1,
        "settings": {"enabled": True, "outro": ""},
    }
    assert not (folder / "relays.json").exists()
    (folder / "carts" / "id.wav").write_bytes(b"changed")
    assert (old.cart_uploads / "id.wav").read_bytes() == b"cart"


def test_duplicate_refuses_bad_saved_settings_and_links(tmp_path: Path) -> None:
    cfg = _root(tmp_path)
    old = StationPaths.default(cfg)
    old.state_dir.mkdir(parents=True)
    old.web_state.write_text("broken", encoding="utf-8")
    table = duplicate_table(cfg, "default", "new", "New", False)
    with pytest.raises(CreationConflict, match="saved settings"):
        stage_station(
            cfg,
            "new",
            "default",
            table,
            copy_clockwheels=False,
            copy_schedule=False,
            copy_carts=False,
        )
    assert not StationPaths.for_station(cfg, "new").state_dir.exists()


def test_duplicate_without_flags_skips_assets(tmp_path: Path) -> None:
    cfg = _root(tmp_path)
    old = StationPaths.default(cfg)
    old.state_dir.mkdir(parents=True)
    old.schedule.write_text('{"schema_version": 1, "events": [], "runs": []}', encoding="utf-8")
    old.clockwheels.mkdir()
    (old.clockwheels / "mix.json").write_text("{}", encoding="utf-8")
    old.carts.write_text("{}", encoding="utf-8")
    table = duplicate_table(cfg, "default", "new", "New", False)
    _, folder, copied = stage_station(
        cfg, "new", "default", table, copy_clockwheels=False, copy_schedule=False, copy_carts=False
    )
    assert copied == []
    assert {item.name for item in folder.iterdir()} == {"station.json"}


def test_station_json_wins_and_bad_definitions_are_reported(tmp_path: Path) -> None:
    cfg = _root(
        tmp_path,
        '\n[stations.owl]\n[stations.owl.stream]\nstation_name = "Owl"\n[stations.owl.recording]\nfolder = "recordings/owl"\n',
    )
    _definition(
        cfg, "owl", {"stream": {"station_name": "Ignored"}, "recording": {"folder": "recordings/o"}}
    )
    bad = _definition(
        cfg, "bad", {"stream": {"station_name": "Bad", "listener_password": "secret"}}
    )
    reloaded = load_config(cfg.config_path, environ={})
    assert [item.slug for item in reloaded.stations] == ["owl"]
    # A station with its own station.json is read from it, not config.toml.
    assert reloaded.stations[0].defined_in == "web"
    assert reloaded.stations[0].name == "Ignored"
    assert any(
        path == str(bad) and "secrets" in reason for path, reason in reloaded.skipped_definitions
    )
    assert any(
        check.name == "station-definition"
        and check.status is CheckStatus.WARN
        and str(bad) in check.summary
        for check in run_doctor(reloaded, python_version=(3, 14)).checks
    )


def test_serve_moves_a_config_station_into_its_own_file(tmp_path: Path) -> None:
    from autodj.config import move_station_tables

    extra = (
        '\n[stations.owl]\nautostart = true\n[stations.owl.stream]\nstation_name = "Owl"\n'
        'listener_user = "dj"\nlistener_password = "secret"\n[stations.owl.recording]\nfolder = "recordings/owl"\n'
        '[stations.owl.stream.relays.main]\ntype = "icecast"\nhost = "radio.example.net"\nmount = "/owl"\n'
    )
    (tmp_path / "config.local.toml").write_text(
        '[stations.owl.stream.relays.main]\npassword = "source"\n'
        '[[stations.owl.webhooks]]\nurl = "https://example.com/hook"\n',
        encoding="utf-8",
    )
    cfg = _root(tmp_path, extra)
    assert cfg.stations[0].defined_in == "config"
    assert move_station_tables(cfg) is True
    paths = StationPaths.for_station(cfg, "owl")
    document = json.loads(paths.definition.read_text("utf-8"))
    assert "listener_password" not in document["station"]["stream"]
    assert "relays" not in document["station"]["stream"]
    relays = json.loads(paths.relays.read_text("utf-8"))["relays"]
    assert [(item["name"], item["password"]) for item in relays] == [("main", "source")]
    secrets = json.loads(paths.secrets.read_text("utf-8"))
    assert secrets["stream"] == {"listener_user": "dj", "listener_password": "secret"}
    assert secrets["webhooks"] == [{"url": "https://example.com/hook"}]
    reloaded = load_config(cfg.config_path, environ={})
    owl = reloaded.stations[0]
    assert (owl.slug, owl.defined_in, owl.autostart) == ("owl", "web", True)
    assert owl.cfg.stream.listener_password == "secret"
    assert [hook.url for hook in owl.cfg.webhooks] == ["https://example.com/hook"]
    assert move_station_tables(reloaded) is False
    # Once moved, a password changed in config.toml is not read.
    cfg.config_path.write_text(
        cfg.config_path.read_text("utf-8").replace('"secret"', '"other"'), encoding="utf-8"
    )
    again = load_config(cfg.config_path, environ={}).stations[0]
    assert again.cfg.stream.listener_password == "secret"
