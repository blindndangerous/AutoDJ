# Use the keyboard and a screen reader

The control page has named tabs, headings, lists, buttons, and status messages. Press the **Keyboard shortcuts** button at the bottom of the page, or press `?`, to open the list in a dialog. **Settings**, **Global**, **Browser preferences**, **Enable keyboard shortcuts** turns single-key shortcuts off for this browser. Text fields and dropdowns keep their typing keys. Open dialogs stop playback shortcuts.

## Move around the page

Use `Alt+1` through `Alt+9` to open, in order: **Now Playing**, **Queue & Search**, **Track info**, **History**, **Carts**, **Voice tracks**, **Ads**, **Library tools**, and **Settings**. These work even in text fields. Some tabs are hidden when their feature is unavailable.

Tab enters the main section tab strip on its selected tab, its only Tab stop. Use Left Arrow and Right Arrow to change tabs; Home and End jump to its ends. Settings uses the same keys within each tab group. Each Settings group stays reachable with Tab; a group without the selected panel starts on its first visible tab. Headings and landmarks let a screen reader jump to the page area you need. Lists and tables have named controls for each item. Grid tables use one Tab stop: arrows move between cells, and Tab leaves the table. A dialog keeps focus inside it until you close it. Press Escape to close the shortcut dialog or a message you can dismiss. A confirmation, such as Clear queue or Restart AutoDJ, opens with focus on Cancel, its last button: Enter on Cancel or Escape changes nothing and returns focus to the opener (its grid cell for a table row action). Other dialogs also put the button that changes nothing last: Cancel in Browse… and Rename, Keep editing when Track info asks about unsaved changes.

Schedule events, saved Clockwheels, clockwheel steps, Shows, show songs and search results, prepared occurrences and takes, the queue, listener requests, booked voice tracks, categories, intro rules, matched intros, liner files, ad spots and breaks, profiles and browser access use the shared collection table. Up Arrow and Down Arrow move between rows; Left Arrow and Right Arrow move between cells. NVDA does not say table headings in focus mode, so a cell you move to is named by its text, then its column heading when you changed column, then the row's name when you changed row. A cell holding a button or field is named by that control instead. Each table is named by its caption, which screen readers say on entering it. Home and End move to the first or last cell in the row. Ctrl+Home and Ctrl+End move to the first or last row while keeping the same column. Each row with actions ends in one **Actions** column holding **Options for <row name>**. Its menu lists every action for that row, the most frequent one, such as Edit or Open, first. Many cells are one sentence of details, so a row has few columns to cross. Upcoming schedule events, recent runs and recent request decisions use the same table navigation without an Actions column. The ad delivery report's two tables are plain tables, each in a region Tab reaches; read them with the screen reader's table keys (Ctrl+Alt+arrows in NVDA browse mode).

Press Enter or Space in an Actions cell to open its menu. When the Options button itself has focus, Down Arrow opens on the first item and Up Arrow opens on the last. From any cell of a row, Shift+F10 or the Applications (Menu) key opens the same menu for that row, and right-clicking a row opens the menu of the row you clicked. Rows without actions keep the browser's own context menu. Inside the menu, Up/Down, Home/End and typed letters move between actions; Enter activates. Escape closes the menu and returns focus to the cell you opened it from: the Actions cell, the focused cell for the keyboard, or the clicked cell. So does an action that does not move focus elsewhere. Cancelling a menu action's confirmation or closing its editor returns to the Actions cell. Tab closes the menu and leaves it. Disabled choices remain reachable, have a description of their restriction, and explain it again when activated. A checkbox cell, such as a schedule event's **Enabled** cell, takes focus on the checkbox itself. Its name is the action and the row, such as **Enable Morning show**. Press Space to turn the event on or off in place; focus stays on the checkbox and the result is announced. While the change saves, a second Space does nothing. If the change is refused, the checkbox returns to its previous state and the reason is announced. An ended event that is off has no checkbox; its cell says "No, ended, no runs left". One that is still on keeps its checkbox, described as "Ended, no runs left", so you can turn it off. Refreshing a table keeps your place; if its focused row disappears, focus moves to a nearby row or a panel control and the page announces the destination.

In clockwheel steps, Alt+Up and Alt+Down move the focused step. Delete removes it without a confirmation; **Undo step change** or Ctrl+Z restores the last step change. Ctrl+Z inside a text field keeps its normal text-editing behavior. Other collection removals ask for confirmation unless the screen offers Undo.

Clockwheel **New clockwheel name** supplies the name for **Create** and **Import .PAL**. Opening or saving a wheel leaves this field as you typed it, including an empty value. Create opens a name dialog with that value, which you can change. Its new row says **Not saved** until you press **Save clockwheel**. To abandon that wheel, choose **Delete** from its **Options** menu and confirm. Saved-wheel **Duplicate** and **Rename** also open name dialogs and leave the new-name field alone. Duplicate asks before discarding unsaved work in the current wheel, naming the wheel that would lose those edits. The steps table has two columns: **Step**, the numbered sentence for the step, such as "3. music from Rock, least recently played, only on Friday from 08:00 until 09:00", and **Actions**. The sentence includes the length or target, how it picks and its rules and hours when they apply. Saved clockwheels show **Name**, **Active** and **Actions**; the open wheel's name is marked current.

