# Contributing to AutoDJ

Shortest path from "I have an idea" to "my change is merged".

## Quick start

```bash
# 1. Fork + clone
git clone https://github.com/<your-fork>/autodj
cd autodj

# 2. Install with dev tools
uv sync --frozen --all-extras
npm ci

# 3. Wire pre-commit
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg

# 4. Run reproducible Python and frontend gates
uv run python scripts/ci_pytest.py
npm run lint
npm test
npm run build
```

Full suite must remain green. `scripts/ci_pytest.py` enforces at least 99.1% line coverage and 94.7%
branch coverage. `pyproject.toml` also rejects an incomplete test run below its combined coverage
floor. New behavior needs focused tests.

## Branching + commits

Trunk-based. Cut a topic branch off `master`, push, open a PR. No
long-lived `develop` or `release` branches.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).
Lowercase, imperative, ≤ 72 characters:

```
feat: add dub-siren transition effect
fix: handle empty FAISS results without crashing
docs: clarify MuQ fp32 requirement
```

`commitlint` runs on `commit-msg` so you'll find out before you push if
the format is wrong.

## Pull request checklist

The PR template auto-renders this list. Tick the boxes:

- Tests added or updated for the new behaviour.
- `CHANGELOG.md` updated under `[Unreleased]`.
- `ruff`, `mypy`, `bandit`, `vulture`, `deptry`, ESLint, and all tests pass locally. You can run
  their overlapping hooks with `uv run pre-commit run --all-files`.
- `uv run pyright src/autodj/` remains required; pre-commit does not run Pyright.
- Run `uv run autodj doctor` against intended local configuration.
- For web UI changes, start AutoDJ and run Playwright Chromium audit with
  `AUTODJ_BROWSERS=chromium npm run audit:ci`.
- For container changes on Linux or WSL2, run `bash scripts/container_smoke.sh`.

See [Operations](docs/operations.md) for setup, diagnosis, backup, restore, and upgrade procedures.

## Releasing

The maintainer cuts releases from `master`. For version `X.Y.Z`:

1. Set `version = "X.Y.Z"` in `pyproject.toml` and run `uv lock` so the lock records the new
   project version.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh empty
   `## [Unreleased]` above it, and update the link references at the bottom of the file.
3. Record the manual screen-reader sample that
   [Accessibility testing](docs/accessibility-testing.md) requires, and link it from the release
   notes.
4. Commit, then build and check the artifacts the way the release workflow does:

   ```bash
   npm ci && npm run build
   uv build --sdist --wheel
   uv run --frozen python scripts/verify_release.py --tag vX.Y.Z      --wheel dist/autodj-X.Y.Z-py3-none-any.whl --sdist dist/autodj-X.Y.Z.tar.gz
   ```

5. Tag and push the tag: `git tag vX.Y.Z`, then `git push origin vX.Y.Z`. The Release workflow
   reruns CI and the security scans, verifies the tag against `pyproject.toml`, the changelog,
   and the wheel, then publishes the signed artifacts.

The README install command uses `X.Y.Z` placeholders, so it needs no edit per release. Only
v0.12.0, v0.16.0, and v0.16.1 were tagged. The other versions from 0.1.0 through 0.15.0 exist only
as changelog entries.

## Where things live

- `src/autodj/cli.py` — CLI entry points (Click).
- `src/autodj/server.py` — FastAPI + WebSocket web layer.
- `src/autodj/static/` — web UI: HTML, CSS, JS, AudioWorklets.
- `src/autodj/player.py` — crossfade audio engine.
- `src/autodj/similarity.py` — FAISS query + ranking.
- `src/autodj/explain.py` — the "why this track?" reasoner.
- `src/autodj/jobs.py` — background subprocess runner for the web UI.
- `src/autodj/transitions.py` — the transition effects; `TransitionFx` is the single source of
  truth for the names the CLI, the server and the web UI accept.
- `tests/unit/` — pure unit tests, no audio hardware.
- `tests/integration/` — pipeline + server tests against mocks.
- `tests/smoke/` — CLI end-to-end smoke tests.
- `tests/fuzz/` — Hypothesis property tests; nightly Fuzz workflow only.
- `tests/jsmodules/` — Vitest unit tests for the web UI ES modules.
- `tests/playwright/` — cross-browser audits against a running server.

## Reporting bugs / requesting features

Use the GitHub issue templates. They ask for version, surface (CLI vs
web UI), and reproduction steps so we don't have to ping you for the
basics.

## Security

Found a vulnerability? Report it privately via the Security tab,
not a public issue. Details in [SECURITY.md](SECURITY.md).

## Code of conduct

[Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Participate and you've
agreed.
