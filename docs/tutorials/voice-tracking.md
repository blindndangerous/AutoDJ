# Record a voice track and place it between songs

You will record a short DJ break in the browser, listen privately, and explicitly
book it into Practice playback. You will use the booking state to distinguish a
recorded take from audio committed to the station.

Start after [Build your first station](first-station.md), with its saved Practice
category and clockwheel and at least five of your own songs. Practice means your
default practice station; its displayed name may still be **AutoDJ**. Start with
**Browser** output; you will switch to **Server sound card** below. Keep
**Clockwheel** pick mode and the Practice wheel active. Use a private practice
installation with no listeners or relay destinations.
Do not follow these booking steps on a live service.

## Before you begin

You need a working microphone and a browser with microphone recording support.
Open AutoDJ on its own computer at `http://localhost:<port>`, using your actual
port, or use an existing HTTPS connection. A plain HTTP LAN address cannot record.
The signed-in browser session is sufficient; no announcer token, generated
speech provider, optional practice pack or advanced similarity analysis is needed.

Use headphones for browser previews. In **Settings**, **Sound**, choose them as
**Preview output device** if your browser offers that choice; otherwise arrange
for the system default to use headphones. Keep the server speakers low or use a
separate headphone route so they do not leak into the microphone. Preview routing
depends on browser/device support; see [feature availability](../reference/feature-availability.md).

This lesson saves output, transition and break settings. **Record** captures a take in
the page; **Listen back** auditions it locally. **Book voice track** uploads the
take and commits it to station playback, which would include the broadcast on a
streaming station. Booking is the explicit publication step. The saved take is
not a reusable liner asset: the uploaded break is temporary and removed after
it airs, is dropped or expires.

Press **Record** again, or **Alt+R** while Voice tracks is open, to stop recording.
Leaving the tab or hiding the page also stops capture and releases the microphone,
keeping the take. **Listen back** toggles preview playback. For a complete stop,
use the Practice row's **Stop** in **Settings**, **Stations**. Stopping the station
is different from muting your listening device. Use the
[keyboard guide](../guides/keyboard-and-screen-reader.md) for tab and grid navigation.

## Move Practice playback to the server sound card

1. Confirm Practice is the selected station. Open **Settings**, **Stations**.
2. Under **Output for the selected station**, set **Playback output** to
   **Server sound card** and press **Save station options**.
3. In **Stations on this server**, find the Practice row, using its actual
   displayed name. Press **Stop**, then confirm with **Stop station**. If its
   row already offers **Start**, it is stopped; skip the stop action.
4. Wait until the row offers **Start**, then press it. Check the status reports
   that the station started and read any warnings.
5. Open **Now Playing** and check the song title. Confirm music plays through
   the AutoDJ computer's sound card.

