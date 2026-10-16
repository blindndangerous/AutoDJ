# 8. Bound background work outside live playout

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

Indexing, previewing, synthesis/decoding, and remote analysis have costs and
failure modes different from the clock-paced mix bus in
[ADR 4](0004-server-mix-bus-and-stream-mode.md). This record covers ownership,
isolation, and shutdown; it introduces no general-purpose job platform.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Use the existing bounded mechanisms for each workload: a single-slot CLI
subprocess runner for maintenance, leased catalog analysis for workers, isolated
simulation workers, and render-ahead preparation for audio.

- Maintenance accepts only vetted commands and flags, runs without a shell,
  refuses a concurrent start, and retains bounded recent output.
- Server and remote analysis coordinate through SQLite leases; expiration and
  stale/failed holds must avoid permanently lost work or tight retry loops.
- Preview and rotation work use their existing worker, cancellation, duration,
  queue, and output bounds. Read those constants at the implementation; they
  are resource controls, not promised response-time service levels.
- Render-ahead may prepare a replacement while retaining a usable fallback;
  normal queue refresh must not discard the fallback prematurely. Hard resets
  have different semantics and can temporarily leave no prepared audio.
- Lifecycle cleanup must not release a shared library, mastering process, or
  licence while a worker still owns it. Shutdown failure is observable; it is
  not permission to start another owner on top of an unfinished one.

## Alternatives for review

- Workload-specific bounded workers: reuse current failure/resource controls,
  but each mechanism needs lifecycle tests.
- Inline work on request/audio paths: fewer components, but slow work or model
  failures can stall control or playout.
- A durable distributed queue: broader scheduling/retry capabilities, but adds
  a service and operational ownership not required by current workloads.

## Consequences and revisit conditions

One limit cannot be assumed to govern every workload. New background work
must choose an owner, bounded admission/output, cancellation, and cleanup path.
Revisit if concurrent jobs or durable resumable command execution become an
actual requirement. Unit tests do not establish a hard real-time guarantee.

## Implementation and operational references

- [Jobs](../../src/autodj/jobs.py), [analysis leases](../../src/autodj/analysis_queue.py),
  [worker protocol](../../src/autodj/worker_api.py), and [render ahead](../../src/autodj/render_ahead.py).
- [Preview](../../src/autodj/hour_preview.py), [simulation](../../src/autodj/rotation_sim.py),
  [station lifecycle](../../src/autodj/stations.py), and [child ownership](../../src/autodj/child_jobs.py).
- [GPU workers](../operations.md#using-a-gpu-machine),
  [hour preview](../operations.md#hour-preview-and-backtiming), and
  [station soak testing](../operations.md#soak-test).

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
| `ADR-0008-01` | Maintenance rejects unsupported commands/flags and concurrent starts. | automated | `tests/unit/test_jobs.py::TestJobManagerStart::test_rejects_unknown_subcommand`; `tests/unit/test_jobs.py::TestJobManagerStart::test_rejects_flags_the_web_ui_never_sends`; `tests/unit/test_jobs.py::TestJobManagerStart::test_refuses_concurrent_start` |
| `ADR-0008-02` | Retained job output stays bounded and retains recent progress from a verbose run. | automated | `tests/unit/test_jobs.py::test_verbose_job_keeps_recent_output_without_retaining_the_entire_run` |
| `ADR-0008-03` | Analysis leases exclude competing holders until expiry and failed work receives a retry hold. | automated | `tests/unit/test_analysis_queue.py::test_a_song_is_leased_to_one_holder_until_it_expires`; `tests/unit/test_analysis_queue.py::test_finishing_counts_and_holds_back_failed_songs` |
| `ADR-0008-04` | A preview stops at its declared work limits and runs outside the request event loop. | automated | `tests/unit/test_hour_preview.py::test_the_dry_run_stops_at_its_limits`; `tests/unit/test_hour_preview.py::test_previews_run_on_their_own_worker_thread` |
| `ADR-0008-05` | Mastering child processes exit when AutoDJ ends on Windows (requires a Windows run). | automated | `tests/unit/test_station_runtime.py::test_mastering_children_die_with_autodj_on_windows` |

## Verification

Checked items refer to the scoped 2026-10-16 runs in
[the audit evidence](verification-2026-10-16.md), including its platform limits.
They do not change this record's Proposed status.

- [x] Verify allowlists, one job slot, bounded logs, and stop behavior with
  [job tests](../../tests/unit/test_jobs.py).
- [x] Verify exclusive leases, expiration, renewal, and failed-work holds with
  [analysis queue tests](../../tests/unit/test_analysis_queue.py).
- [x] Verify soft-refresh fallback and reset races with
  [render-ahead tests](../../tests/unit/test_render_ahead.py).
- [x] Verify lifecycle failure and cleanup ownership with
  [station runtime tests](../../tests/unit/test_station_runtime.py).
- [x] Verify isolated/bounded previews with
  [preview tests](../../tests/unit/test_hour_preview.py).

## Evidence gaps

CPU/memory limits, audio continuity under load, and Windows process cleanup
need applicable platform/soak evidence. Timing constants alone prove none of
those outcomes. Historical rationale and final acceptance remain pending.
