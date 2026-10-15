"""Run Thimeo's Stereo Tool plugin library in a child process of its own.

``python -m autodj.stereotool_host --library L --preset P --home H`` loads
the library with :mod:`ctypes` and turns it into a pipe, so the mastering
code drives it exactly as it drives FFmpeg: 32-bit float, stereo,
44.1 kHz, interleaved, in on stdin and out on stdout.  A crash in Thimeo's
code ends only this process; the master then bypasses and restarts it.

Stdin starts with a header, then audio:

- the 6 bytes ``ADST1\\n``;
- the licence key's length as a 4-byte little-endian number (0 for none,
  at most 4096);
- the key itself, ASCII.

So the key is never on a command line or in the environment.  It is
handed to ``stereoTool_Create3`` and the buffers that held it are zeroed.

Stdout carries only audio.  Anything the library prints is sent to stderr,
(the library prints ALSA and JACK complaints there, and more), where
AutoDJ's own report lines start with ``AUTODJ-STEREOTOOL `` and hold
one JSON object: ``{"version": "11.05", "latency_ms": 12.3,
"preset_load_type": 20386}`` once the preset is loaded, ``{"licensed": true, "unlicensed": null}`` once some
audio has passed, or ``{"error": "..."}`` before it exits.  Every other
stderr line is the library's and is ignored.  Nothing here prints the key.

The C API is Thimeo's ``libStereoTool.h`` (Generic plugin SDK, 11.05);
Liquidsoap's ``src/modules/stereotool/stereotool.ml`` calls the same
functions.  Exit codes: 0 at the end of the audio, 2 bad arguments or
header, 3 the library is missing or is not Stereo Tool's, 4 the preset
did not load.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol, TextIO

MAGIC = b"ADST1\n"
MAX_KEY_BYTES = 4096
REPORT_PREFIX = "AUTODJ-STEREOTOOL "
SAMPLE_RATE = 44100
CHANNELS = 2
#: Frames per call; the master writes whole blocks of this size.
BLOCK_FRAMES = 1024
_BLOCK_BYTES = BLOCK_FRAMES * CHANNELS * 4
#: ID_SAVE_ALLSETTINGS in ParameterEnum.h of the 11.05 SDK (ID_BAND1 = 20000,
#: then 386 entries): every processing setting, but not configuration such
#: as the web interface.  Older SDKs numbered it 10386 (Liquidsoap still
#: does); that is tried when this one is refused.
LOAD_ALL_SETTINGS = 20386
_OLD_LOAD_ALL_SETTINGS = 10386
#: About one second of audio before the licence can be checked.
_LICENCE_CHECK_BLOCKS = 44


def encode_header(key: str | None) -> bytearray:
    """The stdin header for *key*; the caller zeroes it once written."""
    data = (key or "").encode("ascii")
    if len(data) > MAX_KEY_BYTES:
        raise ValueError("the licence key is too long")
    header = bytearray(MAGIC)
    header += struct.pack("<I", len(data))
    header += data
    return header


def zero(buffer: bytearray) -> None:
    """Overwrite a buffer that held the key."""
    buffer[:] = bytes(len(buffer))


def read_header(stream: BinaryIO) -> bytearray:
    """Read the header; return the key bytes (empty for none).

    Raises:
        ValueError: The header is missing or malformed.
    """
    head = _read_exactly(stream, len(MAGIC) + 4)
    if head is None or head[: len(MAGIC)] != MAGIC:
        raise ValueError("no AutoDJ header on stdin")
    (length,) = struct.unpack("<I", head[len(MAGIC) :])
    if length > MAX_KEY_BYTES:
        raise ValueError("the licence key in the header is too long")
    key = _read_exactly(stream, length) if length else bytearray()
    if key is None:
        raise ValueError("the header ended early")
    return key


def _read_exactly(stream: BinaryIO, size: int) -> bytearray | None:
    """Read *size* bytes, or ``None`` if the stream ends first."""
    buffer = bytearray(size)
    view = memoryview(buffer)
    got = 0
    while got < size:
        count = stream.readinto(view[got:])  # type: ignore[attr-defined]
        if not count:
            return None
        got += count
    return buffer


class Engine(Protocol):
    """What the pipe loop needs from Stereo Tool."""

    def process(self, samples: ctypes.Array[ctypes.c_float], frames: int) -> None: ...

    def licence(self) -> tuple[bool, str | None]: ...

    def close(self) -> None: ...


class LibraryError(RuntimeError):
    """The library could not be loaded, or is not Stereo Tool's."""


