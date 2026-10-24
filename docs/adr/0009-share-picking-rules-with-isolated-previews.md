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
| `ADR-0009-08` | A category season or a schedule event's date range may repeat every year (month and day only, wrapping past New Year, 29 February read as 28 February in a common year), and a category season every week (the first and last dates' weekdays, from the first date on); a range that does not repeat keeps its fixed dates, `dates_repeat` is written only when the dates repeat, and the next run of a yearly event is next year's once this year's has passed. | automated | `tests/unit/test_date_repeat.py::test_a_yearly_range_runs_past_new_year_in_every_year`; `tests/unit/test_date_repeat.py::test_29_february_is_28_february_in_a_common_year`; `tests/unit/test_date_repeat.py::test_a_weekly_window_runs_from_its_first_date_and_may_wrap_past_sunday`; `tests/unit/test_date_repeat.py::test_a_weekly_range_with_one_date_is_that_weekday`; `tests/unit/test_date_repeat.py::test_what_is_wrong_with_a_range`; `tests/unit/test_scheduler_dates.py::test_a_yearly_range_runs_every_year_past_new_year`; `tests/unit/test_scheduler_dates.py::test_a_yearly_29_february_runs_on_28_february_in_common_years`; `tests/unit/test_scheduler_dates.py::test_next_events_show_next_years_run_once_this_years_has_passed`; `tests/unit/test_scheduler_dates.py::test_a_one_time_event_in_a_yearly_range`; `tests/unit/test_scheduler_dates.py::test_bad_yearly_dates_name_the_field`; `tests/unit/test_scheduler_dates.py::test_yearly_dates_are_saved_only_when_set_and_old_files_load`; `tests/unit/test_category_dates_step_hours.py::test_a_weekly_season_keeps_its_weekdays_from_its_first_date`; `tests/unit/test_category_dates_step_hours.py::test_a_yearly_season_runs_past_new_year_and_keeps_29_february`; `tests/unit/test_category_dates_step_hours.py::test_repeating_seasons_in_words`; `tests/unit/test_category_dates_step_hours.py::test_a_repeating_season_that_cannot_be_saved_names_its_field`; `tests/unit/test_category_dates_step_hours.py::test_a_repeating_season_is_saved_only_when_set`; `tests/jsmodules/categories-panel.test.js::repeats a season every week or every year, with help the date fields read too`; `tests/jsmodules/scheduler-panel.test.js::repeats an event's dates every year and shows next year's run in Next events` |

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

## Amendment 2026-10-24: Dates that repeat every week or every year

Issue [#470](https://github.com/blindndangerous/AutoDJ/issues/470) lets seasonal programming
repeat without editing its dates each year. The existing date fields stay, and a `dates_repeat`
choice next to them says how to read them. `"none"`, the default, keeps the fixed dates exactly
as before. `"yearly"` uses only the month and day of both dates. A range whose last day comes
before its first runs past New Year, such as 20 December to 2 January. A missing last date, or
the same month and day, is that one day. 29 February counts as 28 February in a year without one.
Categories may also use `"weekly"`: the weekdays of the first and last dates set a window that
repeats every week, from the first date on with no end, such as Friday to Sunday. The last date is
at most 6 days after the first, so the window may run past Sunday. Without a last date, it is the
first date's weekday. Schedule events offer `"none"` and `"yearly"` only, because their own
Repeat rule already chooses the days. A repeating range needs its first date. The field is
written only when the dates repeat, so files from earlier versions load unchanged and stay
readable by them. The scheduler looks up to 29 years ahead for the next run of a yearly range,
so a one-day range on a chosen weekday still finds it. Next events lists next year's run once
this year's has passed. `ADR-0009-08` maps the checks. This amendment does not change this
record's Proposed status.
