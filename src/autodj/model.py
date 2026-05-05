"""MuQ audio embedding model loader with automatic download.

Loads the MuQ-large-msd-iter (or configured variant) music understanding
model from HuggingFace and provides a simple interface for embedding audio
arrays into 1024-dimensional L2-normalized vectors.

The model is downloaded once and cached in the configured ``model_dir``.
If the download fails, clear instructions for manual download are printed.

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
import threading
import time
from pathlib import Path

import numpy as np
import torch
from huggingface_hub import snapshot_download

from autodj.config import IndexConfig, ModelConfig

logger = logging.getLogger(__name__)

# Expected embedding dimension for MuQ-large-msd-iter (encoder_dim from config.json)
EMBEDDING_DIM = 1024

# Sampling rate expected by MuQ (24 kHz, hard requirement)
MUQ_SAMPLE_RATE = 24_000


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ModelLoadError(RuntimeError):
    """Raised when the MuQ model cannot be loaded or downloaded."""


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

# Defaults for retry behaviour — overridable via config
_DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes per attempt before declaring it stuck
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 5  # seconds between attempts


def _snapshot_download_with_timeout(
    repo_id: str,
    local_dir: str,
    ignore_patterns: list[str],
    token: str | None,
    timeout: int,
) -> None:
    """Run ``snapshot_download`` in a thread; raise ``TimeoutError`` if it hangs.

    Args:
        repo_id: HuggingFace model repository ID.
        local_dir: Local directory to download files into.
        ignore_patterns: File patterns to skip (e.g. TF/Flax weights).
        token: Optional HuggingFace API token.
        timeout: Maximum seconds to wait before declaring the download stuck.

    Raises:
        TimeoutError: If the download thread does not finish within *timeout* seconds.
        Exception: Any exception raised by ``snapshot_download`` itself.
    """
    exc_holder: list[BaseException] = []

    def _run() -> None:  # pragma: no cover — network IO
        try:
            # Public model checkpoint download — repo_id is a known constant.
            snapshot_download(  # nosec B615
                repo_id=repo_id,
                local_dir=local_dir,
                ignore_patterns=ignore_patterns,
                token=token,
            )
        except Exception as exc:
            exc_holder.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():  # pragma: no cover — timing-sensitive
        raise TimeoutError(
            f"Download of '{repo_id}' did not complete within {timeout}s — "
            "the connection appears stuck."
        )
    if exc_holder:  # pragma: no cover — network failure path
        raise exc_holder[0]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_model_if_needed(
    model_cfg: ModelConfig,
    index_cfg: IndexConfig,
    hf_token: str | None = None,
) -> Path:
    """Ensure the MuQ model checkpoint is available locally.

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
        models/MuQ-large-msd-iter
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

    # --- download with retry ---
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading model %s to %s ...", model_name, cache_dir)
    print(
        f"\n[AutoDJ] Downloading model '{model_name}' (~1.2 GB) to {cache_dir}\n"
        "This is a one-time download. Please wait...\n"
    )

    max_retries = _DEFAULT_MAX_RETRIES
    timeout = _DEFAULT_TIMEOUT_SECONDS
    retry_delay = _DEFAULT_RETRY_DELAY
    ignore_patterns = ["*.msgpack", "flax_model*", "tf_model*", "rust_model*"]

    last_exc: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Download attempt %d/%d (timeout=%ds) ...", attempt, max_retries, timeout)
            if attempt > 1:
                print(f"[AutoDJ] Retry {attempt}/{max_retries} ...\n")
            _snapshot_download_with_timeout(
                repo_id=model_name,
                local_dir=str(cache_dir),
                ignore_patterns=ignore_patterns,
                token=hf_token,
                timeout=timeout,
            )
            logger.info("Download complete.")
            return cache_dir
        except TimeoutError as exc:
            last_exc = exc
            logger.warning(
                "Attempt %d/%d timed out after %ds: %s",
                attempt,
                max_retries,
                timeout,
                exc,
            )
            print(
                f"[AutoDJ] Attempt {attempt}/{max_retries} timed out "
                f"after {timeout}s — retrying in {retry_delay}s...\n"
            )
        except Exception as exc:
            last_exc = exc
            logger.warning("Attempt %d/%d failed: %s", attempt, max_retries, exc)
            print(
                f"[AutoDJ] Attempt {attempt}/{max_retries} failed "
                f"({exc}) — retrying in {retry_delay}s...\n"
            )

        if attempt < max_retries:
            time.sleep(retry_delay)

    raise ModelLoadError(
        f"Failed to download model '{model_name}' after {max_retries} attempts.\n"
        f"Last error: {last_exc}\n\n"
        "Manual download instructions:\n"
        f"  1. Visit https://huggingface.co/{model_name}\n"
        "  2. Click 'Files and versions' and download all files\n"
        f"  3. Place them in: {cache_dir}\n"
        "  4. Add to config.toml:\n"
        f"     [model]\n"
        f'     manual_path = "{cache_dir}"\n'
    ) from last_exc


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


def load_model(model_path: Path) -> MuqWrapper:
    """Load the MuQ model from a local directory and return a :class:`MuqWrapper`.

    Automatically selects CUDA if available, falls back to CPU otherwise.
    The model is set to eval mode and gradient computation is disabled.

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
    device = "cuda" if torch.cuda.is_available() else "cpu"  # pragma: no cover
    logger.info("Loading MuQ model from %s on device=%s", model_path, device)  # pragma: no cover

    try:  # pragma: no cover
        model = MuQ.from_pretrained(str(model_path))
    except Exception as exc:  # pragma: no cover
        raise ModelLoadError(
            f"Failed to load model from {model_path}: {exc}\n"
            "The model files may be incomplete. Try deleting the directory and re-running."
        ) from exc

    model = model.to(device)  # pragma: no cover
    model.eval()  # pragma: no cover

    return MuqWrapper(model=model, device=device)  # pragma: no cover
