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
- Give each row with actions one last **Actions** column holding only its
  **Options for <row name>** menu button. The menu lists every action of the row,
  the most frequent first: usually Edit or Open, Track info for the queue and
  show songs. Shift+F10, the Applications key and right-click open the same menu.
  Use the shared menu and table-cell helper. (Rewritten by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table); this bullet
  first allowed one visible primary action beside the menu.)
- Follow the WAI-ARIA APG menu-button pattern. The button has
  `aria-haspopup="menu"`, `aria-expanded` and `aria-controls`; the popup has
  `role="menu"` and `menuitem` children. Enter, Space or Down opens on the first
  item; Up opens on the last. Arrows, Home, End and type-ahead move through items.
  Enter activates; Escape closes and returns to the grid cell the menu opened from
  in a table (the button for a standalone menu); Tab closes and leaves
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
- Read-only data tables, including history, hour preview and listener
  statistics, adopt the shared class and navigation but need no action menu.
  Ad delivery reports are plain native tables with row headers, read with
  browse-mode table keys (changed by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table)).

The cart wall retains its button wall. Text-only lists remain plain lists:
why-this-track explanations, lyrics, landmarks, player-page history, rotation-test
results, update commands, relay status and hour-preview notes.

## Initial migration and remaining work

Phase 1 establishes the shared components and migrates Schedule events, Upcoming
and Runs, plus saved Clockwheels and their steps. Schedule events expose Name,
When, Action, Next run and Enabled columns, primary Edit, and Run now,
Enable/Disable and Delete in Options. Duplicate is included only if already
supported. Upcoming and Runs are read-only tables. Superseded by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table):
Next run is removed and Edit is the first Options item.

Saved Clockwheels expose Name, Active and Steps, primary Open, and Duplicate,
Rename, Activate/Turn off and Delete in Options. Export appears only if already
supported. Actions target the named saved wheel rather than whichever wheel is
typed or open. Create and PAL import remain panel-level actions. Steps expose
their number, short summary, kind and length/target data, primary Edit, and Move
up, Move down, Duplicate and Delete in Options. Alt+Up/Down, Delete and Ctrl+Z
remain accelerators on the focused step; step editing retains its shared dialog.
Superseded by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table): saved wheels show Name, Active and Actions with Open first
in Options, and steps show one numbered Step sentence and Actions with Edit first.

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
Since the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table), Track info is the first item of that menu rather than a separate
button.

Saved shows use Open list as primary and Edit/Delete in Options. Songs use Track
info and Options for ordering/removal, with Alt+Up/Down accelerators; search
results use Add and retain Add to edition in Options. Occurrences have Options
for ordering/removal. Takes use Listen and Options for Re-record, Review,
Approve, Reattach and Remove. Reattach opens a Position editor. Existing record
and import controls retain their production workflow. Superseded by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table):
Open list, Track info, Add and Listen are each the first item of their row's
Options menu, and the column sets are those the amendment lists.

Listener requests retain Song, Requester, Time and Fit data, primary Approve,
and Options for Play next and Turn down. An unavailable Approve stays present
with its reason. Recent decisions are a read-only shared table. Turn down asks
before rejection. Voice-track bookings retain their 50-row bound and expose
Cancel in Options, with a confirmation before cancellation. Unavailable actions
remain present and focusable. Empty tables use the shared spanning row.
Superseded by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table): Approve is the first Options item, and Requester and Time
share one Requested column.

### Batch 2 review clarification, 2026-10-31

The owner confirmed Track info on show songs, consistent with the queue. Shared
collection buttons and menu items use a 44 CSS pixel minimum height, including
the previously migrated Schedule and Clockwheel screens. This supersedes the
initial 24 CSS pixel minimum height; the minimum width remains 24 CSS pixels.

While a station is stopped, listener Approve and Play next use the same disabled
reason as queue mutations: "Start the station to change the queue." Turn down
remains available because it neither starts playback nor changes the live queue.
The operation checks this restriction again when retrying after a voice choice.

