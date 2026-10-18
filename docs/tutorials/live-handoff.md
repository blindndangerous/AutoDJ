# Hand Practice to a live DJ and back

You will interrupt Practice's automatic rotation with a short microphone
announcement from a DJ source program, then disconnect that source and watch
automation return. You will learn to distinguish a connected source from a
DJ actually on air, and a live set from a paused song waiting to resume.

Start with [Build your first station](first-station.md) and
[Hear Practice on a second device](stream-second-device.md) complete. Select
the default Practice station, running in **Radio stream** output with its
Practice clockwheel active. This is a rehearsal with no audience: only your
own test listener may be connected, and no relays or broadcast recording
should be enabled.

## Before you begin

Install [BUTT](https://www.danielnoethen.de/butt/), a free source program, on the
AutoDJ computer. You need a microphone, FFmpeg available to AutoDJ, and
headphones. This lesson uses one concrete path: BUTT sends MP3 using the
Icecast 2 source protocol to the local AutoDJ server. AutoDJ itself receives
that source; you do not install a separate Icecast server or buy hosting.

Use the operator page on the AutoDJ computer at its loopback address, such as
`http://127.0.0.1:8080`. Use the actual port for your installation. This
direct loopback HTTP connection is accepted by default. This is the worked
path. If your installation requires HTTPS, use the
[access guide](../guides/manage-access.md) to establish that source connection
before following the handoff steps; keep its TLS settings intact.

Keep the source on the AutoDJ computer for this lesson. Other source locations
have different access requirements; use the
[feature reference](../reference/feature-availability.md) and
[live-source security boundary](../../THREAT_MODEL.md#live-dj-source-mounts-and-talkover-48)
for those cases. A pairing code is for browser control, not a DJ password.

You will create one station-local account, `practice-dj`, with display name
`Practice DJ`. Choose a unique password of 12 to 256 characters and keep it
in a password manager. Do not put it in screenshots, URLs or shared examples.
The app never shows saved passwords again, and backups omit them.

Before connecting, locate BUTT's stop control. Pressing it disconnects the
source and returns control to automation; merely muting the microphone is
not a reliable handoff. If anything goes wrong, stop the source first. Keep
headphones on so the broadcast cannot feed back into your microphone.

For AutoDJ controls, use Tab, Enter or Space and arrow keys in tab strips.
The [keyboard guide](../guides/keyboard-and-screen-reader.md) covers its grids.
BUTT is a separate application; its keyboard and assistive-technology behavior
depends on its version and operating system.

## Establish the automated before-state

1. Open **Now Playing** and press **Listen here** if you are not already using
   your own external test listener. Keep its volume comfortable in headphones.
2. Note the current song title and let part of it play. Confirm the elapsed
   time advances.
3. Check **Settings**, **Song picking** for Clockwheel pick mode, and
   **Library tools**, **Clockwheels** for the active Practice wheel.
4. In **Queue & Search**, clear any practice queue and confirm the action.
5. Open **Settings**, **Live DJ**.

Check that **Connect a DJ program** and **DJ accounts** are available. If the
tab is absent or says live input is unavailable, confirm Radio stream is the
running output, rather than just a saved selection awaiting restart. Follow
the stream lesson's restart step on this unused server before continuing.

Record the title you will interrupt. The later return may choose any eligible
Practice song; do not depend on a specific title or exact handoff duration.

## Create the rehearsal account

1. Under **DJ accounts**, press **Add DJ**.
2. Enter `practice-dj` in **Username** and `Practice DJ` in **Display name**.
3. Enter your unique password in **Password** and press **Save DJ**.
4. Inspect the DJ table. It should list the account and say **Saved** in its
   password column.
5. Under **Connect a DJ program**, read **Address**, **Port**, **Mount** and
   **Encryption**. Keep these available for the next step.

The username is the source login; the display name is what listeners and
History see. This account can send audio to this station's source mount. It
does not grant browser control or permission to send to a different station.
If validation names a field, correct that field and save again. A new account
needs a password; an empty edit field only preserves an existing password.

## Configure BUTT without connecting yet

In BUTT, open its settings and use **ADD** to add a server named `Practice`.
Choose Icecast as the server type. Copy AutoDJ's **Address** and **Port** into
the corresponding fields; use its **Mount** for the mountpoint and
`practice-dj` as the user. Enter the saved DJ password. Set **Use SSL/TLS**
to match AutoDJ's **Encryption**: off for this direct loopback HTTP example,
on for HTTPS. Add the server and select it.

In BUTT's audio settings, select your microphone as the input and MP3 at
128 kbps as the streaming codec and bitrate. Save the configuration. Before
connecting, speak and check its input meter. BUTT transmits input audio;
selecting a music file is not its source workflow. Its play control connects
and its stop control disconnects. See the
[BUTT manual](https://www.danielnoethen.de/butt/release/1.47.0/butt-1.47.0_manual.html)
for version-specific controls.

Keep the configuration private because it contains credentials. Leave any
automatic connection or recording features off. Confirm the microphone is
selected rather than a device capturing headphone playback.

## Take over, then hand back

Press BUTT's play control and say, “This is the Practice DJ handoff test.”
Continue a short spoken announcement while watching AutoDJ's **Now Playing**.

When decoded sound arrives, automation fades out and the app shows a line
such as **Live: Practice DJ since ...**. The display name and start time are
the text check alongside hearing the announcement through your test stream.
BUTT reporting a connection alone does not establish that audible input has
reached AutoDJ. A silent source can wait without taking the station live.

After about 20 seconds, press BUTT's stop control. Confirm it is no longer
streaming and not retrying a connection. Watch the Live line disappear,
automation return, and an eligible song begin. Inspect **History** for a
Practice DJ live session and the returning music. A live session has its own
entry rather than being recorded as a library song.

The interrupted song does not resume from its old position: AutoDJ returns
with a new automated song. A listener's audio can lag the page because of
buffering. Allow that delay before deciding the return failed.

## Recover a failed source connection

If BUTT connects but no Live line appears, stop it. Select the actual
microphone, confirm input while speaking, and reconnect once. Silence alone
does not demonstrate a working handoff; a source that stays silent on air can
also be disconnected automatically.

If the login fails, stop retries and compare the user, mount and port with
this station's settings. In AutoDJ, use **Edit DJ practice-dj** to set a new
known password, then enter it in BUTT. Failed attempts are rate limited, so
wait for a reported lockout rather than guessing repeatedly. If you used a
LAN address over HTTP, return to the direct loopback address on this computer
or use the existing HTTPS path with matching TLS settings. Do not disable
access controls to make the source connect.

## Try it: disconnect instead of pausing a song

Predict whether a second brief live segment will pause the automated song
and resume it at the same elapsed time, or produce another handoff to a new
song. Note the current title and elapsed time. Connect, speak briefly until
the Live line appears, then stop BUTT and inspect the returning title,
elapsed time and History.

Expected result: the source takes the station live, then disconnecting
returns to a newly started automated song. With a small library, the title
alone is insufficient evidence because a title can recur; use elapsed time
and History too. Explain the result as a change of playout owner rather than
a browser pause operation.

## Stop and remove the rehearsal credential

Stop BUTT and verify the Live line is gone. In **Settings**, **Live DJ**, use
**Remove DJ practice-dj**, then confirm **Remove DJ**. Check that the account
is absent. Removal disconnects that DJ if still connected and prevents this
saved credential from taking over again.

Delete the rehearsal server entry from BUTT and save its configuration.
Disconnect your test listener and turn **Listen here** off. With continuous
playback, relays and recording off, verify the listener count reaches zero
and the set idles after its grace period. For Browser practice, follow the
[stream cleanup](stream-second-device.md#stop-and-restore-browser-practice).
Your Practice rotation and History remain saved.

You have rehearsed both sides of a live handoff. The
[going-live guide](../guides/going-live.md) covers Talk and announcer breaks.

AutoDJ and this page were developed with AI assistance.
