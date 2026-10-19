"""In-memory audio preparation and cue analysis for radio playback."""

from __future__ import annotations

import logging
import math
import mmap
import os
import re
import shutil
import subprocess  # nosec B404 -- audio processing uses fixed FFmpeg argv, no shell
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from autodj.stereo import (
    ANALYSIS_WINDOW_SECONDS,
    LONG_TRACK_SECONDS,
    SAFE_FFMPEG_INPUT,
    SAMPLE_RATE,
    AudioSpool,
    TrackTooLongError,
    all_finite_and_audible,
    load_stereo,
)

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry

logger = logging.getLogger(__name__)

PREPARATION_VERSION = "loudness-v1"
LINER_PREPARATION_VERSION = "liner-loudness-v2"
TARGET_LUFS = {"music": -28.0, "liner": -28.0, "voice": -28.0}
MIN_GAIN_DB, MAX_GAIN_DB = -60.0, 18.0
_FFMPEG_TIMEOUT_S = 600
_READ_CHUNK_BYTES = 1 << 16
_SILENCE_AMPLITUDE = 10 ** (-60 / 20)
VOICE_FILTER = (
    "highpass=f=70,acompressor=threshold=0.063095734:ratio=2:attack=10:release=180:"
    "makeup=1:knee=4:link=maximum:detection=rms"
)
MASTER_FILTER = (
    "volume=6dB,acompressor=threshold=0.16:ratio=2:attack=30:release=400:"
    "makeup=1:knee=6:link=maximum:detection=rms,volume=9.01dB,"
    "aresample=176400,alimiter=limit=0.87096359:attack=5:release=100:"
    "level=0:latency=1,aresample=44100"
)

_LUFS = re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s* LUFS")


def _ffmpeg_timeout(source: Path) -> float:
    """Seconds an FFmpeg pass over *source* may take: ten minutes, or its length.

    A pass slower than real time is stuck, but an hour-long mix may
    honestly take longer than ten minutes over a slow share.  The length
    comes from the tags' audio information, an estimate that is good
    enough for a time limit.
    """
    from autodj.audio_meta import _open_mutagen

    try:
        seconds = float(getattr(getattr(_open_mutagen(source), "info", None), "length", 0.0))
    except Exception:  # an unreadable header keeps the default; FFmpeg reports the file
        seconds = 0.0
    return max(float(_FFMPEG_TIMEOUT_S), seconds if math.isfinite(seconds) else 0.0)


