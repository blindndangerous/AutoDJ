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
import json
import logging
import math
import os
import re
import stat
import tomllib
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import MISSING, dataclass, field, fields, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autodj.liners import LINER_PICK_MODES
from autodj.repeat_settings import RepeatSettings, ignore_unknown_repeat_keys
from autodj.sqlite_utils import readonly_uri


def stereotool_library_name(system: str | None = None, machine: str | None = None) -> str:
    """The file name of Thimeo's Stereo Tool plugin library for this computer.

    The names are those in Thimeo's Generic plugin zip: the 64-bit Windows
    DLL, the Linux build without X11 (AutoDJ needs no Stereo Tool window),
    and the macOS library, which AutoDJ does not download.
    """
    import platform
    import sys

    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if system == "win32":
        return "libStereoTool_64.dll"
    if system == "darwin":
        return "libStereoTool_64.dylib"
    if machine in {"aarch64", "arm64"}:
        return "libStereoTool_noX11_arm64.so"
    return "libStereoTool_intel64.so"


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
        music_dir: Optional base for legacy relative paths only. New libraries
            start empty; registered folders determine the music available.
            For beets users, this should match the local mount point of the
            beets ``directory`` setting — relative paths stored in the beets
            database are resolved against ``music_dir``.
        beets_db: Optional path to the beets SQLite library database.
        supported_formats: List of audio file extensions to index (without dots).
        playlists_folder: Folder holding the M3U and M3U8 files that rotation
            categories read; ``None`` means ``<index_dir>/<name>/playlists``.
    """

    SECTION: ClassVar[str] = "library"

    music_dir: Path | None = None
    beets_db: Path | None = None
    supported_formats: list[str] = field(default_factory=lambda: ["mp3", "flac", "m4a"])
    playlists_folder: Path | None = None

    def __post_init__(self) -> None:
        """Expand ``~`` in the paths; an empty ``beets_db`` or ``playlists_folder`` means none."""
        self.music_dir = Path(self.music_dir).expanduser() if self.music_dir else None
        self.beets_db = Path(self.beets_db).expanduser() if self.beets_db else None
        self.playlists_folder = (
            Path(self.playlists_folder).expanduser() if self.playlists_folder else None
        )


@dataclass
class IndexConfig(_Section):
    """Where AutoDJ keeps its data.

    ``<index_dir>`` holds the machine-wide files (paired browsers, tokens,
    ``tools/``, ``logs/``), the library in ``libraries/main/``, what every
    station shares in ``shared/``, and each station, the default one too,
    in ``stations/<slug>/``.

    Attributes:
        index_dir: The folder holding all of it.
        model_dir: Directory where the MuQ model checkpoint is cached.
        throttle_ms: Pause before each track that ``autodj index`` embeds
            and ``autodj analyse`` analyses, in milliseconds.  ``0`` = none.
    """

    SECTION: ClassVar[str] = "index"

    index_dir: Path = field(default_factory=lambda: Path("index"))
    model_dir: Path = field(default_factory=lambda: Path("models"))
    throttle_ms: float = 0.0

    def __post_init__(self) -> None:
        """Expand the paths and validate the throttle.

        Raises:
            ValueError: ``throttle_ms`` is negative.
            TypeError: ``throttle_ms`` is not a number.
        """
        self.index_dir = Path(self.index_dir).expanduser()
        self.model_dir = Path(self.model_dir).expanduser()
        if isinstance(self.throttle_ms, bool) or not isinstance(self.throttle_ms, int | float):
            raise TypeError("index.throttle_ms must be a number")
        self.throttle_ms = float(self.throttle_ms)
        if self.throttle_ms < 0:
            raise ValueError(f"index.throttle_ms must be >= 0, got {self.throttle_ms}")

    @property
    def library_dir(self) -> Path:
        """The music library: library.db, dj_meta.db, related artists and tracks, logs."""
        # ponytail: one library, "main"; a station picking another comes later.
        return self.index_dir / "libraries" / "main"

    @property
    def shared_dir(self) -> Path:
        """What every station shares: categories, playlists, Stereo Tool presets."""
        return self.index_dir / "shared"

    @property
    def stations_dir(self) -> Path:
        """Every station's own folder, ``<slug>/``, the default station's too."""
        return self.index_dir / "stations"


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


#: The song length limit for server playout, gone since tracks of any length
#: play (#391).  Station migration drops it from saved station settings.
RETIRED_PLAYBACK_KEY = "server_max_track_minutes"


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
        liners_talk_up: Legacy server live overlays and time checks only.
            Saved-point automatic liners always use the shared cue-based
            handoff. When ``True``, a liner due as a track starts in
            the legacy server mix is timed to end just before the incoming
            track's vocal (its first synced lyric line, else its detected
            intro end), starting over the crossfade when it is too long
            for the intro alone.  Without room for it, or with nothing to
            time it by, it plays as the track starts.  Browser playback
            ignores it.  Default ``False``.
    """

    SECTION: ClassVar[str] = "playback"

    crossfade_seconds: float = 3.0
    fade_in_seconds: float = 3.0
    # Music playback rate as a percentage change. Server rendering consumes
    # this at the next unrendered audio-buffer boundary.
    music_speed_percent: float = 0.0
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
    # Prefer an eligible song whose handoff lands near the next fixed point.
    hour_fit: bool = False
    hour_fit_window_minutes: int = 10
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
    # Voice liners — DJ-style spoken drops layered over the live mix,
    # from the registered liner folders and the station's own ``liners``
    # folder, where uploads go.  The browser evaluates the triggers
    # when it plays the audio, fetching the list from ``GET /api/liners``
    # and each file from ``GET /api/liners/file/<name>``; in the
    # server-mixed modes (--server-audio, --stream) LinerScheduler does.
    liners_enabled: bool = False
    liners_every_n_songs: int | None = None
    liners_every_minutes: float | None = None
    liners_random_min_minutes: float | None = None
    liners_random_max_minutes: float | None = None
    liners_pick_mode: str = "random"
    liners_duck_db: float = -3.0
    # Matched song intros may take the automatic liner slot unless this is enabled.
    matched_intro_only_without_liner: bool = False
    # Legacy live timing only; saved-point interval liners always use cue timing.
    liners_talk_up: bool = False

    def __post_init__(self) -> None:
        """Coerce the TOML values and range-check them.

        Raises:
            TypeError: If ``music_speed_percent`` is not a number.
            ValueError: If ``crossfade_seconds`` or ``fade_in_seconds`` is
                outside 0-20, ``pick_temperature`` or a liner trigger is
                negative, ``pick_top_k`` is below 1, or a choice
                is not one of its options.
        """
        for name in ("crossfade_seconds", "fade_in_seconds"):
            seconds = float(getattr(self, name))
            if not 0 <= seconds <= MAX_FADE_SECONDS:
                raise ValueError(
                    f"playback.{name} must be between 0 and {MAX_FADE_SECONDS:g}, got {seconds}"
                )
            setattr(self, name, seconds)
        speed = self.music_speed_percent
        if isinstance(speed, bool) or not isinstance(speed, int | float):
            raise TypeError("playback.music_speed_percent must be a number")
        speed = float(speed)
        if not math.isfinite(speed) or not -10 <= speed <= 10:
            raise ValueError("playback.music_speed_percent must be between -10 and 10")
        if speed != round(speed, 2):
            raise ValueError("playback.music_speed_percent may have at most 2 decimal places")
        self.music_speed_percent = speed
        self.repeat_rules = RepeatSettings.model_validate(
            ignore_unknown_repeat_keys(self.repeat_rules)
        )
        self.discovery_every = _optional(int, self.discovery_every)
        if self.discovery_every is not None and self.discovery_every < 1:
            # Every pick divides the track number by it once Discovery is on.
            raise ValueError(
                f"playback.discovery_every must be 1 or more, got {self.discovery_every}; "
                "leave it out to turn discovery off"
            )
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
        self.skip_style = _validate_skip_style(str(self.skip_style))
        self.post_queue_seed = _validate_post_queue_seed(str(self.post_queue_seed))
        self.pick_top_k = int(self.pick_top_k)
        if self.pick_top_k < 1:
            raise ValueError(f"playback.pick_top_k must be >= 1, got {self.pick_top_k}")
        if type(self.hour_fit) is not bool:
            raise ValueError("playback.hour_fit must be true or false")
        if (
            type(self.hour_fit_window_minutes) is not int
            or not 1 <= self.hour_fit_window_minutes <= 30
        ):
            raise ValueError("playback.hour_fit_window_minutes must be between 1 and 30")
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
            "matched_intro_only_without_liner",
        ):
            setattr(self, name, bool(getattr(self, name)))
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


