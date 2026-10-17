# Listener request checks, placement and retention

Look up request admission order, bridge selection, clockwheel interactions, work limits and saved-data retention.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

**What the station checks**, in this order:

1. The form's token.
2. The address's limits. Each address may run 30 searches a minute and send 10 forms in ten
   minutes, whatever becomes of them. Only accepted requests count toward `per_listener` per
   `per_listener_minutes`; a request turned away, for example by the repeat rules, does not. An
   IPv6 address counts together with its whole network of `ipv6_prefix` bits, a /56 by default:
   a home is usually delegated a /56 or a /64 and could otherwise step through its addresses. Set
   `ipv6_prefix = 64` to count each /64 apart (a home delegated more than one /64 then gets more
   than one allowance), or `128` for single addresses. An IPv4 address written as IPv6 counts as
   itself.
3. The daily cap.
4. That the song is in the library, is not blocked, and is not playing, coming up or waiting
   already.
5. How many requests are waiting.
6. Where it could play.

For the last check, AutoDJ looks at each place in the queue that would start within
`window_minutes`. The first place, after the song playing and any "play next" song, always counts.
For each place, AutoDJ works out when the song would start, as the hour preview times the queue
([Expected start times](../../explanation/operations/dmca.md#expected-start-times)). It then checks the repeat rules (track,
artist, album, title, and related tracks and artists) as of then, and in
[DMCA mode](../../explanation/operations/dmca.md#dmca-mode) the DMCA limits too. It also checks the queued songs after that place
again: a place where a later song would break a rule only because the request is ahead of it
counts as breaking that rule, named as "Artist — Song after it: same artist". A rule a queued
song breaks already does not count against the request. Of the places that break nothing, it picks the one
where the song joins its neighbours most smoothly. It weighs sound (the embeddings), tempo (half
and double time count as a match), Camelot key and the step in energy, and the sooner place wins a
tie. Working out the place and adding the song there happen as one step, so the queue cannot
change in between. A request is a song in the queue like any other. When a clockwheel is active,
the request must match its next music or category step, including that step's folder or conditions,
category membership, and step hours. A preceding clockwheel liner or settings step still runs;
a fixed track step cannot be taken by a request. A matching request advances the clockwheel
cursor when it plays. Station IDs, ad breaks and time checks retain their own scheduled places.
The queue does not reserve a music step beyond a fixed show-song step in advance; the request
waits until that step has passed and the next music step can be checked. A request never goes
between a liner you queued and the song after it, which that liner leads into.

With `bridges_enabled = true`, a request above `bridge_cost_threshold` may get one to
`max_bridges` bridge songs before it. AutoDJ searches the same embedding index the picker uses,
from both the preceding song and the request, and prefers smaller steps in sound, tempo, key and
energy. It checks each bridge at its projected play time against repeat and song-version holds,
DMCA limits, dayparting, the daypart and quarter-hour rules (on the station's clock), the
clockwheel step and request blocklists. Liners and silent tracks are never bridges. The bridges
and request are inserted together after a queue version check. Removing the queued request
(Remove, or a reorder that leaves it out) removes its bridges that have not played yet. They
belong to that request alone: a song you queued yourself stays, even when it is the same song. The
queue, the hour preview and their API data mark each bridge "Bridge to <request>"; the
operator API includes `bridges`, a `reason`, and the four transition settings.

If no bridge is eligible or no transition reaches the threshold, `no_smooth_spot = "best"`
uses the best legal position and explains that choice to the operator. With `"wait"`, an
automatically approved request stays waiting for you instead, with the reason; when you approve
it, it goes in the best legal position. When no matching clockwheel music step is available, it
waits regardless of that setting; a request never overrides a reserved step. An automatic request
that has to wait counts toward `max_pending`.

The bridge search examines at most 24 nearest neighbours from each end at each of three steps,
and validates at most 12 candidates per step. On a synthetic library of 50,000 songs with
1,040-dimensional vectors, two 24-neighbour searches took a median 255 ms and a 95th percentile
741 ms over 25 warm runs on the development machine (Windows, Python 3.14, FAISS CPU). These
numbers cover vector search, not rule projection or disk-backed category reads. Search and rule
checks run outside the queue lock and off the event loop, inside the listener request slots
when a listener's request is approved automatically; the lock covers the final version check,
insertion, bridge markers and the existing queue refresh. The operator's list, which refreshes
every 15 seconds, never runs the search: it says when approving would look for bridge songs.

Requests are kept in `listener_requests.json` in the station's state folder: waiting requests and
the last 50 decisions, with titles, names and times. A listener's name is removed 30 days after
the decision, however many decisions there are. The file never holds an address, and it is not
part of backups. The file is tidied when AutoDJ starts and every hour, so expiries and names
past their 30 days are applied even when nobody uses the page or the card. At most four searches
or request checks run at once, across all stations. Others wait their turn without holding a
worker thread, and get "busy" after two seconds. The same search within 30 seconds is answered
from memory. A song can be requested only after a search has shown it, which needs no map of the
whole library. So a flood of requests cannot take the server's time from the music. Listener
addresses are kept in memory only, for the limits, and are forgotten when AutoDJ restarts.

Letting listeners choose when songs play can make a station an "interactive" service, which the
US statutory webcasting licence does not cover. Requests therefore never play at once or at a
promised time, each listener is limited, and in DMCA mode requests obey the album and artist
limits. General information, not legal advice.


## Related tasks and contracts

- [How DMCA mode checks programming](../../explanation/operations/dmca.md)
