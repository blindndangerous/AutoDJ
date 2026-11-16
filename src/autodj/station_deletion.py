"""Quarantine and recover archived station deletion operations."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autodj import index_manifest as im
from autodj.fsutil import atomic_write, fsync_directory
from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN, StationPaths

_RETAINED = (
    "announcements",
    "liners",
    "speech",
    "edition_audio",
    "carts",
    "time_checks",
    "time_checks_cache",
    "intros",
    "ads",
)


class StationDeletionError(ValueError):
    """The station cannot be safely deleted or its deletion needs recovery."""

    def __init__(self, message: str, *, committed: bool = False) -> None:
        super().__init__(message)
        self.committed = committed


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _plain_tree(root: Path) -> None:
    """Reject links, reparse points, special files and hard links before quarantine."""
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise StationDeletionError(f"Station folder is not a plain directory: {root}")
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in dirs:
            child = base / name
            if child.is_symlink() or child.is_junction() or not child.is_dir():
                raise StationDeletionError(f"Linked or special path prevents deletion: {child}")
        for name in files:
            child = base / name
            if child.is_symlink() or child.is_junction():
                raise StationDeletionError(f"Linked path prevents deletion: {child}")
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StationDeletionError(f"Non-regular or shared file prevents deletion: {child}")


def _reject_external_overlap(
    cfg: Any,
    state_dir: Path,
    music_roots: tuple[Path, ...] = (),
    protected_roots: tuple[Path, ...] = (),
) -> None:
    protected = [
        Path(cfg.index.library_dir),
        Path(cfg.index.shared_dir),
        Path(cfg.library.music_dir) if cfg.library.music_dir else None,
        Path(cfg.recording.folder),
        Path(cfg.time_checks.clips_folder) if cfg.time_checks.clips_folder else None,
        *(Path(folder) for folder in cfg.time_checks.voice_packs),
        *music_roots,
        *protected_roots,
    ]
    target = state_dir.resolve()
    for candidate in (item for item in protected if item is not None):
        resolved = candidate.resolve()
        if _within(resolved, target) or _within(target, resolved):
            raise StationDeletionError(
                f"Station storage overlaps a protected library, recording or time-check folder: {candidate}"
            )


def _journal_write(path: Path, journal: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(journal, indent=2) + "\n")
    # atomic_write flushes the file and directory where supported. Keep the
    # explicit call here so failures are visible before a destructive phase.
    fsync_directory(path.parent)


def _paths(cfg: Any) -> tuple[Path, Path, Path]:
    root = Path(cfg.index.index_dir).absolute()
    journal = root / ".station-deletions"
    retained = root / "retained-station-audio"
    return root, journal, retained


def _no_link_components(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise StationDeletionError(f"Linked deletion path component: {part}")


def _retained_names(paths: StationPaths) -> list[str]:
    """The station-owned audio children; a configured external clips path is excluded."""
    state = paths.state_dir.absolute()
    candidates = (
        paths.announcements,
        paths.liners,
        paths.speech,
        paths.edition_audio,
        paths.cart_uploads,
        paths.time_check_clips,
        paths.time_checks_cache,
        paths.intros,
        paths.ad_spots,
    )
    return list(
        dict.fromkeys(
            path.name
            for path in candidates
            if path.absolute().parent == state and path.name in _RETAINED
        )
    )


def _validate_journal_paths(cfg: Any, journal: dict[str, Any]) -> None:
    """Recompute every recovery path; never trust a journal-supplied target."""
    slug = journal.get("slug")
    operation = journal.get("operation")
    if type(journal.get("version")) is not int or journal["version"] != 1:
        raise StationDeletionError("Unsupported station deletion journal version")
    if journal.get("phase") not in {"prepared", "committed", "purging", "complete"}:
        raise StationDeletionError("Unknown station deletion journal phase")
    if type(journal.get("delete_voice_recordings")) is not bool:
        raise StationDeletionError("Invalid voice-recording choice in deletion journal")
    names = journal.get("retained_names")
    if (
        not isinstance(names, list)
        or any(not isinstance(name, str) or name not in _RETAINED for name in names)
        or len(names) != len(set(names))
    ):
        raise StationDeletionError("Invalid retained paths in station deletion journal")
    if not isinstance(journal.get("station_name"), str) or not journal["station_name"].strip():
        raise StationDeletionError("Missing station name in deletion journal")
    if not isinstance(journal.get("started_at"), str) or not journal["started_at"]:
        raise StationDeletionError("Missing deletion timestamp in journal")
    if not isinstance(slug, str) or slug == DEFAULT_SLUG or not SLUG_PATTERN.fullmatch(slug):
        raise StationDeletionError("Invalid station ID in deletion journal")
    if (
        not isinstance(operation, str)
        or len(operation) != 32
        or any(c not in "0123456789abcdef" for c in operation)
    ):
        raise StationDeletionError("Invalid operation ID in deletion journal")
    retained_done = journal.get("retained_done", [])
    if (
        not isinstance(retained_done, list)
        or any(not isinstance(item, str) or item not in names for item in retained_done)
        or len(retained_done) != len(set(retained_done))
    ):
        raise StationDeletionError("Invalid retained-audio progress in deletion journal")
    root, _journal_dir, retained_root = _paths(cfg)
    _no_link_components(root)
    _no_link_components(root / ".station-deletions")
    _no_link_components(root / ".station-deletions" / "quarantine")
    _no_link_components(retained_root)
    expected = {
        "original": StationPaths.for_station(cfg, slug).state_dir.absolute(),
        "liner_source_path": StationPaths.for_station(cfg, slug).liners.absolute(),
        "quarantine": root / ".station-deletions" / "quarantine" / f"{slug}-{operation}",
        "retained": retained_root / f"{slug}-{operation}",
    }
    for key, path in expected.items():
        _no_link_components(path.parent)
        if Path(journal.get(key, "")) != path:
            raise StationDeletionError(f"Invalid {key} path in deletion journal")


def _finish(cfg: Any, journal_path: Path, journal: dict[str, Any]) -> None:
    """Finish a committed deletion idempotently; never restore after purge starts."""
    quarantine = Path(journal["quarantine"])
    retained = Path(journal["retained"])
    original = Path(journal["original"])
    if original.exists() or original.is_symlink() or original.is_junction():
        raise StationDeletionError("The original station folder exists during committed deletion")
    if not journal["delete_voice_recordings"]:
        retained_root = retained.parent
        for path in (retained_root, retained):
            if path.is_symlink() or path.is_junction():
                raise StationDeletionError(f"Linked retained-audio folder: {path}")
        retained.mkdir(parents=True, exist_ok=True)
        _plain_tree(retained)
        fsync_directory(retained.parent)
        names = journal.get("retained_names")
        if not isinstance(names, list) or any(name not in _RETAINED for name in names):
            raise StationDeletionError("Invalid retained paths in station deletion journal")
        manifest_path = retained / "retention.json"
        manifest = {
            "version": 1,
            "operation": journal["operation"],
            "station_id": journal["slug"],
            "station_name": journal["station_name"],
            "deleted_at": journal["started_at"],
            "original_station_directory": journal["original"],
            "retained_paths": [f"{retained.name}/{name}" for name in names],
            "recovery": "Copy each retained child folder back into a recreated station directory with the same station ID. Restore the matching AutoDJ backup first if settings or history are also needed.",
        }
        if manifest_path.exists() or manifest_path.is_symlink() or manifest_path.is_junction():
            info = manifest_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StationDeletionError(f"Unsafe retention manifest: {manifest_path}")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or existing.get("operation") != journal["operation"]:
                raise StationDeletionError(
                    f"Retention manifest belongs to another deletion: {manifest_path}"
                )
        else:
            atomic_write(manifest_path, json.dumps(manifest, indent=2) + "\n")
        for name in journal["retained_names"]:
            source = quarantine / name
            target = retained / name
            source_exists = source.exists() or source.is_symlink() or source.is_junction()
            target_exists = target.exists() or target.is_symlink() or target.is_junction()
            if source_exists and target_exists:
                raise StationDeletionError(f"Retained audio exists in both locations: {name}")
            if source_exists:
                _plain_tree(source)
                os.replace(source, target)
                fsync_directory(quarantine)
                fsync_directory(retained)
            elif not target_exists:
                raise StationDeletionError(
                    f"Retained station audio is missing from both locations: {name}"
                )
            # A missing source plus existing target means a prior recovery
            # moved it successfully before the journal update.
            journal["retained_done"] = [
                item for item in journal["retained_names"] if not (quarantine / item).exists()
            ]
            _journal_write(journal_path, journal)
    if quarantine.exists() or quarantine.is_symlink() or quarantine.is_junction():
        _plain_tree(quarantine)
        shutil.rmtree(quarantine)
        fsync_directory(quarantine.parent)
    journal["phase"] = "complete"
    journal["completed_at"] = datetime.now(UTC).isoformat()
    _journal_write(journal_path, journal)
    fsync_directory(journal_path.parent)


def recover_station_deletions(cfg: Any) -> list[str]:
    """Recover interrupted station deletions before definitions are loaded."""
    with im.publication_lock(Path(cfg.index.index_dir)):
        return _recover_station_deletions_locked(cfg)


def _recover_station_deletions_locked(cfg: Any) -> list[str]:
    """Inspect and complete journals while backups are excluded."""
    root, journal_dir, _retained = _paths(cfg)
    _no_link_components(root)
    _no_link_components(journal_dir)
    if journal_dir.is_symlink() or journal_dir.is_junction():
        raise StationDeletionError(f"Linked station deletion journal folder: {journal_dir}")
    if not journal_dir.exists():
        return []
    if not journal_dir.is_dir():
        raise StationDeletionError(f"Station deletion journal path is not a folder: {journal_dir}")
    recovered: list[str] = []
    for path in sorted(journal_dir.glob("*.json")):
        try:
            _no_link_components(path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StationDeletionError(
                    f"Linked or non-regular station deletion journal: {path}"
                )
            journal = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(journal, dict) or journal.get("version") != 1:
                raise StationDeletionError(f"Invalid station deletion journal: {path}")
            _validate_journal_paths(cfg, journal)
            if path.stem != journal["operation"]:
                raise StationDeletionError(f"Journal filename does not match its operation: {path}")
            original = Path(journal["original"])
            quarantine = Path(journal["quarantine"])
            if journal.get("phase") == "prepared":
                if quarantine.exists() or quarantine.is_symlink() or quarantine.is_junction():
                    if original.exists() or original.is_symlink() or original.is_junction():
                        raise StationDeletionError(
                            f"Both station and quarantine folders exist; inspect {path}"
                        )
                    _plain_tree(quarantine)
                    os.replace(quarantine, original)
                    fsync_directory(original.parent)
                    fsync_directory(quarantine.parent)
                elif original.is_symlink() or original.is_junction() or not original.is_dir():
                    raise StationDeletionError(
                        f"Prepared deletion has neither station nor quarantine folder: {path}"
                    )
                path.unlink(missing_ok=True)
                fsync_directory(path.parent)
                recovered.append(f"Restored interrupted station deletion for {journal['slug']}.")
            elif journal.get("phase") in {"committed", "purging", "complete"}:
                _finish(cfg, path, journal)
                recovered.append(f"Finished deleting station {journal['slug']}.")
            else:
                raise StationDeletionError(f"Unknown station deletion phase in {path}")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise StationDeletionError(f"Station deletion recovery is incomplete: {exc}") from exc
    return recovered


def delete_archived_station(
    cfg: Any,
    slug: str,
    *,
    station_name: str,
    delete_voice_recordings: bool,
    music_roots: tuple[Path, ...] = (),
    protected_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Delete under the index publication lock shared with backup snapshots."""
    with im.publication_lock(Path(cfg.index.index_dir)):
        return _delete_archived_station_locked(
            cfg,
            slug,
            station_name=station_name,
            delete_voice_recordings=delete_voice_recordings,
            music_roots=music_roots,
            protected_roots=protected_roots,
        )