def validate_trusted_proxy_secret(secret: str | None, secret_file: str | None = None) -> None:
    """Reject ambiguous sources and values unsafe for the proxy proof header."""
    if secret is not None and secret_file is not None:
        raise ValueError(
            "server.trusted_proxy_secret and server.trusted_proxy_secret_file are mutually exclusive"
        )
    if secret is None:
        return
    if not isinstance(secret, str):
        raise TypeError("server.trusted_proxy_secret must be a string")
    if len(secret) < 32 or any(not 33 <= ord(char) <= 126 for char in secret):
        raise ValueError(
            "server.trusted_proxy_secret must contain at least 32 printable ASCII bytes "
            "without spaces"
        )


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
    """Web-server bind, request policy, session, TLS files and shared proxies."""

    SECTION: ClassVar[str] = "server"

    host: str = "127.0.0.1"
    port: int = 8080
    access_token: str | None = field(default=None, repr=False)
    auth_mode: str = "native"
    trusted_proxy_secret: str | None = field(default=None, repr=False)
    trusted_proxy_secret_file: str | None = None
    sso_logout_url: str | None = None
    insecure_lan: bool = False
    lan: bool = False
    allowed_hosts: list[str] | None = None
    allowed_origins: list[str] | None = None
    session_ttl_seconds: int = 90 * 24 * 60 * 60
    trusted_proxies: list[str] = field(default_factory=list)
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    # Live DJ source clients (#48) may log in over plain HTTP from the LAN,
    # not only from this machine.  Their passwords then cross the LAN in
    # the clear; off unless chosen (see THREAT_MODEL.md).
    live_plain_http_lan: bool = False

    def __post_init__(self) -> None:
        """Normalize and validate server settings after initialization."""
        for name in ("ssl_certfile", "ssl_keyfile", "trusted_proxy_secret_file"):
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
        self.auth_mode = _one_of(self.auth_mode, ("native", "trusted_proxy"), "server.auth_mode")
        validate_trusted_proxy_secret(self.trusted_proxy_secret, self.trusted_proxy_secret_file)
        if self.sso_logout_url is not None:
            value = self.sso_logout_url
            if (
                not isinstance(value, str)
                or not value
                or "\\" in value
                or any(not char.isprintable() or char.isspace() for char in value)
            ):
                raise ValueError("server.sso_logout_url must be a safe path or HTTPS URL")
            parsed = urlsplit(value)
            relative = value.startswith("/") and not value.startswith("//")
            if not relative and (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("server.sso_logout_url must be a same-origin path or HTTPS URL")
            if not relative:
                canonicalize_allowed_origin(f"https://{parsed.netloc}")
        if not isinstance(self.insecure_lan, bool):
            raise TypeError("server.insecure_lan must be a boolean")
        if not isinstance(self.lan, bool):
            raise TypeError("server.lan must be a boolean")
        if not isinstance(self.live_plain_http_lan, bool):
            raise TypeError("server.live_plain_http_lan must be a boolean")
        self.allowed_hosts = _canonicalize_allowed_hosts(self.allowed_hosts)
        self.allowed_origins = _canonicalize_allowed_origins(self.allowed_origins)
        self.session_ttl_seconds = _require_int(
            self.session_ttl_seconds,
            "server.session_ttl_seconds",
        )
        if not MIN_SESSION_TTL_SECONDS <= self.session_ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError("server.session_ttl_seconds must be between 60 and 31536000")
        if not isinstance(self.trusted_proxies, list) or not all(
            isinstance(item, str) for item in self.trusted_proxies
        ):
            raise TypeError("server.trusted_proxies must be a list of addresses or CIDRs")
        for item in self.trusted_proxies:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid server.trusted_proxies entry: {item}") from exc

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
        """Construct server settings from its TOML table.

        Raises:
            TypeError: If *data* is not a table.
            ValueError: On an invalid server value.
        """
        if not isinstance(data, Mapping):
            raise TypeError("server section must be a table")
        data = dict(data)
        return super().from_dict(data)


def resolve_trusted_proxy_secret(cfg: ServerConfig) -> str | None:
    """Read one configured source without replacing the operator's source settings."""
    from autodj.secret_files import read_secret

    validate_trusted_proxy_secret(cfg.trusted_proxy_secret, cfg.trusted_proxy_secret_file)
    if cfg.trusted_proxy_secret_file is None:
        return cfg.trusted_proxy_secret
    secret = read_secret(
        Path(cfg.trusted_proxy_secret_file),
        valid=lambda _: True,
        error=ValueError,
        what="trusted proxy secret",
        require_private=True,
    )
    validate_trusted_proxy_secret(secret)
    return secret


STATION_OUTPUTS = ("browser", "server", "stream")


@dataclass
class StationConfig(_Section):
    """Station-local output mode, time zone and liner upload cap."""

    SECTION: ClassVar[str] = "station"

    timezone: str | None = None
    liner_upload_max_mib: int = 50
    output: str = "browser"
    server_audio_too: bool = False

    def __post_init__(self) -> None:
        """Validate the station's time zone, upload cap and output mode."""
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("station.timezone must be an IANA time zone") from exc
        self.liner_upload_max_mib = _require_int(
            self.liner_upload_max_mib, "station.liner_upload_max_mib"
        )
        if not 1 <= self.liner_upload_max_mib <= MAX_LINER_UPLOAD_MIB:
            raise ValueError("station.liner_upload_max_mib must be between 1 and 1024")
        self.output = _one_of(self.output, STATION_OUTPUTS, "station.output")
        if not isinstance(self.server_audio_too, bool):
            raise TypeError("station.server_audio_too must be true or false")
        if self.server_audio_too and self.output != "stream":
            raise ValueError("station.server_audio_too requires station.output = 'stream'")


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
    proxy_authenticated = False
    if cfg.auth_mode == "trusted_proxy":
        if resolve_trusted_proxy_secret(cfg) is None:
            raise ValueError("server.trusted_proxy_secret is required in trusted_proxy mode")
        proxy_authenticated = True
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
    if not loopback and not proxy_authenticated and not cfg.access_token and not cfg.insecure_lan:
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
#: The AAC mount's bitrates in kbps (``[stream] aac_bitrate``; 0 is off).
AAC_BITRATES = (64, 96, 128, 192, 256)
#: The Opus mount's bitrates in kbps (``[stream] opus_bitrate``; 0 is off).
OPUS_BITRATES = (48, 64, 96, 128, 192)
#: The most days of listener statistics ``[stream] stats_days`` may keep.
MAX_STATS_DAYS = 3650


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
    """Return the default station environment variable for a relay password.

    Args:
        name: The relay's table name.

    Returns:
        ``AUTODJ_STATION_DEFAULT_RELAY_<NAME>_PASSWORD``.
    """
    return station_relay_password_variable("default", name)


@dataclass
class StreamConfig(_Section):
    """Radio stream settings (``[stream]``).

    Attributes:
        bitrate: MP3 bitrate in kbps, one of :data:`STREAM_BITRATES`.
        idle_grace_seconds: Seconds without listeners before the set stops.
        max_listeners: Concurrent listener limit, across every format.
        station_name: Name sent to players as ``icy-name``.
        relays: External servers to push the stream to, by name.
        aac_bitrate: The AAC mount's bitrate in kbps, one of
            :data:`AAC_BITRATES`, or 0 for no AAC mount.
        opus_bitrate: The Opus mount's bitrate in kbps, one of
            :data:`OPUS_BITRATES`, or 0 for no Opus mount.
        stats_days: Days of listener statistics to keep, 0 to keep none.
    """

    SECTION: ClassVar[str] = "stream"

    continuous: bool = False
    bitrate: int = 320
    idle_grace_seconds: float = 30.0
    max_listeners: int = 8
    max_listeners_per_address: int = 0
    listener_user: str = ""
    listener_password: str = field(default="", repr=False)
    station_name: str = "AutoDJ"
    genre: str = ""
    description: str = ""
    url: str = ""
    relays: dict[str, RelayConfig] = field(default_factory=dict)
    aac_bitrate: int = 0
    opus_bitrate: int = 0
    stats_days: int = 90

    def __post_init__(self) -> None:
        """Validate every field."""
        for name, allowed in (("aac_bitrate", AAC_BITRATES), ("opus_bitrate", OPUS_BITRATES)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"stream.{name} must be an integer")
            if value and value not in allowed:
                raise ValueError(f"stream.{name} must be 0 (off) or one of {allowed}, got {value}")
        if isinstance(self.stats_days, bool) or not isinstance(self.stats_days, int):
            raise TypeError("stream.stats_days must be an integer")
        if not 0 <= self.stats_days <= MAX_STATS_DAYS:
            raise ValueError(f"stream.stats_days must be between 0 and {MAX_STATS_DAYS}")
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
        for key in ("genre", "description", "url"):
            value = getattr(self, key)
            limit = 2048 if key == "url" else 255
            if not isinstance(value, str) or len(value) > limit or not value.isprintable():
                raise ValueError(
                    f"stream.{key} must be at most {limit} characters without control characters"
                )
        if self.url:
            from urllib.parse import urlsplit

            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("stream.url must be an HTTP or HTTPS URL")
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


#: How listener requests are approved (``[requests] approval``).
REQUEST_APPROVALS: tuple[str, ...] = ("manual", "auto")
REQUEST_NO_SMOOTH_SPOT: tuple[str, ...] = ("best", "wait")


@dataclass
class RequestsConfig(_Section):
    """The public listener request page (``[requests]``).

    Attributes:
        enabled: Serve the request page (``/requests``, or
            ``/stations/<slug>/requests``).  Off by default.
        approval: ``"manual"``: each request waits for the operator;
            ``"auto"``: a request that keeps the rules is queued at once.
        per_listener: Requests one address may make per ``per_listener_minutes``.
        per_listener_minutes: The window ``per_listener`` counts over.
        daily_limit: Requests the station accepts per day, from everyone
            (midnight in the station time zone).
        max_pending: Requests that may wait for the operator at once.
        window_minutes: How far ahead a request may be placed in the queue.
        blocked_artists: Artists listeners cannot request.
        blocked_folders: Folders whose songs listeners cannot request or
            find, as full paths or paths relative to registered music folders.
            A configured legacy ``music_dir`` retains the older relative-path behavior.
        ipv6_prefix: The IPv6 network one listener's limits count over,
            in bits: 56 (the default) counts a whole home network that was
            delegated a /56; 64 a single /64; 128 a single address.
        bridges_enabled: Add transition songs before a distant request. Off by default.
        max_bridges: At most this many transition songs, from one to three.
        bridge_cost_threshold: Cost above which a smooth bridge is sought.
        no_smooth_spot: ``"best"`` queues the best legal position; ``"wait"``
            leaves the request pending for the operator.
    """

    SECTION: ClassVar[str] = "requests"
    enabled: bool = False
    approval: str = "manual"
    per_listener: int = 3
    per_listener_minutes: int = 60
    daily_limit: int = 50
    max_pending: int = 20
    window_minutes: int = 30
    blocked_artists: list[str] = field(default_factory=list)
    blocked_folders: list[str] = field(default_factory=list)
    ipv6_prefix: int = 56
    bridges_enabled: bool = False
    max_bridges: int = 3
    bridge_cost_threshold: float = 2.0
    no_smooth_spot: str = "best"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("requests.enabled must be true or false")
        if self.approval not in REQUEST_APPROVALS:
            raise ValueError("requests.approval must be manual or auto")
        if not isinstance(self.bridges_enabled, bool):
            raise TypeError("requests.bridges_enabled must be true or false")
        if self.no_smooth_spot not in REQUEST_NO_SMOOTH_SPOT:
            raise ValueError("requests.no_smooth_spot must be best or wait")
        if isinstance(self.bridge_cost_threshold, bool) or not isinstance(
            self.bridge_cost_threshold, (int, float)
        ):
            raise TypeError("requests.bridge_cost_threshold must be a number")
        if not 0.0 <= self.bridge_cost_threshold <= 10.0:
            raise ValueError("requests.bridge_cost_threshold must be between 0 and 10")
        for name, low, high in (
            ("per_listener", 1, 100),
            ("per_listener_minutes", 1, 1440),
            ("daily_limit", 1, 10000),
            ("max_pending", 1, 200),
            ("window_minutes", 5, 240),
            ("ipv6_prefix", 32, 128),
            ("max_bridges", 1, 3),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"requests.{name} must be an integer")
            if not low <= value <= high:
                raise ValueError(f"requests.{name} must be between {low} and {high}")
        for name in ("blocked_artists", "blocked_folders"):
            value = getattr(self, name)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise TypeError(f"requests.{name} must be a list of non-empty strings")
            if len(value) > 500:
                raise ValueError(f"requests.{name} allows at most 500 entries")


#: The most sites ``[player_page] embed_origins`` may name.
MAX_EMBED_ORIGINS = 20


@dataclass
class PlayerPageConfig(_Section):
    """The public listening page and its embeddable version (``[player_page]``).

    Attributes:
        enabled: Serve the player page (``/player``, or
            ``/stations/<slug>/player``).
        embed: Serve the embeddable player (``/embed``, or
            ``/stations/<slug>/embed``).
        embed_origins: The sites allowed to show the embeddable player in a
            frame, as ``https://host[:port]`` origins, or ``"*"`` for any
            site.  Empty means only this server's own pages.
    """

    SECTION: ClassVar[str] = "player_page"
    enabled: bool = False
    embed: bool = False
    embed_origins: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("player_page.enabled must be true or false")
        if not isinstance(self.embed, bool):
            raise TypeError("player_page.embed must be true or false")
        if not isinstance(self.embed_origins, list) or not all(
            isinstance(item, str) for item in self.embed_origins
        ):
            raise TypeError("player_page.embed_origins must be a list of origins")
        if len(self.embed_origins) > MAX_EMBED_ORIGINS:
            raise ValueError(f"player_page.embed_origins allows at most {MAX_EMBED_ORIGINS} sites")
        origins: list[str] = []
        for item in self.embed_origins:
            if item == "*":
                origins.append(item)
                continue
            try:
                origins.append(canonicalize_allowed_origin(item))
            except ValueError as exc:
                raise ValueError(
                    f"player_page.embed_origins entry {item!r} must be an HTTP(S) origin "
                    'such as "https://radio.example.net", or "*" for any site'
                ) from exc
        self.embed_origins = list(dict.fromkeys(origins))


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


@dataclass
class AdsConfig(_Section):
    """Scheduled ad breaks with play reports (#51).

    Attributes:
        enabled: Air the ad breaks set up in the Ads tab.  Like
            ``[breaks] enabled``, it makes the station plan each song change
            one song ahead, so a break can join it.  Off by default.
        max_upload_mb: The largest spot file, in megabytes (at most 200).
        max_spot_seconds: The longest spot, in seconds (5 to 600).  A spot is
            decoded whole into memory when it airs, about 0.35 MB per second.
        max_spots: The most spots one station keeps (1 to 1000).
    """

    SECTION: ClassVar[str] = "ads"

    enabled: bool = False
    max_upload_mb: int = 20
    max_spot_seconds: float = 120.0
    max_spots: int = 200

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("ads.enabled must be true or false")
        value = self.max_spot_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("ads.max_spot_seconds must be a number")
        if not math.isfinite(value) or not 5.0 <= value <= 600.0:
            raise ValueError("ads.max_spot_seconds must be between 5 and 600")
        self.max_spot_seconds = float(value)
        for name, low, high in (("max_upload_mb", 1, 200), ("max_spots", 1, 1000)):
            number = getattr(self, name)
            if isinstance(number, bool) or not isinstance(number, int):
                raise TypeError(f"ads.{name} must be a whole number")
            if not low <= number <= high:
                raise ValueError(f"ads.{name} must be between {low} and {high}")

    @property
    def max_upload_bytes(self) -> int:
        """:attr:`max_upload_mb` in bytes."""
        return self.max_upload_mb * 1024 * 1024


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
        ads: Scheduled ad breaks (#51).
        stations: The stations other than the default one, from
            ``[stations.<slug>]`` tables; empty for a single station.
        config_sources: Where the values came from, in load order.
    """

    library: LibraryConfig
    index: IndexConfig
    model: ModelConfig
    huggingface: HuggingFaceConfig
    config_path: Path | None
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    presets: dict[str, Preset] = field(default_factory=dict)
    replaygain: ReplayGainConfig = field(default_factory=lambda: ReplayGainConfig())
    djmix: DjMixConfig = field(default_factory=lambda: DjMixConfig())
    transitions: TransitionsConfig = field(default_factory=lambda: TransitionsConfig())
    server: ServerConfig = field(default_factory=ServerConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    nowplaying: NowPlayingConfig = field(default_factory=NowPlayingConfig)
    requests: RequestsConfig = field(default_factory=RequestsConfig)
    player_page: PlayerPageConfig = field(default_factory=PlayerPageConfig)
    time_checks: TimeChecksConfig = field(default_factory=TimeChecksConfig)
    breaks: BreaksConfig = field(default_factory=BreaksConfig)
    ads: AdsConfig = field(default_factory=AdsConfig)
    dmca: DmcaConfig = field(default_factory=DmcaConfig)
    station: StationConfig = field(default_factory=StationConfig)
    webhooks: tuple[WebhookConfig, ...] = ()
    stations: tuple[StationSpec, ...] = ()
    default_station: StationSpec | None = None
    legacy_station_environment: dict[str, Any] = field(default_factory=dict, repr=False)
    legacy_relay_environment: dict[str, str] = field(default_factory=dict, repr=False)
    config_sources: tuple[str, ...] = ("defaults",)
    skipped_definitions: tuple[tuple[str, str], ...] = ()


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
    defined_in: str = "config"

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
    "AUTODJ_TRUSTED_PROXY_SECRET": ("server", "trusted_proxy_secret", str),
    "AUTODJ_TRUSTED_PROXY_SECRET_FILE": ("server", "trusted_proxy_secret_file", str),
    "AUTODJ_LAN": ("server", "lan", parse_env_bool),
    "AUTODJ_HUGGINGFACE_TOKEN": ("huggingface", "token", str),
}

_STATION_STREAM_ENVIRONMENT: dict[str, tuple[str, Callable[[str], object]]] = {
    "CONTINUOUS": ("continuous", parse_env_bool),
    "BITRATE": ("bitrate", int),
    "IDLE_GRACE_SECONDS": ("idle_grace_seconds", float),
    "MAX_LISTENERS": ("max_listeners", int),
    "STATION_NAME": ("station_name", str),
}
_LEGACY_STREAM_ENVIRONMENT: dict[str, tuple[str, Callable[[str], object]]] = {
    **_STATION_STREAM_ENVIRONMENT,
    "ENABLED": ("enabled", parse_env_bool),
}


def _default_raw() -> dict[str, Any]:
    """Return the minimal raw configuration defaults."""
    return {
        "library": {},
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


def _station_environment_overlay(slug: str, environ: Mapping[str, str]) -> dict[str, Any]:
    """Read supported stream environment overrides for one canonical station ID."""
    prefix = f"AUTODJ_STATION_{slug.upper().replace('-', '_')}_STREAM_"
    stream: dict[str, Any] = {}
    station: dict[str, Any] = {}
    for suffix, (key, converter) in _STATION_STREAM_ENVIRONMENT.items():
        variable = prefix + suffix
        if variable not in environ:
            continue
        try:
            stream[key] = converter(environ[variable])
        except ValueError as exc:
            raise ValueError(f"{variable} has invalid value {environ[variable]!r}") from exc
    return {**({"stream": stream} if stream else {}), **({"station": station} if station else {})}


def _legacy_station_environment(environ: Mapping[str, str]) -> dict[str, Any]:
    """Capture old default-only stream env settings for one-time migration."""
    stream: dict[str, Any] = {}
    output = None
    for suffix, (key, converter) in _LEGACY_STREAM_ENVIRONMENT.items():
        variable = f"AUTODJ_STREAM_{suffix}"
        if variable not in environ:
            continue
        try:
            value = converter(environ[variable])
        except ValueError as exc:
            raise ValueError(f"{variable} has invalid value {environ[variable]!r}") from exc
        if key == "enabled":
            output = "stream" if value else "browser"
        else:
            stream[key] = value
    return {
        **({"stream": stream} if stream else {}),
        **({"station": {"output": output}} if output else {}),
    }


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


_SECTION_TYPES: tuple[type[_Section], ...] = (
    LibraryConfig,
    IndexConfig,
    ModelConfig,
    HuggingFaceConfig,
    ServerConfig,
)


#: The sections each station has of its own; ``presets`` and ``webhooks``
#: too.  The others (library, index, model, huggingface, server) are shared
#: by every station and read only from the root.
STATION_SECTION_TYPES: tuple[type[_Section], ...] = (
    StationConfig,
    PlaybackConfig,
    DjMixConfig,
    TransitionsConfig,
    ReplayGainConfig,
    StreamConfig,
    RecordingConfig,
    NowPlayingConfig,
    RequestsConfig,
    PlayerPageConfig,
    TimeChecksConfig,
    BreaksConfig,
    AdsConfig,
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
        keys = tuple(sorted(set(table) - known))
        if keys:
            unknown[prefix + section] = keys
    return unknown


def _build_config(
    raw: dict[str, Any],
    *,
    config_path: Path | None,
    sources: list[str],
    legacy_station_environment: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> AutoDJConfig:
    """Validate shared TOML sections and load station definitions from their files."""
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
        legacy_station_environment=dict(legacy_station_environment or {}),
    )
    cfg.legacy_relay_environment = _legacy_relay_environment(environment or {}, cfg)
    _load_web_stations(cfg, environment=environment)
    return cfg


#: A station's secrets: kept in its relays.json and secrets.json, never in
#: its station.json.
_STATION_SECRETS = {"stream": ("relays", "listener_user", "listener_password")}


def _without_secrets(table: Mapping[str, Any]) -> dict[str, Any]:
    """*table* without the keys :data:`_STATION_SECRETS` names, nor webhooks."""
    copy = deepcopy(dict(table))
    copy.pop("webhooks", None)
    for section, keys in _STATION_SECRETS.items():
        part = copy.get(section)
        if isinstance(part, dict):
            for key in keys:
                part.pop(key, None)
    return copy


def _secrets_of(table: Mapping[str, Any]) -> dict[str, Any]:
    """Only the secrets in a ``[stations.<slug>]`` table."""
    out: dict[str, Any] = {}
    if "webhooks" in table:
        out["webhooks"] = table["webhooks"]
    for section, keys in _STATION_SECRETS.items():
        part = table.get(section)
        if isinstance(part, Mapping):
            found = {key: part[key] for key in keys if key in part}
            if found:
                out[section] = found
    return out


def _toml_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _legacy_relay_environment(environ: Mapping[str, str], cfg: AutoDJConfig) -> dict[str, str]:
    """Capture old default-only relay password variables for startup migration."""
    from autodj.stations import StationPaths

    if not environ:
        return {}
    tables: list[Mapping[str, Any]] = []
    if cfg.config_path is not None:
        shared = _toml_document(cfg.config_path)
        local = _toml_document(cfg.config_path.parent / "config.local.toml")
        for document in (shared, local):
            stream = document.get("stream")
            relays = stream.get("relays") if isinstance(stream, Mapping) else None
            if isinstance(relays, Mapping):
                tables.append(relays)
            stations = document.get("stations")
            default = stations.get("default") if isinstance(stations, Mapping) else None
            stream = default.get("stream") if isinstance(default, Mapping) else None
            relays = stream.get("relays") if isinstance(stream, Mapping) else None
            if isinstance(relays, Mapping):
                tables.append(relays)

    paths = StationPaths.default(cfg)
    try:
        definition = json.loads(paths.definition.read_text(encoding="utf-8"))
        stream = (
            definition.get("station", {}).get("stream") if isinstance(definition, dict) else None
        )
        relays = stream.get("relays") if isinstance(stream, Mapping) else None
        if isinstance(relays, Mapping):
            tables.append(relays)
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    try:
        saved = json.loads(paths.relays.read_text(encoding="utf-8"))
        relays = saved.get("relays") if isinstance(saved, dict) else None
        if isinstance(relays, list):
            tables.append(
                {
                    item["name"]: item
                    for item in relays
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }
            )
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass

    names = {name for table in tables for name in table if isinstance(name, str)}
    owners: dict[str, str] = {}
    found: dict[str, str] = {}
    for name in sorted(names):
        variable = f"AUTODJ_STREAM_RELAY_{name.upper().replace('-', '_')}_PASSWORD"
        previous = owners.get(variable)
        if previous is not None and previous != name and variable in environ:
            raise ValueError(
                f"[stream.relays.{name}] and [stream.relays.{previous}] would both read "
                f"{variable}, which is set; rename one of them"
            )
        owners[variable] = name
        if variable in environ:
            found[name] = environ[variable]
    return found


def _upsert_toml_key(path: Path, section: str, key: str, value: object) -> bool:
    """Insert one missing TOML key in place, keeping all existing text intact."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    document = tomllib.loads(text) if text.strip() else {}
    table = document.get(section)
    if isinstance(table, Mapping) and key in table:
        return False
    rendered = json.dumps(value, ensure_ascii=False)
    header = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$", re.MULTILINE)
    match = header.search(text)
    if match is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        updated = f"{text}{suffix}\n[{section}]\n{key} = {rendered}\n"
    else:
        next_header = re.search(
            r"^\s*\[\[?[^\]]+\]\]?\s*(?:#.*)?$", text[match.end() :], re.MULTILINE
        )
        insert_at = match.end() + next_header.start() if next_header else len(text)
        before, after = text[:insert_at], text[insert_at:]
        suffix = "" if not before or before.endswith("\n") else "\n"
        updated = f"{before}{suffix}{key} = {rendered}\n{after}"
    from autodj.fsutil import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, updated)
    return True


def _saved_catalog_timezone(cfg: AutoDJConfig) -> str | None:
    """Read the legacy catalog-wide station time zone without opening it for writes."""
    import sqlite3
    from contextlib import closing

    path = Path(cfg.index.library_dir) / "library.db"
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(readonly_uri(path), uri=True)) as db:
            found = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_settings'"
            ).fetchone()
            if found is None:
                return None
            row = db.execute(
                "SELECT value FROM catalog_settings WHERE key='station_timezone'"
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Could not read the saved station time zone from %s: %s", path, exc)
        return None
    if row is None or not isinstance(row[0], str):
        return None
    try:
        StationConfig(timezone=row[0])
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid saved station time zone in %s", path)
        return None
    return row[0]


def move_station_tables(cfg: AutoDJConfig) -> bool:
    """Migrate legacy root and ``[stations.*]`` TOML into station files.

    Loading stays read-only. Startup calls this once, then reloads the
    canonical station definitions. Existing definitions win over legacy
    values, and secret files are completed on a retry after interruption.

    Returns:
        Whether anything was moved, so the caller loads the configuration
        again.
    """
    from autodj.fsutil import atomic_write
    from autodj.secret_files import update_private_json, write_private_file
    from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN, StationPaths

    config_path = Path(cfg.config_path) if cfg.config_path is not None else None
    local_path = config_path.parent / "config.local.toml" if config_path else None
    shared = _toml_document(config_path) if config_path else {}
    local = _toml_document(local_path) if local_path else {}
    tables = shared.get("stations", {})
    local_tables = local.get("stations", {})
    if not isinstance(tables, Mapping):
        raise TypeError("stations must be named tables such as [stations.nightowl]")
    if not isinstance(local_tables, Mapping):
        raise TypeError("stations must be named tables such as [stations.nightowl]")
    for slug, table in (*tables.items(), *local_tables.items()):
        if slug != DEFAULT_SLUG and (not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug)):
            raise ValueError(
                f"[stations.{slug}]: a station name uses lower-case letters, digits and "
                "inner hyphens, at most 32 characters, such as nightowl"
            )
        if not isinstance(table, Mapping):
            raise TypeError(f"[stations.{slug}] must be a table")
    for slug, table in tables.items():
        if "webhooks" in table:
            raise ValueError(f"[stations.{slug}] webhooks belong only in config.local.toml")
    for slug in local_tables:
        if slug not in tables and not StationPaths.for_station(cfg, slug).definition.is_file():
            raise ValueError(
                f"[stations.{slug}] is in config.local.toml but no station has that ID; "
                "config.local.toml holds only a station's secrets, so correct the name"
            )

    legacy_default: dict[str, Any] = {}
    for section in STATION_SECTION_TYPES:
        name = section.SECTION
        for document, label in ((shared, "config.toml"), (local, "config.local.toml")):
            if name in document and not isinstance(document[name], Mapping):
                raise TypeError(f"{name} section must be a table in {label}")
        base_table = shared.get(name, {})
        local_table = local.get(name, {})
        if isinstance(base_table, Mapping) or isinstance(local_table, Mapping):
            legacy_default[name] = _deep_merge(
                dict(base_table) if isinstance(base_table, Mapping) else {},
                dict(local_table) if isinstance(local_table, Mapping) else {},
            )
    if isinstance(local.get("webhooks"), list):
        legacy_default["webhooks"] = local["webhooks"]
    table_default = tables.get(DEFAULT_SLUG, {}) if isinstance(tables, Mapping) else {}
    local_default = local_tables.get(DEFAULT_SLUG, {}) if isinstance(local_tables, Mapping) else {}
    if isinstance(table_default, Mapping):
        legacy_default = _deep_merge(legacy_default, dict(table_default))
    if isinstance(local_default, Mapping):
        legacy_default = _deep_merge(legacy_default, dict(local_default))

    # The proxy list is machine policy now. Move it to [server] in the same
    # source layer so config.local.toml keeps its usual override precedence.
    base_server = shared.get("server", {})
    local_server = local.get("server", {})
    base_stream = shared.get("stream", {})
    local_stream = local.get("stream", {})
    changed = False
    if (
        local_path is not None
        and isinstance(local_stream, Mapping)
        and "trusted_proxies" in local_stream
        and not (isinstance(local_server, Mapping) and "trusted_proxies" in local_server)
    ):
        changed = (
            _upsert_toml_key(
                local_path, "server", "trusted_proxies", local_stream["trusted_proxies"]
            )
            or changed
        )
    elif (
        config_path is not None
        and isinstance(base_stream, Mapping)
        and "trusted_proxies" in base_stream
        and not (isinstance(base_server, Mapping) and "trusted_proxies" in base_server)
        and not (isinstance(local_server, Mapping) and "trusted_proxies" in local_server)
    ):
        changed = (
            _upsert_toml_key(
                config_path, "server", "trusted_proxies", base_stream["trusted_proxies"]
            )
            or changed
        )

    saved_timezone = _saved_catalog_timezone(cfg)
    old_timezone = base_server.get("station_timezone") if isinstance(base_server, Mapping) else None
    if isinstance(local_server, Mapping) and "station_timezone" in local_server:
        old_timezone = local_server["station_timezone"]
    old_liner_mib = (
        local_server.get("liner_upload_max_mib") if isinstance(local_server, Mapping) else None
    )
    if old_liner_mib is None and isinstance(base_server, Mapping):
        old_liner_mib = base_server.get("liner_upload_max_mib")

    legacy_tables: dict[str, dict[str, Any]] = {DEFAULT_SLUG: legacy_default}
    for slug, table in tables.items():
        if slug == DEFAULT_SLUG or not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
            continue
        if not isinstance(table, Mapping):
            raise TypeError(f"[stations.{slug}] must be a table")
        local_table = local_tables.get(slug, {})
        legacy_tables[slug] = _deep_merge(
            dict(table), dict(local_table) if isinstance(local_table, Mapping) else {}
        )
    if len(set(legacy_tables) - {DEFAULT_SLUG}) > MAX_STATIONS:
        raise ValueError(f"stations allows at most {MAX_STATIONS} stations besides the default")

    # Existing station files are also upgraded: every station gets an
    # explicit output mode and station-local copies of old server settings.
    slugs = list(legacy_tables)
    migrated_specs: dict[str, StationSpec] = {}
    station_root = Path(cfg.index.stations_dir)
    if station_root.is_dir():
        slugs.extend(
            child.name
            for child in station_root.iterdir()
            if child.name != DEFAULT_SLUG
            and SLUG_PATTERN.fullmatch(child.name)
            and child.is_dir()
            and not child.is_symlink()
            and (child / "station.json").is_file()
            and child.name not in slugs
        )
        for slug in slugs:
            if slug != DEFAULT_SLUG and slug not in legacy_tables:
                local_table = local_tables.get(slug, {})
                legacy_tables[slug] = dict(local_table) if isinstance(local_table, Mapping) else {}
    for index, slug in enumerate(slugs):
        paths = (
            StationPaths.default(cfg)
            if slug == DEFAULT_SLUG
            else StationPaths.for_station(cfg, slug)
        )
        path = paths.definition
        if path.is_symlink():
            raise ValueError(f"station definition must not be a symlink: {path}")
        table = legacy_tables.get(slug, {})
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or document.get("version") != 1
                or not isinstance(document.get("station"), dict)
            ):
                raise ValueError(f"{path} needs a version 1 station table")
            table = _deep_merge(table, document["station"])
        else:
            document = {
                "version": 1,
                "created": datetime(2000, 1, 1, tzinfo=UTC).replace(second=index).isoformat(),
                "source": None,
            }
        migrate_legacy_environment = (
            slug == DEFAULT_SLUG
            and bool(cfg.legacy_station_environment)
            and document.get("legacy_environment_migrated") is not True
        )
        if migrate_legacy_environment:
            table = _deep_merge(table, cfg.legacy_station_environment)
        stream = table.get("stream", {})
        if not isinstance(stream, Mapping):
            raise TypeError(f"[stations.{slug}.stream] must be a table")
        stream = dict(stream)
        old_enabled = stream.pop("enabled", None)
        stream.pop("trusted_proxies", None)
        table["stream"] = stream
        station = table.get("station", {})
        if not isinstance(station, Mapping):
            raise TypeError(f"[stations.{slug}.station] must be a table")
        station = dict(station)
        if "output" not in station:
            station["output"] = (
                ("stream" if old_enabled else "browser")
                if slug == DEFAULT_SLUG and isinstance(old_enabled, bool)
                else ("browser" if slug == DEFAULT_SLUG else "stream")
            )
        if "timezone" not in station:
            timezone = saved_timezone or old_timezone
            if timezone is not None:
                station["timezone"] = timezone
        if "liner_upload_max_mib" not in station and old_liner_mib is not None:
            station["liner_upload_max_mib"] = old_liner_mib
        table["station"] = station
        playback = table.get("playback")
        if isinstance(playback, Mapping) and RETIRED_PLAYBACK_KEY in playback:
            # Tracks of any length play now (#391): the old limit is dropped.
            table["playback"] = {k: v for k, v in playback.items() if k != RETIRED_PLAYBACK_KEY}
        if slug == DEFAULT_SLUG:
            time_checks = table.get("time_checks", {})
            if isinstance(time_checks, Mapping) and not time_checks.get("clips_folder"):
                legacy_clips = dict(time_checks)
                legacy_clips["clips_folder"] = str(Path(cfg.index.shared_dir) / "time_checks")
                table["time_checks"] = legacy_clips
        prefix_token = _TABLE_PREFIX.set(f"stations.{slug}.")
        try:
            candidate = deepcopy(table)
            candidate_stream = candidate.get("stream")
            candidate_relays = (
                candidate_stream.get("relays") if isinstance(candidate_stream, dict) else None
            )
            if isinstance(candidate_relays, dict):
                for relay in candidate_relays.values():
                    if isinstance(relay, dict):
                        relay.setdefault("password", "migration-only-placeholder")
            migrated_specs[slug] = _build_station(slug, candidate, cfg)
        except (TypeError, ValueError, KeyError) as exc:
            if isinstance(exc, KeyError):
                raise KeyError(f"[stations.{slug}] {exc.args[0] if exc.args else exc}") from exc
            error_type = TypeError if isinstance(exc, TypeError) else ValueError
            raise error_type(f"[stations.{slug}] {exc}") from exc
        finally:
            _TABLE_PREFIX.reset(prefix_token)
        clean = _without_secrets(table)
        if document.get("station") != clean or migrate_legacy_environment:
            path.parent.mkdir(parents=True, exist_ok=True)
            document["station"] = clean
            if migrate_legacy_environment:
                document["legacy_environment_migrated"] = True
            atomic_write(path, json.dumps(document, indent=2) + "\n")
            changed = True
            logger.warning("Migrated station settings into %s.", path)

        secrets = _secrets_of(legacy_tables.get(slug, {}))
        if slug == DEFAULT_SLUG and isinstance(local.get("webhooks"), list):
            secrets["webhooks"] = local["webhooks"]
        if secrets and path.exists():
            secret_stream = secrets.get("stream", {})
            relays = secret_stream.pop("relays", None) if isinstance(secret_stream, dict) else None
            if relays and not paths.relays.exists():
                entries = [{"name": name, **relay} for name, relay in relays.items()]
                if slug == DEFAULT_SLUG:
                    for item in entries:
                        if item["name"] in cfg.legacy_relay_environment:
                            item["password"] = cfg.legacy_relay_environment[item["name"]]
                relay_document: dict[str, Any] = {"version": 1, "relays": entries}
                if slug == DEFAULT_SLUG and cfg.legacy_relay_environment:
                    relay_document["legacy_environment_migrated"] = True
                write_private_file(
                    paths.relays,
                    json.dumps(relay_document, indent=2) + "\n",
                )
                changed = True
            if isinstance(secret_stream, dict) and not secret_stream:
                secrets.pop("stream", None)
            if secrets:

                def add(document: dict[str, Any], secrets: dict[str, Any] = secrets) -> bool:
                    # A secrets.json holding only a Stereo Tool key has none of these yet.
                    if any(key in document for key in ("stream", "webhooks")):
                        return False
                    document.update(secrets)
                    return True

                try:
                    changed = update_private_json(paths.secrets, add) or changed
                except ValueError as exc:
                    logger.error("Did not move the secrets of [stations.%s]: %s", slug, exc)
        if slug == DEFAULT_SLUG and cfg.legacy_relay_environment and paths.relays.exists():
            try:
                relay_document = json.loads(paths.relays.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"cannot safely migrate old relay environment: {paths.relays}"
                ) from exc
            if (
                not isinstance(relay_document, dict)
                or relay_document.get("version") != 1
                or not isinstance(relay_document.get("relays"), list)
            ):
                raise ValueError(f"cannot safely migrate old relay environment: {paths.relays}")
            if relay_document.get("legacy_environment_migrated") is not True:
                for item in relay_document["relays"]:
                    if isinstance(item, dict) and item.get("name") in cfg.legacy_relay_environment:
                        item["password"] = cfg.legacy_relay_environment[item["name"]]
                relay_document["legacy_environment_migrated"] = True
                write_private_file(paths.relays, json.dumps(relay_document, indent=2) + "\n")
                changed = True
    if DEFAULT_SLUG in migrated_specs:
        _check_station_conflicts(
            migrated_specs[DEFAULT_SLUG].cfg,
            (spec for slug, spec in migrated_specs.items() if slug != DEFAULT_SLUG),
        )
    return changed


