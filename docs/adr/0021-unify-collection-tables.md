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
verification is complete. The initial requirements were agreed in the owner's
implementation brief.

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
  usually Edit or Open. The queue uses Track info, as clarified below. Put every other action in one
  **Options for <row name>** menu. Use the shared menu and table-cell helper.
- Follow the WAI-ARIA APG menu-button pattern. The button has
  `aria-haspopup="menu"`, `aria-expanded` and `aria-controls`; the popup has
  `role="menu"` and `menuitem` children. Enter, Space or Down opens on the first
  item; Up opens on the last. Arrows, Home, End and type-ahead move through items.
  Enter activates; Escape closes and returns to the Options grid cell in a table
  (the button for a standalone menu); Tab closes and leaves
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
  at least 24 CSS pixels wide and 44 CSS pixels high for buttons, including
  Options and menu items; preserve forced-colors and narrow-screen access.
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

Batch 2 migrates shows, songs, editions/takes, queue, listener requests and
voice-track bookings. Later batches migrate categories, intro rules, matched
intros, ads, liners, profiles and devices; then administrative collections, small lists, library
search and remaining read-only tables. Existing unmigrated screens are migration
gaps, not alternative patterns sanctioned for new work.

### Clarification 2026-10-31: unsaved wheels and destructive transitions

The name-field wording below records the initial migration; the NVDA round 1
amendment below supersedes that field's label and use.

Create opens the shared name editor. Its new working copy appears in the same
clockwheel table marked **Not saved** until Save clockwheel succeeds. Its
Options menu includes Delete, which asks before discarding the unsaved wheel;
this does not send a deletion request for a file that does not exist.
Duplicate targets the selected saved row and opens the copy. If opening it would
replace unsaved work, the shared confirmation names the currently open wheel
and asks before discarding that work. Declining preserves the draft. Rename and
Duplicate obtain their new names through the shared editor; the panel's
**Imported clockwheel name** field belongs only to PAL import.

### Batch 2 clarification, 2026-10-31

Shows, their songs and search results, prepared-edition occurrences and takes,
the queue, listener requests and voice-track bookings adopt the same components.
Show metadata and take reattachment use `createEditorDialog`. Existing occurrence
bindings, recording/review/approval rules, live voice conflicts and station/session
checks remain in their owning modules; this migration changes no route or format.

The queue uses Track info as its primary action: it is the existing direct action
for inspecting or editing a queued track without changing its playback order.
Play next is not an action on an already queued entry. Options contains Move to
top, Move up, Move down and Remove. Remove confirms through the shared helper;
Clear queue keeps its existing confirmation. Undo is not added: current queue
APIs cannot restore an exact consumed occurrence and its voice bindings safely.
This applies the approved confirmation rule without inventing a restoration API.

Saved shows use Open list as primary and Edit/Delete in Options. Songs use Track
info and Options for ordering/removal, with Alt+Up/Down accelerators; search
results use Add and retain Add to edition in Options. Occurrences have Options
for ordering/removal. Takes use Listen and Options for Re-record, Review,
Approve, Reattach and Remove. Reattach opens a Position editor. Existing record
and import controls retain their production workflow.

Listener requests retain Song, Requester, Time and Fit data, primary Approve,
and Options for Play next and Turn down. An unavailable Approve stays present
with its reason. Recent decisions are a read-only shared table. Turn down asks
before rejection. Voice-track bookings retain their 50-row bound and expose
Cancel in Options, with a confirmation before cancellation. Unavailable actions
remain present and focusable. Empty tables use the shared spanning row.

### Batch 2 review clarification, 2026-10-31

The owner confirmed Track info on show songs, consistent with the queue. Shared
collection buttons and menu items use a 44 CSS pixel minimum height, including
the previously migrated Schedule and Clockwheel screens. This supersedes the
initial 24 CSS pixel minimum height; the minimum width remains 24 CSS pixels.

While a station is stopped, listener Approve and Play next use the same disabled
reason as queue mutations: "Start the station to change the queue." Turn down
remains available because it neither starts playback nor changes the live queue.
The operation checks this restriction again when retrying after a voice choice.

Pointer activation must survive the transfer of focus from a primary button to
its grid cell. That transfer must not scroll the button away before mouseup.
Direct keyboard navigation still brings the destination cell into view; native
controls scroll themselves. This preserves the existing cell focus model.

### Amendment 2026-10-31: NVDA round 1 corrections

Observed NVDA speech on the phase 1 build exposed repeated column headings,
menu return focus that left table navigation, misleading step data and name-field
wording, and an editor description that read its entire form. These corrections
preserve the accepted collection pattern and supersede the original button-return
rule and import-only name-field wording.

- Native column headings provide column context. Do not append column labels to
  gridcell accessible names, including in native tables awaiting migration.
  Action names retain their row context.
