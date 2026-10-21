# Put a recorded station ID over your music

You will add an existing spoken clip, preview it privately, and place it in
Practice playback. Then you will hear how ducking lowers the music under the
voice and learn to stop automatic announcements without deleting the clip.

Start with [Build your first station](first-station.md) completed: the Practice
folder category, one-step Practice clockwheel, **Clockwheel** pick mode, at least
five songs, **Browser** output and no listeners. Use this private station, not
an on-air service. You do not need a microphone, speech provider or stream encoder.

## Before you begin

Prepare one short spoken audio file you have permission to use, preferably saying
“This is Practice radio.” Call it `practice-id.mp3`; renaming your own copy before
uploading makes the lesson easy to follow. Keep it a few seconds long. The liner
uploader accepts MP3, WAV, OGG, M4A, FLAC and AAC, subject to the station upload limit.
A supported extension alone does not make an invalid file playable.

This lesson uploads a copy into the station's liner folder and saves liner settings.
It does not change your original recording or request generated speech. Keep the
recording afterwards for [the speech lesson](speech-liner.md), where you will
compare recorded audio with a generated asset's publication steps.

Pause music with **Play or Pause** in **Now Playing** while reading. Pause the
separate **Song point preview** player after an audition. These are independent
players. Use a comfortable volume; keyboard and screen-reader navigation is
covered in the [keyboard guide](../guides/keyboard-and-screen-reader.md).

## Upload without starting a repeating announcement

The upload controls are inside the enabled liner settings. First enable those
controls with every automatic trigger off.

1. In **Settings**, **Voice liners**, check **Enable voice liners**.
2. Set **Every N tracks** and **Every N minutes** to `0`. Set both **Random window
   minimum (minutes)** and **Random window maximum (minutes)** to `0`.
3. Set **Rotation mode** to **Sequential** and **Music duck (dB)** to `3`.
4. Use **Choose liner file** to select `practice-id.mp3`. Leave **Replace existing file**
   unchecked, then press **Upload**.

These settings save on change; move focus away from a numeric field to finish its
change. Check the upload status for “Uploaded practice-id.mp3” and find the name in
**Voice liner files**. Returning to the settings should still show the trigger
values you entered. Uploading makes an asset available; it does not set a clock
time or create an automatic interval.

If a file with that name already exists, the upload is refused. For this lesson,
rename your new local copy and upload it again without replacing anything.
Replacement deliberately overwrites the station copy. If the file is refused as
unplayable, open the original in an audio player and export a real audio file in a
supported format; changing its filename suffix is not a conversion.

## Audition the clip before placing it

1. In **Voice liner files**, press **Track info** for your uploaded liner.
2. Check **Type** is **Voice liner**, the filename is correct, and **Duration**
   is plausible. Advanced analysis should say **Not needed for liners**.
3. Expand **Cue points**. Inspect **Start time**, **Segue time**, **End time**
   and **Overlap start** without editing them.
4. Press **Preview Start**. Listen to the opening, and pause **Song point preview**
   when finished. Use its native controls to replay the clip if needed.

This preview does not insert the clip into the queue or clockwheel. The player
position and duration provide text observations; hearing the opening also lets
you judge whether the speech is clipped or silent. Start and End bound playback.
Segue and Overlap start describe handoff behavior where the selected output and
transition mode use them. A preview is not a rehearsal of every possible overlap.

