"""Configuration loading and validation for AutoDJ.

Loads settings from a TOML file (default: ``config.toml`` in the working
directory) and exposes them as typed dataclasses.

Example:
    >>> from autodj.config import load_config
    >>> cfg = load_config("config.toml")
    >>> print(cfg.playback.crossfade_seconds)
    3.0
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import MISSING, dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autodj.liners import LINER_PICK_MODES
from autodj.repeat_settings import RepeatSettings, ignore_unknown_repeat_keys

DEFAULT_STEREOTOOL_PATH = (
    "./tools/stereo-tool/stereo_tool_cmd.exe"
    if os.name == "nt"
    else "./tools/stereo-tool/stereo_tool_cmd"
)

if TYPE_CHECKING:
    from autodj.presets import Preset

logger = logging.getLogger(__name__)

# The table a section is read from sits under, such as "stations.nightowl."
# while a station's own sections are read; warnings name the full table.
_TABLE_PREFIX: ContextVar[str] = ContextVar("autodj_config_table_prefix", default="")


# ---------------------------------------------------------------------------
# Sub-section dataclasses
# ---------------------------------------------------------------------------


class _Section:
    """Build a section dataclass from its TOML table.

    Each subclass names its table in ``SECTION``; its dataclass fields are
    the accepted keys and ``__post_init__`` coerces and range-checks them.
    """

    SECTION: ClassVar[str]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Construct the section from a raw TOML table.

        Raises:
            TypeError: If *data* is not a table, or a value has the wrong type.
            KeyError: If a required key is missing.
            ValueError: On an invalid value for a supported setting.
        """
        if not isinstance(data, Mapping):
            raise TypeError(f"{cls.SECTION} section must be a table")
        known = _field_names(cls)
        _warn_unknown_keys(cls.SECTION, data, known)
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name not in data and f.default is MISSING and f.default_factory is MISSING:
                raise KeyError(f"config.toml [{cls.SECTION}] section is missing {f.name!r}")
        return cls(**{key: value for key, value in data.items() if key in known})


def _optional(convert: Callable[[Any], Any], value: Any) -> Any:
    """Return ``convert(value)``, or ``None`` when *value* is ``None``."""
    return None if value is None else convert(value)


@dataclass
class LibraryConfig(_Section):
    """Settings for the music library location and format filtering.

    Attributes:
        music_dir: Path to the root music folder (local or NAS mapped drive).
            For beets users, this should match the local mount point of the
            beets ``directory`` setting — relative paths stored in the beets
            database are resolved against ``music_dir``.
        beets_db: Optional path to the beets SQLite library database.
        supported_formats: List of audio file extensions to index (without dots).
        playlists_folder: Folder holding the M3U and M3U8 files that rotation
            categories read; ``None`` means ``<index_dir>/<name>/playlists``.
    """

    SECTION: ClassVar[str] = "library"

    music_dir: Path
    beets_db: Path | None = None
    supported_formats: list[str] = field(default_factory=lambda: ["mp3", "flac", "m4a"])
    playlists_folder: Path | None = None

    def __post_init__(self) -> None:
        """Expand ``~`` in the paths; an empty ``beets_db`` or ``playlists_folder`` means none."""
        self.music_dir = Path(self.music_dir).expanduser()
        self.beets_db = Path(self.beets_db).expanduser() if self.beets_db else None
        self.playlists_folder = (
            Path(self.playlists_folder).expanduser() if self.playlists_folder else None
        )


@dataclass
class IndexConfig(_Section):
    """Settings for the FAISS index storage locations.

    AutoDJ supports **named indexes** so you can keep multiple curated
    libraries side-by-side — a "workout" index of high-BPM tracks, a
    "chill" index for evening listening, etc.  Each named index lives
    in its own sub-directory ``<index_dir>/<name>/`` with its own FAISS
    files, metadata, web state, liners and dj-meta cache.  Profiles, paired
    browsers and the access token live in ``<index_dir>`` and are shared
    by every named index.

    Attributes:
        index_dir: Base directory holding all named indexes.
        model_dir: Directory where the MuQ model checkpoint is cached.
        name: Active index name.  Files live in ``<index_dir>/<name>/``.
            ``--name`` on ``index``, ``prune``, ``enrich``, ``analyse``,
            ``serve`` and ``stats`` overrides it.
        throttle_ms: Pause before each track that ``autodj index`` embeds
            and ``autodj analyse`` analyses, in milliseconds.  ``0`` = none.
    """

    SECTION: ClassVar[str] = "index"

    index_dir: Path = field(default_factory=lambda: Path("index"))
    model_dir: Path = field(default_factory=lambda: Path("models"))
    name: str = "default"
    throttle_ms: float = 0.0

    def __post_init__(self) -> None:
        """Expand the paths and validate the index name and throttle.

        Raises:
            ValueError: If ``name`` contains path separators / traversal /
                leading dot — names are bare identifiers, not paths — or
                ``throttle_ms`` is negative.
            TypeError: If ``throttle_ms`` is not a number.
        """
        self.index_dir = Path(self.index_dir).expanduser()
        self.model_dir = Path(self.model_dir).expanduser()
        self.name = str(self.name).strip() or "default"
        validate_index_name(self.name)
        if isinstance(self.throttle_ms, bool) or not isinstance(self.throttle_ms, int | float):
            raise TypeError("index.throttle_ms must be a number")
        self.throttle_ms = float(self.throttle_ms)
        if self.throttle_ms < 0:
            raise ValueError(f"index.throttle_ms must be >= 0, got {self.throttle_ms}")

    @property
    def active_dir(self) -> Path:
        """Resolved location of the active named index."""
        return self.index_dir / self.name


def validate_index_name(name: str) -> None:
    """Reject names that aren't safe single-segment directory names.

    Use this on every CLI ``--name`` flag and on the ``[index] name``
    config value before storing.  A *bad* name like ``index/tracks.db``
    would silently produce the wrong on-disk path; failing fast with a
    clear error is better.

    Args:
        name: Candidate index name.

    Raises:
        ValueError: If *name* is empty, contains ``/`` or ``\\``, contains
            ``..``, or starts with a dot.
    """
    if not name or not name.strip():
        raise ValueError("Index name must not be empty.")
    if "/" in name or "\\" in name:
        raise ValueError(
            f"Index name cannot contain path separators (got {name!r}).  "
            f"Use a bare identifier like 'workout' or 'chill' — files land "
            f"under <index_dir>/<name>/ automatically.",
        )
    if ".." in name or name.startswith("."):
        raise ValueError(
            f"Index name cannot start with '.' or contain '..' (got {name!r}).",
        )


TRANSITION_MODES: tuple[str, ...] = (
    "full_intro_outro",
    "outro_fade",
    "fixed_skip_silence",
    "fixed",
    "saved_points",
)

PICK_MODES: tuple[str, ...] = ("similarity", "random", "least_recent", "clock")
LOUDNESS_PROCESSING_MODES: tuple[str, ...] = ("off", "replaygain", "smooth", "stereotool")

KEY_NOTATIONS: tuple[str, ...] = (
    "camelot",
    "musical",
)

#: How a skip ends the playing track in the server mix
#: (``[playback] skip_style``); see :mod:`autodj.skip_fx`.
SKIP_STYLES: tuple[str, ...] = ("fade", "echo_out", "backspin", "loop_roll")


POST_QUEUE_SEED_MODES = ("last_queued", "pre_queue")
# The longest crossfade or fade-in, in seconds; the Settings page, the web
# routes and config.toml all use this range.
MAX_FADE_SECONDS = 20.0


def _one_of(value: str, options: tuple[str, ...], field_name: str) -> str:
    """Return *value* unchanged if it is one of *options*, else raise.

    Args:
        value: Candidate string from configuration or runtime state.
        options: Allowed values.
        field_name: Dotted config field name used in the error message.

    Returns:
        The validated string.

    Raises:
        ValueError: If *value* is not in *options*.
    """
    if value not in options:
        raise ValueError(f"{field_name} must be one of {options}, got {value!r}")
    return value


def _warn_unknown_keys(section: str, data: Mapping[str, Any], known: Iterable[str]) -> None:
    """Warn about config keys this version does not recognize.

    Args:
        section: Section name used in the error message, e.g. ``"library"``.
        data: Raw keys of that section.
        known: Accepted key names.
    """
    unknown = set(data) - set(known)
    if unknown:
        logger.warning(
            "Ignoring unknown config keys in [%s%s]: %s",
            _TABLE_PREFIX.get(),
            section,
            sorted(unknown),
        )


def _field_names(cls: Any) -> set[str]:
    """Return the dataclass field names of *cls*, which double as its TOML keys."""
    return {f.name for f in fields(cls)}


#: ``post_queue_seed`` picks how similarity restarts once a queue empties:
#: ``last_queued`` (default) seeds from the final queued track, ``pre_queue``
#: rewinds to the track playing when the queue was added, so it acts as a detour.
_validate_key_notation = partial(_one_of, options=KEY_NOTATIONS, field_name="playback.key_notation")
_validate_post_queue_seed = partial(
    _one_of, options=POST_QUEUE_SEED_MODES, field_name="playback.post_queue_seed"
)
_validate_transition_mode = partial(
    _one_of, options=TRANSITION_MODES, field_name="playback.transition_mode"
)
_validate_pick_mode = partial(_one_of, options=PICK_MODES, field_name="playback.pick_mode")
_validate_loudness_processing = partial(
    _one_of,
    options=LOUDNESS_PROCESSING_MODES,
    field_name="playback.loudness_processing",
)
_validate_skip_style = partial(_one_of, options=SKIP_STYLES, field_name="playback.skip_style")


