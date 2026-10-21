"""A small saved-point station for the breaks tests (#88): four songs and tiny audio.

Every song is 20 seconds of one constant level on the left channel, so a
test can tell the songs apart by level; a break is a constant level on the
right channel, so the voice is easy to find in a render.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import soundfile as sf

from autodj.breaks import Break, BreakDesk, new_id, speech_cues
from autodj.config import BreaksConfig, PlaybackConfig
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._fakes import make_cfg_mock, make_entry, make_sim_index

#: Left-channel level of each song, by index.
LEVELS = (1.0, 0.25, 0.5, 0.75)


def write_voice(
    path: Path, seconds: float = 3.0, *, level: float = 0.5, silence: float = 1.0
) -> Path:
    """Write a WAV holding *seconds* of sound on the right channel, then *silence*."""
    sound = np.zeros((round((seconds + silence) * SR), 2), dtype=np.float32)
    sound[: round(seconds * SR), 1] = level
    sf.write(path, sound, SR, subtype="FLOAT")
    return path


def make_station(tmp_path: Path, cfg=None, **breaks: object):
    """A player in saved-point station mode with a break desk.

    Returns ``(player, entries, points, desk)``.  Songs: *a* has an outro
    at 8 s and a segue at 10 s; *b*, *c* and *d* start at 0.25 s with a
    vocal at 2.25 s (a 2-second intro).
    """
    entries = [make_entry(i) for i in range(4)]
    for entry in entries:
        entry.length = 20.0
    if cfg is None:
        cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        transition_mode="saved_points",
        crossfade_seconds=1.5,
        loudness_processing="off",
        pick_mode="random",
        liners_duck_db=0.0,
    )
    cfg.breaks = BreaksConfig(**{"enabled": True, **breaks})  # type: ignore[arg-type]
    player = Player(cfg, make_sim_index(4), dry_run=True)
    points = {entries[0].path: {"start": 0, "outro": 8, "segue": 10, "end": 15}}
    for entry in entries[1:]:
        points[entry.path] = {"start": 0.25, "intro": 2.25, "segue": 18, "end": 20}
    audio = {}
    for entry, level in zip(entries, LEVELS, strict=True):
        array = np.zeros((round(entry.length * SR), 2), dtype=np.float32)
        array[:, 0] = level
        audio[entry.path] = array
    player._load_playout_audio = lambda entry: audio[entry.path].copy()  # type: ignore[method-assign]
    player._library = SimpleNamespace(
        catalog=SimpleNamespace(
            points=lambda path: points[path],
            record_for_path=lambda path: {"id": f"song-{Path(path).stem}", "kind": "music"},
        ),
        entries_snapshot=lambda kind="music": () if kind == "liner" else tuple(entries),
        ledger=SimpleNamespace(
            remember_play=MagicMock(),
            remember_setting=MagicMock(),
            last_played=lambda: {},
        ),
    )
    desk = BreakDesk(lambda: cfg.breaks, tmp_path / "announcements")
    desk.folder.mkdir(parents=True, exist_ok=True)
    player.break_desk = desk
    return player, entries, points, desk


def make_break(
    desk: BreakDesk,
    slot_id: str,
    *,
    kind: str = "dj",
    placement: str = "auto",
    seconds: float = 3.0,
    allow_outro: bool = False,
    created_at: float = 1000.0,
    expires_at: float = 10**12,
) -> Break:
    """A break with a real WAV in the desk's folder, not yet booked."""
    break_id = new_id()
    path = write_voice(desk.folder / f"brk-{break_id}.wav", seconds)
    data, _sr = sf.read(path, dtype="float32", always_2d=True)
    return Break(
        break_id=break_id,
        kind=kind,
        slot_id=slot_id,
        ending="ending",
        upcoming="upcoming",
        placement=placement,
        allow_outro=allow_outro,
        path=path,
        size=path.stat().st_size,
        cues=speech_cues(data),
        label="",
        token_name="announcer",
        created_at=created_at,
        expires_at=expires_at,
    )


def start_station(player: Player, entries) -> tuple:
    """Play *a*, render *b* ahead and plan the change after it.

    The queue holds b, c and d, so the picks are a into b, b into c, and
    the open slot is c into d.  Returns ``(render_a, render_b, plan)``.
    """
    a, b, c, d = entries
    player._state.queue[:] = [b, c, d]
    player._set_render_cursor(a, 0, "seed")
    render_a = player._next_rendered()
    player._on_track_start(render_a)
    render_b = player._next_rendered()
    player._plan_slot(render_b)
    return render_a, render_b, player._slot_plan
