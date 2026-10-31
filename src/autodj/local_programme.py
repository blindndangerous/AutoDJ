"""Station-owned, socket-bound lease for local programme input and recovery."""

import secrets
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from autodj.live import LiveDesk

if TYPE_CHECKING:
    from autodj.local_programme_audio import LocalPCM
    from autodj.local_programme_playout import LocalPerformance


class OwnershipError(ValueError):
    """A claim is busy, expired, stopped or no longer belongs to this socket."""


@dataclass(frozen=True)
class Grant:
    epoch: str
    generation: int
    capability: str
    identity: str
    socket_id: str


class LocalProgramme:
    """One controller per station run. Lock order: controller, then live desk."""

    def __init__(self, live: LiveDesk) -> None:
        self.live = live
        self._lock = threading.RLock()
        self._epoch = secrets.token_hex(16)
        self._generation = 0
        self._grant: Grant | None = None
        self._lease_until = 0.0
        self._prepare_until = 0.0
        self._closed = False
        self._handshakes = 0
        self._control_tokens = 10.0
        self._control_previous: float | None = None
        self._reason = "automation"
        self.audio: LocalPCM | None = None
        self.performance: LocalPerformance | None = None

    def enter_handshake(self) -> bool:
        with self._lock:
            if self._closed or self._handshakes >= 2:
                return False
            self._handshakes += 1
            return True

    def leave_handshake(self) -> None:
        with self._lock:
            self._handshakes -= 1

    def consume_control(self, now: float) -> bool:
        """Station-wide control budget survives socket replacement and refusal."""
        with self._lock:
            previous = self._control_previous
            if previous is not None:
                self._control_tokens = min(10.0, self._control_tokens + max(0, now - previous) * 20)
            self._control_previous = now
            if self._control_tokens < 1:
                return False
            self._control_tokens -= 1
            return True

    def claim(self, identity: str, socket_id: str, now: float) -> Grant:
        from autodj.local_programme_audio import LocalPCM

        with self._lock:
            self.check(now)
            if self._closed or self._grant is not None:
                raise OwnershipError("Local control is already held or the station is stopped")
            if self.performance is not None:
                if self.performance.busy:
                    raise OwnershipError("Local performance recovery is still pending")
                self.performance.admit()
            self._generation += 1
            grant = Grant(self._epoch, self._generation, secrets.token_hex(32), identity, socket_id)
            if not self.live.reserve_local(grant):
                raise OwnershipError("A live source or microphone is connected")
            self._grant = grant
            self.audio = LocalPCM(grant, now=now)
            self._lease_until = now + 3
            self._prepare_until = now + 10
            return grant

    def check(self, now: float) -> bool:
        with self._lock:
            if self._grant is not None and now >= min(self._lease_until, self._prepare_until):
                self.revoke(self._grant.generation, "Local control preparation expired")
            return self._grant is not None

    def validate(self, grant: Grant, now: float) -> None:
        with self._lock:
            if not self.check(now) or grant != self._grant:
                raise OwnershipError("Local control lease is stale")

    def renew(self, grant: Grant, now: float) -> None:
        with self._lock:
            self.validate(grant, now)
            self._lease_until = now + 3

    def activated(self, grant: Grant) -> None:
        with self._lock:
            if grant != self._grant:
                raise OwnershipError("Local control lease is stale")
            self._prepare_until = float("inf")
            self._reason = "Local control"

    def revoke(self, generation: int, reason: str) -> None:
        with self._lock:
            grant = self._grant
            if grant is None or grant.generation != generation:
                return
            self._grant = None
            self._reason = reason
            if self.audio is not None:
                self.audio.close(reason)
                self.audio = None
            if self.performance is None or not self.performance.busy:
                self.live.release_local(grant)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._grant is not None:
                self.revoke(self._grant.generation, "Station stopped")

    def return_observed(self, epoch: str, generation: int) -> None:
        """Administrative return targets exactly the ownership the caller observed."""
        with self._lock:
            if epoch != self._epoch or generation != self._generation:
                raise OwnershipError("Local control changed; refresh before returning it")
            self.revoke(generation, "Returned to automation")

    def status(self) -> dict[str, str | int]:
        with self._lock:
            return {
                "state": (
                    "returning"
                    if self.performance and self.performance.returning
                    else "local"
                    if self.performance and self.performance.on_air
                    else "preparing"
                )
                if self._grant
                else ("recovering" if self.performance and self.performance.busy else "automation"),
                "generation": self._generation,
                "epoch": self._epoch,
                "reason": (
                    getattr(self.performance, "reason", "") or self._reason
                    if self.performance and self.performance.busy
                    else self._reason
                ),
            }
