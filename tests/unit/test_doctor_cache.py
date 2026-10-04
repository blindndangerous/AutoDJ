from __future__ import annotations

from pathlib import Path

import pytest

from autodj.doctor_cache import backup_dj_meta_cache


def test_backs_up_database_and_sqlite_sidecars(tmp_path: Path) -> None:
    names = ("dj_meta.db", "dj_meta.db-wal", "dj_meta.db-shm", "dj_meta.db-journal")
    contents = {name: f"contents of {name}" for name in names}
    for name, content in contents.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    unrelated = tmp_path / "other.db"
    unrelated.write_text("keep", encoding="utf-8")

    backup = backup_dj_meta_cache(tmp_path)

    assert backup is not None
    assert backup.is_dir()
    assert {path.name for path in backup.iterdir()} == set(names)
    assert {path.name: path.read_text(encoding="utf-8") for path in backup.iterdir()} == contents
    assert all(not (tmp_path / name).exists() for name in names)
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_returns_none_when_cache_files_are_absent(tmp_path: Path) -> None:
    assert backup_dj_meta_cache(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_restores_moved_files_after_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "dj_meta.db"
    sidecar = tmp_path / "dj_meta.db-wal"
    database.write_text("database", encoding="utf-8")
    sidecar.write_text("wal", encoding="utf-8")
    original_rename = Path.rename

    def failing_rename(path: Path, target: str | Path) -> Path:
        if path == sidecar:
            raise OSError("simulated move failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError, match="simulated move failure"):
        backup_dj_meta_cache(tmp_path)

    assert database.read_text(encoding="utf-8") == "database"
    assert sidecar.read_text(encoding="utf-8") == "wal"
    assert list(tmp_path.glob("dj_meta.db.doctor-*.bak")) == []


def test_refuses_symlinked_cache_file(tmp_path: Path) -> None:
    source = tmp_path / "real.db"
    source.write_text("keep", encoding="utf-8")
    link = tmp_path / "dj_meta.db"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="non-regular"):
        backup_dj_meta_cache(tmp_path)
    assert source.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
    assert list(tmp_path.glob("dj_meta.db.doctor-*.bak")) == []


def test_refuses_non_file_cache_path(tmp_path: Path) -> None:
    (tmp_path / "dj_meta.db").mkdir()

    with pytest.raises(ValueError, match="non-regular"):
        backup_dj_meta_cache(tmp_path)

    assert (tmp_path / "dj_meta.db").is_dir()
    assert list(tmp_path.glob("dj_meta.db.doctor-*.bak")) == []
