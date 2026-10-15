"""Time check voice packs: folders of hour and minute files joined like RadioBOSS.

A voice pack is a folder listed in ``[time_checks] voice_packs``; its name in
Settings is the folder name.  Only files directly in the folder count, and only
these names (letters compared without regard to case, extension ``.wav``,
``.mp3``, ``.flac`` or ``.ogg``):

- an *hour file*: letters, spaces, ``_`` or ``-`` that include ``h`` and no
  ``m``, then two digits 00 to 23 (``hr_05.mp3``, ``H05.wav``, ``hour-17.ogg``);
  it says "it's 5" and is followed by a minute file;
- an *exact-hour file*: an hour file name with ``o`` after the digits, straight
  after them or after one space, ``_`` or ``-`` (``hr_05o.mp3``, ``hrs05_o.mp3``);
  it says "it's 5 o'clock" and plays alone at minute 00;
- a *minute file*: letters, spaces, ``_`` or ``-`` that include ``m`` and no
  ``h``, then two digits 00 to 59 (``m_56.mp3``, ``min 07.wav``);
- ``intro``, ``outro``, ``am`` and ``pm`` (``intro.wav``, ``PM.mp3``).

Any other audio file is reported and not used: a name with both or neither of
``h`` and ``m`` before the number, a number out of range, a minute with ``o``,
or two files for the same piece (both are dropped rather than one guessed).

The pieces for one minute are decoded, trimmed of leading and trailing
silence, matched in loudness and joined with a short equal-power crossfade by
the pure functions below; :class:`JoinCache` keeps recent joins in memory.
"""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autodj.stereo import SAMPLE_RATE

AUDIO_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg")
EXTRAS = ("intro", "outro", "am", "pm")
_TIME_NAME = re.compile(r"([a-z _-]*)(\d{2})((?:[ _-]?o)?)")
_LABELS = {"hour": "hour", "exact": "exact hour", "minute": "minute"}

#: Frames quieter than this, relative to the piece's own peak, are silence.
#: Speech onsets and fricatives sit 20 to 35 dB under the peak, so -45 dB
#: only takes breath noise, room tone and the padding recorders leave.
SILENCE_DB = -45.0
#: Envelope frame for the silence and loudness measurements.
FRAME_MS = 5
#: Kept before the first frame above the threshold, for soft onsets.
LEAD_MS = 10
#: Kept after the last frame above the threshold, for the word's decay.
TAIL_MS = 30
#: The crossfade between two pieces.  It equals LEAD_MS + TAIL_MS, so it
#: covers only the kept margins: the outgoing word is at full level until its
#: last loud frame and the incoming word reaches full level at its first loud
#: frame, with no silent gap and no speech under the fade.  Much shorter (under
#: 20 ms) would cut the decay audibly; much longer (over 60 ms) would blend
#: speech and smear the consonant that starts the minute.
OVERLAP_MS = 40
#: Fade at the outer ends, so a file that starts or ends abruptly cannot click.
EDGE_FADE_MS = 3
#: Loudness matching never moves a piece more than this.
MAX_GAIN_DB = 12.0
#: The joined voice peaks no higher than this.
PEAK_CEILING = 0.98
#: Memory for joined time checks: about 30 checks of three seconds.
CACHE_BYTES = 32 * 1024 * 1024


def _ms(milliseconds: float, sample_rate: int) -> int:
    return round(sample_rate * milliseconds / 1000)


