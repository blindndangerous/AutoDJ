# Enable a public player or embedded player

Enable the selected station's listener pages and check the stream and allowed framing sites.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Player page and embeddable player

A station can have a public page where listeners press Play to hear it in the browser, and
a compact version for another website. Both are off by default. Use **Settings**, **Station settings**, **Broadcasting**
to turn on the **Public player page** or **Embeddable player** and set **Allowed embed sites**.
You can change them while the station is stopped, and saving takes effect immediately.
The settings belong to the selected station and are saved in `station.json`:

```json
{
  "player_page": {
    "enabled": true,
    "embed": true,
    "embed_origins": ["https://example.com"]
  }
}
```

For another station, select it in Settings > Station settings > Stations and change the same controls; its pages are at
`/stations/<slug>/player` and `/stations/<slug>/embed` (the default station's also answer at
`/stations/default/player` and `/stations/default/embed`). A page that is off answers 404. The
settings also load when AutoDJ starts. `GET /api/stations` lists each station's `player_path` and
`embed_path`, or `null` while that page is off.

**The player page** (`/player`) has the station's name, a **Play** button, a **Volume** slider,
the song playing, the next song when **Show the next song publicly** is on (never in DMCA mode),
the songs played most recently (as many as `[nowplaying] history_limit`), and links to the stream,
its playlist file and the status page. Nothing plays until Play is pressed; the button then reads
**Stop**, and a line under it says "Connecting to the stream.", "Playing.", "Stopped." or why the
stream could not be played. Stop closes the connection, so a stopped page does not hold a listener
slot, and Play joins the live stream again. The volume is remembered in that browser. The song
line is the only part a screen reader hears on its own, once per song, as on the status page;
**Pause updates** stops the page following the station. Without JavaScript the browser's own audio
controls play the stream instead. If the stream stalls while playing, the line says
"Buffering." once and "Playing." when the sound is back; if nothing has started 15 seconds after
Play, it stops and says the stream could not be played. Where the browser does not let a page set
the volume (Safari on iPhone and iPad), the slider is replaced by a note that the device's own
volume applies. The page uses the system's light or dark colours, or the Theme chosen in the web
UI in that browser, and cannot be shown inside another site's frame. Like the rest of AutoDJ, both
pages are in English (`lang="en"`); there is no language setting.

**The embeddable player** (`/embed`) has the station's name, the song playing, Play, Volume,
Pause updates and, when the player page is on, a link that opens it in a new tab. Put it on a web
page with an `<iframe>`, giving the frame a title so screen readers can name it:

```html
<iframe src="https://radio.example.net/embed" title="Night Owl Radio player"
        width="100%" height="240" style="border: 0"></iframe>
```

A frame 240 px high fits it on a wide page; give it about 330 px where the frame is as narrow as a
phone, since the controls then wrap onto more lines. Add `?theme=light` or `?theme=dark` to the
address to match the site; without it the player
follows the visitor's system colours. A browser shows the embed only inside pages from the sites
in `embed_origins` (and this server's own pages): list each site as `https://host`, with a port
if it is not the usual one, or `"*"` to let any site show it. Leaving the list empty keeps it to
this server's own pages. The list goes into the page's `Content-Security-Policy:
frame-ancestors`, so browsers enforce it; it does not stop anyone from listening to the stream
itself, which is public anyway.

Both pages use the station's public feed and stream and nothing else: no pairing, no controls
beyond listening, and no file paths, credentials or other private details. A station with a
listener password still asks for it: AutoDJ never puts the password in the page, so the browser
asks the listener, and a browser that will not ask inside another site's frame cannot play the
embed. A station that does not stream (the default station outside stream mode) shows its song
and says it cannot be played there. While a station is stopped, both pages answer 503, say it is
off air, and notice when it is back.
