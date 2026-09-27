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
_MAX_GENERATION_ATTEMPTS = 8


class StreamSecretError(Exception):
    """The stream secret file exists but cannot be read or written."""


def paired_devices_path(cfg: AutoDJConfig) -> Path:
    """Return the paired-devices database path."""
    return Path(cfg.index.index_dir) / ".paired-devices.sqlite3"


def stream_secret_path(cfg: AutoDJConfig) -> Path:
    """Return the stream secret file path, beside the paired-devices database."""
    return Path(cfg.index.index_dir) / ".stream-secret"


def _new_value(forbidden: str | None) -> str:
    """Generate a 43-character URL-safe random value, excluding a forbidden value if given.

    Args:
        forbidden: A value to exclude from the generated secret.

    Returns:
        A 43-character URL-safe random string.

    Raises:
        StreamSecretError: No acceptable value was generated within a
            bounded number of attempts. In practice a fresh
            :func:`secrets.token_urlsafe` value always matches ``_VALID``
            and only collides with *forbidden* with astronomically low
            probability, so this only guards against this loop ever
            spinning forever (e.g. a broken CSPRNG).
    """
    for _ in range(_MAX_GENERATION_ATTEMPTS):
        value = secrets.token_urlsafe(32)
        if _VALID.match(value) and value != forbidden:
            return value
    raise StreamSecretError("could not generate a stream secret")


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
        secret = cls(path, _new_value(forbidden))
        secret._write()
        return secret

    def _write(self) -> None:
        """Write the current secret value atomically to the file.

        Creates parent directories if needed, writes to a temporary file first,
        then atomically renames it to avoid partial writes.

        Raises:
            StreamSecretError: If the file cannot be written.
        """
        tmp = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".stream-secret.")
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(self.value + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)
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
        """Replace the secret with a new one and return it."""
        self.value = _new_value(self.value)
        self._write()
        return self.value
