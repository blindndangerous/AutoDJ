# Test rotation and preview an hour

Run an isolated rotation test, inspect fixed and predicted hour-preview rows, and decide whether hour fitting suits the station.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Testing the rotation

Architecture and verification context: [Proposed picking and preview ADR](../../adr/0009-share-picking-rules-with-isolated-previews.md).

The rotation tester plays a span of time, or a number of songs, through the station's own song
picker and reports what would happen, without changing anything on air. It answers questions such
as "do a 720-minute song rule and a 240-minute artist rule starve my six-song Power category?"
before the rules go live.

It uses everything the station picks with: the pick mode or the active clockwheel and its place,
the rotation categories and their days and hours, the schedule's song-picking changes (with their
date ranges), the repeat rules including related artists and related tracks, and the DMCA limits. It starts from the
station's real play history and last-played times. The run picks with a private copy of the
player, with its own clock and its own random choices, so the on-air player, its history, the
clockwheel place and the library are never touched.

From the command line:

```bash
uv run autodj simulate                       # the next 24 hours
uv run autodj simulate --days 7 --seed 42    # a week, repeatable with the same seed
uv run autodj simulate --picks 500 --json    # 500 songs, as JSON
uv run autodj simulate --hours 12 --start 2026-10-16T18:00 --station nightowl
```

`--station` names the default station or one with a canonical station definition; the run uses
that station's settings, clockwheels, schedule and plays. `--start` is read in the station time
zone (`station.timezone in the station definition`) and must fall between
2000 and 2099. Without `--seed` a run draws a seed and the report gives it, so the same seed repeats
a run song for song, in every pick mode. Runs are capped at 30 days or 20,000 songs, and at ten
minutes of work: a run that reaches that limit stops with the songs picked so far, and the report
says so.

