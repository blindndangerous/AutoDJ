# Prepare and air a show

Generate and edit a prepared song list, schedule or start it, and handle queue priority, DMCA skips and restart recovery.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Shows

To hear its joins privately before airing, use [Programme review](programme-review.md#programme-review-and-export)
with **Saved show** as the source. This creates a separate review snapshot and
does not change the saved list or schedule it.

A show is a list of songs prepared ahead of time, such as "Remembering the 2000s", that airs at
a scheduled time. Clockwheels pick songs as they play; a show fixes them in advance, so you can
read the list, change it, and know what will air. Shows are in **Library tools**, **Shows**,
below Clockwheels. Each station has its own.

## Planning a show

1. Press **Add show** and fill in:
   - **Name**, such as Remembering the 2000s. Names are unique within the station.
   - **Songs come from**: **A category** (see [Rotation categories](categories.md#rotation-categories); a
     category can hold any genre, year or beets field, such as `original_year:2000..2009`), or
     **A clockwheel** (see [Clockwheels](clockwheels.md#clockwheels)), whose steps may use conditions such as
     Genre is Rock and categories.
   - **Target length (minutes)**: 5 to 360.
   - **Station name while active** (optional): the station name listeners see while the show is
     on the air. Leave it empty to keep the name in use, the active clockwheel's or the normal one.
     See [A station name while a clockwheel or show is on the air](../streaming-and-relays.md#a-station-name-while-a-clockwheel-or-show-is-on-the-air).

   Press **Save show**, then **Open list** on the show.
2. Press **Generate list**. AutoDJ runs the rotation tester's dry run of the station's own picker
   (see [Testing the rotation](rotation-preview.md#testing-the-rotation)): a category is played as a one-step
   clockwheel that picks its songs at random, a clockwheel in its own order. The repeat rules,
   related artists and tracks, dayparting and the DMCA limits apply as they do on the air, from
   the station's real play history. Voice liners are left out of the list, and for a category so
   is any song outside it. Generating stops once the songs reach the target length; if the songs
   the rules allow run out first, the list is shorter and the page says why. Nothing on the air
   changes, and each generation gives a new list. **Pick as if at** (optional) generates as the
   station would at that date and time in the station time zone, so a category's season, days
   and hours and a step's hours count then: use it to plan a Christmas show in November.
   Generating runs off the web server's event loop, below normal priority, for at most 20
   seconds, one show at a time for the whole server.
3. Choose **Track info** in a song row to open its details. **Return to track list** returns
   to that song's row. Use the row's **Options** menu for **Move up**, **Move down** and
   **Remove**. Remove asks for confirmation. Alt+Up and Alt+Down also move the focused song.
   Unavailable moves remain reachable in the menu and explain their restriction. Focus stays
   with the moved song; removal returns to a nearby row or the search box when none remains. Under **Add
   songs**, search the library by title, artist or album and press **Add** to put a song at the
   end. Each change is saved at once and said once, with the new total against the target. A
   song whose file has gone from the library stays in the list, marked, and is skipped on the
   air. A song may be in the list more than once; each copy plays, and the list and its total
   warn about it. While the list is being generated, Move up, Move down, Remove and Add remain
   reachable but are marked unavailable and explain why, and the server refuses a change to the
   list (409) until the new list is saved. A list holds at most 500 songs, and a station at most
   200 shows.

In the saved show's **Options** menu, **Edit** opens a dialog for its name, source, target or
station name and keeps the list. Generate again to apply a new source. **Delete** in the same menu asks first and names the schedule events that play the show; those events
fail, in the run log, until you change them.

## Airing a show

Add an event under **Settings**, **Station**, **Schedule** with the action **Play a show** and the show (see
[Scheduler](scheduler.md#scheduler)): once on a date, or repeating, with an optional date range. **Start
after this song**, in the open list, airs it now, to test it.

- Timing: a **hard** event plays the show's first song at its time, fading out the song
  playing, as Play now does. A **soft** event queues the show's first songs at its time without
  cutting the song playing, so the show follows that song. With server audio or a stream, the
  song after it may already be mixed into its ending; the show then follows that one.
- The show feeds the queue: its first two songs go to the front of the queue, and as each one
  starts the next joins after the show's last waiting song. Liners, time checks, breaks, matched
  intros and the clockwheel's place behave as they do with any queued song. The scheduled show
  wins: its songs go ahead of everything already in the queue, listener requests included, and
  songs you queue while it plays wait after its songs; use **Play next** to put one before them.
  A listener request accepted while a show is on the air, or due to start within the request
  window, is placed after the show's waiting songs and the listener is told only that it plays
  "after the station's show", never a time; the operator's card says "After the show" and its
  name. A request queued earlier waits until the show ends, and when a show starts the
  operator's card adds "After the show" and its name to every request still in the queue, so it
  promises no place it cannot keep. (A listener is told only once, when sending the request.)
- A hard start is the same in every playback mode: the show's first song plays at once, and a
  song the operator chose with **Play next** plays right after it, then the rest of the show.
- The show's songs in the queue are its own entries. Removing or moving a song in Queue & Search
  acts on the very entry you pressed, so a listener's request for the same song never takes a
  show song's place, and a show song is never removed by mistake.
- Show songs are pre-approved: the repeat rules do not hold them back, as with any song you
  queue. The DMCA limits are never broken: in DMCA mode each song is checked where it would play
  just before it joins the queue, and the waiting songs again at every song change. A song that
  would break a limit is taken out, skipped and logged, and the next one is tried.
- When the list ends, the show is over and the station's own picking resumes. A new show
  replaces the one on the air, whose waiting songs leave the queue.
- The card says which show is on the air, how many songs have played, are waiting and are to
  come, and what was skipped and why. **Stop show** takes its waiting songs out of the queue,
  and only those; a song already mixed into the one playing still plays. If the queue keeps
  changing and a song cannot be taken out at once, the card says the show is stopping, nothing
  more is queued, and AutoDJ tries again at each song change until they are out.
- The show plays its list as it was when it started; changes made meanwhile count the next time.
- A restart resumes the show on the air: AutoDJ saves the show and its place in
  `show_on_air.json` in the station's state folder as it goes. Until a song plays again, the
  card says the show will resume at the next song, and from which song; **Stop show** cancels
  that. Then it goes on from the earliest song that had not started, songs it had queued
  included (the queue itself is not kept over a restart). It is not resumed when its list would
  have ended by then: the time it started plus the length of its songs plus 15 minutes. **Stop
  show** and the end of the list remove the file; if it cannot be removed (held open), AutoDJ
  writes a stopped marker into it, which is never resumed, and removes it at a later song
  change.
- `show_on_air.json` is runtime state and is left out of backups on purpose. A restore removes
  every station's `show_on_air.json`, so no show from before the restore resumes; restore warns
  if one cannot be removed.

The hour preview lists the rest of a show on the air as "Fixed: show" and its name, and a
scheduled show's songs the same way from the first song change after the event's time. A song the
DMCA limits would skip on the air is listed where it would have played as "Skipped from show" and
the show's name, taking no time, as on the air.


## Related tasks and contracts

- [Matched intro, ad and show APIs and storage](../../reference/operations/station-content-api.md)
- [Schedule timed station events](scheduler.md)
- [Test rotation and preview an hour](rotation-preview.md)

For production-specific speech, follow [Prepared show editions](prepared-editions.md).


## Shows and confirmed voice tracks

Manual start, Play now, replacement and Stop show preflight their queue changes.
When booked speech loses its pair, resolve Keep/Discard/Cancel before the Show
state changes. Ordinary automatic feeding and soft schedule events defer conflicts
with a visible reason. A hard scheduled Show releases displaced speech before
queue cleanup, including when it waited for the current song to finish.

Explicitly stopping a prepared edition does not create a new interrupted edition
to resume. An actual replacement or interruption retains the existing explicit
resume rules. Live queue decisions do not rewrite edition sources or approvals.
See [the voice tracking workflow](spoken-breaks.md#choose-and-confirm-a-future-song-change).
