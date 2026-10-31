# Book announcer breaks and browser voice tracks

Enable external announcer uploads or record in a paired browser, and book speech for the open song change.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Announcer breaks (DJ breaks, news and weather)

An announcer program on the station's own machine can send a station spoken breaks: a DJ talking
about the songs, news, weather or music news. It asks AutoDJ which song change is still open,
writes a break for that pair of songs, and uploads it. AutoDJ mixes the break into that song
change, reports when it airs, and deletes the file afterwards.

In streaming mode, external announcer uploads target the currently airing song change and its
selected successor, like browser voice tracks. AutoDJ checks the handoff again after decoding and
preparing the upload, and installs it only if the same change is still open and within its outro
cutoff and applicable scheduled boundary. Scheduled ads/news keep their independent due/expiry
tick and retry if a handoff changes during booking. Server audio and streaming use the same
live handoff. Queue edits that affect associated speech offer Keep, Discard, or Cancel.

## Turning breaks on

Breaks need saved-point playback (Transition mode: Use saved segue points, or Clockwheel) and are
off by default. Turn them on for each station that takes them in **Settings, Profile, Announcements, Breaks**: tick
**Accept announcer uploads** and save with the Announcements tab's shared Save changes bar. They work from then on, with
no restart. The same tab sets **Where a DJ break goes** (`dj_placement`), **Shortest intro to talk
up** (`min_intro_seconds`) and **Music under a break** (`duck_db`; leave it blank to use the voice
liner duck depth). It also says, in plain text, when breaks cannot be taken now, such as without
saved-point playback. The result of a save is said once.

Turning breaks off drops announcer uploads still waiting for their song change; a break already
mixed into the next song change still plays. Manual voice tracks and the ad schedule's spots stay
booked. From then on the announcer's `GET context` answers
`"accepting": false` with the reason "Breaks are off for this station: turn them on in
Settings, Profile, Announcements, Breaks", and an upload is refused with 409 and the same words.

The station definition holds these startup values. Settings > Profile > Announcements > Breaks changes them for the
selected station:

```json
{
  "breaks": {
    "enabled": true,
    "dj_placement": "auto"
  }
}
```

Until you save the **Announcements** tab's shared Save changes bar for the station, `enabled`, `dj_placement`,
`min_intro_seconds` and `duck_db` in its station definition are in charge; a change there takes
effect at the next start. Once you press **Save changes** in the Announcements tab's shared bar (`PATCH /api/settings`),
the station's `web_state.json` keeps all four,
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
else. It works for one station only. In Settings, Global, Analysis workers, Machine tokens, type a name,
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

