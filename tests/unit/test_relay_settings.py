import json
import os

from autodj.config import RelayConfig, relay_password_variable, station_relay_password_variable
from autodj.relay_settings import load_relays, public_relay, save_relays


def relay(name="main", **values):
    return RelayConfig.from_table(
        name,
        {
            "type": "icecast",
            "host": "ice.example",
            "mount": "/live",
            "password": "private",
            **values,
        },
    )


def test_private_relay_file_is_authoritative_redacted_and_owner_only(tmp_path):
    path = tmp_path / "station" / "relays.json"
    config = relay()
    save_relays(path, {config.name: config}, "default")

    text = path.read_text(encoding="utf-8")
    assert "private" in text
    assert public_relay(config, {"state": "live"}, "default")["password_configured"] is True
    assert "password" not in public_relay(config, {}, "default")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_relays(path, {}, "default")
    assert loaded["main"].password == "private"

    save_relays(path, {}, "default")
    assert load_relays(path, {"main": config}, "default") == {}


def test_environment_password_overrides_and_is_not_written(tmp_path, monkeypatch):
    config = relay()
    variable = relay_password_variable("main")
    monkeypatch.setenv(variable, "from-environment")
    path = tmp_path / "relays.json"
    save_relays(path, {"main": config}, "default")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "password" not in data["relays"][0]
    loaded = load_relays(path, {}, "default")
    assert loaded["main"].password == "from-environment"
    safe = public_relay(loaded["main"], {}, "default")
    assert safe["password_managed"] is True
    assert "from-environment" not in str(safe)


def test_station_password_environment_name_is_isolated(tmp_path, monkeypatch):
    config = relay()
    variable = station_relay_password_variable("nightowl", "main")
    monkeypatch.setenv(variable, "station-secret")
    path = tmp_path / "relays.json"
    save_relays(path, {"main": config}, "nightowl")
    assert load_relays(path, {}, "nightowl")["main"].password == "station-secret"


def _station(tmp_path):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock

    station = SimpleNamespace(
        slug="default",
        paths=SimpleNamespace(relays=tmp_path / "relays.json"),
        cfg=SimpleNamespace(stream=SimpleNamespace(relays={})),
        bridge=SimpleNamespace(relays=Mock(), relay_status=lambda: []),
    )
    registry = SimpleNamespace(relay_lock=threading.RLock(), relay_configs=lambda: {})
    return station, registry


def _body(name):
    from autodj.station_routes import RelaySettingsBody

    return RelaySettingsBody(
        name=name,
        type="icecast",
        host="ice.example",
        port=8000,
        mount=f"/{name}",
        password="private",
    )


def test_concurrent_relay_saves_keep_both_definitions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from autodj.station_routes import _save_stream_relay

    station, registry = _station(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(_save_stream_relay, station, _body(name), registry)
            for name in ("one", "two")
        ]
        for job in jobs:
            job.result(timeout=5)
    assert set(station.cfg.stream.relays) == {"one", "two"}
    assert set(load_relays(station.paths.relays, {}, "default")) == {"one", "two"}


def test_failed_relay_remove_restores_authoritative_file(tmp_path):
    import pytest
    from fastapi import HTTPException

    from autodj.station_routes import _remove_stream_relay

    station, registry = _station(tmp_path)
    station.cfg.stream.relays = {"main": relay()}
    save_relays(station.paths.relays, station.cfg.stream.relays, "default")
    before = station.paths.relays.read_bytes()
    station.bridge.relays.reconfigure.side_effect = RuntimeError("cannot reconfigure")
    with pytest.raises(HTTPException) as error:
        _remove_stream_relay(station, "main", registry)
    assert error.value.status_code == 503
    assert station.paths.relays.read_bytes() == before
    assert set(station.cfg.stream.relays) == {"main"}


def test_new_env_managed_relay_needs_no_password_in_request(tmp_path, monkeypatch):
    from autodj.station_routes import _save_stream_relay

    station, registry = _station(tmp_path)
    monkeypatch.setenv(relay_password_variable("one"), "environment-secret")
    body = _body("one").model_copy(update={"password": None})
    result = _save_stream_relay(station, body, registry)
    assert result["relay"]["password_managed"]
    assert "environment-secret" not in str(result)
    assert "environment-secret" not in station.paths.relays.read_text()


def test_invalid_saved_relays_do_not_reactivate_config_relays(tmp_path):
    path = tmp_path / "relays.json"
    path.write_text("bad json")
    assert load_relays(path, {"main": relay()}, "default") == {}
