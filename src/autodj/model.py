"""MuQ audio embedding model loader with automatic download.

Loads the MuQ-large-msd-iter (or configured variant) music understanding
model from HuggingFace and provides a simple interface for embedding audio
arrays into 1024-dimensional L2-normalized vectors.

The model is downloaded once into the HuggingFace cache layout under the
configured ``model_dir``.

MuQ requires fp32 inference (fp16 may produce NaN values per the model
authors). Audio must be resampled to 24 kHz.

Example:
    >>> from autodj.config import load_config
    >>> from autodj.model import download_model_if_needed, load_model
    >>> cfg = load_config()
    >>> model_path = download_model_if_needed(cfg.model, cfg.index)
    >>> wrapper = load_model(model_path)
    >>> import numpy as np
    >>> audio = np.zeros(24000, dtype=np.float32)
    >>> vec = wrapper.embed_array(audio, sample_rate=24000)
    >>> vec.shape
    (1024,)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
import torch
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

from autodj.config import IndexConfig, ModelConfig

if TYPE_CHECKING:
    from transformers.modeling_outputs import BaseModelOutput

logger = logging.getLogger(__name__)

# Expected embedding dimension for MuQ-large-msd-iter (encoder_dim from config.json)
EMBEDDING_DIM = 1024

# Sampling rate expected by MuQ (24 kHz, hard requirement)
MUQ_SAMPLE_RATE = 24_000

# Weight formats MuQ never loads; skipping them saves a second copy of the weights.
_IGNORE_PATTERNS = [
    "*.msgpack",
    "flax_model*",
    "tf_model*",
    "rust_model*",
    "pytorch_model.bin",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ModelLoadError(RuntimeError):
    """Raised when the MuQ model cannot be loaded or downloaded."""


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCacheStatus:
    """Whether the configured model is on disk, checked without the network."""

    path: Path
    complete: bool
    reason: str


def inspect_model_cache(model_cfg: ModelConfig, index_cfg: IndexConfig) -> ModelCacheStatus:
    """Report whether the configured model is available locally, without downloading.

    Args:
        model_cfg: Model configuration (name, revision, optional manual path).
        index_cfg: Index configuration providing the model cache directory.

    Returns:
        The model folder and ``complete=True``, or the expected location and
        why it cannot be used yet.
    """
    if model_cfg.manual_path is not None:
        found = model_cfg.manual_path.is_dir()
        return ModelCacheStatus(
            model_cfg.manual_path, found, "complete" if found else "manual_path missing"
        )
    try:
        path = snapshot_download(  # nosec B615 -- repository comes from user configuration
            repo_id=model_cfg.name,
            revision=model_cfg.revision,
            cache_dir=index_cfg.model_dir,
            ignore_patterns=_IGNORE_PATTERNS,
            local_files_only=True,
        )
    except LocalEntryNotFoundError:
        return ModelCacheStatus(index_cfg.model_dir, False, "not downloaded or incomplete")
    return ModelCacheStatus(Path(path), True, "complete")


def download_model_if_needed(
    model_cfg: ModelConfig,
    index_cfg: IndexConfig,
    hf_token: str | None = None,
) -> Path:
    """Ensure the MuQ model checkpoint is available locally.

    If ``model_cfg.manual_path`` is set, that folder is used as-is.  Otherwise
    :func:`huggingface_hub.snapshot_download` fetches the configured revision
    into the HuggingFace cache layout under ``index_cfg.model_dir``, one file
    at a time so it also works where symlinks need a privilege; it only
    downloads missing files and falls back to the cached copy when the Hub
    cannot be reached.

    Args:
        model_cfg: Model configuration (name, revision, optional manual path).
        index_cfg: Index configuration providing the model cache directory.
        hf_token: Optional HuggingFace API token.  Enables authenticated
            requests with higher rate limits and faster downloads.  Set via
            ``[huggingface] token`` in ``config.toml``.

    Returns:
        The local :class:`~pathlib.Path` to the model directory.

    Raises:
        ModelLoadError: If ``manual_path`` does not exist, or if the
            HuggingFace download fails.
    """
    if model_cfg.manual_path is not None:
        if not model_cfg.manual_path.is_dir():
            raise ModelLoadError(f"manual_path does not exist: {model_cfg.manual_path}")
        return model_cfg.manual_path
    try:
        path = snapshot_download(  # nosec B615 -- repository comes from user configuration
            repo_id=model_cfg.name,
            revision=model_cfg.revision,
            cache_dir=index_cfg.model_dir,
            token=hf_token,
            ignore_patterns=_IGNORE_PATTERNS,
            # One file at a time.  Where symlinks need a privilege (Windows
            # without Developer Mode) huggingface_hub copies files instead,
            # but it tests for symlinks on the first file and, with several
            # download threads, the others try a symlink before the test
            # has finished and fail with WinError 1314.  The weights are one
            # large file, so parallel downloads saved nothing.
            max_workers=1,
        )
    except Exception as exc:
        raise ModelLoadError(f"Failed to download model '{model_cfg.name}': {exc}") from exc
    return Path(path)


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


class MuqWrapper:
    """Thin wrapper around a loaded MuQ model for audio embedding.

    MuQ takes raw audio tensors at 24 kHz directly (no separate processor).
    Long tracks are split into ``CHUNK_SECONDS``-second chunks, embeddings
    are mean-pooled across time and across chunks, then L2-normalized.

    Attributes:
        model: The loaded MuQ model in eval mode.
        device: PyTorch device string (``"cpu"`` or ``"cuda"``).
    """

    # Maximum chunk length fed to MuQ in one forward pass (seconds).
    # Longer songs are split into chunks and their embeddings averaged.
    # 30 s × 24000 Hz = 720 000 samples → safe on an 8 GB GPU at fp32.
    CHUNK_SECONDS: int = 30

    # Maximum number of chunks per batched forward pass.
    # 1 = sequential (safest on any GPU); higher values speed up indexing
    # but use more VRAM. fp32 is required so we batch conservatively.
    MAX_CHUNK_BATCH: int = 2

    # MuQ's mel front-end requires at least this many samples; pad below.
    _MIN_CHUNK_SAMPLES: int = MUQ_SAMPLE_RATE  # 1 second

    def __init__(self, model: torch.nn.Module, device: str) -> None:
        """Store the loaded model and target device.

        Args:
            model: A loaded MuQ model in eval mode.
            device: PyTorch device string, e.g. ``"cpu"`` or ``"cuda"``.
        """
        self.model = model
        self.device = device

    def _embed_batch(self, chunks: list[np.ndarray]) -> np.ndarray:
        """Embed a batch of same-track chunks in one forward pass.

        Args:
            chunks: List of 1-D float32 arrays at MUQ_SAMPLE_RATE, each at
                most CHUNK_SECONDS long. All chunks from one track are batched
                together so the GPU processes them in parallel.

        Returns:
            float32 array of shape ``(len(chunks), EMBEDDING_DIM)``,
            NOT yet L2-normalized.
        """
        # Zero-pad any chunk shorter than the minimum input length.
        chunks = [
            np.pad(c, (0, self._MIN_CHUNK_SAMPLES - len(c)))
            if len(c) < self._MIN_CHUNK_SAMPLES
            else c
            for c in chunks
        ]
        # Right-pad shorter chunks in the batch up to the longest length so
        # they can be stacked into a single tensor.
        max_len = max(len(c) for c in chunks)
        padded = np.stack(
            [np.pad(c, (0, max_len - len(c))) if len(c) < max_len else c for c in chunks]
        ).astype(np.float32)

        wavs = torch.from_numpy(padded).to(self.device)

        # MuQ requires fp32 — no autocast.
        with torch.no_grad():
            outputs = self.model(wavs, output_hidden_states=False)

        hidden: torch.Tensor = outputs.last_hidden_state  # [B, T, EMBEDDING_DIM]
        pooled = hidden.mean(dim=1)  # [B, EMBEDDING_DIM]
        return pooled.cpu().float().numpy()

    def embed_array(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Embed a raw audio array into a 1024-dimensional L2-normalized vector.

        Long tracks are split into ``CHUNK_SECONDS``-second chunks. Up to
        ``MAX_CHUNK_BATCH`` chunks are batched into a single GPU forward pass,
        then their embeddings are averaged and L2-normalized. This keeps peak
        memory bounded while maximising GPU utilisation.

        The audio is resampled to MuQ's required 24 kHz using librosa if the
        provided sample rate differs.

        Args:
            audio: 1-D float32 numpy array of audio samples (mono).
            sample_rate: Sample rate of *audio* in Hz (44100, 48000, 96000, etc.).

        Returns:
            A float32 numpy array of shape ``(EMBEDDING_DIM,)``, L2-normalized.
        """
        if sample_rate != MUQ_SAMPLE_RATE:
            import librosa as _librosa

            audio = _librosa.resample(audio, orig_sr=sample_rate, target_sr=MUQ_SAMPLE_RATE)

        chunk_len = self.CHUNK_SECONDS * MUQ_SAMPLE_RATE
        chunks = [
            audio[start : start + chunk_len]
            for start in range(0, len(audio), chunk_len)
            if len(audio[start : start + chunk_len]) > 0
        ]

        # Process in mini-batches; collect per-chunk vectors
        all_vecs: list[np.ndarray] = []
        for i in range(0, len(chunks), self.MAX_CHUNK_BATCH):
            batch_vecs = self._embed_batch(chunks[i : i + self.MAX_CHUNK_BATCH])
            all_vecs.append(batch_vecs)  # each is (B, EMBEDDING_DIM)

        if self.device == "cuda":  # pragma: no cover — GPU-only
            torch.cuda.empty_cache()

        vec = np.vstack(all_vecs).mean(axis=0).astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class _MuqConformerAdapter(torch.nn.Module):
    """Adapt MuQ's encoder for AutoDJ's final-layer-only inference.

    MuQ 0.1.0 supplies an EasyDict instead of a Transformers configuration and
    reads the final embedding through ``hidden_states[-1]``. Transformers 5
    expects configuration methods and returns only ``last_hidden_state`` from
    the bare encoder. Keep its implementation and loaded weights, exposing the
    final layer in the form MuQ needs. This does not provide intermediate layers.
    """

    def __init__(self, encoder: torch.nn.Module) -> None:
        """Normalize the shared configuration without replacing parameters."""
        super().__init__()
        from transformers import Wav2Vec2ConformerConfig

        previous = cast("dict[str, object] | Wav2Vec2ConformerConfig", encoder.config)
        values = dict(previous) if isinstance(previous, dict) else previous.to_dict()
        config = Wav2Vec2ConformerConfig.from_dict(values)
        # Match MuQ's original eager attention, including relative position bias.
        config._attn_implementation = "eager"
        for module in encoder.modules():
            if getattr(module, "config", None) is previous:
                # A third-party non-module attribute.  Whether mypy flags it depends
                # on the torch build installed, so an unneeded ignore is allowed too.
                module.config = config  # type: ignore[assignment, unused-ignore]
        self.encoder = encoder

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: torch.Tensor | None = None,
        output_hidden_states: bool = True,
    ) -> BaseModelOutput:
        """Return the final encoder layer through MuQ's expected output fields."""
        from transformers.modeling_outputs import BaseModelOutput

        result = self.encoder(hidden_states, attention_mask=attention_mask)
        last = result.last_hidden_state
        return BaseModelOutput(
            last_hidden_state=last,
            hidden_states=(last,) if output_hidden_states else None,
        )


