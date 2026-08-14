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
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from autodj.presets import Preset


# ---------------------------------------------------------------------------
# Sub-section dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LibraryConfig:
    """Settings for the music library location and format filtering.

    Attributes:
        music_dir: Path to the root music folder (local or NAS mapped drive).
            For beets users, this should match the local mount point of the
            beets ``directory`` setting — relative paths stored in the beets
            database are resolved against ``music_dir``.
        beets_db: Optional path to the beets SQLite library database.
        supported_formats: List of audio file extensions to index (without dots).
        path_remap: Optional list of ``(from_prefix, to_prefix)`` pairs applied
            to absolute paths stored in the index when the current machine
            mounts the library at a different location.  Useful for cross-OS
            shared indexes built on another host.  Each entry is a two-element
            list in TOML, e.g. ``path_remap = [["/mnt/music/", "Z:/Music/"]]``.
    """

    music_dir: Path
    beets_db: Path | None
    supported_formats: list[str]
    path_remap: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LibraryConfig:
        """Construct a LibraryConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[library]`` TOML section.

        Returns:
            A populated LibraryConfig instance.

        Raises:
            KeyError: If ``music_dir`` is not present.
            ValueError: If ``path_remap`` is malformed.
        """
        if "music_dir" not in data:
            raise KeyError("config.toml [library] section is missing 'music_dir'")
        beets_raw = data.get("beets_db")
        remap_raw = data.get("path_remap", [])
        remap: list[tuple[str, str]] = []
        for pair in remap_raw:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(
                    f"library.path_remap entries must be [from, to] pairs, got: {pair!r}"
                )
            remap.append((str(pair[0]), str(pair[1])))
        return cls(
            music_dir=Path(data["music_dir"]).expanduser(),
            beets_db=Path(beets_raw).expanduser() if beets_raw else None,
            supported_formats=data.get("supported_formats", ["mp3", "flac", "m4a"]),
            path_remap=remap,
        )


@dataclass
class IndexConfig:
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

    index_dir: Path = field(default_factory=lambda: Path("index"))
    model_dir: Path = field(default_factory=lambda: Path("models"))
    name: str = "default"

    @property
    def active_dir(self) -> Path:
        """Resolved location of the active named index."""
        return self.index_dir / self.name

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IndexConfig:
        """Construct an IndexConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[index]`` TOML section.

        Returns:
            A populated IndexConfig instance with defaults applied for missing keys.

        Raises:
            ValueError: If ``name`` contains path separators / traversal /
                leading dot — names are bare identifiers, not paths.
        """
        name = str(data.get("name", "default")).strip() or "default"
        validate_index_name(name)
        return cls(
            index_dir=Path(data.get("index_dir", "index")).expanduser(),
            model_dir=Path(data.get("model_dir", "models")).expanduser(),
            name=name,
        )


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


def _validate_key_notation(value: str) -> str:
    """Return *value* unchanged if it is a known key notation, else raise.

    Raises:
        ValueError: If *value* is not in :data:`KEY_NOTATIONS`.
    """
    if value not in KEY_NOTATIONS:
        raise ValueError(
            f"playback.key_notation must be one of {KEY_NOTATIONS}, got {value!r}",
        )
    return value


POST_QUEUE_SEED_MODES = ("last_queued", "pre_queue")


def _validate_post_queue_seed(value: str) -> str:
    """Return *value* unchanged if known, else raise.

    ``last_queued`` (default) seeds similarity from the final queued
    track once the queue empties.  ``pre_queue`` rewinds and seeds
    from the track that was playing when the queue was first added,
    so the queue acts as a detour.
    """
    if value not in POST_QUEUE_SEED_MODES:
        raise ValueError(
            f"playback.post_queue_seed must be one of {POST_QUEUE_SEED_MODES}, got {value!r}",
        )
    return value


