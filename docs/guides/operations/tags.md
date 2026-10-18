# Edit tags in the catalog, files and beets

Choose where a tag change is saved, handle busy files or beets failures, and confirm that the intended records changed.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Editing tags

Track info has a **Tags** section for the song or liner it shows. It has a field each for Title,
Artist, Album, Genre, Year, BPM, Key and ISRC, filled in with what AutoDJ has for the file.
Change any of them and choose **Save tags**. AutoDJ saves only the fields you changed, in its own
library. The file is not touched unless you check **Also save the tags to the file**, which starts
unchecked for every track. Leave a field empty to remove that tag. Title cannot be empty.

The fields take:

- Year: four digits, such as 1999.
- BPM: a number from 1 to 400, such as 128 or 127.5. M4A files keep a whole number.
- Key: a letter key such as Am, F#m, Db or C major, or a Camelot key such as 8A. AutoDJ writes it
  as a letter key, and Track info shows it in the key notation set in Settings.

A key or BPM you set here is kept when AutoDJ analyses the song later; the analysis still
measures energy and the rest. Clear the field to let the analysis set it again.
- ISRC: 12 characters, such as AB1CD2600001. Hyphens and spaces are ignored. AutoDJ keeps the
  ISRC only in the file, so changing it needs **Also save the tags to the file**.

A field AutoDJ cannot take is marked invalid with the reason under it, and focus moves to the
first one. When the save works, the page says so once and focus stays on Save tags.

AutoDJ writes MP3, FLAC, M4A, Ogg Vorbis, Opus, WAV and AIFF tags. For any other kind of file,
or one it cannot read, the Tags section says the tags are saved in AutoDJ only, and the checkbox
and ISRC field are not shown. Writing the file is refused while the song is playing or up next;
saving in AutoDJ only is not. The write is safe to
interrupt: AutoDJ copies the file to a hidden `.<name>.<random>.autodj-tags.tmp` beside it,
tags the copy, flushes it to disk, reads every changed field back, and only then puts it in
place of the original in one step. On Linux and macOS it then flushes the folder too; Windows
has no folder flush, so there the step is as durable as the file system makes it. If anything
before that step fails, the original file is left exactly as it was and the page says why. A
copy left behind by a crash or power cut is removed the next time that file's tags are saved,
once it is an hour old (a younger one may be another AutoDJ's save in progress); it is safe to
delete by hand.

Because the file is replaced by a new one:

- Its modification time becomes the time of the save.
- It keeps its permissions. On Linux and macOS it keeps its owner and group when AutoDJ is
  allowed to set them (usually only when AutoDJ runs as that user or as root).
- Access control lists on Windows are not carried over; the new file takes its folder's.
- A hard link to the file keeps the old tags, because it still points at the old file. A
  symbolic link stays a link, and the file it points to is the one changed, but only when that
  file is inside one of the library folders; a link pointing elsewhere is refused.
- An MP3 tag keeps its ID3 version: a version 2.3 tag stays 2.3.

Saving tags does not analyse the song again. The library and search show the new details at
once, and the song keeps its advanced analysis, cue points and loudness measurement. A later
scan sees the same file and leaves it alone.

A song that is playing or up next on any station cannot be saved; save it once it has played.
That includes the song the server mix is preparing next. AutoDJ checks again at the moment it
replaces the file, while the stations wait for it, so a song that comes up in between is still
refused.
If another program has the file open, AutoDJ waits a few seconds and then says the file is busy
without changing it.

When `[library] beets_db` names a beets database, the Tags section also has **Also update the
beets library**, ticked by default. With it ticked, AutoDJ copies the changed fields into the
song's beets entry (ISRC only when your beets database has an `isrc` field) and sets its
modification time, so `beet update` has nothing new to read. When file saving is selected, the file is written first.
If beets has no entry for the file, or its database cannot be written (for example while beets
itself holds it), the file and AutoDJ's library keep the new tags, beets keeps the old ones,
and the page says so; run `beet update` to read the new tags into beets. When beets has no field
for any tag you changed (an ISRC, in most beets libraries), beets is left exactly as it was, so
`beet update` still reads the new file. Beets is never changed when the file could not be saved.

If the file is saved but AutoDJ's own library cannot be updated, the page says the tags were
saved in the file and what went wrong. A rescan then reads the new tags, and the song is
analysed again. Rescans also detect external tag edits that fit inside existing file padding.

In Docker, the music folder is mounted read-only by default. **Save tags** can still change
AutoDJ's library; writing the file fails if **Also save the tags to the file** is ticked.
Remove `:ro` from the music volume in
`compose.yaml` to allow it, and from the beets volume to let AutoDJ update beets.
