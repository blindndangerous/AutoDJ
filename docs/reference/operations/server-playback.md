# Server playback controls and idle behavior

Look up the effect of playback controls, speed, processing, volume and idle transitions on a server-mixed stream.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Controls while streaming

- **Skip** takes effect immediately on the server with a short fade. A speaker hears it a few
  seconds later, once its own playback buffer catches up. **Skip style** (Settings, Transitions, or
  `[playback] skip_style`) can make it a DJ-style exit instead: Echo out, Backspin or Loop roll.
  Each lasts one to two beats, at most 2 seconds, and ends on a beat when the song's tempo is
  trusted; then the next song starts. Skip style applies to stream mode and server audio only;
  browser playback keeps its own skip.
- **Voice liners** use the configured song-count and time triggers. In saved-point playback,
  automatic liners join the same cue-based handoff as queued liners: their saved Start, Segue,
  End, Overlap start and outro permission determine placement across the outgoing Outro and
  incoming Intro. The talk-up checkbox does not affect this path. Selection is committed only when heard.
  Non-saved-point server playback retains the legacy `[playback] liners_talk_up` timing for
  existing configurations; it uses synced lyrics or the detected intro to avoid the vocal when
  the clip fits. Browser automatic liners keep their existing immediate playback path.
- **Keep vocals apart** (Settings, DJ mixing, or `[djmix] vocal_guard`, on by default) shortens
  or moves a crossfade when both songs have synced lyrics, so the outgoing singer finishes before
  the incoming one starts. The fade never gets longer, or shorter than 1 second; when nothing
  avoids the clash it is left as it was. Songs without synced lyrics crossfade as before.
- **Pause** holds the stream for everyone; it plays silence rather than disconnecting, so
  speakers stay connected and resume from the same spot. It also keeps the set alive past the
  idle timeout below.
- **Seek** is not available while streaming. The seek slider is hidden on the page, and the comma
  and full-stop keyboard shortcuts announce "Seeking is not available while streaming."
- **EQ** is applied once, on the server, so every listener hears the same shaped sound.
- **Music speed** (Settings, Sound) runs music from 10% slower to 10% faster, with 0 as the
  default. Type up to two decimal places; the slider's arrow keys move one percentage point.
  Pitch changes with speed. Liners, voice tracks, matched intros and cue previews keep their
  normal speed. Saved cues and seek positions still refer to the original file; clock fitting,
  forecasts and the DMCA and repeat checks on queued songs and listener requests account for the
  changed playing time.
  Stream and server audio apply changes at the next safe rendered-buffer handoff, keeping the
  current audio and booked speech intact. Browser playback applies a change immediately when
  no transition is running. At nonzero speed, ordinary music/liner crossfades use a plain fade
  so effects cannot change the voice; browser beatmatching also changes pitch. The setting is
  saved per station as `[playback] music_speed_percent` and creates no audio files.
