# Build your first station

You will turn a small folder of music into an automatic rotation. By the end,
you will have heard a queued song and used a category and clockwheel to choose
what plays next.

Start with AutoDJ installed and open in a local browser. Use a fresh, empty
station with **Playback output** set to **Browser**, and no listeners.
If you have not installed AutoDJ, follow [Install AutoDJ](../guides/install.md)
first. This lesson changes saved station settings; do not follow it on a live station.

[No-model setup](../setup.md#no-model-installation) supports this lesson's browser
output, basic import and category rotation. Local model analysis is not required.

In this series, Practice means your default practice station. Its displayed name
may still be AutoDJ; note that name when choosing its row in Settings. The
category and clockwheel you create below are both named `Practice`.

## Before you begin

Prepare a folder containing at least five MP3, FLAC, or M4A songs on the computer
running AutoDJ. Choose songs you can recognize. You will use your own titles;
their exact order is not prescribed.

You will register this folder, save a category named `Practice`, and save a
clockwheel named `Practice`. Importing does not move your music files. The saved
example remains available for later lessons.

Use a comfortable, low listening volume. In browser playback, **Play or Pause**
on **Now Playing** pauses the music; press it again to resume. You can pause
whenever you need time to read.

The lesson moves through importing music, hearing one song, building a song
pool, and running a rotation. Each stage includes a result to check.

Use Tab to reach controls and Enter or Space to activate buttons. In a tab
strip, use Left Arrow and Right Arrow to change tabs. Expand a named section
before using its controls. The [keyboard and screen-reader guide](../guides/keyboard-and-screen-reader.md)
explains grid navigation and screen-reader modes. You do not need single-letter
shortcuts to complete the lesson.

## Add your practice music

1. Open **Library tools** and expand its **Library tools** section.
2. In **Folder path**, enter the full path of your prepared folder.
3. Leave **Folder type** set to **Music** and **Add subfolders** checked.
4. Uncheck **Perform advanced analysis** for this lesson.
5. Press **Add folder**.

The page reports “Folder added; import queued” and lists the folder. Wait for
the library job to finish; use **Library jobs** and **Output** to inspect progress.
Basic analysis prepares songs for automatic playback. Advanced analysis finds
similar-sounding songs, but this lesson chooses from a named pool instead.

Check that the folder is listed and the job finishes without an error. If the
folder cannot be read, confirm the path exists on the AutoDJ computer, rather
than another device. Correct the path before continuing. For other import
failures, use [music troubleshooting](../guides/troubleshooting.md#music-and-the-library).

## Hear a song you choose

1. Open **Queue & Search**.
2. Leave **Search text (optional)** empty and press **Search** to browse songs.
3. Find a song you recognize. Use its **Add to queue** button.
4. Confirm that its title appears in the queue.
5. Find a different song in the search results and press its **Play now** button.
6. Open **Now Playing**. Check its title and listen. Let this song finish;
   the song you queued should play next. Check its title too.
7. While the queued song plays, press **Play or Pause** to pause while you build the rotation.

The title and playback state let you inspect the result without relying on
audio alone. If the title changes but there is no sound, check the computer's
audio output, the page's volume and mute controls, and whether playback is paused.
Use [playback troubleshooting](../guides/troubleshooting.md#playback-and-sound)
if it remains silent.

## Create the Practice song pool

A **category** is a named pool of songs. Practice will contain songs from your
registered folder, so this example does not depend on genre tags or advanced analysis.

1. In **Library tools**, expand **Categories** and press **Add category**.
2. Enter `Practice` in **Name**.
3. Under **Songs come from**, choose **Matching conditions**.
4. In the existing **Condition 1**, set **Condition type** to **Folder**.
5. Choose your practice folder in **Library folder**. Leave **Subfolder** empty.
6. Check **Include subfolders** and leave **Operator** set to **is**.
7. Leave the days, times, and dates under **When the songs may play** unset.
8. Press **Check**. Confirm that the matching count includes your songs.
9. Press **Save category**. Confirm that `Practice` appears in **Saved categories**.

If the count is zero, check the selected library folder and wait for basic
analysis to finish. Press **Check** again. Do not build the next stage around
an empty category.

## Build and activate the Practice rotation

A **clockwheel** repeats an ordered list of steps. This first wheel has one
step: choose a song from Practice, then repeat that choice for the next song.

1. In **Library tools**, expand **Clockwheels**.
2. Enter `Practice` in **Name** and press **Create**.
3. Press **New step**. In the dialog, choose **Music matching conditions** as **Step type**.
4. In the existing **Condition 1**, set **Condition type** to **Category**.
5. Set **Music category** to **Practice** and leave **Operator** set to **is**.
6. Set **Choose** to **Least recently played**.
7. Uncheck **Follow the repeat rules** for this small practice pool.
8. Leave day and hour restrictions unset. Press **Add step** to close the dialog.
9. Clear **Restart at these minutes past each hour**. The wheel will repeat when it wraps.
10. Press **Save clockwheel**, then **Activate**.
11. Open **Settings**, **Song picking**. Set **Pick mode** to **Clockwheel**; this control saves on change.

Confirm that the clockwheel has one saved step, is active, and **Pick mode**
remains **Clockwheel** when you return to Settings. Creating a wheel alone does
not make it play: saving, activating, and selecting the pick mode have separate jobs.

If activation asks you to save changes, return to **Clockwheels** and press
**Save clockwheel** before **Activate**. If no songs match the step, edit it and
confirm its **Music category** is **Practice** with no time restrictions.

Skipping repeat rules keeps this tiny example usable. A real station needs a
larger pool and deliberate repeat rules; see [Build your sound](../guides/build-your-sound.md).

## Check automatic playback

Open **Queue & Search**. If anything remains in the queue, use **Clear queue**
and confirm the action; it removes queued entries, not your music files.

Return to **Now Playing** and resume with **Play or Pause**. Let the current
song finish, then let another song finish. Check the changing titles and open
**History** to confirm what played. With the queue empty, the active clockwheel
supplies automatic picks from Practice. A title changing alone does not prove
which picker supplied it; check the empty queue, active wheel, and pick mode too.

If playback does not continue, pause and check that basic analysis finished,
the category has songs, and the wheel is saved and active. Check **Pick mode**
again before resuming. Do not wait for advanced analysis; it is not required here.

## Try it: put a song ahead of the rotation

While music plays, predict what will happen if you add one different song to
the empty queue. Will the clockwheel choose the next song, or will the queue?

In **Queue & Search**, find that song and press **Add to queue** once. Confirm
its title is queued. Let the current song finish and check the next title.
Then let the queued song finish and inspect the queue and next title again.

Expected result: the queued song takes priority. Once it has played and the
queue is empty, automatic picking continues from Practice. The clockwheel
stays active; adding a song does not replace it. If the result differs, inspect
the queue for additional entries and repeat the check from an empty queue.

You have tested two different sources for the next song: a manual queue entry
and an automatic choice from a category.

## Stop and keep your station

In **Now Playing**, press **Play or Pause** while playing. Check that the state
is paused and the elapsed playback time stops advancing. This stops browser
playback for this lesson; it does not shut down the AutoDJ application.

Keep the music folder, Practice category, and Practice clockwheel for later
lessons. Their settings remain saved, including Clockwheel pick mode and the
step's disabled repeat rules. No deletion is needed. To remove the imported
folder later, use [Change or remove a folder](../guides/add-your-music.md#change-or-remove-a-folder).

You can now build a song pool, run an automatic rotation, and explain queue
priority. Next, [shape a rotation with two categories](two-categories.md)
to extend Practice with an alternating rotation. Use the
[sound guide](../guides/build-your-sound.md) for individual adjustments or the
[settings reference](../settings-reference.md) to look up a control.

For background, read [how selection works](../explanation/selection.md).
Before using a practice setup regularly, follow
[Prepare a station](../guides/prepare-station.md).

AutoDJ and this page were developed with AI assistance.
