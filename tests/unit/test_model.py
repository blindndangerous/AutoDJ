"""Unit tests for autodj.model.

All external dependencies (muq, torch, huggingface_hub) are mocked so
tests run fast without downloading anything.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.config import IndexConfig, ModelConfig
from autodj.model import (
    EMBEDDING_DIM,
    ModelLoadError,
    MuqWrapper,
    download_model_if_needed,
    inspect_model_cache,
)

REPO = "OpenMuQ/MuQ-large-msd-iter"


@pytest.fixture
def index_config(tmp_path: Path) -> IndexConfig:
    return IndexConfig(index_dir=tmp_path / "index", model_dir=tmp_path / "models")


# ---------------------------------------------------------------------------
# download_model_if_needed
# ---------------------------------------------------------------------------


def test_manual_path_is_used_without_download(tmp_path: Path, index_config: IndexConfig) -> None:
    manual = tmp_path / "manual"
    manual.mkdir()
    with patch("autodj.model.snapshot_download") as download:
        assert download_model_if_needed(ModelConfig(manual_path=manual), index_config) == manual
    download.assert_not_called()


def test_missing_manual_path_fails(tmp_path: Path, index_config: IndexConfig) -> None:
    cfg = ModelConfig(manual_path=tmp_path / "nonexistent")
    with pytest.raises(ModelLoadError, match="manual_path"):
        download_model_if_needed(cfg, index_config)


def test_download_caches_under_model_dir_and_returns_snapshot(
    tmp_path: Path, index_config: IndexConfig
) -> None:
    snapshot = tmp_path / "snapshot"
    with patch("autodj.model.snapshot_download", return_value=str(snapshot)) as download:
        result = download_model_if_needed(
            ModelConfig(name=REPO, revision="v1"), index_config, hf_token="hf_x"
        )

    assert result == snapshot
    kwargs = download.call_args.kwargs
    assert (kwargs["repo_id"], kwargs["revision"]) == (REPO, "v1")
    assert kwargs["cache_dir"] == index_config.model_dir
    assert kwargs["token"] == "hf_x"
    # Parallel download threads raced huggingface_hub's symlink test on
    # Windows without Developer Mode and failed with WinError 1314.
    assert kwargs["max_workers"] == 1


def test_download_failure_raises_model_load_error(index_config: IndexConfig) -> None:
    with (
        patch("autodj.model.snapshot_download", side_effect=OSError("offline")),
        pytest.raises(ModelLoadError, match="offline"),
    ):
        download_model_if_needed(ModelConfig(name=REPO), index_config)


# ---------------------------------------------------------------------------
# inspect_model_cache
# ---------------------------------------------------------------------------


def test_inspection_finds_downloaded_snapshot(index_config: IndexConfig) -> None:
    commit = "a" * 40
    repo = index_config.model_dir / "models--OpenMuQ--MuQ-large-msd-iter"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text(commit, encoding="utf-8")
    snapshot = repo / "snapshots" / commit
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")

    status = inspect_model_cache(ModelConfig(name=REPO), index_config)

    assert status.complete
    assert status.path == snapshot


def test_inspection_reports_missing_model_without_creating_folders(
    index_config: IndexConfig,
) -> None:
    status = inspect_model_cache(ModelConfig(name=REPO), index_config)

    assert not status.complete
    assert status.reason == "not downloaded or incomplete"
    assert not index_config.model_dir.exists()


def test_inspection_of_manual_path_checks_it_exists(
    tmp_path: Path, index_config: IndexConfig
) -> None:
    missing = inspect_model_cache(ModelConfig(manual_path=tmp_path / "gone"), index_config)
    present = inspect_model_cache(ModelConfig(manual_path=tmp_path), index_config)

    assert (missing.complete, present.complete) == (False, True)


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
    def test_prepares_weight_norm_before_loading_checkpoint(self, tmp_path: Path) -> None:
        """The compatibility loader must be installed before checkpoint loading."""
        from autodj.model import load_model

        events = []
        model = MagicMock()
        model.to.return_value = model
        muq = MagicMock()

        def load_checkpoint(path):
            assert path == str(tmp_path)
            events.append("load")
            return model

        muq.MuQ.from_pretrained.side_effect = load_checkpoint
        with (
            patch.dict(sys.modules, {"muq": muq}),
            patch("autodj.compute.device_string", return_value="cpu"),
            patch(
                "autodj.model._prepare_muq_weight_norm",
                side_effect=lambda: events.append("prepare"),
            ),
            patch("autodj.model._prepare_muq_conformer"),
        ):
            wrapper = load_model(tmp_path)

        assert events == ["prepare", "load"]
        assert wrapper.model is model
        model.eval.assert_called_once()

    def test_raises_if_muq_package_missing(self, tmp_path: Path) -> None:
        """If 'muq' is not installed, a clear ModelLoadError is raised."""
        from autodj.model import load_model

        # Make sure any cached `muq` import is removed, then block the import
        sys.modules.pop("muq", None)
        with patch.dict(sys.modules, {"muq": None}), pytest.raises(ModelLoadError, match="muq"):
            load_model(tmp_path)
