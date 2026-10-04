"""Reversible backup helpers for the DJ metadata cache."""

from __future__ import annotations

import stat
import uuid
from contextlib import suppress
from pathlib import Path


def backup_dj_meta_cache(index_dir: Path) -> Path | None:
    """Move a corrupt or legacy metadata cache aside before rebuilding it.

    AutoDJ's server and indexing process must be stopped before calling this
    function so the database and its SQLite sidecars cannot change mid-move.
    """
    database = index_dir / "dj_meta.db"
    files = [
        database,
        *(database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal")),
    ]
    present: list[Path] = []
    for path in files:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Refusing to back up non-regular cache path: {path}")
        present.append(path)

    if not present:
        return None

    backup_dir: Path | None = None
    for _ in range(100):
        candidate = index_dir / f"dj_meta.db.doctor-{uuid.uuid4().hex}.bak"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        backup_dir = candidate
        break
    if backup_dir is None:
        raise FileExistsError(f"Could not create a unique cache backup directory in {index_dir}")

    moved: list[tuple[Path, Path]] = []
    try:
        for source in present:
            # Recheck immediately before moving, including symlink status.
            metadata = source.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"Refusing to back up non-regular cache path: {source}")
            destination = backup_dir / source.name
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            source.rename(destination)
            moved.append((source, destination))
    except BaseException:
        for source, destination in reversed(moved):
            if not source.exists() and not source.is_symlink() and destination.exists():
                with suppress(OSError):
                    destination.rename(source)
        if not any(backup_dir.iterdir()):
            backup_dir.rmdir()
        raise

    return backup_dir
