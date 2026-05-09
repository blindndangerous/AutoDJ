"""Tag, album art, and lyric extraction helpers for AutoDJ.

Wraps :mod:`mutagen` to expose three small APIs used at play time:

- :func:`read_replaygain` — return the ReplayGain track-gain (dB) + peak
  embedded in the file (if any), used by the player to normalise loudness
  across tracks.
- :func:`read_cover_art` — return the embedded cover image bytes + MIME type
  for the web UI's now-playing card.
- :func:`load_lrc_for` — return parsed timestamped lyrics from a sibling
  ``.lrc`` file, used by the web UI scrolling-lyrics panel.

All three return ``None`` (or empty list) when the data is missing.  None
of them raise on malformed files — broken tags are treated as "no tag".

Example:
    >>> from autodj.audio_meta import read_replaygain, read_cover_art, load_lrc_for
    >>> rg = read_replaygain("song.flac")
    >>> rg
    ReplayGain(track_gain_db=-6.5, track_peak=0.98)
    >>> art = read_cover_art("song.flac")
    >>> art and (art.mime_type, len(art.data))
    ('image/jpeg', 234112)
    >>> lyrics = load_lrc_for("song.flac")
    >>> lyrics[0]
    LyricLine(time_s=12.3, text='Hello darkness my old friend')
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ReplayGain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayGain:
    """ReplayGain tag values for one track.

    Attributes:
        track_gain_db: Recommended gain in decibels (e.g. ``-6.5``).
            Apply as a linear multiplier ``10 ** (gain_db / 20)``.
        track_peak: Sample peak in the original file (0.0–1.0+).  Used
            with the gain to compute a clip-safe applied gain.
    """

    track_gain_db: float
    track_peak: float


_GAIN_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _parse_gain_string(s: str) -> float | None:
    """Parse a ReplayGain string like ``"-6.50 dB"`` into a float (dB)."""
    m = _GAIN_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _open_mutagen(audio_path: str | Path) -> Any:
    """Return a mutagen File object, or ``None`` on any failure."""
    try:
        import mutagen
        from mutagen import File as MutagenFile
    except ImportError:
        return None
    try:
        return MutagenFile(str(audio_path))
    except (OSError, ValueError, TypeError, mutagen.MutagenError):
        return None


def _rg_from_vorbis(m: Any) -> tuple[str | None, str | None]:
    """Read ReplayGain from Vorbis / APEv2 plain tags."""
    for key_gain, key_peak in (
        ("replaygain_track_gain", "replaygain_track_peak"),
        ("REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK"),
    ):
        if hasattr(m, "get") and m.get(key_gain):
            try:
                gain = str(m.get(key_gain)[0])
                peak = str(m.get(key_peak)[0]) if m.get(key_peak) else None
                return gain, peak
            except (IndexError, TypeError):
                return None, None
    return None, None


def _rg_from_id3(m: Any) -> tuple[str | None, str | None]:
    """Read ReplayGain from ID3v2 TXXX frames."""
    if not (hasattr(m, "tags") and m.tags is not None):
        return None, None
    for frame_key in ("TXXX:replaygain_track_gain", "TXXX:REPLAYGAIN_TRACK_GAIN"):
        try:
            frame = m.tags.get(frame_key)
            if not (frame and frame.text):
                continue
            gain = str(frame.text[0])
            peak_key = (
                frame_key[:-5] + "_peak"
                if frame_key.endswith("_gain")
                else frame_key[:-5] + "_PEAK"
            )
            peak_frame = m.tags.get(peak_key)
            peak = str(peak_frame.text[0]) if peak_frame and peak_frame.text else None
            return gain, peak
        except (AttributeError, IndexError, TypeError):
            continue
    return None, None


def _decode_mp4_atom(value: Any) -> str:
    """Decode an MP4 atom value (bytes or scalar) into a string."""
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _rg_from_mp4(m: Any) -> tuple[str | None, str | None]:
    """Read ReplayGain from MP4 ``----:com.apple.iTunes:replaygain_*`` atoms."""
    if not (hasattr(m, "tags") and m.tags is not None):
        return None, None
    gain_str: str | None = None
    peak_str: str | None = None
    for k in list(m.tags.keys()):
        kl = k.lower()
        try:
            if "replaygain_track_gain" in kl:
                gain_str = _decode_mp4_atom(m.tags[k][0])
            elif "replaygain_track_peak" in kl:
                peak_str = _decode_mp4_atom(m.tags[k][0])
        except (IndexError, AttributeError, UnicodeDecodeError):
            continue
    return gain_str, peak_str


def _coerce_peak(peak_str: str | None) -> float:
    """Parse a ReplayGain peak string; default 1.0 on parse error."""
    if peak_str is None:
        return 1.0
    try:
        return float(_GAIN_RE.search(peak_str).group(1))  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return 1.0


def read_replaygain(audio_path: str | Path) -> ReplayGain | None:
    """Read embedded ReplayGain track-gain + peak tags from an audio file."""
    m = _open_mutagen(audio_path)
    if m is None:
        return None
    gain_str, peak_str = _rg_from_vorbis(m)
    if gain_str is None:
        gain_str, peak_str = _rg_from_id3(m)
    if gain_str is None:
        gain_str, peak_str = _rg_from_mp4(m)
    if gain_str is None:
        return None
    gain_db = _parse_gain_string(gain_str)
    if gain_db is None:
        return None
    return ReplayGain(track_gain_db=gain_db, track_peak=_coerce_peak(peak_str))


def replaygain_multiplier(
    rg: ReplayGain | None,
    target_db: float = -14.0,
    max_clip_safe_gain: float = 1.0,
) -> float:
    """Convert a :class:`ReplayGain` reading into a clip-safe linear gain factor.

    Implements the standard "ReplayGain 2.0 reference loudness" approach:
    the file's track gain is offset to *target_db* (the player's preferred
    output loudness, default −14 LUFS-ish to match streaming services), then
    clamped so the resulting peak does not exceed *max_clip_safe_gain*.

    Args:
        rg: ReplayGain tag, or ``None`` (returns 1.0 — no change).
        target_db: Desired output reference level in dB.  Higher = louder
            output overall.  ``-18.0`` = original ReplayGain reference
            (quiet); ``-14.0`` ≈ Spotify/YouTube reference (default).
        max_clip_safe_gain: Hard cap on the linear gain so peaks never
            exceed this fraction of full-scale (default 1.0 = no clipping).

    Returns:
        A linear gain multiplier in ``(0.0, max_clip_safe_gain]``.
        ``1.0`` is returned when *rg* is ``None``.
    """
    if rg is None:
        return 1.0
    # Adjust the file's gain so it lands at target_db
    applied_db = rg.track_gain_db + (target_db - (-18.0))
    linear = 10.0 ** (applied_db / 20.0)
    # Clip-safe: scale down so peak * linear <= max_clip_safe_gain
    if rg.track_peak > 0:
        safe_cap = max_clip_safe_gain / rg.track_peak
        linear = min(linear, safe_cap)
    return max(0.0, linear)


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverArt:
    """Embedded cover image extracted from an audio file.

    Attributes:
        data: Raw image bytes (JPEG or PNG).
        mime_type: MIME type, e.g. ``"image/jpeg"``.
    """

    data: bytes
    mime_type: str


def _cover_from_flac(m: Any) -> CoverArt | None:
    """Return cover art from a FLAC METADATA_BLOCK_PICTURE list."""
    pictures = getattr(m, "pictures", None)
    if not pictures:
        return None
    pic = pictures[0]
    return CoverArt(data=bytes(pic.data), mime_type=str(pic.mime or "image/jpeg"))


def _cover_from_apic(tags: Any) -> CoverArt | None:
    """Return cover art from an ID3v2 APIC frame."""
    for key in list(tags.keys()) if hasattr(tags, "keys") else []:
        if isinstance(key, str) and key.startswith("APIC"):
            frame = tags[key]
            try:
                return CoverArt(
                    data=bytes(frame.data),
                    mime_type=str(getattr(frame, "mime", "image/jpeg")),
                )
            except AttributeError:
                continue
    return None


def _cover_from_mp4(tags: Any) -> CoverArt | None:
    """Return cover art from an MP4 ``covr`` atom."""
    if "covr" not in tags:
        return None
    try:
        cover = tags["covr"][0]
        mime = "image/png" if getattr(cover, "imageformat", 13) == 14 else "image/jpeg"
        return CoverArt(data=bytes(cover), mime_type=mime)
    except (IndexError, AttributeError, TypeError):
        return None


def read_cover_art(audio_path: str | Path) -> CoverArt | None:
    """Return the embedded cover image bytes from an audio file, if any."""
    m = _open_mutagen(audio_path)
    if m is None:
        return None
    flac = _cover_from_flac(m)
    if flac is not None:
        return flac
    tags = getattr(m, "tags", None)
    if tags is None:
        return None
    return _cover_from_apic(tags) or _cover_from_mp4(tags)


# ---------------------------------------------------------------------------
# LRC lyrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LyricLine:
    """A single timestamped lyric line.

    Attributes:
        time_s: Start time within the track in seconds.
        text: The lyric text (may be empty for instrumental sections).
    """

    time_s: float
    text: str


_LRC_TIMESTAMP_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text: str) -> list[LyricLine]:
    """Parse an LRC-format string into timestamped :class:`LyricLine` entries.

    Handles the standard LRC syntax::

        [mm:ss.xx]Lyric line text
        [00:12.30][00:45.10]Repeated chorus line  # multiple stamps per line

    Metadata tags like ``[ar:Artist]`` and ``[ti:Title]`` are skipped.
    Lines without a timestamp are skipped.  The result is sorted by time.

    Args:
        text: Raw LRC file contents.

    Returns:
        List of :class:`LyricLine`, sorted by ``time_s`` ascending.
    """
    out: list[LyricLine] = []
    for line in text.splitlines():
        stamps = _LRC_TIMESTAMP_RE.findall(line)
        if not stamps:
            continue
        # The lyric text is whatever follows the last timestamp
        body = _LRC_TIMESTAMP_RE.sub("", line).strip()
        for mm, ss in stamps:
            try:
                t = int(mm) * 60.0 + float(ss)
            except ValueError:
                continue
            out.append(LyricLine(time_s=t, text=body))
    out.sort(key=lambda x: x.time_s)
    return out


def load_lrc_for(audio_path: str | Path) -> list[LyricLine]:
    """Load and parse the sibling ``.lrc`` file for an audio track.

    Looks for ``<basename>.lrc`` next to the audio file (the most common
    convention used by music players and lyric tools).

    Args:
        audio_path: Path to the audio file.

    Returns:
        List of :class:`LyricLine`.  Empty list if no sidecar exists or
        the file is unreadable.
    """
    p = Path(audio_path)
    lrc = p.with_suffix(".lrc")
    if not lrc.exists():
        return []
    try:
        text = lrc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse_lrc(text)


@dataclass(frozen=True)
class FileTags:
    """Minimal tag set read from a file's ID3 / Vorbis / MP4 atoms.

    Used by the indexer when beets isn't configured — gives proper
    track metadata instead of falling back to filename-derived fields.

    Attributes:
        title: Track title, or empty string.
        artist: Primary artist, or empty string.
        album: Album name, or empty string.
        genre: Genre string, or empty string.
        bpm: Beats per minute (often missing → 0.0).
        year: Release year (often missing → 0).
        length: Duration in seconds (audio info, always populated).
    """

    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    bpm: float = 0.0
    year: int = 0
    length: float = 0.0


def read_file_tags(audio_path: str | Path) -> FileTags:
    """Read embedded title / artist / album / genre / year / BPM via mutagen.

    Best-effort fallback for users without a beets database.  Handles
    the three common tag containers:

    - **Vorbis** (FLAC, OGG): plain tag keys (TITLE, ARTIST, ALBUM, GENRE, BPM, DATE)
    - **ID3** (MP3): TIT2 / TPE1 / TALB / TCON / TBPM / TDRC frames
    - **MP4** (M4A): atom keys ©nam, ©ART, ©alb, ©gen, tmpo, ©day

    Args:
        audio_path: Path to the audio file.

    Returns:
        :class:`FileTags` with whatever could be read.  Missing fields
        are left at their type-appropriate zero values.
    """
    try:
        import mutagen
        from mutagen import File as MutagenFile
    except ImportError:
        return FileTags()

    try:
        m = MutagenFile(str(audio_path))
    except (OSError, ValueError, TypeError, mutagen.MutagenError):
        return FileTags()
    if m is None:
        return FileTags()

    title = _first_tag(m, "title", "TIT2", "\xa9nam")
    artist = _first_tag(m, "artist", "TPE1", "\xa9ART")
    album = _first_tag(m, "album", "TALB", "\xa9alb")
    genre = _first_tag(m, "genre", "TCON", "\xa9gen")
    bpm_raw = _first_tag(m, "bpm", "TBPM", "tmpo")
    year_raw = _first_tag(m, "date", "TDRC", "\xa9day", "year")

    bpm = 0.0
    if bpm_raw:
        try:
            bpm = float(bpm_raw)
        except (TypeError, ValueError):
            bpm = 0.0

    year = 0
    if year_raw:
        # Year can be "2024-05-01" or "2024" — take the first 4 digits.
        try:
            year = int(str(year_raw)[:4])
        except (TypeError, ValueError):
            year = 0

    length = 0.0
    info = getattr(m, "info", None)
    if info is not None:
        length = float(getattr(info, "length", 0.0) or 0.0)

    return FileTags(
        title=title,
        artist=artist,
        album=album,
        genre=genre,
        bpm=bpm,
        year=year,
        length=length,
    )


def _vorbis_get(m: Any, key: str) -> str | None:
    """Read *key* from Vorbis / dict-like tag access; ``None`` when absent."""
    getter = getattr(m, "get", None)
    if not callable(getter):
        return None
    try:
        v = getter(key)
    except (TypeError, AttributeError):
        return None
    if not v:
        return None
    val = v[0] if isinstance(v, list) else v
    return str(val)


def _frame_text(frame: Any) -> str | None:
    """Extract a printable string from an ID3 frame or MP4 atom value."""
    text = getattr(frame, "text", None)
    if text is not None:
        try:
            return str(text[0])
        except (IndexError, TypeError):
            return None
    try:
        first = frame[0] if isinstance(frame, (list, tuple)) else frame
    except (IndexError, TypeError):
        return None
    if isinstance(first, bytes):
        try:
            return first.decode("utf-8", errors="replace")
        except (TypeError, UnicodeDecodeError):
            return None
    return str(first)


def _id3_get(m: Any, key: str) -> str | None:
    """Read *key* from ID3 / MP4 tag indexing; ``None`` when absent."""
    tags = getattr(m, "tags", None)
    if tags is None:
        return None
    try:
        v = tags[key]
    except (KeyError, TypeError):
        return None
    if v is None:
        return None
    return _frame_text(v)


def _first_tag(m: object, *keys: str) -> str:
    """Return the first matching tag value from a mutagen file, as a string."""
    for k in keys:
        result = _vorbis_get(m, k) or _id3_get(m, k)
        if result is not None:
            return result
    return ""


def _lyrics_from_vorbis(m: Any) -> str:
    """Return embedded lyrics from Vorbis / FLAC / APE-style flat tags."""
    for k in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS", "Lyrics"):
        getter = getattr(m, "get", None)
        if not callable(getter):
            continue
        v = getter(k)
        if not v:
            continue
        try:
            val = v[0] if isinstance(v, list) else v
            if val:
                return str(val).strip()
        except (IndexError, TypeError):
            continue
    return ""


def _lyrics_from_uslt(tags: Any) -> str:
    """Return embedded lyrics from an ID3v2 USLT frame."""
    try:
        keys = list(tags.keys()) if hasattr(tags, "keys") else []
    except (AttributeError, TypeError):
        keys = []
    for k in keys:
        if not (isinstance(k, str) and k.upper().startswith("USLT")):
            continue
        try:
            text = getattr(tags[k], "text", None)
            if text:
                return str(text).strip()
        except (AttributeError, KeyError, TypeError):
            continue
    return ""


def _lyrics_from_mp4(tags: Any) -> str:
    """Return embedded lyrics from an MP4 ``©lyr`` / iTunes LYRICS atom."""
    for k in ("\xa9lyr", "----:com.apple.iTunes:LYRICS"):
        try:
            v = tags[k]
        except (KeyError, TypeError):
            continue
        try:
            first = v[0] if isinstance(v, (list, tuple)) else v
        except (IndexError, TypeError):
            continue
        if isinstance(first, bytes):
            try:
                return first.decode("utf-8", errors="replace").strip()
            except (TypeError, UnicodeDecodeError):
                continue
        return str(first).strip()
    return ""


def read_plain_lyrics(audio_path: str | Path) -> str:
    """Read embedded plain (unsynced) lyrics from an audio file's tags."""
    m = _open_mutagen(audio_path)
    if m is None:
        return ""
    found = _lyrics_from_vorbis(m)
    if found:
        return found
    tags = getattr(m, "tags", None)
    if tags is None:
        return ""
    return _lyrics_from_uslt(tags) or _lyrics_from_mp4(tags)


def current_lyric(lyrics: list[LyricLine], elapsed_s: float) -> LyricLine | None:
    """Return the lyric line active at *elapsed_s* into the track.

    Args:
        lyrics: Sorted list of :class:`LyricLine` from :func:`load_lrc_for`.
        elapsed_s: Seconds elapsed since the track started.

    Returns:
        The most recent :class:`LyricLine` whose ``time_s <= elapsed_s``,
        or ``None`` if no line has fired yet.
    """
    if not lyrics:
        return None
    last: LyricLine | None = None
    for line in lyrics:
        if line.time_s > elapsed_s:
            break
        last = line
    return last
