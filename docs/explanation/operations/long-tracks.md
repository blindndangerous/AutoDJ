# How long tracks use memory and disk

Understand the analysis windows, advanced-analysis cutoff and playback spill files for long recordings.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Long tracks

Tracks of any length are imported and play, including hour-long DJ mixes and full-album files.
Memory stays bounded however long a track is:

- **Basic analysis.** A track longer than 15 minutes is decoded once as it streams. AutoDJ keeps
  its first and last 5 minutes and measures its length, energy, and first and last audible frames
  over the whole file. Intro comes from the first window; Segue and Outro come from the last.
  Start and End use whole-track audible positions when their windows contain only silence.
- **Beats and song structure.** A long track's beat grid and cues come from the same first and
  last 5 minutes, the parts a transition uses. The grid has a gap in the middle.
- **Advanced analysis.** By default, songs longer than 15 minutes are left out of the advanced
  analysis, which decodes the whole song and can need several gigabytes for a long mix. Set the
  cutoff in Library tools under **Advanced analysis length**; 0 analyses every length. Songs left
  out are not errors and are not retried. The import summary counts them, and they are analysed
  as soon as the cutoff allows. They still play in rotation, in clock categories and as queue or
  request picks. Similarity picks need the advanced analysis, so a song without it is never
  chosen as "similar" to another. When it plays in similarity mode, the next pick falls back to a
  random playable song.
- **Server playback.** A decoded track longer than 10 minutes, about 21 MB per minute, is kept
  in a temporary file mapped into memory instead of in ordinary memory. Spill files use
  `<index_dir>/cache/spill/`, created when needed; startup clears files left by a crash.
  AutoDJ uses the system temporary folder (`TMPDIR` or `TEMP`) only when that directory is not
  writable. A three-hour mix needs about 4 GB of free disk there while it plays. Files are
  removed when the track is done. During a transition, peak disk use can reach about three times
  the decoded track size per station: the decode, resampled output at music speed other than 1,
  crossfade join, handoff chunk, and prefetched next track can coexist. FFmpeg may take as long
  as the track to decode it, or at least 10 minutes.

The old `server_max_track_minutes` playback setting is gone. AutoDJ removes it from saved
station settings when it starts.
