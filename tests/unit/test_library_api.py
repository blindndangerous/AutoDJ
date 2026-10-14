"""Folder and point operations through the web route boundary."""

import asyncio
import dataclasses
import os
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import call as mock_call
from urllib.parse import urlsplit

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from autodj.indexer import FEATURE_DIM
from autodj.library import LibraryManager
from autodj.processing import PREPARATION_VERSION, TARGET_LUFS
from autodj.server import (
    LibraryAutoRescanBody,
    LibraryFolderBody,
    LibraryFolderUpdateBody,
    LibraryRescanBody,
    SongPointsBody,
    TimeSettingsBody,
    _apply_time_settings,
    create_app,
)
from tests.unit.test_library import Similarity, make_config, make_entry


@pytest.fixture
def library_api(tmp_path):
    cfg = make_config(tmp_path)
    manager = LibraryManager(cfg, Similarity())
    manager.catalog.add_source(cfg.library.music_dir)
    manager.start_scan = Mock()
    bridge = SimpleNamespace(player=SimpleNamespace(_cfg=cfg), library=manager)
    app = create_app(bridge)
    routes = {
        (method, route.path): route.endpoint
        for route in app.routes
        if "/api/library/" in route.path
        for method in (route.methods or set())
    }

    def call(method, path, *args, **kwargs):
        return asyncio.run(routes[(method, path)](*args, **kwargs))

    yield manager, call, app
    manager.close()


def test_folder_choices_persist_and_removing_a_source_keeps_files(library_api, tmp_path):
    manager, call, _app = library_api
    folder = tmp_path / "extra"
    folder.mkdir()
    call("POST", "/api/library/folders", LibraryFolderBody(path=str(folder)))
    source = next(
        s for s in manager.catalog.sources() if s["path"] == os.path.normcase(str(folder))
    )
    assert source["recursive"] and source["advanced"]
    call(
        "PATCH",
        "/api/library/folders/{source_id}",
        source["id"],
        LibraryFolderUpdateBody(recursive=False, advanced=False),
    )
    changed = next(s for s in manager.catalog.sources() if s["id"] == source["id"])
    assert not changed["recursive"] and not changed["advanced"]
    recording = folder / "song.flac"
    recording.touch()
    entry = make_entry(recording)
    manager.catalog.put_entry(entry, points={"start": 0, "end": 180})
    manager.refresh()
    call("DELETE", "/api/library/folders/{source_id}", source["id"])
    assert recording.exists()
    assert manager.catalog.record_for_path(recording) is not None
    assert manager.entry_for_path(recording) is None
    with pytest.raises(HTTPException) as missing:
        call("GET", "/api/library/points", str(recording))
    assert missing.value.status_code == 404
    call("POST", "/api/library/rescan")
    assert manager.start_scan.call_args_list == [
        mock_call({source["id"]}),
        mock_call({source["id"]}),
        mock_call(override_tags=False),
    ]


def test_rescan_passes_explicit_tag_override_option(library_api):
    manager, call, _app = library_api

    call("POST", "/api/library/rescan")
    call("POST", "/api/library/rescan", LibraryRescanBody(override_tags=True))

    assert manager.start_scan.call_args_list == [
        mock_call(override_tags=False),
        mock_call(override_tags=True),
    ]


def test_auto_rescan_routes_store_shared_interval(library_api):
    manager, call, _app = library_api
    assert call("GET", "/api/library/auto-rescan") == {"hours": 0, "minutes": 0}
    assert call("PUT", "/api/library/auto-rescan", LibraryAutoRescanBody(hours=1, minutes=30)) == {
        "hours": 1,
        "minutes": 30,
    }
    assert manager.catalog.get_setting("auto_rescan_minutes") == 90
    assert call("GET", "/api/library/auto-rescan") == {"hours": 1, "minutes": 30}
    assert call("PUT", "/api/library/auto-rescan", LibraryAutoRescanBody(hours=0, minutes=0)) == {
        "hours": 0,
        "minutes": 0,
    }
    assert manager.auto_rescan_minutes() == 0


def test_time_settings_are_saved_and_the_zone_applies_at_the_next_start(library_api, monkeypatch):
    from autodj import timefmt

    monkeypatch.setattr(timefmt, "time_format", "24")
    manager, _call, app = library_api
    routes = {
        (method, route.path): route.endpoint
        for route in app.routes
        if route.path == "/api/time-settings"
        for method in (route.methods or set())
    }
    running = asyncio.run(routes[("GET", "/api/time-settings")]())["running_timezone"]
    saved = asyncio.run(
        routes[("PUT", "/api/time-settings")](
            TimeSettingsBody(timezone="Pacific/Auckland", time_format="12")
        )
    )
    assert (saved["timezone"], saved["running_timezone"]) == ("Pacific/Auckland", running)
    assert timefmt.time_format == "12"
    with pytest.raises(HTTPException) as unknown:
        asyncio.run(
            routes[("PUT", "/api/time-settings")](
                TimeSettingsBody(timezone="Mars/Olympus", time_format="24")
            )
        )
    assert unknown.value.status_code == 400
    timefmt.time_format = "24"
    _apply_time_settings(manager.cfg, manager)
    assert manager.cfg.server.station_timezone == "Pacific/Auckland"
    assert timefmt.time_format == "12"


