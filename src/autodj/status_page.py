"""The public station status page at ``/status``, rendered on the server.

The page works without JavaScript: every value is in the HTML as plain
text.  ``/modules/status-page.js`` then polls ``/api/nowplaying`` and
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


def render_status_page(payload: dict[str, Any], *, stream_mode: bool) -> str:
    """Return the whole status page for one now-playing *payload*."""
    station = escape(str(payload.get("station_name") or "AutoDJ"))
    now = escape(track_text(payload.get("current_track"), empty=NOTHING_PLAYING))
    # The feed sends next_track only when the operator shows the next song
    # (and one is picked), so the section shows only then.
    next_track = payload.get("next_track")
    next_text = escape(track_text(next_track, empty=""))
    next_hidden = "" if next_track else " hidden"
    listen = (
        '<p><a href="/stream.mp3">Listen to the stream</a>, or '
        '<a href="/stream.m3u" download="autodj.m3u">download the playlist file (.m3u)</a>'
        " for a media player.</p>"
        if stream_mode
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{station}: station status</title>
<link rel="stylesheet" href="/modules/status-page.css">
<script type="module" src="/modules/status-page.js"></script>
</head>
<body>
<header>
<h1 id="status-station">{station}</h1>
<p>Station status</p>
</header>
<main>
<section>
<h2>Now playing</h2>
<p id="status-now" aria-live="polite" aria-atomic="true">{now}</p>
</section>
<section id="status-next-section"{next_hidden}>
<h2>Up next</h2>
<p id="status-next">{next_text}</p>
</section>
<section>
<h2>Listeners and uptime</h2>
<dl>
<dt>Listening now</dt>
<dd id="status-listeners">{listeners_text(payload.get("listener_count"))}</dd>
<dt>Peak today</dt>
<dd id="status-peak">{listeners_text(payload.get("peak_listeners_today"))}</dd>
<dt>AutoDJ running for</dt>
<dd id="status-uptime">{uptime_text(payload.get("uptime_seconds"))}</dd>
</dl>
{listen}
</section>
</main>
<footer>
<p id="status-updated">Shows the station as it was when this page loaded. Reload the page for the latest.</p>
<p><button type="button" id="status-pause" aria-pressed="false" hidden>Pause updates</button></p>
<p><a href="/api/nowplaying">The same details as JSON</a></p>
</footer>
</body>
</html>
"""
