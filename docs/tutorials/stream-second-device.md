# Hear Practice on a second device

You will move the Practice station's audio from the browser to AutoDJ's server,
then listen from another device. You will learn to distinguish controlling a
station from listening to its stream, and test what happens when its last
listener leaves.

Start with [Build your first station](first-station.md) complete: at least five
analyzed songs, the Practice category and one-step Practice clockwheel, and
**Pick mode** set to **Clockwheel**. Use the default station, paused in
**Browser** output, with no listeners. LAN setup may restart AutoDJ; use a
practice server with no other stations serving listeners.

## Before you begin

You need FFmpeg on the AutoDJ computer and a second device on the same trusted
local network. Use a network audio player on that device; the worked example
uses VLC's network-stream function. No relay, paid host, router port forwarding,
or public Internet address is needed. Choose music you may share on this network.

Start this lesson with AutoDJ already reachable in LAN mode and your control
browser paired. If necessary, complete
[LAN startup and pairing](../guides/install.md#use-autodj-from-other-devices)
first, using the procedure for your installation. Do not start a second server
over the existing instance.

You will save an output change and enable LAN access. In LAN mode, browsers
need pairing to control AutoDJ, but anyone who can reach the stream address can
listen unless listener access controls restrict them. Pairing does not make
the audio private. Keep existing access controls; do not use `--insecure-lan`.
Plain HTTP does not encrypt network traffic. Use the existing HTTPS address if
your installation has one; public hosting is outside this lesson.

Use headphones or low volume to avoid hearing two delayed copies. You can stop
your own listening by stopping VLC or toggling **Listen here** off. That does
not necessarily stop the station; you will check the distinction below.

Use Tab and Enter or Space for controls, and arrow keys in tab strips. The
[keyboard guide](../guides/keyboard-and-screen-reader.md) covers the app's tables.

## Give the server responsibility for audio

1. In **Queue & Search**, clear any remaining practice queue using **Clear queue**
   and its confirmation. This removes queued entries, not songs from the library.
2. Open **Settings**, **Stations**. Under **Output for the selected station**,
   choose **Radio stream** in **Playback output**.
3. Leave **Also play the radio stream on the server sound card** unchecked.
4. Press **Save changes** in the Stations tab.
5. In **Stations on this server**, use **Stop** for the default station, confirm
   **Stop station**, and wait for **Off air**. Then use its **Start** button.

Saving the output does not convert the running player. The new output takes
effect when the station next starts. Stop and Start apply it to that station.
Changing the server's LAN startup option needs a server restart. That interrupts
every station on that server, which is why this lesson requires an unused one.

Open an address printed by AutoDJ that uses the computer's LAN address. Pair
the control browser if asked, using the current pairing code. Keep that code
private; VLC will not need it. If the code expired, obtain another using the
[installation instructions](../guides/install.md#use-autodj-from-other-devices).

Check that **Settings**, **Stream** is now available and **Playback output**
remains **Radio stream**. If Stream is missing, check that you restarted the
same installation and saved the output for the default station. Do not try to
repair this by repeatedly opening new servers.

## Open the stream on the second device

1. In **Settings**, **Stream**, read **Stream address** and press **Copy address**.
2. Confirm the address uses the AutoDJ computer's network name or LAN address,
   rather than `localhost` or `127.0.0.1`. For the default station it ends in
   `/stream.mp3`.
3. On the second device, open VLC's network-stream command. On desktop VLC this
   is **Media**, **Open Network Stream**. Paste the copied address and start it.
   Mobile VLC places its network-stream command differently.
4. On the control page, open **Now Playing** and note the song title. Open
   **Settings**, **Stream** and inspect the listener count and **Listeners** list.

You should hear music in VLC and see a connected listener in AutoDJ. The title
may reach the player later than the control page because the player buffers
audio; do not expect sample-synchronized playback. The listener count is the
text check that a stream connection exists, and **Now Playing** and **History**
let you inspect the songs without relying only on hearing them.

If VLC cannot connect and the copied address contains `localhost`, you have a
specific address error: on that second device, localhost means the second
device itself. Reopen the control page using AutoDJ's printed LAN address,
copy **Stream address** again, and replace the address in VLC. If it still
fails, check that both devices share the intended network and that the host's
firewall allows this private-network connection. Keep existing listener
passwords and limits; a refused or full stream is not fixed by removing them.
Use [stream troubleshooting](../guides/troubleshooting.md#streaming) for diagnosis.

## Compare a listener with the controller

Leave VLC playing. In **Now Playing**, press **Listen here** once to listen
through the control browser too. Its pressed state indicates local listening.
Inspect the listener count again: your browser is an additional listener.

Lower the browser's volume, then toggle **Listen here** off. VLC should keep
playing and the count should drop again. Local listening volume affects this
browser, not the encoded broadcast level. The page continues to control the
station while it is not listening. Avoid **Skip** or other station-changing
controls just to adjust what you hear locally: those affect the shared mix.

If the browser reports that playback was blocked, press **Listen here** again
after that message. If it reports that the stream did not load, inspect the
running station and connection limits before continuing. A browser restriction
does not establish that VLC or the station failed.

## Try it: remove the last listener

In **Settings**, **Stream**, inspect **Keep playing without listeners**. For
this experiment, uncheck it if necessary, then press **Save changes** in the Stream tab. Confirm there
are no enabled relays or broadcast recording keeping Practice active.

Predict: with **Listen here** off, will stopping VLC stop the station's set
immediately? Stop VLC, inspect the listener count until it is zero, then wait
for the idle grace period and read the station's state. It is 30 seconds by
default; if your installation changed `idle_grace_seconds`, wait for that
configured period instead. The [stream settings reference](../settings-reference.md#stream)
describes the setting. Reopen the stream in VLC.

Expected result: after the last listener leaves and the grace period expires,
the set stops, with a waiting-for-a-listener message in the app.
Connecting again starts a new set. An enabled relay, recording, continuous
playback, or another listener changes this result. Explain your observation
using those conditions before attributing it to a fault. Muting VLC would
leave its connection open and would not perform this experiment.

## Stop and restore browser practice

Stop VLC and make sure **Listen here** is not pressed. Leave **Keep playing
without listeners** off and verify the count is zero and the set becomes idle.
This leaves the server running and its stream address reachable; idle is not
an access-control setting.

Return to the first lesson's playback state: select **Browser** under
**Settings**, **Stations**, press **Save changes**, then stop and start
that station using its row and **Stop station** confirmation. Pause browser
playback and confirm its elapsed time stops.
Restore your original startup command if LAN access was only for this lesson.
The Practice music, category, clockwheel and history remain saved.

You can now tell a shared server mix from a device's listening connection.
Continue with [listener requests](listener-requests.md), or use the
[streaming guide](../guides/streaming-and-relays.md) for formats and relays.

AutoDJ and this page were developed with AI assistance.