def _validate_transition_mode(value: str) -> str:
    """Return *value* unchanged if it is a known transition mode, else raise.

    Args:
        value: Mode string from ``[playback] transition_mode``.

    Returns:
        The validated mode string.

    Raises:
        ValueError: If *value* is not in :data:`TRANSITION_MODES`.
    """
    if value not in TRANSITION_MODES:
        raise ValueError(
            f"playback.transition_mode must be one of {TRANSITION_MODES}, got {value!r}",
        )
    return value


@dataclass
class PlaybackConfig:
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
    #   - "fixed": legacy behaviour — fixed crossfade_seconds at the
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
    # Falls back to seconds-based legacy timing when no beat grid /
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
    # legacy "skip = clean break" behaviour for users who want it.
    # CLI server-audio skip path cannot pitch-stretch on the fly so
    # it cold-cuts regardless of this flag.
    beatmatch_on_skip: bool = False
    # Voice liners — DJ-style spoken drops layered over the live mix.
    # ``liners_folder`` is the source directory (default
    # ``<index_dir>/liners`` resolved at server startup).  Trigger
    # parameters are evaluated client-side in the browser; the server
    # exposes the file list + raw bytes via ``/api/liner/...``.
    liners_enabled: bool = False
    liners_folder: str | None = None
    liners_every_n_songs: int | None = None
    liners_every_minutes: float | None = None
    liners_random_min_minutes: float | None = None
    liners_random_max_minutes: float | None = None
    liners_pick_mode: str = "random"
    liners_duck_db: float = -12.0
    # Per-file daypart directory.  When set, load one TOML per file
    # under this folder (each file is one daypart) instead of the
    # built-in profiles.  Each file may declare ``indexes = [...]``
    # to scope the daypart to specific index names.  Empty / missing
    # folder falls back to the built-in DAYPARTS list.
    dayparts_dir: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaybackConfig:
        """Construct a PlaybackConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[playback]`` TOML section.

        Returns:
            A populated PlaybackConfig instance.

        Raises:
            ValueError: If ``crossfade_seconds`` is negative or
                ``no_repeat_window`` is negative.
        """
        crossfade = float(data.get("crossfade_seconds", 3.0))
        no_repeat = int(data.get("no_repeat_window", 500))
        artist_repeat = int(data.get("artist_repeat_window", 3))

        if crossfade < 0:
            raise ValueError(f"playback.crossfade_seconds must be >= 0, got {crossfade}")
        if no_repeat < 0:
            raise ValueError(f"playback.no_repeat_window must be >= 0, got {no_repeat}")

        history_raw = data.get("history_file")
        discovery_every_raw = data.get("discovery_every")

        fade_in = float(data.get("fade_in_seconds", 3.0))
        if fade_in < 0:
            raise ValueError(f"playback.fade_in_seconds must be >= 0, got {fade_in}")

        return cls(
            crossfade_seconds=crossfade,
            fade_in_seconds=fade_in,
            no_repeat_window=no_repeat,
            artist_repeat_window=max(0, artist_repeat),
            history_file=Path(history_raw).expanduser() if history_raw else None,
            discovery_every=int(discovery_every_raw) if discovery_every_raw is not None else None,
            crossfade_eq_duck=bool(data.get("crossfade_eq_duck", False)),
            crossfade_bass_cutoff_hz=float(data.get("crossfade_bass_cutoff_hz", 180.0)),
            transition_mode=_validate_transition_mode(
                str(data.get("transition_mode", "full_intro_outro")),
            ),
            post_queue_seed=_validate_post_queue_seed(
                str(data.get("post_queue_seed", "last_queued")),
            ),
            pick_top_k=max(1, int(data.get("pick_top_k", 1))),
            pick_temperature=max(0.0, float(data.get("pick_temperature", 0.3))),
            key_notation=_validate_key_notation(
                str(data.get("key_notation", "camelot")),
            ),
            key_prefer_flats=bool(data.get("key_prefer_flats", False)),
            show_lyrics=bool(data.get("show_lyrics", True)),
            prefetch_next_track=bool(data.get("prefetch_next_track", True)),
            silence_trigger_crossfade=bool(
                data.get("silence_trigger_crossfade", True),
            ),
            audio_device=data.get("audio_device") or None,
            enable_daypart=bool(data.get("enable_daypart", False)),
            enable_mood_arc=bool(data.get("enable_mood_arc", False)),
            mood_arc_hours=max(0.25, float(data.get("mood_arc_hours", 3.0))),
            import_external_cues=bool(data.get("import_external_cues", True)),
            beat_sync_fx=bool(data.get("beat_sync_fx", True)),
            key_sync_fx=bool(data.get("key_sync_fx", True)),
            beatmatch_on_skip=bool(data.get("beatmatch_on_skip", False)),
            liners_enabled=bool(data.get("liners_enabled", False)),
            liners_folder=(data.get("liners_folder") or None),
            liners_every_n_songs=(
                int(data["liners_every_n_songs"])
                if data.get("liners_every_n_songs") is not None
                else None
            ),
            liners_every_minutes=(
                float(data["liners_every_minutes"])
                if data.get("liners_every_minutes") is not None
                else None
            ),
            liners_random_min_minutes=(
                float(data["liners_random_min_minutes"])
                if data.get("liners_random_min_minutes") is not None
                else None
            ),
            liners_random_max_minutes=(
                float(data["liners_random_max_minutes"])
                if data.get("liners_random_max_minutes") is not None
                else None
            ),
            liners_pick_mode=str(data.get("liners_pick_mode", "random")),
            liners_duck_db=float(data.get("liners_duck_db", -12.0)),
            dayparts_dir=(data.get("dayparts_dir") or None),
        )


