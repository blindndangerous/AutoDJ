"""Configuration loading and validation for AutoDJ.

Loads settings from a TOML file (default: ``config.toml`` in the working
directory) and exposes them as typed dataclasses.

Example:
    >>> from autodj.config import load_config
    >>> cfg = load_config("config.toml")
    >>> print(cfg.playback.crossfade_seconds)
    3.0
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Sub-section dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LibraryConfig:
    """Settings for the music library location and format filtering.

    Attributes:
        music_dir: Path to the root music folder (local or NAS mapped drive).
        beets_db: Optional path to the beets SQLite library database.
        supported_formats: List of audio file extensions to index (without dots).
    """

    music_dir: Path
    beets_db: Path | None
    supported_formats: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LibraryConfig":
        """Construct a LibraryConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[library]`` TOML section.

        Returns:
            A populated LibraryConfig instance.

        Raises:
            KeyError: If ``music_dir`` is not present.
        """
        if "music_dir" not in data:
            raise KeyError("config.toml [library] section is missing 'music_dir'")
        beets_raw = data.get("beets_db")
        return cls(
            music_dir=Path(data["music_dir"]),
            beets_db=Path(beets_raw) if beets_raw else None,
            supported_formats=data.get("supported_formats", ["mp3", "flac", "m4a"]),
        )


@dataclass
class IndexConfig:
    """Settings for the FAISS index storage locations.

    Attributes:
        index_dir: Directory where ``vectors.index`` and ``metadata.json`` are stored.
        model_dir: Directory where the MERT model checkpoint is cached.
    """

    index_dir: Path = field(default_factory=lambda: Path("index"))
    model_dir: Path = field(default_factory=lambda: Path("models"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IndexConfig":
        """Construct an IndexConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[index]`` TOML section.

        Returns:
            A populated IndexConfig instance with defaults applied for missing keys.
        """
        return cls(
            index_dir=Path(data.get("index_dir", "index")),
            model_dir=Path(data.get("model_dir", "models")),
        )


@dataclass
class PlaybackConfig:
    """Settings for audio playback behaviour.

    Attributes:
        crossfade_seconds: Duration of the crossfade between tracks in seconds.
            Set to ``0.0`` to disable crossfade entirely.
        no_repeat_window: Number of recently played tracks excluded from the
            next-song candidate pool.
    """

    crossfade_seconds: float = 3.0
    no_repeat_window: int = 50

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaybackConfig":
        """Construct a PlaybackConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[playback]`` TOML section.

        Returns:
            A populated PlaybackConfig instance.

        Raises:
            ValueError: If ``crossfade_seconds`` is negative or
                ``no_repeat_window`` is negative.
        """
        crossfade = float(data.get("crossfade_seconds", 3.0))
        no_repeat = int(data.get("no_repeat_window", 50))

        if crossfade < 0:
            raise ValueError(
                f"playback.crossfade_seconds must be >= 0, got {crossfade}"
            )
        if no_repeat < 0:
            raise ValueError(
                f"playback.no_repeat_window must be >= 0, got {no_repeat}"
            )

        return cls(crossfade_seconds=crossfade, no_repeat_window=no_repeat)


@dataclass
class ModelConfig:
    """Settings for the MERT embedding model.

    Attributes:
        name: HuggingFace model ID to load (used for auto-download).
        manual_path: Optional local path to a pre-downloaded model directory.
            When set, ``name`` is ignored and the model is loaded from disk.
    """

    name: str = "m-a-p/MERT-v1-330M"
    manual_path: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelConfig":
        """Construct a ModelConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[model]`` TOML section.

        Returns:
            A populated ModelConfig instance.
        """
        manual_raw = data.get("manual_path")
        return cls(
            name=data.get("name", "m-a-p/MERT-v1-330M"),
            manual_path=Path(manual_raw) if manual_raw else None,
        )


# ---------------------------------------------------------------------------
# Root config dataclass
# ---------------------------------------------------------------------------


@dataclass
class AutoDJConfig:
    """Root configuration for the AutoDJ application.

    Attributes:
        library: Library location and format settings.
        index: FAISS index storage settings.
        playback: Playback behaviour settings.
        model: MERT model settings.
        config_path: Path to the config file this instance was loaded from.
    """

    library: LibraryConfig
    index: IndexConfig
    playback: PlaybackConfig
    model: ModelConfig
    config_path: Path


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_config(path: str | Path = "config.toml") -> AutoDJConfig:
    """Load and validate the AutoDJ configuration from a TOML file.

    Args:
        path: Path to the ``config.toml`` file. Defaults to ``config.toml``
            in the current working directory.

    Returns:
        A fully populated and validated :class:`AutoDJConfig` instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        KeyError: If a required key (e.g. ``library.music_dir``) is missing.
        ValueError: If a value is out of the accepted range.

    Example:
        >>> cfg = load_config("config.toml")
        >>> cfg.library.music_dir
        PosixPath('Z:/Music')
    """
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Run 'autodj' in the project directory or pass --config <path>."
        )

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    return AutoDJConfig(
        library=LibraryConfig.from_dict(raw.get("library", {})),
        index=IndexConfig.from_dict(raw.get("index", {})),
        playback=PlaybackConfig.from_dict(raw.get("playback", {})),
        model=ModelConfig.from_dict(raw.get("model", {})),
        config_path=config_path,
    )
