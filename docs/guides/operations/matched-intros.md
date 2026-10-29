# Add matched intros and conditional intro rules

Attach speech to a song or artist, configure conditional liner rules, and inspect eligibility and placement.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Matched intros

A matched intro is a short voice clip tied to one song, or to every song by one artist, such as
"Here's the new one from Example Artist". When that song comes up, AutoDJ mixes the clip into the
song change that leads into it. Matched intros need saved-point playback (Transition mode: Use
saved segue points) with the server mixing the audio (server audio or a stream), as breaks do;
otherwise Track info says why and they do not play.

## Adding one

Open **Track info** for the song (from Now Playing, History, or the lists in Queue & Search) and
go to **Matched intros**, below the cue points. It lists the intros **For this song** and **For
any song by** the song's artist, each with how often it plays, its play dates and how many seconds
of speech it holds, and a **Listen** and a **Remove** button. Listen plays the clip on the Preview
output device when one is chosen in Settings, Global, Preferences, and is pressed until the clip ends or you
press it again. Remove asks first and deletes the clip.

Under **Add an intro**:

- **Audio file**: MP3, WAV, Ogg, M4A, FLAC or AAC, at most `station.liner_upload_max_mib` (the
  liner upload limit) and one minute long. AutoDJ trims the silence before and after the voice.
- **Plays on**: **This song**, or **Any song by** the artist. The artist comes from the song's
  artist tag; a song without one takes only an intro for itself. A credit such as "Someone feat.
  Example Artist" counts as a song by Example Artist too.
- **Label**: optional words to tell intros apart, shown in the list and the hour preview.
- **Chance (percent)**: 1 to 100. At 50 the intro plays about every other time its song comes up.
- **Wait between plays (minutes)**: 0 to 10080 (a week), 60 by default. After an intro plays, or
  while a song change that holds it waits to air, it is passed over until this much time has gone.
