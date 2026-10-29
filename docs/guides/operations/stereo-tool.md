# Install Stereo Tool and configure licences

Install the supported plugin library, configure shared or station-specific licence keys, and check plugin status and platform limits.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Getting Stereo Tool and its licence

AutoDJ cannot include Thimeo's Stereo Tool, and the container image does not either. Each install
gets Thimeo's Stereo Tool plugin library (`libStereoTool`, the same one AzuraCast and Liquidsoap
use) itself, from **Settings**, **General settings**, **Server**, with no configuration file.
Its processing mode and preset are set in **Settings**, **Profile settings**, **Sound**:

1. Read Thimeo's [terms and conditions](https://www.thimeo.com/terms-and-conditions/) and
   [licensing](https://www.thimeo.com/stereo-tool/licenses/) (both linked there), then tick
   **I accept Thimeo's terms for Stereo Tool**. Download refuses without the tick and moves focus
   to the box.
2. Press **Download Stereo Tool**. The server fetches Thimeo's Generic plugin zip
   (`Stereo_Tool_Generic_plugin.zip`, about 320 MB) straight from thimeo.com over HTTPS and keeps
   only the one library this server needs: `libStereoTool_intel64.so` on 64-bit Linux PCs,
   `libStereoTool_noX11_arm64.so` on 64-bit ARM Linux (a Raspberry Pi 4 or 5 with a 64-bit
   system), and `libStereoTool_64.dll` on 64-bit Windows. The Linux libraries are the builds
   without X11, so a server or container without a desktop can load them. It takes a few minutes;
   the status line says "Downloading Stereo Tool from thimeo.com" until it ends, then "Stereo Tool
   11.05 is installed", with the version from the zip. The button then reads **Update Stereo
   Tool**; press it to fetch Thimeo's latest plugin. A running Stereo Tool restarts with the new
   library, and the sound bypasses it for a moment.
3. Paste the key Thimeo sent you (it starts with `<` and ends with `>`) in **Stereo Tool licence
   key** and press **Save licence key**. The status line says "Licence set" or "No licence"; the
   key is never shown again. Right after a save the page says "Stereo Tool checks the key in a
   moment"; a few seconds later, once Stereo Tool has restarted with the key, the line adds what
   Stereo Tool itself says, and speaks it if it changed: "Stereo Tool says the licence key is
   valid", or "Not licensed: Stereo Tool says the licence does not cover this preset", followed
   by the features it lists, while the station plays Thimeo's unlicensed reminder. **Clear licence** deletes the saved key after
   asking. Stereo Tool restarts when the key changes: a key for every station restarts every
   station that runs Stereo Tool, a key for one station restarts only that one, and saving the
   same key again restarts nothing.
4. In **Sound**, choose Stereo Tool under **Output processing**, then choose a preset in the
   **Stereo Tool** group or upload your own `.sts` file there.
   AutoDJ loads it with all of its processing settings but not Stereo Tool's configuration
   settings, so a preset cannot change how Stereo Tool itself is set up.

Without a key, Stereo Tool runs as its free version, with the limits Thimeo sets for it.

The download needs about 400 MB free in the index folder while it runs (the zip is deleted
afterwards) and stops if it has not finished within 30 minutes. If any check fails, the old
library stays and the page says why, for example "Could not download Stereo Tool: thimeo.com
answered with error 503."

The library is kept in `tools/stereo-tool/` inside the index folder (`/index` in the container),
under its name from the zip, with a `.version` file beside it. Every station uses that copy;
there is no setting for another one. You can also put the library from a plugin zip you
downloaded yourself from thimeo.com there by hand, under the same name; the page then shows the
version Stereo Tool reports once it runs. Backups leave the library out.

AutoDJ no longer runs Thimeo's command-line program (`stereo_tool_cmd`), and the old
`[playback] stereotool_path` and `stereotool_license_file` settings are gone. A config that still
has them gets an unknown-key warning and is otherwise unaffected; delete the lines.

AutoDJ never loads the library into the server itself. Each station that uses Stereo Tool starts
a small helper process of AutoDJ's own (`python -m autodj.stereotool_host`) that loads the library
and turns it into a pipe; the mix goes through it and then through AutoDJ's output limiter. If
Thimeo's code crashes, only that helper ends: the station plays on with its limited bypass and
starts Stereo Tool again. The library prints its own messages too, such as ALSA and JACK
complaints ("Unknown PCM dmix", "jack server is not running") because it looks for sound cards it
does not use. AutoDJ discards them: they never reach its log or the page.

**Delay.** A preset adds its own delay (about 100 ms for a typical broadcast preset). The helper
measures it when Stereo Tool starts and drops that much of Stereo Tool's first output, so the
processed audio stays in time with the mix and the station's advertised delay stays the same
480 ms as with Smooth processing; nothing adds the preset's delay on top. A preset whose delay is
over about 160 ms makes Stereo Tool take longer to take over from the bypass when it starts. The
log line "Stereo Tool 11.05 started; its preset adds 100.6 ms, dropped to stay in time (preset
load type 20386)" gives the measured delay and how the preset was loaded.

