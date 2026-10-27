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
