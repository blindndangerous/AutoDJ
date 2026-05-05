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
