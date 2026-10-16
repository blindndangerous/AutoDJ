"""The public player page and the embeddable player (#52).

Listeners press Play to hear a station's stream in the browser, with its
song, a volume control and, on the full page, the recent songs.  The
default station's pages are ``/player`` and ``/embed``; every station's
are also ``/stations/<slug>/player`` and ``/stations/<slug>/embed``.  Both
are off until ``[player_page] enabled`` or ``embed`` turns them on, for
each station on its own.

Like the status page (:mod:`autodj.status_page`), every value is in the
HTML as plain text.  Without JavaScript the browser's own audio controls
play the stream; ``/modules/player-page.js`` swaps them for a labelled
Play button and Volume slider, and ``/modules/status-page.js`` keeps the
song current from the station's public feed.  Nothing plays until the
listener presses Play.  The pages carry only what the public feed already
shows: no file paths, credentials or internal addresses.

The pages are in English, like the rest of AutoDJ (``lang="en"``); there
is no station or server language setting to take another from.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from autodj.station_log import CURRENT_STATION, DEFAULT_SLUG
from autodj.stations import StoppedStation, public_feed_path, public_page_path
from autodj.status_page import NOTHING_PLAYING, off_air_text, track_text

if TYPE_CHECKING:
    from fastapi import FastAPI

    from autodj.config import PlayerPageConfig
    from autodj.stations import StationRegistry, StationRuntime

Kind = Literal["player", "embed"]

#: Both pages load only their own script and stylesheet, fetch only this
#: server's feed and play only this server's stream.
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "media-src 'self'; base-uri 'none'; form-action 'none'"
)
#: The full page is never shown inside another site's frame.
PLAYER_PAGE_CSP = _CSP + "; frame-ancestors 'none'"

NO_HISTORY = "No songs have finished playing yet."


def embed_csp(origins: list[str]) -> str:
    """The embeddable player's policy: framed only by this server and *origins*.

    *origins* are already checked and written canonically by
    :class:`~autodj.config.PlayerPageConfig`; ``"*"`` lets any site frame it.
    """
    ancestors = "*" if "*" in origins else " ".join(["'self'", *origins])
    return f"{_CSP}; frame-ancestors {ancestors}"


@dataclass(frozen=True)
class PageAddresses:
    """One station's public addresses, as one of its pages links them.

    Attributes:
        feed: Its now-playing feed, which the page polls.
        stream: Its stream, or ``None`` when the station does not stream.
        m3u: Its one-line playlist.
        playlist_name: The file name the playlist downloads as.
        status: Its status page.
        player: Its full player page, or ``None`` when that is off.
    """

    feed: str
    stream: str | None
    m3u: str
    playlist_name: str
    status: str
    player: str | None

    @classmethod
    def for_station(
        cls, slug: str, *, by_name: bool, streams: bool, player_enabled: bool
    ) -> PageAddresses:
        """The addresses of station *slug*, by name or as the default station's own."""
        stream = f"/{slug}.mp3" if by_name else "/stream.mp3"
        return cls(
            feed=public_feed_path(slug, by_name=by_name),
            stream=stream if streams else None,
            m3u=stream.removesuffix(".mp3") + ".m3u",
            playlist_name=f"{slug}.m3u" if slug != DEFAULT_SLUG else "autodj.m3u",
            status=public_page_path(slug, "status", by_name=by_name),
            player=public_page_path(slug, "player", by_name=by_name) if player_enabled else None,
        )


def _history_items(payload: dict[str, Any] | None) -> list[str]:
    history = payload.get("history") if payload else None
    if not isinstance(history, list):
        return []
    return [track_text(row, empty="") for row in history if isinstance(row, dict)]


