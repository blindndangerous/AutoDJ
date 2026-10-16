# 13. Produce speech outside playout with explicit provider comparisons and publication

Date: 2026-10-16
Status: Accepted; maintainer approval in conversation on 2026-10-16.
Decision authority: Project maintainer, explicit approval on 2026-10-16.
The architecture is accepted; implementation and verification remain separate.

## Context and scope

AutoDJ can import liners, record voice tracks, accept announcer breaks and run a
configured time-check TTS command. It cannot author speech or compare providers in
the browser. The operator requested selected Showcraft capabilities: supplied text,
voice auditions, reusable station voices and generated time-check audio. Comparisons
must include OpenAI, Speechify, Fish, Chatterbox and GPT-SoVITS through configured
services, with successful samples retained independently of failures.

The trigger is a speech-production workflow within AutoDJ. Copying Showcraft's full
episode workflow would introduce family/show/episode authoring, research and assembly
that short liners do not need. There is no automatic Showcraft producer handoff.

[ADR 5](0005-a-station-owns-its-playout-chain.md) retains station ownership and shared
library boundaries. [ADR 4](0004-server-mix-bus-and-stream-mode.md) retains audio
ownership. Proposed [ADR 7](0007-separate-browser-worker-and-listener-access.md),
[ADR 8](0008-bound-background-work-outside-playout.md) and
[ADR 9](0009-share-picking-rules-with-isolated-previews.md) describe related existing
access, worker and time-check behavior; their Proposed status remains unchanged.

Execution tracking is `autodj-0kc`. No GitHub issue has been published; the approved
requirements must accompany an authorized issue or PR before contributor handoff.
This record contains the architectural consequences and implementation map, rather
than making Beads a contributor prerequisite.

## Decision

### Ownership and access

Use a server-owned speech service with shared provider connections and credentials.
Each station owns its drafts, comparison results, voice presets, selected default,
saved speech assets and generated time-check sets. Derive station paths exclusively
through `StationPaths`; never accept filesystem paths from the browser.

Provider settings are shared browser-control routes under `/api/speech/providers`.
Station speech routes under `/api/speech/...` also have the existing
`/api/stations/<slug>/speech/...` aliases. Draft editing and generation work while a
station is stopped. Apply existing loopback/LAN browser policy, Origin checks and
early body limits. Listener, analysis-worker and announcer credentials grant no
speech-generation or provider-configuration access. Pairing remains server-wide.

Store shared connections in a versioned private `speech_providers.json` under the
configured index root, using `write_private_file`. API keys are write-only through
the browser; return presence and configuration status, never values. Reuse AutoDJ's
owner-only file protections, not Showcraft's multi-user credential encryption system.
Exclude credentials from backups, diagnostic exports, job errors and manifests;
restore must refuse archived credentials even if an archive includes them.
Environment-injected credentials override saved keys and are read-only in the UI,
following existing relay conventions. Nonsecret endpoint definitions may be backed
up separately; restored connections remain uncredentialed until configured again.

### Amendment 2026-10-16: reuse the station TTS command

The maintainer approved exposing `time_checks.tts_command` as a station-local studio
candidate alongside shared HTTP connections. Reuse its argv array, stdin text,
`{text}`/`{out}` substitutions and 20-second process timeout through a shared runner.
Command execution remains operator-configured; browser requests cannot provide argv,
paths or environment. Voice/model/delivery are fixed by the command and unsupported
overrides fail preflight. No automatic command discovery or shell execution is added.

Persist a hash of the configured argv in candidate snapshots and cache identity,
never argv itself. Before execution/retry, reject changed configuration rather than
silently substituting the new generator. Existing legacy fallback keeps its own cache;
studio output uses the common validated cache, preview, save and generated-set flows.
Cancellation/timeout reaps the command and its POSIX process group; Windows uses the
existing child-job binding. No guarantee is made about cancellation of remote work
started by an operator's command. Complete activated sets remain local-only.

### Provider protocols and delivery controls

Use a small fixed adapter dispatch for the supported protocols, not a plug-in
framework. Implement direct OpenAI and Speechify requests and explicitly versioned
service profiles for Fish, Chatterbox and GPT-SoVITS. An engine identifies speech
capabilities; a protocol identifies request, discovery, response and authentication
formats. A configurable URL does not imply arbitrary API compatibility. Label
connections by operator name and engine; infrastructure hosting is not a provider.

Use standard-library HTTP and existing audio decoders; add no provider SDK,
Showcraft dependency, service deployment code or local model runtime. Fish,
Chatterbox and GPT-SoVITS support means compatibility with the specified endpoint
profiles, not every upstream server exposing those model names. Use explicit voice
IDs where discovery is unavailable; never choose a random replacement voice.

