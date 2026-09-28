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
import os
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import MISSING, dataclass, field, fields
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self
from urllib.parse import urlsplit

from autodj.liners import LINER_PICK_MODES

if TYPE_CHECKING:
    from autodj.presets import Preset


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
            ValueError: On an unknown key or an invalid value.
        """
        if not isinstance(data, Mapping):
            raise TypeError(f"{cls.SECTION} section must be a table")
        _reject_unknown_keys(cls.SECTION, data, _field_names(cls))
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name not in data and f.default is MISSING and f.default_factory is MISSING:
                raise KeyError(f"config.toml [{cls.SECTION}] section is missing {f.name!r}")
        return cls(**data)


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
    """

    SECTION: ClassVar[str] = "library"

    music_dir: Path
    beets_db: Path | None = None
    supported_formats: list[str] = field(default_factory=lambda: ["mp3", "flac", "m4a"])

    def __post_init__(self) -> None:
        """Expand ``~`` in the paths; an empty ``beets_db`` means none."""
        self.music_dir = Path(self.music_dir).expanduser()
        self.beets_db = Path(self.beets_db).expanduser() if self.beets_db else None


@dataclass
class IndexConfig(_Section):
    """Settings for the FAISS index storage locations.

    AutoDJ supports **named indexes** so you can keep multiple curated
    libraries side-by-side — a "workout" index of high-BPM tracks, a
    "chill" index for evening listening, etc.  Each named index lives
    in its own sub-directory ``<index_dir>/<name>/`` so they share
    nothing (independent FAISS files, metadata, runtime state, dj-meta
    cache).

    Attributes:
        index_dir: Base directory holding all named indexes.
        model_dir: Directory where the MuQ model checkpoint is cached.
        name: Active index name.  Files live at
            ``<index_dir>/<name>/vectors.index`` etc.  Override with
            ``--name`` on any CLI subcommand.
    """

    SECTION: ClassVar[str] = "index"

    index_dir: Path = field(default_factory=lambda: Path("index"))
    model_dir: Path = field(default_factory=lambda: Path("models"))
    name: str = "default"

    def __post_init__(self) -> None:
        """Expand the paths and validate the index name.

        Raises:
            ValueError: If ``name`` contains path separators / traversal /
                leading dot — names are bare identifiers, not paths.
        """
        self.index_dir = Path(self.index_dir).expanduser()
        self.model_dir = Path(self.model_dir).expanduser()
        self.name = str(self.name).strip() or "default"
        validate_index_name(self.name)

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
)

KEY_NOTATIONS: tuple[str, ...] = (
    "camelot",
    "musical",
)


POST_QUEUE_SEED_MODES = ("last_queued", "pre_queue")


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


def _reject_unknown_keys(section: str, data: Mapping[str, Any], known: Iterable[str]) -> None:
    """Raise if a config section holds a key outside *known*.

    Removed and misspelled options must fail loudly rather than be ignored.

    Args:
        section: Section name used in the error message, e.g. ``"library"``.
        data: Raw keys of that section.
        known: Accepted key names.

    Raises:
        ValueError: If *data* has any key not in *known*.
    """
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown [{section}] keys: {sorted(unknown)}")


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


