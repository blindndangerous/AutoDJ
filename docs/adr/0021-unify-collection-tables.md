# 21. Use one collection table and row-action pattern

Date: 2026-10-31
Status: Accepted
Decision authority: Project owner, explicit implementation instruction on 2026-10-31.

## Context and scope

Collection screens mixed action lists, native table grids, a separate clockwheel
grid and rows with several Tab stops. Operators had to learn different navigation,
editing and removal rules. Long sentence rows also hid useful data structure.

The owner chose one shared pattern for every web collection screen and authorized
implementation without another design review. This record captures that decision;
acceptance of the pattern does not claim that migration or accessibility
verification is complete. No issue or PR has been published for this phase.

This decision complements the native-control, dialog and announcement contracts
described in [ADR 11](0011-verify-accessibility-with-browser-and-speech-evidence.md)
and preserves the individual-record CRUD boundary described in
[ADR 17](0017-save-settings-through-one-draft-boundary.md). Those records retain
their own Proposed statuses. Collection editing does not join the Settings draft
bar or change station/server/library ownership, routes or persisted formats.

## Decision and constraints

- Build collections with `collection-table.js`, reusing `tableRow` and
  `installTableNavigation`. Use native tables, real data columns and plain grid
  cells. Do not introduce row headers: the existing NVDA interaction deliberately
  avoids repeated "row header" announcements. Keep the established cell-focus
  keyboard model and one Tab stop for the whole table.
- Give each row at most one visible primary action, selected by frequency of use:
  usually Edit or Open, and Play next for the queue. Put every other action in one
  **Options for <row name>** menu. Use the shared menu and table-cell helper.
- Follow the WAI-ARIA APG menu-button pattern. The button has
  `aria-haspopup="menu"`, `aria-expanded` and `aria-controls`; the popup has
  `role="menu"` and `menuitem` children. Enter, Space or Down opens on the first
  item; Up opens on the last. Arrows, Home, End and type-ahead move through items.
  Enter activates; Escape closes and returns to the button; Tab closes and leaves
  the menu. The popup must not create a second page Tab stop. Table-cell arrows
  retain table navigation; the menu-button keys apply to the button, and popup
  keys apply only while the menu owns focus.
- Keep disabled actions present and reachable with `aria-disabled`, and associate
  their reason through `aria-describedby`. Activation also announces the reason
  without performing the action. Do not silently omit or skip a disabled menu item.
- Use `createEditorDialog` for add and edit. Remove inline hidden record forms as
  their screens migrate. Preserve validation, pending-save guards, dirty-draft
  confirmation and direct focus restoration through the shared dialog guard.
- Route delete/remove actions through `collection-delete.js` and `confirmAction`,
  unless the screen offers Undo. Clockwheel step deletion keeps Undo and gains no
  confirmation. This rule applies to menu actions and keyboard accelerators alike.
- Render an empty collection as one row spanning every column, using
  `.no-results`. Use one `.collection-table` CSS class instead of per-ID table
  styles for migrated collections. Both themes need visible focus and targets of
  at least 24 by 24 CSS pixels; preserve forced-colors and narrow-screen access.
- Keep keyed rows and controls in place during updates. Reuse the focus-preserving
  row placement from `dom-helpers`; move a removed row's focus to an adjacent row
  or a named panel fallback. Announce the list change and new focus destination
  through `announceStatus`. All other transient announcements also use that helper.
  Quiet updates must not repeatedly announce an unchanged collection.
- Read-only data tables, including history, hour preview, ad report and listener
  statistics, adopt the shared class and navigation but need no action menu.

The cart wall retains its button wall. Text-only lists remain plain lists:
why-this-track explanations, lyrics, landmarks, player-page history, rotation-test
results, update commands, relay status and hour-preview notes.

## Initial migration and remaining work

Phase 1 establishes the shared components and migrates Schedule events, Upcoming
and Runs, plus saved Clockwheels and their steps. Schedule events expose Name,
When, Action, Next run and Enabled columns, primary Edit, and Run now,
Enable/Disable and Delete in Options. Duplicate is included only if already
supported. Upcoming and Runs are read-only tables.

Saved Clockwheels expose Name, Active and Steps, primary Open, and Duplicate,
Rename, Activate/Turn off and Delete in Options. Export appears only if already
supported. Actions target the named saved wheel rather than whichever wheel is
typed or open. Create and PAL import remain panel-level actions. Steps expose
their number, short summary, kind and length/target data, primary Edit, and Move
up, Move down, Duplicate and Delete in Options. Alt+Up/Down, Delete and Ctrl+Z
remain accelerators on the focused step; step editing retains its shared dialog.

