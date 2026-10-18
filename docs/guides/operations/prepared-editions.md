# Prepared show editions

For a guided first production, follow [Prepare tomorrow's speech while the station is stopped](../../tutorials/prepared-show-edition.md).

In **Library tools**, open **Shows**, open a saved show list, then expand
**Prepared editions**. Enter a distinct edition name and press **Create edition
from show**. The edition starts with its own copy of the song order. Each repeated
song is a separate occurrence. Editing the reusable Show later does not replace
this production's list or recordings. Keep one active take at each opening, closing,
transition or segment position; use **Re-record** to replace its audio while
retaining the previous source asset.

Use **Move up**, **Move down**, **Remove**, and the library search's **Add to
edition** to edit that production. Changes use a revision check: if another page
saved first, reload rather than overwriting its work. Read the estimated duration
and any likely-overrun warning. Estimates do not guarantee a finish time; scheduler
hard events retain priority and can interrupt the edition.

## Record, review and approve

Station playout can stay stopped throughout preparation.

1. Choose **Opening**, a particular **Song transition**, **Closing**, or a
   **Standalone segment** insertion point. Check the adjacent occurrence labels,
   especially when the same song appears twice.
2. Select **Placement** and whether to allow outgoing-song overlap. Prepared
   speech joins use the existing saved-point placement and ducking rules. Normal
   song-only joins retain the station's chosen transition mode.
3. Press **Record**, stop within 3:00, and listen with **Private edition take**.
   **Save recording** stores a pending take. Re-recording preserves the previous
   saved source. **Discard local recording** abandons only the unsaved recording.
   Listening to a saved take preserves an unsaved local recording. Replacing or
   discarding an unsaved recording requires confirmation; cancelling keeps it
   available to save. Browser recording needs HTTPS or localhost and microphone permission.
4. Alternatively choose **Audio file** and **Import take**. Imported short takes are bounded
   to 5:00; externally edited standalone segments can be up to 30:00. Import uses
   the existing local decoder. The upload cap is 512 MiB; invalid or failed imports
   leave the saved edition intact. Larger uploads get more time: 60 seconds plus
   one second per 256 KiB of declared data, or the 512 MiB cap when the size is
   unknown. Decoding runs after the upload deadline ends. There is no interview editor here.
5. From the saved take, explicitly review pending speech. AutoDJ opens the shared
   [Programme review](programme-review.md) controls. Its approval-target
   review retains the entire take, including long interviews, while music middles
   can shorten. Listen and navigate; neither action approves the take.
6. Return to the edition and explicitly approve using that completed review. The
   server checks the exact source asset, adjacent occurrences, cues and applicable
   mixing inputs. A missing, shortened or placeholder target cannot approve speech.

**Review edition** follows broadcast eligibility and excludes unapproved speech.
Pending review is an explicit separate request. Ordinary programme reviews still
shorten clips above their configured threshold; an ordinary shortened interview
review cannot establish full-take approval. Prior review files stay playable even
when out of date. Approval may reuse a review after unrelated edits when the
reviewed take's relevant inputs remain identical.

Changing audio, its bound pair, relevant cues or placement inputs marks the take
**Needs review** without deleting its recording. Moving an intact adjacent pair
or changing an unrelated edition item preserves valid approval. Missing, invalid,
unattached and unapproved speech is skipped and reported during playout; valid
songs continue without generating a replacement take.

Edits refresh uncommitted prepared audio. The playout commit boundary is the
first affected speech duck ramp or incoming overlap; a primary speech occurrence
commits when its first PCM enters playout, including an incoming overlap.
AutoDJ refuses an edit whose early check already sees
that boundary crossed. That check is not an atomic promise: playback may cross
while a save or replacement render is in progress. In that case the edit is saved
for future review and playout, while the current occurrence retains its prior
approval and continues without interruption. **Current committed audio continues**
is reported explicitly. Approval changes cannot retract audio already committed
in this run; later uncommitted buffers are rebuilt without ineligible speech.

## Schedule, re-air and resume

Use **Air at (station time)**, **Schedule timing**, then **Schedule edition**, or
select the prepared production under the existing **Play a show** schedule action.
Soft timing follows the playing song; hard timing starts at the event boundary.
**Start edition after this song** and **Start edition now** use the same runner.
Initial prepared-edition playout requires server audio or streaming; browser-mode
station playout is not supported. Broadcast uses local approved audio and does
not call speech providers. Long standalone segments play at normal speed without
being treated as liners or automatically shortened.

Scheduling or starting an edition that has begun before requires explicit re-air
confirmation: its speech may already have aired, including before a crash.
After a restart, an interrupted prepared edition returns to automation and remains
visibly interrupted. Choose **Resume from item** and **Resume edition explicitly**
to continue after revalidating current speech. The saved occurrence is selected
when available; an existing operator choice survives editor refresh. If the
chosen occurrence was removed, choose another explicitly. Stopping a different
Show does not dismiss this edition's Interrupted state. Ordinary saved Shows retain their
existing automatic-resume behavior. Creating or recovering a review never starts
a broadcast.

## Retention and backup

Edition state lives in the station's `editions.json`; immutable source WAV files
live in `edition_audio/`. Normal AutoDJ backup/restore includes both. Partial
imports and derived review files are excluded. Keep an independent backup before
removing a production. An active edition cannot be deleted. Removing a take keeps
its prior source asset with the edition; deleting the edition removes assets no
other edition references. Source audio and explicit approval state survive restart.

Retained source assets count toward the station's 4 GiB edition-audio cap,
including superseded takes. There is no individual source-asset cleanup control;
back up a production before deleting its edition to reclaim its unshared files.

The required [Windows/Chrome/NVDA rehearsal](../../accessibility-testing.md#prepared-edition-rehearsal)
and listening checks run after both implementation PRs are submitted. Publication
is not a claim that those manual checks have passed.
