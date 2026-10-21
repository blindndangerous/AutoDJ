# Feature availability

Use this reference to check an output mode's requirements before configuring it.
These are source capabilities, not certification of every operating system,
browser, audio device or external service. See [sound setup](../guides/build-your-sound.md),
[streaming](../guides/streaming-and-relays.md) and [live work](../guides/going-live.md)
for procedures.

## Station outputs

Choose **Browser**, **Server sound card** or **Radio stream** in **Settings,
Stations**. Every station supports these choices. Startup flags
`serve --server-audio` and `serve --stream` override the default station for that run.

| Capability | Browser | Server sound card | Radio stream |
| --- | --- | --- | --- |
| Mixing | Selected browser's Web Audio graph | Server mix bus | Server mix bus |
| Listening | Browser device | Server audio device | Connected stream players; optional browser listening |
| Browser must stay running for this output | Yes | No | No; listener/continuous policy still applies |
| Crossfades, EQ, transitions, liners | Browser path | Server path | Server path |
| DJ mixing beatmatching | Separate Beatmatch on skip control | Server DJ mixing | Server DJ mixing |
| Record and book a voice track | No server song-change slot | Available with breaks enabled | Available with breaks enabled |
| Volume, mute, device | Local browser controls | Server sound-card controls | Listening-device controls; no change to broadcast level |
| Live DJ and browser talkover | Unavailable | Unavailable | Available with live-input requirements |
| Broadcast recording and outbound relays | Unavailable | Unavailable | Available when configured |

The mixing paths need separate checks. Server preparation does not guarantee
continuity under arbitrary load. Seeking is restricted in server-mixed playback.
Stream start/stop depends on listeners, continuous-play settings and components
keeping the station active. Closing the control page is not a stream stop command.

Beatmatching needs suitable BPM/beat data and a pair within the configured stretch
limit; a checked box does not establish a successful match. The
[beatmatching lesson](../tutorials/beatmatched-set.md) compares the server path.
The [voice-tracking lesson](../tutorials/voice-tracking.md) distinguishes private
microphone preview from booking audio at an available server song change.

## Browser and connection requirements

Browser playback needs Web Audio and a user action when autoplay is blocked.
Audio-device selection depends on browser APIs, permissions and actual devices.
Microphone recording, talkover and AudioWorklet-dependent behavior need a secure
context: trusted HTTPS, or localhost on the machine running AutoDJ. A remote LAN
HTTP page is not a secure context. Each client must trust the HTTPS certificate.

Players must support the selected format: MP3 by default, optional AAC and Opus.
Opus does not carry song-title metadata. Authentication, codec and certificate
support varies by player; test the actual device. Listening is public by default
and grants no station control. Optional listener credentials differ from pairing.
Live-source passwords require HTTPS or loopback unless the operator deliberately
enables the documented trusted-LAN exception. See [access management](../guides/manage-access.md).

## Runtime and library requirements

- Follow [setup](../setup.md) for current CPU/GPU choices and driver limits.
  Detection alone does not prove working inference. Playback does not require
  GPU analysis. Node.js is a contributor-tool dependency, not a runtime step.
- FFmpeg is required for streaming and analysis/server decoding of MP3, M4A,
  MP4 and AAC. Other formats use soundfile with FFmpeg fallback. Server sound-card
  output needs a usable system audio device and its platform audio support.
- [No-model setup](../setup.md#no-model-installation) includes browser playback,
  streaming and basic imports. It omits local model analysis and sound-card output
  packages. For direct package installs, `index` adds local model analysis and
  worker execution, `play` adds sound-card output, and `all` adds both. Guided
  default setup and the container retain model analysis.
- Basic imported songs remain playable without advanced embeddings. Similarity
  selection needs compatible advanced analysis; random rotation and queued songs
  can use songs without it. The advanced-analysis length cutoff can exclude long
  tracks without making them unplayable. See [music import](../guides/add-your-music.md).
  A no-model server can accept remote analysis only with an existing compatible
  server-side model cache; see [worker requirements](../guides/operations/gpu-worker.md#3-install-autodj-on-the-gpu-machine).
- Speech synthesis needs a compatible configured endpoint or station TTS command.
  Preflight does not prove authentication, service availability or audio quality.
  Generation can cost money. Saved liners and activated complete time-check sets
  play locally without provider access; missing clips are not synthesized on demand.
- Stereo Tool needs a separately obtained compatible library and applicable licence;
  container images do not bundle it. External music, voice packs, playlists and
  tools must remain reachable at their configured paths.

## Verification limits

The [browser ADR](../adr/0003-browser-driven-playback.md) and
[server-mix ADR](../adr/0004-server-mix-bus-and-stream-mode.md) distinguish automated
invariants from manual device/load checks. The Proposed
[setup record](../adr/0012-separate-runtime-setup-from-model-execution.md) records
compatibility gaps and does not expand hardware support.

This page was checked against source. This documentation change establishes no
native Windows, macOS, sound-card, external-player or live-provider run. Run
[Doctor](../guides/collect-diagnostics.md), then [rehearse](../guides/prepare-station.md)
on your installation. If a required feature is unavailable, retain the working
configuration and collect diagnostics before changing dependencies or access.

AutoDJ and this page were developed with AI assistance.
