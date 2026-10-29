# 18. Profiles store overrides of station defaults

Date: 2026-10-29
Status: Proposed
Decision authority: user requirements in [issue #666](https://github.com/blindndangerous/AutoDJ/issues/666); architectural record pending maintainer acceptance.

## Context

Profiles currently serialize a limited set of snapshot values and apply them
through a separate settings path. That duplicates validation, setter ordering,
runtime refresh and persistence already owned by station settings. Snapshots
also make an omitted setting indistinguishable from a choice copied from an old
default. Saving and applying therefore need one durable meaning for inheritance,
activation and ownership.

## Proposal

A station owns its saved defaults, profile overrides and active profile
identity. A profile stores only explicit overrides of its station's saved
defaults. `Station defaults` is an editable target and an activatable selection
with no overrides. The selected active profile name, or the defaults selection,
is persisted with the station. Profiles do not own shared library or server
settings, browser preferences, credentials or machine-local devices.

Settings has one native Profile combobox, with **Station defaults** first. It
selects an edit target only; it never changes on-air settings. There are no
per-field override switches. Profile and defaults edits remain ordinary
Settings drafts: they are saved with the selected Settings tab's Save changes
control, can be discarded there, and participate in its unsaved-changes guard.
Switching targets follows the same warning. Cancelling keeps both the current
target and its draft. A scheduled or external profile application cannot retarget
an open draft.

When a draft is saved, compare its target with the station's active identity at
that time. Saving changed fields for the currently active profile applies those
fields immediately. Saving changed fields for Station defaults updates the
currently active selection immediately: inherited values update, explicit
profile overrides remain unchanged, and the active profile identity does not
change. If a schedule activates another profile while one is being edited,
saving that now-inactive profile persists it without switching playback back.
Explicit activation resolves all eligible groups from the latest saved defaults
and selected overrides.

When saving an override, a value that equals the saved station default becomes
inherited instead of being pinned. Explicit `false` and `0` are values, not
absence; supported clear/reset values remain explicit values as well. Existing
partial profiles retain every non-null saved setting as an override; omitted
settings and legacy `null` values inherit from the station's saved defaults.
Migration must not infer override values from the settings that happen to be on
air.

Manual activation uses **Activate** actions in Settings, **Station**,
**Profiles**, for a saved profile or Station defaults. The **Profile to edit**
combobox selects a draft target only; it does not activate a profile. It marks
the active profile with **(active)** and updates that marker when activation
changes. There is no activation button on the combobox. Scheduled profile
actions use the same activation behavior.
Every activation resolves the latest saved station defaults with that profile's
overrides, validates all eligible groups and referenced resources, then sends
common settings through the same validated station-settings path as an ordinary
edit. Activating Station defaults resolves all eligible groups from saved
defaults. Saving Station defaults updates inherited settings of whichever
profile is active at save time, preserves overrides and active identity, and
does not retarget an open draft.

Reset profile to defaults requires confirmation. Cancelling reset leaves both
the saved profile and the current draft intact. Keep profile deletion confirmed.
Show which profile is active. Keep Browser access in the general Server tab,
visibly separate from profile management. The Settings Profile combobox uses
native labeling and established focus/validation behavior; it adds no
profile-switch announcement or extra `aria-describedby` help.

At startup, validate station defaults and the selected profile. Persist the
active name and a revision of its resolved saved settings. When that revision
is unchanged, retain the separately persisted live station state: manual and
scheduled changes survive a normal restart without rewriting saved profiles.
If defaults or the selected profile were edited while the server was stopped,
resolve and apply the new values. Explicit Activate always reloads saved values.
Older selection files without a revision resolve once and acquire one.

Saving an active editing target applies only changed fields. Unrelated live
changes remain in place. An invalid or missing saved profile is reported in
Profiles and logged before any profile settings are applied; the station keeps
its last saved state so the operator can repair it. Failures while actually
applying validated settings retain the existing partial-failure reporting.

Explicit resource renames update saved references. Confirmed deletion clears
clockwheel references, removes deleted voice packs from saved selections, and
replaces deleted Stereo Tool preset references with the included preset.
Reference file updates use atomic writes and are restored if the resource
operation fails; this is not a power-loss-safe cross-store transaction.
Legacy backup restoration removes profile metadata that was absent from the
archive instead of combining it with restored profiles.

### Profile field inventory

The inventory is about durable station programming and playback choices. The
runtime's shared library, station infrastructure and device-specific behavior
retain their current owners.

| Profile disposition | Settings | Reason |
| --- | --- | --- |
| Include | Preset selection and preset overrides; transition mode and timing; DJ mix and harmonic choices; playback and repeat rules; BPM range; discovery rate and enablement; crossfade EQ settings; break behavior; time-check behavior; selected clockwheel; shown cart page. | These choices describe the station's programming or how its selected program is played. Referenced presets, clockwheels, cart pages, break settings and time-check resources must be validated when the profile is applied. |
| Exclude | `playback.key_notation`, `key_prefer_flats`, `show_lyrics`. | These station-owned settings control presentation and display, not the station's program or audio playback decisions. |
| Exclude | `playback.prefetch_next_track`. | This station-owned playback-performance setting must not vary with a station programming profile. |
| Exclude | Music folders, catalog/index paths, similarity/analysis models, audio-feature cache and analysis workers. | These belong to the shared library and its workers; a station profile cannot change shared catalog ownership or processing infrastructure. |
| Exclude | Station name/ID, scheduling time zone, output mode and device, upload limits, log label and station lifecycle/autostart. | These define station identity, infrastructure, machine routing or operation, rather than a programming preset. |
| Exclude | Stream destinations, relay credentials, public player/embed settings, listener access/limits, recording folder, split, retention and free-space policy. | These are station infrastructure, access and recording ownership. A profile must not redirect a broadcast, expose a public surface or change recording storage/retention. |
| Exclude | Live talkover/microphone settings and recording start, stop or split operations. | Live input and recording controls are operational infrastructure/actions, not saved program choices. Profile activation cannot start or stop these operations. |
| Exclude | Browser theme, keyboard shortcuts and announcements; browser audio output and preview device; authentication tokens, passwords and secrets. | These belong to a browser, machine, credential store or access boundary, never a station profile. |
| Exclude | Server-wide configuration, trusted proxies, shared server time display, worker pairing and shared worker settings. | These belong to the server and shared worker owners, not an individual station. |
| Exclude | Schedule definitions, clockwheel definitions, cart contents and uploaded audio, library folders, credentials, queues, history and listener state. | Profiles can select an existing clockwheel or cart page, but cannot copy or redefine those records, assets or runtime state. |

Clockwheel selection and cart-page selection remain explicit existing
operations. Profile application must validate referenced names and accurately
report failures across station settings, clockwheel storage and cart storage.
These stores are not one cross-store transaction. Invalid or unavailable
references must not be silently ignored; the consistent all-group validation
and partial-failure response behavior remains an implementation criterion.

## Dated amendment — 2026-10-29: Settings tab groups

Settings presents three always-visible, heading-labelled tab groups: Profile,
Station and Global. One selected tab across the groups controls the visible
settings panel. Profile
has four tabs: Mixing (transitions and DJ mixing), Song picking, Sound
(profile processing), and Announcements (liners, time checks, carts and breaks).
Station has Stations, Profiles, Broadcasting (stream, Live DJ and
DMCA), and Schedule. Global has Server (analysis limits, shared time
format, Stereo Tool installation and shared licence, and browser access),
Analysis workers (worker pairing, settings and machine tokens), Update and
restart, and Browser preferences (browser display, keyboard and audio
device controls). The tabs keep their server and browser owners. Station
display, lyrics, key notation and prefetch controls are in Stations.
Profiles manages profile creation, activation, reset and deletion. The selected
clockwheel is edited in Song picking beside Pick mode; the cart-page selection
is edited in Announcements under Carts. Each uses its containing tab's Save
changes control and the selected Profile to edit target.

The **Profile to edit** selector applies only to profile-aware controls. Browser
preferences remain outside profile storage; key notation, lyrics and prefetch
settings remain station-owned exclusions even though they are presented in
Stations. The Profile to edit selector follows all three tab groups and is shown
only while a Profile tab is selected. A Settings tab has one Save/Discard bar
for its draft. Record forms,
uploads, credentials, pairing and operational actions keep their existing
individual actions. This layout clarification does not change the proposal's
Proposed status or add a profile override contract.

## Ownership and related decisions

This proposal extends [station ownership in ADR 5](0005-a-station-owns-its-playout-chain.md):
defaults, overrides and active selection are per station, including for the
default station. Preserve [settings draft and save ownership in ADR 17](0017-save-settings-through-one-draft-boundary.md):
profile edits are drafts, settings stores remain separate owners, stale replies
cannot erase edits, and a profile save does not imply a cross-store transaction.
Preserve the output ownership in [ADR 4](0004-server-mix-bus-and-stream-mode.md)
and selection/scheduling boundaries in [ADR 9](0009-share-picking-rules-with-isolated-previews.md).

Implementation entry points include [profiles](../../src/autodj/profiles.py),
[station settings](../../src/autodj/station_settings.py),
[station routes](../../src/autodj/station_routes.py),
[settings bodies](../../src/autodj/settings_bodies.py), and the browser's
[settings drafts](../../src/autodj/static/modules/settings-drafts.js) and
[profile management](../../src/autodj/static/modules/profiles.js). The contract table identifies behavioral tests and remaining manual evidence;
test references alone do not establish a passing run.

## Consequences and revisit conditions

Profiles stop being independent partial-snapshot writes and become station-owned
overrides applied through shared station validation. Defaults can evolve without
silently rewriting each saved profile, and profile editing no longer doubles as
activation. The active identity and saved-settings revision distinguish a
normal restart from an offline profile edit; live settings remain in their
existing station stores.

Because activation can touch station settings and named operational resources, errors
must identify which group failed, and documentation and tests must distinguish
validation before application from failures after a separately owned store has
changed. Revisit the inventory if a new candidate field crosses the station,
server, shared-library, credential or device ownership boundaries.

## Current contracts

These are proposed acceptance constraints; they are not accepted architecture
or evidence that implementation is complete.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0018-01` | Saving profiles stores only overrides; false, zero and supported clear/reset values remain explicit, while omitted and legacy null values inherit. | automated | `tests/integration/test_profile_editing.py::test_profile_defaults_editing_apply_reset_and_station_isolation`; `tests/unit/test_profile_settings.py::test_override_diff_keeps_false_zero_and_explicit_clear`; `tests/unit/test_profile_settings.py::test_legacy_profile_keeps_only_non_null_fields_as_overrides` |
| `ADR-0018-02` | Editing a selected profile is draft-only; target changes honor the unsaved-changes warning and scheduled applies cannot retarget or overwrite the draft. | gap | The checked-in Settings browser audit covers target-switch cancellation and external activation on a running station while a profile has an unsaved draft, including the active marker. Unit/integration tests cover draft isolation and scheduled application. Triggering the combined browser scenario through a real scheduled event remains separate evidence; the external-activation scenario does not establish schedule timing. |
| `ADR-0018-03` | Manual and scheduled activation resolve current saved defaults plus overrides through shared validation; invalid harmonic groups do not partially change active playback, and the active profile can be activated again. Saving defaults updates inherited values in the active selection without changing overrides or identity. | automated | `tests/integration/test_profile_editing.py::test_profile_defaults_editing_apply_reset_and_station_isolation`; `tests/integration/test_profile_editing.py::test_invalid_harmonic_group_does_not_partially_apply_profile`; `tests/jsmodules/settings-extras.test.js::keeps Apply available for the active profile so it can be reapplied` |
| `ADR-0018-04` | Normal restarts retain persisted live changes without rewriting profiles; offline edits to defaults or the selected profile are applied. | automated | `tests/unit/test_profile_settings.py::test_restart_reads_offline_edits_to_defaults_and_selected_profile`; `tests/unit/test_profile_settings.py::test_restart_applies_saved_defaults_before_any_profile_has_been_selected`; `tests/unit/test_profile_settings.py::test_restart_preserves_live_changes_without_rewriting_saved_profiles` |
| `ADR-0018-05` | Profile overrides and active selection are isolated per station, and profile routes are classified as station-owned. | automated | `tests/integration/test_profile_editing.py::test_profile_defaults_editing_apply_reset_and_station_isolation`; `tests/integration/test_station_routes.py::test_every_route_is_classified_as_shared_or_station` |
| `ADR-0018-06` | The Profile to edit combobox and reset confirmation preserve native keyboard focus and report confirmation results accessibly. | manual | Walk through target changes, cancelled switch, reset confirmation and cancellation with keyboard and a supported screen reader; record browser, screen reader, date and actual speech. |