@dataclass
class PlaybackConfig(_Section):
    """Settings for audio playback behaviour.

    Attributes:
        crossfade_seconds: Duration of the crossfade between tracks in seconds.
            ``0.0`` cuts from one track to the next.  In the
            ``full_intro_outro`` and ``outro_fade`` transition modes a track
            with detected markers fades for 1-12 s instead, even at ``0.0``.
        repeat_rules: Independent artist, album, track and title rules.
        discovery_every: Default discovery rate: inject a sonically distant
            track every *N* tracks.  ``None`` disables discovery by default.
            The user must also toggle discovery ON at runtime.
        crossfade_eq_duck: When ``True``, the crossfade applies a Butterworth
            high-pass sweep on the outgoing track during the overlap so its
            bass frequencies don't clash with the incoming track's bass —
            the trick pro DJs use when manually mixing.  Adds tiny CPU cost
            via scipy filtering.
        crossfade_bass_cutoff_hz: Frequency below which the outgoing track is
            progressively attenuated during an EQ-ducked crossfade.  Default
            180 Hz covers kick drums and sub-bass.
        skip_style: How a skip ends the playing track when the server
            mixes (stream mode and server audio), one of
            :data:`SKIP_STYLES`: ``"fade"`` (default) fades out over
            150 ms; ``"echo_out"``, ``"backspin"`` and ``"loop_roll"``
            play a one-to-two-beat effect, at most 2 seconds, that ends
            on a beat when the tempo is trusted.  Browser playback keeps
            its own skip.
        liners_talk_up: When ``True``, a liner due as a track starts in
            the server mix is timed to end just before the incoming
            track's vocal (its first synced lyric line, else its detected
            intro end), starting over the crossfade when it is too long
            for the intro alone.  Without room for it, or with nothing to
            time it by, it plays as the track starts.  Browser playback
            ignores it.  Default ``False``.
    """

    SECTION: ClassVar[str] = "playback"

    crossfade_seconds: float = 3.0
    fade_in_seconds: float = 3.0
    repeat_rules: RepeatSettings = field(default_factory=RepeatSettings)
    discovery_every: int | None = None
    crossfade_eq_duck: bool = False
    crossfade_bass_cutoff_hz: float = 180.0
    # Mixxx-style transition mode.  Controls how the crossfade aligns
    # with each track's intro / outro markers from the DJ-meta sidecar.
    #   - "full_intro_outro" (default): the fade starts at the outgoing
    #     outro and the incoming track enters at its first sound, so its
    #     intro plays under the outro; fade length = min(intro_len,
    #     outro_len) clamped to 1-12 s.
    #   - "outro_fade":  begin fade at outro_start, length = outro_len
    #     (1-12 s).  Ignores the intro.
    #   - "fixed_skip_silence": fixed crossfade_seconds; the incoming
    #     track enters at its first sound.
    #   - "fixed": plain fixed crossfade_seconds at the
    #     end of the outgoing track.  No marker alignment.
    transition_mode: str = "full_intro_outro"
    # Similarity is the default. Random preserves the legacy pure-shuffle
    # choice; the other pickers are selected by newer playback controls.
    pick_mode: str = "similarity"
    # None follows the legacy [replaygain].enabled setting.
    loudness_processing: str | None = None
    intro_lift_max_db: float = 4.0
    stereotool_input_trim_db: float = 0.0
    stereotool_agc_drive_db: float = 7.0
    stereotool_preset: str = "included"
    # Executable name or path used by Stereo Tool processing; installation setting.
    stereotool_path: str = DEFAULT_STEREOTOOL_PATH
    # Where the similarity engine seeds from after a user-built queue
    # empties.  "last_queued" (default) continues from the final queued
    # track -- the user steered the set, the auto-DJ follows.
    # "pre_queue" rewinds and seeds from the track that was playing
    # when the queue was first added, treating the queue as a detour.
    post_queue_seed: str = "last_queued"
    # Top-K weighted random pick for similarity selection.  After FAISS
    # returns ranked candidates and any BPM/energy re-ranking, the next
    # track is sampled from the top ``pick_top_k`` results with weights
    # derived from a softmax over their scores at temperature
    # ``pick_temperature``.  ``pick_top_k = 1`` (default) is fully
    # deterministic — same seed always picks the same next track.
    # Higher K + non-zero temperature breaks the "same song -> same
    # path" loop while keeping picks within the closest neighbourhood.
    # Recommended starting point for variety: k=10, temperature=0.3.
    pick_top_k: int = 1
    pick_temperature: float = 0.3
    # Display notation for the current track's key in the now-playing
    # badge + advance log.  Either "camelot" (Mixed In Key wheel labels
    # like 8A / 8B) or "musical" (letter names: C, Am, F#m).  Camelot
    # is the default because the in-page wheel SVG is Camelot-shaped.
    # Internal logic (harmonic mode, picker math) keeps using
    # chromatic key + mode ints regardless of display.
    key_notation: str = "camelot"
    # Only meaningful when key_notation == "musical": render accidentals
    # as flats (Db, Eb, Gb, Ab, Bb) instead of sharps (C#, D#, F#, G#,
    # A#).  Default False = sharps, which matches the spelling most DJ
    # tag editors emit.
    key_prefer_flats: bool = False
    # When False, the player never loads lyrics and the web UI hides its
    # lyric card.  Default True — opt-out, not opt-in.
    show_lyrics: bool = True
    # Web-UI gapless prefetch — preload next track's bytes on the standby
    # deck as soon as the server picks it.  Off only for very tight
    # bandwidth budgets.
    prefetch_next_track: bool = True
    # Web-UI silence-detector — fire the crossfade early when the active
    # track has gone quiet past the half-way mark.  Eliminates dead-air
    # tails on long fade-out songs.
    silence_trigger_crossfade: bool = True
    # Output device for sounddevice — None / "" = system default.
    # Either an int (sounddevice.query_devices() index) or a substring of
    # the device name.  Set via [playback] audio_device; used by server audio.
    audio_device: str | int | None = None
    # Import cue points from external DJ software (Mixxx, Rekordbox,
    # Traktor libraries; Serato file tags) and merge them with the detected
    # cues when `autodj index`, `autodj analyse` or the player analyses a track.  Off
    # only when the user wants the auto-detected cues alone.
    import_external_cues: bool = True
    # Beat-sync transition FX: rhythmic effects (beat_repeat,
    # gate_stutter, echo_out, dub_delay, sidechain_pump, halftime,
    # stutter_build, scratch) snap their start to the next outgoing
    # downbeat and size their internal events to whole bars at a BPM
    # blended from outgoing -> incoming track tempo.  Envelope FX
    # (sweeps, risers) bar-round their length but don't snap start.
    # Falls back to seconds-based timing when no beat grid /
    # tempo is known.  Default ON.  The server mix times echo_out,
    # dub_delay, gate_stutter, transformer, stutter_build, sidechain_pump,
    # beat_repeat and scratch by the outgoing tempo when it is trusted
    # (transitions.trusted_bpm) and starts the gates, loops and pump on
    # the next downbeat; it does not round effect lengths to bars.
    beat_sync_fx: bool = True
    # Key-sync pitched FX: oscillator-based effects (pitch_swell,
    # pitch_fall, dub_siren, ring_modulator, air_horn) tune their
    # carrier frequency to the song's root note.  Lerps in log space
    # from outgoing root -> incoming root across the fade.  Default ON.
    # The server mix tunes air_horn, dub_siren and ring_modulator.
    key_sync_fx: bool = True
    # Beatmatch on skip: when the user presses Skip / N hotkey mid-track,
    # the browser-side crossfade applies playbackRate = outgoing_bpm /
    # incoming_bpm to the standby deck (preservesPitch=true) so the new
    # track joins the existing groove instead of cold-cutting at its
    # native tempo.  Reverts at fade-out.  Off by default — keeps the
    # "skip = clean break" behaviour for users who want it.
    # Server-mixed playback ignores this flag: a skip there is a short
    # fade-out.
    beatmatch_on_skip: bool = False
    # How a skip ends the playing track when the server mixes; see the
    # class docstring.  Browser playback keeps its own skip.
    skip_style: str = "fade"
    # Voice liners — DJ-style spoken drops layered over the live mix.
    # ``liners_folder`` is the source directory (``~`` is expanded; default
    # ``<index_dir>/<name>/liners``).  The browser evaluates the triggers
    # when it plays the audio, fetching the list from ``GET /api/liners``
    # and each file from ``GET /api/liners/file/<name>``; in the
    # server-mixed modes (--server-audio, --stream) LinerScheduler does.
    liners_enabled: bool = False
    liners_folder: str | None = None
    liners_every_n_songs: int | None = None
    liners_every_minutes: float | None = None
    liners_random_min_minutes: float | None = None
    liners_random_max_minutes: float | None = None
    liners_pick_mode: str = "random"
    liners_duck_db: float = -12.0
    # Server mix only: time a liner due at a track start to end just
    # before the incoming vocal ("hitting the post").
    liners_talk_up: bool = False
    # Server-side mixing (--server-audio and --stream) decodes each track
    # whole, at about 21 MB per minute, and holds a few such buffers at
    # once (the playing track, the next one, and the render in progress).
    # Longer tracks are skipped with a log line so a one-hour mix cannot
    # exhaust a small machine's memory.  Browser playback has no limit.
    server_max_track_minutes: float = 15.0

    def __post_init__(self) -> None:
        """Coerce the TOML values and range-check them.

        Raises:
            TypeError: If ``server_max_track_minutes`` is not a number.
            ValueError: If ``crossfade_seconds`` or ``fade_in_seconds`` is
                outside 0-20, ``pick_temperature`` or a liner trigger is
                negative, ``pick_top_k`` is below 1, a choice
                is not one of its options, or ``server_max_track_minutes``
                is outside 1-600.
        """
        for name in ("crossfade_seconds", "fade_in_seconds"):
            seconds = float(getattr(self, name))
            if not 0 <= seconds <= MAX_FADE_SECONDS:
                raise ValueError(
                    f"playback.{name} must be between 0 and {MAX_FADE_SECONDS:g}, got {seconds}"
                )
            setattr(self, name, seconds)
        self.repeat_rules = RepeatSettings.model_validate(
            ignore_unknown_repeat_keys(self.repeat_rules)
        )
        max_minutes = self.server_max_track_minutes
        if isinstance(max_minutes, bool) or not isinstance(max_minutes, int | float):
            raise TypeError("playback.server_max_track_minutes must be a number")
        if not 1 <= max_minutes <= 600:
            raise ValueError(
                f"playback.server_max_track_minutes must be between 1 and 600, got {max_minutes}"
            )
        self.server_max_track_minutes = float(max_minutes)
        self.discovery_every = _optional(int, self.discovery_every)
        self.crossfade_bass_cutoff_hz = float(self.crossfade_bass_cutoff_hz)
        self.transition_mode = _validate_transition_mode(str(self.transition_mode))
        self.pick_mode = _validate_pick_mode(str(self.pick_mode))
        if self.loudness_processing is not None:
            self.loudness_processing = _validate_loudness_processing(str(self.loudness_processing))
        self.intro_lift_max_db = float(self.intro_lift_max_db)
        if not math.isfinite(self.intro_lift_max_db) or not 0 <= self.intro_lift_max_db <= 6:
            raise ValueError("playback.intro_lift_max_db must be between 0 and 6")
        self.stereotool_input_trim_db = float(self.stereotool_input_trim_db)
        if (
            not math.isfinite(self.stereotool_input_trim_db)
            or not -12 <= self.stereotool_input_trim_db <= 6
        ):
            raise ValueError("playback.stereotool_input_trim_db must be between -12 and 6")
        self.stereotool_agc_drive_db = float(self.stereotool_agc_drive_db)
        if (
            not math.isfinite(self.stereotool_agc_drive_db)
            or not 0 <= self.stereotool_agc_drive_db <= 12
        ):
            raise ValueError("playback.stereotool_agc_drive_db must be between 0 and 12")
        from autodj.stereotool_presets import validate_preset_name

        validate_preset_name(self.stereotool_preset)
        if not isinstance(self.stereotool_path, str) or not self.stereotool_path.strip():
            raise ValueError("playback.stereotool_path must be a non-empty string")
        self.skip_style = _validate_skip_style(str(self.skip_style))
        self.post_queue_seed = _validate_post_queue_seed(str(self.post_queue_seed))
        self.pick_top_k = int(self.pick_top_k)
        if self.pick_top_k < 1:
            raise ValueError(f"playback.pick_top_k must be >= 1, got {self.pick_top_k}")
        self.pick_temperature = float(self.pick_temperature)
        self.key_notation = _validate_key_notation(str(self.key_notation))
        self.audio_device = self.audio_device or None
        for name in (
            "crossfade_eq_duck",
            "key_prefer_flats",
            "show_lyrics",
            "prefetch_next_track",
            "silence_trigger_crossfade",
            "import_external_cues",
            "beat_sync_fx",
            "key_sync_fx",
            "beatmatch_on_skip",
            "liners_enabled",
            "liners_talk_up",
        ):
            setattr(self, name, bool(getattr(self, name)))
        self.liners_folder = (
            str(Path(self.liners_folder).expanduser()) if self.liners_folder else None
        )
        self.liners_every_n_songs = _optional(int, self.liners_every_n_songs)
        self.liners_every_minutes = _optional(float, self.liners_every_minutes)
        self.liners_random_min_minutes = _optional(float, self.liners_random_min_minutes)
        self.liners_random_max_minutes = _optional(float, self.liners_random_max_minutes)
        for name in (
            "pick_temperature",
            "liners_every_n_songs",
            "liners_every_minutes",
            "liners_random_min_minutes",
            "liners_random_max_minutes",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"playback.{name} must be >= 0, got {value}")
        self.liners_pick_mode = _one_of(
            str(self.liners_pick_mode), LINER_PICK_MODES, "playback.liners_pick_mode"
        )
        self.liners_duck_db = float(self.liners_duck_db)
        if not -30.0 <= self.liners_duck_db <= 0.0:
            # A positive value would boost the music under every liner.
            raise ValueError(
                f"playback.liners_duck_db must be between -30 and 0 dB, got {self.liners_duck_db}"
            )


