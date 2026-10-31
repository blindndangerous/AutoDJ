# 6. Use a playable catalog with explicit legacy migrations

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

The original [FAISS ADR](0002-faiss-for-vector-search.md) predates the
playable catalog. Tracks must remain available before advanced analysis and
without published vector files. Station history/settings have also moved to
station-owned stores under [ADR 5](0005-a-station-owns-its-playout-chain.md).
This record covers persistence and vector compatibility, not a replacement
ranking algorithm or a switch to a different embedding model.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Use the shared SQLite `library.db` catalog for playable library records and
stored analysis vectors. Build the process's FAISS search representation from
that catalog; FAISS files from older releases are migration inputs.

- Basic scanning and playback must not require every track to have a vector.
- Station-owned history/settings use `StationStore` and `StationLedger`; do not
  infer current ownership from the legacy tables still present in catalog code.
- Normal startup refuses FAISS-only libraries and v1 runtime state requiring
  explicit `migrate-index` commands, and must not repair a corrupt catalog.
  This does not prohibit automatic migration of the old index layout, station
  history into `station.db`, or TOML station definitions into `station.json`.
- Preview `migrate-index` before applying it; preserve recovery data and reject
  invalid/corrupt inputs before publishing replacements.
- Remote vectors must match the server's model, resolved weight identity, analysis
  version, expected dimensions, finiteness, normalization, and claimed file identity.
  A branch name alone is not a compatible-weight identity.

## Alternatives for review

- Catalog plus derived in-memory FAISS: playable unanalysed tracks and one
  persistent library; requires coherent change tracking and compatible vectors.
- Persist independent metadata/vector generations: useful self-contained search
  snapshots, but introduces multiple publication units and does not by itself
  represent unanalysed playable records.
- External vector/database service: can centralize remote access, but introduces
  a service dependency contrary to the existing local-library boundary.

## Consequences and revisit conditions

Catalog and analysis changes must preserve unanalysed playback and station
isolation. Legacy import and data-layout migration are distinct operations;
consult their actual entrypoints before changing startup. Revisit if multiple
library owners or a different embedding representation becomes a requirement.
No ANN scaling claim or benchmark threshold is established by this record.

## Implementation and operational references

- [Catalog](../../src/autodj/catalog.py), [library orchestration](../../src/autodj/library.py),
  [station store](../../src/autodj/station_store.py), and [ledger](../../src/autodj/station_ledger.py).
- [Explicit index migration](../../src/autodj/index_migration.py),
  [layout migration](../../src/autodj/layout.py), and [worker validation](../../src/autodj/worker_api.py).
