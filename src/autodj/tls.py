"""Pick up a renewed TLS certificate without restarting the server.

Certificate tools such as certbot replace the certificate and key files
every couple of months.  :class:`CertificateReloader` checks the files'
modification times every few minutes and, when they change, loads them into
the ``SSLContext`` uvicorn is already serving with.  New connections then
get the new certificate; open connections keep the one they started with.
"""

from __future__ import annotations

import logging
import shutil
import ssl
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from autodj.secret_files import write_private_file

if TYPE_CHECKING:
    from autodj.config import ServerConfig

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300.0


def certificate_date_warning(before: datetime, after: datetime) -> str:
    """Dates warn without closing the HTTPS route used to renew the certificate."""
    now = datetime.now(UTC)
    if now >= after:
        return "The HTTPS certificate has expired. HTTPS remains enabled; renew the certificate in Secure connections."
    if now < before:
        return "The HTTPS certificate is not yet valid. HTTPS remains enabled; check the clock or replace the certificate in Secure connections."
    return ""


def tls_date_warning(server: ServerConfig) -> str:
    """Describe a configured certificate's date warning after pair validation."""
    from cryptography import x509

    assert server.ssl_certfile is not None
    cert = x509.load_pem_x509_certificate(Path(server.ssl_certfile).read_bytes())
    return certificate_date_warning(cert.not_valid_before_utc, cert.not_valid_after_utc)


def validate_tls_pair(server: ServerConfig) -> ssl.SSLContext | None:
    """Load configured HTTPS read-only, refusing unusable pairs without HTTP fallback."""
    if server.ssl_certfile is None and server.ssl_keyfile is None:
        return None
    if server.ssl_certfile is None or server.ssl_keyfile is None:
        raise ValueError("HTTPS certificate and key must be set together")
    from cryptography import x509

    try:
        x509.load_pem_x509_certificate(Path(server.ssl_certfile).read_bytes())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Encrypted keys cannot prompt on an unattended server or in Doctor.
        context.load_cert_chain(server.ssl_certfile, server.ssl_keyfile, password=lambda: b"")
        return context
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"HTTPS certificate/key cannot be used: {exc}. "
            "Restore a valid certificate and matching unencrypted key before starting AutoDJ."
        ) from exc


class CertificateReloader:
    """Reload a certificate and key into a live server ``SSLContext`` when they change.

    Args:
        context: The context the server hands to every new connection.
        certfile: Certificate chain (PEM) the context was loaded from.
        keyfile: Private key (PEM) the context was loaded from.
        interval: Seconds between checks in the background thread.
    """

    def __init__(
        self,
        context: ssl.SSLContext,
        certfile: str | Path,
        keyfile: str | Path,
        *,
        interval: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self.context = context
        self.certfile = Path(certfile)
        self.keyfile = Path(keyfile)
        self.interval = interval
        self._seen = self._stamp()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def _stamp(self) -> tuple[int, int, int, int] | None:
        """Return both files' modification time and size, or ``None`` if one is missing."""
        try:
            cert = self.certfile.stat()
            key = self.keyfile.stat()
        except OSError:
            return None
        return (cert.st_mtime_ns, cert.st_size, key.st_mtime_ns, key.st_size)

    def check(self) -> bool:
        """Load the files into the context if they changed since the last check.

        The pair is copied first and test-loaded into a scratch context, so a
        half-copied or mismatched pair is refused with a warning and the
        server keeps its current certificate.  A later change is tried again.
        Files that could not be read (the renewal tool still holds them, a
        share blipped) are tried again at the next check, changed or not.

        Returns:
            Whether a new certificate was loaded.
        """
        with self._lock:
            return self._check()

    def _check(self) -> bool:
        stamp = self._stamp()
        if stamp is None or stamp == self._seen:
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="autodj-tls-") as scratch:
                cert = Path(scratch) / "cert.pem"
                key = Path(scratch) / "key.pem"
                # Create protected targets before copyfile writes private material.
                write_private_file(cert, b"")
                write_private_file(key, b"")
                shutil.copyfile(self.certfile, cert)
                shutil.copyfile(self.keyfile, key)
                self._seen = stamp  # read: only a further change is tried again
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
                self.context.load_cert_chain(cert, key)
        except (OSError, ssl.SSLError) as exc:
            logger.warning(
                "TLS certificate files changed but could not be loaded (%s); "
                "still serving the previous certificate",
                exc,
            )
            return False
        logger.info("Loaded the renewed TLS certificate from %s", self.certfile)
        return True

    def replace(self, certfile: Path, keyfile: Path) -> None:
        """Select a validated saved pair for new connections and future renewal checks."""
        with self._lock:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certfile, keyfile)
            try:
                self.context.load_cert_chain(certfile, keyfile)
            except (OSError, ssl.SSLError):
                self.context.load_cert_chain(self.certfile, self.keyfile)
                raise
            self.certfile, self.keyfile = Path(certfile), Path(keyfile)
            self._seen = self._stamp()

    def start(self) -> None:
        """Check in a daemon thread every ``interval`` seconds until :meth:`stop`."""
        self._thread = threading.Thread(target=self._run, name="autodj-tls-reload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.check()
