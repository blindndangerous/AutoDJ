# Server playback controls and idle behavior

Look up the effect of playback controls, speed, processing, volume and idle transitions on a server-mixed stream.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

Local server sound-card playback and streaming use the same progressive live
mixing engine. FFmpeg is required for both. Choosing only the sound card does
not enable a public stream or listener-driven start/stop. Device volume and
mute remain local to the output; show review/export uses its separate offline
renderer. Local seeking refills the decoder off the audio thread and may briefly
hold playback while the requested audio becomes available.

## Controls while streaming

### Experimental local browser control

Set `local_programme_enabled = true` under `[server]` in the server TOML and
restart to expose **Take local control** and **Return to automation** on a
streaming station. The default is false: existing installations show neither
control and install no local programme routes. This is a bounded prototype;
[ADR 19](../../adr/0019-local-browser-programme-ownership.md) records its limits.

Use Chromium through a direct native loopback connection. Reverse proxies,
forwarded headers, remote browsers and non-native authentication are refused.
When enabled, the page requests 44,100 Hz for its shared AudioContext from the
first audio action, including previews. Local control is unavailable if the
browser does not obtain that rate. Conversion from 48 kHz is deferred.

The station must contain ordinary songs. Automatic liners, breaks, ads, time
checks, configured scheduled events, matched intros, prepared shows and booked
voice are outside this prototype. Admission reports the unsupported feature;
it does not rewrite settings or discard prepared content.

Taking control stops this browser's encoded Listen playback. Its decks feed the
existing broadcast master and encoders; its monitor volume and mute act after
that split. The station's sound-card output is temporarily gated, preserving
saved volume, mute and device settings. Private tests and auditions remain on
their private branch. **Skip** makes a browser-deck transition; **Play or Pause**
pauses that programme while the encoded stream stays connected. Return before
changing the monitor device or station programming. Unreserved queue entries
remain editable; the two reserved occurrences cannot be moved or removed.
Review generation, cancellation and deletion require returning to automation;
existing review audio remains available through its private playback path.

**Return to automation** prepares the next unheard song while the browser keeps
feeding, then waits for the programme fade to be consumed. Losing the tab or a stalled feed also returns control;
neither path restarts stream connections. Recovery can emit silence while the
next source becomes ready. A failed history flush retains receipts and blocks
another claim; retry Return after correcting the storage failure.
If rendering does not become ready within the bounded wait, the station stays
silent with a recovery status instead of resuming an unprepared cursor. Retry
Return once the source is available; an unchanged target reuses its pending render.

During local playback, progress and lyrics still follow the automation cursor.
They do not show the browser deck's current position in this prototype.

Capture has 32 fixed transferable buffers (320 ms) and a separate 200 ms server ceiling. Long main
thread tasks can exhaust that budget and trigger a safe return. Keep expensive
page audits outside an active performance. This prototype makes no long-run
drift, physical latency or subjective audio-quality guarantee.
Initial endurance trials returned early because of capture-pool exhaustion,
accumulated sender timer delay and receive batching. The revised fixed pool,
sender pacing and bounded arrival allowance completed separate 30-minute idle
and controlled-load trials on one Windows/Chromium setup, with no detected
broadcast dropouts or stream reconnection. This does not cover other devices,
browser engines, arbitrary main-thread stalls or longer performances. An
active-owner axe audit still exceeded the fixed capture bound and returned to
automation; run such audits after returning control.

### Automation controls

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

## Held silence warnings

The mix bus never plays past audio it does not have. When the audio due next is not ready, it
plays exact silence and holds the programme where it is, so nothing is skipped. Once a set has
played its first song, each such hold is counted and logged as a warning like this one:

```text
Mix bus held 1.300 s of silence: the next song's file deck was still seeking (/music/song.mp3)
```

The warning gives the length of the hold, its cause and, when known, the file involved. It is
logged when the hold ends. Each station logs at most one such warning a minute; the next one says
how many holds it left out. A hold that reaches 10 seconds is also logged once while it lasts,
even within that minute. Silence before a set's first song is not counted. `/api/status` reports the station's
count of held 20 ms blocks since its mix bus started as `playout.held_blocks`.

The causes:

- **the next song was still being prepared**: the song change was reached before the next song
  had been selected and prepared. Look for slow song picking, analysis or decoding on the same
  computer at that time.
- **the next song had no audio yet**: the next song had no audio left to play on its own, because
  its overlap with the previous song used all of it or its preparation came back empty. The
  station waits for the song after it to be prepared.
- **a queue change was being applied to the next song**: a queue or settings edit replaced the
  coming song change, and the replacement was not ready when the change was due.
- **the next song's file deck was still seeking**: the next song's decoder was still starting at
  the position its audio continues from.
- **a file deck was still seeking or refilling**: the playing song's decoder fell behind. This is
  usually a slow disk or network share, or an overloaded processor.
- **a live edit holds the song change**: an edit to the current song change is waiting to be
  applied, and the old outgoing song must not play into music it has replaced.
- **the master processor was starting**: the mastering processor (smooth processing or Stereo
  Tool) restarted after a set had started, and the bus waited for it to warm up.

"Mix bus fell behind" is a different warning: the bus thread itself ran late, so whole 20 ms
periods were skipped. A held-silence warning means the bus ran on time but had no audio to play.
