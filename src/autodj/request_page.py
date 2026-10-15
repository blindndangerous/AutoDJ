"""The public listener request page (#49), rendered on the server.

The default station's page is ``/requests``; every station's is also
``/stations/<slug>/requests``.  It is off until it is turned on for the
station: ``[requests] enabled``, until the operator's Listener requests card
saves its own switch for the station, then that saved switch.  While it is off
it answers 404 with a page
that says requests are closed, so a listener whose page was open when
requests were turned off is told so in words.

The page needs no JavaScript: a search form (GET ``?q=``), the songs found
as links (GET ``?track=<id>``), a confirmation form with an optional name
(POST), and the answer as a page whose title and first heading say what
happened, so a screen reader hears it as the page loads.  Each POST
carries a signed form token (:class:`~autodj.listener_requests.FormTokens`)
and, like every unsafe request, must come with this server's own
``Origin``.  Songs are named only by an opaque id; no file path is ever in
the page.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from autodj.listener_access import normalize_address
from autodj.listener_requests import (
    CLOSED,
    MAX_NAME,
    MAX_QUERY,
    MIN_QUERY,
    TRACK_ID,
    Outcome,
    RequestDesk,
    _minutes,
    _songs,
    clean_name,
)
from autodj.station_log import CURRENT_STATION
from autodj.stations import StoppedStation
from autodj.status_page import track_text

if TYPE_CHECKING:
    from fastapi import FastAPI

    from autodj.indexer import IndexEntry
    from autodj.stations import StationRegistry, StationRuntime

#: The page loads only its own theme script and stylesheet and posts only to itself.
REQUEST_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
#: The most a request form may send; it has three short fields.
FORM_MAX_BYTES = 2048
#: Searches and request checks running at once, across every station.  More
#: wait on the event loop up to WORK_WAIT_SECONDS, holding no thread, then
#: get "busy": a flood can never park the worker threads that carts,
#: breaks, approvals and the player's helpers need.
WORK_SLOTS = 4
WORK_WAIT_SECONDS = 2.0
# One semaphore per event loop (an asyncio semaphore belongs to one loop).
_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


class Busy(RuntimeError):
    """Too many searches or requests are being worked on at once."""


class Closed(LookupError):
    """The station is not taking requests: the page says so, with status 404."""


async def bounded[T, *Ts](work: Callable[[*Ts], T], *args: *Ts) -> T:
    """Run *work* in a worker thread, once one of :data:`WORK_SLOTS` is free.

    The wait for a slot happens on the event loop, so waiting requests hold
    no thread.

    Raises:
        Busy: No slot came free within :data:`WORK_WAIT_SECONDS`.
    """
    loop = asyncio.get_running_loop()
    slots = _SLOTS.get(loop)
    if slots is None:
        slots = _SLOTS[loop] = asyncio.Semaphore(WORK_SLOTS)
    try:
        await asyncio.wait_for(slots.acquire(), WORK_WAIT_SECONDS)
    except TimeoutError:
        raise Busy from None
    try:
        return await asyncio.to_thread(work, *args)
    finally:
        slots.release()


def _page_path(slug: str, *, by_name: bool) -> str:
    return f"/stations/{slug}/requests" if by_name else "/requests"


def _status_path(slug: str, *, by_name: bool) -> str:
    return f"/stations/{slug}/status" if by_name else "/status"


def _how_it_works(desk: RequestDesk) -> str:
    cfg = desk.cfg
    decided = (
        "Requests that keep the station's rules are usually added to what is coming up. "
        "Some wait for a smoother place."
        if cfg.approval == "auto"
        else "The station looks at each request and decides whether to play it."
    )
    return (
        '<section aria-labelledby="req-how">\n<h2 id="req-how">How requests work</h2>\n'
        f"<p>You can request up to {_songs(cfg.per_listener)} every "
        f"{escape(_minutes(cfg.per_listener_minutes))}. {decided} A requested song plays "
        "when it fits in with what is playing, usually within the hour, never at a set time. "
        "Songs, artists and albums played recently cannot be requested again yet.</p>\n"
        "</section>"
    )


def render_request_page(
    station: str,
    *,
    page_path: str,
    status_path: str,
    desk: RequestDesk | None,
    query: str = "",
    query_error: str | None = None,
    results: list[tuple[str, IndexEntry]] | None = None,
    confirm: tuple[str, IndexEntry] | None = None,
    token: str = "",
    outcome: Outcome | None = None,
    off_air: bool = False,
    closed: bool = False,
) -> str:
    """Return the whole request page for one state of the conversation.

    Args:
        station: The station's name.
        page_path: This page's own address.
        status_path: The station's status page.
        desk: The station's request desk; ``None`` while it is off air.
        query: The search, shown back in the field.
        query_error: Why the search was refused, shown with the field.
        results: The songs found, as ``(id, entry)``; ``None`` before a search.
        confirm: The song to confirm a request for, as ``(id, entry)``.
        token: The form token for the confirmation form.
        outcome: What happened to a request just sent.
        off_air: The station is stopped.
        closed: The station is not taking requests.
    """
    name = escape(station)
    page = escape(page_path)
    parts: list[str] = []
    if closed:
        off_air = True  # no search form either
        title = f"{CLOSED.headline}: {station}"
        parts.append(
            f'<section aria-labelledby="req-message" class="message">\n'
            f'<h2 id="req-message">{escape(CLOSED.headline)}</h2>\n<p>{name} is not taking '
            "requests right now. Please try again later.</p>\n</section>"
        )
    elif off_air:
        title = f"Off air: {station}"
        parts.append(
            f'<section aria-labelledby="req-message" class="message">\n'
            f'<h2 id="req-message">Off air</h2>\n<p>{name} is off air right now, so it is '
            "not taking requests. Please try again later.</p>\n</section>"
        )
    elif outcome is not None:
        title = f"{outcome.headline}: {station}"
        parts.append(
            f'<section aria-labelledby="req-message" class="message">\n'
            f'<h2 id="req-message">{escape(outcome.headline)}</h2>\n'
            f"<p>{escape(outcome.detail)}</p>\n</section>"
        )
    elif confirm is not None:
        title = f"Confirm your request: {station}"
    elif query_error is not None:
        title = f"Error: {query_error} {station}: request a song"
    elif results is not None:
        title = f"Search results: {station}: request a song"
    else:
        title = f"{station}: request a song"

    if confirm is not None and outcome is None and not off_air:
        track_id, entry = confirm
        parts.append(
            f"""<section aria-labelledby="req-confirm">
