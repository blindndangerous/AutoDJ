"""Deterministic stereo stimulus and offline correlation measurements.

This measures decoded PCM, not acoustic latency. Windows are matched independently
so an inserted gap cannot hide a dropout behind a single whole-file correlation.
"""

import argparse
import json

import numpy as np
from scipy.signal import correlate, firwin, lfilter


def signal(rate: int, seconds: float) -> np.ndarray:
    """Return distinct, reproducible, band-limited stereo codes with headroom."""
    noise = np.random.default_rng(541).normal(size=(int(rate * seconds), 2))
    taps = firwin(65, min(700, rate / 8), fs=rate)
    return (lfilter(taps, [1], noise, axis=0) * 0.4).astype(np.float32)


def measure(reference: np.ndarray, received: np.ndarray, rate: int) -> dict:
    """Estimate offset, clock drift, channel order and missing 250 ms windows."""
    reference = np.asarray(reference, dtype=np.float64)
    received = np.asarray(received, dtype=np.float64)
    if rate <= 0 or any(a.ndim != 2 or a.shape[1] != 2 for a in (reference, received)):
        raise ValueError("positive sample rate and stereo arrays required")
    width = rate // 4
    if min(len(reference), len(received)) < width or not np.isfinite(received).all():
        raise ValueError("finite PCM of at least 250 ms required")
    cumulative = np.concatenate(([0.0], np.cumsum(np.sum(received**2, axis=1))))
    energy = np.sqrt(
        np.maximum(
            cumulative[width:] - cumulative[:-width],
            1e-12,
        )
    )
    points, scores, swaps = [], [], []
    for start in range(0, len(reference) - width + 1, width):
        part = reference[start : start + width]
        norm = max(float(np.linalg.norm(part)), 1e-10)
        choices = []
        for swapped in (False, True):
            channels = (1, 0) if swapped else (0, 1)
            corr = sum(
                correlate(received[:, channels[c]], part[:, c], mode="valid", method="fft")
                for c in range(2)
            ) / (energy * norm)
            position = int(np.argmax(corr))
            choices.append((float(corr[position]), position, swapped))
        score, position, swapped = max(choices)
        scores.append(score)
        if score >= 0.65:
            points.append((start, position - start))
            swaps.append(swapped)
    if len(points) < 2:
        raise ValueError("insufficient matching windows")
    slope, offset = np.polyfit(*np.array(points).T, 1)
    channel_swap = bool(sum(swaps) > len(swaps) / 2)
    ordered = received[:, ::-1] if channel_swap else received
    # Long-window correlation estimates alignment, but cannot reveal one missing
    # 10 ms packet. Compare aligned per-channel energy on the packet timescale.
    # This detects sustained silence, not every kind of distortion or click.
    short = max(1, rate // 100)
    ratios = []
    for start in range(0, len(reference) - short + 1, short):
        position = round(start * (1 + slope) + offset)
        expected = np.sum(reference[start : start + short] ** 2, axis=0)
        if np.min(expected) < 1e-8:
            continue
        observed = (
            np.sum(ordered[position : position + short] ** 2, axis=0)
            if 0 <= position <= len(ordered) - short
            else np.zeros(2)
        )
        ratios.append(observed / expected)
    ratios = np.array(ratios)
    matched_energy = []
    for start, lag in points:
        position = start + lag
        expected = np.sum(reference[start : start + width] ** 2, axis=0)
        observed = np.sum(ordered[position : position + width] ** 2, axis=0)
        matched_energy.append(observed / np.maximum(expected, 1e-12))
    # Estimate gain only from correlated windows. Long outages must not lower
    # the reference level to zero and make their own silence appear normal.
    typical = np.median(matched_energy, axis=0)
    dropouts = int(np.sum(np.any(ratios < typical * 0.1, axis=1)))
    return {
        "delay_ms": float(offset * 1000 / rate),
        "drift_ppm": float(slope * 1e6),
        "dropouts": dropouts,
        "dropout_window_ms": 10,
        "unmatched_windows": len(scores) - len(points),
        "window_ms": 250,
        "channel_swap": channel_swap,
        "minimum_correlation": min(scores),
        "matched_windows": len(points),
    }


def measure_files(reference_path, received_path) -> dict:
    """Analyse an endurance recording in bounded chunks without skipping gaps.

    The initial ten-second search anchors the first occurrence. Later searches
    stay within 200 ms of the previous lag, avoiding later repeated songs.
    Global drift is fitted from absolute offsets, not chunk-local residuals.
    """
    import soundfile as sf

    with sf.SoundFile(reference_path) as reference, sf.SoundFile(received_path) as received:
        rate = reference.samplerate
        if received.samplerate != rate or reference.channels != 2 or received.channels != 2:
            raise ValueError("matching stereo sample rates required")
        start = int(rate * 0.7)
        stop = len(reference) - int(rate * 0.2)
        reference.seek(start)
        anchor = measure(reference.read(rate * 2), received.read(rate * 10), rate)
        lag = anchor["delay_ms"] * rate / 1000 - start
        points, chunks = [], []
        for position in range(start, stop, rate * 2):
            length = min(rate * 2, stop - position)
            if length < rate // 2:
                break
            reference.seek(position)
            search = max(0, round(position + lag) - rate // 5)
            received.seek(search)
            result = measure(reference.read(length), received.read(length + rate * 2 // 5), rate)
            lag = search + result["delay_ms"] * rate / 1000 - position
            midpoint = position + length / 2
            points.append((midpoint, lag + length / 2 * result["drift_ppm"] / 1e6))
            chunks.append({"start_frame": position, "frames": length, **result})
        if len(points) < 2:
            raise ValueError("at least two endurance chunks required")
        slope, offset = np.polyfit(*np.array(points).T, 1)
        return {
            "analysed_seconds": sum(c["frames"] for c in chunks) / rate,
            "delay_ms": float(offset * 1000 / rate),
            "drift_ppm": float(slope * 1e6),
            "dropouts": sum(c["dropouts"] for c in chunks),
            "dropout_window_ms": 10,
            "unmatched_windows": sum(c["unmatched_windows"] for c in chunks),
            "matched_windows": sum(c["matched_windows"] for c in chunks),
            "minimum_correlation": min(c["minimum_correlation"] for c in chunks),
            "channel_swap": any(c["channel_swap"] for c in chunks),
            "chunks": chunks,
        }


if __name__ == "__main__":
    import soundfile as sf

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    parser.add_argument("decoded")
    parser.add_argument("--endurance", action="store_true")
    args = parser.parse_args()
    if args.endurance:
        print(json.dumps(measure_files(args.reference, args.decoded), indent=2))
    else:
        reference, rate = sf.read(args.reference, always_2d=True)
        decoded, decoded_rate = sf.read(args.decoded, always_2d=True)
        if rate != decoded_rate:
            parser.error("decode both files at the same rate first")
        print(json.dumps(measure(reference, decoded, rate), indent=2))
