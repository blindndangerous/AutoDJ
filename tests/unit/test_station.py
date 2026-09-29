"""Station lifecycle: idle, first listener, grace period, new set."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from autodj._bridge import PlayerBridge
from autodj.mixbus import MixBus, RenderedTrack
from autodj.player import Player, PlayerState
from autodj.station import Station
from tests.unit._fakes import make_cfg_mock, make_sim_index


class _Rig:
    """A station over a fake bus, stream and player."""

    def __init__(self, queue=None, queued_next=None, shuffle=True) -> None:
        self.now = 0.0
        self.bus = MagicMock()
        self.bus.playing = False
        self.bus.start_set.side_effect = lambda: setattr(self.bus, "playing", True)
        self.bus.stop_set.side_effect = lambda: setattr(self.bus, "playing", False)
        self.stream = MagicMock()
        self.stream.listener_count = 0
        self.player = MagicMock()
        self.player._state = PlayerState()
        self.player._state.queue = list(queue or [])
        self.player._state.queued_next = queued_next
        self.player._pop_user_queue.side_effect = lambda: Player._pop_user_queue(self.player)
        self.shuffle_pick = MagicMock(name="shuffle")
        self.player._random_start_entry.return_value = self.shuffle_pick if shuffle else None
        self.player.end_set.return_value = None  # the track a stop cut short
        self.events: list[str] = []
        self.forget = MagicMock()
        self.station = Station(
            self.bus,
            self.stream,
            self.player,
            idle_grace=30.0,
            clock=lambda: self.now,
            on_event=self.events.append,
            forget_track=self.forget,
        )

    def listeners(self, count: int) -> None:
        """Change the listener count and let the station's next tick see it."""
        self.stream.listener_count = count
        self.station.tick()


def test_starts_idle() -> None:
    rig = _Rig()
    assert rig.station.state == "idle"
    rig.bus.start_set.assert_not_called()


def test_first_listener_starts_set_from_queue_head() -> None:
    head, second = MagicMock(name="head"), MagicMock(name="second")
    rig = _Rig(queue=[head, second])
    rig.listeners(1)
    rig.player.begin_set.assert_called_once_with(head, "queue")
    assert rig.player._state.queue == [second]
    rig.bus.start_set.assert_called_once()
    assert rig.events == ["set_started"]
    assert rig.station.state == "playing"


def test_play_next_pick_comes_before_the_queue() -> None:
    head, chosen = MagicMock(name="head"), MagicMock(name="chosen")
    rig = _Rig(queue=[head], queued_next=chosen)
    rig.listeners(1)
    rig.player.begin_set.assert_called_once_with(chosen, "queue")
    assert rig.player._state.queued_next is None
    assert rig.player._state.queue == [head]


def test_first_listener_without_queue_uses_shuffle_pick() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.player.begin_set.assert_called_once_with(rig.shuffle_pick, "seed")


def test_set_is_started_before_the_bus_and_outside_the_queue_lock() -> None:
    rig = _Rig()
    order: list[str] = []
    lock = rig.player._state.queue_lock

    def begin(_entry, _mode) -> None:
        # An RLock owned by this thread would be re-acquirable; check it is free.
        acquired = lock.acquire(blocking=False)
        assert acquired
        lock.release()
        order.append("begin")

    rig.player.begin_set.side_effect = begin
    rig.bus.start_set.side_effect = lambda: order.append("start")
    rig.listeners(1)
    assert order == ["begin", "start"]


def test_more_listeners_do_not_restart_the_set() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.listeners(2)
    rig.listeners(3)
    assert rig.bus.start_set.call_count == 1


def test_empty_library_waits_and_a_later_tick_starts_the_set() -> None:
    rig = _Rig(shuffle=False)
    rig.listeners(1)
    rig.bus.start_set.assert_not_called()
    assert rig.station.state == "idle"
    rig.station.tick()  # still nothing to play: warned once, no retry storm
    rig.player._random_start_entry.return_value = rig.shuffle_pick
    rig.station.tick()
    rig.bus.start_set.assert_called_once()
    assert rig.events == ["set_started"]


def test_reconnect_within_grace_keeps_playing() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.listeners(0)
    rig.now = 20.0
    rig.station.tick()
    rig.listeners(1)
    rig.now = 100.0
    rig.station.tick()
    rig.bus.stop_set.assert_not_called()
    assert rig.events == ["set_started"]


