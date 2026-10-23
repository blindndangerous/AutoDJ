# Configure recorded time checks and voice packs

Enable time checks, manage recorded clips and packs, choose play dates, and check coverage with Doctor.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Spoken time checks

Open **Settings**, then **Time checks**, and turn on **Enable time checks** (or set
`[time_checks] enabled = true`). Choose 12- or 24-hour speech, and choose **After the song that
crosses the hour** to hear the time when the song playing at the top of the hour ends. The
Schedule panel also offers a **Time check** action for set times; it needs server audio or
stream mode. Both use `station.timezone in the station definition` (or the detected system zone), and the time
said is the time when the check plays. A check plays at most once an hour from the rule: when a
scheduled check already played in that hour, the rule stays quiet.

A check is one clip: the optional intro, the time, and the optional outro. The time comes from
the first source that has the minute: a ticked voice pack, then a recorded minute clip, then
text to speech. A missing or unreadable file only moves on to the next source; it never stops
the music. It plays like a
voice liner, with the liner duck depth. After the hour, in server audio or stream mode, it takes
the place of the automatic liner due at that song change. This live time-check path retains the
legacy `[playback] liners_talk_up` timing when configured and the clip fits before the singing. When a talk-up liner is already playing over the crossfade
into that song, the check waits for the following song rather than cutting the liner off. In
browser playback the page plays it when the next song starts; there it does not replace a voice
liner due at the same song change, so the two can overlap. The **Play a time check now** button plays one straight away: on the server in server
audio or stream mode, and in this browser otherwise.

Each station has a **time checks folder**: the `time_checks` folder in the station's folder, or
`time_checks.clips_folder` in its `station.json` when it is set. It holds the recorded minute clips, the intro and outro clips, and one folder per voice
pack. Everything in it is managed from **Settings**, **Time checks**; the text-to-speech
command is set in the station's `station.json`, because it runs a program on the server.

Record one clip per minute: `00_00.wav` through `23_59.wav`, using 24-hour names even when
speech uses 12 hours. MP3, FLAC and OGG are also accepted. For example, `03_05.wav` plays at
3:05 in the morning and `15_05.wav` at 3:05 in the afternoon. Put them directly in the time
checks folder, or in a folder inside it and choose that folder as **Recorded minute clips
folder** (a zip of the clips can be added with **Add a voice pack**, below, and then chosen
there). A folder chosen for the clips is not a voice pack. **Upload a clip** adds one clip: an
intro or outro goes to the time checks folder, and a minute clip such as `15_05.wav` to the
recorded minute clips folder in use. When a clip of that name is there it asks before replacing
it. The file must play as the audio its extension says, and may be up to the station's
`station.liner_upload_max_mib` MiB limit and two minutes long.

**Intro clip** and **Outro clip** wrap every check. Choose **None**, or an audio file directly in
the time checks folder (minute clips are not offered). Until one is chosen here, `intro_clip`
and `outro_clip` in the station definition are used, and the list shows that file's name followed by
"from station settings"; choosing **None** turns off one set there. The choices are saved in
`time_checks.json` by name, never as paths. A chosen clip that is no longer in the folder is
shown as "not found", and a check that needs it is skipped with a warning, as a missing
`intro_clip` always was. When a station is duplicated, its clip choices are not copied, because
its time checks folder is not.

The Settings panel says how many minute clips there are. Run `uv run autodj doctor` for the
number missing, the first ten missing names, and warnings for a missing folder, intro or outro
clip or TTS program. A minute without a clip uses TTS when it is configured. Without a clip or
working TTS command, the check is skipped and the server logs one warning.

## Voice packs

A voice pack is a folder of short recordings that AutoDJ joins into one sentence, the way
RadioBOSS does, so its packs work as they are: an hour file says "it's 5", a minute file says
"fifty-six", and an exact-hour file says "it's 5 o'clock". The folder name is the pack's name
in Settings.

AutoDJ finds packs by itself: every folder in the station's time checks folder is a pack, and so
is every folder in the shared `<index_dir>/shared/time_checks` folder, which every station
shares. Some
folders are not packs: those whose names start with a dot, which AutoDJ uses while it unpacks
an upload; a folder whose audio files are all minute clips (`15_05.wav`); the station's chosen
recorded clips folder; and any explicitly chosen recorded clips folder. Packs are looked for again each time the panel loads and before each check, so a folder copied
in on the server needs no restart; **Look for new voice packs** looks straight away and names
the new ones. Folders elsewhere can also be listed in the station definition:

```json
{
  "time_checks": {
    "voice_packs": ["D:/Voices/Studio", "D:/Voices/Night"],
    "pack_order": "rotate"
  }
}
```

The packs are listed in this order: those in `voice_packs`, then the station's own folders, then
the shared ones, each by name. One folder is one pack however it was found, so a `voice_packs`
line for a folder that is also found by itself is fine. Names are compared without regard to
case, and when two different folders have the same name the first in that order wins: a listed
pack keeps its name over a found folder, and the station's folder over the shared one. The
folder left out is logged once and reported by `uv run autodj doctor`; rename one of them to use
both.

