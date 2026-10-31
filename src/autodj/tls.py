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
from pathlib import Path

from autodj.secret_files import write_private_file

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300.0


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
