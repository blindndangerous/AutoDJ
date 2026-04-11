"""MERT audio embedding model loader with automatic download.

Loads the MERT-v1-330M (or configured variant) music understanding model
from HuggingFace and provides a simple interface for embedding audio arrays
into 768-dimensional L2-normalized vectors.

The model is downloaded once and cached in the configured ``model_dir``.
If the download fails, clear instructions for manual download are printed.

Example:
    >>> from autodj.config import load_config
    >>> from autodj.model import download_model_if_needed, load_model
    >>> cfg = load_config()
    >>> model_path = download_model_if_needed(cfg.model, cfg.index)
    >>> wrapper = load_model(model_path)
    >>> import numpy as np
    >>> audio = np.zeros(22050, dtype=np.float32)
    >>> vec = wrapper.embed_array(audio, sample_rate=22050)
    >>> vec.shape
    (768,)
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import torch
from huggingface_hub import snapshot_download
from transformers import AutoModel, AutoProcessor

from autodj.config import IndexConfig, ModelConfig

logger = logging.getLogger(__name__)

# Expected embedding dimension for MERT models
EMBEDDING_DIM = 768

# Sampling rate expected by MERT (24 kHz)
MERT_SAMPLE_RATE = 24_000


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ModelLoadError(RuntimeError):
    """Raised when the MERT model cannot be loaded or downloaded."""


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_model_if_needed(
    model_cfg: ModelConfig,
    index_cfg: IndexConfig,
    hf_token: str | None = None,
) -> Path:
    """Ensure the MERT model checkpoint is available locally.

    If ``model_cfg.manual_path`` is set, that path is used directly (no
    download).  Otherwise the model is fetched from HuggingFace Hub using
    :func:`huggingface_hub.snapshot_download` and cached in
    ``index_cfg.model_dir / <model_name>``.

    Args:
        model_cfg: Model configuration (name, optional manual path).
        index_cfg: Index configuration providing the model cache directory.
        hf_token: Optional HuggingFace API token.  Enables authenticated
            requests with higher rate limits and faster downloads.  Set via
            ``[huggingface] token`` in ``config.toml``.

    Returns:
        The local :class:`~pathlib.Path` to the model directory.

    Raises:
        ModelLoadError: If ``manual_path`` does not exist, or if the
            HuggingFace download fails.

    Example:
        >>> path = download_model_if_needed(cfg.model, cfg.index, hf_token="hf_...")
        >>> print(path)
        models/MERT-v1-330M
    """
    # --- manual path ---
    if model_cfg.manual_path is not None:
        if not model_cfg.manual_path.exists():
            raise ModelLoadError(
                f"manual_path does not exist: {model_cfg.manual_path}\n"
                "Check [model] manual_path in config.toml."
            )
        logger.info("Using manually specified model path: %s", model_cfg.manual_path)
        return model_cfg.manual_path

    # --- auto-download cache ---
    model_name = model_cfg.name
    # Derive a safe directory name from the HuggingFace model ID (strip the "org/" prefix)
    cache_name = model_name.split("/")[-1]
    cache_dir = index_cfg.model_dir / cache_name

    if cache_dir.exists() and any(cache_dir.iterdir()):
        logger.info("Model already cached at %s", cache_dir)
        return cache_dir

    # --- download ---
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading model %s to %s ...", model_name, cache_dir)
    print(
        f"\n[AutoDJ] Downloading model '{model_name}' (~1.3 GB) to {cache_dir}\n"
        "This is a one-time download. Please wait...\n"
    )
    try:
        snapshot_download(
            repo_id=model_name,
            local_dir=str(cache_dir),
            ignore_patterns=["*.msgpack", "flax_model*", "tf_model*", "rust_model*"],
            token=hf_token,
        )
    except Exception as exc:
        raise ModelLoadError(
            f"Failed to download model '{model_name}': {exc}\n\n"
            "Manual download instructions:\n"
            f"  1. Visit https://huggingface.co/{model_name}\n"
            "  2. Click 'Files and versions' and download all files\n"
            f"  3. Place them in: {cache_dir}\n"
            "  4. Add to config.toml:\n"
            f"     [model]\n"
            f"     manual_path = \"{cache_dir}\"\n"
        ) from exc

    return cache_dir


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


class MertWrapper:
    """Thin wrapper around a loaded MERT model for audio embedding.

    Attributes:
        model: The loaded HuggingFace MERT model in eval mode.
        processor: The HuggingFace audio processor for the model.
        device: PyTorch device string (``"cpu"`` or ``"cuda"``).
    """

    def __init__(
        self,
        model: torch.nn.Module,
        processor: object,
        device: str,
    ) -> None:
        """Store the loaded model, processor, and target device.

        Args:
            model: A loaded HuggingFace MERT model in eval mode.
            processor: The HuggingFace ``AutoProcessor`` for the model.
            device: PyTorch device string, e.g. ``"cpu"`` or ``"cuda"``.
        """
        self.model = model
        self.processor = processor
        self.device = device

    def embed_array(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Embed a raw audio array into a 768-dimensional L2-normalized vector.

        The audio is resampled to MERT's expected 24 kHz if needed (librosa
        resampling is called by the processor). The model's last hidden states
        are mean-pooled across the time axis to produce a fixed-size vector,
        then L2-normalized.

        Args:
            audio: 1-D float32 numpy array of audio samples.
            sample_rate: Sample rate of *audio* in Hz.

        Returns:
            A float32 numpy array of shape ``(768,)``, L2-normalized.
        """
        inputs = self.processor(
            audio,
            sampling_rate=sample_rate,
            return_tensors="pt",
        )
        # Move all input tensors to the target device
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)

        # outputs.last_hidden_state: [batch=1, time_frames, hidden=768]
        hidden: torch.Tensor = outputs.last_hidden_state
        # Mean pool across the time dimension → [1, 768]
        pooled = hidden.mean(dim=1).squeeze(0)  # → [768]
        vec = pooled.cpu().float().numpy()

        # L2 normalize
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_model(model_path: Path) -> MertWrapper:
    """Load the MERT model from a local directory and return a :class:`MertWrapper`.

    Automatically selects CUDA if available, falls back to CPU otherwise.
    The model is set to eval mode and gradient computation is disabled.

    Args:
        model_path: Path to the local HuggingFace model directory containing
            ``config.json`` and model weights.

    Returns:
        A :class:`MertWrapper` ready for embedding.

    Raises:
        ModelLoadError: If the model files are missing or corrupt.

    Example:
        >>> wrapper = load_model(Path("models/MERT-v1-330M"))
        >>> vec = wrapper.embed_array(audio_array, sample_rate=22050)
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Loading MERT model from %s on device=%s", model_path, device)

    try:
        processor = AutoProcessor.from_pretrained(str(model_path), trust_remote_code=True)
        model = AutoModel.from_pretrained(str(model_path), trust_remote_code=True)
    except Exception as exc:
        raise ModelLoadError(
            f"Failed to load model from {model_path}: {exc}\n"
            "The model files may be incomplete. Try deleting the directory and re-running."
        ) from exc

    model = model.to(device)
    model.eval()

    return MertWrapper(model=model, processor=processor, device=device)
