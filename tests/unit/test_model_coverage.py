"""Behavioral coverage for model-cache validation and durable promotion."""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import autodj.model as model
from autodj.config import IndexConfig, ModelConfig
from autodj.model import ModelLoadError, _indexed_weights, _inspect_model_path


def test_portable_windows_lock_fallback_reports_unavailable_api() -> None:
    with pytest.raises(OSError, match="Windows file locking is unavailable"):
        model._unsupported_windows_lock(12, 1, 1)


def _write_complete_model(path: Path, *, repo_id: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"weights")
    if repo_id is not None:
        (path / ".autodj-complete").write_text(
            json.dumps({"repo_id": repo_id, "revision": "main"}),
            encoding="utf-8",
        )


def _auto_config(tmp_path: Path) -> tuple[ModelConfig, IndexConfig, Path]:
    model_config = ModelConfig(name="example/model", revision="main")
    index_config = IndexConfig(
        index_dir=tmp_path / "index",
        model_dir=tmp_path / "models",
    )
    return model_config, index_config, model.model_cache_path(model_config, index_config)


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_reparse_cache_root_is_rejected_before_traversal(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(Path, "is_symlink", lambda _path: kind == "symlink")
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda _path: kind == "junction",
        raising=False,
    )

    assert model._is_reparse_point(cache)
    assert _inspect_model_path(cache).reason == "symlinked cache path"


def test_cache_without_config_is_reported_before_weight_validation(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.safetensors").write_bytes(b"weights")

    assert _inspect_model_path(cache).reason == "missing config.json"


def test_multiple_weight_indexes_are_rejected(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.safetensors.index.json").write_text("{}", encoding="utf-8")
    (cache / "pytorch_model.bin.index.json").write_text("{}", encoding="utf-8")

    assert _indexed_weights(cache) == (False, "invalid-index")


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("not-json", "invalid shard index: model.safetensors.index.json"),
        (json.dumps({"weight_map": {}}), "invalid-index"),
    ],
)
def test_malformed_or_empty_weight_map_is_rejected(
    tmp_path: Path, contents: str, expected: str
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.safetensors.index.json").write_text(contents, encoding="utf-8")

    assert _indexed_weights(cache) == (False, expected)


def test_noncanonical_index_filename_is_rejected(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    index = cache / "weights.safetensors.index.json"
    index.write_text(json.dumps({"weight_map": {"x": "model.safetensors"}}), encoding="utf-8")

    assert _indexed_weights(cache) == (False, f"invalid shard index: {index.name}")


def test_shards_must_agree_on_total_count(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    shards = [
        "model-00001-of-00002.safetensors",
        "model-00002-of-00003.safetensors",
    ]
    for shard in shards:
        (cache / shard).write_bytes(b"weights")
    (cache / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {str(index): shard for index, shard in enumerate(shards)}}),
        encoding="utf-8",
    )

    assert _indexed_weights(cache) == (
        False,
        "invalid shard index: model.safetensors.index.json",
    )


def test_unindexed_extra_shard_is_rejected(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    indexed = "model-00001-of-00001.safetensors"
    extra = "pytorch_model-00001-of-00001.bin"
    (cache / indexed).write_bytes(b"weights")
    (cache / extra).write_bytes(b"weights")
    (cache / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"encoder": indexed}}),
        encoding="utf-8",
    )

    assert _indexed_weights(cache) == (
        False,
        "invalid shard index: model.safetensors.index.json",
    )


def test_indexed_and_standalone_weights_are_ambiguous(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "config.json").write_text("{}", encoding="utf-8")
    shard = "model-00001-of-00001.safetensors"
    (cache / shard).write_bytes(b"weights")
    (cache / "model.safetensors").write_bytes(b"standalone")
    (cache / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"encoder": shard}}),
        encoding="utf-8",
    )

    assert _inspect_model_path(cache).reason == "ambiguous model weight layout"


