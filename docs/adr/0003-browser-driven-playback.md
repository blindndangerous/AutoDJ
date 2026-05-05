# 3. Web UI defaults to browser-driven audio playback

Date: 2026-05-05
Status: Accepted

## Context

`autodj serve` historically ran a single audio output stream from the
server process.  When a user opened the web UI on the same machine
they already had `autodj play` running on, *both* processes streamed
audio to the soundcard — one slightly out-of-phase with the other.

A user toggling skip / volume / EQ also expected those changes to
affect *the audio they were hearing in the browser*, not a parallel
server-side stream.

## Decision

`autodj serve` defaults to **`--no-playback=True`**.  The Python
process picks tracks; the browser's Web Audio graph plays them.
Passing `--server-audio` opts back into the legacy mode (rare — only
useful for headed hosts where the user wants both surfaces).

## Consequences

- The web UI and the CLI `play` loop are now fully decoupled — they
  never share a soundcard.
- All playback effects (crossfade, transitions, volume, device
  selection) live in the browser's Web Audio graph.
- `AudioContext.setSinkId` is the only working route-changing API
  once Web Audio intercepts the `<audio>` element; the element-level
  `setSinkId` becomes a Firefox-only fallback.
- A user with no audio deps installed (`uv sync` minimal) can still
  drive the web UI from a headless server — the browser is the
  audio host.
- The server is no longer a single point of failure for audio
  output; a web-UI page reload doesn't affect a parallel CLI
  session.

## Alternatives considered

- Keep server-side playback as default and let users opt out — too
  surprising; users don't expect duplicate audio.
- Drop server-side playback entirely — closes the door on legacy
  workflows that depend on it; `--server-audio` keeps the option.
