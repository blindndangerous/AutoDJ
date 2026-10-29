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
| `ADR-0009-08` | The station name on the air is the show on the air's **Station name while active**, else that of the clockwheel picking the songs, else the saved normal name; a blank field leaves the level below. It follows activation by hand or by schedule, pick-mode changes and the end of a show, reaches the now-playing feed, status page and relays, survives a restart through the saved wheel and show, coalesces changes close together into one relay reconnect, costs an unchanged check no file read, never rewrites the saved normal name, and a name-only edit neither restarts the wheel nor waits for its restart. | automated | `tests/unit/test_station_name.py::test_a_clockwheel_saves_its_station_name_only_when_it_has_one`; `tests/unit/test_station_name.py::test_a_show_saves_its_station_name_and_names_the_field_it_refuses`; `tests/unit/test_station_name.py::test_activating_a_clockwheel_by_hand_names_the_station`; `tests/unit/test_station_name.py::test_the_schedule_switching_clockwheels_names_the_station`; `tests/unit/test_station_name.py::test_saving_only_the_name_of_the_wheel_on_the_air_keeps_its_place`; `tests/unit/test_station_name.py::test_a_show_on_the_air_names_the_station_before_the_clockwheel`; `tests/unit/test_station_name.py::test_an_unchanged_check_reads_no_file`; `tests/unit/test_station_name.py::test_after_a_restart_the_name_matches_what_is_on_the_air`; `tests/unit/test_prepared_playout.py::test_a_prepared_edition_names_the_station_as_its_show_does`; `tests/unit/test_clock_config_api.py::test_a_clockwheel_keeps_the_station_name_the_editor_sends`; `tests/integration/test_shows_api.py::test_a_show_keeps_the_station_name_the_form_sends`; `tests/integration/test_station_name_api.py::test_the_name_on_the_air_reaches_the_feed_and_status_page_but_not_settings`; `tests/integration/test_stations.py::test_relays_connect_with_the_name_on_the_air_and_reconnect_when_it_changes`; `tests/jsmodules/station-name-field.test.js::allows a blank name, trims it and refuses what the server refuses`; `tests/jsmodules/clockwheel-panel.test.js::saves the station name while active and puts a refusal on the field`; `tests/jsmodules/clockwheel-panel.test.js::says the restart note only when the steps or restart minutes changed`; `tests/jsmodules/shows-panel.test.js::saves a show's station name while active and puts a refusal on the field` |
| `ADR-0009-09` | A category season or a schedule event's date range may repeat every year (month and day only, wrapping past New Year, 29 February read as 28 February in a common year), and a category season every week (the first and last dates' weekdays, from the first date on); a range that does not repeat keeps its fixed dates, `dates_repeat` is written only when the dates repeat, and the next run of a yearly event is next year's once this year's has passed. | automated | `tests/unit/test_date_repeat.py::test_a_yearly_range_runs_past_new_year_in_every_year`; `tests/unit/test_date_repeat.py::test_29_february_is_28_february_in_a_common_year`; `tests/unit/test_date_repeat.py::test_a_weekly_window_runs_from_its_first_date_and_may_wrap_past_sunday`; `tests/unit/test_date_repeat.py::test_a_weekly_range_with_one_date_is_that_weekday`; `tests/unit/test_date_repeat.py::test_what_is_wrong_with_a_range`; `tests/unit/test_scheduler_dates.py::test_a_yearly_range_runs_every_year_past_new_year`; `tests/unit/test_scheduler_dates.py::test_a_yearly_29_february_runs_on_28_february_in_common_years`; `tests/unit/test_scheduler_dates.py::test_next_events_show_next_years_run_once_this_years_has_passed`; `tests/unit/test_scheduler_dates.py::test_a_one_time_event_in_a_yearly_range`; `tests/unit/test_scheduler_dates.py::test_bad_yearly_dates_name_the_field`; `tests/unit/test_scheduler_dates.py::test_yearly_dates_are_saved_only_when_set_and_old_files_load`; `tests/unit/test_category_dates_step_hours.py::test_a_weekly_season_keeps_its_weekdays_from_its_first_date`; `tests/unit/test_category_dates_step_hours.py::test_a_yearly_season_runs_past_new_year_and_keeps_29_february`; `tests/unit/test_category_dates_step_hours.py::test_repeating_seasons_in_words`; `tests/unit/test_category_dates_step_hours.py::test_a_repeating_season_that_cannot_be_saved_names_its_field`; `tests/unit/test_category_dates_step_hours.py::test_a_repeating_season_is_saved_only_when_set`; `tests/jsmodules/categories-panel.test.js::repeats a season every week or every year, with help the date fields read too`; `tests/jsmodules/scheduler-panel.test.js::repeats an event's dates every year and shows next year's run in Next events` |
| `ADR-0009-10` | A category build matches each Beets query against whole columns of the library, with the same songs and first-release fallback counts as matching each song. A new library snapshot works out only the songs added, changed or removed (a full build beyond 2% of the library, or when the categories, folders, Beets or a playlist file changed, or a result had an error); playlist categories are read again whenever songs changed, and on the hour only categories with relative dates are worked out again. Beets is read again only when its database changes; when songs are only added or removed, the values already read are counted against the new songs, unless their paths were matched from a guessed folder or no song would match. | automated | `tests/unit/test_categories.py::test_matching_whole_columns_gives_the_same_songs_as_matching_each_song`; `tests/unit/test_categories.py::test_a_changed_song_updates_the_categories_as_a_full_rebuild_would`; `tests/unit/test_categories.py::test_on_the_hour_only_categories_with_relative_dates_are_worked_out_again`; `tests/unit/test_categories.py::test_the_clockwheel_and_every_station_read_beets_once` |
| `ADR-0009-11` | Random and LRP forecast picks retain eligible preferences for the same clockwheel occurrence, including a step temporarily omitted at a restart; later laps have separate choices and preview state remains isolated. | automated | `tests/unit/test_clock_preview_refresh.py::test_a_step_returns_with_its_song_after_the_hour_restart_omits_it`; `tests/unit/test_clock_preview_refresh.py::test_fixed_prefix_advancement_keeps_the_next_lap_distinct`; `tests/unit/test_clock_preview_memory.py::test_cycles_without_restarts_have_distinct_choices_and_follow_fixed_advancement` |
| `ADR-0009-12` | A dry run starts from the clockwheel conditions, track keys, song versions and eligible songs the station's player has worked out, as copies checked against its own inputs; it normalizes its history once and keeps it as it plays, and picks the same songs as one that worked everything out itself. | automated | `tests/unit/test_hour_preview.py::test_a_preview_starts_from_what_the_station_worked_out`; `tests/unit/test_rotation_sim.py::test_a_dry_runs_kept_history_matches_one_built_again` |

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
this year's has passed. `ADR-0009-09` maps the checks. This amendment does not change this
record's Proposed status.

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
programming once a second on its schedule tick (and once at start, before relays connect). The check
reads memory only: the show on the air keeps its name, taken when it starts or resumes and again when
the show is saved, so a steady tick does no file read or parse near the mix bus (#570). On a change it
refreshes the now-playing feed at once and reconnects enabled relays, as saving the station name does,
once the name has stayed put for 1.5 seconds: changes close together, such as a clockwheel switch just
before a show at the top of the hour, cost relay listeners one gap, and a name that comes back costs
none. New listener connections receive it as `icy-name`. A clockwheel's name is not part of how it picks:
saving only the name applies at once and neither restarts the wheel nor waits for its next restart.
A blank name is not written to the clockwheel or show file, so files without one stay readable by an
older AutoDJ. The announcer context's programming (`ADR-0009-06`) uses the same definition of the
wheel on the air. `ADR-0009-08` maps the checks. This amendment does not change this record's
Proposed status.

## Amendment 2026-10-25: Working out categories quickly and only for what changed

Issue #584: on a station with 17 Beets query categories and 79,000 songs, working the categories
out took 24 seconds in the field, and any change to one song, such as a stored analysis result,
or the hour moving on for a relative date, worked every category out again for every song. Each
query was matched song by song, parsing every number and folding every text once per query and
song, and Beets was read again for each new library snapshot.

A build now turns each field the queries read into a column once, numbers parsed and text folded
as before, and matches each query's terms against whole columns; the songs and the first-release
fallback counts are those of matching each song, which a test compares on varied data. A new
library snapshot works out each category for the songs added, changed or removed only, and keeps
the rest; more than 2% of the library changed, or a change to the categories, the folders, Beets
or a playlist file, or an earlier error, still works everything out. A playlist category is read
again whenever songs changed. When the hour moves on, only categories with relative dates are
worked out again. Beets is read again only when its database or the library's playable paths
change, not when a song's tags or analysis do. The info line from ADR-0008-35 still says what
was worked out, why and how long it took.

In a benchmark at the field's shape (17 query categories, 80,000 synthetic songs, 4 cores), a
full build fell from 1.90 to 1.17 seconds, working out the categories after one changed song
from 1.92 to 0.03 seconds, and a pick after one changed song from 2.15 to 2.35 seconds to 0.09 to
0.29 seconds. These are measurements on one machine. Contract ADR-0009-10 records this; the ADR
remains Proposed.

## Amendment, 2026-10-25: removed songs no longer read Beets again (#584)

In the field, after a scan found 739 songs gone, working the categories out for those songs took
16.1 seconds on a 4-core Celeron J4125, as long as a full build. The categories were worked out
only for the changed songs, but the library-wide Beets reader of ADR-0008-35 read the whole
database again because the library's paths had changed: 4.8 million flexible-attribute rows for
78,000 items. Beets' values are keyed by the library path each Beets path names, whatever songs
the library holds, so adding or removing songs does not change them. The reader now keeps them
and only counts how many library songs match, which the library page's path check also reads.
When the paths were matched from a guessed folder, or no song matched before or would now, it
reads Beets again as before, since the guess may change. A change to the database still reads
it again. In a two-station benchmark at the field's shape, pinned to 4 efficiency cores, the
update after 739 songs were removed fell from 2.5 to 2.7 seconds to 0.3 seconds. Contract
ADR-0009-10 records this; the ADR remains Proposed.

## Amendment 2026-10-27: Retaining forecast choices across refreshes

Issue [#604](https://github.com/blindndangerous/AutoDJ/issues/604) makes random and
least-recently-played clockwheel steps use remembered forecast preferences. The shared
picker still checks eligibility and fitting; LRP keeps its age ordering. A refresh remembers
attempted steps separately from the timeline, including the candidate discarded when the
wheel restarts. If that step fits again, it can recover its eligible previous song.

Preference identity includes the wheel definition, restart boundary, lap and actual music
step. The preview advances that identity when a predicted prefix becomes fixed, counting music
rather than attached liners. If live advancement no longer matches the forecast, it starts
fresh instead of attaching an old song to an uncertain occurrence. Completed occurrences,
changed wheels and elapsed boundaries expire; each station/span retains at most twice the
forecast song limit. Dormant choices do not enter playback history or reserve live songs.

This state belongs to the isolated preview. Live selection, simulation and programme review
without remembered preferences retain their ordinary selection behavior. Contract
`ADR-0009-11` covers retention and occurrence identity; the existing isolation checks remain
applicable. Live preview testing is still required under `ADR-0009-05`. This amendment does
not change the record's Proposed status.

## Amendment 2026-10-29: A dry run starts from the station's worked-out library

On a station with 78,500 songs and a clockwheel on a low-power x86 server, the Hour preview
stopped at its 4-second time limit after two predicted songs. Each dry run made a new player,
so every cache the station's player keeps for the library started empty: the first pick of each
conditions step matched the whole library again, the first picks split again the credits of
every song they checked (with Count versions as one song, every song in the library), and every
pick normalized the whole play history again.

`_copy_player` now hands the copy what the station's player has worked out: the conditions
steps (when the copy's slots equal the station's), the track keys (when the copy splits artists
the same way), the song versions and the songs that are not silent or closed. Each is still
checked against the copy's library snapshot, slots, categories, Beets values and hour at its next
use and worked out again when they differ, as the station's own player does. The conditions and
keys caches are copies, so neither player's later results reach the other, and nothing the station
plays or draws changes. A step the station has not picked since the library last changed is
still worked out by the copy. The dry run's ledger keeps the normalized history up to date as it
plays instead of building it again for each pick; its contents and order match a mapping built
again.

In a benchmark at that shape (78,500 synthetic songs, 70% with plays, four categories and two
conditions steps, pinned to 2 cores of one laptop CPU), a 60-minute preview fell from 0.73 to
0.76 seconds to 0.31 to 0.40 seconds, and with Count versions as one song from 1.38 to 1.48
seconds to 0.40 to 0.55 seconds; the previews were the same. These are measurements on one
machine, not on the server where the limit was reached. The time limit is unchanged.
Contract `ADR-0009-12` records this; the ADR remains Proposed.
