"""Stream secret storage and checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autodj.stream_secret import (
    _MAX_GENERATION_ATTEMPTS,
    StreamSecret,
    StreamSecretError,
    _new_value,
    paired_devices_path,
    stream_secret_path,
)


def test_created_on_first_load(tmp_path: Path) -> None:
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    assert len(secret.value) == 43
    assert (tmp_path / ".stream-secret").read_text(encoding="ascii").strip() == secret.value


def test_reload_returns_same_value(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    first = StreamSecret.load_or_create(path)
    assert StreamSecret.load_or_create(path).value == first.value


@pytest.mark.parametrize(
    "content", ["", "   \n", "short", "has space in it and is long enough to pass length"]
)
def test_invalid_content_is_replaced(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".stream-secret"
    path.write_text(content, encoding="ascii")
    secret = StreamSecret.load_or_create(path)
    assert len(secret.value) == 43
    assert secret.value != content.strip()


def test_never_equals_forbidden_value(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    first = StreamSecret.load_or_create(path)
    again = StreamSecret.load_or_create(path, forbidden=first.value)
    assert again.value != first.value


def test_matches_is_exact(tmp_path: Path) -> None:
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    assert secret.matches(secret.value)
    assert not secret.matches("")
    assert not secret.matches(secret.value[:-1])
    assert not secret.matches(secret.value + "x")


def test_rotate_invalidates_old(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    secret = StreamSecret.load_or_create(path)
    old = secret.value
    new = secret.rotate()
    assert new != old and not secret.matches(old) and secret.matches(new)
    assert StreamSecret.load_or_create(path).value == new


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_is_private(tmp_path: Path) -> None:
    StreamSecret.load_or_create(tmp_path / ".stream-secret")
    mode = stat.S_IMODE((tmp_path / ".stream-secret").stat().st_mode)
    assert mode == 0o600


def test_unreadable_file_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    path.mkdir()
    with pytest.raises(StreamSecretError):
        StreamSecret.load_or_create(path)


def test_matches_rejects_lone_surrogate(tmp_path: Path) -> None:
    """Test that matches() returns False for untrusted input with lone surrogates."""
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    assert not secret.matches("\ud800")


def test_matches_accepts_non_ascii(tmp_path: Path) -> None:
    """Test that matches() works with non-ASCII but valid UTF-8 strings."""
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    assert not secret.matches("ñoño")


def test_write_cleanup_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that write failure cleans up the temp file."""
    path = tmp_path / ".stream-secret"
    secret = StreamSecret.load_or_create(path)

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr("os.replace", failing_replace)

    with pytest.raises(StreamSecretError):
        secret._write()

    # Verify no temp files left behind
    temp_files = list(tmp_path.glob(".stream-secret.*"))
    assert len(temp_files) == 0


def test_write_mkdir_failure_is_stream_secret_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that mkdir failure during write raises StreamSecretError."""
    path = tmp_path / ".stream-secret"
    secret = StreamSecret(path, "test" * 11)  # 44 chars, will be normalized to 43

    def failing_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
        raise OSError("simulated mkdir failure")

    monkeypatch.setattr("pathlib.Path.mkdir", failing_mkdir)

    with pytest.raises(StreamSecretError, match="cannot write stream secret"):
        secret._write()


def test_write_mkstemp_failure_is_stream_secret_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that mkstemp failure during write raises StreamSecretError."""
    path = tmp_path / ".stream-secret"
    secret = StreamSecret(path, "test" * 11)

    def failing_mkstemp(dir: str, prefix: str) -> tuple[int, str]:
        raise OSError("simulated mkstemp failure")

    monkeypatch.setattr("tempfile.mkstemp", failing_mkstemp)

    with pytest.raises(StreamSecretError, match="cannot write stream secret"):
        secret._write()


def test_paired_devices_path_with_minimal_config() -> None:
    """Test paired_devices_path with a minimal mock config."""
    cfg = MagicMock()
    cfg.index.index_dir = "/path/to/index"
    result = paired_devices_path(cfg)
    assert result == Path("/path/to/index") / ".paired-devices.sqlite3"


def test_stream_secret_path_with_minimal_config() -> None:
    """Test stream_secret_path with a minimal mock config."""
    cfg = MagicMock()
    cfg.index.index_dir = "/path/to/index"
    result = stream_secret_path(cfg)
    assert result == Path("/path/to/index") / ".stream-secret"


def test_new_value_bounded_and_raises_when_always_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generator that always returns the forbidden value must not spin forever."""
    forbidden = "x" * 43
    calls = 0

    def fake_token_urlsafe(nbytes: int) -> str:
        nonlocal calls
        calls += 1
        return forbidden

    monkeypatch.setattr("autodj.stream_secret.secrets.token_urlsafe", fake_token_urlsafe)

    with pytest.raises(StreamSecretError, match="could not generate a stream secret"):
        _new_value(forbidden)
    assert calls == _MAX_GENERATION_ATTEMPTS


def test_new_value_retries_once_after_a_forbidden_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """One forbidden draw is retried and a later good value is accepted."""
    forbidden = "x" * 43
    good = "y" * 43
    responses = iter([forbidden, good])

    monkeypatch.setattr(
        "autodj.stream_secret.secrets.token_urlsafe", lambda nbytes: next(responses)
    )

    assert _new_value(forbidden) == good
