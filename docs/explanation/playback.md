# Where playback happens

A browser can make the music, control music made on the server, or listen to a
server stream. These are different roles even when the same page shows the
current song. The selected station's **Playback output** determines which role
applies.

## Browser output

With **Browser**, this browser's audio engine plays the music. The server still
provides the library and song choices. The browser needs permission to start
audio, and its output device and volume affect what you hear on this device.
Closing or suspending that page matters to its playback. Browser playback does
not create a radio stream for another player to open.

This is the default and a useful place to learn basic picking. Features that
need prepared server mixes, including booked voice tracks, require a different
output. See [feature availability](../reference/feature-availability.md) before
building a workflow around a control.

## Server sound card and radio stream

With **Server sound card**, AutoDJ mixes on the server and sends sound to that
computer's audio hardware. The browser acts as a control surface. With
**Radio stream**, AutoDJ makes a shared encoded broadcast for network listeners;
optional server monitoring can play that mix on the server sound card too.
Closing a control page is different from stopping either server output.

All stream listeners receive the same programme. A skip or a booked voice track
changes that programme. Listening in the browser adds a local listening path;
it does not move ownership of the broadcast into the browser. Listener-dependent
stream startup and stopping also depend on the station's stream settings.

## Listening controls and programme controls

A local mute or listening volume does not prove that the broadcast is silent.
Output device selection routes a local listening or preview path; it does not
choose the speakers of every remote listener. Server sound-card volume is also
separate from stream level. Changing processing, transitions, the queue or
playback on the station can affect the programme listeners receive.

A **preview** lets you audition audio. Track info and Voice tracks previews use
**Preview output device** where the browser supports it. Choose headphones to
keep that sound separate from speakers or a microphone. A voice take is private
in the page until you explicitly book it; **Book voice track** uploads and
commits it to the station's song-change workflow. Controls labelled as playing
on the stream or server output should be treated as programme actions.

For procedures, use [core operations](../guides/core-operations.md),
[prepare a station](../guides/prepare-station.md), and
[playback troubleshooting](../guides/troubleshooting.md#playback-and-sound).
The accepted [browser playback](../adr/0003-browser-driven-playback.md) and
[server mix](../adr/0004-server-mix-bus-and-stream-mode.md) decisions explain the
architectural reasons and verification limits. Browser and audio-device support
still depend on the actual platform.
