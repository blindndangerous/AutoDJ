"""The update contract: the files a server and its updater exchange (update_contract)."""

from __future__ import annotations

import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from autodj import update_contract as contract
from autodj.update_contract import HealthProbe

PROBE = HealthProbe("http", "127.0.0.1", 8080, "127.0.0.1:8080")


def _request(**overrides: object) -> dict[str, object]:
    return {
        "id": "0123abcd0123abcd",
        "action": "update",
        "channel": "stable",
        "requested_at": 1_000.0,
        "health": PROBE.to_dict(),
        **overrides,
    }


def test_the_update_folder_is_the_updaters_or_the_index_folders(tmp_path: Path) -> None:
    """ADR-0016-12: a launcher names its folder; a container's helper uses the index folder."""
    named = {contract.UPDATE_DIR_ENV: str(tmp_path / "checkout" / ".uv" / "update")}
    assert (
        contract.update_dir(tmp_path / "index", named) == tmp_path / "checkout" / ".uv" / "update"
    )
    assert contract.update_dir(tmp_path / "index", {}) == tmp_path / "index" / "updates"
    assert contract.update_dir(None, {}) is None


def test_every_file_is_versioned_and_damage_reads_as_missing(tmp_path: Path) -> None:
    """ADR-0016-12: written atomically with the contract's version; anything else is ignored."""
    path = tmp_path / contract.REQUEST
    assert contract.read(path) is None
    contract.write(path, _request())
    assert contract.read(path) == {**_request(), "contract": contract.CONTRACT_VERSION}
    request = contract.read_request(tmp_path)
    assert request == contract.Request("0123abcd0123abcd", "update", "stable", 1_000.0, PROBE)
    for damaged in (
        "not json",
        "[]",
        '{"contract": 2, "id": "0123abcd"}',
        '{"contract": 1, "id": "../etc"}',
        '{"contract": 1, "pad": "' + "x" * 70_000 + '"}',
    ):
        path.write_text(damaged, encoding="utf-8")
        assert contract.read(path) is None, damaged[:30]
    for bad in (
        {"action": "downgrade"},
        {"channel": "nightly"},
        {"health": {"scheme": "file"}},
        {"requested_at": True},
        {"id": None},
    ):
        contract.write(path, _request(**bad))
        assert contract.read_request(tmp_path) is None, bad


def test_a_result_has_generic_steps_and_commands_only_on_failure(tmp_path: Path) -> None:
    """ADR-0016-12: the steps any updater maps onto, skipped rather than missing."""
    steps = contract.new_steps()
    assert [step["name"] for step in steps] == [
        "check",
        "backup",
        "fetch",
        "switch",
        "setup",
        "restore",
        "migration-check",
        "start",
        "health",
        "rollback",
    ]
    assert {step["status"] for step in steps} == {"pending"}
    result = {
        "id": "0123abcd",
        "action": "update",
        "outcome": "updated",
        "message": "Updated to 0.22.0.",
        "steps": steps,
        "commands": [],
    }
    contract.write(tmp_path / contract.RESULT, result)
    assert contract.read_result(tmp_path)["outcome"] == "updated"
    contract.write(tmp_path / contract.RESULT, {**result, "commands": ["git status --short"]})
    assert contract.read_result(tmp_path) is None
    contract.write(
        tmp_path / contract.RESULT,
        {**result, "outcome": "refused", "commands": ["git status --short"]},
    )
    assert contract.read_result(tmp_path)["commands"] == ["git status --short"]


