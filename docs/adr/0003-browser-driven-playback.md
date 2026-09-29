# 3. Web UI defaults to browser-driven audio playback

Date: 2026-05-05
Status: Accepted. Superseded in part by [ADR 4](0004-server-mix-bus-and-stream-mode.md):
the server-side modes and where their playback effects live. The `autodj play` command this
record mentions was removed in 0.19.0; the notes marked "Superseded" below say what holds now.

## Context

`autodj serve` historically ran a single audio output stream from the
server process.  When a user opened the web UI on the same machine
they already had `autodj play` running on, *both* processes streamed
audio to the soundcard — one slightly out-of-phase with the other.

A user toggling skip / volume / EQ also expected those changes to
affect *the audio they were hearing in the browser*, not a parallel
server-side stream.

## Decision

`autodj serve` runs with **server-side playback off**.  The Python
process picks tracks; the browser's Web Audio graph plays them.
Passing `--server-audio` opts back into server-side playback, for hosts
that should play through their own sound card.

## Consequences

- The web UI and the CLI `play` loop are now fully decoupled — they
  never share a soundcard. (Superseded: `autodj play` no longer exists,
  and `serve --server-audio` plays on the host's sound card.)
- In browser playback, all playback effects (crossfade, transitions,
  volume, device selection) live in the browser's Web Audio graph.
  (Superseded, ADR 4: `--server-audio` and stream mode apply crossfades,
  transitions, EQ and liners on the server mix bus; volume, mute and the
  output device are applied only in the sound-card output, and stream
  mode applies no volume.)
- `AudioContext.setSinkId` is the only working route-changing API
  once Web Audio intercepts the `<audio>` element; the element-level
  `setSinkId` becomes a Firefox-only fallback.
- A user with no audio deps installed (`uv sync` minimal) can still
  drive the web UI from a headless server — the browser is the
  audio host.
- The server is no longer a single point of failure for audio
  output; a web-UI page reload doesn't affect a parallel CLI
  session. (Superseded: there is no CLI playback session any more.)

## Alternatives considered

- Keep server-side playback as default and let users opt out — too
  surprising; users don't expect duplicate audio.
- Drop server-side playback entirely — closes the door on workflows
  that depend on it; `--server-audio` keeps the option.
