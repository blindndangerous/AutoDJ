# Schedule and verify a short show

You will prepare a two-song Practice show, schedule it once, and watch normal
rotation resume afterward. You will distinguish saving a show, preparing its
songs, and arranging when those songs play.

## Before you begin

Start from [Build your first station](first-station.md): at least five basically
analysed songs, the folder-based `Practice` category, and the one-step `Practice`
clockwheel with repeat rules off. Use **Browser** output, no listeners, and a
comfortable volume. The [two-category lesson](two-categories.md) is optional.

If you followed another lesson, activate the original Practice wheel and check
**Settings**, **Song picking**, **Pick mode** is **Clockwheel**. Stop any show
and disable other practice schedule events. Keep this lesson separate from a
live schedule. You will save a show named `Practice short show` and a one-time
event named `Practice show start`. No paid services or advanced analysis are needed.

Allow about twenty minutes, or longer for long songs. AutoDJ must stay running
through the scheduled time. An event missed while the application is stopped
does not run automatically when it returns.

To stop early, disable the lesson event, use **Stop show** in **Library tools**,
**Shows** if a show is active, and pause with **Play or Pause** in **Now Playing**.
Pausing the browser alone does not disable a future event.

Use Tab and Enter or Space for controls, and Left and Right Arrow in tab strips.
The [keyboard and screen-reader guide](../guides/keyboard-and-screen-reader.md)
provides navigation detail. The checks use text as well as audio.

## Prepare a saved list

A **show** is a saved, ordered list of songs. Unlike a clockwheel, it has an
end. Generating its list chooses songs ahead of time; scheduling it later does
not generate a new list.

1. In **Library tools**, expand **Shows** and press **Add show**.
2. Enter `Practice short show` in **Name**.
3. Under **Songs come from**, choose **A clockwheel**. Set **Clockwheel**
   to **Practice**, using its unrestricted, repeat-rules-off step.
4. Set **Target length (minutes)** to `5`, then press **Save show**.
5. In **Saved shows**, use **Open list** for Practice short show.
6. Leave **Pick as if at** empty and press **Generate list**. Wait for the
   result and read the song count, total length, and any note.

The target controls when generation stops; it is not an exact broadcast length.
A song crossing the five-minute target can make the list longer. Preparing
this list does not start the show or change what is playing.

Make the example exactly two different songs. If generation produced more than
two, use **Remove** on the extra entries. If it produced only one, enter another
practice title in **Search the library**, press **Search**, then **Add** for a
different song. Use **Move up** or **Move down** to put the two songs in an order
you recognize. These edits save immediately. Write down the two titles in order.

Checkpoint: the list has two songs and a total duration; the saved show no longer
says “no list yet.” Avoid **Generate list** after arranging it, because generating
again replaces the list.

If generation fails, confirm Practice exists in **Clockwheels**, its step uses
the nonempty Practice category, and the step has no day/hour restrictions or
repeat rules. Save the corrected wheel and try **Generate list** again. If a
song is missing from the library, replace that list entry through **Add songs**
before proceeding. Do not schedule an empty or broken list.

## Choose a time in the station's time zone

1. Open **Settings**, **Schedule**. Read the time zone displayed above
   **Next events**.
2. Check the current date and time in that zone using your computer's clock.
   If it differs from the computer's local zone, use that zone's time for the
   fields below. Choose a start at least five minutes ahead.
3. Press **Add event** and enter `Practice show start` in **Name**.
4. Set **Action** to **Play a show**, then **Show** to **Practice short show**.
5. Set **Repeat** to **Once** and enter the chosen **Date** and **Time**.
6. Set **Timing** to **Soft**, check **Enabled**, and press **Save event**.

**Soft** timing lets the current song finish before the show's first song.
The event can therefore act at its due time while the audible show begins at
the following song change. We use that behavior to preserve the song already
playing. **Hard** timing can interrupt it.

Checkpoint: **Next events** lists Practice show start at the intended date and
time, and its **Events** entry describes Soft timing. If it does not appear,
use that event's **Edit**, check **Enabled**, date, and time, then save again.
If the chosen time has already passed, choose a new future time. Do not change
the system clock to make an event happen.

If the station time zone itself is wrong, stop here and correct it under
**Settings**, **Stations**, **Time for the selected station**. Save the time
setting, then stop and restart AutoDJ with your original launch command so the
scheduler uses the new zone. Return and recheck **Next events** before
proceeding; see the [schedule guide](../guides/schedule.md).

## Watch the scheduled handoff

1. In **Queue & Search**, clear any waiting queue entries with **Clear queue**
   and its confirmation. Leave the show's saved list intact.
2. In **Now Playing**, use **Play or Pause** to resume if paused. If there is
   no current song, search a Practice song and use **Play now** first.
3. Let music continue through the scheduled time. In **Schedule**, read
   **Recent runs** for the event's result.
4. Open **Library tools**, **Shows**. Check that Practice short show is on air
   and reports songs waiting. In **Now Playing**, let the current song finish.
5. Compare the next two titles and **History** with your saved two-song order.
   Let both show songs finish, then observe one more song.

The event result establishes that its action ran. The on-air show text, queue,
and played titles establish what followed. After the final show entry has been
consumed, the show indicator clears and normal Practice picking supplies music
again when the queue is empty. A title may recur because this is a tiny pool;
also check that no show remains active and the original wheel is still active.

If **Recent runs** reports that nothing was playing, the event could not start
the show. Start a Practice song with **Play now**, edit the event to a new future
time, check **Enabled**, and save. If it reports the show has no songs, return
to **Open list**, prepare the two entries, then reschedule. A successful action
with delayed audio may simply be Soft timing waiting for the current song.

## Try it: disable the clock, test the action

Predict whether **Run now** can play the show when its schedule event is
disabled. Does disabling remove the show, or just its automatic appointment?

After the first show finishes, disable Practice show start if it is still
enabled. If its row offers **Enable**, it is already disabled. Confirm a song
is playing, then press that event's **Run now** once. Inspect **Recent runs**,
the on-air show text, and the next titles.

Expected result: the action runs even though automatic scheduling is disabled.
The prepared list follows the current song because timing is still Soft.
Disabling prevents timed runs; it preserves the event and the saved show.
Explain the difference using the disabled event entry and the new run result.
Let this second run finish, or use **Stop show** to cancel its waiting songs.

## Stop and keep your work

1. Leave Practice show start disabled. A completed one-time event normally
   disables itself; check the row rather than assuming it did.
2. If a show is still active, press **Stop show** in **Shows**. Wait until the
   text says “No show is on the air.” If it reports stopping, let the next
   song change complete the removal, then check again.
3. Pause in **Now Playing** and confirm the elapsed time stops advancing.
4. Confirm the original Practice wheel is active and **Pick mode** remains
   **Clockwheel**.

The show, its two-song list, the disabled event, and recent-run records remain
saved. Stopping a show removes its waiting entries; it does not delete the saved
list or cut the song already playing. No deletion is needed for the next lesson.

You can now prepare a finite list, schedule its start, and verify its return to
rotation. Continue with [Back up and recover Practice safely](backup-recovery.md),
or use the [schedule guide](../guides/schedule.md) for recurring events.

For background, read [how scheduling works](../explanation/scheduling.md).
Before using a practice setup regularly, follow
[Prepare a station](../guides/prepare-station.md).

AutoDJ and this page were developed with AI assistance.