@dataclass
class DjMixConfig(_Section):
    """Settings for the DJ-grade mixing layer (beatmatch, phrase align, sweep, harmony).

    Every option defaults to off so the basic crossfade behaviour is
    unchanged; opt in only as you want each feature.

    Attributes:
        beatmatch: When ``True``, the incoming track is time-stretched,
            pitch preserved (up to ±``beatmatch_max_stretch``), so its
            BPM matches the outgoing track during the crossfade, and the
            two tracks' downbeats are lined up when both have a trusted
            beat grid.  Requires both tracks to have a known BPM in the
            index.
        beatmatch_max_stretch: Maximum allowed stretch ratio deviation
            from 1.0.  ``0.08`` = ±8 % (typical DJ practice).  Half and
            double time count as the same tempo.
        beatmatch_glide_bars: Bars (of the incoming track) over which a
            beatmatched track eases back to its own tempo once the
            crossfade ends, at most 30 seconds.  ``0`` returns it straight
            away.
        drop_mix: When ``True`` (and ``beatmatch`` is on), a pair of
            tracks with reliable drops is mixed drop to drop: the
            incoming build-up plays under the outgoing track and the
            incoming drop lands on the downbeat that starts the outgoing
            track's next section, where the crossfade ends.  Drops set by
            hand or in DJ software count; a detected drop only when it is
            clear.  Any pair that does not fit (no reliable drop, beat
            grids not trusted, the incoming drop more than 90 s in, or
            the outgoing track cut more than 32 s short) mixes as usual.
            Server mix only.
        key_shift: When ``True``, an incoming track whose key clashes with
            the outgoing one, but would mix after moving it one or two
            semitones, is played that much higher or lower (tempo
            unchanged) during the crossfade and slides back to its own
            key over the following ``beatmatch_glide_bars`` bars (at
            least 4 seconds).  Server mix only.
        outro_intro_align: When ``True``, the crossfade starts at the
            outgoing track's outro start (auto-detected on first play)
            and the incoming track enters at its first sound.
        phrase_align: When ``True``, the crossfade start time is snapped
            to the nearest ``phrase_bars``-bar phrase boundary (uses the
            cached beat grid).  Applies to the server mix and to browser
            crossfades (``static/modules/audio-engine.js``).
        phrase_bars: Phrase length in bars used by phrase alignment.
        filter_sweep: When ``True``, applies a low-pass sweep on the
            outgoing tail (cutoff sliding from full-range down to
            ``filter_sweep_floor_hz``) during the crossfade — adds the
            classic "filter-out" energy lift.
        filter_sweep_floor_hz: Floor cutoff for the sweep.
        harmonic_mode: Harmonic-mixing rule for similarity candidates, one
            of :data:`autodj.dj_meta.HARMONIC_MODES`.  ``"off"`` (the
            default) applies no key filter; see
            :func:`autodj.dj_meta.harmonic_compatible` for the others.
        vocal_guard: When ``True`` (the default, unlike the options
            above) and both tracks have synced (LRC) lyrics, the server
            crossfade is shortened or moved so the outgoing track's last
            sung line ends before the incoming track starts singing.  The
            fade is never lengthened or made shorter than 1 second; when
            nothing avoids the clash it stays as it was.
    """

    SECTION: ClassVar[str] = "djmix"

    beatmatch: bool = False
    beatmatch_max_stretch: float = 0.08
    beatmatch_glide_bars: int = 8
    drop_mix: bool = False
    key_shift: bool = False
    outro_intro_align: bool = False
    phrase_align: bool = False
    phrase_bars: int = 8
    filter_sweep: bool = False
    filter_sweep_floor_hz: float = 250.0
    harmonic_mode: str = "off"
    vocal_guard: bool = True

    def __post_init__(self) -> None:
        """Coerce the TOML values and validate ``harmonic_mode``.

        Raises:
            ValueError: If ``harmonic_mode`` is not one of
                :data:`autodj.dj_meta.HARMONIC_MODES`.
            TypeError: If ``drop_mix`` or ``key_shift`` is not a boolean.
        """
        if isinstance(self.harmonic_mode, str):
            self.harmonic_mode = self.harmonic_mode.lower()
        if self.harmonic_mode != "off":
            # dj_meta pulls in numpy, so the default "off" is accepted
            # without importing it; config loading stays light for doctor.
            from autodj.dj_meta import HARMONIC_MODES

            if not isinstance(self.harmonic_mode, str):
                raise ValueError(
                    f"djmix.harmonic_mode must be one of {HARMONIC_MODES}, "
                    f"got {self.harmonic_mode!r}"
                )
            _one_of(self.harmonic_mode, HARMONIC_MODES, "djmix.harmonic_mode")
        self.beatmatch = bool(self.beatmatch)
        self.beatmatch_max_stretch = float(self.beatmatch_max_stretch)
        self.beatmatch_glide_bars = max(0, int(self.beatmatch_glide_bars))
        for name in ("drop_mix", "key_shift"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"djmix.{name} must be true or false")
        self.outro_intro_align = bool(self.outro_intro_align)
        self.phrase_align = bool(self.phrase_align)
        self.phrase_bars = int(self.phrase_bars)
        self.filter_sweep = bool(self.filter_sweep)
        self.vocal_guard = bool(self.vocal_guard)
        self.filter_sweep_floor_hz = float(self.filter_sweep_floor_hz)


@dataclass
class TransitionsConfig(_Section):
    """Settings for transition effects layered onto every crossfade.

    Attributes:
        effect: Which effect to apply.  ``"none"`` = standard crossfade
            only.  Every member of :class:`~autodj.transitions.TransitionFx`
            is accepted, for example ``"echo_out"``, ``"reverb_tail"``,
            ``"highpass_sweep"``, ``"tape_stop"`` or ``"halftime"``.
            Meta modes: ``"random"`` (uniform random per crossfade),
            ``"rotate"`` (cycle through all real effects in order),
            ``"auto"`` (an effect suited to the two tracks' tempo, key,
            energy and pick; rules in :mod:`autodj.transitions`).  An
            unknown name plays a plain crossfade.
        wet_mix: Global wet/dry of the transition effect's contribution
            to the final overlap (0.0 = effect inaudible, 1.0 = full).
            Some effects already have their own internal wet — this is
            the outer mix on top of that.
    """

    SECTION: ClassVar[str] = "transitions"

    effect: str = "none"
    wet_mix: float = 1.0

    def __post_init__(self) -> None:
        """Lower-case the effect name and coerce the wet mix."""
        self.effect = str(self.effect).lower()
        self.wet_mix = float(self.wet_mix)


@dataclass
class ReplayGainConfig(_Section):
    """Settings for ReplayGain loudness normalisation.

    Attributes:
        enabled: If ``True``, apply per-track ReplayGain tags so all tracks
            play at a consistent loudness.  Tracks without tags play
            unchanged.  Default ``False`` (off — opt-in).
        target_db: Output reference level in dB.  ``-18.0`` is the original
            ReplayGain reference (quiet).  ``-14.0`` matches Spotify /
            YouTube loudness (default).  Higher = louder overall.
        max_clip_safe_gain: Caps the linear gain so tagged peaks never
            exceed this fraction of full-scale.  Default ``1.0`` = no
            clipping.  Lower it (e.g. ``0.95``) for extra headroom.
    """

    SECTION: ClassVar[str] = "replaygain"

    enabled: bool = False
    target_db: float = -14.0
    max_clip_safe_gain: float = 1.0

    def __post_init__(self) -> None:
        """Coerce the TOML values."""
        self.enabled = bool(self.enabled)
        self.target_db = float(self.target_db)
        self.max_clip_safe_gain = float(self.max_clip_safe_gain)


