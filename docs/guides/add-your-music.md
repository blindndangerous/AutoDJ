# Add your music

Add music and voice liners, then keep the library current.

## Before you start

- AutoDJ reads music from folders on the computer it runs on. A folder on your own laptop is
  not visible to AutoDJ on another computer. Copy or share the music to the AutoDJ computer first.
- A network share works if the AutoDJ computer can open it, for example a mapped drive or a
  path such as `\\nas\music` on Windows, or a mounted folder on Linux.
- In Docker, the music is mounted at `/music` inside the container. Use `/music` as the folder
  path. See [Install AutoDJ](install.md#docker-and-nas).
- AutoDJ imports MP3, FLAC and M4A files by default. To add other formats, such as OGG or WAV,
  list them in `supported_formats` in the `[library]` section of `config.toml`; see the
  [Settings reference](../settings-reference.md#library).

A new installation starts with an empty library. Importing does not move your files. The **Tags** card in **Track info** can change library tags later.

## Add a music folder

1. Open the **Library tools** tab.
2. Under **Music folders and Voice liner folders**, type the folder's full path in **Folder
   path**, such as `D:\Music` or `/srv/music`.
   Or press **Browse…** beside it to choose a folder on the AutoDJ machine. In the dialog,
   press Enter on a folder to open it, use **Up one folder** to go back, and press **Select
   this folder**. **Cancel** or Escape leaves the field unchanged.
3. Leave **Folder type** set to **Music**.
4. Leave **Add subfolders** ticked to include every folder inside it.
5. Choose **Basic**, **Audio** or **Similarity** for **Music folder analysis** (see below).
6. Press **Add folder**.

The page says "Folder added; import queued", and the folder appears in the table of registered
folders. You can add as many folders as you like.

## What happens next

AutoDJ scans new songs first, so they appear in the library before optional audio or MuQ work
finishes. You can play music while later work continues.

1. **Import.** AutoDJ reads each file's tags: title, artist, album, genre, year and BPM. The song
   appears in the library at once, and you can queue it by hand.
2. **Basic scan.** AutoDJ measures duration and energy, suggests cues, and reads any enabled
   loudness data. A song becomes available to play after these measurements finish.
3. **Audio analysis.** This optional stage stores a versioned set of audio features and derives
   key, BPM, beat grid and song structure. It does not create a MuQ similarity vector.
4. **Similarity analysis.** This adds a MuQ vector to the same audio features for similarity
   picking, Discovery and harmonic mixing.

Choose **Basic** to stop after scanning, **Audio** to add the shared audio analysis, or
**Similarity** to add MuQ vectors too. Registered music folders can overlap; AutoDJ uses the
strongest requested mode across every matching music folder (Similarity, then Audio, then Basic).
If a path is also under a voice-liner folder, liner handling takes priority and uses Basic.
Existing installations migrate an enabled legacy advanced-analysis setting to Similarity. Disabled folders migrate
to Audio when the saved DJ-analysis setting is on (the default), otherwise Basic. Liners stay
Basic. The Similarity choice is offered when installed MuQ weights and the local runtime are
available, or when a compatible worker can use an existing compatible feature cache. A folder
that already uses Similarity keeps that setting while those resources are unavailable.

With [no-model setup](../setup.md#no-model-installation), Basic and Audio work remain available;
Similarity waits for MuQ weights and a compatible local model runtime or worker. `autodj index`
does not download weights in no-model mode. Use categories, clockwheels or queues for songs
without similarity vectors. Remote analysis requires the server's existing compatible model
cache; see [worker requirements](operations/gpu-worker.md#3-install-autodj-on-the-gpu-machine).

### Why similarity analysis matters

Audio analysis describes each song's musical features and estimates key and BPM. Similarity adds
the MuQ vector AutoDJ uses to find songs with a similar sound.

With Similarity analysis, you can use:

- **Similarity** song picking, AutoDJ's default, where each song leads to one that sounds like it,
- **Discovery**, which now and then picks a song that sounds different,
- **Harmonic mixing**, which needs each song's key.

Without similarity analysis:

- Songs still play and appear in searches, queues, categories, and clockwheels.
- Similarity picking cannot compare them. When the playing song has no vector, AutoDJ picks
  the next song at random instead, and notes this in its log.
- **Random walk**, **Least recently played**, and **Clockwheel** still work.
- Basic-only folders have no audio-derived key; hand-set or beets tags still work.

Choose Audio when you want audio features and DJ metadata without MuQ. Choose Basic when you only
need playable tracks and scan measurements.

### How long it takes

Similarity analysis is the slow step. On an ordinary computer without a graphics card, a large
library can take many hours. A supported NVIDIA or AMD graphics card makes MuQ analysis faster.
Audio features for tracks longer than 15 minutes use the first and last five minutes. Set the
Similarity maximum length under **Settings**, **Global**, **Server**; longer tracks still receive Audio
analysis. The first local MuQ run downloads weights into the `models` folder.

The default model's weights are licensed for non-commercial use only. Read [Credits and licensing](../../README.md#credits-and-licensing) before commercial use.

If the AutoDJ computer has no graphics card, another computer that has one can do the analysis.
Create a token in Settings, **Global**, **Server**, **Analysis workers**, then run `autodj worker` on that computer. See
[Using a GPU machine](../operations.md#using-a-gpu-machine) for the steps. While a worker is
connected, the AutoDJ server pauses its own analysis.

### Check progress

- **Library stats** shows how many songs are in the library and how many have a BPM, key, genre
  and energy. Press **Refresh summary** to update it.
- **Library jobs** and **Output** show the job that is running and its log.
- Press **Shift+J** on any tab to hear the job status.
- In the **Track info** tab, each song says whether its similarity analysis is Complete, Pending
  (still available for playback) or not enabled for its folder.

## Add a voice liner folder

Voice liners are station IDs, jingles and other short spoken clips.

1. In **Library tools**, type the folder path in **Folder path**.
2. Set **Folder type** to **Voice liner**.
3. Press **Add folder**.

Liner folders accept MP3, WAV, OGG, M4A, FLAC and AAC files. AutoDJ suggests a Start, Segue and
End point for each liner, but does not run music audio or similarity analysis on them. You can also upload single
liners in Settings, **Voice liners**; uploads go to the station's own liner folder.

For both music and liners, automatic Segue suggestions look for a sustained final drop in level.
Trailing silence is excluded from the reference level so padded files do not push the suggestion
into the quiet tail. Updating AutoDJ does not recalculate existing saved cues or overwrite manual cues.

## Change or remove a folder

Each folder in the table has its own **Add subfolders** and **Music folder analysis** settings.
Changing one saves at once and rescans that folder.

**Remove folder** takes the folder out of the library. Its songs stop playing and stop appearing
in searches. AutoDJ keeps what it learned about them, so adding the same folder again brings them
back without analysing them again.

## Keep the library up to date

- **Rescan** looks for new, changed and moved files in every folder. Matching files reuse their
  audio feature cache and keep saved vectors and cue points; changed file identity invalidates
  the audio cache, while an existing vector remains until replacement analysis is saved. Each
  station keeps its own play history.
  A file counts as changed when its size or modification time changes.
- **Override with ID3 tags**, ticked before **Rescan**, reads the tags of every file again, not
  only files that changed. BPM and key values you set by hand, and your cue points, are kept.
  It also finds audio changed by a tool that kept the file's size and time.
- **Automatic rescan every** runs Rescan on a timer, counted from the end of the last scan. Set
  **Hours** and **Minutes**, then press **Save**. The shortest interval is 5 minutes, and 0 hours
  and 0 minutes turns it off. It is off by default.
- **Remove missing tracks** lists songs whose files are gone and removes them after you confirm.
  Each station's play history is kept. It works only after a clean scan, so a disconnected drive is not mistaken
  for deleted music.

If a whole folder is missing, for example because a network share is not mounted, AutoDJ simply
plays nothing from it until it returns. Nothing is marked as deleted.

## Tags, lyrics and album art

- **Tags**: open a song in **Track info** to change its title, artist, album, genre, year,
  BPM, key and ISRC. **Save tags** changes the library only. To write the file too, tick
  **Also save the tags to the file** before saving. That box is unticked by default. In Docker,
  the music mount is read-only by default, so leave it unticked unless you make the mount writable.
- **Lyrics** come from an `.lrc` file with the same name beside the song, or from lyrics stored
  in the file. Timed `.lrc` lyrics scroll and highlight as the song plays.
- **Album art** comes from the picture stored in the file.
- **ReplayGain** loudness tags in the files are used when you choose ReplayGain output
  processing in Settings, Profile, Sound.

## Using beets (optional)

If you keep your library in [beets](https://beets.io/), AutoDJ can read it. Set `beets_db` in the
`[library]` section of `config.toml` to the path of beets' `library.db`. Then:

- **Enrich**, in Library jobs, refreshes title, artist, genre, BPM and key from beets.
- Categories can use beets queries, such as `original_year:2000..2009`.

Beets is not required. Without it, AutoDJ uses the tags in your files.

Beets' stored music paths must match the folder AutoDJ sees. In Docker, that folder is `/music`.
AutoDJ reports a mismatch. For relative beets paths, when `music_dir` is unset or names a
missing folder, AutoDJ suggests a registered music folder and uses it if only one matches.

## From the command line

The same jobs can run from a terminal. Use `./autodj` on Linux and macOS, or `.\autodj.cmd` on
Windows:

- `autodj index` imports new songs and runs the analysis requested by their registered folders.
  `--limit 50` analyses only 50 songs, which is a good first test.
- `autodj prune` removes songs whose files are gone, after asking.
- `autodj enrich` refreshes tags from beets.
- `autodj stats` prints a summary of the library.

Each command takes `--help`.

## Where the library lives

The library is kept in AutoDJ's data folder, in `index/libraries/main/library.db`, with the
song structure in `dj_meta.db` beside it. Every station on the server shares it. `autodj backup`
saves both.

## Related pages

- [Install AutoDJ](install.md)
- [Settings reference](../settings-reference.md)
- [Troubleshooting](troubleshooting.md#music-and-the-library)

AutoDJ and this page were developed with AI assistance.
