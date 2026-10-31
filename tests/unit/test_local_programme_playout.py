"""Local performance reserves the existing queue and accounts only heard audio."""

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autodj.config import AdsConfig, BreaksConfig, PlaybackConfig, TimeChecksConfig
from autodj.live import LiveDesk
from autodj.local_programme import LocalProgramme, OwnershipError
from autodj.local_programme_playout import LocalPerformance, Occurrence


def fixture(tmp_path):
    songs = []
    for name in ("a", "b", "c"):
        path = tmp_path / f"{name}.wav"
        path.write_bytes(b"song")
        songs.append(SimpleNamespace(path=str(path), title=name, artist="", length=10))
    state = SimpleNamespace(
        queue_lock=threading.RLock(),
        queue=songs[1:],
        queued_next=None,
        current_track=songs[0],
        next_track=songs[1],
        is_paused=False,
    )
    player = SimpleNamespace(
        _state=state,
        _cfg=SimpleNamespace(
            playback=PlaybackConfig(),
            breaks=BreaksConfig(),
            ads=AdsConfig(),
            time_checks=TimeChecksConfig(),
        ),
        bus=Mock(),
        _prepared_plan=lambda: None,
        _pick_next=lambda previous: songs[0],
        _pick_context=lambda **kwargs: None,
        _choose_next=lambda previous, context: (songs[0], "random"),
        prepare_local_return=Mock(return_value=True),
    )
    bridge = SimpleNamespace(
        player=player,
        _is_liner=lambda entry: False,
        local_occurrence_started=Mock(),
        local_occurrence_ended=Mock(),
        _ledger=lambda: None,
    )
    station = SimpleNamespace(
        player=player,
        bridge=bridge,
        stopped=False,
        schedule=SimpleNamespace(events={}, pending=[], _tasks=set()),
        intros=SimpleNamespace(all=lambda: {}),
    )
    controller = LocalProgramme(LiveDesk(tmp_path / "djs.json"))
    station.local_programme = controller
    host = LocalPerformance(station)
    controller.performance = host
    return host, controller, station, songs


def test_reservations_do_not_dequeue_and_consumption_counts_once(tmp_path):
    """ADR-0020-06: one queue, two occurrences, durable idempotent consumption."""
    host, controller, station, songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    views = host.prepare(grant)
    assert len(views) == 2
    assert station.player._state.queue == songs[1:]
    audio = controller.audio
    event = {"sequence": 0, "kind": "started", "occurrence": views[0]["id"], "frame": 0}
    host.marker(event)
    assert station.bridge.local_occurrence_started.call_count == 0
    audio.events.consume([(0, 441)])
    host.drain()
    host.drain()
    assert station.player._state.queue == [songs[2]]
    assert station.player._state.next_track is songs[2]
    station.bridge.local_occurrence_started.assert_called_once_with(songs[1], views[0]["id"])
    assert audio.events.submit(event, 441) == "completed"


def test_recovery_preserves_unheard_next_and_blocks_claim_on_ledger_failure(tmp_path):
    """ADR-0020-07: unstarted reservations survive, heard audio never replays."""
    host, controller, station, songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    views = host.prepare(grant)
    host.marker({"sequence": 0, "kind": "started", "occurrence": views[0]["id"], "frame": 0})
    controller.audio.events.consume([(0, 441)])
    host.on_air = True
    ledger = Mock()
    ledger.flush.side_effect = OSError("disk unavailable")
    station.bridge._ledger = lambda: ledger
    controller.revoke(grant.generation, "lost")
    with pytest.raises(OSError):
        host.recover()
    with pytest.raises(OwnershipError):
        controller.claim("user", "other", 1)
    ledger.flush.side_effect = None
    host.recover()
    station.player.prepare_local_return.assert_called_once_with(songs[2], timeout=2.0)
    assert station.bridge.local_occurrence_started.call_count == 1


