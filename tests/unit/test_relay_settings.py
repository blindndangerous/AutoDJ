"""Relays saved from Settings, Stream: precedence, secrets, failed saves (#185)."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodj import relay_settings
from autodj.config import RelayConfig, StreamConfig
from autodj.relay import RelayError
from autodj.relay_settings import (
    RelayControl,
    RelaySettingsError,
    RelayStore,
    Saved,
    check_host,
    merge,
    parse_body,
    password_variable,
)

PASSWORD = "Saved-Pa55word"
TOML_PASSWORD = "Toml-Pa55word"


def _paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        relays=tmp_path / "relays.json", relay_passwords=tmp_path / "relay-passwords.json"
    )


def _icecast(**extra: Any) -> dict[str, Any]:
    return {
        "name": "main",
        "type": "icecast",
        "host": "relay.example.org",
        "port": 8000,
        "mount": "/live",
        **extra,
    }


class FakeManager:
    """Records what the control asks of the running relays."""

    def __init__(self) -> None:
        self.applied: list[RelayConfig] = []
        self.removed: list[str] = []
        self.reconnects = 0
        self.fail: BaseException | None = None

    def apply(self, cfg: RelayConfig) -> None:
        if self.fail is not None:
            raise self.fail
        self.applied.append(cfg)

    def remove(self, name: str) -> None:
        self.removed.append(name)

    def reconnect(self) -> None:
        self.reconnects += 1

    def status(self) -> list[dict[str, Any]]:
        return [
            {"name": cfg.name, "state": "live", "error": None, "title_error": None}
            for cfg in self.applied
        ]


def _public(host, port, **_kwargs):
    """A resolver that finds every name at a documentation address, as a server would be."""
    return [(2, 1, 6, "", ("192.0.2.10", port))]


def _control(
    tmp_path: Path,
    toml: dict[str, Any] | None = None,
    *,
    environ: dict[str, str] | None = None,
    elsewhere=tuple,
    slug: str = "default",
    resolve=_public,
) -> tuple[RelayControl, StreamConfig, FakeManager]:
    stream = StreamConfig(relays=toml or {})
    control = RelayControl(
        slug=slug,
        stream=stream,
        paths=_paths(tmp_path),  # type: ignore[arg-type]
        environ=environ or {},
        elsewhere=elsewhere,
        resolve=resolve,
    )
    control.load()
    manager = FakeManager()
    control.attach(manager)  # type: ignore[arg-type]
    return control, stream, manager


def _toml_relay(**extra: Any) -> dict[str, Any]:
    return {
        "type": "shoutcast2",
        "host": "sc.example.org",
        "port": 8010,
        "password": TOML_PASSWORD,
        **extra,
    }


# -- the body the page sends ---------------------------------------------------------


def test_parse_body_keeps_only_what_the_type_uses() -> None:
    name, table, password = parse_body(
        {**_icecast(user="", stream_id=4), "password": PASSWORD, "bitrate": 128}
    )
    assert name == "main" and password == PASSWORD
    assert table == {
        "type": "icecast",
        "host": "relay.example.org",
        "port": 8000,
        "mount": "/live",
        "stream_id": None,
        "user": None,
        "bitrate": 128,
        "public": False,
        "enabled": True,
    }
    _name, shoutcast, empty = parse_body(
        {"name": "sc", "type": "shoutcast1", "host": "h", "mount": "/x", "password": ""}
    )
    assert shoutcast["mount"] is None and shoutcast["port"] == 8000 and empty is None


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"name": "bad name"}, "name"),
        ({"name": ""}, "name"),
        ({"type": "http"}, "type"),
        ({"host": "   "}, "host"),
        ({"port": True}, "port"),
        ({"port": "8000"}, "port"),
        ({"mount": None}, "mount"),
        ({"bitrate": 64}, "bitrate"),
        ({"public": "yes"}, "public"),
        ({"enabled": 1}, "enabled"),
        ({"password": 5}, "password"),
    ],
)
def test_parse_body_names_the_field_refused(change: dict, field: str) -> None:
    with pytest.raises(RelaySettingsError) as caught:
        parse_body({**_icecast(), **change})
    assert caught.value.status == 422 and caught.value.field == field


def test_parse_body_refuses_unknown_settings() -> None:
    with pytest.raises(RelaySettingsError, match="Unknown relay setting: secret"):
        parse_body({**_icecast(), "secret": 1})
    with pytest.raises(RelaySettingsError, match="JSON object"):
        parse_body(["main"])


# -- where each value comes from -----------------------------------------------------------


def test_merge_precedence_environment_then_saved_then_toml(caplog) -> None:
    toml = {
        "a": RelayConfig.from_table("a", _toml_relay()),
        "b": RelayConfig.from_table("b", _toml_relay(stream_id=2)),
        "c": RelayConfig.from_table("c", _toml_relay(stream_id=3)),
    }
    saved = Saved(
        definitions={
            "b": {"type": "icecast", "host": "relay.example.org", "mount": "/b", "extra": 1},
            "d": {"type": "icecast", "host": "relay.example.org", "mount": "/d"},
            "e": {"type": "icecast", "host": "relay.example.org"},
        },
        passwords={"a": "saved-a", "b": "saved-b"},
    )
    environ = {password_variable("default", "a"): "from-env"}
    with caplog.at_level(logging.WARNING):
        entries = merge(toml, saved, slug="default", environ=environ)
    assert list(entries) == ["a", "b", "c", "d", "e"]
    a, b, c, d, e = entries.values()
    assert (a.source, a.password_source, a.config.password) == ("config", "environment", "from-env")
    # A table saved here replaces the TOML one as a whole.
    assert (b.source, b.password_source, b.config.password) == ("config_saved", "saved", "saved-b")
    assert (b.table["type"], b.table["mount"], b.table["user"]) == ("icecast", "/b", "source")
    assert (c.source, c.password_source, c.config.password) == ("config", "config", TOML_PASSWORD)
    assert d.config is None and d.password_source is None and "No password" in d.problem
    assert e.config is None and not e.valid and "not valid" in e.problem
    assert "unknown keys of relay b" in caplog.text


def test_unusable_saved_settings_fall_back_to_the_toml_table() -> None:
    toml = {"a": RelayConfig.from_table("a", _toml_relay())}
    saved = Saved(definitions={"a": {"type": "icecast", "host": "x y"}})
    entry = merge(toml, saved, slug="default", environ={})["a"]
    assert entry.source == "config" and entry.config is not None
    assert entry.config.host == "sc.example.org" and "not valid" in entry.problem


def test_station_password_variables_name_the_station() -> None:
    assert password_variable("default", "main-1") == "AUTODJ_STREAM_RELAY_MAIN_1_PASSWORD"
    assert password_variable("night-owl", "main") == "AUTODJ_STATION_NIGHT_OWL_RELAY_MAIN_PASSWORD"


# -- the files -------------------------------------------------------------------------


def test_the_password_file_is_private_and_the_settings_file_has_no_password(tmp_path) -> None:
    control, stream, manager = _control(tmp_path)
    control.save({**_icecast(), "password": PASSWORD}, create=True)
    settings = (tmp_path / "relays.json").read_text(encoding="utf-8")
    assert PASSWORD not in settings
    assert json.loads(settings)["relays"]["main"]["mount"] == "/live"
    secrets = json.loads((tmp_path / "relay-passwords.json").read_text(encoding="utf-8"))
    assert secrets == {"version": 1, "passwords": {"main": PASSWORD}}
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(tmp_path / "relay-passwords.json").st_mode)
        assert mode == 0o600
    assert stream.relays["main"].password == PASSWORD
    assert manager.applied[-1].password == PASSWORD


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"version": 2, "relays": {}}', '{"version": 1, "relays": []}']
)
def test_a_damaged_file_is_never_overwritten(tmp_path, content, caplog) -> None:
    (tmp_path / "relays.json").write_text(content, encoding="utf-8")
    control, _stream, manager = _control(tmp_path)
    assert "relays.json" in caplog.text
    described = control.describe()
    assert "could not be read" in described["problem"]
    with pytest.raises(RelaySettingsError) as caught:
        control.save({**_icecast(), "password": PASSWORD}, create=True)
    assert caught.value.status == 503
    assert (tmp_path / "relays.json").read_text(encoding="utf-8") == content
    assert manager.applied == []


def test_unknown_keys_in_the_files_are_warned_about_and_ignored(tmp_path, caplog) -> None:
    (tmp_path / "relays.json").write_text(
        json.dumps({"version": 1, "relays": {}, "future": True}), encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        control, _stream, _manager = _control(tmp_path)
    assert "Ignoring unknown keys in relays.json: ['future']" in caplog.text
    assert control.describe()["problem"] is None


def test_a_failed_save_changes_nothing_and_keeps_the_working_password(
    tmp_path, monkeypatch
) -> None:
    control, stream, manager = _control(tmp_path)
    control.save({**_icecast(), "password": PASSWORD}, create=True)
    before = {
        path.name: path.read_bytes()
        for path in (tmp_path / "relays.json", tmp_path / "relay-passwords.json")
    }
    real = relay_settings.atomic_write

    def failing(path: Path, data, **kwargs) -> None:
        if path.name == "relays.json":
            raise OSError("disk full")
        real(path, data, **kwargs)

    monkeypatch.setattr(relay_settings, "atomic_write", failing)
    with pytest.raises(RelaySettingsError) as caught:
        control.save({**_icecast(port=9000), "password": "a-new-password"}, create=False)
    assert caught.value.status == 503 and "Nothing was changed" in caught.value.message
    after = {name: (tmp_path / name).read_bytes() for name in before}
    assert after == before
    assert stream.relays["main"].port == 8000 and stream.relays["main"].password == PASSWORD
    assert len(manager.applied) == 1
    monkeypatch.setattr(relay_settings, "atomic_write", real)
    # A later save still knows the working password.
    control.save({**_icecast(port=9000)}, create=False)
    assert manager.applied[-1].password == PASSWORD and manager.applied[-1].port == 9000


def test_saved_relays_come_back_after_a_restart(tmp_path) -> None:
    control, _stream, _manager = _control(tmp_path)
    control.save({**_icecast(public=True), "password": PASSWORD}, create=True)
    control.set_enabled("main", False)
    again, stream, _manager = _control(tmp_path)
    relay = stream.relays["main"]
    assert (relay.public, relay.enabled, relay.password) == (True, False, PASSWORD)
    assert again.describe()["relays"][0]["source"] == "saved"


# -- what the page sees ------------------------------------------------------------------


def test_no_password_reaches_the_page_or_the_log(tmp_path, caplog) -> None:
    toml = {"a": _toml_relay()}
    with caplog.at_level(logging.DEBUG):
        control, _stream, _manager = _control(tmp_path, toml)
        control.save({**_icecast(), "password": PASSWORD}, create=True)
        control.save({**_icecast(), "password": ""}, create=False)
        described = control.describe()
    text = json.dumps(described)
    for secret in (PASSWORD, TOML_PASSWORD):
        assert secret not in text
        assert secret not in caplog.text
    a, main = described["relays"]
    assert a["password_set"] and a["password_source"] == "config"
    assert main["password_set"] and main["password_source"] == "saved"
    assert main["password_variable"] == "AUTODJ_STREAM_RELAY_MAIN_PASSWORD"
    assert (a["removable"], a["revertable"]) == (False, False)
    assert (main["removable"], main["revertable"]) == (True, False)
    assert main["state"] == "live" and main["target"] == "relay.example.org:8000/live"
    assert described["max_relays"] == 8


def test_a_relay_without_a_password_says_so_and_cannot_be_turned_on(tmp_path) -> None:
    (tmp_path / "relays.json").write_text(
        json.dumps({"version": 1, "relays": {"main": {**_icecast(), "enabled": False}}}),
        encoding="utf-8",
    )
    control, stream, _manager = _control(tmp_path)
    assert "main" not in stream.relays
    (entry,) = control.describe()["relays"]
    assert entry["password_set"] is False and entry["state"] == "off"
    assert "No password" in entry["error"]
    with pytest.raises(RelaySettingsError, match="cannot be turned on") as caught:
        control.set_enabled("main", True)
    assert caught.value.status == 409


# -- changes -------------------------------------------------------------------------------


def test_add_edit_disable_and_remove_change_only_that_relay(tmp_path) -> None:
    control, stream, manager = _control(tmp_path, {"a": _toml_relay()})
    added = control.save({**_icecast(), "password": PASSWORD}, create=True)
    assert added["name"] == "main" and manager.applied[-1].name == "main"
    edited = control.save({**_icecast(bitrate=128)}, create=False)
    assert edited["bitrate"] == 128 and manager.applied[-1].password == PASSWORD
    off = control.set_enabled("main", False)
    assert off["enabled"] is False and manager.applied[-1].enabled is False
    assert control.remove("main") == ("removed", None)
    assert manager.removed == ["main"] and "main" not in stream.relays
    assert not (tmp_path / "relay-passwords.json").read_text(encoding="utf-8").count(PASSWORD)
    # The TOML relay was never touched.
    assert all(cfg.name != "a" for cfg in manager.applied)
    with pytest.raises(RelaySettingsError) as caught:
        control.remove("main")
    assert caught.value.status == 404
    with pytest.raises(RelaySettingsError) as caught:
        control.save({**_icecast(), "password": PASSWORD}, create=False)
    assert caught.value.status == 404


def test_a_toml_relay_can_be_changed_here_and_reverted(tmp_path) -> None:
    control, stream, manager = _control(tmp_path, {"a": _toml_relay()})
    with pytest.raises(RelaySettingsError, match=r"set in config.toml") as caught:
        control.remove("a")
    assert caught.value.status == 409
    changed = control.save({**_toml_relay(stream_id=7), "name": "a", "password": ""}, create=False)
    assert changed["source"] == "config_saved" and changed["revertable"]
    assert manager.applied[-1].password == TOML_PASSWORD and manager.applied[-1].stream_id == 7
    assert control.remove("a") == ("reverted", None)
    assert manager.applied[-1].stream_id == 1 and stream.relays["a"].stream_id == 1
    assert control.describe()["relays"][0]["source"] == "config"


def test_an_environment_password_is_never_replaced_from_the_page(tmp_path) -> None:
    variable = "AUTODJ_STREAM_RELAY_MAIN_PASSWORD"
    control, _stream, manager = _control(tmp_path, environ={variable: "from-env"})
    with pytest.raises(RelaySettingsError) as caught:
        control.save({**_icecast(), "password": PASSWORD}, create=True)
    assert caught.value.status == 409 and caught.value.field == "password"
    assert variable in caught.value.message
    added = control.save(_icecast(), create=True)
    assert added["password_source"] == "environment"
    assert manager.applied[-1].password == "from-env"
    assert not (tmp_path / "relay-passwords.json").exists()


def test_a_new_relay_needs_a_password(tmp_path) -> None:
    control, _stream, _manager = _control(tmp_path)
    with pytest.raises(RelaySettingsError) as caught:
        control.save(_icecast(), create=True)
    assert (caught.value.status, caught.value.field) == (422, "password")


def test_names_targets_and_the_relay_limit_are_checked(tmp_path) -> None:
    control, _stream, _manager = _control(tmp_path)
    control.save({**_icecast(), "password": PASSWORD}, create=True)
    with pytest.raises(RelaySettingsError) as caught:
        control.save({**_icecast(), "password": PASSWORD}, create=True)
    assert (caught.value.status, caught.value.field) == (409, "name")
    with pytest.raises(RelaySettingsError, match="already sends") as caught:
        control.save(
            {**_icecast(name="copy", host="RELAY.example.org."), "password": "x"}, create=True
        )
    assert caught.value.field == "host"
    with pytest.raises(RelaySettingsError, match="password from") as caught:
        control.save({**_icecast(name="MAIN", mount="/other"), "password": "x"}, create=True)
    assert caught.value.field == "name"
    for number in range(7):
        control.save(
            {**_icecast(name=f"r{number}", mount=f"/m{number}"), "password": "x"}, create=True
        )
    with pytest.raises(RelaySettingsError, match="at most 8"):
        control.save({**_icecast(name="ninth", mount="/ninth"), "password": "x"}, create=True)


def test_another_stations_target_is_refused_and_not_started(tmp_path) -> None:
    taken = ("night", "theirs", ("relay.example.org", 8000, "/live", None), "AUTODJ_X")
    control, _stream, _manager = _control(tmp_path, elsewhere=lambda: [taken])
    with pytest.raises(RelaySettingsError, match="Station night's relay theirs") as caught:
        control.save({**_icecast(), "password": PASSWORD}, create=True)
    assert caught.value.status == 409
    # One saved before the other station took the target is kept but not started.
    (tmp_path / "relays.json").write_text(
        json.dumps({"version": 1, "relays": {"main": _icecast()}}), encoding="utf-8"
    )
    (tmp_path / "relay-passwords.json").write_text(
        json.dumps({"version": 1, "passwords": {"main": PASSWORD}}), encoding="utf-8"
    )
    again, stream, _manager = _control(tmp_path, elsewhere=lambda: [taken])
    assert stream.relays == {}
    assert "Station night already relays" in again.describe()["relays"][0]["error"]


def test_a_failed_start_after_saving_is_reported_as_saved(tmp_path) -> None:
    control, _stream, manager = _control(tmp_path)
    manager.fail = OSError("no ffmpeg")
    added = control.save({**_icecast(bitrate=128), "password": PASSWORD}, create=True)
    # Saved, so the page lists it as the file has it, and says why it is not running.
    assert "could not start" in added["warning"]
    assert added["bitrate"] == 128
    assert "main" in json.loads((tmp_path / "relays.json").read_text(encoding="utf-8"))["relays"]
    assert [relay["name"] for relay in control.describe()["relays"]] == ["main"]
    manager.fail = RelayError("the stream is stopping")
    assert "stopping" in control.save({**_icecast()}, create=False)["warning"]
    manager.fail = None
    assert control.save({**_icecast()}, create=False)["warning"] is None


def test_a_stopped_stream_refuses_changes_before_writing(tmp_path) -> None:
    control, _stream, _manager = _control(tmp_path)
    control.attach(None)
    for change in (
        lambda: control.save({**_icecast(), "password": PASSWORD}, create=True),
        lambda: control.remove("main"),
    ):
        with pytest.raises(RelaySettingsError, match="stream is stopped") as caught:
            change()
        assert caught.value.status == 409
    assert not (tmp_path / "relays.json").exists()
    assert not (tmp_path / "relay-passwords.json").exists()


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "169.254.169.254.",
        "2852039166",
        "0xA9.0xFE.0xA9.0xFE",
        "0251.0376.0251.0376",
        "169.254.43518",
        "::ffff:169.254.169.254",
        "[::ffff:a9fe:a9fe]",
        "2002:a9fe:a9fe::1",
        "fd00:ec2::254",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "fe80::1",
    ],
)
def test_hosts_that_are_never_servers_are_refused_however_written(host: str) -> None:
    with pytest.raises(RelaySettingsError) as caught:
        check_host(host, 8000, resolve=_public)
    assert (caught.value.status, caught.value.field) == (422, "host")


def test_a_name_resolving_to_a_metadata_address_is_refused() -> None:
    def metadata(host, port, **_kwargs):
        return [
            (2, 1, 6, "", ("192.0.2.10", port)),
            (10, 1, 6, "", ("::ffff:169.254.169.254", port, 0, 0)),
        ]

    with pytest.raises(RelaySettingsError, match="never a streaming server"):
        check_host("relay.example.org", 8000, resolve=metadata)

    def missing(host, port, **_kwargs):
        raise OSError("name not known")

    with pytest.raises(RelaySettingsError, match="could not be found") as caught:
        check_host("relay.example.org", 8000, resolve=missing)
    assert caught.value.field == "host"


@pytest.mark.parametrize(
    "host", ["relay.example.org", "192.0.2.10", "2001:db8::10", "127.0.0.1", "cafe.be"]
)
def test_ordinary_hosts_are_accepted(host: str) -> None:
    seen: list = []

    def resolve(name, port, **_kwargs):
        seen.append(name)
        return _public(name, port)

    check_host(host, 8000, resolve=resolve)
    assert seen == [host]


def test_a_new_host_is_checked_but_turning_a_toml_relay_off_is_not(tmp_path) -> None:
    looked_up: list[str] = []

    def resolve(host, port, **kwargs):
        looked_up.append(host)
        return _public(host, port)

    # config.toml may name a link-local server; the page does not judge it again.
    toml = {"lan": _toml_relay(host="169.254.10.20")}
    control, _stream, manager = _control(tmp_path, toml, resolve=resolve)
    off = control.set_enabled("lan", False)
    assert off["enabled"] is False and manager.applied[-1].host == "169.254.10.20"
    assert looked_up == []
    control.save({**_icecast(), "password": PASSWORD}, create=True)
    assert looked_up == ["relay.example.org"]
    control.save({**_icecast(public=True)}, create=False)
    assert looked_up == ["relay.example.org"]
    with pytest.raises(RelaySettingsError) as caught:
        control.save({**_icecast(host="2852039166")}, create=False)
    assert caught.value.field == "host"


def test_a_slow_restart_on_one_station_does_not_hold_up_another(tmp_path) -> None:
    import threading

    release = threading.Event()
    started = threading.Event()

    class SlowManager(FakeManager):
        def apply(self, cfg: RelayConfig) -> None:
            started.set()
            release.wait(5)
            super().apply(cfg)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    slow, _stream, _manager = _control(tmp_path / "a")
    slow.attach(SlowManager())  # type: ignore[arg-type]
    other, _stream, other_manager = _control(tmp_path / "b", slug="night")
    worker = threading.Thread(
        target=lambda: slow.save({**_icecast(), "password": PASSWORD}, create=True)
    )
    worker.start()
    try:
        assert started.wait(5)
        other.save({**_icecast(name="b", mount="/b"), "password": PASSWORD}, create=True)
        assert [cfg.name for cfg in other_manager.applied] == ["b"]
    finally:
        release.set()
        worker.join(5)


def test_unknown_keys_are_removed_from_relays_json_so_backups_hold_no_secret(
    tmp_path, caplog
) -> None:
    (tmp_path / "relays.json").write_text(
        json.dumps(
            {
                "version": 1,
                "relays": {"main": {**_icecast(), "password": "typed-into-the-file"}, "bad": "x"},
                "note": "secret-note",
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        _control(tmp_path)
    text = (tmp_path / "relays.json").read_text(encoding="utf-8")
    assert "typed-into-the-file" not in text and "secret-note" not in text
    data = json.loads(text)
    assert set(data) == {"version", "relays"} and set(data["relays"]) == {"main"}
    assert "password" not in data["relays"]["main"]
    assert "Removing unknown keys of relay main" in caplog.text
    assert "typed-into-the-file" not in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def test_the_password_file_is_readable_by_this_user_only_on_windows(tmp_path) -> None:
    import subprocess

    control, _stream, _manager = _control(tmp_path)
    control.save({**_icecast(), "password": PASSWORD}, create=True)
    listing = subprocess.run(
        ["icacls", str(tmp_path / "relay-passwords.json")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = [line.strip() for line in listing.splitlines()[:-2] if line.strip()]
    # One entry, the user's, full control, not inherited.
    assert len(entries) == 1, listing
    assert entries[0].endswith(":(F)"), listing
    assert os.environ["USERNAME"].lower() in entries[0].lower(), listing


def test_reconnect_reaches_the_manager(tmp_path) -> None:
    control, _stream, manager = _control(tmp_path)
    control.reconnect()
    assert manager.reconnects == 1
    control.attach(None)
    control.reconnect()
    assert manager.reconnects == 1


def test_targets_list_this_stations_relays(tmp_path) -> None:
    control, _stream, _manager = _control(tmp_path, {"a": _toml_relay()}, slug="night")
    ((slug, name, target, variable),) = control.targets()
    assert (slug, name, target) == ("night", "a", ("sc.example.org", 8010, None, 1))
    assert variable == "AUTODJ_STATION_NIGHT_RELAY_A_PASSWORD"


def test_store_write_puts_back_old_passwords_when_settings_fail(tmp_path, monkeypatch) -> None:
    store = RelayStore(tmp_path / "relays.json", tmp_path / "relay-passwords.json")
    old = Saved({"a": {"type": "icecast"}}, {"a": "old"})
    store.write(old, Saved())
    real = relay_settings.atomic_write

    def failing(path: Path, data, **kwargs) -> None:
        if path.name == "relays.json":
            raise OSError("read-only")
        real(path, data, **kwargs)

    monkeypatch.setattr(relay_settings, "atomic_write", failing)
    with pytest.raises(OSError, match="read-only"):
        store.write(Saved({"a": {"type": "icecast"}}, {"a": "new"}), old)
    assert store.load().passwords == {"a": "old"}
