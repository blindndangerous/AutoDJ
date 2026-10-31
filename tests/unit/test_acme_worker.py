"""Bounded ownership and publication without real provider calls."""

import json
import time
from types import SimpleNamespace

import pytest

from autodj.certificates import CertificateStore
from autodj.secret_files import write_private_file


def issued(manager, source, *, environment="production"):
    row = source.create("Issued", ["radio.example.com"])
    cert, key = source.paths(row["id"])
    task = {
        "id": "1" * 32,
        "name": "Radio",
        "domain": "radio.example.com",
        "zone_id": "a" * 32,
        "environment": environment,
        "token": "private-token",
        "action": "issue",
        "phase": "issued",
        "certificate": cert.read_text(),
        "private_key": key.read_text(),
        "next_attempt": time.time() + 3600,
        "accepted_terms": "https://example.com/terms",
        "accepted_at": time.time(),
        "host_confirmed": True,
    }
    manager.state.write("index", {"tasks": [task["id"]]})
    manager.state.write(task["id"], task)
    return task


def test_staging_never_enters_inventory_and_secrets_are_removed(tmp_path):
    """ADR-0019-08."""
    from autodj.acme_worker import AcmeManager

    store = CertificateStore(tmp_path / ".tls")
    manager = AcmeManager(store)
    task = issued(manager, CertificateStore(tmp_path / "ca"), environment="staging")
    manager.finish(task)
    assert store.list() == [] and store.active_id is None
    saved = manager.state.read(task["id"])
    assert saved["phase"] == "complete"
    assert "private_key" not in saved and "certificate" not in saved and "token" not in saved
    assert "not trusted" in manager.status(task["id"])["label"]


def test_renewal_respects_manual_selection(tmp_path):
    """ADR-0019-09."""
    from autodj.acme_worker import AcmeManager

    store = CertificateStore(tmp_path / ".tls")
    manager = AcmeManager(store)
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    first = store.list()[0]
    assert first["type"] == "Let's Encrypt" and store.active_id is None
    store.activate(first["id"])
    manual = store.create("Manual", ["localhost"])
    task = issued(manager, CertificateStore(tmp_path / "ca2"))
    task["expected_active"] = first["id"]
    store.activate(manual["id"])
    manager.finish(task)
    assert store.active_id == manual["id"]
    assert len(store.list()) == 3


def test_active_renewal_and_publication_replay(tmp_path):
    from autodj.acme_worker import AcmeManager

    store = CertificateStore(tmp_path / ".tls")
    applied = []
    manager = AcmeManager(store, apply=lambda *pair: applied.append(pair))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    old = store.list()[0]["id"]
    store.activate(old)
    task = issued(manager, CertificateStore(tmp_path / "ca2"))
    task["expected_active"] = old
    manager.finish(task.copy())
    new = store.active_id
    assert new != old and len(applied) == 1
    manager.finish(task.copy())
    assert len(store.list()) == 2 and store.active_id == new


def test_activation_failure_retains_working_selection(tmp_path):
    from autodj.acme_worker import AcmeManager

    store = CertificateStore(tmp_path / ".tls")
    old = store.create("Working", ["localhost"])
    store.activate(old["id"])

    def fail(*_):
        raise OSError("secret input must not be echoed")

    manager = AcmeManager(store, apply=fail)
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task["expected_active"] = old["id"]
    manager.finish(task)
    assert store.active_id == old["id"]
    assert manager.status(task["id"])["error"]
    assert "secret input" not in str(manager.status(task["id"]))


def test_one_slot_and_consent_are_enforced(tmp_path):
    from autodj.acme_dns import AcmeError
    from autodj.acme_worker import AcmeManager

    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = manager.preflight("Radio", "radio.example.com", "a" * 32, "secret", "staging")
    with pytest.raises(AcmeError, match="running"):
        manager.preflight("Radio", "radio.example.com", "a" * 32, "secret", "production")
    assert "secret" not in str(task)
    with pytest.raises(AcmeError):
        manager.issue(
            task["id"],
            terms_url="https://example.com/terms",
            accept_terms=False,
            host_confirmed=False,
        )


def test_doctor_acme_is_read_only_and_redacted(tmp_path, monkeypatch):
    """ADR-0019-14."""
    from autodj.acme_worker import AcmeManager
    from autodj.doctor import _acme_check

    cfg = SimpleNamespace(index=SimpleNamespace(index_dir=tmp_path))
    assert _acme_check(cfg).status == "pass"
    assert not (tmp_path / ".tls").exists()
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task.update(phase="error", error="DNS unavailable", needs_attention=True)
    manager.state.write(task["id"], task)
    check = _acme_check(cfg)
    assert check.status == "warn" and "private-token" not in str(check)
    assert "PRIVATE KEY" not in str(check)