def test_marker_identity_requires_both_repo_and_revision(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_complete_model(cache)

    assert _inspect_model_path(cache, repo_id="example/model").reason == "invalid-marker-request"


def test_directory_fsync_flushes_and_closes_descriptor() -> None:
    calls: list[tuple[str, int]] = []
    fake_os = SimpleNamespace(
        name="posix",
        O_RDONLY=1,
        O_DIRECTORY=2,
        open=lambda _path, flags: calls.append(("open", flags)) or 17,
        fsync=lambda descriptor: calls.append(("fsync", descriptor)),
        close=lambda descriptor: calls.append(("close", descriptor)),
    )

    with patch("autodj.model.os", fake_os):
        model._fsync_directory(Path("cache"))

    assert calls == [("open", 3), ("fsync", 17), ("close", 17)]


def test_thread_lock_cache_is_discarded_when_process_id_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = model._thread_lock(tmp_path / "stale")
    monkeypatch.setattr(model, "_thread_locks_pid", -1)
    monkeypatch.setattr(model.os, "getpid", lambda: 12345)

    current = model._thread_lock(tmp_path / "current")

    assert current is not stale
    assert model._thread_locks_pid == 12345
    assert list(model._thread_locks) == [str((tmp_path / "current").absolute())]


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [(errno.EACCES, True), (errno.EAGAIN, True), (errno.EBADF, False)],
)
def test_lock_contention_falls_back_to_errno(error_number: int, expected: bool) -> None:
    error = OSError(error_number, "lock failed")

    assert model._is_windows_lock_contention(error) is expected


def test_posix_cache_lock_releases_after_body_error(tmp_path: Path) -> None:
    operations: list[int] = []
    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda _descriptor, operation: operations.append(operation),
    )
    fake_os = SimpleNamespace(name="posix", getpid=lambda: 12345)

    with (
        patch("autodj.model.os", fake_os),
        patch.dict(sys.modules, {"fcntl": fake_fcntl}),
        pytest.raises(RuntimeError, match="body failed"),
        model._model_cache_lock(tmp_path / "cache"),
    ):
        raise RuntimeError("body failed")

    assert operations == [fake_fcntl.LOCK_EX, fake_fcntl.LOCK_UN]
    assert not model._reentrant_locks


def test_incomplete_download_is_rejected_and_staging_is_cleaned(tmp_path: Path) -> None:
    model_config, index_config, _cache = _auto_config(tmp_path)

    with (
        patch("autodj.model.snapshot_download", return_value="unused"),
        pytest.raises(ModelLoadError, match="download produced incomplete model cache"),
    ):
        model.download_model_if_needed(model_config, index_config)

    assert not list(index_config.model_dir.glob("*.staging-*"))


def test_completed_competing_download_wins_without_being_replaced(tmp_path: Path) -> None:
    model_config, index_config, cache = _auto_config(tmp_path)

    def populate(**kwargs: object) -> str:
        staging = Path(str(kwargs["local_dir"]))
        _write_complete_model(staging)
        _write_complete_model(cache, repo_id=model_config.name)
        return str(staging)

    with patch("autodj.model.snapshot_download", side_effect=populate):
        result = model.download_model_if_needed(model_config, index_config)

    assert result == cache
    assert model.inspect_model_cache(model_config, index_config).complete
    assert not list(index_config.model_dir.glob("*.staging-*"))


@pytest.mark.parametrize("existing_kind", ["file", "directory"])
def test_incomplete_destination_is_replaced_atomically(tmp_path: Path, existing_kind: str) -> None:
    model_config, index_config, cache = _auto_config(tmp_path)
    cache.parent.mkdir(parents=True)
    if existing_kind == "file":
        cache.write_text("incomplete", encoding="utf-8")
    else:
        cache.mkdir()
        (cache / "stale").write_text("incomplete", encoding="utf-8")

    def populate(**kwargs: object) -> str:
        staging = Path(str(kwargs["local_dir"]))
        _write_complete_model(staging)
        return str(staging)

    with patch("autodj.model.snapshot_download", side_effect=populate):
        result = model.download_model_if_needed(model_config, index_config)

    assert result == cache
    assert model.inspect_model_cache(model_config, index_config).complete
    assert not (cache / "stale").exists()
