"""Server-side liner triggers, picking and decoding."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from autodj.liner_scheduler import LinerDecodeError, LinerScheduler, decode_liner


def _cfg(**over):
    base = {
        "liners_enabled": True,
        "liners_every_n_songs": 2,
        "liners_every_minutes": None,
        "liners_random_min_minutes": None,
        "liners_random_max_minutes": None,
        "liners_pick_mode": "sequential",
        "liners_duck_db": -10.0,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _folder(tmp_path: Path) -> Path:
    for name in ("a.wav", "b.wav"):
        sf.write(tmp_path / name, np.zeros(441, np.float32), 44100)
    return tmp_path


def test_decode_wav_is_stereo(tmp_path: Path) -> None:
    sf.write(tmp_path / "x.wav", np.full(441, 0.25, np.float32), 22050)
    out = decode_liner(tmp_path, "x.wav")
    assert out.ndim == 2 and out.shape[1] == 2
    assert abs(len(out) - 882) <= 2


def test_decode_falls_back_to_ffmpeg(tmp_path: Path) -> None:
    (tmp_path / "x.m4a").write_bytes(b"not really m4a")
    pcm = np.full((10, 2), 0.5, np.float32).tobytes()
    runner = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=pcm, stderr=b""))
    out = decode_liner(tmp_path, "x.m4a", runner=runner)
    assert out.shape == (10, 2)
    args = runner.call_args.args[0]
    assert args[0] == "ffmpeg" and "pipe:0" in args


def test_undecodable_liner_raises(tmp_path: Path) -> None:
    (tmp_path / "x.m4a").write_bytes(b"junk")
    runner = MagicMock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"bad"))
    with pytest.raises(LinerDecodeError):
        decode_liner(tmp_path, "x.m4a", runner=runner)


def test_every_n_songs_fires_and_rotates(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), _folder(tmp_path), bus, clock=lambda: 0.0, rng=random.Random(1))
    sched.on_track_start()
    assert bus.play_liner.call_count == 0
    sched.on_track_start()
    assert bus.play_liner.call_count == 1
    assert bus.play_liner.call_args.kwargs["duck_db"] == -10.0


def test_timed_trigger_fires_on_tick(tmp_path: Path) -> None:
    now = [0.0]
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(liners_every_n_songs=None, liners_every_minutes=1.0),
        _folder(tmp_path),
        bus,
        clock=lambda: now[0],
    )
    sched.tick()
    assert bus.play_liner.call_count == 0
    now[0] = 61.0
    sched.tick()
    assert bus.play_liner.call_count == 1


def test_bad_liner_is_skipped_and_music_carries_on(tmp_path: Path) -> None:
    bus = MagicMock()

    def broken(_root: Path, _name: str) -> np.ndarray:
        raise LinerDecodeError("corrupt")

    sched = LinerScheduler(_cfg(liners_every_n_songs=1), _folder(tmp_path), bus, decoder=broken)
    sched.on_track_start()
    bus.play_liner.assert_not_called()


def test_disabled_never_fires(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(liners_enabled=False, liners_every_n_songs=1), _folder(tmp_path), bus
    )
    sched.on_track_start()
    sched.tick()
    bus.play_liner.assert_not_called()


def test_fire_by_name_plays_that_file(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), _folder(tmp_path), bus)
    assert sched.fire("b.wav") == "b.wav"
    assert bus.play_liner.call_count == 1


def test_fire_with_no_files_returns_none(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), tmp_path, bus)
    assert sched.fire() is None
    bus.play_liner.assert_not_called()


def test_nested_liner_name_is_skipped_gracefully(tmp_path: Path) -> None:
    """A nested-folder name from ``rglob`` cannot open under the plain root;

    firing it must be swallowed rather than raising into the mix bus.
    """
    bus = MagicMock()
    sub = tmp_path / "sub"
    sub.mkdir()
    sf.write(sub / "jingle.wav", np.zeros(441, np.float32), 44100)
    sched = LinerScheduler(_cfg(), tmp_path, bus)
    assert sched.fire("jingle.wav") is None
    bus.play_liner.assert_not_called()
