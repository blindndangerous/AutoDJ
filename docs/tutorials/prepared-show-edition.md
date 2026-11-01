# Prepare tomorrow's speech while the station is stopped

You will record a short introduction between two songs, save it in a prepared
edition, and explicitly approve it after hearing a contextual review. The result
will be ready for a later airing while the station remains stopped.

Begin with the two-song `Practice review` Show from
[Listen to and export a shortened programme review](programme-review.md), or an
equivalent saved Show containing two available songs. Use a private practice
installation with no listeners. A prepared edition is a separate production of
that list: its recordings and approval state do not change the reusable Show.

## Before you begin

Use a paired operator browser with a microphone. Open AutoDJ through localhost or
HTTPS; a plain HTTP LAN address cannot record microphone audio. Use headphones
for private previews, and check **Settings**, **Sound**, **Preview output
device**. If the browser falls back to its default device, arrange that default
before recording. No speech provider or near-air voice-track slot is needed.

In **Settings**, **Station**, **Stations**, select the practice station. If its row offers
**Stop**, press it, confirm **Stop station**, and wait until it offers **Start**.
Leave the station stopped throughout this lesson. Preparation and review do not
require a particular station playback mode. Later broadcast playback requires
server audio or streaming; it is a separate action.

This lesson saves an edition, a local speech asset and a private review. Recording
stops with **Stop recording**. **Discard local recording** removes an unsaved take.
Saving a take does not approve or air it. Use the
[keyboard and screen-reader guide](../guides/keyboard-and-screen-reader.md) for
navigation between tabs, headings and native controls.

## Give this production its own identity

1. Open **Library tools**, expand **Shows**, and choose **Open list** from
   the **Options** menu of `Practice review`.
2. Expand **Prepared editions**. Enter `Tomorrow practice` in **New edition name**
   and press **Create edition from show**.
3. Read the edition summary and its occurrence table. Confirm that items 1 and
   2 are the intended songs in that order.
4. Check that the station row still offers **Start**.

The edition has its own revision and occurrence list. An occurrence means one
particular place in that list: two appearances of the same song can have
separate introductions. This lesson uses the transition from item 1 to item 2.

If the Show has no songs, return to its list and add the two songs before creating
an edition. If you opened a different saved edition, choose `Tomorrow practice`
in **Saved edition** and press **Open edition**. Do not recreate it just because
another page has made your view stale: **Open edition** reloads the saved state.

## Record the introduction privately

1. In **Position**, choose **Song transition**. In **Transition**, choose the
   numbered entry from item 1 to item 2.
2. Set **Placement** to **Between songs** and leave **Allow outgoing song overlap**
   unchecked. This gives the sentence its own space between the songs.
3. Press **Record**, allow microphone access if asked, and say “This is our
   practice programme. Here is the next song.” Keep the take to a few seconds.
4. Press **Stop recording**. Check that the microphone indicator stops and the
   page says the recording stopped for this position.
5. Play **Private edition take**, listen for the complete sentence, then pause it.
6. Press **Save recording**. Focus moves to the saved take's **Options** cell.
   Read **Needs review** in its Status column and check its numbered transition.

If the saved audio is missing or invalid, focus returns to **Edition contents
and recordings**. Repair the audio before reviewing it.

The stored take is now durable, but it is not approved for broadcast. Short
recordings have a 3:00 limit. Listening privately has not changed that state.
If you need to remove it, wait for any edition save to finish, then choose
**Options**, **Remove** and confirm. Remove explains the restriction while saving.

If recording is unavailable, check the secure/localhost address and site
microphone permission, then retry. For silence or clipping, check the input
and record another short take before saving. **Discard local recording** lets
you abandon an unsaved take. Alternatively, choose an existing short WAV in
**Audio file** and press **Import take** with the same position selected; it also
starts in Needs review. An import failure leaves the saved edition intact.

## Review the real transition, then approve

1. Open the saved take's **Options** menu and choose **Review**. AutoDJ opens **Programme
   review** in **Now Playing** and generates the take's contextual review.
2. Wait for **Review ready**. Read the summary's pending-speech message. The
   target is retained completely even when ordinary review rules shorten song
   middles or long clips.
3. Use **Private programme review** to listen through the song ending, your
   sentence and the next song's opening. Item buttons and **Previous item** /
   **Next item** give programme and preview timestamps for navigation.
4. Pause the player, then press **Return to edition**. The take still needs
   explicit approval; generating or listening has not approved it.
5. Open that take's **Options** menu and choose **Approve**. Check that its state becomes
   **ready**, and that the station remains stopped.

Approval records the exact audio and relevant transition inputs. A completed
review is evidence for this take, not permission to air another recording.
If approval is unavailable, read its restriction and finish the target's **Review** request
and return from that review. An incomplete result or missing target cannot
approve speech: repair the named source and generate a new target review.
If a save or approval reports a stale revision, press **Open edition**, inspect
what changed, and repeat review when the take or its relevant inputs changed.

## Try a change that preserves approval

Predict whether changing only the edition's name will make this take need review
again. Set **Edition name** to `Tomorrow practice revised` and press **Save edition
name**. Read the take's state.

Expected result: the name changes and the take stays **ready**. Its audio,
adjacent occurrences and mixing inputs are unchanged. Re-recording the take,
replacing an adjacent occurrence, or changing relevant cues would instead mark
it **Needs review** without deleting the source recording.

If you separate a transition's two items, its take becomes **Unattached**. Choose
**Reattach** from that take's **Options** menu. In the dialog, choose an adjacent pair
in **Position** and press **Reattach**.
The recording stays saved; review and approve its new context before airing.
Standalone segments instead follow their own occurrence and need review again
when their neighbours change.

## Stop and retain the production

Stop any active recording, pause private audio, and cancel any unfinished review
with **Cancel generation**. Keep the edition for later practice. The saved Show
still has its original list; the edition retains its recording and approval.
Station backup includes these source assets and edition state, while downloaded
review exports must be retained separately.

This lesson ends before scheduling or starting the edition. You can now prepare
speech independently of playout. Follow [Prepared show editions](../guides/operations/prepared-editions.md)
for scheduling, re-air confirmation, explicit interrupted-run resume and importing
standalone interviews up to 30:00. An interview's approval review must retain its
full target, even above the ordinary five-minute shortening threshold.

AutoDJ and this page were developed with AI assistance.
