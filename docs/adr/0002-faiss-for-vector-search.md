# 2. Use FAISS for vector similarity search

Date: 2026-05-05
Status: Accepted

> Current scope and implementation evidence are in the dated amendment
> at the end of this record. Original sections below are retained as history.

## Context

AutoDJ needs fast nearest-neighbour search over 10k–100k embeddings on
commodity hardware, with no external services.

## Decision

Use FAISS with `IndexFlatIP` over L2-normalised vectors.  This gives
us exact cosine similarity in a process-local file (`vectors.index`)
that ships next to the metadata sidecar. (Superseded in part: each
index generation is now a `vectors.g<number>.index` and
`tracks.g<number>.db` pair named by `index-manifest.json`, published
atomically by `index_manifest.publish_generation`.)

## Consequences

- One C++ dependency (`faiss-cpu`); pre-built wheels available for
  Linux / macOS / Windows.
- Exact search; no recall vs latency tuning needed at this scale.
- Switching to ANN later keeps the vector storage layout. (Superseded:
  the search code now relies on a flat index, reading vectors back with
  `reconstruct()` and asking some searches for every vector, so ANN would
  need those paths rewritten too.)
- Index files are atomic-rename safe; partial writes can't corrupt
  the on-disk index (see `indexer.save_index`).

## Alternatives considered

- **pgvector** — adds a Postgres dependency we don't otherwise need.
- **Annoy** — slower at our scale; less actively maintained.
- **ScaNN** — Linux-only wheels at the time of writing.
- **Hand-rolled cosine over numpy** — works to ~5k tracks, then
  per-query latency becomes noticeable.

## Amendment, 2026-10-16: separate search choice from historical storage

The exact, process-local FAISS search choice remains accepted. The original
sidecar/generation publication descriptions above are historical: serving now
loads playable records and stored vectors from `library.db`; the generation
helpers remain for explicit legacy migration. Do not implement new serving or
backup behavior from the original filenames or `save_index` reference.

Evidence: [catalog](../../src/autodj/catalog.py),
[library startup](../../src/autodj/library.py),
[legacy format declaration](../../src/autodj/index_manifest.py),
[similarity search](../../src/autodj/similarity.py), and
[migration](../../src/autodj/index_migration.py).
[Catalog startup tests](../../tests/unit/test_catalog_startup.py) check that a
current library survives without a manifest, ignores stale search input, and
refuses legacy-only startup without mutation. [Similarity tests](../../tests/unit/test_similarity.py)
cover search/ranking behavior; [migration tests](../../tests/unit/test_index_migration.py)
cover legacy import.

[Proposed ADR 6](0006-use-a-catalog-with-explicit-migrations.md) reconstructs
the current persistence contract; it does not supersede this accepted search
decision. The original rationale for FAISS versus external services remains
the recorded rationale, not a new performance comparison. Scope excludes a
new model, ANN migration, and hardware-independent latency guarantees. Revisit
search choice if measured library scale or representation changes justify it.

Verification criteria: catalog-only startup remains playable; search uses the
current catalog vectors; legacy import requires its supported explicit command.
Run the linked tests for the changed surface and consult
[selection](../track-selection.md), [migration operations](../operations.md#moved-files),
and [audit evidence](verification-2026-10-16.md) for results and limits.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0002-01` | With other ranking inputs equal, select the eligible neighbour with the highest cosine score. | automated | `tests/unit/test_similarity.py::TestFindNext::test_returns_closest_neighbor` |
| `ADR-0002-02` | Representation or search changes require representative scale/quality evidence; no universal latency guarantee is established. | manual | Record library size, hardware, representation, options, measured performance, and listening evaluation when revisiting search. |