def _read_secrets(path: Path) -> dict[str, Any]:
    """A station's secrets.json, without its version; empty when it has none.

    Its Stereo Tool key (``stereo_tool``) is not a station setting and is left
    out: :mod:`autodj.stereotool_setup` reads it.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(document, dict) or document.pop("version", None) != 1:
        raise ValueError(f"{path.name} needs version 1")
    document.pop("stereo_tool", None)
    stream = document.get("stream", {})
    if set(document) - {"stream", "webhooks"} or (
        not isinstance(stream, dict) or set(stream) - {"listener_user", "listener_password"}
    ):
        raise ValueError(
            f"{path.name} holds only listener_user, listener_password, webhooks and stereo_tool"
        )
    return document


def _definition_spec(
    path: Path,
    slug: str,
    root: AutoDJConfig,
    existing: tuple[StationSpec, ...],
    *,
    document: object | None = None,
    secrets: Mapping[str, Any] | None = None,
) -> StationSpec:
    """Validate a station's definition; *secrets* come from its secrets.json."""
    if document is None:
        document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] != 1
    ):
        raise ValueError("station.json needs version 1")
    created = document.get("created")
    try:
        timestamp = datetime.fromisoformat(created) if isinstance(created, str) else None
    except ValueError as exc:
        raise ValueError("created must be an ISO 8601 UTC timestamp") from exc
    if (
        timestamp is None
        or timestamp.tzinfo is None
        or timestamp.utcoffset() != UTC.utcoffset(timestamp)
    ):
        raise ValueError("created must be an ISO 8601 UTC timestamp")
    if document.get("source") is not None and not isinstance(document["source"], str):
        raise ValueError("source must be a station ID or null")
    table = document.get("station")
    if not isinstance(table, dict):
        raise ValueError("station must be a table")
    stream = table.get("stream", {})
    if "webhooks" in table or (
        isinstance(stream, dict)
        and any(key in stream for key in ("relays", "listener_user", "listener_password"))
    ):
        raise ValueError("station.json must not contain secrets")
    if slug != "default" and len(existing) >= MAX_STATIONS:
        raise ValueError(f"stations allows at most {MAX_STATIONS} stations besides the default")
    spec = replace(
        _build_station(slug, _deep_merge(table, dict(secrets or {})), root),
        defined_in="web",
    )
    if slug != "default":
        _check_station_conflicts(root, (*existing, spec))
    return spec


