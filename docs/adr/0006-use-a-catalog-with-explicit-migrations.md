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
- Normal startup must refuse data needing explicit index/state migration rather
  than silently importing old generations or repairing a corrupt catalog.
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

## Verification

Checked items refer to the scoped 2026-10-16 runs in
[the audit evidence](verification-2026-10-16.md), including its platform limits.
They do not change this record's Proposed status.

- [x] Verify catalog-only startup, unanalysed tracks, stale-index exclusion,
  corrupt-catalog preservation, and explicit legacy refusal with
  [catalog startup tests](../../tests/unit/test_catalog_startup.py).
- [x] Verify migration preview, failure recovery, and metadata/vector preservation
  with [migration tests](../../tests/unit/test_index_migration.py).
- [x] Verify station history isolation with [ledger tests](../../tests/unit/test_station_ledger.py).
- [x] Verify incompatible/stale worker result rejection with
  [worker API tests](../../tests/unit/test_worker_api.py) and
  [model identity tests](../../tests/unit/test_model_identity.py).

## Evidence gaps

Historical motivation for every storage migration has not been recovered.
The test links do not prove behavior on every filesystem or against every old
release. The FAISS choice remains accepted under ADR 2; acceptance of this
storage/compatibility record remains pending.
