# Take and moderate listener requests

Enable requests for the selected station, choose approval settings, and moderate the waiting list.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Listener requests

A station can take song requests from its listeners on a public page, `/requests` (and
`/stations/<slug>/requests` for every station). It is off until you turn it on, for each station
on its own: tick **Take listener requests** in the **Listener requests** card in Queue & Search
and choose **Save request settings**. The page opens at once, with no restart. Turning it off
closes the page at once too. While it is off the page answers 404 with a page that says
"Requests are closed", and a request sent from a page opened earlier gets the same answer.
Requests already waiting are kept, and come back when you turn requests on again (each still
expires three hours after it was sent).

**Approval** in the same card chooses **I approve each one** (`approval = "manual"`) or
**Automatic** (`"auto"`): a request that keeps the rules joins the queue at once.

Until you save **Take listener requests** and **Approval** with this card, the values in that
station's `station.json` are in charge; a change there takes effect at the next start. Once you
save the card, the station's `web_state.json` keeps both values, and they take the place of the
definition values at every start from then on. When a saved value differs, the log says so once at
start. The other keys are read from the station definition when AutoDJ starts:

```json
{
  "requests": {
    "enabled": true,
    "approval": "manual",
    "per_listener": 3,
    "per_listener_minutes": 60,
    "daily_limit": 50,
    "max_pending": 20,
    "window_minutes": 30,
    "bridges_enabled": false,
    "max_bridges": 3,
    "bridge_cost_threshold": 2.0,
    "no_smooth_spot": "best",
    "blocked_artists": ["Night Owl Jingles"],
    "blocked_folders": ["Christmas"],
    "ipv6_prefix": 56
  }
}
```

For another station, select it in Settings > Station settings > Stations and change the same settings.

**What a listener does.** The page has a search box for title, artist or album. The songs it
finds are links: at most 20, and never from a blocked folder or by a blocked artist. Choosing one
opens a confirmation page with an optional "Your name" field (up to 40 characters, which you might
say on air) and **Send request**. The answer is a page whose title and first heading say what
happened, so a screen reader hears it as the page loads. It is one of:

- **Request received**: waiting for you.
- **Request accepted**: queued.
- **Not right now**: it would break a repeat rule or a DMCA limit. The page does not say which
  play caused it.
- **Already on the way**.
- **Too many requests**: that address's limit, with how many it may make.
- **No more requests today**.
- **This form has expired**: the form is good for two hours, for the song it was opened for.
- **The station is busy**: too many searches or requests at once, across all stations. Try again
  in a few seconds.

The page needs no JavaScript and works without pairing. A listener is only ever told roughly when
a song may play ("within the next half hour", "within the next hour or so"), never a set time.
While a show is on the air, or due to start within `window_minutes`, a request is placed after
the show's waiting songs and the listener is told only that it plays "after the station's show"
(see [Shows](shows.md#shows)).

**What you do.** Queue & Search has a **Listener requests** card. While requests are off it says
so, and its form holds the switch to turn them on. While they are on, it says how many requests
are waiting and how many the station took today. Each waiting request shows who
sent it (if they gave a name), when, and either where it would fit and why ("Fits after Song by
Artist: same key, 2 BPM apart.") or what stops it. Each request has three buttons:

- **Approve** queues it at that place, checked again at that moment. It is left out while no place
  keeps the rules.
- **Play next** queues it first, after the song playing and any "play next" song, whatever fits.
  DMCA limits and clockwheel step restrictions still hold, counting that "play next" song
  and the queued songs after it.
- **Turn down**.

Below the decisions, **Request settings** starts with **Take listener requests** and
**Approval**, then **Smooth transitions** holds four more of the `[requests]` settings:
**Add bridge songs before a request that would sound abrupt**, **Most bridge songs** (1 to 3),
**Roughness limit** (0 to 10, the `bridge_cost_threshold`) and **When no smooth spot is found**
(**Use the best spot that keeps the rules** or **Keep the request waiting for you to decide**).
**Save request settings** saves them all for this station at once (`POST
/api/requests/settings`, which also takes `enabled` and `approval`), and says whether the request
page opened or closed. While it saves, the button is marked busy. They are kept in
`web_state.json` and, like the other settings saved on the page, take the place of the
config.toml values from then on. A Roughness limit outside 0 to 10 is refused in the browser:
the field is marked, its error shown under it and linked to it, and focus goes to it.

Focus moves to the next request's Approve button (its first button when Approve is not offered),
or to the card's title when none is left, and the result is said once just after that. Recent
decisions are listed below. The card refreshes every 15 seconds while it is open, without
speaking: rows are updated in place, and a row that has focus stays until focus leaves it. A
request still waiting after three hours expires, and can no longer be approved. The same actions are available as paired routes: `GET /api/requests`,
and `POST /api/requests/<id>/approve`, `/play-next` and `/reject` (each also under
`/api/stations/<slug>/` for a station by name). The web UI's card shows the selected station's
requests.


## Related tasks and contracts

- [Listener request checks, placement and retention](../../reference/operations/listener-requests.md)
- [How DMCA mode checks programming](../../explanation/operations/dmca.md)
