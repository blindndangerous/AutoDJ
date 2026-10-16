# ADR reconciliation verification, 2026-10-16

This is evidence for the documentation review on `docs/adr-requirements`, based
on code revision `3b3b5b3d`. It is not an architecture decision or a live task list.
The [index](README.md) identifies accepted historical decisions and seven
retrospective Proposed records awaiting explicit maintainer acceptance.

This report covers the initial documentation-only phase. The subsequent
[contract-enforcement report](contract-verification-2026-10-16.md) records later
validator, test, hook, and guidance changes and their separate verification.

## Scope and method

Reviewed the runtime module inventory, existing ADRs, contributor guidance,
operations, security/accessibility/model/setup docs, package/CI configuration,
and tests governing the boundaries in the index. Followed important ADR claims
to their implementing paths and relevant tests. Historical accepted text was
preserved with dated amendments and current-scope notices.

The changes are documentation, issue/PR templates, and agent entrypoints.
No runtime source, existing tests, lockfiles, dependencies, or workflows changed.
Existing tests already exercise the important invariants identified here; no
new test suite or documentation-check framework was added.

Review corrected these concrete discrepancies:

- ADR 2's storage layout and ADR 5's station settings/history/output restrictions
  were historical. Current storage and ownership are explicitly separated from
  those original descriptions.
- The selection guide called the removed `SimilarityIndex.from_index_dir` API.
  Its replacement reads a bounded sample of stored vectors through read-only
  SQLite. The introduction now distinguishes similarity analysis from basic
  playable catalog records.
- Backup/restore and threat-model prose assigned history to `library.db`.
  Current history/clock/last-played state lives in each station's `station.db`.
  Restore documentation now covers valid archived station definitions and
  ledger-only removed-station recovery, matching the current tests.
- A draft overstated Torch-free diagnostics. Importing Doctor and checking an
  empty index avoid heavy runtimes; the model-cache check imports `model.py`
  and Torch without constructing the model or loading weights. ADR 12 records
  that distinction and the remaining decision gap.

## Environment

Linux, CPython 3.14.8, Node 26.11.1. Used an ignored `.venv` with system-site
packages, lockfile versions of the top-level tools/runtime dependencies needed
for these checks, and an editable `autodj` install without dependency expansion.
This was not a full `uv sync --frozen --all-extras` installation. Torch, MuQ,
Transformers, and model weights were not installed/downloaded for this review.
Frontend tools came from `npm ci --ignore-scripts` and the checked-in lockfile.

FFmpeg was absent from PATH and the standard executable locations, confirmed
outside the sandbox. For the later checks only, `imageio-ffmpeg` 0.6.0 supplied
FFmpeg 7.0.2 through an ignored `.cache/bin/ffmpeg` symlink. No system packages
or tracked dependency declarations changed.

## Executed checks

All test chunks ran serially. Focused pytest commands deliberately cleared the
default coverage options with `-o addopts=`; these results do not measure the
repository's full-suite coverage floor. Scratch files stayed under ignored
`.cache/` because shared `/tmp` space was limited.

| Check | Result |
| --- | --- |
| Catalog startup, station config/settings/creation/ledger, jobs, leases, filesystem/state, model identity, launcher | 252 passed, 1 skipped |
| Migration, worker/token authorization, middleware/pairing, station routes, stopped-station editing; initial environment | 304 passed, 3 failed because FFmpeg was unavailable |
| Stopped-station rerun plus mix bus, render ahead, output modes/formats, station lifecycle, recording, live input; FFmpeg available | 258 passed, 1 skipped; all three initial failures resolved |
| Simulation, hour preview, shows, DMCA logic, playout timing, time checks, similarity, listener requests | 362 passed |
| Backup, station backup, database recovery, lightweight Doctor import/empty-index contract | 128 passed, 3 skipped |
| Five JavaScript suites: announcements, dialogs, hotkeys, station switching, stream controls | 178 passed across 5 files |
| Corrected selection example on real temporary catalogs with 0, 2, and 513 analysed tracks | Passed; insufficient-input guard, 512-row bound, output, and unchanged database bytes checked |

Skip locations are platform-specific filesystem/sharing, Windows job-object,
and junction checks. The Linux results do not validate those Windows behaviors.
The passing counts are per run, including the stopped-station rerun; they are
not a count of unique tests. Python runs reported a Starlette/AnyIO deprecation
warning; Node reported an experimental localStorage warning. Neither failed
the assertions. No runtime defect was established by the initial missing-tool
failures, so no runtime repair was made.

The focused commands, run from the worktree, were:

