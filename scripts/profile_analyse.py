"""Profile DJ-meta analyse phase per detector.

Picks N un-analysed tracks (or random tracks if all analysed) from the
active index, times each stage of analyse_audio in isolation, and prints
a per-stage / per-track breakdown plus aggregate medians.

Stages timed:
    1. _load_audio       (decode + NAS read)
    2. detect_intro_outro (numpy block-RMS)
    3. detect_beat_grid  (librosa.beat.beat_track — suspected hot path)
    4. detect_cues       (RMS + beat-derived cues)

Usage:
    uv run python scripts/profile_analyse.py [--name NAME] [--n 10]
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import numpy as np

from autodj.config import load_config
from autodj.dj_meta import (
    analyse_audio,
    detect_beat_grid,
    detect_cues,
    detect_intro_outro,
    get_cache,
)
from autodj.indexer import _load_audio, load_index


def _time(fn, *args, **kwargs):
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, time.perf_counter() - t0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default=None)
    parser.add_argument("--n", type=int, default=10)
    args = parser.parse_args()

    cfg = load_config()
    if args.name:
        cfg.index.name = args.name
    music_dir = Path(cfg.library.music_dir).expanduser() if cfg.library.music_dir else None
    index_dir = cfg.index.active_dir
    entries, _ = load_index(index_dir, music_dir=music_dir, path_remap=cfg.library.path_remap)
    cache = get_cache(index_dir)
    pending = [e for e in entries if cache is None or not cache.get(e.path).analysed]
    pool = pending if pending else entries
    rng = np.random.default_rng(0)
    sample = [pool[i] for i in rng.choice(len(pool), size=min(args.n, len(pool)), replace=False)]

    rows = []
    for i, entry in enumerate(sample, 1):
        path = Path(entry.path)
        if not path.exists():
            continue
        try:
            (audio, sr), t_load = _time(_load_audio, path)
            if len(audio) == 0:
                continue
            (intro_outro, t_io) = _time(detect_intro_outro, audio, sr)
            intro_end, outro_start = intro_outro
            beats, t_beat = _time(detect_beat_grid, audio, sr)
            _cues, t_cues = _time(detect_cues, audio, sr, intro_end, outro_start, beats)
            total = t_load + t_io + t_beat + t_cues
            rows.append((path.name, len(audio) / sr, t_load, t_io, t_beat, t_cues, total))
            print(
                f"[{i:>3}] {path.name[:55]:<55}  "
                f"dur={len(audio) / sr:5.1f}s  "
                f"load={t_load:5.2f}  io={t_io:5.2f}  "
                f"beat={t_beat:5.2f}  cues={t_cues:5.2f}  "
                f"TOTAL={total:5.2f}",
                flush=True,
            )
        except Exception as exc:
            print(f"[{i:>3}] {path.name}: FAILED {type(exc).__name__}: {exc}")

    if not rows:
        print("No tracks profiled.")
        return 1

    cols = list(zip(*rows, strict=True))
    names = ["dur", "load", "intro_outro", "beat_grid", "cues", "TOTAL"]
    print("\n=== medians (s) ===")
    for name, col in zip(names, cols[1:], strict=True):
        med = statistics.median(col)
        mx = max(col)
        print(f"  {name:<12} median={med:6.2f}  max={mx:6.2f}")
    total_med = statistics.median(cols[-1])
    beat_med = statistics.median(cols[4])
    print(f"\nbeat_grid share of TOTAL (median): {100 * beat_med / total_med:.1f}%")
    print("\nSanity check — analyse_audio end-to-end on first track:")
    audio, sr = _load_audio(Path(sample[0].path))
    (_meta, t_full) = _time(analyse_audio, audio, sr)
    print(f"  analyse_audio: {t_full:.2f}s  (sum of stages was {sum(rows[0][2:6]):.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