def test_the_health_check_asks_this_computer_with_an_allowed_host() -> None:
    """ADR-0016-13: plain HTTP a host script can make too, to this computer only."""
    everywhere = contract.health_for(
        "0.0.0.0", 8080, https=False, host_allowed=lambda header: header == "localhost:8080"
    )
    assert everywhere is not None
    assert everywhere.url == "http://127.0.0.1:8080/healthz"
    assert everywhere.host == "localhost:8080"
    # Neither loopback nor the address is allowed: an allowed name, still sent to loopback.
    named = contract.health_for(
        "0.0.0.0",
        8080,
        https=False,
        host_allowed=lambda header: header == "radio.example.lan:8080",
        allowed_hosts=["radio.example.lan", "192.168.1.10"],
    )
    assert named is not None
    assert named.url == "http://127.0.0.1:8080/healthz"
    assert named.host == "radio.example.lan:8080"
    six = contract.health_for("::", 8443, https=True, host_allowed=lambda h: h == "[::1]:8443")
    assert six is not None
    assert six.url == "https://[::1]:8443/healthz"
    # Nothing the server would accept, an address of another computer, a zone
    # address or a name that does not resolve: no probe, so no Update.
    assert contract.health_for("0.0.0.0", 8080, https=False, host_allowed=lambda _h: False) is None
    assert (
        contract.health_for("192.0.2.10", 8080, https=False, host_allowed=lambda _h: True) is None
    )
    assert (
        contract.health_for("fe80::1%eth0", 8080, https=False, host_allowed=lambda _h: True) is None
    )

    def nowhere(*_args: object, **_kwargs: object) -> list[object]:
        raise OSError("no such name")

    assert (
        contract.health_for(
            "radio.example.lan", 8080, https=False, host_allowed=lambda _h: True, resolve=nowhere
        )
        is None
    )
    resolved = contract.health_for(
        "radio.example.lan",
        8080,
        https=False,
        host_allowed=lambda h: h == "radio.example.lan:8080",
        resolve=lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 8080))],
    )
    assert resolved is not None
    assert (resolved.address, resolved.host) == ("127.0.0.1", "radio.example.lan:8080")
    assert contract.parse_health(everywhere.to_dict()) == everywhere
    for bad in (
        None,
        {**everywhere.to_dict(), "scheme": "file"},
        {**everywhere.to_dict(), "address": "example.com"},
        # Not this computer: an updater never asks another one.
        {**everywhere.to_dict(), "address": "192.0.2.10"},
        {**everywhere.to_dict(), "address": "fe80::1%eth0"},
        {**everywhere.to_dict(), "port": 0},
        {**everywhere.to_dict(), "port": True},
        {**everywhere.to_dict(), "host": "a b"},
        {**everywhere.to_dict(), "path": "/api/devices"},
    ):
        assert contract.parse_health(bad) is None, bad


def test_the_health_probe_needs_200_and_ok_from_healthz() -> None:
    """ADR-0016-13: asked on this computer, with the Host header the server allows."""
    seen: list[str | None] = []
    answers = iter([(200, b'{"status": "ok"}'), (403, b"Disallowed Host"), (200, b"{}")])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Host"))
            code, body = next(answers)
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        probe = HealthProbe("http", "127.0.0.1", port, f"localhost:{port}")
        assert contract.probe_health(probe) is True
        assert contract.probe_health(probe) is False
        assert contract.probe_health(probe) is False
    finally:
        server.shutdown()
        server.server_close()
    assert seen == [f"localhost:{port}"] * 3
    assert contract.probe_health(probe, timeout=0.5) is False


def _go_back(folder: Path, backup: Path) -> None:
    contract.write(
        folder / contract.GO_BACK,
        {
            "tag": "v0.21.0",
            "version": "0.21.0",
            "commit": "1" * 40,
            "backup": str(backup),
            "made_at": 1_793_000_000.0,
        },
    )


def test_go_back_only_names_a_backup_this_flow_made(tmp_path: Path) -> None:
    """ADR-0016-20: never a backup from anywhere else; a missing or unreadable one is explained."""
    folder = tmp_path / "update"
    backup = folder / contract.BACKUPS / "autodj-go-back-0.21.0-20261027-101500.zip"
    backup.parent.mkdir(parents=True)
    _go_back(folder, backup)
    record = contract.read_go_back(folder)
    assert record is not None
    assert record.tag == "v0.21.0"
    assert contract.go_back_problem(record) == (
        f"Going back to 0.21.0 is not offered, because the backup AutoDJ made on {record.date} "
        "is missing."
    )
    backup.write_text("not a zip", encoding="utf-8")
    assert "cannot be read" in contract.go_back_problem(record)
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("manifest.json", "{}")
    assert contract.go_back_problem(record) is None
    for elsewhere in (
        tmp_path / "autodj-go-back-0.21.0.zip",
        folder / contract.BACKUPS / "night-owl.zip",
        folder / contract.BACKUPS / "autodj-update-0.21.0-20261027-101500.zip",
    ):
        _go_back(folder, elsewhere)
        assert contract.read_go_back(folder) is None, elsewhere.name


@pytest.mark.parametrize("value", ["0123abcd", "f" * 64])
def test_an_id_is_lowercase_hex(value: str) -> None:
    """ADR-0016-12."""
    assert contract.valid_id(value) == value
    assert contract.valid_id(value.upper() + "G") is None
