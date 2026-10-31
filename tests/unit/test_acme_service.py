"""ACME orchestration with a fake CA and provider."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj.certificates import CertificateStore


def test_persistent_budget_and_retry_schedule(tmp_path):
    """ADR-0019-14."""
    from autodj.acme_dns import AcmeError
    from autodj.acme_service import Budget, retry_time

    budget = Budget(tmp_path)
    for _ in range(3):
        budget.take("order:production:radio.example.com", 1000)
    with pytest.raises(AcmeError, match="budget"):
        Budget(tmp_path).take("order:production:radio.example.com", 1001)
    Budget(tmp_path).take("order:production:radio.example.com", 90000)
    assert [retry_time(1000, i, 0) for i in range(1, 6)] == [1900, 4600, 22600, 87400, 87400]
    assert retry_time(1000, 1, 5000) == 5000


def test_renewal_schedule_uses_actual_validity_and_ari():
    """ADR-0019-13."""
    from autodj.acme_service import renewal_time

    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=6)
    cert = SimpleNamespace(not_valid_before_utc=start, not_valid_after_utc=end)
    fallback = (start + timedelta(days=4)).timestamp()
    assert renewal_time(cert, None, jitter=0) == fallback
    assert renewal_time(cert, None, jitter=1) < fallback
    ari = start + timedelta(days=3)
    assert renewal_time(cert, ari, jitter=0) == ari.timestamp()


class FakeCA:
    terms = "https://example.com/terms"
    orders = 0
    accounts = 0
    failure = None

    def __init__(self, account, save_account, budget, now):
        self.account = account
        self.save_account = save_account
        self.budget = budget
        self.now = now

    def register(self, environment):
        if not self.account.get("registered"):
            self.budget.take("account:" + environment, self.now)
            self.account["registered"] = True
            self.save_account(self.account)
            type(self).accounts += 1

    def issue(self, task, save, dns):
        if not task.get("order"):
            self.budget.take("order:" + task["environment"] + ":" + task["domain"], self.now)
            task["order"] = "saved-order"
            save(task)
            type(self).orders += 1
        if self.failure:
            raise self.failure
        store = CertificateStore(self.root / "fake-ca")
        row = store.create("Issued", [task["domain"]])
        cert, key = store.paths(row["id"])
        return cert.read_text(), key.read_text(), None


class FakeDNS:
    def __init__(self, *_):
        pass

    def zone(self):
        return "example.com"


@pytest.fixture
def operation(tmp_path, monkeypatch):
    from autodj import acme_service as service

    FakeCA.root = tmp_path
    FakeCA.orders = FakeCA.accounts = 0
    FakeCA.failure = None
    monkeypatch.setattr(service, "Authority", FakeCA)
    monkeypatch.setattr(service, "CloudflareDNS", FakeDNS)
    monkeypatch.setattr(service, "preflight", lambda _: {"addresses": [], "confirmed": False})
    service.PrivateState(tmp_path).write("index", {"tasks": ["1" * 32]})
    return service


def task_data():
    return {
        "id": "1" * 32,
        "name": "Radio",
        "domain": "radio.example.com",
        "zone_id": "a" * 32,
        "token": "never-public-token",
        "environment": "staging",
        "action": "preflight",
        "phase": "queued",
    }


def test_terms_and_host_confirmation_before_registration(tmp_path, operation):
    """ADR-0019-09."""
    task = task_data()
    operation.run_operation(tmp_path, task)
    assert task["phase"] == "ready"
    assert task["terms_url"] == FakeCA.terms
    assert not FakeCA.accounts and not FakeCA.orders
    task["action"] = "issue"
    operation.run_operation(tmp_path, task)
    assert task["phase"] == "error"
    assert not FakeCA.accounts and not FakeCA.orders
    task.update(accepted_terms=FakeCA.terms, accepted_at=1000, host_confirmed=True)
    operation.run_operation(tmp_path, task)
    assert task["phase"] == "issued"
    assert FakeCA.accounts == FakeCA.orders == 1
    assert "PRIVATE KEY" in task["private_key"]


def test_failure_resumes_order_and_redacts_secrets(tmp_path, operation):
    from autodj.acme_dns import AcmeError

    task = task_data()
    task.update(action="issue", accepted_terms=FakeCA.terms, accepted_at=1000, host_confirmed=True)
    FakeCA.failure = AcmeError(
        "ACME temporarily unavailable.", permanent=False, retry_at=9999999999
    )
    operation.run_operation(tmp_path, task)
    assert task["phase"] == "error" and task["next_attempt"] == 9999999999
    FakeCA.failure = None
    operation.run_operation(tmp_path, task)
    assert task["phase"] == "issued" and FakeCA.orders == 1
    public = operation.public_task(task)
    assert "never-public-token" not in str(public)
    assert "PRIVATE KEY" not in str(public)


def test_changed_terms_stop_renewal(tmp_path, operation):
    task = task_data()
    task.update(
        action="issue",
        accepted_terms="https://example.com/old",
        accepted_at=1000,
        host_confirmed=True,
    )
    operation.run_operation(tmp_path, task)
    assert task["phase"] == "error" and task["needs_attention"]
    assert FakeCA.orders == FakeCA.accounts == 0


def test_private_state_rejects_linked_root(tmp_path):
    from autodj.acme_service import PrivateState

    root = tmp_path / "missing"
    state = PrivateState(root)
    assert state.read("budget") == {}
    assert not root.exists()
    state.write("budget", {"secret": "private"})
    assert state.read("budget") == {"secret": "private"}
    with pytest.raises(ValueError):
        state.read("../outside")


def test_cleanup_retry_does_not_register_or_issue(tmp_path, operation, monkeypatch):
    removed = []
    monkeypatch.setattr(
        FakeDNS, "cleanup", lambda self, receipt: removed.append(receipt), raising=False
    )
    task = task_data()
    task.update(
        action="cleanup",
        phase="running",
        dns_receipt={"id": "c" * 32},
        cleanup_pending=True,
        certificate_id="2" * 32,
    )
    operation.run_operation(tmp_path, task)
    assert removed == [{"id": "c" * 32}]
    assert task["phase"] == "complete" and not task["cleanup_pending"]
    assert FakeCA.accounts == FakeCA.orders == 0


def test_unexpected_failure_does_not_persist_exception_secrets(tmp_path, operation):
    task = task_data()
    task.update(action="issue", accepted_terms=FakeCA.terms, accepted_at=1000, host_confirmed=True)
    FakeCA.failure = RuntimeError("private-token-never-log")
    operation.run_operation(tmp_path, task)
    assert "private-token-never-log" not in str(operation.public_task(task))


@pytest.mark.parametrize("action", ["read", "write", "remove"])
def test_private_state_refuses_resolved_escape(tmp_path, monkeypatch, action):
    from autodj.acme_service import PrivateState

    state = PrivateState(tmp_path / ".tls" / "acme")
    ident = "1" * 32
    state.write("index", {"tasks": [ident]})
    state.write(ident, {"secret": "unchanged"})
    path = state.path(ident)
    before = path.read_bytes()
    resolve = Path.resolve

    def redirected(candidate, *args, **kwargs):
        return (
            tmp_path / "outside.json" if candidate == path else resolve(candidate, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="unsafe"):
        if action == "write":
            state.write(ident, {"secret": "changed"})
        else:
            getattr(state, action)(ident)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "name", ["../outside", "..\\outside", "/outside", "C:\\outside", "state.json", "state\n"]
)
def test_private_state_rejects_names_outside_allowlist(tmp_path, name):
    from autodj.acme_service import PrivateState

    state = PrivateState(tmp_path / ".tls" / "acme")
    with pytest.raises(ValueError, match="Invalid ACME state name"):
        state.path(name)
    assert not state.root.exists()


@pytest.mark.parametrize("action", ["read", "write", "remove"])
def test_private_state_refuses_unindexed_task_even_when_file_exists(tmp_path, action):
    from autodj.acme_service import PrivateState
    from autodj.secret_files import write_private_file

    state = PrivateState(tmp_path / ".tls" / "acme")
    ident = "a" * 32
    path = state.root / (ident + ".json")
    write_private_file(path, json.dumps({"version": 1, "token": "untouched"}))
    state.write("index", {"tasks": []})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unknown ACME operation"):
        if action == "write":
            state.write(ident, {"token": "changed"})
        else:
            getattr(state, action)(ident)
    assert path.read_bytes() == before


def test_private_state_rejects_escaping_index_entry(tmp_path):
    from autodj.acme_service import PrivateState

    state = PrivateState(tmp_path / ".tls" / "acme")
    state.write("index", {"tasks": ["../outside"]})
    with pytest.raises(ValueError, match="index is invalid"):
        state.path("a" * 32)


def test_new_task_is_indexed_before_request_lookup_and_rolls_back_failed_admission(
    tmp_path, monkeypatch
):
    from autodj.acme_service import PrivateState

    state = PrivateState(tmp_path / ".tls" / "acme")
    task = state.create_task({"token": "secret"}, existing=[])
    assert state.read("index")["tasks"] == [task["id"]]
    assert state.read(task["id"])["token"] == "secret"
    write = state.write

    def fail_index(name, data):
        if name == "index":
            raise OSError("index write failed")
        write(name, data)

    monkeypatch.setattr(state, "write", fail_index)
    with pytest.raises(OSError, match="index write failed"):
        state.create_task({"token": "orphan-secret"}, existing=[task["id"]])
    assert {p.name for p in state.root.glob("*.json")} == {"index.json", task["id"] + ".json"}
