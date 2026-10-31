# Create, edit and import clockwheels

Build and activate a clockwheel, edit its working copy, choose when saved changes apply, or import a supported PAL file.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Clockwheels

A clockwheel is a repeating order of steps that song picking follows when **Pick mode** (Settings, Profile, Song picking) is set
to **Clockwheel**. Keep as many as you like in the **Library tools** tab, **Clockwheels**: create,
duplicate, rename, delete, activate one, or turn the clockwheel off. Only the active one plays.
A saved profile can override the station's selected clockwheel using **Clockwheel**
beside **Pick mode** in **Settings**, **Profile**, **Song picking**. Saving applies that changed
group when the edited profile is still active; saving an inactive profile does not switch the
station back to it. **Activate** in the Profiles list resolves all eligible groups for the selected profile. A schedule
event can also switch to a saved profile by name.

Enter **New clockwheel name**, then press **Create** to open the name dialog.
Opening or saving a wheel leaves **New clockwheel name** as you typed it; it
does not replace it with the open wheel's name. Review or change **Name**, then press **Create**
in the dialog. The new wheel appears in the table marked **Not saved**; add its
steps and press **Save clockwheel** to persist it. To abandon it, open that row's
**Options for \<name\>**, choose **Delete**, and confirm discarding the unsaved wheel.

Each saved row shows **Name**, **Active** and **Steps**. Press its **Open** action
to edit its steps. Use **Options for \<name\>** for **Duplicate**, **Rename**,
**Activate** or **Turn off**, and **Delete**. Duplicate and Rename ask for the new
name in a dialog and leave **New clockwheel name** alone. These actions target their row, even when another wheel is
open. Duplicate opens the copy, so it first asks before discarding unsaved work
in the currently open wheel. Saved-wheel Delete asks for confirmation; step
deletion instead offers Undo.

Step types:

- **Music** or **Voice liner** matching the conditions in the step. Choose least recently played
  or random, and whether to follow the repeat rules. A liner uses its saved overlap points with
  the next music step; two liner steps cannot be next to each other.
- **A specific track**, by its path inside a registered music folder. It follows the repeat rules;
  when they hold it back, the clockwheel moves on to the next step.
