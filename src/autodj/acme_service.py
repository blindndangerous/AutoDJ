"""Private ACME checkpoints and issuance, called only by the owned worker."""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography import x509

from autodj.acme_dns import AcmeError, CloudflareDNS, preflight, validate_domain
from autodj.secret_files import read_private_json, write_private_file

DIRECTORIES = {
    "staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "production": "https://acme-v02.api.letsencrypt.org/directory",
}
STAGING_LABEL = "Let's Encrypt staging test — not trusted"
BACKOFF = (900, 3600, 21600, 86400)


class PrivateState:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, name: str) -> Path:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", name):
            raise ValueError("Invalid ACME state name.")
        paths = {
            "index": self.root / "index.json",
            "budget": self.root / "budget.json",
            "account-staging": self.root / "account-staging.json",
            "account-production": self.root / "account-production.json",
        }
        if name not in paths:
            identifiers = self.read("index").get("tasks", [])
            if (
                not isinstance(identifiers, list)
                or len(identifiers) > 20
                or any(
                    not isinstance(i, str) or not re.fullmatch(r"[0-9a-f]{32}", i)
                    for i in identifiers
                )
            ):
                raise AcmeError("The ACME task index is invalid.")
            paths.update({ident: self.root / (ident + ".json") for ident in identifiers})
        if name not in paths:
            raise AcmeError("Unknown ACME operation.")
        return self._checked_path(paths[name])

    def _checked_path(self, path: Path) -> Path:
        for part in (self.root.parent, self.root, path):
            if part.is_symlink() or part.is_junction():
                raise AcmeError("The ACME storage path is unsafe.")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise AcmeError("The ACME storage path is unsafe.")
        return path

    def read(self, name: str) -> dict:
        return self._read_path(self.path(name))

    @staticmethod
    def _read_path(path: Path) -> dict:
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            raise AcmeError("The ACME state is too large.")
        return read_private_json(path, require_private=True)

    def write(self, name: str, data: dict) -> None:
        write_private_file(self.path(name), json.dumps({**data, "version": 1}))

    def remove(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)

    def create_task(self, data: dict, *, existing: list[str]) -> dict:
        """Only the host generates new names; request lookup starts after admission."""
        ident = uuid.uuid4().hex
        task = {**data, "id": ident}
        path = self._checked_path(self.root / (ident + ".json"))
        write_private_file(path, json.dumps({**task, "version": 1}))
        try:
            self.write("index", {"tasks": [*existing, ident]})
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return task

    def recover_orphans(self) -> None:
        """Inspect unindexed files only through host directory entries, never request IDs."""
        referenced = set(self.read("index").get("tasks", []))
        for path in self.root.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem) or path.stem in referenced:
                continue
            try:
                orphan = self._read_path(self._checked_path(path))
            except (OSError, ValueError):
                continue
            if orphan.get("deleted") or (
                orphan.get("action") == "preflight" and orphan.get("phase") == "queued"
            ):
                path.unlink(missing_ok=True)


class Budget:
    """Durable rolling-day limits, independent of deletable accounts and lineages."""

    def __init__(self, root: Path) -> None:
        self.state = PrivateState(root)

    def take(self, key: str, now: float) -> None:
        entries = self.state.read("budget")
        entries = {k: [t for t in times if t > now - 86400] for k, times in entries.items()}
        entries = {k: times for k, times in entries.items() if times}
        times = entries.setdefault(key, [])
        if len(times) >= 3:
            raise AcmeError(
                "The local ACME order/account budget is exhausted.",
                permanent=False,
                retry_at=times[0] + 86400,
            )
        if len(entries) > 100:
            raise AcmeError(
                "The local ACME budget is full. Try tomorrow.",
                permanent=False,
                retry_at=now + 86400,
            )
        times.append(now)
        self.state.write("budget", entries)


def retry_time(now: float, failures: int, retry_at: float) -> float:
    return max(now + BACKOFF[min(max(1, failures), len(BACKOFF)) - 1], retry_at)


