# Contributing to AutoDJ

Shortest path from "I have an idea" to "my change is merged".

## Quick start

```bash
# 1. Fork + clone
git clone https://github.com/<your-fork>/autodj
cd autodj

# 2. Install base dependencies and dev tools, without model packages
uv sync --frozen
# Node.js is needed for frontend tests, lint and audits
npm ci --ignore-scripts

# 3. Install the pre-commit, commit-msg and pre-push hooks
uv run --frozen --no-sync pre-commit install

# 4. Run the fixed local smoke suite, then tests relevant to your change
uv run --frozen --no-sync python scripts/ci_pytest.py --profile smoke
uv run --frozen --no-sync python -m pytest --no-cov tests/unit/test_architecture_docs.py
```

For a constrained contributor environment with only base dependencies and test
tools, use `uv sync --frozen --no-default-groups --group test`. This is a supported
no-model setup; hook and quality tools use the default dev setup above. Validate
the no-model environment with
`uv run --frozen --no-sync python scripts/ci_pytest.py --profile no-model`.
This profile requires `torch`, `muq` and `transformers` to be absent, then runs
the smoke suite and selected no-model, index and worker tests. Smoke and no-model
profiles run serially without the full-suite coverage floor. Smoke tests must
execute without skips. These profiles do not prove model execution works.

For full local validation, install `uv sync --frozen --all-extras` and run
`uv run --frozen --no-sync python scripts/ci_pytest.py --profile full`.
The full profile requires model packages and at least 2 GiB plus 64 MiB of free
scratch space. The runner uses a private temporary directory and checks loopback
permissions before running tests; resolve a failed preflight before retrying.
The default profile is full. It enforces 90% line coverage, with no branch floor.
There is no whole-run duration guarantee. Use `--no-sync` for verification so
`uv run` does not silently change the selected environment.

## Choose checks for the change

This table governs local validation scope. Select checks by what the change can
execute or affect; a file extension alone does not establish scope. Add tests
only for meaningful missing invariants, not to raise coverage numbers.

| Change | Local evidence |
| --- | --- |
| Prose, links, documentation moves | Documentation structural checks and review against cited sources; check changed examples and procedures using the [documentation verification guide](docs/contributor/documentation-verification.md). |
| Comments or test annotations that do not change execution | Structural checks, including ADR mappings when affected; explicitly reuse unaffected behavioral evidence as described below. |
| Runtime behavior or executable test changes | Run affected tests, callers and integrations; use `uv run --frozen --no-sync python -m pytest --no-cov <selectors>` for focused iteration. |
| Dependencies, test runner, fixtures or CI configuration | Validate the affected environment and runner/configuration behavior; exercise changed jobs in CI. |
| Final merge candidate | Required CI must pass on the final head; local smoke, focused tests and reused evidence do not replace it. |

Draft pushes run fast static/documentation checks and no-model smoke in CI.
Marking a PR ready for review triggers full CI, as do subsequent pushes while it
is ready. Required checks must actually execute on their designated CI runner;
a passing job that skipped its required behavior is insufficient. Optional
platform-specific skips remain allowed. Full Python CI explicitly requires the
programme-review render tests to execute.

Record the scope, reason, commands, revision, results and limitations in the PR.
To reuse successful evidence, name its prior run/report and revision, identify
unchanged relevant code, configuration, dependencies, options and environment,
and explain why it covers the candidate. A documentation or annotation change
can reuse unaffected behavioral evidence; a link to a test alone is not evidence
that it ran. Repeat checks when relevant execution inputs change or coverage is
uncertain. There is no automatic evidence cache.

Do not automatically retry a failed behavioral test. Diagnose the failure,
fix or escalate an environment problem, then retry once. For a suspected baseline
failure, reproduce it once on the unchanged base in the same environment, record
both results and link follow-up work. Stop chasing a green run; a caused
regression must be fixed, and unresolved required CI failures block merging.

## Checks before a push

