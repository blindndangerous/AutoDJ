# 12. Separate runtime setup from model execution and verify compatibility

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

AutoDJ needs platform-specific compute setup without making routine server
operations load the music model. The configured model name is not an arbitrary
architecture plug-in. The repository also has a defined wheel/container release
path. This record covers setup, dependency, model, and distribution boundaries;
it does not select new hardware backends or re-evaluate model quality.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Use the launcher and guided setup to select and verify the supported runtime;
keep model loading in analysis execution and validate exact model identity at
the worker boundary. Maintain the existing plain-file browser delivery and
versioned Python/container distribution.

- Setup remembers and verifies the supported CPU/NVIDIA/AMD runtime selection;
  unattended fresh startup must not silently install missing software.
- Routine serving and diagnostic entrypoints defer model construction and weight
  loading. Importing Doctor and checking an empty index avoid heavy runtimes.
  Doctor's model-cache check currently imports `model.py` and therefore Torch,
  without constructing the model or loading its weights. Remote model identity
  inspection works without importing Torch.
- The current MuQ path assumes its loader, 24 kHz input, pooling, and embedding
  dimensions. A configuration-name change does not implement a different model.
  Representation changes require compatible preprocessing, dimensions, identity,
  and a deliberate reindex strategy.
- Keep directly imported dependencies declared, runtime extras meaningful, and
  development-only Node tools outside the serving requirement.
- Follow the existing release procedure: version/lock/changelog agreement,
  required CI/security checks, installed-wheel smoke verification, and release
  accessibility evidence. Publication requires maintainer authority.
- Licences and weight usage restrictions remain documented in README and model
  guidance; this ADR makes no new legal or benchmark claims.

## Alternatives for review

- Launcher-managed runtimes and lazy analysis loading: supports different
  compute environments without model startup cost on routine paths; requires
  compatibility tests and explicit setup selection.
- One environment importing the model everywhere: simpler imports, but expands
  resource and failure requirements for serving and diagnosis.
- Generic model/backend plug-ins: could enable alternatives, but are additional
  architectures requiring evaluation and migration rather than a config toggle.

## Consequences and revisit conditions

Dependency and loader changes must consider setup, CPU/GPU execution,
workers, wheels, and containers. Revisit for a supported alternate architecture
or new distribution target. Mocked setup tests do not establish hardware
compatibility; model quality needs representative evaluation.

## Implementation and operational references

- [Launcher](../../src/autodj/launcher.py), [setup](../../scripts/setup.py),
  [hardware selection](../../scripts/setup_hardware.py), and [diagnostics](../../src/autodj/doctor.py).
- [Model](../../src/autodj/model.py), [identity](../../src/autodj/model_identity.py),
  [package metadata](../../pyproject.toml), [container](../../Containerfile), and
  [release workflow](../../.github/workflows/release.yml).
- [Setup guide](../setup.md), [AMD guide](../windows-amd.md),
  [model selection](../model-selection.md), and [release procedure](../../CONTRIBUTING.md#releasing).

This documentation pass adds no runtime dependency, configuration switch, or
migration. Any future change to this boundary needs its own affected-path and
compatibility assessment.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0012-01` | Unattended fresh bootstrap does not silently install software. | automated | `tests/unit/test_launcher.py::test_unattended_fresh_bootstrap_does_not_install` |
| `ADR-0012-02` | Weight identity follows resolved snapshot commits or manual-folder bytes, not a floating branch name. | automated | `tests/unit/test_model_identity.py::test_a_cached_model_is_named_by_the_commit_its_branch_resolved_to`; `tests/unit/test_model_identity.py::test_a_manual_folder_is_named_by_a_digest_of_its_weights` |
| `ADR-0012-03` | Importing Doctor and checking an empty index do not import heavy runtimes. | automated | `tests/unit/test_doctor.py::test_import_and_empty_index_check_do_not_import_heavy_runtimes` |
| `ADR-0012-04` | A fully Torch-free Doctor model-cache check is not implemented or accepted. | gap | Cache inspection currently imports model.py/Torch without loading weights; do not infer broader coverage from import-only tests. |
| `ADR-0012-05` | Hardware/model compatibility and release artifacts require their applicable environments. | manual | Run the relevant MuQ inference, hardware, wheel/container, full CI, and release-sampling checks for changes to those surfaces. |

## Verification

Checked items refer to the scoped 2026-10-16 runs in
[the audit evidence](verification-2026-10-16.md), including its platform limits.
They do not change this record's Proposed status.

- [x] Verify saved setup selection and unattended behavior with
  [launcher tests](../../tests/unit/test_launcher.py).
- [x] Verify identity and weight-file changes without loading the model with
  [model identity tests](../../tests/unit/test_model_identity.py).
- [ ] Verify supported compatibility handling with
  [MuQ compatibility tests](../../tests/integration/test_muq_transformers_compat.py).
- [ ] For changes to distribution, exercise the applicable CI/release checks and
  [container smoke test](../../scripts/container_smoke.sh), not just unit tests.

## Evidence gaps

A fully Torch-free Doctor cache check is not implemented or established as an
accepted requirement. The import-only diagnostic test must not be cited as proof
of that broader property. See [Doctor code](../../src/autodj/doctor.py),
[model imports](../../src/autodj/model.py), and
[diagnostic tests](../../tests/unit/test_doctor.py).

Real CPU/GPU compatibility, listening quality, installed-wheel behavior, and
container operation need their applicable environments and checks. This pass
does not download weights or assert new platform support. Historical rationale
and final acceptance remain pending.
