AutoDJ is the basis for this station. The station-playout branch adds folder management, editable song points, the agreed transitions, Smooth and Stereo Tool processing, and a rolling clockwheel. The server makes one continuous mix; the browser controls and monitors it. Use stream mode for this setup.

On this machine, open [AutoDJ](http://127.0.0.1:8081). To start it again from this folder, run `.venv\Scripts\python.exe -m autodj serve --stream --no-server-audio`. In the browser, use Listen to hear the stream. Keep playing without listeners controls whether the station continues after you stop listening.

The sound references are the B and C versions of the [latest listening examples](playout-round9/index.html): B uses Smooth; C uses Stereo Tool. The [saved track points](playout-cues.md) and [earlier research](playout-research.md) remain separate.

The [station test results](playout-test-results.md) include the imported folders, verification results and a recording of the running station.

The everyday workflow is:

1. Open Library, then Library tools. Enter a Folder path on the computer running AutoDJ, choose Music, and select Add folder. This first version adds folders; individual music files can follow.
2. Add subfolders and Perform advanced analysis both start checked for music. Each folder remembers its choices. New songs become available after the short read that finds tags and basic points. They can play in Random walk or the queue while detailed analysis is pending.
3. Audio preparation and advanced matching run in the background. Similarity needs matching data. Random walk, Least recently played and Clockwheel use the whole playable library. Selecting an uncached song for Smooth or Stereo Tool playback also prepares it, which can make the first start slower.
4. Choose Voice liner as the folder type to add IDs, voice tracks or jingles. Use the same folder controls, without advanced music analysis. For C:\station\drops\drops, leave Add subfolders unchecked. Add C:\station\drops\sweepers separately. The existing upload button still writes to its original liner folder.
5. Use Rescan after adding or moving files. Overlapping folder registrations do not duplicate a file. A confirmed move of an unchanged recording retains its points, history and analysis. Remove stops using that folder without deleting files or saved information. A file covered by another registered folder stays available.
6. Use Edit song points in Now Playing or Edit points beside a search result to adjust timing. The separate audio preview has Preview buttons for each point and Set to current position. Previewing does not seek the station. Saving points does not repeat analysis.
7. Start with Random walk to audition the library. Then choose Clockwheel to run the imported plague2024.pal rotation. Keep playing without listeners lets the station continue when the browser closes.

There is no required cue-review step. AutoDJ fills points it can find and labels their source in the editor. The first timed lyric can supply Intro; the last lyric and its estimated duration can help with Outro. Existing loudness detection supplies rough suggestions when lyric timing is unavailable. These suggestions take effect immediately, as agreed. They can be wrong about vocals, so adjust them when you hear a clash. Manual and imported points take priority over later analysis.

Songs keep five points:

- Start is the first audio to play, after unwanted leading silence.
- Intro is where the first words begin. An empty Intro allows no talk-over space at the opening.
- Outro is where a liner may begin over the ending. An empty Outro allows no early talk-over space.
- Segue is where the next item enters. If empty, use Outro, then End. A later Outro does not move a saved Segue.
- End is the last wanted audio, including any tail worth keeping.

Liners use Start, End and one additional field, Overlap start. At 0, fit the complete recording between the outgoing Outro and the next song's Intro. The liner starts no later than the new song. A short liner does not pull the song ahead of its Segue; a long one can delay it. Internal pauses remain intact. A ten-second ID with a five-second incoming intro can therefore start the song five seconds into the ID. A cold intro waits until the words finish.

A positive Overlap start is an explicit release point, measured from the liner's selected Start. Start the liner at the outgoing song's Segue, then bring in the next song after that delay. The rest of the liner plays over it without being faded or cut. Plague uses 5.3 seconds, keeping its reverb through End at about 8.809. Stingers 2 through 5 use 0. The sweeper uses its full retained length so it finishes before music starts. There are no separate ID, jingle and voice-track timing modes.

The station settings are:

- Pick mode: Random walk for the first listening test, then Clockwheel. Least recently played is also available on its own. Similarity retains its matching-based behavior.
- Tracks before a song can repeat and Tracks before an artist can repeat: keep the existing controls. Random walk, least-recent selection and clock slots marked EnforceRules use them. PAL slots marked NoRules retain that choice.
- Transition mode: Use saved segue points. Selecting Clockwheel also selects this mode. The older modes remain available.
- Crossfade seconds: 1.5. The exponential fade starts 0.5 seconds before Segue; the new item enters at Segue at full prepared level, with up to 1 second of the old song underneath. Changing this setting scales the timing. A short file tail limits overlap.
- Fade-in seconds: retained for older modes and hidden for saved-point playback.
- Transition effect: none. EQ-ducked crossfade stays off. Beatmatch, Drop mixing, Key shift, Phrase align, Outro / intro align, Filter sweep and Keep vocals apart do not override saved-point playback. Controls are hidden when they do not apply; their values remain available for older modes.
- Voice liners: retain the existing enable switch, Every N tracks, Every N minutes, random-window triggers and Random/Sequential rotation. These choose automatic liners outside Clockwheel. Clockwheel takes its liners from the clock slots.
- Talk up to the vocal: on. Saved-point playback uses the complete-liner timing above, including the outgoing Outro and delaying incoming music when needed.
- Duck depth: use the existing setting at 0 dB. There is no extra duck control for this setup.
- Loudness processing: Stereo Tool (free preset) for the next listening pass. Smooth (Type B), Off and ReplayGain remain available. ReplayGain keeps its target-loudness field. Smooth and Stereo Tool share the prepared tracks, without adding ReplayGain. The choice is saved in playback profiles. A change takes effect when a newly prepared song reaches playback; the playing song and its prepared handoff finish with their current setting.
- Keep playing without listeners: on for unattended operation. Leave it off for the existing listener-triggered stream behavior.
- Clockwheel: import the PAL text file under Library tools, give it a name, and choose resync minutes. 0 means near the top of each hour; 0, 30 adds the half-hour. An empty list disables resync.

The clock chooses each next item as playback advances. It preserves PAL folder order, Dir versus SubDir scope, smLRP versus random selection, and EnforceRules versus NoRules. smLRP means least recently played; ties among never-played songs are random. Near a resync time, the rotation restarts at the nearer natural song handoff. It does not force an exact-time cut through the playing song. A selection replaced before it is heard stays in the unplayed pool. Missing or exhausted slots appear in clock status.

Queue items can be added, removed and reordered during playback. Once a song starts, its next handoff is already prepared. The page identifies that next song so it is clear where queue changes take effect. Play now interrupts the prepared sequence. Skipping before the next song has begun starts it at its Start point, preserving its unheard intro.

Smooth and Stereo Tool use B music preparation: -22 LUFS with slow leveling capped at a 4 dB boost. Common liner preparation uses 3:1 voice compression and a -17.5 LUFS target. There is 6 dB of mixing headroom. Smooth then uses the continuous v2-style compressor and limiter; Stereo Tool uses the preset from our C examples. The chosen processor stays running across songs and liners. Original files remain unchanged; prepared versions are cached.

The earlier examples used individual levels for the produced IDs and Plague. The common liner preset will not reproduce those levels exactly. Listen to raw voice and produced imaging before changing that shared preset. Output gain stays fixed during a programme; per-excerpt loudness matching from the audition files is not part of live playback. An offline check of three saved mixes reproduced the B master output exactly with today's Smooth filters. Flat EQ means 100%, labelled Unity, or 0 dB; the EQ repair preserves that sound.

Stereo Tool adds automatic gain control, a five-band compressor and a final limiter. Its preset is included with AutoDJ. Install the Stereo Tool command-line program separately, then set `stereotool_path` under `[playback]` in config.toml to its executable name or full path. This machine uses `C:/git/AutoDJ/playout-tools/stereo-tool/stereo_tool_cmd.exe`. That installation path and the browser's Loudness processing choice are the only added settings; there are no extra compressor controls to configure.

Thimeo says its [trial has no time limit](https://www.thimeo.com/help/stereo-tool/). Unlicensed paid features can add beeps or spoken messages; its [free features do not add audio reminders](https://www.thimeo.com/stereo-tool/licenses/). Our exact saved preset processed a seven-minute test file without stopping or reporting unsupported paid features. If the external processor fails, AutoDJ keeps audio moving through a limited fallback, reports the fault in playout status, and tries to recover. That fallback keeps the station audible but sounds less controlled than the normal processing.

config.toml still supplies installation defaults. The active library saves its folder lists, checkbox choices, points and clock in library.db. Configured folders are imported once; an intentionally empty list stays empty. Browser playback settings remain in web_state.json, and prepared audio is stored beside the library. Playback profiles do not change source folders. Existing Beets/index tools remain available as advanced jobs.

The requested folders beneath C:\station\throwback contain 1,518 playable songs. There are 12 liner files: ten in the requested Drops and Sweepers folders, plus the two existing imaging copies in the upload folder. Both requested liner folders exclude subfolders. Gold's 211 existing matching vectors are unchanged. Fourteen exact SHA-256 sample matches received our [saved cues](playout-cues.md). The two sample voice tracks had no match in the registered folders and remain separate; adding their folder or uploading them makes them available as liners. The initial clock is [plague2024.pal](plague2024.pal). Work stays on the local station-playout branch, without a pull request or push.

Windows is the test machine for this pass. Linux deployment, an extended unattended Stereo Tool test, BPM mixing within saved-point playback and a live-presenter desk still need separate work. The next listening test is to compare Stereo Tool with Smooth, adjust incorrect detected points, and confirm the common liner level. The EQ repair and Stereo Tool integration require a server restart before they can be heard; playback will not be restarted without permission.
