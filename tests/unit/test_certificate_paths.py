"""Certificate request identifiers and resolved files stay inside the private store."""

import os
import subprocess
from pathlib import Path

import pytest

from autodj.certificates import CertificateError, CertificateStore


@pytest.mark.parametrize(
    "ident", ["../outside", "..\\outside", "/outside", "C:\\outside", "a" * 32 + "\n"]
)
@pytest.mark.parametrize("operation", ["paths", "public_pem", "activate", "delete"])
def test_request_identifier_cannot_leave_store(tmp_path, ident, operation):
    store = CertificateStore(tmp_path / ".tls")
    with pytest.raises(CertificateError, match="unknown certificate"):
        getattr(store, operation)(ident)
    assert not store.root.exists()


@pytest.mark.parametrize("component", ["directory", "cert.pem", "key.pem"])
@pytest.mark.parametrize("operation", ["paths", "public_pem", "activate", "delete"])
def test_resolved_certificate_path_cannot_leave_store(tmp_path, monkeypatch, component, operation):
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("Local", ["localhost"])
    directory = store.root / item["id"]
    selected = directory if component == "directory" else directory / component
    outside = tmp_path / ".tls-other" / component
    outside.parent.mkdir(exist_ok=True)
    outside.write_bytes(b"outside data")
    resolve = Path.resolve

    # Model a filesystem redirect even when link metadata does not identify it.
    def redirected(path, *args, **kwargs):
        return outside if path == selected else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(CertificateError, match="unsafe"):
        getattr(store, operation)(item["id"])
    assert outside.read_bytes() == b"outside data"
    assert directory.is_dir()


@pytest.mark.parametrize("component", ["cert.pem", "key.pem"])
@pytest.mark.parametrize("operation", ["paths", "public_pem", "activate", "delete"])
def test_hardlinked_pem_is_refused(tmp_path, component, operation):
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("Local", ["localhost"])
    pem = store.root / item["id"] / component
    outside = tmp_path / component
    pem.rename(outside)
    pem.hardlink_to(outside)
    original = outside.read_bytes()
    assert pem.stat().st_nlink == 2

    with pytest.raises(CertificateError, match="unsafe"):
        getattr(store, operation)(item["id"])
    assert outside.read_bytes() == original
    assert pem.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
@pytest.mark.parametrize("operation", ["paths", "public_pem", "activate", "delete"])
def test_directory_junction_cannot_leave_store(tmp_path, operation):
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("Local", ["localhost"])
    directory = store.root / item["id"]
    outside = tmp_path / "outside"
    directory.rename(outside)
    originals = {p.name: p.read_bytes() for p in outside.iterdir()}
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(directory), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        assert directory.is_junction()
        assert directory.resolve() == outside.resolve()
        with pytest.raises(CertificateError, match="unsafe"):
            getattr(store, operation)(item["id"])
        assert {p.name: p.read_bytes() for p in outside.iterdir()} == originals
    finally:
        directory.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive paths")
def test_case_variant_root_keeps_certificate_access(tmp_path):
    store = CertificateStore(tmp_path / ".tls")
    item = store.create("Local", ["localhost"])
    variant = CertificateStore(store.root.with_name(".TLS"))
    assert variant.root.samefile(store.root)
    assert variant.paths(item["id"]) == store.paths(item["id"])
    assert variant.public_pem(item["id"]) == store.public_pem(item["id"])
    variant.activate(item["id"])
