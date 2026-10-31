"""Server-owned HTTPS setup, restricted to a direct local browser."""

from __future__ import annotations

import ipaddress
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from cryptography import x509
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from autodj.certificates import CertificateError, CertificateStore
from autodj.config import AutoDJConfig
from autodj.security import peer_address

PREFIX = "/api/server/certificates"
BODY_LIMITS: dict[str, Callable[[], int]] = {
    PREFIX: lambda: 16 * 1024,
    PREFIX + "/import": lambda: 270 * 1024,
    PREFIX + "/{certificate_id}/activate": lambda: 1024,
}


class CreateCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    hosts: list[Annotated[str, Field(min_length=1, max_length=253)]] = Field(
        min_length=1, max_length=100
    )


class ImportCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    certificate: str = Field(max_length=128 * 1024)
    private_key: str = Field(max_length=128 * 1024, repr=False)


def direct_local(request: Request) -> bool:
    """Require both a local socket and local URL, without proxy headers."""
    headers = {key.lower() for key, _ in request.scope.get("headers", ())}
    if any(key.startswith(b"x-forwarded-") for key in headers) or headers.intersection(
        {b"forwarded", b"x-real-ip", b"x-autodj-proxy-auth"}
    ):
        return False
    try:
        local_peer = ipaddress.ip_address(peer_address(request.scope)).is_loopback
    except ValueError:
        return False
    host = request.url.hostname or ""
    try:
        local_host = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local_host = host.lower() == "localhost"
    return local_peer and local_host


def install_certificates_api(app: FastAPI, cfg: AutoDJConfig, *, https: bool) -> None:
    store = CertificateStore(Path(cfg.index.index_dir) / ".tls")
    app.state.certificate_store = store
    configured = (cfg.server.ssl_certfile, cfg.server.ssl_keyfile)
    configured_paths = (
        (Path(configured[0]), Path(configured[1])) if configured[0] and configured[1] else None
    )
    initial_active = store.active_id
    overridden = bool(
        initial_active and configured_paths and store.paths(initial_active) != configured_paths
    )
    # The running pair is distinct from a selection awaiting the first HTTPS restart.
    running: list[tuple[Path, Path] | None] = [configured_paths]

    def local(request: Request) -> bool:
        return cfg.server.auth_mode == "native" and direct_local(request)

    def require_local(request: Request) -> None:
        if not local(request):
            raise HTTPException(403, "Open AutoDJ at localhost on the host to manage certificates.")
        if overridden:
            raise HTTPException(
                409, "Restart AutoDJ without the TLS command-line flags to use saved certificates."
            )

    def read_trusted(request: Request) -> None:
        if not local(request) and not (https and request.url.scheme == "https"):
            raise HTTPException(403, "Download the certificate on the host or over trusted HTTPS.")

    def status(request: Request) -> dict:
        entries = store.list()
        active = (
            next(
                (item["id"] for item in entries if store.paths(item["id"]) == configured_paths),
                None,
            )
            if overridden
            else store.active_id
        )
        selected = store.paths(active) if active else None
        for item in entries:
            item["managed"] = True
            item["active"] = item["id"] == active
        # Existing externally renewed pairs remain externally owned.
        if (
            configured[0]
            and configured[1]
            and not any(store.paths(item["id"]) == configured_paths for item in entries)
        ):
            try:
                cert = x509.load_pem_x509_certificate(Path(configured[0]).read_bytes())
                external = store.describe(cert, "Configured certificate", "configured")
                external.update(managed=False, active=active is None)
                entries.insert(0, external)
            except (OSError, ValueError):
                raise HTTPException(503, "The configured certificate cannot be read.") from None
        hosts = sorted(set(cfg.server.effective_allowed_hosts()))
        active_row = next((item for item in entries if item["active"]), None)
        names = active_row["hosts"] if active_row else hosts
        urls = []
        for name in names:
            if name not in hosts or "*" in name:
                continue
            authority = f"[{name}]" if ":" in name else name
            urls.append(f"https://{authority}:{cfg.server.port}")
        return {
            "certificates": entries,
            "active_id": active or ("configured" if active_row else None),
            "local": local(request),
            "can_manage": local(request) and not overridden,
            "management_note": "Restart AutoDJ without the TLS command-line flags to use saved certificates."
            if overridden
            else "",
            "https": https,
            "restart_required": selected is not None and (not https or selected != running[0]),
            "hosts": hosts,
            "urls": urls,
            "generation_available": True,
        }

    @app.get(PREFIX)
    def inventory(request: Request) -> dict:
        try:
            return status(request)
        except (CertificateError, OSError):
            raise HTTPException(503, "The certificate inventory could not be read.") from None

    @app.post(PREFIX, status_code=201)
    def create(request: Request, body: CreateCertificate) -> dict:
        require_local(request)
        try:
            return store.create(body.name, body.hosts)
        except CertificateError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(503, "The certificate could not be saved.") from None

    @app.post(PREFIX + "/import", status_code=201)
    def import_pair(request: Request, body: ImportCertificate) -> dict:
        require_local(request)
        try:
            return store.import_pair(
                body.name, body.certificate.encode(), body.private_key.encode()
            )
        except CertificateError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(503, "The certificate could not be saved.") from None

    @app.post(PREFIX + "/{certificate_id}/activate")
    def activate(request: Request, certificate_id: str) -> dict:
        require_local(request)
        try:
            # At least the local management address must keep working. Additional
            # SANs are shown explicitly; a certificate need not cover every interface.
            store.validate_hosts(certificate_id, [request.url.hostname or "localhost"])
            reloader = getattr(app.state, "certificate_reloader", None)
            store.activate(certificate_id, reloader.replace if reloader else None)
            if reloader:
                running[0] = store.paths(certificate_id)
            return status(request)
        except CertificateError as exc:
            raise HTTPException(400, str(exc)) from None
        except (OSError, ssl.SSLError):
            raise HTTPException(503, "The certificate could not be activated. Try again.") from None

    @app.delete(PREFIX + "/{certificate_id}", status_code=204)
    def delete(request: Request, certificate_id: str) -> Response:
        require_local(request)
        try:
            # Also protect a still-running pair during a pending restart.
            if store.paths(certificate_id) == running[0]:
                raise CertificateError("Restart AutoDJ before deleting the previous certificate.")
            store.delete(certificate_id)
        except CertificateError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(503, "The certificate could not be deleted.") from None
        return Response(status_code=204)

    @app.get(PREFIX + "/{certificate_id}/download")
    def download(request: Request, certificate_id: str) -> Response:
        read_trusted(request)
        try:
            pem = (
                Path(configured[0]).read_bytes()
                if certificate_id == "configured" and configured[0]
                else store.public_pem(certificate_id)
            )
        except (CertificateError, OSError):
            raise HTTPException(404, "Certificate not found.") from None
        # Serialize certificates only, even if an external PEM file contains a key.
        from cryptography.hazmat.primitives.serialization import Encoding

        try:
            public = x509.load_pem_x509_certificate(pem).public_bytes(Encoding.PEM)
        except ValueError:
            raise HTTPException(503, "The certificate could not be read.") from None
        return Response(
            public,
            media_type="application/x-pem-file",
            headers={
                "Content-Disposition": 'attachment; filename="autodj.crt"',
                "Cache-Control": "no-store",
            },
        )
