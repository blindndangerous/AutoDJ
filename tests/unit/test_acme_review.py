"""Regression cases from review of ACME issuance and lifecycle ownership."""

import threading
import time
from types import SimpleNamespace

import pytest

from autodj import acme_worker
from autodj.acme_dns import AcmeError
from autodj.acme_worker import AcmeManager
from autodj.certificates import CertificateStore
from autodj.doctor import _acme_check
from autodj.secret_files import write_private_file
from tests.unit.test_acme_worker import issued


def test_deleted_lineage_sweep_is_independent_and_retryable(tmp_path):
    """ADR-0019-18."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    first = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(first)
    manager.store.delete(first["certificate_id"])
    saved = manager.state.read(first["id"])
    saved.update(cleanup_pending=True, dns_receipt={"id": "c" * 32})
    manager.state.write(first["id"], saved)
    second = {**saved, "id": "2" * 32, "cleanup_pending": False}
    second.pop("dns_receipt")
    manager.state.write(second["id"], second)
    manager.state.write("index", {"tasks": [first["id"], second["id"]]})
    manager.certificate_deleted()
    assert manager.state.read(second["id"]) == {}
    saved = manager.state.read(first["id"])
    assert saved["removal_pending"] and saved["removal_retry_at"] > time.time()
    assert saved["token"] == "private-token"
    saved.pop("dns_receipt")
    saved.update(cleanup_pending=False, removal_retry_at=0)
    manager.state.write(first["id"], saved)
    manager.tick()
    assert manager.state.read(first["id"]) == {}


def test_startup_sweeps_lineage_after_interrupted_certificate_delete(tmp_path):
    """ADR-0019-18."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    manager.state.write("account-production", {"key": "private-account"})
    manager.store.delete(task["certificate_id"])
    resumed = AcmeManager(manager.store)
    resumed.tick()
    assert resumed.tasks() == []
    assert resumed.state.read(task["id"]) == {}
    assert resumed.state.read("account-production") == {}


def test_job_setup_failure_terminates_and_reaps_child(tmp_path, monkeypatch):
    """ADR-0019-21."""
    events = []

    class Process:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            events.append("spawn")

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")
            self.returncode = -1

        def wait(self, timeout):
            events.append("wait")
            return self.returncode

    def fail_job(_process):
        raise OSError("job creation failed")

    monkeypatch.setattr(acme_worker.subprocess, "Popen", Process)
    monkeypatch.setattr(acme_worker, "job_for_tree", fail_job)
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = manager.preflight("Radio", "radio.example.com", "a" * 32, "token", "staging")
    manager.tick()
    assert events == ["spawn", "terminate", "wait"]
    assert manager._process is None and manager._running is None
    manager.tick()
    assert events == ["spawn", "terminate", "wait"]
    assert manager.status(task["id"])["next_attempt"] > time.time()


def test_cleanup_release_is_confirmed_logged_and_unblocks_removal(tmp_path, caplog):
    """ADR-0019-19."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task.update(
        phase="error",
        action="cleanup",
        needs_attention=True,
        cleanup_pending=True,
        dns_receipt={"id": "c" * 32},
        error="DNS token revoked",
    )
    manager.state.write(task["id"], task)
    with pytest.raises(AcmeError, match="Confirm"):
        manager.release_cleanup(task["id"], confirm=False)
    assert manager.state.read(task["id"])["cleanup_pending"]
    result = manager.release_cleanup(task["id"], confirm=True)
    assert not result["cleanup_pending"] and result["cleanup_released"]
    assert "_acme-challenge.radio.example.com" in result["cleanup_note"]
    assert "by hand" in result["cleanup_note"]
    assert "c" * 32 in caplog.text
    assert "private-token" not in caplog.text
    assert "PRIVATE KEY" not in str(result)
    manager.remove(task["id"])
    assert manager.tasks() == []


def test_successful_renewal_keeps_only_active_and_previous_in_its_lineage(tmp_path):
    """ADR-0019-20."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    manual = manager.store.create("Manual", ["localhost"])
    ca = CertificateStore(tmp_path / "other")
    other_source = ca.create("Other", ["other.example.com"])
    cert, key = ca.paths(other_source["id"])
    other = manager.store.save_acme("Other", cert.read_bytes(), key.read_bytes(), "2" * 32)
    previous = None
    for number in range(4):
        task = issued(manager, CertificateStore(tmp_path / str(number)))
        if previous:
            task["expected_active"] = previous
        manager.finish(task)
        current = task["certificate_id"]
        if previous is None:
            manager.store.activate(current)
        assert manager.store.active_id == current
        rows = manager.store.list()
        lineage = {r["id"] for r in rows if r.get("acme_lineage") == task["id"]}
        assert lineage == {current, previous} - {None}
        assert {manual["id"], other["id"]} <= {r["id"] for r in rows}
        previous = current