@dataclass
class ModelConfig(_Section):
    """Settings for the MuQ embedding model.

    Attributes:
        name: HuggingFace model ID to load (used for auto-download).
        revision: HuggingFace revision (branch, tag, or commit) to cache.
        manual_path: Optional local path to a pre-downloaded model directory.
            When set, ``name`` is ignored and the model is loaded from disk.
    """

    SECTION: ClassVar[str] = "model"

    name: str = "OpenMuQ/MuQ-large-msd-iter"
    revision: str = "main"
    manual_path: Path | None = None

    def __post_init__(self) -> None:
        """Validate the configured model revision; an empty ``manual_path`` means none."""
        if (
            not isinstance(self.revision, str)
            or not self.revision
            or self.revision != self.revision.strip()
        ):
            raise ValueError(
                "model.revision must be a non-empty string without surrounding whitespace"
            )
        self.manual_path = Path(self.manual_path) if self.manual_path else None


MIN_ACCESS_TOKEN_BYTES = 32
MIN_SESSION_TTL_SECONDS = 60
MAX_SESSION_TTL_SECONDS = 365 * 24 * 60 * 60
MAX_LINER_UPLOAD_MIB = 1024
_MIB = 1024 * 1024
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _require_int(value: object, field_name: str) -> int:
    """Return an integer value or raise a field-specific type error."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _canonicalize_host(
    value: object,
    *,
    field_name: str,
    allow_unspecified: bool,
) -> str:
    """Validate and normalize an IP address or DNS hostname."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} entries must be strings")
    if (
        not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{field_name} contains an invalid host")
    if not value.isascii():
        raise ValueError(f"{field_name} hostnames must use ASCII")
    if (
        value.startswith("[")
        or value.endswith("]")
        or any(marker in value for marker in ("@", "/", "\\", "?", "#", "%", "*"))
    ):
        raise ValueError(f"{field_name} contains an invalid host")

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if ":" in value:
            raise ValueError(f"{field_name} contains an invalid host") from None
    else:
        if address.is_unspecified and not allow_unspecified:
            raise ValueError(f"{field_name} cannot contain a wildcard host")
        return address.compressed.lower()

    hostname = value.removesuffix(".")
    if not hostname or hostname == "0":
        raise ValueError(f"{field_name} contains an invalid host")
    ascii_hostname = hostname.lower()
    if len(ascii_hostname) > 253 or any(
        not _DNS_LABEL.fullmatch(label) for label in ascii_hostname.split(".")
    ):
        raise ValueError(f"{field_name} contains an invalid host")
    return ascii_hostname


def validate_access_token(token: str | None) -> None:
    """Reject configured access tokens shorter than the required byte length."""
    if token is None:
        return
    if not isinstance(token, str):
        raise TypeError("server.access_token must be a string")
    if len(token.encode("utf-8")) < MIN_ACCESS_TOKEN_BYTES:
        raise ValueError("server.access_token must be at least 32 UTF-8 bytes")


def canonicalize_allowed_origin(value: str) -> str:
    """Validate and normalize an HTTP or HTTPS origin."""
    if not isinstance(value, str):
        raise TypeError("server.allowed_origins entries must be strings")
    if (
        not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("server.allowed_origins contains an invalid origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("server.allowed_origins contains an invalid origin") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or port == 0
        or parsed.path not in {"", "/"}
        or "?" in value
        or "#" in value
    ):
        raise ValueError(
            "server.allowed_origins entries must be HTTP(S) origins without "
            "userinfo, path, query, or fragment"
        )
    if parsed.netloc.startswith("["):
        try:
            ipaddress.IPv6Address(parsed.hostname)
        except ValueError as exc:
            raise ValueError(
                "server.allowed_origins bracketed hosts must be valid IPv6 addresses"
            ) from exc
    hostname = _canonicalize_host(
        parsed.hostname,
        field_name="server.allowed_origins",
        allow_unspecified=False,
    )
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    scheme = parsed.scheme.lower()
    default_port = 80 if scheme == "http" else 443
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{suffix}"


def canonicalize_allowed_host(value: str) -> str:
    """Validate and normalize one allowed Host name the way the Host check compares it.

    Args:
        value: A DNS name or IP address, without brackets or port.

    Returns:
        The lower-case name, or the compressed IP address.

    Raises:
        ValueError: *value* is not a valid, non-wildcard host.
    """
    return _canonicalize_host(value, field_name="server.allowed_hosts", allow_unspecified=False)


def _canonicalize_allowed_hosts(values: object) -> list[str] | None:
    """Validate, normalize, and deduplicate an optional host allowlist."""
    if values is None:
        return None
    if not isinstance(values, list):
        raise TypeError("server.allowed_hosts must be a list of strings")
    return list(
        dict.fromkeys(
            _canonicalize_host(
                value,
                field_name="server.allowed_hosts",
                allow_unspecified=False,
            )
            for value in values
        )
    )


def _canonicalize_allowed_origins(values: object) -> list[str] | None:
    """Validate, normalize, and deduplicate an optional origin allowlist."""
    if values is None:
        return None
    if not isinstance(values, list):
        raise TypeError("server.allowed_origins must be a list of strings")
    return list(dict.fromkeys(canonicalize_allowed_origin(value) for value in values))


@dataclass
class ServerConfig(_Section):
    """Web-server bind, request policy, session, TLS files, and upload limits."""

    SECTION: ClassVar[str] = "server"

    host: str = "127.0.0.1"
    port: int = 8080
    access_token: str | None = field(default=None, repr=False)
    insecure_lan: bool = False
    lan: bool = False
    allowed_hosts: list[str] | None = None
    allowed_origins: list[str] | None = None
    session_ttl_seconds: int = 90 * 24 * 60 * 60
    liner_upload_max_bytes: int = 50 * _MIB
    station_timezone: str | None = None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    def __post_init__(self) -> None:
        """Normalize and validate server settings after initialization."""
        if self.station_timezone is not None:
            try:
                ZoneInfo(self.station_timezone)
            except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("server.station_timezone must be an IANA time zone") from exc
        for name in ("ssl_certfile", "ssl_keyfile"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"server.{name} must be a nonempty path")
        if (self.ssl_certfile is None) != (self.ssl_keyfile is None):
            raise ValueError(
                "server.ssl_certfile and server.ssl_keyfile (or --ssl-certfile and "
                "--ssl-keyfile) must be set together"
            )
        self.host = _canonicalize_host(
            self.host,
            field_name="server.host",
            allow_unspecified=True,
        )
        self.port = _require_int(self.port, "server.port")
        if not 1 <= self.port <= 65535:
            raise ValueError("server.port must be between 1 and 65535")
        validate_access_token(self.access_token)
        if not isinstance(self.insecure_lan, bool):
            raise TypeError("server.insecure_lan must be a boolean")
        if not isinstance(self.lan, bool):
            raise TypeError("server.lan must be a boolean")
        self.allowed_hosts = _canonicalize_allowed_hosts(self.allowed_hosts)
        self.allowed_origins = _canonicalize_allowed_origins(self.allowed_origins)
        self.session_ttl_seconds = _require_int(
            self.session_ttl_seconds,
            "server.session_ttl_seconds",
        )
        if not MIN_SESSION_TTL_SECONDS <= self.session_ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError("server.session_ttl_seconds must be between 60 and 31536000")
        self.liner_upload_max_bytes = _require_int(
            self.liner_upload_max_bytes,
            "server.liner_upload_max_bytes",
        )
        if not _MIB <= self.liner_upload_max_bytes <= MAX_LINER_UPLOAD_MIB * _MIB:
            raise ValueError("server.liner_upload_max_bytes must be between 1 MiB and 1024 MiB")

    def effective_allowed_hosts(self) -> list[str]:
        """Return configured hosts or the default host derived from the bind address."""
        if self.allowed_hosts is not None:
            return list(self.allowed_hosts)
        # Sentinel comparison selects defaults; it does not bind a socket.
        return [] if self.host in {"0.0.0.0", "::"} else [self.host]  # nosec B104

    def effective_allowed_origins(self) -> list[str]:
        """Return configured origins or the default origin derived from the bind address."""
        if self.allowed_origins is not None:
            return list(self.allowed_origins)
        # Sentinel comparison selects defaults; it does not bind a socket.
        if self.host in {"0.0.0.0", "::"}:  # nosec B104
            return []
        rendered_host = f"[{self.host}]" if ":" in self.host else self.host
        return [canonicalize_allowed_origin(f"http://{rendered_host}:{self.port}")]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Construct server settings; the upload cap is written in MiB, stored in bytes.

        Raises:
            TypeError: If *data* is not a table.
            ValueError: On an unknown key or an out-of-range value.
        """
        if not isinstance(data, Mapping):
            raise TypeError("server section must be a table")
        data = dict(data)
        _warn_unknown_keys(
            "server",
            data,
            (_field_names(cls) - {"liner_upload_max_bytes"}) | {"liner_upload_max_mib"},
        )
        data.pop("liner_upload_max_bytes", None)
        max_mib = _require_int(data.pop("liner_upload_max_mib", 50), "server.liner_upload_max_mib")
        if not 1 <= max_mib <= MAX_LINER_UPLOAD_MIB:
            raise ValueError("server.liner_upload_max_mib must be between 1 and 1024")
        return super().from_dict({**data, "liner_upload_max_bytes": max_mib * _MIB})


def is_loopback_bind(host: str) -> bool:
    """Return whether a valid bind host resolves to a loopback address."""
    try:
        canonical = _canonicalize_host(
            host,
            field_name="server.host",
            allow_unspecified=True,
        )
    except (TypeError, ValueError):
        return False
    if canonical == "localhost":
        return True
    try:
        return ipaddress.ip_address(canonical).is_loopback
    except ValueError:
        return False


def validate_server_exposure(cfg: ServerConfig) -> None:
    """Normalize mutable overrides and reject unsafe bind configurations.

    Also requires the TLS certificate and key files, when set, to exist.
    """
    cfg.__post_init__()
    for name in ("ssl_certfile", "ssl_keyfile"):
        value = getattr(cfg, name)
        if value is not None and not Path(value).is_file():
            raise ValueError(f"server.{name} is not a file: {value}")
    loopback = is_loopback_bind(cfg.host)
    # Sentinel comparison enforces explicit allowlists; it does not bind a socket.
    if cfg.host in {"0.0.0.0", "::"} and (  # nosec B104
        not cfg.allowed_hosts or not cfg.allowed_origins
    ):
        raise ValueError(
            "LAN binding requires explicit nonempty allowed_hosts and allowed_origins; "
            "wildcard binding requires both lists"
        )
    if not cfg.effective_allowed_hosts() or not cfg.effective_allowed_origins():
        raise ValueError("wildcard binding requires explicit allowed_hosts and allowed_origins")
    if not loopback and not cfg.access_token and not cfg.insecure_lan:
        raise ValueError(
            "LAN binding requires [server] access_token/--access-token or explicit --insecure-lan"
        )


@dataclass
class HuggingFaceConfig(_Section):
    """Settings for HuggingFace Hub access.

    Attributes:
        token: Optional HuggingFace API token (read-only scope is sufficient).
            Without a token, downloads are unauthenticated and rate-limited.
            Get one free at https://huggingface.co/settings/tokens
    """

    SECTION: ClassVar[str] = "huggingface"

    token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Treat an empty token as none."""
        self.token = self.token or None


STREAM_BITRATES = (128, 192, 256, 320)


def parse_env_bool(value: str) -> bool:
    """Parse an environment boolean.

    Args:
        value: Raw environment text.

    Returns:
        The boolean it names.

    Raises:
        ValueError: If *value* is not a recognised boolean word.
    """
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value!r}")


#: Server types a relay can push to: Icecast 2.4 or later (HTTP PUT),
#: Shoutcast 1 and Shoutcast 2 (both through the Shoutcast 1 source login).
RELAY_TYPES: tuple[str, ...] = ("icecast", "shoutcast1", "shoutcast2")
MAX_RELAYS = 8
_RELAY_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_RELAY_HOST = re.compile(r"[A-Za-z0-9._:-]{1,253}\Z")
_RELAY_MOUNT = re.compile(r"/[A-Za-z0-9._~/-]{1,200}\Z")
_RELAY_REQUIRED = ("type", "host", "password")


def _check_relay_text(where: str, key: str, value: Any) -> None:
    """Raise unless *value* is a printable string of 1 to 255 characters."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}.{key} must be a non-empty string")
    if len(value) > 255 or not value.isprintable():
        raise ValueError(
            f"{where}.{key} must be at most 255 characters, without control characters"
        )


@dataclass
class RelayConfig:
    """One external server the stream is pushed to (``[stream.relays.<name>]``).

    Relays are named tables rather than an array so ``config.local.toml``
    can add the password to a relay that ``config.toml`` describes: TOML
    overlays merge tables key by key but replace arrays whole.

    Attributes:
        name: The table name; shown in the web page, logs and ``doctor``.
        type: One of :data:`RELAY_TYPES`.
        host: Server host name or IP address, without a scheme or port.
        password: Source password.  Never logged, saved or sent to a browser.
        port: The server's listener port.  Shoutcast sources connect one
            above it, as Shoutcast expects.
        mount: Icecast mount point, such as ``/live``; Icecast only.
        stream_id: Shoutcast 2 stream ID (default 1); Shoutcast 2 only.
        user: Icecast source user name (default ``source``); Icecast only.
        bitrate: MP3 bitrate in kbps; ``None`` sends the stream's own audio.
        public: Ask the server to list the station in public directories.
        enabled: ``False`` keeps the table but does not connect.
    """

    name: str
    type: str
    host: str
    password: str = field(repr=False)
    port: int = 8000
    mount: str | None = None
    stream_id: int | None = None
    user: str | None = None
    bitrate: int | None = None
    public: bool = False
    enabled: bool = True

    @classmethod
    def from_table(cls, name: str, data: Any) -> RelayConfig:
        """Build a relay from its raw TOML table.

        Raises:
            TypeError: If *data* is not a table, or a value has the wrong type.
            KeyError: If ``type``, ``host`` or ``password`` is missing.
            ValueError: On an invalid value for a supported setting.
        """
        if not isinstance(name, str) or not _RELAY_NAME.match(name):
            raise ValueError(
                "stream.relays names may use only letters, digits, '-' and '_' "
                f"(1 to 32 characters), got {name!r}"
            )
        where = f"stream.relays.{name}"
        if not isinstance(data, Mapping):
            raise TypeError(f"{where} must be a table")
        _warn_unknown_keys(where, data, _field_names(cls) - {"name"})
        for key in _RELAY_REQUIRED:
            if key not in data:
                raise KeyError(f"config [{where}] is missing {key!r}")
        known = _field_names(cls) - {"name"}
        return cls(name=name, **{key: value for key, value in data.items() if key in known})

    def __post_init__(self) -> None:
        """Validate every field and fill the per-type defaults."""
        where = f"stream.relays.{self.name}"
        if self.type not in RELAY_TYPES:
            raise ValueError(f"{where}.type must be one of {RELAY_TYPES}, got {self.type!r}")
        if not isinstance(self.host, str) or not _RELAY_HOST.match(self.host):
            raise ValueError(
                f"{where}.host must be a host name or IP address, without a scheme, port or path"
            )
        if ":" in self.host:
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                raise ValueError(
                    f"{where}.host {self.host!r} must not include a port; "
                    f"give the host name alone and set {where}.port instead"
                ) from None
        _check_relay_text(where, "password", self.password)
        top_port = 65535 if self.type == "icecast" else 65534
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise TypeError(f"{where}.port must be an integer")
        if not 1 <= self.port <= top_port:
            raise ValueError(f"{where}.port must be between 1 and {top_port}")
        for key in ("public", "enabled"):
            if not isinstance(getattr(self, key), bool):
                raise TypeError(f"{where}.{key} must be true or false")
        if self.bitrate is not None and (
            isinstance(self.bitrate, bool) or self.bitrate not in STREAM_BITRATES
        ):
            raise ValueError(f"{where}.bitrate must be one of {STREAM_BITRATES}")
        self._check_type_keys(where)

    def _check_type_keys(self, where: str) -> None:
        """Check the keys only one server type uses; fill their defaults."""
        if self.type == "icecast":
            if self.mount is None:
                raise KeyError(f"config [{where}] is missing 'mount' (for example \"/live\")")
            if not isinstance(self.mount, str) or not _RELAY_MOUNT.match(self.mount):
                raise ValueError(
                    f"{where}.mount must start with '/' and use only letters, digits and . _ ~ - /"
                )
            self.user = "source" if self.user is None else self.user
            _check_relay_text(where, "user", self.user)
            if ":" in self.user:
                raise ValueError(f"{where}.user must not contain ':'")
        else:
            for key in ("mount", "user"):
                if getattr(self, key) is not None:
                    raise ValueError(f"{where}.{key} is for Icecast only")
        if self.type == "shoutcast2":
            self.stream_id = 1 if self.stream_id is None else self.stream_id
            if isinstance(self.stream_id, bool) or not isinstance(self.stream_id, int):
                raise TypeError(f"{where}.stream_id must be an integer")
            if not 1 <= self.stream_id <= 2_147_483_647:
                raise ValueError(f"{where}.stream_id must be 1 or more")
        elif self.stream_id is not None:
            raise ValueError(f"{where}.stream_id is for Shoutcast 2 only")


def relay_password_variable(name: str) -> str:
    """Return the environment variable that overrides relay *name*'s password.

    Args:
        name: The relay's table name.

    Returns:
        ``AUTODJ_STREAM_RELAY_<NAME>_PASSWORD``, with *name* upper-cased
        and ``-`` turned into ``_``.
    """
    return f"AUTODJ_STREAM_RELAY_{name.upper().replace('-', '_')}_PASSWORD"


@dataclass
class StreamConfig(_Section):
    """Radio stream settings (``[stream]``).

    Attributes:
        enabled: Serve in stream mode.
        bitrate: MP3 bitrate in kbps, one of :data:`STREAM_BITRATES`.
        idle_grace_seconds: Seconds without listeners before the set stops.
        max_listeners: Concurrent listener limit.
        station_name: Name sent to players as ``icy-name``.
        relays: External servers to push the stream to, by name.
    """

    SECTION: ClassVar[str] = "stream"

    enabled: bool = False
    continuous: bool = False
    bitrate: int = 320
    idle_grace_seconds: float = 30.0
    max_listeners: int = 8
    max_listeners_per_address: int = 0
    trusted_proxies: list[str] = field(default_factory=list)
    listener_user: str = ""
    listener_password: str = field(default="", repr=False)
    station_name: str = "AutoDJ"
    relays: dict[str, RelayConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate every field."""
        if not isinstance(self.relays, Mapping):
            raise TypeError(
                "stream.relays must be named tables such as [stream.relays.main], not a list"
            )
        if len(self.relays) > MAX_RELAYS:
            raise ValueError(f"stream.relays allows at most {MAX_RELAYS} relays")
        self.relays = {
            name: table if isinstance(table, RelayConfig) else RelayConfig.from_table(name, table)
            for name, table in self.relays.items()
        }
        if not isinstance(self.enabled, bool):
            raise TypeError("stream.enabled must be true or false")
        if not isinstance(self.continuous, bool):
            raise TypeError("stream.continuous must be true or false")
        if isinstance(self.bitrate, bool) or not isinstance(self.bitrate, int):
            raise TypeError("stream.bitrate must be an integer")
        if self.bitrate not in STREAM_BITRATES:
            raise ValueError(f"stream.bitrate must be one of {STREAM_BITRATES}, got {self.bitrate}")
        if isinstance(self.idle_grace_seconds, bool) or not isinstance(
            self.idle_grace_seconds, int | float
        ):
            raise TypeError("stream.idle_grace_seconds must be a number")
        if not 0 < float(self.idle_grace_seconds) <= 3600:
            raise ValueError("stream.idle_grace_seconds must be between 0 and 3600")
        if isinstance(self.max_listeners, bool) or not isinstance(self.max_listeners, int):
            raise TypeError("stream.max_listeners must be an integer")
        if not 1 <= self.max_listeners <= 100:
            raise ValueError("stream.max_listeners must be between 1 and 100")
        if isinstance(self.max_listeners_per_address, bool) or not isinstance(
            self.max_listeners_per_address, int
        ):
            raise TypeError("stream.max_listeners_per_address must be an integer")
        if not 0 <= self.max_listeners_per_address <= 100:
            raise ValueError("stream.max_listeners_per_address must be between 0 and 100")
        if not isinstance(self.trusted_proxies, list) or not all(
            isinstance(item, str) for item in self.trusted_proxies
        ):
            raise TypeError("stream.trusted_proxies must be a list of addresses or CIDRs")
        import ipaddress

        for item in self.trusted_proxies:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid stream.trusted_proxies entry: {item}") from exc
        if not isinstance(self.listener_user, str) or not isinstance(self.listener_password, str):
            raise TypeError("stream listener credentials must be strings")
        if self.listener_password and not self.listener_user:
            raise ValueError("stream.listener_user is required with listener_password")
        if self.listener_user and not self.listener_password:
            raise ValueError("stream.listener_password is required with listener_user")
        if ":" in self.listener_user:
            raise ValueError("stream.listener_user must not contain ':'")
        if not isinstance(self.station_name, str) or not self.station_name.strip():
            raise ValueError("stream.station_name must be a non-empty string")
        if not self.station_name.isprintable():
            # It is sent as an HTTP header (icy-name): no control characters.
            raise ValueError("stream.station_name must not contain control characters")
        self.idle_grace_seconds = float(self.idle_grace_seconds)


@dataclass
class RecordingConfig(_Section):
    """Encoded stream archive settings (``[recording]``)."""

    SECTION: ClassVar[str] = "recording"

    folder: Path = Path("recordings")
    split: str = "hour"
    retention_days: int = 0
    free_space_mb: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.folder, str | Path) or not str(self.folder).strip():
            raise ValueError("recording.folder must be a non-empty path")
        self.folder = Path(self.folder).expanduser()
        if self.split not in {"hour", "show"}:
            raise ValueError("recording.split must be hour or show")
        for name in ("retention_days", "free_space_mb"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"recording.{name} must be a non-negative integer")


