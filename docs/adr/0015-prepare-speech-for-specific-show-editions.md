# 15. Prepare speech for specific show editions

Date: 2026-10-17
Status: Accepted; maintainer approval in conversation on 2026-10-18 (PR #451 merged after review fixes).
Decision authority: Project maintainer. Operator agreement on feature scope does not accept this ADR.

## Context and scope

A producer needs to record tomorrow's programme while no near-air voice-track
slot exists. A reusable saved Show supplies a song list, but recordings belong
to a particular production of that list. A repeated song can have different
introductions on its two occurrences. Binding speech by its file path, or booking
it into today's live slot, cannot express that distinction safely.

This proposal extends Shows with prepared editions. It owns edition identity,
source recordings, approval, backup, scheduling and interrupted-run recovery.
[ADR 14](0014-isolate-programme-review-and-export.md) owns isolated contextual
review and export; this feature consumes that engine rather than defining another
mixer or review service. The OVC base is [PR 450](https://github.com/blindndangerous/AutoDJ/pull/450).
Contributor-visible G05 requirements and verification belong in its separate PR.
Operational steps are in [Prepared show editions](../guides/operations/prepared-editions.md).
The optional execution bead is `autodj-44r`.

Accepted [ADR 4](0004-server-mix-bus-and-stream-mode.md),
[ADR 5](0005-a-station-owns-its-playout-chain.md) and
[ADR 13](0013-produce-speech-outside-playout.md) establish the renderer, station
ownership and speech-production boundaries. Proposed
[ADR 7](0007-separate-browser-worker-and-listener-access.md),
[ADR 9](0009-share-picking-rules-with-isolated-previews.md) and
[ADR 10](0010-preserve-state-through-atomic-writes-and-recovery.md) remain Proposed.

## Proposed decision

### Edition and asset ownership

Keep reusable saved Shows unchanged. Creating an edition copies a Show's list and
assigns a persistent ID to each occurrence, including repeated paths. Reordering
preserves those IDs; replacing an occurrence creates a new identity. Bind a
transition take to the exact adjacent occurrence pair. Opening and closing takes
bind to the edition's first and last occurrence respectively. Keep one active take
per position/bound pair; re-recording replaces that take and retains its prior
source asset. Multiple pending speech chains at the same edge are outside scope.

A station owns its edition manifest and immutable source audio through
`StationPaths`. Record short takes with the existing bounded browser recording
and WAV conversion facilities. Import externally edited primary speech segments
up to 30 minutes with bounded upload, decoding and storage. Keep primary segments
distinct from liners: they occupy a queue occurrence, play at normal speed and
use the disk-backed audio path. This feature does not add an interview editor.

Manifest writes use optimistic revisions and atomic replacement. Cancellation,
stale edits, invalid audio and failed writes preserve the last valid edition and
its audio. Re-recording retains the old source asset. Backup and restore enumerate
edition state and source audio explicitly; derived review files remain excluded.

### Relevant approval dependencies

Creating, importing, rendering or listening to speech never approves it.
Unapproved, changed, unavailable or invalid speech is visibly Needs review and
cannot enter uncommitted broadcast audio. Preserve the recording when approval
becomes invalid. The current-run commit exception below retains the prior approval
of an occurrence whose speech or transition is already committed.

Approval identifies the immutable take asset, reviewed edition/revision and its
relevant dependencies: bound occurrence identities, adjacent audio/cues and the
applicable placement/mixing settings. Revalidate those dependencies atomically
when applying explicit approval and again for broadcast eligibility. Moving an
intact adjacent pair or editing unrelated content preserves approval when these
inputs remain identical. A global edition revision alone is not an approval
dependency.

Use a server-owned receipt from a completed OVC review. Explicit pending-speech
review can include valid Needs review material, labelled accordingly. An approval
review must retain its target completely, including a 30-minute interview, even
when ordinary OVC rules would shorten it. Missing media, diagnostic placeholders,
omissions and shortened targets cannot establish approval. Retained stale reviews
remain playable; they cannot approve a target whose relevant inputs changed.

### Local playout and recovery

Schedule prepared editions through the existing Shows action. Preserve occurrence
identity when copying entries, queuing ahead and starting immediately; a path-only
lookup must not silently associate another occurrence's take.

Inject local approved transition speech through the shared server renderer before
automatic intro selection, independently of running near-air slots. Prepared
speech joins reuse saved-point placement and ducking; ordinary music joins retain
the station's selected transition mode. Source recordings are never deleted by
the temporary break lifecycle. Opening, closing and long segments are primary
speech occurrences rather than liners or oversized whole-file breaks.

Server audio and streaming are supported; initial browser-mode edition playout
is outside this scope. Provider access and speech generation never occur during
playout. Missing, invalid or unapproved speech is skipped and reported while valid
songs continue, with no automatic replacement take. Existing scheduler hard-event
priority remains authoritative. Duration is an estimate: warn about likely
overrun without promising an exact finish or shortening interviews on air.

Persist edition run identity and progress. A prepared edition interrupted by a
restart returns to ordinary automation and exposes Interrupted state. Only an
explicit resume action selecting an occurrence can restart it, after validating
current speech eligibility. Preserve ordinary saved Shows' automatic-resume
behavior. Review recovery cannot start either type of programme.

#### 2026-10-17: edits crossing the current-run commit boundary

Rebuild future prepared PCM after relevant edits. Atomically replace a current
future tail only before both the old and replacement speech duck ramps/incoming
overlap, preserving its original music prefix and playhead. Discard ineligible
prepared render-ahead fallbacks; ordinary queue refresh keeps its existing policy.

An API preflight rejects an edit when it already observes committed speech, but
playback can cross the boundary while a save or render is in progress. If it does,
the edit remains saved and governs future review/playout. The current occurrence
retains its prior frozen approval and continues uninterrupted, with an explicit
`committed_audio_notice`; do not describe that audio as revoked or skipped.
Eligibility changes cannot unhear committed audio. This bounded exception avoids
holding the audio lock across manifest writes or decoding. A primary speech item
commits when its first PCM enters playout, including an incoming overlap; a
transition commits at its first affected duck ramp or
incoming overlap. The dependency guard prevents a stale replacement from being
installed after a newer edit; it does not make the early preflight and save atomic.

An edition that has started may already have aired speech even if a crash
prevented exact accounting. Require an explicit re-air confirmation before
scheduling or starting it again; do not claim exact transmission history.

### 2026-10-18: binding recovery and scoped edit checks

Segment neighbours are derived from the segment occurrence's current position.
Moving the segment or changing its neighbours preserves the source and attachment
while invalidating changed approval dependencies. A separated transition pair is
explicitly Unattached. Reattach chooses a valid adjacent pair, retains the take
and asset identities, and requires fresh review and approval. The same action
repairs displaced opening and closing bindings.

Removing a segment occurrence removes its active take in the same revision while
retaining the source asset. This frees its binding for another segment. A missing
segment item requires removal or re-import rather than transition reattachment.
Partial-import cleanup runs once per process per resolved audio directory, even
when stopped editors and running stations use different objects. Cleanup errors
are logged and do not block startup or repeat cleanup over active imports.

Apply edit preflight only to changes affecting current speech or the immediate
handoff, including next primary speech eligibility. Unrelated future take approval
remains available while an opening clip plays. Preserve the existing race policy
when a relevant edit crosses the boundary after preflight.

Cache content hashes process-wide by resolved path, size, nanosecond modification
time and inode. Share a frozen runtime plan through render selection and decoding;
recheck its revision and eligibility after decoding. A detected change triggers a
fresh render attempt. The final tail-swap guard still validates current inputs.
An error during post-decode eligibility validation omits prepared speech for the
retry; valid music continues without retrying an unverifiable speech plan.
This avoids repeated whole-file reads; it does not guarantee latency or detect an
external byte edit that preserves every cache-key field.

Persist resume choices as real occurrences. An opening maps to the first real
item; a closing with no following item maps to the last real item, which may replay
on explicit resume. Once the durable may-have-aired marker succeeds, progress-file
failure is logged without retrying that marker as an unconfirmed re-air.

## Alternatives and consequences

- Attach speech to reusable Shows or song paths: less state, but edits and repeated
  songs make the intended production and occurrence ambiguous.
- Book recordings into live near-air slots: reuses today's slot identity, but
  prevents stopped-station preparation and cannot safely represent tomorrow.
- Export a mixed programme and queue one file: simple playout, but prevents
  individual song/speech edits, relevant approval checks and normal hard-event
  behavior. Full-show export is outside this feature.
- Use one revision to invalidate every approval: easy to implement, but unrelated
  edits unnecessarily repeat review. Relevant fingerprints require more explicit
  dependency selection and tests.

Immutable source assets consume disk space and retained reviews can outlive
approval. Operators retain control over deletion and export. Initial limits are
resource bounds, not measured completion-time or real-time guarantees. Revisit
them only with deployment evidence.

## Amendment, 2026-10-22: live queue edits and current-airing speech

[Issue #519](https://github.com/blindndangerous/AutoDJ/issues/519) defines a
current-airing choice when a live queue edit changes an affected handoff. The
then-current model's preflight covers the current editable handoff, ready
handoff, and affected pinned slot plans. One revision-bound choice covers the
affected
speech contexts in that replan. **Keep voice track** carries affected speech
with its changed music pair; a previously eligible prepared-edition recording
can remain in that pair through an operator-kept copy that retains its source
asset. **Discard voice track** removes affected speech from the replanned
contexts only. **Cancel change** leaves the queue untouched. A stale conflict
requires a fresh choice; a missing choice never approves speech implicitly.

If a voice or liner chain has already started, its original timing and layers
remain while unheard incoming music is replaced at the original boundary, so
the speech is not repeated.

This choice does not edit a saved edition, change its approval, or approve an
ineligible take for future use. Source recordings and saved approval state stay
under edition ownership. Automatic callers without an operator choice leave the
protected queue intact and retry; they do not choose to keep or discard speech.
Hour Preview continues to describe future live points as estimates.

## Amendment, 2026-10-22: streaming handoff scope ([#521](https://github.com/blindndangerous/AutoDJ/issues/521), [#523](https://github.com/blindndangerous/AutoDJ/issues/523))

Streaming no longer creates future pinned `SlotPlan`s. Queue choices apply to
the current or ready live handoff; local slot planning is unchanged. Ready
cancellation extends the prior current-handoff cancellation window and removes
only the targeted speech, leaving sibling speech, approved edition state and
source assets intact.

## Amendment, 2026-10-23: local sound-card editions use live playout

[Issue #538](https://github.com/blindndangerous/AutoDJ/issues/538) applies the
live speech and queue-edit lifecycle to local sound-card playback as well as
streaming. Local playout no longer depends on future pinned slots or a rendered
fallback. Occurrence identity, explicit keep/discard decisions, committed-audio
protection, source recordings and saved approval retain their existing ownership.
Private review/export still uses the isolated offline renderer under ADR 14.
See [ADR 4](0004-server-mix-bus-and-stream-mode.md) for the shared engine boundary.

## Current contracts

Automated selectors identify behavioral checks, not completed execution or ADR
acceptance. Platform and human listening evidence remains a separate obligation.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0015-01` | Stable occurrence identities distinguish repeated songs and preserve take bindings through moves. | automated | `tests/unit/test_prepared_editions.py::test_repeated_song_ids_survive_reorder_and_stale_edit_is_rejected`; `tests/unit/test_prepared_editions.py::test_segment_follows_current_neighbours_and_can_be_reapproved`; `tests/unit/test_prepared_editions.py::test_broken_pair_is_unattached_and_reattach_keeps_asset`; `tests/unit/test_prepared_api.py::test_reattach_route_keeps_source_and_rejects_stale_revision`; `tests/unit/test_prepared_editions.py::test_removing_segment_row_removes_take_and_frees_binding_without_losing_asset` |
| `ADR-0015-02` | Relevant changes invalidate approval without losing audio; unrelated edits preserve approval. | automated | `tests/unit/test_prepared_editions.py::test_intact_pair_move_and_unrelated_edits_preserve_approval_but_changed_song_does_not`; `tests/unit/test_prepared_editions.py::test_segment_follows_current_neighbours_and_can_be_reapproved`; `tests/unit/test_prepared_editions.py::test_broken_pair_is_unattached_and_reattach_keeps_asset`; `tests/unit/test_prepared_review.py::test_appending_after_segment_still_produces_complete_receipt_and_airable_take`; `tests/unit/test_prepared_api.py::test_reattach_route_keeps_source_and_rejects_stale_revision`; `tests/unit/test_prepared_editions.py::test_removing_adjacent_segment_keeps_reapproved_take_valid_on_rename`; `tests/unit/test_prepared_editions.py::test_removing_segment_row_removes_take_and_frees_binding_without_losing_asset` |
| `ADR-0015-03` | Full-target contextual review and explicit approval use server-owned complete, non-omitted receipts. | automated | `tests/unit/test_prepared_review.py::test_pending_review_retains_real_take_and_never_approves_it`; `tests/integration/test_prepared_api.py::test_stopped_edition_import_real_review_explicit_approval_and_retained_audio`; `tests/unit/test_prepared_long_audio.py::test_real_above_threshold_segment_gets_complete_target_receipt`; `tests/unit/test_prepared_review.py::test_appending_after_segment_still_produces_complete_receipt_and_airable_take` |
| `ADR-0015-04` | Approved local audio uses shared placement; long primary segments use bounded normal-speed playback. | automated | `tests/unit/test_prepared_playout.py::test_prepared_handoff_renders_when_announcer_breaks_are_disabled`; `tests/unit/test_prepared_playout.py::test_primary_speech_uses_bounded_long_decode_instead_of_break_decoder`; `tests/unit/test_prepared_long_audio.py::test_real_thirty_minute_primary_decode_is_scoped_and_disk_backed` |
| `ADR-0015-05` | Interrupted editions require explicit resume; ordinary Shows keep automatic resume and hard-event priority. | automated | `tests/unit/test_prepared_playout.py::test_prepared_restart_requires_explicit_chosen_occurrence_and_preserves_ordinary_resume`; `tests/unit/test_prepared_editions.py::test_resume_take_id_maps_to_real_occurrence`; `tests/unit/test_prepared_playout.py::test_progress_saves_real_resume_occurrence_for_edge_clips` |
| `ADR-0015-06` | Station source assets survive backup/restore and browser-only authorization applies. | automated | `tests/unit/test_prepared_backup.py::test_backup_restores_editions_and_source_audio_but_not_partial_imports`; `tests/integration/test_prepared_api.py::test_edition_access_and_early_upload_cap` |
| `ADR-0015-07` | Windows/Chrome/NVDA production, full-target review, approval and listening are demonstrated. | manual | Operator rehearsal occurs after both feature PRs are submitted. Record revision, versions, steps, focus/speech and audible joins; keep the feature open pending evidence. |
| `ADR-0015-08` | Edits crossing the commit boundary preserve current-run approval honestly and invalidate future PCM. | automated | `tests/unit/test_prepared_tail.py::test_boundary_crossed_after_edit_preflight_freezes_current_run_approval`; `tests/unit/test_prepared_tail.py::test_future_take_edit_is_allowed_while_opening_clip_is_committed`; `tests/unit/test_prepared_api.py::test_closing_approval_succeeds_while_opening_clip_plays`; `tests/unit/test_prepared_tail.py::test_edit_lock_uses_rendered_speech_after_approval_changes` |
| `ADR-0015-09` | One live queue choice covers affected current and ready handoffs; keep/discard does not change saved edition approval or delete source audio, started speech retains timing/layers without repetition, and cancel/missing choice leaves the queue unchanged. | automated | `tests/unit/test_live_queue.py::test_keep_copies_prepared_voice_and_preserves_independent_ad_and_news`; `tests/unit/test_live_queue.py::test_discard_drops_only_song_specific_speech_and_intro`; `tests/unit/test_live_queue.py::test_missing_or_cancelled_choice_is_a_conflict`; `tests/unit/test_live_queue_replan.py::test_live_edit_preserves_heard_prefix_and_bus_cursor`; `tests/unit/test_server_audio_queue.py::test_bridge_queue_add_requires_a_fresh_voice_choice_and_keeps_speech`; `tests/unit/test_server_audio_queue.py::test_bridge_queue_add_discards_voice_only_after_matching_choice`; `tests/unit/test_server_audio_queue.py::test_cancelled_bridge_queue_add_has_no_mutation_or_approval` |

## Verification limits

Structural ADR validation does not execute behavioral tests or accept this
decision. Automated audio comparisons do not establish human listening, hardware
routing or screen-reader operation. Matching combined OVC/G05 evidence can be
reused only when it covers the same final revision, relevant inputs and environment,
with each feature's coverage and limits recorded explicitly.