def test_unsupported_features_and_changed_source_fail_closed(tmp_path):
    """ADR-0020-07: scheduled insert features and replaced sources cannot enter local air."""
    host, controller, station, songs = fixture(tmp_path)
    station.player._cfg.ads.enabled = True
    with pytest.raises(OwnershipError, match="Ads"):
        host.admit()
    station.player._cfg.ads.enabled = False
    grant = controller.claim("user", "socket", 0)
    host.prepare(grant)
    Path(songs[1].path).write_bytes(b"changed")
    with pytest.raises(OwnershipError, match="source"):
        host.validate_reservations()


@pytest.mark.parametrize(
    "name,args",
    [
        ("skip", ()),
        ("advance_now", ()),
        ("repick_next", ()),
        ("pause", ()),
        ("set_transition", ("none",)),
    ],
)
def test_internal_bridge_mutations_are_fenced(name, args):
    """ADR-0020-01: an internal caller cannot race browser-owned programming."""
    from autodj._bridge import PlayerBridge

    bridge = object.__new__(PlayerBridge)
    bridge.player = SimpleNamespace(local_performance=SimpleNamespace(busy=True))
    with pytest.raises(OwnershipError, match="Return to automation"):
        getattr(bridge, name)(*args)


def test_queue_edits_preserve_reserved_prefix_but_allow_unreserved_tail(tmp_path):
    """ADR-0020-06: queue edits cannot replace either reserved occurrence."""
    host, controller, _station, songs = fixture(tmp_path)
    host.prepare(controller.claim("user", "socket", 0))
    host.validate_queue(None, [songs[1], songs[2], songs[0]])
    with pytest.raises(OwnershipError):
        host.validate_queue(None, [songs[2], songs[1]])


def test_automation_advance_invalidates_preparation_even_for_identical_queue_objects(tmp_path):
    """ADR-0020-01: render generation fences a queue of repeated identical entries."""
    host, controller, station, songs = fixture(tmp_path)
    station.player._state.queue = [songs[1]] * 3
    host.prepare(controller.claim("user", "socket", 0))
    station.player._state.queue.pop(0)
    station.player._local_render_generation = 1
    with pytest.raises(OwnershipError, match="Automation advanced"):
        host.validate_reservations()


def test_loss_keeps_live_reservation_until_recovery_finishes(tmp_path):
    """ADR-0020-01: external inputs cannot acquire during local recovery."""
    host, controller, _station, _songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    host.prepare(grant)
    controller.revoke(grant.generation, "lost")
    assert not controller.live.reserve_local(object())
    host.recover()
    assert controller.live.reserve_local(object())


@pytest.mark.parametrize(
    "name,args", [("set_transition", ("none",)), ("set_eq", ()), ("set_playback_settings", (None,))]
)
def test_settings_owner_cannot_bypass_programming_fence(name, args):
    """ADR-0020-01: internal settings callers use the same programming fence."""
    from autodj.station_settings import StationSettings

    settings = object.__new__(StationSettings)
    settings.player = SimpleNamespace(local_performance=SimpleNamespace(busy=True))
    with pytest.raises(OwnershipError, match="Return to automation"):
        getattr(settings, name)(*args)


def real_player_recovery(tmp_path):
    """Stage a lost local feed, then use the production recovery coordinator."""
    from tests.unit.test_local_programme_audio import packet
    from tests.unit.test_player_stream_mode import _player

    host, controller, station, _songs = fixture(tmp_path)
    host.player = station.player = _player(stream_mode=True)
    player = host.player
    player._cfg.playback.liners_enabled = False
    player._cfg.breaks.enabled = False
    player._cfg.ads.enabled = False
    player._cfg.time_checks.enabled = False
    entry = player._sim.entries[0]
    player._state.queue = [entry]
    grant = controller.claim("user", "socket", 0)
    audio = host.audio = controller.audio
    audio.arm()
    for sequence in range(6):
        audio.accept_packet(grant, packet(sequence, generation=grant.generation), now=0)
    host.occurrences = [Occurrence("unheard", entry, (0, 0), True)]
    host.on_air = True
    player.bus.take_local_programme(audio)
    controller.revoke(grant.generation, "lost")
    return host, player, entry, audio


