from __future__ import annotations

import re
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


def test_devices_rename_stores_a_valid_name_and_refuses_the_rest() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        _write_config()
        registry = DeviceRegistry(Path("index/.paired-devices.sqlite3"))
        device = registry.pair("Kitchen tablet")

        def rename(device_id: str, name: str):
            args = ["--config", "config.toml", "devices", "rename", device_id, name]
            return runner.invoke(cli, args)

        renamed = rename(device.device_id, "Hall speaker")
        too_long = rename(device.device_id, "x" * 65)
        missing = rename("f" * 32, "Nobody")
        names = [d.name for d in registry.list_devices()]

    assert renamed.exit_code == 0
    assert f"Renamed device {device.device_id} to Hall speaker." in renamed.output
    assert too_long.exit_code == 1
    assert "1 to 64 printable characters" in too_long.output
    assert missing.exit_code == 1
    assert "was not found" in missing.output
    assert names == ["Hall speaker"]


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
    # stdout stays the bare code for scripts; the validity note goes to stderr.
    assert re.fullmatch(r"[0-9]{8}\n", code.stdout)
    assert re.search(
        r"Pairs one browser within about \d+ seconds\. A new code starts in \d+", code.stderr
    )
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


def test_devices_pairing_code_lets_one_browser_pair_on_the_running_server() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        _write_config()
        registry = DeviceRegistry(Path("index/.paired-devices.sqlite3"))
        assert registry.pair_on_request("Before asking") is None

        result = runner.invoke(cli, ["--config", "config.toml", "devices", "pairing-code"])

        assert result.exit_code == 0
        assert registry.pair_on_request("Kitchen tablet") is not None
        assert registry.pair_on_request("Second phone") is None
