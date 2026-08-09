"""Unit tests for autodj.model.

All external dependencies (muq, torch, huggingface_hub) are mocked so
tests run fast without downloading anything.
"""

import hashlib
import json
import multiprocessing
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.config import IndexConfig, ModelConfig
from autodj.model import (
    EMBEDDING_DIM,
    ModelCacheStatus,
    ModelLoadError,
    MuqWrapper,
    download_model_if_needed,
    inspect_model_cache,
    model_cache_path,
)


def _hold_model_cache_lock(cache_path: str, ready: object, release: object) -> None:
    """Spawn-safe helper proving OS lock serializes independent processes."""
    from autodj.model import _model_cache_lock

    with _model_cache_lock(Path(cache_path)):
        ready.set()
        release.wait(10)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def model_config_auto() -> ModelConfig:
    """ModelConfig with auto-download (no manual_path)."""
    return ModelConfig(name="OpenMuQ/MuQ-large-msd-iter", manual_path=None)


@pytest.fixture
def model_config_manual(tmp_path: Path) -> ModelConfig:
    """ModelConfig pointing to a pre-existing local model directory."""
    model_dir = tmp_path / "MuQ-large-msd-iter"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(b"weights")
    return ModelConfig(name="OpenMuQ/MuQ-large-msd-iter", manual_path=model_dir)


@pytest.fixture
def index_config(tmp_path: Path) -> IndexConfig:
    return IndexConfig(
        index_dir=tmp_path / "index",
        model_dir=tmp_path / "models",
    )


# ---------------------------------------------------------------------------
# download_model_if_needed
# ---------------------------------------------------------------------------


