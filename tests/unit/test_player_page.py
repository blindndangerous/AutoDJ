"""The public player page and embeddable player: settings, HTML and policy (#52)."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from autodj.config import MAX_EMBED_ORIGINS, PlayerPageConfig, load_config
from autodj.player_page import (
    PLAYER_PAGE_CSP,
    PageAddresses,
    embed_csp,
    render_embed,
    render_player_page,
)

FEED = {
    "schema": 2,
    "station_name": "Night Owl Radio",
    "current_track": {"artist": "Artist", "title": "Song", "album": "Album"},
    "next_track": None,
    "listener_count": 1,
    "history": [
        {"artist": "Earlier", "title": "Before", "duration": 1.0, "played_at": "x"},
        {"artist": "", "title": "", "duration": 1.0, "played_at": "y"},
    ],
    "stream_path": "/nightowl.mp3",
}
OWL = PageAddresses.for_station("nightowl", by_name=True, streams=True, player_enabled=True)


class _Outline(HTMLParser):
    """Collect start tags with their attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def find(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


def _outline(html: str) -> _Outline:
    parser = _Outline()
    parser.feed(html)
    return parser


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_both_pages_are_off_by_default() -> None:
    settings = PlayerPageConfig()
    assert settings.enabled is False
    assert settings.embed is False
    assert settings.embed_origins == []


def test_embed_origins_are_checked_and_written_canonically() -> None:
    settings = PlayerPageConfig(
        embed=True,
        embed_origins=["https://Radio.Example.net:443", "http://blog.example.org:8080", "*"],
    )
    assert settings.embed_origins == [
        "https://radio.example.net",
        "http://blog.example.org:8080",
        "*",
    ]
    # The same site twice is kept once.
    twice = PlayerPageConfig(embed_origins=["https://a.example", "https://A.example/"])
    assert twice.embed_origins == ["https://a.example"]


@pytest.mark.parametrize(
    "origin",
    [
        "radio.example.net",
        "https://radio.example.net/player",
        "https://radio.example.net; script-src *",
        "https://user:pw@radio.example.net",
        "javascript:alert(1)",
        "https://*.example.net",
        "'self'",
        "",
    ],
)
def test_an_embed_origin_that_is_no_origin_is_refused(origin: str) -> None:
    with pytest.raises(ValueError, match=r"player_page\.embed_origins entry"):
        PlayerPageConfig(embed_origins=[origin])


def test_wrong_types_and_too_many_origins_are_refused() -> None:
    with pytest.raises(TypeError, match="enabled"):
        PlayerPageConfig(enabled="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="embed must"):
        PlayerPageConfig(embed=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="list of origins"):
        PlayerPageConfig(embed_origins="https://a.example")  # type: ignore[arg-type]
    many = [f"https://site{number}.example" for number in range(MAX_EMBED_ORIGINS + 1)]
    with pytest.raises(ValueError, match="at most"):
        PlayerPageConfig(embed_origins=many)


def test_each_station_has_its_own_player_page_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[library]
music_dir = "music"

[player_page]
enabled = true

[stations.nightowl.stream]
station_name = "Night Owl Radio"

[stations.nightowl.recording]
folder = "recordings/nightowl"

[stations.nightowl.player_page]
embed = true
embed_origins = ["https://nightowl.example"]
""",
        encoding="utf-8",
    )
    cfg = load_config(path, environ={})
    assert cfg.player_page.enabled is True
    assert cfg.player_page.embed is False
    (owl,) = cfg.stations
    # A station starts from the defaults, not from the root section.
    assert owl.cfg.player_page.enabled is False
    assert owl.cfg.player_page.embed is True
    assert owl.cfg.player_page.embed_origins == ["https://nightowl.example"]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_the_full_page_is_never_framed_and_the_embed_only_where_allowed() -> None:
    assert PLAYER_PAGE_CSP.endswith("frame-ancestors 'none'")
    for policy in (PLAYER_PAGE_CSP, embed_csp([])):
        assert "default-src 'none'" in policy
        assert "script-src 'self'" in policy
        assert "media-src 'self'" in policy
        assert "connect-src 'self'" in policy
        assert "form-action 'none'" in policy
        assert "unsafe" not in policy
    assert embed_csp([]).endswith("frame-ancestors 'self'")
    assert embed_csp(["https://a.example", "http://b.example:8080"]).endswith(
        "frame-ancestors 'self' https://a.example http://b.example:8080"
    )
    assert embed_csp(["https://a.example", "*"]).endswith("frame-ancestors *")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_addresses_by_name_and_for_the_default_station() -> None:
    assert OWL.feed == "/api/stations/nightowl/nowplaying"
    assert OWL.stream == "/nightowl.mp3"
    assert OWL.m3u == "/nightowl.m3u"
    assert OWL.playlist_name == "nightowl.m3u"
    assert OWL.status == "/stations/nightowl/status"
    assert OWL.player == "/stations/nightowl/player"
    default = PageAddresses.for_station(
        "default", by_name=False, streams=False, player_enabled=False
    )
    assert default.feed == "/api/nowplaying"
    assert default.stream is None
    assert default.status == "/status"
    assert default.player is None
    assert default.playlist_name == "autodj.m3u"


def test_the_player_page_has_labelled_native_controls_and_one_song_region() -> None:
    html = render_player_page(FEED, station="Night Owl Radio", addresses=OWL)
    outline = _outline(html)
    assert "<title>Night Owl Radio: listen live</title>" in html
    assert all("aria-labelledby" not in attrs for attrs in outline.find("section"))
    assert '<h2 id="player-listen-heading">Listen</h2>' in html
    (body,) = outline.find("body")
    assert body["data-page"] == "player"
    assert body["data-feed"] == "/api/stations/nightowl/nowplaying"
    assert body["data-stream"] == "/nightowl.mp3"
    (audio,) = outline.find("audio")
    # Nothing plays, or loads, until Play is pressed.
    assert audio["preload"] == "none"
    assert "autoplay" not in audio
    assert "controls" in audio  # the browser's own, without JavaScript
    (toggle,) = [b for b in outline.find("button") if b.get("id") == "player-toggle"]
    assert toggle["type"] == "button"
    assert "aria-pressed" not in toggle
    assert '<button type="button" id="player-toggle">Play</button>' in html
    (volume,) = outline.find("input")
    assert volume["type"] == "range"
    assert volume["id"] == "player-volume"
    assert '<label for="player-volume">Volume</label>' in html
    assert volume["aria-valuetext"] == "100%"
    # Shown instead of the slider where the browser fixes the volume (iOS).
    assert (
        '<p id="player-volume-note" hidden>The volume follows your device\'s own volume '
        "control.</p>"
    ) in html
    live = [attrs for _tag, attrs in outline.tags if "aria-live" in attrs]
    assert [attrs["id"] for attrs in live] == ["status-now"]
    assert [attrs.get("id") for _t, attrs in outline.tags if attrs.get("role") == "status"] == [
        "player-state"
    ]
    assert "Song by Artist, from the album Album." in html
    assert "<li>Before by Earlier.</li><li>Unknown title by an unknown artist.</li>" in html
    assert '<p id="player-history-empty" hidden>' in html
    assert '<section id="status-next-section" hidden>' in html
    assert '<a href="/nightowl.mp3">the stream</a>' in html
    assert 'download="nightowl.m3u"' in html
    assert '<a href="/stations/nightowl/status">Station status</a>' in html
    assert [attrs["href"] for attrs in outline.find("script") if "href" in attrs] == []
    assert [attrs["src"] for attrs in outline.find("script")] == [
        "/modules/theme-init.js",
        "/modules/player-page.js",
    ]


def test_the_player_page_escapes_station_and_song_text() -> None:
    hostile = {
        **FEED,
        "current_track": {"artist": "<script>x</script>", "title": '"&', "album": ""},
        "history": [{"artist": "<b>", "title": "<i>"}],
    }
    html = render_player_page(hostile, station="<Owl & Co>", addresses=OWL)
    assert "<script>x" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<title>&lt;Owl &amp; Co&gt;: listen live</title>" in html
    assert "<li>&lt;i&gt; by &lt;b&gt;.</li>" in html


def test_the_player_page_says_when_there_is_nothing_to_play() -> None:
    quiet = PageAddresses.for_station("default", by_name=False, streams=False, player_enabled=True)
    html = render_player_page(
        {**FEED, "history": [], "next_track": {"artist": "N", "title": "Soon"}},
        station="AutoDJ",
        addresses=quiet,
    )
    assert "AutoDJ is not streaming right now, so it cannot be played here." in html
    assert "<audio" not in html
    assert "data-stream" not in html
    assert '<ol id="player-history" hidden></ol>' in html
    assert '<p id="player-history-empty">No songs have finished playing yet.</p>' in html
    assert '<section id="status-next-section">' in html
    assert "Soon by N." in html


def test_an_off_air_player_page_keeps_its_controls() -> None:
    html = render_player_page(None, station="Night Owl Radio", addresses=OWL)
    assert "Night Owl Radio is off air right now." in html
    assert 'id="player-toggle"' in html
    assert '<ol id="player-history" hidden></ol>' in html


def test_the_embed_is_compact_and_pins_only_a_known_theme() -> None:
    html = render_embed(FEED, station="Night Owl Radio", addresses=OWL, theme="dark")
    outline = _outline(html)
    (root,) = outline.find("html")
    assert root["data-theme"] == "dark"
    (body,) = outline.find("body")
    assert body["data-page"] == "embed"
    assert body["class"] == "embed"
    assert "<title>Night Owl Radio player</title>" in html
    # The host's theme, not the web UI's stored choice.
    assert "theme-init.js" not in html
    assert '<label for="player-volume">Volume</label>' in html
    assert [attrs["id"] for _t, attrs in outline.tags if "aria-live" in attrs] == ["status-now"]
    (link,) = outline.find("a")
    assert link == {
        "href": "/stations/nightowl/player",
        "target": "_blank",
        "rel": "noopener",
    }
    assert "Open the full player (new tab)" in html
    # No download link or history in the compact version.
    assert "download=" not in html
    assert "player-history" not in html
    for theme in (None, "blue", '"><script>'):
        other = render_embed(FEED, station="Night Owl Radio", addresses=OWL, theme=theme)
        assert "data-theme" not in other
    alone = PageAddresses.for_station("nightowl", by_name=True, streams=True, player_enabled=False)
    assert "<a " not in render_embed(FEED, station="Night Owl Radio", addresses=alone)


def test_the_pages_carry_nothing_the_feed_does_not() -> None:
    private = {
        **FEED,
        "current_track": {**FEED["current_track"], "path": "Z:/Music/secret.flac"},
    }
    for html in (
        render_player_page(private, station="Night Owl Radio", addresses=OWL),
        render_embed(private, station="Night Owl Radio", addresses=OWL),
    ):
        assert "Z:/Music" not in html
        assert "secret" not in html