<h2 id="req-confirm">Confirm your request</h2>
<p>{escape(track_text(_track(entry), empty=""))}</p>
<form method="post" action="{page}">
<input type="hidden" name="token" value="{escape(token)}">
<input type="hidden" name="track" value="{escape(track_id)}">
<label for="req-name">Your name (optional)</label>
<p class="hint" id="req-name-hint">The station may say it on air. Up to {MAX_NAME} characters.</p>
<input type="text" id="req-name" name="name" maxlength="{MAX_NAME}" autocomplete="nickname" aria-describedby="req-name-hint">
<p><button type="submit">Send request</button></p>
</form>
<p><a href="{page}">Back to the search</a></p>
</section>"""
        )
    elif not off_air:
        invalid = ""
        error = ""
        if query_error is not None:
            invalid = ' aria-invalid="true" aria-describedby="req-q-error"'
            error = f'<p class="error" id="req-q-error">{escape(query_error)}</p>\n'
        parts.append(
            f"""<section aria-labelledby="req-search">
<h2 id="req-search">Find a song</h2>
<form method="get" action="{page}" role="search">
<label for="req-q">Title, artist or album</label>
{error}<div class="search-row">
<input type="search" id="req-q" name="q" value="{escape(query)}" maxlength="{MAX_QUERY}"{invalid}>
<button type="submit">Search</button>
</div>
</form>
</section>"""
        )
        if results is not None:
            parts.append(_results(results, page))
    if desk is not None:
        parts.append(_how_it_works(desk))
    body = "\n".join(parts)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<script src="/modules/theme-init.js"></script>
<link rel="stylesheet" href="/modules/request-page.css">
</head>
<body>
<header>
<h1>{name}</h1>
<p>Request a song</p>
</header>
<main>
{body}
</main>
<footer>
<p><a href="{escape(status_path)}">Station status</a></p>
</footer>
</body>
</html>
"""


