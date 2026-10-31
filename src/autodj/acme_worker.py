"""One server-owned ACME slot with a killable child and durable checkpoints."""

from __future__ import annotations

import logging
import math
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography import x509

from autodj.acme_dns import AcmeError, CloudflareDNS, validate_domain
from autodj.acme_service import (
    DIRECTORIES,
    STAGING_LABEL,
    PrivateState,
    has_dns_work,
    public_task,
    renewal_time,
    retry_time,
)
from autodj.certificates import CertificateStore
from autodj.child_jobs import end_job, job_for_tree

OPERATION_SECONDS = 900
logger = logging.getLogger(__name__)
PAUSED_NOTE = "Renewal paused: this Let's Encrypt certificate is not active"


class AcmeManager:
    def __init__(
        self,
        store: CertificateStore,
        *,
        apply: Callable[[Path, Path], None] | None = None,
        enabled: bool = True,
        running: Callable[[], tuple[Path, Path] | None] | None = None,
    ) -> None:
        self.store, self.apply, self.enabled = store, apply, enabled
        self.running = running
        self.state = PrivateState(store.root / "acme")
        self.lock = threading.RLock()
        self._process_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: Any = None
        self._job: int | None = None
        self._running: str | None = None
        self._started = 0.0
        self._start_failed = False
        self._recovered = False
        self.error = ""

    def tasks(self) -> list[dict]:
        identifiers = self.state.read("index").get("tasks", [])
        if (
            not isinstance(identifiers, list)
            or len(identifiers) > 20
            or any(
                not isinstance(i, str) or not re.fullmatch(r"[0-9a-f]{32}", i) for i in identifiers
            )
        ):
            raise AcmeError("The ACME task index is invalid.")
        tasks = []
        for ident in identifiers:
            task = self._read_task(ident)
            if task.get("deleted"):
                continue
            tasks.append(task)
        return tasks

    def _read_task(self, ident: str) -> dict:
        """Keep damaged checkpoints visible without executing or overwriting them."""
        try:
            task = self.state.read(ident)
            if task.get("deleted") is True:
                return task
            if (
                task.get("id") != ident
                or task.get("environment") not in DIRECTORIES
                or not isinstance(task.get("domain"), str)
                or task.get("phase")
                not in {"queued", "running", "ready", "issued", "complete", "error"}
            ):
                raise ValueError("invalid task")
            failures = task.get("failures", 0)
            if type(failures) is not int or failures < 0:
                raise ValueError("invalid failure count")
            for key in ("next_attempt", "renew_at", "removal_retry_at", "cleanup_retry_at"):
                value = task.get(key, 0)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("invalid schedule")
            for key in ("name", "domain", "error", "certificate_id", "action"):
                if key in task and not isinstance(task[key], str):
                    raise ValueError("invalid task field")
            for key in ("dns_receipts", "dns_intents"):
                if key in task and (
                    not isinstance(task[key], list)
                    or any(
                        not isinstance(receipt, dict) or not isinstance(receipt.get("id", ""), str)
                        for receipt in task[key]
                    )
                ):
                    raise ValueError("invalid DNS checkpoints")
            for key in ("dns_receipt", "dns_intent", "after_cleanup"):
                if key in task and not isinstance(task[key], dict):
                    raise ValueError("invalid checkpoint")
            if "dns_receipt" in task and not isinstance(task["dns_receipt"].get("id", ""), str):
                raise ValueError("invalid DNS checkpoint")
            return task
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return {
                "id": ident,
                "phase": "error",
                "invalid": True,
                "needs_attention": True,
                "error": "The ACME saved task is missing, unreadable or invalid. Check host storage; other tasks continue.",
            }

    @staticmethod
    def _ident(ident: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise AcmeError("Unknown ACME operation.")
        return ident

    def status(self, ident: str) -> dict:
        with self.lock:
            self._ident(ident)
            task = next((t for t in self.tasks() if t["id"] == ident), None)
            if task is None:
                raise AcmeError("Unknown ACME operation.")
            return self.describe(task)

    def describe(self, task: dict, rows: list[dict] | None = None) -> dict:
        result = public_task(task)
        if task.get("certificate_id") and task.get("environment") == "production":
            active = self.store.active_id
            if rows is None:
                rows = self.store.list()
            active_lineage = next(
                (row.get("acme_lineage") for row in rows if row["id"] == active), None
            )
            result["renewal_note"] = "" if task["id"] == active_lineage else PAUSED_NOTE
        receipts = [*task.get("dns_receipts", [])]
        if task.get("dns_receipt"):
            receipts.append(task["dns_receipt"])
        if has_dns_work(task):
            result["cleanup_record_ids"] = [
                r["id"] for r in receipts if re.fullmatch(r"[0-9a-f]{32}", r.get("id", ""))
            ]
            result["cleanup_name"] = "_acme-challenge." + task["domain"]
        return result

    def _idle(self) -> None:
        if not self.enabled or self._stop.is_set():
            raise AcmeError("ACME is unavailable for this server configuration.")
        if self._running or any(
            t.get("phase") in {"queued", "running", "issued"} for t in self.tasks()
        ):
            raise AcmeError("An ACME operation is already running.")

    def preflight(self, name: str, domain: str, zone_id: str, token: str, environment: str) -> dict:
        with self.lock:
            self._idle()
            if environment not in DIRECTORIES:
                raise AcmeError("Unknown ACME environment.")
            domain = validate_domain(domain, domain)
            CloudflareDNS(zone_id, token)  # Validate without doing network work.
            if not name.strip() or len(name) > 120 or not name.isprintable():
                raise AcmeError("Enter a certificate name of 1 to 120 characters.")
            tasks = self.tasks()
            if len(tasks) >= 20:
                raise AcmeError("Remove an unused ACME setup before adding another.")
            task = {
                "id": uuid.uuid4().hex,
                "name": name,
                "domain": domain,
                "zone_id": zone_id,
                "token": token,
                "environment": environment,
                "action": "preflight",
                "phase": "queued",
            }
            self.state.write(task["id"], task)
            try:
                self.state.write("index", {"tasks": [t["id"] for t in tasks] + [task["id"]]})
            except OSError:
                self.state.remove(task["id"])
                raise
            self._wake.set()
            return public_task(task)

    def issue(
        self, ident: str, *, terms_url: str, accept_terms: bool, host_confirmed: bool
    ) -> dict:
        with self.lock:
            self._idle()
            task = self.state.read(self._ident(ident))
            if (
                task.get("phase") not in {"ready", "error"}
                or not accept_terms
                or terms_url != task.get("terms_url")
            ):
                raise AcmeError("Run the preflight and accept its current terms before issuance.")
            if not task.get("confirmed") and not host_confirmed:
                raise AcmeError(
                    "Confirm that devices on your network reach this host at the domain."
                )
            if task.get("next_attempt", 0) > time.time():
                raise AcmeError("Wait until the saved retry time before trying again.")
            if has_dns_work(task):
                raise AcmeError(
                    "Wait for the previous DNS challenge cleanup before another issuance."
                )
            if task.get("certificate") and task.get("private_key"):
                task["phase"] = "issued"
                self.state.write(ident, task)
                self._wake.set()
                return public_task(task)
            if task.get("needs_attention"):
                # Explicit correction may retry, but never bypass the persistent order budget.
                task.pop("order", None)
            task.update(
                action="issue",
                phase="queued",
                accepted_terms=terms_url,
                accepted_at=time.time(),
                host_confirmed=host_confirmed,
                needs_attention=False,
            )
            self.state.write(ident, task)
            self._wake.set()
            return public_task(task)

    def finish(self, task: dict) -> None:
        with self.lock:
            if task.get("phase") != "issued":
                return
            previous = None
            try:
                if not task.get("next_attempt"):
                    cert = x509.load_pem_x509_certificate(task["certificate"].encode())
                    task["next_attempt"] = renewal_time(cert, None)
                if not task.get("renew_at"):
                    task["renew_at"] = task["next_attempt"]
                previous = self.store.active_id
                if task["environment"] == "staging":
                    task.update(phase="complete", label=STAGING_LABEL)
                    if not task.get("cleanup_pending"):
                        task.pop("token", None)
                else:
                    cert = x509.load_pem_x509_certificate(task["certificate"].encode())
                    sans = cert.extensions.get_extension_for_class(
                        x509.SubjectAlternativeName
                    ).value
                    if list(sans) != [x509.DNSName(task["domain"])]:
                        raise AcmeError(
                            "The issued certificate does not match the requested domain."
                        )
                    row = self.store.save_acme(
                        task["name"],
                        task["certificate"].encode(),
                        task["private_key"].encode(),
                        task["id"],
                    )
                    task["certificate_id"] = row["id"]
                    if expected := task.get("expected_active"):
                        activated = self.store.activate_if_current(row["id"], expected, self.apply)
                        if activated or self.store.active_id == row["id"]:
                            self.store.prune_acme(
                                task["id"],
                                row["id"],
                                expected,
                                running=self.running() if self.running else None,
                            )
                    task.update(phase="complete", error="")
                for key in ("certificate", "private_key", "order", "expected_active"):
                    task.pop(key, None)
                task["next_attempt"] = task.pop("renew_at")
                if task.get("cleanup_pending"):
                    task["error"] = "Certificate obtained; DNS challenge cleanup needs attention."
                    task["renew_at"] = task["next_attempt"]
                    task["next_attempt"] = max(time.time() + 900, task.get("cleanup_retry_at", 0))
            except Exception:
                try:
                    unchanged = self.store.active_id == previous
                    outcome = (
                        "The previous selection is unchanged."
                        if unchanged
                        else "The saved selection changed; check the active certificate and HTTPS listener before restarting."
                    )
                except Exception:
                    outcome = "The saved selection could not be confirmed. Check host storage and the HTTPS listener."
                task.update(
                    phase="error",
                    error="The issued certificate could not be saved or activated. " + outcome,
                    needs_attention=True,
                    next_attempt=0,
                )
            self.state.write(task["id"], task)
            self._prune_accounts()

    def _prune_accounts(self) -> None:
        tasks = self.tasks()
        if any(t.get("invalid") for t in tasks):
            # An unreadable checkpoint may still reference either account key.
            return
        environments = {t["environment"] for t in tasks if t.get("token")}
        for environment in DIRECTORIES:
            if environment not in environments:
                self.state.remove("account-" + environment)

    def check_delete(self, ident: str) -> None:
        with self.lock:
            row = next((r for r in self.store.list() if r["id"] == ident), {})
            lineage = row.get("acme_lineage")
            if lineage:
                task = self.state.read(lineage)
                if (
                    lineage == self._running
                    or task.get("phase") in {"queued", "running", "issued"}
                    or has_dns_work(task)
                ):
                    raise AcmeError(
                        "Wait for ACME issuance or challenge cleanup before deleting this certificate."
                    )

    def remove(self, ident: str) -> None:
        with self.lock:
            task = self.state.read(self._ident(ident))
            if (
                ident == self._running
                or task.get("phase") in {"queued", "running", "issued"}
                or has_dns_work(task)
                or any(r.get("acme_lineage") == ident for r in self.store.list())
            ):
                raise AcmeError("This ACME setup still has certificates or unfinished DNS work.")
            remaining = [t["id"] for t in self.tasks() if t["id"] != ident]
            # A tombstone strips secrets before the separate index update can fail.
            self.state.write(ident, {"id": ident, "deleted": True})
            self.state.write("index", {"tasks": remaining})
            self.state.remove(ident)
            self._prune_accounts()

    def certificate_deleted(self) -> None:
        """Best-effort post-delete sweep; never turn a completed deletion into an error."""
        with self.lock:
            try:
                referenced = {r.get("acme_lineage") for r in self.store.list()}
                tasks = self.tasks()
            except Exception:
                self.error = "ACME credential cleanup will retry when host storage is available."
                self._recovered = False
                return
            for task in tasks:
                if task.get("certificate_id") and task["id"] not in referenced:
                    try:
                        self.remove(task["id"])
                    except Exception:
                        try:
                            if self.state.read(task["id"]).get("deleted"):
                                # The secret-free tombstone remains safe in the index.
                                continue
                            task.update(removal_pending=True, removal_retry_at=time.time() + 900)
                            self.state.write(task["id"], task)
                        except Exception:
                            self.error = (
                                "ACME credential cleanup will retry when host storage is available."
                            )
                            self._recovered = False

    def release_cleanup(self, ident: str, *, confirm: bool) -> dict:
        with self.lock:
            if not confirm:
                raise AcmeError(
                    "Confirm that DNS cleanup will stop and the TXT record must be removed by hand."
                )
            task = self.state.read(self._ident(ident))
            if ident == self._running or task.get("phase") in {"queued", "running", "issued"}:
                raise AcmeError(
                    "Wait for the ACME operation to finish before releasing DNS cleanup."
                )
            if not has_dns_work(task):
                raise AcmeError("This ACME setup has no pending DNS cleanup.")
            public = self.describe(task)
            record_ids = public.get("cleanup_record_ids", [])
            name = public["cleanup_name"]
            task.update(task.pop("after_cleanup", {}))
            for key in ("dns_receipt", "dns_intent", "dns_receipts", "dns_intents"):
                task.pop(key, None)
            task.update(
                cleanup_pending=False,
                cleanup_released=True,
                cleanup_record_ids=record_ids,
                cleanup_name=name,
                cleanup_note=f"Remove the {name} TXT record by hand in Cloudflare. AutoDJ has stopped retrying DNS cleanup.",
            )
            if task.get("phase") == "complete" and "renew_at" in task:
                task["next_attempt"] = task.pop("renew_at")
                task["error"] = ""
            if task.get("phase") == "error":
                task.update(action="issue", needs_attention=True)
            if task.get("environment") == "staging" and task.get("phase") == "complete":
                task.pop("token", None)
            self.state.write(ident, task)
            logger.warning(
                "DNS cleanup released for %s; TXT record IDs: %s. Remove the records by hand.",
                name,
                ", ".join(record_ids) or "unknown (creation receipt was lost)",
            )
            self._prune_accounts()
            self._wake.set()
            return self.describe(task)

    def start(self) -> None:
        with self.lock:
            if not self.enabled or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="autodj-acme", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.error = ""
            except Exception:
                self.error = "ACME saved state or worker could not be accessed. Check host storage."
            self._wake.wait(1 if self._running else 60)
            self._wake.clear()

    def tick(self) -> None:
        # Only worker/shutdown contend here. Requests never wait for child exit.
        with self._process_lock:
            process = self._tick_locked()
            if process is not None:
                self._terminate_process(process)
                with self.lock:
                    self._complete_child()

    def _complete_child(self) -> None:
        assert self._running is not None
        # The child may have checkpointed an order or TXT receipt before job
        # setup failed. Reap it before reading its final durable state.
        task = self._read_task(self._running)
        if task.get("phase") == "running":
            task["failures"] = task.get("failures", 0) + 1
            task.update(
                phase="error",
                error="The ACME worker could not start."
                if self._start_failed
                else "ACME worker stopped before completion. The saved order will be retried.",
                next_attempt=retry_time(time.time(), task["failures"], 0),
                needs_attention=False,
            )
            self.state.write(task["id"], task)
        if self._job is not None:
            end_job(self._job)
        self._job = self._process = self._running = None
        if not self._start_failed:
            self.finish(task)
        self._start_failed = False
        self._prune_accounts()

    def _tick_locked(self) -> Any:
        with self.lock:
            if self._stop.is_set() or not self.enabled:
                return
            if not self._recovered:
                self.tasks()  # Validate the index, including its secret-free tombstones.
                referenced = set(self.state.read("index").get("tasks", []))
                for path in self.state.root.glob("*.json"):
                    if not re.fullmatch(r"[0-9a-f]{32}", path.stem) or path.stem in referenced:
                        continue
                    try:
                        orphan = self.state.read(path.stem)
                    except (OSError, ValueError):
                        continue
                    if orphan.get("deleted") or (
                        orphan.get("action") == "preflight" and orphan.get("phase") == "queued"
                    ):
                        self.state.remove(path.stem)
                self._recovered = True
                self.certificate_deleted()
            if self._process is not None:
                if (
                    self._process.poll() is None
                    and not self._start_failed
                    and time.monotonic() - self._started < OPERATION_SECONDS
                ):
                    return
                return self._process
            tasks = self.tasks()
            if any(
                t.get("removal_pending") and t.get("removal_retry_at", 0) <= time.time()
                for t in tasks
            ):
                self.certificate_deleted()
                tasks = self.tasks()
            self._prune_accounts()
            active = self.store.active_id
            active_lineage = next(
                (r.get("acme_lineage") for r in self.store.list() if r["id"] == active), None
            )
            for task in tasks:
                if task.get("invalid"):
                    continue
                phase = task.get("phase")
                if phase == "issued":
                    self.finish(task)
                    return
                if phase == "running":
                    # A previous server stopped. Resume the same order, respecting a delay.
                    task.update(
                        phase="error",
                        next_attempt=time.time() + 900,
                        needs_attention=False,
                        error="ACME was interrupted. The saved order will be retried.",
                    )
                    self.state.write(task["id"], task)
                    continue
                due = task.get("next_attempt", float("inf")) <= time.time()
                renew = task["id"] == active_lineage and phase == "complete" and due
                retry = phase == "error" and due and not task.get("needs_attention")
                cleanup = phase in {"complete", "error"} and due and has_dns_work(task)
                if task.get("removal_pending") and not cleanup:
                    continue
                if phase != "queued" and not (renew or retry or cleanup):
                    continue
                if cleanup:
                    if "after_cleanup" not in task:
                        task["after_cleanup"] = {
                            key: task[key]
                            for key in (
                                "phase",
                                "error",
                                "needs_attention",
                                "action",
                                "next_attempt",
                            )
                            if key in task
                        }
                        if phase == "complete":
                            task["after_cleanup"].update(
                                error="", next_attempt=task.get("renew_at", time.time() + 86400)
                            )
                    task["action"] = "cleanup"
                elif renew:
                    task["action"] = "issue"
                    task["expected_active"] = active
                task["phase"] = "running"
                self.state.write(task["id"], task)
                process = None
                try:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "autodj.acme_worker",
                            str(self.state.root),
                            task["id"],
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    self._job = job_for_tree(process)
                    self._process = process
                    self._running = task["id"]
                    self._started = time.monotonic()
                except Exception:
                    if process is not None:
                        # Reserve the slot until reaping outside the management lock.
                        self._process, self._running = process, task["id"]
                        self._start_failed = True
                        return process
                    if task.get("phase") == "running":
                        task.update(
                            phase="error",
                            error="The ACME worker could not start.",
                            needs_attention=False,
                            next_attempt=time.time() + 900,
                        )
                        self.state.write(task["id"], task)
                return

    @staticmethod
    def _terminate_process(process: Any) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        else:
            process.wait(timeout=5)

    def shutdown(self, timeout: float = 5) -> bool:
        self._stop.set()
        self._wake.set()
        with self._process_lock:
            with self.lock:
                process = self._process
            if process is not None:
                self._terminate_process(process)
            with self.lock:
                if self._job is not None:
                    end_job(self._job)
                self._job = self._process = self._running = None
        if self._thread is not None:
            self._thread.join(timeout=max(0, timeout))
        return self._thread is None or not self._thread.is_alive()


def main() -> None:
    # Client libraries can log signed requests and response bodies in debug mode.
    logging.disable(logging.CRITICAL)
    from autodj.acme_service import run_operation

    root, ident = Path(sys.argv[1]), AcmeManager._ident(sys.argv[2])
    run_operation(root, PrivateState(root).read(ident))


if __name__ == "__main__":
    main()
