"""Restart and publication failures must retain both cleanup and renewal state."""

import time

import pytest

from autodj.acme_worker import AcmeManager
from autodj.certificates import CertificateStore
from tests.unit.test_acme_worker import issued


def test_indexed_tombstone_survives_startup_until_index_reconciles(tmp_path):
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    ident = "1" * 32
    manager.state.write("index", {"tasks": [ident]})
    manager.state.write(ident, {"id": ident, "deleted": True})
    manager.tick()
    assert manager.tasks() == []
    manager.tick()
    manager.preflight("Radio", "radio.example.com", "a" * 32, "token", "staging")


@pytest.mark.parametrize("cleanup", [False, True])
def test_cached_publication_retains_renewal_deadline(tmp_path, monkeypatch, cleanup):
    from autodj import acme_worker

    store = CertificateStore(tmp_path / ".tls")
    old = store.create("Working", ["localhost"])
    store.activate(old["id"])

    def failed_activation(*_):
        raise OSError("reload unavailable")

    manager = AcmeManager(store, apply=failed_activation)
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task.update(expected_active=old["id"], terms_url="https://example.com/terms")
    planned = task["next_attempt"]
    manager.finish(task)
    assert manager.status(task["id"])["phase"] == "error"
    if cleanup:
        from autodj import acme_service

        class DNS:
            def __init__(self, *_):
                pass

            def cleanup(self, _receipt):
                pass

        monkeypatch.setattr(acme_service, "CloudflareDNS", DNS)
        saved = manager.state.read(task["id"])
        saved.update(
            action="cleanup",
            dns_receipt={"id": "c" * 32},
            cleanup_pending=True,
            after_cleanup={"phase": "error", "next_attempt": 0, "needs_attention": True},
        )
        acme_service.run_operation(manager.state.root, saved)
    manager.apply = None
    manager.issue(
        task["id"], terms_url="https://example.com/terms", accept_terms=True, host_confirmed=True
    )
    manager.finish(manager.state.read(task["id"]))
    assert store.active_id != old["id"]
    assert manager.state.read(task["id"])["next_attempt"] == planned
    assert planned > time.time()

    def unexpected_child(*_args, **_kwargs):
        pytest.fail("Cached publication caused an immediate new order")

    monkeypatch.setattr(acme_worker.subprocess, "Popen", unexpected_child)
    manager.tick()
    assert manager._process is None
