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

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    config value before storing.  A *bad* name like ``index/metadata.json``
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

        return cls(
            crossfade_seconds=crossfade,
            no_repeat_window=no_repeat,
            artist_repeat_window=max(0, artist_repeat),
            history_file=Path(history_raw).expanduser() if history_raw else None,
            discovery_every=int(discovery_every_raw) if discovery_every_raw is not None else None,
            crossfade_eq_duck=bool(data.get("crossfade_eq_duck", False)),
            crossfade_bass_cutoff_hz=float(data.get("crossfade_bass_cutoff_hz", 180.0)),
            transition_mode=_validate_transition_mode(
                str(data.get("transition_mode", "full_intro_outro")),
            ),
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
        manual_path: Optional local path to a pre-downloaded model directory.
            When set, ``name`` is ignored and the model is loaded from disk.
    """

    name: str = "OpenMuQ/MuQ-large-msd-iter"
    manual_path: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelConfig:
        """Construct a ModelConfig from a raw TOML section dict.

        Args:
            data: Dictionary of keys from the ``[model]`` TOML section.

        Returns:
            A populated ModelConfig instance.
        """
        manual_raw = data.get("manual_path")
        return cls(
            name=data.get("name", "OpenMuQ/MuQ-large-msd-iter"),
            manual_path=Path(manual_raw) if manual_raw else None,
        )


@dataclass
class HuggingFaceConfig:
    """Settings for HuggingFace Hub access.

    Attributes:
        token: Optional HuggingFace API token (read-only scope is sufficient).
            Without a token, downloads are unauthenticated and rate-limited.
            Get one free at https://huggingface.co/settings/tokens
    """

    token: str | None = None

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
    config_path: Path
    presets: dict[str, Preset] = field(default_factory=dict)
    replaygain: ReplayGainConfig = field(default_factory=lambda: ReplayGainConfig())
    djmix: DjMixConfig = field(default_factory=lambda: DjMixConfig())
    transitions: TransitionsConfig = field(default_factory=lambda: TransitionsConfig())


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* into *base*, returning a new dict.

    Nested dicts are merged key-by-key; non-dict values in *overlay* replace
    those in *base*.  Used to apply machine-specific overrides from
    ``config.local.toml`` on top of the shared ``config.toml``.
    """
    out: dict[str, Any] = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path = "config.toml") -> AutoDJConfig:
    """Load and validate the AutoDJ configuration from a TOML file.

    If a sibling ``config.local.toml`` file exists alongside *path*, its
    contents are deep-merged on top of the base config.  This lets you
    keep a shared ``config.toml`` (e.g. on a network share) and override
    per-machine settings (paths, ``music_dir``, ``path_remap``) in a
    gitignored ``config.local.toml``.

    Args:
        path: Path to the ``config.toml`` file. Defaults to ``config.toml``
            in the current working directory.

    Returns:
        A fully populated and validated :class:`AutoDJConfig` instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        KeyError: If a required key (e.g. ``library.music_dir``) is missing.
        ValueError: If a value is out of the accepted range.

    Example:
        >>> cfg = load_config("config.toml")
        >>> cfg.library.music_dir
        PosixPath('Z:/Music')
    """
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Run 'autodj' in the project directory or pass --config <path>."
        )

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    # Apply per-machine overlay if config.local.toml exists alongside.
    local_overlay_path = config_path.parent / "config.local.toml"
    if local_overlay_path.exists():
        with local_overlay_path.open("rb") as fh:
            overlay = tomllib.load(fh)
        raw = _deep_merge(raw, overlay)

    # Presets live in their own sidecar — `presets.toml` next to the
    # main config — so user-defined BPM curves are easy to share /
    # version separately from machine-specific paths.  Falls back to
    # any legacy ``[presets.*]`` sections inside ``config.toml`` if
    # the sidecar is missing.
    from autodj.presets import load_user_presets

    presets_path = config_path.parent / "presets.toml"
    presets_raw: dict[str, Any] = {}
    if presets_path.exists():
        with presets_path.open("rb") as fh:
            presets_raw = tomllib.load(fh)
    elif "presets" in raw:
        # Legacy inline form — load from config.toml
        presets_raw = {"presets": raw["presets"]}

    return AutoDJConfig(
        library=LibraryConfig.from_dict(raw.get("library", {})),
        index=IndexConfig.from_dict(raw.get("index", {})),
        playback=PlaybackConfig.from_dict(raw.get("playback", {})),
        model=ModelConfig.from_dict(raw.get("model", {})),
        huggingface=HuggingFaceConfig.from_dict(raw.get("huggingface", {})),
        replaygain=ReplayGainConfig.from_dict(raw.get("replaygain", {})),
        djmix=DjMixConfig.from_dict(raw.get("djmix", {})),
        transitions=TransitionsConfig.from_dict(raw.get("transitions", {})),
        presets=load_user_presets(presets_raw),
        config_path=config_path,
    )