def _load_web_stations(
    cfg: AutoDJConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Load the canonical default and every other station from their files."""
    folder = Path(cfg.index.stations_dir)
    from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN, StationPaths

    skipped: list[tuple[str, str]] = []
    default_path = StationPaths.default(cfg).definition
    try:
        if default_path.exists() and not stat.S_ISREG(default_path.lstat().st_mode):
            raise ValueError(f"station definition must be a regular file: {default_path}")
        if default_path.is_file():
            document = json.loads(default_path.read_text(encoding="utf-8"))
            if isinstance(document, dict) and isinstance(document.get("station"), dict):
                document["station"] = _deep_merge(
                    document["station"],
                    _station_environment_overlay(DEFAULT_SLUG, environment or {}),
                )
            cfg.default_station = _definition_spec(
                default_path,
                DEFAULT_SLUG,
                cfg,
                (),
                document=document,
                secrets=_read_secrets(default_path.with_name("secrets.json")),
            )
        else:
            cfg.default_station = replace(
                _build_station(
                    DEFAULT_SLUG,
                    _station_environment_overlay(DEFAULT_SLUG, environment or {}),
                    cfg,
                ),
                defined_in="default",
            )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        reason = str(exc)
        logger.error("Skipping %s: %s", default_path, reason)
        skipped.append((str(default_path), reason))
        cfg.default_station = replace(_build_station(DEFAULT_SLUG, {}, cfg), defined_in="default")
    _project_default_station(cfg)
    if not folder.is_dir():
        cfg.skipped_definitions = tuple(skipped)
        return
    found: list[tuple[str, Path]] = []
    for child in sorted(folder.iterdir()):
        slug = child.name
        if (
            slug == DEFAULT_SLUG
            or not SLUG_PATTERN.fullmatch(slug)
            or child.is_symlink()
            or child.is_junction()
            or not child.is_dir()
        ):
            continue
        path = StationPaths.for_station(cfg, slug).definition
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            reason = str(exc)
            logger.error("Skipping %s: %s", path, reason)
            skipped.append((str(path), reason))
            continue
        found.append((slug, path))
    documents = []
    for slug, path in found:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            reason = str(exc)
            logger.error("Skipping %s: %s", path, reason)
            skipped.append((str(path), reason))
            continue
        created = document.get("created") if isinstance(document, dict) else None
        documents.append((str(created), slug, path, document))
    # Stations moved from config.toml first, in its order, then the page's.
    for _created, slug, path, document in sorted(documents, key=lambda item: item[:2]):
        try:
            spec = _definition_spec(
                path,
                slug,
                cfg,
                cfg.stations,
                document=(
                    {
                        **document,
                        "station": _deep_merge(
                            document.get("station", {}),
                            _station_environment_overlay(slug, environment or {}),
                        ),
                    }
                    if isinstance(document, dict)
                    else document
                ),
                secrets=_read_secrets(path.with_name("secrets.json")),
            )
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            reason = str(exc)
            logger.error("Skipping %s: %s", path, reason)
            skipped.append((str(path), reason))
            continue
        cfg.stations += (spec,)
    _check_station_conflicts(cfg, cfg.stations)
    _validate_relay_environment(cfg, environment or {})
    cfg.skipped_definitions = tuple(skipped)


def _validate_relay_environment(cfg: AutoDJConfig, environment: Mapping[str, str]) -> None:
    """Reject set password variables that identify relays in multiple stations."""
    from autodj.stations import StationPaths

    owners: dict[str, str] = {}
    for slug in ("default", *(spec.slug for spec in cfg.stations)):
        paths = (
            StationPaths.default(cfg) if slug == "default" else StationPaths.for_station(cfg, slug)
        )
        try:
            document = json.loads(paths.relays.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        relays = document.get("relays") if isinstance(document, dict) else None
        if not isinstance(relays, list):
            continue
        for item in relays:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str):
                continue
            variable = (
                relay_password_variable(name)
                if slug == "default"
                else station_relay_password_variable(slug, name)
            )
            previous = owners.get(variable)
            if previous is not None and variable in environment:
                raise ValueError(
                    f"[{slug}.stream.relays.{name}] and [{previous}] would both read "
                    f"{variable}, which is set; rename one of them"
                )
            owners[variable] = f"{slug}.stream.relays.{name}"


def _project_default_station(cfg: AutoDJConfig) -> None:
    """Expose the default station's config through legacy root accessors."""
    assert cfg.default_station is not None
    for section in STATION_SECTION_TYPES:
        setattr(cfg, section.SECTION, getattr(cfg.default_station.cfg, section.SECTION))


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
    presets_raw = table.get("presets", {})
    if not isinstance(presets_raw, Mapping):
        raise TypeError("presets section must be a table")
    station_raw = table.get("station", {})
    if not isinstance(station_raw, Mapping):
        raise TypeError("station section must be a table")
    station_raw = dict(station_raw)
    station_raw.setdefault("output", "browser" if slug == "default" else "stream")
    sections: dict[str, Any] = {
        t.SECTION: t.from_dict(stream_raw if t is StreamConfig else table.get(t.SECTION, {}))
        for t in STATION_SECTION_TYPES
    }
    sections["station"] = StationConfig.from_dict(station_raw)
    if (
        not isinstance(stream_raw, Mapping) or "station_name" not in stream_raw
    ) and slug != "default":
        sections["stream"].station_name = slug
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
        _check_own_folder(slug, cfg)
        for relay in cfg.stream.relays.values():
            target = _relay_target(relay)
            other = targets.get(target)
            if other is not None and other != slug:
                raise ValueError(
                    f"stations {other} and {slug} both relay to {relay.host}:{relay.port}"
                    f"{relay.mount or ''}; each station needs its own relay target"
                )
            targets[target] = slug


def _check_own_folder(slug: str, cfg: AutoDJConfig) -> None:
    """Refuse a station folder setting that holds the station's own state folder.

    Restoring that folder would replace the station's settings, schedule and
    carts with it, and its files would be backed up twice.

    Raises:
        ValueError: Naming the setting and the folders.
    """
    state = os.path.normcase(os.path.abspath(Path(cfg.index.stations_dir) / slug))
    for setting, value in (("time_checks.clips_folder", cfg.time_checks.clips_folder),):
        if not value:
            continue
        folder = os.path.normcase(os.path.abspath(value))
        if state == folder or state.startswith(folder.rstrip(os.sep) + os.sep):
            raise ValueError(
                f"[stations.{slug}] {setting} {value} holds the station's own folder {state}; "
                "choose a folder beside it"
            )


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
    shared_names = {section.SECTION for section in _SECTION_TYPES} | {"presets"}

    if candidate.exists():
        with candidate.open("rb") as fh:
            shared = tomllib.load(fh)
            if "webhooks" in shared:
                raise ValueError("webhooks belong only in config.local.toml")
            raw = _deep_merge(
                raw, {key: value for key, value in shared.items() if key in shared_names}
            )
        loaded_path = candidate
        sources.append(str(candidate))
    elif explicit:
        raise FileNotFoundError(f"Config file not found: {candidate}")

    if loaded_path is not None:
        local_path = loaded_path.parent / "config.local.toml"
        if local_path.exists():
            with local_path.open("rb") as fh:
                local = tomllib.load(fh)
            raw = _deep_merge(
                raw, {key: value for key, value in local.items() if key in shared_names}
            )
            sources.append(str(local_path))

    env_raw = _environment_overlay(environment)
    env_shared = {key: value for key, value in env_raw.items() if key in shared_names}
    legacy_station_environment = _legacy_station_environment(environment)
    if (
        env_shared
        or legacy_station_environment
        or any(
            key.startswith("AUTODJ_STATION_") or key.startswith("AUTODJ_STREAM_RELAY_")
            for key in environment
        )
    ):
        raw = _deep_merge(raw, env_shared)
        sources.append("environment")

    presets_path = candidate.parent / "presets.toml"
    if presets_path.exists():
        raise ValueError(
            f"{presets_path} is no longer read: move each [NAME] table into config.toml "
            "as [presets.NAME], then delete presets.toml"
        )
    return _build_config(
        raw,
        config_path=loaded_path,
        sources=sources,
        legacy_station_environment=legacy_station_environment,
        environment=environment,
    )
