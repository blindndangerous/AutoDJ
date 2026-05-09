"""PlayerBridge — thread-safe adapter between Player and FastAPI app.

Extracted from ``autodj.server`` so neither file balloons over the 2000-
line working budget.  Re-exported from :mod:`autodj.server` for API
compatibility (``from autodj.server import PlayerBridge`` still works).

Most attribute / module references inside the methods are deferred via
local ``import`` statements; that keeps the minimal-install path light
(the bridge file is imported by the server module on every ``serve``
call) and avoids circular imports between ``autodj.player`` and the
server stack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry

logger = logging.getLogger(__name__)


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


def _history_entry(entry: Any) -> dict:
    return {
        "title": getattr(entry, "title", "") or "",
        "artist": getattr(entry, "artist", "") or "",
        "duration": float(getattr(entry, "length", 0.0) or 0.0),
        "played_at": datetime.now(UTC).isoformat(),
    }


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
    _play_history: list = field(default_factory=list, init=False)

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def record_seed(self, entry: Any) -> None:
        """Append the seed track to history before the first advance fires."""
        if entry is not None and not self._play_history:
            self._play_history.append(_history_entry(entry))

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
        # Lazy-capture seed on first advance (seed never goes through advance_now as nxt).
        if not self._play_history and cur is not None:
            self._play_history.append(_history_entry(cur))
        self._play_history.append(_history_entry(nxt))
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
        # Record nxt as played BEFORE calling _pick_next so the FAISS
        # self-match (nxt is its own nearest neighbour at score=1.0) is
        # excluded from the next pick.  Without this, every song after
        # the seed played twice: nxt was not yet in recently_played when
        # _pick_next(nxt) ran, so nxt was returned as next_track again.
        state.record_played(nxt)
        state.track_number += 1
        p._previous_track = cur
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

        # Single-line advance banner (INFO).  Shows outgoing -> incoming
        # with BPM + key + pick mode so a user tailing the log can see
        # exactly what the browser just crossfaded into.  Key is
        # rendered in the user's selected notation (camelot or musical
        # letter names; flats / sharps preference honoured for musical
        # mode).  Cur is None on the very first seed advance; format
        # conditionally.
        try:
            from autodj.dj_meta import key_label  # local — avoid cycles

            cfg_pb = getattr(p._cfg, "playback", None)
            notation = getattr(cfg_pb, "key_notation", "camelot") if cfg_pb else "camelot"
            prefer_flats = bool(getattr(cfg_pb, "key_prefer_flats", False)) if cfg_pb else False

            def _fmt(t: Any) -> str:
                if t is None:
                    return "(none)"
                bpm = f"{t.bpm:.0f} BPM" if getattr(t, "bpm", 0) else "BPM ?"
                lbl = key_label(
                    getattr(t, "key", -1),
                    getattr(t, "mode", -1),
                    notation,
                    prefer_flats=prefer_flats,
                )
                return f"{t.display_name} ({bpm}, {lbl})"

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
        from autodj.dj_meta import camelot_label, key_label

        # Two key fields per track:
        #   ``key_label``  -- notation-aware display string (Camelot or
        #                     letter-name, sharps or flats per settings).
        #                     Drives the now-playing badge + log lines.
        #   ``camelot_cell`` -- always-Camelot cell address (8A / 8B)
        #                     used by the Camelot wheel SVG, since the
        #                     wheel is Camelot-shaped regardless of
        #                     which display notation the user picked.
        cfg_pb = getattr(self.player._cfg, "playback", None)
        _key_notation = getattr(cfg_pb, "key_notation", "camelot") if cfg_pb else "camelot"
        _key_prefer_flats = bool(getattr(cfg_pb, "key_prefer_flats", False)) if cfg_pb else False

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
                "camelot_cell": camelot_label(entry.key, entry.mode),
                "key_label": key_label(
                    entry.key,
                    entry.mode,
                    _key_notation,
                    prefer_flats=_key_prefer_flats,
                ),
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
                "fade_in_seconds": getattr(cfg.playback, "fade_in_seconds", 3.0),
                "crossfade_eq_duck": cfg.playback.crossfade_eq_duck,
                "smart_shuffle": p._smart_shuffle,
                "pure_shuffle": getattr(p, "_pure_shuffle", False),
                "anchor_to_seed": getattr(p, "_anchor_to_seed", False),
                "replaygain_enabled": cfg.replaygain.enabled,
                "transition_mode": cfg.playback.transition_mode,
                "key_notation": getattr(cfg.playback, "key_notation", "camelot"),
                "key_prefer_flats": bool(
                    getattr(cfg.playback, "key_prefer_flats", False),
                ),
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
        fade_in_seconds: float | None = None,
        crossfade_eq_duck: bool | None = None,
        smart_shuffle: bool | None = None,
        pure_shuffle: bool | None = None,
        anchor_to_seed: bool | None = None,
        replaygain_enabled: bool | None = None,
        transition_mode: str | None = None,
        key_notation: str | None = None,
        key_prefer_flats: bool | None = None,
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
        if fade_in_seconds is not None:
            cfg.playback.fade_in_seconds = max(0.0, float(fade_in_seconds))
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
        if key_notation is not None:
            from autodj.config import _validate_key_notation

            cfg.playback.key_notation = _validate_key_notation(str(key_notation))
        if key_prefer_flats is not None:
            cfg.playback.key_prefer_flats = bool(key_prefer_flats)
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
