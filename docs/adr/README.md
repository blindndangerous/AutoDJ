# Architecture decision records

Start here before changing architecture. Read the applicable record's status
and latest dated amendment before interpreting its original decision. The
original text is retained as history; historical details are not current
implementation instructions. [ADR 1](0001-record-architecture-decisions.md)
establishes the process.

## Coverage and status

This index covers the consequential boundaries found in the 2026-10-16
repository audit. It is a discovery map, not a claim that every feature has
an ADR or that every linked test has passed. Newly reconstructed decisions
remain Proposed until explicitly accepted by the maintainer. Their alternatives
are present-day tradeoffs, not invented accounts of historical deliberations.

| ADR | Status | Governs |
| --- | --- | --- |
| [1: Record decisions](0001-record-architecture-decisions.md) | Accepted; proposed process amendments 2026-10-16 and 2026-10-20 | Requirements, lifecycle, scoped validation, designated CI evidence, agent discovery, GitHub/Beads boundary |
| [2: FAISS](0002-faiss-for-vector-search.md) | Accepted search choice; storage description historical | Exact similarity search; see Proposed ADR 6 for current storage |
| [3: Browser playback](0003-browser-driven-playback.md) | Accepted default; partially superseded by ADR 4 | Browser audio ownership and device-local controls |
| [4: Server mix bus](0004-server-mix-bus-and-stream-mode.md) | Accepted; amendments apply | Shared live sound-card/stream playout, handoffs selected at promotion, stream fan-out, mastering, live input, recordings; restarted stations start with the song they had picked 2026-10-25; playable continuations during handoff preparation 2026-10-27 |
| [5: Station ownership](0005-a-station-owns-its-playout-chain.md) | Accepted; current ownership amendment 2026-10-16 | Station lifecycle, settings, routes, state paths, shared library |
| [6: Catalog and migrations](0006-use-a-catalog-with-explicit-migrations.md) | Proposed; retrospective; shared audio-feature cache and three folder modes amendment 2026-10-27 | Playable records, derived vectors, model identity, legacy migration |
| [7: Access boundaries](0007-separate-browser-worker-and-listener-access.md) | Proposed; retrospective | Browser sessions, machine/announcer credentials, public surfaces; provider-neutral SSO amendment 2026-10-18 and proxy proof, private secret sources and bounded socket authority 2026-10-19, worker pairing codes 2026-10-23 |
| [8: Background work](0008-bound-background-work-outside-playout.md) | Proposed; retrospective | Jobs, remote analysis leases, rendering, shutdown and resource ownership; worker scan decoding, serial recovery, failed import completion, publication races, playable replacement, pending identity, quiet refresh, settling retries, tag-only validation, deferred edit snapshots, same-byte edit retention and colliding move holds amendments 2026-10-22; server-managed worker settings and refusal notices 2026-10-23; scan memory and garbage-collector pauses, and rescans that skip unchanged files 2026-10-24; first songs sooner after a restart and no full garbage-collector passes while a bus plays 2026-10-25; shared feature-cache worker protocol 2026-10-27 |
| [9: Picking and scheduling](0009-share-picking-rules-with-isolated-previews.md) | Proposed; retrospective | Selection, simulation, preview truth, scheduling, requests and inserted audio; announcer programming context, relative date conditions, repeating dates and station name while active amendments 2026-10-24; categories worked out by columns and for changed songs only, and removed songs without a Beets read 2026-10-25 |
| [10: State recovery](0010-preserve-state-through-atomic-writes-and-recovery.md) | Proposed; retrospective | Saved-state validation, secrets, backup/restore and recovery |
| [11: Accessible browser UI](0011-verify-accessibility-with-browser-and-speech-evidence.md) | Proposed; retrospective | Native controls, announcements, keyboard access, release evidence |
| [12: Setup and model compatibility](0012-separate-runtime-setup-from-model-execution.md) | Proposed; retrospective | Hardware setup, model loading, dependency boundaries, packaging and release; launcher restart loop 2026-10-23; shared Audio and MuQ Similarity availability 2026-10-27 |
| [13: Speech production](0013-produce-speech-outside-playout.md) | Accepted; verification limits apply | Provider comparisons, shared credentials, station speech assets, bounded synthesis and generated time checks |
| [14: Programme review](0014-isolate-programme-review-and-export.md) | Accepted | Frozen private audio reviews, shortened timelines, exports, storage and worker lifecycle |
| [15: Prepared editions](0015-prepare-speech-for-specific-show-editions.md) | Accepted | Occurrence-bound recordings, full-target review receipts, explicit approval, local playout and interrupted-edition recovery |
| [16: Updates and restart](0016-update-and-restart-from-the-web.md) | Proposed | Update channels, the update check and notice, restart from the web page, the launcher restart loop |
| [17: Explicit settings saves](0017-save-settings-through-one-draft-boundary.md) | Proposed | Shared drafts, revision conflicts, separate storage owners and removal of superseded writes |

[ADR 13](0013-produce-speech-outside-playout.md) describes selected
Showcraft-inspired speech capabilities inside AutoDJ. Its contract table records
automated checks and the remaining live-service, listening and workflow evidence.
Family-specific speech production and an automated Showcraft handoff remain
outside existing architecture. The current announcer/liner boundary is covered
by ADRs 7 and 9. No public-hosting, multi-user-role, or alternate embedding-model
capability is implied by these records.

