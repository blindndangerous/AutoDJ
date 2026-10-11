"""Uploaded Stereo Tool presets stay inside the station's data folder."""

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from autodj.mastering import BLOCK_FRAMES, ContinuousMaster
from autodj.stereotool_presets import (
    INCLUDED_PATH,
    MAX_PRESET_BYTES,
    PresetInUseError,
    delete_preset,
    list_presets,
    preset_folder,
    preset_path,
    store_preset,
    validate_preset_name,
)


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int = -1) -> bytes:
        return self.data[:size]


@pytest.mark.asyncio
async def test_upload_list_and_delete(tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    data = b"[Processing]\nOutput Gain=0\n"
    target = await store_preset(active, "Night.sts", Reader(data))
    assert target.read_bytes() == data
    assert [item["name"] for item in list_presets(active)] == ["included", "Night.sts"]
    assert preset_path(active, "Night.sts") == target
    with pytest.raises(FileExistsError):
        await store_preset(active, "Night.sts", Reader(data))
    delete_preset(active, "Night.sts")
    assert [item["name"] for item in list_presets(active)] == ["included"]


@pytest.mark.asyncio
async def test_upload_rejects_bad_names_and_contents(tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    for name in ("../outside.sts", "C:\\outside.sts", "CON.sts", "not.txt"):
        with pytest.raises(ValueError):
            validate_preset_name(name)
        with pytest.raises(ValueError):
            await store_preset(active, name, Reader(b"[A]\nB=1\n"))
    for content in (b"not a preset", b"[A]\nB=\0", b"[A]\nB=1\n" + b"x" * MAX_PRESET_BYTES):
        with pytest.raises(ValueError):
            await store_preset(active, "safe.sts", Reader(content))
    with pytest.raises(ValueError, match="read-only"):
        delete_preset(active, "included")


@pytest.mark.parametrize("name", ["COM0.sts", "lpt0.sts", "nul.sts", "Con .sts", "aux.x.sts"])
def test_every_windows_device_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        validate_preset_name(name)


@pytest.mark.parametrize("name", [".hidden.sts", "a/b.sts", "a:b.sts", "x" * 97 + ".sts", "a.STS "])
def test_names_outside_the_plain_pattern_are_refused(name: str) -> None:
    with pytest.raises(ValueError):
        validate_preset_name(name)


@pytest.mark.asyncio
async def test_active_preset_cannot_be_deleted(tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    await store_preset(active, "Night.sts", Reader(b"[A]\nB=1\n"))
    with pytest.raises(PresetInUseError):
        delete_preset(active, "Night.sts", active="Night.sts")
    if (preset_folder(active) / "NIGHT.STS").exists():  # case-insensitive filesystem
        with pytest.raises(PresetInUseError):
            delete_preset(active, "NIGHT.STS", active="Night.sts")
    assert (preset_folder(active) / "Night.sts").is_file()
    with pytest.raises(FileNotFoundError, match=r"Missing.sts was not found"):
        delete_preset(active, "Missing.sts")


@pytest.mark.asyncio
async def test_upload_never_writes_outside_the_folder_or_leaves_parts(tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    await store_preset(active, "Night.sts", Reader(b"[A]\nB=1\n"))
    with pytest.raises(FileExistsError):
        await store_preset(active, "Night.sts", Reader(b"[A]\nB=2\n"))
    assert (preset_folder(active) / "Night.sts").read_bytes() == b"[A]\nB=1\n"
    assert sorted(path.name for path in preset_folder(active).iterdir()) == ["Night.sts"]
    assert sorted(path.name for path in tmp_path.rglob("*") if path.is_file()) == ["Night.sts"]
    (preset_folder(active) / "included").write_text("[A]\nB=1\n")
    assert [item["name"] for item in list_presets(active)] == ["included", "Night.sts"]
    assert preset_path(active, "included") == INCLUDED_PATH
    assert INCLUDED_PATH.is_file()


def test_master_starts_with_the_selected_preset_and_falls_back_when_it_is_missing(
    tmp_path: Path,
) -> None:
    chosen = tmp_path / "Night.sts"
    chosen.write_text("[Pre Compressor]\nDrive=4\n")
    selected = [chosen]
    commands: list[list[str]] = []

    def factory(command, **_kwargs):
        commands.append(command)
        raise OSError("stop here")

    master = ContinuousMaster(
        popen_factory=factory,
        mode=lambda: "stereotool",
        preset=lambda: selected[0],
        agc_drive_db=lambda: 0,
    )
    try:
        deadline = time.monotonic() + 2
        while not commands and time.monotonic() < deadline:
            time.sleep(0.01)
        assert commands[0][commands[0].index("-s") + 1] == str(chosen)
        selected[0] = tmp_path / "Gone.sts"
        master.process(np.zeros((BLOCK_FRAMES, 2), dtype=np.float32))
        deadline = time.monotonic() + 2
        while len(commands) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert commands[-1][commands[-1].index("-s") + 1] == str(INCLUDED_PATH)
    finally:
        master.close()


def _pre_compressor_drive(data: bytes) -> float:
    text = data.decode("latin-1")
    section = text.split("[Pre Compressor]", 1)[1].split("\n[", 1)[0]
    return float(
        next(
            line.split("=", 1)[1]
            for line in section.splitlines()
            if line.lower().startswith("drive=")
        )
    )


def test_agc_drive_updates_only_selected_preset_copy_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Night.sts"
    original = b"[Pre Compressor]\r\nDrive=4\r\n[Other]\r\nDrive=1\r\n"
    source.write_bytes(original)
    drive = [7.0]
    master = ContinuousMaster(preset=lambda: source, agc_drive_db=lambda: drive[0])
    try:
        boosted = master._runtime_stereotool_preset(master._stereotool_signature())
        assert _pre_compressor_drive(boosted.read_bytes()) == pytest.approx(4 * 10 ** (7 / 20))
        assert source.read_bytes() == original
        drive[0] = 0.0
        assert master._runtime_stereotool_preset(master._stereotool_signature()) == source
        assert source.read_bytes() == original
    finally:
        temp_path = Path(master._preset_temp.name)
        master.close()
    assert not temp_path.exists()


def test_invalid_custom_preset_falls_back_to_included_with_requested_drive(tmp_path: Path) -> None:
    invalid = tmp_path / "Bad.sts"
    invalid.write_text("[Pre Compressor]\nGain=4\n")
    master = ContinuousMaster(preset=lambda: invalid, agc_drive_db=lambda: 7.0)
    try:
        fallback = master._runtime_stereotool_preset(master._stereotool_signature())
        assert fallback != invalid
        assert _pre_compressor_drive(fallback.read_bytes()) == pytest.approx(
            _pre_compressor_drive(INCLUDED_PATH.read_bytes()) * 10 ** (7 / 20)
        )
    finally:
        master.close()


def test_agc_preset_io_runs_on_master_lifecycle_thread(tmp_path: Path) -> None:
    source = tmp_path / "Night.sts"
    source.write_text("[Pre Compressor]\nDrive=4\n")
    caller = threading.get_ident()
    started: list[int] = []

    def factory(command, **_kwargs):
        started.append(threading.get_ident())
        raise OSError("test stop")

    mode = ["off"]
    master = ContinuousMaster(
        popen_factory=factory,
        mode=lambda: mode[0],
        preset=lambda: source,
        agc_drive_db=lambda: 7,
    )
    try:
        mode[0] = "stereotool"
        master.process(np.zeros((BLOCK_FRAMES, 2), dtype=np.float32))
        deadline = time.monotonic() + 2
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started and started[0] != caller
    finally:
        master.close()
