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

Trunk-based. Cut a topic branch off `main`, push, open a PR. No
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

## Where things live

| Path | What's there |
|---|---|
| `src/autodj/cli.py` | CLI entry points (Click) |
| `src/autodj/server.py` | FastAPI + WebSocket web layer |
| `src/autodj/static/` | Web UI: HTML, CSS, JS, AudioWorklets |
| `src/autodj/player.py` | Crossfade audio engine |
| `src/autodj/similarity.py` | FAISS query + ranking |
| `src/autodj/explain.py` | The "why this track?" reasoner |
| `src/autodj/jobs.py` | Background subprocess runner for the web UI |
| `src/autodj/transitions.py` | 26 transition effects |
| `tests/unit/` | Pure unit tests, no audio hardware |
| `tests/integration/` | Pipeline + server tests against mocks |
| `tests/smoke/` | CLI end-to-end smoke tests |

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