def test_saved_renewal_reports_paused_after_manual_selection(tmp_path):
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    first = task["certificate_id"]
    manager.store.activate(first)
    manual = manager.store.create("Manual", ["localhost"])
    task = issued(manager, CertificateStore(tmp_path / "ca2"))
    task["expected_active"] = first
    manager.store.activate(manual["id"])
    manager.finish(task)
    assert manager.status(task["id"])["renewal_note"] == (
        "Renewal paused: this Let's Encrypt certificate is not active"
    )
    assert manager.store.active_id == manual["id"]


def test_activation_failure_message_matches_saved_selection(tmp_path, monkeypatch):
    """ADR-0019-23."""
    store = CertificateStore(tmp_path / ".tls")
    old = store.create("Working", ["localhost"])
    store.activate(old["id"])

    def fail_apply(*_args):
        # Exercise failure to save jage9's existing rollback after apply fails.
        monkeypatch.setattr(store, "_save", fail_rollback)
        raise OSError("private callback details")

    def fail_rollback(_data):
        raise OSError("disk unavailable")

    manager = AcmeManager(store, apply=fail_apply)
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    task["expected_active"] = old["id"]
    manager.finish(task)
    assert store.active_id == task["certificate_id"]
    error = manager.status(task["id"])["error"]
    assert "unchanged" not in error
    assert "saved selection changed" in error
    assert "private callback" not in error


def test_doctor_warns_about_released_cleanup_without_a_failure(tmp_path):
    from types import SimpleNamespace

    from autodj.doctor import _acme_check

    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"), environment="staging")
    task.update(cleanup_pending=True, dns_receipt={"id": "c" * 32})
    manager.finish(task)
    manager.release_cleanup(task["id"], confirm=True)
    saved = manager.state.read(task["id"])
    saved["error"] = ""
    manager.state.write(task["id"], saved)
    result = _acme_check(SimpleNamespace(index=SimpleNamespace(index_dir=tmp_path)))
    assert result.status == "warn"
    assert "by hand" in str(result)


def test_post_delete_sweep_does_not_restore_secrets_after_tombstone(tmp_path, monkeypatch):
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    manager.store.delete(task["certificate_id"])
    write = manager.state.write

    def fail_index(name, data):
        if name == "index":
            raise OSError("index unavailable")
        write(name, data)

    monkeypatch.setattr(manager.state, "write", fail_index)
    manager.certificate_deleted()
    assert manager.state.read(task["id"])["deleted"]
    assert "token" not in manager.state.read(task["id"])


