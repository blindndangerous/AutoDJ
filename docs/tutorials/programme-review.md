# Listen to and export a shortened programme review

You will make a private review of a two-song practice show, navigate its song
changes, and download the same edit as MP3 and WAV. Then you will change one
review setting and compare the new result with the saved original.

Start with AutoDJ installed, a paired operator browser, and a saved Show named
`Practice review` containing two playable songs, each between two and four minutes long.
Use music you may copy and a practice station with no listeners. If you need to
make the list first, follow [Planning a show](../operations.md#planning-a-show),
then return here. Advanced similarity analysis and a speech provider are not
needed for these two songs.

## Before you begin

Check that both files are available on the AutoDJ computer and FFmpeg is
installed. The [installation guide](../guides/install.md) covers setup. Select
the practice station. In **Settings**, **Stations**, press **Stop** on its row,
confirm **Stop station**, and wait until the row offers **Start**. If it already
offers Start, leave it stopped. Saved-show review works without station playout
or a near-air voice-track slot.

Use headphones for the private player. In **Settings**, **Sound**, check
**Preview output device**. Device selection depends on browser support; if the
browser uses its default device, arrange that default before continuing. Use the
[keyboard and screen-reader guide](../guides/keyboard-and-screen-reader.md) for
moving between tabs and native controls.

This lesson creates saved review files and browser downloads. It does not start
or schedule the Show. **Cancel generation** stops an unfinished review job;
existing completed reviews remain available. Pause the private player to stop
listening.

## Capture the practice show

1. Open **Library tools**, then expand **Programme review**.
2. Press **Refresh reviews**. Set **Source** to **Saved show**, then choose
   `Practice review` in **Show**.
3. Leave **Intended start** blank for now.
   Leave both **Range start (seconds)** and **Range end (seconds)** blank so
   the review covers the captured show rather than one selected section.
4. Expand **Review timing**. Keep **Opening (seconds)** and **Ending (seconds)**
   at `10`, **Long clip (seconds)** at `300`, and **Skip tone (seconds)** at `0.25`.
5. Press **Generate review** once and wait for **Review ready**.

The result describes its creation time, **Complete** status, preview length and
effective programme range. It is a saved snapshot of planned audio. Check that
the station is still stopped: creating a review has not aired the Show.

If the Show is absent, confirm the selected station and press **Refresh reviews**.
If the result is **Incomplete**, read the omission list and repair the named
missing file before generating again. Missing material needs the station's
default TTS command to make a spoken placeholder; an error about that command
is not a successful complete review. For this example, restoring both song files
avoids needing placeholders. See [review recovery](../operations.md#incomplete-or-out-of-date-results)
for other failures.

## Listen and inspect the two clocks

1. Find **Private programme review** and start its native audio controls.
2. Listen for an opening, a skip tone where a song middle was removed, and the
   actual song change. Pausing the player stops private listening.
3. Inspect an item button below the player. It gives **Programme** times for
   the full planned mix and **preview** times for the shortened file.
4. Press **Next item**, then **Previous item**. Read the announced item and
   timestamps. These controls seek; if you paused, start the player explicitly
   to hear the new position.

The two clocks differ because the review removes song middles. Real transitions,
overlays and ducking can keep more than the requested ten seconds. Short clips
remain complete; clips longer than five minutes shorten like songs by default.
This is planned audio before final broadcast mastering, not a recording of what
aired or a promise of identical future automatic choices.

If the native player has no sound, check its paused state, volume and the private
output device before retrying. A playable result and readable timestamps do not
establish that your headphones were audible.

## Keep the same edit in two formats

1. Pause the player. Press **Download MP3**, then **Download WAV** and save both
   with names that identify this practice result.
2. Open each download in your usual audio player. Check that the same song
   openings, skips and change are present. MP3 is compressed; its file size need
   not match WAV.
3. In **Saved review**, choose this result and press **Load review**. Its original
   audio and timestamps remain available without generating another mix.

Both downloads come from the frozen result. Retain downloads you need:
AutoDJ's station backup excludes derived review files.

## Try a shorter opening

Predict what will change if **Opening (seconds)** becomes `5` while the ending
stays `10`. Set that value and press **Generate review** again.

Expected result: a second dated review appears. Where transition protection does
not require extra audio, its song openings are shorter. The first result remains
unchanged: select it in **Saved review** and press **Load review** to compare.
A protected join or a short song can limit the difference; the setting does not
cut required transition audio merely to reach five seconds.

## Stop and keep your work

Pause any player still running. If generation is active, press **Cancel generation**
and wait for its status to settle. Keep the Show and downloads for later practice.
To remove only a review, select it in **Saved review**, press **Delete review**,
check the dated result in the confirmation, and confirm **Delete review**.
Check that it disappears from the list and no replacement is selected.
This does not delete the Show or songs.

You can now review a saved programme without airing it. For a single transition,
try the range controls described in [Programme review and export](../operations.md#programme-review-and-export).
See [review settings](../settings-reference.md#programme-review) for configurable
windows and limits.

AutoDJ and this page were developed with AI assistance.