Pointer activation must survive the transfer of focus from a cell's button, now
the Options button, to its grid cell. That transfer must not scroll the button away before mouseup.
Direct keyboard navigation still brings the destination cell into view; native
controls scroll themselves. This preserves the existing cell focus model.

### Amendment 2026-10-31: NVDA round 1 corrections

Observed NVDA speech on the phase 1 build exposed repeated column headings,
menu return focus that left table navigation, misleading step data and name-field
wording, and an editor description that read its entire form. These corrections
preserve the accepted collection pattern and supersede the original button-return
rule and import-only name-field wording.

- Superseded by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table):
  this round dropped column headings from gridcell names. What NVDA speaks
  depends on its configuration, and #712 chose the opposite rule.
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
  context has the accessible name `<cell text>, <row name>`. Clear it on
  focusout. Updates to a focused named row must not retain stale names or data.
  The [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table)
  adds the column heading on horizontal moves and supersedes "horizontal
  movement does not add it".
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

Menus add one step to every action. Since the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table) there is no visible primary
action: the most frequent action is first in the menu, and Shift+F10, the
Applications key and the retained accelerators keep work direct. Shared components also broaden the effect
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

## Batch 3 implementation and shared page helpers, 2026-10-31

Categories and intro rules retain their shared editors and move to data-column
collections with primary Edit and Delete in Options. Matched song and artist
intros use primary Listen, Remove in Options, and Add intro opens the shared
editor on Audio file. Selection checkboxes remain native form controls.

Spots and ad breaks use primary Edit; Options contains Delete, plus Run now for
breaks. Existing booking restrictions and scheduling remain authoritative. Ad
report totals and spot entries use read-only shared tables, with native column
headers and one cell Tab stop each; the old focusable report wrappers are removed.

Liners use primary Play dates and Options for Track info and Delete. Profiles
use primary Activate, with Reset and Delete in Options. Devices use primary
Rename in the shared editor; Options contains Revoke access, or Forget for
trusted-proxy browser registrations. Activation, reset, date selection and access
revocation keep their existing confirmation and session behavior.

Superseded by the [#712 amendment](#amendment-2026-10-31-712-review-of-the-shared-table): each primary action named in this section (Edit, Listen,
Play dates, Activate, Rename) is now the first item of its row's Options menu,
the column sets are those the amendment lists, and the ad report tables are
plain native tables again.

The companion consolidation reuses field errors, busy presentation, announcements,
failure text, standalone settings saves and card-open refresh throughout the web
app. CONTRIBUTING specifies each helper. ADR 22 owns browser mapping of public
field refusals and stale request wording; ADR 11 owns speech/busy presentation;
ADR 17 retains the boundary between settings drafts, standalone saves and record
editors. No endpoint, storage ownership or dependency changes are introduced.

## Amendment 2026-10-31: #712 review of the shared table

Issue #712 (jage9) reports that the shared table made navigation harder: NVDA
stopped announcing column headings while moving between cells, the second such
regression, and the refactor added columns, notably in Clockwheels. It keeps the
row menu, asks for keyboard and pointer context-menu access and limits further
migration until the operator has reviewed the result. The owner follows these
design decisions. This amendment supersedes the earlier primary-action, column
and heading rules where they conflict.

1. **Cell names.** NVDA says no table headings in focus mode, so jage9's rule
   from commit `ccba1e0f` returns. Until focus leaves, the cell an arrow key
   moves to is named by its text, then the column heading when the column
   changed, then the row's name cell (`[data-name]`) when the row changed. A
   cell holding a button, input or select gets no label. The menu, focus-redirect,
   scrolling, `select` and label-ordering improvements made since remain.
   Speech differs by NVDA configuration, notably the Document Formatting table
   headers setting; the round 1 sample heard headings twice under another
   setting. #712 chose this rule. Record that NVDA setting with every future
   speech sample.
