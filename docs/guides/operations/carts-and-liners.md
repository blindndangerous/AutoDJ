# Operate carts and set liner timing and play dates

Configure and fire carts, edit liner overlap cues, and restrict cart or liner playback to the intended dates.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Cart wall

The **Carts** tab, after Queue & Search, holds four pages of twelve numbered carts for station
IDs, jingles and sound effects. Each button is named for its cart, for example "Cart 3, Station
ID, 0:07", or "Cart 4, empty". Press a cart to play it; an empty cart opens in the editor.

In the editor, choose the cart, its name, its clip and what happens to the music while it plays:

- Duck lowers the music by the voice liner duck depth.
- Over leaves the music at full volume.
- Replace fades the music out. The song keeps playing silently under the cart and fades back in
  when the cart ends, so it resumes at its later position.

A clip is a voice liner from the library or the liner folder, or an audio file uploaded under
**Uploaded clips**. Uploads use the voice liner extensions and the selected station's
`station.liner_upload_max_mib` limit, must decode as audio, and never replace a clip with the same name. A
clip still on a cart cannot be deleted. **Rename this page** names the page shown.

One cart plays at a time: a new one cuts off the last, and **Stop carts** fades it out. Each
open paired page says "Cart 3, Station ID, playing" when a cart starts and "... ended" or
"... stopped" when it finishes, from any tab.

With focus on the cart wall, keys 1 to 9, 0, minus and equals play carts 1 to 12 on the page
shown, and Page Up and Page Down change the page. The keys do nothing in text fields and
dropdowns, and the **Enable keyboard shortcuts** checkbox (Settings, Display and keys) turns
them off. With NVDA, switch to focus mode first (NVDA+Space).

Carts play through the voice liner path. In server audio and stream mode the server mixes them
in, and they need playback running: a cart is refused while the stream is idle or paused. In
browser playback the page that plays the cart plays it, as the voice liner Test button does.

The server keeps the wall in `carts.json` in the station's folder and the uploads in its
`carts/` folder. The file is versioned; one that cannot be read is moved aside as
`carts.json.bad-<id>` and an empty wall loads. Backups include the wall and the uploads. Saving
a profile records the name of the cart page shown, and applying the profile shows that page; a
profile whose page has been renamed is refused.

A cart can also have play dates; see [Liner and cart play dates](#liner-and-cart-play-dates).

## Liner cue points

Track info shows a liner's cue points as Start, Segue, End and Overlap start.
In server saved-point playback, automatic interval liners use these same cues and handoff
rules as queued liners; no separate talk-up setting is required.

- **Segue** marks the end of the liner's main content. **End** keeps the rest of its tail.
- **Allow start over outro**: unchecked, the liner starts at the outgoing song's Segue. Checked, it
  may start earlier, over that song's Outro.
- **Overlap start** is when the next song may come in, counted from the liner's saved Start. Left
  blank, it uses Segue, or End when Segue is blank. It cannot be later than Segue.
  - **0** lets the next song's intro run under the liner when the timing allows.
  - **A positive value** is the earliest time the next song may come in.
  - **A negative value** starts the liner after the next song begins, as far as that song's intro
    allows.

Every Overlap start respects the next song's intro. A song with no Intro point waits until the
liner's Segue, though the liner's tail may still run over it.

## Liner and cart play dates

A voice liner or a cart can have a **Not before** date, a **Do not play after** date, or both,
such as a promotion that must stop after the event. Both days are included, and the days are
read in the station time zone (`station.timezone in the station definition`). Leave both empty to play the clip
on any day.

- **Voice liners:** in **Settings > Voice liners**, press **Play dates** on a liner.
  Outside its dates, automatic liners (in the browser and on the server), clockwheel liner steps
  and scheduled liner events skip it, and a scheduled event's run says why in Recent runs. The
  liner list says the dates in words, such as "Plays until 26 December 2026", and adds "Skipped
  now: expired after 26 December 2026". **Test** never picks a skipped liner. A cart that plays
  the liner still plays it; give the cart its own dates.
- **Carts:** set the dates in the cart editor. Outside them, pressing the cart, its key or a
  scheduled Play cart event plays nothing and says why, such as "Cart 3, Promo was skipped: it
  is expired after 26 December 2026", and the cart button ends with "expired" or "not playable
  yet".

A page left open past midnight follows the station's day: the liner list adds or drops "Skipped
now", and the cart buttons and the editor's cart list add or drop "expired" and "not playable
yet", in place. Only the words change, so focus stays where it is and nothing is announced. A
refused date in the cart editor is shown under the editor and linked to its field, whether the
page or the server refused it.

Liner dates are kept in `liner_dates.json` in the station's folder, by the ID Browse voice
liners uses (the library ID of a registered liner, or the file name of one in the liner folder),
and are included in backups. Deleting a liner forgets its dates. So does a library scan for a
liner deleted outside AutoDJ: a registered liner whose file the scan found gone from a folder
that is there, or a liner folder file that is no longer in the folder. A liner on a share that is
not mounted keeps its dates, and so does every liner after a scan that could not read one of its
folders, or when the liner folder cannot be listed: such a scan marks nothing gone and forgets
nothing. A file listed but not reachable for a moment is not gone either. Dates kept by a library
ID that is no longer in the library are kept too. A liner folder that a scan finds empty forgets
its liners' dates only when the next scan finds it empty as well, and names are compared as the
file system compares them (without case on Windows). When `liner_dates.json` cannot be read just
now, or its folder is away (a dropped share says "not found" as a missing file does), the dates
last read are used, a save is refused with a message, and the file is never set aside or written
over; only a file that is not valid dates is set aside. An upload under a free name waits (it
answers that the dates could not be read) until they can be read, so it never takes an old
liner's dates, and deleting a liner says when its dates could not be forgotten yet. A liner uploaded under a name that is not in use starts without
dates, even where an earlier file of that name left some; uploading over an existing liner with
Replace keeps them. Cart dates are kept with the cart in `carts.json`. A liner or cart without
dates is saved as before, so an older AutoDJ still reads both files.

With **Pick in order** (sequential), a liner outside its dates is passed over and the rotation
keeps its place: the liner after it still comes next. Talk-up liners planned ahead pass over it
too, judged by the day they air, and so do clockwheel liner steps, by the day the next song
starts. **Test** on the server refuses a liner outside its dates and says why. Time check voice packs have dates of their own; see [Voice packs](time-checks.md#voice-packs).