@pytest.mark.parametrize("phase", ["running", "issued"])
def test_job_setup_failure_preserves_child_checkpoints(tmp_path, monkeypatch, phase):
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = manager.preflight("Radio", "radio.example.com", "a" * 32, "token", "staging")

    class Process:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            checkpoint = manager.state.read(task["id"])
            checkpoint.update(
                phase=phase,
                order={"uri": "https://ca.example/order/1"},
                dns_receipts=[{"id": "c" * 32}],
                cleanup_pending=True,
            )
            manager.state.write(task["id"], checkpoint)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -1

        def wait(self, timeout):
            return self.returncode

    def fail_job(_process):
        raise OSError("job setup failed after child checkpoint")

    monkeypatch.setattr(acme_worker.subprocess, "Popen", Process)
    monkeypatch.setattr(acme_worker, "job_for_tree", fail_job)
    manager.tick()
    checkpoint = manager.state.read(task["id"])
    assert checkpoint["order"] == {"uri": "https://ca.example/order/1"}
    assert checkpoint["dns_receipts"] == [{"id": "c" * 32}]
    assert checkpoint["phase"] == ("error" if phase == "running" else phase)
    assert manager._process is None


def test_releasing_completed_cleanup_restores_saved_renewal_schedule(tmp_path):
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    planned = time.time() + 86400 * 30
    task.update(next_attempt=planned, cleanup_pending=True, dns_receipt={"id": "c" * 32})
    manager.finish(task)
    assert manager.state.read(task["id"])["next_attempt"] < planned
    manager.release_cleanup(task["id"], confirm=True)
    released = manager.state.read(task["id"])
    assert released["next_attempt"] == planned
    assert "renew_at" not in released
    assert released["cleanup_released"]


@pytest.mark.parametrize(
    "damage",
    ["json", "missing", "unreadable", "identity", "schedule", "receipt", "overflow", "domain"],
)
def test_bad_task_does_not_stop_other_renewals_and_reports_attention(tmp_path, monkeypatch, damage):
    """ADR-0019-25."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    manager.finish(task)
    manager.store.activate(task["certificate_id"])
    task["next_attempt"] = 1
    manager.state.write(task["id"], task)
    broken_id = "b" * 32
    broken = {**task, "id": broken_id, "token": "unreadable-private-token"}
    if damage == "identity":
        broken["id"] = "invalid"
    if damage == "schedule":
        broken["next_attempt"] = {"secret": "unreadable-private-token"}
    if damage == "receipt":
        broken.update(cleanup_pending=True, dns_receipts=[{"id": []}])
    if damage == "overflow":
        broken["next_attempt"] = 10**400
    if damage == "domain":
        broken.pop("domain")
        broken.update(cleanup_pending=True, dns_receipts=[{"id": "c" * 32}])
    manager.state.write(broken_id, broken)
    manager.state.write("index", {"tasks": [broken_id, task["id"]]})
    manager.state.write("account-staging", {"key": "retain-while-references-unknown"})
    if damage == "json":
        write_private_file(manager.state.path(broken_id), b'{"token":"unreadable-private-token"')
    elif damage == "missing":
        manager.state.remove(broken_id)
    elif damage == "unreadable":
        read = acme_worker.PrivateState.read

        def denied(state, name):
            if name == broken_id:
                raise PermissionError("unreadable-private-token")
            return read(state, name)

        monkeypatch.setattr(acme_worker.PrivateState, "read", denied)

    class Process:
        def __init__(self, *_args, **_kwargs):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(acme_worker.subprocess, "Popen", Process)
    monkeypatch.setattr(acme_worker, "job_for_tree", lambda _process: None)
    manager.tick()
    assert manager._running == task["id"]
    problem = manager.status(broken_id)
    assert problem["needs_attention"] and "saved task" in problem["error"]
    assert "unreadable-private-token" not in str(problem)
    diagnostic = _acme_check(SimpleNamespace(index=SimpleNamespace(index_dir=tmp_path)))
    assert diagnostic.status == "warn" and broken_id in str(diagnostic)
    assert "unreadable-private-token" not in str(diagnostic)
    assert manager.state.read("account-staging")["key"] == "retain-while-references-unknown"
    assert broken_id in manager.state.read("index")["tasks"]


def test_publication_retry_restores_future_renewal_schedule(tmp_path, monkeypatch):
    """ADR-0019-26."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = issued(manager, CertificateStore(tmp_path / "ca"))
    real_save = manager.store.save_acme

    def fail_save(*_args):
        raise OSError("inventory full")

    monkeypatch.setattr(manager.store, "save_acme", fail_save)
    manager.finish(task)
    assert task["phase"] == "error" and task["next_attempt"] == 0
    # Legacy failed-publication state can lack the separate renewal checkpoint.
    task.pop("renew_at", None)
    task["terms_url"] = task["accepted_terms"]
    manager.state.write(task["id"], task)
    monkeypatch.setattr(manager.store, "save_acme", real_save)
    manager.issue(task["id"], terms_url=task["terms_url"], accept_terms=True, host_confirmed=True)
    manager.tick()
    saved = manager.state.read(task["id"])
    assert saved["phase"] == "complete"
    assert saved["next_attempt"] > time.time() + 86400
    manager.store.activate(saved["certificate_id"])

    def unexpected_order(*_args, **_kwargs):
        pytest.fail("Publication retry must not immediately create another order")

    monkeypatch.setattr(acme_worker.subprocess, "Popen", unexpected_order)
    manager.tick()
    assert manager._process is None