2. **One action column.** A row with actions ends in one column, headed
   **Actions**, holding only its **Options for <row name>** button. The menu holds
   every action of the row, the former primary action first. Disabled reasons,
   the delete rule and the menu keys are unchanged. Read-only tables have no
   action column. The `primary` builder option is removed. A menu item cannot
   be pressed, so matched-intro **Listen** reads **Stop listening** while its clip
   plays. Busy feedback of a running action, such as listener **Approve**, is
   shown on the Options button.
3. **Context menu.** In every shared table, Shift+F10, the Applications key and
   right-click on any cell of a row open that row's menu, through one
   `contextmenu` listener on the table body. The browser's own menu is prevented
   only for rows with a menu. Escape, Tab and an action that leaves focus alone
   return focus to the cell the menu opened from: the focused cell for the
   keyboard, the clicked cell for the pointer, the Actions cell from the button.
   An action that opens an editor or confirmation takes focus straight from the
   menu, with no intermediate cell focus; cancelling returns to the Actions cell.
   No keydown fallback is added. Automated Chromium input fires `contextmenu` for
   both keys; automated Firefox key input does not reach the widget layer that
   produces it, so the browser audit dispatches the event there and records that.
4. **Fewer columns.** A column must be data an operator scans or compares down
   the table. Secondary facts fold back into one cell sentence. No column may
   repeat another or mostly read "None", "Not applicable" or "Not specified".
   This pilot applies the principle to Schedule and Clockwheels only:
   - Schedule events: Name, When, Action, Enabled, Actions. Next run repeated
     Upcoming and is removed; an ended event's Enabled cell says "No, ended, no
     runs left".
   - Upcoming: Name, Due, Action. Due folds the former When (date and time) and
     Due (how soon) into one cell, as the list read before #707.
   - Recent runs: Name, When, Action, Result. Its When is the time the event ran,
     not the repeat rule, so it stays.
   - Saved clockwheels: Name, Active, Actions. The Steps count, and the
     background reads of every saved wheel that filled it, are removed. The open
     wheel's name cell has `aria-current="true"`, replacing the mark on its Open
     button.
   - Clockwheel steps: Step, Actions. The Step cell is the step's number and its
     full sentence from the step describer, such as "3. music from Rock, least
     recently played, only on Friday from 08:00 until 09:00".