def _frame_levels(audio: np.ndarray, frame: int) -> np.ndarray:
    """RMS of each *frame*-long block, the louder channel."""
    count = -(-len(audio) // frame)
    padded = np.zeros((count * frame, audio.shape[1]), dtype=np.float64)
    padded[: len(audio)] = audio
    blocks = padded.reshape(count, frame, audio.shape[1])
    levels: np.ndarray = np.sqrt(np.mean(blocks**2, axis=1)).max(axis=1)
    return levels


def _gate(audio: np.ndarray) -> float:
    return float(np.abs(audio).max()) * 10 ** (SILENCE_DB / 20)


def trim_silence(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Cut leading and trailing silence, keeping a short lead-in and decay.

    Only the ends move: a pause inside the speech stays.  Returns an empty
    array for a silent piece.
    """
    if not len(audio) or not np.any(audio):
        return audio[:0]
    frame = max(1, _ms(FRAME_MS, sample_rate))
    gate = _gate(audio)
    # Never empty: the frame holding the peak has an RMS of at least
    # peak / sqrt(frame), far above the gate.
    loud = np.flatnonzero(_frame_levels(audio, frame) >= gate)
    # The frames find the speech; the first and last samples over the gate
    # inside them place the cut exactly, so the margins are LEAD_MS and
    # TAIL_MS and not up to a frame longer.
    envelope = np.abs(audio).max(axis=1)
    first, last = int(loud[0]) * frame, int(loud[-1]) * frame
    onset = first + int(np.argmax(envelope[first : first + frame] >= gate))
    tail = envelope[last : last + frame] >= gate
    offset = last + len(tail) - int(np.argmax(tail[::-1]))
    start = max(0, onset - _ms(LEAD_MS, sample_rate))
    end = min(len(audio), offset + _ms(TAIL_MS, sample_rate))
    return audio[start:end]


def speech_level_db(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    """Mean power of the frames above the silence threshold, in dBFS."""
    frame = max(1, _ms(FRAME_MS, sample_rate))
    levels = _frame_levels(audio, frame)
    active = levels[levels >= _gate(audio)]
    power = float(np.mean(active**2)) if active.size else 0.0
    return 10 * float(np.log10(power)) if power > 0 else -np.inf


def match_loudness(pieces: list[np.ndarray], sample_rate: int = SAMPLE_RATE) -> list[np.ndarray]:
    """Bring every piece to the pieces' mean speech level.

    Each gain is limited to :data:`MAX_GAIN_DB`, and all gains drop together
    when a piece would peak above :data:`PEAK_CEILING`.
    """
    levels = [speech_level_db(piece, sample_rate) for piece in pieces]
    finite = [level for level in levels if np.isfinite(level)]
    if not finite:
        return list(pieces)
    target = float(np.mean(finite))
    gains = np.array(
        [
            np.clip(target - level, -MAX_GAIN_DB, MAX_GAIN_DB) if np.isfinite(level) else 0.0
            for level in levels
        ]
    )
    peaks = [float(np.abs(piece).max()) if len(piece) else 0.0 for piece in pieces]
    loudest = max(peak * 10 ** (gain / 20) for peak, gain in zip(peaks, gains, strict=True))
    if loudest > PEAK_CEILING:
        gains -= 20 * np.log10(loudest / PEAK_CEILING)
    return [
        (piece * 10 ** (gain / 20)).astype(np.float32)
        for piece, gain in zip(pieces, gains, strict=True)
    ]


def crossfade_join(pieces: list[np.ndarray], overlap: int) -> np.ndarray:
    """Join pieces end to start, overlapping by *overlap* frames.

    The fade is equal-power (cosine out, sine in), so uncorrelated material
    keeps a steady level through the seam.  The overlap shrinks to half the
    shorter piece when a piece is very short.
    """
    joined = pieces[0]
    for piece in pieces[1:]:
        span = min(overlap, len(joined) // 2, len(piece) // 2)
        if span <= 0:
            joined = np.concatenate([joined, piece])
            continue
        phase = (np.arange(span) + 0.5) / span * (np.pi / 2)
        seam = joined[-span:] * np.cos(phase)[:, None] + piece[:span] * np.sin(phase)[:, None]
        joined = np.concatenate([joined[:-span], seam, piece[span:]])
    return joined


def fade_edges(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Raised-cosine fade over the first and last :data:`EDGE_FADE_MS`."""
    span = min(_ms(EDGE_FADE_MS, sample_rate), len(audio) // 2)
    if span <= 0:
        return audio
    ramp = 0.5 - 0.5 * np.cos((np.arange(span) + 0.5) / span * np.pi)
    faded = audio.copy()
    faded[:span] *= ramp[:, None]
    faded[-span:] *= ramp[::-1, None]
    return faded


def join_voice(pieces: list[np.ndarray], sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Trim, loudness-match and crossfade decoded ``(frames, 2)`` pieces.

    Raises:
        ValueError: No pieces, or a piece is silent.
    """
    if not pieces:
        raise ValueError("no pieces to join")
    trimmed = [trim_silence(piece, sample_rate) for piece in pieces]
    if any(not len(piece) for piece in trimmed):
        raise ValueError("a piece is silent")
    matched = match_loudness(trimmed, sample_rate)
    joined = crossfade_join(matched, _ms(OVERLAP_MS, sample_rate))
    return fade_edges(joined, sample_rate).astype(np.float32)


# ---------------------------------------------------------------------------
# File names and coverage
# ---------------------------------------------------------------------------


def classify(name: str) -> tuple[str, int] | str | None:
    """What a file name is in a pack.

    Returns:
        ``(kind, number)`` with kind ``hour``, ``exact`` or ``minute``, or
        ``(extra, 0)`` for ``intro``, ``outro``, ``am`` and ``pm``; a reason
        string for an audio file that is not used; ``None`` for a file that is
        not audio.
    """
    path = Path(name)
    if path.suffix.lower() not in AUDIO_EXTENSIONS or name.startswith("."):
        return None
    stem = path.stem.lower()
    if stem in EXTRAS:
        return stem, 0
    match = _TIME_NAME.fullmatch(stem)
    if match is None:
        return "not an hour, minute, intro, outro, am or pm name"
    letters, digits, o_suffix = match.groups()
    number = int(digits)
    if ("h" in letters) == ("m" in letters):
        return "needs h (hour) or m (minute) before the number, not both or neither"
    if "h" in letters:
        if number > 23:
            return "hour over 23"
        return ("exact" if o_suffix else "hour"), number
    if o_suffix:
        return "o after a minute number"
    if number > 59:
        return "minute over 59"
    return "minute", number


@dataclass(frozen=True)
class VoicePack:
    """The usable files of one pack folder and the files it does not use."""

    name: str
    found: bool
    hours: dict[int, Path]
    exact: dict[int, Path]
    minutes: dict[int, Path]
    extras: dict[str, Path]
    problems: tuple[str, ...]


def scan_pack(folder: Path) -> VoicePack:
    """Read a pack folder's file names (not the audio)."""
    tables: dict[str, dict[int | str, list[str]]] = {
        "hour": {},
        "exact": {},
        "minute": {},
        "extra": {},
    }
    problems: list[str] = []
    try:
        # scandir gives each entry's type with the listing, so a folder on a
        # share is one request, not one more per file.
        with os.scandir(folder) as listing:
            entries = sorted(folder / entry.name for entry in listing if entry.is_file())
    except OSError:
        return VoicePack(folder.name, False, {}, {}, {}, {}, ())
    for entry in entries:
        kind = classify(entry.name)
        if kind is None:
            continue
        if isinstance(kind, str):
            problems.append(f"{entry.name}: {kind}")
            continue
        table, key = ("extra", kind[0]) if kind[0] in EXTRAS else (kind[0], kind[1])
        tables[table].setdefault(key, []).append(entry.name)
    usable: dict[str, dict[int | str, Path]] = {}
    for table, keys in tables.items():
        usable[table] = {}
        for key, names in keys.items():
            if len(names) == 1:
                usable[table][key] = folder / names[0]
                continue
            label = key if isinstance(key, str) else f"{_LABELS[table]} {key:02d}"
            problems.append(f"{' and '.join(names)}: more than one file for {label}, none used")
    extras = {str(key): path for key, path in usable["extra"].items()}
    halves = [key for key in ("am", "pm") if key in extras]
    if len(halves) == 1:
        problems.append(f"{extras.pop(halves[0]).name}: {halves[0]} without the other, not used")
    return VoicePack(
        folder.name,
        True,
        {int(key): path for key, path in usable["hour"].items()},
        {int(key): path for key, path in usable["exact"].items()},
        {int(key): path for key, path in usable["minute"].items()},
        extras,
        tuple(problems),
    )


def _hour_file(table: dict[int, Path], hour: int, hour_format: str) -> Path | None:
    """The file for a 24-hour *hour*; in 12-hour mode 13 falls back to 01 and 00 to 12."""
    if hour in table:
        return table[hour]
    return table.get(hour % 12 or 12) if hour_format == "12" else None


def pieces_for(pack: VoicePack, hour: int, minute: int, hour_format: str) -> list[Path] | None:
    """The files that say *hour*:*minute* in order, or ``None`` when one is missing.

    Minute 00 is the exact-hour file alone, or the hour file and ``m_00``
    when there is no exact-hour file.  In 12-hour mode ``am`` or ``pm``
    follows when the pack has both.
    """
    body: list[Path] | None = None
    hour_file = _hour_file(pack.hours, hour, hour_format)
    if minute == 0 and (exact := _hour_file(pack.exact, hour, hour_format)):
        body = [exact]
    elif hour_file is not None and minute in pack.minutes:
        body = [hour_file, pack.minutes[minute]]
    if body is None:
        return None
    if hour_format == "12" and "am" in pack.extras:
        body.append(pack.extras["am" if hour < 12 else "pm"])
    return [
        *([pack.extras["intro"]] if "intro" in pack.extras else []),
        *body,
        *([pack.extras["outro"]] if "outro" in pack.extras else []),
    ]


def _numbers(values: list[int]) -> str:
    """``[1, 2, 3, 7]`` as ``01 to 03, 07``; ``none`` when empty."""
    if not values:
        return "none"
    runs: list[list[int]] = []
    for value in values:
        if runs and value == runs[-1][-1] + 1:
            runs[-1].append(value)
        else:
            runs.append([value])
    return ", ".join(
        f"{run[0]:02d}" if len(run) == 1 else f"{run[0]:02d} to {run[-1]:02d}" for run in runs
    )


@dataclass(frozen=True)
class Coverage:
    """Which minutes a pack can say, and why the others are missing."""

    minutes_covered: int
    missing_hours: list[int]
    missing_exact_hours: list[int]
    missing_minutes: list[int]

    def describe(self, pack: VoicePack) -> str:
        """Plain sentences for doctor and Settings; no paths."""
        if not pack.found:
            return "Folder not found."
        parts = [
            f"{self.minutes_covered} of 1440 minutes covered.",
            f"Hour files missing: {_numbers(self.missing_hours)}.",
            f"Exact-hour files missing: {_numbers(self.missing_exact_hours)}.",
            f"Minute files missing: {_numbers(self.missing_minutes)}.",
        ]
        if pack.extras:
            parts.append(f"Also uses: {', '.join(key for key in EXTRAS if key in pack.extras)}.")
        if pack.problems:
            parts.append(f"Not used: {'; '.join(pack.problems)}.")
        return " ".join(parts)


def coverage(pack: VoicePack, hour_format: str) -> Coverage:
    """Count the minutes *pack* covers in *hour_format*.

    Minute 00 needs no ``m_00`` file, so missing minutes are 01 to 59.
    """
    return Coverage(
        sum(
            pieces_for(pack, hour, minute, hour_format) is not None
            for hour in range(24)
            for minute in range(60)
        ),
        [hour for hour in range(24) if _hour_file(pack.hours, hour, hour_format) is None],
        [hour for hour in range(24) if _hour_file(pack.exact, hour, hour_format) is None],
        [minute for minute in range(1, 60) if minute not in pack.minutes],
    )


Signature = tuple[tuple[str, int, int], ...]


def signature(paths: list[Path]) -> Signature:
    """Path, size and modification time of each piece, to notice a changed file.

    Raises:
        OSError: A file went away.
    """
    stats = [(path, path.stat()) for path in paths]
    return tuple((str(path), stat.st_size, stat.st_mtime_ns) for path, stat in stats)


class JoinCache:
    """Joined voices per pack and minute, least recently used first out.

    An entry is used only while its files' :func:`signature` is unchanged, so
    replacing, adding or removing a pack file is seen at the next check.
    """

    def __init__(self, max_bytes: int = CACHE_BYTES) -> None:
        self.max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, int, int, str], tuple[Signature, np.ndarray]] = (
            OrderedDict()
        )
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[str, int, int, str], current: Signature) -> np.ndarray | None:
        """The cached voice for *key*, unless its files changed since."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry[0] != current:
                self._drop(key)
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def put(self, key: tuple[str, int, int, str], current: Signature, audio: np.ndarray) -> None:
        """Keep a read-only copy, evicting the oldest entries over the cap."""
        if audio.nbytes > self.max_bytes:
            return
        stored = audio.copy()
        stored.flags.writeable = False
        with self._lock:
            if key in self._entries:
                self._drop(key)
            self._entries[key] = (current, stored)
            self._bytes += stored.nbytes
            while self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))

    def _drop(self, key: tuple[str, int, int, str]) -> None:
        self._bytes -= self._entries.pop(key)[1].nbytes