def _delete_archived_station_locked(
    cfg: Any,
    slug: str,
    *,
    station_name: str,
    delete_voice_recordings: bool,
    music_roots: tuple[Path, ...] = (),
    protected_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Quarantine, commit, preserve spoken assets if requested, then purge the station."""
    if slug == DEFAULT_SLUG or not SLUG_PATTERN.fullmatch(slug):
        raise StationDeletionError("The default station cannot be deleted.")
    if type(delete_voice_recordings) is not bool:
        raise StationDeletionError("Delete voice recordings must be true or false.")
    if not isinstance(station_name, str) or not station_name.strip():
        raise StationDeletionError("Station display name is missing.")
    paths = StationPaths.for_station(cfg, slug)
    original_path = paths.state_dir
    _no_link_components(original_path)
    _plain_tree(original_path)
    original = original_path.resolve()
    if not original.exists():
        raise StationDeletionError("Station folder is missing; refusing permanent deletion.")
    _reject_external_overlap(cfg, original, music_roots, protected_roots)
    root, journal_dir, retained_root = _paths(cfg)
    _no_link_components(root)
    root.mkdir(parents=True, exist_ok=True)
    _no_link_components(root)
    _no_link_components(journal_dir)
    _no_link_components(journal_dir / "quarantine")
    _no_link_components(retained_root)
    if original.stat().st_dev != root.stat().st_dev:
        raise StationDeletionError(
            "Station folder and deletion journal must be on the same volume."
        )
    operation = uuid.uuid4().hex
    quarantine_dir = root / ".station-deletions" / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    _no_link_components(quarantine_dir)
    quarantine = quarantine_dir / f"{slug}-{operation}"
    retained = retained_root / f"{slug}-{operation}"
    if any(
        path.exists() or path.is_symlink() or path.is_junction() for path in (quarantine, retained)
    ):
        raise StationDeletionError("Could not reserve a unique deletion location.")
    journal_dir.mkdir(parents=True, exist_ok=True)
    _no_link_components(journal_dir)
    journal_path = journal_dir / f"{operation}.json"
    journal: dict[str, Any] = {
        "version": 1,
        "phase": "prepared",
        "operation": operation,
        "slug": slug,
        "original": str(original),
        "quarantine": str(quarantine),
        "retained": str(retained),
        "liner_source_path": str(paths.liners.absolute()),
        "retained_names": [name for name in _retained_names(paths) if (original / name).exists()],
        "delete_voice_recordings": bool(delete_voice_recordings),
        "station_name": station_name.strip(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    _journal_write(journal_path, journal)
    try:
        _plain_tree(original)
        os.replace(original, quarantine)
        fsync_directory(original.parent)
        fsync_directory(quarantine.parent)
        _plain_tree(quarantine)
        journal["phase"] = "committed"
        _journal_write(journal_path, journal)
    except BaseException:
        if quarantine.exists() and not original.exists():
            os.replace(quarantine, original)
            fsync_directory(original.parent)
            fsync_directory(quarantine.parent)
        journal_path.unlink(missing_ok=True)
        fsync_directory(journal_path.parent)
        raise
    try:
        _finish(cfg, journal_path, journal)
    except BaseException:
        # Committed deletion is completed forward during serve startup recovery.
        raise StationDeletionError(
            f"Deletion of {slug} is committed and will finish during recovery.", committed=True
        ) from None
    return {
        "slug": slug,
        "retained_audio": str(retained) if not delete_voice_recordings else None,
        "journal": str(journal_path),
        "cleanup_pending": True,
    }


def complete_station_deletion(cfg: Any, journal_path: Path) -> None:
    """Remove a completed journal after every catalog has forgotten owned state."""
    with im.publication_lock(Path(cfg.index.index_dir)):
        _complete_station_deletion_locked(cfg, journal_path)


def _complete_station_deletion_locked(cfg: Any, journal_path: Path) -> None:
    _root, journal_dir, _retained = _paths(cfg)
    _no_link_components(journal_dir)
    _no_link_components(journal_path)
    if journal_path.parent != journal_dir:
        raise StationDeletionError("Invalid station deletion journal location")
    if not journal_path.name.endswith(".json"):
        raise StationDeletionError("Invalid station deletion journal filename")
    info = journal_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StationDeletionError(
            f"Linked or non-regular station deletion journal: {journal_path}"
        )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if not isinstance(journal, dict):
        raise StationDeletionError("Invalid station deletion journal")
    _validate_journal_paths(cfg, journal)
    if journal_path.stem != journal["operation"]:
        raise StationDeletionError("Journal filename does not match its operation")
    if journal.get("phase") != "complete":
        raise StationDeletionError("Station deletion purge is not complete")
    journal_path.unlink()
    fsync_directory(journal_dir)


def cleanup_completed_station_deletions(cfg: Any, catalogs: tuple[Any, ...]) -> list[str]:
    """Retry catalog-owned cleanup for physically complete deletion journals."""
    root, journal_dir, _retained = _paths(cfg)
    completed: list[str] = []
    with im.publication_lock(Path(cfg.index.index_dir)):
        _no_link_components(root)
        _no_link_components(journal_dir)
        if journal_dir.is_symlink() or journal_dir.is_junction():
            raise StationDeletionError(f"Linked station deletion journal folder: {journal_dir}")
        if not journal_dir.exists():
            return completed
        if not journal_dir.is_dir():
            raise StationDeletionError(
                f"Station deletion journal path is not a folder: {journal_dir}"
            )
        for path in sorted(journal_dir.glob("*.json")):
            _no_link_components(path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StationDeletionError(
                    f"Linked or non-regular station deletion journal: {path}"
                )
            journal = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(journal, dict):
                raise StationDeletionError(f"Invalid station deletion journal: {path}")
            _validate_journal_paths(cfg, journal)
            if path.stem != journal["operation"]:
                raise StationDeletionError(f"Station deletion journal name mismatch: {path}")
            if journal["phase"] != "complete":
                continue
            for catalog in catalogs:
                catalog.remove_station_lifecycle_state(
                    journal["slug"], journal["liner_source_path"]
                )
            complete_station_deletion(cfg, path)
            completed.append(journal["slug"])
        quarantine = journal_dir / "quarantine"
        _no_link_components(quarantine)
        if quarantine.exists():
            if not quarantine.is_dir():
                raise StationDeletionError(
                    f"Station deletion quarantine path is not a folder: {quarantine}"
                )
            if next(quarantine.iterdir(), None) is not None:
                raise StationDeletionError(
                    f"Untracked station deletion quarantine data remains in {quarantine}"
                )
    return completed