def _track(entry: IndexEntry) -> dict[str, Any]:
    return {"title": entry.title, "artist": entry.artist, "album": entry.album}


def _results(results: list[tuple[str, IndexEntry]], page: str) -> str:
    if not results:
        return (
            '<section aria-labelledby="req-results">\n<h2 id="req-results">Search results</h2>\n'
            "<p>No songs found. Try other words, or fewer of them.</p>\n</section>"
        )
    count = len(results)
    items = "\n".join(
        f'<li><a href="{page}?track={quote(track_id)}">'
        f"{escape(track_text(_track(entry), empty=''))}</a></li>"
        for track_id, entry in results
    )
    many = " Add words to narrow the search." if count >= 20 else ""
    return (
        '<section aria-labelledby="req-results">\n<h2 id="req-results">Search results</h2>\n'
        f"<p>{count} song{'' if count == 1 else 's'} found.{many} Choose a song to request it.</p>\n"
        f"<ul>\n{items}\n</ul>\n</section>"
    )


def _headers(retry_after: int | None = None) -> dict[str, str]:
    headers = {
        # The page carries a form token: never kept by a cache.
        "Cache-Control": "no-store",
        "Content-Security-Policy": REQUEST_PAGE_CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        # same-origin, not no-referrer: under no-referrer browsers send
        # "Origin: null" with the page's own form posts, which the Origin
        # check refuses, so no request could be sent.  same-origin still
        # gives other sites no referrer.
        "Referrer-Policy": "same-origin",
    }
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return headers


def _address(request: Request) -> str:
    try:
        return normalize_address(request.client.host if request.client else "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid listener address") from None


async def _form(request: Request) -> dict[str, str]:
    """The posted form's fields, read up to :data:`FORM_MAX_BYTES`; 400 or 413 otherwise."""
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Send the request form")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > FORM_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
    try:
        fields = parse_qs(
            body.decode("utf-8"), keep_blank_values=True, strict_parsing=False, max_num_fields=8
        )
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid request form") from None
    if any(len(values) != 1 for values in fields.values()):
        raise HTTPException(status_code=400, detail="Invalid request form")
    return {key: values[0] for key, values in fields.items()}


