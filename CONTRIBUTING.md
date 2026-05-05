# Contributing to AutoDJ

Thanks for your interest in improving AutoDJ.  This file gives you the
shortest path from "I have an idea" to "my change is merged".

## Quick start

```bash
# 1. Fork + clone
git clone https://github.com/<your-fork>/autodj
cd autodj

# 2. Install with dev tools
uv sync --extra all --extra dev

# 3. Run the test suite
uv run pytest

# 4. Run lint + type checks
uv run ruff check src/ tests/
uv run mypy src/autodj/
uv run bandit -q -r src/
```

The full test suite must stay green.  Coverage is gated at 89%
(`fail_under = 89` in `pyproject.toml`); add tests for any new code path
you touch.

## Branching + commit style

- Cut a topic branch off `main`: `git checkout -b feat/your-thing`.
- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `style:`.
- Keep commits small.  PR titles follow the same convention — they
  become the squash commit on `main`.

```
feat: add dub-siren transition effect
fix: handle empty FAISS results without crashing
docs: clarify MuQ fp32 requirement
```

## Pull request checklist

The PR template auto-renders this list — tick each box:

- [ ] Tests added or updated for the new behaviour
- [ ] CHANGELOG.md updated under `[Unreleased]`
- [ ] `ruff check`, `mypy`, and `bandit` are clean
- [ ] The web UI changes (if any) passed an a11y review (see
  [accessibility-lead skill](.github/copilot-instructions.md) if your
  team uses one)

## Where things live

| Path | What's in it |
|---|---|
| `src/autodj/cli.py` | CLI entry points (Click) |
| `src/autodj/server.py` | FastAPI + WebSocket web layer |
| `src/autodj/static/` | Web UI (HTML / CSS / JS / AudioWorklets) |
| `src/autodj/player.py` | Crossfade audio engine |
| `src/autodj/similarity.py` | FAISS query + ranking |
| `src/autodj/explain.py` | "Why this track?" reasoner |
| `src/autodj/jobs.py` | Background subprocess runner for the web UI |
| `src/autodj/transitions.py` | 25 transition effects |
| `tests/unit/` | Pure unit tests — no audio hardware |
| `tests/integration/` | Pipeline + server tests with mocks |
| `tests/smoke/` | CLI end-to-end smoke tests |

## Reporting bugs / requesting features

Please use the GitHub issue templates — they ask for the version, the
surface (CLI vs web UI), and reproduction steps.

## Security

Found a vulnerability?  See [SECURITY.md](SECURITY.md) — please report
privately, not via a public issue.

## Code of conduct

By participating you agree to abide by the
[Contributor Covenant](CODE_OF_CONDUCT.md).
