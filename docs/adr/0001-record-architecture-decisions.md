# 1. Record architecture decisions

Date: 2026-05-05
Status: Accepted

## Context

Decisions that meaningfully shape the codebase deserve a paper trail.
Otherwise the rationale evaporates and future maintainers (humans + AI)
end up reverse-engineering intent from diffs.

## Decision

We adopt [MADR](https://adr.github.io/madr/) for Architecture Decision
Records.  Every architectural choice — choice of stack, storage layout,
threading model, public API conventions — that survives a code review
gets an ADR file in `docs/adr/`.  Numbered sequentially.

## Consequences

- One source of truth for "why is it this way?".
- Newcomers (and LLMs) can answer most "why" questions from `docs/adr/`
  without grepping the codebase.
- Cost: each non-trivial decision needs a 5-minute write-up.

## Alternatives considered

- Decisions live only in commit messages — too fragmented; nobody reads
  the entire `git log`.
- Decisions live in a wiki — drifts from the code unless aggressively
  curated.

## Status transitions

`Proposed` → `Accepted` / `Rejected`.  When a decision is superseded,
update its status to `Superseded by <number>`; never edit the original
content.

## Amendment, 2026-10-16: discovery, requirements, and evidence

Decision authority: Operator approval during the ADR/documentation scope review.
This amendment extends the accepted process; the original text above is retained.

GitHub issues and PRs own shared requirements and contributor-visible status.
Beads is optional agent execution tracking, linked to the shared issue when used;
closing a bead does not close the GitHub issue. Contributors must not need Beads
to discover requirements, decisions, blockers, or verification results.

ADRs capture consequential choices, rationale, scope/exclusions, lasting
constraints, alternatives, consequences, and revisit conditions. Link to the code,
tests, and operational procedures that implement and verify them. Do not duplicate
feature acceptance criteria or volatile CLI instructions. The
[ADR index and lifecycle](README.md) define the content and review standard.

Keep uncertain retrospective records Proposed until explicitly accepted by the
maintainer. Do not invent historical reasoning. Preserve accepted history with
dated amendments; replace a decision through a new accepted record and reciprocal
supersession links. Proposed replacements do not supersede accepted decisions.

When docs, code, and tests disagree, investigate intentional evolution versus a
defect. Passing tests do not establish intended behavior. Record verification gaps
and track runtime defects separately from documentation reconciliation.

[AGENTS.md](../../AGENTS.md) is canonical agent discovery guidance;
[CLAUDE.md](../../CLAUDE.md) and existing tool-specific entrypoints point there.
[CONTRIBUTING.md](../../CONTRIBUTING.md) and issue/PR templates connect acceptance
criteria, applicable ADRs, and verification. No runtime or dependency change is
required by this process amendment. Revisit if shared project tracking changes.

Alternatives: requiring Beads for all contributors would hide shared context from
non-users; duplicating policy in each agent file would create competing guidance.
The adopted boundary keeps shared decisions in Git/GitHub with thin tool pointers.

Verification: check entrypoint links, index coverage, template acceptance/ADR
fields, and the absence of a mandatory Beads workflow in contributor instructions.
Actual audit checks are recorded in [verification evidence](verification-2026-10-16.md).

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0001-01` | The documentation gate is read-only, fails invalid contracts, and does not execute tests or accept decisions. | automated | `tests/unit/test_architecture_docs.py::test_valid_contracts_are_read_only_and_do_not_claim_execution`; `tests/unit/test_architecture_docs.py::test_invalid_contracts_fail_with_actionable_diagnostics`; `tests/unit/test_architecture_docs.py::test_class_methods_resolve_without_importing_test_modules`; `tests/unit/test_architecture_docs.py::test_proposed_contracts_remain_proposed_and_retired_ids_need_no_tests`; `tests/unit/test_architecture_docs.py::test_repository_architecture_contracts_are_consistent` |
| `ADR-0001-02` | GitHub owns shared requirements/status; Beads is optional execution tracking. | manual | Review issue/PR context and contributor/agent guidance; work must be resumable without Beads. |
| `ADR-0001-03` | An architectural change records a reasoned ADR disposition without automatic issue or ADR mutation. | manual | PR review records existing_adr_still_valid, update_existing_adr, create_new_adr, create_superseding_adr, or no_adr_needed with rationale and follow-up. |

## Amendment, 2026-10-16: local contract enforcement

Decision authority: Operator approval after review of ADR/test traceability and
selected Polyrepo contracts. The local rules in the [index](README.md#constraint-traceability-and-local-checks)
are complete and apply without a coordination checkout. This amendment does not
accept the retrospective Proposed ADRs or add aspirational runtime guarantees.

Adopt stable constraint IDs, reciprocal native test mappings, deterministic
read-only documentation checks, and a reasoned manual ADR disposition in PRs.
Reuse behavioral tests; add coverage only for demonstrated gaps. Keep manual
verification and missing evidence visible. Report the actual candidate, commands,
results, environment, and coverage limits; focused, staged, and full-suite checks
are distinct. Never infer acceptance or platform coverage from a valid table.

Adapted sources: `mine/polyrepo_coordination`, reviewed revision `a332e7b`,
`docs/adr/` records 0003 (reviewed canon/fixture promotion), 0028 (manual ADR
drift disposition), 0053 (source-classified documentation checks), and 0067
(truthful verification and locally complete guidance; supersedes 0061).
These references provide attribution, not runtime imports or a requirement to
install Polyrepo, enroll AutoDJ, copy its schemas, or adopt its Beads workflow.

Alternatives: a coordination framework or heuristic semantic drift detector adds
maintenance without establishing intent. Text-only ADRs leave dangling evidence
undetected. Local deterministic checks plus human review provide a smaller,
reviewable boundary. Revisit if actual drift demonstrates the need for more.
Verification uses the validator fixtures, real-repository smoke test, referenced
behavioral tests, and explicit manual/gap reports. Failed behavior is a defect to
investigate; do not weaken the ADR to make a gate green.
