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
