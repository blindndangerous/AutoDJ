"""Bounded-window transition preparation for streamed server playout."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from fractions import Fraction
from typing import Any

import numpy as np

from autodj.live_audio import AudioLayer, BufferedFile, LiveAudio
from autodj.mixbus import RenderedTrack
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)
_WINDOW_READ_FRAMES = SAMPLE_RATE // 4
_WINDOW_TIMEOUT = 30.0
_RESAMPLE_CONTEXT_FRAMES = 4_096


def _read_window(player: Any, entry: Any, deck: BufferedFile, first: int, last: int) -> np.ndarray:
    """Decode a bounded source interval off the bus thread using the deck's filters."""
    first, last = max(0, first), min(round(float(entry.length or 0) * SAMPLE_RATE), last)
    if last <= first:
        return np.zeros((0, 2), np.float32)
    source = BufferedFile(
        entry.path,
        round(float(entry.length or 0) * SAMPLE_RATE),
        filters=deck.filters,
        start=first,
        lift_db=deck.lift_db,
    )
    parts = []
    position = first
    try:
        while position < last:
            count = min(_WINDOW_READ_FRAMES, last - position)
            deadline = time.monotonic() + _WINDOW_TIMEOUT
            while not source.ready(position, count):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out decoding transition window: {entry.path}")
                time.sleep(0.002)
            parts.append(source.read(position, count))
            position += count
        return np.concatenate(parts) if len(parts) > 1 else parts[0]
    finally:
        source.close()


class _WindowAudio:
    """Array-like source whose slices decode only the requested source window."""

    def __init__(self, player: Any, entry: Any, deck: BufferedFile) -> None:
        self.player, self.entry, self.deck = player, entry, deck
        self.frames = round(float(entry.length or 0) * SAMPLE_RATE)
        self.override: tuple[int, np.ndarray] | None = None

    def __len__(self) -> int:
        return self.frames

    def __getitem__(self, key: slice | int) -> np.ndarray:
        if isinstance(key, int):
            first, last = (key, key + 1) if key >= 0 else (self.frames + key, self.frames + key + 1)
        else:
            first, last, step = key.indices(self.frames)
            if step != 1:
                raise ValueError("transition audio windows require unit steps")
        audio = _read_window(self.player, self.entry, self.deck, first, last)
        if self.override is not None and len(audio):
            offset, replacement = self.override
            begin, end = max(first, offset), min(last, offset + len(replacement))
            if begin < end:
                audio[begin - first : end - first] = replacement[begin - offset : end - offset]
        return audio


def _stream_body(
    player: Any,
    entry: Any,
    deck: BufferedFile,
    start_offset: int,
    end_offset: int,
    rate: float,
    glide_in: Any = None,
    glide_audio: np.ndarray | None = None,
) -> LiveAudio:
    """Stream a track body, substituting only a bounded incoming glide when present."""
    from autodj.player import _music_output_frame, _music_segment

    start_out = _music_output_frame(start_offset, rate)
    end_out = _music_output_frame(end_offset, rate)
    total = max(0, end_out - start_out)
    layers = []
    if glide_audio is not None and glide_in is not None and glide_in.origin == start_offset:
        glide_end = min(end_offset, glide_in.source_end)
        glide_out = max(0, _music_output_frame(glide_end, rate) - start_out)
        if glide_out:
            layers.append(AudioLayer(_music_segment(glide_audio, rate, glide_out), 0, glide_out))
        body_first = max(start_offset, glide_in.source_end)
        body_out = max(0, _music_output_frame(body_first, rate) - start_out)
    else:
        body_first, body_out = start_offset, 0
    if body_out < total:
        layers.append(
            AudioLayer(deck, body_out, total - body_out, _music_output_frame(body_first, rate))
        )
    return LiveAudio(total, tuple(layers))


