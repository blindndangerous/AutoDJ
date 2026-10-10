"""Folder and point operations through the web route boundary."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from autodj.library import LibraryManager
from autodj.server import (
    LibraryFolderBody,
    LibraryFolderUpdateBody,
    SongPointsBody,
    create_app,
)
from tests.unit.test_library import Similarity, make_config, make_entry


@pytest.fixture
def library_api(tmp_path):
    cfg = make_config(tmp_path)
    manager = LibraryManager(cfg, Similarity())
    manager.start_scan = Mock()
    bridge = SimpleNamespace(player=SimpleNamespace(_cfg=cfg), library=manager)
    app = create_app(bridge)
    routes = {
        (method, route.path): route.endpoint
        for route in app.routes
        if "/api/library/" in route.path
        for method in (route.methods or set())
    }

    def call(method, path, *args):
        return asyncio.run(routes[(method, path)](*args))

    yield manager, call
    manager.close()


def test_folder_choices_persist_and_removing_a_source_keeps_files(library_api, tmp_path):
    manager, call = library_api
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
    assert manager.start_scan.call_count == 3


def test_points_validate_and_keep_manual_values_without_analysis(library_api, tmp_path):
    manager, call = library_api
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
        SongPointsBody(path=str(liner), points={"overlap_start": 5.3}),
    )
    assert saved["points"]["overlap_start"] == 5.3
    with pytest.raises(HTTPException) as invalid:
        call("POST", "/api/library/points", SongPointsBody(path=str(liner), points={"intro": 1}))
    assert invalid.value.status_code == 400