Keep installed hooks enabled. Existing hooks read the updated configuration;
reinstalling them is unnecessary. Commits run quick checks. Python-related
pushes run the fixed smoke suite; choose relevant tests from the table above
before pushing. Expensive quality and audit hooks are available explicitly:

```bash
uv run --frozen --no-sync pre-commit run --all-files --hook-stage manual
```

These checks remain in full CI. The secret-scanning baseline stays enabled.
To run applicable commit and pre-push hooks manually, use:

```bash
uv run --frozen --no-sync pre-commit run --all-files
uv run --frozen --no-sync pre-commit run --all-files --hook-stage pre-push
```

When a push changes web UI files (`src/autodj/static/`), frontend tests
(`tests/jsmodules/`), or npm, Vitest or ESLint configuration, the hook also runs:

- `knip`, the unused-code scan, over the whole project.
- Vitest tests related to the pushed files: tests that import a changed file,
  read it or mention it by name. A configuration-only change may have no related tests.

Python-only and documentation-only pushes skip those frontend checks. The hook
needs `node_modules`; if missing, run `npm ci --ignore-scripts`. CI uses Node.js
26, as `engines` in package.json says, and full CI runs the complete Vitest suite.
Run `npm test` locally after a large frontend change. Fix failed hooks rather
than bypassing them; use the failure protocol above for behavioral tests.

## Writing documentation