def renewal_time(cert: Any, ari: datetime | None, *, jitter: float | None = None) -> float:
    before, after = cert.not_valid_before_utc, cert.not_valid_after_utc
    fallback = before.timestamp() + (after - before).total_seconds() * 2 / 3
    if ari is not None:
        # Some acme versions return a naive local datetime from Retry-After.
        selected = ari.timestamp()
        if before.timestamp() < selected < after.timestamp():
            return selected
    fraction = random.SystemRandom().random() if jitter is None else max(0, min(1, jitter))
    return fallback - (after - before).total_seconds() * 0.05 * fraction


def public_task(task: dict) -> dict:
    """An allowlist, also used by read-only diagnostics. Never serialize a job wholesale."""
    return {
        key: task[key]
        for key in (
            "id",
            "name",
            "domain",
            "environment",
            "phase",
            "addresses",
            "confirmed",
            "terms_url",
            "error",
            "needs_attention",
            "next_attempt",
            "certificate_id",
            "label",
            "cleanup_pending",
            "cleanup_released",
            "cleanup_note",
            "cleanup_record_ids",
            "cleanup_name",
            "renewal_note",
            "removal_pending",
        )
        if key in task
    }


def has_dns_work(task: dict) -> bool:
    return any(
        task.get(key)
        for key in ("cleanup_pending", "dns_receipt", "dns_intent", "dns_receipts", "dns_intents")
    )


def run_operation(root: Path, task: dict) -> None:
    state = PrivateState(root)
    now = time.time()

    def save(data: dict) -> None:
        state.write(data["id"], data)

    try:
        environment = task["environment"]
        if environment not in DIRECTORIES:
            raise AcmeError("Unknown ACME environment.")
        dns = CloudflareDNS(task["zone_id"], task["token"])
        if task["action"] == "cleanup":
            cleanup_dns(task, save, dns)
            task.update(phase="complete", cleanup_pending=False, error="", needs_attention=False)
            task["next_attempt"] = task.get("renew_at", time.time() + 86400)
            task.update(task.pop("after_cleanup", {}))
            if task["phase"] == "complete":
                task.pop("renew_at", None)
            if task["environment"] == "staging" and task["phase"] == "complete":
                task.pop("token", None)
            save(task)
            return
        task["domain"] = validate_domain(task["domain"], dns.zone())
        account = state.read("account-" + environment)
        account["environment"] = environment
        authority = Authority(
            account, lambda data: state.write("account-" + environment, data), Budget(root), now
        )
        task["terms_url"] = authority.terms
        if task["action"] == "preflight":
            task.update(preflight(task["domain"]))
            task.update(
                phase="ready",
                error="",
                needs_attention=False,
                label=STAGING_LABEL if environment == "staging" else "Let's Encrypt",
            )
        else:
            if not task.get("accepted_at") or task.get("accepted_terms") != authority.terms:
                raise AcmeError("Review and accept the current ACME terms before issuance.")
            observed = preflight(task["domain"])
            task.update(observed)
            if not observed["confirmed"] and not task.get("host_confirmed"):
                raise AcmeError(
                    "Confirm that devices on your network reach this host at the domain."
                )
            account.update(terms_url=authority.terms, accepted_at=task["accepted_at"])
            authority.register(environment)
            certificate, private_key, ari = authority.issue(task, save, dns)
            cert = x509.load_pem_x509_certificate(certificate.encode())
            task.update(
                phase="issued",
                certificate=certificate,
                private_key=private_key,
                next_attempt=renewal_time(cert, ari),
                error="",
                needs_attention=False,
                failures=0,
            )
    except AcmeError as exc:
        task["failures"] = task.get("failures", 0) + 1
        task.update(
            phase="error",
            error=str(exc),
            needs_attention=exc.permanent,
            next_attempt=retry_time(now, task["failures"], exc.retry_at),
        )
    except Exception:
        # Never persist remote messages, exception reprs or tracebacks containing credentials.
        task["failures"] = task.get("failures", 0) + 1
        task.update(
            phase="error",
            error="ACME failed. Check connectivity and the saved DNS credentials.",
            needs_attention=False,
            next_attempt=retry_time(now, task["failures"], 0),
        )
    save(task)


# Kept patchable at the service boundary for offline CA tests.
from autodj.acme_client import Authority, cleanup_dns  # noqa: E402
