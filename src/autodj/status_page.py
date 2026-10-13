"""The public station status pages, rendered on the server.

The default station's is at ``/status``; every station's is also at
``/stations/<slug>/status``.  The page works without JavaScript: every
value is in the HTML as plain text.  ``/modules/status-page.js`` then
polls the station's feed (``data-feed`` on the body, such as
``/api/nowplaying`` or ``/api/stations/<slug>/nowplaying``) and
rewrites the same elements, so the wording here and there must match.
Only the now-playing line is a live region, and the script changes it
only when the song changes.
"""

from __future__ import annotations

from html import escape
from typing import Any

#: Sent with the page: it loads only its own script and stylesheet and
#: fetches only this server's feed.
STATUS_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'"
)


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def track_text(track: dict[str, Any] | None, *, empty: str) -> str:
    """Say a track in words: "Title by Artist, from the album Album."."""
    if not track:
        return empty
    title = str(track.get("title") or "").strip() or "Unknown title"
    artist = str(track.get("artist") or "").strip() or "an unknown artist"
    text = f"{title} by {artist}"
    album = str(track.get("album") or "").strip()
    if album:
        text += f", from the album {album}"
    return text + "."


def listeners_text(count: Any) -> str:
    """Return "1 listener" or "3 listeners"."""
    number = _count(count)
    return f"{number} listener{'' if number == 1 else 's'}"


def uptime_text(seconds: Any) -> str:
    """Say a duration to the minute: "2 days, 3 hours, 1 minute"."""
    total = _count(seconds)
    if total < 60:
        return "less than a minute"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [
        f"{value} {unit}{'' if value == 1 else 's'}"
        for value, unit in ((days, "day"), (hours, "hour"), (minutes, "minute"))
        if value
    ]
    return ", ".join(parts)


NOTHING_PLAYING = "Nothing is playing right now."
#: The uptime while a station is stopped.
OFF_AIR = "Not on air"


def off_air_text(station: str) -> str:
    """The now-playing line while a station is stopped."""
    return f"{station} is off air right now."


def render_status_page(
    payload: dict[str, Any],
    *,
    stream_mode: bool,
    feed_url: str = "/api/nowplaying",
    stream_path: str = "/stream.mp3",
    m3u_path: str = "/stream.m3u",
    playlist_name: str = "autodj.m3u",
) -> str:
    """Return the whole status page for one now-playing *payload*.

    Args:
        payload: The station's public now-playing feed.
        stream_mode: Whether the station streams, so the page links to it.
        feed_url: The station's feed, which the page script polls.
        stream_path: The station's stream.
        m3u_path: The station's one-line playlist.
        playlist_name: The file name the playlist downloads as.
    """
    station = str(payload.get("station_name") or "AutoDJ")
    # The feed sends next_track only when the operator shows the next song
    # (and one is picked), so the section shows only then.
    next_track = payload.get("next_track")
    return _page(
        station,
        now=track_text(payload.get("current_track"), empty=NOTHING_PLAYING),
        next_text=track_text(next_track, empty="") if next_track else None,
        listeners=listeners_text(payload.get("listener_count")),
        peak=listeners_text(payload.get("peak_listeners_today")),
        uptime=uptime_text(payload.get("uptime_seconds")),
        listen=(stream_path, m3u_path, playlist_name) if stream_mode else None,
        feed_url=feed_url,
    )


def render_off_air_page(
    station: str, *, feed_url: str, stream_path: str, m3u_path: str, playlist_name: str
) -> str:
    """Return the status page of a stopped station.

    It has the same parts as a running station's page, so the page script
    fills them in when the station starts again; until then it says the
    station is off air.
    """
    return _page(
        station,
        now=off_air_text(station),
        next_text=None,
        listeners=listeners_text(0),
        peak=listeners_text(0),
        uptime=OFF_AIR,
        listen=(stream_path, m3u_path, playlist_name),
        feed_url=feed_url,
    )


def _page(
    station: str,
    *,
    now: str,
    next_text: str | None,
    listeners: str,
    peak: str,
    uptime: str,
    listen: tuple[str, str, str] | None,
    feed_url: str,
) -> str:
    name = escape(station)
    next_hidden = "" if next_text else " hidden"
    links = ""
    if listen is not None:
        stream_path, m3u_path, playlist_name = (escape(value) for value in listen)
        links = (
            f'<p><a href="{stream_path}">Listen to the stream</a>, or '
            f'<a href="{m3u_path}" download="{playlist_name}">download the playlist file '
            "(.m3u)</a> for a media player.</p>"
        )
    feed = escape(feed_url)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}: station status</title>
<script src="/modules/theme-init.js"></script>
<link rel="stylesheet" href="/modules/status-page.css">
<script type="module" src="/modules/status-page.js"></script>
</head>
<body data-page="status" data-feed="{feed}">
<header>
<h1 id="status-station">{name}</h1>
<p>Station status</p>
</header>
<main>
<section>
<h2>Now playing</h2>
<p id="status-now" aria-live="polite" aria-atomic="true">{escape(now)}</p>
</section>
<section id="status-next-section"{next_hidden}>
<h2>Up next</h2>
<p id="status-next">{escape(next_text or "")}</p>
</section>
<section>
<h2>Listeners and uptime</h2>
<dl>
<dt>Listening now</dt>
<dd id="status-listeners">{escape(listeners)}</dd>
<dt>Peak today</dt>
<dd id="status-peak">{escape(peak)}</dd>
<dt>Uptime</dt>
<dd id="status-uptime">{escape(uptime)}</dd>
</dl>
{links}
</section>
</main>
<footer>
<p id="status-updated">Shows the station as it was when this page loaded. Reload the page for the latest.</p>
<p><button type="button" id="status-pause" aria-pressed="false" hidden>Pause updates</button></p>
<p><a href="{feed}">The same details as JSON</a></p>
</footer>
</body>
</html>
"""
