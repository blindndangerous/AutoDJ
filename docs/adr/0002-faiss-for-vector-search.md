# 2. Use FAISS for vector similarity search

Date: 2026-05-05
Status: Accepted

## Context

AutoDJ needs fast nearest-neighbour search over 10k–100k embeddings on
commodity hardware, with no external services.

## Decision

Use FAISS with `IndexFlatIP` over L2-normalised vectors.  This gives
us exact cosine similarity in a process-local file (`vectors.index`)
that ships next to the metadata sidecar.

## Consequences

- One C++ dependency (`faiss-cpu`); pre-built wheels available for
  Linux / macOS / Windows.
- Exact search; no recall vs latency tuning needed at this scale.
- Switching to ANN later requires only changing the index type, not
  the vector storage layout.
- Index files are atomic-rename safe; partial writes can't corrupt
  the on-disk index (see `indexer.save_index`).

## Alternatives considered

- **pgvector** — adds a Postgres dependency we don't otherwise need.
- **Annoy** — slower at our scale; less actively maintained.
- **ScaNN** — Linux-only wheels at the time of writing.
- **Hand-rolled cosine over numpy** — works to ~5k tracks, then
  per-query latency becomes noticeable.