def _listen(name: str, addresses: PageAddresses, *, links: bool) -> str:
    """The Play button, Volume slider and the playback status line.

    Without JavaScript the ``<audio>`` element's own controls show instead
    (``preload="none"``: nothing is fetched until Play is pressed).
    """
    if addresses.stream is None:
        return f"<p>{escape(name)} is not streaming right now, so it cannot be played here.</p>"
    stream = escape(addresses.stream)
    more = ""
    if links:
        more = (
            f'<p>Or open <a href="{stream}">the stream</a> in a media player, or '
            f'<a href="{escape(addresses.m3u)}" download="{escape(addresses.playlist_name)}">'
            "download the playlist file (.m3u)</a>.</p>"
        )
    return f"""<audio id="player-audio" controls preload="none" src="{stream}"></audio>
<div id="player-controls" class="player-controls" hidden>
<button type="button" id="player-toggle">Play</button>
<div class="player-volume" id="player-volume-group">
<label for="player-volume">Volume</label>
<input type="range" id="player-volume" min="0" max="100" step="5" value="100" aria-valuetext="100%">
<span id="player-volume-value" aria-hidden="true">100%</span>
</div>
<p id="player-volume-note" hidden>The volume follows your device's own volume control.</p>
</div>
<p id="player-state" role="status"></p>
{more}"""


def _head(title: str, *, theme_script: bool) -> str:
    # The full page follows the theme chosen in the web UI on this browser
    # (theme-init.js); the embed follows its host's ?theme= or the system.
    init = '<script src="/modules/theme-init.js"></script>\n' if theme_script else ""
    return f"""<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
{init}<link rel="stylesheet" href="/modules/player-page.css">
<script type="module" src="/modules/player-page.js"></script>
</head>"""


def _body_attributes(page: str, addresses: PageAddresses) -> str:
    stream = f' data-stream="{escape(addresses.stream)}"' if addresses.stream else ""
    return f'data-page="{page}" data-feed="{escape(addresses.feed)}"{stream}'


def render_player_page(
    payload: dict[str, Any] | None, *, station: str, addresses: PageAddresses
) -> str:
    """Return the full player page; *payload* ``None`` means the station is off air."""
    if payload is None:
        now = off_air_text(station)
        next_text = None
    else:
        now = track_text(payload.get("current_track"), empty=NOTHING_PLAYING)
        upcoming = payload.get("next_track")
        next_text = track_text(upcoming, empty="") if upcoming else None
    history = _history_items(payload)
    items = "".join(f"<li>{escape(text)}</li>" for text in history)
    name = escape(station)
    return f"""<!doctype html>
<html lang="en">
{_head(f"{station}: listen live", theme_script=True)}
<body {_body_attributes("player", addresses)}>
<header>
<h1 id="status-station">{name}</h1>
<p>Listen live</p>
</header>
<main>
<section>
<h2 id="player-listen-heading">Listen</h2>
{_listen(station, addresses, links=True)}
</section>
<section>
<h2>Now playing</h2>
<p id="status-now" aria-live="polite" aria-atomic="true">{escape(now)}</p>
</section>
<section id="status-next-section"{"" if next_text else " hidden"}>
<h2>Up next</h2>
<p id="status-next">{escape(next_text or "")}</p>
</section>
<section>
<h2>Recently played</h2>
<ol id="player-history"{"" if history else " hidden"}>{items}</ol>
<p id="player-history-empty"{" hidden" if history else ""}>{NO_HISTORY}</p>
</section>
</main>
<footer>
<p id="status-updated">Shows the station as it was when this page loaded. Reload the page for the latest.</p>
<p><button type="button" id="status-pause" aria-pressed="false" hidden>Pause updates</button></p>
<p><a href="{escape(addresses.status)}">Station status</a></p>
</footer>
</body>
</html>
"""


