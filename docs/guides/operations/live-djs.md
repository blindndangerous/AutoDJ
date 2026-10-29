# Connect a live DJ and use browser talkover

Add a station DJ, connect the source, hand back to automation, or talk over the music from a paired browser.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Live DJs and talkover

A live DJ can take over a station from their own DJ software, and anyone signed in to the page
can talk over the music from the browser. Both need that station's stream output, because the
server mixes what goes on the air. The `serve --stream` option can override the default station's
saved output for that run.

**Adding DJs.** In **Settings > Station > Broadcasting**, add each DJ with a username, the name
listeners see (for example "DJ Alice") and a password of 12 to 256 characters. Passwords are
never shown again; editing a DJ with the password left blank keeps the saved one. Each station
has its own DJs, kept in that station's `live_djs.json` as scrypt hashes, written atomically and,
on POSIX systems, readable only by the server's user. Removing a DJ who is on the air takes them
off at once. A backup keeps the DJs and the talkover settings but not the password hashes, so
after a restore each DJ shows "no password" until you set their password again.

**Connecting BUTT or Mixxx.** Live DJs connect to the station's source mount on AutoDJ's own
address and port, with the same settings as an Icecast 2 server:

- Server type: Icecast (Icecast 2).
- Address and port: the ones the page is open on, for example `radio.example.com` and `8080`.
- Mount: `live` for the default station, `stations/<name>/live` for another station. The
  Live DJ tab shows the full address for the station the page controls.
- Username and password: the DJ's. (Not `source`: each DJ has their own login.)
- Format: MP3, AAC or Ogg Vorbis, at up to 512 kbit/s.
- Encryption: turn on SSL/TLS in BUTT ("Use SSL/TLS") when AutoDJ serves HTTPS.

A DJ's password goes in an HTTP Basic login, so AutoDJ accepts it only over HTTPS or from the
machine AutoDJ runs on. To let DJs on your local network connect over plain HTTP, set
`live_plain_http_lan = true` under `[server]`; their passwords then cross your LAN unencrypted,
so use it only on a network you trust. It never admits plain HTTP from outside the private
address ranges, or through a proxy. Five wrong passwords from one address in a minute (one
IPv6 /64) lock that address out for the rest of the minute, and one station takes one DJ at a
time: a second DJ is refused until the first disconnects.

**Going on the air.** A DJ who connects is on standby until their software sends sound: silence
does not take the station. When sound arrives, AutoDJ buffers about 0.3 seconds of it and
crossfades from the music to the DJ over one second, at equal power so the level holds. From
that moment no new song starts: if the song playing ends during the crossfade, the DJ comes in
over silence rather than the next song. The interrupted song's History row ends when the DJ
came on. While the DJ is on, automation waits exactly where it was: nothing is added to the play history or the repeat rules, the clockwheel does not move
on, and the queue, a show on the air and any waiting breaks stay as they were. The stream
title, the status page and the public feed show the DJ's name and "Live", and the page says
"Live: DJ Alice since 21:02" once, politely. The station keeps playing while a DJ or a talker
is connected, even with no listeners.

**Handing back.** When the DJ disconnects, or sends only silence for 15 seconds, automation
takes back the air. It does not resume the interrupted song: the song that was due next is
rendered again for now (its time check and liners were planned before the DJ came on), and
the DJ's last buffered audio crossfades into it. If that render takes longer than the DJ's
remaining audio, the song as rendered before the DJ came on plays instead as a fallback. This does not
guarantee uninterrupted audio under arbitrary resource pressure. A station with no set playing (no listeners and nothing keeping it on) goes back to
idle.

**Scheduled events while a DJ is on.** Events still run at their time and are logged as usual,
but AutoDJ does not put its own audio over a live DJ:

- An event that changes the song with hard timing (**Play a show**, a song played now) takes
  effect when the DJ hands back: its song is the first one automation plays.
- Soft-timed events queue as usual and play in turn after the DJ.
- A station ID or other liner due while the DJ is on (fired by the schedule or **Test**) plays
  once when the DJ hands back, over the first song; if several fall due, only the latest plays.
  The log says it waits.
- A time check due while the DJ is on would be stale by then: it is skipped, and the log says
  so.