@dataclass
class NowPlayingConfig(_Section):
    """Public now-playing feed settings (``[nowplaying]``)."""

    SECTION: ClassVar[str] = "nowplaying"
    show_next: bool = False
    history_limit: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.show_next, bool):
            raise TypeError("nowplaying.show_next must be true or false")
        if isinstance(self.history_limit, bool) or not isinstance(self.history_limit, int):
            raise TypeError("nowplaying.history_limit must be an integer")
        if not 0 <= self.history_limit <= 20:
            raise ValueError("nowplaying.history_limit must be between 0 and 20")


@dataclass
class DmcaConfig(_Section):
    """Optional US statutory-webcasting programming safeguards."""

    SECTION: ClassVar[str] = "dmca"
    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("dmca.enabled must be true or false")


#: Where a DJ break goes in a song change (:class:`BreaksConfig`).
BREAK_PLACEMENTS: tuple[str, ...] = ("auto", "talk_up", "standalone")


@dataclass
class BreaksConfig(_Section):
    """DJ breaks, news and weather from an announcer program (#88).

    Attributes:
        enabled: Plan each song change one song ahead and take breaks for it.
            Off by default: a station with breaks off picks songs exactly as
            before.
        dj_placement: Where a DJ break goes when its request says ``auto``:
            ``"auto"`` talks up the next song when its intro is at least
            *min_intro_seconds* long and otherwise plays between the songs;
            ``"talk_up"`` talks up any intro; ``"standalone"`` always plays
            between the songs.  News, weather and music news always play
            between the songs.
        min_intro_seconds: The shortest intro ``"auto"`` talks up.
        duck_db: How far the music drops under a break's voice, in dB
            (-30 to 0).  Unset uses ``[playback] liners_duck_db``.
        max_upload_mb: The largest break file, in megabytes (at most 200).
        max_seconds: The longest break, in seconds (at most 900).  A break
            is decoded whole into memory, about 0.35 MB per second of this
            limit, so the default 300 s takes about 106 MB at most.
        max_queued: Breaks waiting to air at once, on this station.
        max_per_slot: Breaks one song change can take.
        default_expires_seconds: How long a break may wait when its request
            does not say.
        max_expires_seconds: The longest wait a request may ask for.
        quota_mb: Disk space the waiting break files may use, in megabytes.
        requests_per_minute: Requests one token may make to the breaks API.
    """

    SECTION: ClassVar[str] = "breaks"

    enabled: bool = False
    dj_placement: str = "auto"
    min_intro_seconds: float = 2.0
    duck_db: float | None = None
    max_upload_mb: int = 40
    max_seconds: float = 300.0
    max_queued: int = 6
    max_per_slot: int = 2
    default_expires_seconds: int = 1800
    max_expires_seconds: int = 7200
    quota_mb: int = 400
    requests_per_minute: int = 120

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("breaks.enabled must be true or false")
        if self.dj_placement not in BREAK_PLACEMENTS:
            raise ValueError("breaks.dj_placement must be one of: " + ", ".join(BREAK_PLACEMENTS))
        for name, low, high in (
            ("min_intro_seconds", 0.0, 60.0),
            ("max_seconds", 5.0, 900.0),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"breaks.{name} must be a number")
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"breaks.{name} must be between {low:g} and {high:g}")
            setattr(self, name, float(value))
        if self.duck_db is not None:
            if isinstance(self.duck_db, bool) or not isinstance(self.duck_db, (int, float)):
                raise TypeError("breaks.duck_db must be a number")
            if not math.isfinite(self.duck_db) or not -30.0 <= self.duck_db <= 0.0:
                raise ValueError("breaks.duck_db must be between -30 and 0 dB")
            self.duck_db = float(self.duck_db)
        for name, low, high in (
            ("max_upload_mb", 1, 200),
            ("max_queued", 1, 50),
            ("max_per_slot", 1, 5),
            ("default_expires_seconds", 60, 86400),
            ("max_expires_seconds", 60, 86400),
            ("quota_mb", 1, 100000),
            ("requests_per_minute", 1, 6000),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"breaks.{name} must be a whole number")
            if not low <= value <= high:
                raise ValueError(f"breaks.{name} must be between {low} and {high}")
        if self.default_expires_seconds > self.max_expires_seconds:
            raise ValueError(
                "breaks.default_expires_seconds must not be more than max_expires_seconds"
            )
        if self.quota_mb < self.max_upload_mb:
            raise ValueError("breaks.quota_mb must be at least max_upload_mb")

    @property
    def max_upload_bytes(self) -> int:
        """:attr:`max_upload_mb` in bytes."""
        return self.max_upload_mb * 1024 * 1024

    @property
    def quota_bytes(self) -> int:
        """:attr:`quota_mb` in bytes."""
        return self.quota_mb * 1024 * 1024


