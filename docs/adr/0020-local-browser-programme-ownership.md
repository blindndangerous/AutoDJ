# 20. One local browser owns a station's DJ performance

Date: 2026-10-30
Status: Accepted; maintainer approval in conversation on 2026-10-30, with phased scope and evidence below.
Decision authority: Project maintainer. Approved in conversation on 2026-10-30; implementation and verification remain scoped by phase.

## Context and scope

[Issue #541](https://github.com/blindndangerous/AutoDJ/issues/541) requires local
gestures and monitoring on the browser's audio clock, with that actual stereo
performance sent into the existing broadcast chain. This record defines the approved prototype. Phase 2a implements only measurement,
ownership/admission and bounded input. Phase 2b connects the operator workflow.
Acceptance of this decision does not establish implementation or close the issue.

[PR #543](https://github.com/blindndangerous/AutoDJ/pull/543) was withdrawn: its
server-to-browser mixed PCM monitor replaced browser-owned performance. Its
architecture text is not accepted authority. Several independently advancing
local renderers and replaying browser gestures in a server renderer are excluded.

The baseline includes [#537](https://github.com/blindndangerous/AutoDJ/issues/537)/
[#539](https://github.com/blindndangerous/AutoDJ/pull/539) and
[#538](https://github.com/blindndangerous/AutoDJ/issues/538)/
[#542](https://github.com/blindndangerous/AutoDJ/pull/542).
Read later amendments too: [ADR 4](0004-server-mix-bus-and-stream-mode.md)'s
2026-10-27/28 continuation and decoder preseek changes supersede the original
silent cursor-only promotion. They remain the automation path.

This is a new ownership/input boundary, so the disposition is `create_new_adr`.
It does not yet supersede [ADR 3](0003-browser-driven-playback.md), change
[ADR 5](0005-a-station-owns-its-playout-chain.md)'s station ownership, or relax
[ADR 14](0014-isolate-programme-review-and-export.md)'s private-review isolation.
Dated scope amendments in ADRs 3/4/5 record the accepted extension: a browser can own
performance while a streaming station retains its server output bus. Legacy
browser-only mode still constructs no bus (ADR-0003-01). Do not retire that ID
merely to make a new local-control test pass.

The prototype is opt-in through `server.local_programme_enabled = false` by
default, initially Chromium on a direct native loopback
connection, on an already-running streaming station with ordinary song content.
It demonstrates acquisition, two browser decks, local cue/fader response, one
transition, per-song accounting, a competing browser, release and controller loss.
It is not a general release of all #541 acceptance criteria.

## Amendment, 2026-10-30: approved Phase 2a scope

The maintainer approved this design with three changes before implementation:
44,100 Hz only, a server-wide opt-in defaulting off, and unchanged ordinary proxy
semantics backed by regression evidence. The setting lives in server TOML and
requires restart; Phase 2a adds no Settings UI or alternate Save/Discard path.
With it off, claims are refused and the existing middleware and WebSocket
configuration remain unchanged. With it on, preserve ordinary client address
and scheme with and without explicit proxy settings. Existing proxy/security,
station-route and WebSocket tests must pass unchanged.

Phase 2a stops after plan steps 1 through 3. The bus selector and PCM path are exercised
through a disposable fixture/internal activation seam. Public connections may
prepare and validate input but cannot activate on-air ownership before steps
4 through 6 provide occurrence accounting and handoff. This prevents exposing an
unaccounted broadcast path while retaining a runnable input prototype.

## Existing boundaries and evidence

- [Browser engine](../../src/autodj/static/modules/audio-engine.js):
  `ensureAudioGraph` routes media-element decks and effects through `_program`,
  optional `_duck`, then monitor `_master`. `startCrossfade` calls media `play()`
  and schedules gains on `AudioContext.currentTime`; automatic starts also depend
  on `timeupdate` and timers. Measure this before replacing the deck source layer.
- [Application](../../src/autodj/static/app.js): `applyState` disables browser
  decks in stream mode. `playBrowserClip` sends programme inserts and some private
  liner tests directly to `_master`; those uses need separate routing.
  [Stream listening](../../src/autodj/static/modules/stream-mode.js),
  [private output](../../src/autodj/static/modules/preview-output.js), and
  [programme review](../../src/autodj/static/modules/programme-review.js) have
  independent listening lifecycles to preserve.
- [Bridge](../../src/autodj/_bridge.py): `advance(from_path)` deduplicates only
  by filename and calls `skip`; it cannot distinguish two occurrences of one
  song and would skip the server bus in stream mode. `_advance_now_locked`
  illustrates server-owned picks/history, not a suitable owner protocol.
- [Live desk](../../src/autodj/live.py): `claim` reserves before opening its
  decoder; `release` checks source identity, prepares automation, then retains
  a return source. External audio is MP3/AAC/Ogg decoded into 44.1 kHz stereo.
  Its PCM buffer holds two seconds and drops oldest overflow; the encoded queue
  holds 64 chunks. Admission allows 512 kbit/s over five seconds plus 64 KiB,
  at most 2 GiB per connection. Timeouts are 10 seconds without bytes,
  30 minutes silent standby and 15 seconds amplitude silence on air.
- [Mix bus](../../src/autodj/mixbus.py): external live requires 300 ms preroll,
  reprimes on underrun and crossfades over one second by default. Automation is
  held while live. `render_block` produces 882 stereo frames every 20 ms;
  `_run_paced` passes these to continuous mastering and attached outputs.
- [Station runtime](../../src/autodj/stations.py): `_live_started` closes the
  interrupted song, writes one `kind=live` session and drops unaired breaks.
  `_ready_automation` prepares the next selection and drops the held song.
  External live holds listener-idle shutdown; hard events affect the return,
  soft events queue, the latest deferred liner survives, time checks expire,
  and carts are refused. These are not local-DJ song/scheduling semantics.
- [Microphone route](../../src/autodj/live_routes.py) and
  [capture](../../src/autodj/static/modules/live-talk.js) provide useful
  authorization/cleanup patterns, but mono Int16 talkover, linear resampling,
  a half-second buffer and two-second idle timeout are not a stereo programme feed.
- [Station routes](../../src/autodj/station_routes.py) own default and named
  aliases. [Ledger](../../src/autodj/station_ledger.py) buffers history by event
  ID; [store](../../src/autodj/station_store.py) upserts that ID. Neither proves
  browser occurrence deduplication or a crash-atomic queue/history transaction.

Relevant existing evidence entrypoints are ADR-0004-05/09/11/12/14,
ADR-0005-01/02/04/05, and ADR-0014-01/05/06. Their mappings remain intact.
Existing [live tests](../../tests/unit/test_live.py),
[live integration tests](../../tests/integration/test_live_source.py),
[browser levels tests](../../tests/jsmodules/audio-engine-levels.test.js), and
[station mode tests](../../tests/unit/test_player_stream_mode.py) establish
where to check regressions, not that checks ran for this proposal.

Source discrepancy: the external-live return description in
[live DJ operations](../guides/operations/live-djs.md) and `_ready_automation`'s
docstring still describes a stale-render fallback. Current
`Player.prepare_live_return` and ADR 4's accepted amendments do not provide it.
This is a documentation defect to reconcile separately; this proposal does not
copy it, restore obsolete runtime behavior, or claim a fallback exists.

## Decision

### Ownership and lifecycle

One station-owned `LocalProgrammeController` arbitrates automation, external
live input and local browser input. Store it on `StationRuntime`, not on the
shared library or a process-global browser singleton. The server remains the
authority for station identity, queue, permissions and history. The browser
owns decoding, deck timing, transitions and programme gestures while admitted.

The record contains a random runtime epoch, monotonically increasing 64-bit
generation, random 256-bit owner capability, authenticated browser identity
(or anonymous native loopback identity), socket identity, phase, monotonic lease
deadline, PCM sequence/frame cursors, event sequence and reserved occurrences.
The capability is socket-bound, returned only to its owner, held in page memory,
and absent from URLs, storage, logs, status broadcasts and backups. A cookie is
not a tab identity. Two tabs sharing a cookie still compete for one record.

States are `automation`, `preparing`, `local`, and `returning`. Claim is an
atomic compare-and-set from automation. Preparation has a 10-second absolute
deadline; active leases last three seconds and renew every second. Separate
feed health requires advancing PCM frames within 500 ms, even if heartbeats
continue. Intentional pause sends zero PCM with advancing capture-frame numbers
and a pause state; amplitude never expires ownership. These are initial
experimental deadlines, not performance guarantees.

Use one authenticated station WebSocket, `/api/local-programme/ws` and its
`/api/stations/<slug>/local-programme/ws` alias, for claim, readiness, PCM,
events, acknowledgements, heartbeat and release. Acquisition conflicts are
explicit (4409); authentication/locality failures use 4401/4403, unsupported
protocol 1003 and size/rate abuse 1009. Bounded JSON messages carry version,
type and generation, with unknown fields refused. No third socket or media
frame silently steals an owner. Add station route classification and body caps.

Every mutation checks runtime epoch, generation, owner capability, socket
identity, lease and authorization. The bus checks a published generation fence
again before consuming a block. Revocation also flushes unconsumed PCM and its
events: checking only on arrival leaves stale buffered audio playable.
Only an already-rendering block can finish; no subsequently selected block uses
the revoked source. Expiry checks run independently of incoming messages.

Disconnect, session revocation, stop, malformed feed and expired lease
invalidate the generation once and enter return. A voluntary return request
enters `returning` but keeps current-generation PCM and lease renewals admitted
until handoff commits or its two-second deadline expires; new occurrence starts
are refused. The committed release then invalidates that generation.
Concurrent cleanup is
idempotent; failed teardown retains ownership of its resources until it exits,
as in ADR-0005-05. Reconnect is a new connection and generation, never a resume
of old audio. It can acquire only after return finishes, through a new explicit
Take local control action. A dropped status socket alone does not end a healthy
programme socket. Page reload or programme-socket loss does.

Any authorized operator may explicitly request Return to automation through
`POST /api/local-programme/return`, with the observed generation and ordinary
Host/Origin/session checks. This is also the remote administrative revocation
path; it grants no remote DJ ownership. Station Stop remains available and
invalidates the owner before normal teardown. Start never restores a browser
lease. In-flight replies carry station and generation and are ignored after
either changes. A selected-station change first returns ownership, then switches;
failure leaves the selection unchanged with an actionable status.

### Same-machine admission and proxy handling

Trust the actual connection peer captured before forwarding middleware, not a
client claim, URL, DNS result, Host, Origin or rewritten `scope['client']`.
Currently `security.peer_address` reads that rewritten field and `server.py`
enables Uvicorn proxy handling. `machine_transport_allowed` accepts HTTPS from
remote clients and therefore is not a locality predicate.

Only when the server opt-in is enabled, on the existing listener disable Uvicorn's implicit proxy wrapper and install
an equivalent explicit wrapper inside a raw-peer capture ASGI wrapper. Capture
the original protocol-supplied client tuple in private scope metadata before
`ProxyHeadersMiddleware`; never populate it from headers. Preserve the current
trusted-proxy processing for ordinary routes. A server launched without this
verified wrapper has local control unavailable. Test the real Uvicorn listener,
including WebSocket upgrades, rather than only fabricated ASGI scopes.

Local claim requires an IPv4/IPv6 loopback peer (normalize mapped IPv4), native
authentication mode, exactly one permitted Host and Origin, and a direct loopback
page origin with the served port/scheme. Reject missing/duplicate/null Origin,
forwarding headers (`Forwarded`, all `X-Forwarded-*`, `X-Real-IP`) and proxy
identity/proof headers, regardless of whether they would otherwise be trusted.
Continue native pairing requirements when enabled; anonymous loopback mode
still needs the server-issued ownership capability and all other checks.
Machine, announcer, external DJ and listener credentials confer no ownership.

Prototype availability is off for trusted-proxy authentication or explicitly
configured proxy deployments, including proxy environment overrides. Do not
relax authentication to reach a direct port. Behind a reverse proxy the browser
keeps ordinary stream listening and sees a local-control unavailable reason.
Remote ownership and a dedicated authenticated local ingress are later work.

The trusted boundary includes local processes and deployment configuration.
A header-stripping local proxy or tunnel is indistinguishable from another
loopback peer; raw peer proves the immediate connection, not a remote human's
physical location. Operators must not forward local-control routes, even under
native authentication. If such forwarding cannot be excluded, leave the
prototype disabled. Stronger protection against a cooperating local relay needs
an OS-bound companion/capability and is outside this prototype. Host/Origin
remain rebinding and cross-site protections, not proof of end-browser locality.
This limitation follows the existing
[threat model](../../THREAT_MODEL.md#announcer-breaks), and is not hidden by a
localhost-looking address. See also [Uvicorn proxy settings](https://uvicorn.dev/settings/).

### Programme transport, rates and bounded work

Capture the post-effect stereo programme sum with an AudioWorklet. Retain
media-element decks initially; preload the reserved sources, await usable media
and observed start, and measure start/seek behavior before deciding whether a
bounded AudioBuffer deck implementation is needed. Do not predecode an entire
library or substitute a server-rendered feed. Web Audio's render clock governs
gains; packet transfer and server acknowledgements never gate each loaded-deck
gesture. See the [Web Audio specification](https://www.w3.org/TR/webaudio/).

Use binary WebSocket messages, interleaved little-endian float32 stereo, at the
actual `AudioContext.sampleRate`. Create the programme context with
`{ sampleRate: 44100 }` and verify the obtained rate. Accept only 44,100 Hz;
otherwise local control is unavailable with a clear reason before acquisition.
The negotiated format cannot change mid-generation. One 10 ms packet holds
441 frames, including silence. This is 352,800 bytes/s before headers.

Version 1 has a 48-byte little-endian header: four-byte magic `ADJP`, uint8
version, uint8 flags (zero initially), uint16 channel count, uint64 generation,
uint64 packet sequence, uint64 first capture frame, uint32 frame count,
uint32 sample rate, and eight reserved zero bytes. Payload length must equal
48 plus frame count times eight, at most 3,576 bytes. Sequence and capture
frames start at zero and advance exactly; overflow requires a new generation.
Reject unknown versions/flags, nonfinite samples, wrong rate/channels, short or
extra bytes, duplicates and reordered/gapped packets. Finite samples may exceed
unity within a fixed magnitude bound of 16; reject larger magnitudes. Preserve
mix headroom for the existing master, clipping only at its existing boundary.
Do not reuse `PcmBuffer.push_frames`' pre-master clipping unchanged.

The server accepts 44.1 kHz without resampling. 48 kHz conversion and its FIR
state, phase and lookahead are deferred. Keep exact source-to-bus frame mapping.
Reject every other rate rather than silently relabeling samples.

Initial experiment settings: 60 ms prefill, 200 ms total unconsumed input
credit, and a 200 ms server PCM hard cap (8,820 output frames). Server consumption
acknowledgements replenish frame credit; bytes accepted by TCP alone do not.
The worklet has a fixed transferable pool for at most four 10 ms packets and
never blocks its render callback. Main-thread `WebSocket.bufferedAmount` may
not exceed 50 ms of negotiated PCM plus one packet. Count queued, transferring,
validated and acknowledged-but-unconsumed audio against credit; do not grant
each stage an independent unbounded backlog. A bounded staging slot holds at
most one packet without conversion state. Exceeding a cap terminates the feed and starts
recovery; never silently discard old programme while claiming continuous play.

Enforce a rolling one-second ingress budget of 1.25 times the negotiated PCM
byte rate plus two packets, at most 125 PCM messages/second plus a two-message
burst, and 20 control messages/second plus a burst of 10. Text is capped at
4 KiB with fixed schemas and bounded event fields. Limit handshake candidates
to two per station, give the first message a two-second deadline, and refuse
excess attempts without allocating PCM workers. Configure Uvicorn's assembled
WebSocket message limit to 64 KiB and receive queue to four; verify existing
32 KiB microphone messages and status sockets still work. Application checks
alone after assembling arbitrarily large frames are insufficient.

The bus reads fixed 882-frame blocks nonblockingly. A shortage emits zeros
for missing frames and increments missing-frame/block counters; it never
replays the last audio. Received source-frame progress holds over the missing
part, and later PCM is mapped to its actual later bus position. Three successive
short blocks (60 ms) or 500 ms without fresh PCM trigger return. Valid sustained
zero PCM does neither. All cleanup, logging, ledger work stay
off the bus callback. Admission backpressure never delays monitor rendering;
feed failure fences and silences the browser's programme monitor. A worklet-side
watchdog mutes the programme monitor after 750 ms of audio-clock time without a
fresh server progress acknowledgement relayed by the page, even if its main
thread is stalled. Preparation-discard acknowledgements qualify only while
preparing. A suspended context produces no audio; it must revalidate ownership
before its programme gate opens again. The watchdog never mutes private previews.

Browser hardware and server wall clocks can drift even at equal nominal rates.
This prototype uses 44,100 Hz without conversion and measures occupancy/drift; it
does not claim indefinite synchronization. Sustained occupancy outside 20–160 ms
for five seconds, or reaching the hard cap, triggers explicit return with a
drift/buffering reason. Adaptive asynchronous resampling is deferred until
measurements justify its filter and control policy. Changing packet/prefill
settings requires recorded measurements and updated test expectations.

### Output boundary and local monitoring

Add a distinct local-programme input selector to `MixBus.render_block` before
the existing continuous `master.process` and output fanout. While selected,
browser PCM bypasses automatic music, station EQ, liners and microphone
talkover; these effects must not run a second time. Arbitration excludes an
external source or talker both before and during local ownership. Keep the
master, primary/alternate encoders, relays, recorder and public listener sockets
alive. Encoder identity and a continuously open HTTP response must survive
normal takeover and return. A normal encoder failure retains existing recovery.

Browser signal flow is decks/effects, programme ducking if supported, explicit
programme sum, then two branches: capture worklet and monitor gain/mute/device.
`_master` remains monitor gain; programme faders/EQ precede the split. Pause is
a programme action, not monitor mute. Capture remains connected and clocked
when monitor gain is zero. Device selection uses the existing feature-detected
context sink API after this logical split; changing a sink can still suspend
the shared context and must be measured. If continuity cannot be maintained,
require return before changing the device; do not silently promise isolation
from device-clock changes. A second monitor context is a later alternative.

Private cue, audition, liner Test, track/speech previews and programme review
remain on their independent preview elements/output route. In particular,
private `playBrowserClip` calls cannot enter the programme sum or duck it.
No application-wide/system-loopback capture is used. Preview playback never
sends a programme event or consumes a queue occurrence.

The owning browser stops its own encoded Listen playback when local monitoring
activates, avoiding two delayed copies. Other browsers keep listening normally.
Listen, volume, mute and device controls neither claim nor release ownership.
Use owner-specific state instead of changing `stream_mode` or the legacy
`browser_playback` flag for every client. Separate local monitor settings from
existing handlers that also write server speaker volume.

Station Start/Stop remain in the station list. Existing optional server sound
output remains configured in Settings. During local ownership temporarily
suppress that station's server speaker output through an output-only gate,
restoring its prior configured level/mute after return; never change stream
gain or saved settings. This conservative prototype avoids accidental double
monitoring. Independently enabling simultaneous server speakers while owning
is deferred. New configurations retain server speakers off by default.

### Occurrences, accounting and one queue

Reserve at most two server-issued occurrence IDs from the station's one queue
and picker. A reservation contains queue/source revisions, library identity,
validated cues and authorized media address, not a client-supplied file path.
Repeated songs receive distinct IDs. Transfer an already-selected unheard
successor instead of selecting it twice. Reservations are views of the shared
queue, not another advancing browser queue. Explicit prototype reservations
may preload two decks; they do not enable speculative server B-to-C rendering.

The owner reports `started`, `ended`, `paused`, `resumed` and load-failure events
with generation, monotonic event sequence, occurrence ID and capture-frame
position. Queue an observed start marker before the PCM containing its first
programme contribution on the same ordered socket; do not report start merely
because `play()` was requested. An event awaiting its frame is pending, not
aired. Refuse impossible order, unreserved IDs, wrong station/generation and
out-of-range cues. Permit forward markers only within outstanding frame credit;
validate them against corresponding PCM on arrival, and discard markers whose
PCM never arrives. A repeated identical event
gets its original acknowledgement without side effects; a reused sequence with
different content is a protocol error. Bound pending events to 64 and completed
response cache to 256; evicted sequences are rejected, never reapplied.

Commit queue consumption, history/last-played updates and public metadata only
when the bus consumes that occurrence's first declared programme frames. Pass a
bounded receipt to the control worker; perform no database work on the audio
thread. Use the server occurrence ID as the history event ID and reuse ledger
upserts. Apply the queue occurrence transition once under the existing
bus/queue/worker lock order, with an idempotent receipt state; flush history
before acknowledging a completed accounting event. At most two song history
rows may be open during a crossfade. End each at its actual last admitted
contribution, including interruption; zero-gain cueing has no heard record.
Metadata identifies the incoming occurrence at its first programme contribution.

Make start ordering explicit: start a prepared media element with its programme
gain at zero, await observed `playing`, then schedule its gain opening on a future
audio-clock frame and arm that same frame/occurrence in the capture worklet.
The worklet returns the start marker and containing packet together; the sender
emits marker JSON before that binary packet in one task. Report the actual media
offset at the opening, not an assumed exact cue position; measure media-start
and cue offsets in the first experiment. Require a worklet acknowledgement before
exposing programme gain, with sufficient measured lead to the future frame.
If arming misses that frame, keep the gate closed and retry preparation rather
than air unmarked audio. Delayed main-thread delivery must be tested explicitly.

Reserve one of 64 receipt slots before admitting each event-bearing span, plus
two dedicated finalization slots for the two open songs. Exhaustion refuses new
markers and initiates return before any unaccountable new contribution airs.
Revocation discards only unconsumed spans/markers. Already-consumed receipts,
including a block rendering when the fence changed, drain once before resource
cleanup completes. Persistence failure retains these bounded receipts, reports
an accounting error and blocks acquisition until resolved; it never acknowledges
an unpersisted completed event.

An admitted browser remains trusted to describe its own content: frame receipts
prove transport/consumption, not that arbitrary PCM matches a claimed song.
Tests must distinguish accounting accuracy from content attestation. Exactly-once
here covers accepted events, retries and tab loss in one running station.
Existing buffered ledger/queue persistence does not establish crash-atomic
exactly-once recovery through server or power failure. Epoch invalidation rejects
all old events after restart; a durable queue/event journal for that stronger
guarantee is deferred and remains a feature-closure risk, not a passing claim.

Once local preparation begins, fence legacy `/advance`, `/repick-next`, Skip,
Play now, seek and programme-setting mutations that could move the server
renderer, including nonowner media-error handlers. The owner uses its local
loaded decks for next/cue/fader actions and the occurrence protocol for results.
It requests another server reservation only after a slot becomes free. A load
failure cannot manufacture a play; the server cancels/replaces the reservation
through existing picking rules. No nonowner independently repicks.

Operators may append or reorder unreserved future songs under ordinary queue
revision checks. Editing/removing a reserved occurrence returns a conflict and
requires return to automation first. Clear queue, hard play-now and changes to
unsupported scheduling settings also refuse during local control. Validate
these rules server-side, including internal scheduler/request paths, rather
than only disabling buttons. Emergency station Stop and explicit administrative
Return retain their authority.

### Takeover, return and bounded content scope

During preparation automation continues. Preload reserved decks, start the
capture path with zero PCM and prime the feed; audition is private. Only after
readiness, authentication, source/queue revision and content admission checks
succeed does the server arm takeover. The browser starts the first reserved
occurrence on its own clock and supplies the marker and initial PCM. At the
next bus boundary with a full prefill and valid marker, atomically fence
automation promotion, record its outgoing endpoint and select local input.
Use a 20 ms fade down of automation followed by a 20 ms fade up of local PCM,
with each side's actual consumed endpoints recorded. These ramps are an
experimental click-avoidance policy, not a seamless transition guarantee.
Discard old automatic preparation only after commit. Failed preparation leaves
automation and queue consumption unchanged and releases reservations.

Preparation zeros are explicitly off air: the receiver validates and
discards them, returning credit without history receipts. Nonzero preparation
PCM is refused. The arm exchange names a future capture-frame boundary; from
that boundary the receiver retains PCM under normal consumption credit and
requires the fresh 60 ms prefill and first start marker. No earlier discarded
zero can be replayed on air. Packet/frame sequences remain monotonic through
arming. This separates testing capture readiness from filling the on-air queue.

Refuse takeover during an already-audible overlap or any started/pending inserted
audio, to avoid transferring half a transition or speech chain. A song interrupted
at takeover retains its heard history and ends there; it is not silently requeued.
Already heard incoming content must never be treated as an unheard reservation.

Return prepares automation's next unstarted occurrence off the callback while
the owner continues feeding. Freeze new browser starts once return is requested.
Commit the prepared return at a bus boundary with the same 20 ms down/up ramps,
then invalidate the owner and discard unconsumed local PCM. Do not replay the
interrupted browser song from its beginning. Preserve all unused reserved IDs
in their original queue order for automation; revalidate source revisions and
normal selection rules. Do not call external `_live_started`/`_live_ended` or
`drop_unaired` for local input. No synthetic external-DJ history row is written.

Voluntary preparation waits at most two seconds. If it cannot ready automation,
finish release into explicit silence/recovery with a reported reason and continue
ordinary bounded preparation; do not trap the user as owner indefinitely. On
tab loss, stalled feed or revocation, fence immediately, discard pending PCM,
close open occurrences at their last consumed frames and prepare the same
unstarted continuation. Silence can last until that source is ready. Persist
and report the observed gap, not a promise to recover within the lease deadline.
Station Stop does not restart automation; it follows normal shutdown. A local
owner holds the existing listener-idle lifecycle open without adding a fake
listener; after return the normal idle policy resumes.

For this prototype, reject acquisition if a show/prepared edition, queued
non-song item, booked voice/announcer break, matched intro, automatic liner,
time check, ad schedule or enabled timed programme event could require playback.
Require the corresponding automatic features disabled, not merely no event due
in the next few seconds. Do not turn them off on the operator's behalf. Recheck
atomically at takeover; refuse attempts to activate them while preparing/local/
returning, including background admission and machine uploads. An unexpected
internal admission race aborts preparation or initiates return without marking
content aired, missed or deleted. Existing files, recordings and approvals survive.
Private review can still run, with ADR 14's no-live-mutation checks.

Broadcast recording continues from the existing encoded output and includes
the browser mix and any measured recovery silence. It does not record private
cue/preview audio. Direct monitoring is pre-master and may sound different from
the mastered/encoded listener output; it is intentionally not an off-air monitor.

### Deferred slices and compatibility

Later slices handle scheduled events/ads, inserted speech and prepared editions,
full DJ controls/MIDI/scratch, 48 kHz conversion and additional browser rates/platforms, adaptive clock
correction, proxy-hosted local access, server-crash accounting guarantees and
simultaneous speaker monitoring. No remote DJ support or new stream format is
implied. Returning to automation remains available without those features.

Output configuration migration is deliberately deferred: the prototype requires
an existing stream configuration and writes no output migration or new default.
The later release must separate ownership/listening from stream and server-speaker
settings, preserve each old Browser/Server/Radio-stream selection's intended
outputs, preserve combined stream/speakers, and integrate with Settings Save/
Discard. Test migration and rollback before changing defaults. Legacy browser
playback continues unchanged outside local-control sessions.

## Alternatives, consequences and revisit conditions

- Selected: station-owned lease/controller plus a distinct PCM input at the
  existing pre-master boundary. It keeps one performance and all broadcast
  outputs, but adds a clock crossing and explicit occurrence protocol.
- Reusing external `LiveSource` unchanged is smaller but encodes/decodes needlessly,
  expires intentional silence, buffers too much and applies incompatible history
  and scheduling policy. Share bounded lifecycle ideas, not those semantics.
- A server-to-browser PCM monitor or server gesture replay cannot broadcast the
  browser's actual performance and repeats #543's rejected ownership approach.
- WebRTC adds negotiation, codec/jitter behavior and dependencies unnecessary for
  direct loopback PCM. MediaRecorder chunks introduce codec/chunk delay. WebSocket
  is simpler here, but TCP head-of-line stalls require bounded failure recovery.
- A dedicated local listener would simplify peer provenance but adds port, origin,
  cookie and lifecycle rules, and cannot identify a header-stripping local relay.
  Use the captured-peer wrapper with direct-only admission for the prototype.
- Immediate AudioBuffer replacement might improve cue scheduling, but duplicates
  deck lifecycle and adds decoded-memory bounds before measuring existing sources.
  Keep reusable gains/effects and measure media elements first.
- Full scheduling integration and durable transactional journaling would expand
  the first slice substantially. Explicit admission refusals and documented crash
  limits keep the experiment bounded without silently losing programme content.

Revisit the prototype bounds after 44.1 kHz measurements, loaded runs and
failure injection. Frequent safe returns under ordinary load indicate that clock
correction or a smaller/higher-quality deck layer belongs in the next slice.
Do not turn those failures into silently larger queues or relaxed ownership.

## Verification plan and evidence limits

New behavioral evidence remains pending until executed. Phase 1 ran architecture-document
checks and review against source; it makes no latency, audibility or test-pass claim.
Use [contributor check selection](../../CONTRIBUTING.md#choose-checks-for-the-change),
[accessibility guidance](../accessibility-testing.md), and
[server playback operations](../reference/operations/server-playback.md) during
implementation. New selectors below are plans, so their method is `gap` until
tests exist with reciprocal annotations. Never map nonexistent tests as automated.

The agent can produce the following without a human tester:

1. Unit and integration checks with fake monotonic clocks: simultaneous claims,
   same-cookie tabs, expiry, release/reconnect, stale buffered frames/events,
   revocation without incoming messages, station isolation, malformed/flooded
   input, intentional zero PCM, queue revisions/repeated files, and admission
   refusals for every unsupported content path. Exercise real socket peer and
   proxy handling separately from unit authentication doubles.
2. A disposable native station with synthetic indexed stereo sources and real
   master/FFmpeg encoder/HTTP stream. No external relays, paid services or personal
   configuration. Headless Chromium/Playwright uses
   `--use-fake-ui-for-media-stream --autoplay-policy=no-user-gesture-required`;
   add fake media devices only for microphone/preview-path tests. The programme
   comes through actual browser decks, effects, worklet and socket, not a Python
   feed substitute. Record browser/OS/CPU, sample rates, selected virtual sink,
   load and exact revision. Mocked hardware/model startup is disclosed.
3. Feed distinct left/right coded tones, swept frequencies and timestamped burst
   markers through the engine. Tee the captured reference and receipt timestamps
   in the harness; decode the continuously received broadcast with FFmpeg to PCM.
   Correlate windows against the reference to report delay distribution, missing
   markers, silence/dropout lengths, channel swaps, transition envelopes, drift
   in ppm and buffer occupancy. Align browser `performance.now`, audio clock and
   server monotonic measurements with repeated ping samples and report alignment
   uncertainty. Separate browser-to-bus delay from master/encoder/HTTP buffering.
4. Compare unprocessed and actual configured-master runs. Use identifiable
   low-level markers and normalized correlation tolerant of gain/codec changes;
   do not demand byte equality after lossy encoding or infer delay from a lone
   steady sine. Verify the detector with known injected delay, missing packet,
   reversed channels and rate error before trusting a clean result. Synthetic
   errors must be detected, not averaged away.
5. Measure 100 warm keyboard/pointer fader actions with a known continuous source.
   Record handler time, scheduled AudioParam frame and observed gain envelope at
   a render-worklet probe; delay control-network replies deliberately. Report
   p50/p95/max render-clock response, media play/seek readiness and startup/preload
   separately. This measures rendered samples, not physical gesture-to-ear latency.
6. Use a second isolated browser context and a second same-cookie tab to race
   acquisition and submit stale controls/media errors. Run A, B, A occurrences,
   retry accounting events and lose acknowledgements; assert one queue commit and
   one history identity per heard occurrence. Preview unique markers through every
   private path and require their absence from captured/broadcast PCM while monitor
   volume/mute changes leave feed level unchanged.
7. Keep one HTTP stream response and decoder open across takeover, a browser
   transition and return. Record encoder/master object identities, response count,
   bytes and decoded gaps; no hidden listener reconnect qualifies. Kill the owning
   context, suspend its AudioContext, stall its main thread/socket, revoke its
   session and inject queue/content races separately. Report detection delay,
   recovery gap/overlap and queue continuation for each failure.
8. Run at least 30 minutes at 44.1 kHz, idle and under controlled
   CPU load, collecting counters and memory/queue high-water marks. Inject signed
   clock error to verify bounded safe return. A failure within normal-load runs
   remains a result to fix or review, not a reason to claim indefinite operation.
   Keep full result JSON, decoded audio, correlation plots and logs in ignored
   test artifacts; characterize candidate thresholds before proposing latency goals.
9. Drive native buttons by keyboard; inspect accessible names, disabled reasons,
   focus order, once-only status and stale-station responses with Playwright/axe.
   Use the available screen-reader bridge to drive installed NVDA and capture
   actual speech in a headed browser, following the repository's existing
   agent-produced sampling precedent. Retain versioned speech/focus transcripts.
   No person is required to test keyboard navigation or capture NVDA speech.

Only physical output routing, hardware/driver gesture-to-sound latency and
subjective listening quality need equipment or a listener beyond that harness.
An agent can measure the first two when a loopback capture device is available;
without it, leave those checks pending. Human listening is needed for subjective
clicks, distortion, perceived cue responsiveness and monitoring comfort that
signal metrics do not establish. Do not require the owner to test by hand.
Unavailable NVDA/device access is an environment gap, not proof that a human is
intrinsically required. Results for one browser/device/reader do not certify others.
Existing ADR 14 manual obligations remain unchanged for that separate feature.

## Amendment, 2026-10-30: Phase 2a implementation and measured limitations

The implemented preparation lease, locality checks and PCM ring live in
[local_programme.py](../../src/autodj/local_programme.py),
[local_programme_routes.py](../../src/autodj/local_programme_routes.py) and
[local_programme_audio.py](../../src/autodj/local_programme_audio.py).
The independent [capture worklet](../../src/autodj/static/programme-worklet.js)
and [PCM sender](../../src/autodj/static/modules/programme-transport.js) are not
attached to the production browser graph yet. The bus selector is internal;
there is no activation or administrative Return route in this slice. Queue,
history, unsupported programme-content admission and output-setting handoff
remain in steps 4 through 6. Preparation excludes external live and microphone
claims but leaves automation running. Preparation accepts only zero PCM, discards
it, and grants cumulative frame credit. Consumption credit is also emitted
without inbound packets, at the socket watchdog tick (at most 100 ms); nonzero preparation input is rejected.
Loss or expiry clears the reservation and buffered input without changing the
existing automation queue. Thus partial implementation cannot broadcast an
unaccounted browser performance.

The installed Uvicorn automatic sans-I/O receiver ignores `ws_max_queue`.
Opt-in startup explicitly selects the installed bounded `websockets` receiver,
64 KiB assembled messages and four queued messages. Opt-out keeps all original
startup options. The bounded receiver is deprecated upstream; dependency upgrades
must retain the queue bound or provide a tested bounded replacement. Selecting
an unbounded receiver merely to remove a warning is not an acceptable substitute.
Tests exercise fragmented oversized messages on a real socket. Header-stripping
local relay deployments remain unsupported and cannot be detected reliably.

The runnable existing-engine audit uses actual HTMLMediaElement decks and the
existing gain graph with a 44,100 Hz context, a worklet render probe, synthetic
stereo input and deliberately delayed control replies. Its initial Windows/
headless Chromium run measured 100 local gain changes at p95 2.90 ms and maximum
8.71 ms on the audio clock, with 2.60 ms seek readiness. Audio-clock quantization
is one 128-frame quantum (2.90 ms); zero measured intervals are not zero acoustic
latency. These initial direct-setter samples precede the keyboard-driven audit;
retain both distinctions in the implementation evidence. A subsequent trusted
keyboard-event run through a fixture handler calling the real engine measured
p95 below one audio quantum, maximum 2.90 ms and seek readiness 1.30 ms on the
same headless platform. This is not the future production Take/Return UI or an
acoustic latency measurement. The rejected `play()`
case advanced the active deck despite failure. Step 4 must gate ownership/start
receipts on observed readiness before exposing the workflow. Successful seek,
gain and crossfade measurements do not currently justify replacing media elements.

The internal selector test uses Python-generated PCM, a deterministic gain master,
the real FFmpeg encoder, one continuously open HTTP response and decoded stereo
correlation. It demonstrates input selection, held automation and output object/
connection continuity, not browser feed latency or queue handoff correctness.
The complete browser-to-broadcast capture, monitoring split, receipt accounting,
recovery and loaded 30-minute drift trials remain pending. 48 kHz conversion is
deferred. Existing accepted private-review, automation and external-live policies
remain intact.

## Amendment, 2026-10-30: Phase 2b implementation and evidence limits

Phase 2b adds the browser owner, independent monitoring, two server-issued song
occurrences, consumption receipts and accessible controls. It retains the opt-in,
direct native loopback admission, 44,100 Hz rate and existing output lifetime.
The browser uses the existing media-element decks and gain graph for a bounded
250 ms linear transition. A rejected `play()` leaves the active deck unchanged;
local starts require media readiness and an acknowledged future render marker.

The four worklet buffers also bound the sender queue. Real Chromium can deliver
several startup packets together, so the sender spaces them at no more than
125 packets/s without adding buffers or relaxing the receiver burst limit.
Worklet starvation silences the monitor and returns to automation. Running axe
on an active owner exhausted this deliberately small budget; accessibility audits
run with the controls visible before takeover. This is explicit load evidence,
not a long-run stability claim.

Takeover revalidates queue/source and automation render generation under the bus
boundary. The generation catches an automation advance even when a queue repeats
the identical library object. Programming mutations use the same fence in route,
bridge, player and settings entry points. Unreserved queue edits preserve the
reserved prefix. Local speaker gating changes no saved output setting.

An explicit return first prepares the next unheard automation song while the
owner continues feeding. The server enters `returning`, refuses new starts and
acknowledges readiness before the browser schedules its fade. The final release
carries the fade's capture frame. The server waits for
that frame to be consumed before revoking the feed. Unexpected loss discards
unconsumed audio immediately. Both drain heard receipts off the audio thread,
retain the live reservation through recovery, and render the next unheard song
with its queue provenance intact. A failed flush retains receipts and blocks new
claims; process-crash transactional guarantees remain deferred.

The automated prototype audit uses a real Player, station SQLite ledger, mix bus,
FFmpeg encoder and one HTTP listener. Its catalog and media are synthetic. Its
page shell imports production markup, owner controls and engine modules; normal
application bootstrap and every surrounding product flow are not established by
that shell. Correlation is restricted to the first occurrence so a later replay
cannot masquerade as clock drift. A calibrated 5 kHz private marker lies outside
the test programme's passband. The audit measures browser-to-bus alignment,
decoded PCM, monitor independence, channel order, loss recovery and connection
continuity separately. Exact commands and platform results accompany the local
handoff evidence; no acoustic latency or universal performance claim follows.

The attempted thirty-minute idle and loaded trials stopped early: initial runs
hit the receive rate limit, and final runs exhausted the four-buffer transfer
pool. Receive scheduling can bunch otherwise paced packets beyond the approved
two-packet burst; browser callback delays can exhaust the capture pool. The
prototype retains those bounds and reports safe recovery. Sustained operation
remains an unresolved evidence gap. Other browsers
and third-party mastering latency also remain outstanding. Physical routing and
acoustic latency need a device/loopback setup; subjective clicks and sound quality
need a listener. The reviewer owns NVDA speech capture. Neither axe nor DOM tests
establish a screen-reader speech result or complete WCAG conformance.

The final review found that the legacy queue picker consumes entries. Reservation
now uses its existing peek context, including the pending occurrence, and leaves
the sole queue unchanged until a consumed start. Acquisition resets the legacy
programme pause. Pause markers are serialized, pending pause timers are cancelled
on resume, and stale crossfade completions cannot touch a reused deck.
Acquisition pins the station epoch before asynchronous work. Station switching
waits for automation recovery, and administrative Return includes the observed
epoch and generation. Private reviews and recording controls stay outside the
programming mutation fence. Listen remains unavailable throughout acquisition.

Render-clock samples are taken at the worklet's process boundary and refreshed
after media readiness before scheduling a marker. A media graph becoming active
can invalidate a clock sample taken while its source was idle. The short audit
also exercises context suspension, stalled PCM with continuing heartbeats and
revocation of a real signed device session, separately from closing the owner.
The monitor watchdog uses 750 ms of render time and renews only on advancing
cumulative credit. An occupancy window outside 20–160 ms for five seconds returns
control before the 200 ms hard cap; these limits do not correct clock drift.

## Amendment, 2026-10-30: transfer-pool endurance correction

The earlier uninstrumented trials reported capture-pool exhaustion as the
ownership-loss reason at 138.136 seconds idle and 44.378 seconds loaded. The
four-slot pool provides less transfer headroom than the 200 ms jitter budget.
Test-only instrumentation subsequently measured completed recycle round trips
of p50 11.61 ms, p99/max 14.51 ms on the worklet audio clock (2.90 ms quantum),
but those short runs ended on socket/rate failure. Their exhaustion messages
arrived after teardown; they are not evidence that the pool caused those losses.
No nearby PerformanceObserver long task of at least 50 ms was observed. The
harness now excludes post-teardown worklet messages from causal diagnostics.

Increase the fixed pool to 32 slots: 320 ms and 112,896 bytes of stereo float32
storage. This covers the 200 ms jitter budget with transfer/recycle margin.
The sender queue holds these same loans, without a second allocation pool.
The processor never waits or grows storage, and exhaustion remains a reported
failure. This capacity is not a guarantee that a 320 ms main-thread stall keeps
the broadcast audible: server prefill, underrun policy and 200 ms credit/cap
remain independently enforced. Every sender
failure ends the session and disconnects capture; a failed recycle post now
does the same. Successful sends return the original buffer.

Increasing the pool alone did not fix endurance. An idle trial ended at 388.240
seconds on the receiver's rate limiter. Its last accepted intervals were 22.295
ms and 1.643 ms: the two-packet token bucket could reject ordinary 100 Hz traffic
batched by scheduling. Completed recycle p99 was 72.56 ms and maximum 75.46 ms;
the subsequent 32-slot exhaustion was again a consequence of teardown.
Replace the two-packet arrival burst with 20 packets, tied to existing PCM
capacity, and enforce the original one-second byte/message ceiling separately.
The rolling ceiling is 127 messages and 1.25 times the PCM byte rate plus two
framed packets; burst tokens replenish at 125 packets/second and 1.25 times the
PCM byte rate. Control-message and WebSocket assembly limits are unchanged.
No PCM storage or consumption credit increases. Preparation silence remains
rate-limited even though it consumes no PCM storage. The rate-window timestamp
deque is bounded by the message/byte ceiling (at most 125 entries for this
fixed packet format). Unit tests exercise delayed arrival batches, exhaustion
of the unchanged credit cap, and a flood that tries to spend burst headroom
inside one second. The existing instant-flood test moves its rejection from
packet 3 to packet 21 because that arrival-burst policy intentionally changed.

The loaded diagnostic also exposed cumulative sender delay: at 503 seconds,
17 loans were outstanding and completed recycle p50/p99/max were
84.17/153.83/165.44 ms, without a nearby long task. A relative 8 ms timer cannot
catch up when the platform rounds wakeups to 10 ms. A deterministic test
reproduced six packets of permanent backlog after a 60 ms interruption while
capture continued. The sender now replenishes a two-packet token bucket from
monotonic elapsed time, at one token per 8.2 ms. This includes header overhead
within the byte budget, catches up after delayed callbacks, and retains fixed
transfer storage and the receiver's independent credit/rate bounds. It does
not discard, resample or skip programme frames.

Instrumentation is confined to the Playwright fixture and generated worklet
source; it is absent from ordinary serving. Preserve the earlier failures when
reporting subsequent thirty-minute idle/load trials and active-owner axe results.
The measured trial reports are retained under `.codex-tmp/541/` at handoff.

The corrected prototype completed separate 1,800-second idle and loaded trials
on Windows with Chromium 156.0.8078.4. Load was 6 ms of main-thread work every
50 ms. Maximum outstanding loans were two idle and three loaded. Completed
recycle round-trip p50/p99 were 2.90/2.90 ms in both trials; maxima were 14.51 ms
idle and 23.22 ms loaded. PCM high-water was 4,311 frames in both. Decoded-stream
correlation detected no 10 ms dropouts or channel reversal and approximately
zero drift over those recordings. One HTTP connection survived takeover,
all song transitions and explicit Return, with encoder generation unchanged.
These are platform observations, not acoustic or unlimited-duration guarantees.

Large diagnostic exports originally disturbed the owner after the measured
window. The harness now returns to automation before exporting arrays; both
final trials exercised that sequence. An active-owner axe run still exhausted
the fixed pool, despite reporting no light-theme violations. Keep expensive
audits outside performance; this is an explicit limit, not an active-owner axe
pass. Pre-takeover audits cover both themes. NVDA and hardware obligations remain
separate. The endurance procedure uses the README's platform/performance method;
it is scripted and needs no human listener.

## Amendment 2026-10-30: review round 2 corrections

Ordinary browser playback retains synchronous crossfade ramp scheduling in the
initiating tick. A rejected incoming `play()` cancels the scheduled ramps and
pauses the standby deck without advancing it. Only local-owned playback waits
for observed media readiness before scheduling. When the server setting is
enabled, the initial page bootstrap requests the shared 44,100 Hz AudioContext
before any audio initializer can create a device-rate context. Disabled
installations retain the original context construction.

Admission and marker work run off the ASGI event loop. The station controller
owns the control-message token bucket across reconnects. Every admitted
handshake releases its slot, including initialization failures. Cancellation
settles acquisition and cleanup workers before ending the socket, and delayed
recovery checks the observed generation before changing performance state. Parser
recursion failures close with unsupported-data code 1003. Receipt service runs
at most once per 100 ms tick, with an additional service opportunity after a
control marker. Occurrence signatures are cached at reservation and checked
again before takeover outside the queue and bus locks. The bus commit checks
published generation/revision values without file access. Fade-down still
delivers pending music and liner callbacks outside the bus lock.

Recovery does not hold the performance lock while waiting for the renderer.
Automation resumes only after its replacement cursor is ready. A bounded wait
that expires retains the closed local input as silence, its reservation, and
an explicit recovery reason; a subsequent Return reuses an unchanged target's
pending render.
Consumption completes pre-arm load-failure receipts from discarded silence.
An audible receipt skipped by a consumed span fails loudly into recovery.

Native mode with no access token configured permits anonymous loopback claims,
matching the rest of the native API. This is already the anonymous mode
described above: actual peer, exact same origin, no forwarding headers, and
the socket-bound capability remain mandatory. Configuring an access token
requires a valid native browser session. This does not introduce a separate
authentication bypass or permit proxy ingress.

The review API has no live-placement guard. Remove its blanket programming-write
exemption during local ownership; review reads and private playback remain
available, and recording controls retain their separate exemption. The prior
test that treated review writes as exempt is narrowed to recording controls.

The real Chromium prototype audit runs as a standalone step in the existing
Playwright stream-audit CI job, after Chromium installation. Its report location
comes from `LOCAL_PROGRAMME_AUDIT_ROOT`, defaulting to
`test-results/local-programme-prototype`. CI uploads that directory with the
stream-audit reports. Python tests no longer invoke Node or read ignored local
reports. The added CI step has a three-minute bound; its actual Linux duration
must come from CI evidence. Scripted browser evidence remains a platform
procedure under this ADR's lifecycle, separate from native test mappings.

## Amendment 2026-10-30: final review corrections

The final review replaces the ordinary-playback requirement in the preceding
amendment. When `localProgrammeOwned` is false, `startCrossfade` retains the
behavior and ordering of the pre-prototype implementation: clear the next-track
cache and request advancement synchronously, schedule the normal timer finish
without waiting for `play()`, and keep the original rejected-play handling.
Readiness, settlement and rejection cancellation apply only to local-owned
playback. This preserves existing installations even when a media promise never
settles. The original tests no longer need added promise flushes.

The route publishes Return intent and clears its completion event before
queuing the preparation worker. Recovery joins or refuses that preparation;
it cannot start a second render in the scheduling gap. Performance state keeps
the selected return occurrence across retries, including a song picked when no
unstarted reservation remains. Inline controller lease checks remain cheap;
claim, marker, preparation and recovery work still run off the event loop.
Cancellation retains worker lifetime and logs a worker failure before propagating
the outer cancellation.

A refused Return refreshes the authoritative HTTP status before rendering the
controls again. Disabled Take explains preparation, return and recovery states
separately. The status GET and administrative Return POST deliberately use the
global authentication and Origin policy, without the socket's locality check.
Return is a safety action, available under the same anonymous-write policy as
other native API writes when no access token is configured. It still fences the
observed generation; these HTTP routes cannot acquire programme ownership.

The queue exemption covers add, remove and reorder. Each reaches the bridge's
reservation validator before mutation; Clear uses reorder with an empty list.
Play next, Play now and listener-request decisions are outside that exemption
and are rejected earlier by the station programming fence. Queue-tail edits
remain available when they preserve the reserved prefix.

The standalone audit preserves its original failure when conversion, FFmpeg
version lookup or cleanup also fails, and still writes its JSON report. Its CI
timeout is eight minutes pending measured Linux timing; local runtime belongs
in the execution report rather than an assumed CI duration.

## Amendment 2026-10-31: bounded preparation credit waits

CI on a slower host exposed a preparation failure in the sender: its initial
8,820-frame credit could run out while the socket route awaited control-thread
work or delivery of a consumption acknowledgement. Acquisition already finishes
`LocalPerformance.prepare` before sending the grant, and the worklet waits for
that grant. `LocalPCM.accept_packet` already verifies and discards pre-arm zero
PCM, replenishing its cumulative credit. Increasing the server ring or granting
credit for TCP delivery would not address delayed acknowledgement processing.

During preparation only, the browser now pauses its paced sender when cumulative
credit is unavailable. It retains the unsent packets, in order, in the existing
32 transferable worklet buffers. No new storage, skipped frames, synthetic
acknowledgements or additional server credit are permitted. A credit message
resumes the sender at its existing two-packet burst/rate bound. Exhausting the
fixed pool still ends the session explicitly; this is bounded tolerance, not
support for arbitrarily slow preparation. The 500 ms server PCM timeout, 750 ms
monitor watchdog and ownership deadlines remain unchanged.

Each server credit message also reports whether the bus has activated local
programme. On the first `local` credit the browser permanently ends the
preparation grace for that grant. Further credit exhaustion fails the feed as
before. The receiver still holds at most 8,820 frames (200 ms); its prefill,
underrun, rate and drift checks are unchanged. This chooses a bounded preparation
wait over enlarging the ring or discarding audible programme. The transferable
pool can stage preparation packets while acknowledgement processing is delayed,
but it can never authorize sending beyond the server's cumulative credit.

Deterministic browser tests withhold credit during 450 ms of capture, verify that
only 200 ms crosses the socket, and then verify ordered sending and recycling.
They also verify the fixed pool failure and fatal on-air exhaustion. The socket
regression delays server preparation for 300 and 600 ms, confirms capture cannot precede
the grant, and checks the preparation/on-air state on credit replies.

The prototype audit can opt into Chromium CPU throttling through
`LOCAL_PROGRAMME_CPU_RATE` (1 by default, 1?20 accepted). It applies the rate to
each owning page and records it in the report. Its fault trials wait for both
server recovery completion and an enabled Take control before reacquiring.
This is browser CPU evidence, not a simulation of Linux server scheduling.

## Amendment 2026-10-31: consumption-driven credit publication

The subsequent Linux CI failure occurred after activation, with the input ring
near its 200 ms cap. The recorded bus kept its 20 ms cadence during the feed;
packet arrivals also continued. Credit nevertheless depended on the socket
control loop, including its 100 ms receive timeout and awaited control workers.
The artifacts do not contain outbound credit timestamps, so they cannot isolate
every scheduling delay. They do establish that increasing the PCM cap or blaming
a sustained render stall would not address the observed startup headroom.

The receiver now wakes a dedicated socket credit publisher whenever a read frees
frames. The render thread schedules at most one pending event-loop notification;
notifications coalesce until the publisher acknowledges them. One fixed task
publishes cumulative credit independently of receive, marker and accounting
work. Socket sends share one asynchronous lock, and repeated notifications never
grant credit twice. Shutdown detaches the callback and cancels/joins the task.
The publisher checks lease, session, station and input health before extending
credit. It does no accounting or file I/O on the render thread.

Takeover service also runs as soon as the existing 60 ms prefill is ready,
instead of waiting up to another 100 ms service interval. Active accounting
keeps its existing cadence. This avoids needlessly filling most of the 200 ms
ring before the bus can begin consuming it. The ring, pool, packet rate,
watchdogs and fatal on-air credit-exhaustion policy remain unchanged.

Regression tests hold a control worker blocked while consuming a bus block and
require a credit response before releasing that worker. Separate tests hold the
service clock below the poll deadline and verify immediate prefill activation,
and verify coalescing/detachment of render notifications. The audit fixture
records consumption and outbound credit timestamps for subsequent diagnosis.

## Current contracts

Automated rows identify the bounded prototype checks. They do not establish
other browsers, physical routing, screen-reader speech or unlimited-duration performance.
ADR-0020-18 records the platform endurance procedure separately from the reproducible
short signal audit in ADR-0020-09. Migration beyond the opt-in remains deferred.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0020-01` | Full on-air ownership fences queued audio, events and late callbacks across acquisition, expiry, reconnect, revocation and stop. | automated | `tests/integration/test_local_programme.py::test_administrative_return_rejects_an_old_observed_generation`; `tests/jsmodules/local-programme.test.js::cancels acquisition when the station changes during context preparation`; `tests/jsmodules/local-programme.test.js::does not apply old render acknowledgements to a replacement owner`; `tests/jsmodules/local-programme.test.js::does not report an old pause failure against a replacement owner`; `tests/jsmodules/local-programme.test.js::ignores an old worklet load failure after acquisition was replaced`; `tests/jsmodules/local-programme.test.js::serializes pause markers and cancels the old pause timer before resume`; `tests/unit/test_local_programme_playout.py::test_automation_advance_invalidates_preparation_even_for_identical_queue_objects`; `tests/unit/test_local_programme_playout.py::test_internal_bridge_mutations_are_fenced`; `tests/unit/test_local_programme_playout.py::test_loss_keeps_live_reservation_until_recovery_finishes`; `tests/unit/test_local_programme_playout.py::test_settings_owner_cannot_bypass_programming_fence`; `tests/integration/test_local_programme_control.py::test_review_mutation_is_fenced_without_a_live_placement_guard`; `tests/unit/test_local_programme_playout.py::test_handoff_rechecks_generation_after_prevalidation`; `tests/integration/test_local_programme_cancellation.py::test_delayed_administrative_recovery_preserves_replacement_owner` |
| `ADR-0020-02` | Local admission uses preserved raw loopback peer plus native session/Host/Origin checks; proxy deployment is refused and ordinary forwarded client/scheme behavior is preserved. | automated | `tests/integration/test_local_programme.py::test_real_socket_locality_and_proxy_refusal`; `tests/integration/test_local_programme.py::test_socket_denies_forged_locality`; `tests/unit/test_local_programme.py::test_locality_requires_raw_loopback_same_origin_and_no_proxy`; `tests/unit/test_local_programme.py::test_raw_capture_preserves_ordinary_proxy_semantics` |
| `ADR-0020-03` | 44,100 Hz stereo input enforces framing, headroom, sequence, credit, bounded PCM storage and stall/underrun limits; zero preparation PCM earns credit without entering the bus. | automated | `tests/integration/test_local_programme.py::test_consumption_credit_arrives_without_another_packet`; `tests/integration/test_local_programme.py::test_preparation_pcm_credit_stall_and_malformed_release`; `tests/unit/test_local_programme_audio.py::test_framing_credit_and_timeout_bounds`; `tests/unit/test_local_programme_audio.py::test_prefill_underrun_and_revocation_never_replay`; `tests/unit/test_local_programme_audio.py::test_preparation_silence_rate_flood_and_grant_fence`; `tests/unit/test_local_programme_audio.py::test_rejects_malformed_without_advancing`; `tests/unit/test_local_programme_audio.py::test_sustained_high_occupancy_returns_before_the_hard_cap`; `tests/unit/test_local_programme_audio.py::test_receive_jitter_batch_keeps_the_same_pcm_credit_bound`; `tests/unit/test_local_programme_audio.py::test_receive_jitter_allowance_does_not_expand_one_second_rate_budget`; `tests/integration/test_local_programme_control.py::test_control_bucket_survives_reconnect`; `tests/integration/test_local_programme_control.py::test_nested_control_closes_with_unsupported_data`; `tests/integration/test_local_programme_control.py::test_slow_preparation_grants_before_capture_and_reports_on_air_credit`; `tests/unit/test_local_programme_audio.py::test_capture_grant_starts_the_unchanged_stall_deadline`; `tests/integration/test_local_programme_control.py::test_consumption_credit_is_sent_while_control_worker_is_blocked`; `tests/integration/test_local_programme_control.py::test_ready_prefill_does_not_wait_for_the_service_poll`; `tests/unit/test_local_programme_audio.py::test_consumption_wakeups_coalesce_and_detach` |
| `ADR-0020-04` | The browser performs locally; monitor level and every private preview stay outside broadcast gain/content. | automated | `tests/integration/test_local_programme.py::test_local_ownership_preserves_private_review_and_recording_controls`; `tests/jsmodules/audio-engine-requests.test.js::clears the legacy programme pause when local ownership starts`; `tests/jsmodules/audio-engine-requests.test.js::creates a 44100 Hz programme context and leaves monitor gain after capture`; `tests/jsmodules/local-programme.test.js::does not open a deck or emit started when play rejects`; `tests/jsmodules/local-programme.test.js::renews the monitor watchdog for at most 750 milliseconds`; `tests/jsmodules/local-programme.test.js::waits for an observed render clock before preparing the first audible marker`; `tests/jsmodules/programme-worklet.test.js::acknowledges future markers before PCM and silences only monitor on watchdog expiry`; `tests/jsmodules/programme-worklet.test.js::samples the capture clock at a render boundary instead of a port callback`; `tests/jsmodules/stream-mode.test.js::does not start encoded monitoring while the browser owns the programme`; `tests/unit/test_sound_output.py::test_local_monitor_gate_preserves_saved_volume_and_mute`; `tests/integration/test_server_recent.py::test_index_bootstraps_local_programme_rate_before_scripts`; `tests/jsmodules/audio-engine-requests.test.js::uses 44100 Hz for the first shared context when local programme is enabled` |
| `ADR-0020-05` | Takeover/return retains master, encoders, recording and listener connections and emits only the selected programme input. | automated | `tests/unit/test_local_programme_audio.py::test_public_handoff_ramps_at_boundaries_and_keeps_outputs`; `tests/unit/test_local_programme_audio.py::test_handoff_prevalidation_runs_outside_bus_lock`; `tests/unit/test_local_programme_audio.py::test_handoff_starts_pcm_without_holding_bus_lock`; `tests/unit/test_local_programme_audio.py::test_local_fade_drains_music_and_liner_callbacks_outside_bus_lock` |
| `ADR-0020-06` | Heard occurrence receipts commit once to the single queue and history, including repeated files and lost acknowledgements. | automated | `tests/unit/test_local_programme_events.py::test_consumption_receipts_survive_revocation_and_retry`; `tests/unit/test_local_programme_events.py::test_only_reserved_occurrences_ordered_future_frames_and_bounded_pending`; `tests/unit/test_local_programme_events.py::test_same_song_occurrences_are_distinct_and_ended_releases_slot`; `tests/unit/test_local_programme_playout.py::test_queue_edits_preserve_reserved_prefix_but_allow_unreserved_tail`; `tests/unit/test_local_programme_playout.py::test_real_player_reservation_never_pops_the_only_queued_song`; `tests/unit/test_local_programme_playout.py::test_reservations_do_not_dequeue_and_consumption_counts_once`; `tests/integration/test_local_programme_control.py::test_pcm_packet_rate_does_not_drive_service`; `tests/unit/test_local_programme_audio.py::test_prearm_load_failure_completes_from_discarded_silence`; `tests/unit/test_local_programme_events.py::test_skipped_marker_fails_without_silently_losing_receipt`; `tests/unit/test_local_programme_playout.py::test_active_service_uses_cached_admission_and_source_signatures`; `tests/unit/test_local_programme_playout.py::test_source_signatures_are_read_without_holding_queue_lock`; `tests/integration/test_local_programme_queue.py::test_every_queue_write_preserves_local_reservations` |
| `ADR-0020-07` | Failure returns to the next unstarted occurrence without restarting outputs; unsupported speech/schedules are refused without mutation. | automated | `tests/unit/test_local_programme_playout.py::test_real_player_return_keeps_queue_occurrence_until_consumption`; `tests/unit/test_local_programme_playout.py::test_recovery_preserves_unheard_next_and_blocks_claim_on_ledger_failure`; `tests/unit/test_local_programme_playout.py::test_unsupported_features_and_changed_source_fail_closed`; `tests/unit/test_local_programme_playout.py::test_voluntary_return_prepares_automation_while_local_input_remains_live`; `tests/integration/test_local_programme_control.py::test_slow_return_does_not_block_other_requests`; `tests/unit/test_local_programme_playout.py::test_real_player_timeout_keeps_local_bus_and_reuses_pending_render`; `tests/unit/test_local_programme_playout.py::test_recovery_render_wait_releases_performance_lock_and_serializes_retries`; `tests/unit/test_local_programme_playout.py::test_return_timeout_retains_safe_silence_and_reservation_for_retry`; `tests/unit/test_local_programme_playout.py::test_stop_during_recovery_wait_does_not_resume_the_bus`; `tests/integration/test_local_programme_cancellation.py::test_queued_return_preparation_blocks_administrative_recovery`; `tests/unit/test_local_programme_playout.py::test_recovery_retry_reuses_picked_song_without_unstarted_occurrences` |
| `ADR-0020-08` | Take/Return controls work by keyboard with stable focus, names, unavailable reasons and once-only status announcements. | automated | `tests/jsmodules/local-programme-controls.test.js::hides disabled installations and announces each change once without moving focus`; `tests/jsmodules/local-programme-controls.test.js::announces an acquisition refusal once without intermediate state messages`; `tests/jsmodules/local-programme-controls.test.js::announces only the final ownership state during a clean return`; `tests/jsmodules/local-programme-controls.test.js::explains unavailable local control actions when activated`; `tests/jsmodules/local-programme-controls.test.js::reports a disconnected return once and confirms the final server state`; `tests/jsmodules/local-programme-controls.test.js::explains why Take is unavailable while preparing`; `tests/jsmodules/local-programme-controls.test.js::explains why Take is unavailable while recovering`; `tests/jsmodules/local-programme-controls.test.js::explains why Take is unavailable while returning`; `tests/jsmodules/local-programme-controls.test.js::refreshes authoritative automation state after Return is refused`; `tests/jsmodules/local-programme-controls.test.js::refreshes authoritative local state after Return is refused`; `tests/jsmodules/local-programme-controls.test.js::keeps Preparing while the server still reports automation before the claim lands` |
| `ADR-0020-09` | Render response, feed delay, dropouts, drift and recovery have separate reproducible measurements and stated limits. | manual | Run `node tests/playwright/local_programme_prototype_audit.mjs` with Chromium, FFmpeg and the locked Python environment. The Playwright stream-audit CI job runs this command and uploads its report directory. Set `LOCAL_PROGRAMME_AUDIT_ROOT` to choose the artifact path; otherwise inspect `test-results/local-programme-prototype/audit.json`. Require coded stereo transition, preview isolation, monitor mute isolation, second-context refusal, owner-loss recovery and one HTTP stream across takeover/return. Report environment and measured delay/dropouts/drift; do not infer hardware latency. |
| `ADR-0020-10` | Physical devices and subjective audibility are described only from actual evidence. | manual | Pending: use a hardware output/loopback capture to measure routing and latency; record device/driver/browser/sample rates and measured gaps. A listener evaluates clicks, distortion and responsiveness. No human participation or hardware result is claimed. |
| `ADR-0020-11` | Prototype leaves existing output configuration intact; broader migration preserves intended stream and speaker outputs. | gap | Prototype speaker-gate evidence is mapped under ADR-0020-04, and output continuity under ADR-0020-05. The broader output-default migration and its rollback cases remain deferred to a later #541 slice. |
| `ADR-0020-12` | Local programme preparation is strictly opt-in, defaults off and installs no route when disabled; absent transport provenance fails closed. | automated | `tests/integration/test_local_programme.py::test_disabled_has_no_routes_and_missing_provenance_fails_closed`; `tests/unit/test_local_programme.py::test_opt_in_is_strict_and_off_by_default`; `tests/integration/test_server_recent.py::test_index_preserves_opt_out_audio_bootstrap`; `tests/jsmodules/audio-engine-requests.test.js::preserves default context construction when local programme is disabled`; `tests/jsmodules/audio-engine-requests.test.js::preserves ordinary crossfade ordering with pending play`; `tests/jsmodules/audio-engine-requests.test.js::preserves ordinary crossfade ordering with rejected play` |
| `ADR-0020-13` | The internal input selector holds automation, excludes the existing mix and retains master/encoder/output identity and one HTTP stream connection. | automated | `tests/integration/test_local_programme.py::test_internal_selector_keeps_master_encoder_and_http_connection`; `tests/unit/test_local_programme_audio.py::test_selector_holds_automation_and_bypasses_eq_liners` |
| `ADR-0020-14` | Standalone browser capture has at most 32 transferable packets; PCM sender bounds socket backlog and uses cumulative consumption credit. Preparation credit waits use only that pool; on-air exhaustion fails. Each loan is recycled or its session ends explicitly. | automated | `tests/jsmodules/local-programme.test.js::paces batched render messages within the 32 buffer pool`; `tests/jsmodules/local-programme.test.js::ends the session without stranded live loans on every sender failure path`; `tests/jsmodules/programme-transport.test.js::encodes exactly one little endian stereo packet and never overspends credit`; `tests/jsmodules/programme-transport.test.js::uses cumulative consumption credit and stops on socket backpressure`; `tests/jsmodules/programme-worklet.test.js::frames stereo without gain and bounds outstanding transfers to 32`; `tests/jsmodules/programme-worklet.test.js::recycles only loaned slots and rejects unsupported rates`; `tests/jsmodules/programme-worklet.test.js::survives 200 milliseconds of delayed recycling without growing or losing stereo frames`; `tests/jsmodules/programme-worklet.test.js::waits for a fresh grant before starting its capture clock`; `tests/jsmodules/local-programme.test.js::catches up after timer quantization without exceeding a two packet sender burst`  `tests/jsmodules/local-programme.test.js::waits for delayed preparation credit within the fixed capture pool`; `tests/jsmodules/local-programme.test.js::ends preparation when waiting capture reaches the fixed pool bound` |
| `ADR-0020-15` | Offline signal measurement detects injected delay, gain changes, reversed channels, missing windows and clock error. | automated | `tests/unit/test_local_programme_measure.py::test_detector_finds_dropout_and_reversed_channels`; `tests/unit/test_local_programme_measure.py::test_detector_finds_one_missing_ten_ms_packet`; `tests/unit/test_local_programme_measure.py::test_detector_measures_clock_error`; `tests/unit/test_local_programme_measure.py::test_detector_measures_injected_delay_and_gain`; `tests/unit/test_local_programme_measure.py::test_occurrence_measurement_does_not_jump_to_a_later_repeated_song`; `tests/unit/test_local_programme_measure.py::test_private_marker_detector_rejects_injected_audition`; `tests/unit/test_local_programme_measure.py::test_streaming_detector_covers_every_chunk_and_missing_packet`; `tests/unit/test_local_programme_measure.py::test_streaming_detector_measures_accumulating_clock_error` |
| `ADR-0020-16` | One preparation owner wins atomically per station; socket/generation fencing, expiry, revocation, stop and external-input exclusion preserve the reservation. | automated | `tests/integration/test_local_programme.py::test_competing_tabs_station_isolation_release_and_stale_owner`; `tests/integration/test_local_programme.py::test_revocation_while_receive_waits_cannot_claim`; `tests/integration/test_local_programme.py::test_revocation_without_incoming_messages_and_stop`; `tests/unit/test_local_programme.py::test_claim_race_and_stale_grants`; `tests/unit/test_local_programme.py::test_deadlines_stop_and_preparation_cap`; `tests/unit/test_local_programme.py::test_external_input_exclusion`; `tests/integration/test_local_programme_control.py::test_blocking_control_leaves_event_loop_responsive`; `tests/integration/test_local_programme_control.py::test_handshake_cleanup_covers_cookie_failure`; `tests/integration/test_local_programme_cancellation.py::test_cancelled_control_waits_for_worker_before_cleanup`; `tests/integration/test_local_programme_cancellation.py::test_cancelled_control_logs_worker_failure_before_reraising` |
| `ADR-0020-17` | Ordinary crossfades retain pre-prototype synchronous accounting and timer completion regardless of media settlement; local-owned rejected or cancelled starts cannot advance, and repeated occurrences rewind with observed readiness. | automated | `tests/jsmodules/audio-engine-requests.test.js::does not advance either deck when incoming play rejects`; `tests/jsmodules/audio-engine-requests.test.js::ignores stale play completion after the standby deck was reused`; `tests/jsmodules/local-programme.test.js::does not start media after its ownership operation was cancelled`; `tests/jsmodules/local-programme.test.js::rejects failed play and requires observed media readiness`; `tests/jsmodules/local-programme.test.js::rewinds a reused media deck for a new occurrence of the same song`; `tests/jsmodules/audio-engine-requests.test.js::does not finish or advance a local-owned crossfade while play is unsettled`; `tests/jsmodules/audio-engine-requests.test.js::schedules local-owned crossfade ramps in the initiating tick and cancels rejected play without advance`; `tests/jsmodules/audio-engine-requests.test.js::preserves ordinary crossfade ordering with pending play`; `tests/jsmodules/audio-engine-requests.test.js::preserves ordinary crossfade ordering with rejected play` |
| `ADR-0020-18` | Sustained 44,100 Hz operation is evaluated for 30 minutes idle and under controlled load, with bounded storage and separate drift/dropout evidence. | manual | Scripted platform procedure: run `node tests/playwright/local_programme_soak.mjs idle` and `node tests/playwright/local_programme_soak.mjs load` serially at the default 1800 seconds, with Chromium, FFmpeg and the locked Python environment installed. Archive `audit.json`, `signal.json`, timing and audio artifacts; inspect ownership, loan/recycle statistics, PCM high-water, decoded dropouts/drift and one connection through Return. Both final trials passed on Windows NT 10.0.26300 / Chromium 156.0.8078.4 on 2026-10-30; see the dated amendment. This procedure is automated by the script, but remains platform evidence outside native pytest/Vitest discovery. No human listener is required. |