@pytest.mark.parametrize("failures", [0.5, 1.0])
def test_invalid_child_failure_counter_releases_slot_without_overwriting_state(tmp_path, failures):
    """ADR-0019-25."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = manager.preflight("Radio", "radio.example.com", "a" * 32, "token", "staging")
    saved = manager.state.read(task["id"])
    saved.update(phase="running", failures=failures)
    manager.state.write(task["id"], saved)

    class ExitedProcess:
        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    manager._process, manager._running = ExitedProcess(), task["id"]
    manager.tick()
    assert manager._process is None and manager._running is None
    assert manager.status(task["id"])["needs_attention"]
    assert manager.state.read(task["id"]) == saved
    following = manager.preflight("Other", "other.example.com", "a" * 32, "token", "staging")
    assert following["phase"] == "queued"


@pytest.mark.parametrize("cause", ["timeout", "job_setup", "shutdown"])
def test_slow_child_reaping_does_not_hold_management_lock(tmp_path, monkeypatch, cause):
    """ADR-0019-13."""
    manager = AcmeManager(CertificateStore(tmp_path / ".tls"))
    task = manager.preflight("Radio", "radio.example.com", "a" * 32, "token", "staging")
    manual = manager.store.create("Manual", ["localhost"])
    waiting, release, responded = threading.Event(), threading.Event(), threading.Event()
    failures = []

    class Process:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            pass

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -1

        def wait(self, timeout):
            waiting.set()
            assert release.wait(5), "test must release the blocked child"
            return self.returncode

    def job(process):
        if cause == "job_setup":
            raise OSError("job setup failed")
        return None

    monkeypatch.setattr(acme_worker.subprocess, "Popen", Process)
    monkeypatch.setattr(acme_worker, "job_for_tree", job)
    if cause != "job_setup":
        manager.tick()
        manager._started -= acme_worker.OPERATION_SECONDS + 1

    def reap():
        try:
            manager.shutdown() if cause == "shutdown" else manager.tick()
        except BaseException as exc:
            failures.append(exc)

    def manage():
        try:
            assert manager.status(task["id"])["id"] == task["id"]
            with pytest.raises(AcmeError):
                manager.preflight("Another", "radio.example.com", "a" * 32, "token", "staging")
            with manager.lock:
                manager.check_delete(manual["id"])
                manager.store.delete(manual["id"])
                manager.certificate_deleted()
            responded.set()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=reap)
    request = threading.Thread(target=manage)
    worker.start()
    try:
        assert waiting.wait(2)
        request.start()
        assert responded.wait(2), "management requests blocked behind child.wait()"
    finally:
        release.set()
        worker.join(5)
        if request.ident is not None:
            request.join(5)
    assert not failures
    assert not worker.is_alive() and not request.is_alive()