- [Model identity](../../src/autodj/model_identity.py), [model selection](../model-selection.md),
  [moving files](../operations.md#moved-files), and [multiple machines](../operations.md#more-than-one-machine).

This documentation pass adds no runtime dependency, configuration switch, or
migration. Any future change to this boundary needs its own affected-path and
compatibility assessment.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0006-01` | Catalog tracks remain playable before advanced analysis and without a legacy vector manifest. | automated | `tests/unit/test_catalog_startup.py::test_serve_uses_catalog_counts_and_can_seed_an_unanalyzed_song`; `tests/unit/test_catalog_startup.py::test_catalog_survives_without_a_manifest_and_ignores_stale_similarity` |
| `ADR-0006-02` | Legacy-only startup refuses explicit-migration inputs without mutating them. | automated | `tests/unit/test_catalog_startup.py::test_legacy_only_startup_requires_explicit_migration_without_mutation` |
| `ADR-0006-03` | Migration preview writes nothing; applied migration preserves vectors and metadata. | automated | `tests/unit/test_index_migration.py::test_dry_run_writes_nothing`; `tests/unit/test_index_migration.py::test_apply_preserves_vectors_metadata_and_reports_missing_file` |
| `ADR-0006-04` | Worker results reject incompatible model/weight/version identities, invalid vectors, and files changed since claim. | automated | `tests/unit/test_worker_api.py::test_results_from_other_weights_or_versions_are_refused`; `tests/unit/test_worker_api.py::test_invalid_vectors_are_refused`; `tests/unit/test_worker_api.py::test_a_result_for_a_changed_file_is_refused_as_stale` |
| `ADR-0006-05` | Changed file identity invalidates cached timing analysis; fresh cues cannot inherit old Intro/Outro, and disabled DJ analysis leaves work pending. | automated | `tests/unit/test_library.py::test_changed_file_recalculates_cues_without_inheriting_dj_cache`; `tests/unit/test_library.py::test_deferred_replacement_clears_old_dj_cache_before_publishing_identity`; `tests/unit/test_library.py::test_rescan_reads_external_tag_edit_with_unchanged_size` |
| `ADR-0006-06` | Audio-feature results are versioned and tied to catalog file identity; storing estimates preserves manual key/BPM and a positive source BPM. | automated | `tests/unit/test_catalog_audio_analysis.py::test_audio_analysis_cache_is_identity_checked_and_survives_source_changes`; `tests/unit/test_catalog_audio_analysis.py::test_audio_estimates_preserve_manual_tags_and_existing_source_bpm` |
| `ADR-0006-07` | Station history keeps audio aired over or between the songs, and a show's standalone speech segments, with its kind in the existing `play_history.kind` column, with no schema change; existing station databases read as before. The repeat rules, pending repeat rows, the default play-log export and SoundExchange reports count songs and live sets only, and audio mixed over a song never shortens its reported duration, while a standalone speech segment ends it as the next song would. Inserted rows have their own room, so the songs-only History view keeps its 500 rows live and after a restart. | automated | `tests/unit/test_history_inserts.py::test_repeat_rules_count_songs_only`; `tests/unit/test_history_inserts.py::test_the_play_log_leaves_inserted_items_out_and_keeps_song_lengths`; `tests/unit/test_history_inserts.py::test_a_database_written_before_inserted_items_keeps_working`; `tests/unit/test_history_inserts.py::test_pending_rows_for_the_repeat_rules_leave_inserted_items_out`; `tests/unit/test_history_inserts.py::test_the_export_counts_songs_unless_asked_for_everything`; `tests/unit/test_history_inserts.py::test_a_show_speech_segment_is_speech_not_a_song`; `tests/unit/test_history_inserts.py::test_inserted_items_never_push_songs_out_of_history`; `tests/unit/test_history_inserts.py::test_a_standalone_speech_segment_ends_the_song_before_it` |
| `ADR-0006-08` | Inserted audio is recorded as it actually airs on a station's playout, from in-memory buffers that touch no disk on the mix-bus thread; it never changes last-played times. The mix bus says when a liner, cart or time check starts: at once, at its cue, or when a live DJ hands back; one it drops is never recorded. Scheduler snapshots used for forecasts never record. | automated | `tests/unit/test_history_inserts.py::test_the_bridge_records_an_inserted_item_without_disturbing_song_history`; `tests/unit/test_history_inserts.py::test_the_player_reports_each_handoff_item_once_as_it_airs`; `tests/unit/test_history_inserts.py::test_a_scheduler_snapshot_never_reports`; `tests/unit/test_history_inserts.py::test_a_liner_held_for_a_live_dj_is_recorded_when_it_airs`; `tests/unit/test_history_inserts.py::test_the_bus_says_when_a_cued_liner_really_starts`; `tests/unit/test_live.py::test_a_liner_due_under_a_dj_plays_once_after_and_a_time_check_never` |
| `ADR-0006-09` | Serve checks library compatibility read-only before startup writes, locks or station construction. Doctor uses the same check: legacy data offers migration; a newer manifest or catalog schema requires a compatible AutoDJ version. | automated | `tests/unit/test_serve_refusal.py::test_library_refusal_is_read_only_and_matches_doctor`; `tests/unit/test_serve_refusal.py::test_catalog_refusal_never_writes_sqlite_sidecars`; `tests/unit/test_serve_refusal.py::test_large_checkpointed_catalog_is_inspected_without_copying`; `tests/unit/test_serve_refusal.py::test_cli_passes_successful_library_preflight_to_server_and_manager` |

## Clarification, 2026-10-22: analysis follows file identity

[Issue #545](https://github.com/blindndangerous/AutoDJ/issues/545) repairs rescan
invalidation: changed size/fingerprint invalidates the path-keyed DJ analysis,
including cached Intro/Outro, beats and cue markers. Basic analysis uses the
current audio without importing those old suggestions. With DJ analysis disabled,
the cleared cache remains unanalysed so later enabled analysis processes the file.
Invalidation happens when discovery observes replacement bytes, before deferred
scans publish the new identity. Edits made while that new scan is pending survive
completion. Unchanged file identities retain existing manual cues and legacy suggestions.
This clarifies cache coherence; the record retains its Proposed status.

## Clarification, 2026-10-24: a matching size and time stand for the fingerprint

[Issue #578](https://github.com/blindndangerous/AutoDJ/issues/578): a rescan reads a file's
fingerprint only when its size or modification time differs from the record, the record has
no stored time, the time is too recent to trust, or the rescan overrides tags. New bytes under
the same size and time are seen at the next change of either. Contract `ADR-0008-34` in
[ADR 8](0008-bound-background-work-outside-playout.md) records the rule; the record retains
its Proposed status.

## Amendment clarification 2026-10-27: shared audio features and three folder modes

[Issue #594](https://github.com/blindndangerous/AutoDJ/issues/594) separates basic scanning,
shared audio analysis and MuQ similarity vectors. Music sources request Basic, Audio or Similarity;
overlapping music sources use the strongest request, while a liner source keeps liner handling at
Basic. Existing `advanced=true` source settings migrate to Similarity. A disabled source migrates
to Audio when the saved `dj_analysis` setting is enabled (default true), otherwise Basic. Liners
remain Basic.

The catalog stores a versioned audio-feature result independently from its vector. A rescan reuses
that result for a matching file identity and ignores it after an identity or analysis-version
change. Replacing audio features does not discard an existing vector; a replacement vector is
published separately. New features use 44.1 kHz audio for agreement between local and remote
jobs; older vectors remain saved and are not recomputed just for this migration. Feature extraction
for tracks over 15 minutes uses the first and last five minutes. The Similarity maximum-length
setting controls MuQ vector work; longer tracks may still receive Audio analysis. This extends the
proposed record and does not change its status.

## Amendment 2026-10-28: station history includes inserted audio

[Issue #601](https://github.com/blindndangerous/AutoDJ/issues/601) adds liners and other non-music
items to History. A station's `play_history` table already had a `kind` column, `song` or `live`.
Rows for audio aired over or between the songs now use the same column with the kinds in
[`history_kinds.py`](../../src/autodj/history_kinds.py): `liner`, `ad`, `matched_intro`, `time_check`,
`cart`, `voice_track`, `speech` and the announcer break kinds. No column or table is added, so an
existing `station.db` needs no migration. A row with an unknown kind is still written as a song, as
before. A show's standalone speech segment airs as a track but is recorded as `speech`.

The player reports handoff liners, breaks (including ad spots and voice tracks) and matched intros
as the bus plays past them. The liner scheduler hands each liner, time check and cart to the mix bus
with a callback that the bus calls when the clip really starts: at once, at its cue, or when a live
DJ hands back. A clip the bus drops is never recorded. Both hand the row to
`PlayerBridge.record_aired`, which buffers it in the station ledger like a song play. An inserted
row has no listener count and does not change last-played times.

In memory and on reload, songs and live sets keep their 500 rows and inserted rows have room for
1,500 beside them, so a busy hour of liners never shortens the songs-only view. Pages are built by
walking both lists newest first, copying only the rows on the page.

Readers that count music keep counting songs: the repeat rules (`recent_plays` and pending rows), the
now-playing history, the play-log export unless it asks for everything, and SoundExchange reports. A
song's reported duration still ends at the next song, live set or standalone speech segment, not
at a liner mixed over its end.
History shows songs, liner files played as tracks and live sets by default, as before; asked
for liners, it shows every row with its kind. The
CSV export's liner choice adds inserted rows except ads, which still come from the ad play log.

Older AutoDJ releases opened on a database with inserted rows would list them as songs in History,
play-log exports and SoundExchange reports, and end song durations at them; the
[upgrade guide](../guides/operations/upgrade.md#going-back-to-an-older-release) warns operators.
This extends the proposed record and does not change its status.

## Clarification 2026-10-31: check library compatibility before startup writes

The owner's serve refusal policy requires a read-only compatibility check before startup
moves files, opens writable databases, claims its lock or constructs stations. Doctor and
serve use the same detection for the current library directory, the previous layout and
all configured stations' saved state. Legacy data offers the existing explicit migration
through `autodj doctor --fix`; newer schemas require a compatible AutoDJ version or backup.

Current catalogs have SQLite `user_version = 0`. A positive version is unsupported by this
release and is refused before opening the catalog for writes. This check does not introduce
a new schema migration system. A current catalog still supersedes a retired index manifest,
so stale generation files do not prevent startup. An empty library remains a warning.

SQLite read-only connections can create WAL sidecars. After review, compatibility inspection
uses immutable mode when no WAL exists, and reads in place when WAL and shared memory exist.
Only an orphaned WAL without shared memory needs a temporary copy, because SQLite must
reconstruct its sidecar and immutable mode would ignore pending changes. Ordinary startup
does not copy the catalog or need proportional temporary space. Existing shared-memory read
marks may change during a read; database and WAL content stay unchanged. The CLI passes its
successful preflight to the server and library manager, so startup inspects compatibility once.

This clarifies the proposed record without changing its status. The broader startup policy
is recorded in [ADR 19](0019-manage-server-certificates-on-the-host.md#clarification-2026-10-31-serve-refusal-policy).

## Verification

Run the following checks for affected changes and record results and platform
limits in the relevant PR. Test links do not establish execution or change this
record's Proposed status.

- [ ] Verify catalog-only startup, unanalysed tracks, stale-index exclusion,
  corrupt-catalog preservation, and explicit legacy refusal with
  [catalog startup tests](../../tests/unit/test_catalog_startup.py).
- [ ] Verify migration preview, failure recovery, and metadata/vector preservation
  with [migration tests](../../tests/unit/test_index_migration.py).
- [ ] Verify station history isolation with [ledger tests](../../tests/unit/test_station_ledger.py).
- [ ] Verify incompatible/stale worker result rejection with
  [worker API tests](../../tests/unit/test_worker_api.py) and
  [model identity tests](../../tests/unit/test_model_identity.py).

## Evidence gaps

Historical motivation for every storage migration has not been recovered.
The test links do not prove behavior on every filesystem or against every old
release. The FAISS choice remains accepted under ADR 2; acceptance of this
storage/compatibility record remains pending.
