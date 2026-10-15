"""The Stereo Tool host process (autodj.stereotool_host), with a stand-in library.

Thimeo's real library is never loaded here: a Python object plays its C
functions, so the tests check what AutoDJ hands it and when.
"""

from __future__ import annotations

import ctypes
import io
import json
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from autodj import stereotool_host as host

KEY = "<ABCD-1234-SECRET-KEY-5678>"


def test_the_header_carries_the_key_and_reads_back() -> None:
    header = host.encode_header(KEY)
    assert bytes(header[:6]) == b"ADST1\n"
    assert struct.unpack("<I", header[6:10]) == (len(KEY),)
    key = host.read_header(io.BytesIO(bytes(header) + b"audio follows"))
    assert key.decode() == KEY
    host.zero(header)
    assert set(header) == {0}
    assert host.read_header(io.BytesIO(bytes(host.encode_header(None)))) == bytearray()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"ADST1\n",
        b"NOPE!\n" + struct.pack("<I", 0),
        b"ADST1\n" + struct.pack("<I", 9) + b"abc",
    ],
)
def test_a_bad_header_is_refused(data: bytes) -> None:
    with pytest.raises(ValueError):
        host.read_header(io.BytesIO(data))


def test_a_key_too_long_is_refused_both_ways() -> None:
    with pytest.raises(ValueError):
        host.encode_header("x" * (host.MAX_KEY_BYTES + 1))
    data = b"ADST1\n" + struct.pack("<I", host.MAX_KEY_BYTES + 1)
    with pytest.raises(ValueError, match="too long"):
        host.read_header(io.BytesIO(data))


class _Function:
    """Plays one C function: records its calls, answers with *result*."""

    def __init__(self, calls: list, name: str, result: object = None) -> None:
        self.calls, self.name, self.result = calls, name, result
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        recorded = []
        for arg in args:
            if isinstance(arg, ctypes.Array):
                recorded.append(bytes(arg))
            else:
                recorded.append(arg)
        self.calls.append((self.name, recorded))
        result = self.result
        return result(*args) if callable(result) else result


def _library(*, create3: bool = True, presets: tuple[bool, ...] = (True,), licensed: bool = True):
    calls: list = []
    answers = iter(presets)

    def unlicensed(_handle, text, _size):
        if licensed:
            return True
        ctypes.memmove(text, b"Declipper, 5-band AGC\0", 22)
        return False

    functions = {
        "stereoTool_Create": 1234,
        "stereoTool_Delete": None,
        "stereoTool_Process": lambda handle, samples, frames, channels, rate: None,
        "stereoTool_LoadPreset": lambda *_args: next(answers),
        "stereoTool_CheckLicenseValid": licensed,
        "stereoTool_GetUnlicensedUsedFeatures": unlicensed,
        "stereoTool_GetSoftwareVersion": 11050,  # what 11.05 really returns
        "stereoTool_GetLatency2": 441,
        "stereoTool_global_setBindIp": None,
    }
    if create3:
        functions["stereoTool_Create3"] = 5678
    lib = SimpleNamespace(
        **{name: _Function(calls, name, value) for name, value in functions.items()}
    )
    return lib, calls


def test_stereo_tool_starts_with_the_key_on_this_computer_only_and_zeroes_it() -> None:
    lib, calls = _library()
    key = bytearray(KEY.encode())
    engine = host.StereoTool(
        "/lib/libStereoTool.so", key, "/presets/Night.sts", "/home/st", loader=lambda _p: lib
    )
    names = [name for name, _args in calls]
    # The web and UECP servers are bound to this computer before Stereo Tool starts.
    assert names[:3] == [
        "stereoTool_global_setBindIp",
        "stereoTool_Create3",
        "stereoTool_LoadPreset",
    ]
    assert calls[0][1] == [b"127.0.0.1", b"127.0.0.1"]
    has_gui, secret, name, home, load_ini = calls[1][1]
    assert (has_gui, name, home, load_ini) == (False, b"autodj", b"/home/st", False)
    assert secret == KEY.encode() + b"\0"
    # The buffer handed to Create3 held the key and is zeroed afterwards.
    assert set(key) == {0}
    assert calls[2][1][1:] == [b"/presets/Night.sts", host.LOAD_ALL_SETTINGS]
    assert engine.version() == "11.05"
    assert engine.load_type == host.LOAD_ALL_SETTINGS
    assert engine.latency_frames() == 441
    assert engine.licence() == (True, None)
    engine.close()
    engine.close()
    assert [name for name, _args in calls].count("stereoTool_Delete") == 1
    assert lib.stereoTool_Process.argtypes[1] is ctypes.POINTER(ctypes.c_float)
    assert lib.stereoTool_LoadPreset.restype is ctypes.c_bool