The [GPU endpoint contract](../speech-endpoints.md) defines the service-side
translation, voice mappings and readiness checks. Reuse a conforming service;
keep any native-engine shim with its GPU deployment rather than in AutoDJ.

Define and fixture each protocol from inspected client/service contracts before
implementation. Showcraft currently delegates those three engines to an external
client script; AutoDJ must not discover or implicitly depend on that sibling script or inherit SSH
catalog discovery. An operator-configured station command is covered below. Record fixtures without secrets, endpoints or voice-reference file paths
from the operator's deployment. No adapter is advertised as verified until its
contract checks pass; live readiness additionally requires a configured service test.

Only configured connections may be used. Generation requests carry connection IDs,
not URLs or arbitrary headers. Validate connection URLs when configured: HTTP(S)
only, no URL credentials or fragments, TLS required except an explicit trusted-local
HTTP connection. Private services and loopback tunnels are intentional destinations;
block metadata/link-local destinations and cross-origin redirects. Never forward
credentials to a provider-supplied audio URL. Prefer audio bytes or base64 responses;
download-result protocols need an explicit destination policy and tests before use.

Use one canonical script per comparison. Each candidate snapshots voice, model and
delivery overrides. Report applied, approximated and unsupported controls; reject
unsupported requested controls rather than silently discarding them. Escape supplied
text when producing provider markup. Only enable controls the adapter can express;
an instruction prompt does not promise exact timing or phonemes. Presets snapshot
these settings; later edits affect new generations, not existing audio or active sets.

### Explicit generation, bounded work and retry

Preflight each candidate before synthesis: credentials present where required,
configured protocol, valid voice/model/settings, text limits and storage admission.
A missing key is a local configuration failure, not proof a key will authenticate.
Present reliable estimates per candidate and the batch total; otherwise say cost is
unknown. Generation is an explicit POST after selection, never a side effect of
opening a form, changing a preset, previewing saved audio or restarting AutoDJ.

Use one dedicated server-wide speech worker and one admitted batch at a time,
initially with no waiting batches. Do not repurpose the CLI maintenance command
allowlist in `JobManager`. A comparison has at most eight candidates, processed
independently and sequentially. Initial limits are 5,000 Unicode characters per
script, 32 MiB decoded HTTP response per sample, 120 seconds of audio per clip and
a 300-second request deadline. Enforce stricter provider limits before submission.
Set a 64 KiB browser JSON body limit. These are proposed resource ceilings, not
measured throughput promises; revise with documented evidence if unsuitable.

Persist candidate state before dispatch: pending, running, succeeded, failed,
cancelled or interrupted. Persist validated successful audio independently, then
expose duration and playable audio. No automatic paid retries or provider failover.
Retry only explicitly selected failed/interrupted candidates; warn that a timed-out
or interrupted request may already have been billed. Retrying uses the original
snapshot; edited text/settings create new candidates. An idempotent admission token
prevents double-clicks or HTTP retransmission from starting the same batch twice.

Cancellation stops unstarted requests and interrupts local work where possible;
it does not promise remote cancellation or reimbursement. On restart, in-flight
candidates become interrupted and are not resubmitted. Shutdown refuses new work,
signals cancellation and retains worker ownership until bounded cleanup finishes.
No synthesis, HTTP, discovery or credential lookup occurs on audio callbacks.

### Cache, retention and publication

Hash a versioned canonical request containing station scope, connection identity,
endpoint/configuration revision, engine, protocol revision, model/version when known,
voice identity/revision when available, exact text, applied delivery controls and
output encoding. Credentials never enter metadata or plaintext cache keys. Cache
sharing is station-local even though credentials are shared. Revalidate audio and
metadata before a hit; coalesce identical in-flight candidates. Mutable remote voice
or model versions cannot be inferred: offer an explicit fresh render and record
when upstream identity is unknown.

Keep comparison audio outside all scanned liner and time-check folders. Initially
retain unsaved samples for seven days with a 1 GiB server-wide draft/cache ceiling;
evict only expired/unreferenced temporary audio and refuse new work if still full.
Never evict saved liners, active sets or audio owned by a running operation. Persist
text/settings and surface expired audio with an explicit regenerate action. Cleanup
must not submit provider requests. Back up saved assets and metadata; exclude cache
and unfinished generations. Validate IDs, relative names and audio on restore.

Saving a liner preserves its audio, source text, provider/model/voice identity,
effective settings and lowering report in a station-owned saved-speech collection
outside rotation. Saving neither selects the station voice nor publishes audio.
An explicit assignment publishes a validated local file through existing liner
library/import paths or another existing supported placement. Do not make an inactive
file visible to `LinerLibrary.refresh`, which scans eligible folder contents. Keep
source metadata with the published asset through backup and restore; a missing
provider must not prevent existing audio from playing.

### Generated time checks

