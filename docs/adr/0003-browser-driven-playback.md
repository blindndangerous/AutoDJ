# 3. Web UI defaults to browser-driven audio playback

Date: 2026-05-05
Status: Accepted. Superseded in part by [ADR 4](0004-server-mix-bus-and-stream-mode.md):
the server-side modes and where their playback effects live. The `autodj play` command this
record mentions was removed in 0.19.0; the notes marked "Superseded" below say what holds now.

> Current scope and implementation evidence are in the dated amendment
> at the end of this record. Original sections below are retained as history.

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

## Amendment, 2026-10-16: current scope and verification

The browser default remains accepted. Original references to a parallel CLI
playback session, an audio-dependency-free base installation, and the exact
device-routing fallback are historical context, not current setup instructions
or a cross-browser capability guarantee. `play` has been removed. The base
dependency set and supported install path are defined by
[package metadata](../../pyproject.toml) and [setup](../setup.md).

The selected station's browser Web Audio graph owns browser playback. Server
output modes follow [ADR 4](0004-server-mix-bus-and-stream-mode.md), and every
station can select its supported output under the amended
[ADR 5](0005-a-station-owns-its-playout-chain.md). Device-local volume/mute and
output selection must not be interpreted as changes to the broadcast level.

Implementation: [audio engine](../../src/autodj/static/modules/audio-engine.js),
[stream-mode UI](../../src/autodj/static/modules/stream-mode.js),
[player](../../src/autodj/player.py), and [station runtime](../../src/autodj/stations.py).
Verification criteria: browser mode does not construct the server mix bus;
server modes do; station switching preserves the intended listening route.
See [mode tests](../../tests/unit/test_player_stream_mode.py),
[station browser tests](../../tests/jsmodules/stations.test.js), and
[stream UI tests](../../tests/jsmodules/stream-mode.test.js).

Browser autoplay and audio-device support remain platform capabilities; tests
with fake audio contexts do not prove every browser/device combination. Follow
[listening operations](../operations.md#controls-while-streaming) and the
[accessibility audit](../accessibility-testing.md) for live checks. Revisit for
a changed default output or browser-delivery architecture. This amendment adds
no new output path. Record actual checks and their limits in the relevant PR.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0003-01` | Browser output constructs no server mix bus; explicit server-audio output does. | automated | `tests/unit/test_player_stream_mode.py::test_server_audio_builds_the_bus_up_front_and_browser_mode_has_none` |
| `ADR-0003-02` | Native browser playback/device behavior needs supported browser/device evidence. | manual | Exercise browser playback and device selection on the affected engine/device; unit audio-context doubles are insufficient. |

## Amendment, 2026-10-30: local DJ ownership

[Accepted ADR 20](0020-local-browser-programme-ownership.md) designs one local
browser's performance feeding an existing streaming station. It preserves
browser-owned rendering and distinguishes it from legacy browser-only output.
This adds an opt-in streaming input; legacy browser-only mode still has no bus
(ADR-0003-01). Phase 2a exposed preparation only. Phase 2b connects the bounded
owner workflow, independent monitor branch and consumption accounting described
in ADR 20; broader issue acceptance and hardware evidence remain separate.