def test_an_old_library_without_create3_uses_create() -> None:
    lib, calls = _library(create3=False)
    host.StereoTool("lib.so", bytearray(b"<k>"), "p.sts", "h", loader=lambda _p: lib)
    assert "stereoTool_Create" in [name for name, _args in calls]


def test_the_older_load_type_is_tried_when_the_new_one_is_refused() -> None:
    lib, calls = _library(presets=(False, True))
    engine = host.StereoTool("lib.so", bytearray(), "p.sts", "h", loader=lambda _p: lib)
    loads = [args[2] for name, args in calls if name == "stereoTool_LoadPreset"]
    assert loads == [host.LOAD_ALL_SETTINGS, 10386]
    assert engine.load_type == 10386


def test_a_preset_that_will_not_load_stops_stereo_tool() -> None:
    lib, calls = _library(presets=(False, False))
    with pytest.raises(ValueError, match=r"could not load the preset Night.sts"):
        host.StereoTool("lib.so", bytearray(), "/x/Night.sts", "h", loader=lambda _p: lib)
    assert "stereoTool_Delete" in [name for name, _args in calls]


def test_a_library_without_stereo_tools_functions_is_refused() -> None:
    with pytest.raises(host.LibraryError, match="not Thimeo's Stereo Tool plugin library"):
        host.StereoTool(
            "/x/libother.so", bytearray(), "p", "h", loader=lambda _p: SimpleNamespace()
        )


def test_a_missing_library_is_refused(tmp_path) -> None:
    with pytest.raises(host.LibraryError, match="is not there"):
        host.StereoTool(str(tmp_path / "gone.so"), bytearray(), "p", "h")


def test_an_unlicensed_preset_says_what_is_not_covered() -> None:
    lib, _calls = _library(licensed=False)
    engine = host.StereoTool("lib.so", bytearray(), "p.sts", "h", loader=lambda _p: lib)
    assert engine.licence() == (False, "Declipper, 5-band AGC")


class _Halver:
    """An engine that halves the audio and counts licence checks."""

    def __init__(self) -> None:
        self.checks = 0

    def process(self, samples, frames: int) -> None:
        array = np.ctypeslib.as_array(samples)
        array *= 0.5

    def licence(self) -> tuple[bool, str | None]:
        self.checks += 1
        return False, "Declipper"

    def close(self) -> None:
        pass


def test_pump_processes_whole_blocks_skips_the_delay_and_reports_the_licence() -> None:
    blocks = host._LICENCE_CHECK_BLOCKS + 2
    audio = np.full(blocks * host.BLOCK_FRAMES * 2, 0.8, dtype=np.float32)
    source = io.BytesIO(audio.tobytes() + b"\0" * 100)  # a partial block at the end is dropped
    sink = io.BytesIO()
    status = io.StringIO()
    engine = _Halver()
    host.pump(engine, source, sink, status, skip_frames=100)
    out = np.frombuffer(sink.getvalue(), dtype=np.float32)
    assert len(out) == (blocks * host.BLOCK_FRAMES - 100) * 2
    assert np.allclose(out, 0.4)
    assert engine.checks == 1
    line = status.getvalue().strip()
    assert line.startswith(host.REPORT_PREFIX)
    assert json.loads(line[len(host.REPORT_PREFIX) :]) == {
        "licensed": False,
        "unlicensed": "Declipper",
    }


def test_main_refuses_a_missing_header(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"no header")))
    assert host.main(["--library", "l", "--preset", "p", "--home", "h"]) == 2
    report = capsys.readouterr().err
    assert report.startswith(host.REPORT_PREFIX)
    assert "error" in json.loads(report[len(host.REPORT_PREFIX) :])


def test_main_says_a_missing_library_and_never_the_key(monkeypatch, capsys, tmp_path) -> None:
    header = bytes(host.encode_header(KEY))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(header)))
    monkeypatch.setattr(host, "_audio_out", io.BytesIO)
    library = str(tmp_path / "libStereoTool_intel64.so")
    assert host.main(["--library", library, "--preset", "p", "--home", "h"]) == 3
    err = capsys.readouterr().err
    values = json.loads(err[err.index(host.REPORT_PREFIX) + len(host.REPORT_PREFIX) :])
    assert values["missing"] is True
    assert "is not there" in values["error"]
    assert KEY not in err


@pytest.mark.parametrize(
    ("number", "version"),
    [
        (11050, "11.05"),
        (11051, "11.05.1"),
        (10920, "10.92"),
        (1105, "11.05"),
        (912, "9.12"),
        (0, None),
    ],
)
def test_the_software_version_reads_as_thimeo_writes_it(number: int, version: str | None) -> None:
    assert host.software_version(number) == version
