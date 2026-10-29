# Review and export a programme

Generate a private shortened mix, listen to its transitions and keep matching MP3/WAV exports.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Programme review and export

Use **Library tools**, **Programme review** to hear a shortened planned mix of a
transition, section or programme and download the same result as MP3 or WAV.
This is private audio before final broadcast mastering. It neither starts nor
changes station playout and does not record what actually aired. Automatic
broadcast choices can differ later. The [architecture proposal](../../adr/0014-isolate-programme-review-and-export.md)
defines ownership and evidence limits.

For a guided first run, follow [Listen to and export a shortened programme review](../../tutorials/programme-review.md).

### Generate and listen

1. Select the station. Open **Programme review** and press **Refresh reviews** to
   load its saved shows and retained results.
2. Choose **Source**: **Saved show**, **Current queue**, or **Forecast**. For a
   saved show, choose **Show**; it can be reviewed while the station is stopped.
   For a forecast, set **Programme minutes** from 1 to 60. Saved shows and queues
   use their captured content.
3. Optionally set **Intended start** in this browser's local time. Blank means
   now. Automatic content is resolved against that full programme time in the
   station's time zone, before song middles are removed.
4. For a transition or section, enter **Range start (seconds)** and **Range end
   (seconds)** relative to the captured programme. Leave both blank for the full
   programme. The effective range can expand to preserve complete short clips,
   transitions and ducking.
5. Open **Review timing** to change the opening/ending windows, long-clip
   threshold or skip-tone length. See the [values and defaults](../../settings-reference.md#programme-review).
6. Press **Generate review**. Generation runs separately from playout; **Cancel
   generation** abandons that job and preserves existing results.
7. When ready, choose the result under **Saved review** and press **Load review**.
   Read its date in this browser's locale and time zone, effective range and
   complete/incomplete status. Use the native
   **Private programme review** audio controls to listen. Audio never starts
   automatically. **Settings**, **Global**, **Preferences**, **Preview output device** selects
   the same private device used by other previews; browser/device fallback advice
   remains applicable.
8. Use **Previous item**, **Next item**, or an item button to seek without starting
   playback. Each item gives both its full programme and shortened preview times.
   **Download MP3** and **Download WAV** contain the same frozen edits, skip tones
   and placeholders heard in the player.

Songs retain their first and last 10 seconds by default. Non-song clips lasting
five minutes or less remain complete; longer interviews and other clips shorten
like songs. Actual joins, overlapping voice and ducking can extend these windows.
Overlapping windows merge rather than repeat audio. A quiet generated skip tone
marks each removed middle; it is a review edit, not a broadcast segue.

### Incomplete or out-of-date results

An unresolved item appears in the omission list and is replaced by spoken audio
using the configured default TTS command (`time_checks.tts_command`). This does
not generate the missing programme content. If that command is unset or fails,
reviews needing placeholders fail with an error; complete content still renders.
Configure or repair the existing command and generate again. Where a source
duration is unknown, downstream programme times say **Unknown**; the placeholder
does not establish broadcast duration.

Press **Refresh reviews** after editing a show, audio, cues or relevant settings.
Affected results say **Out of date** and remain playable/exportable with their
original audio. Generate explicitly to create a new result. Editing or hearing a
review does not approve pending speech for broadcast. Prepared editions and their
approval workflow are a separate feature.

Play-history updates and unrelated loudness measurements do not invalidate saved
reviews. First-play cache fills are ignored when the review did not use a cached
measurement; changes to selected cached gain used by processed review can mark
it out of date. Queue and forecast reviews can become out of date as their
captured live queue advances.

### Storage and availability

- **Saved-show private review:** available without running station playout.
- **Queue/forecast review:** captures the selected station's current planned material.
- **Listening and MP3/WAV export:** operator browser; the same frozen pre-mastering result.
- **Public/listener or limited DJ/announcer/worker access:** no review access.
- **Actual-air recording or final Stereo Tool processing:** outside programme review.

One render runs across the server at a time, with a cooperative 120-second work
limit covering capture, rendering and encoding. Each station retains up to 20
results and 512 MiB; reaching either limit
refuses generation instead of deleting older reviews. These are resource limits,
not a promise that every admitted job completes. Choose a shorter saved show or
queue, reduce forecast minutes, or delete unwanted snapshots after a capacity
error. Selecting a range does not reduce the programme captured and mixed.

Working audio has a separate 2 GiB scratch allocation budget, including decoded
sources and copies, and leaves at least 64 MiB free. Reservations are released
when decoded buffers are freed; active views keep their reservation. Forecasts
offer at most 60 minutes. Saved shows and queues admit at most 60 minutes of known
source audio, conservatively estimated from indexed lengths before decode; their
captured span must fit that limit too. A longer show or queue fails with advice
to choose a shorter source list, even when a short range is selected. A 180-minute
unshortened mix alone exceeds the scratch budget. A long individual source or
mostly complete audio below 60 minutes can still exceed
that budget even if its shortened export would be small. Generation fails
actionably rather than spilling outside its owned workspace. These service
limits are separate from the review timing controls.

Results live under the station's state folder in `reviews/`. Completed results
survive restart; interrupted jobs fail and their partial files are cleaned up.
Recovery never starts the station. Results have no automatic expiry. Select an
unwanted **Saved review** and press **Delete review**, then confirm the dated result to remove it explicitly.
After deletion, choose another saved review before loading or deleting again.
AutoDJ backups exclude these derived files: download and separately retain MP3/WAV
exports that you need to keep. Source audio remains under its existing ownership.
