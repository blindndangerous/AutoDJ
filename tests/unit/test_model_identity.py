"""Model weights are named by snapshot commit or file digest, never by branch name."""

import os
import time
from pathlib import Path

import pytest

from autodj.config import IndexConfig, ModelConfig
from autodj.model_identity import (
    CachedModelCommit,
    folder_commit,
    local_model_commit,
    local_model_folder,
    weights_digest,
)
from tests.unit.test_worker_api import fake_model_cache


def _configs(tmp_path: Path, **model) -> tuple[ModelConfig, IndexConfig]:
    return ModelConfig(**model), IndexConfig(index_dir=tmp_path / "index", model_dir=tmp_path / "m")


def test_a_cached_model_is_named_by_the_commit_its_branch_resolved_to(tmp_path):
    """ADR-0012-02."""
    model, index = _configs(tmp_path)
    cfg = type("Cfg", (), {"model": model, "index": index})()
    assert local_model_commit(model, index) is None
    fake_model_cache(cfg, "1" * 40)
    assert local_model_commit(model, index) == "1" * 40
    # Another machine that downloaded "main" later holds other weights.
    fake_model_cache(cfg, "2" * 40)
    assert local_model_commit(model, index) == "2" * 40
    pinned = ModelConfig(revision="1" * 40)
    assert local_model_commit(pinned, index) == "1" * 40


def test_a_manual_folder_is_named_by_a_digest_of_its_weights(tmp_path):
    """ADR-0012-02."""
    folder = tmp_path / "MuQ"
    folder.mkdir()
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(b"weights one")
    (folder / "README.md").write_text("ignored")
    model, index = _configs(tmp_path, manual_path=folder)
    first = local_model_commit(model, index)
    assert first == weights_digest(folder)
    assert first.startswith("sha256:")
    (folder / "README.md").write_text("still ignored")
    assert local_model_commit(model, index) == first
    (folder / "model.safetensors").write_bytes(b"weights two")
    assert local_model_commit(model, index) != first
    assert local_model_folder(ModelConfig(manual_path=tmp_path / "gone"), index) is None
    # A snapshot-looking name outside a snapshots folder is digested too.
    lookalike = tmp_path / ("3" * 40)
    lookalike.mkdir()
    assert folder_commit(lookalike).startswith("sha256:")


def test_weights_rewritten_within_one_clock_tick_get_a_new_digest(tmp_path):
    """Same size and same modification time must not hide a change.

    A coarse filesystem clock (FAT's two seconds, a slow Windows tick) gives
    both writes the same time; pin it to show that case every run.
    """
    weights = tmp_path / "model.safetensors"
    tick = time.time_ns()
    weights.write_bytes(b"weights one")
    os.utime(weights, ns=(tick, tick))
    first = weights_digest(tmp_path)
    weights.write_bytes(b"weights two")
    os.utime(weights, ns=(tick, tick))
    assert weights_digest(tmp_path) != first


def test_settled_weights_are_read_once(tmp_path, monkeypatch):
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"weights one")
    settled = time.time_ns() - 60_000_000_000
    os.utime(weights, ns=(settled, settled))
    first = weights_digest(tmp_path)
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("read again"))
    assert weights_digest(tmp_path) == first


def test_the_server_looks_the_commit_up_at_most_once_a_minute(tmp_path):
    model, index = _configs(tmp_path)
    cfg = type("Cfg", (), {"model": model, "index": index})()
    now = [0.0]
    cached = CachedModelCommit(lambda: (model, index), monotonic=lambda: now[0])
    assert cached() is None
    fake_model_cache(cfg, "4" * 40)
    now[0] = 59.0
    assert cached() is None
    now[0] = 61.0
    assert cached() == "4" * 40


def test_a_download_missing_only_ignored_files_still_counts(tmp_path):
    """The lookup filters like the download, so skipped formats are not "missing".

    huggingface_hub compares a local snapshot with the file list it cached for
    the commit; a download that skipped the TensorFlow or Flax weights is
    complete for AutoDJ.
    """
    import json

    model, index = _configs(tmp_path)
    cfg = type("Cfg", (), {"model": model, "index": index})()
    fake_model_cache(cfg, "3" * 40)
    repo = next(index.model_dir.glob("models--*"))
    snapshot = repo / "snapshots" / ("3" * 40)
    files = {
        p.name: {"size": p.stat().st_size, "blob_id": "0" * 40}
        for p in snapshot.iterdir()
        if p.is_file()
    }
    # The repository also holds the old PyTorch weights, which AutoDJ skips.
    files["pytorch_model.bin"] = {"size": 1, "blob_id": "1" * 40}
    listed = {"format_version": 1, "files": files}
    (repo / "trees").mkdir(exist_ok=True)
    (repo / "trees" / ("3" * 40 + ".json")).write_text(json.dumps(listed), encoding="utf-8")
    assert local_model_folder(model, index) == snapshot
    assert local_model_commit(model, index) == "3" * 40


@pytest.mark.parametrize("missing", [None, "torch", "muq", "transformers"])
def test_model_analysis_availability_checks_packages_without_importing(monkeypatch, missing):
    import autodj.model_identity as identity

    monkeypatch.delenv("AUTODJ_NO_MODEL", raising=False)
    monkeypatch.setattr(
        identity.importlib.util, "find_spec", lambda name: None if name == missing else object()
    )
    assert identity.model_analysis_available() is (missing is None)


def test_model_analysis_disabled_even_when_packages_are_installed(monkeypatch):
    import autodj.model_identity as identity

    monkeypatch.setenv("AUTODJ_NO_MODEL", "1")
    monkeypatch.setattr(
        identity.importlib.util,
        "find_spec",
        lambda name: pytest.fail("disabled analysis probed packages"),
    )
    assert not identity.model_analysis_available()