def test_stops_after_grace_and_forgets_cut_short_track() -> None:
    rig = _Rig()
    rig.listeners(1)
    current = MagicMock(name="current")
    rig.player.end_set.return_value = current  # end_set reports what it cut short
    rig.now = 5.0
    rig.listeners(0)
    rig.now = 34.9
    rig.station.tick()
    rig.bus.stop_set.assert_not_called()
    rig.now = 35.0
    rig.station.tick()
    rig.bus.stop_set.assert_called_once()
    rig.player.end_set.assert_called_once()
    rig.forget.assert_called_once_with(current)
    assert rig.station.state == "idle"
    assert rig.events == ["set_started", "set_stopped"]


def test_stop_without_a_current_track_forgets_nothing() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.listeners(0)
    rig.now = 31.0
    rig.station.tick()
    rig.bus.stop_set.assert_called_once()
    rig.forget.assert_not_called()


def test_stop_calls_the_bus_before_parking_the_player() -> None:
    rig = _Rig()
    order: list[str] = []
    rig.bus.stop_set.side_effect = lambda: (
        order.append("stop"),
        setattr(rig.bus, "playing", False),
    )
    rig.player.end_set.side_effect = lambda: order.append("end")
    rig.listeners(1)
    rig.listeners(0)
    rig.now = 31.0
    rig.station.tick()
    assert order == ["stop", "end"]


def test_new_set_after_stop() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.listeners(0)
    rig.now = 31.0
    rig.station.tick()
    rig.listeners(1)
    assert rig.bus.start_set.call_count == 2
    assert rig.events == ["set_started", "set_stopped", "set_started"]


def test_idle_station_ticks_do_nothing() -> None:
    rig = _Rig()
    rig.now = 1000.0
    rig.station.tick()
    rig.bus.stop_set.assert_not_called()
    rig.bus.start_set.assert_not_called()
    assert rig.events == []


def test_paused_set_is_not_stopped_by_idle() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.player._state.is_paused = True
    rig.listeners(0)
    rig.now = 500.0
    rig.station.tick()
    rig.bus.stop_set.assert_not_called()
    assert rig.station.state == "paused"


def test_grace_restarts_when_a_paused_set_resumes() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.player._state.is_paused = True
    rig.listeners(0)
    rig.now = 500.0
    rig.station.tick()
    rig.player._state.is_paused = False
    rig.now = 529.0
    rig.station.tick()
    rig.bus.stop_set.assert_not_called()
    rig.now = 530.0
    rig.station.tick()
    rig.bus.stop_set.assert_called_once()


def test_grace_starts_at_the_first_empty_tick() -> None:
    rig = _Rig()
    rig.listeners(1)
    rig.bus.start_set.assert_called_once()
    rig.now = 10.0
    rig.listeners(0)  # grace starts now
    rig.now = 39.0
    rig.station.tick()
    rig.bus.stop_set.assert_not_called()
    rig.now = 40.0
    rig.station.tick()
    rig.bus.stop_set.assert_called_once()


def test_defaults_need_no_callbacks() -> None:
    bus = MagicMock()
    bus.playing = False
    bus.start_set.side_effect = lambda: setattr(bus, "playing", True)
    bus.stop_set.side_effect = lambda: setattr(bus, "playing", False)
    player = MagicMock()
    player._state = PlayerState()
    player._state.current_track = MagicMock()
    stream = MagicMock()
    stream.listener_count = 0
    now = [0.0]
    station = Station(bus, stream, player, idle_grace=1.0, clock=lambda: now[0])
    stream.listener_count = 1
    station.tick()
    stream.listener_count = 0
    station.tick()
    now[0] = 2.0
    station.tick()
    assert station.state == "idle"


# -- a real player, bridge and mix bus ---------------------------------------

WAIT = 2.0
FRAMES = MixBus.BLOCK * 20