After the operator reviewed the pilot, a second pass applied the principle to the
other screens. A **Details** cell is one sentence per fact that is set, so it
never reads None:
   - Shows: Name, Songs, Length, Details (source and target, then missing songs,
     station name while active and schedule events when set), Actions. Show songs:
     Song ("N. title" with the not-in-library and repeated-song warnings, as before
     #707), Length, Actions. Search results: Song, Length, Actions.
   - Prepared occurrences: Item ("N. label"), Actions; Position and Kind are
     removed, and a segment without a label is named "Segment N". Takes are
     unchanged: Name, Placement, Status, Actions.
   - Queue: Track ("N. track", then ", bridge to ..." as before #707), Actions.
   - Listener requests: Song, Requested ("by <name> at <time>"), Fit, Actions.
     Recent decisions: Song, Requested, Decision (the decision, then its reason).
   - Voice-track bookings: Booking, State, Actions. The state words already name
     the state, so the State cell is that sentence alone rather than the raw state
     followed by the same words.
   - Categories: Name, Songs, Details (source, hours when set, outside-now note,
     and the clockwheels that use it), Actions. Intro rules: Name, Conditions,
     Details (clips, playback, dates when set), Actions. Matched intros: Name,
     Details (playback, dates when set, a skipped-now note, speech length), Actions.
     Liners: Name, Details (play dates, a skipped-now note), Actions.
   - Ad spots: Title, Advertiser, Status, Details (campaign, seconds, competing
     category, flight dates and limits when set, aired or booked today), Actions.
     Ad breaks: Name, When, Next run, Details (spot and seconds limits, airs or
     turned off), Actions.
   - The ad report's Totals and Every spot return to the plain native tables used
     before Batch 3: a row header in the first column, no grid arrow navigation
     and a focusable scrolling region, read with browse-mode table keys.
   - Profiles and browser access keep their columns.

Batch 4 migration remains on hold until the operator reviews this result. Automated Vitest and browser audits cover these contracts; they do not
establish NVDA speech or operator acceptance, which #712 requires before the
regression is resolved. ADR disposition: `update_existing_adr`.

## Current contracts

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0021-01` | Shared tables retain keyed rows and focus during data updates. | automated | `tests/jsmodules/collection-table.test.js::preserves keyed rows and focus while updating data` |
| `ADR-0021-02` | Newly created and empty collections contain one no-results row spanning every column, including before the first asynchronous load. | automated | `tests/jsmodules/collection-table.test.js::renders one empty row across all columns`, `tests/jsmodules/collection-table.test.js::starts with a spanning empty row before the first asynchronous load` |
| `ADR-0021-03` | Removing a focused row selects a usable fallback and announces its destination through the shared status helper. | automated | `tests/jsmodules/collection-table.test.js::moves focus after removal and announces the new target`, `tests/jsmodules/collection-table.test.js::names a labelled fallback field when the last focused row disappears` |
| `ADR-0021-04` | Menus implement the menu-button keys, retain disabled choices and explain disabled activation. | automated | `tests/jsmodules/row-actions-menu.test.js::supports menu keys and disabled reasons` |
| `ADR-0021-05` | Removal requires confirmation unless Undo is available. | automated | `tests/jsmodules/collection-delete.test.js::confirms removal unless undo is available` |
| `ADR-0021-06` | Retired by #712: Schedule events no longer have a primary action column or a Next run column. | retired | Replaced by `ADR-0021-34` (one Actions column) and `ADR-0021-37` (Schedule columns). |
| `ADR-0021-07` | Table/menu/dialog interaction and visual accessibility require browser and speech evidence. | manual | Follow docs/accessibility-testing.md Collection tables in both themes; record revision, browser, screen reader, NVDA's Document Formatting table headers setting, keyboard focus, speech method, axe results and remaining limits. Include one Tab stop, heading and row context on arrow moves, menu open by Enter/Space, Shift+F10, the Applications key and right-click, navigation/activation/close, disabled reasons, editor return, refresh/removal focus, clockwheel Undo and collection/menu button targets at least 24 CSS pixels wide and 44 CSS pixels high. #712 also requires an operator test. |
| `ADR-0021-08` | All non-exempt collection screens converge on this pattern. | gap | Schedule, Clockwheels and Batch 2/3 collections are migrated. Batch 4 administrative/library collections remain unverified against the shared contract, and they and the #712 column pass for other screens are on hold until the operator reviews the Schedule and Clockwheels pilot. |
| `ADR-0021-09` | Saved Clockwheels and their steps use shared data columns and row menus without row headers. | automated | `tests/jsmodules/clockwheel-panel.test.js::uses shared collection columns and row menus without row headers` |
| `ADR-0021-10` | Saved shows, songs and search results share tables, one Actions menu per row and modal metadata editing. | automated | `tests/jsmodules/shows-panel.test.js::uses shared show, song and search tables with one Actions menu` |
| `ADR-0021-11` | Prepared occurrences and takes share tables while reattachment uses the modal editor and pending saves guard removal. | automated | `tests/jsmodules/prepared-editions.test.js::uses shared occurrence and take tables with dialog reattachment`, `tests/jsmodules/prepared-editions.test.js::does not remove a take while an edition save is pending` |
| `ADR-0021-12` | The queue lists Track info first in each row's menu and confirms removal through the shared helper. | automated | `tests/jsmodules/queue.test.js::uses the shared queue table with Track info and confirmed removal` |
| `ADR-0021-13` | Pending and recent listener requests share tables; unavailable approval stays focusable with its reason. | automated | `tests/jsmodules/listener-requests.test.js::uses shared pending and recent request tables with focusable unavailable approval` |
| `ADR-0021-14` | Voice-track bookings keep the listing bound, confirm cancellation from their shared menu and retain readable failure context if the row disappears. | automated | `tests/jsmodules/voice-tracker.test.js::uses a bounded shared bookings table with confirmed cancellation`, `tests/jsmodules/voice-tracker.test.js::links a failed Cancel to the heading when refresh removes its booking` |
| `ADR-0021-15` | Disabled row actions associate their current reason before activation; since #712 every row action is a menu item. | automated | `tests/jsmodules/row-actions-menu.test.js::describes a disabled choice before activation and updates or removes its reason` |
| `ADR-0021-16` | Transferring button focus to its grid cell preserves pointer activation and keyboard cell scrolling. | automated | `tests/jsmodules/table-navigation.test.js::keeps a clicked button stationary while redirecting focus to its cell` |
| `ADR-0021-17` | Retired by #712: data-cell names now add the column heading on a column change. | retired | Replaced by `ADR-0021-36`. |
| `ADR-0021-18` | Closing Options with Escape and cancelling removal restore its gridcell and immediate table navigation. | automated | `tests/jsmodules/collection-table.test.js::resumes grid arrows from the Options cell after Escape`, `tests/jsmodules/collection-delete.test.js::returns cancelled removal to the owning gridcell without focusing its button` |
| `ADR-0021-19` | Retired by #712: step targets and selection modes return to the step's sentence. | retired | Replaced by `ADR-0021-38`. |
| `ADR-0021-20` | New clockwheel name has help for Create and PAL import and prefills the editable Create dialog. | automated | `tests/jsmodules/clockwheel-panel.test.js::labels the shared new name and uses it to prefill Create` |
| `ADR-0021-21` | Editor descriptions provide short help instead of announcing the complete form. | automated | `tests/jsmodules/editor-dialog.test.js::describes editors with short help instead of their form contents` |
| `ADR-0021-22` | Loading a clockwheel preserves the operator's new-name draft, including an empty value. | automated | `tests/jsmodules/clockwheel-panel.test.js::preserves an empty or operator-entered new name when opening saved wheels` |
| `ADR-0021-23` | Retired by #712: horizontal moves now add the column heading as well. | retired | Replaced by `ADR-0021-36`. |
| `ADR-0021-24` | Action return focus resolves its owning cell without selecting a sibling native control. | automated | `tests/jsmodules/table-navigation.test.js::returns actions to their cell without selecting a sibling native control` |
| `ADR-0021-25` | Opening an editor focuses its requested field once without an intermediate field, including from a row menu. | automated | `tests/jsmodules/editor-dialog.test.js::lets native opening focus the requested field once without an intermediate field`, `tests/jsmodules/collection-table.test.js::lets a menu action that moves focus take it straight from the menu` |
| `ADR-0021-26` | The selected main tab remains the sole tab-strip Tab stop after selection and refresh. | automated | `tests/jsmodules/tabs.test.js::keeps Settings as the sole main tab stop after selection and refresh` |
| `ADR-0021-27` | Categories and intro rules use keyed collection cells and one Actions menu with Edit first and Delete. | automated | `tests/jsmodules/categories-panel.test.js::uses a named collection grid with Edit then Delete in one Actions menu` |
| `ADR-0021-28` | Matched intros use collection cells and a shared Add editor with direct initial focus. | automated | `tests/jsmodules/matched-intros.test.js::uses collection cells and opens Add intro directly on Audio file` |
| `ADR-0021-29` | Ads preserve keyed rows and Options-cell focus when deletion is cancelled. | automated | `tests/jsmodules/ads-panel.test.js::preserves keyed ad rows and focus through refresh and returns cancelled delete to Options` |
| `ADR-0021-30` | Ad reports use plain native tables with column and row headers, outside grid navigation, and one announcement (#712). | automated | `tests/jsmodules/ads-panel.test.js::shows the report as tables with headers and says its totals once` |
| `ADR-0021-31` | Liners keep keyed rows with dates and availability in one Details cell and Actions-cell return focus. | automated | `tests/jsmodules/asset-collections.test.js::keeps liner rows keyed with dates and availability in Details and Options return focus` |
| `ADR-0021-32` | Profiles expose active state separately and retain reset and deletion in Options. | automated | `tests/jsmodules/asset-collections.test.js::shows profile active state in its own cell and keeps reset and deletion in Options` |
| `ADR-0021-33` | Devices expose data columns and one menu with Rename first and revocation. | automated | `tests/jsmodules/asset-collections.test.js::shows paired device data in columns with Rename and a revocation menu` |
| `ADR-0021-34` | A row with actions has one last Actions column holding only its Options menu button; the menu lists every action, the former primary first. Read-only tables have no action column. | automated | `tests/jsmodules/collection-table.test.js::puts every row action in one Actions column menu with the former primary action first` |
| `ADR-0021-35` | Shift+F10, the Applications key and right-click open the row's menu through `contextmenu`; focus returns to the originating cell, and rows or tables without actions keep the browser menu. | automated | `tests/jsmodules/collection-table.test.js::opens a row's menu from the context menu key or right-click and returns to the originating cell`, `tests/jsmodules/collection-table.test.js::leaves the browser menu on rows and tables without actions` |
| `ADR-0021-36` | An arrow move names the destination cell by its text, the column heading when the column changed and the row's name when the row changed; control cells get no label and focusout clears it. | automated | `tests/jsmodules/table-navigation.test.js::names a moved-to cell with its text, a changed column heading and a changed row's name`, `tests/jsmodules/collection-table.test.js::adds a changed column heading to computed data names after navigation and updates`, `tests/jsmodules/collection-table.test.js::refreshes the focused row context after keyed data changes` |
| `ADR-0021-37` | Schedule tables keep only scanned columns: events Name, When, Action, Enabled and Actions; Upcoming Name, Due and Action; Recent runs Name, When, Action and Result. | automated | `tests/jsmodules/scheduler-panel.test.js::keeps only scanned Schedule columns and one Actions menu per event` |
| `ADR-0021-38` | Saved clockwheels show Name, Active and Actions; steps show Step and Actions, the Step cell being the numbered step sentence with its target, selection, rules and hours. | automated | `tests/jsmodules/clockwheel-panel.test.js::uses shared collection columns and row menus without row headers`, `tests/jsmodules/clockwheel-panel.test.js::shows each step as one numbered sentence with its target, selection and hours` |
| `ADR-0021-39` | The #712 second pass keeps only scanned columns on the other collection screens, folding secondary facts into one Details or numbered cell; profiles and browser access keep theirs. | automated | `tests/jsmodules/shows-panel.test.js::uses shared show, song and search tables with one Actions menu`, `tests/jsmodules/prepared-editions.test.js::uses shared occurrence and take tables with dialog reattachment`, `tests/jsmodules/queue.test.js::uses the shared queue table with Track info and confirmed removal`, `tests/jsmodules/listener-requests.test.js::uses shared pending and recent request tables with focusable unavailable approval`, `tests/jsmodules/voice-tracker.test.js::uses a bounded shared bookings table with confirmed cancellation`, `tests/jsmodules/categories-panel.test.js::uses a named collection grid with Edit then Delete in one Actions menu`, `tests/jsmodules/intro-rules-panel.test.js::uses a named intro-rule collection grid and returns its menu to the Options cell`, `tests/jsmodules/matched-intros.test.js::uses collection cells and opens Add intro directly on Audio file`, `tests/jsmodules/ads-panel.test.js::uses shared spot and break columns with one Actions menu and disabled run reasons`, `tests/jsmodules/asset-collections.test.js::keeps liner rows keyed with dates and availability in Details and Options return focus` |