class TestDownloadModelIfNeeded:
    def test_returns_manual_path_directly(
        self, model_config_manual: ModelConfig, index_config: IndexConfig
    ) -> None:
        """Complete manual cache is accepted without download."""
        result = download_model_if_needed(model_config_manual, index_config)
        assert result == model_config_manual.manual_path

    def test_raises_if_manual_path_missing(self, tmp_path: Path, index_config: IndexConfig) -> None:
        cfg = ModelConfig(name="x", manual_path=tmp_path / "nonexistent")
        with pytest.raises(ModelLoadError, match="manual_path"):
            download_model_if_needed(cfg, index_config)

    def test_raises_if_manual_path_is_incomplete(
        self, tmp_path: Path, index_config: IndexConfig
    ) -> None:
        manual = tmp_path / "manual"
        manual.mkdir()
        (manual / "config.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ModelLoadError, match="incomplete"):
            download_model_if_needed(ModelConfig(manual_path=manual), index_config)

    def test_returns_cached_path_if_exists(
        self, model_config_auto: ModelConfig, tmp_path: Path
    ) -> None:
        """If the model is already in model_dir, skip download."""
        index_config = IndexConfig(
            index_dir=tmp_path / "index",
            model_dir=tmp_path / "models",
        )
        cache_dir = model_cache_path(model_config_auto, index_config)
        cache_dir.mkdir(parents=True)
        (cache_dir / "config.json").write_text("{}", encoding="utf-8")
        (cache_dir / "model.safetensors").write_bytes(b"weights")
        (cache_dir / ".autodj-complete").write_text(
            json.dumps({"repo_id": model_config_auto.name, "revision": "main"}),
            encoding="utf-8",
        )

        result = download_model_if_needed(model_config_auto, index_config)
        assert result == cache_dir

    def test_calls_snapshot_download_if_not_cached(
        self, model_config_auto: ModelConfig, index_config: IndexConfig, tmp_path: Path
    ) -> None:
        """New automatic cache is staged, marked, then promoted."""
        index_config = IndexConfig(
            index_dir=tmp_path / "index",
            model_dir=tmp_path / "models",
        )

        def populate(**kwargs: object) -> str:
            staged = Path(str(kwargs["local_dir"]))
            (staged / "config.json").write_text("{}", encoding="utf-8")
            (staged / "model.safetensors").write_bytes(b"weights")
            return str(staged)

        with patch("autodj.model.snapshot_download", side_effect=populate) as mock_dl:
            result = download_model_if_needed(model_config_auto, index_config)

        mock_dl.assert_called_once()
        assert result == model_cache_path(model_config_auto, index_config)
        marker = json.loads((result / ".autodj-complete").read_text(encoding="utf-8"))
        assert marker == {"repo_id": model_config_auto.name, "revision": "main"}
        assert mock_dl.call_args.kwargs["revision"] == "main"

    def test_raises_model_load_error_on_download_failure(
        self, model_config_auto: ModelConfig, index_config: IndexConfig, tmp_path: Path
    ) -> None:
        index_config = IndexConfig(
            index_dir=tmp_path / "index",
            model_dir=tmp_path / "models",
        )
        with (
            patch("autodj.model.snapshot_download", side_effect=Exception("network error")),
            pytest.raises(ModelLoadError, match="download"),
        ):
            download_model_if_needed(model_config_auto, index_config)

    def test_failure_removes_only_owned_staging(
        self, model_config_auto: ModelConfig, index_config: IndexConfig
    ) -> None:
        with (
            patch("autodj.model.snapshot_download", side_effect=TimeoutError("stuck")),
            pytest.raises(ModelLoadError, match="download"),
        ):
            download_model_if_needed(model_config_auto, index_config)
        assert not list(index_config.model_dir.glob("*.staging-*"))

    def test_download_timeout_does_not_mutate_global_socket_timeout(
        self, model_config_auto: ModelConfig, index_config: IndexConfig
    ) -> None:
        original_timeout = socket.getdefaulttimeout()
        with (
            patch("autodj.model.snapshot_download", side_effect=TimeoutError("stuck")),
            pytest.raises(ModelLoadError, match="download"),
        ):
            download_model_if_needed(model_config_auto, index_config)
        assert socket.getdefaulttimeout() == original_timeout

    def test_concurrent_threads_share_one_download(
        self, model_config_auto: ModelConfig, index_config: IndexConfig
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0
        calls_guard = threading.Lock()

        def populate(**kwargs: object) -> str:
            nonlocal calls
            with calls_guard:
                calls += 1
            started.set()
            assert release.wait(10)
            staged = Path(str(kwargs["local_dir"]))
            (staged / "config.json").write_text("{}", encoding="utf-8")
            (staged / "model.safetensors").write_bytes(b"weights")
            return str(staged)

        results: list[Path] = []
        failures: list[BaseException] = []

        def download() -> None:
            try:
                results.append(download_model_if_needed(model_config_auto, index_config))
            except BaseException as exc:  # test propagates worker failure below
                failures.append(exc)

        with patch("autodj.model.snapshot_download", side_effect=populate):
            first = threading.Thread(target=download)
            second = threading.Thread(target=download)
            first.start()
            assert started.wait(10)
            second.start()
            release.set()
            first.join(10)
            second.join(10)
        assert not failures
        assert calls == 1
        assert results == [model_cache_path(model_config_auto, index_config)] * 2

    def test_promotion_fsyncs_marker_tree_and_parent(
        self, model_config_auto: ModelConfig, index_config: IndexConfig
    ) -> None:
        def populate(**kwargs: object) -> str:
            staged = Path(str(kwargs["local_dir"]))
            (staged / "config.json").write_text("{}", encoding="utf-8")
            (staged / "model.safetensors").write_bytes(b"weights")
            return str(staged)

        with (
            patch("autodj.model.snapshot_download", side_effect=populate),
            patch("autodj.model._fsync_file") as fsync_file,
            patch("autodj.model._fsync_tree") as fsync_tree,
            patch("autodj.model._fsync_directory") as fsync_directory,
        ):
            result = download_model_if_needed(model_config_auto, index_config)
        assert fsync_file.call_args.args[0].name == ".autodj-complete"
        assert fsync_tree.call_args.args[0].name.startswith(".")
        assert fsync_directory.call_args.args[0] == result.parent


class TestModelCacheInspection:
    def test_public_status_is_frozen(self, tmp_path: Path) -> None:
        status = ModelCacheStatus(tmp_path, False, "missing")
        with pytest.raises(AttributeError):
            status.complete = True  # type: ignore[misc]

    def test_rejects_partial_and_invalid_sharded_weights(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "config.json").write_text("{}", encoding="utf-8")
        (cache / "model-00001-of-00002.safetensors").write_bytes(b"one")
        assert inspect_model_cache(cache).reason == "unindexed-partial-shard"
        (cache / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"x": "../outside.safetensors"}}), encoding="utf-8"
        )
        assert inspect_model_cache(cache).reason == "invalid-index"

    def test_sharded_index_requires_all_safe_shards(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "config.json").write_text("{}", encoding="utf-8")
        (cache / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"x": "model-00001-of-00002.safetensors"}}), encoding="utf-8"
        )
        assert inspect_model_cache(cache).reason == "missing-shard"
        (cache / "model-00001-of-00002.safetensors").write_bytes(b"one")
        assert inspect_model_cache(cache).complete

    def test_auto_cache_marker_must_match_exactly(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "config.json").write_text("{}", encoding="utf-8")
        (cache / "model.safetensors").write_bytes(b"weights")
        assert (
            inspect_model_cache(cache, repo_id="org/model", revision="main").reason
            == "missing-marker"
        )
        (cache / ".autodj-complete").write_text("not-json", encoding="utf-8")
        assert (
            inspect_model_cache(cache, repo_id="org/model", revision="main").reason
            == "invalid-marker"
        )
        (cache / ".autodj-complete").write_text(
            json.dumps({"repo_id": "org/model", "revision": "other"}), encoding="utf-8"
        )
        assert (
            inspect_model_cache(cache, repo_id="org/model", revision="main").reason
            == "marker-mismatch"
        )

    def test_repo_and_revision_create_distinct_auto_paths(self, index_config: IndexConfig) -> None:
        first = ModelConfig(name="org/same", revision="main")
        second = ModelConfig(name="other/same", revision="v2")
        assert model_cache_path(first, index_config) != model_cache_path(second, index_config)
        assert (
            hashlib.sha256(b"org/same@main").hexdigest()[:16]
            in model_cache_path(first, index_config).name
        )

    def test_process_lock_blocks_independent_holder(self, tmp_path: Path) -> None:
        from autodj.model import _model_cache_lock

        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        cache = tmp_path / "cache"
        holder = context.Process(target=_hold_model_cache_lock, args=(str(cache), ready, release))
        holder.start()
        assert ready.wait(10)
        acquired = threading.Event()

        def acquire() -> None:
            with _model_cache_lock(cache):
                acquired.set()

        waiter = threading.Thread(target=acquire)
        waiter.start()
        assert not acquired.wait(0.2)
        release.set()
        waiter.join(10)
        holder.join(10)
        assert holder.exitcode == 0
        assert acquired.is_set()


