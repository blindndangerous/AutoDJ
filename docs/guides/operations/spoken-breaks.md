# Book announcer breaks and browser voice tracks

Enable external announcer uploads or record in a paired browser, and book speech for the open song change.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Announcer breaks (DJ breaks, news and weather)

An announcer program on the station's own machine can send a station spoken breaks: a DJ talking
about the songs, news, weather or music news. It asks AutoDJ which song change is still open,
writes a break for that pair of songs, and uploads it. AutoDJ mixes the break into that song
change, reports when it airs, and deletes the file afterwards.

AutoDJ prepares each song change one song ahead: while one song plays, the next one is already
rendered together with its handoff. So a break cannot join the change that is coming up; it joins
the one after, the *open slot*. As soon as the next song is rendered, AutoDJ also picks the song
that follows it, so the open slot names both songs: the one the break follows (*ending*) and the
one it leads into (*upcoming*). A break booked for the slot pins that pair: the upcoming song
stays, whatever happens to the queue, because the break names it. A song queued after the booking
plays after the upcoming song. News and weather therefore air about one song later than a system
that splices them in at once.

## Turning breaks on

Breaks need saved-point playback (Transition mode: Use saved segue points, or Clockwheel) and are
off by default. Turn them on for each station that takes them in **Settings, Breaks**: tick
**Accept announcer uploads** and choose **Save break settings**. They work from then on, with
no restart. The same tab sets **Where a DJ break goes** (`dj_placement`), **Shortest intro to talk
up** (`min_intro_seconds`) and **Music under a break** (`duck_db`; leave it blank to use the voice
liner duck depth). It also says, in plain text, when breaks cannot be taken now, such as without
saved-point playback. The result of a save is said once.

Turning breaks off drops announcer uploads still waiting for their song change; a break already
mixed into the next song change still plays. Manual voice tracks and the ad schedule's spots stay
booked. From then on the announcer's `GET context` answers
`"accepting": false` with the reason "Breaks are off for this station: turn them on in
Settings, Breaks", and an upload is refused with 409 and the same words.

The station definition holds these startup values. Settings > Breaks changes them for the
selected station:

```json
{
  "breaks": {
    "enabled": true,
    "dj_placement": "auto"
  }
}
```

Until you save Settings, Breaks for the station, `enabled`, `dj_placement`,
`min_intro_seconds` and `duck_db` in its station definition are in charge; a change there takes
effect at the next start. Once you save Settings, Breaks (`POST /api/breaks/settings`, also
under `/api/stations/<slug>/`; `GET` reads them), the station's `web_state.json` keeps all four,
and they take the place of definition values at every start from then on. When the saved
switch differs from the station definition, the log says so once at start. With external uploads
off and no ad schedule or manual voice tracker requesting a slot, AutoDJ does no advance slot
planning. The upload limits, quotas and waiting
times below are station settings:

- `dj_placement`: where a DJ break goes when its request says `auto`. `"auto"` talks up the
  upcoming song when its intro (Start to Intro) is at least `min_intro_seconds` long (2 seconds),
  and otherwise plays the break between the songs. `"talk_up"` talks up any intro; with no intro
  point the break still plays between the songs. `"standalone"` always plays it between the songs.
- `duck_db`: how far the music drops under a break's voice, -30 to 0 dB. Unset, it is
  `[playback] liners_duck_db`.
- `max_upload_mb` (40), `max_seconds` (300), `max_queued` (6 breaks waiting per station),
  `max_per_slot` (2), `quota_mb` (400 MB of waiting files): the limits on uploads.
- `default_expires_seconds` (1800) and `max_expires_seconds` (7200): how long a break may wait.
- `requests_per_minute` (120): requests one token may make.

## Placement

- **Talk-up**: the break's speech ends just as the upcoming song's vocal starts, with the song's
  intro playing under the voice. The voice starts at the ending song's Segue, or earlier over its
  Outro when the request allows it (`allow_outro`).
- **Standalone**: the ending song fades, the break plays, and the upcoming song starts when the
  speech ends.

News, weather and music news are always standalone. When one song change has several breaks,
the standalone ones play first, and only the last DJ break can talk up the song. Liners the clock
or the queue put in the same change play before the breaks. AutoDJ finds where a break's speech
starts and ends, so trailing silence in the file does not delay the music; it keeps a short tail
under the music. With Smooth or Stereo Tool processing, a break gets the same voice processing
as a voice liner.

## The token

An announcer token can read one station's coming songs and send that station breaks, and nothing
else. It works for one station only. In Settings, Analysis workers, Machine tokens, type a name,
choose Token use: Announcer, choose the Station, and choose Create
token. Copy the token when it appears; it is never shown again. From the command line:

```bash
uv run autodj tokens create announcer --scope announcer --station default
uv run autodj tokens create nightowl-announcer --scope announcer --station nightowl
```

Keep the token out of logs and shell history, for example in an environment variable read from a
file only the announcer's account can read. AutoDJ stores only a digest of it and never logs it.
Revoking the token in Settings or with `autodj tokens revoke <id>` stops it at its next request.

