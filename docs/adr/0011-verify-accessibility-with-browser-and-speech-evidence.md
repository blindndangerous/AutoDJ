# 11. Verify browser accessibility with interaction and speech evidence

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

AutoDJ exposes substantial playback and station control through the browser.
The repository already defines an announcement contract, keyboard conventions,
browser audits, and release speech samples. This record connects those existing
requirements to architecture; it does not certify WCAG conformance.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Keep the served UI in native HTML/CSS/ES modules with accessible interaction
contracts, and use both automated browser checks and documented screen-reader
release samples to evaluate changes.

- Prefer native controls, short labels, visible keyboard focus, and help that
  remains available without repeatedly announcing long explanations.
- Route transient announcements through the shared live-region helper. One
  message has one automatic announcement path; ticking counters stay quiet.
- Dialog, tab, and keyboard changes must preserve meaningful focus and work
  without requiring pointer input. Keep visual and spoken numeric text consistent.
- Check themes, contrast, reduced motion, forced colors, and relevant responsive
  layouts using the documented audit surface.
- State exactly which browser/screen-reader combinations and flows were sampled,
  how sampling was performed, defects found, and method limitations. An agent
  reading speech output is not a claim that a person listened.
- Node tooling is development/test infrastructure; the served UI requires no
  JavaScript framework build step.

## Alternatives for review

- Native modules plus interaction/speech evidence: fits the existing UI and
  exercises actual behavior; audits and samples still have coverage limits.
- Static markup checks alone: cheap regression detection, but cannot establish
  focus behavior, announcements, or what a screen reader speaks.
- Adopt a component framework for accessibility: reusable patterns may help,
  but migration adds a build/runtime model and cannot substitute for verification.

## Consequences and revisit conditions

UI changes must identify affected interaction paths and follow the release
sampling rules. Avoid duplicating the detailed contracts here; their canonical
home stays in the accessibility guide. Revisit when UI delivery or supported
assistive-technology expectations change.

## Implementation and operational references

- [Accessibility contracts and sampling](../accessibility-testing.md),
  [contribution checks](../../CONTRIBUTING.md), and [release evidence](../accessibility-samples/0.21.0.md).
- [Live regions](../../src/autodj/static/modules/live-region.js),
  [dialogs](../../src/autodj/static/modules/confirm-dialog.js),
  [hotkeys](../../src/autodj/static/modules/hotkeys.js), and [HTML](../../src/autodj/static/index.html).
- [Development tooling](../../package.json) and [CI](../../.github/workflows/ci.yml).

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
| `ADR-0011-01` | The visual status mirror does not add a second automatic announcement path. | automated | `tests/jsmodules/live-region.test.js::keeps the visible mirror out of the accessibility tree` |
| `ADR-0011-02` | An unchanged status does not rewrite its live region during passive updates. | automated | `tests/jsmodules/live-region.test.js::does not rewrite a region that already carries the message` |
| `ADR-0011-03` | Cancel returns focus to the opener after the dialog closes. | automated | `tests/jsmodules/confirm-dialog.test.js::puts focus back on the opener only once the dialog has closed, after Cancel` |
| `ADR-0011-04` | A release records the required real-browser and screen-reader sample with scope and limitations. | manual | Follow docs/accessibility-testing.md; record exact engine, screen reader, flows, defects, and whether a person listened. |

## Verification

Checked items refer to the scoped 2026-10-16 runs in
[the audit evidence](verification-2026-10-16.md), including its platform limits.
They do not change this record's Proposed status.

- [x] Verify announcement behavior with [live-region tests](../../tests/jsmodules/live-region.test.js).
- [x] Verify dialog/focus and keyboard behavior with
  [dialog tests](../../tests/jsmodules/confirm-dialog.test.js) and
  [hotkey tests](../../tests/jsmodules/hotkeys.test.js).
- [ ] Run the applicable [browser visual audit](../../tests/playwright/visual_audit.mjs)
  on a running server according to the accessibility guide.
- [ ] For a release, record the required screen-reader sample and link it from
  release notes; existing historical samples do not verify new changes.

## Evidence gaps

Browser/speech checks have explicit limits, and CI does not exercise every
engine. This documentation pass changes no UI and does not create a fresh
release sample. Maintainer acceptance of the reconstructed decision is pending.
