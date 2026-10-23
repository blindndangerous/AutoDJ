"""Browse-button listings: server folders and the inside of a library folder."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from autodj import path_browse
from autodj.path_browse import BrowseError, check_relative, list_library_folder, list_server_folder
from tests.unit.test_library_api import library_api  # noqa: F401  (fixture)


def _tree(root: Path) -> Path:
    (root / "Rock" / "Live").mkdir(parents=True)
    (root / "jazz").mkdir()
    (root / "Rock" / "song.FLAC").write_bytes(b"")
    (root / "Rock" / "cover.jpg").write_bytes(b"")
    (root / "notes.txt").write_bytes(b"")
    return root


def test_server_folder_lists_folders_only_sorted_without_case(tmp_path):
    _tree(tmp_path)
    listing = list_server_folder(str(tmp_path))
    assert listing["path"] == str(tmp_path)
    assert [item["name"] for item in listing["folders"]] == ["jazz", "Rock"]
    assert listing["folders"][1]["path"] == str(tmp_path / "Rock")
    assert listing["files"] == []
    assert listing["parent"] == str(tmp_path.parent)
    assert listing["truncated"] is False


@pytest.mark.parametrize("path", ["relative/folder", "..", "/x/../y", "C:..\\x", "a\0b"])
def test_server_folder_refuses_relative_paths_and_dot_dot(tmp_path, path):
    if path == "/x/../y":
        path = str(tmp_path / "x" / ".." / "y")
    with pytest.raises(BrowseError) as refused:
        list_server_folder(path)
    assert refused.value.status == 400


def test_server_folder_reports_a_missing_folder_as_not_found(tmp_path):
    with pytest.raises(BrowseError) as missing:
        list_server_folder(str(tmp_path / "gone"))
    assert missing.value.status == 404


def test_server_roots_list_drives_or_open_the_single_root(monkeypatch, tmp_path):
    monkeypatch.setattr(path_browse, "filesystem_roots", lambda: ["C:\\", "D:\\"])
    listing = list_server_folder("")
    assert listing["path"] == ""
    assert [item["path"] for item in listing["folders"]] == ["C:\\", "D:\\"]
    monkeypatch.setattr(path_browse, "filesystem_roots", lambda: [str(tmp_path)])
    _tree(tmp_path)
    assert list_server_folder("")["path"] == str(tmp_path)


def test_listings_are_capped(tmp_path, monkeypatch):
    for name in "abc":
        (tmp_path / name).mkdir()
    monkeypatch.setattr(path_browse, "MAX_ENTRIES", 2)
    listing = list_server_folder(str(tmp_path))
    assert len(listing["folders"]) == 2
    assert listing["truncated"] is True


def test_library_folder_lists_relative_folders_and_audio_files(tmp_path):
    _tree(tmp_path)
    top = list_library_folder(tmp_path, "", files=True, extensions={".flac"})
    assert top["path"] == "" and top["parent"] is None
    assert [item["path"] for item in top["folders"]] == ["jazz", "Rock"]
    assert top["files"] == []
    rock = list_library_folder(tmp_path, "Rock", files=True, extensions={".flac"})
    assert rock["parent"] == ""
    assert rock["folders"] == [{"name": "Live", "path": "Rock/Live"}]
    assert rock["files"] == [{"name": "song.FLAC", "path": "Rock/song.FLAC"}]
    live = list_library_folder(tmp_path, "Rock/Live/")
    assert live["path"] == "Rock/Live" and live["parent"] == "Rock"
    assert list_library_folder(tmp_path, "Rock")["files"] == []


@pytest.mark.parametrize(
    "path", ["../x", "Rock/../..", "/etc", "C:/x", "Rock\\Live", "a//b", "./a"]
)
def test_library_paths_follow_the_clockwheel_rules(path):
    with pytest.raises(BrowseError) as refused:
        check_relative(path)
    assert refused.value.status == 400


def test_library_listing_never_follows_a_link_out_of_the_folder(tmp_path):
    root = _tree(tmp_path / "library")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.flac").write_bytes(b"")
    try:
        os.symlink(outside, root / "escape", target_is_directory=True)
        os.symlink(root / "Rock", root / "inside", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    listing = list_library_folder(root, "", files=True, extensions={".flac"})
    assert [item["name"] for item in listing["folders"]] == ["inside", "jazz", "Rock"]
    with pytest.raises(BrowseError) as refused:
        list_library_folder(root, "escape", files=True, extensions={".flac"})
    assert refused.value.status == 403


def test_browse_routes_list_server_and_registered_folders(library_api, tmp_path):  # noqa: F811
    manager, call, app = library_api
    music = manager.cfg.library.music_dir
    _tree(music)
    source = manager.catalog.sources("music")[0]
    listing = call("GET", "/api/library/folders/{source_id}/browse", source["id"], "Rock", True)
    assert [item["path"] for item in listing["files"]] == ["Rock/song.FLAC"]
    with pytest.raises(HTTPException) as unknown:
        call("GET", "/api/library/folders/{source_id}/browse", "missing", "", False)
    assert unknown.value.status_code == 404
    with pytest.raises(HTTPException) as escape:
        call("GET", "/api/library/folders/{source_id}/browse", source["id"], "../..", False)
    assert escape.value.status_code == 400
    with pytest.raises(HTTPException) as relative:
        call("GET", "/api/library/browse", "relative")
    assert relative.value.status_code == 400

    origin = app.state.security_policy.effective_allowed_origins()[0]
    response = TestClient(app).get(
        "/api/library/browse",
        params={"path": str(music)},
        headers={"host": urlsplit(origin).netloc, "origin": origin},
    )
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["folders"]] == ["jazz", "Rock"]
