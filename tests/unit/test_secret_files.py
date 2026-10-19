"""Private secret files under ``<index_dir>`` (used by LAN pairing)."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from autodj.secret_files import (
    load_or_create_secret,
    paired_devices_path,
    read_secret,
    write_secret,
)

_VALID = re.compile(r"^[A-Za-z0-9_-]{43}$")


class _SecretError(Exception):
    """Stands in for the caller's own error type."""


def _load(path: Path) -> str:
    return load_or_create_secret(
        path, valid=lambda text: bool(_VALID.match(text)), error=_SecretError, what="test secret"
    )


def test_created_on_first_load(tmp_path: Path) -> None:
    value = _load(tmp_path / ".secret")
    assert len(value) == 43
    assert (tmp_path / ".secret").read_text(encoding="ascii").strip() == value


def test_reload_returns_same_value(tmp_path: Path) -> None:
    path = tmp_path / ".secret"
    assert _load(path) == _load(path)


@pytest.mark.parametrize(
    "content", ["", "   \n", "short", "has space in it and is long enough to pass length"]
)
def test_invalid_content_is_replaced(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".secret"
    path.write_text(content, encoding="ascii")
    value = _load(path)
    assert len(value) == 43
    assert value != content.strip()


def test_missing_file_reads_as_none(tmp_path: Path) -> None:
    assert read_secret(tmp_path / "nope", valid=bool, error=_SecretError, what="x") is None


@pytest.mark.parametrize(
    "mode,private",
    [(0o600, True), (0o400, True), (0o640, False), (0o604, False), (0o660, False), (0o601, False)],
)
def test_private_secret_read_checks_posix_permissions_before_contents(
    tmp_path, monkeypatch, mode, private
):
    from autodj import secret_files

    path = tmp_path / "secret"
    path.write_text("private-test-value", encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(secret_files.sys, "platform", "linux")
        patch.setattr(
            secret_files.os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFREG | mode)
        )
        if private:
            assert (
                read_secret(
                    path, valid=bool, error=_SecretError, what="test secret", require_private=True
                )
                == "private-test-value"
            )
        else:
            with pytest.raises(_SecretError, match="owner-only") as rejected:
                read_secret(
                    path, valid=bool, error=_SecretError, what="test secret", require_private=True
                )
            assert "private-test-value" not in str(rejected.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_is_private(tmp_path: Path) -> None:
    _load(tmp_path / ".secret")
    assert stat.S_IMODE((tmp_path / ".secret").stat().st_mode) == 0o600


def test_unreadable_file_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / ".secret"
    path.mkdir()
    with pytest.raises(_SecretError, match="cannot read test secret"):
        _load(path)


def test_write_cleanup_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed rename leaves no temporary file behind."""

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr("os.replace", failing_replace)
    with pytest.raises(_SecretError, match="cannot write test secret"):
        write_secret(tmp_path / ".secret", "value", error=_SecretError, what="test secret")
    assert list(tmp_path.glob(".secret*")) == []


def test_write_mkdir_failure_is_the_callers_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
        raise OSError("simulated mkdir failure")

    monkeypatch.setattr("pathlib.Path.mkdir", failing_mkdir)
    with pytest.raises(_SecretError, match="cannot write test secret"):
        write_secret(tmp_path / ".secret", "value", error=_SecretError, what="test secret")


def test_write_mkstemp_failure_is_the_callers_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_mkstemp(dir: str, prefix: str) -> tuple[int, str]:
        raise OSError("simulated mkstemp failure")

    monkeypatch.setattr("tempfile.mkstemp", failing_mkstemp)
    with pytest.raises(_SecretError, match="cannot write test secret"):
        write_secret(tmp_path / ".secret", "value", error=_SecretError, what="test secret")


def test_paired_devices_path_with_minimal_config() -> None:
    cfg = MagicMock()
    cfg.index.index_dir = "/path/to/index"
    assert paired_devices_path(cfg) == Path("/path/to/index") / ".paired-devices.sqlite3"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def test_file_is_private_on_windows(tmp_path: Path) -> None:
    import subprocess

    path = tmp_path / ".secret"
    _load(path)
    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True)
    user = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    entries = [line.replace(str(path), "").strip() for line in listing.stdout.splitlines()]
    entries = [line for line in entries if line and ":" in line and "processed" not in line]
    # One entry, for this user, full control and not inherited ("(I)").
    assert [line.lower() for line in entries] == [f"{user.lower()}:(f)"]