Generate complete local minute clips for the selected 12- or 24-hour wording using
the existing `spoken_text` function, not a second wording implementation. Preflight
the complete set: enumerate all 1,440 minute mappings, deduplicate identical requests,
show missing/cache-hit counts and aggregate cost/unknown pricing, and reserve space
before explicit generation. Apply existing pack file-count and 512 MiB expanded-set
limits. Stop between clips for cancellation; persist progress and explicitly retry
only missing/failed clips. A partially generated set remains inactive.

Activation validates complete minute coverage, audio and settings fingerprint, then
atomically switches the station's selected generated set. Before activation, the
operator previews samples and explicitly acknowledges review; do not claim that an
automated check proves intelligibility. Failed activation preserves the previous set.
Generation and activation are separate actions, independent of selecting a default
voice. Voice/wording edits mark a replacement as needed without changing current audio.

For generated-set mode, consume local clips before existing time-check sources and
disable the TTS-command fallback. A missing/corrupt generated clip skips with a clear
warning and performs no network call. Leave legacy recorded-pack/clip/TTS-command
behavior unchanged when generated-set mode is not selected. Existing intro/outro,
placement, ducking and mastering remain AutoDJ responsibilities. Backup/restore must
preserve the active set and source metadata so playback works without credentials.

## Alternatives and consequences

- Embed selected capabilities as above: one AutoDJ operator workflow, with new
  adapter, credential, persistence and worker responsibilities to maintain.
- Invoke a separately installed Showcraft: reuse its production pipeline, but short
  clips inherit its authored tree, runtime configuration and external-client setup.
  This conflicts with the approved standalone AutoDJ workflow.
- External WAV generation/import: already possible and simplest operationally, but
  does not provide the requested provider comparisons or editable generation history.
- Copy Showcraft wholesale: brings proven patterns but also episode/research/casting
  policy and dependencies. Source reuse additionally requires checking licensing;
  this proposal adopts concepts and independently implements the bounded adapters.

The worker serializes speech production, so a time-check set can delay comparisons;
operators can cancel and resume incomplete production explicitly. Provider contracts
and prices can drift. Unknown pricing stays unknown, and all service profiles need
contract fixtures and applicable live checks. Time-check production can be expensive:
the full inventory must be visible before spending. Pre-generation trades latency
at playback for storage and an explicit refresh workflow.

Revisit for measured worker contention, a provider protocol that cannot fit the
bounded request model, independent station billing/authorization, or an actual need
for unattended script production. None is implied by this decision.

## Implementation and verification map

Use the existing runtime and helpers as integration points:

- [Server](../../src/autodj/server.py), [station routes](../../src/autodj/station_routes.py)
  and [security](../../src/autodj/security.py): install and classify shared provider
  and station speech routes; enforce body limits before handler parsing.
- [Station paths/lifecycle](../../src/autodj/stations.py),
  [private files](../../src/autodj/secret_files.py),
  [relay settings](../../src/autodj/relay_settings.py) and
  [atomic writes](../../src/autodj/fsutil.py): extend ownership and private persistence.
- Add `src/autodj/speech.py` for snapshots, persistence, cache and bounded worker;
  `speech_providers.py` for protocol dispatch and HTTP limits; `speech_api.py` for
  browser routes. Keep engine-specific code together until size requires splitting.
- [Liner files](../../src/autodj/liner_files.py),
  [liner library](../../src/autodj/liners.py),
  [time checks](../../src/autodj/time_checks.py) and
  [time-check files](../../src/autodj/time_check_files.py): integrate explicit
  publication and generated-set activation without changing legacy fallback mode.
- [Backups](../../src/autodj/backup.py),
  [station duplication](../../src/autodj/station_creation.py) and
  [Doctor](../../src/autodj/doctor.py): cover metadata, active local audio, redaction,
  exclusion of secrets and safe unavailable-provider diagnostics.
- [Browser shell](../../src/autodj/static/index.html),
  [application](../../src/autodj/static/app.js),
  [liners](../../src/autodj/static/modules/liners.js) and
  [time-check panel](../../src/autodj/static/modules/time-checks.js): add a plain
  speech studio using existing form, status, focus and station-switch conventions.
  Preview audio stays local to the operator and never enters on-air audio.

Implement in dependency order: provider contracts/private persistence, bounded
comparison jobs/cache, browser auditions/saved-liner assignment, then generated
time-check sets. Track execution in the native issue rather than a parallel plan.
No package dependency, model installation or configuration migration is required;
missing new state means the feature is unconfigured. Preserve legacy station files.

Add meaningful protocol and state-machine checks under `tests/unit/`, route/security
checks under `tests/integration/`, and operator-flow checks under `tests/jsmodules/`.
The contract table below maps implemented invariants to runnable checks and reciprocal
annotations. Update [operations](../operations.md), [setup](../setup.md),
[threat model](../../THREAT_MODEL.md) and
[accessibility guidance](../accessibility-testing.md) alongside runtime changes.