class StereoTool:
    """One Stereo Tool instance from the plugin library, through ctypes.

    Raises:
        LibraryError: The library is missing or lacks Stereo Tool's functions.
        ValueError: The preset did not load.
    """

    def __init__(
        self,
        library: str,
        key: bytearray,
        preset: str,
        home: str,
        *,
        loader: Callable[[str], ctypes.CDLL] | None = None,
    ) -> None:
        lib = (loader or _load)(library)
        try:
            self._bind(lib)
        except AttributeError as exc:
            raise LibraryError(
                f"{Path(library).name} is not Thimeo's Stereo Tool plugin library ({exc})"
            ) from None
        self._lib = lib
        if hasattr(lib, "stereoTool_global_setBindIp"):
            # A preset can turn on Stereo Tool's web and UECP servers; keep
            # them off the network, on this computer only.
            lib.stereoTool_global_setBindIp(b"127.0.0.1", b"127.0.0.1")
        secret = ctypes.create_string_buffer(bytes(key), len(key) + 1) if key else None
        try:
            if hasattr(lib, "stereoTool_Create3"):
                # No GUI, its own settings name and home, and no .ini read.
                handle = lib.stereoTool_Create3(False, secret, b"autodj", _c_path(home), False)
            else:
                handle = lib.stereoTool_Create(secret)
        finally:
            if secret is not None:
                ctypes.memset(secret, 0, len(secret))
            zero(key)
        if not handle:
            raise LibraryError("Stereo Tool did not start")
        self._handle = handle
        path = _c_path(preset)
        #: The load type the preset loaded with (20386 or the older 10386).
        self.load_type = next(
            (
                load_type
                for load_type in (LOAD_ALL_SETTINGS, _OLD_LOAD_ALL_SETTINGS)
                if lib.stereoTool_LoadPreset(handle, path, load_type)
            ),
            None,
        )
        if self.load_type is None:
            self.close()
            raise ValueError(f"Stereo Tool could not load the preset {Path(preset).name}")

    @staticmethod
    def _bind(lib: ctypes.CDLL) -> None:
        c_bool, c_int, c_int32 = ctypes.c_bool, ctypes.c_int, ctypes.c_int32
        handle, text = ctypes.c_void_p, ctypes.c_char_p
        signatures: dict[str, tuple[list[object], object]] = {
            "stereoTool_Create": ([text], handle),
            "stereoTool_Delete": ([handle], None),
            "stereoTool_Process": (
                [handle, ctypes.POINTER(ctypes.c_float), c_int32, c_int32, c_int32],
                None,
            ),
            "stereoTool_LoadPreset": ([handle, text, c_int], c_bool),
            "stereoTool_CheckLicenseValid": ([handle], c_bool),
            "stereoTool_GetUnlicensedUsedFeatures": ([handle, text, c_int], c_bool),
            "stereoTool_GetSoftwareVersion": ([], c_int),
            "stereoTool_GetLatency2": ([handle, c_int32, c_bool], c_int),
        }
        optional: dict[str, tuple[list[object], object]] = {
            "stereoTool_Create3": ([c_bool, text, text, text, c_bool], handle),
            "stereoTool_global_setBindIp": ([text, text], None),
        }
        for name, (args, result) in signatures.items():
            function = getattr(lib, name)
            function.argtypes = args
            function.restype = result
        for name, (args, result) in optional.items():
            function = getattr(lib, name, None)
            if function is not None:
                function.argtypes = args
                function.restype = result

    def version(self) -> str | None:
        """The library's version, such as ``11.05``; see :func:`software_version`."""
        return software_version(int(self._lib.stereoTool_GetSoftwareVersion()))

    def latency_frames(self) -> int:
        """Frames of delay the preset adds; feeds a little silence to find out."""
        return max(0, int(self._lib.stereoTool_GetLatency2(self._handle, SAMPLE_RATE, True)))

    def process(self, samples: ctypes.Array[ctypes.c_float], frames: int) -> None:
        self._lib.stereoTool_Process(self._handle, samples, frames, CHANNELS, SAMPLE_RATE)

    def licence(self) -> tuple[bool, str | None]:
        """Whether the licence covers the preset, and what it does not cover."""
        valid = bool(self._lib.stereoTool_CheckLicenseValid(self._handle))
        text = ctypes.create_string_buffer(1024)
        covered = self._lib.stereoTool_GetUnlicensedUsedFeatures(self._handle, text, len(text))
        missing = None if covered else text.value.decode("utf-8", "replace").strip() or None
        return valid and covered, missing

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._lib.stereoTool_Delete(handle)