Later batches migrate shows, songs, editions/takes, queue, listener requests and
voice-track bookings first; categories, intro rules, matched intros, ads, liners,
profiles and devices next; then administrative collections, small lists, library
search and remaining read-only tables. Existing unmigrated screens are migration
gaps, not alternative patterns sanctioned for new work.

### Clarification 2026-10-31: unsaved wheels and destructive transitions

Create opens the shared name editor. Its new working copy appears in the same
clockwheel table marked **Not saved** until Save clockwheel succeeds. Its
Options menu includes Delete, which asks before discarding the unsaved wheel;
this does not send a deletion request for a file that does not exist.
Duplicate targets the selected saved row and opens the copy. If opening it would
replace unsaved work, the shared confirmation names the currently open wheel
and asks before discarding that work. Declining preserves the draft. Rename and
Duplicate obtain their new names through the shared editor; the panel's
**Imported clockwheel name** field belongs only to PAL import.

## Alternatives and consequences

Keeping feature-specific list and grid implementations would preserve current
markup but retain inconsistent keys, focus recovery and repeated maintenance.
Using tables with every action visible would expose data columns but keep dense
rows of controls. A component framework would add a delivery and dependency
change without replacing accessibility verification. Shared native modules reuse
the existing tested keyboard and dialog decisions with a smaller runtime change.

Menus add one step to infrequent actions. The primary action and retained
accelerators keep frequent work direct. Shared components also broaden the effect
of a regression: test their contracts and migrated callers together. Revisit if
screen-reader evidence reveals a conflict between menu focus and table navigation,
or a collection cannot express its data and actions without losing necessary
workflow behavior. Preserve the shared pattern while resolving such cases.

## Implementation and verification

- [Collection table](../../src/autodj/static/modules/collection-table.js),
  [row-action menu](../../src/autodj/static/modules/row-actions-menu.js) and
  [removal helper](../../src/autodj/static/modules/collection-delete.js).
- [Table rows](../../src/autodj/static/modules/table-row.js),
  [table navigation](../../src/autodj/static/modules/table-navigation.js),
  [DOM helpers](../../src/autodj/static/modules/dom-helpers.js),
  [editor dialogs](../../src/autodj/static/modules/editor-dialog.js) and
  [shared styles](../../src/autodj/static/app.css).
- [Schedule panel](../../src/autodj/static/modules/scheduler-panel.js) and
  [Clockwheel panel](../../src/autodj/static/modules/clockwheel-panel.js).
- [Accessibility procedures](../accessibility-testing.md#collection-tables),
  [keyboard guide](../guides/keyboard-and-screen-reader.md) and
  [selected checks](../../CONTRIBUTING.md#choose-checks-for-the-change).

ADR disposition: `create_new_adr`. This establishes the common collection
architecture; it does not change ADR 11 or 17's acceptance status. Run structural
documentation checks separately from Vitest, browser keyboard/axe audits and
screen-reader sampling. Test selectors below identify evidence to execute, not
claims that it passed.

## Current contracts

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0021-01` | Shared tables retain keyed rows and focus during data updates. | automated | `tests/jsmodules/collection-table.test.js::preserves keyed rows and focus while updating data` |
| `ADR-0021-02` | Empty collections contain one no-results row spanning every column. | automated | `tests/jsmodules/collection-table.test.js::renders one empty row across all columns` |
| `ADR-0021-03` | Removing a focused row selects a usable fallback and announces its destination through the shared status helper. | automated | `tests/jsmodules/collection-table.test.js::moves focus after removal and announces the new target` |
| `ADR-0021-04` | Menus implement the menu-button keys, retain disabled choices and explain disabled activation. | automated | `tests/jsmodules/row-actions-menu.test.js::supports menu keys and disabled reasons` |
| `ADR-0021-05` | Removal requires confirmation unless Undo is available. | automated | `tests/jsmodules/collection-delete.test.js::confirms removal unless undo is available` |
| `ADR-0021-06` | Schedule events use data columns and one primary action in the shared table. | automated | `tests/jsmodules/scheduler-panel.test.js::uses shared collection tables with event columns and one primary action` |
| `ADR-0021-07` | Table/menu/dialog interaction and visual accessibility require browser and speech evidence. | manual | Follow docs/accessibility-testing.md Collection tables in both themes; record revision, browser, screen reader, keyboard focus, speech method, axe results and remaining limits. Include one Tab stop, menu open/navigation/activation/close, disabled reasons, editor return, refresh/removal focus and clockwheel Undo. |
| `ADR-0021-08` | All non-exempt collection screens converge on this pattern. | gap | Phase 1 covers Schedule and Clockwheels. The remaining migration batches above are not yet verified against the shared contract. |
| `ADR-0021-09` | Saved Clockwheels and their steps use shared data columns and row menus without row headers. | automated | `tests/jsmodules/clockwheel-panel.test.js::uses shared collection columns and row menus without row headers` |