def test_auto_rescan_rejects_interval_below_five_minutes(library_api):
    _manager, call, _app = library_api
    with pytest.raises(HTTPException) as invalid:
        call("PUT", "/api/library/auto-rescan", LibraryAutoRescanBody(hours=0, minutes=4))
    assert invalid.value.status_code == 400
    assert invalid.value.detail == "Use at least 5 minutes, or 0 to turn it off."


@pytest.mark.parametrize(
    "values",
    [
        {"hours": 169, "minutes": 0},
        {"hours": 0, "minutes": 60},
        {"hours": -1, "minutes": 0},
        {"hours": 0, "minutes": -1},
        {"hours": 0, "minutes": 5, "extra": True},
        {"hours": "5", "minutes": 0},
    ],
)
def test_auto_rescan_body_rejects_invalid_values(values):
    with pytest.raises(ValidationError):
        LibraryAutoRescanBody.model_validate(values)


def test_auto_rescan_http_validation(library_api):
    _manager, _call, app = library_api
    origin = app.state.security_policy.effective_allowed_origins()[0]
    response = TestClient(app).put(
        "/api/library/auto-rescan",
        json={"hours": "1", "minutes": 0},
        headers={"host": urlsplit(origin).netloc, "origin": origin},
    )
    assert response.status_code == 422


def test_points_validate_and_keep_manual_values_without_analysis(library_api, tmp_path):
    manager, call, _app = library_api
    music = manager.cfg.library.music_dir / "song.flac"
    music.touch()
    manager.catalog.put_entry(make_entry(music), points={"start": 0, "end": 180})
    manager.refresh()
    changed = call(
        "POST",
        "/api/library/points",
        SongPointsBody(path=str(music), points={"intro": 9, "outro": 170, "segue": 169.5}),
    )
    assert changed["point_sources"]["intro"] == "user"
    assert changed["points"]["segue"] == 169.5
    with pytest.raises(HTTPException) as invalid:
        call("POST", "/api/library/points", SongPointsBody(path=str(music), points={"end": 999}))
    assert invalid.value.status_code == 400

    assert manager.catalog.points(music)["end"] == 180
    manager.start_scan.assert_not_called()

    liners = tmp_path / "ids"
    liners.mkdir()
    call("POST", "/api/library/folders", LibraryFolderBody(path=str(liners), kind="liner"))
    assert not next(
        s for s in manager.catalog.sources("liner") if s["path"] == os.path.normcase(str(liners))
    )["advanced"]
    liner = liners / "jingle.flac"
    liner.touch()
    manager.catalog.put_entry(make_entry(liner), "liner", {"start": 0, "end": 8.8})
    manager.refresh()
    saved = call(
        "POST",
        "/api/library/points",
        SongPointsBody(path=str(liner), points={"overlap_start": 5.3, "segue": 5.5}),
    )
    assert saved["points"]["overlap_start"] == 5.3
    assert saved["points"]["segue"] == 5.5
    saved = call(
        "POST",
        "/api/library/points",
        SongPointsBody(path=str(liner), points={"overlap_start": -3}),
    )
    assert saved["points"]["overlap_start"] == -3
    assert manager.catalog.points(liner)["overlap_start"] == -3
    with pytest.raises(HTTPException) as invalid:
        call(
            "POST",
            "/api/library/points",
            SongPointsBody(path=str(liner), points={"overlap_start": -16}),
        )
    assert invalid.value.status_code == 400
    with pytest.raises(HTTPException) as invalid:
        call("POST", "/api/library/points", SongPointsBody(path=str(liner), points={"intro": 1}))
    assert invalid.value.status_code == 400


def test_unchanged_track_info_cues_save_after_display_rounding(library_api):
    """#237: Track info shows cues to 0.01 s, so a saved End can read past the length."""
    manager, call, _app = library_api
    music = manager.cfg.library.music_dir / "song.flac"
    music.touch()
    length = 213.4567
    stored = {"start": 1.2351, "intro": 12.3456, "outro": 200.0049, "segue": 205.555}
    manager.catalog.put_entry(
        dataclasses.replace(make_entry(music), length=length), points={**stored, "end": length}
    )
    manager.refresh()
    loaded = call("GET", "/api/library/points", str(music))["points"]
    # What the editor sends back after formatPointTime/parsePointTime.
    shown = {name: round(value, 2) for name, value in loaded.items() if value is not None}
    assert shown["end"] > length

    saved = call("POST", "/api/library/points", SongPointsBody(path=str(music), points=shown))

    assert saved["points"]["end"] == length
    for name, value in stored.items():
        assert abs(saved["points"][name] - value) <= 0.005
    with pytest.raises(HTTPException) as invalid:
        call(
            "POST",
            "/api/library/points",
            SongPointsBody(path=str(music), points={"end": round(length + 0.02, 2)}),
        )
    assert invalid.value.status_code == 400
    assert manager.catalog.points(music)["end"] == length


