# AutoDJ operations

For everyday tasks, start with the [documentation home](README.md): music, sound, schedules, live work, streaming, several stations, backups, keyboard access, and troubleshooting. Use the [settings reference](settings-reference.md) for values and defaults. This index routes detailed commands and migration notes to focused guides, reference pages and explanations.

For architectural constraints and implementation/test links, consult the
[ADR index](adr/README.md), especially the current amendments to
[playout](adr/0004-server-mix-bus-and-stream-mode.md) and
[station ownership](adr/0005-a-station-owns-its-playout-chain.md).
The index also identifies Proposed records for storage, trust, background
work, scheduling, and recovery; those await maintainer acceptance.

Commands labeled Bash require a Linux host or WSL2. Native Windows operators should use the
PowerShell equivalents below. Run Linux container commands inside WSL2 with the repository on a
WSL filesystem so user IDs and POSIX modes have their documented meaning.

Browse [operation tasks](guides/operations/README.md), [detailed reference](reference/operations/README.md), or [operational explanations](explanation/operations/README.md). Existing section links below lead to their precise new homes.

## Configuration precedence

See [Configuration precedence and repeat settings — Configuration precedence](reference/operations/configuration.md#configuration-precedence).

## Diagnose before serving

See [Diagnose an installation before serving — Diagnose before serving](guides/operations/diagnose.md#diagnose-before-serving).

## Indexing and DJ analysis

See [Import, analyse and maintain a music library — Indexing and DJ analysis](guides/operations/library.md#indexing-and-dj-analysis).

### Long tracks

See [How long tracks use memory and disk — Long tracks](explanation/operations/long-tracks.md#long-tracks).

### Drop mixing and key shift

See [How drop mixing and key shift work — Drop mixing and key shift](explanation/operations/dj-transitions.md#drop-mixing-and-key-shift).

## Local network access

See [Connect devices on a private network — Local network access](guides/operations/network.md#local-network-access).

### HTTPS on your home network

See [Connect devices on a private network — HTTPS on your home network](guides/operations/network.md#https-on-your-home-network).

### Paired devices

See [Connect devices on a private network — Paired devices](guides/operations/network.md#paired-devices).

## Docker

See [Run AutoDJ in Docker or Windows PowerShell — Docker](guides/operations/containers-and-windows.md#docker).

### Build stamp

See [Run AutoDJ in Docker or Windows PowerShell — Build stamp](guides/operations/containers-and-windows.md#build-stamp).

### Advanced overrides

See [Connect devices on a private network — Advanced overrides](guides/operations/network.md#advanced-overrides).

## HTTPS with your own domain

See [Configure domain HTTPS and remote access — HTTPS with your own domain](guides/operations/domain-https.md#https-with-your-own-domain).

### 1. Get the certificate with certbot

See [Configure domain HTTPS and remote access — 1. Get the certificate with certbot](guides/operations/domain-https.md#1-get-the-certificate-with-certbot).

### 2. Copy the certificate to the AutoDJ machine on every renewal

See [Configure domain HTTPS and remote access — 2. Copy the certificate to the AutoDJ machine on every renewal](guides/operations/domain-https.md#2-copy-the-certificate-to-the-autodj-machine-on-every-renewal).

### 3. Configure AutoDJ

See [Configure domain HTTPS and remote access — 3. Configure AutoDJ](guides/operations/domain-https.md#3-configure-autodj).

### 4. Keep home traffic on the LAN

See [Configure domain HTTPS and remote access — 4. Keep home traffic on the LAN](guides/operations/domain-https.md#4-keep-home-traffic-on-the-lan).

### 5. Reach it from outside with a Cloudflare Tunnel and Cloudflare Access

See [Configure domain HTTPS and remote access — 5. Reach it from outside with a Cloudflare Tunnel and Cloudflare Access](guides/operations/domain-https.md#5-reach-it-from-outside-with-a-cloudflare-tunnel-and-cloudflare-access).

## DMCA mode

See [How DMCA mode checks programming — DMCA mode](explanation/operations/dmca.md#dmca-mode).

### The limits

See [How DMCA mode checks programming — The limits](explanation/operations/dmca.md#the-limits).

### Where the limits apply

See [How DMCA mode checks programming — Where the limits apply](explanation/operations/dmca.md#where-the-limits-apply).

### Expected start times

See [How DMCA mode checks programming — Expected start times](explanation/operations/dmca.md#expected-start-times).

### Public pages

See [How DMCA mode checks programming — Public pages](explanation/operations/dmca.md#public-pages).

## Now-playing feed and webhooks

See [Now-playing feed, status page and webhook contracts — Now-playing feed and webhooks](reference/operations/now-playing.md#now-playing-feed-and-webhooks).

### Status page

See [Now-playing feed, status page and webhook contracts — Status page](reference/operations/now-playing.md#status-page).

### Player page and embeddable player

See [Enable a public player or embedded player — Player page and embeddable player](guides/operations/public-player.md#player-page-and-embeddable-player).

### Settings

See [Now-playing feed, status page and webhook contracts — Settings](reference/operations/now-playing.md#settings).

## Listener requests

See [Take and moderate listener requests — Listener requests](guides/operations/listener-requests.md#listener-requests).

## Radio stream (Sonos, VLC and other players)

See [Listen through Sonos, VLC and other stream players — Radio stream (Sonos, VLC and other players)](guides/operations/stream-listening.md#radio-stream-sonos-vlc-and-other-players).

### Finding the stream address

See [Listen through Sonos, VLC and other stream players — Finding the stream address](guides/operations/stream-listening.md#finding-the-stream-address).

### Listener access controls

See [Listen through Sonos, VLC and other stream players — Listener access controls](guides/operations/stream-listening.md#listener-access-controls).

### Adding the station to Sonos

See [Listen through Sonos, VLC and other stream players — Adding the station to Sonos](guides/operations/stream-listening.md#adding-the-station-to-sonos).

### Adding the station to VLC

See [Listen through Sonos, VLC and other stream players — Adding the station to VLC](guides/operations/stream-listening.md#adding-the-station-to-vlc).

### Controls while streaming

See [Server playback controls and idle behavior — Controls while streaming](reference/operations/server-playback.md#controls-while-streaming).

### Idle sets

See [Server playback controls and idle behavior — Idle sets](reference/operations/server-playback.md#idle-sets).

### More formats: AAC and Opus

See [Enable stream formats and inspect listener statistics — More formats: AAC and Opus](guides/operations/stream-formats-and-statistics.md#more-formats-aac-and-opus).

### Listener statistics

See [Enable stream formats and inspect listener statistics — Listener statistics](guides/operations/stream-formats-and-statistics.md#listener-statistics).

### Relaying to Icecast and Shoutcast

See [Relay a station to Icecast or Shoutcast — Relaying to Icecast and Shoutcast](guides/operations/relays.md#relaying-to-icecast-and-shoutcast).

### Live DJs and talkover

See [Connect a live DJ and use browser talkover — Live DJs and talkover](guides/operations/live-djs.md#live-djs-and-talkover).

### Recording the broadcast

See [Record and split the broadcast — Recording the broadcast](guides/operations/broadcast-recording.md#recording-the-broadcast).

### Troubleshooting

See [Listen through Sonos, VLC and other stream players — Troubleshooting](guides/operations/stream-listening.md#troubleshooting).

## Several stations

See [Create, switch, edit and retire stations — Several stations](guides/operations/stations.md#several-stations).

### Configuration

See [Station definitions, routes and lifecycle — Configuration](reference/operations/stations.md#configuration).

### Editing a stopped station

See [Create, switch, edit and retire stations — Editing a stopped station](guides/operations/stations.md#editing-a-stopped-station).

### Creating a station from the web page

See [Create, switch, edit and retire stations — Creating a station from the web page](guides/operations/stations.md#creating-a-station-from-the-web-page).

### Running

See [Station definitions, routes and lifecycle — Running](reference/operations/stations.md#running).

### Backups and doctor

See [Back up the installation and its stations — Backups and doctor](guides/operations/backup.md#backups-and-doctor).

### Getting Stereo Tool and its licence

See [Install Stereo Tool and configure licences — Getting Stereo Tool and its licence](guides/operations/stereo-tool.md#getting-stereo-tool-and-its-licence).

#### One licence or one per station

See [Install Stereo Tool and configure licences — One licence or one per station](guides/operations/stereo-tool.md#one-licence-or-one-per-station).

#### Several Stereo Tool instances

See [Install Stereo Tool and configure licences — Several Stereo Tool instances](guides/operations/stereo-tool.md#several-stereo-tool-instances).

### The station switcher

See [Create, switch, edit and retire stations — The station switcher](guides/operations/stations.md#the-station-switcher).

### Logs

See [Station definitions, routes and lifecycle — Logs](reference/operations/stations.md#logs).

### Cost of each running station

See [Measure multi-station resource use — Cost of each running station](guides/operations/station-soak.md#cost-of-each-running-station).

### Soak test

See [Measure multi-station resource use — Soak test](guides/operations/station-soak.md#soak-test).

### Adding a station, step by step

See [Create, switch, edit and retire stations — Adding a station, step by step](guides/operations/stations.md#adding-a-station-step-by-step).

## Windows PowerShell setup

See [Run AutoDJ in Docker or Windows PowerShell — Windows PowerShell setup](guides/operations/containers-and-windows.md#windows-powershell-setup).

## Editing tags

See [Edit tags in the catalog, files and beets — Editing tags](guides/operations/tags.md#editing-tags).

## Moved files

See [Move music files and migrate legacy indexes — Moved files](guides/operations/catalog-migration.md#moved-files).

## More than one machine

See [Move music files and migrate legacy indexes — More than one machine](guides/operations/catalog-migration.md#more-than-one-machine).

### One catalog per machine

See [Move music files and migrate legacy indexes — One catalog per machine](guides/operations/catalog-migration.md#one-catalog-per-machine).

### Importing a legacy index on another machine

See [Move music files and migrate legacy indexes — Importing a legacy index on another machine](guides/operations/catalog-migration.md#importing-a-legacy-index-on-another-machine).

## Using a GPU machine

See [Run analysis on a GPU worker — Using a GPU machine](guides/operations/gpu-worker.md#using-a-gpu-machine).

### 1. Give the server HTTPS

See [Run analysis on a GPU worker — 1. Give the server HTTPS](guides/operations/gpu-worker.md#1-give-the-server-https).

### 2. Create a token

See [Run analysis on a GPU worker — 2. Create a token](guides/operations/gpu-worker.md#2-create-a-token).

### 3. Install AutoDJ on the GPU machine

See [Run analysis on a GPU worker — 3. Install AutoDJ on the GPU machine](guides/operations/gpu-worker.md#3-install-autodj-on-the-gpu-machine).

### 4. Run the worker

See [Run analysis on a GPU worker — 4. Run the worker](guides/operations/gpu-worker.md#4-run-the-worker).

### Sharing the GPU with another program

See [Run analysis on a GPU worker — Sharing the GPU with another program](guides/operations/gpu-worker.md#sharing-the-gpu-with-another-program).

### Adding new music

See [Run analysis on a GPU worker — Adding new music](guides/operations/gpu-worker.md#adding-new-music).

## Announcer breaks (DJ breaks, news and weather)

See [Book announcer breaks and browser voice tracks — Announcer breaks (DJ breaks, news and weather)](guides/operations/spoken-breaks.md#announcer-breaks-dj-breaks-news-and-weather).

### Turning breaks on

See [Book announcer breaks and browser voice tracks — Turning breaks on](guides/operations/spoken-breaks.md#turning-breaks-on).

### Placement

See [Book announcer breaks and browser voice tracks — Placement](guides/operations/spoken-breaks.md#placement).

### The token

See [Book announcer breaks and browser voice tracks — The token](guides/operations/spoken-breaks.md#the-token).

### API reference

See [Announcer API and uploaded-break lifecycle — API reference](reference/operations/announcer-api.md#api-reference).

#### GET context

See [Announcer API and uploaded-break lifecycle — GET context](reference/operations/announcer-api.md#get-context).

#### POST breaks

See [Announcer API and uploaded-break lifecycle — POST breaks](reference/operations/announcer-api.md#post-breaks).

#### GET breaks and GET breaks/{break_id}

See [Announcer API and uploaded-break lifecycle — GET breaks and GET breaks/{break_id}](reference/operations/announcer-api.md#get-breaks-and-get-breaksbreak_id).

#### DELETE breaks/{break_id}

See [Announcer API and uploaded-break lifecycle — DELETE breaks/{break_id}](reference/operations/announcer-api.md#delete-breaksbreak_id).

### What happens to the files

See [Announcer API and uploaded-break lifecycle — What happens to the files](reference/operations/announcer-api.md#what-happens-to-the-files).

## Voice tracking from the browser

See [Book announcer breaks and browser voice tracks — Voice tracking from the browser](guides/operations/spoken-breaks.md#voice-tracking-from-the-browser).

## Song structure and landmarks

See [Inspect and navigate song landmarks — Song structure and landmarks](guides/operations/landmarks.md#song-structure-and-landmarks).

### In Track info

See [Inspect and navigate song landmarks — In Track info](guides/operations/landmarks.md#in-track-info).

### Keys

See [Inspect and navigate song landmarks — Keys](guides/operations/landmarks.md#keys).

## Matched intros

See [Add matched intros and conditional intro rules — Matched intros](guides/operations/matched-intros.md#matched-intros).

### Adding one

See [Add matched intros and conditional intro rules — Adding one](guides/operations/matched-intros.md#adding-one).

### How one is chosen and placed

See [Add matched intros and conditional intro rules — How one is chosen and placed](guides/operations/matched-intros.md#how-one-is-chosen-and-placed).

### Intro rules

See [Add matched intros and conditional intro rules — Intro rules](guides/operations/matched-intros.md#intro-rules).

### Where they are kept

See [Matched intro, ad and show APIs and storage — Where they are kept](reference/operations/station-content-api.md#where-they-are-kept).

## Ad breaks

See [Schedule ad breaks and read the ad report — Ad breaks](guides/operations/ad-breaks.md#ad-breaks).

### Spots

See [Schedule ad breaks and read the ad report — Spots](guides/operations/ad-breaks.md#spots).

### Ad breaks and when they run

See [Schedule ad breaks and read the ad report — Ad breaks and when they run](guides/operations/ad-breaks.md#ad-breaks-and-when-they-run).

### Ad breaks in a clockwheel

See [Schedule ad breaks and read the ad report — Ad breaks in a clockwheel](guides/operations/ad-breaks.md#ad-breaks-in-a-clockwheel).

### How a break is filled

See [Schedule ad breaks and read the ad report — How a break is filled](guides/operations/ad-breaks.md#how-a-break-is-filled).

### Late and missed spots

See [Schedule ad breaks and read the ad report — Late and missed spots](guides/operations/ad-breaks.md#late-and-missed-spots).

### The ad report

See [Schedule ad breaks and read the ad report — The ad report](guides/operations/ad-breaks.md#the-ad-report).

### Where they are kept

See [Matched intro, ad and show APIs and storage — Where they are kept](reference/operations/station-content-api.md#where-they-are-kept-1).

## Play log export

See [Export play logs and webcasting reports — Play log export](guides/operations/play-log-export.md#play-log-export).

### Listener counts

See [Play-log fields, exclusions and listener counts — Listener counts](reference/operations/play-log.md#listener-counts).

### Reporting for webcasting licenses

See [Export play logs and webcasting reports — Reporting for webcasting licenses](guides/operations/play-log-export.md#reporting-for-webcasting-licenses).

## Repeat settings

See [Configuration precedence and repeat settings — Repeat settings](reference/operations/configuration.md#repeat-settings).

## Testing the rotation

See [Test rotation and preview an hour — Testing the rotation](guides/operations/rotation-preview.md#testing-the-rotation).

## Programme review and export

Use **Now Playing**, **Programme review** to hear a shortened planned mix of a
transition, section or programme and download the same result as MP3 or WAV.
This is private audio before final broadcast mastering. It neither starts nor
changes station playout and does not record what actually aired. Automatic
broadcast choices can differ later. The [architecture proposal](adr/0014-isolate-programme-review-and-export.md)
defines ownership and evidence limits.

### Generate and listen

1. Select the station. Open **Programme review** and press **Refresh reviews** to
   load its saved shows and retained results.
2. Choose **Source**: **Saved show**, **Current queue**, or **Forecast**. For a
   saved show, choose **Show**; it can be reviewed while the station is stopped.
   Set **Programme minutes** from 1 to 180.
3. Optionally set **Intended start** in this browser's local time. Blank means
   now. Automatic content is resolved against that full programme time in the
   station's time zone, before song middles are removed.
4. For a transition or section, enter **Range start (seconds)** and **Range end
   (seconds)** relative to the captured programme. Leave both blank for the full
   programme. The effective range can expand to preserve complete short clips,
   transitions and ducking.
5. Open **Review timing** to change the opening/ending windows, long-clip
   threshold or skip-tone length. See the [values and defaults](settings-reference.md#programme-review).
6. Press **Generate review**. Generation runs separately from playout; **Cancel
   generation** abandons that job and preserves existing results.
7. When ready, choose the result under **Saved review** and press **Load review**.
   Read its date, effective range and complete/incomplete status. Use the native
   **Private programme review** audio controls to listen. Audio never starts
   automatically. **Settings**, **Playback**, **Preview output device** selects
   the same private device used by other previews; browser/device fallback advice
   remains applicable.
8. Use **Previous item**, **Next item**, or an item button to seek without starting
   playback. Each item gives both its full programme and shortened preview times.
   **Download MP3** and **Download WAV** contain the same frozen edits, skip tones
   and placeholders heard in the player.

Songs retain their first and last 10 seconds by default. Non-song clips lasting
five minutes or less remain complete; longer interviews and other clips shorten
like songs. Actual joins, overlapping voice and ducking can extend these windows.
Overlapping windows merge rather than repeat audio. A quiet generated skip tone
marks each removed middle; it is a review edit, not a broadcast segue.

### Incomplete or out-of-date results

An unresolved item appears in the omission list and is replaced by spoken audio
using the configured default TTS command (`time_checks.tts_command`). This does
not generate the missing programme content. If that command is unset or fails,
reviews needing placeholders fail with an error; complete content still renders.
Configure or repair the existing command and generate again. Where a source
duration is unknown, downstream programme times say **Unknown**; the placeholder
does not establish broadcast duration.

Press **Refresh reviews** after editing a show, audio, cues or relevant settings.
Affected results say **Out of date** and remain playable/exportable with their
original audio. Generate explicitly to create a new result. Editing or hearing a
review does not approve pending speech for broadcast. Prepared editions and their
approval workflow are a separate feature.

### Storage and availability

| Capability | Availability |
| --- | --- |
| Saved-show private review | Available without running station playout |
| Queue/forecast review | Captures the selected station's current planned material |
| Listening and MP3/WAV export | Operator browser; same frozen pre-mastering result |
| Public/listener or limited DJ/announcer/worker access | No review access |
| Actual-air recording or final Stereo Tool processing | Outside programme review |

One render runs across the server at a time, with a cooperative 120-second work
limit. Each station retains up to 20 results and 512 MiB; reaching either limit
refuses generation instead of deleting older reviews. These are resource limits,
not a promise that every admitted job completes. Reduce the requested span or
delete unwanted snapshots and retry after a capacity/storage error.

Working audio has a separate 2 GiB scratch allocation budget, including decoded
sources and copies, and leaves at least 64 MiB free. A long source can exceed
that budget even if its shortened export would be small. Generation fails
actionably rather than spilling outside its owned workspace. These service
limits are separate from the review timing controls.

Results live under the station's state folder in `reviews/`. Completed results
survive restart; interrupted jobs fail and their partial files are cleaned up.
Recovery never starts the station. Results have no automatic expiry. Select an
unwanted **Saved review** and press **Delete review** to remove it explicitly.
AutoDJ backups exclude these derived files: download and separately retain MP3/WAV
exports that you need to keep. Source audio remains under its existing ownership.

## Hour preview and backtiming

See [Test rotation and preview an hour — Hour preview and backtiming](guides/operations/rotation-preview.md#hour-preview-and-backtiming).

## Backup

See [Back up the installation and its stations — Backup](guides/operations/backup.md#backup).

## Restore and validate

See [Restore an archive and recover interrupted restores — Restore and validate](guides/operations/restore.md#restore-and-validate).

## Upgrade checklist

See [Upgrade or return to an older release — Upgrade checklist](guides/operations/upgrade.md#upgrade-checklist).

### Going back to an older release

See [Upgrade or return to an older release — Going back to an older release](guides/operations/upgrade.md#going-back-to-an-older-release).

## Cart wall

See [Operate carts and set liner timing and play dates — Cart wall](guides/operations/carts-and-liners.md#cart-wall).

## Liner cue points

See [Operate carts and set liner timing and play dates — Liner cue points](guides/operations/carts-and-liners.md#liner-cue-points).

## Liner and cart play dates

See [Operate carts and set liner timing and play dates — Liner and cart play dates](guides/operations/carts-and-liners.md#liner-and-cart-play-dates).

## Scheduler

See [Schedule timed station events — Scheduler](guides/operations/scheduler.md#scheduler).

## Clockwheels

See [Create, edit and import clockwheels — Clockwheels](guides/operations/clockwheels.md#clockwheels).

## Rotation categories

See [Build rotation categories and repair beets paths — Rotation categories](guides/operations/categories.md#rotation-categories).

### Beets paths and `music_dir`

See [Build rotation categories and repair beets paths — Beets paths and `music_dir`](guides/operations/categories.md#beets-paths-and-music_dir).

### Example: rotations A to D from playlists

See [Build rotation categories and repair beets paths — Example: rotations A to D from playlists](guides/operations/categories.md#example-rotations-a-to-d-from-playlists).

### Example: Remembering the 2000s

See [Build rotation categories and repair beets paths — Example: Remembering the 2000s](guides/operations/categories.md#example-remembering-the-2000s).

## Shows

See [Prepare and air a show — Shows](guides/operations/shows.md#shows).

To hear its joins privately before airing, use [Programme review](#programme-review-and-export)
with **Saved show** as the source. This creates a separate review snapshot and
does not change the saved list or schedule it.

### Planning a show

See [Prepare and air a show — Planning a show](guides/operations/shows.md#planning-a-show).

### Airing a show

See [Prepare and air a show — Airing a show](guides/operations/shows.md#airing-a-show).

### Where they are kept

See [Matched intro, ad and show APIs and storage — Where they are kept](reference/operations/station-content-api.md#where-they-are-kept-2).

## Spoken time checks

See [Configure recorded time checks and voice packs — Spoken time checks](guides/operations/time-checks.md#spoken-time-checks).

### Voice packs

See [Configure recorded time checks and voice packs — Voice packs](guides/operations/time-checks.md#voice-packs).

### Text to speech

See [Configure a station text-to-speech command — Text to speech](guides/operations/tts-command.md#text-to-speech).

## Speech studio

See [Compare speech and prepare generated time checks — Speech studio](guides/operations/speech-studio.md#speech-studio).

### Configure speech connections

See [Compare speech and prepare generated time checks — Configure speech connections](guides/operations/speech-studio.md#configure-speech-connections).

### Compare and save speech

See [Compare speech and prepare generated time checks — Compare and save speech](guides/operations/speech-studio.md#compare-and-save-speech).

### Prepare time checks

See [Compare speech and prepare generated time checks — Prepare time checks](guides/operations/speech-studio.md#prepare-time-checks).

### Evidence for workflow improvements

See [Compare speech and prepare generated time checks — Evidence for workflow improvements](guides/operations/speech-studio.md#evidence-for-workflow-improvements).
