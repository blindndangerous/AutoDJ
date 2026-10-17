# Compare a beatmatched transition

You will inspect two songs and compare the same transition with tempo matching
off and on. By the end, you will know what the beatmatching control changes,
what evidence the page offers, and when to leave it off.

Start with the private Practice station from [Build your first station](first-station.md):
at least five imported songs, the Practice folder category and one-step clockwheel,
**Clockwheel** pick mode, **Browser** output, and no listeners. This lesson briefly
changes output to **Radio stream** so the server can mix the transition. It needs
working FFmpeg stream encoding; follow [streaming setup](../guides/streaming-and-relays.md)
if that is not ready. You need no relay, public listing, or second listening device.
Practice means the default station; its displayed name may still be AutoDJ.
Use that station's row for every Stop and Start in this lesson.

## Before you begin

Choose two steady-tempo songs with clear instrumental openings and endings. Call
them A and B in your notes, recording their real titles. Prefer slightly different
tempos, such as approximately 120 and 124 BPM, within the default 8 percent stretch
limit. Identical tempos make a tempo change harder to observe.

Allow enough time to hear both songs through a natural transition twice. Skipping
is a different operation and will not demonstrate the same overlap. Keep listening
volume low. You can stop at any point using **Stop** for Practice in **Settings**,
**Stations**, then confirm **Stop station**. **Listen here** only controls your
browser's listening connection; stopping that connection is not a station stop.

Use Tab and Enter or Space for controls, and arrow keys in tab strips. The
[keyboard guide](../guides/keyboard-and-screen-reader.md) covers result-grid navigation.
This lesson saves transition settings but does not require editing shared song cues.

## Inspect the two songs

1. Pause browser playback in **Now Playing** with **Play or Pause**.
2. In **Queue & Search**, search for A and press its **Track info** button.
3. Read **BPM**, then expand **Cue points**. Inspect **Start time**, **Intro time**,
   **Segue time**, and **End time** without changing them.
4. Press **Preview Start** and **Preview Intro** separately. Pause **Song point preview**
   before continuing. These controls audition the file locally; they do not queue it.
5. Expand **Song structure**. Read its timed entries, especially intro, outro and
   any phrase or bar information. Repeat for B.

Start is where playback begins; Intro marks the end of the instrumental opening;
Segue marks the handoff; End bounds playback. A beat grid describes detected beats.
The text structure list exposes landmarks and any available bar numbers; it is
not an editor for individual beats. Bar labels are evidence of structure data,
not proof that every detected beat is correct.

If structure is missing, open **Library tools**, expand **Library tools**, check
**Find beats and song structure**, and press **Rescan**. Wait for the job in
**Library jobs** and **Output** to finish, then reopen Track info. Basic analysis
prepares playback points; song-structure analysis supplies beats and phrases.
**Perform advanced analysis** is separate: it supports similarity, key and energy
and can estimate missing BPM. It is not a prerequisite for a folder clockwheel
or a song that already has usable tempo and structure data.

If BPM remains unavailable, choose another song with known tempo rather than
inventing a value. A typed BPM does not manufacture a trustworthy beat grid.
For drifting performances or obviously wrong landmarks, choose another pair for
this worked comparison. See [music analysis](../guides/add-your-music.md#what-happens-next).

## Prepare a plain transition

Record the original settings before changing them so you can restore them later.

1. In **Settings**, **Transitions**, set **Transition mode** to **Fixed (skip silence)**
   and **Crossfade seconds** to `8`.
2. In **DJ mixing**, uncheck **Beatmatch**, **Phrase align**, **Drop mixing** and
   **Key shift** where available. In **Transitions**,
   set **Transition effect** to **none** so it does not distract from this comparison.
3. In **Voice liners**, leave **Enable voice liners** unchecked.
4. In **Settings**, **Stations**, set **Playback output** to **Radio stream**.
   Leave **Also play the radio stream on the server sound card** unchecked.
5. Press **Save station options**. In Practice's station row press **Stop**,
   confirm **Stop station**, wait for **Off air**, then press **Start**.

Transition controls save on change. Output has two boundaries: **Save station options**
saves the choice, and restarting the station applies it. The **Beatmatch** option
in **DJ mixing** serves server mixing. Browser **Beatmatch on skip** is a separate
control and is not used here. **Use saved segue points** would bypass these mixing
options, which is why the lesson chooses another transition mode.

Check that Radio stream remains selected and **Listen here** is available in
**Now Playing**. If the station fails to start, read its error and resolve the
encoder problem before continuing. Do not interpret a saved output choice as a
working encoder.

## Run A into B without beatmatching

1. In **Now Playing**, press **Listen here**; it becomes pressed.
2. In **Queue & Search**, use **Clear queue** and confirm if entries remain.
3. Search for B and press **Add to queue** once. Confirm B is queued.
4. Search for A and press **Play now**. Queued B stays queued after this jump.
5. Check A's title in **Now Playing**, then let A finish naturally.
6. Check B's title and **History**. Listen through the overlap if you can.

You have fixed the order and heard the plain overlap. Stream audio reaches your
browser after buffering, so the title change and sound need not happen together.
Do not use that delay as a measurement of beat accuracy.

If another song appears, check that B was queued before playing A and repeat
from the cleared queue. An empty queue returns to the Practice rotation; its
continued operation does not establish that your chosen comparison ran.

## Repeat with beatmatching

1. Stop listening with **Listen here**, then stop Practice in **Settings**, **Stations**.
2. In **DJ mixing**, check **Beatmatch**, set **Beatmatch stretch limit (percent)**
   to `8` and **Glide back (bars)** to `8`. Uncheck **Drop mixing**,
   and leave **Phrase align** and **Key shift** off.
3. Start Practice again. Restarting avoids comparing a transition rendered before
   you changed the setting.
4. Repeat the complete A-into-B procedure above, including clearing the queue,
   queueing B, playing A, and listening through A's natural finish.

With a suitable pair, the server adjusts B's tempo during the overlap and eases
it back afterwards. The page can show **Beatmatch** with a numeric ratio, and the
track announcement can say “beatmatched” with a ratio. A small or absent ratio
is possible when little stretch is needed. These are evidence of the applied
plan; listening still matters for judging the musical result.

Steady trusted grids can align downbeats as well as tempo. With insufficient grid
data, matching can fall back to BPM-based stretching. Unknown tempo or a change
outside the limit can leave a plain transition. The switch is not a promise of
perfect detection, alignment or sound quality. If the overlap sounds worse, turn
Beatmatch off and choose a better pair rather than widening the limit blindly.

## Try it: remove the allowed stretch

Predict what will happen to the same unequal-tempo pair if **Beatmatch stretch
limit (percent)** becomes `0`. Stop Practice, change that one value, restart,
and repeat A into B.

Expected result: a pair requiring nonzero stretch no longer receives that tempo
adjustment. Exact or half/double-time matches are a special case. Explain the
result using the permitted stretch, rather than assuming the checkbox always
forces a change. Restore `8` after checking your answer.

## Stop and restore Browser output

Stop **Listen here** if pressed. Stop Practice and confirm **Off air**. Restore
the transition settings you recorded, select **Browser** output and press
**Save station options**. Start Practice, then pause browser playback with
**Play or Pause** if needed. Check that elapsed time stops advancing.

Your category, clockwheel, songs and analysis remain. No cue edits were necessary;
any cue edits you choose to save later belong to the shared library and can affect
other stations. Use [Build your sound](../guides/build-your-sound.md#set-the-transitions)
for further mixing tasks and the [settings reference](../settings-reference.md)
for control ranges.

AutoDJ and this page were developed with AI assistance.