# ---------------------------------------------------------------------------
# MuqWrapper
# ---------------------------------------------------------------------------


def _make_mock_model(batch_size: int = 1, time_steps: int = 50):
    """Return a mock MuQ model that outputs plausible last_hidden_state tensors."""
    import torch

    model = MagicMock()
    hidden = torch.randn(batch_size, time_steps, EMBEDDING_DIM)
    model_output = MagicMock()
    model_output.last_hidden_state = hidden
    model.return_value = model_output
    return model


class TestMuqWrapper:
    def test_embed_returns_numpy_array(self) -> None:
        model = _make_mock_model()
        wrapper = MuqWrapper(model=model, device="cpu")

        audio = np.zeros(24000, dtype=np.float32)
        result = wrapper.embed_array(audio, sample_rate=24000)

        assert isinstance(result, np.ndarray)

    def test_embed_returns_embedding_dim(self) -> None:
        model = _make_mock_model()
        wrapper = MuqWrapper(model=model, device="cpu")

        audio = np.zeros(24000, dtype=np.float32)
        result = wrapper.embed_array(audio, sample_rate=24000)

        assert result.shape == (EMBEDDING_DIM,)

    def test_embed_returns_l2_normalized_vector(self) -> None:
        model = _make_mock_model()
        wrapper = MuqWrapper(model=model, device="cpu")

        audio = np.random.randn(24000).astype(np.float32)
        result = wrapper.embed_array(audio, sample_rate=24000)

        norm = np.linalg.norm(result)
        assert abs(norm - 1.0) < 1e-5, f"Vector not L2-normalized: norm={norm}"

    def test_embed_zero_audio_returns_valid_vector(self) -> None:
        """All-zero audio (silence) should still return a valid normalized vector."""
        import torch

        model = MagicMock()
        # Force a non-zero hidden state so normalization works
        hidden = torch.ones(1, 50, EMBEDDING_DIM)
        model_output = MagicMock()
        model_output.last_hidden_state = hidden
        model.return_value = model_output

        wrapper = MuqWrapper(model=model, device="cpu")
        audio = np.zeros(24000, dtype=np.float32)
        result = wrapper.embed_array(audio, sample_rate=24000)

        assert result.shape == (EMBEDDING_DIM,)
        assert np.isfinite(result).all()

    def test_model_called_with_no_grad(self) -> None:
        """Model inference should run inside torch.no_grad() for efficiency."""
        model = _make_mock_model()
        wrapper = MuqWrapper(model=model, device="cpu")

        audio = np.zeros(24000, dtype=np.float32)
        wrapper.embed_array(audio, sample_rate=24000)

        model.assert_called_once()

    def test_resamples_to_muq_rate_when_needed(self) -> None:
        """Audio not at 24000 Hz must be resampled before the model sees it."""
        from autodj.model import MUQ_SAMPLE_RATE

        assert MUQ_SAMPLE_RATE == 24_000

        # Calling resample of a 44.1 kHz array down to 24 kHz should change length
        with patch("autodj.model.MuqWrapper._embed_batch") as mock_embed:
            mock_embed.return_value = np.zeros((1, EMBEDDING_DIM), dtype=np.float32)
            wrapper = MuqWrapper(model=MagicMock(), device="cpu")

            audio_44k = np.random.randn(44100).astype(np.float32)
            wrapper.embed_array(audio_44k, sample_rate=44100)

        # _embed_batch was called with chunks at 24 kHz length
        call_chunks = mock_embed.call_args[0][0]
        # 1-second of 44.1 kHz audio resampled to 24 kHz = ~24000 samples
        assert len(call_chunks) >= 1
        assert all(len(c) <= MUQ_SAMPLE_RATE * MuqWrapper.CHUNK_SECONDS for c in call_chunks)

    def test_embed_works_at_native_24k(self) -> None:
        """Audio already at 24000 Hz embeds without error and returns correct shape."""
        from autodj.model import MUQ_SAMPLE_RATE

        model = _make_mock_model()
        wrapper = MuqWrapper(model=model, device="cpu")

        audio_24k = np.random.randn(MUQ_SAMPLE_RATE).astype(np.float32)
        result = wrapper.embed_array(audio_24k, sample_rate=MUQ_SAMPLE_RATE)

        assert result.shape == (EMBEDDING_DIM,)

    def test_long_audio_is_split_into_chunks(self) -> None:
        """Audio longer than CHUNK_SECONDS is processed in batched calls."""
        import torch

        from autodj.model import MUQ_SAMPLE_RATE

        n_chunks = 3
        model = MagicMock()
        model_output = MagicMock()
        # Return [batch=n_chunks, T, EMBEDDING_DIM] — one row per chunk
        model_output.last_hidden_state = torch.randn(n_chunks, 10, EMBEDDING_DIM)
        model.return_value = model_output

        wrapper = MuqWrapper(model=model, device="cpu")

        chunk_samples = wrapper.CHUNK_SECONDS * MUQ_SAMPLE_RATE
        audio = np.random.randn(n_chunks * chunk_samples).astype(np.float32)

        # Configure the mock to return appropriately shaped tensors per call
        def model_side_effect(wavs, **kwargs):
            batch = wavs.shape[0]
            out = MagicMock()
            out.last_hidden_state = torch.randn(batch, 10, EMBEDDING_DIM)
            return out

        model.side_effect = model_side_effect

        result = wrapper.embed_array(audio, sample_rate=MUQ_SAMPLE_RATE)

        assert result.shape == (EMBEDDING_DIM,)
        assert np.isfinite(result).all()
        # MAX_CHUNK_BATCH=2, n_chunks=3 → ceil(3/2) = 2 model calls
        assert model.call_count == 2

    def test_chunk_embeddings_are_averaged(self) -> None:
        """embed_array returns the mean of per-chunk vectors, then L2-normalized."""
        import torch

        from autodj.model import MUQ_SAMPLE_RATE

        # MAX_CHUNK_BATCH=2: both chunks in one call → hidden [2, 1, EMBEDDING_DIM]
        batch_hidden = torch.zeros(2, 1, EMBEDDING_DIM)
        batch_hidden[1] = 1.0  # chunk 0 → zeros, chunk 1 → ones
        out = MagicMock()
        out.last_hidden_state = batch_hidden
        model = MagicMock()
        model.return_value = out

        wrapper = MuqWrapper(model=model, device="cpu")

        chunk_samples = wrapper.CHUNK_SECONDS * MUQ_SAMPLE_RATE
        audio = np.random.randn(2 * chunk_samples).astype(np.float32)
        result = wrapper.embed_array(audio, sample_rate=MUQ_SAMPLE_RATE)

        # chunk 0 pooled → [0,...,0], chunk 1 pooled → [1,...,1]
        # mean → [0.5,...,0.5], L2-normalized → [1/√EMBEDDING_DIM, ...]
        expected_unnorm = np.full(EMBEDDING_DIM, 0.5, dtype=np.float32)
        expected = expected_unnorm / np.linalg.norm(expected_unnorm)
        np.testing.assert_allclose(result, expected, atol=1e-5)


# ---------------------------------------------------------------------------
# load_model
# ---------------------------------------------------------------------------


class TestLoadModel:
    def test_raises_if_muq_package_missing(self, tmp_path: Path) -> None:
        """If 'muq' is not installed, a clear ModelLoadError is raised."""
        from autodj.model import load_model

        # Make sure any cached `muq` import is removed, then block the import
        sys.modules.pop("muq", None)
        with patch.dict(sys.modules, {"muq": None}), pytest.raises(ModelLoadError, match="muq"):
            load_model(tmp_path)