- **Switch preset**: the next song is picked and mixed with that preset.
- **Ad break**: books spots at the song change before the next song the clockwheel picks.
  **Most spots** or **Spots in the break** limits the count (1 to 10); **Longest break in
  seconds** is its target length (0 means no limit). Optional **Advertiser** and **Competing
  category** restrict eligible spots. It needs `[ads] enabled = true`, saved segue points and
  server mixing, as described under [Ad breaks](ad-breaks.md#ad-breaks). With no eligible spot, music
  continues and the report records a missed break with the reason. JSON example:
  `{"type":"ad_break","max_spots":3,"max_seconds":60,"advertiser":"","category":""}`.

The step table separates **#**, **Step**, **Kind**, **Length or target**,
**Selection**, and **Rules and hours**. Length or target shows the track path,
preset name, or ad-break spot limit and optional seconds. Music, liner and category
steps say **Not specified** because their lengths are not known here. Selection
shows **Least recently played** or **Random selection** for those steps, and
**Not applicable** for track, preset and ad-break steps.
Each row has **Edit** and **Options for <step name>**;
Options contains **Move up**, **Move down**, **Duplicate** and **Delete**.
Tab enters the table once. Up and Down move between rows in the same column;
Left and Right move between cells. Home and End reach the row's first or last
cell; Ctrl+Home and Ctrl+End reach the first or last row in the same column.
Enter or Space activates an action cell. Within Options, arrows and Home/End
move between choices, typing matches their names, Enter activates, and Escape
returns to the Options cell, where arrows continue table navigation. Cancelling
a menu action's confirmation also returns there. Disabled choices explain their restriction when activated.
Tab leaves the table or closes and leaves its menu. Delete removes the focused
step immediately, like **Options**, **Delete**. Alt+Up and Alt+Down reorder it,
and focus moves with it. See the [keyboard guide](../keyboard-and-screen-reader.md).

**New step** and a step's **Edit** action use the shared modal editor. Cancel or Escape
asks before discarding changed step fields. **Add step** or **Update step** changes
only the working copy; **Save clockwheel** still saves the wheel to disk.

**Undo step change** or **Ctrl+Z** restores the last step you added, edited, moved, duplicated or
deleted. Delete and Undo are unavailable while a step is being edited; update or cancel the edit
first. You can undo several changes in order, until you save or open another clockwheel.
Ctrl+Z in a text field keeps the browser's usual text undo. Undo changes the working copy;
press **Save clockwheel** to keep the result.

These table keys do not replace the playback or spoken-status shortcuts. Step edits are not saved until you press **Save clockwheel**; opening
another clockwheel with unsaved changes asks first. **Restart at these minutes past each hour**
sends the clockwheel back to step 1 at the song change nearest each minute.

When you save edits to the active clockwheel, **Apply saved changes** defaults to **At the next
clock restart**. The current clock keeps playing until its next scheduled restart, and the saved
edit becomes the new step 1 there. If the restart-minute list is empty, the next restart is the
wheel's natural wrap to step 1. The editor announces when a saved edit begins waiting. If AutoDJ
restarts while an edit is waiting, it loads the latest saved definition at step 1. Choose **Immediately, start at step 1**
to use the new steps for the earliest selection that has not already been lined up; the current
track and an already committed handoff stay in place. Choose **Immediately, continue at next step**
to keep the next step number when it still exists in the edited wheel; otherwise playback starts at
step 1. Reordering or deleting steps can shift which step that position refers to. A server restart
loads the latest saved clockwheel definition. API clients that omit `apply_timing` keep the immediate behavior.

After a restart, a streaming station starts with the song the clockwheel had picked to follow the
song that was playing, so it does not stay silent while the next pick is worked out. It does this
only when that song was picked within the last 30 minutes, has not played since, the clockwheel is
unchanged and its restart time has not passed, no show is on the air, the category and step hours
are as they were, and the repeat rules and DMCA limits still allow it. Otherwise the station picks
as usual. The log says which.

**Station name while active** (optional, up to 100 characters) is the station name listeners see
while this clockwheel picks the songs. Leave it empty for the station's normal name. A show on
the air with its own name comes first. Saving a new name applies at once and does not restart
the wheel. See [A station name while a clockwheel or show is on the air](../streaming-and-relays.md#a-station-name-while-a-clockwheel-or-show-is-on-the-air).

For a voice liner step, **Only play if the following track fits before the next reset** is off
by default. Turn it on to leave out the liner when the liner and the selected song would run
past the next reset. The check uses their saved cue points and overlap, through the song's
next handoff. The song still follows the usual clock and repeat rules; skipping the liner does
not count it as played. With no reset times set, this checkbox has no effect.

**Import .PAL** turns a SAM Broadcaster `.PAL` file into a new clockwheel, once; the `.PAL` file
is not used after that. Enter **New clockwheel name**, choose **.PAL file to
import**, then press **Import .PAL**. Only `Dir[...]` and `SubDir[...]` lines with `QueueBottom` are read, and
nothing in the file runs. Each folder must be a registered music or voice liner folder, or a folder
inside one that adds subfolders; the innermost registered folder is used. A folder above every
registered folder (for example `SubDir['C:\Station']` when only `C:\Station\Rock` is registered)
is refused: register the parent folder, or edit that step after the import.

Upgrading from the single clockwheel: the clock saved by earlier versions is not read. The
Clockwheels section and the server log say so. Import the `.PAL` file again under a name, then
open its **Options for \<name\>** menu and choose **Activate**.


## Related tasks and contracts

- [Clockwheel conditions, category rules and storage](../../reference/operations/clock-and-category-rules.md)
- [Build rotation categories and repair beets paths](categories.md)
- [Schedule timed station events](scheduler.md)
