# 17. Save settings through one draft boundary

Date: 2026-10-23
Status: Proposed
Decision authority: operator requirements in [issue #550](https://github.com/blindndangerous/AutoDJ/issues/550); architectural record pending maintainer acceptance.

## Context and proposal

Settings combined automatic writes, feature-specific Save buttons and duplicate
update endpoints. Operators could not reliably tell when an edit took effect.
Use one draft controller and one Save/Discard bar per Settings tab. Preserve
[station ownership](0005-a-station-owns-its-playout-chain.md) and
[browser audio ownership](0003-browser-driven-playback.md).

`GET /api/settings` returns editable groups and an opaque revision for the selected
station. `PATCH /api/settings` submits that revision and changed fields grouped by
their existing validators. Named stations use the equivalent station route.
Compare revisions under the settings lock before mutation; reject stale revisions
with 409. Validate the entire submission before writing any group. Core playback
groups persist together and refresh runtime processing once.

Browser preferences, station stores, private credentials and shared server values
keep their existing owners. A tab save is not a cross-store transaction. Return
applied and failed groups, the resulting snapshot and revision; partial failures
use 207 and retain failed drafts. Never describe a partial result as total success.
If runtime application preceded a failed disk write, report that the values are
active in memory but unsaved; the previous atomic file remains usable on restart.
Bind browser replies to the submitted draft, station and authentication session.
Preserve newer edits while a save is pending and ignore stale replies.

Replace superseded configuration write endpoints rather than keeping compatibility
wrappers. Retain operational commands, GET routes and individual record CRUD.
Existing persisted formats stay usable; this proposal does not reset station data
or move secrets. Browser announcement preferences have a one-time migration
described in the [settings reference](../settings-reference.md#saving-changes-in-settings).

## Alternatives and limits

Keeping autosave would retain inconsistent operator behavior. One global storage
transaction would cross established ownership boundaries and require substantially
more recovery machinery. The chosen explicit partial result preserves those owners
while making failures visible. A revision covers more than the submitted fields,
so unrelated concurrent edits can require a reload; revisit if this becomes common.

Legacy runtime pushes have no revision. The browser retains a saved value until
its matching echo, with the existing five-second hold limit. A future versioned
runtime snapshot can remove this time-based limitation. HTTP loads and saves also
need station/session guards; a revision alone does not identify the active browser.

## Implementation and verification

Entry points: [draft controller](../../src/autodj/static/modules/settings-drafts.js),
[station routes](../../src/autodj/station_routes.py),
[settings owner](../../src/autodj/station_settings.py), and
[validators](../../src/autodj/settings_bodies.py).
Follow the [accessibility procedure](../accessibility-testing.md) for actual browser,
keyboard and screen-reader evidence. Unit tests do not establish native audio-device
behavior, screen-reader speech or filesystem power-loss durability.

## Current contracts

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0017-01` | Editing stages a draft; Save submits only changed fields. | automated | `tests/jsmodules/settings-drafts.test.js::does not write on edit and submits changed fields once on Save` |
| `ADR-0017-02` | A save reply cannot erase edits made while the request is pending. | automated | `tests/jsmodules/settings-drafts.test.js::blocks duplicate submits and preserves edits made during a request` |
| `ADR-0017-03` | Stopped stations validate batches and reject stale revisions. | automated | `tests/integration/test_stopped_station_editing.py::test_stopped_station_settings_batch_validates_and_detects_conflicts` |
| `ADR-0017-04` | Real keyboard, screen-reader and device behavior requires platform evidence. | manual | Exercise Save, Discard, invalid fields, conflicts and station switching per docs/accessibility-testing.md; report engine/device and speech limitations. |
| `ADR-0017-05` | Standalone settings saves release stale busy state without allowing an old reply to unlock or overwrite a newer save. | automated | `tests/jsmodules/standalone-settings.test.js::clears an old save immediately without letting its reply unlock or overwrite a newer save` |

## Amendment 2026-10-31: shared browser-page helpers

Explicit standalone controls with custom endpoints use `createSettingsSave` for
single-flight saves, pending presentation and session/view invalidation. Domain
validation and routes remain in each caller. This does not move those controls
into the Settings draft bar or change this ADR's Proposed status. Liner dates
already use the shared record editor; time-check dates retain their existing
standalone flow through the shared save controller.

The draft bar's Save and Discard, and standalone Save buttons, stay enabled
while a save runs even when they do not hold focus; they are busy and ignore
presses instead (#331). A refused setting is placed on its control by the
setting's name: a part's `fields` map, a control named for the setting, or the
only control of a one-setting part. A refusal no control matches is said in
the panel status with "Unsaved changes remain."