In Shows, a show's Options menu has **Open list**, which opens the song table, **Edit**, which opens a metadata dialog, and confirmed **Delete**. A song's Options menu has **Track info**, **Move up**, **Move down** and confirmed **Remove**; Alt+Up and Alt+Down also move the focused song. A search result's menu has **Add** and **Add to edition**. Prepared occurrences use Options for ordering and confirmed removal. A take's menu has **Listen**, **Re-record**, **Review**, **Approve**, **Reattach** and confirmed **Remove**. Reattach opens a Position dialog and returns to that take's Actions cell.

A queue row's Options menu has **Track info**, **Move to top**, **Move up**, **Move down** and confirmed **Remove**. A listener request's menu has **Approve**, **Play next** and confirmed **Turn down**. Booked voice tracks use Options for **Cancel**, followed by **Cancel this booking?** confirmation. Restrictions remain readable on unavailable actions.

The Options menus of categories, intro rules, ad spots and ad breaks start with **Edit** and contain **Delete**; ad breaks also offer **Run now**. A matched intro's menu has **Listen**, which becomes **Stop listening** while it plays, and **Remove**. A liner file's menu has **Play dates**, **Track info** and **Delete**. A profile's menu has **Activate**, **Reset** and **Delete**. A browser access row's menu has **Rename**, then native **Revoke access** or SSO **Forget**. Some nested lists use ordinary Tab navigation. The cart wall and text-only lists retain their existing controls.

In NVDA, shortcuts work in focus mode. Press NVDA+Space to switch modes. Browse mode intercepts letter keys; press NVDA+F2 and then a shortcut to pass one key to the page. JAWS can pass a key with Insert+3. When a button has focus, Space activates the button. Arrows operate a focused slider, tab strip, or list instead of a page shortcut.

Add/Edit dialogs use the shared editor, including scheduled events, clockwheel steps, show metadata and prepared-take reattachment. Opening focuses the requested first field directly. The opening announcement gives the dialog title and short help; use Tab or reading commands to explore its fields. Save returns to the saved item's control; in a collection table this is its action cell. Cancel or Escape returns to the opener's cell for a table action, or the opening control elsewhere, and asks before discarding changed fields. While a save is pending, wait for its result before closing the dialog. Intro rules, categories, ads, Add intro, liner Play dates and browser Rename also use this editor.

In NVDA browse mode, Ctrl+Home followed by Tab also enters the main strip on
its selected tab. Settings' Profile, Station and Global groups follow the same
entry rule; a group without a selected tab starts on its first visible tab.
Entering a group does not change the selected panel. Use its arrows, Home or
End to change selection, or activate a specific tab with browse-mode Enter.
Pointer clicks still select the clicked tab.

NVDA 2026.2 can repeat an editor's title, short help and form landmark within
one speech event, although the page focuses its first field only once. This
known behavior also occurs on the baseline; see the
[NVDA testing notes](../accessibility-testing.md#other-dialogs-with-nvda).

## Playback and navigation shortcuts

- `Space` or `k`: play or pause. In stream mode, start or stop listening in this browser.
- `n`: skip to the next track. `s` or `S`: shuffle to a random track. `m` or `M`: mute or unmute.
- Up Arrow and Down Arrow: raise or lower volume by 5 percent.
- Comma and Period: seek back or forward one measure from BPM, or five seconds if BPM is unknown. Seeking is unavailable in stream mode.
- `?`: open or close the shortcut dialog. Escape closes it.
- `t`: open **Track info** for the current track, with the same unsaved-change prompt as its Now Playing button. If nothing is playing, the page says “No track currently playing”.
- `Alt+1` to `Alt+9`: open the tab at that position.
- `[` and `]`: announce previous or next song landmark. In **Track info**, they move the cue preview. On other tabs, they seek the song on air only if **Bracket keys seek the song on air** is on; that setting is off by default and seeking never works in stream mode.
- `Shift+U`: speak the upcoming landmark on air.
- `c`: focus the station switcher when several stations exist. `Shift+C`: speak the selected station and its state. `Shift+[` and `Shift+]`: switch stations.
- `Alt+M`: talk over music while held, when live talk is available. Releasing the keys stops talk.
- `Alt+R`: start or stop recording while **Voice tracks** is open.

Space and the volume arrows act as page shortcuts only on **Now Playing**. On other tabs, they scroll. Playback letters and punctuation work on other tabs when focus is not inside a text field or dropdown.

## Speak station status

- `Shift+T`: current artist and title.
- `Shift+N`: next artist, title, and BPM if known.
- `Shift+R`: time remaining. `Shift+E`: elapsed and total time.
- `Shift+B`: current BPM. `Shift+K`: current musical key.
- `Shift+L`: current lyric line. `Shift+V`: volume and mute state.
- `Shift+Q`: queue length and first song. `Shift+J`: library job status.
- `Shift+H`: how the hour lands, from **Hour preview**.

The **Carts** wall has its own keys while it has focus: number keys 1 through 9 and 0, Minus, and Equals play carts 1 through 12. Page Up and Page Down change cart pages. These keys do not act as global playback shortcuts.

## Set announcements

**Settings**, **Global**, **Browser preferences**, **Announce track changes** offers **On every tab**, **Only on Now Playing**, and **Off**. The default is **Only on Now Playing**. Choose **Off** if song changes interrupt other work; use the status shortcuts when you want an update. Short success and error messages are spoken once through the page area that owns them. Their visible message can be dismissed without moving focus away from your work.

**Enable keyboard shortcuts** and announcement choices are stored in this browser. **Bracket keys seek the song on air** is also per browser. Buttons, tab strips, sliders, and dialogs still have their normal keyboard controls when shortcuts are off.

AutoDJ and this page were developed with AI assistance.
