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

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast

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
# Durable model-cache helpers
# ---------------------------------------------------------------------------

_MARKER_NAME = ".autodj-complete"
_ETAG_TIMEOUT_SECONDS = 10
_IGNORE_PATTERNS = ["*.msgpack", "flax_model*", "tf_model*", "rust_model*"]
_SAFE_SHARD_PATTERNS = {
    ".safetensors": re.compile(r"model-(\d{5})-of-(\d{5})\.safetensors$"),
    ".bin": re.compile(r"pytorch_model-(\d{5})-of-(\d{5})\.bin$"),
}
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()
_thread_locks_pid = os.getpid()
_reentrant_locks: dict[tuple[int, int, str], int] = {}
_reentrant_locks_guard = threading.Lock()


class _FcntlApi(Protocol):
    """POSIX lock API omitted from Windows type stubs."""

    LOCK_EX: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None:
        """Apply or release an advisory file lock."""


def _reset_model_cache_locks_after_fork() -> None:
    """Discard inherited process-local locks in a fork child."""
    global \
        _reentrant_locks, \
        _reentrant_locks_guard, \
        _thread_locks, \
        _thread_locks_guard, \
        _thread_locks_pid
    _thread_locks = {}
    _thread_locks_guard = threading.Lock()
    _reentrant_locks = {}
    _reentrant_locks_guard = threading.Lock()
    _thread_locks_pid = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_model_cache_locks_after_fork)


@dataclass(frozen=True)
class ModelCacheStatus:
    """Result of checking whether a local model cache is safe to use."""

    path: Path
    complete: bool
    reason: str


def model_cache_path(model_cfg: ModelConfig, index_cfg: IndexConfig) -> Path:
    """Return exact manual path or collision-proof automatic cache path."""
    if model_cfg.manual_path is not None:
        return model_cfg.manual_path
    digest = hashlib.sha256(f"{model_cfg.name}@{model_cfg.revision}".encode()).hexdigest()[:16]
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", model_cfg.name.rsplit("/", 1)[-1]).strip(".-")
    return index_cfg.model_dir / f"{readable or 'model'}-{digest}"


def _is_reparse_point(path: Path) -> bool:
    """Reject links and Windows junction/reparse points before filesystem work."""
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & 0x400)


