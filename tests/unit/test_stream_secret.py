"""Stream secret storage and checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from autodj.stream_secret import StreamSecret, StreamSecretError


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