@dataclass(frozen=True)
class WebhookConfig:
    """One outbound track-change destination, stored in config.local.toml."""

    url: str = field(repr=False)
    format: str = "generic"
    events: tuple[str, ...] = ("track_change",)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise TypeError("webhooks.url must be a string")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("webhooks.url must be an HTTP or HTTPS URL without user info")
        try:
            if parsed.port == 0:
                raise ValueError("webhooks.url has an invalid port")
        except ValueError as exc:
            raise ValueError("webhooks.url has an invalid port") from exc
        if len(self.url) > 2048 or not self.url.isprintable():
            raise ValueError("webhooks.url is too long or contains control characters")
        if not isinstance(self.format, str) or self.format not in {"generic", "discord"}:
            raise ValueError("webhooks.format must be generic or discord")
        if (
            not isinstance(self.events, (list, tuple))
            or not all(isinstance(event, str) for event in self.events)
            or set(self.events) != {"track_change"}
        ):
            raise ValueError("webhooks.events must contain only track_change")
        object.__setattr__(self, "events", tuple(self.events))


# ---------------------------------------------------------------------------
# Root config dataclass
# ---------------------------------------------------------------------------


@dataclass
class TimeChecksConfig(_Section):
    """Recorded and synthesized spoken time announcements."""

    SECTION: ClassVar[str] = "time_checks"

    enabled: bool = False
    clips_folder: str | None = None
    hour_format: str = "12"
    intro_clip: str | None = None
    outro_clip: str | None = None
    tts_command: list[str] = field(default_factory=list)
    rule: str = "off"
    voice_packs: list[str] = field(default_factory=list)
    pack_order: str = "rotate"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("time_checks.enabled must be true or false")
        if self.pack_order not in ("rotate", "random"):
            raise ValueError("time_checks.pack_order must be 'rotate' or 'random'")
        packs = self.voice_packs
        if not isinstance(packs, list) or any(
            not isinstance(path, str) or not path.strip() for path in packs
        ):
            raise TypeError("time_checks.voice_packs must be an array of nonempty folder paths")
        self.voice_packs = [str(Path(path).expanduser()) for path in packs]
        names = [Path(path).name.casefold() for path in self.voice_packs]
        if "" in names or len(set(names)) != len(names):
            raise ValueError(
                "time_checks.voice_packs folders need distinct names; the name shows in Settings"
            )
        if self.hour_format not in ("12", "24"):
            raise ValueError("time_checks.hour_format must be '12' or '24'")
        if self.rule not in ("off", "after_hour_song"):
            raise ValueError("time_checks.rule must be 'off' or 'after_hour_song'")
        for name in ("clips_folder", "intro_clip", "outro_clip"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"time_checks.{name} must be a nonempty path")
            if value:
                setattr(self, name, str(Path(value).expanduser()))
        command = self.tts_command
        if not isinstance(command, list) or any(
            not isinstance(part, str) or not part for part in command
        ):
            raise TypeError("time_checks.tts_command must be an array of nonempty strings")
        if command and sum(part.count("{out}") for part in command) != 1:
            raise ValueError("time_checks.tts_command must contain {out} once")
        if any("{" in part.replace("{out}", "").replace("{text}", "") for part in command):
            raise ValueError("time_checks.tts_command supports only {out} and {text}")


