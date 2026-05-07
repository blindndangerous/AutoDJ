"""Unit tests for :mod:`autodj.liners`.

Covers the trigger evaluation, library discovery, and rotation
modes used by the voice-liner overlay feature.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from autodj.liners import LINER_EXTS, LinerLibrary, LinerTrigger


class TestLinerTrigger:
    def test_disabled_never_fires(self) -> None:
        t = LinerTrigger(enabled=False, every_n_songs=1)
        assert t.should_fire(track_count=999, minutes_since_last=999) is False

    def test_every_n_songs_fires(self) -> None:
        t = LinerTrigger(enabled=True, every_n_songs=3)
        assert t.should_fire(track_count=2, minutes_since_last=0) is False
        assert t.should_fire(track_count=3, minutes_since_last=0) is True
        assert t.should_fire(track_count=4, minutes_since_last=0) is True

    def test_every_minutes_fires(self) -> None:
        t = LinerTrigger(enabled=True, every_minutes=5.0)
        assert t.should_fire(track_count=0, minutes_since_last=4.5) is False
        assert t.should_fire(track_count=0, minutes_since_last=5.0) is True
        assert t.should_fire(track_count=0, minutes_since_last=10.0) is True

    def test_random_target_fires(self) -> None:
        t = LinerTrigger(enabled=True)
        assert (
            t.should_fire(
                track_count=0,
                minutes_since_last=4.0,
                random_target_minutes=5.0,
            )
            is False
        )
        assert (
            t.should_fire(
                track_count=0,
                minutes_since_last=5.0,
                random_target_minutes=5.0,
            )
            is True
        )

    def test_either_trigger_fires(self) -> None:
        # Both modes set; either one alone is enough.
        t = LinerTrigger(enabled=True, every_n_songs=10, every_minutes=2.0)
        assert t.should_fire(track_count=1, minutes_since_last=2.5) is True
        assert t.should_fire(track_count=10, minutes_since_last=0.5) is True

    def test_roll_random_target_in_range(self) -> None:
        t = LinerTrigger(
            enabled=True,
            random_min_minutes=2.0,
            random_max_minutes=8.0,
        )
        rng = random.Random(42)
        for _ in range(20):
            target = t.roll_random_target(rng=rng)
            assert target is not None
            assert 2.0 <= target <= 8.0

    def test_roll_random_target_unconfigured(self) -> None:
        t = LinerTrigger(enabled=True)
        assert t.roll_random_target() is None

    def test_roll_random_target_invalid_range(self) -> None:
        t = LinerTrigger(
            enabled=True,
            random_min_minutes=10.0,
            random_max_minutes=5.0,  # min > max
        )
        assert t.roll_random_target() is None

    def test_zero_or_negative_n_songs_disabled(self) -> None:
        t = LinerTrigger(enabled=True, every_n_songs=0)
        assert t.should_fire(track_count=999, minutes_since_last=0) is False


class TestLinerLibrary:
    def test_from_folder_missing(self, tmp_path: Path) -> None:
        lib = LinerLibrary.from_folder(tmp_path / "nope")
        assert lib.files == []

    def test_from_folder_picks_up_known_extensions(self, tmp_path: Path) -> None:
        for name in ["a.mp3", "b.WAV", "c.ogg", "d.txt", "e.jpg"]:
            (tmp_path / name).write_bytes(b"")
        lib = LinerLibrary.from_folder(tmp_path)
        names = sorted(f.name.lower() for f in lib.files)
        assert names == ["a.mp3", "b.wav", "c.ogg"]

    def test_extensions_are_normalised_lower_case(self) -> None:
        # Ensure the extension table itself doesn't drift.
        for ext in LINER_EXTS:
            assert ext == ext.lower()
            assert ext.startswith(".")

    def test_from_folder_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.mp3").write_bytes(b"")
        (tmp_path / "top.wav").write_bytes(b"")
        lib = LinerLibrary.from_folder(tmp_path)
        names = sorted(f.name for f in lib.files)
        assert names == ["deep.mp3", "top.wav"]

    def test_pick_empty(self) -> None:
        lib = LinerLibrary(files=[])
        assert lib.pick() is None

    def test_pick_random_returns_member(self) -> None:
        files = [Path(f"a{i}.mp3") for i in range(5)]
        lib = LinerLibrary(files=files, weights=[1.0] * 5)
        rng = random.Random(7)
        for _ in range(10):
            result = lib.pick("random", rng=rng)
            assert result in files

    def test_pick_sequential_advances_cursor(self) -> None:
        files = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3")]
        lib = LinerLibrary(files=files)
        assert lib.pick("sequential") == files[0]
        assert lib.pick("sequential") == files[1]
        assert lib.pick("sequential") == files[2]
        # Wraps:
        assert lib.pick("sequential") == files[0]

    def test_pick_weighted_honours_weights(self) -> None:
        # Weight skewed completely to file index 1 — pick is deterministic.
        files = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3")]
        lib = LinerLibrary(files=files, weights=[0.0, 1.0, 0.0])
        rng = random.Random(0)
        for _ in range(5):
            assert lib.pick("weighted", rng=rng) == files[1]

    def test_pick_weighted_zero_total_falls_back_to_random(self) -> None:
        files = [Path("a.mp3"), Path("b.mp3")]
        lib = LinerLibrary(files=files, weights=[0.0, 0.0])
        result = lib.pick("weighted", rng=random.Random(1))
        assert result in files

    def test_pick_unknown_mode_falls_back_to_random(self) -> None:
        files = [Path("a.mp3"), Path("b.mp3")]
        lib = LinerLibrary(files=files, weights=[1.0, 1.0])
        result = lib.pick("nonsense_mode", rng=random.Random(2))
        assert result in files

    def test_from_folder_sorts_case_insensitive(self, tmp_path: Path) -> None:
        (tmp_path / "Zeta.mp3").write_bytes(b"")
        (tmp_path / "alpha.mp3").write_bytes(b"")
        (tmp_path / "Beta.mp3").write_bytes(b"")
        lib = LinerLibrary.from_folder(tmp_path)
        names = [f.name for f in lib.files]
        assert names == ["alpha.mp3", "Beta.mp3", "Zeta.mp3"]


class TestLinerLibraryWeightsParity:
    """Default weights match the file count and are 1.0 each."""

    def test_default_weights_balanced(self, tmp_path: Path) -> None:
        for n in ["a.mp3", "b.mp3", "c.mp3"]:
            (tmp_path / n).write_bytes(b"")
        lib = LinerLibrary.from_folder(tmp_path)
        assert lib.weights == [1.0, 1.0, 1.0]
        # Trim to length sanity.
        assert len(lib.weights) == len(lib.files)

    @pytest.mark.parametrize("count", [1, 5, 50])
    def test_weights_track_file_count(self, tmp_path: Path, count: int) -> None:
        for i in range(count):
            (tmp_path / f"f{i}.mp3").write_bytes(b"")
        lib = LinerLibrary.from_folder(tmp_path)
        assert len(lib.weights) == count
