# AutoDJ — AI-powered music continuity player

## Project Goal

Build a local auto-DJ system that:
1. Analyzes a music library and indexes every song
2. Given a playing (or chosen) song, picks the next most sonically similar song
3. Continues indefinitely, creating a natural-feeling listening flow

No cloud services. No Spotify API. Runs fully offline on the user's local music files.

## Chosen Approach: Pre-trained Embeddings + FAISS

### Why this approach
- No model training required — use pre-trained CLAP weights
- Rich multi-dimensional similarity (mood, timbre, energy, key, BPM all factored in)
- Fast at query time even for large libraries (FAISS nearest-neighbor search)

### Core libraries
| Library | Purpose |
|---|---|
| `laion_clap` | Pre-trained audio embeddings (512-dim vectors capturing musical "feel") |
| `librosa` | Traditional audio feature extraction (BPM, key, loudness, chroma, etc.) |
| `faiss-cpu` | Fast nearest-neighbor vector search across the library index |
| `numpy` | Vector math and storage |
| `soundfile` / `sounddevice` | Audio playback |

### CLAP model to use
- Repo: https://github.com/LAION-AI/CLAP
- Checkpoint: `music_audioset_epoch_15_esc_90.14.pt` (music-specific, best for this use case)
- Why LAION over Microsoft CLAP: music-specific training data, larger dataset, better community adoption for music tasks

## Architecture Plan

```
library/
    song.mp3 --> [CLAP] --> 512-dim embedding
                [librosa] --> BPM, key, loudness, chroma, spectral centroid, etc.
                         --> combined into one rich vector

index/
    vectors.index   (FAISS index)
    metadata.json   (song paths + extracted features)

autodj.py           (main: index builder + playback loop)
```

### Pipeline steps
1. **Index build** (one-time, slow): walk library, extract CLAP embedding + librosa features per song, combine into single vector, store in FAISS
2. **Query** (instant): embed the current song, query FAISS for top-N neighbors, exclude recently played, pick next
3. **Playback loop**: play song -> on finish (or hotkey) -> query -> play next

## Rich Feature Vector Design

Each song gets a vector combining:
- CLAP 512-dim embedding (captures overall sonic character)
- BPM (normalized)
- Key (one-hot or circular encoding)
- Mode (major/minor)
- Loudness (RMS)
- Spectral centroid
- Zero crossing rate
- Chroma mean features (12-dim)
- Onset strength

These can be weighted differently — e.g., weight CLAP heavily for mood matching, add BPM weight if smooth transitions are desired.

## User's Setup
- OS: Windows 11
- Music library format: TBD (MP3/FLAC/WAV)
- GPU: Unknown — CLAP will fall back to CPU if no CUDA GPU available (slower index build, same query speed)
- Python version target: 3.10+

## Status
- [ ] Project scaffolded
- [ ] Dependencies installed
- [ ] CLAP model downloaded
- [ ] Index builder written
- [ ] Query function written
- [ ] Playback loop written
- [ ] Basic UI or CLI interface

## Next Steps (start here in a new session)
1. Scaffold `pyproject.toml` or `requirements.txt` with all dependencies
2. Write `index_library.py` — walks a folder, extracts features, builds FAISS index
3. Write `autodj.py` — loads index, plays song, queries next, loops
4. Test on a small subset of the library first (10-20 songs)
