"""The paired clockwheel routes store native files and select one clockwheel."""

import asyncio
import functools
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from autodj.catalog import LibraryCatalog
from autodj.clockwheel_store import OLD_CLOCK_MESSAGE
from autodj.config import AdsConfig, RequestsConfig, ServerConfig, StreamConfig, TimeChecksConfig
from autodj.server import create_app
from autodj.station_ledger import StationLedger
from autodj.station_routes import ClockBody, ClockNameBody, NewClockNameBody, PalImportBody

PAL = """Queue.Clear;
Dir['{music}/top'].QueueBottom(smLRP, EnforceRules);
Dir['{liners}/drops'].QueueBottom(smrandom, NoRules);
subDir['{music}'].QueueBottom(smrandom, EnforceRules);
"""


def app_for(catalog, root):
    cfg = SimpleNamespace(
        server=ServerConfig(),
        stream=StreamConfig(),
        time_checks=TimeChecksConfig(),
        requests=RequestsConfig(),
        ads=AdsConfig(),
        index=SimpleNamespace(active_dir=root),
        presets={},
    )
    player = SimpleNamespace(_cfg=cfg, _clock_status="Idle", _clock=None)
    player.configure_clock = Mock()
    player.configure_clock_after_selection = Mock()
    player.apply_clockwheel_update = Mock()
    library = SimpleNamespace(
        catalog=catalog,
        entry_for_path=lambda _path: None,
        entries_snapshot=lambda _kind="music": (),
        ledger=StationLedger(catalog),
    )
    bridge = SimpleNamespace(player=player, library=library, _ledger=lambda: library.ledger)
    app = create_app(bridge)
    # Each clockwheel route acts on the station it is called for: the default one.
    station = app.state.stations.default
    routes = {
        (method, route.path): functools.partial(route.endpoint, station)
        for route in app.routes
        if "/api/library/clock" in route.path
        for method in (route.methods or set())
    }
    return bridge, routes


def call(routes, method, path, *args):
    return asyncio.run(routes[(method, path)](*args))


def body(name="Morning", steps=(), minutes=(0,)):
    return ClockBody(version=1, name=name, steps=list(steps), resync_minutes=list(minutes))


def add_sources(catalog, tmp_path, *, recursive=True):
    music, liners = tmp_path / "Music", tmp_path / "Liners"
    for folder in (music / "top", liners / "drops"):
        folder.mkdir(parents=True)
    return (
        catalog.add_source(music, "music", recursive=recursive, advanced=False),
        catalog.add_source(liners, "liner", recursive=True, advanced=False),
    )


def pal(tmp_path):
    return PAL.format(music=tmp_path / "Music", liners=tmp_path / "Liners")


