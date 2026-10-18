# Run Practice and a second station

You will create a second station named Practice Two, give it its own rotation,
and compare its queue and history with Practice. You will learn what a station
owns, what the server shares, and why switching the control page does not stop
another station's stream.

Start with [Build your first station](first-station.md) complete. The default
Practice station should have its Practice category, one-step Practice
clockwheel, Clockwheel pick mode, and Browser output. Pause playback and use a
practice server with no audience or other live work. If you left Practice in
stream mode, first use the
[stream lesson's restore steps](stream-second-device.md#stop-and-restore-browser-practice).

## Before you begin

Practice means your default station from the first lesson; its displayed name
may still be AutoDJ. Note that name before starting so you can select it later.

You need FFmpeg for Practice Two's Radio stream output, but no second device,
LAN exposure, relay or paid service. You will listen from the same browser at
the existing AutoDJ address. A running stream station consumes extra CPU and
memory even while idle; you will stop it after the exercise. This is a short
workflow check, not a capacity test for your computer.

Use the station ID `practice-two`. Open **Settings**, **Stations** and confirm
that neither this ID nor the name Practice Two already exists. If either does,
stop here and inspect that existing station before choosing a different
practice exercise. Do not overwrite it, remove its folder, or stop it just to
reuse the example. This lesson creates one new station and touches only your
two practice stations.

You will save a new station definition, a new category and clockwheel, and a
few queue and history entries. Creating the station leaves it stopped. Music
folders are shared: do not import the same folder again or remove it from
Library tools as cleanup. Categories and clockwheels belong to each station,
even when they refer to the same shared songs.

Use Tab and Enter or Space for controls. The **Station** button opens a menu:
use arrow keys to find a station and Enter or Space to choose it. Confirm the
button and page heading name the intended station before changing its content.
The [keyboard guide](../guides/keyboard-and-screen-reader.md) covers grid navigation.

## Leave a marker in Practice's queue

1. In **Now Playing**, verify Browser playback is paused and elapsed time is
   not advancing.
2. Open **Queue & Search**. Clear any old test queue with **Clear queue** and
   its confirmation.
3. Search for a recognizable song in your practice folder and press its
   **Add to queue** button once.
4. Note its title and the current History entries. Leave playback paused.

This queued title is your comparison marker. It is saved station state, not
an instruction for every station on the server. If the queue is not empty
apart from this song, remove the additional test entries before continuing.
Do not press **Play now**: that would begin the song rather than leave the
marker waiting for your later comparison.

## Create Practice Two while it is off air

1. Open **Settings**, **Stations**, and find **New station**.
2. Leave **Start from** set to **AutoDJ defaults**.
3. Enter `Practice Two` in **Name** and `practice-two` in **Station ID**.
4. Leave **Start with the server** unchecked.
5. Press **Create station**.
6. Read the creation result. It should say Practice Two was created off air.
   Press **Switch to Practice Two**.

The switch button takes the new station's name after creation. Confirm the
page's **Station** button names Practice Two, then inspect **Stations on this
server**. Practice Two should say **Off air**, with a **Start** button. Creation
has not started another player or stopped Practice.

If creation reports an existing ID or folder, inspect the error and stop this
creation attempt. A folder can remain from an earlier station even if you do
not see its name. Do not delete it to force success. Resolve the ownership of
that existing data before trying a new station.

Starting from defaults avoids copying Practice's saved picking settings without
their referenced content. The copy workflow is useful later, but is not needed
to demonstrate independent stations.

## Build the second rotation from the shared music

Keep Practice Two selected and stopped. In **Library tools**, expand
**Categories**, press **Add category**, and build its pool:

1. Enter `Practice Two` in **Name** and select **Matching conditions** under
   **Songs come from**.
2. In **Condition 1**, choose **Folder** as **Condition type**.
3. In **Library folder**, select the same practice folder used by Practice.
   Leave **Subfolder** empty, check **Include subfolders**, and keep **Operator**
   set to **is**.
4. Leave day, time and date restrictions unset. Press **Check**, confirm a
   nonzero matching count, then press **Save category**.

The existing folder and songs are visible without another import. The new
category is Practice Two's own definition. If its count is zero, check the
folder choice and completed analysis; do not add another copy of the folder.

Now expand **Clockwheels** and create this station's rotation:

1. Enter `Practice Two` in **Name** and press **Create**.
2. Press **New step**, then choose **Music matching conditions** in **Step type**.
3. In **Condition 1**, choose **Category** as **Condition type**, set
   **Music category** to **Practice Two**, and keep **Operator** as **is**.
4. Set **Choose** to **Least recently played**, uncheck **Follow the repeat rules**,
   leave day and hour restrictions unset, and press **Add step**.
5. Clear **Restart at these minutes past each hour**. Press **Save clockwheel**,
   then **Activate**.
6. In **Settings**, **Song picking**, choose **Clockwheel** in **Pick mode**.
   It saves on change.

Check the saved one-step wheel is active and the pick mode persists. You have
edited a stopped station's saved content; a running player is not required.
The small pool's disabled repeat rules are for practice, not a suggested
production rotation. Saving or activating here must not replace Practice's wheel.

## Start and compare the stations

1. In **Settings**, **Stations**, check **Playback output** is **Radio stream**
   for Practice Two. Leave **Also play the radio stream on the server sound card**
   unchecked. Press **Save station options** if you changed either control.
2. In **Stations on this server**, press **Start Practice Two**. Its visible
   button text is **Start** in the Practice Two row.
3. Open **Now Playing** and press **Listen here**. Allow the stream to prepare.
4. Check **Settings**, **Stream**, **Stream address** ends in `/practice-two.mp3`.
5. Open **Queue & Search**. Practice's queued marker should be absent.
6. Add a different practice song with **Add to queue**. Note its title, let it
   play, and inspect this station's **History**.

Your local stream connection starts a set when continuous playback is off.
The stream address and station name identify which output you hear; the queue
and History identify which state you are inspecting. In the station table,
Practice's paused song and Practice Two's active song can differ.

If **Listen here** reports a stream failure, inspect whether Practice Two is
running and FFmpeg is available. Recheck its active category, wheel and pick
mode if there are no playable songs. If Practice's marker appears, confirm
the Station button: you may still control Practice rather than Practice Two.
Switch to the intended station before touching the queue.

## Try it: switch the page, then stop the station

While Practice Two plays, predict what switching the control page to Practice
will do to each station's queue. Use **Station** to select Practice and inspect
its queue, History and category/clockwheel names. Then select Practice Two again.
If local listening changed during the switch, press **Listen here** again.

Expected result: Practice still has its queued marker and Practice definitions;
Practice Two has its own queue, history and Practice Two definitions. Switching
chooses what the page controls. It does not stop a running server stream;
another connected network listener would keep hearing Practice Two. This
lesson checks state separation without connecting an audience.

Now predict what pressing **Stop Practice Two** will do. In **Settings**,
**Stations**, use that row's **Stop** button, read the warning, and confirm
**Stop station**. It disconnects that station's listeners and takes it off air.
Check its row says **Off air** and Practice retains its own state. Explain why
switching the page and stopping a runtime have different consequences.

## Leave the new station safely stopped

Ensure Practice Two's **Start with the server** remains unchecked. This stops
it returning automatically on the next server start. Its saved settings,
category, wheel and history remain available to edit or start later; the page
does not offer station deletion. Do not remove shared music to retire it.

Switch to Practice, remove your queued marker using **Remove**,
and leave Browser playback paused. Confirm its Practice wheel is still active.
You have kept the original lesson's setup and a separate stopped station.

For daily operations and copying content, use
[Run several stations](../guides/several-stations.md). The lesson demonstrated
ownership; it did not establish how many concurrent streams your hardware can run.

For background, read [how stations works](../explanation/stations.md).
Before using a practice setup regularly, follow
[Prepare a station](../guides/prepare-station.md).

AutoDJ and this page were developed with AI assistance.
