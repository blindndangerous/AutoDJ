"""Turning the AAC and Opus streams on, off and to another bitrate while on air (#50)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from autodj.stream_formats import AAC, MP3, OPUS, StreamFormat
from autodj.stream_mounts import StreamMounts


class FakeOutput:
    def __init__(self, fmt: StreamFormat, bitrate: int, log: list[str]) -> None:
        self.format = fmt
        self.bitrate = bitrate
        self.listener_count = 0
        self.titles: list[tuple[str, str]] = []
        self._log = log
        log.append(f"started {fmt.name} {bitrate}")

    def set_title(self, artist: str, title: str) -> None:
        self.titles.append((artist, title))

    def set_bitrate(self, bitrate: int) -> None:
        self._log.append(f"restarted {self.format.name} {bitrate}")
        self.bitrate = bitrate

    def close(self) -> None:
        self._log.append(f"closed {self.format.name}")


class Rig:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.bus: list[Any] = []
        self.main = FakeOutput(MP3, 320, [])
        self.main.listener_count = 2
        self.mounts = StreamMounts(self.main, self.make, self.attach, self.detach)
        self.fail_make = False
        self.fail_attach = False
        self.fail_detach = False

    def make(self, fmt: StreamFormat, bitrate: int) -> FakeOutput:
        if self.fail_make:
            raise OSError("no ffmpeg")
        return FakeOutput(fmt, bitrate, self.log)

    def attach(self, output: FakeOutput) -> None:
        if self.fail_attach:
            raise RuntimeError("bus refused")
        self.bus.append(output)
        self.log.append(f"attached {output.format.name}")

    def detach(self, output: FakeOutput) -> None:
        if self.fail_detach:
            raise RuntimeError("bus refused")
        self.bus.remove(output)
        self.log.append(f"detached {output.format.name}")


def test_start_turns_on_only_the_formats_with_a_bitrate() -> None:
    rig = Rig()
    rig.mounts.start({"aac": 96, "opus": 0})
    assert rig.log == ["started aac 96", "attached aac"]
    assert rig.mounts.get("mp3") is rig.main
    assert rig.mounts.get("aac") is rig.bus[0]
    assert rig.mounts.get("opus") is None
    assert rig.mounts.bitrate("aac") == 96
    assert rig.mounts.bitrate("opus") == 0
    assert rig.mounts.outputs == rig.bus


def test_a_format_that_cannot_start_stays_off_and_the_others_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.fail_make = True
    with caplog.at_level(logging.ERROR, logger="autodj.stream_mounts"):
        rig.mounts.start({"aac": 96, "opus": 64})
    assert rig.mounts.get("aac") is None and rig.mounts.get("opus") is None
    assert len([r for r in caplog.records if "stays off" in r.message]) == 2


def test_listeners_on_every_format_count_together() -> None:
    rig = Rig()
    rig.mounts.start({"aac": 96, "opus": 64})
    rig.mounts.get("aac").listener_count = 3
    rig.mounts.get("opus").listener_count = 1
    assert rig.mounts.counts() == {"mp3": 2, "aac": 3, "opus": 1}
    assert rig.mounts.listener_count == 6


def test_changing_turning_off_and_on_again() -> None:
    rig = Rig()
    rig.mounts.set_bitrate("opus", 64)
    output = rig.mounts.get("opus")
    rig.mounts.set_bitrate("opus", 64)  # the same: nothing happens
    rig.mounts.set_bitrate("opus", 96)
    assert rig.mounts.get("opus") is output and output.bitrate == 96
    rig.mounts.set_bitrate("opus", 0)
    rig.mounts.set_bitrate("opus", 0)  # already off
    assert rig.mounts.get("opus") is None
    assert rig.log == [
        "started opus 64",
        "attached opus",
        "restarted opus 96",
        "detached opus",
        "closed opus",
    ]
    assert rig.bus == []


def test_a_mount_turned_on_gets_the_song_playing() -> None:
    rig = Rig()
    rig.mounts.set_title("Artist", "Song")
    rig.mounts.set_bitrate("aac", 128)
    rig.mounts.set_title("Artist", "Next")
    assert rig.mounts.get("aac").titles == [("Artist", "Song"), ("Artist", "Next")]


@pytest.mark.parametrize(
    ("name", "bitrate"), [("mp3", 128), ("flac", 128), ("aac", 48), ("opus", 256)]
)
def test_refuses_other_formats_and_bitrates(name: str, bitrate: int) -> None:
    rig = Rig()
    with pytest.raises(ValueError):
        rig.mounts.set_bitrate(name, bitrate)
    assert rig.log == []


def test_an_output_the_bus_refuses_is_closed_not_leaked() -> None:
    rig = Rig()
    rig.fail_attach = True
    with pytest.raises(RuntimeError):
        rig.mounts.set_bitrate("aac", 96)
    assert rig.log == ["started aac 96", "closed aac"]
    assert rig.mounts.get("aac") is None


def test_close_stops_every_mount_even_when_a_step_fails(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig()
    rig.mounts.start({"aac": 96, "opus": 64})
    rig.fail_detach = True
    with caplog.at_level(logging.ERROR, logger="autodj.stream_mounts"):
        rig.mounts.close()
    assert rig.log[-2:] == ["closed aac", "closed opus"]
    assert rig.mounts.outputs == []
    assert rig.mounts.counts() == {"mp3": 2}
    assert len([r for r in caplog.records if "detaching it failed" in r.message]) == 2
    assert AAC.label in caplog.text and OPUS.label in caplog.text
