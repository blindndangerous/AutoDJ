"""Listen-only secret for the radio stream URL."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
            StreamSecretError: If the file exists but cannot be read.
        """
        try:
            text = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            text = ""
        except (OSError, UnicodeDecodeError) as exc:
            raise StreamSecretError(f"cannot read stream secret at {path}: {exc}") from exc
        if _VALID.match(text) and text != forbidden:
            return cls(path, text)
        secret = cls(path, secrets.token_urlsafe(32))
        secret._write()
        return secret

    def _write(self) -> None:
        """Write the current secret value atomically to the file.

        Raises:
            StreamSecretError: If the file cannot be written.
        """
        try:
            write_private_file(self._path, self.value + "\n")
        except OSError as exc:
            raise StreamSecretError(f"cannot write stream secret at {self._path}: {exc}") from exc

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
        old = self.value
        self.value = secrets.token_urlsafe(32)
        try:
            self._write()
        except StreamSecretError:
            self.value = old
            raise
        return self.value