@dataclass
class PlaybackConfig(_Section):
    """Settings for audio playback behaviour.

    Attributes:
        crossfade_seconds: Duration of the crossfade between tracks in seconds.
            Set to ``0.0`` to disable crossfade entirely.
        no_repeat_window: Number of recently played tracks excluded from the
            next-song candidate pool.
        history_file: Optional path to a JSON Lines file where every played
            track is appended with a timestamp.  ``None`` disables history.
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
    """

    SECTION: ClassVar[str] = "playback"

    crossfade_seconds: float = 3.0
    fade_in_seconds: float = 3.0
    # Memory of recently-played tracks excluded from candidate pool.  Larger
    # numbers = the auto-DJ has to traverse more of the library before
    # revisiting any track.  Default 500 — comfortable for libraries of a
    # few thousand tracks; bump higher for larger collections.
    no_repeat_window: int = 500
    artist_repeat_window: int = 3
    history_file: Path | None = None
    discovery_every: int | None = None
    crossfade_eq_duck: bool = False
    crossfade_bass_cutoff_hz: float = 180.0
    # Mixxx-style transition mode.  Controls how the crossfade aligns
    # with each track's intro_end / outro_start markers from the
    # DJ-meta sidecar.
    #   - "full_intro_outro" (default): start of incoming intro lines up
    #     with start of outgoing outro; fade length = min(intro_len,
    #     outro_len) clamped to [_MIN_FX_DURATION_S, 12 s].
    #   - "outro_fade":  begin fade at outro_start, length = outro_len.
    #     Ignores intro_end.
    #   - "fixed_skip_silence": fixed crossfade_seconds, but trim
    #     leading silence on incoming + trailing silence on outgoing.
    #   - "fixed": plain fixed crossfade_seconds at the
    #     end of the outgoing track.  No marker alignment.
    transition_mode: str = "full_intro_outro"
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
    # When False, the player never loads / renders lyrics (CLI panel + web
    # UI lyric card both honour this).  Default True — opt-out, not opt-in.
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
    # the device name.  Set via [playback] audio_device or `--device` CLI.
    audio_device: str | int | None = None
    # Wall-clock daypart targeting.  When True, the picker biases
    # candidate ranking toward the BPM/energy of the active built-in
    # daypart (morning/midday/afternoon/evening/night) -- only applied
    # when no explicit preset is active.  Lets unattended playback
    # follow time of day automatically.
    enable_daypart: bool = False
    # Set-relative mood arc (warmup -> peak -> cool envelope).  When
    # both daypart and arc are enabled, arc takes priority while a
    # session is in progress; daypart is the idle-baseline.
    enable_mood_arc: bool = False
    # Hours over which the mood arc spans before looping.  Default 3 h
    # = standard club set length.
    mood_arc_hours: float = 3.0
    # Auto-discover cue points from external DJ software (Mixxx,
    # Rekordbox, Traktor) and merge with auto-detected cues.  Off
    # only when the user wants the auto-detected cues alone.
    import_external_cues: bool = True
    # Beat-sync transition FX: rhythmic effects (beat_repeat,
    # gate_stutter, echo_out, dub_delay, sidechain_pump, halftime,
    # stutter_build, scratch) snap their start to the next outgoing
    # downbeat and size their internal events to whole bars at a BPM
    # blended from outgoing -> incoming track tempo.  Envelope FX
    # (sweeps, risers) bar-round their length but don't snap start.
    # Falls back to seconds-based timing when no beat grid /
    # tempo is known.  Default ON.
    beat_sync_fx: bool = True
    # Key-sync pitched FX: oscillator-based effects (pitch_swell,
    # pitch_fall, dub_siren, ring_modulator, air_horn) tune their
    # carrier frequency to the song's root note.  Lerps in log space
    # from outgoing root -> incoming root across the fade.  Default ON.
    key_sync_fx: bool = True
    # Beatmatch on skip: when the user presses Skip / N hotkey mid-track,
    # the browser-side crossfade applies playbackRate = outgoing_bpm /
    # incoming_bpm to the standby deck (preservesPitch=true) so the new
    # track joins the existing groove instead of cold-cutting at its
    # native tempo.  Reverts at fade-out.  Off by default — keeps the
    # "skip = clean break" behaviour for users who want it.
    # CLI server-audio skip path cannot pitch-stretch on the fly so
    # it cold-cuts regardless of this flag.
    beatmatch_on_skip: bool = False
    # Voice liners — DJ-style spoken drops layered over the live mix.
    # ``liners_folder`` is the source directory (default
    # ``<index_dir>/liners`` resolved at server startup).  Trigger
    # parameters are evaluated client-side in the browser; the server
    # exposes the file list via ``GET /api/liners`` and raw bytes via
    # ``GET /api/liners/file/<name>``.
    liners_enabled: bool = False
    liners_folder: str | None = None
    liners_every_n_songs: int | None = None
    liners_every_minutes: float | None = None
    liners_random_min_minutes: float | None = None
    liners_random_max_minutes: float | None = None
    liners_pick_mode: str = "random"
    liners_duck_db: float = -12.0
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
            ValueError: If ``crossfade_seconds``, ``fade_in_seconds`` or
                ``no_repeat_window`` is negative, a choice is not one of its
                options, or ``server_max_track_minutes`` is outside 1-600.
        """
        self.crossfade_seconds = float(self.crossfade_seconds)
        if self.crossfade_seconds < 0:
            raise ValueError(
                f"playback.crossfade_seconds must be >= 0, got {self.crossfade_seconds}"
            )
        self.no_repeat_window = int(self.no_repeat_window)
        if self.no_repeat_window < 0:
            raise ValueError(f"playback.no_repeat_window must be >= 0, got {self.no_repeat_window}")
        self.fade_in_seconds = float(self.fade_in_seconds)
        if self.fade_in_seconds < 0:
            raise ValueError(f"playback.fade_in_seconds must be >= 0, got {self.fade_in_seconds}")
        max_minutes = self.server_max_track_minutes
        if isinstance(max_minutes, bool) or not isinstance(max_minutes, int | float):
            raise TypeError("playback.server_max_track_minutes must be a number")
        if not 1 <= max_minutes <= 600:
            raise ValueError(
                f"playback.server_max_track_minutes must be between 1 and 600, got {max_minutes}"
            )
        self.server_max_track_minutes = float(max_minutes)
        self.artist_repeat_window = max(0, int(self.artist_repeat_window))
        self.history_file = Path(self.history_file).expanduser() if self.history_file else None
        self.discovery_every = _optional(int, self.discovery_every)
        self.crossfade_bass_cutoff_hz = float(self.crossfade_bass_cutoff_hz)
        self.transition_mode = _validate_transition_mode(str(self.transition_mode))
        self.post_queue_seed = _validate_post_queue_seed(str(self.post_queue_seed))
        self.pick_top_k = max(1, int(self.pick_top_k))
        self.pick_temperature = max(0.0, float(self.pick_temperature))
        self.key_notation = _validate_key_notation(str(self.key_notation))
        self.audio_device = self.audio_device or None
        self.mood_arc_hours = max(0.25, float(self.mood_arc_hours))
        for name in (
            "crossfade_eq_duck",
            "key_prefer_flats",
            "show_lyrics",
            "prefetch_next_track",
            "silence_trigger_crossfade",
            "enable_daypart",
            "enable_mood_arc",
            "import_external_cues",
            "beat_sync_fx",
            "key_sync_fx",
            "beatmatch_on_skip",
            "liners_enabled",
        ):
            setattr(self, name, bool(getattr(self, name)))
        self.liners_folder = self.liners_folder or None
        self.liners_every_n_songs = _optional(int, self.liners_every_n_songs)
        self.liners_every_minutes = _optional(float, self.liners_every_minutes)
        self.liners_random_min_minutes = _optional(float, self.liners_random_min_minutes)
        self.liners_random_max_minutes = _optional(float, self.liners_random_max_minutes)
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
        beatmatch: When ``True``, the incoming track is pitch-stretched
            (up to ±``beatmatch_max_stretch``) so its BPM matches the
            outgoing track during the crossfade.  Requires both tracks
            to have a known BPM in the index.
        beatmatch_max_stretch: Maximum allowed stretch ratio deviation
            from 1.0.  ``0.08`` = ±8 % (typical DJ practice).
        outro_intro_align: When ``True``, the crossfade is positioned
            against the outgoing track's outro start and incoming
            track's intro end (auto-detected on first play).  Avoids
            cold-cutting into a 4-bar intro.
        phrase_align: When ``True``, the crossfade start time is snapped
            to the nearest 8-bar phrase boundary (uses the cached beat
            grid).
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
    """

    SECTION: ClassVar[str] = "djmix"

    beatmatch: bool = False
    beatmatch_max_stretch: float = 0.08
    outro_intro_align: bool = False
    phrase_align: bool = False
    phrase_bars: int = 8
    filter_sweep: bool = False
    filter_sweep_floor_hz: float = 250.0
    harmonic_mode: str = "off"

    def __post_init__(self) -> None:
        """Coerce the TOML values and validate ``harmonic_mode``.

        Raises:
            ValueError: If ``harmonic_mode`` is not one of
                :data:`autodj.dj_meta.HARMONIC_MODES`.
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
        self.outro_intro_align = bool(self.outro_intro_align)
        self.phrase_align = bool(self.phrase_align)
        self.phrase_bars = int(self.phrase_bars)
        self.filter_sweep = bool(self.filter_sweep)
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
            ``"rotate"`` (cycle through all real effects in order).
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
        max_clip_safe_gain: Hard cap on the linear gain so peaks never
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
    """Web-server bind, request policy, session, and upload limits."""

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

    def __post_init__(self) -> None:
        """Normalize and validate server settings after initialization."""
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
        if "liner_upload_max_bytes" in data:
            raise ValueError("unknown [server] keys: ['liner_upload_max_bytes']")
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
    """Normalize mutable overrides and reject unsafe bind configurations."""
    cfg.__post_init__()
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


@dataclass
class StreamConfig(_Section):
    """Radio stream settings (``[stream]``).

    Attributes:
        enabled: Serve in stream mode.
        bitrate: MP3 bitrate in kbps, one of :data:`STREAM_BITRATES`.
        idle_grace_seconds: Seconds without listeners before the set stops.
        max_listeners: Concurrent listener limit.
        station_name: Name sent to players as ``icy-name``.
    """

    SECTION: ClassVar[str] = "stream"

    enabled: bool = False
    bitrate: int = 320
    idle_grace_seconds: float = 30.0
    max_listeners: int = 8
    station_name: str = "AutoDJ"

    def __post_init__(self) -> None:
        """Validate every field."""
        if not isinstance(self.enabled, bool):
            raise TypeError("stream.enabled must be true or false")
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
        if not isinstance(self.station_name, str) or not self.station_name.strip():
            raise ValueError("stream.station_name must be a non-empty string")
        if not self.station_name.isprintable():
            # It is sent as an HTTP header (icy-name): no control characters.
            raise ValueError("stream.station_name must not contain control characters")
        self.idle_grace_seconds = float(self.idle_grace_seconds)


# ---------------------------------------------------------------------------
# Root config dataclass
# ---------------------------------------------------------------------------


@dataclass
class AutoDJConfig:
    """Root configuration for the AutoDJ application.

    Attributes:
        library: Library location and format settings.
        index: FAISS index storage settings.
        playback: Playback behaviour settings.
        model: MuQ model settings.
        huggingface: HuggingFace Hub access settings.
        presets: User-defined BPM presets loaded from ``[presets.*]`` sections.
        config_path: Path to the config file this instance was loaded from.
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
    config_sources: tuple[str, ...] = ("defaults",)


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
)


