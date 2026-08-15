from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from click.testing import CliRunner

from autodj.cli import cli
from autodj.pairing import DeviceRegistry

_SECRET = "a" * 64


def _write_config(*, access_token: str | None = _SECRET) -> None:
    token_line = f"access_token = '{access_token}'\n" if access_token is not None else ""
    Path("config.toml").write_text(
        "[index]\nindex_dir = 'index'\nname = 'workout'\n[server]\n" + token_line,
        encoding="utf-8",
    )


def test_setup_lan_writes_ignored_compose_environment_without_echoing_secret(
    monkeypatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr("autodj.cli.secrets.token_hex", lambda size: _SECRET)

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["setup-lan", "--host-name", "radio.local"])
        content = Path(".env").read_text(encoding="utf-8")
        mode = stat.S_IMODE(Path(".env").stat().st_mode)

    assert result.exit_code == 0
    assert content == (
        f"AUTODJ_ACCESS_TOKEN={_SECRET}\n"
        "AUTODJ_LAN_HOST=radio.local\n"
        "AUTODJ_LAN_ORIGIN=http://radio.local:8080\n"
    )
    assert _SECRET not in result.output
    assert "docker compose --profile lan up autodj-lan" in result.output
    if os.name != "nt":
        assert mode == 0o600


def test_setup_lan_never_overwrites_existing_environment(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr("autodj.cli.secrets.token_hex", lambda size: _SECRET)

    with runner.isolated_filesystem():
        Path(".env").write_text("KEEP=this\n", encoding="utf-8")
        result = runner.invoke(cli, ["setup-lan", "--host-name", "radio.local"])
        content = Path(".env").read_text(encoding="utf-8")

    assert result.exit_code == 1
    assert content == "KEEP=this\n"
    assert "was not changed" in result.output


def test_setup_lan_formats_ipv6_origin_for_fresh_clone(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr("autodj.cli.secrets.token_hex", lambda size: _SECRET)

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["setup-lan", "--host-name", "2001:db8::25"])
        content = Path(".env").read_text(encoding="utf-8") if Path(".env").exists() else ""

    assert result.exit_code == 0
    assert "AUTODJ_LAN_HOST=2001:db8::25\n" in content
    assert "AUTODJ_LAN_ORIGIN=http://[2001:db8::25]:8080\n" in content


def test_setup_lan_removes_secret_file_when_permission_hardening_fails(
    monkeypatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr("autodj.cli.secrets.token_hex", lambda size: _SECRET)

    def fail_chmod(_path: Path, _mode: int) -> None:
        raise OSError("denied")

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["setup-lan", "--host-name", "radio.local"])
        remains = Path(".env").exists()

    assert result.exit_code == 1
    assert "Could not create .env" in result.output
    assert not remains


def test_devices_list_and_revoke_use_persistent_registry() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        _write_config()
        registry = DeviceRegistry(Path("index/.paired-devices.sqlite3"))
        device = registry.pair("Kitchen tablet")

        listed = runner.invoke(cli, ["--config", "config.toml", "devices", "list"])
        revoked = runner.invoke(
            cli,
            ["--config", "config.toml", "devices", "revoke", device.device_id],
        )
        is_active = registry.is_active(device.device_id)

    assert listed.exit_code == 0
    assert device.device_id in listed.output
    assert "Kitchen tablet" in listed.output
    assert revoked.exit_code == 0
    assert not is_active


def test_devices_pairing_code_and_reset_manage_all_browsers() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        _write_config()
        registry = DeviceRegistry(Path("index/.paired-devices.sqlite3"), now=lambda: 1_000)
        registry.pair("Kitchen tablet")
        registry.pair("Living room display")

        code = runner.invoke(cli, ["--config", "config.toml", "devices", "pairing-code"])
        reset = runner.invoke(
            cli,
            ["--config", "config.toml", "devices", "reset"],
            input="y\n",
        )
        devices = registry.list_devices()

    assert code.exit_code == 0
    assert re.fullmatch(r"[0-9]{8}\n", code.output)
    assert _SECRET not in code.output
    assert reset.exit_code == 0
    assert "Revoked 2 paired browser(s)." in reset.output
    assert all(device.revoked_at is not None for device in devices)


def test_devices_commands_report_empty_and_missing_configuration() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        _write_config(access_token=None)
        listed = runner.invoke(cli, ["--config", "config.toml", "devices", "list"])
        code = runner.invoke(cli, ["--config", "config.toml", "devices", "pairing-code"])
        revoked = runner.invoke(
            cli,
            ["--config", "config.toml", "devices", "revoke", "f" * 32],
        )

    assert listed.exit_code == 0
    assert "No browsers have been paired." in listed.output
    assert code.exit_code == 1
    assert "Configure LAN access" in code.output
    assert revoked.exit_code == 1
    assert "was not found" in revoked.output
