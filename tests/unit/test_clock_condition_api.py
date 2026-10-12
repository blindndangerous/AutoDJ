"""Conditional clocks through saved files, player loading and the edit API."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from autodj.catalog import LibraryCatalog
from autodj.clockwheel_store import ClockwheelStore, validate_clockwheel
from autodj.player import Player
from autodj.server import ClockNameBody, NewClockNameBody
from tests.unit.test_clock_config_api import add_sources, app_for, body, call
from tests.unit.test_clockwheel_store import _player, clock

YEAR = {"field": "year", "operator": "between", "value": 1990, "upper": 1999}


def test_conditions_save_and_load_into_player_without_a_folder(tmp_path):
    saved = ClockwheelStore(tmp_path / "clockwheels").save(
        clock([{"type": "music", "conditions": [YEAR], "match": "any"}])
    )
    assert saved["steps"][0]["match"] == "any"
    player = _player(tmp_path, {"name": "Morning"}, [])
    Player.configure_clock(player)
    slot = player._clock_slots[0]
    assert slot.conditions == (YEAR,)
    assert slot.match == "any" and not slot.folder
    assert slot.selection == "least_recent" and slot.enforce_rules


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"conditions": []}, "between 1 and 32"),
        ({"conditions": [YEAR] * 33}, "between 1 and 32"),
        ({"conditions": "year"}, "between 1 and 32"),
        ({"conditions": [None]}, "needs a field"),
        ({"match": "xor"}, "Match all or Match any"),
        ({"source": "one"}, "unknown fields"),
        ({"conditions": [{"field": "folder", "source": "one", "recursive": 1}]}, "true or false"),
        ({"conditions": [{"field": "folder", "source": "one", "folder": "../outside"}]}, "no '..'"),
        ({"conditions": [{"field": "folder", "source": "one", "extra": 1}]}, "unknown fields"),
        ({"conditions": [{"field": "group", "conditions": [YEAR]}]}, "unsupported"),
    ],
)
def test_invalid_condition_steps_are_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        validate_clockwheel(clock([{"type": "music", "conditions": [YEAR], **changes}]))


def test_api_conditions_roundtrip_and_registered_folder_validation(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        music, liners = add_sources(catalog, tmp_path)
        _bridge, routes = app_for(catalog, tmp_path)
        step = {
            "type": "music",
            "match": "any",
            "conditions": [
                {"field": "folder", "source": music["id"], "folder": "top", "recursive": True},
                YEAR,
            ],
        }
        saved = call(routes, "POST", "/api/library/clock", body(steps=[step]))
        assert saved["steps"][0]["conditions"] == step["conditions"]
        assert call(routes, "GET", "/api/library/clock/{name}", "Morning") == saved
        assert (
            call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))[
                "active"
            ]
            == "Morning"
        )
        bad = {**step, "conditions": [{"field": "folder", "source": liners["id"]}]}
        with pytest.raises(HTTPException, match="music folder is not registered"):
            call(routes, "POST", "/api/library/clock", body(name="Bad", steps=[bad]))
        assert not (tmp_path / "clockwheels" / "Bad.json").exists()
        # A bad update leaves the saved clock untouched.
        bad = {
            **step,
            "conditions": [{"field": "year", "operator": "between", "value": 2000, "upper": 1990}],
        }
        with pytest.raises(HTTPException):
            call(routes, "PUT", "/api/library/clock/{name}", "Morning", body(steps=[bad]))
        assert call(routes, "GET", "/api/library/clock/{name}", "Morning") == saved


def test_genre_choices_use_both_music_and_liners(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        bridge, routes = app_for(catalog, tmp_path)
        bridge.library.entries_snapshot = lambda kind="music": (
            SimpleNamespace(genre="Rock; Pop" if kind == "music" else "Station IDs"),
        )
        snapshot = call(routes, "GET", "/api/library/clock")
        assert snapshot["genres"] == ["Pop", "Rock", "Station IDs"]
        assert snapshot["beets_fields"] == []
        assert "Configure a Beets database" in snapshot["beets_error"]


def test_beets_conditions_require_available_fields_on_save_and_activation(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        bridge, routes = app_for(catalog, tmp_path)
        step = {
            "type": "music",
            "conditions": [
                {"field": "beets", "name": "mood", "operator": "contains", "value": "happy"}
            ],
        }
        with pytest.raises(HTTPException, match="Configure a Beets database"):
            call(routes, "POST", "/api/library/clock", body(steps=[step]))
        db = tmp_path / "beets.db"
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("CREATE TABLE items (id INTEGER, path BLOB, genre TEXT)")
            connection.execute(
                "CREATE TABLE item_attributes (entity_id INTEGER, key TEXT, value TEXT)"
            )
            connection.execute("INSERT INTO item_attributes VALUES (1, 'mood', 'happy')")
            connection.commit()
        bridge.player._cfg.library = SimpleNamespace(beets_db=db, music_dir=tmp_path)
        saved = call(routes, "POST", "/api/library/clock", body(steps=[step]))
        assert saved["steps"][0]["conditions"] == step["conditions"]
        listed = call(routes, "GET", "/api/library/clock")
        assert listed["beets_fields"] == ["genre", "mood"]
        assert listed["beets_error"] is None
        bridge.player._cfg.library.beets_db = None
        with pytest.raises(HTTPException, match="Configure a Beets database"):
            call(routes, "POST", "/api/library/clock/active", ClockNameBody(name="Morning"))
        assert catalog.get_setting("clock", {}) == {}
        assert (
            json.loads((tmp_path / "clockwheels" / "Morning.json").read_text())["steps"]
            == saved["steps"]
        )


def test_renaming_active_clock_keeps_it_selected_when_beets_is_unavailable(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        _bridge, routes = app_for(catalog, tmp_path)
        step = {
            "type": "music",
            "conditions": [{"field": "beets", "name": "mood", "operator": "is", "value": "happy"}],
        }
        ClockwheelStore(tmp_path / "clockwheels").save(clock([step]))
        catalog.set_setting("clock", {"name": "Morning", "cursor": 0})
        result = call(
            routes,
            "POST",
            "/api/library/clock/{name}/rename",
            "Morning",
            NewClockNameBody(name="Daytime"),
        )
        assert result["name"] == "Daytime"
        assert catalog.get_setting("clock")["name"] == "Daytime"
        assert (tmp_path / "clockwheels" / "Daytime.json").exists()
        assert not (tmp_path / "clockwheels" / "Morning.json").exists()