The token travels in the `Authorization: Bearer` header. AutoDJ accepts it over plain HTTP only
from the same machine (`127.0.0.1` or `::1`) and only when no proxy carried the request: a plain
HTTP request with a `Forwarded`, `X-Forwarded-*` or `X-Real-IP` header is refused (403) even from
loopback, because a reverse proxy on the same machine may have brought it in from the network in
the clear. From anywhere else, or through a reverse proxy, it needs HTTPS. A reverse proxy must
terminate TLS itself and send `X-Forwarded-Proto: https`, and AutoDJ must trust that proxy: the
addresses in `[server] trusted_proxies` when that list is set, otherwise a proxy on the same
machine (`127.0.0.1` or `::1`), so a TLS proxy on the station's own host needs no setting. A
request with no proxy headers is treated as before. A request with an `Origin` header, as every
browser sends, is refused: these routes are for programs only.

## Voice tracking from the browser

The **Voice tracks** tab records a DJ break with your microphone and books it for the open song
change, using the same booking desk as announcer breaks (see [Announcer breaks](#announcer-breaks-dj-breaks-news-and-weather)).
It works with server audio or stream output and saved-point transitions, even when external
announcer uploads are off. Browser playback and stopped stations cannot render these tracks.
Opening the tab requests advance slot planning; an unused tracker adds no planning work. No token
is needed: the tab uses your signed-in browser.

The browser lets a page use the microphone only over a secure connection: open AutoDJ over HTTPS,
or at `http://localhost:<port>` on the station's own computer. On a plain `http://` address from
another computer the tab says so instead of recording.

The tab shows the open song change: the ending song, the next song, how long the next song's
intro is, the time left to book it and roughly when it airs. That change is the one after the next
song, because the next one is already mixed. The time left counts down on the page; it is not
read out on its own.

Choose **Microphone** before recording. The choice is remembered in this browser;
**System default** forgets it. **Refresh microphones** updates the list; names may
be hidden until permission is granted. The input status reports the acquired
track's identity when the browser supplies it. An unavailable selected microphone
is not silently replaced. Use **Test microphone** with headphones for a private
monitor on the Preview output device, then **Stop microphone test** before recording.
The monitor never books a take or joins the station output.

1. Choose **Record**. The first time, the browser asks to use the microphone. Speak, then choose
   **Record** again to stop. **Alt+R** starts and stops recording from anywhere in the tab without
   moving focus, which suits a screen reader user who wants to stay on another control.
2. Choose **Listen back** to hear the take, on the Preview output device when one is chosen in
   Settings, Sound (headphones, say). **Record** again replaces it; **Discard recording**
   throws it away.
3. Choose the **Placement**: **Automatic** (follows **Where a DJ break goes** in Settings, Breaks;
   the description under it says what it will do for this change), **Talk up the next song's
   intro** (your voice ends as the vocal starts), or **Standalone, between the songs**. For a
   talk-up, **Start over the ending song's outro** lets your voice begin while the ending song
   plays out.
4. Choose **Book voice track**. The browser turns the take into a mono WAV at 44.1 kHz and
   uploads it; AutoDJ trims the silence before and after your voice as it does for any break.

The recording state is said once when recording starts and stops, then once a minute, and 30
seconds before the length limit or before the song change closes. Recording stops by itself at
10 minutes. Manual voice tracks have their own 600-second limit and 64 MiB WAV upload allowance;
the announcer program still uses `max_seconds` and `max_upload_mb`. The station's shared
`max_queued`, `max_per_slot` and `quota_mb` still apply to both. Recording also stops, keeping the
take and letting the microphone go, when you leave the Voice tracks tab or the page is hidden
(another browser tab, a minimised window); that is said once.

A standalone voice track cannot run across a hard scheduled event or a clockwheel restart. AutoDJ
refuses it at booking when the timing is known, and checks again before rendering in case the
schedule changed; a conflicting queued track is dropped whole rather than cut off.

If the song change moves on before you book, the tab says which change is open now. When the open
change has the same two songs (it was planned again), **Book voice track** books the take there.
When it has other songs, the take is not booked: record again for those songs, or, if what you
said still fits, use **Listen back** and then choose **Book voice track** again. Errors, such as a
blocked or missing microphone or a Cancel that failed, are shown under the Record heading and
linked to the control they are about.

**Booked voice tracks** lists what became of each voice track (waiting, mixed in, on the air,
aired, dropped or expired, with the reason). **Cancel** drops one that is still waiting for its
song change. Voice tracks share the queue, per-song-change and disk quota limits with announcer
breaks: `max_queued`, `max_per_slot` and `quota_mb`. Uploads and cancels count against
`requests_per_minute` for the station.

For scripts and other stations, the routes are `GET`, `POST` and `DELETE` under
`/api/voice-tracks` and `/api/stations/<slug>/voice-tracks`. They take the browser session and the
page's `Origin`, never an announcer token. `POST` takes the announcer upload's fields (`slot_id`,
`ending_id`, `upcoming_id`, `placement`, `allow_outro` and `file`), with no `kind`: a voice track is
always a DJ break.


## Related tasks and contracts

- [Announcer API and uploaded-break lifecycle](../../reference/operations/announcer-api.md)
- [Add matched intros and conditional intro rules](matched-intros.md)
