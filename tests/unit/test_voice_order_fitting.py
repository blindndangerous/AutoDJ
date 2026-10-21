"""Render/forecast parity when companion transition-order PR #507 is present."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from autodj.breaks import VOICE_TRACK, new_id
from autodj.breaks_api import _voice_chain_forecast
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE
from tests.unit._break_station import make_break, make_station
from tests.unit.test_break_render import incoming_entry


@pytest.mark.skipif(
    not hasattr(Player, "_voice_transition_order"),
    reason="Requires the companion #507 ordered transition renderer",
)
@pytest.mark.parametrize("action", ["replace", "before", "after"])
@pytest.mark.parametrize("placement", ["auto", "standalone"])
def test_edited_liner_order_forecast_matches_actual_voice_and_music_frames(
    tmp_path, action, placement
):
    player, (ending, upcoming, liner, _last), _points, desk = make_station(tmp_path)
    take = replace(make_break(desk, new_id(), seconds=4.0, placement=placement), source=VOICE_TRACK)
    candidate = SimpleNamespace(
        item_id="liner",
        kind="liner",
        source="queued",
        label="Station ID",
        entry=liner,
        intro=None,
        committed=True,
        editable=True,
    )
    plan = SimpleNamespace(
        ending=ending,
        upcoming=upcoming,
        ending_offset_s=0.0,
        liners=((liner, True),),
        transition_items=(candidate,),
        transition_order=(),
        slot_id=take.slot_id,
    )
    choices = [{"id": "liner", "action": action}]
    order, replaced = player._voice_transition_order(plan, take.break_id, choices)
    forecast = _voice_chain_forecast(
        SimpleNamespace(player=player, breaks=desk),
        plan,
        take.cues.speech,
        take.cues.end - take.cues.segue,
        placement,
        choices,
    )
    desk.add(take)
    (booked,) = desk.take(take.slot_id, 0.0)
    rendered = player._render_track(
        ending,
        upcoming,
        0,
        liners=() if replaced else ((liner, True),),
        breaks=(booked,),
        transition_order=order,
        transition_items=(candidate,),
    )
    [(voice_frame, _id, _end)] = rendered.break_events
    assert forecast["recording_start_s"] == pytest.approx(
        (voice_frame + rendered.start_offset) / SAMPLE_RATE, abs=0.001
    )
    assert forecast["music_entry_s"] == pytest.approx(
        max(0.0, incoming_entry(rendered) - player.saved_points(ending)["segue"]),
        abs=0.001,
    )