- **Sound processing** is chosen in Settings, Sound. Stereo Tool loads the selected `.sts` preset
  when its processor starts; changing the preset restarts it. The bundled Included preset is
  read-only. Uploads (up to 2 MB) live in `index/shared/stereo-tool-presets`, for every station, and
  survive restarts. Deleting the active upload in Settings switches to Included
  first; a preset file removed by hand is replaced by Included when Stereo Tool next starts. Stereo Tool's
  settings use preset files; AutoDJ applies the drive boost to a temporary copy, leaving your saved preset unchanged.
  An uploaded preset without a single `Drive` line under `[Pre Compressor]` plays unchanged,
  without the boost; Settings, Sound shows "Preset ... has no pre-compressor Drive setting;
  AGC drive not applied" under the preset, and `/api/status` reports it as
  `playout.preset_warning`.
  `[playback] stereotool_input_trim_db` adjusts the input from -12 to +6 dB before the fixed
  output limiter. `[playback] stereotool_agc_drive_db` (0 to 12, default 7) raises the selected
  preset's `[Pre Compressor] Drive` after the tracks are blended; 0 keeps its base value and does
  not disable AGC. Smooth uses its continuous mix compressor and does not level each song's quiet
  passages. Music and liners stay as original files: AutoDJ caches loudness measurements in the
  catalog and applies the measured gain when it decodes a track. Music, catalog liners and
  spoken breaks all use the same -28 LUFS preparation target before the master processor.
  Catalog liners (produced IDs and jingles) retain a 70 Hz high-pass but receive no extra
  compression. Recorded/uploaded breaks and matched intros retain that high-pass and use
  lighter 2:1 voice compression. Both kinds of voice clip cap upward gain at 18 dB.
  These are fixed processing defaults, not additional settings; Stereo Tool AGC drive and
  master limiting are unchanged. A raw voice file imported as a catalog liner follows the
  produced-ID treatment; use a break or matched intro for the voice treatment.
  Old liner loudness measurements are refreshed when scanned or next played; music measurements
  remain valid. Server-mixed playback holds decoded audio in memory while it is mixed.
  Earlier versions wrote processed copies to a `prepared` folder inside the old index
  folder. It is no longer read or written and is safe to delete: its 32-bit float WAVs take
  about 21 MB per minute of audio, often tens of gigabytes for a large library. After
  upgrading, a library scan measures loudness in the background; a song played before it is
  measured is measured as it loads, so that first start takes longer.
  `[playback] intro_lift_max_db` (0 to 6, default 4) caps an extra lift for a quiet opening;
  it fades back to normal by eight seconds. Zero disables it. Analysis caches the source's
  peak-safe opening lift in `library.db`; already cataloged songs get it on their next library scan.
  ReplayGain browser, server audio and stream playback use the cached lift, with decoded server
  audio checked against its peak again. Smooth and Stereo Tool use their continuous processors instead of this intro-only lift.
  Off leaves the source unchanged.
- **Page volume and mute** affect only the page's own "Listen here" playback in the browser.
  Each speaker keeps its own volume, set on the speaker or in its own app.
- **With `--server-audio` as well**, the page's volume and Mute also set the volume and mute of
  the machine's own speakers, and the page's "Listen here" playback follows them. Stream
  listeners and speakers still hear the stream at full level.
- Quality (bitrate) is chosen under Settings, Stream: 128, 192, 256 or 320 kbps, default 320.
- **Station name**, also under Settings, Stream, is the name Sonos, VLC and other players show
  for the station (`[stream] station_name`, default AutoDJ). A player shows a new name the next
  time it connects.
  Changing it restarts the encoder; every listener, including speakers, reconnects on its own a
  moment later.

## Idle sets

The stream starts idle: nothing plays until a listener connects. The first listener to connect
starts a new set from the beginning of a track. While at least one listener stays connected the
set keeps playing; a listener that disconnects and reconnects within 30 seconds
(`[stream] idle_grace_seconds`) finds the same set still going. After 30 seconds with nobody
connected, the set stops; the next listener to connect starts a fresh set from the start of a
track, never mid-song.

The listening address stays the same across restarts. There is no key to copy or renew.

## Mastering startup diagnostics

The log reports the first non-silent input to the master and when processing is ready, measured from processor startup, restart, or a processing-mode change. Readiness includes counts of discarded silent and non-silent input blocks. Silence discarded before any non-silent input arrives is informational; overflow after audio begins, during recovery, or after readiness remains a warning even when the discarded block is silent. Non-silent discarded blocks may contain program audio. Processor output can be nonzero on silent input, so it is not used to identify the song opening. The initial mix bus feeds silence until processing is ready, while track preparation continues in parallel; tracks and liners retain their opening samples. A failed processor releases playback immediately to bounded bypass. If warmup exceeds five seconds, playback uses bounded bypass while the processor continues warming. Recovery during playback does not hold the song. Timings exclude encoder and listener buffering. Compare the timings and counts across restarts before changing buffering.
