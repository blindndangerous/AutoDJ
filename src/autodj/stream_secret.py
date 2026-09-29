"""Secret files under ``<index_dir>``: the radio stream's listen-only secret.

Also holds the paths of the paired-devices database and the saved access
token, and the private-file helpers :mod:`autodj.lan` uses for the token.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from autodj.config import AutoDJConfig

_VALID = re.compile(r"^[A-Za-z0-9_-]{43}$")


class StreamSecretError(Exception):
    """The stream secret file exists but cannot be read or written."""


def paired_devices_path(cfg: AutoDJConfig) -> Path:
    """Return the paired-devices database path."""
    return Path(cfg.index.index_dir) / ".paired-devices.sqlite3"


def stream_secret_path(cfg: AutoDJConfig) -> Path:
    """Return the stream secret file path, beside the paired-devices database."""
    return Path(cfg.index.index_dir) / ".stream-secret"


def access_token_path(cfg: AutoDJConfig) -> Path:
    """Return the saved LAN access token path, beside the paired-devices database."""
    return Path(cfg.index.index_dir) / ".access-token"


def write_private_file(path: Path, text: str) -> None:
    """Write *text* to *path* atomically, readable only by the owner (0600).

    Creates parent directories if needed, writes to a temporary file in the
    same directory first, then renames it over *path* so readers never see a
    partial file.

    Args:
        path: Destination file.
        text: ASCII content to store.

    Raises:
        OSError: The file could not be written; no temporary file is left.
    """
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)
        raise


def read_secret(
    path: Path, *, valid: Callable[[str], bool], error: type[Exception], what: str
) -> str | None:
    """Return the secret saved at *path*, or ``None`` if it is missing or not *valid*.

    Raises:
        error: The file exists but cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise error(f"cannot read {what} at {path}: {exc}") from exc
    return text if valid(text) else None


def write_secret(path: Path, value: str, *, error: type[Exception], what: str) -> None:
    """Save *value* to *path* with :func:`write_private_file`.

    Raises:
        error: The file cannot be written.
    """
    try:
        write_private_file(path, value + "\n")
    except OSError as exc:
        raise error(f"cannot write {what} at {path}: {exc}") from exc


def load_or_create_secret(
    path: Path, *, valid: Callable[[str], bool], error: type[Exception], what: str
) -> str:
    """Return the secret saved at *path*, creating or replacing it when missing or invalid.

    Raises:
        error: The file cannot be read or written.
    """
    value = read_secret(path, valid=valid, error=error, what=what)
    if value is None:
        value = secrets.token_urlsafe(32)
        write_secret(path, value, error=error, what=what)
    return value


class StreamSecret:
    """A 43-character URL-safe secret stored in one private file."""

    def __init__(self, path: Path, value: str) -> None:
        """Wrap an already validated *value* stored at *path*."""
        self._path = path
        self.value = value

    @classmethod
    def load_or_create(cls, path: Path, *, forbidden: str | None = None) -> StreamSecret:
        """Load the secret, creating or replacing it when missing or invalid.

        Args:
            path: Secret file path.
            forbidden: A value the secret must never equal (the access token,
                or a value being replaced).

        Raises:
            StreamSecretError: If the file cannot be read or written.
        """
        value = load_or_create_secret(
            path,
            valid=lambda text: bool(_VALID.match(text)) and text != forbidden,
            error=StreamSecretError,
            what="stream secret",
        )
        return cls(path, value)

    def matches(self, candidate: str) -> bool:
        """Return whether *candidate* equals the secret, in constant time.

        Returns False if *candidate* cannot be encoded as UTF-8 (e.g., contains
        lone surrogates), preventing UnicodeEncodeError on untrusted input.
        """
        try:
            candidate_bytes = candidate.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(candidate_bytes, self.value.encode("ascii"))

    def rotate(self) -> str:
        """Replace the secret with a new one and return it.

        Raises:
            StreamSecretError: The new secret could not be saved; the old
                one stays in force (in memory and on disk).
        """
        value = secrets.token_urlsafe(32)
        write_secret(self._path, value, error=StreamSecretError, what="stream secret")
        self.value = value
        return value