@dataclass
class DjMixConfig:
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
        harmonic_mixing: When ``True``, similarity candidates are filtered
            to only Camelot-compatible keys.
    """

    beatmatch: bool = False
    beatmatch_max_stretch: float = 0.08
    outro_intro_align: bool = False
    phrase_align: bool = False
    phrase_bars: int = 8
    filter_sweep: bool = False
    filter_sweep_floor_hz: float = 250.0
    harmonic_mixing: bool = False
    # Harmonic-mixing rule when ``harmonic_mixing`` is enabled.  See
    # :func:`autodj.dj_meta.harmonic_compatible` for the full rule list.
    # Default ``"compatible"`` keeps the long-standing AutoDJ behaviour.
    harmonic_mode: str = "compatible"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DjMixConfig:
        """Construct a DjMixConfig from a raw TOML section dict."""
        return cls(
            beatmatch=bool(data.get("beatmatch", False)),
            beatmatch_max_stretch=float(data.get("beatmatch_max_stretch", 0.08)),
            outro_intro_align=bool(data.get("outro_intro_align", False)),
            phrase_align=bool(data.get("phrase_align", False)),
            phrase_bars=int(data.get("phrase_bars", 8)),
            filter_sweep=bool(data.get("filter_sweep", False)),
            filter_sweep_floor_hz=float(data.get("filter_sweep_floor_hz", 250.0)),
            harmonic_mixing=bool(data.get("harmonic_mixing", False)),
            harmonic_mode=str(data.get("harmonic_mode", "compatible")).lower(),
        )


@dataclass
class TransitionsConfig:
    """Settings for transition effects layered onto every crossfade.

    Attributes:
        effect: Which effect to apply.  ``"none"`` = standard crossfade
            only.  Concrete effects: ``"echo_out"``, ``"reverb_tail"``,
            ``"highpass_riser"``, ``"tape_stop"``, ``"gate_stutter"``,
            ``"noise_riser"``, ``"backspin"``, ``"cross_eq_swap"``.
            Meta modes: ``"random"`` (uniform random per crossfade),
            ``"rotate"`` (cycle through all real effects in order).
        wet_mix: Global wet/dry of the transition effect's contribution
            to the final overlap (0.0 = effect inaudible, 1.0 = full).
            Some effects already have their own internal wet — this is
            the outer mix on top of that.
    """

    effect: str = "none"
    wet_mix: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransitionsConfig:
        """Construct a TransitionsConfig from a raw TOML section dict."""
        return cls(
            effect=str(data.get("effect", "none")).lower(),
            wet_mix=float(data.get("wet_mix", 1.0)),
        )


@dataclass
class ReplayGainConfig:
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

    enabled: bool = False
    target_db: float = -14.0
    max_clip_safe_gain: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayGainConfig:
        """Construct a ReplayGainConfig from a raw TOML section dict."""
        return cls(
            enabled=bool(data.get("enabled", False)),
            target_db=float(data.get("target_db", -14.0)),
            max_clip_safe_gain=float(data.get("max_clip_safe_gain", 1.0)),
        )


@dataclass
class ModelConfig:
    """Settings for the MuQ embedding model.

    Attributes:
        name: HuggingFace model ID to load (used for auto-download).
        revision: HuggingFace revision (branch, tag, or commit) to cache.
        manual_path: Optional local path to a pre-downloaded model directory.
            When set, ``name`` is ignored and the model is loaded from disk.
    """

    name: str = "OpenMuQ/MuQ-large-msd-iter"
    revision: str = "main"
    manual_path: Path | None = None

    def __post_init__(self) -> None:
        """Validate the configured model revision."""
        if (
            not isinstance(self.revision, str)
            or not self.revision
            or self.revision != self.revision.strip()
        ):
            raise ValueError(
                "model.revision must be a non-empty string without surrounding whitespace"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelConfig:
        """Construct a ModelConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[model]`` TOML section.

        Returns:
            A populated ModelConfig instance.
        """
        manual_raw = data.get("manual_path")
        revision = data.get("revision", "main")
        if not isinstance(revision, str) or not revision or revision != revision.strip():
            raise ValueError(
                "model.revision must be a non-empty string without surrounding whitespace"
            )
        return cls(
            name=data.get("name", "OpenMuQ/MuQ-large-msd-iter"),
            revision=revision,
            manual_path=Path(manual_raw) if manual_raw else None,
        )


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


def _deduplicate(values: list[str]) -> list[str]:
    """Return values in first-seen order with duplicates removed."""
    return list(dict.fromkeys(values))


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


def _canonicalize_allowed_hosts(values: object) -> list[str] | None:
    """Validate, normalize, and deduplicate an optional host allowlist."""
    if values is None:
        return None
    if not isinstance(values, list):
        raise TypeError("server.allowed_hosts must be a list of strings")
    return _deduplicate(
        [
            _canonicalize_host(
                value,
                field_name="server.allowed_hosts",
                allow_unspecified=False,
            )
            for value in values
        ]
    )


def _canonicalize_allowed_origins(values: object) -> list[str] | None:
    """Validate, normalize, and deduplicate an optional origin allowlist."""
    if values is None:
        return None
    if not isinstance(values, list):
        raise TypeError("server.allowed_origins must be a list of strings")
    return _deduplicate([canonicalize_allowed_origin(value) for value in values])


@dataclass
class ServerConfig:
    """Web-server bind, request policy, session, and upload limits."""

    host: str = "127.0.0.1"
    port: int = 8080
    access_token: str | None = field(default=None, repr=False)
    insecure_lan: bool = False
    allowed_hosts: list[str] | None = None
    allowed_origins: list[str] | None = None
    session_ttl_seconds: int = 24 * 60 * 60
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
    def from_dict(cls, data: dict[str, Any]) -> ServerConfig:
        """Construct server settings from a raw configuration section."""
        if not isinstance(data, dict):
            raise TypeError("server section must be a table")
        max_mib = _require_int(data.get("liner_upload_max_mib", 50), "server.liner_upload_max_mib")
        if not 1 <= max_mib <= MAX_LINER_UPLOAD_MIB:
            raise ValueError("server.liner_upload_max_mib must be between 1 and 1024")
        return cls(
            host=data.get("host", "127.0.0.1"),
            port=data.get("port", 8080),
            access_token=data.get("access_token"),
            insecure_lan=data.get("insecure_lan", False),
            allowed_hosts=data.get("allowed_hosts"),
            allowed_origins=data.get("allowed_origins"),
            session_ttl_seconds=data.get("session_ttl_seconds", 24 * 60 * 60),
            liner_upload_max_bytes=max_mib * _MIB,
        )


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
    validated = ServerConfig(
        host=cfg.host,
        port=cfg.port,
        access_token=cfg.access_token,
        insecure_lan=cfg.insecure_lan,
        allowed_hosts=cfg.allowed_hosts,
        allowed_origins=cfg.allowed_origins,
        session_ttl_seconds=cfg.session_ttl_seconds,
        liner_upload_max_bytes=cfg.liner_upload_max_bytes,
    )
    cfg.__dict__.update(validated.__dict__)
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
class HuggingFaceConfig:
    """Settings for HuggingFace Hub access.

    Attributes:
        token: Optional HuggingFace API token (read-only scope is sufficient).
            Without a token, downloads are unauthenticated and rate-limited.
            Get one free at https://huggingface.co/settings/tokens
    """

    token: str | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HuggingFaceConfig:
        """Construct a HuggingFaceConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[huggingface]`` TOML section.

        Returns:
            A populated HuggingFaceConfig instance.
        """
        return cls(token=data.get("token") or None)


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


ENVIRONMENT_OVERLAY: dict[str, tuple[str, str, type[str] | type[int]]] = {
    "AUTODJ_LIBRARY_MUSIC_DIR": ("library", "music_dir", str),
    "AUTODJ_INDEX_DIR": ("index", "index_dir", str),
    "AUTODJ_MODEL_DIR": ("index", "model_dir", str),
    "AUTODJ_HOST": ("server", "host", str),
    "AUTODJ_PORT": ("server", "port", int),
    "AUTODJ_ACCESS_TOKEN": ("server", "access_token", str),
    "AUTODJ_HUGGINGFACE_TOKEN": ("huggingface", "token", str),
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


def _build_config(
    raw: dict[str, Any],
    *,
    config_path: Path | None,
    sources: list[str],
    presets_raw: dict[str, Any],
) -> AutoDJConfig:
    """Validate raw sections and construct the typed application configuration."""
    from autodj.presets import load_user_presets

    for section in (
        "library",
        "index",
        "playback",
        "model",
        "huggingface",
        "replaygain",
        "djmix",
        "transitions",
        "server",
    ):
        if not isinstance(raw.get(section, {}), Mapping):
            raise TypeError(f"{section} section must be a table")

    return AutoDJConfig(
        library=LibraryConfig.from_dict(raw.get("library", {})),
        index=IndexConfig.from_dict(raw.get("index", {})),
        playback=PlaybackConfig.from_dict(raw.get("playback", {})),
        model=ModelConfig.from_dict(raw.get("model", {})),
        huggingface=HuggingFaceConfig.from_dict(raw.get("huggingface", {})),
        replaygain=ReplayGainConfig.from_dict(raw.get("replaygain", {})),
        djmix=DjMixConfig.from_dict(raw.get("djmix", {})),
        transitions=TransitionsConfig.from_dict(raw.get("transitions", {})),
        server=ServerConfig.from_dict(raw.get("server", {})),
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
        presets_raw = {"presets": raw["presets"]} if "presets" in raw else {}
    return _build_config(
        raw,
        config_path=loaded_path,
        sources=sources,
        presets_raw=presets_raw,
    )
