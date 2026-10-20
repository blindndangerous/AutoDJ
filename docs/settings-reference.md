# Settings reference

These defaults follow the configuration dataclasses in `src/autodj/config.py`. Shared settings
live in `config.toml`, with private shared values in `config.local.toml`. Each station's settings
live under the `station` object in `index/stations/<slug>/station.json`. Settings saved in the
page take precedence over that station's file values. Use **Settings** for controls the page
provides. Restart AutoDJ after editing a station file unless a section says otherwise.

The first station is `default`. Every station, including it, has a
`stations/<slug>/station.json` definition. AutoDJ migrates old station tables from `config.toml`
and `config.local.toml` once at startup, then ignores them. The library catalog is shared at
`libraries/main/`. Station settings and history live under `stations/<slug>/`, shared assets
under `shared/`, and logs under `logs/`. Paths are relative to `index.index_dir` unless stated
otherwise. Each station's private `relays.json` holds its relays and passwords. Its private
`secrets.json` holds listener credentials and webhooks.

Each entry gives possible values, the default, and what the setting does. A page label appears in **bold**. Blank means unset unless its entry says otherwise.


## Programme review

**Now Playing**, **Programme review** settings apply to the next explicit
generation. They are review request values, not new `config.toml` or station
playback settings. Existing snapshots retain the settings used to make them.

| Control | Default | Supported values and effect |
| --- | --- | --- |
| Source | Saved show | Saved show, Current queue or Forecast; Show chooses the saved list |
| Intended start | Blank | Optional browser-local date/time; blank means now; automatic eligibility uses station time |
| Programme minutes | 60 | Whole number 1–60; forecast span before shortening; saved shows and queues use their captured content |
| Range start/end (seconds) | Blank | Both blank for the programme, or nonnegative start and greater end; measured from captured programme start |
| Opening (seconds) | 10 | Positive, at most 3600; minimum retained opening per selected item |
| Ending (seconds) | 10 | Positive, at most 3600; minimum retained ending per selected item |
| Long clip (seconds) | 300 | Positive, at most 86400; non-song clips at or below this threshold remain complete |
| Skip tone (seconds) | 0.25 | Positive, at most 10; duration of the generated edit marker |

Forecast requests offer at most 60 minutes. Saved shows and queues admit at most
60 minutes of known source audio, based conservatively on indexed lengths.
Lists above that bound fail before decoding. Their captured span is checked
against the same limit before mixing. Choose a shorter show or queue;
selecting a range does not reduce the
full programme that must be captured and mixed. An unshortened 180-minute mix
alone exceeds the 2 GiB scratch budget. Long individual sources or mostly complete
audio can still exceed resource limits below 60 minutes.
Freed decode buffers release their reservations; the 120-second cooperative
deadline includes capture, rendering and encoding, without a completion guarantee.
Saved review dates use this browser's locale and time zone, including seconds.

The browser timing controls accept values from 0.001 seconds. The API accepts
finite values greater than zero within the maxima above and rounds retained
windows and tones to at least one audio frame. Songs shorten regardless of
length; required transitions, overlays and explicit keep-complete targets can
extend windows without a retained-duration cap. These validation maxima do not
authorize truncating required content.