- Escape, completed menu actions and cancelled confirmations return to the
  Options grid cell, preserving the table's single Tab stop and immediate arrow
  navigation. Editors opened from menu actions also return to that cell.
  Standalone menu buttons remain their own return target. Removed rows use the
  existing adjacent-row or panel fallback.
- Clockwheel steps have distinct **Length or target** and **Selection** columns.
  The former shows a fixed track path, preset name or ad-break spot/seconds limit.
  Music, liner and category steps say **Not specified** because the definition
  supplies no duration. Selection names their picking mode and says
  **Not applicable** for fixed tracks, presets and ad breaks.
- **New clockwheel name** has short help naming **Create** and **Import .PAL**.
  Create prefills its editable Name dialog from this field. Duplicate and Rename
  retain their own dialogs; no storage format or API changes.
- Editor descriptions preserve caller-provided short help. Otherwise the shared
  editor supplies "Use Tab to move between fields and actions." This explicit
  description prevents NVDA deriving one from the complete form. Do not point
  descriptions to the form or live status region. Individual field labels, help
  and validation associations remain available during navigation.

The prior NVDA report establishes the defects, not proof that these corrections
pass speech testing. Execute the updated
[collection-table procedure](../accessibility-testing.md#collection-tables),
and record browser automation separately from a fresh NVDA speech sample.
ADR disposition for this correction: `update_existing_adr`.

### Amendment 2026-10-31: NVDA round 2 corrections

The follow-up NVDA sample confirmed single native column headings, usable Options
cell return, separate step target/selection columns and short editor descriptions.
It also found repeated editor-opening speech, the opened wheel's name replacing
the new-name draft, and tab entry on an unselected tab. The owner's follow-up
retains the shared pattern and restores row context during vertical navigation.

- A shared editor opens with one focus transition to its requested field. Do not
  focus an intermediate field, dialog or form. Retain short help and field-level
  labels and errors. Focus-event tests and browser automation are distinct from
  a fresh speech sample for repeated dialog or form announcements.
- **New clockwheel name** belongs to the operator's next Create or PAL import.
  Opening, saving, creating, importing, duplicating or renaming a wheel preserves
  its exact text, including an empty value. Create still prefills its editable
  Name dialog; Duplicate starts empty and Rename starts with its selected row's
  name. Session clearing still removes the draft.
- The main section tab strip admits Tab only on its selected tab after every selection path.
  Preserve arrow, Home and End navigation and panel visibility behavior.
  Automated baseline/candidate comparisons did not reproduce the reported
  unselected-tab entry; the invariant is retained without a runtime tab change.
  This characterization does not resolve the outstanding NVDA observation.
- On vertical movement to a different row, a plain data cell with row-name
  context has the accessible name `<cell text>, <row name>`. Native headers
  supply column names. Name cells, headings and cells containing controls do not
  gain this extra context. Clear it on focusout; horizontal movement does not
  add it. Updates to a focused named row must not retain stale names or data.
- Returning an action button to a table resolves its owning grid cell. Retain
  native input/select focus only when that control itself is the supplied target;
  never select a sibling control merely because it shares the cell.
- A successful listener decision invalidates earlier polls so they cannot
  resurrect a decided request. Prepared-take Remove exposes its pending-save
  restriction and checks it again at confirmation; loading a new current edition
  resets stale busy state. If a booking disappears while cancellation fails,
  keep its error visible and associate it with the Voice tracks heading.

Execute the updated [accessibility procedure](../accessibility-testing.md#collection-tables)
for the candidate. Earlier speech samples describe their recorded builds; they
do not establish that these corrections pass NVDA. ADR disposition:
`update_existing_adr`; acceptance and the later migration gaps are unchanged.

## Round 3 follow-up, 2026-10-31

The supplied reviewer NVDA evidence confirms row-name context and the independent
New clockwheel name field. Repeated editor title/help/form speech occurs within
one NVDA event with one DOM focus and also on the baseline; retain the editor
behavior and document that limit in the accessibility guide.

The earlier native-Tab characterization did not cover NVDA browse-mode focus
from its buffer. The shared tab-entry correction belongs to
[ADR 11, contract ADR-0011-10](0011-verify-accessibility-with-browser-and-speech-evidence.md#amendment-2026-10-31-browse-mode-tab-entry).
Keep `ADR-0021-26` as the existing selected-tab-stop characterization; no new
tab contract is added to this collection record.

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
- [Shows](../../src/autodj/static/modules/shows-panel.js),
  [prepared editions](../../src/autodj/static/modules/prepared-editions.js),
  [queue](../../src/autodj/static/modules/queue.js),
  [listener requests](../../src/autodj/static/modules/listener-requests.js) and
  [voice-track bookings](../../src/autodj/static/modules/voice-tracker.js).
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
| `ADR-0021-07` | Table/menu/dialog interaction and visual accessibility require browser and speech evidence. | manual | Follow docs/accessibility-testing.md Collection tables in both themes; record revision, browser, screen reader, keyboard focus, speech method, axe results and remaining limits. Include one Tab stop, menu open/navigation/activation/close, disabled reasons, editor return, refresh/removal focus, clockwheel Undo and collection/menu button targets at least 24 CSS pixels wide and 44 CSS pixels high. |
| `ADR-0021-08` | All non-exempt collection screens converge on this pattern. | gap | Schedule, Clockwheels and Batch 2 collections are migrated. Administrative/library collections in later batches remain unverified against the shared contract. |
| `ADR-0021-09` | Saved Clockwheels and their steps use shared data columns and row menus without row headers. | automated | `tests/jsmodules/clockwheel-panel.test.js::uses shared collection columns and row menus without row headers` |
| `ADR-0021-10` | Saved shows, songs and search results share tables, primary actions, menus and modal metadata editing. | automated | `tests/jsmodules/shows-panel.test.js::uses shared show, song and search tables with one primary action` |
| `ADR-0021-11` | Prepared occurrences and takes share tables while reattachment uses the modal editor and pending saves guard removal. | automated | `tests/jsmodules/prepared-editions.test.js::uses shared occurrence and take tables with dialog reattachment`, `tests/jsmodules/prepared-editions.test.js::does not remove a take while an edition save is pending` |
| `ADR-0021-12` | The queue uses Track info as primary and confirms removal through the shared helper. | automated | `tests/jsmodules/queue.test.js::uses the shared queue table with Track info and confirmed removal` |
| `ADR-0021-13` | Pending and recent listener requests share tables; unavailable approval stays focusable with its reason. | automated | `tests/jsmodules/listener-requests.test.js::uses shared pending and recent request tables with focusable unavailable approval` |
| `ADR-0021-14` | Voice-track bookings keep the listing bound, confirm cancellation from their shared menu and retain readable failure context if the row disappears. | automated | `tests/jsmodules/voice-tracker.test.js::uses a bounded shared bookings table with confirmed cancellation`, `tests/jsmodules/voice-tracker.test.js::links a failed Cancel to the heading when refresh removes its booking` |
| `ADR-0021-15` | Disabled primary actions associate their current reason before activation. | automated | `tests/jsmodules/collection-table.test.js::associates the current disabled primary reason before activation` |
| `ADR-0021-16` | Transferring button focus to its grid cell preserves pointer activation and keyboard cell scrolling. | automated | `tests/jsmodules/table-navigation.test.js::keeps a clicked button stationary while redirecting focus to its cell` |
| `ADR-0021-17` | Native data-cell accessible names do not repeat their column heading after navigation or updates. | automated | `tests/jsmodules/collection-table.test.js::keeps computed data names free of native headers after navigation and updates` |
| `ADR-0021-18` | Closing Options with Escape and cancelling removal restore its gridcell and immediate table navigation. | automated | `tests/jsmodules/collection-table.test.js::resumes grid arrows from the Options cell after Escape`, `tests/jsmodules/collection-delete.test.js::returns cancelled removal to the owning gridcell without focusing its button` |
| `ADR-0021-19` | Clockwheel targets and selection modes occupy separate columns without invented durations. | automated | `tests/jsmodules/clockwheel-panel.test.js::separates step targets and selection without inventing track lengths` |
| `ADR-0021-20` | New clockwheel name has help for Create and PAL import and prefills the editable Create dialog. | automated | `tests/jsmodules/clockwheel-panel.test.js::labels the shared new name and uses it to prefill Create` |
| `ADR-0021-21` | Editor descriptions provide short help instead of announcing the complete form. | automated | `tests/jsmodules/editor-dialog.test.js::describes editors with short help instead of their form contents` |
| `ADR-0021-22` | Loading a clockwheel preserves the operator's new-name draft, including an empty value. | automated | `tests/jsmodules/clockwheel-panel.test.js::preserves an empty or operator-entered new name when opening saved wheels` |
| `ADR-0021-23` | Vertical data-cell movement adds only current row context and clears it on focusout. | automated | `tests/jsmodules/table-navigation.test.js::adds only the row name on vertical data-cell moves and clears it on focusout`, `tests/jsmodules/collection-table.test.js::refreshes the focused row context after keyed data changes` |
| `ADR-0021-24` | Action return focus resolves its owning cell without selecting a sibling native control. | automated | `tests/jsmodules/table-navigation.test.js::returns actions to their cell without selecting a sibling native control` |
| `ADR-0021-25` | Opening an editor focuses its requested field once without an intermediate field. | automated | `tests/jsmodules/editor-dialog.test.js::lets native opening focus the requested field once without an intermediate field` |
| `ADR-0021-26` | The selected main tab remains the sole tab-strip Tab stop after selection and refresh. | automated | `tests/jsmodules/tabs.test.js::keeps Settings as the sole main tab stop after selection and refresh` |