- **Not before** and **Do not play after**: optional days, both included, in the station time zone,
  as for liners (see [Liner and cart play dates](carts-and-liners.md#liner-and-cart-play-dates)).

Choose **Upload intro**. A field the page or the server refuses is marked, its error is shown under
it and it takes focus; any other result is said once.

## How one is chosen and placed

AutoDJ picks the intro as it prepares the song change, one song ahead. The song's own intros come
first; when none of them can play, the artist's are tried. An intro is passed over outside its
dates (by the day the song change airs) and within its wait; of the rest, the one heard longest ago
is taken if its chance comes up. If a song intro misses its chance, the artist's intros are tried.
The chance is rolled on a random source of its own, so the rotation tester and the hour preview
keep predicting the same songs.

A matched intro takes the place of the automatic liner at that song change by default, and when it
does the liner trigger stays due for the next one. In Voice liners settings, turn on **Only when no liner is
scheduled** to use a matched intro only when no queued, clockwheel or automatic liner is planned
for the change. With the option off, queued and clockwheel liners still play before the intro, as
do news, weather and music news breaks. A DJ break booked for the change, from an announcer program
or the Voice tracks tab, wins: the DJ already talks the song up, so no intro is added.

For an intro uploaded in Track info, AutoDJ detects the speech's Start, Segue and End, trimming
leading and trailing silence while keeping pauses inside the speech. It then places the clip like
a liner with Overlap start 0 and Allow start over outro enabled. The speech can span the ending
song's Outro and the upcoming song's Intro, including partial overlap when the intro is short.
If the available space is too short, the upcoming song waits so the speech ends before its vocals.
Without an Intro point, the upcoming song waits until the speech ends; without an Outro point,
the speech cannot start before the ending song's Segue. No additional settings are needed.
The music ducks under uploaded speech as it does under a break (`[breaks] duck_db`, or
`[playback] liners_duck_db` when that is unset). With Smooth or Stereo Tool processing the upload
gets the voice liner processing.

A clip chosen by an intro rule is a registered liner. Its saved Start, Segue, End and Overlap start
cue points control where it plays, as they do for other liners; Overlap start still respects the
incoming song's Intro point. A clip that cannot be decoded is left out and the music plays on. The
hour preview lists an intro once its song change is mixed, as "Matched intro" and its label.

## Intro rules

An intro rule plays a voice liner as the next song starts when that song matches the rule's
conditions: a name drop for an artist, a jingle before rock songs, or "here's one from the eighties"
before songs first released in the 1980s. Each station keeps its own rules. Manage them in
**Library tools**, **Intro rules**, below Shows. Each rule is listed in words with an
**Edit** and a **Delete** button; Delete asks first and leaves the liners in the library.

**Add intro rule** (or Edit) opens the same modal form:

- **Label**: optional words to tell rules apart, shown in the list.
- **Match conditions** and the conditions themselves: the same 1 to 32 condition rows as clockwheel
  steps and categories, with **Match all** or **Match any**. A rule can match the artist (Artist
  contains, for a name drop), title, album, genre, year (Year between 1980 and 1989 for a decade),
  BPM, duration, any Beets field (`original_year` is the first release, with the release year
  standing in when Beets has none), a registered music folder, or a saved music category. A song
  with no value for a field, or a category that no longer exists, never matches a condition on it.
- **Voice liners to choose from**: **Choose specific liners** keeps the checkboxes and optional
  registered **Liner folder** (every registered liner in it and its subfolders). **Match filenames**
  instead filters that folder using a pattern, such as `year-{year}-*.wav`. One matching clip is
  picked at random. Missing files and clips outside the selected station's liner play dates
  are passed over.
- **Priority**: where the rule stands next to the song's own matched intros and its artist's.
  By default rules come after both; a rule can come after the song's own intros and before the
  artist's, or before both.
- **Chance (percent)**, **Wait between plays (minutes)**, **Not before** and **Do not play after**:
  as for a matched intro. The wait holds across restarts.

Filename patterns support `{year}`, `{artist}`, `{title}`, `{album}`, `{genre}` and `{bpm}` from
that upcoming song's indexed metadata. **Insert song field** adds a placeholder at the cursor.
BPM is rounded to the nearest whole number (119.5 becomes 120). Matching ignores letter case;
`*` matches any text and `?` one character, including in the extension. For example,
`year-{year}-*.wav` can choose `year-1988-a.wav` or `year-1988-b.wav` for a 1988 song.

Only wildcards typed in the pattern are active: punctuation in a song field is literal.
Patterns match filenames, not folder paths; brackets are literal, and doubled braces (`{{` and
`}}`) match a literal brace. Unknown placeholders or malformed patterns are refused on save.
Missing metadata or no eligible filename match makes the rule ineligible for that song.
There is no automatic punctuation cleanup or fuzzy matching. Year comes from the song's year
field, independently of any year or Beets condition used by the rule.

**Test with a song** lets you search for a song and inspect the expanded pattern, matching files
and exclusions. It checks filename matches and current liner eligibility, not the rule's song
conditions, chance or cooldown. It does not play audio, save the rule or consume any reservation.
The **Wait between plays** applies to the whole rule: airing a 1982 clip also delays the next
1988 clip selected by that rule. Newly indexed matching liners join the folder pool normally.
Playback and hour preview use the same filename resolver. Clockwheel liner steps do not yet
support these placeholders.

Choose **Save intro rule**. A field the page or the server refuses is marked, its error is shown
under it and it takes focus; any other result is said once.

At each song change AutoDJ tries, in priority order, the song's own intros, the artist's and the
rules. Among the rules of one priority whose dates, wait and conditions allow them, the one heard
longest ago is tried first, and its chance is rolled on the matched intros' own random source, so
the rotation tester and the hour preview keep predicting the same songs. A chosen clip is placed
as a registered liner, using its saved Start, Segue, End, and Overlap start cues; it can therefore
overlap the incoming song's Intro. Track-info song and artist uploads use detected speech cues
with the same liner placement. Rule clips follow the registered liner's **Allow start over outro** setting too. See
[liner cue points](carts-and-liners.md#liner-cue-points). A chosen clip takes the automatic liner's
place in the same way, and gives way in the same way to a DJ break and to **Only when no liner is
scheduled**. A song change that is rendered again, or that ended before its intro aired, gives the
rule back for the next one.

Rules are chosen by the background playout preparation worker, never on the audio thread. The
streaming path uses its live playout worker; local playback keeps render-ahead preparation. Measured on
Windows with Python 3.14 and synthetic data (no audio decoded), 100 rules of two conditions each
took 3.3 ms median and 6.9 ms at the 95th percentile per song change against 1,000 songs, and 4.2
ms and 9.0 ms against 50,000; the music library's size does not enter the checks. A liner folder's
clips are gathered by the station's upkeep, which looks every 5 seconds, off the render path: 40 ms
for 50,000 registered liners, once each time the liner library changes; until then the clips
gathered before serve, less any no longer registered. A song change then took 0.3 ms median
and 1.3 ms at the 95th percentile with that folder. For one song change at most 256 clips of a
large folder are looked at, so a folder of missing files cannot hold up the render. Filename
patterns first filter the in-memory names; a bounded cache reuses recently expanded patterns
until the folder pool changes. The timings above do not include a pattern's first filtering pass.

`intro_rules.json` in the station's state folder holds the rules and when each last aired; backups
include it. When it cannot be read just now, the rules last read are used and saves are refused,
so nothing is written over it. A rule in it that is not valid is skipped, logged and named under
the Intro rules heading, and written back as it was with every save; only a file that is not a
file of rules at all is set aside, as `intro_rules.json.bad-<id>`.


## Related tasks and contracts

- [Matched intro, ad and show APIs and storage](../../reference/operations/station-content-api.md)
- [Book announcer breaks and browser voice tracks](spoken-breaks.md)
- [Clockwheel conditions, category rules and storage](../../reference/operations/clock-and-category-rules.md)