def _metadata_warning(player: Any, current: Any, upcoming: Any, meta_a: Any, meta_b: Any) -> None:
    cfg = player._cfg
    needs_markers = (
        cfg.playback.transition_mode != "fixed"
        or cfg.djmix.outro_intro_align
        or cfg.djmix.phrase_align
        or cfg.djmix.beatmatch
        or cfg.djmix.drop_mix
        or cfg.djmix.key_shift
    )
    if needs_markers and (meta_a is None or meta_b is None):
        logger.warning(
            "Cached DJ metadata is unavailable for %s -> %s; using normal fallbacks while analysis is scheduled",
            current.path,
            upcoming.path,
        )
        analyse = getattr(player, "analyse_track_in_background", None)
        if callable(analyse):
            for entry, meta in ((current, meta_a), (upcoming, meta_b)):
                if meta is None:
                    analyse(entry.path)


def build_live_transition(
    player: Any, current: Any, next_entry: Any, start_offset: int, glide_in: Any = None
) -> RenderedTrack | None:
    """Prepare one live server transition without decoding either whole music file.

    This runs on the render-ahead worker. Music bodies stay on buffered file
    decks; only intro, drop-check, glide and effect windows become arrays.
    """
    from autodj.player import _music_output_frame, _music_segment

    try:
        source = player._render_live_file(current, incoming=False)
        rate_a = player.music_playback_rate(current)
        source_frames = round(float(current.length or 0) * SAMPLE_RATE)
        if start_offset >= source_frames:
            return None
        meta_a = player._peek_incoming_meta(current)
        active_glide = (
            glide_in if glide_in is not None and glide_in.origin == start_offset else None
        )
        glide_audio = None
        if active_glide is not None and active_glide.fits(source_frames):
            need = min(source_frames, active_glide.source_end + 4096)
            decoded_glide = _read_window(player, current, source, 0, need)
            glide_audio = active_glide.continuation(decoded_glide)
            current_audio = _WindowAudio(player, current, source)
            current_audio.override = (active_glide.origin, glide_audio)
        else:
            current_audio = _WindowAudio(player, current, source)

        if next_entry is None:
            body = _stream_body(
                player,
                current,
                source,
                start_offset,
                source_frames,
                rate_a,
                active_glide,
                glide_audio,
            )
            return RenderedTrack(
                current,
                body,
                None,
                0,
                "",
                start_offset=start_offset,
                glide_in=glide_in,
                loudness_processing=player._cfg.playback.loudness_processing or "",
                full_audio=body,
                playback_rate=rate_a,
            )

        incoming_source = player._render_live_file(next_entry, incoming=True)
        incoming_audio = _WindowAudio(player, next_entry, incoming_source)
        meta_b = player._peek_incoming_meta(next_entry)
        _metadata_warning(player, current, next_entry, meta_a, meta_b)
        fade_seconds = player._effective_crossfade_seconds(meta_a, meta_b, current.length)
        crossfade = int(fade_seconds * SAMPLE_RATE)
        if crossfade >= source_frames - start_offset or crossfade <= 0:
            body = _stream_body(
                player,
                current,
                source,
                start_offset,
                source_frames,
                rate_a,
                active_glide,
                glide_audio,
            )
            return RenderedTrack(
                current,
                body,
                next_entry,
                0,
                "",
                start_offset=start_offset,
                glide_in=glide_in,
                loudness_processing=player._cfg.playback.loudness_processing or "",
                full_audio=body,
                playback_rate=rate_a,
            )

        intro = player._skip_incoming_silence_samples(incoming_audio, SAMPLE_RATE, meta_b)
        fade_start = player._crossfade_start_in_a(current_audio, SAMPLE_RATE, meta_a, crossfade)
        liner_a, liner_b = player._is_liner(current), player._is_liner(next_entry)
        rate_b = player.music_playback_rate(next_entry)
        if (liner_a or liner_b) and (rate_a != 1.0 or rate_b != 1.0):
            from autodj.player import _apply_crossfade

            a_start = max(0, fade_start - start_offset)
            available_b = len(incoming_audio) - intro
            if not liner_b:
                available_b = round(available_b / rate_b)
            if liner_a:
                crossfade = min(crossfade, max(0, source_frames - start_offset - a_start))
            crossfade = min(crossfade, max(0, available_b))
            if crossfade <= 0:
                body = _stream_body(
                    player,
                    current,
                    source,
                    start_offset,
                    source_frames,
                    rate_a,
                    active_glide,
                    glide_audio,
                )
                return RenderedTrack(
                    current,
                    body,
                    next_entry,
                    0,
                    "none",
                    start_offset=start_offset,
                    loudness_processing=player._cfg.playback.loudness_processing or "",
                    full_audio=body,
                    playback_rate=rate_a,
                    next_playback_rate=rate_b,
                )
            if liner_a:
                prefix_frames = a_start
                a_tail = current_audio[start_offset + a_start : start_offset + a_start + crossfade]
            else:
                source_tail_frames = min(
                    source_frames - start_offset, max(0, round(crossfade * rate_a))
                )
                a_start = min(a_start, source_frames - start_offset - source_tail_frames)
                prefix_frames = _music_output_frame(a_start, rate_a)
                a_tail = _music_segment(
                    current_audio[
                        start_offset + a_start : start_offset + a_start + source_tail_frames
                    ],
                    rate_a,
                    crossfade,
                )
            incoming_consumed = (
                crossfade
                if liner_b
                else min(len(incoming_audio) - intro, max(0, round(crossfade * rate_b)))
            )
            b_head = incoming_audio[intro : intro + incoming_consumed]
            if not liner_b:
                b_head = _music_segment(b_head, rate_b, crossfade)
            mixed = _apply_crossfade(a_tail, b_head, crossfade)
            layers = []
            if prefix_frames:
                layers.append(
                    AudioLayer(
                        source,
                        0,
                        prefix_frames,
                        _music_output_frame(start_offset, rate_a),
                    )
                )
            layers.append(AudioLayer(mixed, prefix_frames, len(mixed)))
            audio = LiveAudio(prefix_frames + len(mixed), tuple(layers))
            return RenderedTrack(
                current,
                audio,
                next_entry,
                intro + incoming_consumed,
                "none",
                start_offset=start_offset,
                overlap_frames=crossfade,
                next_entry_offset=intro,
                loudness_processing=player._cfg.playback.loudness_processing or "",
                full_audio=audio,
                playback_rate=rate_a,
                next_playback_rate=rate_b,
            )
        drop = player._drop_mix(
            current,
            next_entry,
            meta_a,
            meta_b,
            current_audio,
            incoming_audio,
            entry=intro,
            fade_start=fade_start,
            crossfade=crossfade,
            earliest=start_offset,
        )
        if drop is not None:
            intro, crossfade, match = drop.entry, drop.overlap, drop.match
            fade_start = match.fade_start
            player._plan_drop_riser(current, next_entry)
        else:
            match = player._beatmatch_plan(
                current,
                next_entry,
                meta_a,
                meta_b,
                entry=intro,
                fade_start=fade_start,
                crossfade=crossfade,
                earliest=start_offset,
                latest=source_frames - crossfade,
            )
            fade_start, crossfade = player._guard_vocals(
                current,
                next_entry,
                meta_a,
                match,
                entry=intro,
                crossfade=crossfade,
                earliest=start_offset,
                length=source_frames,
            )
        match = player._with_key_shift(match, current, next_entry, meta_b, intro)
        fade_start = max(start_offset, fade_start)
        crossfade = min(crossfade, max(0, source_frames - fade_start))
        if crossfade <= 0 or len(incoming_audio) - intro < crossfade:
            body_end = min(source_frames, fade_start + crossfade)
            body = _stream_body(
                player, current, source, start_offset, body_end, rate_a, active_glide, glide_audio
            )
            return RenderedTrack(
                current,
                body,
                next_entry,
                intro,
                "",
                start_offset=start_offset,
                glide_in=glide_in,
                next_entry_offset=intro,
                loudness_processing=player._cfg.playback.loudness_processing or "",
                full_audio=body,
                playback_rate=rate_a,
            )

        fx_plan = player._fx_plan(current, next_entry, meta_a, start_offset, int(match.semitones))
        from autodj.player import _effect_mode

        effect_name = _effect_mode(fx_plan.name).value
        effect_samples = max(
            crossfade,
            int(player._MIN_FX_DURATION_S.get(effect_name, 0.0) * SAMPLE_RATE),
        )
        effect_samples = min(effect_samples, max(0, fade_start - start_offset) + crossfade)
        fade_end = fade_start + crossfade
        window_start = max(start_offset, fade_end - effect_samples)
        resample_input_step = Fraction(1.0 / rate_a).limit_denominator(10_000).denominator
        context_start = max(0, window_start - start_offset - _RESAMPLE_CONTEXT_FRAMES)
        mix_start = start_offset + (context_start // resample_input_step) * resample_input_step
        outgoing_window = current_audio[mix_start:fade_end]
        incoming_head_frames = max(crossfade, effect_samples)
        next_glide = player._incoming_glide(incoming_audio, intro, match, crossfade)
        if next_glide is not None:
            incoming_head_frames = max(
                incoming_head_frames,
                int(max(player._MIN_FX_DURATION_S.values()) * SAMPLE_RATE),
            )
            incoming_need = min(
                len(incoming_audio), max(intro + incoming_head_frames, next_glide.source_end + 4096)
            )
            incoming_loaded = incoming_audio[:incoming_need]
            incoming_head = next_glide.head(incoming_loaded, incoming_head_frames)
        else:
            incoming_head = incoming_audio[intro : intro + incoming_head_frames]
        a_tail = player._apply_outgoing_filter_sweep(outgoing_window, SAMPLE_RATE, crossfade)
        fx_plan = replace(fx_plan, origin_s=mix_start / SAMPLE_RATE)
        a_tail, b_head, extra, effect = player._apply_transition_effect(
            a_tail, incoming_head, incoming_head[:crossfade], SAMPLE_RATE, crossfade, plan=fx_plan
        )
        mixed = player._mix_overlap(a_tail, b_head, crossfade, SAMPLE_RATE, extra)
        mix_out_start = _music_output_frame(mix_start - start_offset, rate_a)
        window_out_start = _music_output_frame(window_start - start_offset, rate_a)
        output_end = _music_output_frame(fade_end - start_offset, rate_a)
        mixed_rate = _music_segment(mixed, rate_a, max(0, output_end - mix_out_start))
        tail = mixed_rate[max(0, window_out_start - mix_out_start) :]
        layers = []
        prefix = max(0, window_out_start)
        if active_glide is not None and glide_audio is not None:
            glide_end = min(fade_end, active_glide.source_end)
            glide_out = min(prefix, _music_output_frame(glide_end - start_offset, rate_a))
            if glide_out:
                layers.append(
                    AudioLayer(_music_segment(glide_audio, rate_a, glide_out), 0, glide_out)
                )
            source_first = max(start_offset, active_glide.source_end)
            body_out = max(0, _music_output_frame(source_first - start_offset, rate_a))
        else:
            body_out = 0
        if body_out < prefix:
            layers.append(
                AudioLayer(
                    source,
                    body_out,
                    prefix - body_out,
                    _music_output_frame(start_offset, rate_a) + body_out,
                )
            )
        if len(tail):
            layers.append(AudioLayer(tail, prefix, len(tail)))
        full = LiveAudio(output_end, tuple(layers))
        next_offset = next_glide.origin if next_glide is not None else intro + crossfade
        return RenderedTrack(
            current,
            full,
            next_entry,
            next_offset,
            effect,
            next_glide.ratio if next_glide is not None else 1.0,
            start_offset=start_offset,
            next_glide=next_glide,
            glide_in=glide_in,
            overlap_frames=round(crossfade / rate_a),
            next_entry_offset=intro,
            loudness_processing=player._cfg.playback.loudness_processing or "",
            full_audio=full,
            playback_rate=rate_a,
            next_playback_rate=rate_b,
            transition_start_frame=prefix,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        logger.error("Cannot prepare live transition for %s: %s", current.path, exc)
        return None