Placeholder speech reuses `time_checks.tts_command`. There is no additional
provider configuration and no implicit generation of missing programme content.
See [Programme review and export](operations.md#programme-review-and-export) for
listening, downloads, incomplete results and storage limits.

## Shared settings in config.toml

These sections apply to the whole server. `config.local.toml` can hold private shared values.

### Library

TOML section: `[library]`.

- `music_dir`. Values: folder path or blank. Default: blank. Names the music folder AutoDJ sees when matching paths stored by beets; in Docker it is usually `/music`.
- `beets_db`. Values: SQLite file path or blank. Default: blank. Reads metadata from an optional beets library.
- `supported_formats`. Values: list of file extensions. Default: `['mp3', 'flac', 'm4a']`. Limits the music formats imported from registered folders.
- `playlists_folder`. Values: folder path or blank. Default: blank. Holds M3U and M3U8 files used by categories; blank uses index/shared/playlists.

### Index

TOML section: `[index]`.

- `index_dir`. Values: folder path. Default: `index`. Stores library, shared files, stations, and logs.
- `model_dir`. Values: folder path. Default: `models`. Caches downloaded analysis model files.
- `throttle_ms`. Values: number at least 0. Default: 0.0. Pauses this many milliseconds between tracks in long analysis jobs.

### Model

TOML section: `[model]`.

- `name`. Values: supported model identifier. Default: `OpenMuQ/MuQ-large-msd-iter`. Names the supported MuQ model used to describe how songs sound. Changing the name alone cannot make AutoDJ run an unrelated model.
- `revision`. Values: model revision string. Default: `main`. Chooses the checkpoint revision to download.
- `manual_path`. Values: checkpoint file path or blank. Default: blank. Loads a locally supplied model checkpoint.

### Server

TOML section: `[server]`.

- `host`. Values: IP address or host name. Default: `127.0.0.1`. Sets the web server's listen address; LAN mode can override it.
- `port`. Values: whole number from 1 to 65535. Default: 8080. Sets the web server port.
- `access_token`. Values: secret of at least 32 UTF-8 bytes or blank. Default: blank. Sets the pairing and session secret; blank creates a local secret file.
- `auth_mode`. Values: `native` or `trusted_proxy`. Default: `native`. Selects native pairing or browser SSO through an authenticating proxy.
- `trusted_proxy_secret`. Values: secret of at least 32 printable ASCII bytes without spaces or blank. Default: blank. Required in `trusted_proxy` mode, directly or through a secret file. `AUTODJ_TRUSTED_PROXY_SECRET` overrides TOML. Mutually exclusive with `trusted_proxy_secret_file`, even when one source comes from the environment. Keep it in private service configuration. The proxy sends it as `X-AutoDJ-Proxy-Auth`; identity headers alone grant no access.
- `trusted_proxy_secret_file`. Values: owner-only UTF-8 secret file path or blank. Default: blank. Mutually exclusive with a direct secret. `AUTODJ_TRUSTED_PROXY_SECRET_FILE` overrides the configured path. POSIX group/other permission bits cause an error; use mode 0600 or 0400. Windows access lists must grant access only to the account running AutoDJ. Keep the file outside station backups. Missing, unreadable, non-private or invalid secrets prevent trusted-proxy startup.
- `sso_logout_url`. Values: same-origin path or HTTPS URL without user information, or blank. Default: blank. Enables **Sign out through SSO**. The provider controls logout scope. Other tabs may stay connected until their sockets expire, at most 900 seconds or the shorter `session_ttl_seconds`.
- `insecure_lan`. Values: true or false. Default: false. When true, skips browser pairing so anyone on the network can control AutoDJ. Keep false for normal use.
- `lan`. Values: true or false. Default: false. Listens on the local network with browser pairing.
- `allowed_hosts`. Values: list of host names or blank. Default: blank. Limits names accepted in the browser address bar; LAN mode fills the local names.
- `allowed_origins`. Values: list of origins or blank. Default: blank. Limits browser origins allowed to control the server.
- `session_ttl_seconds`. Values: 60 to 31536000 seconds. Default: 7776000. Sets how long a paired browser session lasts. SSO control and microphone sockets use the shorter of this value and 900 seconds before reconnecting through the proxy.
- `trusted_proxies`. Values: list of IP addresses or networks. Default: empty list. Lists proxy IP addresses or networks that AutoDJ trusts when they forward a listener's real IP address; do not list other clients.
- `ssl_certfile`. Values: PEM certificate path or blank. Default: blank. Enables HTTPS with ssl_keyfile.
- `ssl_keyfile`. Values: PEM private key path or blank. Default: blank. Enables HTTPS with ssl_certfile.
- `live_plain_http_lan`. Values: true or false. Default: false. Allows live DJ passwords over plain HTTP on the LAN.

### Hugging Face

TOML section: `[huggingface]`.

- `token`. Values: access token or blank. Default: blank. Authenticates a model download when one is needed.

## Per-station settings

Each station has its own `index/stations/<id>/station.json`. Relay passwords live in its `relays.json`; listener credentials, webhooks, and a station-only Stereo Tool key live in `secrets.json`. Page controls may save into station state or this browser.

### Playback

Station section: `playback` in `station.json`.

- `crossfade_seconds`. Values: 0 to 20 seconds. Default: 3.0. Sets overlap for fixed transitions; cue based modes can choose their own fade length. **Crossfade seconds**.
- `fade_in_seconds`. Values: 0 to 20 seconds. Default: 3.0. Fades in when playback starts. **Fade-in seconds**.
- `music_speed_percent`. Values: -10 to 10 percent. Default: 0.0. Changes music playback speed. **Music speed (%)**.
- `repeat_rules`. Values: repeat rule object. Default: the repeat defaults below. Sets per artist, album, track, title, related artist, and related track limits; details follow below.
- `discovery_every`. Values: positive whole number or blank. Default: blank. Sets the interval for a distant song when discovery is enabled. **Every N tracks**.
- `crossfade_eq_duck`. Values: true or false. Default: false. Reduces outgoing bass during a crossfade. **EQ-ducked crossfade**.
- `crossfade_bass_cutoff_hz`. Values: number of hertz; the page accepts 20 to 1000. Default: 180.0. Sets the bass cutoff for EQ ducking. **EQ duck cutoff (hertz)**.
- `transition_mode`. Values: `full_intro_outro` (Full intro and outro), `outro_fade` (Outro fade), `fixed_skip_silence` (Fixed (skip silence)), `fixed` (Fixed), or `saved_points` (Use saved segue points). Default: `full_intro_outro`. Chooses how each handoff is timed. **Transition mode**.
- `pick_mode`. Values: `similarity` (Similarity (default)), `random` (Random walk (uniform random)), `least_recent` (Least recently played), or `clock` (Clockwheel). Default: `similarity`. Chooses the automatic song picker. **Pick mode**.
- `loudness_processing`. Values: blank (older ReplayGain switch), `off` (Off), `replaygain` (ReplayGain), `smooth` (Smooth), or `stereotool` (Stereo Tool). Default: blank. Selects output leveling; blank follows the older replaygain.enabled switch. **Output processing**.
- `stereotool_input_trim_db`. Values: -12 to 6 decibels. Default: 0.0. Adjusts the level sent into Stereo Tool. **Input trim (decibels)**.
- `stereotool_agc_drive_db`. Values: 0 to 12 decibels. Default: 7.0. Adjusts Stereo Tool automatic gain drive. **AGC drive boost (dB)**.
- `stereotool_preset`. Values: included or an uploaded preset name. Default: `included`. Selects the Stereo Tool preset. **Preset**.
- `post_queue_seed`. Values: `last_queued` (Last queued track) or `pre_queue` (Track playing before queue). Default: `last_queued`. Chooses where similarity picking resumes after the queue empties. **After queue empties, seed from**.
- `pick_top_k`. Values: whole number at least 1; the page accepts 1 to 100. Default: 1. Chooses among this many closest sounding songs. **Choose among the closest songs**.
- `pick_temperature`. Values: number from 0 to 10. Default: 0.3. Controls the randomness among those close songs; has no effect when pick_top_k is 1. **Pick randomness**.
- `hour_fit`. Values: true or false. Default: false. Prefers songs ending near the next fixed time. **Fit songs to the hour**.
- `hour_fit_window_minutes`. Values: whole number from 1 to 30. Default: 10. Starts hour fitting this many minutes before the fixed time. **Minutes before to start fitting**.
- `key_notation`. Values: `camelot` (Camelot (8A or 8B)) or `musical` (Musical (C, A minor, F sharp minor)). Default: `camelot`. Chooses how keys are shown. **Key notation**.
- `key_prefer_flats`. Values: true or false. Default: false. Uses flat names when musical key notation is selected. **Use flats**.
- `show_lyrics`. Values: true or false. Default: true. Shows the lyrics card when lyrics exist. **Show lyrics**.
- `prefetch_next_track`. Values: true or false. Default: true. Loads the next song early for browser playback. **Load the next song early**.
- `silence_trigger_crossfade`. Values: true or false. Default: true. Starts a browser crossfade when the ending song falls silent. **Crossfade when a song goes quiet**.
- `audio_device`. Values: device number, name fragment, or blank. Default: blank. Selects server speaker output; blank uses the system default.
- `import_external_cues`. Values: true or false. Default: true. Imports cue points stored by supported DJ tools when songs are analysed.
- `beat_sync_fx`. Values: true or false. Default: true. Times rhythmic effects to known beats. **Beat-sync transition FX**.
- `key_sync_fx`. Values: true or false. Default: true. Tunes pitched effects to a known song key. **Key-sync pitched FX**.
- `beatmatch_on_skip`. Values: true or false. Default: false. Matches tempo during a browser skip. **Beatmatch on skip**.
- `skip_style`. Values: `fade` (Quick fade), `echo_out` (Echo out), `backspin` (Backspin), or `loop_roll` (Loop roll). Default: `fade`. Chooses the ending of a skipped song in server mixed playback. **Skip style**.
- `liners_enabled`. Values: true or false. Default: false. Enables scheduled voice liners. **Enable voice liners**.
- `liners_every_n_songs`. Values: nonnegative whole number or blank. Default: blank. Plays a liner after this many songs when set. **Every N tracks**.
- `liners_every_minutes`. Values: nonnegative number or blank. Default: blank. Plays liners on a minute interval when set. **Every N minutes**.
- `liners_random_min_minutes`. Values: nonnegative number or blank. Default: blank. Sets the lower bound of a random liner interval. **Random window minimum (minutes)**.
- `liners_random_max_minutes`. Values: nonnegative number or blank. Default: blank. Sets the upper bound of a random liner interval. **Random window maximum (minutes)**.
- `liners_pick_mode`. Values: `random` (Random) or `sequential` (Sequential). Default: `random` (Random). Selects which eligible liner plays next. **Rotation mode**.
- `liners_duck_db`. Values: -30 to 0 decibels. Default: -3.0. Lowers music under voice liners. **Music duck (dB)**.
- `matched_intro_only_without_liner`. Values: true or false. Default: false. Plays matched intros only when no liner is due. **Only when no liner is scheduled**.
- `liners_talk_up`. Values: true or false. Default: false. Times a liner to end before the next song's vocal when possible. **Talk up to the vocal**.

Songs of any length import and play. In **Library tools**, **Advanced analysis length**, set **Longest song, in minutes** to limit the expensive analysis; the default is 15. Set 0 to analyse every length. Longer songs still receive basic analysis.

### DJ mixing

Station section: `djmix` in `station.json`.

- `beatmatch`. Values: true or false. Default: false. Matches tempo across song transitions. **Beatmatch**.
- `beatmatch_max_stretch`. Values: 0 to 0.5 as a fraction. Default: 0.08. Limits tempo stretch; 0.08 is 8 percent. **Beatmatch stretch limit (percent)**.
- `beatmatch_glide_bars`. Values: whole number from 0 to 64. Default: 8. Returns to the song's normal tempo over this many bars. **Glide back (bars)**.
- `drop_mix`. Values: true or false. Default: false. Places the incoming drop at a handoff when possible. **Drop mixing**.
- `key_shift`. Values: true or false. Default: false. Shifts pitch for a harmonic transition when possible. **Key shift**.
- `outro_intro_align`. Values: true or false. Default: false. Aligns the outgoing outro with the incoming intro. **Outro and intro align**.
- `phrase_align`. Values: true or false. Default: false. Aligns transitions with beat grid phrases. **Phrase align**.
- `phrase_bars`. Values: whole number from 1 to 64. Default: 8. Sets the phrase length for alignment. **Phrase length (bars)**.
- `filter_sweep`. Values: true or false. Default: false. Filters the outgoing song during a transition. **Filter sweep**.
- `filter_sweep_floor_hz`. Values: number of hertz; the page accepts 20 to 5000. Default: 250.0. Sets the lowest filter cutoff in a sweep. **Filter sweep floor (hertz)**.
- `harmonic_mode`. Values: `off` (off (no key filter)), `compatible` (compatible (Camelot match, plus or minus 1, relative)), `strict` (strict (same Camelot position only)), `neighbour` (neighbour (same side, plus or minus 1 only)), `mood_change` (mood change (relative major or minor only)), or `energy_boost` (energy boost (same side, plus 2 jump)). Default: `off`. Limits next song keys using the selected rule. **Harmonic mixing**.
- `vocal_guard`. Values: true or false. Default: true. Avoids overlapping known vocal sections. **Keep vocals apart**.

### Transitions

Station section: `transitions` in `station.json`.

- `effect`. Values: one of the Transition effect menu choices listed below. Default: `none`. Adds an effect to a crossfade; saved segue points do not use it. **Transition effect**.
- `wet_mix`. Values: number from 0 to 1. Default: 1.0. Sets effect volume; the page shows 0 to 100 percent, and 0 leaves a plain crossfade. **Transition effect level (percent)**.

### ReplayGain

Station section: `replaygain` in `station.json`.

- `playback.intro_lift_max_db` (stored under `playback`). Values: 0 to 6 decibels. Default: 4.0. Raises a quiet opening by up to this many decibels in every output processing mode except Off. **Quiet intro lift limit (decibels)**.

- `enabled`. Values: true or false. Default: false. Selects the older ReplayGain switch when loudness_processing is blank.
- `target_db`. Values: number of decibels; the page accepts -30 to 0. Default: -14.0. Sets the target loudness for ReplayGain processing. **ReplayGain target loudness (decibels)**.
- `max_clip_safe_gain`. Values: number; the page accepts 0.1 to 2. Default: 1.0. Limits gain to avoid clipping; above 1 allows peaks over full scale. **ReplayGain peak limit**.

### Station

Station section: `station` in `station.json`.

- `timezone`. Values: IANA time zone or blank. Default: blank. Sets this station's schedule time zone; blank uses the system time zone. **Time zone**.
- `liner_upload_max_mib`. Values: whole number from 1 to 1024 MiB. Default: 50. Limits voice liner and cart uploads. **Maximum liner upload size (MiB)**.
- `output`. Values: `browser` (Browser), `server` (Server sound card), or `stream` (Radio stream). Default: `browser`. Chooses where this station plays; a newly created named station defaults to stream even though the dataclass default is browser. **Playback output**.
- `server_audio_too`. Values: true or false. Default: false. Also plays the stream mix through this computer's speakers. **Also play the radio stream on the server sound card**.

### Stream

Station section: `stream` in `station.json`, except for listener credentials and relays.

- `continuous`. Values: true or false. Default: false. Keeps the stream playing without listeners. **Keep playing without listeners**.
- `bitrate`. Values: 128, 192, 256, or 320 kbps. Default: 320. Sets MP3 quality. **Quality**.
- `idle_grace_seconds`. Values: more than 0 and at most 3600 seconds. Default: 30.0. Sets how long playback waits after the last listener leaves.
- `max_listeners`. Values: whole number from 1 to 100. Default: 8. Limits listeners across all formats.
- `max_listeners_per_address`. Values: whole number from 0 to 100. Default: 0. Limits connections from one address; 0 means unlimited. **Connections per address (0 for unlimited)**.
- `listener_user`. Values: user name or empty string. Default: empty string. Sets an optional listener login with listener_password. Stored under `stream` in this station's private `secrets.json`.
- `listener_password`. Values: password or empty string. Default: empty string. Protects the stream when set with listener_user. Stored under `stream` in this station's private `secrets.json`.
- `station_name`. Values: printable name up to 100 characters. Default: `AutoDJ` for the default station; a named station without this field uses its station ID. Names the station in players and feeds. **Station name**.
- `genre`. Values: printable text up to 255 characters. Default: empty string. Sets the genre sent to players. **Genre**.
- `description`. Values: printable text up to 255 characters. Default: empty string. Sends the station description to Icecast only. **Description**.
- `url`. Values: website URL up to 2048 characters. Default: empty string. Sets the station website sent to players. **Website URL**.
- `relays`. Values: up to 8 relay definitions. Default: empty set. Pushes the stream to external servers. Stored in this station's private `relays.json`; fields follow below.
- `aac_bitrate`. Values: 0, 64, 96, 128, 192, or 256 kbps. Default: 0. Adds an AAC stream; 0 turns it off. **AAC quality**.
- `opus_bitrate`. Values: 0, 48, 64, 96, 128, or 192 kbps. Default: 0. Adds an Opus stream; 0 turns it off. **Opus quality**.
- `stats_days`. Values: whole number from 0 to 3650. Default: 90. Keeps this many days of listener counts; 0 keeps none. **Days to keep statistics**.

### Recording

Station section: `recording` in `station.json`.

- `folder`. Values: folder path. Default: `recordings` for the default station and `recordings/<id>` for a new named station. Stores broadcast MP3 recordings outside normal backups; two stations cannot use the same recording folder.
- `split`. Values: `hour` (Every clock hour) or `show` (For each show). Default: `hour` (Every clock hour). Starts a new recording every clock hour or at show boundaries. **Start a new file**.
- `retention_days`. Values: nonnegative whole number. Default: 0. Removes recordings after this many days; 0 keeps them. **Days to keep recordings**.
- `free_space_mb`. Values: nonnegative whole number of MB. Default: 100. Stops recording when less space is free. **Minimum free space in MB**.

### Now-playing feed

Station section: `nowplaying` in `station.json`.

- `show_next`. Values: true or false. Default: false. Shows the next song on public pages unless DMCA mode hides it. **Show the next song publicly**.
- `history_limit`. Values: whole number from 0 to 20. Default: 5. Limits recent songs in the public feed.

### Requests

Station section: `requests` in `station.json`. **Take listener requests** and the other controls in **Listener requests**, **Request settings** save changes for the selected station.

- `enabled`. Values: true or false. Default: false. Opens the public listener request page. **Take listener requests**.
- `approval`. Values: `manual` (I approve each one) or `auto` (Automatic: a request that keeps the rules joins the queue at once). Default: `manual`. Requires an operator to approve requests or accepts eligible ones automatically. **Approval**.
- `per_listener`. Values: whole number from 1 to 100. Default: 3. Limits requests from one listener in the time window.
- `per_listener_minutes`. Values: whole number from 1 to 1440. Default: 60. Sets that listener limit window.
- `daily_limit`. Values: whole number from 1 to 10000. Default: 50. Limits all requests in one day.
- `max_pending`. Values: whole number from 1 to 200. Default: 20. Limits requests waiting for approval.
- `window_minutes`. Values: whole number from 5 to 240. Default: 30. Sets the request scheduling window.
- `blocked_artists`. Values: up to 500 artist names. Default: empty list. Stops requests for these artists.
- `blocked_folders`. Values: up to 500 folder paths. Default: empty list. Stops requests for songs in these folders.
- `ipv6_prefix`. Values: whole number from 32 to 128. Default: 56. Groups IPv6 addresses for per listener limits.
- `bridges_enabled`. Values: true or false. Default: false. Adds songs to smooth a request transition. **Add bridge songs before a request that would sound abrupt**.
- `max_bridges`. Values: whole number from 1 to 3. Default: 3. Limits bridge songs before a request. **Most bridge songs**.
- `bridge_cost_threshold`. Values: number from 0 to 10. Default: 2.0. Sets the roughness level that triggers bridges. **Roughness limit**.
- `no_smooth_spot`. Values: `best` (Use the best spot that keeps the rules) or `wait` (Keep the request waiting for you to decide). Default: `best`. Uses the best allowed slot or waits for an operator. **When no smooth spot is found**.

### Player page

Station section: `player_page` in `station.json`. Set these controls in **Settings**, **Stream** for the selected station.

- `enabled`. Values: true or false. Default: false. Opens the public listening page. **Public player page** in **Settings**, **Stream**.
- `embed`. Values: true or false. Default: false. Opens an embeddable public player. **Embeddable player** in **Settings**, **Stream**.
- `embed_origins`. Values: up to 20 HTTP or HTTPS origins. Default: empty list. Lists the sites that may embed the player. **Allowed embed sites** in **Settings**, **Stream**.

### DMCA mode

Station section: `dmca` in `station.json`. Set this switch in **Settings**, **Song picking** for the selected station.

- `enabled`. Values: true or false. Default: false. When on, limits how often songs from one album, artist, or box set can play and hides the next song publicly. **DMCA mode** in **Settings**, **Song picking**.

### Breaks

Station section: `breaks` in `station.json`.

- `enabled`. Values: true or false. Default: false. Accepts external announcer uploads on this station. Manual voice tracks are independent. **Accept announcer uploads**.
- `dj_placement`. Values: `auto` (Automatic), `talk_up` (Talk up the next song), or `standalone` (Between the songs). Default: `auto`. Places DJ breaks over an intro or between songs. **Where a DJ break goes**.
- `min_intro_seconds`. Values: 0 to 60 seconds. Default: 2.0. Sets the shortest intro for automatic talk up. **Shortest intro to talk up (seconds)**.
- `duck_db`. Values: -30 to 0 decibels or blank. Default: blank. Lowers music under a break; blank uses the voice liner setting. **Music under a break (decibels)**.
- `max_upload_mb`. Values: whole number from 1 to 200 MB. Default: 40. Limits each uploaded break file.
- `max_seconds`. Values: 5 to 900 seconds. Default: 300.0. Limits each break's length.
- `max_queued`. Values: whole number from 1 to 50. Default: 6. Limits breaks waiting for a song change.
- `max_per_slot`. Values: whole number from 1 to 5. Default: 2. Limits breaks booked for one song change.
- `default_expires_seconds`. Values: whole number from 60 to 86400. Default: 1800. Sets a break's default expiry; cannot exceed max_expires_seconds.
- `max_expires_seconds`. Values: whole number from 60 to 86400. Default: 7200. Limits the requested expiry.
- `quota_mb`. Values: whole number from 1 to 100000 MB. Default: 400. Limits stored break audio; cannot be less than max_upload_mb.
- `requests_per_minute`. Values: whole number from 1 to 6000. Default: 120. Limits announcer API requests.

### Ads

Station section: `ads` in `station.json`.

- `enabled`. Values: true or false. Default: false. Enables ad scheduling.
- `max_upload_mb`. Values: whole number from 1 to 200 MB. Default: 20. Limits each uploaded ad spot.
- `max_spot_seconds`. Values: 5 to 600 seconds. Default: 120.0. Limits an ad spot's length.
- `max_spots`. Values: whole number from 1 to 1000. Default: 200. Limits stored ad spots.

### Time checks

Station section: `time_checks` in `station.json`.

- `enabled`. Values: true or false. Default: false. Lets the rule and schedule play spoken times. **Enable time checks**.
- `clips_folder`. Values: folder path or blank. Default: blank. Chooses the recorded minute clip folder. **Recorded minute clips folder**.
- `hour_format`. Values: 12 or 24. Default: `12`. Chooses spoken hour format. **Hour format**.
- `intro_clip`. Values: clip name or blank. Default: blank. Plays before each time check. **Intro clip**.
- `outro_clip`. Values: clip name or blank. Default: blank. Plays after each time check. **Outro clip**.
- `tts_command`. Values: list of nonempty program arguments containing `{out}` exactly once; `{text}` is optional. Default: empty list. Runs a speech program to make a clip, for example `["espeak-ng", "-w", "{out}", "{text}"]`.
- `rule`. Values: `off` (Only from Schedule or the button) or `after_hour_song` (After the song that crosses the hour). Default: `off`. Plays only on demand or after the song crossing the hour. **When to play**.
- `voice_packs`. Values: list of pack names. Default: empty list. Selects the packs used to speak time. **Voice packs**.
- `pack_order`. Values: `rotate` (Take turns) or `random` (Pick one at random). Default: `rotate` (Take turns). Chooses among selected voice packs. **When several packs are ticked**.

### Repeat rules

In **Settings**, **Song picking**, each Tracks value counts intervening songs and each Minutes value counts elapsed time. Both can be set; 0 disables that part of a rule. Tracks accept whole numbers from 0 to 100000; minutes accept numbers from 0 to 10080.

- Artist: 8 tracks, 0 minutes. Related artist: 8 tracks, 0 minutes, with **Use related artists** off.
- Album: 0 tracks, 0 minutes. Track: 50 tracks, 0 minutes. Title: 0 tracks, 0 minutes.
- Related track: 20 tracks, 0 minutes, with **Use related tracks** off. **Count versions as one song** is off.
- **Prevent a song from playing in the same daypart**: 0 days; starts at 0, 6, 10, 15, and 19 hours. Days accepts 0 to 7; start hours accept 0 to 23.
- **Prevent a song from playing in the same quarter of the hour**: 0 plays; accepts 0 to 3.

### Relays

Each entry in the station's private `relays.json` names one relay. Names use 1 to 32 letters, digits, hyphens, or underscores. Change relays in **Settings**, **Stream**, **Relays**. Old TOML relay tables are migrated once at startup, then ignored.

- `type`. Values: `icecast` (Icecast), `shoutcast1` (Shoutcast 1), or `shoutcast2` (Shoutcast 2). Default: required in `relays.json`; the form starts with Icecast. Chooses the host server protocol. **Server type**.
- `host`. Values: host name or IP address without a scheme or port. Default: required. Names the relay server. **Host**.
- `password`. Values: nonempty source password. Default: required. Lets AutoDJ connect as a source; it stays in private `relays.json`. **Source password**.
- `port`. Values: 1 to 65535 for Icecast, or 1 to 65534 for Shoutcast. Default: 8000. Names the listener port given by the host. **Listener port**.
- `mount`. Values: Icecast path starting with `/`. Default: required for Icecast in `relays.json`; the form suggests `/live`. Names the stream path on Icecast. **Mount point**.
- `stream_id`. Values: whole number from 1 to 2147483647. Default: 1 for Shoutcast 2. Selects the stream on a Shoutcast 2 server. **Stream ID**.
- `user`. Values: nonempty user name. Default: `source` for Icecast. Names the Icecast source account. **Username**.
- `bitrate`. Values: station quality, 128, 192, 256, or 320 kbps. Default: station quality. Sets the MP3 bitrate sent to this relay. **Bitrate**.
- `public`. Values: true or false. Default: false. Asks the host to list the relay publicly. **Public directory listing**.
- `enabled`. Values: true or false. Default: true. Connects this relay when on. **Enabled**.

### Other page controls

These controls have possible values and defaults even when they do not appear in `station.json`.

These values are saved for the selected station or browser rather than in the configuration dataclasses above.

- **Volume** in **Now Playing**: 0 to 100 percent; default 100. Changes playback loudness.
- **EQ**, **Low**, **Mid**, and **High** in **Now Playing**: each 0 to 2 times normal level; default 1. Changes bass, middle, or treble; **Reset EQ** returns all three to 1.
- **Audio output device** in **Settings**, **Sound**: system default or an available browser output device; default system default. Sends browser playback to that device.
- **Preview output device** in **Settings**, **Sound**: system default or an available browser output device; default system default. Sends Track info and Voice tracks previews to that device.
- **Stereo Tool licence key** in **Settings**, **Sound**: valid key or blank; default blank. Unlocks licensed Stereo Tool features. **For this station only**: on or off; default off. When on, saves this station's key in its private `secrets.json`; otherwise it is shared by stations without their own key.
- **Automatic rescan every** in **Library tools**: **Hours** 0 to 168 and **Minutes** 0 to 59; default 0 and 0 (off). Runs **Rescan** after the last scan ends; the shortest enabled interval is 5 minutes.
- **Add subfolders** for a registered music folder: on or off; default on. Includes nested folders in import. **Perform advanced analysis**: on or off; default on. Runs sound matching analysis for music in that folder.
- **Find beats and song structure** in **Library tools**: on or off; default on. Finds beat grids and song sections during basic analysis.
- **Longest song, in minutes** under **Advanced analysis length** in **Library tools**: 0 or a positive number; default 15. Longer songs get basic analysis only; 0 runs advanced analysis on every length.
- **Display name** in **Settings**, **Live DJ**: printable name up to 80 characters; default blank. Shows the DJ name to listeners and in History while live.
- **Time format for all stations** in **Settings**, **Stations**: 24 hour or 12 hour; default 24 hour. Changes time display for every station.
- **Theme** in **Settings**, **Display and keys**: System, Light, or Dark; default System. Changes this browser's colors.
- **Announce track changes**: On every tab, Only on Now Playing, or Off; default Only on Now Playing. Controls spoken song changes in this browser.
- **Enable keyboard shortcuts**: on or off; default on. Lets keys control playback outside text fields.
- **Bracket keys seek the song on air**: on or off; default off. Lets bracket shortcuts move the live song.
- **Anchor to seed** in **Song picking**: on or off; default off. Keeps similarity near the session's first song.
- **BPM range** in **Song picking**: optional minimum and maximum BPM; default blank. Restricts automatic picks.
- **Inject distant tracks**: on or off; default off. Enables the `discovery_every` interval.
- **Start with the server** in **Stations**: on or off for a named station; new stations default off. Starts that station on server startup.
- **Source address** and **DJ accounts** in **Live DJ**: station URL and named DJ credentials; no DJ accounts by default. They let a live source take over a stream.
- **Return time (seconds)**: 0.1 to 10; default 0.5. Raises the music after browser talkover. **Duck amount (dB)**: 0 to 30; default 12. Lowers music while someone talks.
- **Record broadcast** in **Stream**: on or off; default off. Saves the server mix as MP3 files.
- **Station name**, **Quality**, and other stream page controls map to the `stream` entries above.

### Transition effect choices

The **Transition effect** choices are none, echo out, reverb tail, high-pass sweep, low-pass sweep, tape stop, gate stutter, noise riser, backspin, forward spin, cross-EQ swap, bitcrusher, flanger, pitch swell, pitch fall, telephone, noise drop, chorus, submerge, vinyl wow, freeze, glitch, scratch, beat repeat, sidechain pump, reverse reverb, air horn, vinyl rewind, transformer, dub siren, stutter build, wow and flutter, phaser, ring modulator, dub delay, halftime, auto, random, and rotate. The default is none. Some browser effects need HTTPS; the page identifies them when opened over HTTP.

### Station definitions

AutoDJ creates `stations/default/station.json` at startup. The page creates
`stations/<slug>/station.json` for each new named station, which starts stopped. The **Start from**
choice is AutoDJ defaults or a saved station; copied settings exclude secrets, history, relays,
and requests. **Also copy** can include Clockwheels, Schedule, and Carts. **Start with the server**
defaults off for a new named station. Station IDs use up to 32 lower-case letters, digits, and
hyphens. A named station's saved settings and content can be edited while it is stopped. The
default streams at `/stream.mp3` when streaming; named stations use `/<slug>.mp3`.

### Webhooks

Each entry in the station's private `secrets.json` `webhooks` list sends a station event to another service. The default is no webhooks. Up to 10 entries are accepted. Legacy webhook entries in `config.local.toml` are migrated once at startup, then ignored.

- `url`. Values: HTTP or HTTPS URL without user information. Default: required. Names the receiving service.
- `format`. Values: `generic` (generic JSON) or `discord` (Discord message). Default: `generic`. Chooses the message shape.
- `events`. Values: `track_change` only. Default: `track_change`. Sends a call when the song changes.

### Presets

`presets.NAME` in `station.json` defines a named tempo preset. The default is no custom presets. Choose one with **Preset** in **Settings**, **Song picking**. A preset needs a target BPM, both start and end BPM, or a slide curve.

- `bpm_target`: number or blank; default blank. Keeps the preset near one BPM. A slide uses it as the peak if `bpm_end` is blank.
- `bpm_start`: number or blank; default blank. Sets the starting BPM of a linear or slide curve; a slide uses 80 when blank.
- `bpm_end`: number or blank; default blank. Sets the final BPM of a linear curve or the peak of a slide curve; a slide uses `bpm_target` or 130 when blank.
- `curve`: `linear`, `slide`, or blank; default blank. Blank uses a constant target or a linear curve when both endpoints are set.
- `bpm_weight`: number from 0 to 1; default 0.25 for constant and slide curves, 0.30 for a linear curve. Sets how strongly the picker favors the BPM target.
- `horizon_tracks`: whole number; default 30. Sets how many picks a tempo curve takes.
- `discovery_every`: positive whole number or blank; default blank. Adds a distant track at this interval while the preset is active.

AutoDJ and this page were developed with AI assistance.