def render_embed(
    payload: dict[str, Any] | None,
    *,
    station: str,
    addresses: PageAddresses,
    theme: str | None = None,
) -> str:
    """Return the embeddable player; *theme* ``"light"`` or ``"dark"`` pins its colours."""
    now = (
        off_air_text(station)
        if payload is None
        else track_text(payload.get("current_track"), empty=NOTHING_PLAYING)
    )
    pinned = f' data-theme="{theme}"' if theme in {"light", "dark"} else ""
    full = ""
    if addresses.player is not None:
        full = (
            f'<a href="{escape(addresses.player)}" target="_blank" rel="noopener">'
            "Open the full player (new tab)</a>"
        )
    return f"""<!doctype html>
<html lang="en"{pinned}>
{_head(f"{station} player", theme_script=False)}
<body class="embed" {_body_attributes("embed", addresses)}>
<main>
<h1 id="status-station">{escape(station)}</h1>
<p class="embed-now">Now playing: <span id="status-now" aria-live="polite" aria-atomic="true">{escape(now)}</span></p>
{_listen(station, addresses, links=False)}
<p class="embed-footer"><button type="button" id="status-pause" aria-pressed="false" hidden>Pause updates</button>
{full}</p>
</main>
</body>
</html>
"""


def _settings(station: StationRuntime | StoppedStation) -> PlayerPageConfig | None:
    if isinstance(station, StoppedStation):
        return station.player_page
    return station.cfg.player_page


def player_page_response(
    station: StationRuntime | StoppedStation,
    kind: Kind,
    *,
    by_name: bool,
    theme: str | None = None,
) -> HTMLResponse:
    """A station's player page or embeddable player; 404 unless it is turned on.

    A stopped station's page answers 503 and says it is off air; its Play
    button stays, and the page notices when the station is back.
    """
    settings = _settings(station)
    if settings is None or not (settings.enabled if kind == "player" else settings.embed):
        raise HTTPException(status_code=404, detail="Not Found")
    payload: dict[str, Any] | None = None
    name = station.name
    if isinstance(station, StoppedStation):
        stopped, streams = True, True
    else:
        CURRENT_STATION.set(station.slug)
        stopped = False
        payload = station.nowplaying.snapshot()
        name = str(payload.get("station_name") or station.name)
        streams = bool(station.bridge.stream_mode) and station.listener_access is not None
    addresses = PageAddresses.for_station(
        station.slug, by_name=by_name, streams=streams, player_enabled=settings.enabled
    )
    headers = {
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if kind == "player":
        html = render_player_page(payload, station=name, addresses=addresses)
        headers["Content-Security-Policy"] = PLAYER_PAGE_CSP
        headers["X-Frame-Options"] = "DENY"
    else:
        html = render_embed(payload, station=name, addresses=addresses, theme=theme)
        headers["Content-Security-Policy"] = embed_csp(settings.embed_origins)
    return HTMLResponse(html, status_code=503 if stopped else 200, headers=headers)


def install_player_pages(app: FastAPI, stations: StationRegistry) -> None:
    """Register ``/player``, ``/embed`` and each station's ``/stations/<slug>/...`` pair."""

    def by_slug(slug: str) -> StationRuntime | StoppedStation:
        try:
            return stations.get(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not Found") from None

    def theme_of(request: Request) -> str | None:
        theme = request.query_params.get("theme")
        return theme if theme in {"light", "dark"} else None

    @app.api_route("/player", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def get_player_page() -> HTMLResponse:
        """Serve the default station's player page, when it is turned on."""
        return player_page_response(stations.default, "player", by_name=False)

    @app.api_route("/embed", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def get_embed(request: Request) -> HTMLResponse:
        """Serve the default station's embeddable player, when it is turned on."""
        return player_page_response(
            stations.default, "embed", by_name=False, theme=theme_of(request)
        )

    @app.api_route("/stations/{slug}/player", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def get_station_player_page(slug: str) -> HTMLResponse:
        """Serve any station's player page by name; 503 "off air" while it is stopped."""
        return player_page_response(by_slug(slug), "player", by_name=True)

    @app.api_route("/stations/{slug}/embed", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def get_station_embed(slug: str, request: Request) -> HTMLResponse:
        """Serve any station's embeddable player by name; 503 "off air" while it is stopped."""
        return player_page_response(by_slug(slug), "embed", by_name=True, theme=theme_of(request))


__all__ = [
    "PLAYER_PAGE_CSP",
    "PageAddresses",
    "embed_csp",
    "install_player_pages",
    "player_page_response",
    "render_embed",
    "render_player_page",
]
