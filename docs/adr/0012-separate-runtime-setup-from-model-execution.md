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
| `ADR-0012-04` | A fully Torch-free model-cache inspection for model-capable installs is not implemented or accepted. | gap | Cache inspection currently imports model.py/Torch without loading weights; do not infer broader coverage from import-only tests. |
| `ADR-0012-05` | Hardware/model compatibility and release artifacts require their applicable environments. | manual | Run the relevant MuQ inference, hardware, wheel/container, full CI, and release-sampling checks for changes to those surfaces. |
| `ADR-0012-06` | Base serving dependencies exclude the model stack; no-model setup saves a reusable runtime without a Torch probe. | automated | `tests/unit/test_setup_flow.py::test_no_model_core_metadata_keeps_analysis_optional`; `tests/unit/test_setup_flow.py::test_no_model_setup_saves_runtime_without_torch_probe` |
| `ADR-0012-07` | No-model servers accept compatible remote results with an existing model cache; missing cache requests are refused with recovery advice that does not promise no-model downloads. | automated | `tests/unit/test_worker_api.py::test_a_valid_result_goes_into_the_live_catalog`; `tests/unit/test_worker_api.py::test_a_server_without_the_model_cannot_check_results` |
| `ADR-0012-08` | Similarity is offered only when local MuQ execution is available or a compatible worker can use installed model weights; a saved selection remains intact when unavailable. | automated | `tests/unit/test_audio_analysis_pipeline.py::test_similarity_choice_reflects_installed_capability`; `tests/jsmodules/library-folders.test.js::offers similarity only when available and preserves unavailable existing folders` |
| `ADR-0012-09` | Setup synchronizes managed runtimes from the lock and selected backend on rerun; adopted external runtimes stay unchanged unless explicitly managed. | automated | `tests/unit/test_setup_flow.py::test_install_python_always_synchronizes_and_checks_runtime`; `tests/unit/test_setup_flow.py::test_explicit_existing_environment_is_adopted_without_package_install`; `tests/unit/test_setup_flow.py::test_manage_environment_updates_adopted_runtime_and_saves_managed_ownership` |
| `ADR-0012-10` | Managed synchronization is exact: the final install removes packages that the lock, the backend wheel profile and playback support do not need. | automated | `tests/unit/test_setup_flow.py::test_managed_sync_removes_packages_the_lock_no_longer_lists`; `tests/unit/test_setup_flow.py::test_no_model_sync_keeps_installed_sound_card_output` |
| `ADR-0012-11` | Interactive setup asks once whether to enable MuQ; Yes verifies a real generated-audio embedding before saving the runtime, while No retains existing weights without invoking model verification. Unattended setup requires an explicit choice. | automated | `tests/unit/test_setup_flow.py::test_interactive_model_choice_is_asked_once_and_does_not_import_tracks`; `tests/unit/test_setup_flow.py::test_unattended_setup_requires_explicit_model_choice`; `tests/unit/test_setup_flow.py::test_model_verification_failure_does_not_save_runtime`; `tests/unit/test_setup_model.py::test_cached_model_is_loaded_offline_without_downloading` |
| `ADR-0012-12` | Setup refuses detected active AutoDJ processes before mutation; updater maintenance excludes its own stopped-server supervisor but still refuses other launchers. Updates preserve model mode without new weight downloads. | automated | `tests/unit/test_setup_flow.py::test_running_autodj_refuses_setup_before_mutations`; `tests/unit/test_setup_flow.py::test_web_update_preserves_model_mode_without_downloading`; `tests/unit/test_setup_activity.py::test_update_ignores_its_own_supervisor_but_not_other_launchers` |

## Amendment 2026-10-18: optional no-model serving runtime

The operator requested a development server within a 2 GiB home. A CPU-only
install without weights still exceeded the available space because it included
the model packages. Add an explicit `setup --no-model` mode and move MuQ and
Transformers into the existing `index` and `all` extras. Keep ordinary guided
CPU/GPU setup and the container model-capable, rather than silently changing
those installation paths. This extends the proposed record; it does not claim
upstream maintainer acceptance.

The no-model runtime verifies serving/audio imports without constructing or
importing Torch. It disables local model jobs and reports absent local analysis
as an expected Doctor warning, while preserving basic imports, playback,
streaming, saved vectors and remote analysis results. It does not introduce an
alternate embedding model or change the trust boundary for remote workers.
Full-install model-cache inspection retains the limitation in ADR-0012-04.

### Clarification 2026-10-20: output packages and remote analysis prerequisites

Fresh no-model setup includes browser playback and streaming but omits the
sound-card output packages supplied by the `play` extra. Remote analysis retains
the existing exact-weight identity contract: the server needs an existing
compatible cache, including in no-model mode. No-model indexing does not
bootstrap that cache, and enabling a remote worker does not remove the server's
model-storage requirement. Supporting fresh servers without cached weights is
outside this change. The worker guide documents the prerequisite; worker API
diagnostics distinguish restoring the cache from using model-capable indexing.

## Amendment 2026-10-23: the launcher restarts `serve`

