"""Small, path-free public snapshot of the radio station."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from autodj._bridge import PlayerBridge
from autodj.station_name import listener_name
from autodj.stream_mounts import StreamMounts

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


def _history_row(row: dict[str, Any]) -> dict[str, Any]:
    """A History row for the public feed; a live DJ's set (#48) also says so and when it ended."""
    public = {key: row.get(key) for key in ("artist", "title", "duration", "played_at")}
    if row.get("kind") == "live":
        public.update(kind="live", ended_at=row.get("ended_at"))
    return public


def _listeners(bridge: Any) -> int:
    """Listeners on every stream format together (a test double may have only the MP3 one)."""
    mounts = getattr(bridge, "mounts", None)
    if isinstance(mounts, StreamMounts):
        return mounts.listener_count
    return int(bridge.stream.listener_count) if bridge.stream else 0


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
        # Live DJ input (#48): the DJ on the air, if any, and the title shown.
        self.live_status: Callable[[], dict[str, str] | None] = lambda: None
        self.live_title = "Live"
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
        live = self.live_status()
        if live is not None:
            current = {
                "artist": live["dj"],
                "title": self.live_title,
                "album": "",
                "duration": 0.0,
                "started_at": live["since"],
                "elapsed": 0.0,
                "kind": "live",
            }
        history = self.bridge.history_snapshot()
        if live is not None and history and history[-1].get("kind") == "live":
            history = history[:-1]
        open_play = getattr(self.bridge, "_open_play", None)
        started_at = (
            open_play[1]["played_at"]
            if current and open_play is not None and open_play[0] is state.current_track
            else None
        )
        if current is not None and live is None:
            current["started_at"] = started_at
            # The playing track's own row, which is last unless it is not
            # music (a show's speech segment, #601) and so not listed.
            playing_id = open_play[1].get("event_id") if started_at and open_play else None
            if started_at:
                elapsed = (datetime.now(UTC) - datetime.fromisoformat(started_at)).total_seconds()
                current["elapsed"] = max(0.0, min(current["duration"], elapsed))
            else:
                current["elapsed"] = 0.0
            if started_at and history and playing_id in (None, history[-1].get("event_id")):
                history = history[:-1]
        limit = cfg.nowplaying.history_limit
        # The station's own stream address (a test double may not have one).
        stream_path = getattr(self.bridge, "stream_path", None)
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            # The clockwheel's or show's name while one asks for it (#472).
            "station_name": listener_name(self.bridge),
            "current_track": current,
            "live": live,
            # DMCA mode never announces a song before it plays.
            "next_track": (
                _track(state.next_track)
                if live is None and cfg.nowplaying.show_next and not cfg.dmca.enabled
                else None
            ),
            "history": [_history_row(row) for row in reversed(history[-limit:])] if limit else [],
            "listener_count": _listeners(self.bridge),
            "stream_path": stream_path if isinstance(stream_path, str) else "/stream.mp3",
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
        if current.get("kind") == "live":
            elapsed = (
                datetime.now(UTC) - datetime.fromisoformat(current["started_at"])
            ).total_seconds()
            return {**payload, "current_track": {**current, "elapsed": max(0.0, elapsed)}}
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
