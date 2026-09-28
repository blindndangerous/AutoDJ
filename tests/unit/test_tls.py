"""Renewed certificate files reach new TLS connections without a restart."""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from autodj.tls import CertificateReloader

OPENSSL = shutil.which("openssl")
pytestmark = pytest.mark.skipif(OPENSSL is None, reason="needs the openssl command")


def _make_pair(folder: Path, name: str) -> tuple[Path, Path]:
    """Write a throwaway self-signed certificate and key; return their paths."""
    cert = folder / f"{name}.pem"
    key = folder / f"{name}-key.pem"
    subprocess.run(
        [
            str(OPENSSL),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={name}",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _served_certificate(context: ssl.SSLContext) -> bytes:
    """Complete one handshake against *context* and return the certificate it presented."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def accept() -> None:
            conn, _ = listener.accept()
            try:
                with context.wrap_socket(conn, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                conn.close()

        server = threading.Thread(target=accept, daemon=True)
        server.start()
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE
        with (
            socket.create_connection(listener.getsockname(), timeout=10) as raw,
            client_context.wrap_socket(raw) as tls,
        ):
            served = tls.getpeercert(binary_form=True)
        server.join(timeout=10)
    assert served is not None
    return served


def _der(cert: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))


def _install(source: tuple[Path, Path], live: tuple[Path, Path], bump_ns: int) -> None:
    """Copy a pair over the live files with a strictly newer modification time."""
    for src, dst in zip(source, live, strict=True):
        shutil.copyfile(src, dst)
        stamp = os.stat(dst).st_mtime_ns + bump_ns
        os.utime(dst, ns=(stamp, stamp))


@pytest.fixture
def pairs(tmp_path: Path) -> tuple[tuple[Path, Path], tuple[Path, Path], tuple[Path, Path]]:
    """Return (live pair, first certificate, renewed certificate)."""
    first = _make_pair(tmp_path, "first")
    renewed = _make_pair(tmp_path, "renewed")
    live = (tmp_path / "live.pem", tmp_path / "live-key.pem")
    _install(first, live, 0)
    return live, first, renewed


def test_renewed_certificate_is_served_to_new_connections(pairs) -> None:
    live, first, renewed = pairs
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*live)
    reloader = CertificateReloader(context, *live)

    assert reloader.check() is False
    assert _served_certificate(context) == _der(first[0])

    _install(renewed, live, 1_000_000_000)

    assert reloader.check() is True
    assert _served_certificate(context) == _der(renewed[0])
    assert reloader.check() is False


def test_half_copied_pair_keeps_serving_the_old_certificate(pairs) -> None:
    live, first, renewed = pairs
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*live)
    reloader = CertificateReloader(context, *live)

    # The renewal hook has copied the new certificate but not yet its key.
    _install((renewed[0],), (live[0],), 1_000_000_000)
    assert reloader.check() is False
    assert _served_certificate(context) == _der(first[0])

    _install((renewed[1],), (live[1],), 2_000_000_000)
    assert reloader.check() is True
    assert _served_certificate(context) == _der(renewed[0])


def test_missing_files_are_skipped_until_they_return(pairs) -> None:
    live, _first, renewed = pairs
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*live)
    reloader = CertificateReloader(context, *live)
    live[1].unlink()

    assert reloader.check() is False

    _install(renewed, live, 1_000_000_000)
    assert reloader.check() is True


async def _asgi_app(scope, receive, send) -> None:  # pragma: no cover - never called
    raise AssertionError("not served")


def test_uvicorn_serves_from_the_context_the_background_thread_reloads(pairs) -> None:
    import uvicorn

    live, _first, renewed = pairs
    reloaders: list[CertificateReloader] = []

    def factory(_config: object, default_factory) -> ssl.SSLContext:
        context = default_factory()
        reloader = CertificateReloader(context, *live, interval=0.01)
        reloader.start()
        reloaders.append(reloader)
        return context

    config = uvicorn.Config(
        _asgi_app,
        ssl_certfile=str(live[0]),
        ssl_keyfile=str(live[1]),
        ssl_context_factory=factory,
    )
    config.load()
    try:
        assert config.ssl is reloaders[0].context
        _install(renewed, live, 1_000_000_000)
        wanted = _der(renewed[0])
        deadline = threading.Event()
        for _ in range(500):
            if _served_certificate(config.ssl) == wanted:
                break
            deadline.wait(0.01)
        assert _served_certificate(config.ssl) == wanted
    finally:
        for reloader in reloaders:
            reloader.stop()
