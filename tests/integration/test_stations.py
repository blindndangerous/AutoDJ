"""Where a station keeps its state (#70)."""

from __future__ import annotations

from pathlib import Path

from autodj.carts import CartStore
from autodj.config import AutoDJConfig, load_config
from autodj.stations import StationPaths


def _config(tmp_path: Path, *, liners: str | None = None, clips: str | None = None) -> AutoDJConfig:
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path / "index"
    cfg.index.name = "main"
    cfg.playback.liners_folder = liners
    cfg.time_checks.clips_folder = clips
    return cfg


def test_default_paths_are_where_the_state_has_always_been(tmp_path: Path) -> None:
    from autodj.relay import relay_status_path
    from autodj.time_checks import TimeChecks, clips_folder, state_path

    cfg = _config(tmp_path)
    index = tmp_path / "index"
    active = index / "main"
    paths = StationPaths.default(cfg)

    assert paths.state_dir == active
    assert paths.web_state == active / "web_state.json"
    assert paths.schedule == active / "schedule.json"
    assert paths.carts == active / "carts.json"
    assert paths.cart_uploads == active / "carts"
    assert paths.time_checks == active / "time_checks.json"
    assert paths.time_checks_cache == active / "time_checks_cache"
    assert paths.time_check_clips == active / "time_checks"
    assert paths.listener_bans == active / "listener_bans.json"
    assert paths.recording == active / "recording.json"
    assert paths.relay_status == index / "relay-status.json"
    assert paths.clockwheels == active / "clockwheels"
    assert paths.liners == active / "liners"
    # Profiles are shared by every named index of the index folder.
    assert paths.profiles == index / "profiles"

    # The modules that keep these files ask StationPaths and get the same answer.
    store = CartStore(paths.state_dir)
    assert (store.path, store.uploads) == (paths.carts, paths.cart_uploads)
    assert state_path(cfg) == paths.time_checks
    assert clips_folder(cfg) == paths.time_check_clips
    assert relay_status_path(cfg) == paths.relay_status
    assert TimeChecks(cfg, "UTC").cache == paths.time_checks_cache


def test_configured_liner_and_clip_folders_win(tmp_path: Path) -> None:
    liners = tmp_path / "voice" / "liners"
    clips = tmp_path / "voice" / "clock"
    paths = StationPaths.default(_config(tmp_path, liners=str(liners), clips=str(clips)))
    assert paths.liners == liners
    assert paths.time_check_clips == clips


def test_paths_follow_a_changed_index_folder(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = StationPaths.default(cfg)
    cfg.index.index_dir = tmp_path / "elsewhere"
    assert paths.schedule == tmp_path / "elsewhere" / "main" / "schedule.json"
