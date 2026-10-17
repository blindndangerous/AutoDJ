# 14. Isolate programme review and export from broadcast state

Date: 2026-10-17
Status: Proposed; explicit maintainer acceptance pending.
Decision authority: Project maintainer. Agreed feature scope is separate from ADR acceptance.

## Context and scope

Operators need to hear a planned transition, section or programme without listening
through every song. The existing Hour preview forecasts an order and timings; it
does not provide a continuous shortened audio file. Contextual review must retain
speech and actual joins while leaving the station's picks and broadcast untouched.

The requested outcome includes listening and matching MP3/WAV exports from saved
Shows and captured queue/forecast content. Execution tracking is `autodj-ovc`;
contributor-visible requirements and evidence belong in the authorized PR. Beads
is not required to implement or review this decision.

[ADR 4](0004-server-mix-bus-and-stream-mode.md) governs the server renderer and
mastering boundary; [ADR 5](0005-a-station-owns-its-playout-chain.md) governs station
paths and routing. Proposed [ADR 8](0008-bound-background-work-outside-playout.md),
[ADR 9](0009-share-picking-rules-with-isolated-previews.md) and
[ADR 10](0010-preserve-state-through-atomic-writes-and-recovery.md) describe related
worker, forecast and recovery behavior. This proposal does not accept or supersede
those records.

## Proposed decision

### Frozen inputs and shared rendering

Capture a review input with occurrence identities, source audio/cues, relevant
mixing settings, intended start and station time zone. Resolve automatic liners,
matched intros, ads, carts and time checks using isolated existing selection rules
on the full programme timeline, before shortening. Preserve actual selected audio
at capture rather than selecting again during export. Unknown duration makes the
affected downstream programme times unknown; a placeholder's duration must not
be reported as the missing original's known broadcast duration.

Use the existing Player renderer, handoff placement and ducking rules. Carry
incoming offsets and glides between rendered tracks. Keep review PCM and output
away from the live mix bus, and copy mutable state and audio. Reading source
metadata must not write catalog measurements or mutate live queues, RNG state,
reservations, history or ad accounting. Concurrent ordinary playout may advance;
review must cause no additional live mutations.

Review represents a plan at capture time, not what actually aired or a promise of
future automatic selections. Render before final broadcast mastering. Live DJs,
unrecorded microphone contributions, final Stereo Tool processing and actual-air
capture are outside this boundary.

### Retention and mapping

Songs retain configurable opening and ending minimum windows. Non-song clips at
or below the configured long-clip threshold remain complete; longer clips follow
song shortening. Explicit keep-complete targets override ordinary shortening.
Expand and union retained windows to preserve transitions, overlays, speech and
ducking envelopes without duplicating samples. Expand selections crossing required
content and report their effective range. Do not impose a retained-duration cap
that silently cuts required content.

Insert a generated skip tone at each removed interval. Store a piecewise mapping
between programme and shortened-preview positions; tones have no programme source
interval. Listening and both export formats use one frozen edit result. Provider
or random selection must not be repeated simply to download another format.

For unresolved content, produce an explicitly incomplete review with an omission
list and spoken placeholders through the existing configured default TTS command.
Do not generate the missing programme speech itself. Missing or failed TTS fails
only reviews needing placeholders, with an actionable error and prior results
preserved. Reuse the current TTS configuration and child-process protections.

### Ownership, access and lifecycle

The server owns one review render slot across stations. A station owns its derived
results under `StationPaths.reviews`; browser-supplied paths never choose storage.
Use existing operator-browser authorization and station aliases, including stopped
station saved-content review. Public listeners and limited DJ, announcer or worker
credentials grant no review generation, status, media or download access. Paired
browser permissions remain server-wide; this feature introduces no operator roles.

Admission defaults to 20 completed results and 512 MiB per station, with a
120-second cooperative render deadline. These bound work and retained artifacts;
they do not promise completion latency or real-time performance. A worker that has
not stopped retains its slot. Cancellation, deadline, decode/TTS/encoding/write
failure and shutdown must clean partial work and release capacity only when its
owner exits. Failures must not replace a valid completed snapshot.

Source capture, decoded spills, working arrays and encoding use an owned scratch
workspace with a separate 2 GiB allocation budget and checks that leave 64 MiB
free. The budget includes copies and may reject a review whose final shortened
files would be small. Do not fall back to an unowned global temporary directory
when its workspace is full. These are service resource bounds, not new public
configuration keys.

Publish completed WAV/MP3 files and metadata atomically. Completed snapshots survive
restart; interrupted work becomes failed and its staging files are removed. Recovery
never starts or resumes broadcast. Completed results have no automatic expiry and
are removed only by explicit deletion. Reaching a limit refuses generation rather
than evicting reviewed audio. Derived reviews are excluded from station backups;
download files that need independent retention. Source recordings remain owned by
their existing catalog or future prepared-edition owner.