To add a pack from the page, open **Add a voice pack**, choose a zip file of the pack's audio
files, give it a **Pack name** or leave that blank to use the folder in the zip (or the zip
file's name), and press **Add voice pack**. The page says "Uploading the voice pack. This can
take a few minutes." as it starts, and the new pack's checkbox takes focus when it is done. When
a pack added from the page has that name, AutoDJ asks before replacing it, before anything is
sent when the name is already in the list; a replaced pack keeps its tick and play dates. A
refused upload is shown under the file input, which takes focus, until another file is chosen. A name taken by a pack listed in `config.toml`, a shared pack, or the
recorded clips folder is refused. AutoDJ checks the whole zip before it keeps anything:

- Only `.wav`, `.mp3`, `.flac` and `.ogg` files, each of which must play as the audio its
  extension says and be at most two minutes long. Any other file refuses the zip, except the
  `__MACOSX` folder, `.DS_Store`, `Thumbs.db` and `desktop.ini` that operating systems add,
  which are skipped.
- The files are at the top of the zip or all in one folder; subfolders are refused.
- No entry may have an absolute path, a drive letter or `..`, no entry may be a link, and none
  may be encrypted.
- At most 2000 files and 512 MB unpacked, counted from the zip's headers and again as each file
  is written; the zip itself may be up to 256 MB. Before the zip is opened, its end record
  (found exactly where Python's `zipfile` finds it, and refused when it is cut short or does not
  end the file) must list at most 3000 entries in a directory of at most 2 MB, and zip64 is
  refused, so a small zip that lists millions of entries cannot use up the memory.
- An entry name that is not valid in the encoding its flags give makes the upload "not a zip
  file". Two names that differ only in letter case or in how an accent is written are the same
  file, so they are refused as in the zip twice.

The zip is unpacked into a hidden folder beside the packs and renamed into place only when all
of that has passed, so a refused upload leaves nothing behind and a pack is never seen half
written. A pack in the station's time checks folder has a **Remove** button. It asks first, then
deletes the folder with the pack's tick and play dates, and focus moves to the next pack, or to
the **Voice packs** group when none follows. Packs listed in a station definition and shared packs have
no **Remove** button and say where they come from. A migrated default station can retain its
former shared clips folder through an explicit setting; fresh stations use their own folders.
Shared packs remain visible to all stations. Like every setting, adding and removing packs and clips needs a paired browser; a machine
token cannot do it.

An upload or removal stopped half way (the server was stopped, say) leaves a hidden
`.pack-upload-…`, `.pack-removed-…` or `.clip-upload-…` file or folder. Those older than an hour
are deleted when the station starts, and backups never include them. A `.pack-replaced-…`
folder is the old pack set aside during a replace; it is normally deleted at once, and is only
left when the replace failed and putting the old pack back failed too, which is logged as an
error. It is never deleted for you: AutoDJ logs it at each start, so you can rename it to put the
pack back or delete it.

**Settings**, **Time checks** shows each pack as a checkbox under **Voice packs**. Its
description says how many of the 1440 minutes it can say, which hour, exact-hour and minute
files are missing, the optional files it uses, and any files it does not use. Tick the packs to
use. Until a choice is saved, every pack is ticked, including one found later; once it is saved,
a pack found or added later starts unticked, as a newly listed `voice_packs` folder always did.
**When several packs are ticked** chooses whether they take turns, one per check, or one is
picked at random. When the chosen pack cannot say the minute, the other ticked packs are tried
before the recorded clips. **Voice to test** picks one pack, ticked or not, for **Play a time
check now**; when the server plays it, the result says which source spoke. The browser only ever
sends a pack's name, never a folder.

A pack can have play dates, such as a holiday voice from 1 to 26 December. Under **Voice pack
play dates**, choose the pack, enter **Not before**, **Do not play after** or both, and press
**Save play dates**. Both days are included, in the station time zone. Outside its dates a ticked
pack is passed over and the other ticked packs speak; **Voice to test** still plays it. Each
pack's description says its dates in words and "Skipped now" when it is outside them. A refused
date is shown under the form and linked to its field. The dates are kept beside the switches in
`time_checks.json` under `pack_dates`, written only when a pack has dates, and are kept for a
pack taken out of the station definition, or whose folder goes away, until it comes back. A pack removed
with its **Remove** button loses its dates, and a new pack added from the page starts without
any. Dates that cannot be read, and keys this
version does not know, are logged and not used, and are written back as they were on the next
save. Each save reads the file again first, so what another process wrote is kept. While the file
cannot be read, saves are refused with a message rather than written over it; a file that is not
valid JSON is set aside as `time_checks.json.bad-<id>` before the next save.

`uv run autodj doctor` shows one `voice-pack-<name>` line per pack, listed or found, with the
same coverage text and the folder, and warns when the folder is missing, a file is not used, or
the pack cannot say every minute. The `time-checks` line warns about a missing TTS fallback only
for minutes that neither a ticked pack nor a recorded clip can say, and warns when a saved pack
name is not found, when a folder is left out because its name is taken, and when a chosen clips
folder, intro or outro is missing.

Backups include everything in the time checks folder, so the packs and clips added from the
page come along (for example `<index>/time_checks/Studio`). Packs listed from other folders are
not copied, like the music library: back them up yourself with `config.toml`.


## Related tasks and contracts

- [Voice-pack filenames and audio assembly](../../reference/operations/voice-packs.md)
- [Configure a station text-to-speech command](tts-command.md)
- [Compare speech and prepare generated time checks](speech-studio.md)
