"""Editable audio timelines read in small blocks by the station mix bus."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

import numpy as np

from autodj.station_log import station_thread
from autodj.stereo import SAMPLE_RATE
from autodj.stream_decode import StreamDecoder

if TYPE_CHECKING:
    from autodj.playout import Handoff


def _shift_gain(
    gain: Callable[[np.ndarray], np.ndarray], offset: int, at: np.ndarray
) -> np.ndarray:
    return gain(at + offset)


class AudioPending(Exception):
    """A decoder is refilling; hold the programme clock for this bus block."""


class BufferedFile:
    """A file deck with asynchronous seek and a two-second decoding cushion.

    Only the control worker opens or closes a decoder. The bus checks all
    participating decks before consuming any samples from an overlap.
    """

    def __init__(
        self,
        path: str,
        frames: int,
        *,
        rate: float = 1,
        filters: str = "",
        start: int = 0,
        lift_db: float = 0,
    ) -> None:
        if not math.isfinite(lift_db) or lift_db < 0:
            raise ValueError("lift_db must be finite and non-negative")
        self.path, self.frames, self.rate, self.filters = path, frames, rate, filters
        self.lift_db = float(lift_db)
        self._condition = threading.Condition()
        self._decoder: StreamDecoder | None = None
        self._position = start
        self._requested: int | None = start
        self._stopped = False
        self._error: str | None = None
        self._worker = station_thread(target=self._run, name="autodj-file-deck", daemon=True)
        self._worker.start()

    def __len__(self) -> int:
        return self.frames

    def ready(self, position: int, frames: int) -> bool:
        with self._condition:
            if self._stopped:
                raise RuntimeError(self._error or f"File deck is closed: {self.path}")
            if self._error is not None:
                raise RuntimeError(self._error)
            if position != self._position:
                self._requested = position
                self._condition.notify_all()
                return False
            decoder = self._decoder
            if decoder is not None and decoder.error is not None:
                raise RuntimeError(decoder.error)
            return decoder is not None and (
                decoder.buffered_frames >= frames or decoder.eof or decoder.error is not None
            )

    def read(self, position: int, frames: int) -> np.ndarray:
        with self._condition:
            if not self.ready(position, frames):
                raise AudioPending
            decoder = self._decoder
            output = np.zeros((frames, 2), np.float32)
            if decoder is not None:
                if decoder.error:
                    raise RuntimeError(decoder.error)
                piece = decoder.read(frames)
                output[: len(piece)] = piece
                self._apply_lift(output[: len(piece)], position)
            self._position += frames
            return output

    def _apply_lift(self, samples: np.ndarray, position: int) -> None:
        if not self.lift_db or not len(samples):
            return
        from autodj.intro_lift import CEILING, HOLD_SECONDS, INTRO_SECONDS

        source_seconds = (position + np.arange(len(samples))) * self.rate / SAMPLE_RATE
        weights = np.clip(
            (INTRO_SECONDS - source_seconds) / (INTRO_SECONDS - HOLD_SECONDS), 0.0, 1.0
        )
        peaks = np.max(np.abs(samples), axis=1)
        limits = np.full(len(samples), np.inf, np.float64)
        nonzero = peaks > 0
        limits[nonzero] = np.maximum(0.0, 20 * np.log10(CEILING / peaks[nonzero]))
        gain_db = np.minimum(self.lift_db * weights, limits)
        samples *= (10 ** (gain_db / 20)).astype(np.float32)[:, None]

    def snapshot(self, position: int, frames: int) -> np.ndarray:
        """Read an independent, bounded decoder window without moving this deck."""
        if position < 0 or frames < 0:
            raise ValueError("snapshot position and frames must be non-negative")
        if position >= self.frames or frames == 0:
            return np.zeros((0, 2), np.float32)
        end = min(self.frames, position + frames)
        deck = BufferedFile(
            self.path,
            self.frames,
            rate=self.rate,
            filters=self.filters,
            start=position,
            lift_db=self.lift_db,
        )
        chunks = []
        cursor = position
        deadline = time.monotonic() + 30.0
        try:
            while cursor < end:
                count = min(round(0.25 * SAMPLE_RATE), end - cursor)
                if not deck.ready(cursor, count):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out decoding snapshot for {self.path}")
                    time.sleep(0.005)
                    continue
                with deck._condition:
                    decoder = deck._decoder
                if decoder is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out opening snapshot for {self.path}")
                    time.sleep(0.005)
                    continue
                if decoder.error:
                    raise RuntimeError(decoder.error)
                available = min(count, decoder.buffered_frames)
                if available:
                    piece = deck.read(cursor, available)
                    chunks.append(piece)
                    cursor += len(piece)
                elif decoder.eof:
                    break
                elif time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out decoding snapshot for {self.path}")
                else:
                    time.sleep(0.005)
        finally:
            deck.close()
        if not chunks:
            return np.zeros((0, 2), np.float32)
        return np.concatenate(chunks)

    def _run(self) -> None:
        decoder = None
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._stopped or self._requested is not None)
                    if self._stopped:
                        break
                    position, self._requested = self._requested, None
                    assert position is not None
                    previous, self._decoder = self._decoder, None
                    self._position = position
                if previous is not None:
                    previous.close()
                assert position is not None
                decoder = StreamDecoder(
                    self.path,
                    start_seconds=position * self.rate / SAMPLE_RATE,
                    filters=self.filters or None,
                    playback_rate=self.rate,
                )
                with self._condition:
                    if self._stopped:
                        break
                    self._decoder, self._position = decoder, position
                    decoder = None  # ownership transferred to self._decoder
        except Exception as exc:
            with self._condition:
                if not self._stopped:
                    self._error = f"Could not prepare audio deck {self.path}: {exc}"
                self._condition.notify_all()
        finally:
            with self._condition:
                current, self._decoder = self._decoder, None
            for item in (decoder, current):
                if item is not None:
                    try:
                        item.close()
                    except Exception as exc:
                        with self._condition:
                            if self._error is None:
                                self._error = f"Could not close audio deck {self.path}: {exc}"
                            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._worker.join(5)
        if self._worker.is_alive():
            raise RuntimeError(f"File deck did not stop: {self.path}")
        with self._condition:
            if self._error is not None:
                raise RuntimeError(self._error)


@dataclass(frozen=True)
class AudioLayer:
    source: BufferedFile | np.ndarray
    start: int
    length: int
    offset: int = 0
    gain: Callable[[np.ndarray], np.ndarray] | None = None


class LiveAudio:
    """A sample-timed mix plan; storing it neither mixes nor consumes audio."""

    def __init__(self, frames: int, layers: tuple[AudioLayer, ...]) -> None:
        self.frames, self.layers = frames, layers

    def __len__(self) -> int:
        return self.frames

    def __getitem__(self, key: slice) -> LiveAudio:
        first, last, step = key.indices(self.frames)
        if step != 1:
            raise ValueError("audio timeline slices require unit steps")
        layers = []
        for layer in self.layers:
            begin, end = max(first, layer.start), min(last, layer.start + layer.length)
            if begin < end:
                gain = layer.gain
                layers.append(
                    replace(
                        layer,
                        start=begin - first,
                        length=end - begin,
                        offset=layer.offset + begin - layer.start,
                        gain=partial(_shift_gain, gain, first) if gain is not None else None,
                    )
                )
        return LiveAudio(max(0, last - first), tuple(layers))

    def _active(self, first: int, frames: int) -> list:
        active = []
        for layer in self.layers:
            begin, end = max(first, layer.start), min(first + frames, layer.start + layer.length)
            if begin >= end:
                continue
            offset = layer.offset + begin - layer.start
            if isinstance(layer.source, BufferedFile) and not layer.source.ready(
                offset, end - begin
            ):
                raise AudioPending
            active.append((layer, begin, end, offset))
        return active

    def ready(self, first: int, frames: int) -> bool:
        try:
            self._active(first, frames)
        except AudioPending:
            return False
        return True

    def read(self, first: int, frames: int) -> np.ndarray:
        active = self._active(first, frames)
        output = np.zeros((frames, 2), np.float32)
        for layer, begin, end, offset in active:
            piece = (
                layer.source.read(offset, end - begin)
                if isinstance(layer.source, BufferedFile)
                else layer.source[offset : offset + end - begin]
            )
            if layer.gain is not None:
                piece = piece * layer.gain(np.arange(begin, end))[:, None]
            output[begin - first : end - first] += piece
        return output


def read_audio(audio: np.ndarray | LiveAudio, first: int, last: int) -> np.ndarray:
    """Read a bus block from an offline render or a live mix plan."""
    return audio.read(first, last - first) if isinstance(audio, LiveAudio) else audio[first:last]


def splice_audio(previous: LiveAudio, replacement: LiveAudio, boundary: int) -> LiveAudio:
    """Retain the old prefix without copying or consuming its decoded samples."""
    prefix, suffix = previous[:boundary], replacement[boundary:]
    layers = list(prefix.layers)
    for layer in suffix.layers:
        gain = layer.gain
        layers.append(
            replace(
                layer,
                start=layer.start + boundary,
                gain=partial(_shift_gain, gain, -boundary) if gain is not None else None,
            )
        )
    return LiveAudio(len(replacement), tuple(layers))


def replace_live_incoming(
    audio: LiveAudio,
    incoming_boundary: int,
    old_source: BufferedFile | None,
    new_source: BufferedFile | None,
    new_offset: int,
    max_frames: int,
) -> tuple[LiveAudio, int]:
    """Retarget only the incoming deck in a saved-point handoff timeline.

    Outgoing music, voice and liner arrays, duck/fade functions, and their
    positions stay attached to the original timeline. The incoming deck keeps
    the original layer start and gain function while reading from *new_source*.
    Returns the number of new incoming frames available in this timeline.
    """
    if min(incoming_boundary, new_offset, max_frames) < 0:
        raise ValueError("incoming boundary, offset and length must be non-negative")
    if incoming_boundary > audio.frames:
        raise ValueError("incoming boundary is outside the audio timeline")
    frames = (
        min(max_frames, audio.frames - incoming_boundary, len(new_source) - new_offset)
        if new_source is not None and new_offset < len(new_source)
        else 0
    )
    layers: list[AudioLayer] = []
    replaced = False
    for layer in audio.layers:
        matches = (
            isinstance(layer.source, BufferedFile)
            and layer.start >= incoming_boundary
            and (layer.source is old_source if old_source is not None else True)
        )
        if not matches:
            layers.append(layer)
            continue
        if not replaced:
            replaced = True
            if new_source is not None and frames:
                length = min(frames, audio.frames - layer.start)
                if length:
                    layers.append(
                        replace(layer, source=new_source, length=length, offset=new_offset)
                    )
                    frames = length
        # Drop any duplicate old incoming layers so they cannot air after it.
    if not replaced and new_source is not None and frames:
        layers.append(AudioLayer(new_source, incoming_boundary, frames, new_offset))
    return LiveAudio(audio.frames, tuple(layers)), frames


def mix_handoff(
    outgoing: BufferedFile | np.ndarray,
    outgoing_points: dict,
    incoming_points: dict,
    *,
    incoming: BufferedFile | np.ndarray,
    liner: np.ndarray | None = None,
    **options: Any,
) -> Handoff[LiveAudio]:
    """Use the offline renderer's cue plan, mixing its sources only as they air."""
    from autodj.playout import DUCK_RAMP_SECONDS, Handoff, plan_handoff

    plan = plan_handoff(
        len(outgoing),
        outgoing_points,
        incoming_points,
        liner_frames=0 if liner is None else len(liner),
        **options,
    )
    first, end = plan.chunk_start, plan.overlay_end
    split = plan.incoming_frame - first
    ramp = round(DUCK_RAMP_SECONDS * SAMPLE_RATE)
    ducks = []
    if plan.liner_start is not None:
        for spans_name, depth_name in (
            ("duck_spans", "duck_db"),
            ("liner_duck_spans", "liner_duck_db"),
        ):
            depth = float(options.get(depth_name, 0))
            if depth < 0:
                for start, stop in options.get(spans_name, ()):
                    ducks.append(
                        (
                            plan.liner_start + round(start * SAMPLE_RATE),
                            plan.liner_start + round(stop * SAMPLE_RATE),
                            10 ** (depth / 20),
                        )
                    )

    def duck(at: np.ndarray) -> np.ndarray:
        gain = np.ones(len(at), np.float32)
        absolute = at + first
        for start, stop, depth in ducks:
            weight = np.clip(
                np.minimum(
                    (absolute - start + ramp + 1) / ramp,
                    (stop + ramp - absolute) / ramp,
                ),
                0,
                1,
            )
            gain = np.minimum(gain, 1 + (depth - 1) * weight)
        return gain

    def outgoing_gain(at: np.ndarray) -> np.ndarray:
        count = plan.fade_end - plan.fade_start
        fraction = np.clip((at + first - plan.fade_start) / max(1, count - 1), 0, 1)
        gain = 1 - np.expm1(3 * fraction) / np.expm1(3)
        if count <= 1:
            gain = np.where(at + first < plan.fade_start, 1, 0)
        return gain * duck(at)

    layers = [
        AudioLayer(outgoing, 0, max(0, min(end, plan.end_frame) - first), first, outgoing_gain)
    ]
    if liner is not None and plan.liner_start is not None:
        begin = max(first, plan.liner_start)
        stop = min(end, plan.liner_start + len(liner))
        if stop > begin:
            layers.append(AudioLayer(liner, begin - first, stop - begin, begin - plan.liner_start))
    incoming_end = min(
        len(incoming),
        round((incoming_points.get("end") or len(incoming) / SAMPLE_RATE) * SAMPLE_RATE),
    )
    incoming_frames = min(end - plan.incoming_frame, incoming_end - plan.next_start_offset)
    if incoming_frames > 0:
        layers.append(AudioLayer(incoming, split, incoming_frames, plan.next_start_offset, duck))
    full = LiveAudio(end - first, tuple(layers))
    return Handoff(
        audio=full[:split],
        next_overlay=full[split:],
        full_audio=full,
        next_start_offset=plan.next_start_offset,
        start_offset=first,
        segue_s=plan.segue_frame / SAMPLE_RATE,
        incoming_s=plan.incoming_frame / SAMPLE_RATE,
        liner_s=None if plan.liner_start is None else plan.liner_start / SAMPLE_RATE,
        fade_start_s=plan.fade_start / SAMPLE_RATE,
    )