def install_request_pages(app: FastAPI, stations: StationRegistry) -> None:
    """Register ``/requests`` and ``/stations/<slug>/requests`` (GET, HEAD and POST)."""

    def lookup(slug: str | None) -> tuple[StationRuntime | StoppedStation, bool]:
        if slug is None:
            return stations.default, False
        try:
            return stations.get(slug), True
        except KeyError:
            raise HTTPException(status_code=404, detail="Not Found") from None

    def enabled_desk(station: StationRuntime | StoppedStation) -> RequestDesk | None:
        """The station's desk; ``None`` while it is stopped; :class:`Closed` unless requests are on."""
        if isinstance(station, StoppedStation):
            if not station.requests_enabled:
                raise Closed
            return None
        desk = station.requests
        if desk is None or desk.cfg.enabled is not True:
            raise Closed
        CURRENT_STATION.set(station.slug)
        return desk

    def respond(
        station: StationRuntime | StoppedStation,
        by_name: bool,
        status: int = 200,
        retry_after: int | None = None,
        **state: Any,
    ) -> HTMLResponse:
        html = render_request_page(
            station.name,
            page_path=_page_path(station.slug, by_name=by_name),
            status_path=_status_path(station.slug, by_name=by_name),
            **state,
        )
        return HTMLResponse(html, status_code=status, headers=_headers(retry_after))

    async def show(request: Request, slug: str | None) -> HTMLResponse:
        station, by_name = lookup(slug)
        try:
            return await _show(request, station, by_name)
        except Busy:
            return busy(station, by_name)
        except Closed:
            return respond(station, by_name, 404, desk=None, closed=True)

    async def send(request: Request, slug: str | None) -> HTMLResponse:
        station, by_name = lookup(slug)
        try:
            return await _send(request, station, by_name)
        except Busy:
            return busy(station, by_name)
        except Closed:
            return respond(station, by_name, 404, desk=None, closed=True)

    def busy(station: StationRuntime | StoppedStation, by_name: bool) -> HTMLResponse:
        outcome = Outcome(
            503, "The station is busy", "Many people are using this page. Please try again soon."
        )
        desk = station.requests if not isinstance(station, StoppedStation) else None
        return respond(station, by_name, 503, 10, desk=desk, outcome=outcome)

    async def _show(
        request: Request, station: StationRuntime | StoppedStation, by_name: bool
    ) -> HTMLResponse:
        desk = enabled_desk(station)
        if desk is None:
            return respond(station, by_name, 503, desk=None, off_air=True)
        params = request.query_params
        track = params.get("track")
        if track is not None:
            entry = None
            if TRACK_ID.fullmatch(track):
                entry = await bounded(desk.song, track)
            if entry is None:
                outcome = Outcome(
                    404, "Song not found", "That song cannot be requested. Please search again."
                )
                return respond(station, by_name, 404, desk=desk, outcome=outcome)
            token = desk.tokens.issue(station.slug, track)
            return respond(station, by_name, desk=desk, confirm=(track, entry), token=token)
        query = params.get("q")
        if query is None:
            return respond(station, by_name, desk=desk)
        query = " ".join(query.split())
        if len(query) < MIN_QUERY or len(query) > MAX_QUERY:
            error = (
                f"Type at least {MIN_QUERY} letters to search."
                if len(query) < MIN_QUERY
                else f"Use at most {MAX_QUERY} characters."
            )
            return respond(
                station, by_name, 400, desk=desk, query=query[:MAX_QUERY], query_error=error
            )
        if (wait := desk.searches.hit(_address(request))) is not None:
            outcome = Outcome(429, "Too many searches", "Please wait a minute, then search again.")
            return respond(station, by_name, 429, wait, desk=desk, query=query, outcome=outcome)
        results = await bounded(desk.search, query)
        return respond(station, by_name, desk=desk, query=query, results=results)

    async def _send(
        request: Request, station: StationRuntime | StoppedStation, by_name: bool
    ) -> HTMLResponse:
        desk = enabled_desk(station)
        if desk is None:
            return respond(station, by_name, 503, desk=None, off_air=True)
        address = _address(request)
        if (wait := desk.posts.hit(address)) is not None:
            outcome = Outcome(429, "Too many tries", "Please wait a few minutes, then try again.")
            return respond(station, by_name, 429, wait, desk=desk, outcome=outcome)
        form = await _form(request)
        if set(form) - {"token", "track", "name"}:
            raise HTTPException(status_code=400, detail="Invalid request form")
        track = form.get("track", "")
        if not desk.tokens.valid(form.get("token", ""), station.slug, track):
            outcome = Outcome(
                403,
                "This form has expired",
                "Please search for the song again and send a new request.",
            )
            return respond(station, by_name, 403, desk=desk, outcome=outcome)
        name = clean_name(form.get("name", ""))
        if name is None or not TRACK_ID.fullmatch(track):
            outcome = Outcome(
                400,
                "Request not sent",
                f"Your name can be up to {MAX_NAME} characters, without special characters. "
                "Please go back and try again.",
            )
            return respond(station, by_name, 400, desk=desk, outcome=outcome)
        # Off the event loop: it reads the library and projects the queue.
        outcome = await bounded(desk.submit, track, name, address)
        return respond(
            station, by_name, outcome.status, outcome.retry_after, desk=desk, outcome=outcome
        )

    @app.api_route("/requests", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def get_request_page(request: Request) -> HTMLResponse:
        """The default station's request page: search, choose and confirm."""
        return await show(request, None)

    @app.post("/requests", response_class=HTMLResponse)
    async def post_request(request: Request) -> HTMLResponse:
        """Send a request to the default station."""
        return await send(request, None)

    @app.api_route(
        "/stations/{slug}/requests", methods=["GET", "HEAD"], response_class=HTMLResponse
    )
    async def get_station_request_page(slug: str, request: Request) -> HTMLResponse:
        """Any station's request page by name; 503 "off air" while it is stopped."""
        return await show(request, slug)

    @app.post("/stations/{slug}/requests", response_class=HTMLResponse)
    async def post_station_request(slug: str, request: Request) -> HTMLResponse:
        """Send a request to any station by name."""
        return await send(request, slug)


__all__ = ["FORM_MAX_BYTES", "REQUEST_PAGE_CSP", "install_request_pages", "render_request_page"]
