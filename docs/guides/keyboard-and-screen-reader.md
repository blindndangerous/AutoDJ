# Use the keyboard and a screen reader

The control page has named tabs, headings, lists, buttons, and status messages. Press the **Keyboard shortcuts** button at the bottom of the page, or press `?`, to open the list in a dialog. **Settings**, **Global**, **Browser preferences**, **Enable keyboard shortcuts** turns single-key shortcuts off for this browser. Text fields and dropdowns keep their typing keys. Open dialogs stop playback shortcuts.

## Move around the page

Use `Alt+1` through `Alt+9` to open, in order: **Now Playing**, **Queue & Search**, **Track info**, **History**, **Carts**, **Voice tracks**, **Ads**, **Library tools**, and **Settings**. These work even in text fields. Some tabs are hidden when their feature is unavailable.

Use Tab to move to a tab strip, then Left Arrow and Right Arrow to change tabs; Home and End jump to its ends. Use the same keys in Settings' tab strip. Headings and landmarks let a screen reader jump to the page area you need. Lists and tables have named controls for each item. Grid tables use one Tab stop: arrows move between cells, and Tab leaves the table. A dialog keeps focus inside it until you close it. Press Escape to close the shortcut dialog or a message you can dismiss. A confirmation, such as Clear queue or Restart AutoDJ, opens with focus on Cancel, its last button: Enter on Cancel or Escape changes nothing and returns focus to the button that opened it. Other dialogs also put the button that changes nothing last: Cancel in Browse… and Rename, Keep editing when Track info asks about unsaved changes.

Schedule events, saved Clockwheels and clockwheel steps use the shared collection table. Up Arrow and Down Arrow move between rows; Left Arrow and Right Arrow move between data and action cells. Home and End move to the first or last cell in the row. Ctrl+Home and Ctrl+End move to the first or last row while keeping the same column. Press Enter or Space in an action cell to activate its button. The row's frequent action, such as Edit or Open, is visible; **Options for <row name>** holds its other actions. Upcoming schedule events and recent runs use the same table navigation without action menus.

Press Enter or Space in an Options cell to open its menu. When the Options button itself has focus, Down Arrow opens on the first item and Up Arrow opens on the last. Inside the menu, Up/Down, Home/End and typed letters move between actions; Enter activates. Escape closes the menu and returns focus to Options. Tab closes the menu and leaves it. Disabled choices remain reachable, have a description of their restriction, and explain it again when activated. Refreshing a table keeps your place; if its focused row disappears, focus moves to a nearby row or a panel control and the page announces the destination.

In clockwheel steps, Alt+Up and Alt+Down move the focused step. Delete removes it without a confirmation; **Undo step change** or Ctrl+Z restores the last step change. Ctrl+Z inside a text field keeps its normal text-editing behavior. Other collection removals ask for confirmation unless the screen offers Undo.

Clockwheel **Create** opens a name dialog. Its new row says **Not saved** until you press **Save clockwheel**. To abandon that wheel, choose **Delete** from its **Options** menu and confirm. Saved-wheel **Duplicate** and **Rename** also open name dialogs. Duplicate asks before discarding unsaved work in the current wheel, naming the wheel that would lose those edits. The panel's **Imported clockwheel name** field is only for PAL import.

Other collection screens are migrating to this pattern. Existing action lists, including intro rules, categories, ads and saved shows, retain their current arrow navigation during that migration. Some nested lists still use ordinary Tab navigation. The cart wall and text-only lists retain their existing controls.

In NVDA, shortcuts work in focus mode. Press NVDA+Space to switch modes. Browse mode intercepts letter keys; press NVDA+F2 and then a shortcut to pass one key to the page. JAWS can pass a key with Insert+3. When a button has focus, Space activates the button. Arrows operate a focused slider, tab strip, or list instead of a page shortcut.

Add/Edit dialogs use the shared editor, including scheduled events and clockwheel steps. Save returns to the saved item's control; in a collection table this is its action cell. Cancel or Escape returns to the opener and asks before discarding changed fields. While a save is pending, wait for its result before closing the dialog. Intro rules, categories and ads also use this editor while their lists await migration.

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