def _indexed_weights(cache_path: Path) -> tuple[bool, str]:
    """Validate an optional HuggingFace weight index in *cache_path*."""
    indexes = [
        item
        for item in cache_path.iterdir()
        if item.is_file()
        and (item.name.endswith(".safetensors.index.json") or item.name.endswith(".bin.index.json"))
    ]
    if not indexes:
        return False, "no-index"
    if len(indexes) != 1:
        return False, "invalid-index"
    index_error = f"invalid shard index: {indexes[0].name}"
    if indexes[0].name == "model.safetensors.index.json":
        extension = ".safetensors"
    elif indexes[0].name == "pytorch_model.bin.index.json":
        extension = ".bin"
    else:
        return False, index_error
    try:
        data = json.loads(indexes[0].read_text(encoding="utf-8"))
        weight_map = data["weight_map"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return False, index_error
    if not isinstance(weight_map, dict) or not weight_map:
        return False, "invalid-index"
    shards: set[str] = set()
    positions: set[int] = set()
    totals: set[int] = set()
    pattern = _SAFE_SHARD_PATTERNS[extension]
    for shard in weight_map.values():
        if (
            not isinstance(shard, str)
            or not shard
            or "/" in shard
            or "\\" in shard
            or Path(shard).name != shard
            or Path(shard).is_absolute()
        ):
            return False, index_error
        match = pattern.fullmatch(shard)
        if match is None:
            return False, index_error
        position, total = (int(value) for value in match.groups())
        if position < 1 or total < 1 or position > total:
            return False, "invalid-index"
        if not (cache_path / shard).is_file():
            return False, f"missing indexed weight: {shard}"
        shards.add(shard)
        positions.add(position)
        totals.add(total)
    if len(totals) != 1:
        return False, index_error
    total = totals.pop()
    if positions != set(range(1, total + 1)) or len(shards) != total:
        return False, index_error
    actual_shards = {
        item.name
        for item in cache_path.iterdir()
        if item.is_file()
        and any(pattern.fullmatch(item.name) for pattern in _SAFE_SHARD_PATTERNS.values())
    }
    if actual_shards != shards:
        return False, index_error
    return True, "indexed-weights"


def _inspect_model_path(
    path: Path,
    repo_id: str | None = None,
    revision: str | None = None,
) -> ModelCacheStatus:
    """Inspect model files without mutating cache state.

    Passing ``repo_id`` and ``revision`` enables strict automatic-cache marker
    validation. Omit both for a manually maintained model directory.
    """
    cache_path = Path(path)
    if _is_reparse_point(cache_path):
        return ModelCacheStatus(cache_path, False, "symlinked cache path")
    if not cache_path.exists():
        return ModelCacheStatus(cache_path, False, "cache directory missing")
    if not cache_path.is_dir():
        return ModelCacheStatus(cache_path, False, "cache directory missing")
    if not (cache_path / "config.json").is_file():
        return ModelCacheStatus(cache_path, False, "missing config.json")
    resolved_root = cache_path.resolve()
    if any(
        _is_reparse_point(item) or not item.resolve().is_relative_to(resolved_root)
        for item in cache_path.rglob("*")
    ):
        return ModelCacheStatus(cache_path, False, "symlinked cache artifact")

    indexed, weight_reason = _indexed_weights(cache_path)
    if not indexed and weight_reason != "no-index":
        if weight_reason.startswith("missing indexed weight:"):
            return ModelCacheStatus(cache_path, False, weight_reason)
        index_name = next(
            (
                item.name
                for item in cache_path.iterdir()
                if item.name.endswith((".safetensors.index.json", ".bin.index.json"))
            ),
            "model.safetensors.index.json",
        )
        return ModelCacheStatus(cache_path, False, f"invalid shard index: {index_name}")
    files = [item for item in cache_path.iterdir() if item.is_file()]
    if indexed and any(item.name in {"model.safetensors", "pytorch_model.bin"} for item in files):
        return ModelCacheStatus(cache_path, False, "ambiguous model weight layout")
    if not indexed:
        if any(
            pattern.fullmatch(item.name)
            for item in files
            for pattern in _SAFE_SHARD_PATTERNS.values()
        ):
            return ModelCacheStatus(cache_path, False, "missing model weights or shard index")
        standalone = [
            item for item in files if item.name in {"model.safetensors", "pytorch_model.bin"}
        ]
        if len(standalone) != 1:
            return ModelCacheStatus(cache_path, False, "missing model weights or shard index")

    if (repo_id is None) != (revision is None):
        return ModelCacheStatus(cache_path, False, "invalid-marker-request")
    if repo_id is not None:
        marker = cache_path / _MARKER_NAME
        if not marker.is_file():
            return ModelCacheStatus(cache_path, False, "missing completion marker")
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ModelCacheStatus(cache_path, False, "invalid completion marker")
        expected = {"repo_id": repo_id, "revision": revision}
        if marker_data != expected:
            return ModelCacheStatus(cache_path, False, "completion marker identity mismatch")
    return ModelCacheStatus(cache_path, True, "complete")


def inspect_model_cache(model_cfg: ModelConfig, index_cfg: IndexConfig) -> ModelCacheStatus:
    """Public inspected-cache status for configured automatic or manual model."""
    path = model_cache_path(model_cfg, index_cfg)
    return _inspect_model_path(
        path,
        None if model_cfg.manual_path is not None else model_cfg.name,
        None if model_cfg.manual_path is not None else model_cfg.revision,
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Flush directory metadata where platform permits it."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for item in root.rglob("*"):
        if item.is_file():
            _fsync_file(item)
    for item in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        _fsync_directory(item)
    _fsync_directory(root)


def _thread_lock(path: Path) -> threading.RLock:
    global _thread_locks_pid, _thread_locks
    with _thread_locks_guard:
        if _thread_locks_pid != os.getpid():
            _thread_locks = {}
            _thread_locks_pid = os.getpid()
        return _thread_locks.setdefault(str(path.absolute()), threading.RLock())


def _is_windows_lock_contention(error: OSError) -> bool:
    """Return whether Windows reported a transient file-lock collision."""
    if error.winerror is not None:
        return error.winerror in {32, 33}
    return error.errno in {errno.EACCES, errno.EAGAIN}


def _acquire_windows_file_lock(handle: BinaryIO) -> None:
    """Acquire one-byte lock, retrying only documented contention errors."""
    import msvcrt

    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if not _is_windows_lock_contention(error):
                raise
            threading.Event().wait(0.05)


@contextmanager
def _model_cache_lock(cache_path: Path) -> Iterator[None]:
    """Serialize a cache promotion across threads and processes."""
    lock = _thread_lock(cache_path)
    lock_path = cache_path.parent / f".{cache_path.name}.lock"
    key = (os.getpid(), threading.get_ident(), str(cache_path.absolute()))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("xb") as initializer:
            initializer.write(b"0")
            initializer.flush()
    except FileExistsError:
        pass
    lock.acquire()
    with _reentrant_locks_guard:
        nested = key in _reentrant_locks
        _reentrant_locks[key] = _reentrant_locks.get(key, 0) + 1
    if nested:
        try:
            yield
        finally:
            with _reentrant_locks_guard:
                _reentrant_locks[key] -= 1
                if not _reentrant_locks[key]:
                    del _reentrant_locks[key]
            lock.release()
        return
    try:
        with lock_path.open("r+b") as handle:
            if os.name == "nt":
                import msvcrt

                _acquire_windows_file_lock(handle)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl_api = cast(_FcntlApi, fcntl)
                fcntl_api.flock(handle.fileno(), fcntl_api.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl_api.flock(handle.fileno(), fcntl_api.LOCK_UN)
    finally:
        with _reentrant_locks_guard:
            _reentrant_locks[key] -= 1
            if not _reentrant_locks[key]:
                del _reentrant_locks[key]
        lock.release()


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
    cache_dir = model_cache_path(model_cfg, index_cfg)
    if model_cfg.manual_path is not None:
        status = inspect_model_cache(model_cfg, index_cfg)
        if not status.complete:
            raise ModelLoadError(f"manual_path is incomplete ({status.reason}): {cache_dir}")
        return cache_dir

    status = inspect_model_cache(model_cfg, index_cfg)
    if status.complete:
        return cache_dir

    staging: Path | None = None
    try:
        with _model_cache_lock(cache_dir):
            status = inspect_model_cache(model_cfg, index_cfg)
            if status.complete:
                return cache_dir
            staging = cache_dir.parent / f".{cache_dir.name}.staging-{uuid.uuid4().hex}"
            staging.mkdir()
            snapshot_download(  # nosec B615 -- repository comes from user configuration
                repo_id=model_cfg.name,
                revision=model_cfg.revision,
                local_dir=str(staging),
                token=hf_token,
                ignore_patterns=_IGNORE_PATTERNS,
                etag_timeout=_ETAG_TIMEOUT_SECONDS,
            )
            staged = _inspect_model_path(staging)
            if not staged.complete:
                raise ModelLoadError(f"download produced incomplete model cache ({staged.reason})")
            marker = staging / _MARKER_NAME
            marker.write_text(
                json.dumps(
                    {"repo_id": model_cfg.name, "revision": model_cfg.revision},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            _fsync_file(marker)
            _fsync_tree(staging)
            # Delete only target already inspected incomplete, immediately before promotion.
            current = inspect_model_cache(model_cfg, index_cfg)
            if current.complete:
                return cache_dir
            if cache_dir.is_symlink() or cache_dir.is_file():
                cache_dir.unlink()
            elif cache_dir.is_dir():
                shutil.rmtree(cache_dir)
            os.replace(staging, cache_dir)
            staging = None
            _fsync_directory(cache_dir.parent)
            return cache_dir
    except Exception as exc:
        if isinstance(exc, ModelLoadError):
            raise
        raise ModelLoadError(f"Failed to download model '{model_cfg.name}': {exc}") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


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
    from autodj.compute import device_string  # pragma: no cover

    device = device_string()  # pragma: no cover
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
