# 23. Named libraries with station bindings

Date: 2026-11-15
Status: Proposed
Decision authority: Operator requirements in [#801](https://github.com/blindndangerous/AutoDJ/issues/801)
and [#802](https://github.com/blindndangerous/AutoDJ/issues/802); architectural record pending maintainer review.

## Context

Stations need separate music collections without running separate servers.
They may use the same files while keeping different catalog metadata, cues,
source folders and analysis. The single-library implementation in
[ADR 5](0005-a-station-owns-its-playout-chain.md) and
[ADR 6](0006-use-a-catalog-with-explicit-migrations.md) needs an explicit owner
for those resources.

## Decision

Each library has a stable ID and an editable name. Its definition, catalog,
analysis leases, similarity view and DJ cue cache belong to
`libraries/<id>/`. Existing `libraries/main/` data stays in place; adoption
adds its definition without reimporting songs or recomputing analysis.
Malformed definitions are errors, not permission to use another library.

A station saves one library ID in its definition. Saving a different ID
does not change a running engine. The operator can restart now or use the
saved choice on the next start. Before saving, the page checks queue and
programming references against the selected catalog. Missing references
remain visible warnings; the application must not replace them with an
unrelated song or category.

Station settings, history and programming remain station-owned. On restart,
the station's existing ledger moves to the selected library's tick without
discarding buffered plays or clock settings. Catalog lookups use the active
binding. Library tools select a library explicitly; this browser choice never
changes a station's running library.

Library jobs and worker requests carry the library ID. Legacy worker requests
without an ID target main. A lease from one catalog cannot authorize writes
to another. Local maintenance keeps one subprocess slot across the server;
separate catalogs do not imply concurrent model processes. Scans and caches
close only after their users have stopped, as required by
[ADR 8](0008-bound-background-work-outside-playout.md).

Music files remain external to the catalogs. Libraries may register overlapping
folders. Catalog-only tag edits affect one library; writing tags to a shared
file changes that file for every reader, and other catalogs see those changes
on rescan. A changed file identity also invalidates their cached analysis and
file-bound cues under [ADR 6](0006-use-a-catalog-with-explicit-migrations.md).
This feature does not copy, deduplicate or delete music files.

Backups include every definition and catalog. Existing main-library archive
paths remain readable, while named libraries have separate archive paths.
Restore and serve locks remain deployment-wide. Doctor inspects all catalogs
and reports missing station assignments without modifying definitions.

## Alternatives and consequences

Separate servers would duplicate pairing, deployment and audio control.
One mutable process-wide selected library would let browser choices or worker
requests change another station's data. Explicit library owners avoid that
coupling but require every catalog caller to carry its context.

Each open library consumes memory for its catalog view and similarity index.
There is no new memory or real-time performance guarantee. Revisit eager
loading if large installations need eviction. Different embedding models per
library and library deletion are outside these issues.

The General settings selector uses the existing draft/save boundary from
[ADR 17](0017-save-settings-through-one-draft-boundary.md). A canceled warning
does not save; a failed save does not restart the station.

Browse follows the station's library even when Library tools selects another
library. Before changing stations or a station's library, unsaved Track info
edits use the existing Save, Discard or Cancel dialog. The old editor and Browse
results are cleared when the owner changes so they cannot be saved into the
next library.

## Amendment, 2026-11-23: duplicate a library

[Issue #831](https://github.com/blindndangerous/AutoDJ/issues/831) adds a full
library copy with a new name and ID. It copies catalog metadata, sources,
library settings, cues and completed analysis while retaining the original
audio paths. The catalogs and their analysis stores remain independent;
this does not introduce a shared analysis cache across libraries.

SQLite snapshots include committed WAL data. The copy removes worker leases,
pending scans and legacy station history/settings before publishing the new
directory. Failed copies leave the source intact and do not register a partial
library. Station assignments and station-owned files remain unchanged.

## Implementation and operation

- [Definitions](../../src/autodj/library_registry.py), [runtime ownership](../../src/autodj/library_runtime.py),
  and [station preflight](../../src/autodj/library_switch.py).
- [Catalog routes](../../src/autodj/server.py), [worker routes](../../src/autodj/worker_api.py),
  [jobs](../../src/autodj/jobs.py), and [cache ownership](../../src/autodj/dj_meta.py).
- [Backup](../../src/autodj/backup.py), [Doctor](../../src/autodj/doctor.py),
  and [operations](../operations.md#named-music-libraries).

## Current contracts

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| ADR-0023-01 | Main adoption preserves existing catalog data and is repeatable. | automated | `tests/unit/test_library_registry.py::test_ensure_main_adopts_existing_data_and_is_idempotent` |
| ADR-0023-02 | Overlapping music folders do not merge catalog metadata or cues. | automated | `tests/unit/test_library_runtime.py::test_shared_physical_folder_has_independent_catalogs` |
| ADR-0023-03 | A library switch preserves the station ledger and leaves the previous runtime configuration intact. | automated | `tests/unit/test_library_runtime.py::test_library_change_transfers_buffered_history_and_station_settings` |
| ADR-0023-04 | Unknown library IDs do not fall back to main. | automated | `tests/unit/test_library_runtime.py::test_unknown_library_never_falls_back` |
| ADR-0023-05 | Selecting and changing libraries remains usable with keyboard and screen-reader navigation. | manual | On Windows with NVDA, create, rename and duplicate a library, cancel Duplicate, change a station's saved choice, cancel a warning, save for next start, and save with restart. Check focus, names and announcements; record browser and screen-reader versions. |
| ADR-0023-06 | Station library preflight presents running-station choices and stopped-station warnings, and ignores a stale reply. | automated | `tests/jsmodules/time-settings.test.js::offers all three running station choices and their warning text`; `tests/jsmodules/time-settings.test.js::asks before saving a stopped station's library when programming warnings exist`; `tests/jsmodules/time-settings.test.js::drops a library preflight reply after the station context changes` |
| ADR-0023-07 | Catalog tag edits affect one library; shared file rewrites reach other libraries on rescan with existing file-identity invalidation. | automated | `tests/integration/test_named_libraries.py::test_tag_edits_target_one_catalog_and_shared_file_changes_arrive_on_rescan` |
| ADR-0023-08 | Browse and Track info keep their station's catalog context across library and station changes. | automated | `tests/jsmodules/library-folders.test.js::keeps Browse folders separate from the library being managed`; `tests/jsmodules/library-folders.test.js::reads the same path from the newly selected station after clearing Track info`; `tests/jsmodules/time-settings.test.js::keeps the library selection unsaved when Track info editing is canceled` |
| ADR-0023-09 | Duplicate preserves independent catalog data and completed analysis, excludes station history and worker state, and publishes no partial library on failure. | automated | `tests/unit/test_library_duplicate.py::test_duplicate_copies_persistent_analysis_and_settings_but_not_runtime_state`; `tests/unit/test_library_duplicate.py::test_duplicate_failure_removes_stage_and_preserves_source` |

Automated test links identify checks to run; they are not a claim that the
checks or the manual procedure have passed. The PR records that evidence.
