"""Certificate management keeps host authority and public downloads separate."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodj.certificates import CertificateStore
from autodj.config import load_config, validate_server_exposure
from tests._layout import point_index
from tests._server import create_app

BASE = "/api/server/certificates"


def client_for(bridge, tmp_path, *, peer="127.0.0.1", https=False):
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.server.allowed_hosts = ["testserver", "localhost"]
    bridge.player._cfg.server.allowed_origins = [
        "http://testserver",
        f"{'https' if https else 'http'}://localhost",
    ]
    app = create_app(bridge, secure_cookie=https)
    client = TestClient(
        app,
        base_url=f"{'https' if https else 'http'}://localhost",
        headers={
            "Host": "localhost",
            "Origin": f"{'https' if https else 'http'}://localhost",
        },
        client=(peer, 12345),
    )
    return client


def test_local_setup_persists_selection_and_download_contains_only_public_certificate(
    bridge, tmp_path
):
    """ADR-0019-01; ADR-0019-02."""
    client = client_for(bridge, tmp_path)
    before = client.get(BASE).json()
    assert before["local"] and not before["https"]
    result = client.post(BASE, json={"name": "Studio", "hosts": ["localhost", "radio.local"]})
    assert result.status_code == 201, result.text
    ident = result.json()["id"]
    assert client.get(BASE).json()["active_id"] is None
    activated = client.post(f"{BASE}/{ident}/activate")
    assert activated.status_code == 200, activated.text
    assert activated.json()["restart_required"]
    assert activated.json()["active_id"] == ident
    assert client.delete(f"{BASE}/{ident}").status_code == 400
    public = client.get(f"{BASE}/{ident}/download")
    assert public.status_code == 200
    assert b"BEGIN CERTIFICATE" in public.content
    assert b"PRIVATE KEY" not in public.content
    assert public.headers["cache-control"] == "no-store"
    config = tmp_path / "config.toml"
    config.write_text(f'[index]\nindex_dir = "{tmp_path.as_posix()}"\n', encoding="utf-8")
    loaded = load_config(str(config))
    validate_server_exposure(loaded.server)
    assert Path(loaded.server.ssl_certfile) == CertificateStore(tmp_path / ".tls").paths(ident)[0]


@pytest.mark.parametrize(
    "headers,peer",
    [
        ({}, "192.168.1.2"),
        ({"X-Forwarded-For": "192.168.1.2"}, "127.0.0.1"),
        ({"Forwarded": "for=192.168.1.2"}, "127.0.0.1"),
    ],
)
def test_remote_or_forwarded_requests_cannot_manage_or_bootstrap_trust(
    bridge, tmp_path, headers, peer
):
    """ADR-0019-03."""
    client = client_for(bridge, tmp_path, peer=peer)
    item = CertificateStore(tmp_path / ".tls").create("Host", ["localhost"])
    assert not client.get(BASE, headers=headers).json()["can_manage"]
    assert (
        client.post(BASE, json={"name": "Bad", "hosts": ["localhost"]}, headers=headers).status_code
        == 403
    )
    assert client.post(f"{BASE}/{item['id']}/activate", headers=headers).status_code == 403
    assert client.delete(f"{BASE}/{item['id']}", headers=headers).status_code == 403
    assert client.get(f"{BASE}/{item['id']}/download", headers=headers).status_code == 403


def test_https_remote_browser_can_download_public_certificate_but_not_manage(bridge, tmp_path):
    client = client_for(bridge, tmp_path, peer="192.168.1.2", https=True)
    item = CertificateStore(tmp_path / ".tls").create("Host", ["localhost"])
    assert client.get(f"{BASE}/{item['id']}/download").status_code == 200
    assert client.post(f"{BASE}/{item['id']}/activate").status_code == 403


def test_activation_rejects_missing_current_address_and_preserves_selection(bridge, tmp_path):
    client = client_for(bridge, tmp_path)
    store = CertificateStore(tmp_path / ".tls")
    old = store.create("Old", ["localhost"])
    store.activate(old["id"])
    new = store.create("Wrong address", ["other.local"])
    assert client.post(f"{BASE}/{new['id']}/activate").status_code == 400
    assert store.active_id == old["id"]


def test_failed_live_activation_preserves_previous_selection(bridge, tmp_path):
    client = client_for(bridge, tmp_path, https=True)
    store = CertificateStore(tmp_path / ".tls")
    old = store.create("Old", ["localhost"])
    store.activate(old["id"])
    new = store.create("New", ["localhost"])

    class BrokenReloader:
        def replace(self, *_):
            raise OSError("cannot read private key")

    client.app.state.certificate_reloader = BrokenReloader()
    response = client.post(f"{BASE}/{new['id']}/activate")
    assert response.status_code == 503
    assert store.active_id == old["id"]


def test_oversized_import_is_rejected_before_parsing_and_unpaired_access_is_denied(
    bridge, tmp_path
):
    client = client_for(bridge, tmp_path)
    response = client.post(
        BASE + "/import", content=b"x" * (280 * 1024), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    bridge.player._cfg.server.access_token = "test-certificate-token-with-more-than-32-bytes"
    paired = client_for(bridge, tmp_path)
    assert paired.get(BASE).status_code == 401
    assert paired.post(BASE, json={"name": "a", "hosts": ["localhost"]}).status_code == 401


def test_import_errors_do_not_echo_private_material(bridge, tmp_path):
    client = client_for(bridge, tmp_path)
    response = client.post(
        BASE + "/import", json={"name": "a", "certificate": "bad", "private_key": "SECRET" * 30000}
    )
    assert response.status_code in {413, 422}
    assert "SECRET" not in response.text
