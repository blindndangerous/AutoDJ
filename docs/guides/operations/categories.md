# Build rotation categories and repair beets paths

Create category pools, diagnose mismatched beets paths, and build the playlist and themed-hour examples.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Rotation categories

A rotation category is a named set of songs that clockwheel steps pick from, the way professional
stations schedule music: rotation A for current hits, B and C for recurrents, D for gold, or a
themed set such as "Remembering the 2000s". There is no separate weighted pick mode; how often a
category plays is set by how many steps of the clockwheel use it. Choose **Music**, add a
**Category** condition, and select its name. You can use several categories or combine a category
with folder and metadata conditions in the same step; the category itself stays unchanged.

In a clockwheel music step or intro rule, **Add category** beside a **Music category**
condition opens the category editor above your unfinished editor. **Save category**
selects the new category in that condition and returns you to it. Other conditions
and unsaved edits stay in place. Cancel or Escape returns without changing the selection,
asking before discarding a changed category draft. The saved category remains available
even if you later cancel the step or rule.

Keep them in the **Library tools** tab, **Categories**, next to the clockwheels that use them. The
list says how many unique songs each category holds, how many playlist lines match no library track,
how many songs were placed by their release year (see below), and which clockwheels use it. **Add
category** and **Edit** open the same modal form; the count refreshes after a short pause, and **Check** gives
immediate feedback. Counts describe the matching selection before playback limits and hours.
Settled category and clock-step counts are also announced to screen readers after each completed
check; edits and stale count responses do not produce announcements. Categories saved with matching
conditions require a newer AutoDJ release: older versions refuse a categories file containing
`source = "filters"`, so upgrade again before downgrading or restore a backup made before saving
such a category.
**Save** refuses a query that cannot run or names a field no song has. **Delete** asks first and names the
clockwheels with a step for the category.

## Beets paths and `music_dir`

AutoDJ finds a song's beets values by its file path. Beets stores full paths, or paths relative
to its music folder (its `directory` setting); Beets queries and Beets conditions read relative
paths from `[library] music_dir`. When `music_dir` names the wrong folder, such as an example
value left in a container's configuration, no beets entry matches a library song: every beets query and Beets
field condition finds nothing, and clockwheel steps for those categories are skipped, so other
songs play.

AutoDJ reports this in four places, naming the setting and the folder that probably holds beets'
paths where useful.

- The server log, once each time it changes: `Beets paths: No song in the beets database
  matches a song in AutoDJ's library...`, and each empty category that reads beets says
  `has no songs in the library: no beets song path matches the library; check [library]
  music_dir`.
- `autodj doctor`: the `beets-paths` check, and the fix in the `categories` check.
- Library tools: under the music folders (with the count of songs that have a beets entry when
  all is well) and above the saved categories.
- Under **Up Next**, while the problem lasts.

These are static text, not announcements; read them when the station plays the wrong songs.

When `music_dir` is not set, or names a folder that does not exist on this machine, AutoDJ looks
up a sample of beets' relative paths in the library. If at least three sampled paths match under
one registered music folder, such as `/music` in the container, and none match under another,
Beets queries and Beets conditions read relative paths from that folder and say so. If paths
match under more than one folder, or `music_dir` names a folder that
exists, nothing is guessed. Set `music_dir` to the named folder in `config.toml` or
`config.local.toml` (or `AUTODJ_LIBRARY_MUSIC_DIR`) and restart AutoDJ. When beets stores full
paths that are not the library's, mount the music where beets keeps it, or register it in
Library tools by that path.

## Example: rotations A to D from playlists

If a script builds rotation playlists from beets (to import them into AzuraCast, for example),
point it at the playlists folder instead. This `update-playlists.sh` writes one M3U8 per rotation
from a beets flexible attribute set with `beet modify rotation=A`, through a temporary file so
AutoDJ never reads half a playlist:

```bash
#!/usr/bin/env bash
# update-playlists.sh: write rotation playlists A to D for AutoDJ.
set -euo pipefail
out=/srv/autodj/playlists      # [library] playlists_folder in config.toml
music=/mnt/music/              # beets' directory; stripped so the lines are relative to music_dir
mkdir -p "$out"
for rotation in A B C D; do
  {
    echo "#EXTM3U"
    beet ls -f '$path' "rotation:$rotation" | sed "s|^$music||"
  } > "$out/.$rotation.m3u8.tmp"
  mv "$out/.$rotation.m3u8.tmp" "$out/$rotation.m3u8"
done
```

Run it from cron after each beets import. Then in **Categories** add four categories, A to D,
each from **A playlist file** with its file, and make a clockwheel such as A, B, A, C, station ID
liner, A, D. Each new run of the script is used from the next song AutoDJ picks. Writing lines relative to
the music folder keeps the playlists working when AutoDJ sees the music at another path, such as
`/music` in the container; with full paths, AutoDJ must see the files at the same paths as beets.

The playlists are optional when beets is on the same machine: a category from the query
`rotation:A` holds the same songs without a script.

## Example: Remembering the 2000s

1. In **Categories**, add **Remembering the 2000s** from **A beets query** with
   `original_year:2000..2009`, press **Check**, then **Save**. The list then says, for example,
   "Remembering the 2000s: 1,284 songs; 12 use the release year because the first-release year is
   missing".
2. In **Clockwheels**, create **Throwback** with the steps: voice liner from your station ID
   folder, music from category Remembering the 2000s, music from category Remembering the 2000s,
   voice liner, music from category Remembering the 2000s, then save it.
3. In **Settings**, **Schedule**, add an event at 20:00 on Saturdays that changes song picking to
   **Throwback**, and one at 21:00 that changes it back to your usual clockwheel.

For a narrower hour, add terms: `original_year:2000..2009 genre:rock ^genre:christmas`.

The clockwheel picks the songs as the hour plays. To choose them ahead of time instead, and
edit the list, make a show (see [Shows](shows.md#shows)).


## Related tasks and contracts

- [Clockwheel conditions, category rules and storage](../../reference/operations/clock-and-category-rules.md)
- [Create, edit and import clockwheels](clockwheels.md)
