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
  [the dialog guard](../../src/autodj/static/modules/dialog-guard.js),
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
| `ADR-0011-03` | Cancel and Escape return focus to the opener after the dialog closes, straight from the dialog's button and never through the page body. Over another modal dialog, the confirmation lets go of focus before it closes and takes it again once closed (#385). Each opening is answered at its own action, before the dialog closes, and removes its listeners then; a later close event only checks focus, so it cannot answer or act for a later opening; one whose action is running when the browser closes the dialog answers with the action's result. | automated | `tests/jsmodules/confirm-dialog.test.js::puts focus back on the opener only once the dialog has closed, after Cancel`, `tests/jsmodules/confirm-dialog.test.js::does the same for Escape, and holds Escape while the action runs`, `tests/jsmodules/confirm-dialog.test.js::lets go of focus and takes it back once closed, over an editor dialog (#385)`, `tests/jsmodules/confirm-dialog.test.js::ignores the last confirmation's close event when another opens before it comes`, `tests/jsmodules/confirm-dialog.test.js::answers each opening at its own action, so a late close event leaves no listener behind`, `tests/jsmodules/confirm-dialog.test.js::answers a running action with its own result when the browser closes the dialog under it` |
| `ADR-0011-04` | A release records the required real-browser and screen-reader sample with scope and limitations. | manual | Follow docs/accessibility-testing.md; record exact engine, screen reader, flows, defects, and whether a person listened. |
| `ADR-0011-05` | A confirmed action that names another control moves focus straight to it from the dialog's button, never through the opener or the page body. | automated | `tests/jsmodules/confirm-dialog.test.js::moves focus to the action's target, not the opener, after a confirm` |
| `ADR-0011-06` | The shared confirmation opens with `show()`, not `showModal()`, unless another modal dialog is open; it is marked `aria-modal`, its form has `role="none"`, Cancel is its last button and the one the browser focuses as it opens (`autofocus`), and while it is open a backdrop takes clicks, Tab and Shift+Tab stay in it, focus sent to the page returns to Cancel, and a click, press, Enter, Space, submission or typed input aimed at the page does not reach it. | automated | `tests/jsmodules/confirm-dialog.test.js::opens with show(), aria-modal, the form out of the tree, Cancel last and the backdrop shown`, `tests/jsmodules/confirm-dialog.test.js::keeps Tab, Shift+Tab and focus in the dialog, and the backdrop does nothing`, `tests/jsmodules/confirm-dialog.test.js::stops a page control activated while it is open, as a screen reader may` |
| `ADR-0011-07` | With NVDA in browse mode, Enter on a newly opened confirmation cancels it, and closing it reads the control focus returns to, not the top of the page. | manual | Open and close a confirmation such as Restart AutoDJ in Settings, Updates with NVDA in Chrome and Firefox; record the revision, versions, flows, speech, and whether a person listened. |
| `ADR-0011-08` | Every other dialog the page opens (editors, Browse…, Track info's unsaved changes, device Rename, the keyboard shortcuts list, pairing) opens the same way through `modules/dialog-guard.js`: `show()` and `aria-modal`, not `showModal()`, unless a modal dialog is open; a backdrop takes clicks; Tab and Shift+Tab go round the top dialog's controls; focus or a press aimed at the page, or at a dialog under it, does not reach it; Escape fires the dialog's cancel event; its safe button is last; and closing moves focus straight to the opener or the action's target, never through the page body. | automated | `tests/jsmodules/dialog-guard.test.js::opens with show() and aria-modal, with the backdrop shown until it closes`, `tests/jsmodules/dialog-guard.test.js::keeps Tab, Shift+Tab and focus in the dialog, past controls Tab cannot reach`, `tests/jsmodules/dialog-guard.test.js::stops a page control activated while it is open, as a screen reader may`, `tests/jsmodules/dialog-guard.test.js::fires cancel on Escape and closes with focus straight back on the opener`, `tests/jsmodules/dialog-guard.test.js::moves focus straight to a target, the opener inert while the dialog closes`, `tests/jsmodules/dialog-guard.test.js::stacks a dialog opened from another: only the top one guards`, `tests/jsmodules/path-picker.test.js::opens with show() and aria-modal, Cancel last, and keeps Tab and Escape in the dialog` |
| `ADR-0011-09` | With NVDA in browse mode, Enter on a newly opened Browse… dialog does not choose the folder, and closing each dialog in `ADR-0011-08` reads the control focus returns to, not the top of the page. | manual | Open and close each dialog listed in docs/accessibility-testing.md with NVDA in Chrome and Firefox; record the revision, versions, flows, speech, and whether a person listened. |

## Amendment 2026-10-27: dialog focus never rests on the page body

The confirmation's buttons no longer let go of focus before it closes. Focus
rested on the page body for a moment, and NVDA in Firefox read the page's
first link before the control focus returned to
([#602](https://github.com/blindndangerous/AutoDJ/issues/602)). Focus now goes
from the dialog's button straight to the opener, which the browser restores
as a modal dialog closes, or straight to the control an action names, with
the opener inert while the dialog closes so the browser does not restore it
first. `ADR-0011-03` gains Escape and the page-body condition; `ADR-0011-05`
is new. Whether NVDA reads each close correctly remains a manual check.

## Amendment 2026-10-28: the confirmation opens without showModal()

The 2026-10-27 change did not stop NVDA reading "Skip to main content".
NVDA reads a web dialog in a browse-mode document of its own. Behind a
`showModal()` dialog, Chrome marks every other node ignored
(`activeModalDialog`: 195 exposed nodes fell to 16), so NVDA's page
document lost its caret, and when the dialog closed NVDA spoke the page
document's line at the caret, its top line, before the focused control.
On opening, NVDA reads a new dialog document through (say all on page
load), leaving its cursor on the last button, so Enter in browse mode
pressed Restart in Chrome
([#627](https://github.com/blindndangerous/AutoDJ/issues/627)).

The shared confirmation therefore opens with `show()` and `aria-modal`,
which leaves the page in the accessibility tree, with Cancel as the last
button (`ADR-0011-06`). The page is not made inert or `aria-hidden`, which
would hide it from NVDA again, so a screen reader that ignores `aria-modal`
can still read it; a click, press, Enter, Space, submission or typed input
aimed at the page while the dialog is open is stopped before the page sees
it. Opened over another modal dialog, it still uses `showModal()` and lets
go of focus before closing, as other modal dialogs do (#385). NVDA evidence for `ADR-0011-07`: on 2026-10-28, with
NVDA 2026.2 in Chrome and Firefox (browser versions not recorded), on
revision f075dfd7 with this behaviour selected for the trial. Flows: the
Restart AutoDJ confirmation in Settings, Updates opened, Tab and Shift+Tab
inside it, Enter on opening, Escape, and Cancel. Enter on opening
cancelled. Tab went round Restart and Cancel. Escape and Cancel returned
focus to Restart AutoDJ, with no skip link in either browser. NVDA
said "button Restart AutoDJ", the page document's line at its caret,
then "Restart AutoDJ button", the focus. NVDA speaks the caret line
whenever focus moves into another browse-mode document, so the page
cannot avoid this double read. An agent read NVDA's speech; no person
listened.

## Amendment 2026-10-29: every dialog opens without showModal()

The other dialogs still opened with `showModal()`, so NVDA read "Skip to
main content" as they closed, and in the Browse… dialog, where Select this
folder came last, Enter after NVDA read the dialog could choose the folder
([#631](https://github.com/blindndangerous/AutoDJ/issues/631)). The
confirmation's guard moved to `modules/dialog-guard.js`, which the editors,
Browse…, Track info's unsaved changes, device Rename, the keyboard
shortcuts list and pairing now share (`ADR-0011-08`). Dialogs opened from
one another stack, and only the top one guards, so a confirmation over an
editor now opens with `show()` as well. Browse… and device Rename put
Cancel last. None of these dialogs lets go of focus before it closes any
more. `ADR-0011-09` is the NVDA check; it has no recorded evidence yet.

## Verification

Run the following checks for affected changes and record results and platform
limits in the relevant PR. Test links do not establish execution or change this
record's Proposed status.

- [ ] Verify announcement behavior with [live-region tests](../../tests/jsmodules/live-region.test.js).
- [ ] Verify dialog/focus and keyboard behavior with
  [dialog tests](../../tests/jsmodules/confirm-dialog.test.js),
  [dialog guard tests](../../tests/jsmodules/dialog-guard.test.js) and
  [hotkey tests](../../tests/jsmodules/hotkeys.test.js).
- [ ] Run the applicable [browser visual audit](../../tests/playwright/visual_audit.mjs)
  on a running server according to the accessibility guide.
- [ ] For a release, record the required screen-reader sample and link it from
  release notes; existing historical samples do not verify new changes.

## Evidence gaps

Browser/speech checks have explicit limits, and CI does not exercise every
engine. This documentation pass changes no UI and does not create a fresh
release sample. Maintainer acceptance of the reconstructed decision is pending.
