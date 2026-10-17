# Compare voices and save one spoken liner

You will compare two voices reading the same short station ID, save the version
you prefer, and decide separately when it becomes available to liner rotation.
The result is one retained audio asset, not a live dependency on a speech service.

Start with the private Practice station from [Build your first station](first-station.md):
at least five songs, the Practice category and one-step clockwheel, **Clockwheel**
pick mode, **Browser** output and no listeners. Knowing the preview and queue steps
from [the recorded-liner lesson](recorded-liners.md) will help with the final check.

## Before you begin

This worked example uses an **already configured Fish connection** implementing
AutoDJ's `fish-v1` endpoint profile. Arrange with the service operator for a connection
named `Practice speech` and two explicit, usable voice IDs, `practice-a` and
`practice-b`. These names describe the example service's configured voices; they
are not built-in Fish voices. If your operator gives you different connection and
voice names, write those three exact replacements down and use them throughout.

The service must already be reachable and authorized, and you must have permission
to use both voices. This lesson neither installs a model nor configures credentials,
URLs, tunnels or a GPU service. If those prerequisites are missing, finish
[Connecting a GPU speech service](../speech-endpoints.md) with the operator first.
A provider's name in the menu does not establish that its service works.

Generation sends your script to that configured service. **Generate selected samples**
can make two requests and incur provider charges; AutoDJ reports cost as unknown.
Only proceed with a script and service you are authorized to use. Checking and
previewing existing audio do not synthesize speech. No generation is necessary
just to read this lesson.

You will save comparison results and one station-owned liner. You will not create
a time-check set or change the default station voice. Keep **Enable voice liners**
off until the final placement exercise. Pause music with **Play or Pause** and
pause each audition's own audio player when finished. Use **Cancel generation**
if you need to stop outstanding work; a submitted request may already be billed.

Use Tab, Enter and Space to operate buttons and native audio controls. The
[keyboard guide](../guides/keyboard-and-screen-reader.md) covers page navigation.

## Prepare one script and two explicit candidates

1. In **Settings**, **Voice liners**, uncheck **Enable voice liners** if necessary.
2. Expand **Speech studio**. Leave **Speech connections for all stations** alone;
   connection changes affect every station on this server.
3. Enter `This is Practice radio. Stay with us for more music.` in **Script**.
4. In **Comparison voice 1**, keep **Include sample** checked, choose
   `Practice speech` as **Connection**, and enter `practice-a` in **Voice ID**.
5. Press **Add comparison voice**. In **Comparison voice 2**, keep **Include sample**
   checked, choose the same connection, and enter `practice-b` in **Voice ID**.
6. Leave **Generate fresh audio instead of reusing matching samples** unchecked.

Both candidates share one script, making the comparison about voice rather than
wording. In this minimal Fish profile, text and voice are supported; **Model**,
**Speed** and **Delivery instructions** are unavailable. This is expected. An
engine's name does not imply every upstream feature or endpoint is compatible.
Do not change to another engine just to enable more controls.

Check the two connection selections and voice IDs before continuing. If the
connection is missing, stop here and ask the service operator to confirm its
configuration. Creating a new connection in this lesson would skip the prerequisite
and could affect other stations.

## Check before making requests

Press **Check comparison**. Read the text beginning **Sample 1:** and **Sample 2:**.
Each should report **ready** rather than an error. The status says “Comparison
checked. Review the results before generating.” Cost remains unknown.

This check validates the supplied configuration and requested controls; it does
not call synthesis, test pronunciation, prove a voice exists remotely, or establish
live-service readiness. The server may still be unavailable when you generate.

For a worked recovery, suppose one candidate reports that its connection is not
configured or that its voice is missing. Return to that candidate, select the
operator-confirmed connection and enter its exact voice ID. Press **Check comparison**
again. Proceed only once both candidates are ready. If the error concerns the
service configuration itself, leave it to the operator rather than entering a
guessed endpoint or key.

If you accidentally added a third candidate, uncheck its **Include sample** before
checking again. Only the checked candidates belong to the request. This prevents
an unintended extra audition and possible charge.

