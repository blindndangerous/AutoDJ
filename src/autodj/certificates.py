"""Private inventory of TLS certificate and key pairs."""

from __future__ import annotations

import ipaddress
import json
import re
import ssl
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from autodj.secret_files import write_private_file

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

MAX_CERTIFICATES = 20
MAX_PEM_SIZE = 128 * 1024
_ID = re.compile(r"^[0-9a-f]{32}$")


class CertificateError(ValueError):
    """A certificate could not be safely added or activated."""


class CertificateStore:
    """Store private certificate pairs and a durable active selection."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest = self.root / "inventory.json"
        self._lock = threading.RLock()

    @property
    def active_id(self) -> str | None:
        with self._lock:
            return self._load()["active"]

    def _safe_root(self) -> None:
        if self.root.is_symlink() or self.root.is_junction():
            raise CertificateError("certificate inventory path is unsafe")
        if self.manifest.is_symlink() or self.manifest.is_junction():
            raise CertificateError("certificate inventory path is unsafe")

    def _load(self) -> dict:
        self._safe_root()
        try:
            raw = self.manifest.read_text(encoding="utf-8")
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("certificates"), list)
            ):
                raise ValueError
            entries = data["certificates"]
            ids = [x.get("id") for x in entries if isinstance(x, dict)]
            if len(entries) > MAX_CERTIFICATES or len(ids) != len(entries):
                raise ValueError
            if any(not isinstance(i, str) or not _ID.fullmatch(i) for i in ids):
                raise ValueError
            if len(set(ids)) != len(ids):
                raise ValueError
            if "active" not in data:
                raise ValueError
            active = data["active"]
            if active is not None and (not isinstance(active, str) or active not in ids):
                raise ValueError
            for entry in entries:
                if (
                    not isinstance(entry.get("name"), str)
                    or not isinstance(entry.get("hosts"), list)
                    or not all(isinstance(h, str) for h in entry["hosts"])
                ):
                    raise ValueError
            return data
        except FileNotFoundError:
            return {"version": 1, "active": None, "certificates": []}
        except (OSError, ValueError, AttributeError):
            raise CertificateError("certificate inventory is unavailable or invalid") from None

    def _save(self, data: dict) -> None:
        write_private_file(self.manifest, json.dumps(data, indent=2) + "\n")

    @staticmethod
    def _metadata(cert: x509.Certificate, name: str, ident: str, cert_type: str) -> dict:
        try:
            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            hosts = sorted(
                {
                    *sans.get_values_for_type(x509.DNSName),
                    *map(str, sans.get_values_for_type(x509.IPAddress)),
                }
            )
        except x509.ExtensionNotFound:
            hosts = []
        before = cert.not_valid_before_utc
        after = cert.not_valid_after_utc
        return {
            "id": ident,
            "name": name,
            "hosts": hosts,
            "type": cert_type,
            "issued": before.isoformat(),
            "expires": after.isoformat(),
            "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
            "status": "expired" if datetime.now(UTC) > after else "valid",
        }

    @staticmethod
    def describe(cert: x509.Certificate, name: str, ident: str) -> dict:
        """Return public metadata for a parsed certificate and caller-supplied identity."""
        return CertificateStore._metadata(
            cert, name, ident, "Self-signed" if cert.issuer == cert.subject else "Imported"
        )

    def _store(self, name: str, cert_pem: bytes, key_pem: bytes, cert_type: str) -> dict:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 120
            or not name.isprintable()
        ):
            raise CertificateError("certificate name must be 1 to 120 characters")
        if len(cert_pem) > MAX_PEM_SIZE or len(key_pem) > MAX_PEM_SIZE:
            raise CertificateError("certificate files exceed the 128 KiB limit")
        try:
            cert = x509.load_pem_x509_certificate(cert_pem)
            key = serialization.load_pem_private_key(key_pem, password=None)
            public_format = serialization.PublicFormat.SubjectPublicKeyInfo
            encoding = serialization.Encoding.DER
            if cert.public_key().public_bytes(
                encoding, public_format
            ) != key.public_key().public_bytes(encoding, public_format):
                raise CertificateError("certificate and private key do not match")
            with tempfile.TemporaryDirectory(prefix="autodj-cert-") as scratch:
                cert_path, key_path = Path(scratch) / "cert.pem", Path(scratch) / "key.pem"
                write_private_file(cert_path, cert_pem)
                write_private_file(key_path, key_pem)
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_path, key_path)
        except CertificateError:
            raise
        except Exception:
            raise CertificateError("certificate or private key is invalid") from None
        if cert.not_valid_before_utc > datetime.now(
            UTC
        ) or cert.not_valid_after_utc <= datetime.now(UTC):
            raise CertificateError("certificate is not currently valid")
        try:
            try:
                eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
                if ExtendedKeyUsageOID.SERVER_AUTH not in eku:
                    raise CertificateError("certificate is not valid for server authentication")
            except x509.ExtensionNotFound:
                pass
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if not san:
                raise CertificateError("certificate has no host names")
        except x509.ExtensionNotFound:
            raise CertificateError("certificate is missing required TLS extensions") from None
        data = self._load()
        if len(data["certificates"]) >= MAX_CERTIFICATES:
            raise CertificateError("certificate inventory is full")
        ident = uuid.uuid4().hex
        item = self._metadata(cert, name.strip(), ident, cert_type)
        directory = self.root / ident
        if directory.exists() or directory.is_symlink() or directory.is_junction():
            raise CertificateError("certificate inventory path is unsafe")
        try:
            write_private_file(directory / "cert.pem", cert_pem)
            write_private_file(directory / "key.pem", key_pem)
        except Exception:
            (directory / "cert.pem").unlink(missing_ok=True)
            (directory / "key.pem").unlink(missing_ok=True)
            if directory.exists():
                directory.rmdir()
            raise
        data["certificates"].append(item)
        try:
            self._save(data)
        except Exception:
            (directory / "cert.pem").unlink(missing_ok=True)
            (directory / "key.pem").unlink(missing_ok=True)
            directory.rmdir()
            raise
        return dict(item)

    def create(self, name: str, hosts: list[str]) -> dict:
        if not hosts or len(hosts) > 100:
            raise CertificateError("one to 100 host names are required")
        now = datetime.now(UTC).replace(microsecond=0)
        normalized: list[x509.GeneralName] = []
        for host in hosts:
            if not isinstance(host, str) or not host.strip():
                raise CertificateError("host names must be non-empty")
            host = host.strip()
            try:
                normalized.append(x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                dns_host = host.rstrip(".")
                labels = dns_host.split(".")
                if labels[0] == "*":
                    labels = labels[1:]
                elif "*" in dns_host:
                    raise CertificateError("invalid host name") from None
                if (
                    not labels
                    or len(host) > 253
                    or any(
                        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                        for part in labels
                    )
                ):
                    raise CertificateError("invalid host name") from None
                normalized.append(x509.DNSName(dns_host.lower()))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(hosts[0])[:64])])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(normalized), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        with self._lock:
            return self._store(
                name,
                cert.public_bytes(serialization.Encoding.PEM),
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
                "Self-signed",
            )

    def import_pair(self, name: str, cert_pem: bytes, key_pem: bytes) -> dict:
        with self._lock:
            return self._store(name, cert_pem, key_pem, "Imported")

    def validate_hosts(self, ident: str, hosts: list[str]) -> None:
        """Require every requested DNS/IP host to be covered by the stored SAN."""
        cert_path, _ = self.paths(ident)
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except Exception:
            raise CertificateError("certificate has no usable host names") from None
        dns = san.get_values_for_type(x509.DNSName)
        ips = san.get_values_for_type(x509.IPAddress)
        for host in hosts:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                name = host.rstrip(".").lower()
                matched = any(
                    pattern.lower() == name
                    or (
                        pattern.startswith("*.")
                        and name.endswith(pattern[1:].lower())
                        and name.count(".") == pattern.count(".")
                    )
                    for pattern in dns
                )
            else:
                matched = address in ips
            if not matched:
                raise CertificateError("certificate does not cover all required hosts")

    def list(self) -> list[dict]:
        with self._lock:
            items = self._load()["certificates"]
            for item in items:
                try:
                    cert = x509.load_pem_x509_certificate(self.paths(item["id"])[0].read_bytes())
                    item.update(
                        self._metadata(cert, item["name"], item["id"], item.get("type", "Imported"))
                    )
                except Exception:
                    item["status"] = "invalid"
            return [dict(item) for item in items]

    def public_pem(self, ident: str) -> bytes:
        return self.paths(ident)[0].read_bytes()

    def paths(self, ident: str) -> tuple[Path, Path]:
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise CertificateError("unknown certificate")
        with self._lock:
            self._safe_root()
            directories = {x["id"]: self.root / x["id"] for x in self._load()["certificates"]}
            if ident not in directories:
                raise CertificateError("unknown certificate")
            root = self.root.resolve()
            directory = directories[ident]
            if (
                directory.is_symlink()
                or directory.is_junction()
                or (directory.exists() and not directory.is_dir())
                or not directory.resolve().is_relative_to(root)
            ):
                raise CertificateError("certificate inventory path is unsafe")
            paths = directory / "cert.pem", directory / "key.pem"
            if any(
                p.is_symlink()
                or p.is_junction()
                or not p.resolve().is_relative_to(root)
                or (p.exists() and p.stat().st_nlink != 1)
                for p in paths
            ):
                raise CertificateError("certificate inventory path is unsafe")
            return paths

    def delete(self, ident: str) -> None:
        with self._lock:
            if not isinstance(ident, str) or not _ID.fullmatch(ident):
                raise CertificateError("unknown certificate")
            cert_path, key_path = self.paths(ident)
            data = self._load()
            if ident == data.get("active"):
                raise CertificateError("active certificate cannot be deleted")
            entries = [x for x in data["certificates"] if x["id"] != ident]
            if len(entries) == len(data["certificates"]):
                raise CertificateError("unknown certificate")
            data["certificates"] = entries
            cert_path.unlink(missing_ok=True)
            key_path.unlink(missing_ok=True)
            directory = cert_path.parent
            if directory.exists():
                directory.rmdir()
            # Keep the row retryable if cleanup fails or the process stops midway.
            self._save(data)

    def activate(self, ident: str, apply: Callable[[Path, Path], None] | None = None) -> None:
        with self._lock:
            cert_path, key_path = self.paths(ident)
            try:
                with cert_path.open("rb") as f:
                    cert = x509.load_pem_x509_certificate(f.read())
                now = datetime.now(UTC)
                if cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now:
                    raise CertificateError("certificate is not currently valid")
                sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                if not sans:
                    raise CertificateError("certificate has no host names")
                with tempfile.TemporaryDirectory(prefix="autodj-activate-") as scratch:
                    cp, kp = Path(scratch) / "cert.pem", Path(scratch) / "key.pem"
                    write_private_file(cp, cert_path.read_bytes())
                    write_private_file(kp, key_path.read_bytes())
                    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cp, kp)
            except CertificateError:
                raise
            except Exception:
                raise CertificateError("certificate cannot be activated") from None
            data = self._load()
            item = next((item for item in data["certificates"] if item["id"] == ident), None)
            if item is None:
                raise CertificateError("unknown certificate")
            self.validate_hosts(ident, item["hosts"])
            previous = data.get("active")
            data["active"] = ident
            self._save(data)
            try:
                if apply is not None:
                    apply(cert_path, key_path)
            except Exception:
                data["active"] = previous
                self._save(data)
                raise


def apply_saved_certificate(cfg: AutoDJConfig) -> bool:
    """Apply the durable inventory selection to the loaded server configuration."""
    store = CertificateStore(Path(cfg.index.index_dir) / ".tls")
    ident = store.active_id
    if ident is None:
        return False
    cert_path, key_path = store.paths(ident)
    cfg.server.ssl_certfile = str(cert_path)
    cfg.server.ssl_keyfile = str(key_path)
    # Keep exact configured origins usable after switching the listener protocol.
    if cfg.server.allowed_origins is None:
        return True
    origins = cfg.server.allowed_origins or []
    cfg.server.allowed_origins = list(
        dict.fromkeys(
            [
                *origins,
                *("https://" + origin[7:] for origin in origins if origin.startswith("http://")),
            ]
        )
    )
    return True