def test_track_points_reports_playback_and_analysis_state(library_api):
    manager, call, _app = library_api
    music = manager.cfg.library.music_dir / "song.flac"
    music.touch()
    manager.catalog.put_entry(make_entry(music))
    stat = music.stat()
    lufs = TARGET_LUFS["music"] - 2
    manager.catalog.set_loudness(
        music,
        {
            "version": PREPARATION_VERSION,
            "kind": "music",
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "lufs": lufs,
            "gain_db": 2,
        },
    )
    manager.refresh()

    record = call("GET", "/api/library/points", str(music))
    assert record["playable"]
    assert record["loudness_ready"]
    assert record["analysis"] == "pending"
    assert record["key_label"] == "--"

    manager.catalog.set_vector(music, np.zeros(FEATURE_DIM, dtype=np.float32))
    assert call("GET", "/api/library/points", str(music))["analysis"] == "ready"

    source = manager.catalog.sources("music")[0]
    manager.catalog.update_source(source["id"], recursive=True, advanced=False)
    assert call("GET", "/api/library/points", str(music))["analysis"] == "ready"

    other = manager.cfg.library.music_dir / "unindexed.flac"
    other.touch()
    manager.catalog.put_entry(make_entry(other))
    manager.refresh()
    assert call("GET", "/api/library/points", str(other))["analysis"] == "disabled"


def test_track_browse_empty_query_paginates_stably(library_api):
    manager, call, _app = library_api
    for name in ("Zebra", "Alpha", "Mango"):
        path = manager.cfg.library.music_dir / f"{name}.flac"
        path.touch()
        manager.catalog.put_entry(make_entry(path))
    manager.refresh()  # as the server's library tick does

    page = call("GET", "/api/library/tracks", offset=1, limit=1)
    assert page["total"] == 3
    assert page["offset"] == 1 and page["limit"] == 1
    assert [track["title"] for track in page["results"]] == ["Mango"]
    assert page["results"][0]["display_name"] == "Artist — Mango"


def test_track_browse_source_respects_recursive_and_kind(library_api, tmp_path):
    manager, call, _app = library_api
    folder = tmp_path / "browse"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    call("POST", "/api/library/folders", LibraryFolderBody(path=str(folder), recursive=False))
    source = next(
        s for s in manager.catalog.sources("music") if s["path"] == os.path.normcase(str(folder))
    )
    direct = folder / "direct.flac"
    child = nested / "child.flac"
    direct.touch()
    child.touch()
    manager.catalog.put_entry(make_entry(direct))
    manager.catalog.put_entry(make_entry(child))
    manager.refresh()

    listed = call("GET", "/api/library/tracks", source_id=source["id"])
    assert [track["path"] for track in listed["results"]] == [os.path.normcase(str(direct))]


def test_track_browse_finds_filename_and_lists_liners(library_api, tmp_path):
    manager, call, _app = library_api
    root = manager.cfg.library.music_dir / "liners"
    root.mkdir()
    call("POST", "/api/library/folders", LibraryFolderBody(path=str(root), kind="liner"))
    source = next(
        s for s in manager.catalog.sources("liner") if s["path"] == os.path.normcase(str(root))
    )
    liner = root / "PromoCode.flac"
    liner.touch()
    manager.catalog.put_entry(make_entry(liner, title="Promo"), kind="liner")
    manager.refresh()

    assert call("GET", "/api/library/tracks", q="code", kind="music")["total"] == 0
    results = call("GET", "/api/library/tracks", q="code", kind="liner", source_id=source["id"])
    assert [track["path"] for track in results["results"]] == [os.path.normcase(str(liner))]


def test_track_browse_rejects_unknown_source_and_invalid_kind(library_api):
    manager, call, app = library_api
    with pytest.raises(HTTPException) as missing:
        call("GET", "/api/library/tracks", source_id="missing")
    assert missing.value.status_code == 404
    music_source = manager.catalog.sources("music")[0]
    with pytest.raises(HTTPException) as mismatch:
        call("GET", "/api/library/tracks", kind="liner", source_id=music_source["id"])
    assert mismatch.value.status_code == 400

    origin = app.state.security_policy.effective_allowed_origins()[0]
    response = TestClient(app).get(
        "/api/library/tracks",
        params={"kind": "other"},
        headers={"host": urlsplit(origin).netloc, "origin": origin},
    )
    assert response.status_code == 422
