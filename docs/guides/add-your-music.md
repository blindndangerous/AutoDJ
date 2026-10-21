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
3. Leave **Folder type** set to **Music**.
4. Leave **Add subfolders** ticked to include every folder inside it.
5. Leave **Perform advanced analysis** ticked unless you have a reason not to (see below).
6. Press **Add folder**.

The page says "Folder added; import queued", and the folder appears in the table of registered
folders. You can add as many folders as you like.

## What happens next

AutoDJ works through new songs in three steps. You can play music while it works.

1. **Import.** AutoDJ reads each file's tags: title, artist, album, genre, year and BPM. The song
   appears in the library at once, and you can queue it by hand.
2. **Basic analysis.** AutoDJ listens to each new song once. It finds where the sound starts and
   ends, how loud the song is, and where its intro and outro are. Until this is done, AutoDJ does
   not pick the song by itself. This step is fairly quick.
3. **Song structure.** With **Find beats and song structure** ticked (it is by default), AutoDJ
   also finds the beat grid, the first downbeat, drops, breakdowns and phrases. These make
   beatmatching, phrase alignment and smoother crossfades possible.

Then, for folders with **Perform advanced analysis** ticked, AutoDJ runs **advanced analysis** in
the background, one song at a time.

With [no-model setup](../setup.md#no-model-installation), basic import and audio
preparation still run, but local model analysis stays pending. `autodj index`
finishes successfully and reports that pending work without downloading weights.
Use categories, clockwheels or queues for songs without advanced analysis. To
analyse them locally later, follow the setup guide's runtime upgrade steps.
Remote analysis requires an existing compatible model cache on the server;
see [worker requirements](operations/gpu-worker.md#3-install-autodj-on-the-gpu-machine).

### Why advanced analysis matters

Advanced analysis describes how each song sounds. AutoDJ uses it to find similar songs and
estimate key, energy, and missing BPM.

With advanced analysis, you can use:

- **Similarity** song picking, AutoDJ's default, where each song leads to one that sounds like it,
- **Discovery**, which now and then picks a song that sounds different,
- **Harmonic mixing**, which needs each song's key.

Without advanced analysis:

- Songs still play and appear in searches, queues, categories, and clockwheels.
- Similarity picking cannot choose them. When the playing song has no analysis, AutoDJ picks
  the next song at random instead, and notes this in its log.
- **Random walk**, **Least recently played**, and **Clockwheel** still work.
- Keys stay unknown unless you set them by hand or get them from beets, so harmonic mixing
  skips those songs.

Keep advanced analysis on for similarity picking. For clockwheels, you can turn it off to save time.

### How long it takes

Advanced analysis is the slow step. On an ordinary computer without a graphics card, a large
library can take many hours, so it is fine to leave it running overnight. A supported NVIDIA or
AMD graphics card makes it much faster. The first run downloads the analysis model from Hugging
Face into the `models` folder.

The default model's weights are licensed for non-commercial use only. Read [Credits and licensing](../../README.md#credits-and-licensing) before commercial use.

If the AutoDJ computer has no graphics card, another computer that has one can do the analysis.
Create a token in Settings, **Analysis workers**, then run `autodj worker` on that computer. See
[Using a GPU machine](../operations.md#using-a-gpu-machine) for the steps. While a worker is
connected, the AutoDJ server pauses its own analysis.

### Check progress

- **Library stats** shows how many songs are in the library and how many have a BPM, key, genre
  and energy. Press **Refresh summary** to update it.
- **Library jobs** and **Output** show the job that is running and its log.
- Press **Shift+J** on any tab to hear the job status.
- In the **Track info** tab, each song says whether its advanced analysis is Complete, Pending
  (still available for playback) or not enabled for its folder.

## Add a voice liner folder

Voice liners are station IDs, jingles and other short spoken clips.

1. In **Library tools**, type the folder path in **Folder path**.
2. Set **Folder type** to **Voice liner**.
3. Press **Add folder**.

Liner folders accept MP3, WAV, OGG, M4A, FLAC and AAC files. AutoDJ suggests a Start, Segue and
End point for each liner, but does not run advanced analysis on them. You can also upload single
liners in Settings, **Voice liners**; uploads go to the station's own liner folder.

For both music and liners, automatic Segue suggestions look for a sustained final drop in level.
Trailing silence is excluded from the reference level so padded files do not push the suggestion
into the quiet tail. Updating AutoDJ does not recalculate existing saved cues or overwrite manual cues.

## Change or remove a folder

Each folder in the table has its own **Add subfolders** and **Perform advanced analysis** boxes.
Changing one saves at once and rescans that folder.

**Remove folder** takes the folder out of the library. Its songs stop playing and stop appearing
in searches. AutoDJ keeps what it learned about them, so adding the same folder again brings them
back without analysing them again.

## Keep the library up to date

- **Rescan** looks for new, changed and moved files in every folder. Files you moved inside your
  registered folders keep their analysis and cue points. Each station keeps its own play history.
- **Override with ID3 tags**, ticked before **Rescan**, reads the tags of every file again, not
  only files that changed. BPM and key values you set by hand, and your cue points, are kept.
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
  processing in Settings, Sound.

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

- `autodj index` imports new songs from the registered folders and runs advanced analysis.
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
