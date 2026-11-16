"""Durable named-library identity and definition behavior."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autodj.library_registry import (
    LibraryConflict,
    LibraryRegistry,
    LibraryRegistryError,
    validate_library_id,
)


@pytest.mark.parametrize("contents", [None, "  \n"])
def test_legacy_listing_is_read_only(tmp_path, contents):
    root = tmp_path / "libraries"
    main = root / "main"
    main.mkdir(parents=True)
    definition = main / "library.json"
    if contents is not None:
        definition.write_text(contents)
    before = {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    listed = LibraryRegistry(root).list(allow_legacy_main=True)
    assert [(item.id, item.name) for item in listed] == [("main", "Main")]
    assert {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == before


def test_ensure_main_adopts_existing_data_and_is_idempotent(tmp_path: Path) -> None:
    """ADR-0023-01: adoption never rebuilds the existing catalog."""
    root = tmp_path / "index" / "libraries"
    main = root / "main"
    main.mkdir(parents=True)
    catalog = main / "library.db"
    catalog.write_bytes(b"existing catalog")
    registry = LibraryRegistry(root)

    first = registry.ensure_main()
    second = registry.ensure_main()

    assert first == second
    assert first.id == "main"
    assert first.name == "Main"
    assert first.settings == {}
    assert first.path == main
    assert catalog.read_bytes() == b"existing catalog"
    assert json.loads((main / "library.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "id": "main",
        "name": "Main",
        "settings": {},
    }


def test_ensure_main_recovers_blank_interrupted_definition(tmp_path: Path) -> None:
    main = tmp_path / "libraries" / "main"
    main.mkdir(parents=True)
    (main / "library.db").write_bytes(b"catalog remains untouched")
    (main / "library.json").write_text(" \n", encoding="utf-8")

    definition = LibraryRegistry(main.parent).ensure_main()

    assert definition.name == "Main"
    assert (main / "library.db").read_bytes() == b"catalog remains untouched"
    assert json.loads((main / "library.json").read_text(encoding="utf-8"))["id"] == "main"


def test_create_and_rename_keep_settings_path_and_identity(tmp_path: Path) -> None:
    registry = LibraryRegistry(tmp_path / "libraries")
    registry.ensure_main()
    created = registry.create("jazz", "Jazz", {"music_folders": ["/music/jazz"]})
    database = created.path / "library.db"
    database.write_bytes(b"catalog")

    renamed = registry.rename("jazz", "Late Night Jazz")

    assert renamed.id == "jazz"
    assert renamed.path == created.path
    assert renamed.settings == created.settings
    assert renamed.name == "Late Night Jazz"
    assert database.read_bytes() == b"catalog"
    assert registry.path("jazz") == created.path
    assert [item.id for item in registry.list()] == ["jazz", "main"]


def test_create_refuses_existing_directory_without_changing_it(tmp_path: Path) -> None:
    root = tmp_path / "libraries"
    registry = LibraryRegistry(root)
    registry.ensure_main()
    existing = root / "new"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(LibraryConflict):
        registry.create("new", "New")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (existing / "library.json").exists()


def test_ids_and_display_names_are_unique(tmp_path: Path) -> None:
    registry = LibraryRegistry(tmp_path / "libraries")
    registry.ensure_main()
    registry.create("jazz", "Jazz")

    with pytest.raises(LibraryConflict):
        registry.create("jazz", "Different")
    with pytest.raises(LibraryConflict):
        registry.create("ambient", "jAZz")
    with pytest.raises(LibraryConflict):
        registry.rename("jazz", "Main")


@pytest.mark.parametrize("library_id", ["", "Main", "has space", "x" * 33, "_x", "x_"])
def test_create_rejects_invalid_ids(tmp_path: Path, library_id: str) -> None:
    registry = LibraryRegistry(tmp_path / "libraries")
    with pytest.raises(LibraryRegistryError):
        registry.create(library_id, "Name")


@pytest.mark.parametrize("name", ["", "  ", "x" * 81])
def test_create_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    registry = LibraryRegistry(tmp_path / "libraries")
    with pytest.raises(LibraryRegistryError):
        registry.create("valid", name)


def test_create_rejects_non_json_settings(tmp_path: Path) -> None:
    registry = LibraryRegistry(tmp_path / "libraries")
    for settings in ({"bad": float("nan")}, {"bad": float("inf")}, {"bad": (1, 2)}):
        with pytest.raises(LibraryRegistryError):
            registry.create("valid", "Valid", settings)


def test_public_id_validator_enforces_registry_identity_format() -> None:
    assert validate_library_id("main") == "main"
    assert validate_library_id("library-2") == "library-2"
    with pytest.raises(LibraryRegistryError):
        validate_library_id("Not-Safe")


def test_list_refuses_malformed_public_definition(tmp_path: Path) -> None:
    root = tmp_path / "libraries"
    registry = LibraryRegistry(root)
    registry.ensure_main()
    broken = root / "broken"
    broken.mkdir()

    with pytest.raises(LibraryRegistryError, match="cannot read library definition"):
        registry.list()


def test_list_ignores_only_private_staging_directories(tmp_path: Path) -> None:
    root = tmp_path / "libraries"
    registry = LibraryRegistry(root)
    registry.ensure_main()
    (root / ".library-create-interrupted").mkdir()

    assert [item.id for item in registry.list()] == ["main"]


def test_list_refuses_linked_root_library_directory_and_definition(tmp_path: Path) -> None:
    root = tmp_path / "libraries"
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(LibraryRegistryError, match="linked library registry root"):
        LibraryRegistry(linked_root).list()

    registry = LibraryRegistry(root)
    registry.ensure_main()
    linked_library = root / "linked"
    try:
        linked_library.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(LibraryRegistryError, match="linked library registry entry"):
        registry.list()
    linked_library.unlink()

    lock_path = root / ".library-registry.lock"
    lock_path.unlink(missing_ok=True)
    try:
        lock_path.symlink_to(tmp_path / "missing.lock")
    except OSError:
        pytest.skip("file symlinks are unavailable")
    with pytest.raises(LibraryRegistryError, match="linked library registry lock"):
        registry.list()
    lock_path.unlink()

    definition = root / "main" / "library.json"
    definition.unlink()
    try:
        definition.symlink_to(tmp_path / "missing.json")
    except OSError:
        pytest.skip("file symlinks are unavailable")
    with pytest.raises(LibraryRegistryError, match="linked library definition"):
        registry.list()


def test_concurrent_creation_has_one_winner(tmp_path: Path) -> None:
    registry = LibraryRegistry(tmp_path / "libraries")
    registry.ensure_main()

    def create(name: str) -> str:
        try:
            registry.create("same-id", name)
            return "created"
        except LibraryConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, ("One", "Two")))

    assert sorted(outcomes) == ["conflict", "created"]
    assert len([item for item in registry.list() if item.id == "same-id"]) == 1