- Cart clips are refused while the DJ is on, and the cart wall says why.
- A voice break (#88) waiting for the next song change misses its slot: it is dropped and
  logged as missed, "live DJ on air". (The missed and late rules being designed in #215 will
  decide this in time.)

**When a source is cut off.** A connection is closed when it sends nothing for 10 seconds,
waits on standby (only silence) for 30 minutes, sends faster than 512 kbit/s over five seconds
(with a 64 KiB burst allowance), passes 2 GiB in one connection (about 14 hours at 320 kbit/s),
or when the decoder cannot keep up or fails. If the DJ's audio briefly runs out (a network
stall), AutoDJ plays silence, logs one warning, and resumes when 0.3 seconds have buffered
again. Each source is decoded by its own FFmpeg process, reading only the format the DJ's
software declared, from a pipe, with no access to files or the network; at most 2 seconds of
decoded audio wait for the mixer, and older audio is dropped first.

On the development PC (Windows 11, FFmpeg 8.1.2), decoding 30 seconds of 128 kbit/s MP3 the way
a source is decoded took 0.09 seconds of CPU and 16.3 MiB of memory for the FFmpeg process, so a
DJ costs well under 1% of one core. The mixer spends about 30 microseconds on each 20 ms block
while a DJ is on, less than with music (the music's EQ is skipped), and about 20 microseconds
more while someone talks over.

**Talkover from the browser.** The **Talk** button in the Controls card on Now Playing puts
your microphone on the air while you hold it: hold the mouse button or a finger on it, or
press and hold Space or Enter while it has focus, or hold **Alt+M** anywhere on the page.
Alt+M only ever talks while held. A quick press or click on the button itself (under about a
third of a second), such as a screen reader's click or a switch, keeps talk on until you press
Talk again instead: the page then shows "Talking until you press Talk again, for 2 minutes at
most." under the button and says so once, and talk turns itself off after 2 minutes, saying
"Talk turned off after 2 minutes." The button stays named "Talk" and reports pressed while you
are on the air; Alt+M also says "Talking." and "Stopped talking." The browser asks
for the microphone when you first press, and the page lets it go when you stop, leave the
window or tab, switch station or sign out. Alt+M is not turned off by the Settings keyboard
switch, which covers single-key shortcuts. While you talk, the
music (or the live DJ) is lowered by the **Duck amount** (12 dB to start with) within 40 ms,
and when you release it comes back up over the **Return time** (half a second to start with);
set both in **Settings > Station > Broadcasting**, per station. One person talks at a time on each
station; while someone else talks, Talk shows as unavailable. When the server has no sign-in
(pairing is off), anyone who can open the page can talk on the air and change the Live DJ
settings, as with every other control on the page. Talkover needs a secure page (HTTPS, or
`localhost`), because browsers give the microphone only to secure pages.

The browser sends 16-bit mono PCM at its own sample rate over a WebSocket, about 0.7 to 0.8
Mbit/s, rather than Opus: there is no decoder process per talker and no codec delay, and the
server resamples to the station's rate. Programs can use the same socket,
`wss://<host>/api/live/mic?rate=48000` (or `/api/stations/<name>/live/mic`), with the page's
session cookie and Origin: binary messages of little-endian 16-bit mono samples, at most 32 KiB
each, sent continuously while talking. The server closes the socket after 2 seconds without a
message, keeps at most half a second of a talker's audio, and closes a talker sending more than
twice real time.

**History and reports.** A DJ's set is one History row with the DJ's name, "Live", when it
started and, once over, when it ended and its length. It is in the CSV play log with kind
`live`, and left out of SoundExchange reports and the repeat rules: the music came from the
DJ's own software, which AutoDJ cannot see. Report a DJ's songs from their own software.

The routes, for scripts: `GET /api/live` (the DJ on the air, the DJs without their passwords,
the talkover settings and the source mount), `PATCH /api/settings` with the `live` group,
`POST /api/live/djs`
(`username`, `display_name`, and `password`, or `null` to keep the saved one),
`DELETE /api/live/djs/<username>`, each also under `/api/stations/<name>/`. They need a paired
browser session like the rest of the page.
