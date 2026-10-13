"""The public status page's HTML: words, escaping and page structure."""

from __future__ import annotations

from html.parser import HTMLParser

from autodj.status_page import (
    NOTHING_PLAYING,
    OFF_AIR,
    listeners_text,
    off_air_text,
    render_off_air_page,
    render_status_page,
    track_text,
    uptime_text,
)

FEED = {
    "schema": 2,
    "station_name": "Radio",
    "current_track": {"artist": "Artist", "title": "Song", "album": "Album"},
    "next_track": None,
    "listener_count": 1,
    "peak_listeners_today": 4,
    "uptime_seconds": 2 * 86400 + 3 * 3600 + 60,
    "history": [],
    "stream_path": "/stream.mp3",
}


class _Outline(HTMLParser):
    """Collect what a screen reader navigates by."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _outline(html: str) -> _Outline:
    parser = _Outline()
    parser.feed(html)
    return parser


def test_words_match_the_page_script() -> None:
    # tests/jsmodules/status-page.test.js checks the same cases in JS.
    assert track_text(FEED["current_track"], empty="") == "Song by Artist, from the album Album."
    assert track_text({"title": " ", "artist": ""}, empty="") == (
        "Unknown title by an unknown artist."
    )
    assert track_text(None, empty=NOTHING_PLAYING) == NOTHING_PLAYING
    assert listeners_text(1) == "1 listener"
    assert listeners_text(0) == "0 listeners"
    assert listeners_text("bad") == "0 listeners"
    assert uptime_text(59) == "less than a minute"
    assert uptime_text(60) == "1 minute"
    assert uptime_text(2 * 86400 + 3 * 3600 + 60) == "2 days, 3 hours, 1 minute"
    assert uptime_text(3600) == "1 hour"
    assert uptime_text(None) == "less than a minute"


def test_page_has_language_title_landmarks_and_headings() -> None:
    html = render_status_page(FEED, stream_mode=False)
    outline = _outline(html)
    tags = [tag for tag, _attrs in outline.tags]
    assert ("html", {"lang": "en"}) in outline.tags
    assert "<title>Radio: station status</title>" in html
    for landmark in ("header", "main", "footer"):
        assert tags.count(landmark) == 1
    # Unnamed sections: headings, not extra region landmarks.
    assert not any("aria-labelledby" in attrs for _tag, attrs in outline.tags)
    headings = [tag for tag in tags if tag in {"h1", "h2", "h3"}]
    assert headings[0] == "h1" and headings.count("h1") == 1
    assert "h3" not in headings
    # Exactly one live region: the now-playing line, polite and atomic.
    live = [attrs for _tag, attrs in outline.tags if "aria-live" in attrs]
    assert live == [
        {"id": "status-now", "aria-live": "polite", "aria-atomic": "true"},
    ]
    assert not any("role" in attrs for _tag, attrs in outline.tags)
    assert "Song by Artist, from the album Album." in html
    assert "1 listener<" in html and "4 listeners<" in html
    assert "2 days, 3 hours, 1 minute" in html
    # Works without JavaScript; the script only keeps it current.
    assert "Reload the page for the latest." in html
    assert '<script type="module" src="/modules/status-page.js"></script>' in html
    # The web UI's Theme choice, applied before the stylesheet paints.
    head = html[: html.index("</head>")]
    assert head.index('<script src="/modules/theme-init.js"></script>') < head.index(
        'href="/modules/status-page.css"'
    )
    # Pause updates (WCAG 2.2.2) appears only once the script can use it.
    assert (
        '<button type="button" id="status-pause" aria-pressed="false" hidden>Pause updates</button>'
        in html
    )


def test_next_song_and_listen_links_show_only_when_they_apply() -> None:
    html = render_status_page(FEED, stream_mode=False)
    assert '<section id="status-next-section" hidden>' in html
    assert "/stream.mp3" not in html
    shown = render_status_page(
        {**FEED, "next_track": {"artist": "B", "title": "Next", "album": ""}}, stream_mode=True
    )
    assert '<section id="status-next-section">' in shown
    assert "Next by B." in shown
    assert '<a href="/stream.mp3">Listen to the stream</a>' in shown


def test_untrusted_text_is_escaped() -> None:
    html = render_status_page(
        {
            **FEED,
            "station_name": "<script>alert(1)</script>",
            "current_track": {"artist": '"><img src=x>', "title": "A & B", "album": ""},
        },
        stream_mode=False,
    )
    assert "<script>alert" not in html and "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "A &amp; B by &quot;&gt;&lt;img src=x&gt;." in html


def test_missing_fields_fall_back_to_plain_words() -> None:
    html = render_status_page({}, stream_mode=False)
    assert "<title>AutoDJ: station status</title>" in html
    assert NOTHING_PLAYING in html
    assert "0 listeners" in html
    assert "less than a minute" in html


def test_the_default_station_keeps_its_addresses() -> None:
    html = render_status_page(FEED, stream_mode=True)
    assert '<body data-feed="/api/nowplaying">' in html
    assert '<a href="/stream.m3u" download="autodj.m3u">' in html
    assert '<a href="/api/nowplaying">The same details as JSON</a>' in html


def test_another_station_page_names_its_own_feed_and_stream() -> None:
    html = render_status_page(
        {**FEED, "station_name": "Night Owl Radio"},
        stream_mode=True,
        feed_url="/api/stations/nightowl/nowplaying",
        stream_path="/stations/nightowl.mp3",
        m3u_path="/stations/nightowl.m3u",
        playlist_name="nightowl.m3u",
    )
    assert "<title>Night Owl Radio: station status</title>" in html
    assert '<body data-feed="/api/stations/nightowl/nowplaying">' in html
    assert '<a href="/stations/nightowl.mp3">Listen to the stream</a>' in html
    assert '<a href="/stations/nightowl.m3u" download="nightowl.m3u">' in html
    assert '<a href="/api/stations/nightowl/nowplaying">The same details as JSON</a>' in html
    assert "/stream.mp3" not in html and '"/api/nowplaying"' not in html


def test_a_stopped_station_page_says_it_is_off_air_with_the_same_parts() -> None:
    html = render_off_air_page(
        "Night Owl Radio",
        feed_url="/api/stations/nightowl/nowplaying",
        stream_path="/stations/nightowl.mp3",
        m3u_path="/stations/nightowl.m3u",
        playlist_name="nightowl.m3u",
    )
    running = render_status_page(FEED, stream_mode=True)
    ids = [attrs.get("id") for _tag, attrs in _outline(html).tags if attrs.get("id")]
    # The script fills in the same elements when the station starts again.
    assert ids == [attrs.get("id") for _tag, attrs in _outline(running).tags if attrs.get("id")]
    assert off_air_text("Night Owl Radio") == "Night Owl Radio is off air right now."
    assert (
        '<p id="status-now" aria-live="polite" aria-atomic="true">'
        "Night Owl Radio is off air right now.</p>"
    ) in html
    assert '<section id="status-next-section" hidden>' in html
    assert f'<dd id="status-uptime">{OFF_AIR}</dd>' in html
    assert '<dd id="status-listeners">0 listeners</dd>' in html
    assert '<body data-feed="/api/stations/nightowl/nowplaying">' in html
    escaped = render_off_air_page(
        "<b>", feed_url='/x"y', stream_path="/s.mp3", m3u_path="/s.m3u", playlist_name="s.m3u"
    )
    assert "<b>" not in escaped and 'data-feed="/x&quot;y"' in escaped
