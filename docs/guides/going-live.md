# Go live and take requests

Use a live DJ source, listener requests, announcer breaks, or voice tracks when a station needs a person on air. Select the right station with the **Station** button first. Live DJ and break controls appear only when that station can use them.

## Connect a live DJ

Use radio stream output and open **Settings**, **Live DJ**. Under **DJ accounts**, press **Add DJ**. Enter **Username**, **Display name**, and **Password**, then press **Save DJ**. **Display name** is what listeners and **History** show while that DJ is live. Passwords are write-only, so keep a safe copy.

Under **Connect a DJ program**, copy **Source address**. Set a source program such as BUTT or Mixxx to Icecast 2, then use the **Address**, **Port**, **Mount**, and DJ account credentials shown on the page. The page lists accepted audio formats and whether the connection is encrypted. When the DJ sends audio, AutoDJ fades its music out. When the source disconnects, AutoDJ starts a new song and resumes automatic playout. Test the handoff before announcing a show.

Use HTTPS for a remote DJ connection; the server refuses DJ passwords over plain HTTP except on loopback or with the explicit trusted-LAN exception. Browser **Talk** needs a secure context too. Follow [network and TLS setup](../operations.md#local-network-access) before connecting from another device.

For brief speech over music from the control page, use **Talk** on **Now Playing**. **Duck amount (dB)** and **Return time (seconds)** are in **Settings**, **Live DJ**. The button can latch briefly; holding Alt+M talks only while the keys are held.

## Take listener requests

Open **Queue & Search**, **Listener requests**, **Request settings**. Turn on **Take listener requests**, choose **Approval**, then press **Save request settings**. **I approve each one** keeps requests waiting for an operator. **Automatic: a request that keeps the rules joins the queue at once** accepts eligible requests without that step.

Share this station's request page, `/requests` for the default station or `/stations/nightowl/requests` for a station with ID `nightowl`. A listener searches and sends a request there without pairing a browser. Read **Waiting requests** and approve or decline each one when manual approval is on. Repeat rules, request limits, show placement, and DMCA mode can keep a requested song from playing when the listener expects. The page reports an accepted request's place without promising an exact time during a show.

**Add bridge songs before a request that would sound abrupt** can smooth a jump. Set **Most bridge songs** and **Roughness limit**, then save. See [Listener requests](../operations.md#listener-requests) for limits and moderation details.

## Take breaks from an announcer program

An announcer program can submit a recorded break, such as news or weather, for an open song change. This needs **Server sound card** or **Radio stream** output and **Use saved segue points** in **Settings**, **Transitions**. In **Settings**, **Breaks**, turn on **Accept announcer uploads** and choose where a DJ break plays. **Talk up the next song** uses the next intro when possible; **Between the songs** keeps it separate. Set **Shortest intro to talk up (seconds)** and **Music under a break (decibels)** as needed, then press **Save break settings**.

Give the announcer program its own token through **Settings**, **Analysis workers**. Follow the program's instructions to send audio to AutoDJ. AutoDJ queues the break and joins it at a song change. The [announcer breaks reference](../operations.md#announcer-breaks-dj-breaks-news-and-weather) gives API details and expiry rules.

## Record a voice track in the browser

Voice tracks need **Server sound card** or **Radio stream** output and saved-point transitions; external announcer uploads can stay off. Open **Voice tracks** and press **Record**; allow microphone access when the browser asks. Stop recording, use **Listen back** to audition privately, then press **Book voice track** to upload and commit the take to station playback. A local take alone is not booked. Use **Preview output device** in **Settings**, **Sound** if you need headphones separate from the main output. Alt+R starts or stops recording while **Voice tracks** is showing. For microphone access from another device, use HTTPS. Follow [the voice-tracking lesson](../tutorials/voice-tracking.md) for a private practice run, checkpoints and stopping instructions.

AutoDJ and this page were developed with AI assistance.