def _run_ffmpeg(
    command: list[str], timeout: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    timeout = _FFMPEG_TIMEOUT_S if timeout is None else timeout
    try:
        return subprocess.run(  # nosec B603 -- fixed FFmpeg argv, no shell
            command,
            capture_output=True,
            check=False,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out after {timeout:g}s: {command[0]}") from exc


def _pipe_ffmpeg(
    command: list[str], sink: Callable[[bytes], None], timeout: float | None = None
) -> tuple[int, bytes]:
    """Run FFmpeg, handing its stdout to *sink* as it arrives.

    Returns:
        ``(returncode, stderr)``.

    Raises:
        RuntimeError: If FFmpeg ran longer than *timeout* seconds (ten
            minutes when ``None``); it is killed.
    """
    timeout = _FFMPEG_TIMEOUT_S if timeout is None else timeout
    proc = subprocess.Popen(  # nosec B603 -- fixed FFmpeg argv, no shell
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:  # pragma: no cover - PIPE always gives both
        proc.kill()
        raise RuntimeError("ffmpeg pipes unavailable")
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    errors: list[bytes] = []
    # Drain stderr alongside stdout so a chatty decode cannot fill its pipe and stall.
    drain = threading.Thread(target=lambda: errors.append(stderr.read()), daemon=True)
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    drain.start()
    timer.start()
    try:
        while data := stdout.read(_READ_CHUNK_BYTES):
            sink(data)
        returncode = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        drain.join()
        stdout.close()
        stderr.close()
    if timed_out.is_set():
        raise RuntimeError(f"ffmpeg timed out after {timeout:g}s: {command[0]}")
    return returncode, b"".join(errors)


def _stream_ffmpeg(
    command: list[str], timeout: float | None = None
) -> tuple[int, bytearray | mmap.mmap, bytes]:
    """Run FFmpeg and gather its stdout in one growing, writable buffer.

    subprocess.run returns stdout as immutable bytes, and writable samples
    built from that need a second full-size copy: two whole decoded tracks
    in memory at once.  A bytearray can back the samples directly.  Past
    :data:`~autodj.stereo.SPILL_FRAMES` of stereo audio the samples go to
    a temporary file instead, returned mapped into memory
    (:class:`~autodj.stereo.AudioSpool`).
    """
    spool = AudioSpool()
    try:
        returncode, stderr = _pipe_ffmpeg(command, spool.write, timeout)
    except BaseException:
        spool.close()
        raise
    return returncode, spool.buffer(), stderr


def _kind(kind: str) -> float:
    try:
        return TARGET_LUFS[kind]
    except KeyError as exc:
        raise ValueError("kind must be 'music', 'liner' or 'voice'") from exc


def analyze_loudness(source: Path, kind: str = "music") -> dict[str, object]:
    """Measure integrated source loudness for caller-owned metadata caching."""
    _kind(kind)
    source = Path(source)
    before = source.stat()
    filters = _processing_filters(kind)
    result = _run_ffmpeg(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-nostdin",
            *SAFE_FFMPEG_INPUT,
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            f"{filters},ebur128=peak=none",
            "-f",
            "null",
            "-",
        ],
        _ffmpeg_timeout(source),
    )
    log = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"ffmpeg loudness measurement failed: {log[-1000:]}")
    values = _LUFS.findall(log)
    if not values:
        raise ValueError(f"silent or invalid audio: {source}")
    lufs = float(values[-1])
    if not math.isfinite(lufs) or lufs <= -65:
        raise ValueError(f"silent or invalid audio: {source}")
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"source changed during loudness measurement: {source}")
    return _measurement(source, kind, lufs, before)


