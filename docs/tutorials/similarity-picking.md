# Follow a song with similarity picking

You will change Practice from a clockwheel rotation to similarity picking and
observe an automatic follow-up. You will then test whether a manual queue entry
still takes priority. Your result is a verified control flow, not a prescribed
pair of songs or a promise that you will like the transition.

Start after [Build your first station](first-station.md), with its default
practice station, music folder, Practice category and Practice clockwheel saved.
Practice is our example name; the station may still display **AutoDJ**. Keep
**Playback output** set to **Browser**, with no listeners. Pause playback while
preparing the lesson. The first lesson's music is your own audio; the optional
practice pack is not used here.

## Before you begin

Use at least five of your own music files, each shorter than the configured
advanced-analysis length limit (15 minutes by default). You need permission to
use them and to use the analysis model. The current MuQ weights use a
CC-BY-NC-4.0 license; consult [model details](../contributor/model-selection.md)
and the [setup guide](../setup.md) before running analysis. This lesson does not
switch models or establish commercial-use permission.

The preparation steps below run local model analysis. If you chose no-model
setup, first [enable a model-capable runtime](../setup.md#no-model-installation),
then return to this lesson. Existing compatible analysis remains usable in
no-model mode, but it does not make that mode able to analyse new songs locally.

**Advanced analysis** saves information about how each song sounds so the picker
can compare it with other songs. The first run may download model files and
requires network access; later cached runs can still be slow. Hardware, library
size and track length affect time and memory. There is no fixed completion time.
The job changes shared library analysis, and this lesson saves station picking
settings. Choose a practice server whose shared library you may change.

You can stop analysis with **Stop running job** in **Library tools**. Completed
analysis is retained, and a later run can work on remaining songs. Pause browser
playback with **Play or Pause** in **Now Playing** whenever you need reading time.
Use Tab and Enter or Space for controls, and arrow keys in tab strips; see the
[keyboard guide](../guides/keyboard-and-screen-reader.md).

## Prepare the sound comparisons

1. Open **Library tools** and expand its **Library tools** section.
2. In the registered folders table, find your practice music folder. Check its
   **Perform advanced analysis** checkbox. This row setting saves on change;
   wait for “Folder settings saved.”
3. Under **Library jobs**, leave **Advanced analysis limit (optional)** empty.
4. Press **Import and analyze** once. Read **Output** and the job status until
   the job finishes.
5. Open **Queue & Search**, press **Search** with no text, and open **Track info**
   for each of your five practice songs. Check **Advanced analysis** says
   **Complete**, using **Return** to go back to the results each time.

Check that analysis has completed for your practice songs, not merely that the
songs exist in search. Basic import finishes before advanced analysis. A job
that ended with skipped files has not analysed those files successfully.

If a job is already running, let it finish before starting another. If analysis
fails, read the reported error, use [music troubleshooting](../guides/troubleshooting.md#music-and-the-library),
and retry after correcting the problem. For a song excluded by the length
limit, use a shorter practice song. Do not continue on the assumption that a
playable song must have sound comparisons.

## Give the picker one clear task

1. Open **Queue & Search**. Use **Clear queue** and confirm if entries remain.
2. Open **Settings**, **Song picking**. Choose **Similarity (default)** in
   **Pick mode**. These picking controls save on change.
3. Set **Preset** to **(none)**, **Choose among the closest songs** to `1`, and
   **Harmonic mixing** to **off (no key filter)**.
4. Uncheck **Anchor to seed** and **Fit songs to the hour**. Under **Discovery**,
   uncheck **Inject distant tracks**. Under **BPM range**, leave **Minimum** and
   **Maximum** empty.
5. Return to **Pick mode** and confirm it still says **Similarity (default)**.

A **seed** is a song used as the reference for similarity choices. With anchoring
off, ordinary similarity follows the changing selection. Keeping the closest-song
count at 1 makes the ranking easier to explore; changing **Pick randomness** alone
has no effect while that count is 1. Repeat rules still matter, and a small pool
can make AutoDJ relax them. This exercise does not prove a rule was enforced.

If you cannot find the extra similarity controls, recheck **Pick mode**. The
Practice clockwheel remains saved and active, but it no longer drives automatic
picks while the mode is Similarity.

## Observe one automatic follow-up

1. In **Queue & Search**, press **Search** with no text and find one analysed
   practice song you recognize. Press its **Play now** button once.
2. Open **Now Playing**. Read the playing title and expand **Up Next** to inspect
   the following title and any problem message.
3. Let the song finish. Read the new title, then open **History** and confirm
   the handover. Listen too if useful; the title and history are the text checks.

With an empty queue and Similarity selected, you have asked the automatic picker
to follow that song. Its choice depends on your library and rules; another
reader can get a different title. A fallback or problem message means ordinary
similarity did not succeed. Pause, inspect that message and the analysis report,
then repeat from an analysed seed after resolving it. A title changing alone
is insufficient evidence that the similarity path succeeded.

If there is no sound, check paused state, local volume and output device using
[playback troubleshooting](../guides/troubleshooting.md#playback-and-sound).

## Try it: predict a queued interruption

While a song plays, find a different song and press **Add to queue** once.
Predict whether the queued song or the normal similarity choice comes next.
Confirm the queue contains only your deliberate choice, then let playback reach
it. Check its title in Now Playing and History.

Expected result: the queued song takes priority. When the queue empties,
automatic picking resumes. The **After queue empties, seed from** setting decides
whether similarity resumes from the last queued song or the song before the
queue; inspect that setting before predicting the next reference. If your queued
song does not arrive, inspect extra queue entries, the already prepared handover
and any reported restriction before repeating the exercise.

## Stop and keep the comparisons

Pause with **Play or Pause** and confirm the elapsed time stops advancing. Wait
for any running library job to finish, or use **Stop running job** if necessary.
Your files, completed analysis, Practice category and clockwheel remain saved.
Similarity and the controls you changed remain saved too. To return to the first
lesson's rotation, set **Pick mode** back to **Clockwheel**; the Practice wheel
should still be active.

You can now distinguish an analysed similarity choice from a queued override.
Read [how selection works](../explanation/selection.md) for the mental model or
[Build your sound](../guides/build-your-sound.md) for individual adjustments.

AutoDJ and this page were developed with AI assistance.