def _prepare_muq_conformer(model: torch.nn.Module) -> None:
    """Adapt the standard MuQ Conformer; leave alternate encoders unchanged."""
    from transformers.models.wav2vec2_conformer.modeling_wav2vec2_conformer import (
        Wav2Vec2ConformerEncoder,
    )

    inner = cast(torch.nn.Module, model.model)
    encoder = inner.conformer
    if isinstance(encoder, Wav2Vec2ConformerEncoder):
        inner.conformer = _MuqConformerAdapter(encoder)


def load_model(model_path: Path) -> MuqWrapper:
    """Load the MuQ model from a local directory and return a :class:`MuqWrapper`.

    Automatically selects CUDA if available, falls back to CPU otherwise.
    The model is set to eval mode; embedding runs under ``torch.no_grad``.

    Args:
        model_path: Path to the local HuggingFace MuQ model directory
            containing ``config.json`` and model weights.

    Returns:
        A :class:`MuqWrapper` ready for embedding.

    Raises:
        ModelLoadError: If the MuQ package is not installed or the model
            files are missing or corrupt.

    Example:
        >>> wrapper = load_model(Path("models/MuQ-large-msd-iter"))
        >>> vec = wrapper.embed_array(audio_array, sample_rate=44100)
    """
    try:
        from muq import MuQ
    except ImportError as exc:
        raise ModelLoadError(
            "The 'muq' package is not installed. Run 'uv sync' (or "
            "'pip install muq') and try again."
        ) from exc

    # Real model load only runs on a host with the MuQ checkpoint and
    # torch installed.  CI environments don't carry either, so the body
    # below is exercised only on the indexing host.
    from autodj.compute import device_string  # pragma: no cover

    device = device_string()  # pragma: no cover
    logger.info("Loading MuQ model from %s on device=%s", model_path, device)  # pragma: no cover

    try:  # pragma: no cover
        model = MuQ.from_pretrained(str(model_path))
        _prepare_muq_conformer(model)
    except Exception as exc:  # pragma: no cover
        raise ModelLoadError(
            f"Failed to load model from {model_path}: {exc}\n"
            "The model files may be incomplete. Try deleting the directory and re-running."
        ) from exc

    model = model.to(device)  # pragma: no cover
    model.eval()  # pragma: no cover

    return MuqWrapper(model=model, device=device)  # pragma: no cover
