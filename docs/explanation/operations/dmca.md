# How DMCA mode checks programming

Understand the station checks, exceptions and timing estimates. These application checks do not establish legal compliance.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## DMCA mode

DMCA mode keeps the station's programming inside the performance complement
of the US statutory webcasting licence (section 114, reported to
SoundExchange). This is general information, not legal advice: you remain
responsible for your licence and your reports.

Turn it on with **DMCA mode** in **Settings**, **General settings**, **Broadcasting**. The choice belongs to the selected station.

## The limits

In any three hours of actual plays, counting the song about to play:

- at most 3 songs from one album, and at most 2 of them in a row;
- at most 4 songs by one artist, and at most 3 of them in a row;
- at most 4 songs from one box set or compilation, and at most 3 of them in a row.

"Actual plays" are the song starts AutoDJ records, including plays from before
a restart, which it reads back from the play log. A song skipped after a few
seconds still counts.

Artists are split the way the repeat rules split them, so every artist a credit
names counts, featured artists included: "Mark Ronson feat. Bruno Mars", or a
title with "(feat. Bruno Mars)", counts as a play by both. Album names are
compared the way the repeat rules compare them (case, accents and punctuation
ignored), so two artists' albums with the same name count as one album. That is
stricter than the licence, never looser.

A box set or compilation is recognised from its album names: albums whose
names end in a disc or volume number, such as "Woodstock (Disc 2)", "Hits CD1",
"Anthology Vol. 3" or "Box (Disc 1 of 4)", count as one set. AutoDJ's index has
no album-artist or compilation tag, and a one-disc compilation needs none: it is
one album, and the album limit is the stricter one. A box set whose discs all
carry the same album name counts as one album, again the stricter limit.

## Where the limits apply

- Every pick mode: similar songs, discovery, random, least recently played and
  the clockwheel, including clock slots that do not enforce the repeat rules,
  and the first song of each set.
- When nothing the pick mode would choose keeps every limit, AutoDJ looks
  further rather than break one. It plays a song that keeps every limit from
  anywhere in the library, preferring one the repeat rules allow, then one not
  played recently, then any other (not the song playing or about to). The log
  says which song would have broken which limit and what plays instead.
- Only when no song in the whole library keeps every limit (a very small
  library) does AutoDJ break one: it plays a song that breaks the fewest limits,
  and logs a warning naming each one, avoiding a stop solely for a DMCA conflict.
- Songs you queue from Search or Library: Play now, Play next and Add to queue
  check the limits at the time the song is expected to start, timed as the
  hour preview times the queue (see [Expected start times](#expected-start-times)).
  If the song would break a limit, nothing is queued; a dialog names the limit in
  words and offers **Queue anyway** (or **Play anyway**) and **Cancel**. Choosing
  Queue anyway queues it and logs a warning; Cancel leaves the queue as it was.
  The result is spoken either way. The expected start is an estimate: skips and
  later queue edits move it, so near a three-hour boundary, check again before
  relying on it.
  A queued song plays even if it breaks a limit when it starts, and the log
  says so.

## Expected start times

The DMCA and repeat checks on queued songs, listener requests and shows use the hour preview's
timing for when a song will start:

- The song playing counts from where it is now, so a seek moves the queue.
- A song the server has already mixed keeps its mixed handoff and the speed it was mixed at.
- Other songs hand over at their saved start and segue cues, or the crossfade, at the current
  music speed. Liners keep their own length and overlap cues.
- A song put ahead of one that is already mixed counts as taking its place, as it does once the
  mix is redone. If the song playing ends before the new mix is ready, the change lands one song
  later.

Automatic liners, breaks not yet booked and skips are not known in advance, so the time is an
estimate.

## Public pages

Turn DMCA mode on or off in Settings > General settings > Broadcasting for the selected station. Saving it changes
the next song pick and public feed immediately. From the moment it turns on, the limits count
the last three hours of saved plays. Songs already picked or queued are not rechecked.
The public now-playing feed (`/api/nowplaying`), the `/status` page and the
track-change webhooks never include the next song in DMCA mode, whatever
`[nowplaying] show_next` says. In Settings, Stream, the "Show the next song
publicly" box stays in the tab order but is unavailable, with the reason read
as its description, and a line under the feed links says DMCA mode is on. The
song playing is shown while it plays: the feed gives its artist, title and
album, and the stream's ICY title its artist and title.

`autodj doctor` has a `dmca` check. With DMCA mode on, it warns about any
setting that conflicts with it and says how to fix it: today that is
`[nowplaying] show_next = true`.