@dataclass
class AutoDJConfig:
    """Root configuration for the AutoDJ application.

    Attributes:
        library: Library location and format settings.
        index: FAISS index storage settings.
        playback: Playback behaviour settings.
        model: MuQ model settings.
        huggingface: HuggingFace Hub access settings.
        config_path: Path to the config file this instance was loaded from.
        presets: User-defined BPM presets loaded from ``[presets.*]`` sections.
        replaygain: ReplayGain settings.
        djmix: DJ-mix layer settings.
        transitions: Transition effect settings.
        server: Web server, LAN and HTTPS settings.
        stream: Radio stream settings.
        breaks: Announcer breaks from another program (#88).
        stations: The stations other than the default one, from
            ``[stations.<slug>]`` tables; empty for a single station.
        config_sources: Where the values came from, in load order.
    """

    library: LibraryConfig
    index: IndexConfig
    playback: PlaybackConfig
    model: ModelConfig
    huggingface: HuggingFaceConfig
    config_path: Path | None
    presets: dict[str, Preset] = field(default_factory=dict)
    replaygain: ReplayGainConfig = field(default_factory=lambda: ReplayGainConfig())
    djmix: DjMixConfig = field(default_factory=lambda: DjMixConfig())
    transitions: TransitionsConfig = field(default_factory=lambda: TransitionsConfig())
    server: ServerConfig = field(default_factory=ServerConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    nowplaying: NowPlayingConfig = field(default_factory=NowPlayingConfig)
    time_checks: TimeChecksConfig = field(default_factory=TimeChecksConfig)
    breaks: BreaksConfig = field(default_factory=BreaksConfig)
    dmca: DmcaConfig = field(default_factory=DmcaConfig)
    webhooks: tuple[WebhookConfig, ...] = ()
    stations: tuple[StationSpec, ...] = ()
    config_sources: tuple[str, ...] = ("defaults",)


@dataclass(frozen=True)
class StationSpec:
    """One station besides the default one (``[stations.<slug>]``).

    Attributes:
        slug: The station's name in URLs, folders and logs; matches
            :data:`autodj.stations.SLUG_PATTERN`.
        autostart: Start the station with the server.  ``False`` leaves it
            stopped until it is started from the API.
        cfg: The station's own configuration.  Its library, index, model,
            Hugging Face and server sections are the root configuration's
            objects; every other section was read from the station's own
            tables, starting from the defaults, not from the root sections.
    """

    slug: str
    autostart: bool
    cfg: AutoDJConfig = field(repr=False)

    @property
    def name(self) -> str:
        """The station's name, as listeners see it (``stream.station_name``)."""
        return self.cfg.stream.station_name


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* into *base*, returning a new dict.

    Nested dicts are merged key-by-key; non-dict values in *overlay* replace
    those in *base*.  Used to apply machine-specific overrides from
    ``config.local.toml`` on top of the shared ``config.toml``.
    """
    out: dict[str, Any] = deepcopy(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


ENVIRONMENT_OVERLAY: dict[str, tuple[str, str, Callable[[str], object]]] = {
    "AUTODJ_LIBRARY_MUSIC_DIR": ("library", "music_dir", str),
    "AUTODJ_INDEX_DIR": ("index", "index_dir", str),
    "AUTODJ_MODEL_DIR": ("index", "model_dir", str),
    "AUTODJ_HOST": ("server", "host", str),
    "AUTODJ_PORT": ("server", "port", int),
    "AUTODJ_ACCESS_TOKEN": ("server", "access_token", str),
    "AUTODJ_LAN": ("server", "lan", parse_env_bool),
    "AUTODJ_HUGGINGFACE_TOKEN": ("huggingface", "token", str),
    "AUTODJ_STREAM_ENABLED": ("stream", "enabled", parse_env_bool),
    "AUTODJ_STREAM_CONTINUOUS": ("stream", "continuous", parse_env_bool),
    "AUTODJ_STREAM_BITRATE": ("stream", "bitrate", int),
    "AUTODJ_STREAM_IDLE_GRACE_SECONDS": ("stream", "idle_grace_seconds", float),
    "AUTODJ_STREAM_MAX_LISTENERS": ("stream", "max_listeners", int),
    "AUTODJ_STREAM_STATION_NAME": ("stream", "station_name", str),
}


def _default_raw() -> dict[str, Any]:
    """Return the minimal raw configuration defaults."""
    return {
        "library": {"music_dir": "music"},
        "index": {"index_dir": "index", "model_dir": "models"},
        "server": {"host": "127.0.0.1", "port": 8080},
    }


def _environment_overlay(environ: Mapping[str, str]) -> dict[str, Any]:
    """Build a configuration overlay from supported environment variables."""
    overlay: dict[str, Any] = {}
    for variable, (section, key, converter) in ENVIRONMENT_OVERLAY.items():
        if variable not in environ:
            continue
        raw_value = environ[variable]
        try:
            value = converter(raw_value)
        except ValueError as exc:
            raise ValueError(f"{variable} has invalid value {raw_value!r}") from exc
        overlay.setdefault(section, {})[key] = value
    return overlay


def _relay_password_overlay(raw: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    """Build an overlay of relay passwords from ``AUTODJ_STREAM_RELAY_<NAME>_PASSWORD``.

    Only relays already named in the loaded TOML are looked up, so a
    variable cannot create a relay on its own.

    Args:
        raw: The merged TOML tables loaded so far.
        environ: The process environment.

    Returns:
        ``{"stream": {"relays": {name: {"password": ...}}}}`` for every
        relay whose variable is set, or an empty dict.
    """
    stream = raw.get("stream")
    relays = stream.get("relays") if isinstance(stream, Mapping) else None
    if not isinstance(relays, Mapping):
        return {}
    found = {
        name: {"password": environ[variable]}
        for name in relays
        if isinstance(name, str) and (variable := relay_password_variable(name)) in environ
    }
    return {"stream": {"relays": found}} if found else {}


def station_relay_password_variable(slug: str, name: str) -> str:
    """Return the environment variable that overrides a station relay's password.

    Args:
        slug: The station's name (``[stations.<slug>]``).
        name: The relay's table name.

    Returns:
        ``AUTODJ_STATION_<SLUG>_RELAY_<NAME>_PASSWORD``, upper-cased, with
        ``-`` turned into ``_``.
    """
    return (
        f"AUTODJ_STATION_{slug.upper().replace('-', '_')}"
        f"_RELAY_{name.upper().replace('-', '_')}_PASSWORD"
    )


def _station_relay_password_overlay(
    raw: Mapping[str, Any], environ: Mapping[str, str]
) -> dict[str, Any]:
    """Build an overlay of station relay passwords from the environment.

    Only relays already named in the loaded TOML are looked up, as for the
    default station's relays.

    Raises:
        ValueError: Two station relays would read the same variable.
    """
    stations = raw.get("stations")
    if not isinstance(stations, Mapping):
        return {}
    owners: dict[str, str] = {}
    overlay: dict[str, Any] = {}
    for slug, table in stations.items():
        stream = table.get("stream") if isinstance(table, Mapping) else None
        relays = stream.get("relays") if isinstance(stream, Mapping) else None
        if not isinstance(slug, str) or not isinstance(relays, Mapping):
            continue
        for name in relays:
            if not isinstance(name, str):
                continue
            variable = station_relay_password_variable(slug, name)
            where = f"stations.{slug}.stream.relays.{name}"
            if variable in owners:
                raise ValueError(
                    f"[{where}] and [{owners[variable]}] would both read {variable}; "
                    "rename one of them"
                )
            owners[variable] = where
            if variable in environ:
                station = overlay.setdefault(slug, {"stream": {"relays": {}}})
                station["stream"]["relays"][name] = {"password": environ[variable]}
    return {"stations": overlay} if overlay else {}


_SECTION_TYPES: tuple[type[_Section], ...] = (
    LibraryConfig,
    IndexConfig,
    PlaybackConfig,
    ModelConfig,
    HuggingFaceConfig,
    ReplayGainConfig,
    DjMixConfig,
    TransitionsConfig,
    ServerConfig,
    StreamConfig,
    RecordingConfig,
    NowPlayingConfig,
    TimeChecksConfig,
    BreaksConfig,
    DmcaConfig,
)


#: The sections each station has of its own; ``presets`` and ``webhooks``
#: too.  The others (library, index, model, huggingface, server) are shared
#: by every station and read only from the root.
STATION_SECTION_TYPES: tuple[type[_Section], ...] = (
    PlaybackConfig,
    DjMixConfig,
    TransitionsConfig,
    ReplayGainConfig,
    StreamConfig,
    RecordingConfig,
    NowPlayingConfig,
    TimeChecksConfig,
    BreaksConfig,
    DmcaConfig,
)
SHARED_SECTIONS: tuple[str, ...] = ("library", "index", "model", "huggingface", "server")
#: The most stations besides the default one.
MAX_STATIONS = 8
_STATION_KEYS = frozenset(
    {"autostart", "presets", "webhooks", *(t.SECTION for t in STATION_SECTION_TYPES)}
)


def unknown_config_keys(raw: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return unknown keys in known TOML sections, by their dotted table name.

    The root sections are named as written (``"playback"``); a station's
    are ``"stations.<slug>"`` for its own keys and
    ``"stations.<slug>.<section>"`` for its sections.  This reports key
    names only; normal config loading remains responsible for validation
    and still raises at the first invalid section.
    """
    unknown = _unknown_section_keys(raw, _SECTION_TYPES, "")
    stations = raw.get("stations")
    if isinstance(stations, Mapping):
        for slug, table in stations.items():
            if not isinstance(table, Mapping):
                continue
            where = f"stations.{slug}"
            # Unknown sub-tables are warned about while loading, like
            # unknown root sections; only plain keys are listed here.
            keys = tuple(
                sorted(
                    key
                    for key, value in table.items()
                    if key not in _STATION_KEYS and not isinstance(value, Mapping)
                )
            )
            if keys:
                unknown[where] = keys
            unknown.update(_unknown_section_keys(table, STATION_SECTION_TYPES, where + "."))
    return unknown


def unknown_config_sections(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the tables AutoDJ ignores, by dotted name, root and station.

    A root table that is no section (``"radio"``) and a station's table that
    is no station section (``"stations.nightowl.server"``) are listed;
    loading warns about the same ones.
    """
    known = {t.SECTION for t in _SECTION_TYPES} | {"presets", "webhooks", "stations"}
    unknown = [name for name in raw if name not in known]
    stations = raw.get("stations")
    if isinstance(stations, Mapping):
        for slug, table in stations.items():
            if isinstance(table, Mapping):
                unknown += [
                    f"stations.{slug}.{name}"
                    for name, value in table.items()
                    if name not in _STATION_KEYS and isinstance(value, Mapping)
                ]
    return tuple(sorted(unknown))


def _unknown_section_keys(
    raw: Mapping[str, Any], section_types: Iterable[type[_Section]], prefix: str
) -> dict[str, tuple[str, ...]]:
    unknown: dict[str, tuple[str, ...]] = {}
    for section_type in section_types:
        section = section_type.SECTION
        table = raw.get(section)
        if not isinstance(table, Mapping):
            continue
        known = _field_names(section_type)
        # ServerConfig exposes this public TOML alias and translates it to
        # the internal byte-valued dataclass field in its from_dict override.
        if section == "server":
            known.discard("liner_upload_max_bytes")
            known.add("liner_upload_max_mib")
        keys = tuple(sorted(set(table) - known))
        if keys:
            unknown[prefix + section] = keys
    return unknown


def _build_config(
    raw: dict[str, Any],
    *,
    config_path: Path | None,
    sources: list[str],
) -> AutoDJConfig:
    """Validate raw sections and construct the typed application configuration."""
    from autodj.presets import load_user_presets

    presets_raw = raw.get("presets", {})
    if not isinstance(presets_raw, Mapping):
        raise TypeError("presets section must be a table")

    unknown_sections = (
        set(raw) - {t.SECTION for t in _SECTION_TYPES} - {"presets", "webhooks", "stations"}
    )
    if unknown_sections:
        logger.warning("Ignoring unknown config sections: %s", sorted(unknown_sections))
    sections: dict[str, Any] = {
        t.SECTION: t.from_dict(raw.get(t.SECTION, {})) for t in _SECTION_TYPES
    }
    cfg = AutoDJConfig(
        **sections,
        webhooks=_load_webhooks(raw.get("webhooks", [])),
        presets=load_user_presets(presets_raw),
        config_path=config_path,
        config_sources=tuple(sources),
    )
    if "stations" in raw:
        cfg.stations = _build_stations(raw["stations"], cfg)
    return cfg


def _build_stations(raw: Any, root: AutoDJConfig) -> tuple[StationSpec, ...]:
    """Read the ``[stations.<slug>]`` tables into station specs.

    Raises:
        TypeError: ``stations`` is not a table of tables.
        ValueError: A name is invalid or reserved, there are too many
            stations, or two stations would write the same recording folder
            or push to the same relay target.
        KeyError: A required key is missing; the message names the station.
    """
    from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN

    if not isinstance(raw, Mapping):
        raise TypeError("stations must be named tables such as [stations.nightowl]")
    if len(raw) > MAX_STATIONS:
        raise ValueError(f"stations allows at most {MAX_STATIONS} stations besides the default")
    specs = []
    for slug, table in raw.items():
        if slug == DEFAULT_SLUG:
            raise ValueError(
                "[stations.default] is reserved: the root sections are the default station"
            )
        if not isinstance(slug, str) or not SLUG_PATTERN.match(slug):
            raise ValueError(
                f"[stations.{slug}]: a station name uses lower-case letters, digits and "
                "inner hyphens, at most 32 characters, such as nightowl"
            )
        if not isinstance(table, Mapping):
            raise TypeError(f"[stations.{slug}] must be a table")
        token = _TABLE_PREFIX.set(f"stations.{slug}.")
        try:
            specs.append(_build_station(slug, table, root))
        except (TypeError, ValueError, KeyError) as exc:
            # Re-raised as the plain kind, since subclasses (pydantic's
            # ValidationError) cannot be built from a message.
            kind: type[Exception]
            if isinstance(exc, KeyError):
                kind, message = KeyError, exc.args[0] if exc.args else str(exc)
            else:
                kind, message = (TypeError if isinstance(exc, TypeError) else ValueError), str(exc)
            raise kind(f"[stations.{slug}] {message}") from exc
        finally:
            _TABLE_PREFIX.reset(token)
    _check_station_conflicts(root, specs)
    return tuple(specs)


def _build_station(slug: str, table: Mapping[str, Any], root: AutoDJConfig) -> StationSpec:
    """Build one station from its table: its own sections over the defaults."""
    from autodj.presets import load_user_presets

    unknown = sorted(set(table) - _STATION_KEYS)
    if unknown:
        logger.warning(
            "Ignoring unknown config keys and sections in [stations.%s]: %s", slug, unknown
        )
    autostart = table.get("autostart", True)
    if not isinstance(autostart, bool):
        raise TypeError("autostart must be true or false")
    stream_raw = table.get("stream", {})
    if isinstance(stream_raw, Mapping):
        stream_raw = dict(stream_raw)
        if stream_raw.pop("enabled", None) is not None:
            logger.warning(
                "Ignoring stations.%s.stream.enabled: a station other than the default "
                "always streams",
                slug,
            )
        if stream_raw.pop("trusted_proxies", None) is not None:
            logger.warning(
                "Ignoring stations.%s.stream.trusted_proxies: it applies server-wide, "
                "from the root [stream] section",
                slug,
            )
    presets_raw = table.get("presets", {})
    if not isinstance(presets_raw, Mapping):
        raise TypeError("presets section must be a table")
    sections: dict[str, Any] = {
        t.SECTION: t.from_dict(stream_raw if t is StreamConfig else table.get(t.SECTION, {}))
        for t in STATION_SECTION_TYPES
    }
    # Every other station is a server-mixed stream; the proxies that may
    # name a listener's address are the server's.
    sections["stream"].enabled = True
    if not isinstance(stream_raw, Mapping) or "station_name" not in stream_raw:
        # Not the default "AutoDJ", which the default station has: two
        # stations would share a name in metadata and on public pages.
        sections["stream"].station_name = slug
    sections["stream"].trusted_proxies = list(root.stream.trusted_proxies)
    cfg = AutoDJConfig(
        **sections,
        **{name: getattr(root, name) for name in SHARED_SECTIONS},
        webhooks=_load_webhooks(table.get("webhooks", [])),
        presets=load_user_presets(presets_raw),
        config_path=root.config_path,
        config_sources=root.config_sources,
    )
    return StationSpec(slug=slug, autostart=autostart, cfg=cfg)


def station_config(station: AutoDJConfig, root: AutoDJConfig) -> AutoDJConfig:
    """A fresh copy of a station's configuration, for one run of the station.

    Its own sections are copies, so what one run changes at runtime is
    gone when the station starts again (its saved settings come back from
    its ``web_state.json``).  The shared sections are the root's current
    objects, so command-line overrides made after loading apply too.
    """
    return replace(
        station,
        **{t.SECTION: deepcopy(getattr(station, t.SECTION)) for t in STATION_SECTION_TYPES},
        **{name: getattr(root, name) for name in SHARED_SECTIONS},
        presets=deepcopy(station.presets),
    )


def _check_station_conflicts(root: AutoDJConfig, specs: Iterable[StationSpec]) -> None:
    """Refuse two stations that share a recording folder or a relay target.

    A recorder deletes old recordings from its folder, so a shared folder
    would let one station's retention delete the other's.  Two sources on
    one relay mount fight over it.  Two stations with the same name are
    only warned about.

    Raises:
        ValueError: Naming both stations.
    """
    folders: dict[str, str] = {}
    targets: dict[tuple[object, ...], str] = {}
    names: dict[str, str] = {}
    for slug, cfg in (("default", root), *((spec.slug, spec.cfg) for spec in specs)):
        name = cfg.stream.station_name.strip().casefold()
        if name in names:
            logger.warning(
                "Stations %s and %s are both called %r; listeners and the station "
                "switcher cannot tell them apart. Give each its own stream.station_name.",
                names[name],
                slug,
                cfg.stream.station_name,
            )
        names.setdefault(name, slug)
        folder = os.path.normcase(os.path.abspath(cfg.recording.folder))
        if folder in folders:
            raise ValueError(
                f"stations {folders[folder]} and {slug} use the same recording folder "
                f"{cfg.recording.folder}; give each station its own recording.folder"
            )
        folders[folder] = slug
        for relay in cfg.stream.relays.values():
            target = _relay_target(relay)
            other = targets.get(target)
            if other is not None and other != slug:
                raise ValueError(
                    f"stations {other} and {slug} both relay to {relay.host}:{relay.port}"
                    f"{relay.mount or ''}; each station needs its own relay target"
                )
            targets[target] = slug


def _relay_target(relay: RelayConfig) -> tuple[object, ...]:
    """What makes two relays push to the same place: host, port and mount or stream."""
    return (relay.host.lower().rstrip("."), relay.port, relay.mount, relay.stream_id)


def _load_webhooks(raw: Any) -> tuple[WebhookConfig, ...]:
    if not isinstance(raw, list) or len(raw) > 10:
        raise ValueError("webhooks must be an array of at most 10 tables")
    hooks = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("webhooks entries must be tables")
        _warn_unknown_keys("webhooks", item, {"url", "format", "events"})
        hooks.append(
            WebhookConfig(
                **{key: value for key, value in item.items() if key in {"url", "format", "events"}}
            )
        )
    return tuple(hooks)


def _refuse_local_only_stations(local: Mapping[str, Any], defined: set[str]) -> None:
    """Refuse a station that config.local.toml names but config.toml does not.

    config.local.toml only adds a station's secrets.  A station named only
    there is a typo or a station removed from config.toml: building it would
    start a public stream with default settings, and the real station would
    run without the credentials meant for it.

    Raises:
        ValueError: Naming the station.
    """
    stations = local.get("stations")
    if not isinstance(stations, Mapping):
        return
    for slug in stations:
        if slug not in defined:
            raise ValueError(
                f"[stations.{slug}] is in config.local.toml but not in config.toml; define "
                "the station in config.toml (config.local.toml holds only its secrets), "
                "or correct the name"
            )


def load_config(
    path: str | Path | None = None, *, environ: Mapping[str, str] | None = None
) -> AutoDJConfig:
    """Load defaults, optional TOML overlays, then typed environment overrides.

    An omitted path uses ``config.toml`` when present and otherwise keeps
    validated defaults. An explicitly supplied missing path remains an error.

    Raises:
        ValueError: A ``presets.toml`` sits where ``config.toml`` is looked
            for.  AutoDJ no longer reads it, and ignoring it would drop its
            presets without a word.
    """
    environment = os.environ if environ is None else environ
    explicit = path is not None
    candidate = Path(path) if path is not None else Path("config.toml")
    raw = _default_raw()
    sources = ["defaults"]
    loaded_path: Path | None = None
    # The stations config.toml defines; config.local.toml only adds their secrets.
    defined_stations: set[str] = set()

    if candidate.exists():
        with candidate.open("rb") as fh:
            shared = tomllib.load(fh)
            if "webhooks" in shared:
                raise ValueError("webhooks belong only in config.local.toml")
            stations = shared.get("stations")
            if isinstance(stations, Mapping):
                defined_stations = set(stations)
                for slug, table in stations.items():
                    if isinstance(table, Mapping) and "webhooks" in table:
                        raise ValueError(
                            f"[stations.{slug}] webhooks belong only in config.local.toml"
                        )
            raw = _deep_merge(raw, shared)
        loaded_path = candidate
        sources.append(str(candidate))
    elif explicit:
        raise FileNotFoundError(f"Config file not found: {candidate}")

    if loaded_path is not None:
        local_path = loaded_path.parent / "config.local.toml"
        if local_path.exists():
            with local_path.open("rb") as fh:
                local = tomllib.load(fh)
            _refuse_local_only_stations(local, defined_stations)
            raw = _deep_merge(raw, local)
            sources.append(str(local_path))

    env_raw = _deep_merge(
        _deep_merge(_environment_overlay(environment), _relay_password_overlay(raw, environment)),
        _station_relay_password_overlay(raw, environment),
    )
    if env_raw:
        raw = _deep_merge(raw, env_raw)
        sources.append("environment")

    presets_path = candidate.parent / "presets.toml"
    if presets_path.exists():
        raise ValueError(
            f"{presets_path} is no longer read: move each [NAME] table into config.toml "
            "as [presets.NAME], then delete presets.toml"
        )
    return _build_config(raw, config_path=loaded_path, sources=sources)