Relevant source changes mark a retained result Out of date, preserving its audio
and exports. Regeneration is explicit and creates another result. A future G05
prepared-edition adapter may request full pending-speech review. G05 owns edition
identity, recording storage, approval and broadcast eligibility, its own ADR and
integration tests; creating or hearing a review never approves speech.

## Alternatives and consequences

- Render frozen inputs with existing placement: preserves one mixing definition
  and supports reproducible exports, but requires capturing audio identities that
  forecast display rows previously discarded.
- Render directly from live state on each request: needs less snapshot storage,
  but consumes selections or lets listening and exports diverge as the station moves.
- Build a separate simplified mixer: can be smaller initially, but gives misleading
  evidence when cue placement, effects or ducking differ from broadcast rendering.
- Retain an unshortened programme export: simplifies mapping, but does not meet the
  review workflow or its listening-time savings.

Disk-backed working PCM limits ordinary memory pressure, while storage admission
can still reject long or mostly keep-complete reviews. Explicit deletion means
operators manage retention. Revisit bounds after measured deployment evidence;
revisit the plan/capture distinction if actual-broadcast review becomes a requirement.

## Implementation and operational references

- [Source adapter](../../src/autodj/review_source.py),
  [forecast capture](../../src/autodj/hour_preview.py), and
  [shared renderer](../../src/autodj/player.py) capture and render isolated choices.
- [Shortening and mapping](../../src/autodj/review_audio.py),
  [review rendering](../../src/autodj/review_render.py),
  [result lifecycle](../../src/autodj/review_jobs.py), and
  [station API](../../src/autodj/review_api.py) own the new boundary.
- [Browser controls](../../src/autodj/static/modules/programme-review.js) reuse the
  native private audio player and existing preview-device routing.
- [Programme review operations](../operations.md#programme-review-and-export),
  [settings reference](../settings-reference.md#programme-review), and
  [manual rehearsal](../accessibility-testing.md#programme-review-rehearsal) define
  controls, limits and evidence. No new package or provider configuration is added.

## Current contracts

Automated selectors identify checks, not a claim they passed on the final candidate.
Manual and gap rows remain obligations, separate from ADR acceptance.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0014-01` | Real render/export uses shared placement and preserves live catalog, ledger and queue state. | automated | `tests/unit/test_review_render.py::test_real_render_exports_matching_wav_mp3_and_does_not_touch_live_state` |
| `ADR-0014-02` | Required joins/windows merge without duplication and explicit full targets override shortening. | automated | `tests/unit/test_review_audio.py::test_protected_joins_and_overlapping_windows_never_duplicate_samples`; `tests/unit/test_review_audio.py::test_keep_complete_overrides_long_clip_threshold_and_selection` |
| `ADR-0014-03` | Missing audio requires placeholders with honest unknown times; missing default TTS fails actionably. | automated | `tests/unit/test_review_render.py::test_unresolved_audio_uses_placeholder_and_marks_unknown_programme_times`; `tests/unit/test_review_render.py::test_unresolved_audio_without_tts_fails_actionably` |
| `ADR-0014-04` | Completed results survive restart; failures preserve prior results and one-slot capacity ownership. | automated | `tests/unit/test_review_jobs.py::test_completed_results_are_immutable_and_survive_restart`; `tests/unit/test_review_jobs.py::test_failed_render_preserves_previous_and_releases_capacity` |
| `ADR-0014-05` | Station changes discard delayed UI replies and remove private audio access from the previous context. | automated | `tests/jsmodules/programme-review.test.js::clears audio and drops delayed replies when station context changes` |
| `ADR-0014-06` | Operator access applies to all review routes and derived reviews stay outside station backups. | automated | `tests/integration/test_review_api.py::test_review_access_needs_paired_browser_and_origin`; `tests/integration/test_review_api.py::test_machine_credentials_cannot_generate_or_download_reviews`; `tests/unit/test_review_backup.py::test_backup_keeps_saved_shows_but_excludes_derived_reviews` |
| `ADR-0014-07` | Independent Windows/Chrome/NVDA operation and audible joins are demonstrated on the candidate. | manual | Follow the programme review rehearsal in accessibility-testing.md; record commit, versions, devices, steps, speech/focus observations and listening findings. Pending; missing evidence blocks feature closure. |

## Verification and remaining evidence

- [ ] Run mapped behavioral tests separately from architecture-document validation.
- [ ] Exercise all input/review scopes, threshold edges, actual decoded MP3/WAV,
  automatic full-time selection and unknown duration; verify failure, cancellation,
  resource bounds, recovery, permissions and backup exclusions.
- [ ] Run applicable Python/frontend contributor gates and real browser review and
  download journeys on the final revision. Do not infer device routing from DOM tests.
- [ ] Have the operator execute the Windows/Chrome/NVDA and listening rehearsal
  from the PR instructions. Keep the feature issue open until that evidence passes.

This record supplies no completed manual rehearsal, hardware-routing claim, broad
assistive-technology guarantee or measured deployment performance evidence.