def loudness_matches(measurement: dict | None, source: Path, kind: str = "music") -> bool:
    """Return whether cached measurement belongs to the current source/settings."""
    if not isinstance(measurement, dict):
        return False
    try:
        target = _kind(kind)
        stat = Path(source).stat()
        lufs, gain = float(measurement["lufs"]), float(measurement["gain_db"])
        return (
            measurement["version"]
            == (PREPARATION_VERSION if kind == "music" else LINER_PREPARATION_VERSION)
            and measurement["kind"] == kind
            and measurement["size"] == stat.st_size
            and measurement["mtime_ns"] == stat.st_mtime_ns
            and math.isfinite(lufs)
            and lufs > -65
            and math.isfinite(gain)
            and MIN_GAIN_DB <= gain <= MAX_GAIN_DB
            and math.isclose(
                gain,
                min(target - lufs, MAX_GAIN_DB) if kind != "music" else target - lufs,
                abs_tol=0.01,
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return False


def load_processed_audio(source: Path, measurement: dict, kind: str = "music") -> np.ndarray:
    """Decode the original source and apply its cached constant loudness gain."""
    _kind(kind)
    if not loudness_matches(measurement, source, kind):
        raise ValueError("stale or invalid loudness measurement")
    source = Path(source)
    gain_db = float(measurement["gain_db"])
    filters = f"{_processing_filters(kind)},volume={gain_db:.9f}dB"
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        *SAFE_FFMPEG_INPUT,
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        filters,
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
    ]
    command += ["-f", "f32le", "pipe:1"]
    returncode, output, stderr = _stream_ffmpeg(command, timeout=_ffmpeg_timeout(source))
    detail = stderr.decode(errors="replace").strip()
    if returncode:
        raise RuntimeError(f"ffmpeg audio processing failed for {source}: {detail[-1000:]}")
    if detail:
        logger.warning(
            "Decoded %s with warnings:\n%s", source, detail, extra={"track": str(source)}
        )
    # The buffer is writable, so the samples use it in place with no copy.
    if len(output) % 8:
        raise ValueError("Incomplete stereo PCM frame")
    audio = np.ndarray((len(output) // 8, 2), dtype="<f4", buffer=output)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (measurement["size"], measurement["mtime_ns"]):
        raise RuntimeError(f"source changed during audio decode: {source}")
    if not all_finite_and_audible(audio):
        raise ValueError("empty, silent or invalid processed audio")
    return audio


def _processing_filters(kind: str) -> str:
    filters = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if kind == "liner":
        filters += ",highpass=f=70"
    elif kind == "voice":
        filters += f",{VOICE_FILTER}"
    return filters


def _measurement(source: Path, kind: str, lufs: float, before: os.stat_result) -> dict:
    """Validate an integrated loudness and return the cacheable measurement."""
    if not math.isfinite(lufs) or lufs <= -65:
        raise ValueError(f"silent or invalid audio: {source}")
    gain_db = _kind(kind) - lufs
    if kind != "music":
        gain_db = min(gain_db, MAX_GAIN_DB)
    if not MIN_GAIN_DB <= gain_db <= MAX_GAIN_DB:
        raise ValueError(f"required gain outside safe range: {source}")
    return {
        "version": PREPARATION_VERSION if kind == "music" else LINER_PREPARATION_VERSION,
        "kind": kind,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "lufs": lufs,
        "gain_db": gain_db,
    }


def load_and_measure_audio(
    source: Path,
    kind: str = "music",
    max_seconds: float | None = None,
    *,
    input_format: str | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Decode and measure *source* in one FFmpeg run, for a loudness cache miss.

    The same as :func:`analyze_loudness` followed by
    :func:`load_processed_audio`, with one decode instead of two: the
    ``ebur128`` filter passes the audio through unchanged while it
    measures, and the gain is applied here once the integrated loudness
    is known.  FFmpeg's ``volume`` filter multiplies float samples by the
    float32 gain too, so the samples are the same.  *input_format* names the
    demuxer, so FFmpeg probes nothing (an uploaded break, #88).

    Returns:
        ``(audio, measurement)``: the processed audio and the measurement
        :func:`load_processed_audio` takes, for the caller to cache.
    """
    _kind(kind)
    source = Path(source)
    before = source.stat()
    # metadata=1 moves ebur128's per-frame lines to verbose; its summary stays at info.
    filters = f"{_processing_filters(kind)},ebur128=peak=none:metadata=1"
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-nostdin",
        "-loglevel",
        "level+info",
        *SAFE_FFMPEG_INPUT,
        *(("-f", input_format) if input_format else ()),
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        filters,
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
    ]
    if max_seconds is not None:
        command += ["-t", str(max_seconds + 1.0)]
    command += ["-f", "f32le", "pipe:1"]
    returncode, output, stderr = _stream_ffmpeg(command, timeout=_ffmpeg_timeout(source))
    log = stderr.decode(errors="replace")
    # What load_processed_audio's "-v error" would have shown.
    detail = "\n".join(
        line for line in log.splitlines() if re.search(r"\[(error|fatal|panic)\]", line)
    ).strip()
    if returncode:
        raise RuntimeError(f"ffmpeg audio processing failed for {source}: {detail[-1000:]}")
    values = _LUFS.findall(log)
    if not values:
        raise ValueError(f"silent or invalid audio: {source}")
    measurement = _measurement(source, kind, float(values[-1]), before)
    if detail:
        logger.warning(
            "Decoded %s with warnings:\n%s", source, detail, extra={"track": str(source)}
        )
    if len(output) % 8:
        raise ValueError("Incomplete stereo PCM frame")
    audio = np.ndarray((len(output) // 8, 2), dtype="<f4", buffer=output)
    if max_seconds is not None and len(audio) > max_seconds * SAMPLE_RATE:
        raise TrackTooLongError(str(source), len(audio) / SAMPLE_RATE)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RuntimeError(f"source changed during audio decode: {source}")
    # The gain load_processed_audio hands FFmpeg (to nine decimals), as float32.
    gain = np.float32(10 ** (float(f"{float(measurement['gain_db']):.9f}") / 20))
    audio *= gain
    if not all_finite_and_audible(audio):
        raise ValueError("empty, silent or invalid processed audio")
    return audio, measurement


def _ffprobe_seconds(source: Path) -> float | None:
    """The container duration FFmpeg's ffprobe reports, or ``None``."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(  # nosec B603 -- fixed ffprobe argv, no shell
            [
                ffprobe,
                "-v",
                "error",
                *SAFE_FFMPEG_INPUT,
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            check=False,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        seconds = float(result.stdout.decode(errors="replace").strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def estimated_seconds(source: Path) -> float | None:
    """The length the file's header gives, estimates included, or ``None``.

    Good enough to tell a long track from a short one; the analysis of a
    long track measures its exact length as it decodes.
    """
    from autodj.audio_meta import _open_mutagen

    try:
        seconds = float(getattr(getattr(_open_mutagen(source), "info", None), "length", 0.0))
    except Exception:  # unreadable tags: ask FFmpeg instead
        seconds = 0.0
    if math.isfinite(seconds) and seconds > 0:
        return seconds
    return _ffprobe_seconds(source)


@dataclass(frozen=True)
class TrackWindows:
    """A long track's start and end, measured in one streaming decode.

    Attributes:
        head: The first :data:`ANALYSIS_WINDOW_SECONDS` as ``(frames, 2)``.
        tail: The last :data:`ANALYSIS_WINDOW_SECONDS` as ``(frames, 2)``.
        tail_start: The frame of the whole track where *tail* begins.
        frames: The whole track's length in frames, as decoded.
        energy: The RMS of the whole track's mono mix, measured as it decoded.
        first_audible: First whole-track frame at or above -60 dBFS, if any.
        last_audible: Frame after the last one at or above -60 dBFS, if any.
    """

    head: np.ndarray
    tail: np.ndarray
    tail_start: int
    frames: int
    energy: float
    first_audible: int | None
    last_audible: int | None

    @property
    def seconds(self) -> float:
        """The whole track's length in seconds."""
        return self.frames / SAMPLE_RATE

    @property
    def tail_start_s(self) -> float:
        """Where *tail* begins, in seconds of the whole track."""
        return self.tail_start / SAMPLE_RATE


class _WindowSink:
    """Keep a decode's start and a rolling end, and its mono energy."""

    def __init__(self, window_frames: int, whole_frames: int, long_header: bool = False) -> None:
        self.window = window_frames
        self.whole = min(whole_frames, 2 * window_frames) if long_header else whole_frames
        self.pending = b""
        self.start: list[np.ndarray] = []
        self.head: np.ndarray | None = None
        self.tail: deque[np.ndarray] = deque()
        self.tail_frames = 0
        self.frames = 0
        self.sum_squares = 0.0
        self.first_audible: int | None = None
        self.last_audible: int | None = None

    def __call__(self, data: bytes) -> None:
        data = self.pending + data
        usable = len(data) - len(data) % 8
        self.pending = data[usable:]
        if not usable:
            return
        chunk = np.frombuffer(data[:usable], dtype="<f4").reshape(-1, 2)
        audible = np.flatnonzero(np.any(np.abs(chunk) >= _SILENCE_AMPLITUDE, axis=1))
        if len(audible):
            if self.first_audible is None:
                self.first_audible = self.frames + int(audible[0])
            self.last_audible = self.frames + int(audible[-1]) + 1
        channel_mean = chunk.mean(axis=1, dtype=np.float64)
        self.sum_squares += float(np.dot(channel_mean, channel_mean))
        self.frames += len(chunk)
        if self.head is None:
            self.start.append(chunk)
            if self.frames <= self.whole:
                return
            # Past the whole-track limit: keep only the start and a rolling end.
            start = np.concatenate(self.start)
            self.start = []
            self.head = start[: self.window].copy()
            self._keep(start[-self.window :])
            return
        self._keep(chunk)

    def _keep(self, chunk: np.ndarray) -> None:
        self.tail.append(chunk)
        self.tail_frames += len(chunk)
        while self.tail_frames - len(self.tail[0]) >= self.window:
            self.tail_frames -= len(self.tail.popleft())


def read_windows(
    source: Path,
    window_seconds: float | None = None,
    whole_seconds: float | None = None,
) -> np.ndarray | TrackWindows:
    """Decode *source* once, keeping all of a short track or a long one's ends.

    Args:
        source: Audio file.
        window_seconds: How much of each end to keep;
            :data:`~autodj.stereo.ANALYSIS_WINDOW_SECONDS` when ``None``.
        whole_seconds: The longest track kept whole;
            :data:`~autodj.stereo.LONG_TRACK_SECONDS` when ``None``.

    Returns:
        The whole ``(frames, 2)`` track when it is no longer than
        *whole_seconds*, else its :class:`TrackWindows`.

    Raises:
        RuntimeError: If FFmpeg cannot decode it.
    """
    source = Path(source)
    window = ANALYSIS_WINDOW_SECONDS if window_seconds is None else window_seconds
    whole = LONG_TRACK_SECONDS if whole_seconds is None else whole_seconds
    seconds = estimated_seconds(source)
    sink = _WindowSink(
        round(window * SAMPLE_RATE),
        round(whole * SAMPLE_RATE),
        seconds is not None and seconds > whole,
    )
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        *SAFE_FFMPEG_INPUT,
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
        "-f",
        "f32le",
        "pipe:1",
    ]
    returncode, stderr = _pipe_ffmpeg(command, sink, _ffmpeg_timeout(source))
    detail = stderr.decode(errors="replace").strip()
    if returncode:
        raise RuntimeError(f"FFmpeg could not decode {source}: {detail[-1000:] or 'unknown error'}")
    if detail:
        logger.warning(
            "Decoded %s with warnings:\n%s", source, detail, extra={"track": str(source)}
        )
    if sink.head is None:
        if not sink.start:
            return np.zeros((0, 2), dtype=np.float32)
        return np.concatenate(sink.start).astype(np.float32)
    tail = np.concatenate(tuple(sink.tail))[-sink.window :]
    return TrackWindows(
        head=sink.head,
        tail=tail,
        tail_start=sink.frames - len(tail),
        frames=sink.frames,
        energy=math.sqrt(sink.sum_squares / sink.frames),
        first_audible=sink.first_audible,
        last_audible=sink.last_audible,
    )


def load_for_analysis(source: Path) -> np.ndarray | TrackWindows:
    """Decode *source* for :func:`analyze_basic`: all of it, or a long track's ends.

    A track the header says is at most :data:`LONG_TRACK_SECONDS` long is
    decoded whole as stereo at ``SAMPLE_RATE``.  A longer one, or one of
    unknown length, is read by :func:`read_windows`, so its analysis
    holds a few minutes of it, whatever its length.

    """
    seconds = estimated_seconds(Path(source))
    if seconds is not None and seconds <= LONG_TRACK_SECONDS:
        return load_stereo(str(source), SAMPLE_RATE)
    return read_windows(Path(source))


def analyze_basic(
    source: Path,
    kind: str = "music",
    audio: np.ndarray | TrackWindows | None = None,
) -> tuple[IndexEntry, dict[str, float | None]]:
    """Return basic library metadata and conservative timing suggestions.

    Full beat-grid and embedding analysis remain separate background work.
    A track longer than :data:`LONG_TRACK_SECONDS` is analysed from its
    start and end (:class:`TrackWindows`): Start and Intro come from its
    first minutes, End, Segue and Outro from its last, and its length and
    energy from the whole decode.

    Args:
        source: Audio file.
        kind: ``"music"`` or ``"liner"``.
        audio: The track already decoded by :func:`load_for_analysis`, so
            the caller can reuse it; decoded here when ``None``.

    """
    from autodj.audio_meta import load_lrc_for, read_file_tags
    from autodj.indexer import IndexEntry
    from autodj.vocals import first_vocal_s, sung_spans

    if kind not in {"music", "liner"}:
        raise ValueError("kind must be 'music' or 'liner'")
    source = Path(source)
    if audio is None:
        audio = load_for_analysis(source)
    if isinstance(audio, TrackWindows):
        length, energy, found = audio.seconds, audio.energy, _window_points(audio, kind)
    else:
        length = len(audio) / SAMPLE_RATE
        mono = audio.mean(axis=1)
        energy = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
        found = _whole_points(audio, mono, kind)
    start, end, segue, detected_intro, detected_outro = found
    tags = read_file_tags(source)
    lines = load_lrc_for(source)
    spans = sung_spans(lines)
    points = {
        "start": start,
        "end": end,
        "segue": segue,
        "intro": first_vocal_s(spans) if spans else (detected_intro or None),
        "outro": spans[-1][1] if spans else detected_outro if detected_outro < length else None,
    }
    entry = IndexEntry(
        path=str(source),
        title=tags.title or source.stem,
        artist=tags.artist,
        album=tags.album,
        genre=tags.genre,
        bpm=tags.bpm,
        year=tags.year,
        length=length,
        energy=energy,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
        embedded_at=source.stat().st_mtime,
        size=source.stat().st_size,
    )
    return entry, points


_Found = tuple[float, float, float | None, float, float]


def _audible_end(audio: np.ndarray) -> int | None:
    """One past the last frame louder than -60 dBFS, or ``None``."""
    amplitude = np.abs(audio)
    if audio.ndim > 1:
        amplitude = np.max(amplitude, axis=1)
    audible = np.flatnonzero(amplitude >= _SILENCE_AMPLITUDE)
    return int(audible[-1]) + 1 if len(audible) else None


def _padded(mono: np.ndarray) -> np.ndarray:
    """*mono* zero-padded to whole half-second blocks for ``detect_intro_outro``."""
    window = SAMPLE_RATE // 2
    remainder = len(mono) % window
    return np.pad(mono, (0, window - remainder)) if remainder else mono


def _whole_points(audio: np.ndarray, mono: np.ndarray, kind: str) -> _Found:
    """Start, End, Segue and the detected intro and outro of a whole track."""
    from autodj.dj_meta import detect_intro_outro, detect_intro_start

    start = detect_intro_start(audio, SAMPLE_RATE)
    detected_intro, detected_outro = detect_intro_outro(_padded(mono), SAMPLE_RATE)
    last = _audible_end(audio)
    end = last / SAMPLE_RATE if last is not None else 0.0
    segue: float | None
    if kind == "liner":
        segue = _suggest_liner_segue(mono, SAMPLE_RATE)
        if segue is None or segue > end:
            segue = end
    else:
        segue = _suggest_segue(mono, SAMPLE_RATE)
    return start, end, segue, detected_intro, detected_outro


def _window_points(windows: TrackWindows, kind: str) -> _Found:
    """The same points for a long track, from its start and end windows.

    The intro and outro detector sees the two windows joined, so it
    normalises against their loudness; points found in the end window
    are moved to the whole track's timeline.
    """
    from autodj.dj_meta import detect_intro_outro, detect_intro_start

    head_mono = windows.head.mean(axis=1)
    tail_mono = windows.tail.mean(axis=1)
    head_padded = _padded(head_mono)
    head_s = len(head_padded) / SAMPLE_RATE
    offset = windows.tail_start_s - head_s
    start = (
        windows.first_audible / SAMPLE_RATE
        if windows.first_audible is not None and windows.first_audible >= len(windows.head)
        else detect_intro_start(windows.head, SAMPLE_RATE)
    )
    intro, outro = detect_intro_outro(
        np.concatenate((head_padded, _padded(tail_mono))), SAMPLE_RATE
    )
    detected_intro = intro if intro < len(head_mono) / SAMPLE_RATE else 0.0
    detected_outro = outro + offset if outro > head_s else windows.seconds
    last = _audible_end(windows.tail)
    end = (
        (windows.tail_start + last) / SAMPLE_RATE
        if last is not None
        else (windows.last_audible or 0) / SAMPLE_RATE
    )
    segue: float | None
    if kind == "liner":
        found = _suggest_liner_segue(tail_mono, SAMPLE_RATE)
        segue = end if found is None else min(end, windows.tail_start_s + found)
    else:
        found = _suggest_segue(tail_mono, SAMPLE_RATE)
        segue = None if found is None else windows.tail_start_s + found
    return start, end, segue, detected_intro, detected_outro


def _segue_reference(rms: np.ndarray, exclude_tail_bins: int) -> float:
    """Keep trailing silence out of the reference without removing interior pauses."""
    audible = np.flatnonzero(rms >= _SILENCE_AMPLITUDE)
    end = max(0, int(audible[-1]) + 1 - exclude_tail_bins) if len(audible) else 0
    return float(np.median(rms[:end])) if end else 0.0


def _suggest_liner_segue(audio: np.ndarray, sample_rate: int) -> float | None:
    """Find a sustained final content drop in a short liner, never an interior pause."""
    window = max(1, round(sample_rate * 0.05))
    if len(audio) < window * 3:
        return None
    samples = np.nan_to_num(np.asarray(audio, dtype=np.float64), copy=False)
    bins = [samples[start : start + window] for start in range(0, len(samples), window)]
    rms = np.asarray([np.sqrt(np.mean(block * block)) for block in bins], dtype=np.float64)
    duration = len(samples) / sample_rate
    reference = _segue_reference(rms, round(0.5 * sample_rate / window))
    if not math.isfinite(reference) or reference <= 1e-6:
        return None

    below = rms <= reference * 10 ** (-10 / 20)
    index = len(below) - 1
    while index >= 0 and below[index]:
        index -= 1
    first = index + 1
    tail_duration = len(samples) - first * window
    point = first * window / sample_rate
    if tail_duration < sample_rate * 0.15 or point <= 0:
        return None
    return point if point < duration else None


def _suggest_segue(audio: np.ndarray, sample_rate: int) -> float | None:
    """Suggest only a sustained final energy drop, never a vocal cue."""
    window = sample_rate
    last = _audible_end(audio)
    if last is None:
        return None
    # Anchor the bounded window to content, so padding cannot push it out of view.
    tail_start = max(0, last - 60 * sample_rate)
    tail = audio[tail_start : min(len(audio), last + 5 * sample_rate)]
    if len(tail) < 6 * window:
        return None
    rms = np.asarray(
        [
            np.sqrt(np.mean(tail[start : start + window].astype(np.float64) ** 2) + 1e-12)
            for start in range(0, len(tail), window)
        ]
    )
    reference = _segue_reference(rms, 5)
    if reference <= 1e-6:
        return None
    threshold = reference * 10 ** (-10 / 20)
    index = len(rms) - 1
    while index >= 0 and rms[index] <= threshold:
        index -= 1
    point = (tail_start + (index + 1) * window) / sample_rate
    quiet_duration = len(audio) - tail_start - (index + 1) * window
    return (
        point
        if quiet_duration >= 5 * sample_rate and 0 < point < len(audio) / sample_rate
        else None
    )
