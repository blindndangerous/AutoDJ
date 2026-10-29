# Schedule ad breaks and read the ad report

Add spots and breaks, check the fill and missed-spot rules, and export the station's ad report.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Ad breaks

The **Ads** tab keeps a station's spots (the ads) and its ad breaks, and reports what aired. An ad
break plays some spots between two songs: it never cuts a song, it joins the next song change it
can, about a song after its time, as an announcer's standalone break does. Ad breaks need
`[ads] enabled = true` for the station and saved-point playback (Transition mode: Use saved
segue points) with the server mixing the audio (server audio or a stream); otherwise the tab says
why at the top. Announcer breaks can stay off. To have a song change open for a break, the
station plans each change one song ahead from 20 minutes before a break is due (by its minutes or
a Schedule event), while one waits or is booked, and after Run now. The rest of the time songs are
picked exactly as with ads off; the rotation tester and the hour preview pick the same songs with
ads on or off.

```json
{
  "ads": {
    "enabled": true,
    "max_upload_mb": 20,
    "max_spot_seconds": 120.0,
    "max_spots": 200
  }
}
```

Each station has its own ads settings, spots, breaks and report; select the station before editing
these settings. The example shows these keys inside the top-level `station` object in its
`station.json` definition.

## Spots

Choose **Add spot** and give:

- **Audio file**: MP3, WAV, OGG, M4A, FLAC or AAC, at most `max_upload_mb` and
  `max_spot_seconds`. AutoDJ trims the silence before and after the sound, as it does for a break.
- **Spot title** and **Advertiser**, such as "Fall sale" from "Example Motors".
- **Campaign**: optional, such as "Autumn". The report adds up by advertiser and campaign.
- **Competing category**: optional, such as "Car dealers". Spots with the same category compete.
- **First day** and **Last day**: the flight, both days included, in the station time zone.
- **Plays a day** and **Plays an hour**: the most times it airs in one station day and one clock
  hour. 0 is no limit.
- **Airs**: clear it to keep the spot without airing it.

**Edit** changes everything but the audio; to replace the audio, add a new spot and delete the old
one. **Delete** asks first and removes the spot and its audio; a spot booked for a coming song
change is kept until it has aired. What a spot aired stays in the report after it is deleted.

## Ad breaks and when they run

Choose **Add ad break** and give a **Break name** and the **Minutes past the hour** it runs at,
such as `15, 45` for quarter past and quarter to. **From hour** and **Until hour** keep it to part
of the day, **Days** to chosen weekdays (none checked is every day), and **First day** and **Last
day** to a date range. **Spots in the break** (1 to 10) and **Longest break in seconds** (0 for no
limit) cap what it holds. Leave the minutes empty for a break that runs only from a Schedule event
or **Run now**.

For anything the minutes cannot say, add a **Schedule** event (Settings, Station, Schedule) with the action
**Ad break** and the break's name: once on a date, daily, on chosen days or every few minutes, with
a date range. Choose **Hard** timing for it: an ad break never fades a song out, so Hard only means
it is booked at its time, where Soft first waits for the playing song to end. The hour preview
shows each run as a fixed point to time to.

**Run now** books a break at the next song change it can join, which is handy for a test.

## Ad breaks in a clockwheel

A [clockwheel](clockwheels.md#clockwheels) can place an ad break before its next song. The report lists its
source as "Clockwheel step" and its break as "Clockwheel <name>, step <n>". If a song change is
re-planned, the new change books the step again; spots dropped from the old change are reported
as missed.

## How a break is filled

When a break falls due, AutoDJ picks its spots:

- A spot is passed over when it is turned off, outside its flight, at its daily or hourly limit,
  or missing its audio file.
- **Separation** (in **Rules for every break**, 15 minutes by default): a spot is passed over when
  a spot from the same advertiser, or with the same competing category, aired or is booked within
  that many minutes. 0 lets them follow each other in the next break.
- One break never holds two spots from one advertiser or one competing category.
- Of the rest, the spot heard longest ago goes first, then the one heard least today; the break
  takes them until it holds its spots or its seconds.

The spots are booked on the open song change and play one after another between the songs; the
next song starts as the last one ends. With Smooth or Stereo Tool processing they get the voice
liner processing. A spot whose file cannot be decoded is left out and the music plays on.

**Waiting for a song change** lists the runs that fell due and are not booked yet, and **Latest
results** what became of the last ones since AutoDJ started.

## Late and missed spots

Nothing is ever dropped without a word in the report:

- A spot that airs more than **Late after** minutes (10 by default) after its break's time is
  reported as aired late, with how late.
- A break that cannot air within **Missed after** minutes (30 by default) is dropped and reported
  missed, with the reason: no song change could take it (the stream was stopped, say), or its
  song change would air too late.
- A break that held no spot is reported missed with the reason, such as "2 spots are at their
  daily limit".
- A spot booked on a song change that was skipped, jumped over or not rendered is reported missed
  with what happened ("skipped before it aired").
- A spot that had started when a skip or a stop cut it off is reported as **May have aired**,
  with what happened ("AutoDJ stopped during the break"): neither aired nor missed.
- One rule covers lost time: a break whose time passed while the computer slept, AutoDJ stalled or
  AutoDJ was not running still joins the next song change, to air late, when it is within
  **Missed after**; past that it is reported missed ("the computer slept or AutoDJ stalled", or
  "AutoDJ was not running"). AutoDJ saves where the ad schedule is every minute and at a stop, so
  the next start knows which breaks fell due while it was down (up to 35 days back). A start with
  ads off clears that place: no break is owed for the time ads were off.
- After a long sleep or stop, the first 200 missed breaks are logged one by one and the rest in
  one line that says how many and from when to when, so the report counts every one.
- A spot booked when AutoDJ stopped is reported missed ("AutoDJ stopped before it aired"), at the
  stop, or at the next start after a crash.
- Lines the log cannot take at a stop (a full disk, a share away) are tried once more, then kept
  in `ad_plays-unwritten.jsonl` beside `ads.json` and added to the log at the next start; the
  server log says how many.

## The ad report

Open the **Ad report** card, closed until you open it, and choose **From** and **To** (both days included, in the station time zone)
and **Show report**. The summary is said once; below it are two tables with column headers:
**Totals by advertiser and campaign** (aired, aired late, missed, may have aired, waiting and
seconds aired) and
**Every spot** (the break's time, when it aired, the result, the spot, the break and the reason).
Each table sits in a region that takes focus, so on a narrow window it scrolls sideways with the
arrow keys. The page lists the first 1000 spots; **Export report** saves them all as CSV:
**Every spot** for an affidavit, or the **Totals**. The play log export (History, and `autodj
export-plays`) also lists the spots that aired, as `kind` `ad`, with the advertiser as the artist
and the campaign as the album; the SoundExchange report leaves them out.


## Related tasks and contracts

- [Matched intro, ad and show APIs and storage](../../reference/operations/station-content-api.md)
- [Read History and export play logs and webcasting reports](play-log-export.md)
