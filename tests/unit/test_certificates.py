from __future__ import annotations

import os
import ssl
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from autodj.certificates import CertificateError, CertificateStore
from autodj.secret_files import _check_private_file


def test_create_persists_private_pair_metadata_and_active_selection(tmp_path: Path) -> None:
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("Home", ["radio.local", "192.168.1.8", "::1"])
    cert, key = store.paths(item["id"])
    assert item["hosts"] == ["192.168.1.8", "::1", "radio.local"]
    assert item["status"] == "valid"
    assert cert.exists() and key.exists()
    if os.name != "nt":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(cert.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.manifest.stat().st_mode) == 0o600
    store.activate(item["id"])
    assert store._load()["active"] == item["id"]
    assert store.public_pem(item["id"]).startswith(b"-----BEGIN CERTIFICATE-----")


def test_import_does_not_modify_external_pair_and_activation_rolls_back(tmp_path: Path) -> None:
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("External source", ["one.local"])
    cert, key = store.paths(item["id"])
    original_cert, original_key = cert.read_bytes(), key.read_bytes()
    source_cert, source_key = tmp_path / "external-cert.pem", tmp_path / "external-key.pem"
    source_cert.write_bytes(original_cert)
    source_key.write_bytes(original_key)
    copy = store.import_pair("copied", source_cert.read_bytes(), source_key.read_bytes())
    assert source_cert.read_bytes() == original_cert
    assert source_key.read_bytes() == original_key
    assert store.paths(copy["id"])[0].read_bytes() == original_cert
    with pytest.raises(RuntimeError):
        store.activate(copy["id"], lambda *_: (_ for _ in ()).throw(RuntimeError("apply failed")))
    assert store._load()["active"] is None


def test_rejects_mismatched_pair_oversize_and_unknown_hosts(tmp_path: Path) -> None:
    store = CertificateStore(tmp_path / ".tls")
    one = store.create("one", ["one.local"])
    two = store.create("two", ["two.local"])
    cert_a, _ = store.paths(one["id"])
    _, key_b = store.paths(two["id"])
    with pytest.raises(CertificateError, match="do not match"):
        store.import_pair("bad", cert_a.read_bytes(), key_b.read_bytes())
    with pytest.raises(CertificateError, match="128 KiB"):
        store.import_pair("large", b"x" * (128 * 1024 + 1), b"x")
    with pytest.raises(CertificateError, match="host names"):
        store.create("empty", [])
    with pytest.raises(CertificateError, match="host name"):
        store.create("bad host", ["bad host"])
    with pytest.raises(CertificateError, match="unknown certificate"):
        store.paths("../../etc/passwd")


def test_delete_only_inactive_and_inventory_is_bounded(tmp_path: Path, monkeypatch) -> None:
    store = CertificateStore(tmp_path / ".tls")
    items = [store.create(f"cert {i}", [f"host{i}.local"]) for i in range(2)]
    store.activate(items[0]["id"])
    with pytest.raises(CertificateError, match="active"):
        store.delete(items[0]["id"])
    store.delete(items[1]["id"])
    assert len(store.list()) == 1
    monkeypatch.setattr("autodj.certificates.MAX_CERTIFICATES", 1)
    with pytest.raises(CertificateError, match="full"):
        store.create("full", ["extra.local"])


def test_active_callback_gets_ssl_loadable_files(tmp_path: Path) -> None:
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("server", ["localhost"])
    seen = []

    def apply(cert: Path, key: Path) -> None:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        seen.append((cert, key))

    store.activate(item["id"], apply)
    assert seen == [store.paths(item["id"])]


def test_expired_import_is_rejected(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired.local")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=3))
        .not_valid_after(now - timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("expired.local")]), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(CertificateError, match="not currently valid"):
        CertificateStore(tmp_path / ".tls").import_pair("expired", cert_pem, key_pem)


def test_active_rollback_restores_prior_selection_and_host_coverage(tmp_path: Path) -> None:
    store = CertificateStore(tmp_path / ".tls")
    first = store.create("wildcard", ["*.example.local", "192.168.1.8"])
    second = store.create("next", ["next.local"])
    store.activate(first["id"])
    store.validate_hosts(first["id"], ["radio.example.local", "192.168.1.8"])
    with pytest.raises(CertificateError, match="does not cover"):
        store.validate_hosts(first["id"], ["a.b.example.local"])
    with pytest.raises(CertificateError, match="does not cover"):
        store.validate_hosts(first["id"], ["192.168.1.9"])
    with pytest.raises(RuntimeError):
        store.activate(second["id"], lambda *_: (_ for _ in ()).throw(RuntimeError("failed")))
    assert store.active_id == first["id"]


@pytest.mark.parametrize(
    "document",
    [
        '{"version":1,"active":null,"certificates":[{"id":[],"name":"x","hosts":[]}]}',
        '{"version":1,"active":null,"certificates":[null]}',
    ],
)
def test_malformed_manifest_is_reported_as_store_error(tmp_path: Path, document: str) -> None:
    root = tmp_path / ".tls"
    root.mkdir()
    (root / "inventory.json").write_text(document, encoding="utf-8")
    with pytest.raises(CertificateError, match="invalid"):
        CertificateStore(root).list()


def test_delete_refuses_symlinked_certificate_directory(tmp_path: Path) -> None:
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("source", ["source.local"])
    other = CertificateStore(tmp_path / "other")
    other_item = other.create("other", ["other.local"])
    cert_path, key_path = store.paths(item["id"])
    cert_path.unlink()
    key_path.unlink()
    directory = cert_path.parent
    directory.rmdir()
    try:
        directory.symlink_to(other.paths(other_item["id"])[0].parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    with pytest.raises(CertificateError, match="unsafe"):
        store.delete(item["id"])
    assert store.list()[0]["id"] == item["id"]


def test_saved_certificate_config_helper(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from autodj.certificates import apply_saved_certificate

    cfg = SimpleNamespace(
        index=SimpleNamespace(index_dir=tmp_path),
        server=SimpleNamespace(
            ssl_certfile="configured.pem",
            ssl_keyfile="configured-key.pem",
            allowed_origins=["http://saved.local:8080"],
        ),
    )
    assert apply_saved_certificate(cfg) is False
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("saved", ["saved.local"])
    store.activate(item["id"])
    assert apply_saved_certificate(cfg) is True
    assert cfg.server.allowed_origins == ["http://saved.local:8080", "https://saved.local:8080"]
    assert (cfg.server.ssl_certfile, cfg.server.ssl_keyfile) == tuple(
        map(str, store.paths(item["id"]))
    )


def test_private_file_access_policy_applies_to_both_pem_files(tmp_path: Path) -> None:
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("private", ["private.local"])
    for path in store.paths(item["id"]):
        with path.open("rb") as handle:
            _check_private_file(handle.fileno())


def test_failed_delete_keeps_a_retryable_inventory_entry(tmp_path, monkeypatch):
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("Retry deletion", ["localhost"])
    _, key = store.paths(item["id"])
    unlink = Path.unlink

    def fail_key(path, *args, **kwargs):
        if path == key:
            raise PermissionError("key in use")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_key)
        with pytest.raises(PermissionError):
            store.delete(item["id"])
    assert store.list()[0]["id"] == item["id"]
    store.delete(item["id"])
    assert store.list() == []
    assert not key.exists()