Saving the output alone does not switch a running station. If startup fails or
sound is absent, stop Practice and use [playback troubleshooting](../guides/troubleshooting.md#playback-and-sound)
before continuing. Browser sound is not evidence that server audio works.

## Open a place for the break

A **voice track** is a recorded DJ break booked for a particular song change.
The **open song change** is the transition still accepting breaks. It is after
the next song because the nearer transition is already prepared.

1. In **Settings**, **Transitions**, set **Transition mode** to **Use saved segue
   points**. This control saves on change; move focus away and recheck the value.
2. Open **Voice tracks**. Read **Ending song**, **Next song**, **Time left to
   book** and **Airs in about** under **Open song change**.
3. Choose **Standalone, between the songs** in **Placement**. This avoids
   depending on any particular intro length for your own music.

Checkpoint: with server audio and saved-point playback, the song-pair facts
are present and the tab is accepting a take even when announcer uploads are off.
The ending song shown here need not be the song playing right now. If the tab
requires server playback and saved-point playback, recheck the transition mode
and output. If no
change is open yet, let the station prepare songs and check again after a few
seconds. Use [voice tracking procedures](../operations.md#voice-tracking-from-the-browser)
if the explanation remains. Do not record against an unavailable slot.

## Capture and privately check one take

1. Choose the intended input in **Microphone**. AutoDJ remembers the choice in
   this browser. **Refresh microphones** updates the list after connecting or
   removing a device. If names are hidden, allow microphone permission; numbered
   inputs can still be selected. Use **Test microphone** to hear a private live
   monitor through **Preview output device**. Press **Stop microphone test** when
   finished, and use headphones to avoid feedback.
2. Press **Record**. If the browser asks, allow microphone access for this site.
3. Say “This is Practice radio. More music after this.” Keep the take to a few
   seconds, then press **Record** again.
4. Read the microphone status and timer. The status reports the active input
   when the browser provides its track device settings; otherwise it says that
   the browser cannot confirm it. The timer should describe a recorded duration and say **not booked
   yet**. The browser's microphone indicator should no longer show active capture.
5. Press **Listen back**. Listen for your complete sentence. Press it again if
   you want to stop the preview before it ends.
6. Check **Booked voice tracks**. Your local take has not created a new booking.

The timer and absence of a booking establish the private state without relying
on sound. Listening establishes whether the microphone captured usable speech.
If the browser blocks recording, allow microphone access in that site's browser
permissions and press Record again. If the selected microphone is missing or
unavailable, choose a different input or **System default**; an explicit choice
is never silently replaced. On an insecure page, reopen using the secure or localhost
address from the prerequisites; permissions alone cannot fix an insecure context.

If the take is silent or clipped, check the input device and record again. A new
recording replaces the old local take. **Discard recording** removes it without
booking anything. Recording has a three-minute browser ceiling and can stop
sooner under station limits; this short example should not approach either.

## Explicitly commit the take to Practice

1. Re-read the current **Ending song** and **Next song**. The pair and deadline
   may have moved while you listened.
2. Confirm **Placement** is **Standalone, between the songs**.
3. Press **Book voice track** once. This uploads and authorizes the take to play
   on Practice's server output. It would be on air for stream listeners if this
   were a streaming station.
4. Find the new entry under **Booked voice tracks**. Read its state and song-pair
   label. It may already say mixed in rather than waiting.
5. Leave the station playing until that entry reaches **aired**, then inspect
   the music titles in **Now Playing** and **History** around the change.

You should hear the sentence between those songs on the server output. The
booking state is the text check of its lifecycle; it cannot prove that speakers
were audible or the recording intelligible. A dropped or expired state includes
a reason and is not an aired result.

If the pair changed before booking, the page can refuse the stale take and tell
you the new pair. For this general sentence, press **Listen back**, confirm it
still fits, then press **Book voice track** again for the current pair. If you
named a song that no longer fits, record a new take instead. If the slot closes
again, wait for a fresh one with enough time; do not keep submitting blindly.
For quota or rate-limit errors, follow the displayed reason and wait rather than
making duplicate uploads.

## Try it: stop before the publication step

Predict what will happen if you record a second short sentence, listen back,
then press **Discard recording** without booking. Do that now and inspect the
timer and **Booked voice tracks**.

Expected result: the timer returns to **Not recording**, and no new booked entry
appears. The earlier aired entry can remain in the list. You have removed the
local draft, not cancelled or replayed the earlier booking. If a second booking
appears, check whether you pressed **Book voice track**; **Cancel** can drop a
booking only while it is still waiting, not once mixed into prepared audio.

## Stop and retain the station settings

Stop any active recording with **Record** and any preview with **Listen back**.
Discard any remaining local take. In **Settings**, **Stations**, use **Stop** on
the Practice row, confirm with **Stop station**, and wait for its button to
change to **Start**. Closing the browser alone does not stop server playback.

The music, category and clockwheel remain saved, as do Server sound card output
and saved-point transition mode. A local unbooked take is not a
saved liner. To reuse the earlier Browser lessons:

1. In **Settings**, **Transitions**, set **Transition mode** to **Full intro and
   outro**; it saves on change. Move focus away and recheck the value.
2. In **Settings**, **Stations**, set **Playback output** to **Browser** and
   press **Save station options**.
3. In the Practice row, press **Start**. Check for a successful start, then use
   **Play or Pause** in **Now Playing** if needed to leave browser playback paused.

For a reusable spoken asset, follow [the recorded liner lesson](recorded-liners.md).

AutoDJ and this page were developed with AI assistance.