def _real_rig():
    sim = make_sim_index(12)
    player = Player(make_cfg_mock(), sim, stream_mode=True)

    def fake_render(current, nxt, offset):
        return RenderedTrack(
            current, np.full((FRAMES, 2), 0.1, np.float32), nxt, 0, "", start_offset=offset
        )

    player._render_track = fake_render  # type: ignore[method-assign]
    player.load_lyrics_in_background = MagicMock()  # type: ignore[method-assign]
    bridge = PlayerBridge(player=player, sim=sim)
    player.on_track_started = bridge.on_track_started
    stream = MagicMock()
    stream.listener_count = 0
    now = [0.0]
    station = Station(
        player.bus,
        stream,
        player,
        idle_grace=30.0,
        clock=lambda: now[0],
        forget_track=bridge.forget_track,
    )
    return player, bridge, stream, station, now, sim


def _play_until_a_track_starts(player: Player) -> None:
    before = player._playing_render
    for _ in range(400):
        if player.bus._current is None:
            assert player._render_ahead.wait_ready(WAIT)
        player.bus.render_block()
        if player._playing_render is not before:
            return
    raise AssertionError("no track started")


def test_real_player_first_listener_plays_queue_head_then_stops_cleanly() -> None:
    player, bridge, stream, station, now, sim = _real_rig()
    head = sim.entries[5]
    player._state.queue.append(head)
    player._render_ahead.start()
    try:
        stream.listener_count = 1
        station.tick()
        _play_until_a_track_starts(player)
        assert player._state.current_track.path == head.path
        assert player._state.queue == []
        assert player._seed_path == head.path
        assert [row["title"] for row in bridge.history_snapshot()] == [head.title]

        stream.listener_count = 0
        station.tick()
        now[0] = 31.0
        station.tick()
        assert station.state == "idle"
        assert not player.bus.playing
        assert player._state.current_track is None
        assert player._state.next_track is None
        assert bridge.history_snapshot() == []  # the cut-short track is forgotten
        # The worker is parked: nothing stale is (or gets) rendered for the next set.
        assert not player._render_ahead.wait_ready(0.3)

        stream.listener_count = 1
        station.tick()
        _play_until_a_track_starts(player)
        assert player._state.current_track is not None
        assert station.state == "playing"
    finally:
        player._render_ahead.stop(timeout=WAIT)


def test_start_with_sets_the_next_sets_first_track_while_idle() -> None:
    head, chosen = MagicMock(name="head"), MagicMock(name="chosen")
    rig = _Rig(queue=[head], queued_next=MagicMock(name="play-next"))
    assert rig.station.start_with(chosen, "seed") is True
    rig.bus.start_set.assert_not_called()  # it waits for a listener
    rig.listeners(1)
    rig.player.begin_set.assert_called_once_with(chosen, "seed")
    assert rig.player._state.queue == [head]  # the queue is left alone
    assert rig.player._state.queued_next is not None


def test_start_with_is_used_once() -> None:
    rig = _Rig()
    chosen = MagicMock(name="chosen")
    rig.station.start_with(chosen, "queue")
    rig.listeners(1)
    rig.listeners(0)
    rig.now = 31.0
    rig.station.tick()
    rig.listeners(1)
    assert rig.player.begin_set.call_args_list[-1].args == (rig.shuffle_pick, "seed")


def test_start_with_is_refused_while_a_set_plays() -> None:
    rig = _Rig()
    rig.listeners(1)
    assert rig.station.start_with(MagicMock(name="late"), "queue") is False
    rig.listeners(0)
    rig.now = 31.0
    rig.station.tick()
    rig.listeners(1)
    assert rig.player.begin_set.call_args_list[-1].args == (rig.shuffle_pick, "seed")


def test_real_player_ignores_a_track_start_left_over_from_a_stopped_set() -> None:
    player, bridge, stream, station, now, _sim = _real_rig()
    player._render_ahead.start()
    try:
        stream.listener_count = 1
        station.tick()
        _play_until_a_track_starts(player)
        assert player._render_ahead.wait_ready(WAIT)
        leftover = player._take_render()  # the bus took it just before stopping
        assert leftover is not None
        numbered = player._state.track_number
        stream.listener_count = 0
        station.tick()
        now[0] = 31.0
        station.tick()
        player._on_track_start(leftover)  # its callback runs after the stop
        assert player._state.current_track is None
        assert player._state.track_number == numbered
        assert bridge.history_snapshot() == []
    finally:
        player._render_ahead.stop(timeout=WAIT)
