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
