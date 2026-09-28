"""Tests for autodj.daypart: the wall-clock BPM/energy targets."""

from __future__ import annotations

import pytest

from autodj.daypart import daypart_target


@pytest.mark.parametrize(
    "hour,bpm",
    [
        (6, 80.0),
        (9, 80.0),
        (10, 105.0),
        (13, 105.0),
        (14, 115.0),
        (17, 115.0),
        (18, 128.0),
        (21, 128.0),
        (22, 90.0),
        (23, 90.0),
        (0, 90.0),
        (5, 90.0),
    ],
)
def test_each_hour_gets_its_dayparts_target(hour: int, bpm: float) -> None:
    assert daypart_target(hour)[0] == bpm


def test_target_is_bpm_weight_energy() -> None:
    assert daypart_target(19) == (128.0, 0.3, 0.10)
