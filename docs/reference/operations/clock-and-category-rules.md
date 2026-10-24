# Clockwheel conditions, category rules and storage

Look up supported conditions, query and playlist semantics, repeat fallbacks, time windows and saved clock/category formats.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

Music and liner steps have an **Add condition** button in the same step form. Choose **Match
all** when every condition must hold, or **Match any** when one is enough. Each folder is its
own condition. For example, Match any with two folders selects from either folder; Match all
with a folder and Year before 2000 selects older songs from that folder. A step can also use
only metadata conditions, without choosing a folder. Mixed groups such as `(folder A or folder
B) and year before 2000` are not supported yet.

Available conditions:

- **Category** (Music steps): choose a saved [rotation category](../../guides/operations/categories.md#rotation-categories). With
  Match all, a song must belong to every selected category and meet the other conditions. With
  Match any, belonging to any selected category or matching another condition is enough. For
  example, Category Gold and BPM above 100 narrows Gold to faster songs; Category Gold or
  Category Current hits combines those groups. A song matching several conditions is included
  once, so it gets no extra chance of being picked. Category hours still apply even when another
  condition also matches the song. Categories are music groups, so this condition is not offered
  for voice liners. Choose **is not** to leave out that category's songs.
- **Folder**: a registered music or liner folder, or a subfolder such as `Rock/Live`. Include
  subfolders applies to that condition; **is not** excludes it. The registered folder must include subfolders before a
  condition can use one of them.
- **Genre**: choose a genre held in the library and choose **is** or **is not**. Matching ignores case. Multiple labels stored
  with semicolons, or as a list, are matched separately; slashes and commas remain part of a label.
- **Year**, **BPM** and **Duration**: equal to, not equal, less than, at most, greater than, at least,
  between two values or outside a range. Between includes both ends; outside excludes both. Year is
  the release year in AutoDJ's library. Duration
  is the full file length; enter seconds or `minutes:seconds`.
- **Artist**, **Title** and **Album**: exact text, contains text, or a wildcard pattern. Matching
  also support does not contain and does not match. Matching ignores case. In a pattern, `*` means any text and `?` means one character. Other punctuation is
  literal; these are not regular expressions.
- **Beets field**: a field from the configured Beets database, including custom fields. Choose a
  text or numeric comparison, including the matching exclusions. `original_year` uses the first-release year, falling back to
  `year` when it is missing. Ordinary conditions work without Beets.
- **Within the last N days**: for a Beets date field, such as `added` (when the song was imported
  into Beets) or `mtime` (when its file last changed), choose the Beets field, the operator
  **within the last N days (date)** and a whole number of **Days** from 1 to 3650. For example,
  Beets field `added`, within the last N days, Days 30 makes a "new music" step or category. The
  window moves with the clock: it ends at the start of the current hour and reaches back that many
  days, and a song added since the hour began, or dated slightly ahead, also matches. The days are
  saved, not a date, so the songs that match change on their own as time passes. Beets keeps these
  dates as Unix times; a song with no date (Beets stores 0) never matches. AutoDJ's own library
  has no date added, so this condition needs Beets. In a saved file it is
  `{"field": "beets", "name": "added", "operator": "within_days", "value": 30}`.

A missing value does not match a condition, including a negative condition. Unknown year, BPM or
duration values are not treated as zero. Genre exclusions reject a track if any genre label matches.
The player reads Beets without changing it and caches the requested fields until the
database changes. An unavailable database or field produces an error when saving or activating
a step that needs it. If it becomes unavailable during playback, that step is skipped with a
status message. Other valid steps can still play.

Repeat rules apply after the conditions have selected their pool. Relaxing repeats never removes
those conditions. When explicit conditions have no matches, including category conditions, the
step is skipped and does not use unfiltered music. The existing dayparting-only fallback remains
when matched songs are held back only by their allowed days or hours. Unedited legacy category
steps keep their old empty-pool fallback; editing and saving one converts it to an explicit
category condition. Existing folder steps also open in the Music or Voice liner form and keep
their selection and repeat-rule settings.

A deleted category skips the affected step, even with Match any, and the status says why. An
existing category with no songs contributes no matches; other Match any conditions can still
supply a song. Saving or activating a step with a missing category asks you to choose a saved one.

Each clockwheel is one JSON file in the station's `clockwheels/` folder, named after the
clockwheel and included in `autodj backup`. Names allow letters, digits, spaces, dashes and
underscores, up to 64. The format is versioned and checked strictly; an unknown field, step type
or version stops that clockwheel from loading, says why in the editor and the server log, and
leaves the clock off while the station keeps playing:

```json
{
  "version": 1,
  "name": "Morning",
  "steps": [
    {"type": "music", "source": "<library folder ID>", "folder": "Rock",
     "recursive": false, "selection": "least_recent", "enforce_rules": true},
    {"type": "liner", "source": "<library folder ID>", "folder": "",
     "recursive": false, "selection": "random", "enforce_rules": false},
    {"type": "track", "source": "<library folder ID>", "path": "Album/Song.flac"},
    {"type": "music", "conditions": [{"field": "category", "category": "<category ID>"}],
     "match": "all", "selection": "least_recent", "enforce_rules": true},
    {"type": "settings", "preset": "winddown"}
  ],
  "resync_minutes": [0]
}
```

`source` is the ID of a registered library folder, so the file never holds a full path; removing
that folder from the library stops the clockwheel until you edit its steps. Later features (carts,
time checks, ad breaks) add their own step types through `autodj.clockwheel_store.register_step_type`.

**When this step plays.** Any step can keep to chosen days, to hours (**Allowed from** and
**Allowed until**), or both, in the step form's **When this step plays** fields, with the rule
categories use: wall-clock times in the station time zone, the start included and the end not,
and a window that crosses midnight belongs to the day it starts. Outside them the step is skipped
as an empty step is, and the clockwheel moves on to its next step; a voice liner step outside
its hours leaves the following song without its liner, and a Switch preset step outside its hours
switches nothing. The hours are checked for when the song is expected to start, as the
categories' are. The step list says them after the step, such as "music from Rock, only on
Friday and Saturday from 22:00 until 02:00", the clockwheel status and the server log say why a
step was skipped ("Step 3: outside its hours: plays only ...", or "outside its days" on a day it
does not keep), and the rotation tester counts those skips. A clockwheel whose every song step is
outside its hours does what it does when no step has a song: one with category steps plays a song
from the whole library, and one of folder, condition or track steps plays nothing until a step
opens, as before. The whole-library song that a category's own hours allow is only for songs held
back by their categories, not by a step's hours. A voice liner step and the song step after it
are both judged at the song change; a liner that started after the song (#159) would still be
judged then. In the
file a step has `"days": [4, 5], "hours_from": "22:00", "hours_until": "02:00"`; a step without
them is written as before, so an older AutoDJ still reads the clockwheel. Use this for a step or
two that differ at night; for a different clockwheel at night, a schedule event that switches
clockwheels is simpler.

A category's songs come from one of three sources:

- **Matching conditions** use the same Match all / Match any filters as a music clockwheel step:
  registered folders, genre, year, BPM, duration, artist, title, album and available Beets fields,
  including a Beets date within the last N days.
  A condition can exclude a folder, genre or text value, or use a numeric not-equal/outside-range
  comparison. Unknown metadata never passes an exclusion; a genre exclusion leaves out a track if
  any of its genre labels matches. Categories cannot refer to other categories.

- **A beets query**, such as `original_year:2000..2009` or `genre:rock ^artist:"bon jovi"`. It is
  read against AutoDJ's library, with the values of the live beets database (`[library] beets_db`,
  opened read-only) laid over each song, so any beets field works, including flexible attributes
  that plugins or `beet modify` add, such as `mood` or `rotation`. Without beets the query sees the
  library's own fields: title, artist, album, genre, year, bpm and length. AutoDJ runs the common
  part of the beets query language itself, as beets is not needed at run time:
  - `field:value` matches when the field contains the value, ignoring case; on a number field such
    as `year` or `bpm` the value must match exactly.
  - `field:a..b` is a range of numbers, both ends included; either end may be left open, as in
    `bpm:..100`.
  - `^` (or `-`) in front of a term leaves out what it matches.
  - Quotes keep words together: `artist:"the beatles"`.
  - A bare word matches artist, title, album, album artist, genre or comments.
  - Every term must match. Regular expressions (`field::re`), exact matches (`field:=x`),
    alternatives with commas, sorting, date ranges, path queries and plugin queries are refused
    with a message that names the term; make a category per alternative and combine them with
    Match any in the clockwheel step.
- **An M3U or M3U8 file** in the playlists folder (`[library] playlists_folder`, by default
  `shared/playlists/` in the index folder). Each line that is not a comment is matched to a
  library song by its full path, or by a path from the playlist's own folder or from
  `[library] music_dir` (where beets keeps relative paths); `file://` addresses work too. Lines
  that match no song, web addresses included, are counted and the first few are named, so a
  script that writes paths for another machine is easy to spot. M3U8 files are UTF-8; plain M3U
  files that are not UTF-8 are read as Windows-1252.

**Years.** A year filter should use `original_year`, the year the song first came out. beets'
`year` is the year of that particular release, so a 1985 song on a 2005 compilation has `year`
2005. When beets has no `original_year` for a song (it stores 0), its `year` stands in, and a song
with neither is left out of year filters. The list, Check and `autodj doctor` count the songs
that used `year`, so you can fix their tags with beets.

**When the songs are worked out.** AutoDJ works out each category's songs once and keeps the
answer until the categories, the beets database, a playlist file or the library changes, which a
few file-time checks before each clockwheel pick tell it; the songs are never read again for
every pick. A category or clockwheel step with a **within the last N days** condition is also
worked out again once an hour, when its window moves. A step judges the window for when the song
is expected to start, as it does its hours, so the rotation tester moves it with its own clock;
a category judges it at the current time, also in the rotation tester and the Hour preview. When a playlist is being rewritten or beets is busy importing, a category keeps the
songs it held last time and the server log says why.

**At play time.** An explicit category condition picks from its category members and follows the
step's repeat rules, relaxing them rather than going silent; in DMCA mode it keeps the DMCA
limits. If no song matches its conditions, the step is skipped, including when a Match any step's
category is missing or empty. An unedited legacy category step keeps its old whole-library
fallback when all steps fail; saving it from the editor converts it to an explicit condition.
The dayparting-only fallback also remains when matched songs are held back only by their allowed
days or hours. The clockwheel keeps its place in each case.

While editing, the step form counts unique tracks that match its current conditions. It uses the
music or liner library selected by the step, before repeat protection, DMCA limits, step or category
hours, and other playback restrictions. A preset change has no track count. Count errors and
incomplete conditions are shown in the form; automatic updates are not announced by screen readers.

**Dayparting.** Under **When the songs may play**, a category can allow its songs only on chosen
days, only between two times (**Allowed from** and **Allowed until**), or both, such as Friday and
Saturday from 22:00 until 02:00. Leave every day unchecked for every day and both times empty for
all day. The times are wall-clock times in the station time zone (`station.timezone in the station definition`);
the start time is included and the end time is not. A window that crosses midnight belongs to the
day it starts, so Friday 22:00 until 02:00 still allows the songs at 01:00 on Saturday. On the
night the clocks go back, a window inside the repeated hour is open for both passes; on the night
they go forward, one inside the skipped hour does not open.

Outside its days and hours, a category's songs are left out of every pick mode: similar songs,
discovery, random, least recently played and every clockwheel step, and a clockwheel step for
that category is skipped with "outside its hours" in the clockwheel status and the server log. A
song in several categories plays only while every one of them allows it. Relaxing the repeat rules
or the DMCA limits never brings such a song back. When nothing else at all can play, the station
plays one anyway rather than going silent, and the server log says so. A song you queue yourself
plays at any time. The list says each category's days and hours in words, such as "Plays only on
Friday and Saturday from 22:00 until 02:00", and when it is outside them now; with all seven days
ticked and no times it says "Plays every day, at any hour".

The hours are checked for when the song is expected to start, not when it is picked: the song
after this one is picked while this one plays, so a pick at 01:58 with four minutes of this song
to go checks the hours at 02:02, and a window that ends at 02:00 no longer lets its songs start
just after it closes. The rotation tester checks them the same way. Which songs are outside their
hours is worked out once a minute, so a pick only looks them up. Without a station-specific
`station.timezone`, the system time zone is asked for once, when AutoDJ starts; a host with no
known zone uses UTC and logs one warning. When every song is outside its hours, the warning that
one plays anyway is logged once, and again only after the hours change.

For dayparting single songs, make a category from a beets query on a field you set, such as
`beet modify daypart=night` and the query `daypart:night`, and give it the hours. Categories that
have no days or hours are saved as before, so an older AutoDJ still reads the file.

**Seasons.** The same fields take a **Not before** and a **Do not play after** date, both
included, in the station time zone, such as 1 December until 26 December for Christmas songs.
Outside them the category's songs are left out exactly as outside its hours, and a clockwheel
step for it is skipped with "outside its dates" (or "outside its days" on a day it does not keep).
The server log names what holds the songs back, such as "every song is outside its category's
dates". Dates combine with days and hours: a Friday
22:00 until 02:00 window whose last date is that Friday still plays until 02:00 on Saturday. The
list says the season in words, such as "Plays only between 1 December 2026 and 26 December
2026", and "Outside its dates now." A category without dates is saved without the two fields.

**Repeating seasons.** **Repeat dates**, under the two dates, says how they are read:

- **Don't repeat**, the default: the fixed dates above.
- **Every week**: the weekday of **Not before** to the weekday of **Do not play after**, every
  week from **Not before** on, with no end. **Not before** Friday 6 November and **Do not play
  after** Sunday 8 November plays every Friday to Sunday from 6 November. The window may run past
  Sunday, such as Saturday to Monday, and **Do not play after** is at most 6 days after **Not
  before**. Without **Do not play after**, it is that one weekday. The list says, for example,
  "Plays only every week from Friday to Sunday, starting 6 November 2026".
- **Every year**: the month and day only; the year is ignored. A range may run past New Year,
  such as 20 December to 2 January. Without **Do not play after**, or with the same month and
  day, it is that one day each year. 29 February counts as 28 February in a year that has none.
  The list says, for example, "Plays only every year between 20 December and 2 January".

A repeating season needs **Not before**. A season that does not repeat is saved without the
choice, so an older AutoDJ still reads the file.

Each station has its own categories, like its clockwheels: Library tools, Categories shows the
ones of the station the page controls. They are kept in the station's `categories.json`
(`<index_dir>/stations/<slug>/categories.json`), written atomically and
included in `autodj backup`; a damaged file is set aside as `categories.json.bad-<id>` and the
station starts with no categories. The playlist files are not backed up: they come from a script
or a music folder, like the music. `autodj doctor` lists categories with no songs, missing
playlist tracks, or a query or playlist that cannot be read.
