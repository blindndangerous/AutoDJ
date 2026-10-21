# Build your sound

Choose how AutoDJ selects music, then shape the transitions. You can use similarity picking for a flowing set, a clockwheel for a planned rotation, or a queue for a show. These choices work together.

## Choose how songs are picked

1. Open **Settings**, then **Song picking**.
2. Set **Pick mode**:
   - **Similarity (default)** follows the sound of the current song. It needs advanced analysis for the songs it picks.
   - **Random walk (uniform random)** draws from eligible songs without following their sound.
   - **Least recently played** favors songs that have waited longest.
   - **Clockwheel** follows the active clockwheel's steps.
3. Set **Prevent the same artist from repeating within**, **Prevent the same track from repeating within**, and any other repeat rules you need. A value of 0 turns that limit off.
4. Press **Save repeat settings** in **Repeats** after changing repeat rules. Open **Test rotation** and press **Test rotation** to inspect future picks before going on air.

The queue takes priority over automatic picks. **After queue empties, seed from** decides whether similarity continues from the last queued song or returns to the song playing before the queue. **Inject distant tracks** occasionally moves a similarity set away from its current sound.

AutoDJ applies repeat rules to automatic picks and clockwheel steps that opt into them. If too few songs qualify, it relaxes rules for that pick so playback can continue. A large, varied library makes repeats less likely. [Add your music](add-your-music.md) explains analysis and library size.

## Make a category

A category is a named pool of songs for a clockwheel step. In **Library tools**, open **Categories** and choose **Add category**.

1. Enter **Name**, such as Evening Gold.
2. Under **Songs come from**, choose **Matching conditions** for fields in the library, **A playlist file** for an M3U or M3U8 file, or **A beets query** if you use beets.
3. Set **When the songs may play** if the pool belongs to certain days, hours, or dates.
4. Save the category and check its song count. If it is empty, check the source and the library tags before adding it to a clockwheel.

Playlist files live in the shared playlists folder by default. Categories themselves belong to the station you are editing. A second station can use the same music and playlist files while keeping a different rotation.

## Build a clockwheel

A clockwheel repeats an ordered list of steps. Each step can choose from a category, choose music another way, play a liner, or change a preset. In **Library tools**, open **Clockwheels**.

1. Enter a **Name** and create the clockwheel.
2. Add a **Music matching conditions** step, then add a **Category** condition to choose its pool.
3. For a category step, set **Choose** to **Least recently played** or **At random**. Turn on **Follow the repeat rules** unless the step must play a specific song.
4. Save the clockwheel and activate it. Set **Pick mode** to **Clockwheel** in **Settings**, **Song picking**.

Use **Restart at these minutes past each hour** to realign a clockwheel at fixed points. **Fit songs to the hour** in **Song picking** can choose a song that ends close to the next restart or scheduled event. A step with no eligible songs is skipped, so check category counts and time restrictions if the rotation sounds incomplete.

If you already have a SAM Broadcaster clockwheel, **.PAL file to import** can create a starting clockwheel. Review the imported steps and categories before using it on air.

## Set the transitions

In **Settings**, **Transitions**, choose **Transition mode**. The default uses detected intros and outros. **Crossfade seconds** sets a fixed overlap where the mode uses one; 0 makes a cut in fixed mode. **EQ-ducked crossfade** reduces overlapping bass. In the separate **DJ mixing** tab, turn on **Beatmatch** or **Phrase align** when songs have trustworthy BPM and beat grids. **Harmonic mixing** is in **Song picking** and needs known keys. Songs without that data still play; the affected transition falls back to simpler timing.

In **Settings**, **Sound**, choose **Output processing** for loudness. **Off** leaves levels alone. **ReplayGain** uses available loudness data. **Smooth** levels songs in AutoDJ. **Stereo Tool** requires its separate plugin library and terms; download it from this panel. Use **Music speed (%)** only if you want a station-wide speed change.

The separate **Time checks** and **Breaks** panels control spoken time and DJ breaks. [Settings reference](../settings-reference.md) lists their defaults and limits.

## Add voice liners

Voice liners are short station IDs, jingles, and announcements. Open **Settings**, **Voice liners** and tick **Enable voice liners**. Use **Choose liner file**, then **Upload**. To replace a file with the same name, tick **Replace existing file** first. You can also register a whole voice liner folder in **Library tools**. **Browse voice liners** in **Queue & Search** lists the files.

Set one or more triggers: **Every N tracks**, **Every N minutes**, or a **Random window** with minimum and maximum minutes. Blank or 0 leaves a trigger off. The random minimum and maximum must both be above 0. **Rotation mode** chooses **Random** or **Sequential**; sequential follows file name order. **Music duck (dB)** lowers music under the clip. In server saved-point playback, automatic liners use their saved cue points across the outgoing Outro and incoming Intro, just like queued liners. **Talk up to the vocal (live server overlays)** controls non-saved-point liners and live time checks; browser playback ignores it. Press **Test now** to hear one before leaving liners on. For a liner at a specific time, use a [Schedule event](schedule.md#schedule-music-liners-and-announcements).

## Listen before going on air

Queue several songs from each category and listen through the handoffs. Check **History** to see what played, then adjust the category, repeat rule, or transition that caused a problem. Use **Test rotation** after changes to catch empty pools and unexpected repeats.

## Related pages

- [Add your music](add-your-music.md)
- [Run several stations](several-stations.md)
- [Settings reference](../settings-reference.md)

AutoDJ and this page were developed with AI assistance.
