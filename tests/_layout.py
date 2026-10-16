"""Point a mocked ``[index]`` section at a test folder."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def point_index(index: Any, folder: Path | str | None) -> None:
    """Keep the library and shared files in *folder*, and the stations below it.

    ``None`` is no index folder at all.
    """
    if folder is None:
        index.index_dir = index.library_dir = index.shared_dir = index.stations_dir = None
        return
    folder = Path(folder)
    index.index_dir = folder
    index.library_dir = folder
    index.shared_dir = folder
    index.stations_dir = folder / "stations"
    if folder.is_dir():
        # As a test's folder held the default station's files before.
        (folder / "stations" / "default").mkdir(parents=True, exist_ok=True)


def layout_index(folder: Path | str | None) -> Any:
    """A stand-in ``[index]`` section pointed at *folder* (see :func:`point_index`)."""
    from types import SimpleNamespace

    index = SimpleNamespace()
    point_index(index, folder)
    return index
