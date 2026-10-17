# Measure multi-station resource use

Estimate the active station workload, then run the documented soak test on the target host and inspect its report.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Cost of each running station

Each running station is a complete playout chain in the same process:

- **Threads, about 10 or more:** the player, the render-ahead worker (decoding, time-stretching
  and effects, in heavy bursts at each song change), the mix bus, the mastering process's reader
  and writer, the encoder's reader and writer, the recorder, the liner worker, one per webhook
  and two per relay.
- **Subprocesses, 2 to 4:** the ffmpeg MP3 encoder (plus one per relay bitrate that differs) and
  either the ffmpeg master filter (`smooth`) or Stereo Tool with an ffmpeg limiter.
- **Memory:** about 21 MB per minute of decoded audio, so roughly 150 to 250 MB per station for
  the playing song, the next render and the overlap. Audio longer than 10 minutes goes to a
  temporary file instead (see [Long tracks](../../explanation/operations/long-tracks.md#long-tracks)). The similarity index is shared, so another
  station adds no index memory.
- **An idle station still runs** its mix bus, mastering and encoder on silence. Stopping a
  station is the real saving.
- **One process, one interpreter lock.** A heavy render on one station could make another
  station's mix bus late. The soak test below measures it.

## Soak test

`tests/integration/test_multi_station.py` has a soak test with two stations, the default one and
Night Owl Radio. Both mix buses run while two threads do song-sized time-stretches back to back,
standing in for each station's render-ahead work, and Night Owl is stopped and started again
every cycle. The test counts the 20 ms blocks each bus sends out. It fails if a stop leaves any
of the station's threads, encoders or mastering process behind, and, in a long run, if either bus
falls short (an underrun). ffmpeg and Stereo Tool are replaced by stand-ins, so it measures AutoDJ's own
work.

Every test run, including CI, runs it for 4 seconds with one restart. To run it for longer, on the
machine that will host the stations, set `AUTODJ_SOAK_SECONDS`. This runs it for four hours,
restarts Night Owl every 10 minutes, and samples memory and processor use every 30 seconds:

```bash
AUTODJ_SOAK_SECONDS=14400 AUTODJ_SOAK_REPORT=soak.jsonl \
  uv run pytest tests/integration/test_multi_station.py -k soak -s -p no:cacheprovider
```

The settings, all optional:

- `AUTODJ_SOAK_SECONDS`: how long to run. A long run uses four-minute songs for the render
  stand-ins and fails if a bus sends fewer than 99% of the blocks it should. The short default
  run uses 20-second songs and only reports the block counts: a few seconds on a shared CI
  machine is too short to judge underruns, so there it checks the restart and leaks only.
- `AUTODJ_SOAK_CYCLE_SECONDS`: how often Night Owl is stopped and started (default 600 in a long
  run).
- `AUTODJ_SOAK_SAMPLE_SECONDS`: how often memory and processor use are sampled (default 30 in a
  long run).
- `AUTODJ_SOAK_REPORT`: a file the samples are added to, one JSON object per line. Each has the
  seconds since the start, `rss_mb` (resident memory, on Linux only), `cpu_percent` (processor
  time as a percentage of one core since the last sample), `threads` and `restarts`. The last
  line is the summary.

At the end the test prints a summary line: the blocks each bus sent against the number expected,
the longest gap between blocks in milliseconds, the number of restarts and renders, memory at the
start and the end, and Night Owl's thread count before and after. Memory that keeps climbing from
one restart to the next, or a thread count that grows, points to a leak; attach the report file to
an issue.
