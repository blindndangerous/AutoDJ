"""Small, path-free public snapshot of the radio station."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from autodj._bridge import PlayerBridge

#: Version of the public payload; 2 added the peak and uptime fields.
SCHEMA_VERSION = 2


def _track(entry: Any) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "artist": str(getattr(entry, "artist", "") or ""),
        "title": str(getattr(entry, "title", "") or ""),
        "album": str(getattr(entry, "album", "") or ""),
        "duration": float(getattr(entry, "length", 0) or 0),
    }


class NowPlaying:
    """Cache metadata when playback or the audience changes."""

    def __init__(
        self,
        bridge: PlayerBridge,
        *,
        timezone: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bridge = bridge
        self._lock = threading.Lock()
        self._payload: dict[str, Any] = {}
        # The calendar day in the station time zone ([server]
        # station_timezone, as the scheduler uses) and the most listeners
        # seen on it.  Kept in memory only, so a restart starts the count again.
        self._zone = ZoneInfo(timezone)
        self._clock = clock
        self._peak_day: date | None = None
        self._peak = 0
        self._monotonic = monotonic
        self._started = monotonic()

    def _peak_today(self, listeners: int) -> int:
        """Fold *listeners* into today's peak, starting a new day at midnight."""
        day = self._clock().astimezone(self._zone).date()
        with self._lock:
            if day != self._peak_day:
                self._peak_day = day
                self._peak = 0
            self._peak = max(self._peak, listeners)
            return self._peak

    def _live_fields(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return *payload* with the peak and uptime as of now."""
        return {
            **payload,
            "peak_listeners_today": self._peak_today(int(payload.get("listener_count") or 0)),
            "uptime_seconds": max(0, int(self._monotonic() - self._started)),
        }

    def refresh(self) -> dict[str, Any]:
        """Replace the cached snapshot and return it for webhook delivery."""
        cfg = self.bridge.player._cfg
        state = self.bridge.player._state
        current = _track(state.current_track)
        history = self.bridge.history_snapshot()
        open_play = getattr(self.bridge, "_open_play", None)
        started_at = (
            open_play[1]["played_at"]
            if current and open_play is not None and open_play[0] is state.current_track
            else None
        )
        if current is not None:
            current["started_at"] = started_at
            if started_at:
                elapsed = (datetime.now(UTC) - datetime.fromisoformat(started_at)).total_seconds()
                current["elapsed"] = max(0.0, min(current["duration"], elapsed))
            else:
                current["elapsed"] = 0.0
            if started_at:
                history = history[:-1]
        limit = cfg.nowplaying.history_limit
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "station_name": cfg.stream.station_name,
            "current_track": current,
            # DMCA mode never announces a song before it plays.
            "next_track": (
                _track(state.next_track)
                if cfg.nowplaying.show_next and not cfg.dmca.enabled
                else None
            ),
            "history": list(reversed(history[-limit:])) if limit else [],
            "listener_count": self.bridge.stream.listener_count if self.bridge.stream else 0,
            "stream_path": "/stream.mp3",
        }
        with self._lock:
            self._payload = payload
        return self._live_fields(payload)

    def snapshot(self) -> dict[str, Any]:
        """Return cached metadata with a cheap elapsed-time adjustment."""
        with self._lock:
            payload = self._payload
        if not payload:
            self.refresh()
            with self._lock:
                payload = self._payload
        payload = self._live_fields(payload)
        current = payload.get("current_track")
        if not current or not current.get("started_at"):
            return payload
        position = getattr(self.bridge.player, "_playback_pos", None)
        sample_rate = getattr(self.bridge.player, "_current_sr", None)
        if (
            isinstance(position, (list, tuple))
            and position
            and isinstance(sample_rate, int | float)
        ):
            elapsed = float(position[0]) / max(1.0, float(sample_rate))
        else:
            elapsed = (
                datetime.now(UTC) - datetime.fromisoformat(current["started_at"])
            ).total_seconds()
        return {
            **payload,
            "current_track": {**current, "elapsed": max(0.0, min(current["duration"], elapsed))},
        }