Settings, Updates gains a Restart button
([ADR 16](0016-update-and-restart-from-the-web.md)). The server asks to be started again by
exiting with status 75. The source checkout's launcher (`autodj`, `autodj.cmd`, which set
`AUTODJ_BOOTSTRAP`) now runs `serve` as a child process in the selected runtime, also when the
launcher already runs in that runtime, and starts it again when it exits with 75. It sets
`AUTODJ_SUPERVISOR=1` on the child, so the server knows it can restart and a nested launcher does
not loop again. Other commands, and the installed `autodj` command of a wheel, keep running as
before. The server stays in the launcher's process group; the launcher passes SIGTERM on to the
server (and SIGINT, SIGHUP or SIGQUIT only when they came to it alone), keeps waiting through Ctrl+C while the
server shuts down, and on Windows ends the server with itself through a job object. The runtime
selection, unattended-setup behaviour and model boundaries above are unchanged; ADR-0012-01
still holds. The launcher's restart contracts are in ADR 16 (ADR-0016-04).

## Amendment clarification 2026-10-27: keep shared audio analysis separate from MuQ

[Issue #594](https://github.com/blindndangerous/AutoDJ/issues/594) adds a Basic, Audio and
Similarity folder policy. Audio analysis uses the existing CPU audio stack to store versioned
features and DJ metadata without loading MuQ; Similarity adds an embedding generated by the
configured MuQ weights. This keeps no-model serving and audio analysis separate from local model
execution. Similarity is offered when the local model runtime and installed weights are usable, or
when a compatible worker can use a feature cache already present on the server. A previously saved
Similarity selection remains saved if those resources become unavailable. Worker results continue
to require the exact configured model/weight and analysis identities. This amendment does not
change the ADR's Proposed status or its hardware and listening evidence limits.

## Amendment 2026-10-27: synchronize setup-managed runtimes

[Issue #609](https://github.com/blindndangerous/AutoDJ/issues/609) changes setup-managed
runtime reuse: every setup run synchronizes application dependencies from `uv.lock` together
with the selected backend's pinned wheel profile, then checks dependency compatibility, imports
and the selected processing device before saving the selection. The environment is reused when
the backend is unchanged. Exported lock requirements exclude the model stack for no-model mode;
existing optional packages are not removed. Explicit `--environment` adoption remains
externally managed, and a saved external selection remains unchanged on reruns unless the
operator supplies `--manage-environment`. That opt-in makes setup responsible for synchronization
and records managed ownership only after successful verification. Dry-run displays the target and
commands without running them or writing setup files. This amendment preserves the existing
hardware compatibility evidence limits and the ADR's Proposed status.

## Amendment 2026-10-29: exact managed synchronization

[Issue #626](https://github.com/blindndangerous/AutoDJ/issues/626) found that synchronization
left packages a newer or older lock no longer lists, so the dependency check could fail on
them, including during a web update's rollback. The final install of a setup-managed runtime
is now exact: it removes packages that the exported lock requirements, the backend's pinned
wheels and the project's selected extras do not need. Packages seeded with the environment,
such as pip, are kept. This replaces the 2026-10-27 statement that existing optional packages
are not removed, except that a no-model runtime that already has the sound-card output
package keeps the `play` extra. Adopted external runtimes remain unchanged. This amendment
preserves the ADR's Proposed status and evidence limits.

## Amendment 2026-10-29: explicit MuQ choice and setup verification

The maintainer agreed in [issue #686](https://github.com/blindndangerous/AutoDJ/issues/686)
that setup should ask one model question, explain its purpose and approximate download
size, and carry out the choice. `--model yes` installs model support, obtains missing
weights and verifies a short generated-audio embedding without scanning the library.
Existing local weights are reused without refreshing a floating revision. `--model no`
and its `--no-model` alias select the no-model runtime and preserve downloaded files.
Interactive setup asks again; only the verified runtime selection is persisted, with
no additional configuration keys. Unattended manual setup requires an explicit choice.
This replaces the optional three-track import described earlier; the hidden legacy
`--test-import` flag now selects the model verification path instead of importing music.

Setup checks launcher locks, the configured port and recognizable processes scoped to
the checkout or selected environment before changing software. This is a point-in-time
check, not an exclusion lock: custom scripts and processes starting afterwards remain
outside its guarantee. The supervising updater identifies itself through a transient
environment marker while its server is stopped. That path retains the existing model
mode and performs runtime verification without asking or downloading new weights;
older setup versions ignore the marker, preserving rollback compatibility.
Setup also recognizes an older updater through its held update/serving locks and
process ancestry, so the first upgrade to this flow can proceed without prompting.

Doctor and setup share plain-text wrapping; Doctor's JSON output remains machine-readable.
Model/GPU checks still require the native evidence in ADR-0012-05. This amendment records
the agreed setup evolution without accepting unrelated Proposed material in this ADR.

## Verification

Run the following checks for affected changes and record results and platform
limits in the relevant PR. Test links do not establish execution or change this
record's Proposed status.

- [ ] Verify saved setup selection and unattended behavior with
  [launcher tests](../../tests/unit/test_launcher.py).
- [ ] Verify identity and weight-file changes without loading the model with
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

## Amendment 2026-10-29: playback support is independent of MuQ

The maintainer clarified in issue #686 that managed setup includes the `play`
extra when MuQ is declined, and `all` when accepted. This supersedes the earlier
fresh no-model omission and installed-package exception: speaker support must
not depend on choosing a music model or on previous installation history.
Minimal package installations remain supported. Doctor checks sounddevice for
stations configured for server sound-card output, including stream monitoring,
rather than warning for browser or streaming-only stations.
