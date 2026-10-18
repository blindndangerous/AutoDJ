# Schedule timed station events

Create events, choose their date windows and timing, and check Recent runs and recovery behavior.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Scheduler

Open **Settings**, then **Schedule** to add timed station events. An event runs once, every day,
on weekdays (Monday to Friday), on chosen days, or every N minutes. An every-N-minutes event can
have an active window, such as 22:00 until 02:00; a window may cross midnight, and both ends run.
Without a window the count starts again at local midnight. An every-N-minutes event can also keep
to chosen days; leave every day unchecked to run every day.

Any repeating event can have a **Date range**: an optional start date, end date, or both. A
one-time event's date is its whole range, so the form hides **Date range** for it, unless the
event was saved with a range: then the dates stay in view to keep or clear. The event runs
only on dates from the start date to the end date, both included, in the station time zone, and
the **Next events** list skips the dates outside it. An active window that crosses midnight
belongs to the day it starts: a Friday 18:00 until 02:00 window whose end date is that Friday
runs until 02:00 on Saturday, and a window that started the day before the start date does not
run. The events list says the range in words, such as "Every day at 00:00, from 27 November to 26
December 2026", and an event with no run left says "Ended, no runs left". An event whose range
has no run left cannot be saved enabled; save it disabled, or move its end date. Schedule files
from earlier versions load unchanged, and an event without a range is saved without the two
fields.

A seasonal clockwheel, such as a holiday one from 27 November to 26 December, takes two events:

1. **Holiday music**: repeat every day at 00:00, start date 27 November, end date 26 December,
   action **Change song picking** to the holiday clockwheel. Running every day means a manual
   change, or a midnight missed while AutoDJ was stopped, is put right the next day.
2. **Regular music**: once on 27 December at 00:00, action **Change song picking** back to the
   usual clockwheel.

Use **Run now** on the first event to start the season at once when you add it partway through.

Times use `station.timezone in the station definition`, an IANA name such as `America/Denver`, or the system time
zone when that is not set (UTC when the system has no IANA name). **Time for the selected station** in
Settings > Stations sets that station's time zone from the page; the saved choice applies
when that station next starts. The same fieldset sets **Time format**, 24 hour
(the default) or 12 hour, for the times every page shows; it applies at once and changes nothing
but how times look. The Schedule panel shows the
zone in use. When daylight saving time skips a time, the event runs at the moment the clock
jumps; when a time happens twice, the event runs the first time only.

Actions:

- Switch to a saved profile or a preset.
- Switch the Stereo Tool preset, to Included or an uploaded one. It takes effect when Output
  processing is Stereo Tool, restarts the processor at once and never skips the song playing. A
  preset deleted after the event was saved makes its runs fail in words.
- Change song picking. Besides similar songs, random and least recently played, the choices are
  the active clockwheel and each saved clockwheel by name; a named one also becomes the active one.
- Play a voice liner or station ID. This needs server audio or stream mode.
- Play a cart by page and cart number, with its saved duck, over or replace mode. This needs
  server audio or stream mode.
- Play a time check. This needs server audio or stream mode.
- Start or stop the stream (stream mode). Start keeps the station playing without listeners,
  stop keeps it off the air even with listeners, and automatic hands control back to the
  listener count. The choice lasts until the next stream event or a restart.
- Turn discovery on or off. Discovery needs a rate set in Song picking before it can be on.
- Play a show: its prepared list of songs goes to the queue (see [Shows](shows.md#shows)).

Timing is soft or hard. A soft event waits for the current song to end; with nothing playing it
runs at once. A hard event runs at its time and fades out the current song through the normal
skip: after the change for profile, preset, song picking and discovery events, so the next song
follows it, and before the liner for liner events. A show event is the exception: hard plays the
show's first song at once, and soft queues it at its time without waiting (see [Shows](shows.md#shows)). Stream and Stereo Tool preset events never
skip.

For a hard event, `wait_seconds` optionally waits for the current song when it will finish within
that many seconds after the event time. The action then runs at the song change without a skip.
If the song stalls or stays paused past the limit, the event runs at the limit through the normal
hard skip path.
The default is 0, which keeps immediate hard timing; accepted values are 0 to 300 seconds. Set it
in the event's **Wait up to seconds** field, shown when Timing is Hard, or as `wait_seconds` in
the schedule API. Soft events already wait and do not accept a nonzero value. Stream and show
events, which never fade the song out, never wait. An event with no
wait is saved without the key, so an older AutoDJ still reads the schedule; one with a wait is
refused by an older version, which keeps a copy of the file.

**Run now** runs an event's action at once, to test it. Each run, failure and missed event goes
to the server log and to **Recent runs**, which keeps the last 200.

Schedules live in `schedule.json` in the station's folder, next to `web_state.json`. AutoDJ
writes the file atomically and includes it in `autodj backup`. A damaged schedule file is copied
to `schedule.json.bad-<id>` and left out, so the server still starts. An event whose action this
version does not have (one a newer AutoDJ added) never runs and is not listed, but it is logged
and written back exactly as it was with every save, so going back and forth between versions
loses no event; it counts toward the 100 events. An event for an action this version has, but
with settings it does not accept, is copied to `schedule.json.bad-<id>` and left out, as
before. Events due before startup are not replayed: a one-time event whose time passed while
AutoDJ was stopped is disabled and recorded as missed, including one due exactly at startup.
After a stall, each event gets one missed record that counts occurrences more than two minutes
late. The latest occurrence still within that window runs once. The schedule holds at most 100
events.
