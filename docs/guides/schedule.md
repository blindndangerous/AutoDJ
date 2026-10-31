# Plan a schedule

Use **Settings**, **Station**, **Schedule** to change a station at set times. Events use the selected station's time zone, shown at the top of the panel. Set it in **Settings**, **Station**, **Stations**, under **Time for the selected station**, then press **Save changes** in **Stations**. The change takes effect when that station next starts. In **Stations on this server**, use its **Stop**, confirm **Stop station**, then **Start**. This applies to the default station too and disconnects its listeners.

## Add an event

1. Open **Settings**, **Station**, **Schedule** and press **Add event**.
2. Enter a **Name** and choose an **Action**. The form then shows fields for that action.
3. Choose **Repeat**: **Once**, **Every day**, **Weekdays, Monday to Friday**, **On chosen days**, or **Every few minutes**. Enter the date, time, days, or interval that appears.
4. For a repeating event, use **Date range** when it should run only during a season. An interval event can also have **Active from** and **Active until** times.
5. Choose **Timing**. **Soft** waits for a song change. **Hard** acts at the event time and may fade out the current song. **Wait up to seconds** lets a hard event wait briefly for a song that is nearly finished.
6. Save the event. Check **Next events** for its next run. In the event's row, open **Options for <event name>** and choose **Run now** to perform the real action immediately. Use it only when you intend that on the selected station, then read **Recent runs** for the result. Use a private practice station for rehearsal.

The **Events** table shows each event's name, timing, action, next run and enabled
state. **Edit** opens its dialog. **Options** also contains **Enable** or
**Disable**, and **Delete**; Delete asks for confirmation. **Next events** and
**Recent runs** are read-only tables. All three use the shared
[table keyboard controls](keyboard-and-screen-reader.md).

An event due while AutoDJ is stopped does not run later. A one-time event missed during an outage appears as missed. A date range and an overnight interval window use the station's local date and time.

## Prepare and air a show

In **Library tools**, open **Shows**, press **Add show**, and enter a **Name**. Choose **A category** or **A clockwheel** under **Songs come from**, set **Target length (minutes)**, and press **Save show**. Open its list and press **Generate list**. Review the order; use **Move up**, **Move down**, **Remove**, or **Add songs** to change it. Generating again replaces the prepared list.

To schedule it, add an event whose **Action** is **Play a show**. Select the show. Soft timing puts its songs in the queue after the current song; hard timing starts its first song at the chosen time. **Start after this song** in the show list starts it now. **Stop show** removes its waiting songs. After its last song, normal picking resumes. Each station keeps its own shows.

Use [Programme review](../operations.md#programme-review-and-export) to listen
privately to a shortened mix and export it before airing. Select **Saved show**;
review generation does not start or schedule the show.

For production-specific recordings, expand **Prepared editions** inside the show
list. Record or import speech while stopped, review each pending take in context,
and approve explicitly before airing. Schedule the production through **Play a
show**. See [Prepared show editions](operations/prepared-editions.md) for
recording, approval, re-air warnings and explicit interrupted-edition resumption.

## Schedule music, liners, and announcements

An event can **Change song picking** to a saved clockwheel, switch a profile or preset, turn discovery on or off, or play a voice liner or station ID. Applying a profile resolves its saved overrides over the station's latest saved defaults; an open profile draft remains attached to the profile selected for editing. For a seasonal clockwheel, create one event to select it and a second event to return to the usual clockwheel. See [Build your sound](build-your-sound.md) for clockwheels and voice liners.

To play a spoken announcement, upload it as a voice liner in **Settings**, **Voice liners**. Add an event that plays that liner. Liner and cart actions require server audio or radio stream output. A hard liner event may fade out the song; a soft one waits for the next change. Choose **Run now** from the event's **Options** menu and listen before leaving the event enabled.

## Schedule ads

In **Ads**, add spots with their advertiser, campaign, play dates, and limits. Add an ad break with **Minutes past the hour**, or leave those minutes empty and trigger the break with a **Schedule** event. An ad break joins the next song change; it does not cut a song. Review the **Ad report** to see what aired and what was missed. The [operations reference](../operations.md#ad-breaks) explains spot eligibility and reports.

## Schedule time checks

Open **Settings**, **Time checks**. Turn on **Enable time checks**, choose **Hour format**, and select recorded **Voice packs** or configure text to speech. **When to play** can speak after the song crossing each hour. For specific times, add a **Time check** event in **Schedule**. Time checks need server audio or radio stream output. **Play a time check now** tests the voice even when the automatic rule is off. See [Spoken time checks](../operations.md#spoken-time-checks) for voice pack setup.

AutoDJ and this page were developed with AI assistance.
