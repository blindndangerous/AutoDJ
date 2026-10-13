"""The request page's HTML: words, structure and escaping (#49)."""

from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj.config import RequestsConfig
from autodj.indexer import IndexEntry
from autodj.listener_requests import Outcome
from autodj.request_page import REQUEST_PAGE_CSP, render_request_page

SONG = IndexEntry(
    path="/srv/private/music/song.flac",
    title="Song",
    artist="Artist",
    album="Album",
    genre="",
    bpm=120.0,
    year=2000,
    length=180.0,
    energy=0.1,
    key=0,
    mode=1,
    tempo_confidence=0.8,
)
DESK = SimpleNamespace(cfg=RequestsConfig(enabled=True, per_listener=1, per_listener_minutes=90))


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(html)
    return parser.tags


def _page(**state: object) -> str:
    return render_request_page(
        "Night Owl Radio",
        page_path="/stations/nightowl/requests",
        status_path="/stations/nightowl/status",
        desk=DESK,  # type: ignore[arg-type]
        **state,  # type: ignore[arg-type]
    )


def test_the_search_form_is_labelled_and_says_how_requests_work() -> None:
    html = _page()
    tags = _tags(html)
    assert "<title>Night Owl Radio: request a song</title>" in html
    (form,) = [attrs for tag, attrs in tags if tag == "form"]
    assert form == {
        "method": "get",
        "action": "/stations/nightowl/requests",
        "role": "search",
    }
    assert '<label for="req-q">Title, artist or album</label>' in html
    assert "You can request up to 1 song every 90 minutes." in html
    assert "never at a set time" in html
    # No live regions: each answer is a new page, said by its title.
    assert not [
        attrs
        for _tag, attrs in tags
        if "aria-live" in attrs or ("role" in attrs and attrs["role"] == "status")
    ]
    assert '<a href="/stations/nightowl/status">Station status</a>' in html
    assert "script-src 'self'" in REQUEST_PAGE_CSP and "form-action 'self'" in REQUEST_PAGE_CSP


def test_results_are_links_named_by_their_songs_and_carry_no_path() -> None:
    other = replace(SONG, path="/srv/private/b.flac", title="B side", album="")
    html = _page(query="song", results=[("a" * 24, SONG), ("b" * 24, other)])
    assert "<title>Search results: Night Owl Radio: request a song</title>" in html
    assert "2 songs found. Choose a song to request it." in html
    assert (
        f'<a href="/stations/nightowl/requests?track={"a" * 24}">'
        "Song by Artist, from the album Album.</a>"
    ) in html
    assert "B side by Artist.</a>" in html
    assert 'value="song"' in html
    assert "/srv/private" not in html
    assert "No songs found." in _page(query="zz", results=[])
    many = _page(query="s", results=[("c" * 24, SONG)] * 20)
    assert "20 songs found. Add words to narrow the search." in many


def test_the_confirm_form_posts_to_itself_with_a_token() -> None:
    html = _page(confirm=("a" * 24, SONG), token="123.abc")
    tags = _tags(html)
    assert "<title>Confirm your request: Night Owl Radio</title>" in html
    (form,) = [attrs for tag, attrs in tags if tag == "form"]
    assert form == {"method": "post", "action": "/stations/nightowl/requests"}
    hidden = {attrs["name"]: attrs["value"] for tag, attrs in tags if attrs.get("type") == "hidden"}
    assert hidden == {"token": "123.abc", "track": "a" * 24}
    assert '<label for="req-name">Your name (optional)</label>' in html
    (name,) = [attrs for tag, attrs in tags if attrs.get("id") == "req-name"]
    assert name["maxlength"] == "40" and name["aria-describedby"] == "req-name-hint"
    assert '<button type="submit">Send request</button>' in html
    assert "/srv/private" not in html


def test_an_answer_is_in_the_title_and_the_first_heading() -> None:
    html = _page(outcome=Outcome(409, "Not right now", "Song by Artist cannot play again so soon."))
    assert "<title>Not right now: Night Owl Radio</title>" in html
    assert '<h2 id="req-message">Not right now</h2>' in html
    assert html.index("req-message") < html.index("req-search")


def test_a_search_error_is_tied_to_its_field() -> None:
    html = _page(query="a", query_error="Type at least 2 letters to search.")
    assert html.startswith("<!doctype html>")
    assert (
        "<title>Error: Type at least 2 letters to search. Night Owl Radio: request a song</title>"
        in html
    )
    assert 'aria-invalid="true" aria-describedby="req-q-error"' in html
    assert '<p class="error" id="req-q-error">Type at least 2 letters to search.</p>' in html


def test_everything_from_tags_and_listeners_is_escaped(tmp_path: Path) -> None:
    hostile = replace(SONG, title="<script>x</script>", artist='"&')
    html = render_request_page(
        "<Owl & Co>",
        page_path="/requests",
        status_path="/status",
        desk=DESK,  # type: ignore[arg-type]
        query='"><img src=x>',
        results=[("a" * 24, hostile)],
    )
    assert "<script>x" not in html and "<img" not in html
    assert "&lt;script&gt;x&lt;/script&gt; by &quot;&amp;, from the album Album." in html
    assert "<h1>&lt;Owl &amp; Co&gt;</h1>" in html


def test_an_off_air_page_offers_no_form() -> None:
    html = render_request_page(
        "Night Owl Radio",
        page_path="/stations/nightowl/requests",
        status_path="/stations/nightowl/status",
        desk=None,
        off_air=True,
    )
    assert "<title>Off air: Night Owl Radio</title>" in html
    assert "<form" not in html


def test_work_waits_on_the_loop_and_says_busy_when_every_slot_is_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from autodj import request_page

    monkeypatch.setattr(request_page, "WORK_SLOTS", 2)
    monkeypatch.setattr(request_page, "WORK_WAIT_SECONDS", 0.05)

    async def scenario() -> None:
        release = threading.Event()
        threads_before = threading.active_count()
        # Two slow jobs take both slots; the third waits on the loop, not in
        # a thread, and gives up as busy.
        slow = [asyncio.ensure_future(request_page.bounded(release.wait, 5)) for _ in range(2)]
        await asyncio.sleep(0.01)
        with pytest.raises(request_page.Busy):
            await request_page.bounded(lambda: 1)
        assert threading.active_count() <= threads_before + 2
        release.set()
        await asyncio.gather(*slow)
        assert await request_page.bounded(lambda x: x + 1, 1) == 2

    asyncio.run(scenario())
