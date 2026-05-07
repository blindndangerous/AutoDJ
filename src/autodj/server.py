"""FastAPI web server and PlayerBridge for the autodj serve command.

Architecture
------------
``autodj serve`` runs a single process:

- :class:`Player` runs in a background daemon thread (blocking loop).
- :class:`PlayerBridge` is a thin, thread-safe adapter between the Player and
  the FastAPI app.
- FastAPI + uvicorn run on the main thread (asyncio event loop).
- A startup asyncio task broadcasts the current player state to all connected
  WebSocket clients once per second.

Routes
------
``GET  /``                  → index.html
``GET  /api/status``        → JSON state snapshot
``GET  /api/version``       → {version, commit, built_at} for footer build stamp
``POST /api/skip``          → skip to next track
``POST /api/pause``         → toggle pause / resume
``POST /api/volume``        → set volume (body: ``{"volume": 0.75}``)
``POST /api/mute``          → toggle mute
``GET  /api/search?q=``     → fuzzy search of indexed tracks
``POST /api/play-next``     → queue a track by path to play after the current one
``WS   /ws``                → push state JSON every 1 second
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json as _json
import logging
import threading
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig
    from autodj.indexer import IndexEntry
    from autodj.presets import Preset
    from autodj.similarity import SimilarityIndex

logger = logging.getLogger(__name__)


@functools.cache
def _version_info() -> dict[str, str]:
    """Return {version, commit, built_at} for the running build.

    Cached on first call so the timestamp reflects when the currently
    installed bundle was produced (preferring static_dist/app.js mtime,
    falling back to the source tree, then to process start time).
    Commit is the short SHA from `git rev-parse` when the source tree
    is a git checkout, else "unknown".
    """
    import datetime as _dt
    import importlib.metadata as _md
    import subprocess as _sp  # nosec B404 - trusted invocation (git, fixed argv)

    here = Path(__file__).parent
    try:
        version = _md.version("autodj")
    except _md.PackageNotFoundError:
        version = "0.0.0"

    commit = "unknown"
    try:
        # Static argv, no shell, no untrusted input.  git on PATH is a
        # build-environment expectation; absence falls through to the
        # "unknown" placeholder.
        out = _sp.check_output(  # nosec B603 B607
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here,
            stderr=_sp.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        if out:
            commit = out
    except (OSError, _sp.SubprocessError):
        pass

    built_at: str | None = None
    for candidate in (here / "static_dist" / "app.js", here / "static" / "app.js"):
        if candidate.exists():
            built_at = _dt.datetime.fromtimestamp(candidate.stat().st_mtime, _dt.UTC).isoformat(
                timespec="seconds"
            )
            break
    if built_at is None:
        built_at = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")

    return {"version": version, "commit": commit, "built_at": built_at}


def _build_why(player: Any) -> list[str]:
    """Build the 'why this track' sentence list for the bridge state."""
    from autodj.explain import explain_pick

    cur = getattr(player._state, "current_track", None)
    prev = getattr(player, "_previous_track", None)
    mode = getattr(player, "_last_pick_mode", "similarity")
    return explain_pick(prev, cur, mode=mode)


def _library_job_snapshot() -> dict:
    """Return current library-job state.  Empty dict when nothing has run."""
    from autodj.jobs import get_manager

    snap = get_manager().snapshot()
    # Only the most recent few lines fly through the WS payload — the
    # full log is fetchable via GET /api/library/job.
    snap = dict(snap)
    snap["lines"] = snap["lines"][-25:]
    return snap


class VolumeBody(BaseModel):
    """Request body for POST /api/volume."""

    volume: float


class ProfileSaveBody(BaseModel):
    """Request body for POST /api/profiles.

    All snapshot fields are optional except ``name``; missing fields
    mean "do not override" when the profile is later applied.
    """

    name: str
    index_name: str | None = None
    preset: str | None = None
    bpm_lo: float | None = None
    bpm_hi: float | None = None
    harmonic_mode: str | None = None
    transition_mode: str | None = None
    beat_sync_fx: bool | None = None
    key_sync_fx: bool | None = None
    beatmatch_on_skip: bool | None = None
    crossfade_seconds: float | None = None
    smart_shuffle: bool | None = None
    pure_shuffle: bool | None = None
    anchor_to_seed: bool | None = None
    enable_daypart: bool | None = None
    enable_mood_arc: bool | None = None
    mood_arc_hours: float | None = None
    liners_enabled: bool | None = None
    liners_pick_mode: str | None = None


class SeekBody(BaseModel):
    """Request body for POST /api/seek.

    Either ``seconds`` (absolute) or ``delta`` (relative) is set.  When
    both are provided, ``seconds`` wins.
    """

    seconds: float | None = None
    delta: float | None = None


class PlayNextBody(BaseModel):
    """Request body for POST /api/play-next."""

    path: str
    now: bool = False  # if True, also skip the current track


class QueueReorderBody(BaseModel):
    """Request body for POST /api/queue/reorder.

    Attributes:
        paths: New ordering of the queue, expressed as a list of
            :attr:`~autodj.indexer.IndexEntry.path` strings.  The first
            entry plays after the current track finishes.
    """

    paths: list[str]


class EqBody(BaseModel):
    """Request body for POST /api/eq.

    Each gain is a linear multiplier in [0.0, 2.0]; 1.0 = unity.
    Omitted fields are left unchanged.
    """

    low: float | None = None
    mid: float | None = None
    high: float | None = None


class PresetBody(BaseModel):
    """Request body for POST /api/preset — empty / null name clears."""

    name: str | None = None


class TransitionBody(BaseModel):
    """Request body for POST /api/transition."""

    effect: str


class DjMixBody(BaseModel):
    """Request body for POST /api/djmix — only set fields are applied."""

    harmonic_mixing: bool | None = None
    harmonic_mode: str | None = None
    beatmatch: bool | None = None
    phrase_align: bool | None = None
    outro_intro_align: bool | None = None
    filter_sweep: bool | None = None


class PlaybackSettingsBody(BaseModel):
    """Request body for POST /api/playback-settings."""

    crossfade_seconds: float | None = None
    crossfade_eq_duck: bool | None = None
    smart_shuffle: bool | None = None
    pure_shuffle: bool | None = None
    anchor_to_seed: bool | None = None
    replaygain_enabled: bool | None = None
    transition_mode: str | None = None
    show_lyrics: bool | None = None
    enable_daypart: bool | None = None
    enable_mood_arc: bool | None = None
    mood_arc_hours: float | None = None
    import_external_cues: bool | None = None
    beat_sync_fx: bool | None = None
    key_sync_fx: bool | None = None
    beatmatch_on_skip: bool | None = None
    liners_enabled: bool | None = None
    liners_folder: str | None = None
    liners_every_n_songs: int | None = None
    liners_every_minutes: float | None = None
    liners_random_min_minutes: float | None = None
    liners_random_max_minutes: float | None = None
    liners_pick_mode: str | None = None
    liners_duck_db: float | None = None


class BpmRangeBody(BaseModel):
    """Request body for POST /api/bpm-range — both null = clear filter."""

    lo: float | None = None
    hi: float | None = None


class DiscoveryBody(BaseModel):
    """Request body for POST /api/discovery — null disables."""

    every: int | None = None


class LibraryJobBody(BaseModel):
    """Request body for POST /api/library/run — invokes a CLI subcommand."""

    name: str
    args: list[str] = []


# ---------------------------------------------------------------------------
# PlayerBridge
# ---------------------------------------------------------------------------


@dataclass
class PlayerBridge:
    """Thread-safe adapter between :class:`~autodj.player.Player` and FastAPI.

    Exposes playback control methods that can be called from the asyncio
    event loop while the Player runs in a separate thread.  All access to
    ``PlayerState`` is single-field reads/writes protected by Python's GIL —
    no additional locking is required.

    Attributes:
        player: The running :class:`~autodj.player.Player` instance.
        sim: The loaded :class:`~autodj.similarity.SimilarityIndex` (for search).
    """

    # Typed as Any so mypy doesn't complain about every duck-typed attribute
    # access (player._state, player._eq_low, etc.).  Real type is
    # autodj.player.Player / autodj.similarity.SimilarityIndex but we don't
    # import them eagerly to keep the minimal-install path light.
    player: Any
    sim: Any

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def seek(self, seconds: float | None = None, delta: float | None = None) -> float:
        """Seek the active track and return the resulting position in seconds.

        ``seconds`` is absolute (from track start); ``delta`` is relative
        to the current playback position.  When both are provided
        ``seconds`` wins.  Clamps inside :meth:`Player.seek_to` so callers
        never overshoot the buffer.
        """
        if seconds is not None:
            return float(self.player.seek_to(float(seconds)))
        if delta is not None:
            return float(self.player.seek_relative(float(delta)))
        return float(self.player._playback_pos[0]) / max(1, self.player._current_sr)

    def skip(self) -> None:
        """Skip the current track immediately.

        In headless / browser-driven mode (``player._dry_run``) the
        bridge mutates state synchronously via :meth:`advance_now` so
        callers (REST endpoints, WS clients) can read the new state
        on the same request.  In server-audio mode the audio loop owns
        track sequencing, so we just signal it.
        """
        if getattr(self.player, "_dry_run", False):
            self.advance_now()
        else:
            self.player._skip_event.set()

    def repick_next(self, blacklist_path: str | None = None) -> None:
        """Replace state.next_track without advancing current.

        Used when the browser's standby deck fails to load the prefetched
        next-track (bad codec, missing file, network drop).  Picking a
        different next track lets the live track keep playing instead of
        cascade-skipping the current one too.

        Args:
            blacklist_path: Path of the failed track — temporarily added
                to recently-played so similarity won't immediately re-pick
                it.  Pass None to just refresh from current.
        """
        p = self.player
        state = p._state
        cur = state.current_track
        if cur is None:
            return
        # Best-effort blacklist: stuff the bad path into recently-played
        # so the similarity engine skips it on the next pick.  Falls back
        # silently if the helper isn't available on this player build.
        if blacklist_path:
            try:
                state.recently_played.append(blacklist_path)
            except Exception:
                logger.debug("repick_next: blacklist append failed", exc_info=True)
        try:
            state.next_track = p._pick_next(cur)
        except Exception:
            logger.debug("repick_next: _pick_next failed", exc_info=True)
            state.next_track = None

    def advance_now(self) -> None:
        """Synchronous track advance for headless / browser-driven mode.

        Browser owns the audio clock; when its ``<audio>.ended`` fires
        (or the user hits Skip / Now / Random) it POSTs to the server
        which calls this method.  We honour any queued pick, fall back
        to the precomputed ``next_track``, then refresh ``next_track``
        with a fresh similarity match so the browser has something to
        prefetch immediately.

        The ``Player`` thread parked in ``_run_headless`` is *not*
        consulted — its sole job is to hold the seed and keep the
        process alive.  This keeps the web player and the (potential)
        CLI player fully decoupled.
        """
        p = self.player
        state = p._state
        cur = state.current_track

        if state.queued_next is not None:
            nxt = state.queued_next
            state.queued_next = None
            p._last_pick_mode = "queue"
        elif state.queue:
            nxt = state.queue.pop(0)
            p._last_pick_mode = "queue"
        elif state.next_track is not None:
            nxt = state.next_track
        elif cur is not None:
            try:
                nxt = p._pick_next(cur)
            except Exception:
                # Picker failed (empty index after prune, FAISS error, ...).
                # Leave state untouched so the browser keeps playing the
                # current track and the user can retry.  Warning so the
                # default INFO floor surfaces it without -v.
                logger.warning("advance_now: _pick_next(cur) failed", exc_info=True)
                return
        else:
            return

        state.current_track = nxt
        # Browser-driven mode skips _play_track, so without an explicit
        # call here the lyric panel would stay frozen on the previous
        # track's words.  Resolution order inside _load_lyrics is
        # LRC sidecar -> beets -> embedded ID3/Vorbis/MP4 tags.
        try:
            p._load_lyrics(nxt.path)
        except Exception:
            logger.debug("advance_now: lyric load failed", exc_info=True)
        # Spawn a background thread to populate the DJ-meta cache (cue
        # points, intro_end_s, outro_start_s, beat grid) for the new
        # track.  Browser-driven mode skips _play_track entirely, so
        # without this hook the cue strip / cue list stay empty and the
        # full intro/outro markers Mixxx-style transition modes rely on
        # never get computed.  No-op when the cache already has the
        # track (sidecar hit) or another worker is already analysing.
        try:
            p.analyse_track_in_background(nxt.path)
        except Exception:
            logger.debug("advance_now: background analysis spawn failed", exc_info=True)
        # Refresh next_track for the browser's prefetcher.  Failure here
        # leaves current_track set but next_track empty -- browser will
        # show "no upcoming track" and the user can advance again.
        try:
            state.next_track = p._pick_next(nxt)
        except Exception:
            # Browser will see "no upcoming track" until the next advance
            # rebuilds it.  Warning so the default INFO floor surfaces
            # the picker failure without -v.
            logger.warning("advance_now: next-track refresh failed", exc_info=True)
            state.next_track = None
        # Pre-warm the DJ-meta cache for the upcoming track too so its
        # intro_end_s / outro_start_s / cues are ready by the time the
        # browser crossfades into it.  Same no-op guards as above.
        if state.next_track is not None:
            try:
                p.analyse_track_in_background(state.next_track.path)
            except Exception:
                logger.debug(
                    "advance_now: next-track analysis spawn failed",
                    exc_info=True,
                )
        state.record_played(nxt)
        state.track_number += 1
        p._previous_track = cur

        # Single-line advance banner (INFO).  Shows outgoing -> incoming
        # with BPM + Camelot key + pick mode so a user tailing the log
        # can see exactly what the browser just crossfaded into.  Cur
        # is None on the very first seed advance; format conditionally.
        try:
            from autodj.dj_meta import camelot_label  # local — avoid cycles

            def _fmt(t: Any) -> str:
                if t is None:
                    return "(none)"
                bpm = f"{t.bpm:.0f} BPM" if getattr(t, "bpm", 0) else "BPM ?"
                cam = camelot_label(getattr(t, "key", -1), getattr(t, "mode", -1))
                return f"{t.display_name} ({bpm}, {cam})"

            logger.info(
                "Advance: %s -> %s, mode=%s",
                _fmt(cur),
                _fmt(nxt),
                p._last_pick_mode,
            )
        except Exception:
            logger.debug("advance_now: log banner failed", exc_info=True)

        # Update timer hint (browser drives the real clock; this just
        # keeps get_state's `duration` field correct on first WS push).
        from autodj.player import _DEFAULT_SR  # local import — avoid cycles

        p._current_sr = _DEFAULT_SR
        p._playback_len = int(
            (nxt.length if nxt.length and nxt.length > 0 else 5.0) * _DEFAULT_SR,
        )
        p._playback_pos[0] = 0

        # M3U / history side effects (mirror the Live-loop behaviour).
        if p._export_m3u:
            from autodj.player import _append_m3u_entry

            _append_m3u_entry(p._export_m3u, nxt)
        if p._history_file:
            from datetime import datetime as _dt

            from autodj.player import _append_history_entry

            _append_history_entry(p._history_file, nxt, _dt.now())

    def pause(self) -> bool:
        """Toggle pause/resume.

        Returns:
            New paused state (``True`` = paused).
        """
        state = self.player._state
        state.is_paused = not state.is_paused
        return state.is_paused

    def set_volume(self, volume: float) -> None:
        """Set playback volume.

        Args:
            volume: Float in ``[0.0, 1.0]``.  Clamped automatically.
        """
        self.player._state.volume = max(0.0, min(1.0, float(volume)))

    def toggle_mute(self) -> bool:
        """Toggle mute.

        Returns:
            New muted state (``True`` = muted).
        """
        state = self.player._state
        state.is_muted = not state.is_muted
        return state.is_muted

    def toggle_discovery(self) -> bool:
        """Toggle discovery mode on/off.

        Has no effect if the player was not started with a discovery rate.

        Returns:
            New ``discovery_enabled`` state (``True`` = enabled).
        """
        state = self.player._state
        state.discovery_enabled = not state.discovery_enabled
        return state.discovery_enabled

    # ------------------------------------------------------------------
    # State read
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        """Return a JSON-serialisable snapshot of the current player state.

        Returns:
            Dict with keys: ``current_track``, ``next_track``, ``is_paused``,
            ``volume``, ``is_muted``, ``elapsed``, ``duration``.
        """
        state = self.player._state
        pos = self.player._playback_pos[0]
        sr = self.player._current_sr

        import contextlib

        from autodj.beat_sync import (
            extract_downbeats,
            key_to_hz,
            synthesize_downbeats,
        )
        from autodj.dj_meta import camelot_label

        # Pull the DJ-meta sidecar (if any) so we can surface the
        # outgoing track's outro length to the browser — the per-effect
        # transition-length scaler in app.js uses it to size effects to
        # the song's actual outro instead of a fixed crossfade window.
        with contextlib.suppress(Exception):  # cache is best-effort
            self.player._ensure_dj_cache()
        dj_cache = getattr(self.player, "_dj_cache", None)

        def _markers(entry: IndexEntry | None) -> tuple[float | None, float | None, float | None]:
            """Return ``(intro_end_s, outro_start_s, outro_len)`` from the
            DJ-meta sidecar, or ``(None, None, None)`` when unanalysed.
            """
            if entry is None or dj_cache is None or not entry.length:
                return (None, None, None)
            try:
                meta = dj_cache.get(entry.path)
            except Exception:
                return (None, None, None)
            if not getattr(meta, "analysed", False):
                return (None, None, None)
            intro_end_raw = float(getattr(meta, "intro_end_s", 0.0) or 0.0)
            outro_start_raw = float(getattr(meta, "outro_start_s", 0.0) or 0.0)
            intro_end = intro_end_raw if intro_end_raw > 0 else None
            if outro_start_raw <= 0:
                return (intro_end, None, None)
            outro_len = max(0.0, float(entry.length) - outro_start_raw)
            return (intro_end, outro_start_raw, outro_len)

        def _cues(entry: IndexEntry | None) -> list[dict]:
            """Cue list for the entry, or empty when uncached / unanalysed.

            Phrase cues are subsampled to at most one every 64 beats so
            the WebSocket payload doesn't balloon for long DJ-software-
            imported tracks.  All non-phrase markers (drop, breakdown,
            first/outro_downbeat, user) survive intact -- they're the
            ones the cue strip + screen-reader summary care about.
            """
            if entry is None or dj_cache is None:
                return []
            try:
                meta = dj_cache.get(entry.path)
            except Exception:
                return []
            if not getattr(meta, "analysed", False):
                return []

            cues_raw = list(getattr(meta, "cues", []))
            kept_phrase: list = []
            other: list = []
            for c in cues_raw:
                (kept_phrase if c.type == "phrase" else other).append(c)
            # Keep every other phrase marker -- 32-beat phrases become
            # ~64-beat (every other phrase boundary).  Keeps the strip
            # legible and halves the WS bytes for typical tracks.
            kept_phrase = kept_phrase[::2]
            shaped = sorted(kept_phrase + other, key=lambda c: c.time_s)
            return [
                {
                    "time_s": round(c.time_s, 2),
                    "type": c.type,
                    "label": c.label,
                    "source": c.source,
                    "color": c.color,
                }
                for c in shaped
            ]

        def _downbeats(
            entry: IndexEntry | None,
            outro_start: float | None,
            intro_end: float | None,
        ) -> tuple[list[float], list[float]]:
            """Return ``(downbeats_outro, downbeats_intro)`` rounded to 3 dp.

            Outro window: last 32 bars before ``length`` (or whole grid if
            shorter).  Intro window: first 32 bars from ``intro_end`` (or
            from 0 when intro_end is unknown).  When the cached beat grid
            is empty / too short, synthesise a downbeat grid from
            ``entry.bpm`` + ``outro_start`` so beat-sync FX still have
            something to align to on tracks where librosa beat detection
            failed.
            """
            if entry is None or not entry.length:
                return ([], [])
            beats: list[float] = []
            if dj_cache is not None:
                try:
                    meta = dj_cache.get(entry.path)
                except Exception:
                    meta = None
                if meta is not None and getattr(meta, "analysed", False):
                    beats = list(getattr(meta, "beats", []))

            downbeats = extract_downbeats(beats)
            # Synthesize when beat grid missing OR too sparse to cover the
            # last 64 s at 120 BPM (~32 bars).
            if len(downbeats) < 8 and entry.bpm and entry.bpm > 0:
                anchor = float(outro_start) if outro_start is not None else 0.0
                downbeats = synthesize_downbeats(
                    float(entry.bpm),
                    float(entry.length),
                    anchor_s=anchor,
                )

            if not downbeats:
                return ([], [])

            # Outro window: 32 bars before length.  At 120 BPM 4/4 ~64 s.
            # Use bar_seconds derived from the bpm if known so the window
            # tracks the actual tempo.
            from autodj.beat_sync import bar_seconds

            bar_s = bar_seconds(entry.bpm) if entry.bpm and entry.bpm > 0 else 2.0
            outro_lo = max(0.0, float(entry.length) - 32 * bar_s)
            d_out = [round(d, 3) for d in downbeats if d >= outro_lo]
            intro_anchor = float(intro_end) if intro_end is not None else 0.0
            intro_hi = intro_anchor + 32 * bar_s
            d_in = [round(d, 3) for d in downbeats if intro_anchor - bar_s <= d <= intro_hi]
            return (d_out, d_in)

        def _track_dict(entry: IndexEntry | None) -> dict | None:
            if entry is None:
                return None
            intro_end, outro_start, outro_len = _markers(entry)
            d_out, d_in = _downbeats(entry, outro_start, intro_end)
            key_hz = key_to_hz(entry.key) if entry.key is not None else None
            return {
                "title": entry.title,
                "artist": entry.artist,
                "album": entry.album,
                "path": entry.path,
                "bpm": entry.bpm,
                "length": entry.length,
                "display_name": entry.display_name,
                "key": entry.key,
                "mode": entry.mode,
                "camelot": camelot_label(entry.key, entry.mode),
                "energy": round(entry.energy, 3) if entry.energy else 0.0,
                "intro_end_s": round(intro_end, 2) if intro_end is not None else None,
                "outro_start_s": round(outro_start, 2) if outro_start is not None else None,
                "outro_len": round(outro_len, 2) if outro_len is not None else None,
                "cues": _cues(entry),
                "downbeats_outro": d_out,
                "downbeats_intro": d_in,
                "key_hz": round(key_hz, 3) if key_hz is not None else None,
            }

        elapsed = round(pos / max(1, sr), 1)

        discovery_every = getattr(self.player, "_discovery_every", None)

        # Compute the active lyric line for the current playback time
        from autodj.audio_meta import current_lyric

        lyrics = getattr(self.player, "_current_lyrics", []) or []
        active = current_lyric(lyrics, elapsed) if lyrics else None
        active_idx: int | None = None
        if active is not None:
            for i, ll in enumerate(lyrics):
                if ll is active:
                    active_idx = i
                    break

        return {
            "current_track": _track_dict(state.current_track),
            "next_track": _track_dict(state.next_track),
            "queue": [_track_dict(e) for e in state.queue],
            "is_paused": state.is_paused,
            "volume": round(state.volume, 2),
            "is_muted": state.is_muted,
            "elapsed": elapsed,
            "duration": round(state.current_track.length, 1)
            if state.current_track and state.current_track.length
            else 0.0,
            "discovery_enabled": state.discovery_enabled,
            "discovery_available": discovery_every is not None,
            "has_lyrics": bool(lyrics),
            "lyric_index": active_idx,
            "lyric_text": active.text if active else None,
            "lyrics_plain": getattr(self.player, "_current_lyrics_plain", "") or "",
            "eq": self.get_eq(),
            "beatmatch_ratio": round(getattr(self.player, "_beatmatch_ratio", 1.0), 3),
            "last_transition_fx": getattr(self.player, "_last_transition_fx", "none"),
            "why_this_track": _build_why(self.player),
            "library_job": _library_job_snapshot(),
            # Browser-side audio drives playback only when the server is
            # headless (dry_run / --no-playback / missing audio deps).
            "browser_playback": bool(getattr(self.player, "_dry_run", False)),
            "settings": self.get_settings(),
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 100) -> list[dict]:
        """Search indexed tracks by title, artist, and album.

        Splits the query into whitespace-separated tokens and requires
        every token to appear somewhere in the combined ``title +
        artist + album`` text of a candidate.  Token matching is case-
        insensitive and substring-based, so:

        - ``"portishead mysterons"`` matches title=Mysterons artist=Portishead
        - ``"dummy sour"`` matches album=Dummy title=Sour Times
        - ``"thom yorke amok"`` matches Atoms For Peace album=Amok

        Args:
            query: Multi-token search string.  Empty / whitespace-only
                returns an empty list.
            limit: Maximum number of results to return.  Default 100.

        Returns:
            List of track dicts (same shape as the ``current_track``
            dict in :meth:`get_state`).
        """
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return []

        results = []
        for entry in self.sim.entries:
            haystack = (f"{entry.title} — {entry.artist} — {entry.album}").lower()
            if all(tok in haystack for tok in tokens):
                results.append(
                    {
                        "title": entry.title,
                        "artist": entry.artist,
                        "album": entry.album,
                        "path": entry.path,
                        "bpm": entry.bpm,
                        "length": entry.length,
                        "display_name": entry.display_name,
                    },
                )
                if len(results) >= limit:
                    break
        return results

    # ------------------------------------------------------------------
    # Queue control
    # ------------------------------------------------------------------

    def play_next(self, path: str, now: bool = False) -> bool:
        """Queue a track by path to play after the current one.

        Args:
            path: The :attr:`~autodj.indexer.IndexEntry.path` string of the
                track to queue.
            now: If ``True``, also skip the current track so the queued track
                starts immediately.

        Returns:
            ``True`` if the track was found in the index, ``False`` otherwise.
        """
        entry = next(
            (e for e in self.sim.entries if e.path == path),
            None,
        )
        if entry is None:
            return False
        self.player._state.queued_next = entry
        if now:
            self.skip()
        return True

    # ------------------------------------------------------------------
    # Queue manipulation
    # ------------------------------------------------------------------

    def reseed_random(self) -> bool:
        """Pick a fresh random track from the index and play it next.

        Unlike :meth:`skip` (which advances via similarity from the
        current track), this reseeds the auto-DJ session from a random
        starting point — useful when the user doesn't like the seed the
        server picked at startup or wants to jump to an unrelated genre.
        """
        import random as _random

        entries = self.sim.entries
        if not entries:
            return False
        chosen = _random.choice(entries)  # nosec B311 — non-security
        self.player._state.queued_next = chosen
        self.skip()
        return True

    def queue_add(self, path: str) -> bool:
        """Append a track by path to the end of the user queue."""
        entry = next(
            (e for e in self.sim.entries if e.path == path),
            None,
        )
        if entry is None:
            return False
        self.player._state.queue.append(entry)
        return True

    def queue_remove(self, path: str) -> bool:
        """Remove the first matching path from the queue."""
        q = self.player._state.queue
        for i, e in enumerate(q):
            if e.path == path:
                del q[i]
                return True
        return False

    def queue_reorder(self, paths: list[str]) -> bool:
        """Reorder the queue to match the given list of paths.

        Tracks present in the queue but missing from *paths* are dropped.
        Paths not found in the current queue are ignored (re-add via
        :meth:`queue_add`).
        """
        q = self.player._state.queue
        by_path = {e.path: e for e in q}
        new_q = [by_path[p] for p in paths if p in by_path]
        # Replace contents in place so any concurrent reads see consistent state
        q.clear()
        q.extend(new_q)
        return True

    # ------------------------------------------------------------------
    # Cover art and lyrics
    # ------------------------------------------------------------------

    def cover_art_for(self, path: str) -> tuple[bytes, str] | None:
        """Return (image_bytes, mime_type) for the embedded cover art."""
        from autodj.audio_meta import read_cover_art

        art = read_cover_art(path)
        if art is None:
            return None
        return art.data, art.mime_type

    def current_lyrics(self) -> list[dict]:
        """Return the currently-loaded lyrics as a list of dicts."""
        lyrics = getattr(self.player, "_current_lyrics", []) or []
        return [{"time_s": ll.time_s, "text": ll.text} for ll in lyrics]

    # ------------------------------------------------------------------
    # 3-band EQ
    # ------------------------------------------------------------------

    def set_eq(
        self,
        low: float | None = None,
        mid: float | None = None,
        high: float | None = None,
    ) -> dict[str, float]:
        """Set one or more EQ band gains.  Returns the resulting state."""
        p = self.player
        if low is not None:
            p._eq_low = max(0.0, min(2.0, float(low)))
        if mid is not None:
            p._eq_mid = max(0.0, min(2.0, float(mid)))
        if high is not None:
            p._eq_high = max(0.0, min(2.0, float(high)))
        return self.get_eq()

    def get_eq(self) -> dict[str, float]:
        """Return current EQ band gains."""
        p = self.player
        return {
            "low": round(p._eq_low, 3),
            "mid": round(p._eq_mid, 3),
            "high": round(p._eq_high, 3),
        }

    # ------------------------------------------------------------------
    # Settings (mirror of CLI flags) — preset, transition, djmix toggles,
    # crossfade seconds, BPM range, discovery, smart-shuffle, ReplayGain
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Persistence — settings survive serve restarts
    # ------------------------------------------------------------------

    def _state_file(self) -> Path | None:
        cfg = getattr(self.player, "_cfg", None)
        if cfg is None:
            return None
        try:
            return Path(cfg.index.active_dir) / "web_state.json"
        except (TypeError, AttributeError):
            return None

    def load_persistent_state(self) -> None:
        """Restore previously-saved settings from web_state.json."""
        from autodj.runtime_state import load_into_player

        cfg = getattr(self.player, "_cfg", None)
        load_into_player(self.player, cfg.index.active_dir if cfg else None)

    def save_persistent_state(self) -> None:
        """Write current settings to web_state.json (atomic)."""
        from autodj.runtime_state import save_from_player

        cfg = getattr(self.player, "_cfg", None)
        save_from_player(
            self.get_settings(),
            cfg.index.active_dir if cfg else None,
        )

    def get_settings(self) -> dict:
        """Return a snapshot of every adjustable setting + available presets."""
        p = self.player
        cfg = p._cfg
        preset = p._preset
        from autodj.presets import BUILTIN_PRESETS

        names = sorted(set(BUILTIN_PRESETS.keys()) | set(cfg.presets.keys()))
        bpm_range = p._bpm_range
        return {
            "preset": preset.name if preset else None,
            "available_presets": names,
            "transition": cfg.transitions.effect,
            "djmix": {
                "harmonic_mixing": cfg.djmix.harmonic_mixing,
                "harmonic_mode": getattr(cfg.djmix, "harmonic_mode", "compatible"),
                "beatmatch": cfg.djmix.beatmatch,
                "phrase_align": cfg.djmix.phrase_align,
                "outro_intro_align": cfg.djmix.outro_intro_align,
                "filter_sweep": cfg.djmix.filter_sweep,
            },
            "playback": {
                "crossfade_seconds": cfg.playback.crossfade_seconds,
                "crossfade_eq_duck": cfg.playback.crossfade_eq_duck,
                "smart_shuffle": p._smart_shuffle,
                "pure_shuffle": getattr(p, "_pure_shuffle", False),
                "anchor_to_seed": getattr(p, "_anchor_to_seed", False),
                "replaygain_enabled": cfg.replaygain.enabled,
                "transition_mode": cfg.playback.transition_mode,
                "show_lyrics": getattr(cfg.playback, "show_lyrics", True),
                "enable_daypart": getattr(cfg.playback, "enable_daypart", False),
                "enable_mood_arc": getattr(cfg.playback, "enable_mood_arc", False),
                "mood_arc_hours": getattr(cfg.playback, "mood_arc_hours", 3.0),
                "import_external_cues": getattr(
                    cfg.playback,
                    "import_external_cues",
                    True,
                ),
                "beat_sync_fx": bool(
                    getattr(cfg.playback, "beat_sync_fx", True),
                ),
                "no_repeat_window": int(p._state.no_repeat_window),
                "library_size": int(len(p._sim.entries) if p._sim else 0),
                "key_sync_fx": bool(
                    getattr(cfg.playback, "key_sync_fx", True),
                ),
                "beatmatch_on_skip": bool(
                    getattr(cfg.playback, "beatmatch_on_skip", False),
                ),
                "prefetch_next_track": getattr(
                    cfg.playback,
                    "prefetch_next_track",
                    True,
                ),
                "silence_trigger_crossfade": getattr(
                    cfg.playback,
                    "silence_trigger_crossfade",
                    True,
                ),
            },
            "bpm_range": {
                "lo": bpm_range[0] if bpm_range else None,
                "hi": bpm_range[1] if bpm_range else None,
            },
            "discovery_every": p._discovery_every,
        }

    def set_preset(self, name: str | None) -> None:
        """Set the active preset by name, or pass None / '' to clear."""
        p = self.player
        if not name:
            p._preset = None
            return
        from autodj.presets import get_preset

        try:
            p._preset = get_preset(name, p._cfg.presets)
            # Apply preset-defined discovery rate if set, only when no
            # explicit discovery_every is currently configured.
            if p._preset.discovery_every and p._discovery_every is None:
                p._discovery_every = p._preset.discovery_every
        except ValueError:
            pass

    def set_transition(self, effect: str) -> None:
        """Set the transition effect by name."""
        valid = {
            "none",
            "echo_out",
            "reverb_tail",
            "highpass_sweep",
            "lowpass_sweep",
            "tape_stop",
            "gate_stutter",
            "noise_riser",
            "noise_drop",
            "backspin",
            "forward_spin",
            "cross_eq_swap",
            "bitcrusher",
            "flanger",
            "pitch_swell",
            "telephone",
            "chorus",
            "submerge",
            "vinyl_wow",
            "freeze",
            "glitch",
            "scratch",
            "beat_repeat",
            "sidechain_pump",
            "reverse_reverb",
            "air_horn",
            "random",
            "rotate",
        }
        if effect.lower() in valid:
            self.player._cfg.transitions.effect = effect.lower()

    def set_djmix(self, **flags: bool | str | None) -> None:
        """Set one or more DJ-mix toggle flags or harmonic_mode string."""
        from autodj.dj_meta import HARMONIC_MODES

        cfg = self.player._cfg
        for k, v in flags.items():
            if v is None:
                continue
            if k == "harmonic_mode":
                mode = str(v).lower()
                if mode in HARMONIC_MODES:
                    cfg.djmix.harmonic_mode = mode
                    # Auto-toggle harmonic_mixing on/off based on mode
                    cfg.djmix.harmonic_mixing = mode != "off"
                continue
            if hasattr(cfg.djmix, k):
                setattr(cfg.djmix, k, bool(v))

    def set_playback_settings(
        self,
        crossfade_seconds: float | None = None,
        crossfade_eq_duck: bool | None = None,
        smart_shuffle: bool | None = None,
        pure_shuffle: bool | None = None,
        anchor_to_seed: bool | None = None,
        replaygain_enabled: bool | None = None,
        transition_mode: str | None = None,
        show_lyrics: bool | None = None,
        enable_daypart: bool | None = None,
        enable_mood_arc: bool | None = None,
        mood_arc_hours: float | None = None,
        import_external_cues: bool | None = None,
        beat_sync_fx: bool | None = None,
        key_sync_fx: bool | None = None,
        beatmatch_on_skip: bool | None = None,
        liners_enabled: bool | None = None,
        liners_folder: str | None = None,
        liners_every_n_songs: int | None = None,
        liners_every_minutes: float | None = None,
        liners_random_min_minutes: float | None = None,
        liners_random_max_minutes: float | None = None,
        liners_pick_mode: str | None = None,
        liners_duck_db: float | None = None,
    ) -> None:
        """Apply playback-related settings; only non-null fields take effect."""
        cfg = self.player._cfg
        if crossfade_seconds is not None:
            cfg.playback.crossfade_seconds = max(0.0, float(crossfade_seconds))
        if crossfade_eq_duck is not None:
            cfg.playback.crossfade_eq_duck = bool(crossfade_eq_duck)
        if smart_shuffle is not None:
            self.player._smart_shuffle = bool(smart_shuffle)
        if pure_shuffle is not None:
            self.player._pure_shuffle = bool(pure_shuffle)
        if anchor_to_seed is not None:
            self.player._anchor_to_seed = bool(anchor_to_seed)
            # If user just enabled anchored mode and there's no seed
            # remembered (e.g. PlayerBridge attached after run() started
            # via a non-standard path), pin the current track as seed so
            # the picker has something to anchor to.
            if (
                self.player._anchor_to_seed
                and not getattr(self.player, "_seed_path", None)
                and self.player._state.current_track
            ):
                self.player._seed_path = self.player._state.current_track.path
        if replaygain_enabled is not None:
            cfg.replaygain.enabled = bool(replaygain_enabled)
        if transition_mode is not None:
            from autodj.config import _validate_transition_mode

            cfg.playback.transition_mode = _validate_transition_mode(
                str(transition_mode),
            )
        if show_lyrics is not None:
            cfg.playback.show_lyrics = bool(show_lyrics)
            if not show_lyrics:
                # Clear immediately so the web UI hides the card / CLI
                # panel doesn't flash leftover text.  Reload happens
                # naturally on the next track load when toggled back on.
                self.player._current_lyrics = []
                self.player._current_lyrics_plain = ""
        if enable_daypart is not None:
            cfg.playback.enable_daypart = bool(enable_daypart)
        if enable_mood_arc is not None:
            cfg.playback.enable_mood_arc = bool(enable_mood_arc)
            # Toggling arc on (re)anchors the start time to "now" so
            # the user always begins the envelope at warmup.
            if enable_mood_arc:
                from autodj.mood_arc import make_default_arc

                self.player._mood_arc = make_default_arc(
                    duration_hours=cfg.playback.mood_arc_hours,
                )
            else:
                self.player._mood_arc = None
        if mood_arc_hours is not None:
            cfg.playback.mood_arc_hours = max(0.25, float(mood_arc_hours))
            # Re-anchor to keep semantics consistent when the user
            # changes duration mid-session.
            if cfg.playback.enable_mood_arc:
                from autodj.mood_arc import make_default_arc

                self.player._mood_arc = make_default_arc(
                    duration_hours=cfg.playback.mood_arc_hours,
                )
        if import_external_cues is not None:
            cfg.playback.import_external_cues = bool(import_external_cues)
        if beat_sync_fx is not None:
            cfg.playback.beat_sync_fx = bool(beat_sync_fx)
        if key_sync_fx is not None:
            cfg.playback.key_sync_fx = bool(key_sync_fx)
        if beatmatch_on_skip is not None:
            cfg.playback.beatmatch_on_skip = bool(beatmatch_on_skip)
        if liners_enabled is not None:
            cfg.playback.liners_enabled = bool(liners_enabled)
        if liners_folder is not None:
            cfg.playback.liners_folder = str(liners_folder) or None
        if liners_every_n_songs is not None:
            cfg.playback.liners_every_n_songs = (
                int(liners_every_n_songs) if liners_every_n_songs > 0 else None
            )
        if liners_every_minutes is not None:
            cfg.playback.liners_every_minutes = (
                float(liners_every_minutes) if liners_every_minutes > 0 else None
            )
        if liners_random_min_minutes is not None:
            cfg.playback.liners_random_min_minutes = (
                float(liners_random_min_minutes) if liners_random_min_minutes > 0 else None
            )
        if liners_random_max_minutes is not None:
            cfg.playback.liners_random_max_minutes = (
                float(liners_random_max_minutes) if liners_random_max_minutes > 0 else None
            )
        if liners_pick_mode is not None:
            mode = str(liners_pick_mode)
            if mode in {"random", "sequential", "weighted"}:
                cfg.playback.liners_pick_mode = mode
        if liners_duck_db is not None:
            cfg.playback.liners_duck_db = float(liners_duck_db)

    def set_bpm_range(self, lo: float | None, hi: float | None) -> None:
        """Set the hard BPM filter; pass both null to clear."""
        if lo is None or hi is None or lo >= hi:
            self.player._bpm_range = None
        else:
            self.player._bpm_range = (float(lo), float(hi))

    def set_discovery_every(self, every: int | None) -> None:
        """Set the discovery rate; null disables."""
        if every is None or every <= 0:
            self.player._discovery_every = None
            self.player._state.discovery_enabled = False
        else:
            self.player._discovery_every = int(every)

    # ------------------------------------------------------------------
    # Hot-reload — pick up new tracks while a parallel `index` runs
    # ------------------------------------------------------------------

    def reload_index_from_disk(self) -> int:
        """Re-read ``metadata.json`` + ``vectors.index`` into the live sim.

        Used by the background watcher in :func:`create_app` so a long-
        running ``serve`` picks up tracks that ``autodj index`` (running
        in parallel) has freshly embedded.  No restart required — the
        next track pick consults the new entries.

        Returns:
            New track count after reload.
        """
        cfg = getattr(self.player, "_cfg", None)
        if cfg is None:
            return self.sim.ntotal
        return self.sim.reload_from_disk(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
        )


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


def create_app(bridge: PlayerBridge) -> FastAPI:
    """Create and return the FastAPI application.

    Args:
        bridge: A fully initialised :class:`PlayerBridge`.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    # Connected WebSocket clients — populated at runtime
    _ws_clients: set[WebSocket] = set()
    _ws_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifespan (replaces deprecated on_event)
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        broadcast = asyncio.create_task(_broadcast_loop())
        watcher = asyncio.create_task(_index_watcher_loop())
        try:
            yield
        finally:
            # Graceful teardown.  Order matters:
            #   1. Tell the Player thread to leave its wait-loop.  It is
            #      a daemon thread so the process can exit either way,
            #      but a clean stop lets pynput / sounddevice release
            #      OS handles instead of being torn down mid-call.
            #   2. Flush the DJ-meta sidecar so any cues / beat grids
            #      computed in background threads land on disk.
            #   3. Cancel the broadcast + watcher tasks last and await
            #      them with CancelledError suppressed so asyncio does
            #      not log "Task was destroyed but it is pending" on
            #      Ctrl+C exit.
            logger.info("Shutting down...")
            try:
                bridge.player._state.should_stop = True
                bridge.player._skip_event.set()
            except Exception:
                logger.debug("shutdown: player stop signal failed", exc_info=True)
            try:
                cache = getattr(bridge.player, "_dj_cache", None)
                if cache is not None:
                    cache.flush(force=True)
            except Exception:
                logger.debug("shutdown: dj-meta flush failed", exc_info=True)
            for task in (broadcast, watcher):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            logger.info("Server stopped cleanly.")

    app = FastAPI(title="AutoDJ", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Static HTML
    # ------------------------------------------------------------------

    # Prefer the bundled / minified output from `npm run build` when it
    # exists; fall back to the raw sources for dev (no Node toolchain
    # required).  Both directories share filenames so the FastAPI
    # routes below resolve transparently regardless of which the user
    # has on disk.  See vite.config.js for the build pipeline.
    _static_src = Path(__file__).parent / "static"
    _static_built = Path(__file__).parent / "static_dist"
    _static_dir = _static_built if (_static_built / "index.html").exists() else _static_src
    if _static_dir is _static_built:
        logger.info("Serving built static assets from %s", _static_dir)
    _static_html_path = _static_dir / "index.html"

    # Cache-busting headers so Firefox / Chrome don't keep serving
    # stale HTML / JS / CSS across server upgrades.  In a single-user
    # NAS deployment we don't need browser caching — every page load
    # should pick up the latest static assets.
    _NO_CACHE = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/", response_class=HTMLResponse)
    async def get_index() -> HTMLResponse:
        return HTMLResponse(
            content=_static_html_path.read_text(encoding="utf-8"),
            headers=_NO_CACHE,
        )

    # Convenience aliases at the top level for `app.css` / `app.js` /
    # the AudioWorklet — bypass StaticFiles caching defaults.
    # Explicit routes MUST be registered before the /static mount,
    # otherwise the mount short-circuits with default headers.
    @app.get("/app.css")
    async def get_css() -> FileResponse:
        return FileResponse(_static_dir / "app.css", media_type="text/css", headers=_NO_CACHE)

    @app.get("/app.js")
    async def get_js() -> FileResponse:
        return FileResponse(_static_dir / "app.js", media_type="text/javascript", headers=_NO_CACHE)

    @app.get("/bitcrusher-worklet.js")
    async def get_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "bitcrusher-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    @app.get("/stutter-worklet.js")
    async def get_stutter_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "stutter-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    @app.get("/freeze-worklet.js")
    async def get_freeze_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "freeze-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    @app.get("/glitch-worklet.js")
    async def get_glitch_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "glitch-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    # ES module imports.  index.html loads /app.js as a module; that
    # script's `import "./modules/foo.js"` resolves against the script
    # URL, so the browser fetches /modules/foo.js -- which previously
    # had no route and 404'd in dev mode (no `npm run build`).
    # Production (static_dist/) does not need this because vite bundles
    # everything into a single /app.js, but the route is harmless when
    # the directory is empty.  Path is sanitised to prevent traversal.
    @app.get("/modules/{name}")
    async def get_module(name: str) -> FileResponse:
        # Reject anything outside the modules/ directory.
        target = (_static_dir / "modules" / name).resolve()
        modules_root = (_static_dir / "modules").resolve()
        try:
            target.relative_to(modules_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(
            target,
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    # Serve any other assets at /static/...
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(bridge.get_state())

    @app.get("/api/version")
    async def api_version() -> JSONResponse:
        # Footer build stamp.  Lets the user verify which commit + bundle
        # the server is actually serving (browser cache vs. fresh build).
        return JSONResponse(_version_info())

    @app.post("/api/skip")
    async def api_skip() -> JSONResponse:
        bridge.skip()
        # Return fresh state so the browser updates its now-playing UI
        # without waiting up to 1 s for the next WS broadcast tick.
        return JSONResponse(bridge.get_state())

    @app.post("/api/seek")
    async def api_seek(body: SeekBody) -> dict[str, float]:
        new_pos = bridge.seek(seconds=body.seconds, delta=body.delta)
        return {"elapsed": round(new_pos, 2)}

    def _profile_store() -> Any:
        from pathlib import Path as _P

        from autodj.profiles import ProfileStore

        cfg = bridge.player._cfg
        # Default location: <index_dir>/../profiles to keep host config
        # local but adjacent to indexes.
        root = _P(cfg.index.active_dir).parent / "profiles"
        return ProfileStore(root)

    @app.get("/api/profiles")
    async def api_profiles() -> dict:
        store = _profile_store()
        return {"profiles": store.list_names(), "root": str(store.root)}

    @app.get("/api/profiles/{name}")
    async def api_profile_get(name: str) -> dict:
        from autodj.profiles import validate_name

        try:
            validate_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            snap = _profile_store().load(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return snap.to_dict()

    @app.post("/api/profiles")
    async def api_profile_save(body: ProfileSaveBody) -> dict:
        from autodj.profiles import ProfileSnapshot, validate_name

        try:
            validate_name(body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snap = ProfileSnapshot(**body.model_dump())
        target = _profile_store().save(snap)
        return {"saved": snap.name, "path": str(target)}

    @app.delete("/api/profiles/{name}")
    async def api_profile_delete(name: str) -> dict:
        from autodj.profiles import validate_name

        try:
            validate_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ok = _profile_store().delete(name)
        if not ok:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"deleted": name}

    @app.post("/api/profiles/{name}/apply")
    async def api_profile_apply(name: str) -> dict:
        """Load a saved profile and push every set field through the bridge."""
        from autodj.profiles import validate_name

        try:
            validate_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            snap = _profile_store().load(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        applied: list[str] = []
        # Playback flags
        kw: dict = {}
        for fld in (
            "transition_mode",
            "beat_sync_fx",
            "key_sync_fx",
            "beatmatch_on_skip",
            "crossfade_seconds",
            "smart_shuffle",
            "pure_shuffle",
            "anchor_to_seed",
            "enable_daypart",
            "enable_mood_arc",
            "mood_arc_hours",
            "liners_enabled",
            "liners_pick_mode",
        ):
            v = getattr(snap, fld, None)
            if v is not None:
                kw[fld] = v
                applied.append(fld)
        if kw:
            bridge.set_playback_settings(**kw)
        # BPM range
        if snap.bpm_lo is not None and snap.bpm_hi is not None:
            bridge.set_bpm_range(snap.bpm_lo, snap.bpm_hi)
            applied.append("bpm_range")
        # DJ-mix harmonic mode
        if snap.harmonic_mode is not None:
            bridge.set_djmix(harmonic_mode=snap.harmonic_mode)
            applied.append("harmonic_mode")
        # Preset
        if snap.preset is not None:
            with contextlib.suppress(Exception):
                bridge.set_preset(snap.preset)
                applied.append("preset")
        return {"applied": applied, "name": name}

    @app.get("/api/liners")
    async def api_liners() -> dict:
        """List discovered voice liners + current trigger config.

        The browser uses this to populate the Settings panel listing
        and to fetch raw liner bytes for ducking-overlay playback.
        """
        from autodj.liners import LinerLibrary

        cfg = bridge.player._cfg
        folder_str = cfg.playback.liners_folder
        if not folder_str:
            from pathlib import Path as _P

            folder_str = str(_P(cfg.index.active_dir) / "liners")
        from pathlib import Path as _P

        lib = LinerLibrary.from_folder(_P(folder_str))
        return {
            "folder": str(folder_str),
            "files": [f.name for f in lib.files],
            "count": len(lib.files),
            "config": {
                "enabled": bool(cfg.playback.liners_enabled),
                "every_n_songs": cfg.playback.liners_every_n_songs,
                "every_minutes": cfg.playback.liners_every_minutes,
                "random_min_minutes": cfg.playback.liners_random_min_minutes,
                "random_max_minutes": cfg.playback.liners_random_max_minutes,
                "pick_mode": cfg.playback.liners_pick_mode,
                "duck_db": cfg.playback.liners_duck_db,
            },
        }

    def _resolve_liner_folder() -> Path:
        """Return the configured liner folder, defaulting under index_dir."""
        from pathlib import Path as _P

        cfg = bridge.player._cfg
        folder_str = cfg.playback.liners_folder
        if not folder_str:
            folder_str = str(_P(cfg.index.active_dir) / "liners")
        return _P(folder_str)

    @app.post("/api/liners/upload")
    async def api_liner_upload(file: UploadFile = File(...)) -> dict:
        """Upload a new liner clip into the configured folder.

        Rejects files whose extension isn't in :data:`LINER_EXTS` so
        users can't drop arbitrary binaries into the served folder.
        Creates the folder when missing.
        """
        from autodj.liners import LINER_EXTS

        name = file.filename or ""
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in LINER_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported extension {ext!r}; allowed: {', '.join(LINER_EXTS)}",
            )
        # Strip path components from the filename so the upload always
        # lands directly in the liner folder regardless of what the
        # browser sent.
        from pathlib import PurePosixPath

        safe_name = PurePosixPath(name).name
        folder = _resolve_liner_folder()
        folder.mkdir(parents=True, exist_ok=True)
        target = (folder / safe_name).resolve()
        if not str(target).startswith(str(folder.resolve())):
            raise HTTPException(status_code=400, detail="Invalid filename")
        contents = await file.read()
        target.write_bytes(contents)
        return {"filename": safe_name, "size": len(contents)}

    @app.delete("/api/liners/file/{name}")
    async def api_liner_delete(name: str) -> dict:
        """Remove a liner clip identified by filename.

        Same path-traversal guard as the GET endpoint.
        """
        from pathlib import Path as _P

        folder = _resolve_liner_folder().resolve()
        target = (folder / name).resolve()
        if not str(target).startswith(str(folder)) or not target.is_file():
            raise HTTPException(status_code=404, detail="Liner not found")
        try:
            _P(target).unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"deleted": target.name}

    @app.get("/api/liners/file/{name}")
    async def api_liner_file(name: str) -> FileResponse:
        """Stream the raw bytes of a liner clip identified by filename.

        Resolves *name* against the liners folder; rejects path
        traversal by ensuring the resolved file is inside that folder.
        """
        import mimetypes
        from pathlib import Path as _P

        cfg = bridge.player._cfg
        folder_str = cfg.playback.liners_folder
        if not folder_str:
            folder_str = str(_P(cfg.index.active_dir) / "liners")
        folder = _P(folder_str).resolve()
        target = (folder / name).resolve()
        if not str(target).startswith(str(folder)) or not target.is_file():
            raise HTTPException(status_code=404, detail="Liner not found")
        mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(target, media_type=mime or "application/octet-stream")

    @app.post("/api/pause")
    async def api_pause() -> dict[str, bool]:
        paused = bridge.pause()
        return {"paused": paused}

    @app.post("/api/volume")
    async def api_volume(body: VolumeBody) -> dict[str, float]:
        bridge.set_volume(body.volume)
        return {"volume": round(bridge.player._state.volume, 2)}

    @app.post("/api/mute")
    async def api_mute() -> dict[str, bool]:
        muted = bridge.toggle_mute()
        return {"muted": muted}

    @app.post("/api/play-next")
    async def api_play_next(body: PlayNextBody) -> dict[str, bool]:
        found = bridge.play_next(body.path, now=body.now)
        return {"ok": found}

    @app.get("/api/search")
    async def api_search(q: str = "", limit: int = 100) -> dict[str, list]:
        results = bridge.search(q, limit=max(1, min(500, int(limit))))
        return {"results": results}

    # ------------------------------------------------------------------
    # Queue manipulation
    # ------------------------------------------------------------------

    @app.post("/api/queue/add")
    async def api_queue_add(body: PlayNextBody) -> dict[str, bool]:
        return {"ok": bridge.queue_add(body.path)}

    @app.post("/api/queue/remove")
    async def api_queue_remove(body: PlayNextBody) -> dict[str, bool]:
        return {"ok": bridge.queue_remove(body.path)}

    @app.post("/api/queue/reorder")
    async def api_queue_reorder(body: QueueReorderBody) -> dict[str, bool]:
        return {"ok": bridge.queue_reorder(body.paths)}

    # ------------------------------------------------------------------
    # Cover art and lyrics
    # ------------------------------------------------------------------

    @app.get("/api/art")
    async def api_art(path: str) -> Response:
        # Only serve art for tracks that exist in the index — prevents
        # arbitrary file access through the path parameter.
        known = any(e.path == path for e in bridge.sim.entries)
        if not known:
            raise HTTPException(status_code=404, detail="Track not in index")
        result = bridge.cover_art_for(path)
        if result is None:
            raise HTTPException(status_code=404, detail="No embedded cover art")
        data, mime = result
        return Response(content=data, media_type=mime)

    @app.get("/api/lyrics")
    async def api_lyrics() -> dict[str, list]:
        return {"lyrics": bridge.current_lyrics()}

    # ------------------------------------------------------------------
    # Browser-driven playback — stream audio bytes + advance trigger
    # ------------------------------------------------------------------

    _MIME_BY_SUFFIX = {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".aif": "audio/aiff",
        ".aiff": "audio/aiff",
    }

    def _audio_mime(p: Path) -> str:
        return _MIME_BY_SUFFIX.get(p.suffix.lower(), "application/octet-stream")

    def _is_alac(p: Path) -> bool:
        """True iff *p* is an ALAC stream (Apple Lossless inside .m4a)."""
        if p.suffix.lower() not in (".m4a", ".mp4"):
            return False
        try:
            from mutagen.mp4 import MP4

            info = MP4(str(p)).info
            codec = getattr(info, "codec", None) or ""
            return codec.lower() == "alac"
        except (OSError, ValueError, ImportError):
            return False

    async def _transcode_alac_to_mp3(p: Path) -> AsyncGenerator[bytes]:  # pragma: no cover
        """Yield MP3 bytes from an ALAC source via ffmpeg subprocess.

        Browser ALAC support is Safari-only, so for Chrome/Firefox we
        decode + re-encode to MP3 on the fly.  Range requests are not
        supported on transcoded streams (ffmpeg can't seek-then-encode
        cheaply); browser falls back to sequential playback which is
        fine for auto-DJ use.

        Excluded from coverage: requires a real ffmpeg subprocess with
        an ALAC source — neither is available in CI.
        """
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(p),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-f",
            "mp3",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                chunk = await stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    @app.get("/api/audio")
    async def api_audio(path: str, request: Request) -> Response:
        """Stream the bytes of an indexed audio file with HTTP Range support.

        The browser <audio> element uses this to play whatever the server
        currently reports as ``current_track``.  Range support is required
        for seek bar scrubbing and for browsers that probe metadata via a
        small initial range request.

        Path-traversal is prevented by allowing only paths that appear
        verbatim in the loaded similarity index.
        """
        # Path validation — must be in the index
        known = any(e.path == path for e in bridge.sim.entries)
        if not known:
            raise HTTPException(status_code=404, detail="Track not in index")
        f = Path(path)
        if not f.exists() or not f.is_file():
            raise HTTPException(status_code=404, detail="File not found on disk")

        # Transcode ALAC → MP3 on the fly so non-Safari browsers can
        # play Apple Lossless tracks.  ffmpeg must be on PATH.
        if _is_alac(f):  # pragma: no cover — requires ALAC source + ffmpeg
            try:
                return StreamingResponse(
                    _transcode_alac_to_mp3(f),
                    media_type="audio/mpeg",
                    headers={"Accept-Ranges": "none"},
                )
            except FileNotFoundError:
                logger.warning(
                    "ffmpeg not on PATH; serving ALAC raw (browser may reject).",
                )

        file_size = f.stat().st_size
        mime = _audio_mime(f)
        range_header = request.headers.get("range") or request.headers.get("Range")

        # No Range — full body
        if not range_header:

            def _full_iter() -> AsyncGenerator[bytes]:
                async def _gen() -> AsyncGenerator[bytes]:
                    chunk = 64 * 1024
                    with open(f, "rb") as fh:
                        while True:
                            data = fh.read(chunk)
                            if not data:
                                break
                            yield data

                return _gen()

            return StreamingResponse(
                _full_iter(),
                media_type=mime,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size),
                },
            )

        # Parse "bytes=START-END" — only single-range supported
        try:
            units, _, ranges = range_header.partition("=")
            if units.strip().lower() != "bytes":
                raise ValueError
            start_s, _, end_s = ranges.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
        except (ValueError, AttributeError):
            raise HTTPException(status_code=416, detail="Invalid Range header") from None

        if start < 0 or start >= file_size or end >= file_size or start > end:
            raise HTTPException(
                status_code=416,
                detail="Requested range not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        length = end - start + 1

        async def _range_gen() -> AsyncGenerator[bytes]:
            chunk = 64 * 1024
            remaining = length
            with open(f, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    data = fh.read(min(chunk, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            _range_gen(),
            status_code=206,
            media_type=mime,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
            },
        )

    @app.post("/api/advance")
    async def api_advance() -> JSONResponse:
        """Browser signals end-of-track — server picks next track.

        Returns the fresh state synchronously so the browser can update
        ``current_track`` / ``next_track`` immediately without waiting
        for the 1 Hz WS broadcast.  Decouples advance latency from the
        broadcast cadence.
        """
        bridge.skip()
        return JSONResponse(bridge.get_state())

    @app.post("/api/repick-next")
    async def api_repick_next(request: Request) -> JSONResponse:
        """Replace next_track without advancing current.

        Browser POSTs this when its standby deck fails to load the
        prefetched next-track (bad codec, codec not supported on this
        client, network drop).  Server picks a different next-track so
        the currently-playing track keeps playing instead of being
        cascade-skipped along with the unplayable upcoming.

        Optional JSON body: ``{"blacklist": "path/that/failed.flac"}``
        adds the failed path to recently-played so similarity skips it.
        """
        blacklist = None
        try:
            body = await request.json()
            blacklist = body.get("blacklist") if isinstance(body, dict) else None
        except (ValueError, TypeError):
            # No JSON body or malformed payload — endpoint accepts either.
            logger.debug("repick_next: no JSON body, blacklist not set")
        bridge.repick_next(blacklist_path=blacklist)
        return JSONResponse(bridge.get_state())

    @app.post("/api/random-track")
    async def api_random_track() -> JSONResponse:
        """Reseed the auto-DJ from a fresh random track in the index.

        Status code distinguishes success from an empty index:
        ``200`` when a track was picked, ``409 Conflict`` when the
        index has nothing to reseed from.
        """
        if not bridge.reseed_random():
            raise HTTPException(status_code=409, detail="Index is empty")
        return JSONResponse(bridge.get_state())

    # ------------------------------------------------------------------
    # Settings (mirror of CLI flags)
    # ------------------------------------------------------------------

    @app.get("/api/settings")
    async def api_settings() -> dict:
        return bridge.get_settings()

    @app.post("/api/preset")
    async def api_preset(body: PresetBody) -> dict:
        bridge.set_preset(body.name)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/transition")
    async def api_transition(body: TransitionBody) -> dict:
        bridge.set_transition(body.effect)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/djmix")
    async def api_djmix(body: DjMixBody) -> dict:
        bridge.set_djmix(**body.model_dump(exclude_none=True))
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/playback-settings")
    async def api_playback_settings(body: PlaybackSettingsBody) -> dict:
        bridge.set_playback_settings(**body.model_dump(exclude_none=True))
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/bpm-range")
    async def api_bpm_range(body: BpmRangeBody) -> dict:
        bridge.set_bpm_range(body.lo, body.hi)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/discovery")
    async def api_discovery(body: DiscoveryBody) -> dict:
        bridge.set_discovery_every(body.every)
        bridge.save_persistent_state()
        return bridge.get_settings()

    # ------------------------------------------------------------------
    # 3-band EQ
    # ------------------------------------------------------------------

    @app.post("/api/eq")
    async def api_eq(body: EqBody) -> dict[str, float]:
        return bridge.set_eq(low=body.low, mid=body.mid, high=body.high)

    # ------------------------------------------------------------------
    # Library tools — index / enrich / prune / stats from the web UI
    # ------------------------------------------------------------------

    @app.get("/api/library/job")
    async def api_library_job_status() -> dict:
        from autodj.jobs import get_manager

        return get_manager().snapshot()

    @app.post("/api/library/run")
    async def api_library_run(body: LibraryJobBody) -> dict:
        from autodj.jobs import get_manager

        mgr = get_manager()
        ok = mgr.start(body.name, body.args)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail="Job already running, or subcommand not allowed.",
            )
        return mgr.snapshot()

    @app.post("/api/library/stop")
    async def api_library_stop() -> dict[str, bool]:
        from autodj.jobs import get_manager

        return {"stopped": get_manager().stop()}

    @app.get("/api/library/stats")
    async def api_library_stats() -> dict:
        """Return summary stats about the loaded SimilarityIndex."""
        sim = bridge.sim
        entries = sim.entries
        n = len(entries)
        bpms = [e.bpm for e in entries if e.bpm > 0]
        avg_bpm = round(sum(bpms) / len(bpms), 1) if bpms else 0.0
        with_genre = sum(1 for e in entries if e.genre)
        with_key = sum(1 for e in entries if e.key >= 0 and e.mode >= 0)
        with_energy = sum(1 for e in entries if e.energy > 0)
        return {
            "track_count": n,
            "tracks_with_bpm": len(bpms),
            "average_bpm": avg_bpm,
            "tracks_with_genre": with_genre,
            "tracks_with_key": with_key,
            "tracks_with_energy": with_energy,
        }

    # ------------------------------------------------------------------
    # WebSocket broadcast
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        # Parameter renamed from 'ws' to 'websocket' to avoid FastAPI
        # mistaking the path segment '/ws' for a query parameter.
        await websocket.accept()
        client_host = getattr(getattr(websocket, "client", None), "host", "unknown")
        async with _ws_lock:
            _ws_clients.add(websocket)
            client_count = len(_ws_clients)
        logger.info(
            "WebSocket connected: %s (%d active client%s)",
            client_host,
            client_count,
            "" if client_count == 1 else "s",
        )
        try:
            while True:
                text = await websocket.receive_text()
                # Handle incoming control commands from the client
                try:
                    msg = _json.loads(text)
                    if isinstance(msg, dict) and msg.get("type") == "toggle_discovery":
                        bridge.toggle_discovery()
                except (_json.JSONDecodeError, AttributeError):
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            async with _ws_lock:
                _ws_clients.discard(websocket)
                remaining = len(_ws_clients)
            logger.info(
                "WebSocket disconnected: %s (%d active client%s remain)",
                client_host,
                remaining,
                "" if remaining == 1 else "s",
            )

    async def _broadcast_loop() -> None:  # pragma: no cover — long-running task
        """Push state JSON to all connected WebSocket clients once per second."""
        while True:
            await asyncio.sleep(1)
            if not _ws_clients:
                continue
            payload = _json.dumps(bridge.get_state())
            dead: list[WebSocket] = []
            async with _ws_lock:
                clients = list(_ws_clients)
            for client in clients:
                try:
                    await client.send_text(payload)
                except (RuntimeError, WebSocketDisconnect, ConnectionError):
                    dead.append(client)
            if dead:
                async with _ws_lock:
                    for client in dead:
                        _ws_clients.discard(client)

    async def _index_watcher_loop() -> None:  # pragma: no cover — long-running task
        """Reload the FAISS index when ``metadata.json`` mtime changes.

        Runs every 10 seconds.  Lets a parallel ``autodj index`` add
        tracks while a long-running ``serve`` is up — the next track
        pick will see the new entries without restarting the server.
        """
        cfg = getattr(bridge.player, "_cfg", None)
        if cfg is None:
            return
        meta_path = cfg.index.active_dir / "metadata.json"
        last_mtime = meta_path.stat().st_mtime if meta_path.exists() else 0.0
        while True:
            await asyncio.sleep(10)
            try:
                if not meta_path.exists():
                    continue
                mtime = meta_path.stat().st_mtime
                if mtime > last_mtime:
                    last_mtime = mtime
                    new_total = await asyncio.to_thread(
                        bridge.reload_index_from_disk,
                    )
                    logger.info(
                        "Index reloaded — %d tracks now available",
                        new_total,
                    )
            except (OSError, ValueError) as exc:
                logger.debug("Index watcher: %s", exc)

    return app


# ---------------------------------------------------------------------------
# serve() — wires everything together
# ---------------------------------------------------------------------------


def serve(
    cfg: AutoDJConfig,
    sim: SimilarityIndex,
    seed_entry: IndexEntry | None,
    host: str = "127.0.0.1",
    port: int = 8080,
    preset: Preset | None = None,
    export_m3u: Path | None = None,
    history_file: Path | None = None,
    discovery_every: int | None = None,
    bpm_range: tuple[float, float] | None = None,
    smart_shuffle: bool = False,
    pure_shuffle: bool = False,
    anchor_to_seed: bool = False,
    no_playback: bool = False,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    """Start the Player thread and the FastAPI/uvicorn web server.

    This function blocks until uvicorn is stopped (Ctrl+C).

    Args:
        cfg: Full :class:`~autodj.config.AutoDJConfig` instance.
        sim: Loaded :class:`~autodj.similarity.SimilarityIndex`.
        seed_entry: Starting :class:`~autodj.indexer.IndexEntry`, or ``None``
            for a random track.
        host: Interface to bind uvicorn to.
        port: Port to bind uvicorn to.
        preset: Optional BPM-shaping preset (forwarded to Player).
        export_m3u: Optional path for live M3U export (forwarded to Player).
        history_file: Optional path for play history (forwarded to Player).
        discovery_every: Discovery injection rate (forwarded to Player).
        bpm_range: Hard BPM filter ``(lo, hi)`` (forwarded to Player).
    """
    import importlib.util as _import_util

    import uvicorn

    from autodj.player import Player

    # Auto-detect missing audio deps and flip to no-playback so headless
    # hosts (NAS, server) don't spam "module not found" per track.
    if not no_playback:
        missing: list[str] = []
        for name in ("soundfile", "sounddevice"):
            try:
                if _import_util.find_spec(name) is None:
                    missing.append(name)
            except (ImportError, ValueError):
                # ValueError fires when the name is mocked / partially
                # installed (e.g. during tests).  Treat as available.
                pass
        if missing:  # pragma: no cover — minimal-install branch
            print(
                "[AutoDJ] Headless mode — browser handles audio output. "
                "Open the web UI from any device on your network.",
            )
            no_playback = True

    player = Player(
        cfg,
        sim,
        dry_run=no_playback,
        preset=preset,
        export_m3u=export_m3u,
        history_file=history_file,
        discovery_every=discovery_every,
        bpm_range=bpm_range,
        smart_shuffle=smart_shuffle,
        pure_shuffle=pure_shuffle,
        anchor_to_seed=anchor_to_seed,
        # The browser is the control surface in serve mode — disable
        # the global pynput keyboard hook so keys typed in OTHER apps
        # / tabs / windows don't accidentally pause / skip / mute the
        # player.
        no_keyboard=True,
    )
    bridge = PlayerBridge(player=player, sim=sim)

    # Restore previously-saved settings (preset, transition, EQ, etc.)
    # so the user doesn't have to re-tick everything on each `serve` restart.
    bridge.load_persistent_state()

    # Start Player in a daemon thread — it blocks internally on playback
    player_thread = threading.Thread(
        target=player.run,
        kwargs={"seed_entry": seed_entry},
        name="autodj-player",
        daemon=True,
    )
    player_thread.start()

    app = create_app(bridge)

    scheme = "https" if (ssl_certfile and ssl_keyfile) else "http"
    pretty_host = (
        "localhost"
        if host in ("0.0.0.0", "127.0.0.1", "::")  # nosec B104 — display only
        else host
    )
    audio_mode = "server-audio" if not no_playback else "browser-driven"
    logger.info(
        "AutoDJ server ready: %s://%s:%d  (%s, %d indexed tracks, seed=%s)",
        scheme,
        pretty_host,
        port,
        audio_mode,
        len(getattr(sim, "entries", []) or []),
        getattr(seed_entry, "display_name", "random"),
    )
    logger.info(
        "Tip: pass `-v` (autodj -v serve) for debug logs.  "
        "Browser-side debug: append ?debug=1 to the URL or set "
        "localStorage.autodjDebug='1'.",
    )

    # ssl_certfile / ssl_keyfile flip uvicorn into HTTPS mode, which is
    # required by browsers to expose AudioWorklet on non-localhost hosts.
    # Without HTTPS the freeze / glitch / bitcrusher worklets fall back
    # to BufferSource / WaveShaper approximations.
    uvicorn_kwargs: dict = {
        "host": host,
        "port": port,
        "log_level": "warning",
    }
    if ssl_certfile and ssl_keyfile:
        uvicorn_kwargs["ssl_certfile"] = ssl_certfile
        uvicorn_kwargs["ssl_keyfile"] = ssl_keyfile
    # uvicorn normally swallows SIGINT and exits cleanly via the
    # FastAPI lifespan, but a second Ctrl+C (or a SIGINT received
    # mid-asyncio-teardown on Windows) can re-raise.  Suppress so the
    # CLI sees a clean return; the lifespan already logged
    # "Server stopped cleanly."
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, **uvicorn_kwargs)
