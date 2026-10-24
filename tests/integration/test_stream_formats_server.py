"""The AAC and Opus addresses, their settings and the listener statistics routes (#50)."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from autodj.stream import EncoderUnavailableError
from autodj.stream_formats import StreamFormat
from autodj.stream_mounts import StreamMounts
from tests._layout import point_index
from tests._server import create_app
from tests.integration._helpers import patch_station_settings


def _output(first: bytes, bitrate: int = 320) -> MagicMock:
    output = MagicMock()
    output.listener_count = 0
    output.bitrate = bitrate
    listener = MagicMock()
    listener.closed = False

    async def chunks():
        yield first

    listener.chunks = chunks
    output.add_listener.return_value = listener
    output.remove_listener.side_effect = lambda target: setattr(target, "closed", True)
    return output


class Rig:
    def __init__(self, bridge, tmp_path: Path) -> None:
        self.bridge = bridge
        self.main = _output(b"\xff\xfb\x90\x00" + b"\x00" * 413)
        self.made: dict[str, MagicMock] = {}
        self.fail = False  # ffmpeg cannot encode the format (the probe says so)
        self.fail_start = False  # the encoder cannot start for a listener
        self.mounts = StreamMounts(
            self.main, self.make, lambda _o: None, lambda _o: None, probe=self.probe
        )
        cfg = bridge.player._cfg
        point_index(cfg.index, tmp_path)
        cfg.station.output = "stream"
        bridge.attach_stream(stream=self.main, station=MagicMock(state="idle"), mounts=self.mounts)
        bridge.save_persistent_state = MagicMock()
        self.client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))

    def probe(self, fmt: StreamFormat, bitrate: int) -> None:
        if self.fail:
            raise EncoderUnavailableError(f"ffmpeg could not encode {fmt.label}: Unknown encoder")

    def make(self, fmt: StreamFormat, bitrate: int) -> MagicMock:
        if self.fail_start:
            raise OSError("ffmpeg vanished")
        first = b"OggS" if fmt.paged else b"\xff\xf1"
        output = self.made[fmt.name] = _output(first, bitrate)
        return output


@pytest.fixture
def rig(bridge, tmp_path: Path) -> Rig:
    return Rig(bridge, tmp_path)


@pytest.mark.parametrize("path", ["/stream.aac", "/stream.opus", "/default.opus"])
def test_a_format_that_is_off_is_not_found(rig: Rig, path: str) -> None:
    assert rig.client.get(path).status_code == 404
    assert rig.client.head(path).status_code == 404


def test_the_aac_stream_carries_titles_for_players_that_ask(rig: Rig) -> None:
    rig.mounts.set_bitrate("aac", 96)
    for path in ("/stream.aac", "/default.aac"):
        head = rig.client.head(path, headers={"Icy-MetaData": "1"})
        assert head.status_code == 200 and head.headers["content-type"] == "audio/aac"
        # Asking about it starts no encoder; only a listener does.
        assert path != "/stream.aac" or "aac" not in rig.made
        with rig.client.stream("GET", path, headers={"Icy-MetaData": "1"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/aac"
            assert response.headers["icy-br"] == "96"
            assert "icy-metaint" in response.headers
            assert next(response.iter_bytes()) == b"\xff\xf1"
    aac = rig.made["aac"]
    aac.add_listener.assert_called_with(icy=True)
    rig.main.add_listener.assert_not_called()


def test_the_opus_stream_never_offers_icy_titles(rig: Rig) -> None:
    rig.mounts.set_bitrate("opus", 64)
    with rig.client.stream("GET", "/stream.opus", headers={"Icy-MetaData": "1"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/ogg"
        assert "icy-metaint" not in response.headers
        assert next(response.iter_bytes()) == b"OggS"
    rig.made["opus"].add_listener.assert_called_once_with(icy=False)


def test_the_other_formats_need_no_pairing(bridge, tmp_path: Path) -> None:
    from autodj.config import ServerConfig

    bridge.player._cfg.server = ServerConfig(
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        access_token="t" * 32,
    )
    rig = Rig(bridge, tmp_path)
    rig.mounts.set_bitrate("aac", 96)
    assert rig.client.head("/stream.aac").status_code == 200
    # Off is "not found", never "sign in".
    assert rig.client.head("/stream.opus").status_code == 404
    assert rig.client.get("/api/stream/stats").status_code == 401
    assert rig.client.get("/api/stream/stats/export").status_code == 401
    assert (
        rig.client.patch(
            "/api/settings",
            json={"revision": "not-used", "changes": {"stream": {"aac_bitrate": 0}}},
        ).status_code
        == 401
    )


def test_listening_is_counted_in_the_statistics(rig: Rig) -> None:
    rig.mounts.set_bitrate("aac", 96)
    rig.mounts.acquire("aac")
    # What the outputs report as connected now (the feed reads it at each release).
    rig.made["aac"].listener_count = 2
    rig.main.listener_count = 1
    with rig.client.stream("GET", "/stream.aac") as response:
        next(response.iter_bytes())
    with rig.client.stream("GET", "/stream.mp3") as response:
        next(response.iter_bytes())
    summary = rig.client.get("/api/stream/stats", params={"days": 7}).json()
    assert summary["now"] == {"total": 3, "formats": {"mp3": 1, "aac": 2}}
    [today] = summary["days"]
    assert today["sessions"] == 2 and today["peak"] == 1
    assert set(today["formats"]) == {"aac", "mp3"}
    assert rig.client.get("/api/stream/stats", params={"days": 0}).status_code == 422
    export = rig.client.get("/api/stream/stats/export")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert export.headers["cache-control"] == "no-store"
    assert 'filename="autodj-listeners-default.csv"' in export.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert rows[0]["sessions"] == "2" and rows[0]["aac_sessions"] == "1"
    # The page and the feed count every format.
    assert rig.client.get("/api/stream").json()["listeners"] == 3
    assert rig.client.get("/api/nowplaying").json()["listener_count"] == 3


def test_formats_are_turned_on_changed_and_off_from_settings(rig: Rig) -> None:
    cfg = rig.bridge.player._cfg
    assert patch_station_settings(rig.client, {"stream": {"aac_bitrate": 128}}).status_code == 200
    info = rig.client.get("/api/stream").json()
    assert cfg.stream.aac_bitrate == 128
    aac = next(item for item in info["formats"] if item["format"] == "aac")
    assert aac == {
        "format": "aac",
        "label": "AAC",
        "bitrate": 128,
        "path": "/stream.aac",
        "listeners": 0,
        "error": None,
    }
    patch_station_settings(rig.client, {"stream": {"opus_bitrate": 48, "aac_bitrate": 0}})
    assert rig.mounts.get("aac") is None and rig.mounts.bitrate("opus") == 48
    assert (cfg.stream.aac_bitrate, cfg.stream.opus_bitrate) == (0, 48)
    assert rig.bridge.save_persistent_state.called
    saved = rig.bridge.get_settings()["playback"]
    assert (saved["stream_aac_bitrate"], saved["stream_opus_bitrate"]) == (0, 48)
    # Only the listed bitrates; the MP3 one is not an AAC one.
    for body in ({"aac_bitrate": 320}, {"opus_bitrate": 32}, {"opus_bitrate": True}):
        assert patch_station_settings(rig.client, {"stream": body}).status_code == 422


def test_a_format_ffmpeg_cannot_encode_is_refused_and_nothing_changes(rig: Rig) -> None:
    rig.mounts.set_bitrate("aac", 96)
    rig.fail = True
    response = patch_station_settings(
        rig.client, {"stream": {"opus_bitrate": 64, "aac_bitrate": 0, "bitrate": 192}}
    )
    assert response.status_code == 207
    assert "ffmpeg could not encode Opus" in response.json()["failed"]["stream"]
    assert not rig.mounts.enabled("opus") and rig.mounts.bitrate("aac") == 96
    assert rig.bridge.player._cfg.stream.opus_bitrate == 0
    assert rig.bridge.player._cfg.stream.bitrate == 320
    rig.main.set_bitrate.assert_not_called()


def test_an_mp3_failure_after_the_formats_changed_says_what_was_saved(rig: Rig) -> None:
    rig.main.set_bitrate.side_effect = EncoderUnavailableError("stream encoder failed")
    response = patch_station_settings(rig.client, {"stream": {"opus_bitrate": 64, "bitrate": 192}})
    assert response.status_code == 207
    assert "Opus change was saved" in response.json()["failed"]["stream"]
    assert rig.mounts.bitrate("opus") == 64
    assert rig.bridge.save_persistent_state.called


def test_a_listener_whose_encoder_cannot_start_is_told_so(rig: Rig) -> None:
    rig.mounts.set_bitrate("opus", 64)
    rig.fail_start = True
    response = rig.client.get("/stream.opus")
    assert response.status_code == 503
    assert response.json()["detail"] == "stream encoder failed"


def test_a_stopped_stream_refuses_format_changes(rig: Rig) -> None:
    rig.mounts.close()
    response = patch_station_settings(rig.client, {"stream": {"aac_bitrate": 96}})
    assert response.status_code == 207
    assert "stream has stopped" in response.json()["failed"]["stream"].lower()
    assert rig.client.get("/stream.aac").status_code == 404


def test_a_stream_without_mounts_refuses_format_changes(bridge, tmp_path: Path) -> None:
    rig = Rig(bridge, tmp_path)
    bridge.mounts = None
    response = patch_station_settings(rig.client, {"stream": {"aac_bitrate": 96}})
    assert response.status_code == 207
    assert "no extra formats" in response.json()["failed"]["stream"].lower()
    # The statistics still work: they count the MP3 stream only.
    rig.main.listener_count = 2
    assert rig.client.get("/api/stream/stats").json()["now"] == {
        "total": 2,
        "formats": {"mp3": 2},
    }


def test_keeping_fewer_days_deletes_the_older_ones_now(rig: Rig) -> None:
    stats_path = (
        Path((rig.bridge.player._cfg.index.stations_dir / "default")) / "listener_stats.json"
    )
    with rig.client.stream("GET", "/stream.mp3") as response:
        next(response.iter_bytes())
    assert patch_station_settings(rig.client, {"stream": {"stats_days": 0}}).status_code == 200
    assert rig.bridge.player._cfg.stream.stats_days == 0
    assert '"days": []' in stats_path.read_text(encoding="utf-8")
    assert rig.client.get("/api/stream").json()["stats_days"] == 0
    for days in (-1, 3651, 1.5):
        assert (
            patch_station_settings(rig.client, {"stream": {"stats_days": days}}).status_code == 422
        )


def test_the_station_starts_and_stops_its_extra_formats_with_the_mp3_stream() -> None:
    from unittest.mock import patch

    from autodj._bridge import PlayerBridge
    from autodj.stream import StreamOutput
    from autodj.stream_formats import AAC, OPUS
    from tests.integration._helpers import _make_player_mock, _make_sim_mock
    from tests.unit._fakes import FakeEncoder

    made: list[StreamFormat] = []

    class Encoder(FakeEncoder):
        def __init__(self, bitrate: int, fmt: StreamFormat | None = None) -> None:
            super().__init__(bitrate)
            if fmt is not None:
                made.append(fmt)

    player = _make_player_mock()
    player.bus = MagicMock()
    player.bus.playing = False
    player._cfg.stream.aac_bitrate = 96
    player._cfg.stream.opus_bitrate = 64
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    removed: list[object] = []
    player.bus.remove_output.side_effect = removed.append
    probed: list[tuple[StreamFormat, int]] = []
    with (
        patch("autodj.stream.FfmpegEncoder", Encoder),
        patch("autodj.stream.probe_encoder", lambda fmt, bitrate: probed.append((fmt, bitrate))),
        TestClient(create_app(bridge, stream_mode=True)) as client,
    ):
        mounts = bridge.mounts
        # Both are on and checked, but no encoder runs until someone listens.
        assert probed == [(AAC, 96), (OPUS, 64)]
        assert made == [] and mounts.get("aac") is None
        aac, opus = bridge.stream_output("aac"), bridge.stream_output("opus")
        assert isinstance(aac, StreamOutput) and aac.format is AAC and aac.bitrate == 96
        assert isinstance(opus, StreamOutput) and opus.format is OPUS
        assert made == [AAC, OPUS]
        added = [call.args[0] for call in player.bus.add_output.call_args_list]
        assert added == [bridge.stream, aac, opus]
        # The station plays for a listener on any format.
        assert bridge.station._stream is mounts
        info = client.get("/api/stream").json()
        assert [(f["format"], f["bitrate"]) for f in info["formats"]] == [
            ("mp3", 320),
            ("aac", 96),
            ("opus", 64),
        ]
        bridge.on_track_started(player._state.current_track)
    assert removed[:2] == [aac, opus] and removed[-1] is not None
    assert mounts.outputs == []
