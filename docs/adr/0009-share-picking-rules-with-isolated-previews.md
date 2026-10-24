# 9. Share picking rules with isolated previews and explicit scheduling semantics

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

Music similarity is only one input to station playout. Repeat rules,
clockwheels, categories, requests, scheduled shows, announcer breaks, ads, and
time checks interact at song changes. Operators need useful forecasts without
changing the station. This record covers that behavioral boundary, not a claim
that every feature is simulated or that any compliance mode guarantees legality.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Route simulation through the station's picker with copied mutable state.
Distinguish committed/fixed audio from predicted choices and use the existing
placement and scheduling rules for inserted content.

- Rotation simulation owns its configuration, clock, play history, last-played
  state, and random generator; it must not write library state or steer live picks.
- Hour previews distinguish fixed from predicted entries and discard/recompute
  cached forecasts when relevant inputs change. They are estimates, not promises
  that future operator edits or requests cannot change the output.
- Rotation simulation has documented omissions (including profile switches and
  some inserted audio). Preview and live handling of hard events are not a reason
  to claim the rotation tester cuts audio identically.
- Repeat-rule relaxation, queue/show precedence, DMCA rechecks, and schedule
  wait/cut behavior remain explicit in their owning modules and tests.
- Announcer breaks and ads use the open song-change placement model; external
  generation stays outside the live audio callback. Time checks prefer enabled
  voice packs, recorded minute clips, then configured TTS, with bounded synthesis.
- Timing calculations distinguish source audio time from rendered wall time,
  including speed changes, cues, overlaps, and inserted speech.

## Alternatives for review

- Reuse the picker on isolated state: fewer divergent rule implementations,
  but snapshot completeness and timing approximations need verification.
- Implement a separate forecasting picker: easier to simplify independently,
  but can disagree with live eligibility and fallback behavior.
- Use the live picker directly for previews: closest access to current state,
  but consuming RNG, cursors, or history changes what actually airs.

## Consequences and revisit conditions

Rule changes must consider live picks, previews, simulations, and show
generation. Reports must expose omissions, limits, skips, and estimates. Revisit
for exact offline reproduction or an automatic content-producer handoff;
neither exists merely because an integration can upload an announcer clip.

## Implementation and operational references

- [Picker](../../src/autodj/player.py), [repeat rules](../../src/autodj/repeat_rules.py),
  [clockwheel](../../src/autodj/clockwheel.py), and [scheduler](../../src/autodj/scheduler.py).
- [Simulation](../../src/autodj/rotation_sim.py), [preview](../../src/autodj/hour_preview.py),
  [playout timing](../../src/autodj/playout.py), and [shows](../../src/autodj/shows.py).
- [Breaks](../../src/autodj/breaks.py), [ads](../../src/autodj/ads.py),
  [time checks](../../src/autodj/time_checks.py), and [listener requests](../../src/autodj/listener_requests.py).