## Maintaining records

Write an ADR for a consequential choice about ownership, storage, concurrency,
trust, compatibility, public interfaces, or operation with real tradeoffs.
Routine fixes and feature acceptance criteria belong in issues. A new dependency
needs an ADR when it establishes or changes such a boundary; a routine update
inside an existing decision does not automatically need a new record.

Use the next sequential number and a descriptive filename. Each record needs:

- Date, status, decision authority, and links to related decisions/issues/PRs
  when available. Label retrospective records and missing historical evidence.
- Problem and trigger; a specific decision; scope and exclusions.
- Lasting constraints, including applicable ownership, data flow, failure and
  recovery behavior, trust, and compatibility. Use measurable bounds where
  established; do not manufacture latency or throughput guarantees.
- Rationale, real alternatives, consequences, risks, and revisit conditions.
- Links to implementation entrypoints, tests of important invariants, and
  operational docs. Name affected paths and migration work for a new change;
  keep volatile command detail in the operational guide or CLI help.
- Testable verification criteria and explicit gaps. Reference concrete test
  names where helpful. Keep a distinction between a test existing, passing
  in a particular environment, and proving behavior on actual hardware.

`Proposed` becomes `Accepted` or `Rejected` only through an explicit maintainer
decision. Uncertain intent remains Proposed even when code and tests agree.
Preserve accepted history: append dated clarifications for intentional
evolution; use a new accepted record and links in both directions when a
decision is replaced. A Proposed replacement does not supersede an Accepted
record. Make the current status and historical scope clear at the top.

When evidence conflicts, investigate before changing either code or policy.
Document intentional evolution; link unintended deviations to a defect issue.
Never weaken a constraint merely to make tests pass. Incomplete evidence is
a recorded gap, not a verified contract.

Update this index and link affected contributor/operational docs when a record
changes. PRs should name the issue's acceptance criteria, applicable ADRs,
verification results, and remaining gaps. See [CONTRIBUTING](../../CONTRIBUTING.md)
and [AGENTS.md](../../AGENTS.md). GitHub is the shared requirements/status
authority; Beads is optional agent execution tracking.

## Constraint traceability and local checks

Each active or Proposed record has a `## Current contracts` table with exactly
`ID | Constraint | Method | Evidence` columns. IDs use `ADR-NNNN-NN`, belong to
the numbered record, and remain stable across wording and test renames. Never
recycle an ID. Preserve a removed constraint as `retired` with its rationale
and replacement, if any. Dated amendments explain intentional evolution;
original historical prose is retained.

Use an observable outcome for each constraint and one verification method:

- `automated`: backticked repository-relative test selectors, with a reciprocal
  ID in each Python test's docstring or an immediately adjacent JavaScript
  `// ADR-NNNN-NN` comment. Python selectors use `tests/.../test_file.py::test_name`
  or `::TestClass::test_method`; JavaScript selectors use
  `tests/jsmodules/...test.js::literal test title`. Titles must be unambiguous
  within the file. Keep test filenames within native pytest/Vitest discovery.
- `manual`: a repeatable procedure and required environment/evidence. Hardware,
  browser, screen-reader, performance, and platform obligations stay explicit.
- `gap`: the missing evidence or test and its limitation; link follow-up work
  when available. A gap is never reported as a passing check.
- `retired`: the reason the ID no longer imposes a current obligation.

Run `uv run --frozen --no-sync python scripts/check_architecture_docs.py` after
changing docs or mapped tests. This standard-library, read-only checker validates
local inline/reference-definition links and Markdown heading anchors, ADR numbers,
statuses and index entries, constraint IDs, native test selectors, and reciprocal
annotations. Fenced examples and inline code are excluded. External links are not
fetched; ordinary bracketed prose and undefined reference syntax are not links.
In Git checkouts, discovery uses tracked files (including staged additions), so
untracked drafts do not fail the gate. Outside Git, all files in the documented
paths are scanned. Local directory links are valid; Markdown heading anchors are
cached per resolved path for each check and refreshed on the next invocation.
The ADR's `Status:` line is canonical; the index mirrors it. Status suffixes may
explain historical scope. The checker never accepts an ADR or executes tests.

The same checker runs in the local commit hook and through a normal pytest test
in CI. Its positive/negative fixtures catch validator regressions. Run the mapped
behavioral tests separately and follow CONTRIBUTING's broader gate requirements.
Prefer assertions about behavior; enforce structure when structure itself is the
contract. Do not regenerate expected values from implementation to erase drift.
A passing structural check establishes traceability, not semantic agreement.

Every PR records one manual disposition with rationale and affected IDs:
`existing_adr_still_valid`, `update_existing_adr`, `create_new_adr`,
`create_superseding_adr`, or `no_adr_needed`. Link amendments, proposals, defects,
and deferred evidence as applicable. There is no heuristic drift detector or
automatic issue/ADR mutation; reviewers decide whether code, tests, and intent
agree. See the [process amendment](0001-record-architecture-decisions.md#amendment-2026-10-16-local-contract-enforcement)
for the proposed process.

## Verification evidence

Record checks actually run, their results, and environment/platform limits in the
relevant PR. Test references identify verification entrypoints; they do not
establish execution or replace CI and the documented screen-reader release sample.
