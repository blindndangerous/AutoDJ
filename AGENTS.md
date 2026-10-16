# Agent guidance

## Discover before changing

1. Read [CONTRIBUTING.md](CONTRIBUTING.md) for repository layout, checks,
   pull requests, and release rules. Read the linked GitHub issue and its
   acceptance criteria; if work starts in a conversation, make those agreed
   requirements understandable in the eventual issue or PR.
2. Read the [ADR index](docs/adr/README.md), then the records governing the
   affected areas. Check status and dated amendments: historical text and
   Proposed records are not newly accepted constraints.
3. Follow ADR links to runtime code, tests, and operational documentation.
   Trace affected callers and integrations before choosing a change. Consult
   [operations](docs/operations.md), [setup](docs/setup.md), the
   [threat model](THREAT_MODEL.md), and
   [accessibility guidance](docs/accessibility-testing.md) as applicable.
4. When code, tests, and an accepted ADR disagree, investigate the evidence.
   Do not silently rewrite the decision to match code or change code to match
   obsolete prose. Document intentional evolution with a dated amendment or
   superseding ADR. Report unintended deviations as bugs. Passing tests alone
   do not establish intended behavior.

## Requirements and decisions

GitHub issues and PRs are authoritative for shared requirements and status.
Keep acceptance criteria, scope changes, and contributor-visible blockers
there. ADRs hold lasting architectural choices and constraints; operational
docs hold commands and procedures. Link these sources instead of maintaining
duplicate specifications. Follow the [ADR lifecycle](docs/adr/README.md#maintaining-records).

Beads is optional agent execution tracking. Contributors must be able to
understand and resume work without installing it. When using Beads, link the
bead to the GitHub issue, keep intermediate execution notes there, and publish
relevant results and blockers in the issue or PR at handoff. Closing a bead
does not close the GitHub issue. A missing or broken Beads installation must
not prevent repository discovery or verification. Do not introduce a Beads
requirement, automatic bidirectional sync, or tracker files into a PR without
an explicit project decision. Do not publish issues or comments without
authorization from the operator.

## ADR discovery and traceability

Read each affected ADR's Current contracts table and follow its stable IDs to
specific tests. Update both mapping and test annotation when tests move. Keep
retired IDs and explain gaps; never infer acceptance from code or a green gate.
Follow the [constraint rules](docs/adr/README.md#constraint-traceability-and-local-checks).
Run `uv run --frozen --no-sync python scripts/check_architecture_docs.py`, then
the mapped behavioral tests and applicable contributor gates. Report structural
validation separately from execution and manual/platform evidence. Record the
manual ADR disposition and rationale in the PR; investigate conflicting intent
before changing policy or runtime behavior. No sibling checkout is required.

## Implementation and verification

- Preserve unrelated local changes. Use an isolated worktree for multi-step
  work; do not nest worktrees when already isolated.
- Reuse existing helpers and dependencies. Keep runtime repairs separate
  when the agreed scope is documentation and verification.
- A station owns its settings, state, and playout; the library and server have
  separate shared ownership. Use the paths and route conventions linked from
  [ADR 5](docs/adr/0005-a-station-owns-its-playout-chain.md).
- Run focused tests while iterating. Follow CONTRIBUTING's validation rules;
  full CI must pass on the final revision before merging. For documentation,
  check links, examples, and agreement with the cited code and tests. Add a
  test only when it catches a meaningful missing invariant.
- Existing test links establish where to verify a claim, not that the test
  has passed. Report commands actually run, outcomes, and limits, including
  untested hardware, browsers, platforms, and performance guarantees.
- Run one heavy test/build command at a time. Use non-interactive file
  operations (`cp -f`, `mv -f`, `rm -f`) and preserve installed hooks.
- Commit, push, remote tracker sync, and publication require explicit operator
  authority. At handoff, report changed files, verification, unresolved
  decisions or defects, and exact pending commands.
