"""Download (when needed) and exercise the configured model during setup.

This module keeps model and numerical-runtime imports out of setup inspection
paths. Run it in the selected runtime with ``python -m autodj.setup_model``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from autodj.terminal_text import format_text

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

# MuQ-large-msd-iter's checkpoint is about 1.334 GB. Leave room for metadata,
# temporary files and the first inference. Custom model sizes are not known.
_DEFAULT_MODEL_NAME = "OpenMuQ/MuQ-large-msd-iter"
_DEFAULT_DOWNLOAD_MINIMUM = 1.6 * 1024**3


class ModelVerificationError(RuntimeError):
    """The configured model could not be downloaded, loaded or exercised."""


def verify_model(cfg: AutoDJConfig) -> None:
    """Ensure model weights exist and verify a real embedding can be computed.

    A compatible local cache is preferred and used as-is, including when the
    configured revision is a floating branch. This makes setup repeatable and
    allows verification without network access.
    """
    try:
        # These imports pull the model stack into memory, so defer them until
        # the explicit verification command is run and report import failures
        # through the same concise CLI error path as inference failures.
        from autodj.model import (
            EMBEDDING_DIM,
            MUQ_SAMPLE_RATE,
            download_model_if_needed,
            load_model,
        )
        from autodj.model_identity import local_model_folder

        model_path = local_model_folder(cfg.model, cfg.index)
        if model_path is None:
            if cfg.model.manual_path is not None:
                raise ModelVerificationError(
                    f"Configured manual model folder does not exist: {cfg.model.manual_path}"
                )
            if cfg.model.name == _DEFAULT_MODEL_NAME:
                model_dir = cfg.index.model_dir
                while not model_dir.exists() and model_dir != model_dir.parent:
                    model_dir = model_dir.parent
                free = shutil.disk_usage(model_dir).free
                if free < _DEFAULT_DOWNLOAD_MINIMUM:
                    raise ModelVerificationError(
                        "Not enough free disk space to download the default MuQ model "
                        f"(need at least {_DEFAULT_DOWNLOAD_MINIMUM / 1024**3:.1f} GiB free; "
                        f"found {free / 1024**3:.1f} GiB). Free space and retry setup."
                    )
            print(format_text("Downloading MuQ model weights."))
            model_path = download_model_if_needed(cfg.model, cfg.index, cfg.huggingface.token)
        else:
            print(format_text("Using existing MuQ model weights."))

        import numpy as np

        print(format_text("Loading MuQ for a short audio check."))
        wrapper = load_model(Path(model_path))
        processor = "graphics processor" if wrapper.device == "cuda" else "CPU"
        print(format_text(f"Checking MuQ on your {processor}."))
        # A short, reproducible, non-silent signal exercises the encoder without
        # writing a temporary audio file or touching the music library.
        sample_count = MUQ_SAMPLE_RATE // 2
        time = np.arange(sample_count, dtype=np.float32) / MUQ_SAMPLE_RATE
        audio = (0.1 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
        embedding = np.asarray(wrapper.embed_array(audio, sample_rate=MUQ_SAMPLE_RATE))
        if embedding.shape != (EMBEDDING_DIM,):
            raise ModelVerificationError(
                f"Model returned embedding shape {embedding.shape}; expected ({EMBEDDING_DIM},)."
            )
        if not np.isfinite(embedding).all():
            raise ModelVerificationError("Model returned an embedding with non-finite values.")
    except ModelVerificationError:
        raise
    except Exception as exc:
        raise ModelVerificationError(f"Model verification failed: {exc}") from exc


def main() -> int:
    """Load project configuration and run setup-time model verification."""
    try:
        from autodj.config import load_config

        verify_model(load_config())
    except Exception as exc:
        print(format_text(f"Model verification failed: {exc}"), file=sys.stderr)
        return 1
    print(format_text("MuQ model verified."))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the setup CLI
    raise SystemExit(main())
