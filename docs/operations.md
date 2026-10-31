# AutoDJ operations

For everyday tasks, start with the [documentation home](README.md): music, sound, schedules, live work, streaming, several stations, backups, keyboard access, and troubleshooting. Use the [settings reference](settings-reference.md) for values and defaults. This index routes detailed commands and migration notes to focused guides, reference pages and explanations.

For architectural constraints and implementation/test links, consult the
[ADR index](adr/README.md), especially the current amendments to
[playout](adr/0004-server-mix-bus-and-stream-mode.md) and
[station ownership](adr/0005-a-station-owns-its-playout-chain.md).
The index also identifies Proposed records for storage, trust, background
work, scheduling, and recovery; those await maintainer acceptance.

[Local DJ prototype](adr/0019-local-browser-programme-ownership.md)
records the approved design and phased evidence for issue #541. The TOML-only
`server.local_programme_enabled` setting defaults off and requires restart.
The bounded workflow is described in
[experimental local browser control](reference/operations/server-playback.md#experimental-local-browser-control).
Disabled installations retain their existing procedures.

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

### Choose an image

See [Run AutoDJ in Docker or Windows PowerShell — Choose an image](guides/operations/containers-and-windows.md#choose-an-image)
and [Build the image yourself](guides/operations/containers-and-windows.md#build-the-image-yourself).

### Use an NVIDIA GPU in Docker

See [Run AutoDJ in Docker or Windows PowerShell — Use an NVIDIA GPU](guides/operations/containers-and-windows.md#use-an-nvidia-gpu).

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

Live mixing uses progressive decoder buffers capped at two seconds each. While
A plays into B, both server outputs keep B's existing decoder and a playable
continuation. B continues at the carried source position while the worker selects
C and prepares B→C; whole songs are not prewarmed. The unheard transition is
installed while B plays. If preparation misses the transition's admission point,
B finishes alone and the selected next song starts at its cue; the missed
transition's unheard speech is discarded rather than joined partway through.
An unavailable decoder or exhausted continuation
can still cause silence. There is no zero-gap or hardware timing guarantee;
listening and soak evidence are still required on deployment hardware.
Queue edits affect the current/imminent transition and ask the operator to keep
affected speech with its changed music pair, discard it from that transition,
or cancel the change. A pending continuation cursor is not a speech target.
Future approved speech remains attached to its occurrence until selected as the
current upcoming change. Started voice or liner layers keep their original
timing while unheard incoming music changes at its original boundary, so speech
does not repeat. Local `RenderAhead` refresh and whole-show preview/export keep
their separate behavior. See [ADR 4](adr/0004-server-mix-bus-and-stream-mode.md#amendment-2026-10-27-play-the-continuation-during-handoff-preparation-591).

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

### Pair the worker

See [Run analysis on a GPU worker — Pair the worker](guides/operations/gpu-worker.md#pair-the-worker).

### Manage a worker from the server

See [Run analysis on a GPU worker — Manage a worker from the server](guides/operations/gpu-worker.md#manage-a-worker-from-the-server).

### When the server refuses a worker

See [Run analysis on a GPU worker — When the server refuses a worker](guides/operations/gpu-worker.md#when-the-server-refuses-a-worker).

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

See [Read History and export play logs and webcasting reports — Play log export](guides/operations/play-log-export.md#play-log-export).

### Listener counts

See [Play-log fields, exclusions and listener counts — Listener counts](reference/operations/play-log.md#listener-counts).

### Reporting for webcasting licenses

See [Read History and export play logs and webcasting reports — Reporting for webcasting licenses](guides/operations/play-log-export.md#reporting-for-webcasting-licenses).

## Repeat settings

See [Configuration precedence and repeat settings — Repeat settings](reference/operations/configuration.md#repeat-settings).

## Testing the rotation

See [Test rotation and preview an hour — Testing the rotation](guides/operations/rotation-preview.md#testing-the-rotation).

## Programme review and export

See [Review and export a programme — Programme review and export](guides/operations/programme-review.md#programme-review-and-export).

### Generate and listen

See [Review and export a programme — Generate and listen](guides/operations/programme-review.md#generate-and-listen).

### Incomplete or out-of-date results

See [Review and export a programme — Incomplete or out-of-date results](guides/operations/programme-review.md#incomplete-or-out-of-date-results).

### Storage and availability

See [Review and export a programme — Storage and availability](guides/operations/programme-review.md#storage-and-availability).

## Prepared show editions

See [Prepared show editions](guides/operations/prepared-editions.md).

### Record, review and approve

See [Record, review and approve](guides/operations/prepared-editions.md#record-review-and-approve).

### Schedule, re-air and resume

See [Schedule, re-air and resume](guides/operations/prepared-editions.md#schedule-re-air-and-resume).

### Retention and backup

See [Retention and backup](guides/operations/prepared-editions.md#retention-and-backup).

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