The download only ever fetches Thimeo's own plugin: the page sends no address and no file, the
server follows redirects only to thimeo.com hosts over HTTPS, refuses a zip over 768 MB, takes out
only the member for this computer (refusing a zip that names anything outside its own folder, or
a library that is a link or over 256 MB), and checks that it is a 64-bit shared library for this
processor before it replaces anything. Nothing downloaded is run during the download. The page
never accepts an uploaded file: that would let anyone who can change settings run any code on
the server.

**Tested so far.** Linux on 64-bit PCs (x86_64) and 64-bit Windows have been tested with
Thimeo's real plugin: it loads, processes the mix and reports its licence. 64-bit ARM Linux
(`libStereoTool_noX11_arm64.so`) uses the same code but has not been tested with Thimeo's real
library yet. On Windows, AutoDJ's index folder must be on a path made of letters Windows' ANSI
code page can spell, or Stereo Tool says it cannot open its preset.

AutoDJ downloads no library for 32-bit systems, 32-bit ARM, Windows on ARM or macOS. Thimeo's zip
has builds for some of them; to try one, put it in `tools/stereo-tool/` in the index folder under
the name AutoDJ looks for.
The page says so on those computers instead of offering the download.

If a station is set to Stereo Tool and the library is not there, the station plays on with its
limited bypass, and `/api/status` reports `playout.error` as "Stereo Tool master unavailable:
Stereo Tool is not installed. Download it in Settings, General settings, Server, with the Download Stereo Tool
button." It starts Stereo Tool by itself within a second of the download finishing. While
Stereo Tool runs, `/api/status` reports what it said under `playout.stereotool`: its version, the
delay its preset adds (`latency_ms`), and whether the licence covers the preset.

In Docker, use the Download button, or copy the library into the index volume:

```bash
docker compose exec autodj mkdir -p /index/tools/stereo-tool
docker compose cp libStereoTool_intel64.so autodj:/index/tools/stereo-tool/libStereoTool_intel64.so
```

### One licence or one per station

The key saved from Settings, General settings, Server is for every station. With more than one station, tick **For
this station only** before saving to give the station the page controls a key of its own; it uses
that key instead. Thimeo requires a separate licence for each radio-processing instance; one key
does not license several stations running at the same time. Check Thimeo's
[installation guidance](https://www.thimeo.com/stereo-tool/installed/) and
[licensing terms](https://www.thimeo.com/stereo-tool/licenses/). AutoDJ does not claim that
unrestricted broadcast use is free.

Saved keys are kept in files readable only by the user AutoDJ runs as (mode 0600 on Linux and
macOS, a protected access list on Windows): `<index_dir>/.stereo-tool-license` for every station,
and the station's `secrets.json` in its own folder for that station, beside its other secrets.
Saving or clearing a station's key leaves the rest of `secrets.json` as it is. Backups never
include them, and duplicating a station does not copy its key, so save the key again after
restoring on another machine. Older versions kept a station's own key in `.stereo-tool-license`
in the station's folder; AutoDJ moves it into `secrets.json` the first time it reads it, deletes
the old file, and logs one line saying so, without the key. A saved key that cannot be read is
reported on the page ("No licence. This station's saved licence cannot be read ...").
An older AutoDJ version cannot read a station's `secrets.json` after it holds that key. If you go
back to an older version after this move, save each station's own key again in that version.

For an install without a browser, give the key in `AUTODJ_STEREOTOOL_LICENSE_KEY` instead. It
is read when Stereo Tool starts and overrides the key saved for every station, which the page
then reports as set by the server's environment and cannot change. The order, first match wins,
is: the station's own saved key, the environment, the key saved for every station.

AutoDJ hands the key to the library inside the helper process, over the helper's standard input,
then zeroes the buffers that held it. It is never on a command line and never in the helper's
environment (AutoDJ's own `AUTODJ_*` variables are left out of it), so other users cannot read it
from the process list. The user AutoDJ runs as, and root or administrators, can still read it
from the key file or from the processes' memory. AutoDJ never writes the key to its log. See
`THREAT_MODEL.md`.

### Several Stereo Tool instances

Each station that uses Stereo Tool runs its own helper process with its own Stereo Tool instance,
so stations may run Stereo Tool at the same time with the same library. Each instance needs its
own licence (above).

Each running station adds a real-time processor and encoder. Test CPU load and underruns while
all intended stations run; AutoDJ does not impose a machine-independent station limit.

Stereo Tool preset files are shared by all stations, and each station selects its own preset. A
preset selected by any station, running or stopped, cannot be deleted; the message names the
station using it.


## Related tasks and contracts

- [Server playback controls and idle behavior](../../reference/operations/server-playback.md)
- [Measure multi-station resource use](station-soak.md)
