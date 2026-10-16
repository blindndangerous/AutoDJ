"""Saved station settings and content, without an audio engine."""

from __future__ import annotations

from typing import Any

from autodj._bridge import LibraryAccess
from autodj.station_settings import StationSettings


class SavedBridge(StationSettings, LibraryAccess):
    """Settings and library operations available while playback is stopped."""

    stream_mode = False
    station = None
    liner_scheduler = None
    relays = None
    mounts = None

    def __init__(self, cfg: Any, library: Any, sim: Any, paths: Any) -> None:
        super().__init__(cfg, library, sim, paths)
        self.ledger = library.station_ledger(paths.slug)

    def _ledger(self) -> Any:
        return self.ledger

    def relay_status(self) -> list[dict]:
        return []


class StationContent:
    """A stopped station's file-backed editors; no threads or playback resources."""

    stopped = True
    stream_mode = False
    player = None
    recorder = None
    requests = None
    listener_access = None
    listener_stats = None

    def __init__(self, cfg: Any, paths: Any, library: Any, sim: Any) -> None:
        from autodj.live import LiveDesk
        from autodj.relay_settings import load_relays
        from autodj.scheduler import system_timezone
        from autodj.station_routes import equip_station
        from autodj.stations import _liner_gate

        self.cfg = cfg
        self.paths = paths
        self.slug = paths.slug
        self.timezone = cfg.station.timezone or system_timezone()
        cfg.stream.relays = load_relays(paths.relays, cfg.stream.relays, self.slug)
        self.bridge = SavedBridge(cfg, library, sim, paths)
        self.bridge.load_persistent_state()
        self.ledger = self.bridge.ledger
        self.liner_gate = _liner_gate(cfg, library, paths)
        self.live = LiveDesk(paths.live_djs)
        self.carts: Any = None
        self.schedule: Any = None
        self.time_checks: Any = None
        self.shows: Any = None
        self.show_runner: Any = None
        self.breaks: Any = None
        self.ads: Any = None
        self.intros: Any = None

        async def publish(_message: dict[str, Any]) -> None:
            # Stopped stations have no playback WebSocket; edit replies update the page.
            pass

        equip_station(self, publish)

    @property
    def name(self) -> str:
        return self.cfg.stream.station_name