For a silent preview, first check that it is playing and the computer's output
is audible. Inspect the Start and End values for obviously misplaced boundaries.
Do not change them merely to match a number in this lesson: your recording has
its own timing. For this worked example, a bad recording is best replaced with
a clean short copy. Saved cue changes require **Save points**; previewing alone
saves nothing. See [adding audio](../guides/add-your-music.md#add-a-voice-liner-folder)
when working with a whole folder instead of one upload.

## Place one liner ahead of the next song

1. Pause the preview player and return to **Queue & Search**.
2. Use **Clear queue** and confirm if there are waiting entries.
3. Choose **Voice liners** in the library-type selector, then press **Search**
   with empty **Search text (optional)**.
4. Find `practice-id.mp3` and press **Add to queue** once.
5. Confirm that the liner appears in the queue. Resume music with **Play or Pause**.
6. Let the current song finish. Observe the queue as the liner is consumed and
   the next song begins, then inspect **Now Playing** and **History** for the music.

The queued liner provides one deliberate placement. It does not replace the
Practice music wheel or become a music-category member. A short overlay may not
become the main song title, so a music title staying visible does not mean the
voice failed. Queue state establishes the placement; listening establishes what
you actually heard. History supplies music context rather than a complete proof
of every overlay's sound.

If the wrong voice plays, check for other queued liners and remove them before
repeating. If this liner is marked **Skipped now**, open **Play dates** for it in
Voice liners. For this practice copy, clear **Not before** and **Do not play after**,
then press **Save dates** and check that the skip explanation disappears. This
makes it eligible on any day; it does not change other liners' dates.

## Hear what ducking changes

1. Return to **Settings**, **Voice liners** while music plays.
2. Confirm this practice station has only your one eligible liner. **Test now**
   picks an eligible liner from its rotation, rather than selecting a file by name.
3. Set **Music duck (dB)** to `0`, leave the field, then press **Test now**.
4. Check the status reports **Liner playing:** followed by your clip's name.
5. After it ends, set **Music duck (dB)** to `9`, leave the field and press
   **Test now** again over the same song.

The second test should lower the music more under the voice, then return it.
The UI asks for a positive drop: `9` means 9 dB lower music, not louder music.
The saved numeric value and “Liner playing” status are accessible checkpoints;
only listening can establish intelligibility and the quality of the fades.
The best amount depends on the recording and music, so this is a comparison,
not a universal recommended level.

If Test now refuses playback, read its status. Unmute or resume as requested,
then try once more. If it reports that every liner is outside its play dates,
recover through **Play dates** as above. If more than one eligible liner exists,
complete the comparison on a fresh Practice station or use the queue to identify
the intended clip; do not delete unrelated recordings to force the test.

## Try it: disable automatic liners

Predict whether a queued liner will still play with **Enable voice liners** off.
First set **Every N tracks** to `2`, leaving the other triggers `0`. This would
allow automatic liners after track changes while enabled. Now uncheck **Enable
voice liners**, queue your clip once as above, and let the current song finish.

Expected result: the explicit queued liner still plays. The enable switch governs
automatic intervals; queued liners and clockwheel liner steps still work while
it is off. Explain why disabling automatic intervals is different from removing
an already queued clip. Your queue and the saved unchecked switch are the text
checks; listen if you can to verify the overlay.

## Stop and keep the recording

Clear any remaining practice liner from the queue. Briefly check **Enable voice
liners** to expose the trigger fields, set all four interval fields back to `0`,
and uncheck it again. Restore **Music duck (dB)** to `3` before disabling if you
want the initial value. Let a playing clip finish, then pause **Now Playing** and
any preview player. Check the music's elapsed time stops advancing.

The uploaded clip and its play dates remain saved. Disabling automatic liners
does not stop a clip already playing, delete files, or cancel explicit placements.
No scheduled event or clockwheel liner step was created here. Later, **Delete**
for the station's uploaded file removes that station copy after confirmation;
your original local recording remains.

You can now audition, place and mix a recorded station ID. Use
[Build your sound](../guides/build-your-sound.md#add-voice-liners) for interval
rotation, or [scheduling](../guides/schedule.md#schedule-music-liners-and-announcements)
for timed events, which require a server output mode for liners.

For background, read [how playback works](../explanation/playback.md).
Before using a practice setup regularly, follow
[Prepare a station](../guides/prepare-station.md).

AutoDJ and this page were developed with AI assistance.
