"""Turning the AAC and Opus streams on and off, and starting them for a listener (#50)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from autodj.stream import EncoderUnavailableError
from autodj.stream_formats import AAC, MP3, OPUS, StreamFormat
from autodj.stream_mounts import IDLE_SECONDS, MountsClosedError, StreamMounts


class FakeOutput:
    def __init__(self, fmt: StreamFormat, bitrate: int, log: list[str]) -> None:
        self.format = fmt
        self.bitrate = bitrate
        self.listener_count = 0
        self.last_error: str | None = None
        self.titles: list[tuple[str, str]] = []
        self.recovery_stopped = False
        self._log = log
        log.append(f"started {fmt.name} {bitrate}")

    def set_title(self, artist: str, title: str) -> None:
        self.titles.append((artist, title))

    def stop_recovery(self) -> None:
        self.recovery_stopped = True

    def close(self) -> None:
        self._log.append(f"closed {self.format.name}")


class Rig:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.bus: list[Any] = []
        self.now = 0.0
        self.main = FakeOutput(MP3, 320, [])
        self.main.listener_count = 2
        self.fail_make = False
        self.fail_attach = False
        self.fail_detach = False
        self.refuse: set[str] = set()
        self.probed: list[tuple[str, int]] = []
        self.make_hook = lambda: None
        self.mounts = StreamMounts(
            self.main, self.make, self.attach, self.detach, probe=self.probe, clock=lambda: self.now
        )

    def probe(self, fmt: StreamFormat, bitrate: int) -> None:
        self.probed.append((fmt.name, bitrate))
        if fmt.name in self.refuse:
            raise EncoderUnavailableError(f"ffmpeg could not encode {fmt.label}: Unknown encoder")

    def make(self, fmt: StreamFormat, bitrate: int) -> FakeOutput:
        self.make_hook()
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


def test_turning_on_probes_but_starts_no_encoder_until_someone_listens() -> None:
    rig = Rig()
    rig.mounts.start({"aac": 96, "opus": 0})
    assert rig.probed == [("aac", 96)]
    assert rig.log == []
    assert rig.mounts.enabled("aac") and not rig.mounts.enabled("opus")
    assert rig.mounts.enabled("mp3")
    assert rig.mounts.get("aac") is None
    assert rig.mounts.bitrate("aac") == 96 and rig.mounts.bitrate("opus") == 0
    assert rig.mounts.bitrate("mp3") == 320
    output = rig.mounts.acquire("aac")
    assert rig.log == ["started aac 96", "attached aac"]
    assert rig.mounts.get("aac") is output and rig.mounts.acquire("aac") is output
    assert rig.mounts.acquire("mp3") is rig.main
    assert rig.mounts.get("mp3") is rig.main
    assert rig.mounts.acquire("opus") is None
    assert rig.mounts.outputs == [output]


def test_an_encoder_nobody_hears_stops_after_the_idle_time() -> None:
    rig = Rig()
    rig.mounts.apply({"opus": 64})
    output = rig.mounts.acquire("opus")
    output.listener_count = 1
    rig.now = 1000.0
    rig.mounts.tick()  # someone listens: it stays
    output.listener_count = 0
    rig.now += IDLE_SECONDS - 1
    rig.mounts.tick()
    assert rig.mounts.get("opus") is output
    rig.now += 1
    rig.mounts.tick()
    assert rig.mounts.get("opus") is None
    assert rig.log[-2:] == ["detached opus", "closed opus"]
    # Still on: the next listener starts it again.
    assert rig.mounts.enabled("opus")
    assert rig.mounts.acquire("opus") is not None


def test_a_format_ffmpeg_cannot_encode_stays_off_and_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.refuse = {"opus"}
    with caplog.at_level(logging.ERROR, logger="autodj.stream_mounts"):
        rig.mounts.start({"aac": 96, "opus": 64})
    assert rig.mounts.enabled("aac") and not rig.mounts.enabled("opus")
    assert "Could not turn on the Opus stream" in caplog.text
    # Asked together, a refused format changes neither.
    with pytest.raises(EncoderUnavailableError, match="Unknown encoder"):
        rig.mounts.apply({"aac": 0, "opus": 64})
    assert rig.mounts.bitrate("aac") == 96 and rig.mounts.bitrate("opus") == 0


def test_listeners_on_every_format_count_together() -> None:
    rig = Rig()
    rig.mounts.apply({"aac": 96, "opus": 64})
    assert rig.mounts.counts() == {"mp3": 2, "aac": 0, "opus": 0}
    rig.mounts.acquire("aac").listener_count = 3
    rig.mounts.acquire("opus").listener_count = 1
    assert rig.mounts.counts() == {"mp3": 2, "aac": 3, "opus": 1}
    assert rig.mounts.listener_count == 6


def test_a_new_bitrate_or_off_stops_the_running_encoder() -> None:
    rig = Rig()
    assert rig.mounts.apply({"opus": 64}) == ["opus"]
    rig.mounts.acquire("opus")
    assert rig.mounts.apply({"opus": 64}) == []  # the same: nothing happens
    assert rig.mounts.apply({"opus": 96}) == ["opus"]
    assert rig.mounts.get("opus") is None and rig.mounts.bitrate("opus") == 96
    assert rig.mounts.acquire("opus").bitrate == 96
    rig.mounts.set_bitrate("opus", 0)
    assert not rig.mounts.enabled("opus")
    assert rig.log == [
        "started opus 64",
        "attached opus",
        "detached opus",
        "closed opus",
        "started opus 96",
        "attached opus",
        "detached opus",
        "closed opus",
    ]
    assert rig.bus == []
    assert rig.probed == [("opus", 64), ("opus", 96)]


def test_a_started_encoder_gets_the_song_playing() -> None:
    rig = Rig()
    rig.mounts.apply({"aac": 128})
    rig.mounts.set_title("Artist", "Song")
    output = rig.mounts.acquire("aac")
    rig.mounts.set_title("Artist", "Next")
    assert output.titles == [("Artist", "Song"), ("Artist", "Next")]


def test_the_mix_never_waits_while_an_encoder_starts() -> None:
    """set_title, get and the counts return while ffmpeg is still starting for a listener."""
    rig = Rig()
    rig.mounts.apply({"aac": 96})
    starting, release = threading.Event(), threading.Event()

    def slow_start() -> None:
        starting.set()
        assert release.wait(5)

    rig.make_hook = slow_start
    worker = threading.Thread(target=rig.mounts.acquire, args=("aac",))
    worker.start()
    try:
        assert starting.wait(2)
        done = threading.Event()

        def mix_thread() -> None:
            rig.mounts.set_title("Artist", "Song")
            rig.mounts.get("aac")
            rig.mounts.counts()
            done.set()

        threading.Thread(target=mix_thread).start()
        assert done.wait(1), "set_title waited on the encoder starting"
    finally:
        release.set()
        worker.join(5)
    assert rig.mounts.get("aac").titles == [("Artist", "Song")]


@pytest.mark.parametrize(
    ("name", "bitrate"), [("mp3", 128), ("flac", 128), ("aac", 48), ("opus", 256)]
)
def test_refuses_other_formats_and_bitrates(name: str, bitrate: int) -> None:
    rig = Rig()
    with pytest.raises(ValueError):
        rig.mounts.apply({name: bitrate})
    assert rig.log == [] and rig.probed == []


def test_a_start_that_fails_leaves_nothing_behind() -> None:
    rig = Rig()
    rig.mounts.apply({"aac": 96})
    rig.fail_attach = True
    with pytest.raises(RuntimeError):
        rig.mounts.acquire("aac")
    assert rig.log == ["started aac 96", "closed aac"]
    assert rig.mounts.get("aac") is None
    rig.fail_attach = False
    rig.fail_make = True
    with pytest.raises(OSError):
        rig.mounts.acquire("aac")


def test_no_orphan_encoder_when_the_station_stops_during_a_start() -> None:
    rig = Rig()
    rig.mounts.apply({"aac": 96})
    rig.make_hook = rig.mounts.close
    assert rig.mounts.acquire("aac") is None
    assert rig.log == ["started aac 96", "closed aac"]


def test_shutdown_does_not_wait_for_an_inflight_mount_factory() -> None:
    """ADR-0004-17. Registry shutdown marks extra formats without waiting for startup."""
    rig = Rig()
    rig.mounts.apply({"aac": 96})
    starting, release = threading.Event(), threading.Event()

    def slow_start() -> None:
        starting.set()
        release.wait(5)

    rig.make_hook = slow_start
    result: list[Any] = []
    worker = threading.Thread(target=lambda: result.append(rig.mounts.acquire("aac")))
    worker.start()
    try:
        assert starting.wait(2)
        start = time.monotonic()
        rig.mounts.stop_recovery()
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        worker.join(5)
    assert result == [None]
    assert rig.log == ["started aac 96", "closed aac"]
    assert rig.bus == []
    with pytest.raises(MountsClosedError):
        rig.mounts.apply({"opus": 64})
    assert rig.mounts.acquire("aac") is None


def test_no_orphan_encoder_when_the_format_changes_during_a_start() -> None:
    rig = Rig()
    rig.mounts.apply({"aac": 96})
    rig.make_hook = lambda: rig.mounts.apply({"aac": 0})
    assert rig.mounts.acquire("aac") is None
    assert rig.bus == []
    assert rig.log[-1] == "closed aac"


def test_close_stops_every_encoder_even_when_a_step_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.mounts.apply({"aac": 96, "opus": 64})
    rig.mounts.acquire("aac")
    rig.mounts.acquire("opus")
    rig.fail_detach = True
    with caplog.at_level(logging.ERROR, logger="autodj.stream_mounts"):
        rig.mounts.close()
    assert rig.log[-2:] == ["closed aac", "closed opus"]
    assert rig.mounts.outputs == []
    assert rig.mounts.counts() == {"mp3": 2}
    assert len([r for r in caplog.records if "detaching it failed" in r.message]) == 2
    assert AAC.label in caplog.text and OPUS.label in caplog.text


def test_the_last_encoder_error_is_shown_while_it_runs() -> None:
    rig = Rig()
    rig.mounts.apply({"opus": 64})
    assert rig.mounts.last_error("opus") is None
    rig.mounts.acquire("opus").last_error = "Error while opening encoder"
    assert rig.mounts.last_error("opus") == "Error while opening encoder"