def test_real_player_return_keeps_queue_occurrence_until_consumption(tmp_path):
    """ADR-0020-07: returning an unheard queued song keeps its queue provenance."""
    host, player, entry, _audio = real_player_recovery(tmp_path)
    player._playout.wait_rendered = Mock(return_value=True)
    host.recover()
    assert player.bus._local_programme is None
    assert not host.busy
    assert player._pending_from_queue is True
    assert player._state.queue == [entry]


def test_real_player_reservation_never_pops_the_only_queued_song(tmp_path):
    """ADR-0020-06: selecting the second reservation cannot consume the first."""
    from autodj.local_programme_audio import LocalPCM
    from tests.unit.test_player_stream_mode import _player

    host, controller, station, _songs = fixture(tmp_path)
    host.player = station.player = _player(stream_mode=True)
    entries = host.player._sim.entries
    host.player._state.current_track = entries[0]
    host.player._state.queue = [entries[1]]
    host._signature = lambda entry: (0, 0)
    host.admit = lambda: None
    host.audio = LocalPCM(controller.claim("user", "socket", 0), now=0)
    host._reserve()
    assert host.player._state.queue == [entries[1]]
    assert host.occurrences[0].entry is entries[1]
    assert len(host.occurrences) == 2


def test_voluntary_return_prepares_automation_while_local_input_remains_live(tmp_path):
    """ADR-0020-07: decode the next unheard song before fencing the owner's PCM."""
    host, controller, station, songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    views = host.prepare(grant)
    host.marker({"sequence": 0, "kind": "started", "occurrence": views[0]["id"], "frame": 0})
    controller.audio.events.consume([(0, 441)])
    host.on_air = True
    entered, finish = threading.Event(), threading.Event()

    def prepare(entry, *, timeout):
        assert entry is songs[2]
        entered.set()
        assert finish.wait(2)
        return True

    station.player.prepare_local_return = prepare
    assert host.begin_return()
    thread = threading.Thread(target=host.prepare_return)
    thread.start()
    assert entered.wait(2)
    assert not controller.audio.failed
    assert host.on_air
    with pytest.raises(OwnershipError, match="Returning"):
        host.marker({"sequence": 1, "kind": "started", "occurrence": views[1]["id"], "frame": 441})
    finish.set()
    thread.join(2)
    controller.revoke(grant.generation, "fade consumed")
    host.recover()
    station.player.bus.return_local_programme.assert_called_once()
    assert not host.busy
    assert host.reason == "Automation"


def test_active_service_uses_cached_admission_and_source_signatures(tmp_path):
    """ADR-0020-06: receipt polling does not repeat source IO or admission scans."""
    host, controller, _station, _songs = fixture(tmp_path)
    host.prepare(controller.claim("user", "socket", 0))
    host.on_air = True
    host.admit = Mock(side_effect=AssertionError("repeated admission"))
    host._signature = Mock(side_effect=AssertionError("repeated stat"))
    assert host.service() == []


def test_source_signatures_are_read_without_holding_queue_lock(tmp_path):
    """ADR-0020-06: source IO does not prevent station queue access."""
    host, controller, station, _songs = fixture(tmp_path)
    signature = host._signature

    def checked(entry):
        assert not station.player._state.queue_lock._is_owned()
        return signature(entry)

    host._signature = checked
    host.prepare(controller.claim("user", "socket", 0))
    host.validate_reservations()


def test_handoff_rechecks_generation_after_prevalidation(tmp_path):
    """ADR-0020-01: automation advancing after IO validation invalidates takeover."""
    from tests.unit.test_local_programme_audio import packet
    from tests.unit.test_mixbus import _bus

    host, controller, station, _songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    views = host.prepare(grant)
    host.marker({"sequence": 0, "kind": "started", "occurrence": views[0]["id"], "frame": 0})
    for sequence in range(6):
        host.audio.accept_packet(grant, packet(sequence, generation=grant.generation), now=0)
    bus, _, _ = _bus([])
    station.player.bus = bus
    validate = host.validate_reservations

    def advance_after_validation():
        result = validate()
        station.player._local_render_generation = 1
        return result

    host.validate_reservations = advance_after_validation
    with pytest.raises(OwnershipError, match="Automation advanced"):
        host.service()
    assert bus._local_programme is None


