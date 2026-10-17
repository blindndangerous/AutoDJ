# Troubleshoot AutoDJ

Start with the symptom you can see. Keep the terminal open while testing: AutoDJ reports startup errors there, and **Library tools**, **Output** shows library job messages. Run `autodj doctor` for a broader check. On Windows use `.\autodj.cmd doctor`; on Linux and macOS use `./autodj doctor`.

## Installing and starting

**The launcher cannot find uv or FFmpeg.** On Windows, run `.\autodj.cmd` in PowerShell and accept its tool installation prompt, or install the missing tool yourself. On Linux and macOS, install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [FFmpeg](https://ffmpeg.org/download.html). Run Doctor again.

**The page does not open.** Use the exact address printed by AutoDJ, usually `http://localhost:8080` on the same computer. If the port is in use, run `autodj serve --port 8081` and open the new address. A different device needs LAN mode and pairing; follow [Use AutoDJ from other devices](install.md#use-autodj-from-other-devices).

**Docker starts but cannot read music or save data.** Check the paths in `.env` and the container logs with `docker compose logs autodj`. Inside the container, the music folder is `/music`. Give the container user read access to music and write access to the index and model folders. The [Docker and NAS steps](install.md#docker-and-nas) explain the mounts and user IDs.

## Music and the library

**No songs appear.** In **Library tools**, check **Music folders and Voice liner folders**. Add the path the AutoDJ computer can see, then run **Rescan**. In Docker use `/music`, not the host's path. If a network share is disconnected, mount it before rescanning.

**Songs appear but are not picked.** Check **Settings**, **Song picking** and the active clockwheel. A category may be empty, restricted to another time, or blocked by repeat rules. Use **Test rotation** and check its reasons. Songs can be queued while analysis runs. [Build your sound](build-your-sound.md) explains the pick modes.

**Advanced analysis is slow.** This is normal for a large library on a processor. Check **Library jobs** and **Output** for progress. You can limit a first command-line run with `autodj index --limit 50`. A separate analysis worker can use another computer's GPU; see [Using a GPU machine](../operations.md#using-a-gpu-machine).

**A file moved or vanished.** Run **Rescan**. If the drive is missing, reconnect it first. After a clean scan, **Remove missing tracks** can remove records for files that are truly gone. Each station keeps its play history apart from the shared catalog.

## Playback and sound

**The browser is silent.** Start playback in **Now Playing**, check the browser tab's volume and the selected output device in **Settings**, **Sound**. A browser may require a click before it can play audio. If the server mixes the station, check the stream address in **Settings**, **Stream**.

**A transition sounds abrupt.** Check whether the songs have intro, outro, BPM, and beat grid data in **Track info**. The default transition can use detected points; without them, it uses a simpler handoff. Try **Transition mode** and **Crossfade seconds** in **Settings**, **Transitions**. Disable **Beatmatch** for tracks whose tempo is unreliable.

**Stereo Tool is unavailable.** Open **Settings**, **Sound** and follow its plugin library download steps after accepting Thimeo's terms. Check the panel's status message and run Doctor if the download or load fails.

**Mastering startup messages.** Compare processor readiness timings and discarded-input counts across restarts before changing buffering. Non-silent discarded blocks may contain program audio; startup silence and overload warnings have different meanings. See [Mastering startup diagnostics](../reference/operations/server-playback.md#mastering-startup-diagnostics) for the fields, bypass behavior and timing exclusions.

## Streaming

**The stream is missing.** In **Settings**, **Stations**, set **Playback output** to **Radio stream** and press **Save station options**. In **Stations on this server**, stop the station, confirm **Stop station**, then start it again; this applies to the default station too and disconnects its listeners. **Settings**, **Stream** appears when stream output is active. For a one-time default stream, start AutoDJ with `serve --stream`. FFmpeg must be installed. The default stream is `/stream.mp3`; named stations use `/<station-id>.mp3`.

**A player cannot connect.** Copy **Stream address** from **Settings**, **Stream** while the page is open through a network address. An address containing `127.0.0.1` works only on the AutoDJ computer. Check the firewall, the listener limit, any password, and address bans. A stopped station has no active stream.

**A relay is disconnected.** In **Settings**, **Stream**, open **Relays** and read the relay's last error. Check the host, listener port, mount point or stream ID, and source password supplied by the host. Relay source passwords cross the network without TLS, so use a trusted connection.

**The wrong station plays.** Use the **Station** button at the top of the page to choose which station you control. Then check **Settings**, **Stations** for its running state and **Start with the server** setting. Each station has its own queue, history, sound settings, and stream.

## Get more detail

Run `autodj doctor --station nightowl` to check one named station, or `autodj doctor` for all of them. For backup, restore, network, and migration procedures, use [AutoDJ operations](../operations.md). Follow [Collect diagnostics and report a problem](collect-diagnostics.md) to gather read-only evidence, record a minimal reproduction and review private information before sharing. Report suspected vulnerabilities through the private security process linked there.

AutoDJ and this page were developed with AI assistance.