def test_pal_import_maps_nested_folders_to_their_source(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        imported = call(
            routes,
            "POST",
            "/api/library/clock/import",
            PalImportBody(text=pal(tmp_path), name="Morning", resync_minutes=[30, 0]),
        )
        assert imported["resync_minutes"] == [0, 30]
        steps = [(s["type"], s["source"], s["folder"], s["recursive"]) for s in imported["steps"]]
        assert steps == [
            ("music", music["id"], "top", False),
            ("liner", liners["id"], "drops", False),
            ("music", music["id"], "", True),
        ]
        saved = json.loads((tmp_path / "clockwheels" / "Morning.json").read_text("utf-8"))
        assert saved == imported
        assert call(routes, "GET", "/api/library/clock/{name}", "Morning") == imported
        with pytest.raises(HTTPException, match="already exists"):
            call(
                routes,
                "POST",
                "/api/library/clock/import",
                PalImportBody(text=pal(tmp_path), name="morning", resync_minutes=[0]),
            )
        with pytest.raises(HTTPException, match="unsupported PAL statement"):
            call(
                routes,
                "POST",
                "/api/library/clock/import",
                PalImportBody(text="Run('x');", name="Bad", resync_minutes=[0]),
            )
        outside = f"Dir['{tmp_path / 'Elsewhere'}'].QueueBottom(smLRP, EnforceRules);"
        with pytest.raises(HTTPException, match="not inside a registered library folder"):
            call(
                routes,
                "POST",
                "/api/library/clock/import",
                PalImportBody(text=outside, name="Bad", resync_minutes=[0]),
            )
        bridge.player.configure_clock.assert_not_called()


def test_pal_import_explains_a_source_without_subfolders(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        add_sources(catalog, tmp_path, recursive=False)
        _bridge, routes = app_for(catalog, tmp_path)
        with pytest.raises(HTTPException, match="Turn on Add subfolders") as refused:
            call(
                routes,
                "POST",
                "/api/library/clock/import",
                PalImportBody(text=pal(tmp_path), name="Morning", resync_minutes=[0]),
            )
        assert refused.value.status_code == 400


def test_select_keeps_the_place_of_the_active_clockwheel(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        steps = [{"type": "music", "source": music["id"]}]
        call(routes, "POST", "/api/library/clock", body(steps=steps))
        assert call(routes, "GET", "/api/library/clock")["active"] is None
        active = call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        assert active["active"] == "Morning"
        catalog.set_setting("clock", {"name": "Morning", "cursor": 3, "resync_minutes": [0]})
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        assert catalog.get_setting("clock") == {"name": "Morning", "cursor": 3}
        cleared = call(routes, "POST", "/api/library/clock/active", ClockNameBody(name=None))
        assert cleared["active"] is None
        assert catalog.get_setting("clock") == {"name": None, "cursor": 0}
        assert bridge.player.configure_clock_after_selection.call_count == 3
        with pytest.raises(HTTPException, match="no clockwheel named 'Night'"):
            call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Night"))


def test_active_clock_update_accepts_deferred_timing(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        bridge.player.apply_clockwheel_update = Mock()
        steps = [{"type": "music", "source": music["id"]}]
        call(routes, "POST", "/api/library/clock", body(steps=steps))
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        result = call(
            routes,
            "PUT",
            "/api/library/clock/{name}",
            "Morning",
            body(steps=steps),
            "next_restart",
        )
        assert result["name"] == "Morning"
        bridge.player.apply_clockwheel_update.assert_called_once_with("Morning", "next_restart")


def test_active_clock_update_accepts_continue_timing(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        bridge.player.apply_clockwheel_update = Mock()
        steps = [{"type": "music", "source": music["id"]}]
        call(routes, "POST", "/api/library/clock", body(steps=steps))
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        call(
            routes,
            "PUT",
            "/api/library/clock/{name}",
            "Morning",
            body(steps=steps),
            "continue",
        )
        bridge.player.apply_clockwheel_update.assert_called_once_with("Morning", "continue")


def test_select_is_not_undone_by_a_buffered_cursor_update(tmp_path):
    # #133: the player buffers the old clockwheel's place; flushing it after
    # a selection must not bring the old clockwheel back.
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        steps = [{"type": "music", "source": music["id"]}]
        for name in ("Morning", "Night"):
            call(routes, "POST", "/api/library/clock", body(name, steps=steps))
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        ledger = bridge.library.ledger
        ledger.remember_setting("clock", {"name": "Morning", "cursor": 2, "resync_minutes": [0]})
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Night"))
        ledger.flush()
        assert catalog.get_setting("clock") == {"name": "Night", "cursor": 0}
        # Reselecting keeps a place the player has not written yet.
        ledger.remember_setting("clock", {"name": "Night", "cursor": 4, "resync_minutes": [0]})
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Night"))
        ledger.flush()
        assert catalog.get_setting("clock") == {"name": "Night", "cursor": 4}


def test_select_is_not_undone_by_the_player_saving_before_it_reloads(tmp_path):
    # #133: the player still holds the old clockwheel between the choice
    # being written and configure_clock loading the new one.
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        steps = [{"type": "music", "source": music["id"]}]
        for name in ("Morning", "Night"):
            call(routes, "POST", "/api/library/clock", body(name, steps=steps))
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        ledger = bridge.library.ledger
        old = {"name": "Morning", "cursor": 3, "resync_minutes": [0]}
        bridge.player.configure_clock_after_selection.side_effect = lambda: ledger.remember_setting(
            "clock", old
        )
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Night"))
        ledger.flush()
        assert catalog.get_setting("clock") == {"name": "Night", "cursor": 0}


def test_renaming_the_active_clockwheel_keeps_a_place_not_yet_written(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        steps = [{"type": "music", "source": music["id"]}]
        call(routes, "POST", "/api/library/clock", body(steps=steps))
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        ledger = bridge.library.ledger
        ledger.remember_setting("clock", {"name": "Morning", "cursor": 4, "resync_minutes": [0]})
        call(
            routes,
            "POST",
            "/api/library/clock/{name}/rename",
            "Morning",
            NewClockNameBody(name="Day"),
        )
        ledger.flush()
        assert catalog.get_setting("clock") == {"name": "Day", "cursor": 4}


def test_create_edit_duplicate_rename_delete(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        step = {"type": "music", "source": music["id"], "folder": "top"}
        created = call(routes, "POST", "/api/library/clock", body(steps=[step]))
        assert created["steps"][0] == {
            **step,
            "recursive": False,
            "selection": "least_recent",
            "enforce_rules": True,
        }
        call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        copied = call(
            routes,
            "POST",
            "/api/library/clock/{name}/duplicate",
            "Morning",
            NewClockNameBody(name="Copy"),
        )
        assert copied["name"] == "Copy"
        renamed = call(
            routes,
            "POST",
            "/api/library/clock/{name}/rename",
            "Morning",
            NewClockNameBody(name="Day"),
        )
        assert renamed["name"] == "Day"
        assert catalog.get_setting("clock")["name"] == "Day"
        call(
            routes, "POST", "/api/library/clock/{name}/rename", "Day", NewClockNameBody(name="DAY")
        )
        assert call(routes, "GET", "/api/library/clock")["names"] == ["Copy", "DAY"]
        liner = {"type": "liner", "source": liners["id"], "folder": "drops"}
        bridge.player.configure_clock.reset_mock()
        updated = call(
            routes, "PUT", "/api/library/clock/{name}", "DAY", body("DAY", [liner, step], [15])
        )
        assert [s["type"] for s in updated["steps"]] == ["liner", "music"]
        bridge.player.apply_clockwheel_update.assert_called_once_with("DAY", "immediate")
        with pytest.raises(HTTPException, match="Use Rename"):
            call(routes, "PUT", "/api/library/clock/{name}", "DAY", body("Other", [step]))
        listed = call(routes, "DELETE", "/api/library/clock/{name}", "DAY")
        assert (listed["names"], listed["active"]) == (["Copy"], None)
        with pytest.raises(HTTPException) as missing:
            call(routes, "DELETE", "/api/library/clock/{name}", "DAY")
        assert missing.value.status_code == 404


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"type": "music", "source": "nope"}], "its music folder is not registered"),
        ([{"type": "cart", "name": "Top"}], "Unknown clockwheel step type 'cart'"),
        ([{"type": "track", "source": "MUSIC", "path": "../escape.flac"}], "no '..'"),
        ([{"type": "track", "source": "MUSIC", "path": "C:/escape.flac"}], "no '..'"),
        ([{"type": "track", "source": "MUSIC", "path": "missing.flac"}], "is not a track"),
        ([{"type": "music", "source": "LINER"}], "its music folder is not registered"),
        (
            [{"type": "music", "source": "MUSIC"}, {"type": "settings", "preset": "nope"}],
            "no preset named nope",
        ),
    ],
)
def test_steps_are_checked_against_the_library(tmp_path, steps, message):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, liners = add_sources(catalog, tmp_path)
        _bridge, routes = app_for(catalog, tmp_path)
        ids = {"MUSIC": music["id"], "LINER": liners["id"]}
        steps = [
            {**s, "source": ids.get(s["source"], s["source"])} if "source" in s else s
            for s in steps
        ]
        with pytest.raises(HTTPException, match=message) as refused:
            call(routes, "POST", "/api/library/clock", body(steps=steps))
        assert refused.value.status_code == 400


def test_track_and_settings_steps_resolve_inside_the_library(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path)
        bridge, routes = app_for(catalog, tmp_path)
        track = str(tmp_path / "Music" / "top" / "song.flac")
        same = os.path.normcase
        bridge.library.entry_for_path = lambda path: object() if same(path) == same(track) else None
        steps = [
            {"type": "settings", "preset": "winddown"},
            {"type": "track", "source": music["id"], "path": "top/song.flac"},
        ]
        saved = call(routes, "POST", "/api/library/clock", body(steps=steps))
        assert saved["steps"] == steps
        snapshot = call(routes, "GET", "/api/library/clock")
        assert "winddown" in snapshot["presets"]
        assert {"music", "liner", "track", "settings"} <= set(snapshot["types"])
        assert {s["id"] for s in snapshot["sources"]} >= {music["id"]}


def test_a_folder_inside_a_source_without_subfolders_is_refused(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, _liners = add_sources(catalog, tmp_path, recursive=False)
        _bridge, routes = app_for(catalog, tmp_path)
        steps = [{"type": "music", "source": music["id"], "folder": "top"}]
        with pytest.raises(HTTPException, match="registered without subfolders"):
            call(routes, "POST", "/api/library/clock", body(steps=steps))


def test_old_single_clock_data_is_reported_with_the_way_back(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        catalog.set_setting("clock", {"name": "Clock", "slots": [{"folder": "x"}], "cursor": 2})
        _bridge, routes = app_for(catalog, tmp_path)
        snapshot = call(routes, "GET", "/api/library/clock")
        assert snapshot["active"] is None
        assert snapshot["notice"] == OLD_CLOCK_MESSAGE
        assert "Import .PAL" in OLD_CLOCK_MESSAGE
        assert "Activate" in OLD_CLOCK_MESSAGE
