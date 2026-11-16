# 10. Preserve station state through validated writes and recoverable restore

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

Station settings, uploaded clips, history, and credentials have different
recovery and confidentiality needs. The current code has atomic file helpers,
SQLite backup, and journaled restore. This record covers those persistence
boundaries, not cloud sync or a promise that archives include every external file.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Preserve existing data on validation or I/O failure, use atomic replacement
for saved files, and restore only validated archives through the recovery journal.
Keep secret-bearing data outside ordinary station backups.

- Saved JSON follows its owning schema and compatibility policy. For stores
  that retain unknown fields or invalid individual records, preserve them on save;
  do not generalize that behavior to every store without inspecting its loader.
- Use the existing atomic writer for file replacement. Unreadable storage must
  not be treated as an empty configuration and overwritten.
- SQLite databases are backed up through SQLite's backup API; archives include
  declared station assets/state, not music libraries, model weights, broadcast
  recording media, or every externally referenced pack.
- Restore validates members, archive compatibility, station definitions, and
  catalog data before replacing state; serving and restore cannot own it together.
- Replacement is journaled with rollback/recovery. Relay and station secrets
  stay local; restored live-DJ accounts require new passwords.
- Owner-only secret-file handling is platform-specific and must not be inferred
  solely from POSIX mode bits on Windows.

## Alternatives for review

- Validated local files plus SQLite backup and restore journal: compatible with
  existing stores and portable archives; requires crash/rollback testing.
- Copy database files and overwrite state in place: simple implementation, but
  can miss WAL data or leave a partly replaced station after failure.
- Centralize all settings in a new database: transactions could reduce file
  boundaries, but require migration and do not remove asset/secret backup decisions.

## Consequences and revisit conditions

New persistent features must declare station/shared ownership, validation,
secret handling, backup membership, and recovery behavior. Revisit when archive
compatibility or external-asset ownership changes. Atomic replacement is not a
guarantee against every hardware/filesystem failure.

## Implementation and operational references

- [Filesystem helpers](../../src/autodj/fsutil.py), [saved settings](../../src/autodj/runtime_state.py),
  [station definitions](../../src/autodj/station_creation.py), and [secrets](../../src/autodj/secret_files.py).
- [Backup and restore](../../src/autodj/backup.py), [station stores](../../src/autodj/station_store.py),
  and [secret exclusions and account sanitization](../../src/autodj/backup.py).
