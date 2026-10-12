"""Stations: one playout chain each, sharing one music library (#70).

A station is a player and the state around it: its schedule, carts,
time checks, listener bans, recording state, clockwheels, liners and
profiles.  The library, the similarity index, pairing and the web server
are shared.

Today a server runs exactly one station, ``default``, whose state lives
where it always has.  :class:`StationPaths` is the only code that knows
those locations, so a later station can keep the same files elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig


class StationPaths:
    """Where one station keeps its state files and folders.

    Each location is worked out from the configuration when it is asked
    for, as the code it replaced did, so a configuration that only sets
    the index folder still answers for the files under it.
    """

    __slots__ = ("_cfg",)

    def __init__(self, cfg: AutoDJConfig) -> None:
        self._cfg = cfg

    @classmethod
    def default(cls, cfg: AutoDJConfig) -> StationPaths:
        """The default station's paths: the active index folder, as always."""
        return cls(cfg)

    @property
    def state_dir(self) -> Path:
        """The folder holding the station's state files."""
        return Path(self._cfg.index.active_dir)

    @property
    def web_state(self) -> Path:
        """Settings saved from the web page."""
        return self.state_dir / "web_state.json"

    @property
    def schedule(self) -> Path:
        """Scheduled events."""
        return self.state_dir / "schedule.json"

    @property
    def carts(self) -> Path:
        """The cart wall."""
        return self.state_dir / "carts.json"

    @property
    def cart_uploads(self) -> Path:
        """Audio uploaded to carts."""
        return self.state_dir / "carts"

    @property
    def time_checks(self) -> Path:
        """The time check switches saved from Settings."""
        return self.state_dir / "time_checks.json"

    @property
    def time_checks_cache(self) -> Path:
        """Spoken time checks made by the text-to-speech program."""
        return self.state_dir / "time_checks_cache"

    @property
    def time_check_clips(self) -> Path:
        """Recorded time check clips: ``[time_checks] clips_folder`` or the default."""
        return Path(self._cfg.time_checks.clips_folder or self.state_dir / "time_checks")

    @property
    def listener_bans(self) -> Path:
        """Banned listener addresses."""
        return self.state_dir / "listener_bans.json"

    @property
    def recording(self) -> Path:
        """Whether a broadcast recording was running."""
        return self.state_dir / "recording.json"

    @property
    def relay_status(self) -> Path:
        """Relay states, for ``autodj doctor``; in the index folder, not the named index."""
        return Path(self._cfg.index.index_dir) / "relay-status.json"

    @property
    def clockwheels(self) -> Path:
        """Saved clockwheels."""
        return self.state_dir / "clockwheels"

    @property
    def liners(self) -> Path:
        """Liner clips: ``[playback] liners_folder`` or the default."""
        return Path(self._cfg.playback.liners_folder or self.state_dir / "liners")

    @property
    def profiles(self) -> Path:
        """Saved profiles; shared by every named index of the index folder."""
        return self.state_dir.parent / "profiles"