- [Selection guide](../track-selection.md), [rotation testing](../operations.md#testing-the-rotation),
  [preview](../operations.md#hour-preview-and-backtiming), and [schedule](../operations.md#scheduler).

This documentation pass adds no runtime dependency, configuration switch, or
migration. Any future change to this boundary needs its own affected-path and
compatibility assessment.

## Current contracts

The separate [programme review proposal](0014-isolate-programme-review-and-export.md)
adds frozen shortened audio and exports using shared selection/placement. It does
not turn the Hour preview forecast into an actual-broadcast promise. This
retrospective record remains Proposed.

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0009-01` | Rotation simulation preserves live player/settings/history and performs no catalog or ledger writes. | automated | `tests/unit/test_rotation_sim.py::test_nothing_on_air_changes` |
| `ADR-0009-02` | Preview generation preserves live state, global random state, and database write state. | automated | `tests/unit/test_hour_preview.py::test_a_preview_changes_nothing_on_the_air` |
| `ADR-0009-03` | Forecast cache invalidation includes picking, handoff, schedule, and library changes. | automated | `tests/unit/test_hour_preview.py::test_cache_freshness_covers_handoff_rules_schedule_library_and_refresh` |
| `ADR-0009-04` | Time checks fall back from voice packs to recorded clips to configured synthesis. | automated | `tests/unit/test_time_checks.py::test_voice_pack_speaks_first_and_falls_back_to_clips_then_tts` |
| `ADR-0009-05` | Forecast accuracy and omitted simulation features require explicit operational interpretation. | manual | Compare predictions with observed station output and document simulation omissions; forecasts are not immutable playout promises. |
| `ADR-0009-06` | The announcer context reports the programming the live station is using (the clockwheel picking, a show on the air and the pick mode), read from the live player and show runner rather than from a copy of the schedule. | automated | `tests/unit/test_breaks_api.py::test_context_says_which_programming_is_on_the_air`; `tests/unit/test_shows.py::test_a_stop_that_cannot_take_the_songs_out_keeps_trying` |
| `ADR-0009-07` | A relative date condition (a Beets date within the last N days) saves the number of days, not a date, and is judged against the time of the pick; cached step and category results are worked out again when its hourly window moves, not for every pick. | automated | `tests/unit/test_clock_conditions.py::test_within_days_matches_beets_dates_in_the_last_n_days`; `tests/unit/test_clock_conditions.py::test_within_days_moves_with_the_clock`; `tests/unit/test_clock_condition_picks.py::test_relative_date_steps_are_worked_out_again_each_hour`; `tests/unit/test_categories.py::test_a_new_music_category_follows_the_clock`; `tests/unit/test_clock_category_player.py::test_a_relative_date_step_is_judged_for_when_the_song_starts`; `tests/jsmodules/condition-editor.test.js::offers within the last N days for a Beets date and reads a whole number of days` |
| `ADR-0009-08` | The station name on the air is the show on the air's **Station name while active**, else that of the clockwheel picking the songs, else the saved normal name; a blank field leaves the level below. It follows activation by hand or by schedule, pick-mode changes and the end of a show, reaches the now-playing feed, status page and relays, survives a restart through the saved wheel and show, never rewrites the saved normal name, and a name-only edit neither restarts the wheel nor waits for its restart. | automated | `tests/unit/test_station_name.py::test_a_clockwheel_saves_its_station_name_only_when_it_has_one`; `tests/unit/test_station_name.py::test_a_show_saves_its_station_name_and_names_the_field_it_refuses`; `tests/unit/test_station_name.py::test_activating_a_clockwheel_by_hand_names_the_station`; `tests/unit/test_station_name.py::test_the_schedule_switching_clockwheels_names_the_station`; `tests/unit/test_station_name.py::test_saving_only_the_name_of_the_wheel_on_the_air_keeps_its_place`; `tests/unit/test_station_name.py::test_a_show_on_the_air_names_the_station_before_the_clockwheel`; `tests/unit/test_station_name.py::test_after_a_restart_the_name_matches_what_is_on_the_air`; `tests/unit/test_prepared_playout.py::test_a_prepared_edition_names_the_station_as_its_show_does`; `tests/unit/test_clock_config_api.py::test_a_clockwheel_keeps_the_station_name_the_editor_sends`; `tests/integration/test_shows_api.py::test_a_show_keeps_the_station_name_the_form_sends`; `tests/integration/test_station_name_api.py::test_the_name_on_the_air_reaches_the_feed_and_status_page_but_not_settings`; `tests/integration/test_stations.py::test_relays_connect_with_the_name_on_the_air_and_reconnect_when_it_changes`; `tests/jsmodules/station-name-field.test.js::allows a blank name, trims it and refuses what the server refuses`; `tests/jsmodules/clockwheel-panel.test.js::saves the station name while active and puts a refusal on the field`; `tests/jsmodules/shows-panel.test.js::saves a show's station name while active and puts a refusal on the field` |

## Verification

Run the following checks for affected changes and record results and platform
limits in the relevant PR. Test links do not establish execution or change this
record's Proposed status.

- [ ] Verify live-state isolation and repeatability using `test_nothing_on_air_changes`
  and `test_the_same_seed_gives_the_same_run` in [simulation tests](../../tests/unit/test_rotation_sim.py).
- [ ] Verify `test_a_preview_changes_nothing_on_the_air`, cache invalidation,
  fixed/predicted boundaries, and source/wall timing in
  [preview tests](../../tests/unit/test_hour_preview.py).
- [ ] Verify scheduled shows, DMCA checks, and inserted-content timing with
  [show tests](../../tests/unit/test_shows.py), [DMCA tests](../../tests/unit/test_dmca.py),
  and [playout timing tests](../../tests/unit/test_playout_timing.py).
- [ ] Verify bounded/fallback speech sources with [time-check tests](../../tests/unit/test_time_checks.py).

## Evidence gaps

Forecast accuracy on an actual station needs observed playback evidence.
The simulation's documented omissions are intentional limits, not complete
end-to-end parity. Historical alternatives and policy acceptance remain pending.

## Related proposal — 2026-10-17

Prepared editions bind local speech to stable song occurrences and consume the isolated programme review engine. Their identity and approval proposal is [ADR 15](0015-prepare-speech-for-specific-show-editions.md). This cross-reference does not accept that proposal or change this record's status.

## Amendment 2026-10-24: Programming in the announcer context

Issue [#469](https://github.com/blindndangerous/AutoDJ/issues/469) adds a `programming` object
to the announcer context: the clockwheel picking the songs, the show on the air and the pick mode.
An announcer that matches its tone to the show reads it from the station instead of keeping its
own copy of the schedule, which drifts when hours change, a show runs long or an operator
switches a clockwheel by hand. The fields are added; no existing field changes, and the
announcer token needs no new scope. The context does not forecast the next scheduled change.
`ADR-0009-06` maps the check. This amendment does not change this record's Proposed status.

## Amendment 2026-10-24: Relative date conditions

Issue [#471](https://github.com/blindndangerous/AutoDJ/issues/471) adds the `within_days`
operator for Beets fields, such as `added` and `mtime`, in clockwheel steps, categories and intro
rules. A condition saves the number of days (1 to 3650), never a date worked out at save time, so
what it matches moves with the clock. Its window ends at the start of the current hour, so
cached step and category results stay valid for an hour and picks keep looking paths up rather
than scanning the library. Clockwheel steps judge it at the pick's expected start time, the same
time as step hours, so the rotation tester and Hour preview move it with their own clocks.
Categories judge it at the current time, also in those previews; that is a documented
simulation omission. AutoDJ's own library has no date added, so the condition reads Beets values
through the existing Beets condition path. `ADR-0009-07` maps the checks. This amendment does not
change this record's Proposed status.

## Amendment 2026-10-24: Station name while active

Issue [#472](https://github.com/blindndangerous/AutoDJ/issues/472) adds an optional
**Station name while active** to each clockwheel and each show, so a station can rename its
stream while a late show or a seasonal wheel is on the air without changing the name by hand
twice. The name on the air is the show's when a show is on the air and has one, else the
clockwheel's while the station picks by clockwheel and that wheel can play, else the saved
`[stream] station_name`. A blank field leaves the level below it, so switching to a wheel without
a name, changing the pick mode or ending a show restores the normal name with no second event;
existing shows, which have no name, change nothing. A prepared edition uses the name of the show it
was made from. A per-wheel field was chosen over a "Station name" schedule action because an action
needs a pair of events and does not follow an operator's own switch.

The name on the air is held on the station's player bridge, apart from the saved name: the settings
store, Settings, Stream and the station switcher keep the operator's name. The station checks the
programming once a second on its schedule tick (and once at start, before relays connect) and, on a
change, refreshes the now-playing feed and reconnects enabled relays, as saving the station name does;
new listener connections receive it as `icy-name`. A clockwheel's name is not part of how it picks:
saving only the name applies at once and neither restarts the wheel nor waits for its next restart.
A blank name is not written to the clockwheel or show file, so files without one stay readable by an
older AutoDJ. The announcer context's programming (`ADR-0009-06`) uses the same definition of the
wheel on the air. `ADR-0009-08` maps the checks. This amendment does not change this record's
Proposed status.