- [Backup](../operations.md#backup), [restore](../operations.md#restore-and-validate),
  [upgrade](../operations.md#upgrade-checklist), and [threat model](../../THREAT_MODEL.md).

This documentation pass adds no runtime dependency, configuration switch, or
migration. Any future change to this boundary needs its own affected-path and
compatibility assessment.

## Current contracts

The separate [programme review proposal](0014-isolate-programme-review-and-export.md)
defines immutable derived reviews, interrupted-job cleanup and backup exclusions.
Its recovery never starts station playout. This retrospective record remains
Proposed.

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0010-01` | Failed atomic file writes preserve the previous file and remove temporary output. | automated | `tests/unit/test_fsutil.py::test_failed_write_keeps_old_file_and_removes_temp` |
| `ADR-0010-02` | Failed restore rolls back the station database and clears its recovery journal. | automated | `tests/unit/test_station_database_backup.py::test_failed_restore_rolls_back_station_database` |
| `ADR-0010-03` | Backups include committed station WAL data and preserve removed-station ledgers without enabling them. | automated | `tests/unit/test_station_database_backup.py::test_station_backup_includes_live_wal_and_unconfigured_station` |
| `ADR-0010-04` | Restore leaves station relay credentials local rather than installing archived secrets. | automated | `tests/unit/test_station_backup.py::test_restore_skips_relay_settings_in_an_archive_that_has_them` |
| `ADR-0010-05` | Real power-loss/network-filesystem recovery lacks environment verification in this audit. | gap | Unit failure injection does not establish real power-loss or network-share durability; retain explicit evidence limits. |
| `ADR-0010-06` | Every consolidated JSON store keeps the old file and removes temporary output when writing or replacing fails. | automated | `tests/unit/test_store_writes.py::test_failed_store_write_keeps_old_file_and_cleans_temporary` |
| `ADR-0010-07` | Shared recovery retains operator-visible filename styles and preserves partially usable schedules at their original path. | automated | `tests/unit/test_fsutil.py::test_set_aside_preserves_recovery_filename_styles`; `tests/unit/test_fsutil.py::test_set_aside_copy_preserves_the_live_schedule` |
| `ADR-0010-08` | After replacement, a refused directory flush logs a warning without reporting the committed save as failed; stores update in-memory state. | automated | `tests/unit/test_fsutil.py::test_directory_flush_failure_keeps_committed_write_and_warns`; `tests/unit/test_store_writes.py::test_time_checks_publish_saved_state_when_directory_flush_is_refused` |
| `ADR-0010-09` | Archive state is atomically recorded without changing the station's saved autostart setting; restore does not start the station. | automated | `tests/unit/test_station_deletion.py::test_archive_restore_preserves_autostart_without_starting`; `tests/unit/test_station_deletion.py::test_archive_stops_the_station_before_writing_its_marker`; `tests/unit/test_station_deletion.py::test_archive_refuses_a_stop_with_warnings_or_a_stuck_player`; `tests/unit/test_station_deletion.py::test_archive_marker_is_loaded_after_configuration_reload`; `tests/integration/test_station_routes.py::test_archived_default_cannot_start_or_delete_and_restore_does_not_start`; `tests/integration/test_station_routes.py::test_archive_unknown_station_is_404_and_maintenance_blocks_named_routes`; `tests/jsmodules/station-create.test.js::archives and restores from the same row, preserving focus and the stopped state` |
| `ADR-0010-10` | Before deletion commit a station remains recoverable as a whole; after commit recovery completes forward and retains station-owned audio by default. | automated | `tests/unit/test_station_deletion.py::test_delete_quarantines_then_retains_station_audio_and_journal`; `tests/unit/test_station_deletion.py::test_completed_journal_recovery_is_forward_only_and_repeatable`; `tests/unit/test_station_deletion.py::test_read_only_config_and_doctor_leave_pending_deletion_untouched`; `tests/unit/test_station_deletion.py::test_backup_and_deletion_serialize_station_audio_snapshot`; `tests/integration/test_station_routes.py::test_committed_delete_cleanup_failure_unregisters_station`; `tests/unit/test_backup.py::test_station_archive_marker_and_retained_audio_round_trip`; `tests/unit/test_backup.py::test_backup_refuses_unknown_retained_station_audio_children`; `tests/jsmodules/station-create.test.js::requires exact deletion confirmation, defaults voice removal off, and returns focus after removal`; `tests/jsmodules/station-create.test.js::ignores a lifecycle reply from an old login without losing the active control` |
| `ADR-0010-11` | Permanent deletion refuses the default station and station folders overlapping registered or configured music roots. | automated | `tests/unit/test_station_deletion.py::test_default_station_cannot_be_permanently_deleted`; `tests/unit/test_station_deletion.py::test_delete_refuses_music_source_overlap_without_moving_station` |

## Amendment 2026-10-31: shared persistence helpers

The owner explicitly approved consolidating existing filesystem operations.
This focused amendment does not accept the broader retrospective proposal.
Cart, live-DJ, clockwheel and time-check JSON writes use `fsutil.atomic_write`.
Live-DJ credentials, clockwheels and time-check settings retain their previous
private creation modes; cart state retains its existing default mode. A failed
write removes its temporary file, and replacement uses the shared bounded
Windows sharing retries.

Owner review clarification, 2026-10-31: after replacement, directory fsync is
best-effort. An `OSError` there logs a warning and leaves the save successful,
so callers update their in-memory state to match the new file. Failures writing
or flushing the temporary file, or replacing the destination, still propagate.
This does not establish durability after power loss on a share that refuses fsync.

Invalid state uses `fsutil.set_aside`. The helper retains the documented
`.bad-<id>`, `.<time_ns>.bad` and `.<32 hex>.bad` recovery names. The scheduler
uses its copy option because a partially valid schedule must remain on disk
until its usable events are saved. Stores that previously refused to continue
after set-aside failure still propagate that failure. See the
[station file reference](../reference/operations/stations.md) for names.

## Proposed amendment, 2026-11-15: station deletion recovery (#800)

This proposal extends the recovery boundary to permanent station deletion. The
station directory is renamed into a same-volume quarantine only after a durable
prepared journal exists. That rename is the logical deletion commit; before it,
failure restores the complete station, and after it, recovery only finishes
forward. Recovery validates journal files and path components, rejects links
and shared-file hard links, and does not follow configured external media paths.
Operator-owned station audio is retained outside the deleted station folder by
default, with a manifest, and backups preserve that retained tree. Catalog
cleanup follows the commit and leaves a completion journal until every catalog
has removed the station's exact owned source and history.

This amendment keeps the record's Proposed status. The mapped tests provide
implementation evidence, not an acceptance decision or a claim of power-loss
durability on every filesystem.

## Verification

Run the following checks for affected changes and record results and platform
limits in the relevant PR. Test links do not establish execution or change this
record's Proposed status.

- [ ] Verify atomic-write cleanup and filesystem error behavior with
  [filesystem tests](../../tests/unit/test_fsutil.py).
- [ ] Verify invalid saved fields and valid-field preservation with
  [saved-state tests](../../tests/unit/test_runtime_state.py).
- [ ] Verify archive validation, secret exclusions, rollback/recovery, and serving
  refusal with [backup tests](../../tests/unit/test_backup.py) and
  [station backup tests](../../tests/unit/test_station_backup.py).
- [ ] Verify current station databases are backed up coherently with
  [database backup tests](../../tests/unit/test_station_database_backup.py).

## Evidence gaps

Platform-specific private-file semantics, network shares, and real power-loss
recovery need their own environment evidence. This record does not claim a
backup exists on an operator's machine. Historical rationale and acceptance
remain pending.

## Related proposal — 2026-10-17

Prepared editions preserve source assets in backups and require explicit resumption after interruption, while ordinary saved Shows retain automatic resume. The focused proposal is [ADR 15](0015-prepare-speech-for-specific-show-editions.md). This cross-reference does not accept that proposal or change this record's status.