```bash
.venv/bin/python -m pytest -o pythonpath=src -o addopts= -p no:cacheprovider --basetemp=.cache/pytest-adr1 -q tests/unit/test_catalog_startup.py tests/unit/test_station_config.py tests/unit/test_station_settings.py tests/unit/test_station_creation.py tests/unit/test_station_ledger.py tests/unit/test_jobs.py tests/unit/test_analysis_queue.py tests/unit/test_fsutil.py tests/unit/test_runtime_state.py tests/unit/test_model_identity.py tests/unit/test_launcher.py

.venv/bin/python -m pytest -o pythonpath=src -o addopts= -p no:cacheprovider --basetemp=.cache/pytest-adr2 -q tests/unit/test_index_migration.py tests/unit/test_worker_api.py tests/unit/test_machine_tokens.py tests/integration/test_server_security.py tests/integration/test_pairing_server.py tests/integration/test_station_routes.py tests/integration/test_stopped_station_editing.py

PATH="$PWD/.cache/bin:$PATH" .venv/bin/python -m pytest -o pythonpath=src -o addopts= -p no:cacheprovider --basetemp=.cache/pytest-adr3 --timeout=60 -q tests/integration/test_stopped_station_editing.py tests/unit/test_mixbus.py tests/unit/test_render_ahead.py tests/unit/test_player_stream_mode.py tests/unit/test_stream_output.py tests/unit/test_stream_output_formats.py tests/unit/test_station_runtime.py tests/unit/test_recorder.py tests/integration/test_live_source.py

PATH="$PWD/.cache/bin:$PATH" .venv/bin/python -m pytest -o pythonpath=src -o addopts= -p no:cacheprovider --basetemp=.cache/pytest-adr4 --timeout=60 -q tests/unit/test_rotation_sim.py tests/unit/test_hour_preview.py tests/unit/test_shows.py tests/unit/test_dmca.py tests/unit/test_playout_timing.py tests/unit/test_time_checks.py tests/unit/test_similarity.py tests/integration/test_request_page_server.py

PATH="$PWD/.cache/bin:$PATH" .venv/bin/python -m pytest -o pythonpath=src -o addopts= -p no:cacheprovider --basetemp=.cache/pytest-adr5 --timeout=60 -q tests/unit/test_backup.py tests/unit/test_station_backup.py tests/unit/test_station_database_backup.py tests/unit/test_doctor.py::test_import_and_empty_index_check_do_not_import_heavy_runtimes

npm test -- tests/jsmodules/live-region.test.js tests/jsmodules/confirm-dialog.test.js tests/jsmodules/hotkeys.test.js tests/jsmodules/stations.test.js tests/jsmodules/stream-mode.test.js
```

For a normal fully bootstrapped installation, use the repository's documented
`uv run` commands; the explicit interpreter above records this audit environment.

## Documentation and review checks

A temporary read-only checker inspects local Markdown link targets and heading
fragments across changed/new documents, parses the feature template and Cursor
front matter, checks unique/expected feature field IDs, checks all seven new
records retain Proposed status and required content sections, and verifies the
five original ADR bodies remain intact after removing only the inserted notices.
Final validation passed: 370 local links and heading fragments across 27
changed/new paths; template/front-matter parsing, expected field IDs, history
preservation, and Proposed statuses passed. `git diff --check` passed, and all
changed/new files have final newlines and no trailing whitespace. The checker
is an audit aid, not a newly declared project tool or dependency.

A separate read-only review found the Torch-boundary overstatement and an
incorrect backup-policy implementation link. Both were corrected and rechecked.
The later selection/history/restore corrections were also independently reviewed
without further findings. Agent/contributor guidance consistently keeps GitHub
authoritative and Beads optional; Claude and Cursor entrypoints point to AGENTS.md.

## Limits and acceptance

- Seven retrospective ADRs remain Proposed. Existing implementation and passing
  tests do not establish maintainer acceptance or unknown historical rationale.
- Full CI, full coverage, the complete native gate, MuQ/Transformers inference,
  physical audio/GPU tests, multi-station soak measurements, installed-release
  smoke checks, and container builds were not run for this documentation-only change.
- No browser UI changed. The documented Playwright audits and fresh screen-reader
  release sampling were not run; existing samples are historical evidence only.
- Windows-specific behavior and real power-loss/network-filesystem recovery were
  not established by these Linux unit/integration tests.
- Full CI remains required on the final revision before merging, as specified in
  [CONTRIBUTING](../../CONTRIBUTING.md). Unrun conditional verification items in
  Proposed ADRs remain unchecked. A documentation link is never itself a test result.