def _build_config(
    raw: dict[str, Any],
    *,
    config_path: Path | None,
    sources: list[str],
    presets_raw: Any,
) -> AutoDJConfig:
    """Validate raw sections and construct the typed application configuration."""
    from autodj.presets import load_user_presets

    if not isinstance(presets_raw, Mapping):
        raise TypeError("presets section must be a table")

    unknown_sections = set(raw) - {t.SECTION for t in _SECTION_TYPES} - {"presets"}
    if unknown_sections:
        raise ValueError(f"unknown config sections: {sorted(unknown_sections)}")
    sections: dict[str, Any] = {
        t.SECTION: t.from_dict(raw.get(t.SECTION, {})) for t in _SECTION_TYPES
    }
    return AutoDJConfig(
        **sections,
        presets=load_user_presets(presets_raw),
        config_path=config_path,
        config_sources=tuple(sources),
    )


def load_config(
    path: str | Path | None = None, *, environ: Mapping[str, str] | None = None
) -> AutoDJConfig:
    """Load defaults, optional TOML overlays, then typed environment overrides.

    An omitted path uses ``config.toml`` when present and otherwise keeps
    validated defaults. An explicitly supplied missing path remains an error.
    """
    environment = os.environ if environ is None else environ
    explicit = path is not None
    candidate = Path(path) if path is not None else Path("config.toml")
    raw = _default_raw()
    sources = ["defaults"]
    loaded_path: Path | None = None

    if candidate.exists():
        with candidate.open("rb") as fh:
            raw = _deep_merge(raw, tomllib.load(fh))
        loaded_path = candidate
        sources.append(str(candidate))
    elif explicit:
        raise FileNotFoundError(f"Config file not found: {candidate}")

    if loaded_path is not None:
        local_path = loaded_path.parent / "config.local.toml"
        if local_path.exists():
            with local_path.open("rb") as fh:
                raw = _deep_merge(raw, tomllib.load(fh))
            sources.append(str(local_path))

    env_raw = _environment_overlay(environment)
    if env_raw:
        raw = _deep_merge(raw, env_raw)
        sources.append("environment")

    sidecar_root = loaded_path.parent if loaded_path is not None else Path.cwd()
    presets_path = sidecar_root / "presets.toml"
    if presets_path.exists():
        with presets_path.open("rb") as fh:
            presets_raw = tomllib.load(fh)
    else:
        presets_raw = raw.get("presets", {})
    return _build_config(
        raw,
        config_path=loaded_path,
        sources=sources,
        presets_raw=presets_raw,
    )
