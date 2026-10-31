"""ACME shares the certificate routes' local/browser authorization boundary."""

import pytest

from autodj.certificates import CertificateStore
from tests.integration.test_certificates_api import BASE, client_for

PREFLIGHT = {
    "name": "Radio",
    "domain": "radio.example.com",
    "zone_id": "a" * 32,
    "api_token": "never-return-this-token",
    "environment": "staging",
}


def test_local_preflight_queues_work_and_does_not_echo_secrets(bridge, tmp_path):
    client = client_for(bridge, tmp_path)
    response = client.post(BASE + "/acme/preflight", json=PREFLIGHT)
    assert response.status_code == 202, response.text
    ident = response.json()["id"]
    assert response.json()["phase"] == "queued"
    assert PREFLIGHT["api_token"] not in response.text
    status = client.get(BASE + "/acme/" + ident)
    assert status.status_code == 200 and PREFLIGHT["api_token"] not in status.text
    assert CertificateStore(tmp_path / ".tls").list() == []
    assert client.app.state.acme._process is None
    assert client.post(BASE + "/acme/preflight", json=PREFLIGHT).status_code == 400


@pytest.mark.parametrize(
    "peer,headers",
    [
        ("192.168.1.20", {}),
        ("127.0.0.1", {"Forwarded": "for=127.0.0.1"}),
        ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}),
        ("127.0.0.1", {"Origin": "https://evil.example"}),
    ],
)
def test_acme_local_policy_rejects_remote_forwarded_and_bad_origin(bridge, tmp_path, peer, headers):
    """ADR-0019-10."""
    client = client_for(bridge, tmp_path, peer=peer)
    assert client.post(BASE + "/acme/preflight", json=PREFLIGHT, headers=headers).status_code == 403
    assert not (tmp_path / ".tls" / "acme").exists()


def test_acme_body_limits_and_validation_redact_token(bridge, tmp_path):
    client = client_for(bridge, tmp_path)
    result = client.post(BASE + "/acme/preflight", json={**PREFLIGHT, "api_token": "SECRET" * 1000})
    assert result.status_code in {413, 422} and "SECRET" not in result.text
    result = client.post(
        BASE + "/acme/preflight", content=b"x" * 17000, headers={"Content-Type": "application/json"}
    )
    assert result.status_code == 413


def test_acme_activation_requires_explicit_allowed_destination(bridge, tmp_path):
    client = client_for(bridge, tmp_path)
    store = CertificateStore(tmp_path / ".tls")
    source = store.create("Source", ["radio.example.com"])
    cert, key = store.paths(source["id"])
    row = store.save_acme("Radio", cert.read_bytes(), key.read_bytes(), "1" * 32)
    route = f"{BASE}/{row['id']}/activate"
    assert client.post(route).status_code == 400
    assert client.post(route, params={"hostname": "radio.example.com"}).status_code == 400
    bridge.player._cfg.server.allowed_hosts.append("radio.example.com")
    result = client.post(route, params={"hostname": "radio.example.com"})
    assert result.status_code == 200, result.text
    assert result.json()["active_id"] == row["id"]


def test_unpaired_acme_is_rejected(bridge, tmp_path):
    bridge.player._cfg.server.access_token = "test-certificate-token-with-more-than-32-bytes"
    client = client_for(bridge, tmp_path)
    assert client.post(BASE + "/acme/preflight", json=PREFLIGHT).status_code == 401


def test_server_lifespan_owns_acme_supervisor(bridge, tmp_path):
    client = client_for(bridge, tmp_path)
    manager = client.app.state.acme
    assert manager._thread is None
    with client:
        assert manager._thread is not None and manager._thread.is_alive()
        assert manager._process is None
    assert not manager._thread.is_alive()
    assert manager._stop.is_set()
    assert not (tmp_path / ".tls" / "acme").exists()


