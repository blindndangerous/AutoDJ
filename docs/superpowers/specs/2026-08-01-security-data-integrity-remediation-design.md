# Security and Data Integrity Remediation Design

**Status:** Approved direction

**Goal:** Prevent index corruption, unsafe live reloads, filesystem escape, unbounded uploads, insecure LAN exposure, and incomplete model caches.

## Scope

This specification covers storage transactions, index publication, model-download durability, liner-file handling, request authentication, and network-boundary defaults. Container construction and release automation are covered by the delivery specification.

## Storage transactions

`tracks.db` and `dj_meta.db` will use explicit transactions. Connections may retain autocommit for ordinary reads, but each multi-statement mutation must issue `BEGIN IMMEDIATE`, then `COMMIT` or `ROLLBACK` on failure. Tests will inject failures after destructive statements and prove that preexisting rows remain intact.

Incremental indexing will stop replacing every `tracks` row after every embedded track. New or changed rows will be upserted by a stable vector-row identity. Full-table replacement remains limited to operations that intentionally reorder or prune vectors. Metadata and vector ordering remain one-to-one.

Connections must be closed deterministically. Tests that create `DjMetaCache` instances will use explicit close methods or context management, eliminating current `ResourceWarning` output.

## Coherent index publication

Index writes will publish a small generation manifest only after both `tracks.db` and `vectors.index` are durable. Manifest fields will include schema version, monotonically increasing generation, vector count, and publication timestamp. Manifest replacement will use the existing temporary-file, fsync, and `os.replace` pattern.

Server watcher will observe manifest generation instead of `tracks.db` modification time. Reload sequence:

1. Read manifest.
2. Load metadata and FAISS into temporary objects.
3. Validate manifest count, SQLite row count, and FAISS `ntotal`.
4. Construct a replacement `SimilarityIndex`, invoking its invariant checks.
5. Swap state while holding one reload/read lock.
6. Advance observed generation only after successful swap.

Mismatch or transient read failure retains current in-memory index and retries same generation. Legacy indexes without a manifest remain loadable at startup; next successful index write creates manifest.

## Model cache durability

Model downloads will use Hugging Face Hub's supported timeout behavior without abandoned retry threads. Download target will be a unique staging directory. Successful completion requires expected model configuration and weight files plus a completion marker. Only then will staging be atomically promoted to cache directory.

Incomplete directories never count as cached. A process-level lock prevents concurrent AutoDJ downloads of same model. Timeout or failure removes owned staging directory without touching valid cache.

## Liner filesystem boundary

Upload, download, and delete operations will share one containment helper. It will reject both `/` and `\` separators, `.`/`..` components, empty names, device names, and names whose resolved target cannot be expressed with `relative_to(liner_root)`.

Uploads will stream to a temporary file with configurable maximum size, defaulting to 50 MiB. Crossing limit returns HTTP 413 and removes temporary file. Existing filenames return HTTP 409 unless caller explicitly requests replacement. Successful writes use atomic replacement.

## Network authentication

Loopback-only operation may remain unauthenticated. Binding any non-loopback address requires configured access token or explicit `--insecure-lan` acknowledgement.

Authenticated mode provides:

- constant-time token comparison;
- login endpoint that exchanges token for `HttpOnly`, `SameSite=Strict`, secure-when-HTTPS session cookie;
- authentication on state-changing, media, job, profile, liner, and WebSocket routes;
- HTTP `Origin` and `Host` validation;
- WebSocket cookie and origin validation before acceptance;
- request IDs and structured audit events for state-changing operations without logging tokens or full private paths.

Compose binds `127.0.0.1:8080` by default. LAN exposure becomes explicit profile/override and requires token unless insecure acknowledgement is supplied.

## Error handling

- Transaction failure rolls back and reports operation failure without publishing new generation.
- Reload mismatch logs counts and retries; playback continues using last valid snapshot.
- Invalid filenames return 400; missing files return 404; conflicts return 409; oversized uploads return 413.
- Missing or invalid authentication returns 401; disallowed origin/host returns 403.
- Model timeout reports retry-safe failure and leaves no accepted partial cache.

## Testing

- SQLite failure injection after `DELETE` and midway through batched writes.
- Incremental checkpoint write-count and ordering assertions.
- Metadata-ahead, vectors-ahead, corrupt-manifest, and concurrent-reader reload tests.
- Timed-out model download, partial directory, concurrent invocation, and successful atomic promotion tests.
- Windows and POSIX traversal cases, sibling-prefix paths, upload limit, conflict, and cleanup tests.
- Loopback anonymous access, LAN startup refusal, token login, HTTP origin, WebSocket origin, expired/invalid cookie, and structured audit-event tests.

## Out of scope

Multi-user accounts, roles, cloud identity providers, and public-internet hosting remain unsupported.
