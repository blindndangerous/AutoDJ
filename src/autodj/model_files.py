"""Which files of the model repository AutoDJ downloads.

Shared by :mod:`autodj.model`, which downloads the model, and
:mod:`autodj.model_identity`, which looks it up without torch.  A lookup
must use the same filter as the download, or huggingface_hub counts the
skipped files as missing.
"""

from __future__ import annotations

#: Weight formats for other frameworks that AutoDJ never loads.
IGNORE_PATTERNS = [
    "*.msgpack",
    "flax_model*",
    "tf_model*",
    "rust_model*",
    "pytorch_model.bin",
]
