# ADR contract verification, 2026-10-16

Evidence for the subsequent contract-enforcement changes on
`docs/adr-requirements`, based on `3b3b5b3d`, in the isolated worktree
`/home/ccn/.codex_openai_chatgpt/worktrees/122a/autodj`. This report describes
the uncommitted candidate; it is not a decision or a task list. The earlier
[reconciliation report](verification-2026-10-16.md) records the initial phase
only. Its documentation-only scope and results are historical, not claims about
this phase. No earlier test result is reused as fresh execution evidence here.

## Scope and inputs

Added 51 stable constraint IDs across 12 ADRs, with specific native test selectors
and reciprocal annotations. Existing ADR bodies remain historical; seven
retrospective ADRs remain Proposed. Added a standard-library, read-only validator
and fixture tests, a local commit hook, and normal pytest/CI smoke coverage.
Updated canonical AGENTS guidance, contributor/index/process/PR guidance, and
local attribution for selected Polyrepo principles reviewed at `a332e7b`.
Claude and Cursor entrypoints remain pointers to AGENTS. No sibling checkout,
registry enrollment, runtime import, or required contributor Beads installation.

Existing tests were reused. Two demonstrated assertion gaps were strengthened:
nearest-neighbor selection now uses hand-known geometry with equal metadata and
asserts the exact eligible answer; a verbose-job test proves retained output is
bounded and keeps the latest output. Other existing-test changes are reciprocal
ADR annotations. Runtime source, dependency declarations, lockfiles, and CI
workflow configuration are unchanged. The added pytest smoke runs through the
existing CI workflow; adding the local hook does not install or bypass hooks.

Linux, CPython 3.14.8, pytest 9.1.1, Node 26.11.1, Vitest 4.1.11. Used the same
ignored partial `.venv`, editable package install, and lockfile-based frontend
installation described in the earlier report. This is not a full all-extras
environment. Torch, MuQ, Transformers, and model weights remain absent. FFmpeg
7.0.2 is provided by the ignored `.cache/bin/ffmpeg` symlink. No ignored files
supply ADR policy or expected results. Scratch/cache inputs support execution
only. Tests ran serially, with focused coverage options cleared explicitly.

## Actual results

| Check | Result and scope |
| --- | --- |
| Every mapped Python selector: 56 selectors, parameterization expanded | 125 passed, 1 skipped in 31.07 s; Windows child-process check skipped on Linux |
| Final validator, complete jobs and similarity files | 143 passed in 6.16 s, including 31 validator fixture cases |
| Both affected JavaScript files | 37 passed across 2 files |
| Documented `uv run --frozen --no-sync python scripts/check_architecture_docs.py` | Passed; explicitly printed manual obligations and two gaps; did not execute behavior |
| Full-tree Ruff lint and format check | Passed; 382 Python files already formatted |
| `npm run lint` | Passed |
| `pre-commit validate-config` and `git diff --check` | Passed |
| Native full Python wrapper | Exit 2 during collection: four missing-Torch errors; no full test/coverage success |
| Controlled in-memory mutations | Both intended regressions rejected by AssertionError; no runtime files changed |

Validator fixtures first reproduced missing/multiline destinations, native-test
filename exclusions, and missing reverse annotations in nested JS or `*_test.py`
files. They failed before their corresponding fixes and pass afterward. Other
negative fixtures cover invalid statuses, index entries, duplicate IDs/numbers,
wrong ownership, nonexistent selectors, missing annotations, malformed contract
methods/evidence, repository escapes, and missing headings. Positive fixtures
cover historical fenced examples, multiline inline code, duplicate heading
anchors, explicit reference definitions, spaces, class selectors without imports,
Proposed status, retirement, read-only operation, and manual/gap reporting.

A read-only reviewer identified the multiline-link and discovery gaps. Both
were reproduced and fixed; the final reverse-discovery fixtures address the
last finding. Review also confirmed the two strengthened behavioral assertions
are meaningful. The reviewer did not run tests or alter files.

## Commands and reproduction

Run from the worktree, with no concurrent heavy suite:

```bash
uv run --frozen --no-sync python scripts/check_architecture_docs.py
.venv/bin/python -m pytest -o addopts= -p no:cacheprovider --basetemp=.cache/pytest-adr-final -q tests/unit/test_architecture_docs.py tests/unit/test_jobs.py tests/unit/test_similarity.py
npm test -- tests/jsmodules/live-region.test.js tests/jsmodules/confirm-dialog.test.js
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/ruff format --check src/ tests/ scripts/
npm run lint
pre-commit validate-config
git diff --check
PATH="$PWD/.cache/bin:$PATH" .venv/bin/python scripts/ci_pytest.py -p no:cacheprovider --basetemp=.cache/pytest-adr-native
```

