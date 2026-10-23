"""Build route-test apps through the production station factory interface."""

from contextlib import asynccontextmanager

from autodj._bridge import LibraryServices
from autodj.scheduler import system_timezone
from autodj.server import create_app as server_app
from autodj.station_log import new_station_scope
from autodj.stations import ExtraStation, StationRuntime


def create_app(
    bridge=None,
    player_thread=None,
    *,
    stream_mode=False,
    stream_first_track=None,
    server_audio=False,
    extra_stations=(),
    **kwargs,
):
    if bridge is None:
        return server_app(extra_stations=extra_stations, **kwargs)
    cfg = bridge.player._cfg
    runtime = StationRuntime(
        bridge,
        timezone=cfg.station.timezone or system_timezone(),
        player_thread=player_thread,
        stream_mode=stream_mode,
        server_audio=server_audio,
        first_track=stream_first_track,
        thread_scope=new_station_scope("default"),
    )
    factory = ExtraStation(
        slug="default",
        name=cfg.stream.station_name,
        autostart=True,
        paths=runtime.paths,
        preset=getattr(getattr(cfg, "playback", None), "stereotool_preset", "included"),
        build=lambda _registry: runtime,
        requests_enabled=cfg.requests.enabled,
        player_page=getattr(cfg, "player_page", None),
    )
    services = LibraryServices(
        cfg, getattr(bridge, "sim", getattr(bridge.player, "_sim", None)), bridge.library
    )
    app = server_app(
        services=services,
        default_station=factory,
        extra_stations=extra_stations,
        **kwargs,
    )
    app.state.library_services = services
    registry = app.state.stations
    # Route-only clients omit lifespan; make their fixture available immediately.
    registry.prepare(runtime)
    registry._running["default"] = runtime
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        # Context-managed clients exercise the actual factory startup/shutdown.
        registry._running.pop("default", None)
        async with original_lifespan(app):
            yield

    app.router.lifespan_context = lifespan
    return app
