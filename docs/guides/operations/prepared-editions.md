# Prepared show editions

For a guided first production, follow [Prepare tomorrow's speech while the station is stopped](../../tutorials/prepared-show-edition.md).

In **Library tools**, open **Shows**, open a saved show list, then expand
**Prepared editions**. Enter a distinct edition name and press **Create edition
from show**. The edition starts with its own copy of the song order. Each repeated
song is a separate occurrence. Editing the reusable Show later does not replace
this production's list or recordings. Keep one active take at each opening, closing,
transition or segment position; choose **Re-record** from the take's **Options** menu to replace its audio while
retaining the previous source asset.

The occurrence table shows Position, Item and Kind. Use its row's **Options** menu for
**Move up**, **Move down** or **Remove**. Remove asks for confirmation. To add a song,
search the library above and choose **Add to edition** from the result's **Options** menu. Changes use a revision check: if another page
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
5. In the saved take's **Options** menu, choose **Review** to review pending speech. AutoDJ opens the shared
   [Programme review](programme-review.md) controls. Its approval-target
   review retains the entire take, including long interviews, while music middles
   can shorten. Listen and navigate; neither action approves the take.
6. Return to the edition, open the take's **Options** menu and choose **Approve** using that completed review. The
   server checks the exact source asset, adjacent occurrences, cues and applicable
   mixing inputs. A missing, shortened or placeholder target cannot approve speech.

The takes table shows Name, Placement and Status. **Listen** loads a saved take into the
private player. Its **Options** menu contains **Re-record**, **Review**, **Approve**, **Reattach**
and **Remove**.
Remove asks for confirmation. While an edition save is pending, Remove stays
reachable and explains that you must wait; it cannot start another change.
Unavailable actions remain reachable and explain their restriction.

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

A standalone segment follows its occurrence when you move it, add a song after
it or remove a neighbour. Changed neighbours require review and approval again.
If an edit separates a transition's pair, its take shows **Unattached**. In that
take's **Options** menu, choose **Reattach**. In the dialog, select the new adjacent pair
in **Position** and press **Reattach**. Focus returns to the take's Options cell.
Then review and approve it again. Reattaching keeps the saved audio. An unattached
opening or closing take offers the current first or last item in the same dialog.

Edits refresh uncommitted prepared audio. The playout commit boundary is the
first affected speech duck ramp or incoming overlap; a primary speech occurrence
commits when its first PCM enters playout, including an incoming overlap.
AutoDJ refuses an edit affecting the current speech or next handoff whose early
check already sees that boundary crossed. Other edits remain available: you can
approve a closing take while the opening plays. That check is not an atomic promise: playback may cross
while a save or replacement render is in progress. In that case the edit is saved
for future review and playout, while the current occurrence retains its prior
approval and continues without interruption. **Current committed audio continues**
is reported explicitly. Approval changes cannot retract audio already committed
in this run; later uncommitted buffers are rebuilt without ineligible speech.

During live streaming, a queue change can affect speech in the current editable
handoff, the ready live playout handoff, or an affected pinned slot plan. One
choice applies to those affected speech contexts: choose **Keep voice track**
to carry the speech with its changed music pair, **Discard voice track** to
remove it from the changed contexts, or **Cancel change** to leave the queue
alone. If a voice or liner chain has already started, its timing and layers stay
in place while unheard incoming music changes at the original boundary; the
speech does not repeat. This does not alter the saved edition, its approval, or
its source recording. If the prepared take is no longer eligible, keeping it
for this one replan does not approve it for reuse. A stale choice requires a
fresh confirmation.

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

Opening and closing clips map to a real programme item for resume: the first
item for an opening, or the last item for a closing. Confirm the selected item
before resuming; the closing fallback can replay that last item.

## Retention and backup

Edition state lives in the station's `editions.json`; immutable source WAV files
live in `edition_audio/`. Normal AutoDJ backup/restore includes both. Partial
imports and derived review files are excluded. Keep an independent backup before
removing a production. An active edition cannot be deleted. Removing a take keeps
its prior source asset with the edition; deleting the edition removes assets no
other edition references. Source audio and explicit approval state survive restart.
Removing a segment's programme row also removes its active take and retains its
source asset. Another segment can then use that position.
On the process's first access to an edition-audio directory, AutoDJ removes
abandoned partial upload and import folders. Later station starts or settings
changes do not repeat that cleanup. Cleanup failures are logged without blocking
station startup.

Retained source assets count toward the station's 4 GiB edition-audio cap,
including superseded takes. There is no individual source-asset cleanup control;
back up a production before deleting its edition to reclaim its unshared files.

The required [Windows/Chrome/NVDA rehearsal](../../accessibility-testing.md#prepared-edition-rehearsal)
and listening checks run after both implementation PRs are submitted. Publication
is not a claim that those manual checks have passed.