def test_recovery_render_wait_releases_performance_lock_and_serializes_retries(tmp_path):
    """ADR-0020-07: blocked automation rendering cannot block other station controls."""
    host, controller, station, _songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    host.prepare(grant)
    host.on_air = True
    controller.revoke(grant.generation, "lost")
    entered, finish = threading.Event(), threading.Event()
    failures = []

    def wait(entry, *, timeout):
        entered.set()
        assert finish.wait(2)
        return True

    def recover():
        try:
            host.recover()
        except Exception as exc:
            failures.append(exc)

    station.player.prepare_local_return = wait
    thread = threading.Thread(target=recover)
    thread.start()
    try:
        assert entered.wait(2)
        acquired = host._lock.acquire(timeout=0.1)
        if acquired:
            host._lock.release()
        assert acquired
        with pytest.raises(OwnershipError, match="progress"):
            host.recover()
    finally:
        finish.set()
        thread.join(2)
    assert not failures
    assert not host.busy


def test_return_timeout_retains_safe_silence_and_reservation_for_retry(tmp_path):
    """ADR-0020-07: an unready renderer cannot acquire the bus or release ownership."""
    host, controller, station, _songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    host.prepare(grant)
    host.on_air = True
    controller.revoke(grant.generation, "lost")
    station.player.prepare_local_return.return_value = False
    with pytest.raises(OwnershipError, match="waiting"):
        host.recover()
    assert host.busy and host.returning
    assert host.audio.failed
    assert not controller.live.reserve_local(object())
    station.player.prepare_local_return.return_value = True
    host.recover()
    assert not host.busy


def test_real_player_timeout_keeps_local_bus_and_reuses_pending_render(tmp_path):
    """ADR-0020-07: a retry waits for the same cursor instead of restarting decoding."""
    host, player, entry, audio = real_player_recovery(tmp_path)
    player._playout.wait_rendered = Mock(side_effect=[False, True])
    reset = Mock(wraps=player._playout.reset)
    player._playout.reset = reset
    with pytest.raises(OwnershipError, match="waiting"):
        host.recover()
    assert player.bus._local_programme is audio
    assert host.busy
    host.recover()
    assert player.bus._local_programme is None
    assert not host.busy
    assert player._state.queue == [entry]
    assert reset.call_count == 1


def test_stop_during_recovery_wait_does_not_resume_the_bus(tmp_path):
    """ADR-0020-07: Stop wins over automation readiness arriving during recovery."""
    from tests.unit.test_mixbus import _bus

    host, controller, station, _songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    host.prepare(grant)
    host.on_air = True
    audio = host.audio
    station.player.bus, _, _ = _bus([])
    station.player.bus._local_programme = audio
    controller.revoke(grant.generation, "lost")

    def prepare(entry, *, timeout):
        station.stopped = True
        return True

    station.player.prepare_local_return = prepare
    host.recover()
    assert station.player.bus._local_programme is audio
    assert not host.busy


def test_recovery_retry_reuses_picked_song_without_unstarted_occurrences(tmp_path):
    """ADR-0020-07: retries retain a picked target after all reservations started."""
    host, controller, station, songs = fixture(tmp_path)
    grant = controller.claim("user", "socket", 0)
    host.prepare(grant)
    host.on_air = True
    for occurrence in host.occurrences:
        occurrence.started = True
    controller.revoke(grant.generation, "lost")
    station.player._choose_next = Mock(side_effect=[(songs[0], "random"), (songs[1], "random")])
    station.player.prepare_local_return.side_effect = [False, True]
    with pytest.raises(OwnershipError, match="waiting"):
        host.recover()
    host.recover()
    targets = [call.args[0] for call in station.player.prepare_local_return.call_args_list]
    assert targets == [songs[0], songs[0]]
    assert not host.busy
