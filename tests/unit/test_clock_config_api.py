import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from autodj.catalog import LibraryCatalog
from autodj.config import ServerConfig, StreamConfig
from autodj.server import ClockBody, PalImportBody, create_app


class Library:
    def __init__(self, catalog):
        self.catalog = catalog


def app_for(catalog):
    player = SimpleNamespace(
        _cfg=SimpleNamespace(
            server=ServerConfig(),
            stream=StreamConfig(),
            index=SimpleNamespace(active_dir="index"),
        ),
        _clock_status="Idle",
    )
    player.configure_clock = Mock()
    bridge = SimpleNamespace(player=player, library=Library(catalog))
    app = create_app(bridge)
    routes = {
        (method, route.path): route.endpoint
        for route in app.routes
        if "/api/library/clock" in route.path
        for method in (route.methods or set())
    }
    return app, bridge, routes


def test_clock_api_import_persists_safe_pal_and_reports_ready_counts(tmp_path):
    music = tmp_path / "music"
    liners = tmp_path / "liners"
    music.mkdir()
    liners.mkdir()
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        catalog.add_source(music, "music", recursive=True, advanced=False)
        catalog.add_source(liners, "liner", recursive=True, advanced=False)
        _app, bridge, routes = app_for(catalog)
        pal = (
            f"Dir['{music}'].QueueBottom(smLRP, EnforceRules);\n"
            f"Dir['{liners}'].QueueBottom(smrandom, NoRules);"
        )
        result = asyncio.run(
            routes[("POST", "/api/library/clock/import")](
                PalImportBody(text=pal, name="Plague", resync_minutes=[0, 30])
            )
        )
        assert result["name"] == "Plague"
        assert result["resync_minutes"] == [0, 30]
        assert [slot["kind"] for slot in result["slots"]] == ["music", "liner"]
        assert [slot["ready_count"] for slot in result["slots"]] == [0, 0]
        assert result["last_status"] == "Idle"
        assert catalog.get_setting("clock")["name"] == "Plague"
        bridge.player.configure_clock.assert_called_once_with()

        loaded = asyncio.run(routes[("GET", "/api/library/clock")]())
        assert loaded["slots"] == result["slots"]

        with pytest.raises(HTTPException) as rejected:
            asyncio.run(
                routes[("POST", "/api/library/clock/import")](
                    PalImportBody(text="Execute('not allowed');", name="Unsafe")
                )
            )
        assert rejected.value.status_code == 400


def test_clock_api_save_preserves_cursor_and_requires_matching_recursive_source(tmp_path):
    music = tmp_path / "music"
    nested = music / "nested"
    nested.mkdir(parents=True)
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        catalog.add_source(music, "music", recursive=False, advanced=False)
        catalog.set_setting(
            "clock", {"name": "Old", "slots": [], "resync_minutes": [0], "cursor": 4}
        )
        _app, _bridge, routes = app_for(catalog)
        with pytest.raises(HTTPException, match="not covered") as rejected:
            asyncio.run(
                routes[("POST", "/api/library/clock")](
                    ClockBody(name="Nested", slots=[{"folder": str(nested)}], resync_minutes=[0])
                )
            )
        assert rejected.value.status_code == 400

        catalog.add_source(nested, "music", recursive=False, advanced=False)
        saved = asyncio.run(
            routes[("POST", "/api/library/clock")](
                ClockBody(name="Nested", slots=[{"folder": str(nested)}], resync_minutes=[])
            )
        )
        assert saved["cursor"] == 4
        assert saved["resync_minutes"] == []
        assert catalog.get_setting("clock")["cursor"] == 4


def test_recursive_clock_parent_can_cover_registered_child_sources(tmp_path):
    station = tmp_path / "station" / "throwback"
    first, second = station / "album-one", station / "album-two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        catalog.add_source(first, "music", recursive=False, advanced=False)
        catalog.add_source(second, "music", recursive=False, advanced=False)
        _app, _bridge, routes = app_for(catalog)

        saved = asyncio.run(
            routes[("POST", "/api/library/clock")](
                ClockBody(
                    name="Throwback",
                    slots=[{"folder": str(station), "recursive": True}],
                    resync_minutes=[0],
                )
            )
        )
        assert saved["slots"][0]["folder"] == str(station)

        with pytest.raises(HTTPException, match="not covered") as rejected:
            asyncio.run(
                routes[("POST", "/api/library/clock")](
                    ClockBody(
                        name="Throwback",
                        slots=[{"folder": str(station), "recursive": False}],
                        resync_minutes=[0],
                    )
                )
            )
        assert rejected.value.status_code == 400