@pytest.mark.parametrize("body", [None, b"not json", b'{"unused":true}'])
def test_activation_preserves_body_independent_contract(bridge, tmp_path, body):
    client = client_for(bridge, tmp_path)
    row = client.post(BASE, json={"name": "Local", "hosts": ["localhost"]}).json()
    response = client.post(
        f"{BASE}/{row['id']}/activate",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text


def test_delete_success_survives_unrelated_pending_cleanup(bridge, tmp_path):
    from tests.unit.test_acme_worker import issued

    client = client_for(bridge, tmp_path)
    manager = client.app.state.acme
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    manager.store.delete(task["certificate_id"])
    saved = manager.state.read(task["id"])
    saved.update(cleanup_pending=True, dns_receipt={"id": "c" * 32})
    manager.state.write(task["id"], saved)
    row = client.post(BASE, json={"name": "Unused", "hosts": ["localhost"]}).json()
    response = client.delete(f"{BASE}/{row['id']}")
    assert response.status_code == 204, response.text
    assert manager.store.list() == []
    assert manager.state.read(task["id"])["removal_pending"]


def test_cleanup_release_requires_confirmation_and_local_authority(bridge, tmp_path):
    from tests.unit.test_acme_worker import issued

    client = client_for(bridge, tmp_path)
    manager = client.app.state.acme
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task.update(phase="error", cleanup_pending=True, dns_receipt={"id": "c" * 32})
    manager.state.write(task["id"], task)
    route = BASE + "/acme/" + task["id"] + "/release-cleanup"
    assert client.post(route, json={"confirm": False}).status_code == 400
    assert client.post(route, json={"confirm": "yes"}).status_code == 422
    assert (
        client.post(
            route, json={"confirm": True}, headers={"X-Forwarded-For": "127.0.0.1"}
        ).status_code
        == 403
    )
    result = client.post(route, json={"confirm": True})
    assert result.status_code == 200, result.text
    assert result.json()["cleanup_released"]
    assert "private-token" not in result.text


def test_saved_acme_operations_are_local_only_and_redacted(bridge, tmp_path):
    client = client_for(bridge, tmp_path)
    client.post(BASE + "/acme/preflight", json=PREFLIGHT)
    local = client.get(BASE).json()
    assert len(local["acme_operations"]) == 1
    assert PREFLIGHT["api_token"] not in str(local)
    remote = client_for(bridge, tmp_path, peer="192.168.1.20")
    assert "acme_operations" not in remote.get(BASE).json()


def test_renewal_pruning_keeps_the_pair_still_served_until_restart(bridge, tmp_path):
    """ADR-0019-20."""
    from tests.unit.test_acme_worker import issued

    store = CertificateStore(tmp_path / ".tls")
    source = CertificateStore(tmp_path / "ca")
    original = source.create("Original", ["radio.example.com"])
    cert, key = source.paths(original["id"])
    running = store.save_acme("Radio", cert.read_bytes(), key.read_bytes(), "1" * 32)
    store.activate(running["id"])
    running_paths = store.paths(running["id"])
    bridge.player._cfg.server.ssl_certfile, bridge.player._cfg.server.ssl_keyfile = running_paths
    client = client_for(bridge, tmp_path, https=True)
    manager = client.app.state.acme
    previous = running["id"]
    for number in range(3):
        task = issued(manager, CertificateStore(tmp_path / f"renewal-{number}"))
        task["expected_active"] = previous
        manager.finish(task)
        assert task["phase"] == "complete"
        current = task["certificate_id"]
        assert store.active_id == current
        assert {row["id"] for row in store.list()} == {running["id"], previous, current}
        assert all(path.is_file() for path in running_paths)
        previous = current
    assert client.get(BASE).json()["restart_required"]
    assert client.delete(f"{BASE}/{running['id']}").status_code == 400

    class Reloader:
        def replace(self, cert, key):
            assert cert.is_file() and key.is_file()

    client.app.state.certificate_reloader = Reloader()
    task = issued(manager, CertificateStore(tmp_path / "live-renewal"))
    task["expected_active"] = previous
    manager.finish(task)
    assert task["phase"] == "complete"
    assert {row["id"] for row in store.list()} == {previous, task["certificate_id"]}
    assert not any(path.exists() for path in running_paths)
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    assert not response.json()["restart_required"]


@pytest.mark.parametrize(
    "peer,headers,auth_mode,expected_local",
    [
        ("127.0.0.1", {}, "native", True),
        ("192.168.1.20", {}, "native", False),
        ("127.0.0.1", {"Forwarded": "for=127.0.0.1"}, "native", False),
        ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}, "native", False),
        ("127.0.0.1", {"Host": "testserver"}, "native", False),
        ("127.0.0.1", {}, "trusted_proxy", False),
    ],
)
def test_inventory_renewal_details_require_direct_native_localhost(
    bridge, tmp_path, peer, headers, auth_mode, expected_local
):
    """ADR-0019-27."""
    from tests.unit.test_acme_worker import issued

    bridge.player._cfg.server.auth_mode = auth_mode
    if auth_mode == "trusted_proxy":
        secret = "test-proxy-secret-" + "a" * 32
        bridge.player._cfg.server.trusted_proxy_secret = secret
        headers = {**headers, "X-AutoDJ-Proxy-Auth": secret, "X-AutoDJ-User-ID": "operator"}
    client = client_for(bridge, tmp_path, peer=peer)
    manager = client.app.state.acme
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    task.update(
        cleanup_pending=True,
        dns_receipt={"id": "c" * 32},
        addresses=["192.168.1.50"],
        error="DNS cleanup needs attention.",
    )
    manager.state.write(task["id"], task)
    response = client.get(BASE, headers=headers)
    assert response.status_code == 200, response.text
    inventory = response.json()
    assert inventory["local"] is expected_local
    row = inventory["certificates"][0]
    assert row["type"] == "Let's Encrypt"
    assert row["hosts"] == ["radio.example.com"]
    if expected_local:
        assert row["renewal"]["cleanup_record_ids"] == ["c" * 32]
        assert row["renewal"]["addresses"] == ["192.168.1.50"]
        assert inventory["acme_operations"][0]["id"] == task["id"]
    else:
        assert "renewal" not in row
        assert "acme_operations" not in inventory
        assert "c" * 32 not in response.text
        assert "192.168.1.50" not in response.text
    assert "private-token" not in response.text


def test_inventory_reads_certificate_rows_once_for_all_renewals(bridge, tmp_path, monkeypatch):
    from tests.unit.test_acme_worker import issued

    client = client_for(bridge, tmp_path)
    manager = client.app.state.acme
    tasks = []
    for number in range(1, 4):
        task = issued(manager, CertificateStore(tmp_path / f"ca-{number}"))
        task["id"] = str(number) * 32
        manager.state.write("index", {"tasks": [t["id"] for t in tasks] + [task["id"]]})
        manager.finish(task)
        tasks.append(task)
    manager.state.write("index", {"tasks": [task["id"] for task in tasks]})
    for task in tasks:
        manager.state.write(task["id"], task)
    reads = []
    original_list = manager.store.list

    def counted_list():
        reads.append(True)
        return original_list()

    monkeypatch.setattr(manager.store, "list", counted_list)
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    assert len(response.json()["acme_operations"]) == 3
    assert len(reads) == 1