The **Voice tracks** tab records a DJ break with your microphone, using the same booking desk as
announcer breaks (see [Announcer breaks](#announcer-breaks-dj-breaks-news-and-weather)).
The current handoff targets the currently airing song and its selected successor. The booking window stays open
until the current song's Outro, or closes earlier if existing speech or a tail begins before it.
AutoDJ calculates the cutoff after decoding and checks it again atomically when it installs the
take. The same live handoff lifecycle serves server audio and streaming.
It works with server audio or stream output and saved-point transitions, even when external
announcer uploads are off. Browser playback and stopped stations cannot render these tracks.
The playout controller maintains the current handoff. No token is needed: the tab uses your signed-in browser.

The browser lets a page use the microphone only over a secure connection: open AutoDJ over HTTPS,
or at `http://localhost:<port>` on the station's own computer. On a plain `http://` address from
another computer the tab says so instead of recording.

The tab shows the open song change: the ending song, its selected successor, how long
the next song's intro is, the time left to book it and roughly when it airs. The time left counts
down on the page; it is not read out on its own.

### Choose and confirm a future song change

Under **Upcoming song changes**, Tab to the native **Song change** select. Use
arrow keys to choose a future pair. Each option names both songs and its position;
repeated songs have separate occurrences. Selection alone does not queue or record.
**Selection: Predicted** describes a forecast. **Estimated airtime** is a separate
estimate and never proves that speech aired.

For a predicted pair, review the number of songs shown, Tab to **Confirm songs**,
and press Enter or Space. Confirmation queues the forecast through that pair as
ordinary music choices. Overlapping confirmations reuse existing occurrences.
**Selection: Confirmed** permits recording; the time remains an estimate. The
current handoff remains directly recordable without this confirmation step.

Targets cover up to 15 minutes of the available preview. Unknown durations,
programme or clock boundaries, unqueued Show tails and prepared-edition items
limit eligibility. Unavailable rows give a reason. Use **Prepared editions** for
advance production outside this near-air workflow. Pause preserves the chosen
songs but makes their airtime unknown; resume and refresh before booking.

Refresh preserves the selected occurrence and keyboard focus. If the occurrence
disappears, its unavailable placeholder stays selected. Choose another explicitly.
A schedule change can flag an existing target as at risk; upload and promotion
check the latest revision and complete-chain timing again.

Recording stays bound to its starting occurrence even while you inspect another.
A failed upload keeps the local take and cached WAV. For the same occurrence,
review the refreshed information and press **Book voice track** again. For another
occurrence, including another play of the same song, select it, listen back, then
press **Book voice track**. Discarding or abandoning a browser take leaves the
confirmed music queued and creates no speech booking.

Queue and manual Show changes remain available. If a change splits a booked pair,
use the shared **Keep voice track**, **Discard voice track**, or **Cancel change**
dialog. Keep prefers the original ending song and its new successor; if the ending
song leaves, it uses the original incoming song and its new predecessor. Keep is
unavailable when there is no eligible pair, capacity is exhausted, or merging
frozen chains would lose their content or order. Cancel makes no queue change.
Moving an intact pair needs no speech choice.

A future take stays queued until its exact handoff becomes current. Cancellation
before that claim removes only speech. After installation, the existing unheard
boundary applies. Hard scheduled actions and live takeover have priority: unheard
speech can be dropped with a reason; interrupted speech is marked cut short.
Soft actions and ordinary Show feeding defer a conflict instead of silently
moving speech. A hard event retains priority after waiting for the current song.

These recordings belong to the current station run. Stop, a new set, a jump or
restart invalidates old targets. Live recordings are not restored after restart;
previous airtime is unknown. Saved editions, source files and approvals retain
their separate recovery rules.

### Record and book

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
   Settings, Global, Browser preferences (headphones, say). **Record** again replaces it; **Discard recording**
   throws it away.
3. Leave **Placement** at **Fit over outro and intro** to use the ending song's usable
   Outro and the next song's instrumental Intro. Speech uses its detected Start and Segue;
   the original tail remains. When speech exceeds that musical window, the incoming song
   waits so its vocal cannot cover the speech. **Between songs** plays a clean break instead.
   The tab shows usable outro, intro, recorded and detected speech lengths, and the estimated
   voice start, next-song entry and speech without song music. After WAV preparation, the
   estimate includes the planned liner and queued-break cues and uses the playout timing rules;
   changing placement refreshes it. The booking cutoff is calculated after decoding and checked
   again when the take is installed. In stream mode it is the current song's Outro, or earlier if existing
   speech or a tail begins before it.
4. Choose **Book voice track**. The browser turns the take into a mono WAV at 44.1 kHz and
   uploads it; AutoDJ trims the silence before and after your voice as it does for any break.

When a planned transition list is available, **Planned transition audio** shows which spoken
items are already committed and which are predictions. Use each item's native **Voice track
position** selector to put your recording **Before** or **After** that item, or choose **Replace**.
Replace skips predicted automatic liners and matched intros for this transition; replacing an
explicit queued or clockwheel liner defers it, leaving it queued for later. The defaults replace
predicted items and place your voice after explicit queued and clockwheel liners. Scheduled breaks
such as ads and news remain in their protected positions. The ordered summary updates as you
change selectors.

AutoDJ freezes predicted content for the open song pair without advancing liner rotation or intro
cooldowns. It reserves the frozen candidates only when that transition is rendered. A retained
intro advances its random chooser only when heard; replacing or discarding it does not. If Before,
After and Replace choices contradict one another, booking is refused without changing the plan.
A stale plan is also refused; refresh the list and submit your choice against the latest revision.

The recording state is said once when recording starts and stops, then once a minute, and 30
seconds before the length limit or before the song change closes. Recording stops by itself at
10 minutes or sooner if the station's upload and storage limits cannot hold the take. Manual voice
tracks have their own 600-second limit and 64 MiB WAV upload allowance; the announcer program still
uses `max_seconds` and `max_upload_mb`. The station's shared `max_queued`, `max_per_slot` and
`quota_mb` still apply to both. The browser prepares the WAV in chunks and caches it for private
listen-back and booking.
Recording also stops, keeping the take and letting the microphone go, when you leave the Voice
tracks tab or the page is hidden (another browser tab, a minimised window); that is said once.

A standalone voice track cannot run across a hard scheduled event or a clockwheel restart. AutoDJ
refuses it at booking when the timing is known, and checks again before rendering in case the
schedule changed; a conflicting queued track is dropped whole rather than cut off.

If the song change moves on before you book, keep the recording and explicitly
choose an available occurrence. Errors, such as a blocked microphone or refused
cancellation, appear under Record and are linked to the relevant control.
If a booking disappears during a refused cancellation, the error remains visible
and focus moves to the Voice tracks heading, where the error is associated.

**Booked voice tracks** lists what became of each voice track (waiting, mixed in, on the air,
aired, dropped or expired, with the reason). Choose **Cancel** from a booking's **Options**
menu, then confirm **Cancel booking** in the **Cancel this booking?** dialog to drop a waiting recording. In server audio and streaming,
it can also remove a recording installed in the current or ready handoff before that recording
starts. The server checks this again when applying the cancellation; other speech and approved
assets are preserved, and audio already heard is unchanged. Voice tracks share the queue,
per-song-change and disk quota limits with announcer
breaks: `max_queued`, `max_per_slot` and `quota_mb`. Uploads and cancels count against
`requests_per_minute` for the station.

For scripts and other stations, the routes are `GET`, `POST` and `DELETE` under
`/api/voice-tracks` and `/api/stations/<slug>/voice-tracks`. They take the browser session and the
page's `Origin`, never an announcer token. `POST` takes the announcer upload's fields (`slot_id`,
`ending_id`, `upcoming_id`, `placement`, `allow_outro` and `file`), with no `kind`: a voice track is
always a DJ break. Future uploads also carry `target_id`, `run_id` and the selected
`transition_revision`. `GET /voice-tracks?target_id=...` inspects a target;
`POST /voice-tracks/targets/confirm` confirms its server-owned snapshot using
`target_id` and `revision`, with optional shared `voice_action`/`voice_revision`
when resolving a conflict. These routes also have station-scoped forms.


## Related tasks and contracts

- [Announcer API and uploaded-break lifecycle](../../reference/operations/announcer-api.md)
- [Add matched intros and conditional intro rules](matched-intros.md)
