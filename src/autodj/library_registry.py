"""Durable names and identities for the server's music libraries."""

from __future__ import annotations

import builtins
import json
import math
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

from autodj.fsutil import atomic_write

SCHEMA_VERSION = 1
DEFINITION_NAME = "library.json"
_LIBRARY_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z", re.ASCII)
_STAGING_PREFIX = ".library-create-"
_RESTORE_STAGE = re.compile(
    r"\.[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\.restore-[0-9a-f]{32}\Z",
    re.ASCII,
)


class LibraryRegistryError(ValueError):
    """A library definition or registry operation is invalid."""


class LibraryConflict(LibraryRegistryError):
    """A library ID or display name is already in use."""


@dataclass(frozen=True)
class LibraryDefinition:
    """One registered library and its opaque settings document."""

    id: str
    name: str
    settings: dict[str, Any]
    path: Path


def validate_library_id(value: Any) -> str:
    """Return a safe stable library ID or raise :class:`LibraryRegistryError`."""
    if not isinstance(value, str) or len(value) > 32 or _LIBRARY_ID.fullmatch(value) is None:
        raise LibraryRegistryError(
            "library ID must be 1 to 32 lowercase ASCII letters, digits, or interior hyphens"
        )
    return value


class LibraryRegistry:
    """Store library definitions beneath ``<index>/libraries/<id>``.

    The registry owns names and opaque settings only. Catalogs, analysis
    caches, and physical music folders remain separate resources.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()

    def ensure_main(self) -> LibraryDefinition:
        """Register the legacy ``main`` directory without touching its data."""
        self._prepare_root()
        with self._lock():
            directory = self.root / "main"
            self._check_directory(directory, "main library directory")
            definition_path = directory / DEFINITION_NAME
            if (
                definition_path.exists()
                or definition_path.is_symlink()
                or definition_path.is_junction()
            ):
                if self._is_link(definition_path):
                    raise LibraryRegistryError(
                        f"refusing linked library definition: {definition_path}"
                    )
                try:
                    raw = definition_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise LibraryRegistryError(
                        f"cannot read library definition {definition_path}: {exc}"
                    ) from exc
                if raw.strip():
                    definition = self._read_definition(directory)
                    if definition.id != "main":
                        raise LibraryRegistryError(
                            "library definition ID "
                            f"{definition.id!r} does not match directory 'main'"
                        )
                    others = self._list_unlocked(skip_ids={"main"})
                    self._check_name_available(definition.name, definitions=others)
                    return definition
            # A missing or blank file is the one recoverable interrupted
            # adoption state. atomic_write leaves a complete document or none.
            others = self._list_unlocked(skip_ids={"main"})
            self._check_name_available("Main", definitions=others)
            self._write_definition(directory, "main", "Main", {})
            return self._read_definition(directory)

    def list(self, *, allow_legacy_main: bool = False) -> builtins.list[LibraryDefinition]:
        """Validate and return every public library definition by ID."""
        self._prepare_root(create=False)
        if not self.root.exists():
            return []
        lock_path = self.root / ".library-registry.lock"
        if self._is_link(lock_path):
            raise LibraryRegistryError(f"refusing linked library registry lock: {lock_path}")
        # Definitions and new directories are published atomically. Readers
        # must not create a lock file: Doctor and backup inspection are read-only.
        return self._list_unlocked(allow_legacy_main=allow_legacy_main)

    def get(self, library_id: str) -> LibraryDefinition:
        """Return one definition, validating the complete registry first."""
        wanted = self._validate_id(library_id)
        for definition in self.list():
            if definition.id == wanted:
                return definition
        raise KeyError(f"unknown library ID: {wanted}")

    def path(self, library_id: str) -> Path:
        """Return the registered library's data directory."""
        return self.get(library_id).path

    def create(
        self,
        library_id: str,
        name: str,
        settings: dict[str, Any] | None = None,
        *,
        initializer: Callable[[Path], None] | None = None,
    ) -> LibraryDefinition:
        """Initialize a private library directory, then atomically publish it."""
        library_id = self._validate_id(library_id)
        name = self._validate_name(name)
        settings = self._validate_settings(settings)
        self._prepare_root()
        with self._lock():
            target = self.root / library_id
            if target.exists() or self._is_link(target):
                raise LibraryConflict(f"library directory already exists: {target}")
            definitions = self._list_unlocked()
            self._check_id_available(library_id, definitions)
            self._check_name_available(name, definitions=definitions)
            stage = self.root / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
            stage.mkdir()
            try:
                if initializer is not None:
                    initializer(stage)
                self._write_definition(stage, library_id, name, settings)
                # The rename publishes a complete, nonempty directory so
                # readers never observe a partially written definition.
                os.rename(stage, target)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            return self._read_definition(target)

    def rename(self, library_id: str, name: str) -> LibraryDefinition:
        """Change only a library's display name; its ID and data path stay fixed."""
        library_id = self._validate_id(library_id)
        name = self._validate_name(name)
        self._prepare_root(create=False)
        with self._lock(create=False):
            definitions = self._list_unlocked()
            current = next((item for item in definitions if item.id == library_id), None)
            if current is None:
                raise KeyError(f"unknown library ID: {library_id}")
            self._check_name_available(name, definitions=definitions, excluding=library_id)
            self._write_definition(current.path, library_id, name, current.settings)
            return self._read_definition(current.path)

    def _prepare_root(self, *, create: bool = True) -> None:
        if self._is_link(self.root):
            raise LibraryRegistryError(f"refusing linked library registry root: {self.root}")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if self.root.exists() and not self.root.is_dir():
            raise LibraryRegistryError(f"library registry root is not a directory: {self.root}")

    def _lock(self, *, create: bool = True) -> FileLock:
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".library-registry.lock"
        if self._is_link(lock_path):
            raise LibraryRegistryError(f"refusing linked library registry lock: {lock_path}")
        return FileLock(lock_path)

    def _list_unlocked(
        self, *, skip_ids: set[str] | None = None, allow_legacy_main: bool = False
    ) -> builtins.list[LibraryDefinition]:
        result: builtins.list[LibraryDefinition] = []
        try:
            children = sorted(self.root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise LibraryRegistryError(f"cannot list library registry {self.root}: {exc}") from exc
        for child in children:
            if (
                child.name == ".library-registry.lock"
                or child.name.startswith(_STAGING_PREFIX)
                or _RESTORE_STAGE.fullmatch(child.name)
            ):
                continue
            if self._is_link(child):
                raise LibraryRegistryError(f"refusing linked library registry entry: {child}")
            if not child.is_dir():
                continue
            library_id = self._validate_id(child.name)
            if library_id in (skip_ids or set()):
                continue
            definition_path = child / DEFINITION_NAME
            if self._is_link(definition_path):
                raise LibraryRegistryError(f"refusing linked library definition: {definition_path}")
            legacy_main = (
                allow_legacy_main
                and library_id == "main"
                and (
                    not definition_path.exists()
                    or (
                        definition_path.is_file()
                        and not definition_path.read_text(encoding="utf-8").strip()
                    )
                )
            )
            definition = (
                LibraryDefinition("main", "Main", {}, child)
                if legacy_main
                else self._read_definition(child)
            )
            if definition.id != library_id:
                raise LibraryRegistryError(
                    "library definition ID "
                    f"{definition.id!r} does not match directory {child.name!r}"
                )
            result.append(definition)
        names: set[str] = set()
        for definition in result:
            key = definition.name.casefold()
            if key in names:
                raise LibraryRegistryError(f"duplicate library display name: {definition.name!r}")
            names.add(key)
        return result

    def _read_definition(self, directory: Path) -> LibraryDefinition:
        self._check_directory(directory, "library directory")
        definition_path = directory / DEFINITION_NAME
        if self._is_link(definition_path):
            raise LibraryRegistryError(f"refusing linked library definition: {definition_path}")
        try:
            info = definition_path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise LibraryRegistryError(
                    f"library definition is not a regular file: {definition_path}"
                )
            document = json.loads(definition_path.read_text(encoding="utf-8"))
        except LibraryRegistryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LibraryRegistryError(
                f"cannot read library definition {definition_path}: {exc}"
            ) from exc
        if not isinstance(document, dict) or set(document) - {
            "schema_version",
            "id",
            "name",
            "settings",
        }:
            raise LibraryRegistryError(f"invalid library definition fields: {definition_path}")
        if document.get("schema_version") != SCHEMA_VERSION or isinstance(
            document.get("schema_version"), bool
        ):
            raise LibraryRegistryError(f"unsupported library definition schema: {definition_path}")
        library_id = self._validate_id(document.get("id"))
        name = self._validate_name(document.get("name"))
        settings = self._validate_settings(document.get("settings", {}))
        return LibraryDefinition(library_id, name, settings, directory)

    def _write_definition(
        self, directory: Path, library_id: str, name: str, settings: dict[str, Any]
    ) -> None:
        document = {
            "schema_version": SCHEMA_VERSION,
            "id": library_id,
            "name": name,
            "settings": settings,
        }
        try:
            payload = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        except (TypeError, ValueError) as exc:
            raise LibraryRegistryError(f"library settings are not JSON-compatible: {exc}") from exc
        atomic_write(directory / DEFINITION_NAME, payload)

    @staticmethod
    def _validate_id(value: Any) -> str:
        return validate_library_id(value)

    @staticmethod
    def _validate_name(value: Any) -> str:
        if not isinstance(value, str):
            raise LibraryRegistryError("library name must be text")
        name = value.strip()
        if not name or len(name) > 80:
            raise LibraryRegistryError("library name must be nonblank and at most 80 characters")
        return name

    @staticmethod
    def _validate_settings(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if type(value) is not dict:
            raise LibraryRegistryError("library settings must be an object with text keys")

        def copy_json(item: Any) -> Any:
            if item is None or type(item) in (str, bool, int):
                return item
            if type(item) is float:
                if not math.isfinite(item):
                    raise LibraryRegistryError("library settings cannot contain NaN or Infinity")
                return item
            if type(item) is list:
                return [copy_json(child) for child in item]
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise LibraryRegistryError("library settings object keys must be text")
                return {key: copy_json(child) for key, child in item.items()}
            raise LibraryRegistryError(
                f"library settings contain a non-JSON value: {type(item).__name__}"
            )

        return copy_json(value)

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    def _check_directory(self, path: Path, label: str) -> None:
        if self._is_link(path):
            raise LibraryRegistryError(f"refusing linked {label}: {path}")
        if path.exists() and not path.is_dir():
            raise LibraryRegistryError(f"{label} is not a directory: {path}")

    @staticmethod
    def _check_id_available(library_id: str, definitions: builtins.list[LibraryDefinition]) -> None:
        if any(definition.id == library_id for definition in definitions):
            raise LibraryConflict(f"library ID already exists: {library_id}")

    @staticmethod
    def _check_name_available(
        name: str,
        definitions: builtins.list[LibraryDefinition] | None = None,
        *,
        excluding: str | None = None,
    ) -> None:
        folded = name.casefold()
        for definition in definitions or []:
            if definition.id != excluding and definition.name.casefold() == folded:
                raise LibraryConflict(f"library name already exists: {name}")
