"""Station adapter for the bounded, song-only local performance prototype."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autodj.local_programme import Grant, OwnershipError
from autodj.local_programme_audio import LocalPCM


@dataclass
class Occurrence:
    id: str
    entry: Any
    signature: tuple[int, int]
    queued: bool
    started: bool = False


def require_automation(player: Any) -> None:
    """Fence internal callers as well as HTTP controls, without changing disabled runs."""
    performance = getattr(player, "local_performance", None)
    if performance is not None and performance.busy is True:
        raise OwnershipError("Return to automation before changing station programming")


class LocalPerformance:
    """Control-thread work only. The bus writes bounded receipts on LocalPCM.

    The adapter lock may precede the bus or queue lock. The render thread never
    calls this object. A failed ledger flush keeps this object busy and retains
    its receipts; it cannot grant another performance over uncommitted history.
    """

    def __init__(self, station: Any) -> None:
        self.station = station
        self.player = station.player
        self.bridge = station.bridge
        self._lock = threading.RLock()
        self.audio: LocalPCM | None = None
        self.occurrences: list[Occurrence] = []
        self.on_air = False
        self._applied = -1
        self._armed = False
        self._render_generation = 0
        self._acks: list[dict[str, Any]] = []
        self.reason = "Automation"
        self.returning = False
        self._return_ready = False
        self._return_entry: Any = None
        self._return_entry_selected = False
        self._recovering = False
        self._return_done = threading.Event()
        self._return_done.set()

    @property
    def busy(self) -> bool:
        return self.audio is not None

    def admit(self) -> None:
        cfg = self.player._cfg
        for name, enabled in (
            ("Automatic liners", cfg.playback.liners_enabled),
            ("Breaks", cfg.breaks.enabled),
            ("Ads", cfg.ads.enabled),
            ("Time checks", cfg.time_checks.enabled),
        ):
            if enabled:
                raise OwnershipError(f"{name} must be disabled for local control")
        schedule = getattr(self.station, "schedule", None)
        if schedule is not None and (schedule.events or schedule.pending or schedule._tasks):
            raise OwnershipError("Scheduled events are not supported by local control")
        intros = getattr(self.station, "intros", None)
        if intros is not None and intros.all():
            raise OwnershipError("Matched intros are not supported by local control")
        runner = getattr(self.station, "show_runner", None)
        if runner is not None and runner.holds_air():
            raise OwnershipError("Shows are not supported by local control")
        if self.player._prepared_plan() is not None or getattr(
            self.player, "_live_voice_slot", None
        ):
            raise OwnershipError(
                "Prepared shows and booked voice are not supported by local control"
            )
        state = self.player._state
        with state.queue_lock:
            for entry in [state.current_track, state.next_track, state.queued_next, *state.queue]:
                if entry is not None and (
                    self.bridge._is_liner(entry) or getattr(entry, "_prepared_clip", False)
                ):
                    raise OwnershipError("Only songs are supported by local control")

    @staticmethod
    def _signature(entry: Any) -> tuple[int, int]:
        stat = Path(entry.path).stat()
        return stat.st_mtime_ns, stat.st_size

    def _queue(self) -> list[Any]:
        state = self.player._state
        return ([state.queued_next] if state.queued_next is not None else []) + list(state.queue)

    def _reserve(self) -> None:
        assert self.audio is not None
        state = self.player._state
        with state.queue_lock:
            available = self._queue()
            for occurrence in self.occurrences:
                if occurrence.queued and not occurrence.started:
                    for index, entry in enumerate(available):
                        if entry is occurrence.entry:
                            del available[index]
                            break
        while len(self.occurrences) < 2:
            with state.queue_lock:
                queued = bool(available)
                entry = available.pop(0) if queued else None
                previous = self.occurrences[-1].entry if self.occurrences else state.current_track
                if entry is None and not self.occurrences and not self.on_air:
                    entry = state.next_track
                if entry is None:
                    entry = self._pick_song(previous)
                if entry is None:
                    break
                if self.bridge._is_liner(entry) or getattr(entry, "_prepared_clip", False):
                    raise OwnershipError("Only songs are supported by local control")
            occurrence = Occurrence(secrets.token_hex(16), entry, self._signature(entry), queued)
            self.audio.events.reserve(occurrence.id)
            self.occurrences.append(occurrence)

    def _pick_song(self, previous: Any) -> Any:
        reserved = next((o.entry for o in self.occurrences if o.queued and not o.started), None)
        context = self.player._pick_context(
            pending=previous, peek_queue=True, queue_reserved=reserved
        )
        entry, _mode = self.player._choose_next(previous, context)
        return entry

    def prepare(self, grant: Grant) -> list[dict[str, Any]]:
        with self._lock:
            if self.returning:
                raise OwnershipError("Automation recovery is already in progress")
            self.admit()
            audio = self.station.local_programme.audio
            if audio is None or audio.grant != grant:
                raise OwnershipError("Local control lease is stale")
            self.audio = audio
            self._applied = -1
            self._armed = False
            self.occurrences = []
            self._acks = []
            self.returning = False
            self._return_ready = False
            self._return_entry = None
            self._return_entry_selected = False
            with self.player._state.queue_lock:
                self._render_generation = getattr(self.player, "_local_render_generation", 0)
            self._reserve()
            if not self.occurrences:
                raise OwnershipError("No song is available for local control")
            self.player._state.local_monitor_owned = True
            return self.views()

    def views(self) -> list[dict[str, Any]]:
        return [
            {
                "id": o.id,
                "path": str(o.entry.path),
                "title": o.entry.title,
                "artist": o.entry.artist,
                "duration": o.entry.length,
                "started": o.started,
            }
            for o in self.occurrences
        ]

    def validate_reservations(self) -> tuple[int, int]:
        self.admit()
        for occurrence in self.occurrences:
            if self._signature(occurrence.entry) != occurrence.signature:
                raise OwnershipError("A reserved source changed; return to automation")
        state = self.player._state
        with state.queue_lock:
            if (
                not self.on_air
                and getattr(self.player, "_local_render_generation", 0) != self._render_generation
            ):
                raise OwnershipError("Automation advanced during preparation; retry local control")
            queued = self._queue()
            for occurrence in self.occurrences:
                if (
                    occurrence.queued
                    and not occurrence.started
                    and (not queued or queued.pop(0) is not occurrence.entry)
                ):
                    raise OwnershipError("The reserved queue changed; retry local control")
            return self._handoff_revision()

    def _handoff_revision(self) -> tuple[int, int]:
        """Published integers only: safe to read while the bus owns its lock."""
        return (
            getattr(self.player, "_local_render_generation", 0),
            getattr(self.player, "_live_queue_revision", 0),
        )

    def _commit_handoff(self, revision: tuple[int, int]) -> None:
        if self._handoff_revision() != revision:
            raise OwnershipError("Automation advanced during preparation; retry local control")

    def validate_queue(self, queued_next: Any, queue: list[Any]) -> None:
        """Called before mutation, under the adapter and station queue locks."""
        proposed = ([queued_next] if queued_next is not None else []) + list(queue)
        for entry in proposed:
            if self.bridge._is_liner(entry) or getattr(entry, "_prepared_clip", False):
                raise OwnershipError("Only songs may be queued during local control")
        for occurrence in self.occurrences:
            if (
                occurrence.queued
                and not occurrence.started
                and (not proposed or proposed.pop(0) is not occurrence.entry)
            ):
                raise OwnershipError("A reserved occurrence cannot be moved or removed")

    def marker(self, event: dict[str, Any]) -> str:
        with self._lock:
            if self.audio is None:
                raise OwnershipError("No local performance is prepared")
            if self.returning and event.get("kind") == "started":
                raise OwnershipError("Returning to automation; no new starts are accepted")
            result = self.audio.events.submit(event, self.audio.next_sequence * 441)
            if not self._armed and event["kind"] == "started":
                self.audio.arm(event["frame"])
                self._armed = True
            return result

    def service(self) -> list[dict[str, Any]]:
        with self._lock:
            if self.audio is None:
                return []
            if not self.on_air and self._armed and self.audio.ready:
                revision = self.validate_reservations()
                self.player._state.local_monitor_owned = True
                self.player.bus.take_local_programme(
                    self.audio, commit=lambda: self._commit_handoff(revision)
                )
                self.on_air = True
                self.station.local_programme.activated(self.audio.grant)
                self.reason = "Local control"
            self.drain()
            acknowledgements, self._acks = self._acks, []
            return acknowledgements

    def drain(self) -> None:
        with self._lock:
            if self.audio is None:
                return
            for event in self.audio.events.receipts():
                occurrence = next(o for o in self.occurrences if o.id == event["occurrence"])
                if event["sequence"] > self._applied:
                    if event["kind"] == "started":
                        state = self.player._state
                        with state.queue_lock:
                            if occurrence.queued and not occurrence.started:
                                if state.queued_next is occurrence.entry:
                                    state.queued_next = None
                                else:
                                    index = next(
                                        i
                                        for i, e in enumerate(state.queue)
                                        if e is occurrence.entry
                                    )
                                    del state.queue[index]
                            occurrence.started = True
                            state.current_track = occurrence.entry
                        self.bridge.local_occurrence_started(occurrence.entry, occurrence.id)
                    elif event["kind"] == "ended":
                        self.bridge.local_occurrence_ended(occurrence.id)
                    elif event["kind"] in {"paused", "resumed"}:
                        self.player._state.is_paused = event["kind"] == "paused"
                    self._applied = event["sequence"]
                ledger = self.bridge._ledger()
                if ledger is not None:
                    ledger.flush()
                self.audio.events.complete(event)
                self._acks.append({"type": "completed", "sequence": event["sequence"]})
                if event["kind"] in {"ended", "load-failure"}:
                    self.occurrences.remove(occurrence)
                    if not self.audio.failed and not self.returning:
                        self._reserve()
            if self.on_air or any(o.started for o in self.occurrences):
                with self.player._state.queue_lock:
                    self.player._state.next_track = next(
                        (o.entry for o in self.occurrences if not o.started), None
                    )

    def begin_return(self) -> bool:
        """Reserve preparation synchronously before dispatching its worker."""
        with self._lock:
            if self.audio is None or not self.on_air or self.station.stopped:
                return False
            if self.returning:
                raise OwnershipError("Automation recovery is already in progress")
            self.returning = True
            self._return_done.clear()
            return True

    def _select_return_entry(self) -> Any:
        if not self._return_entry_selected:
            self._return_entry = next((o.entry for o in self.occurrences if not o.started), None)
            if self._return_entry is None:
                self._return_entry = self._pick_song(self.player._state.current_track)
            self._return_entry_selected = True
        return self._return_entry

    def prepare_return(self) -> bool:
        with self._lock:
            if not self.returning or self._return_done.is_set():
                raise OwnershipError("Automation preparation was not reserved")
            audio = self.audio
        try:
            with self._lock:
                self.drain()
                entry = self._select_return_entry()
            ready = self.player.prepare_local_return(entry, timeout=2.0)
            with self._lock:
                self._return_ready = bool(
                    ready and audio is not None and self.audio is audio and not audio.failed
                )
                return self._return_ready
        finally:
            self._return_done.set()

    def recover(self, *, expected_generation: int | None = None) -> None:
        if not self._return_done.wait(2.5):
            raise OwnershipError("Automation preparation has not finished; retry Return")
        with self._lock:
            if not self._return_done.is_set():
                raise OwnershipError("Automation preparation has not finished; retry Return")
            if self.audio is None:
                return
            if (
                expected_generation is not None
                and self.audio.grant.generation != expected_generation
            ):
                raise OwnershipError("Local performance changed before recovery")
            if self._recovering:
                raise OwnershipError("Automation recovery is already in progress")
            self.drain()
            audio = self.audio
            needs_audio = self.on_air and not self.station.stopped
            ready = self._return_ready
            next_entry = self._select_return_entry() if needs_audio and not ready else None
            self._recovering = True
            self.returning = True
        try:
            if needs_audio and not ready:
                # Rendering may wait two seconds. Keep markers, status, Stop,
                # and competing cleanup attempts responsive during that wait.
                ready = self.player.prepare_local_return(next_entry, timeout=2.0)
            with self._lock:
                if self.audio is not audio:
                    raise OwnershipError("Local performance changed during recovery")
                if needs_audio and not self.station.stopped:
                    if not ready:
                        self.reason = "Recovering: waiting for automation audio"
                        raise OwnershipError(self.reason)
                    self._return_ready = True
                self.reason = "Automation"
                for occurrence in self.occurrences:
                    if occurrence.started:
                        self.bridge.local_occurrence_ended(occurrence.id)
                ledger = self.bridge._ledger()
                if ledger is not None:
                    ledger.flush()
                if needs_audio and not self.station.stopped:
                    self.player._state.is_paused = False
                    self.player.bus.return_local_programme()
                self.station.local_programme.live.release_local(audio.grant)
                self.audio = None
                self.occurrences = []
                self.on_air = False
                self.returning = False
                self._return_ready = False
                self._return_entry = None
                self._return_entry_selected = False
                self.player._state.local_monitor_owned = False
        finally:
            with self._lock:
                self._recovering = False
