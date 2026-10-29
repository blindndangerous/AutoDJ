# A schedule is an instruction, a preview is a forecast

A **scheduled event** tells the station to perform an action when it becomes due.
A **clockwheel** tells automatic picking what to choose at successive steps.
A **show** supplies a prepared ordered programme. These tools can work together:
a schedule can activate a wheel or start a show, but a wheel is not itself a
promise that a particular song starts at a particular minute.

## The station's clock

Schedule times use the selected station's time zone. The zone shown in
**Settings**, **Station settings**, **Schedule** is more useful than assuming the browser and station
share a clock. **Time zone**, under **Time for the selected station** in
**Settings**, **Station settings**, **Stations**, changes scheduling from the station's next start.
**Time format for all stations** changes how times look, not when events run.

Daylight-saving changes need interpretation: a skipped event time runs at the
clock jump; a repeated local time runs at its first occurrence. Date ranges
include both boundary dates, and a range set to repeat every year ignores the
year, so it can run past New Year. A repeating window crossing midnight belongs to
the day it starts. Events missed while AutoDJ is stopped are not a backlog to
be replayed automatically on restart.

## Soft and Hard express priorities

**Soft** timing usually lets the current song finish before the action runs.
**Hard** timing usually applies at the event time and can fade the current song
out. **Wait up to seconds** allows a Hard event to preserve a song that will
finish within that allowance; it is not an indefinite wait for paused playback.

The action matters as well as the timing label. A Soft show queues its songs at
the due time, while a Hard show starts its first song immediately. Stream-control
and Stereo Tool preset actions do not skip the playing song. Ad breaks do not
fade a song out just because their timing is Hard. Consult the
[scheduler reference and procedures](../operations.md#scheduler) and
[ad break details](../operations.md#ad-breaks) before relying on a timing label.
**Run now** performs the action immediately; it is an execution control, not a
private rehearsal.

## Fixed and predicted are different kinds of evidence

The **Hour preview** combines already prepared or queued material with
predictions from an isolated copy of the station's picking state. **Fixed** rows
reflect committed choices at the time of the preview. **Predicted** rows estimate
what automatic picking will do with the current settings, history and schedule.
Opening the preview does not consume live choices or advance the clockwheel.

Even fixed material can be changed by an operator. Future manual choices,
listener requests, live DJs and unbooked voice tracks cannot be promised by a
forecast. Random choices can differ. Missing duration information and bounded
preview work can stop a forecast early; read its explanation rather than assuming
an empty tail is silence on air.

**Backtiming** compares estimated song changes with fixed times, showing how early
or late a handover may be. Hour fitting can prefer a better song length among
eligible choices; it does not turn predictions into reservations. **Recent runs**
and playback history provide evidence of execution. A preview is planning
evidence, not a record that an event ran or audio aired.

Try [the scheduling lesson](../tutorials/scheduled-show.md) for practice, or
use [Hour preview and backtiming](../operations.md#hour-preview-and-backtiming)
and [testing the rotation](../operations.md#testing-the-rotation) for procedures
and specific omissions. [ADR 9](../adr/0009-share-picking-rules-with-isolated-previews.md)
records current implementation evidence with Proposed status.
