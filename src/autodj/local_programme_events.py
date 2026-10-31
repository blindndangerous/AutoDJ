"""Bounded render receipts. No database work runs on the audio thread."""

import threading
from collections import OrderedDict, deque
from typing import Any


class ProgrammeEvents:
    """Serialize markers with PCM; retain heard receipts until durable completion."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, str] = {}
        self._pending: deque[dict[str, Any]] = deque()
        self._heard: deque[dict[str, Any]] = deque()
        self._completed: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self.next_sequence = 0
        self._last_frame = -1

    def reserve(self, occurrence: str) -> None:
        with self._lock:
            if len(self._states) >= 2 or occurrence in self._states:
                raise ValueError("At most two local occurrences may be reserved")
            self._states[occurrence] = "reserved"

    def submit(self, event: dict[str, Any], next_frame: int) -> str:
        with self._lock:
            if set(event) != {"sequence", "kind", "occurrence", "frame"}:
                raise ValueError("Invalid occurrence marker fields")
            sequence, frame = event["sequence"], event["frame"]
            if type(sequence) is not int or type(frame) is not int:
                raise ValueError("Occurrence sequence and frame must be integers")
            for old in (*self._completed.values(), *self._heard, *self._pending):
                if old["sequence"] == sequence:
                    if old != event:
                        raise ValueError("Conflicting occurrence retry")
                    return "completed" if sequence in self._completed else "pending"
            if len(self._pending) + len(self._heard) >= 64:
                raise ValueError("Occurrence receipt budget exhausted")
            if sequence != self.next_sequence or not next_frame <= frame <= next_frame + 44100:
                raise ValueError("Stale or future occurrence marker")
            if frame < self._last_frame or not isinstance(event["occurrence"], str):
                raise ValueError("Occurrence markers must follow render order")
            state = self._states.get(event["occurrence"])
            allowed = {
                "started": {"reserved"},
                "load-failure": {"reserved"},
                "paused": {"started", "resumed"},
                "resumed": {"paused"},
                "ended": {"started", "resumed", "paused"},
            }
            if not isinstance(event["kind"], str) or state not in allowed.get(event["kind"], set()):
                raise ValueError("Unreserved occurrence or invalid playback event order")
            self._states[event["occurrence"]] = event["kind"]
            self._pending.append(dict(event))
            self._last_frame = frame
            self.next_sequence += 1
            return "pending"

    def consume(self, spans: list[tuple[int, int]]) -> None:
        with self._lock:
            for first, count in spans:
                while self._pending and self._pending[0]["frame"] < first + count:
                    if self._pending[0]["frame"] < first:
                        raise ValueError("Occurrence marker is missing its consumed PCM span")
                    self._heard.append(self._pending.popleft())

    def receipts(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._heard)

    def complete(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._heard or self._heard[0] != event:
                raise ValueError("Complete receipts in consumption order")
            self._heard.popleft()
            self._completed[event["sequence"]] = event
            if len(self._completed) > 256:
                self._completed.popitem(last=False)
            if event["kind"] in {"ended", "load-failure"}:
                self._states.pop(event["occurrence"], None)

    def cancel(self) -> None:
        with self._lock:
            self._pending.clear()
