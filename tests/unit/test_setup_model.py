"""Setup model verification tests use mocked model runtime boundaries."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from autodj import setup_model


def _cfg(tmp_path, *, default=True):
    return SimpleNamespace(
        model=SimpleNamespace(
            name=("OpenMuQ/MuQ-large-msd-iter" if default else "custom/model"),
            revision="main",
            manual_path=None,
        ),
        index=SimpleNamespace(model_dir=tmp_path / "models"),
        huggingface=SimpleNamespace(token=None),
    )


def _runtime(monkeypatch, *, cached, embedding=None, load_error=None):
    local = Mock(return_value=cached)
    download = Mock(return_value="downloaded-model")
    wrapper = Mock()
    wrapper.device = "cpu"
    wrapper.embed_array.return_value = (
        np.ones(1024, dtype=np.float32) if embedding is None else embedding
    )
    load = Mock(side_effect=load_error) if load_error else Mock(return_value=wrapper)
    monkeypatch.setitem(
        sys.modules,
        "autodj.model_identity",
        types.SimpleNamespace(local_model_folder=local),
    )
    monkeypatch.setitem(
        sys.modules,
        "autodj.model",
        types.SimpleNamespace(
            EMBEDDING_DIM=1024,
            MUQ_SAMPLE_RATE=24_000,
            download_model_if_needed=download,
            load_model=load,
        ),
    )
    return local, download, load, wrapper


def test_cached_model_is_loaded_offline_without_downloading(tmp_path, monkeypatch):
    """ADR-0012-11: setup verifies existing weights without a new download."""
    cached = tmp_path / "cached"
    local, download, load, wrapper = _runtime(monkeypatch, cached=cached)

    setup_model.verify_model(_cfg(tmp_path))

    local.assert_called_once()
    download.assert_not_called()
    load.assert_called_once_with(cached)
    args, kwargs = wrapper.embed_array.call_args
    audio = args[0]
    assert kwargs == {"sample_rate": 24_000}
    assert audio[0] != audio[1]


def test_missing_model_is_downloaded_then_inferred(tmp_path, monkeypatch):
    _runtime(monkeypatch, cached=None)
    monkeypatch.setattr(
        setup_model.shutil, "disk_usage", lambda _path: SimpleNamespace(free=3 * 1024**3)
    )
    download = sys.modules["autodj.model"].download_model_if_needed

    setup_model.verify_model(_cfg(tmp_path))

    download.assert_called_once()
    assert download.call_args.args[2] is None


def test_default_model_download_stops_when_disk_is_low(tmp_path, monkeypatch):
    _runtime(monkeypatch, cached=None)
    monkeypatch.setattr(setup_model.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1))
    download = sys.modules["autodj.model"].download_model_if_needed

    with pytest.raises(setup_model.ModelVerificationError, match="free disk space"):
        setup_model.verify_model(_cfg(tmp_path))

    download.assert_not_called()


def test_custom_model_does_not_assume_default_download_size(tmp_path, monkeypatch):
    _runtime(monkeypatch, cached=None)
    cfg = _cfg(tmp_path, default=False)
    monkeypatch.setattr(
        setup_model.shutil, "disk_usage", lambda _path: pytest.fail("unexpected disk check")
    )

    setup_model.verify_model(cfg)


def test_load_and_embedding_errors_are_reported(tmp_path, monkeypatch):
    _runtime(monkeypatch, cached=tmp_path / "cached", load_error=RuntimeError("broken weights"))

    with pytest.raises(setup_model.ModelVerificationError, match="broken weights"):
        setup_model.verify_model(_cfg(tmp_path))


@pytest.mark.parametrize(
    "embedding, message",
    [
        (np.ones(3, dtype=np.float32), "embedding shape"),
        (np.full(1024, np.nan, dtype=np.float32), "non-finite"),
    ],
)
def test_invalid_embedding_is_reported(tmp_path, monkeypatch, embedding, message):
    _runtime(monkeypatch, cached=tmp_path / "cached", embedding=embedding)

    with pytest.raises(setup_model.ModelVerificationError, match=message):
        setup_model.verify_model(_cfg(tmp_path))
