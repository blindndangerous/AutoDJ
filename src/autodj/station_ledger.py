"""One station's plays, play log and settings in the shared ``library.db`` (#70).

The library and its catalog are shared by every station, but what a
station played is its own: the play log rows, each track's last-played
time (for least-recent picks, clockwheel selection and liner rotation)
and settings such as the active clockwheel and its place.

A :class:`StationLedger` buffers these the way the library always has:
the player and the mix bus record a play without touching SQLite, and
:meth:`StationLedger.flush` writes them on the library's tick.  The
default station keeps today's storage exactly (``catalog_entries.last_played``
and the ``"clock"`` setting); any other station keeps its last plays in
``station_plays`` and its settings under ``"<name>:<slug>"`` keys.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodj.catalog import DEFAULT_STATION, _path

if TYPE_CHECKING:
    from autodj.catalog import LibraryCatalog
    from autodj.indexer import IndexEntry

# StationLedger._selected_clock before any clockwheel is selected.
_UNSELECTED = object()


def setting_key(station: str, name: str) -> str:
    """The ``catalog_settings`` key of setting *name* for *station*.

    The default station keeps the key it always had (``"clock"``); any
    other station's is ``"clock:<slug>"``.
    """
    return name if station == DEFAULT_STATION else f"{name}:{station}"


class StationLedger:
    """One station's buffered plays, play log rows and settings.

    Thread-safety: the mix bus and the player record plays, request
    handlers read them and the library's tick flushes them; one lock
    covers the buffers.  SQLite is written only by :meth:`flush`.
    """

    def __init__(
        self,
        catalog: LibraryCatalog,
        station: str = DEFAULT_STATION,
        entry_for_path: Callable[[str], IndexEntry | None] | None = None,
    ) -> None:
        """Keep *station*'s plays for *catalog*.

        Args:
            catalog: The shared library catalog.
            station: The station's name.
            entry_for_path: Finds a track's library entry, for the album of
                a play not yet written.
        """
        self.catalog = catalog
        self.station = station
        self._entry_for_path = entry_for_path
        self._lock = threading.Lock()
        # Held while settings are written, so a flush in progress cannot put
        # back a value select_clock has just replaced.
        self._settings_lock = threading.Lock()
        self._pending_plays: dict[str, float] = {}
        self._pending_history: dict[str, dict] = {}
        self._interrupted_plays: set[str] = set()
        self._pending_settings: dict[str, Any] = {}
        # The clockwheel last selected through select_clock, or _UNSELECTED
        # before any selection: the player's place in another one is stale.
        self._selected_clock: Any = _UNSELECTED
        self._last_played: dict[str, float] = {}

    def setting_key(self, name: str) -> str:
        """The ``catalog_settings`` key of this station's setting *name*."""
        return setting_key(self.station, name)

    # ------------------------------------------------------------------
    # Recording, from playout threads
    # ------------------------------------------------------------------

    def remember_play(self, path: str | Path, timestamp: float | None = None) -> None:
        """Note that the station played *path* now (or at *timestamp*)."""
        import time

        value = time.time() if timestamp is None else float(timestamp)
        if not math.isfinite(value):
            return
        normalized = _path(path)
        with self._lock:
            self._pending_plays[normalized] = value
            self._last_played[normalized] = value

    def remember_setting(self, name: str, value: Any) -> None:
        """Buffer a runtime setting update without writing SQLite on an audio thread.

        A place in a clockwheel other than the one last selected is dropped:
        the player can still hold the old one until it reloads (#133).
        """
        with self._lock:
            if name == "clock" and not self._is_selected_clock(value):
                return
            self._pending_settings[self.setting_key(name)] = value

    def select_clock(
        self, name: str | None, cursor: int | None = None, renamed_from: str | None = None
    ) -> dict:
        """Make clockwheel *name* the station's active one and return the setting.

        Without a *cursor*, reselecting the active clockwheel keeps its
        latest place, including one the player has not written yet; so does
        renaming it, from *renamed_from*.  Any other clockwheel starts at its
        first step.  The read, the write and the choice the player's later
        updates are checked against happen under one lock, so neither a
        flush nor the player can put the old clockwheel back (#133).
        """
        key = self.setting_key("clock")
        with self._settings_lock:
            with self._lock:
                buffered = self._pending_settings.get(key)
            setting = buffered if buffered is not None else self.catalog.get_setting(key, {})
            setting = setting if isinstance(setting, dict) else {}
            if cursor is None:
                kept = setting.get("cursor")
                was = setting.get("name")
                same = was is not None and was in (name, renamed_from)
                cursor = kept if same and type(kept) is int else 0
            value = {"name": name, "cursor": cursor}
            self.catalog.set_setting(key, value)
            with self._lock:
                self._selected_clock = name
                # The update read above is written now, and one the player
                # buffered since for the old clockwheel is stale.
                latest = self._pending_settings.get(key)
                if latest == buffered or not self._is_selected_clock(latest):
                    self._pending_settings.pop(key, None)
        return value

    def _is_selected_clock(self, value: Any) -> bool:
        """Whether *value* is a place in the selected clockwheel (hold ``_lock``)."""
        if self._selected_clock is _UNSELECTED:
            return True
        return isinstance(value, dict) and value.get("name") == self._selected_clock

    def remember_history(self, row: dict) -> None:
        """Queue a play-log entry without writing SQLite on the audio thread."""
        with self._lock:
            self._pending_history[row["event_id"]] = dict(row)

    def interrupt_history(self, event_id: str) -> None:
        """Mark a play a set stop cut short, so the play log leaves it out."""
        with self._lock:
            self._interrupted_plays.add(event_id)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def last_played(self) -> dict[str, float]:
        """When the station last played each track, including plays not yet written."""
        with self._lock:
            return dict(self._last_played)

    def pending_history(self) -> list[dict]:
        """Plays not yet written to SQLite, oldest first, with each track's album.

        The rows :meth:`recent_plays` gives once they are written: a play a
        set stop cut short is left out, and a track no longer in the library
        has an empty album.
        """
        with self._lock:
            rows = [
                dict(row)
                for event_id, row in self._pending_history.items()
                if event_id not in self._interrupted_plays
            ]
        for row in rows:
            entry = self._entry_for_path(row.get("path") or "") if self._entry_for_path else None
            row["album"] = entry.album if entry is not None else ""
        return rows

    def get_setting(self, name: str, default: Any = None) -> Any:
        """The station's setting *name*, including an update not yet written."""
        key = self.setting_key(name)
        with self._lock:
            if key in self._pending_settings:
                return self._pending_settings[key]
        return self.catalog.get_setting(key, default)

    def recent_history(self, limit: int = 500) -> list[dict]:
        """The station's latest *limit* completed plays, oldest first."""
        return self.catalog.recent_history(limit, station=self.station)

    def recent_plays(self, limit: int) -> list[dict]:
        """The station's latest *limit* plays for the repeat rules, oldest first."""
        return self.catalog.recent_plays(limit, station=self.station)

    def play_log(self) -> list[dict]:
        """Every completed play of the station, oldest first."""
        return self.catalog.play_log(station=self.station)

    # ------------------------------------------------------------------
    # The library's side
    # ------------------------------------------------------------------

    def loaded(self, played: dict[str, float]) -> None:
        """Take last-played times read from the catalog; a later play known here wins.

        A play flushed after the catalog was read is no longer pending, so
        the later of the two times is kept rather than the catalog's.
        """
        with self._lock:
            known = self._last_played
            for path, value in played.items():
                if value >= known.get(path, value):
                    known[path] = value
            known.update(self._pending_plays)

    def flush(self) -> None:
        """Write buffered plays, play log rows and settings to the catalog."""
        self._flush_plays()
        self._flush_history()
        self._flush_settings()

    def _flush_plays(self) -> None:
        with self._lock:
            pending = dict(self._pending_plays)
        for path, value in pending.items():
            self.catalog.remember_play(path, value, station=self.station)
            with self._lock:
                if self._pending_plays.get(path) == value:
                    self._pending_plays.pop(path, None)

    def _flush_settings(self) -> None:
        with self._settings_lock:
            with self._lock:
                pending = dict(self._pending_settings)
            for key, value in pending.items():
                self.catalog.set_setting(key, value)
                with self._lock:
                    if self._pending_settings.get(key) == value:
                        self._pending_settings.pop(key, None)

    def _flush_history(self) -> None:
        with self._lock:
            pending = dict(self._pending_history)
            interrupted = set(self._interrupted_plays)
        if not pending and not interrupted:
            return
        self.catalog.write_history(list(pending.values()), list(interrupted), station=self.station)
        with self._lock:
            for event_id, row in pending.items():
                if self._pending_history.get(event_id) == row:
                    self._pending_history.pop(event_id, None)
            self._interrupted_plays.difference_update(interrupted)


__all__ = ["StationLedger", "setting_key"]