## Generate and inspect independent results

1. Review the script and the two included candidates one last time.
2. Press **Generate selected samples** once. This is the explicit synthesis action.
3. Read **Comparison results** while generation proceeds. Wait for each candidate
   to reach a final state.
4. For each **succeeded** sample, note its voice ID and duration, then use its
   native audio controls to listen. Pause one sample before starting the other.
5. Compare the spoken words, pronunciation, pace and clarity. Choose one result
   for the Practice station ID.

The text state, duration and capability report let you identify each outcome
without relying on a waveform or color. Listening remains necessary to approve
pronunciation and voice quality. Successful output can be auditioned independently
when another candidate fails. A cached result can say **reused audio**; with the
fresh-audio box unchecked, matching validated output can be reused instead of
requesting it again.

If a result is **failed**, read its error. Keep and audition the successful sample.
Resolve the failed candidate's service problem before pressing **Retry failed sample**
for that candidate only. Retries are explicit; AutoDJ does not silently choose
another provider or a replacement voice. Timeout, cancellation or interruption
can leave a charge even when no usable result appears. Repeatedly pressing Generate
is not a recovery plan.

If you cancel, wait for the results to show their outcome. Cancellation cannot
undo remote work already submitted. An unavailable provider prevents new generation;
it does not make an already saved local liner need the provider for playback.

## Save the audio before assigning it

1. At your chosen successful result, enter `Practice spoken ID` in **Liner name**.
2. Press **Save liner**.
3. Find **Practice spoken ID** under **Saved speech liners** and check its text says
   **Saved; not assigned.**
4. Play its saved audio once and pause it afterwards.

Saving retains the audio and its source text, voice/settings and capability report.
It does not put the file in the ordinary liner rotation, select a default station
voice, enable automatic announcements, or queue anything. Saving is the point at
which this audition becomes a retained station asset.

Unsaved comparison audio expires after seven days and is subject to temporary
storage limits. Save the result you want to keep. If a comparison has already
expired, **Regenerate expired sample** can request it again and incur another
charge; it is not retrieval of the old sound.

## Try it: assign without enabling automatic playback

Predict what will happen if you press **Assign to liner rotation** while **Enable
voice liners** remains off. Will the station immediately announce your ID?

Press **Assign to liner rotation** for the saved Practice spoken ID. Check that
its text changes to **Assigned to liner rotation.** In **Queue & Search**, select
**Voice liners**, press **Search**, and locate the newly available clip. Use
**Track info** if needed to identify it and preview its opening.

Expected answer: assignment publishes the file to the station's normal liner
folder, but does not immediately play it or enable automatic intervals. You can
now explicitly queue it. With an empty queue, press its **Add to queue** once,
resume music, and let the current song finish. The queued liner can play even
while automatic liners are disabled. Observe the queued entry being consumed
and listen to check the voice over the music.

Saving, assignment and playback are three decisions. A **Save voice preset**
action would save reusable voice settings instead; **Use as station voice**
would separately seed future work. Neither rewrites audio already generated.
You have not used either action in this lesson.

## Stop and retain your chosen liner

Pause every audition and preview player. If generation is still running, press
**Cancel generation** and inspect the result before leaving. Clear any remaining
queued Practice liner, leave **Enable voice liners** unchecked, and pause browser
music with **Play or Pause**. Check its elapsed time stops advancing.

Keep **Practice spoken ID** saved and assigned for later practice. If you press
**Remove comparison**, its audition group disappears; saved liners remain available.
No shared connection, credentials, station voice, automatic trigger, or time-check
set was changed. Disabling automatic liners does not cancel a queued liner, so
checking the queue is part of stopping cleanly.

You can now compare explicit voices and preserve one reviewed asset. For interval
placement, use [the recorded-liner lesson](recorded-liners.md); for other production
tasks, use the [speech studio operations guide](../operations.md#speech-studio).
Leave **Generate time checks** alone: generating a full minute set is a separate,
potentially much larger request.

AutoDJ and this page were developed with AI assistance.
