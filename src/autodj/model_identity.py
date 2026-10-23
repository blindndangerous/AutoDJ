"""Which exact model weights this machine analyses with.

A remote worker's vectors are only comparable with the server's when both
ran the same weights.  ``[model] revision`` names a branch (``main`` by
default), and two machines that downloaded ``main`` at different times can
hold different weights, so the branch name proves nothing.  This module
names the weights themselves:

- for a model in the HuggingFace cache, the commit hash of the snapshot the
  revision resolves to on this machine, which the cache records;
- for a ``[model] manual_path`` folder (or a folder that is not a cache
  snapshot), ``sha256:`` plus a digest of its weight and configuration files.

It needs no torch, so the web server can check results without loading
the model.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

from autodj.model_files import IGNORE_PATTERNS

if TYPE_CHECKING:
    from autodj.config import IndexConfig, ModelConfig

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
#: Files whose bytes decide what the model computes.
_WEIGHT_SUFFIXES = frozenset({".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".json"})
_CHUNK = 1 << 20
#: The coarsest modification-time resolution in common use (FAT and exFAT).
_MTIME_BLUR_NS = 2_000_000_000
_digests: dict[tuple, str] = {}
_digests_lock = threading.Lock()


def model_analysis_available() -> bool:
    """Whether local model analysis is enabled and its packages are installed.

    Discover packages without importing the model or any compute runtime.
    """
    if os.environ.get("AUTODJ_NO_MODEL") == "1":
        return False
    try:
        return all(
            importlib.util.find_spec(name) is not None for name in ("torch", "muq", "transformers")
        )
    except (ImportError, ValueError):
        return False


def _weight_files(folder: Path) -> list[Path]:
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in _WEIGHT_SUFFIXES
    )


def weights_digest(folder: Path) -> str:
    """``sha256:`` and a digest of every weight and config file's name and bytes.

    Remembered per file sizes and modification times, so a large model is
    read once per process.  A file rewritten at the same size within one
    tick of the filesystem clock keeps its size and time, so, as git does
    for its index, a digest is only remembered once every file is older
    than that clock can blur.
    """
    started = time.time_ns()
    files = _weight_files(folder)
    stats = [path.stat() for path in files]
    key = tuple(
        (str(path.relative_to(folder)), stat.st_size, stat.st_mtime_ns)
        for path, stat in zip(files, stats, strict=True)
    )
    with _digests_lock:
        found = _digests.get((str(folder), key))
    if found is not None:
        return found
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    value = f"sha256:{digest.hexdigest()}"
    if all(stat.st_mtime_ns < started - _MTIME_BLUR_NS for stat in stats):
        with _digests_lock:
            _digests[(str(folder), key)] = value
    return value


def folder_commit(folder: Path) -> str:
    """The commit of a HuggingFace cache snapshot folder, else its :func:`weights_digest`."""
    if folder.parent.name == "snapshots" and _COMMIT.fullmatch(folder.name):
        return folder.name
    return weights_digest(folder)


def local_model_folder(model_cfg: ModelConfig, index_cfg: IndexConfig) -> Path | None:
    """The model folder on this machine, without the network, or ``None`` if absent."""
    if model_cfg.manual_path is not None:
        return model_cfg.manual_path if model_cfg.manual_path.is_dir() else None
    try:
        path = snapshot_download(  # nosec B615 -- repository comes from user configuration
            repo_id=model_cfg.name,
            revision=model_cfg.revision,
            cache_dir=index_cfg.model_dir,
            # The same filter the download used; without it huggingface_hub
            # counts the skipped files as missing and calls the snapshot
            # incomplete.
            ignore_patterns=IGNORE_PATTERNS,
            local_files_only=True,
        )
    except (LocalEntryNotFoundError, OSError, ValueError):
        return None
    return Path(path)


def local_model_commit(model_cfg: ModelConfig, index_cfg: IndexConfig) -> str | None:
    """What :func:`folder_commit` says of the model here, or ``None`` when it is not here."""
    folder = local_model_folder(model_cfg, index_cfg)
    return None if folder is None else folder_commit(folder)


class CachedModelCommit:
    """:func:`local_model_commit` for a server, looked up at most once a minute.

    *configs* returns the current ``(model, index)`` configuration.
    """

    def __init__(
        self,
        configs: Callable[[], tuple[ModelConfig, IndexConfig]],
        *,
        seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._configs = configs
        self._seconds = seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._value: str | None = None
        self._until = float("-inf")

    def __call__(self) -> str | None:
        with self._lock:
            if self._monotonic() < self._until:
                return self._value
        value = local_model_commit(*self._configs())
        with self._lock:
            self._value = value
            self._until = self._monotonic() + self._seconds
        return value


__all__ = [
    "CachedModelCommit",
    "folder_commit",
    "local_model_commit",
    "local_model_folder",
    "model_analysis_available",
    "weights_digest",
]