def test_child_has_one_slot_deadline_and_no_secret_arguments(tmp_path, monkeypatch):
    """ADR-0019-13."""
    from autodj import acme_worker as worker

    calls = []

    class Process:
        returncode = None

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -1

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(worker.subprocess, "Popen", Process)
    manager = worker.AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = manager.preflight("Radio", "radio.example.com", "a" * 32, "secret-token", "staging")
    manager.tick()
    manager.tick()
    assert len(calls) == 1 and "secret-token" not in str(calls)
    manager._started -= worker.OPERATION_SECONDS + 1
    manager.tick()
    saved = manager.state.read(task["id"])
    assert saved["phase"] == "error" and saved["failures"] == 1
    assert saved["next_attempt"] > time.time()
    assert manager.shutdown()


def test_only_active_lineage_renews_and_last_delete_cleans_secrets(tmp_path, monkeypatch):
    from autodj import acme_worker as worker

    manager = worker.AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    saved = manager.state.read(task["id"])
    saved["next_attempt"] = 1
    manager.state.write(task["id"], saved)
    manager.state.write("account-production", {"key": "private-account"})
    manager.tick()
    assert manager._process is None  # Saved, never-activated certificates do not renew.
    manager.store.delete(saved["certificate_id"])
    manager.certificate_deleted()
    assert not (manager.state.root / (task["id"] + ".json")).exists()
    assert manager.state.read("account-production") == {}


def test_shutdown_interrupts_child_and_restart_retains_order(tmp_path, monkeypatch):
    from autodj.acme_worker import AcmeManager

    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task.update(phase="running", order={"uri": "https://ca.example/order/1"})
    manager.state.write(task["id"], task)
    resumed = AcmeManager(manager.store)
    resumed.tick()
    saved = resumed.state.read(task["id"])
    assert saved["phase"] == "error" and saved["order"] == task["order"]
    assert saved["next_attempt"] > time.time()


def test_failed_authorization_still_schedules_cleanup(tmp_path, monkeypatch):
    from autodj import acme_worker as worker

    class Process:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(worker.subprocess, "Popen", Process)
    manager = worker.AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task.update(
        phase="error",
        needs_attention=True,
        cleanup_pending=True,
        next_attempt=1,
        dns_receipt={"id": "c" * 32},
        error="Authorization failed",
    )
    manager.state.write(task["id"], task)
    manager.tick()
    saved = manager.state.read(task["id"])
    assert saved["action"] == "cleanup" and saved["phase"] == "running"
    assert saved["after_cleanup"]["needs_attention"]


def test_interrupted_setup_delete_does_not_poison_task_index(tmp_path, monkeypatch):
    from autodj.acme_worker import AcmeManager

    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = manager.preflight("Radio", "radio.example.com", "a" * 32, "secret", "staging")
    saved = manager.state.read(task["id"])
    saved["phase"] = "ready"
    manager.state.write(task["id"], saved)
    write = manager.state.write

    def failed_index(name, data):
        if name == "index":
            raise OSError("disk full")
        write(name, data)

    monkeypatch.setattr(manager.state, "write", failed_index)
    with pytest.raises(OSError):
        manager.remove(task["id"])
    resumed = AcmeManager(manager.store)
    assert resumed.tasks() == []
    assert "secret" not in str(resumed.state.read(task["id"]))
    resumed.preflight("Again", "radio.example.com", "a" * 32, "new-secret", "staging")


def test_startup_removes_unadmitted_setup_secrets(tmp_path):
    from autodj.acme_worker import AcmeManager

    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    ident = "1" * 32
    orphan = manager.state.root / (ident + ".json")
    write_private_file(
        orphan,
        json.dumps(
            {
                "version": 1,
                "id": ident,
                "token": "orphan-token",
                "phase": "queued",
                "action": "preflight",
            }
        ),
    )
    manager.tick()
    assert not orphan.exists()


def test_child_entry_runs_without_network_for_invalid_environment(tmp_path):
    import subprocess
    import sys

    from autodj.acme_worker import AcmeManager

    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    ident = "1" * 32
    manager.state.write("index", {"tasks": [ident]})
    manager.state.write(ident, {"id": ident, "environment": "invalid", "phase": "running"})
    result = subprocess.run(
        [sys.executable, "-m", "autodj.acme_worker", str(manager.state.root), ident],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr
    assert manager.state.read(ident)["phase"] == "error"