With breaks on, the station picks the song after the next one a song early so
announcer breaks can name it (see [Announcer breaks](spoken-breaks.md#announcer-breaks-dj-breaks-news-and-weather)),
and a booked break keeps that song whatever the queue does. The rotation tester does not model
either: it picks each song when the one before it ends, as a station with breaks off does.

`autodj simulate` can run beside a running server. It opens `library.db` read-only: no write
lock, no migration, and the file itself is never written, though SQLite may create the
`library.db-wal` and `library.db-shm` files beside it, as any reader does. A library it cannot
read (damaged, not an AutoDJ library, or left half written) stops the command with a message. A
categories or liner dates file it cannot read is skipped, never moved aside.

In the web page: Settings > Song picking > **Test rotation**. Choose a span or a number of songs,
optionally a start and a seed, and press **Test rotation**. The run works in the background; a
progress bar and a line of text show how far it has got, **Cancel test** stops it with the results
so far, and the end is announced once. The results appear under **Rotation test results** as
headings and lists. One run at a time per server, whichever station it tests. The run's thread
works below normal priority and sleeps for 2 milliseconds after each song it picks, so the audio
and the page keep their place; stopping the server cancels a run and waits for it. If the page
cannot reach the server while a run works, it tries again less and less often, says so once, and
after six failed tries says it has stopped checking.

The report, in words or JSON, gives:

- plays per song, per category and per artist: the fewest, the most and the average, and the most
  played;
- songs never played, with a count and the first ten, overall and per category;
- how often each rule had to be relaxed, and which: same artist, related artist, same album, same
  title, related track, same daypart, same quarter hour, the track repeat window, dayparting and
  back-to-back repeats, plus fallbacks to a random
  song. A clockwheel step set not to enforce the rules is counted apart;
- clockwheel steps that were skipped or came up empty, with the reason, such as "all matching
  entries blocked by rules" or "outside its hours";
- DMCA conflicts: songs replaced to keep the limits, and limits broken when nothing could keep
  them, by kind;
- dead air: picks that found nothing (the station starts afresh) and minutes with nothing
  playable;
- the schedule changes it applied.

What it approximates:

- Time moves by each song's length from the catalog: from its saved start to its segue or outro
  with saved-point transitions, less the crossfade otherwise. A song with no length counts as the
  library's average.
- A clockwheel liner adds its full length. Automatic liners, time checks, carts and the queue are
  left out, and so are shows, which play through the queue (the hour preview lists them).
- A scheduled change applies from the first song after its time; hard events do not cut a song
  short. Profile switches are not simulated; the report names them.

How long a run takes grows with the library. On a synthetic 77,000-song library with four
categories and a seven-step clockwheel, an older four-core PC (2018) simulated a week (about 2,500
songs) in about two minutes, and in a little over three with the DMCA limits on.

## Hour preview and backtiming

For audible shortened mixes and exports, use [Programme review](programme-review.md#programme-review-and-export).
Hour preview remains the forecast and backtiming display described below.

Now Playing > **Hour preview** lists what the station plays next, with the time each item is
expected to start, and says how the hour lands: how early or late the song changes fall against
each clockwheel restart and each scheduled event, and the top of the hour when **Fit songs to
the hour** is on. Choose the next 30
minutes, hour, 2 hours or 3 hours under **Show**.

Each row says in its Status column whether it is fixed or predicted:

- **Fixed: on the air**: the song playing now, with the liners, breaks and matched intros mixed
  into it.
- **Fixed: ready to play**: what the server has already rendered, with its liners, breaks and
  matched intros (server audio and stream mode only).
- **Fixed: picked**: the song chosen to follow, and when external breaks, a due ad break or a
  manual voice tracker needs an open song change, the song after it and its liners.
- **Fixed: booked break**: an announcer break, a voice track or an ad spot booked on the open
  song change. An ad spot reads as "Ad break" with its advertiser and title.
- **Fixed: in the queue**: the request queue, which plays before any pick.
- **Fixed: show** and its name: the songs of a show, the rest of the one on the air after the
  queue and a scheduled one's from the first song change after its time (see [Shows](shows.md#shows)).
- **Skipped from show** and its name: a show's song that would break a DMCA limit there, as it
  is skipped on the air; it takes no time.
- **Predicted**: the rest, from a dry run of the station's own picker that starts where the
  fixed part ends, with the station's clockwheel place, play history, repeat rules, DMCA limits,
  categories and schedule. It is the same dry run the rotation tester uses (see
  [Testing the rotation](#testing-the-rotation)), so it never touches the air: the station's
  random choices, clockwheel place, history and library are only read.
- **Predicted** ad breaks (see [Ad breaks](ad-breaks.md#ad-breaks)): a break due in the span and not yet
  booked joins the first predicted song change at or after its time, with the spots its rules
  would pick now and their length; the songs after it move later by that much. Its time is also
  a fixed point below, as "Ad break" and its name.
- Clockwheel ad steps appear as **Predicted** ad breaks with their expected length. When no spot
  would play, the row has length 0 and says why.

Predicted songs also include song/artist intros and Matched intros rules, using the selected
station's categories, play dates, cooldowns and liner precedence. Automatic liners use the
same trigger rules as playback. Talk-ups overlap the song; clips that play before it move
its start later. These predictions use isolated selection state, so opening the preview
cannot consume an intro or liner reservation.

Scheduled liners, carts and time checks appear as audio rows, alongside their timing markers.
Carts overlay the music. Hard events that interrupt playback cut the predicted song; an event
that waits within its configured allowance leaves the song intact. Automatic after-hour time
checks appear at the next eligible song start. If a time check needs newly synthesized speech,
its duration is unknown and the prediction stops with an explanation. Random choices and
chance-based rules remain estimates; the live station may choose differently. A scheduled
stream stop or return to listener-dependent automatic mode ends the prediction there.
Unbooked voice tracks, live DJs and manual playback changes cannot be predicted.

Backtiming goes to each fixed point in the span:

- **Clockwheel restart**: the station restarts the clockwheel at the nearer song change, as it
  does on the air. "Top of the hour, clockwheel restart, 13:00: expected 0:35 early" means the
  song change nearest the restart comes 35 seconds before it; the line names the song that causes
  it, whether that song is fixed or predicted, and how far it runs past the restart. Liners and
  breaks that run straight into a song start its song change.
- **Scheduled events**: a soft event waits for the song playing to end, so the preview says how
  late it runs. A hard event runs on time; the preview reports a cut only when its action skips
  the playing song. Ad breaks and stream changes leave the song playing.

The card shows how many rows are fixed and predicted, such as "3 fixed items, 20 predicted,
through 13:40.", with the backtiming lines under it. **Refresh preview** and **Shift+H** (from
any tab) say the next of these points and how it lands, such as "Next clockwheel restart, 13:30:
expected 0:35 early." While the card is open and Now Playing is showing, the
table refreshes every 30 seconds and shortly after the song changes, changing cells in place,
saying nothing and leaving focus where it is; if those refreshes start failing, the failure is
said once, and not again until one succeeds. Choosing another span under **Show** says its
counts once. If more than 24 fixed points fall in the span, the first 24 are timed and the card
says so. Timestamps and durations use the same compact text on screen and in speech, such as
"14:04:30" and "3:45" (24 hour). Times are in the station
time zone (`station.timezone in the station definition`); clockwheel restarts are worked out in the computer's own
time zone, as the player does.

The page asks the server only while the card is open. Each preview runs on one worker thread
below normal priority, never on the web server's event loop and never on the audio threads. The
dry run stops after 80 songs, or once it has worked about 4 seconds, and says so; the time limit
is checked before each pick, so one slow pick can run a little past it. A preview is reused for
45 seconds while nothing new is fixed, longer than the page's 30-second refresh, so an open card
costs no dry run until a song changes. Several pages asking for the same station and span at once
share one preview, at most 8 different previews wait for the worker, and one more is answered
with 503 and "try again in a few seconds". At shutdown the server waits for the worker; if it has
not stopped, the server logs an error and leaves the library open rather than close it under the
preview. The same fixed part always gives the same prediction, so a refresh does not reshuffle
the predicted songs. Every station has its own:
`GET /api/hour-preview?minutes=60` for the default station and
`GET /api/stations/<name>/hour-preview` for any station, each answering with JSON that holds the
items, the fixed points with their sentences, and the summary.

What it approximates, as the rotation tester does: a song runs from its saved start to its segue,
or its length less the crossfade, scaled to wall time at the configured speed; liners and booked
breaks contribute their placement-adjusted time, and trimmed audio tails can still differ from
the catalog length. Automatic liners, matched intros, scheduled carts and time checks are
included when their local audio duration is known; a check that needs new synthesis stops
the prediction with an explanation. Unbooked voice tracks, live DJs and manual changes remain
unpredictable. A scheduled song-picking change affects the next eligible selection. With hour fitting on, predicted picks use the same fitting choice
as live picks.

To fit song changes near a fixed point, tick **Fit songs to the hour** in Settings > Song picking
(`hour_fit = true` under `[playback]` for the station). **Minutes before to start fitting**
(`hour_fit_window_minutes`) defaults to 10 and accepts 1 to 30. The setting is off by default.
The picker considers the next top of hour, clockwheel restart or hard schedule event within that
window and prefers a song whose estimated handoff lands closest to it. See
[Hour fitting](../../track-selection.md#hour-fitting) for the eligible pool and cost.


## Related tasks and contracts

- [How DMCA mode checks programming](../../explanation/operations/dmca.md)
- [Clockwheel conditions, category rules and storage](../../reference/operations/clock-and-category-rules.md)
