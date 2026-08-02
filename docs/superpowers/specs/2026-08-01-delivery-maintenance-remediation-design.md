# Delivery and Maintenance Remediation Design

**Status:** Approved direction

**Goal:** Make local setup, containers, releases, dependency management, CI, diagnosis, and backups reproducible and consistent with documented behavior.

## Configuration behavior

Omitting `--config` will construct validated defaults: music `./music`, index `./index`, model `./models`, host `127.0.0.1`, port `8080`, and current dataclass defaults for optional features. Explicit `--config PATH` remains strict and errors when missing.

Environment overlay will support documented container variables, including host, port, library music directory, index directory, and access token. Precedence is defaults, TOML, sibling local TOML, environment, then explicit CLI flags. Parsers use same validators as TOML.

Example and local config comments will name SQLite stores correctly and stay synchronized by tests for documented keys.

## Container

Runtime and build stages will use Python 3.14-compatible images. Node stage will consume committed lock through `npm ci`. `.dockerignore` will exclude configs, tokens, libraries, indexes, models, virtual environments, Git data, Node modules, build output, and audit artifacts.

Runtime will use fixed unprivileged UID/GID, own only application/cache paths, and retain dropped capabilities plus `no-new-privileges`. Image and copied tool inputs will be pinned to reviewed versions or digests with update automation.

CI will build container, start it with temporary empty library/index, verify health and loopback behavior, then scan built image/SBOM rather than repository files alone.

## Version and release identity

`pyproject.toml` becomes authoritative version source. Python `__version__` and FastAPI metadata derive installed package version. Frontend package version is either synchronized automatically or removed as product-version source.

Release workflow verifies tag, pyproject version, changelog heading, built wheel metadata, and runtime-reported API version all match. Release depends on successful Python, frontend, security, and container checks for tagged commit. Exact wheel is smoke-installed before publication.

## Frontend reproducibility and dependency updates

`package-lock.json` will be tracked. CI installs Node, runs `npm ci`, ESLint with zero-warning policy for production source, Vitest, Vite build, dead-code scan, npm audit policy, and assertion-based Playwright tests.

Vitest upgrades to current supported 4.1.x line, Vite to patched compatible line, and other direct development dependencies to current compatible versions. Python lock updates will address confirmed advisories and current compatible releases. Scanner disagreements, such as Torch range metadata, will be documented or suppressed only with source-advisory evidence and expiry date.

## Python quality gates

Pyright findings will be fixed or narrowly documented; informational CI may remain advisory only after output is clean. Branch-coverage gate will pass through meaningful tests for confirmed branches, not threshold reduction or broad exclusions. SQLite resource warnings become errors in targeted tests.

Coverage exclusions will be narrowed so broad `except Exception` and core long-running paths do not disappear from measurement solely by pattern. Hardware-only paths may retain justified exclusions.

## Browser audit gates

Playwright audits will use assertions and nonzero exit status. At minimum Chromium runs per pull request; Firefox/WebKit run where platform support is stable or scheduled. Reports remain artifacts, not success criteria themselves.

## Doctor command

New `autodj doctor` command will report actionable checks for:

- configuration source and validated effective values with secrets redacted;
- music/index/model paths and required permissions;
- FAISS/SQLite/manifest count coherence;
- ffmpeg and optional audio dependencies;
- model-cache completeness;
- host binding and authentication safety;
- frontend bundle presence/version;
- supported Python version.

Command is read-only and returns nonzero when required checks fail.

## Backup and restore

Documentation and command support will distinguish re-derivable vectors/analysis from unique profiles, liners, dayparts, history, and runtime state. Backup uses stopped-service snapshot or SQLite backup API so WAL state is included consistently. Restore verifies schema/version and runs `autodj doctor` before serving.

## Documentation

README quickstarts, security support matrix, threat model, contributing coverage requirements, container instructions, and backup procedures will be updated from authoritative configuration. Historical human and AI coauthorship remains accurate; removal of obsolete tooling does not remove contribution credit.

## Testing

- Default/no-config, explicit-missing-config, environment precedence, and invalid-overlay tests.
- Docker build/start/health and unprivileged-write tests.
- Version-consistency and release-artifact smoke tests.
- Clean `npm ci`, frontend gates, current audit, and patched dependency assertions.
- `autodj doctor` healthy, corrupt index, missing dependency, unsafe bind, and redaction tests.
- Backup during stopped service, SQLite online backup, restore, and incompatible-version refusal tests.

## Out of scope

Publishing to PyPI, multi-architecture GPU images, automatic cloud backups, and long-term support for multiple pre-1.0 release lines are not added by this remediation.
