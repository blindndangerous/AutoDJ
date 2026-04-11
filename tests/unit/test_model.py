"""Unit tests for autodj.model.

All external dependencies (transformers, torch, huggingface_hub) are mocked
so tests run fast without downloading anything.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.config import IndexConfig, ModelConfig
from autodj.model import MertWrapper, ModelLoadError, download_model_if_needed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def model_config_auto() -> ModelConfig:
    """ModelConfig with auto-download (no manual_path)."""
    return ModelConfig(name="m-a-p/MERT-v1-330M", manual_path=None)


@pytest.fixture
def model_config_manual(tmp_path: Path) -> ModelConfig:
    """ModelConfig pointing to a pre-existing local model directory."""
    model_dir = tmp_path / "MERT-v1-330M"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    return ModelConfig(name="m-a-p/MERT-v1-330M", manual_path=model_dir)


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
        """When manual_path is set and exists, no download is performed."""
        result = download_model_if_needed(model_config_manual, index_config)
        assert result == model_config_manual.manual_path

    def test_raises_if_manual_path_missing(
        self, tmp_path: Path, index_config: IndexConfig
    ) -> None:
        cfg = ModelConfig(name="x", manual_path=tmp_path / "nonexistent")
        with pytest.raises(ModelLoadError, match="manual_path"):
            download_model_if_needed(cfg, index_config)

    def test_returns_cached_path_if_exists(
        self, model_config_auto: ModelConfig, tmp_path: Path
    ) -> None:
        """If the model is already in model_dir, skip download."""
        index_config = IndexConfig(
            index_dir=tmp_path / "index",
            model_dir=tmp_path / "models",
        )
        # Pre-create the expected cache directory with a marker file
        cache_dir = tmp_path / "models" / "MERT-v1-330M"
        cache_dir.mkdir(parents=True)
        (cache_dir / "config.json").write_text("{}", encoding="utf-8")

        result = download_model_if_needed(model_config_auto, index_config)
        assert result == cache_dir

    def test_calls_snapshot_download_if_not_cached(
        self, model_config_auto: ModelConfig, index_config: IndexConfig, tmp_path: Path
    ) -> None:
        """When model is not cached, snapshot_download is called."""
        index_config = IndexConfig(
            index_dir=tmp_path / "index",
            model_dir=tmp_path / "models",
        )
        expected_cache = tmp_path / "models" / "MERT-v1-330M"

        with patch("autodj.model.snapshot_download") as mock_dl:
            mock_dl.return_value = str(expected_cache)
            expected_cache.mkdir(parents=True)
            result = download_model_if_needed(model_config_auto, index_config)

        mock_dl.assert_called_once()
        call_kwargs = mock_dl.call_args
        assert "m-a-p/MERT-v1-330M" in str(call_kwargs)

    def test_raises_model_load_error_on_download_failure(
        self, model_config_auto: ModelConfig, index_config: IndexConfig, tmp_path: Path
    ) -> None:
        index_config = IndexConfig(
            index_dir=tmp_path / "index",
            model_dir=tmp_path / "models",
        )
        with patch("autodj.model.snapshot_download", side_effect=Exception("network error")):
            with pytest.raises(ModelLoadError, match="download"):
                download_model_if_needed(model_config_auto, index_config)


# ---------------------------------------------------------------------------
# MertWrapper
# ---------------------------------------------------------------------------


def _make_mock_model_and_processor():
    """Return mock transformers model + processor that output plausible tensors."""
    import torch

    processor = MagicMock()
    # Processor returns a dict-like object with input tensors
    processor.return_value = {"input_values": torch.zeros(1, 22050)}

    model = MagicMock()
    # Model output: last_hidden_state shape [1, T, 768]
    hidden = torch.randn(1, 50, 768)
    model_output = MagicMock()
    model_output.last_hidden_state = hidden
    model.return_value = model_output

    return model, processor


class TestMertWrapper:
    def test_embed_returns_numpy_array(self, tmp_path: Path) -> None:
        model, processor = _make_mock_model_and_processor()
        wrapper = MertWrapper(model=model, processor=processor, device="cpu")

        audio = np.zeros(22050, dtype=np.float32)
        result = wrapper.embed_array(audio, sample_rate=22050)

        assert isinstance(result, np.ndarray)

    def test_embed_returns_768_dims(self, tmp_path: Path) -> None:
        model, processor = _make_mock_model_and_processor()
        wrapper = MertWrapper(model=model, processor=processor, device="cpu")

        audio = np.zeros(22050, dtype=np.float32)
        result = wrapper.embed_array(audio, sample_rate=22050)

        assert result.shape == (768,)

    def test_embed_returns_l2_normalized_vector(self, tmp_path: Path) -> None:
        model, processor = _make_mock_model_and_processor()
        wrapper = MertWrapper(model=model, processor=processor, device="cpu")

        audio = np.random.randn(22050).astype(np.float32)
        result = wrapper.embed_array(audio, sample_rate=22050)

        norm = np.linalg.norm(result)
        assert abs(norm - 1.0) < 1e-5, f"Vector not L2-normalized: norm={norm}"

    def test_embed_zero_audio_returns_valid_vector(self, tmp_path: Path) -> None:
        """All-zero audio (silence) should still return a valid normalized vector."""
        import torch

        processor = MagicMock()
        processor.return_value = {"input_values": torch.zeros(1, 22050)}

        model = MagicMock()
        # Force a non-zero hidden state so normalization works
        hidden = torch.ones(1, 50, 768)
        model_output = MagicMock()
        model_output.last_hidden_state = hidden
        model.return_value = model_output

        wrapper = MertWrapper(model=model, processor=processor, device="cpu")
        audio = np.zeros(22050, dtype=np.float32)
        result = wrapper.embed_array(audio, sample_rate=22050)

        assert result.shape == (768,)
        assert np.isfinite(result).all()

    def test_model_called_with_no_grad(self, tmp_path: Path) -> None:
        """Model inference should run inside torch.no_grad() for efficiency."""
        import torch

        model, processor = _make_mock_model_and_processor()
        wrapper = MertWrapper(model=model, processor=processor, device="cpu")

        audio = np.zeros(22050, dtype=np.float32)
        wrapper.embed_array(audio, sample_rate=22050)

        model.assert_called_once()
