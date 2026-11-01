# 22. Share public error handling

Date: 2026-10-31
Status: Accepted.
Decision authority: Project owner, explicit consolidation decision on 2026-10-31.

## Context and scope

Domain refusals repeated the same response construction across station routers.
Pydantic validation also had several first-error extractors. Most request
validation responses contained a list in `detail`, while the browser API client
only displays a string. Operators consequently saw a generic refusal instead
of the validation reason.

The owner requested one shared implementation for each job while preserving
operator-visible status codes, messages, field keys, cache policies and recovery
filenames. The intentional error-contract change is string request-validation
detail, with the field when known. The same decision consolidates explicit CLI
failures and filesystem helpers; [ADR 10](0010-preserve-state-through-atomic-writes-and-recovery.md)
records the persistence boundary.

## Decision

- Public domain refusals derive from `FieldError`. Domain code supplies the
  message, field and status; one app handler renders them. Existing absent,
  null and empty-string field conventions remain compatible.
- Request validation uses the same handler and `first_issue` extractor. It
  returns a string `detail`, never a list or an echo of the rejected input.
  Embedded domain errors retain their public field and wording. Domain-specific
  labels and special messages remain in the owning module.
- Cache policy remains at the route boundary. Ads errors and intro-rule
  errors retain `no-store`; ordinary matched-intro upload field refusals retain
  their existing header behavior. The other surveyed helpers did not set
  `no-store`. Default and named station aliases share the same endpoint policy.
- CLI errors use `click.ClickException` or its subclasses and write to stderr.
  Usage and argument errors use `click.UsageError` or `click.BadParameter` and
  exit 2; other application failures exit 1. Normal output, JSON reports and
  nonfatal warnings retain their output streams. Setup reports a failed child's
  status and exits with that same status, including 130 for interruption and 2
  for argument errors. Server restart control-flow statuses retain their contract.

This decision does not replace structured concurrency/conflict payloads, invent
new public messages for internal exceptions, change station ownership, or
introduce a runtime dependency. [ADR 5](0005-a-station-owns-its-playout-chain.md)
continues to govern station aliases and ownership.

## Alternatives and consequences

Keeping router-local renderers avoids changing exception ancestry but leaves
status, field and cache handling duplicated. A transport-dependent domain base
could return responses directly, but would couple domain operations to FastAPI.
The selected base is independent of FastAPI; HTTP rendering stays in
`api_errors.py`.

Changing all error messages and recovery names would simplify compatibility
policy, but would break operator expectations without improving this refactor.
Small domain policies remain where the survey's copies were not equivalent.
Broad `ValueError` catches must let public domain errors reach the handler
unless the catch performs recovery or applies a distinct domain policy.

Revisit this contract if the client needs multiple simultaneous validation
issues or an externally versioned API. Such a change needs an explicit client
and compatibility plan.

## Implementation and operations

- [Domain errors and extraction](../../src/autodj/errors.py),
  [HTTP rendering](../../src/autodj/api_errors.py), and
  [app registration](../../src/autodj/server.py).
- [Browser API client](../../src/autodj/static/modules/api-client.js),
  [CLI](../../src/autodj/cli.py), and
  [contributor rules](../../CONTRIBUTING.md#where-things-live).
- [Diagnosis and CLI reports](../guides/operations/diagnose.md) and
  [station recovery filenames](../reference/operations/stations.md).

## Current contracts

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0022-01` | Request validation supplies readable string detail and a known field; the browser preserves both. | automated | `tests/integration/test_server_security.py::test_validation_reply_has_readable_detail_and_field`; `tests/jsmodules/api-client.test.js::surfaces validation words and preserves the field for the editor` |
| `ADR-0022-02` | Domain refusals preserve their status, message, field value and route cache policy. | automated | `tests/unit/test_api_errors.py::test_field_error_keeps_status_message_and_field`; `tests/unit/test_api_errors.py::test_error_headers_belong_to_the_route`; `tests/unit/test_api_errors.py::test_route_can_keep_distinct_error_cache_policies` |
| `ADR-0022-03` | CLI usage errors exit 2 and other application failures exit 1; errors go to stderr. | automated | `tests/unit/test_cli_errors.py::test_missing_inputs_are_click_errors_on_stderr`; `tests/unit/test_cli_errors.py::test_application_option_conflicts_exit_two` |
| `ADR-0022-04` | Setup reports failures on stderr and preserves the child process's exit status. | automated | `tests/unit/test_cli_errors.py::test_setup_failure_preserves_child_status_on_stderr` |
| `ADR-0022-05` | Station settings validation retains its location-based field prefix. | automated | `tests/integration/test_profile_editing.py::test_station_validation_keeps_the_location_field_prefix` |
| `ADR-0022-06` | Browser server-field maps preserve help, focus the refused field and leave unknown fields for the ordinary failure path. When that field already has focus, announce the same error once without a focus jump. | automated | `tests/jsmodules/dom-helpers.test.js::maps server fields to controls while preserving help and unknown-field fallback`, `tests/jsmodules/station-create.test.js::announces a server refusal once when its field already has focus` |
| `ADR-0022-07` | Shared request failure text preserves action wording and stale-station outcomes. | automated | `tests/jsmodules/api-client.test.js::formats failures without losing stale-station action outcomes` |

## Review clarification 2026-10-31

Owner review corrected the initial consolidation's CLI status handling: the
shared error family does not flatten usage errors or setup subprocess statuses
to exit 1. The current decision and contracts above record that correction.
Station settings validators currently raise plain `ValueError`, including their
repeat-rule, playback-choice and preset-name helpers. Their existing location
prefix remains unchanged and has a dedicated route regression check.

## Evidence limits

The mapped tests identify executable contracts, not proof of execution.
Record actual local and CI results separately. API-client tests exercise error
delivery; they do not establish browser focus, speech output, hardware behavior
or WCAG conformance. The accessibility evidence obligations in
[ADR 11](0011-verify-accessibility-with-browser-and-speech-evidence.md) remain.

## Amendment 2026-10-31: shared browser-page helpers

Browser editors use `showFieldError`/`clearFieldError` for invalid state and
preserved help, and `applyServerFieldError` for public field maps. Unknown fields
use the ordinary error path. `failureText` owns request failure prefixes and
stale-station wording; feature-specific corrective instructions remain local.
