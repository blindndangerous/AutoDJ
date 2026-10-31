"""Compare captured browser PCM with the real decoded HTTP broadcast."""

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import correlate, find_peaks

from tests.playwright.local_programme_measure import measure


def private_energy(audio: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(audio[:, 0] * np.hanning(len(audio)))) ** 2
    frequencies = np.fft.rfftfreq(len(audio), 1 / rate)
    return float(np.sum(spectrum[abs(frequencies - 5000) < 10]) / np.sum(spectrum))


def measure_first(reference: np.ndarray, received: np.ndarray, rate: int) -> dict:
    """Constrain correlation to the first occurrence, not a later replay of its song."""
    part = reference[: rate // 4]
    width = len(part)
    cumulative = np.concatenate(([0.0], np.cumsum(np.sum(received**2, axis=1))))
    energy = np.sqrt(np.maximum(cumulative[width:] - cumulative[:-width], 1e-12))
    correlation = sum(
        correlate(received[:, c], part[:, c], mode="valid", method="fft") for c in range(2)
    ) / (energy * np.linalg.norm(part))
    peaks, _ = find_peaks(
        correlation, height=max(0.65, float(correlation.max()) * 0.95), distance=width // 2
    )
    if not len(peaks):
        raise ValueError("No first occurrence marker found")
    start = max(0, int(peaks[0]) - rate // 10)
    stop = int(peaks[0]) + len(reference) + rate // 10
    result = measure(reference, received[start:stop], rate)
    result["delay_ms"] += start / rate * 1000
    return result


def analyse(root: Path) -> dict:
    timing = json.loads((root / "timing.json").read_text())
    audit = json.loads((root / "audit.json").read_text())
    feed, rate = sf.read(root / timing["captures"][0]["file"], always_2d=True)
    decoded, decoded_rate = sf.read(root / "broadcast.wav", always_2d=True)
    bus, bus_rate = sf.read(root / "bus.wav", always_2d=True)
    assert rate == decoded_rate == bus_rate == 44100
    # Exclude deliberately silent preparation and the explicit return envelope.
    start = int(0.7 * rate)
    reference = feed[start : -int(0.2 * rate)]
    result = measure_first(reference, decoded, rate)
    bus_result = measure_first(reference, bus, rate)
    assert not result["channel_swap"] and result["dropouts"] == 0, result
    assert result["unmatched_windows"] == 0, result
    assert abs(result["drift_ppm"]) < 200, result
    delay = []
    clock = audit["monitor"]["audioClock"]
    for sample in range(0, len(reference), rate // 4):
        output = round((bus_result["delay_ms"] / 1000 + sample / rate) * rate)
        block, within = divmod(output, 882)
        if not 0 <= block < len(timing["block_times"]):
            continue
        emitted = timing["block_times"][block] * 1000 + within / rate * 1000
        captured = clock["monotonic_ms"] + (start + sample - clock["frame"]) / rate * 1000
        delay.append(emitted - captured)
    monitor = audit["monitor"]
    preview = feed[monitor["previewFrame"] + 4410 : monitor["muteFrame"] - 4410]
    muted = feed[monitor["muteFrame"] + 4410 : monitor["muteEnd"] - 4410]

    def rms(audio):
        return float(np.sqrt(np.mean(audio**2)))

    assert len(preview) > rate / 2 and len(muted) > rate / 2
    level_ratio = rms(muted) / rms(preview)
    assert 0.8 < level_ratio < 1.2, level_ratio
    private_ratio = private_energy(preview, rate)
    assert private_ratio < 1e-4, private_ratio
    high_water = max(capture["high_water_frames"] for capture in timing["captures"])
    assert high_water <= 8820
    return {
        "decoded_stream": result,
        "browser_to_bus_ms": {
            "p50": float(np.percentile(delay, 50)),
            "p95": float(np.percentile(delay, 95)),
            "min": min(delay),
            "max": max(delay),
            "alignment_uncertainty_ms": clock["uncertainty_ms"],
        },
        "monitor_mute_feed_rms_ratio": level_ratio,
        "private_5000hz_energy_ratio": private_ratio,
        "pcm_high_water_frames": high_water,
        "decoded_offset_note": "Correlation offset includes automation before takeover and codec delay; it is not end-to-end listening latency.",
    }


if __name__ == "__main__":
    print(json.dumps(analyse(Path(sys.argv[1])), indent=2))