Use [Diátaxis](https://diataxis.fr/start-here/) to choose a page's primary purpose:

- **Tutorial:** teach through a guided experience. Put lessons in `docs/tutorials/`
  and start from the [author template](docs/tutorials/author-template.md).
- **How-to guide:** help someone complete a task. Keep the existing
  `docs/guides/` pages direct, with prerequisites, steps, and a result to check.
- **Reference:** describe facts readers need to look up, including possible
  values, defaults, and plain explanations. Preserve consequential edge cases.
- **Explanation:** build understanding of how and why things work. Link it from
  procedures rather than interrupting steps with long background discussions.

Small explanations can support a procedure. Link to another page when the reader's
purpose changes. Use reader goals in navigation; folders need not mirror all four
categories. Keep architectural decisions and their history in ADRs.

For tutorials, state the starting state and learning outcome. Keep one named
example throughout, introduce terms when they become useful, and give fully
worked steps before independent practice. Pair checkpoints with observable
results and recovery steps. End with a small change or prediction exercise and
an answer the reader can check. Explain how to stop and what saved state remains.

Write for keyboard and screen-reader use: use descriptive headings in order,
exact control names, short steps, and text results that do not depend on color,
position, or hearing audio alone. Prefer prose and lists where they suffice.
Keep required steps and recovery advice in the reading flow. Link to the
[keyboard guide](docs/guides/keyboard-and-screen-reader.md) for navigation detail.

Check instructions against the UI and implementation. Run the documentation
checker with `uv run --frozen --no-sync python scripts/check_architecture_docs.py`.
Its normal mode excludes untracked drafts. Before staging new pages, also run
`uv run --frozen --no-sync python scripts/check_architecture_docs.py --include-untracked`
to check the candidate documentation, including new files. This is a structural
link/contract check; it does not run app procedures or fetch external links.
A tutorial needs a manual keyboard and screen-reader walkthrough before it is described as fully verified. Record the revision,
operating system, browser, screen reader, date, results, and unresolved findings
in the PR or linked issue. Check the rendered document's heading navigation and
links as well as the app steps. Structural checks, browser automation, and manual
review are separate evidence; identify anything not performed.

Use the [documentation verification guide](docs/contributor/documentation-verification.md)
for executable checks, native platform rehearsals, all thirteen tutorial walkthroughs,
rendered-document review, content-preservation review, independent reader journeys
and the evidence record. Keep pending platform and reader evidence explicit;
source checks do not establish runtime or screen-reader results.

The [tutorial roadmap](docs/tutorials/README.md) distinguishes available lessons
from planned ones. Add a lesson link only when its file exists. Planned lessons
need an outcome and prerequisites, not empty lesson files.

Before a release, review the affected documentation against the release candidate:

- Follow changed install, update and recovery commands, checking native PowerShell
  and Bash differences where both are documented. Record commands actually run.
- Check UI labels, CLI flags, defaults, dependencies and output-mode restrictions
  against the candidate; update the [feature reference](docs/reference/feature-availability.md).
- Check navigation and local links, including newly added pages; remove links to
  unavailable procedures. Reconcile supported-version wording in [SECURITY.md](SECURITY.md)
  when the release becomes the latest tag.
- Record manual keyboard/screen-reader results and untested platforms separately
  from structural checks. Carry unresolved user-facing limits into the release notes.

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

## Writing issues and pull requests

Agree on observable acceptance criteria, scope exclusions, and constraints in
the GitHub issue before implementing substantial work. If work starts in a
conversation, preserve the agreed requirements in the issue or PR so another
contributor can resume it without the conversation or an agent tracker.

Consult the [ADR index](docs/adr/README.md) before architectural changes.
Read the applicable status and dated amendments, then follow links to code,
tests, and operational docs. Significant new choices need a Proposed ADR;
routine changes within an accepted boundary do not. Existing code or passing
tests alone cannot establish that a new policy is accepted. Explain mismatches
and document intentional evolution or link a defect rather than silently
rewriting history. Follow the index's content and lifecycle rules.

Run `uv run --frozen --no-sync python scripts/check_architecture_docs.py` for
ADR/doc changes and mapped-test renames. The read-only check also runs in the
commit hook and normal pytest/CI. It verifies local links, status/index agreement,
stable constraint IDs, and reciprocal test selectors; it does not run behavioral
tests or approve decisions. Follow the [mapping rules](docs/adr/README.md#constraint-traceability-and-local-checks),
keep manual/gap evidence explicit, and select execution or evidence reuse using
the [change-to-check table](#choose-checks-for-the-change).
Record an ADR disposition and rationale in every PR, even when none is needed.

Link applicable ADRs and acceptance criteria in the PR, identify evidence for
each affected invariant, and report the checks actually run and their limits.
Update affected docs and ADR links in the same change. Newly reconstructed
decisions with uncertain intent remain Proposed until maintainer acceptance.

GitHub issues and PRs are authoritative for shared requirements and status.
Beads is optional agent execution tracking; nobody needs it to contribute.
Agents using it link their bead to the issue and carry relevant outcomes and
blockers back to the issue or PR. Closing a bead does not close the GitHub
issue. There is no automatic bidirectional synchronization requirement.
[AGENTS.md](AGENTS.md) provides the agent discovery path.

Write titles and descriptions in clear, natural language for someone who has not read the
conversation. When using AI tools, use Humanize or a similar writing skill when available,
or review the wording for clarity yourself. Lead with the problem and the intended or resulting
behavior. Keep technical detail, validation results, and limitations that help readers assess
the change; omit filler
and repeated explanations. Preserve precise facts and the repository templates.

## Pull request checklist

Use the PR template and check the following as applicable:

- Tests added or updated for the new behaviour.
- Keep help on the page to one short line; put the detail in the docs.
- Use short native form labels; keep longer help outside the label, readable on demand without `aria-describedby`. Keep concise format hints, active restrictions, station context and validation errors associated with their controls, including `aria-invalid` and error focus. In Settings, use `data-help` when visibility logic needs to hide or show that help.
- Use the same numeric times and concise counts for visual and screen-reader output; follow the [UI wording guidance](docs/accessibility-testing.md#ui-wording) rather than assuming numbers need spoken-word replacements.
- Leave `CHANGELOG.md` alone; the release notes are written when releasing.
- Follow the [change-to-check table](#choose-checks-for-the-change), including
  affected callers and integrations, and record results and limitations. Required
  CI must pass on the final head before merging.
- Keep installed hooks enabled; use the commands in
  [Checks before a push](#checks-before-a-push) for explicit broader validation.
- For setup, configuration, dependency, or runtime changes, run `uv run --frozen --no-sync autodj doctor` against
  the intended local configuration.
- For web UI changes, start AutoDJ and run Playwright Chromium audit with
  `AUTODJ_BROWSERS=chromium npm run audit:ci`. CI does not run WebKit, so also run
  `AUTODJ_BROWSERS=webkit npm run audit:visual` after `npx playwright install webkit`
  (see [Accessibility testing](docs/accessibility-testing.md)).
- For container changes on Linux or WSL2, run `bash scripts/container_smoke.sh`.

See [Operations](docs/operations.md) for setup, diagnosis, backup, restore, and upgrade procedures.

## Releasing

The maintainer cuts releases from `master`. For version `X.Y.Z`:

1. Set `version = "X.Y.Z"` in `pyproject.toml` and run `uv lock` so the lock records the new
   project version.
2. In `CHANGELOG.md`, add `## [X.Y.Z] - YYYY-MM-DD` with the user-facing changes from the PRs
   merged since the last tag: a few lines for a big new feature, one short line for anything
   else. Leave out fixes to changes that were never released.
   Update the link references at the bottom of the
   file.
3. Complete the documentation review in [Writing documentation](#writing-documentation).
   Record the manual screen-reader sample that
   [Accessibility testing](docs/accessibility-testing.md) requires, and link it from the release
   notes.
4. Commit, then check that the artifacts build: `uv build --sdist --wheel`.
5. Tag and push the tag: `git tag vX.Y.Z`, then `git push origin vX.Y.Z`. The Release workflow
   reruns CI and the security scans, checks that the tag equals `uv version --short` and that
   `CHANGELOG.md` has a `## [X.Y.Z]` heading, smoke-installs the wheel, then publishes the signed
   artifacts.

The README install command uses `X.Y.Z` placeholders, so it needs no edit per release. Only
v0.12.0, v0.16.0, v0.16.1, v0.18.0, v0.18.1, v0.18.2, v0.19.0, v0.19.1, and v0.21.0 were tagged. The other versions from 0.1.0 through 0.15.0,
0.17.0, and 0.20.0, exist only as changelog entries.

## UI styling

The browser UI uses native HTML and shared rules in
`src/autodj/static/app.css`. Use `.card` for a grouped surface,
`.settings-row` for a label and its control, `.setting-desc` for visible help,
`.field-error` for an error stated in text, and `.no-results` for an empty
list. Use `.dialog-actions` for a right-aligned, wrapping button row. These
patterns preserve source and keyboard order; keep action-list rows aligned with
their labels, and keep purpose-specific layouts such as the clockwheel step
grid and dense tables local to their feature. Reuse the existing theme and
spacing tokens, and keep native focus, forced-colors, and narrow-screen
behavior in view.

## Where things live

- `src/autodj/cli.py` — CLI entry points (Click).
- `src/autodj/server.py` — FastAPI + WebSocket web layer.
- `src/autodj/stations.py` — a station's runtime (stream, recorder, schedule, start and
  shutdown order) and `StationPaths`, the only code that knows where station state files live.
- `src/autodj/station_routes.py` — routes that act on one station. Each answers at `/api/...`
  for the default station and at `/api/stations/<slug>/...` by name. A new route goes either here
  or, when every station shares it, in `server.py`; `tests/integration/test_station_routes.py`
  fails until the route is listed as one or the other.
- `src/autodj/static/` — web UI: HTML, CSS, JS, AudioWorklets.
- `src/autodj/player.py` — track picking and the server-side mix (the browser's crossfade
  engine is `src/autodj/static/modules/audio-engine.js`).
- `src/autodj/similarity.py` — FAISS query + ranking.
- `src/autodj/explain.py` — the "why this track?" reasoner.
- `src/autodj/jobs.py` — background subprocess runner for the web UI.
- `src/autodj/transitions.py` — the transition effects; `TransitionFx` is the single source of
  truth for the names `config.toml`, the server and the web UI accept.
- `tests/unit/` — pure unit tests, no audio hardware.
- `tests/integration/` — pipeline + server tests against mocks.
- `tests/smoke/` — CLI end-to-end smoke tests.
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