## Current contracts

These are accepted invariants with implementation checks. Test selectors identify
where to verify behavior; only an actual run establishes a result. Live provider
readiness, audible quality, platform protections and workflow superiority require
the separate evidence described below.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0013-01` | Provider credentials are shared, write-only to browsers, redacted and excluded from backup/restore; station state remains isolated. | automated | `tests/unit/test_speech_providers.py::TestSpeechProviders::test_private_credentials_never_appear_in_public_settings`; `tests/unit/test_speech_backup.py::test_speech_round_trip_keeps_playable_saved_assets_and_excludes_keys`; `tests/unit/test_speech_backup.py::test_speech_export_omits_unfinished_work_and_retains_active_set`; `tests/integration/test_speech_api.py::test_provider_keys_are_write_only_and_station_routes_are_aliased` |
| `ADR-0013-02` | Only configured, validated protocol connections can receive generation requests; remote audio cannot redirect credentials. | automated | `tests/unit/test_speech_providers.py::TestSpeechProviders::test_protocol_requests_and_base64_audio_for_all_engines`; `tests/unit/test_speech_providers.py::TestSpeechProviders::test_untrusted_urls_and_network_destinations_are_refused` |
| `ADR-0013-03` | Explicit requests alone admit bounded generation outside playout; repeated admissions do not duplicate spend. | automated | `tests/unit/test_speech.py::TestSpeech::test_busy_cancel_and_interrupted_restart_never_resubmit`; `tests/unit/test_speech.py::TestSpeech::test_restart_marks_inflight_work_interrupted_without_generation`; `tests/unit/test_speech_providers.py::TestSpeechProviders::test_deadline_terminates_and_reaps_a_stuck_provider_worker` |
| `ADR-0013-04` | Successful samples survive sibling failures; retries target only selected failures and never automatically switch providers. | automated | `tests/unit/test_speech.py::TestSpeech::test_success_survives_failure_retry_and_restart` |
| `ADR-0013-05` | Cache reuse requires matching effective request identity and valid audio; expired samples require explicit regeneration. | automated | `tests/unit/test_speech.py::TestSpeech::test_station_command_compares_caches_and_rejects_changed_retry`; `tests/unit/test_speech.py::TestSpeech::test_cache_and_station_isolation`; `tests/unit/test_speech.py::TestSpeech::test_expired_partial_set_can_be_explicitly_regenerated` |
| `ADR-0013-06` | Saving audio or selecting a voice does not publish a liner; assignments alone make generated liners eligible for rotation. | automated | `tests/unit/test_speech.py::TestSpeech::test_save_keeps_audio_out_of_rotation_and_default_unchanged` |
| `ADR-0013-07` | Generated time checks activate only as complete reviewed sets; failures retain the prior set and playback performs no synthesis. | automated | `tests/unit/test_speech.py::TestSpeech::test_complete_sets_activate_atomically_and_survive_without_provider`; `tests/unit/test_time_checks.py::test_generated_mode_never_calls_legacy_tts_when_audio_is_missing` |
| `ADR-0013-08` | Comparisons preserve the script and settings, expose capability limitations and keep preview audio off air. | automated | `tests/jsmodules/speech-studio.test.js::keeps successes playable beside failures without publishing or auto retry` |
| `ADR-0013-09` | Unknown pricing is reported as unknown; bulk generation requires a complete request inventory before spending. | automated | `tests/unit/test_speech.py::TestSpeech::test_presets_and_bulk_inventory_do_not_generate` |
| `ADR-0013-10` | Workflow improvement and audible quality require direct evidence beyond test-source inspection. | manual | Compare external generation/import with the studio using the same liner: record completion time, repeated entry, synthesis calls and failure recovery; perform listening and keyboard/screen-reader trials on implemented flows. No trial has run yet. |

## Review and verification

- [x] Maintainer accepted ownership, protocol, persistence and worker decision on 2026-10-16.
- [x] Implement mapped invariants and link runnable selectors. Broader contributor gates
  and manual/live evidence remain separate from these mappings.
- [ ] Run focused behavioral checks, full applicable contributor gates and Doctor.
- [ ] Run Chromium and WebKit accessibility/browser checks and manual speech sampling
  on the implemented UI; test platform-specific private-file protections where changed.
- [ ] Validate configured services with explicit live synthesis only when authorized;
  mocked protocol success does not prove endpoint readiness or voice quality.

For structural validation, run
`uv run --frozen --no-sync python scripts/check_architecture_docs.py` and inspect
`git diff --check`. Report structural results separately from runtime, paid-provider,
platform, audio and usability evidence. No provider requests are needed to structurally validate this record.