The mapped test run collected the unique backticked Python selectors from
`automated` rows in all numbered ADRs and executed them directly. Reproduce its
selection and command without depending on a sibling repository:

```python
from pathlib import Path
import os
import re
import subprocess

selectors = sorted({
    selector
    for path in Path("docs/adr").glob("[0-9]*.md")
    for line in path.read_text().splitlines()
    if "| automated |" in line
    for selector in re.findall(r"`(tests/[^`]+)`", line)
    if ".py::" in selector
})
env = os.environ.copy()
env["PATH"] = str(Path(".cache/bin").resolve()) + os.pathsep + env["PATH"]
raise SystemExit(subprocess.call([
    ".venv/bin/python", "-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider",
    "--basetemp=.cache/pytest-adr-mapped", "-q", *selectors,
], env=env))
```

For the mutation checks, an isolated Python process loaded the actual test
functions and temporarily patched `autodj.jobs.deque` to an unbounded deque and
`SimilarityIndex.find_next_for_path` to return the wrong eligible song. Each
actual assertion raised; patches ended with the process. These are additional
regression checks, not changes to runtime code or a new test framework.

## Limits and pending verification

The native wrapper collected 7,266 items but could not proceed: Torch was
missing in `tests/integration/test_muq_transformers_compat.py`,
`tests/unit/test_compute.py`, `tests/unit/test_indexer.py`, and
`tests/unit/test_model.py`. `git diff --exit-code HEAD --` for those four files
and `src/autodj/model.py` / `src/autodj/compute.py` passed, establishing that
these import paths are unchanged. This is a local provisioning limitation,
not evidence of a candidate runtime regression. No full coverage result exists.
Local follow-up `autodj-m91` records provisioned full-gate/CI verification; shared
requirements and results remain understandable from this report without Beads.

Next required merge verification in a normally provisioned environment:
`uv sync --frozen --all-extras`, then `uv run python scripts/ci_pytest.py` and
final CI for this exact candidate. All frontend suites, browser audits, Windows,
physical audio/GPU/model inference, real power-loss/network-share recovery,
soak/performance measurements, release/container checks, and fresh human speech
sampling are not established by these focused checks. Manual/gap rows remain
obligations or evidence limits, not passes. Full CI must pass before merging.
No commit, push, remote tracker sync, or external publication was performed.

## Subsequent review: continuing without local Torch

Operator approval permits continuing the bounded ADR review without installing
Torch. Rechecked that runtime source, dependency declarations/lockfiles, and CI
workflow bytes are unchanged. The collection-blocked files cover compute device
selection, model/indexer helpers, and real MuQ/Transformers compatibility. They
are not evidence required to execute the standard-library validator, and all
automated constraint mappings were already exercised without Torch apart from
the explicitly recorded Windows skip. Model inference and hardware claims
remain unverified, and ADR 12 retains its diagnostic import limitation.

Reviewed ADRs 6–12 against context, scope/exclusions, authority, alternatives,
consequences/revisit triggers, implementation paths, verification, and evidence
gaps; checked agent pointers and contributor/PR discovery guidance. No new
architecture conflict was found. Corrected one stale sentence describing the
PR checklist. No acceptance status, runtime, test assertion, skip policy,
dependency, or CI rule changed in this review.

The preceding behavioral, lint, formatting, mutation, and native-wrapper results
are reused evidence from this same worktree: the relevant code, tests, tooling,
options, and environment are unchanged. This review does not claim another
behavioral or full-suite run. The final documentation checker, historical-body
and template checks, and real-repository pytest smoke are checked separately
after these prose edits. Full provisioned CI remains required before merge;
continuing local review does not waive it or establish the coverage floor.

Final review results: the documented validator passed; the temporary historical
and template audit passed (377 local links/fragments across 60 changed/new paths,
original ADR bodies preserved, all seven retrospective statuses still Proposed);
the real-repository pytest smoke passed (1 test, 1.62 s); and
`git diff --check` passed. The smoke command was:

```bash
.venv/bin/python -m pytest -o addopts= -p no:cacheprovider --basetemp=.cache/pytest-adr-review -q tests/unit/test_architecture_docs.py::test_repository_architecture_contracts_are_consistent
```