def software_version(number: int) -> str | None:
    """``11.05`` from what ``stereoTool_GetSoftwareVersion`` returns.

    The header's example is ``912`` for 9.12, but the 11.05 library
    returns ``11050``: two digits of minor version and one more digit, a
    patch number shown only when it is not 0.
    """
    if number <= 0:
        return None
    if number < 10000:
        return f"{number // 100}.{number % 100:02d}"
    major, rest = divmod(number, 1000)
    minor, patch = divmod(rest, 10)
    return f"{major}.{minor:02d}" + (f".{patch}" if patch else "")


def _load(library: str) -> ctypes.CDLL:
    if not os.path.isfile(library):
        raise LibraryError(f"{Path(library).name} is not there")
    try:
        if os.name == "nt":
            return ctypes.CDLL(library)
        return ctypes.CDLL(library, mode=getattr(os, "RTLD_NOW", 2))
    except OSError as exc:
        raise LibraryError(f"{Path(library).name} could not be loaded: {exc}") from None


def _c_path(path: str) -> bytes:
    """A path as the library's char* wants it: the ANSI code page on Windows."""
    return path.encode("mbcs") if os.name == "nt" else os.fsencode(path)


def report(stream: TextIO, **values: object) -> None:
    """Write one report line for the master (never the key)."""
    stream.write(REPORT_PREFIX + json.dumps(values, separators=(",", ":")) + "\n")
    stream.flush()


def pump(
    engine: Engine,
    source: BinaryIO,
    sink: BinaryIO,
    status: TextIO,
    *,
    skip_frames: int = 0,
) -> None:
    """Process whole blocks from *source* to *sink* until *source* ends.

    The first *skip_frames* output frames (the preset's own delay, silence)
    are dropped, so the output is in time with the input like the
    command-line program's was.  The licence is checked once, after about
    a second of audio.
    """
    samples = (ctypes.c_float * (BLOCK_FRAMES * CHANNELS))()
    view = memoryview(samples).cast("B")
    skip_bytes = skip_frames * CHANNELS * 4
    blocks = 0
    while True:
        got = 0
        while got < _BLOCK_BYTES:
            count = source.readinto(view[got:])  # type: ignore[attr-defined]
            if not count:
                return
            got += count
        engine.process(samples, BLOCK_FRAMES)
        out = view
        if skip_bytes:
            cut = min(skip_bytes, _BLOCK_BYTES)
            out, skip_bytes = view[cut:], skip_bytes - cut
        if len(out):
            sink.write(out)
            sink.flush()
        blocks += 1
        if blocks == _LICENCE_CHECK_BLOCKS:
            licensed, missing = engine.licence()
            report(status, licensed=licensed, unlicensed=missing)


def _audio_out() -> BinaryIO:
    """Keep stdout for audio only: the library's own prints go to stderr."""
    sys.stdout.flush()
    audio = os.dup(1)
    os.dup2(2, 1)
    # Buffered, so each write is whole; pump flushes after every block.
    return os.fdopen(audio, "wb")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m autodj.stereotool_host")
    parser.add_argument("--library", required=True)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--home", required=True)
    args = parser.parse_args(argv)
    status = sys.stderr
    source = sys.stdin.buffer
    try:
        key = read_header(source)
    except ValueError as exc:
        report(status, error=str(exc))
        return 2
    sink = _audio_out()
    try:
        engine = StereoTool(args.library, key, args.preset, args.home)
    except LibraryError as exc:
        report(status, error=str(exc), missing=not os.path.isfile(args.library))
        return 3
    except ValueError as exc:
        report(status, error=str(exc))
        return 4
    finally:
        zero(key)
    try:
        latency = engine.latency_frames()
        report(
            status,
            version=engine.version(),
            latency_ms=round(latency * 1000 / SAMPLE_RATE, 1),
            preset_load_type=engine.load_type,
        )
        pump(engine, source, sink, status, skip_frames=latency)
    except (BrokenPipeError, OSError):
        pass
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
