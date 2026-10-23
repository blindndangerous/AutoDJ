# 16. One programme with browser-local listening routes

Date: 2026-10-22
Status: Accepted; maintainer agreement in the #541 implementation conversation.
Decision authority: Project maintainer.

## Context

[Issue #541](https://github.com/blindndangerous/AutoDJ/issues/541) separates how
an operator listens from how the station plays. The old Browser output ran a
second mixing implementation and let the browser advance the station. Enabling
it alongside streaming would create competing playback clocks and omit
server-mixed speech and live input from local listening.

## Decision

Every running station has one server-owned programme and mix clock. Play, Next,
queue changes and other station controls affect that programme. Each paired
browser chooses **Local** or **Stream** and toggles **Listen** independently.

Local subscribes to the same post-master mix-bus blocks used by the encoded
stream and server sound card. The authenticated monitor WebSocket carries
44.1 kHz stereo float32 little-endian PCM. Each station admits at most eight
monitor connections, each with four queued 20 ms blocks. A slow connection drops
old queued blocks; a blocked send times out. The browser AudioWorklet keeps a
bounded 200 ms ring and resamples to its audio context's sample rate. These are
buffer bounds, not measured end-to-end latency or a DJ timing guarantee.

Stream uses the existing encoded broadcast and its listener access rules.
Local requires a secure browser context and AudioWorklet support; unavailable
routes are shown honestly. Neither route sends audio back into the station.
Private cue audition, speech previews and programme reviews retain their
separate audio elements and preview-device routing under
[ADR 14](0014-isolate-programme-review-and-export.md).

Listening volume and mute belong to the browser. The server sound card is an
independent optional output, disabled by default. Local works on the server
machine or remotely; network and device timing need actual listening evidence.
Monitor listeners participate in the existing listener-driven idle policy;
switching routes does not create another set while the current one is active.
Station Start/Stop remains on the station list.

## Compatibility and ownership

This supersedes the browser-owned runtime playback decision in
[ADR 3](0003-browser-driven-playback.md). Offline simulation can still construct
a dry-run Player; it is not a second running station clock.

Saved `station.output = "browser"` becomes `"local"` with server speakers off.
Saved `"server"` becomes `"local"` with server speakers on. `"stream"` preserves
its broadcast and optional speaker configuration. Normalization happens when
configuration is read and is saved with later station option edits. All running
stations now need FFmpeg for progressive decoding. Changing a browser's listening
route needs no station restart. Broadcast/device configuration retains the
existing next-start lifecycle.

The monitor is owned and closed by the station runtime under
[ADR 5](0005-a-station-owns-its-playout-chain.md). Its socket uses the existing
operator Host, Origin and browser-session checks, including bounded session
revalidation. Public stream, worker and announcer credentials grant no monitor
access. Disconnect, session expiry and station stop release subscriptions.

## Consequences and alternatives

Receiving the shared mix preserves speech, live input and processing without
running another browser mixer. PCM avoids another encoder but uses more network
bandwidth than the public stream. Browser/device buffers, mastering and network
jitter still contribute delay. Reusing only the public stream would avoid the
new socket but retain its larger listening delay; enabling the old browser decks
would split programme ownership. Revisit transport and timing with measured DJ
workflow requirements.

## Current contracts

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0016-01` | Monitor clients receive bounded independent queues; slow clients cannot accumulate unlimited programme audio. | automated | `tests/unit/test_monitor.py::test_monitor_payload_is_bounded_and_a_slow_client_drops_old_audio` |
| `ADR-0016-02` | Actual browser routing, audible continuity and local/remote latency require listening evidence. | manual | Exercise Local/Stream switches, station switches, shared Play/Next controls, private previews and device volume on localhost and a remote HTTPS browser; record OS, browser, devices, audible gaps and latency. |
| `ADR-0016-03` | Monitor subscriptions attach and release station-owned output; a Local listener keeps an existing stream set alive through the usual idle policy. | automated | `tests/unit/test_monitor.py::test_station_runtime_attaches_detaches_and_closes_monitor_output`; `tests/unit/test_monitor.py::test_local_monitor_audience_starts_and_holds_set_through_idle_grace` |

Implementation: [monitor output](../../src/autodj/monitor.py),
[monitor routes](../../src/autodj/monitor_routes.py),
[browser monitor](../../src/autodj/static/modules/local-monitor.js),
[listening controls](../../src/autodj/static/modules/listening.js).
Procedure: [server playback](../reference/operations/server-playback.md).
