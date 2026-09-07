"""Every transition effect must be offered and accepted by all four surfaces.

The web UI dropdown, the browser audio engine, the CLI ``--transition`` choice
list and the server allowlist were maintained by hand, and the last three had
drifted ten effects behind the enum: choosing one of them in the browser was
accepted with a 200, ignored, and silently reverted on the next state push.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from autodj.cli import cli
from autodj.runtime_state import TRANSITION_EFFECTS
from autodj.transitions import TRANSITION_EFFECT_NAMES, TransitionFx

_STATIC = Path(__file__).resolve().parents[2] / "src" / "autodj" / "static"
_META_MODES = frozenset({"none", "random", "rotate"})


class TestServerAcceptsEveryEffect:
    @pytest.mark.parametrize("effect", sorted(TRANSITION_EFFECT_NAMES))
    def test_effect_round_trips(self, client, effect: str) -> None:
        resp = client.post("/api/transition", json={"effect": effect})
        assert resp.status_code == 200
        assert resp.json()["transition"] == effect

    def test_unknown_effect_is_rejected(self, client) -> None:
        resp = client.post("/api/transition", json={"effect": "wobble"})
        assert resp.status_code == 400
        assert "wobble" in resp.json()["detail"]

    def test_rejected_effect_leaves_the_previous_choice(self, client) -> None:
        client.post("/api/transition", json={"effect": "halftime"})
        client.post("/api/transition", json={"effect": "wobble"})
        assert client.get("/api/settings").json()["transition"] == "halftime"

    def test_effect_name_is_case_insensitive(self, client) -> None:
        resp = client.post("/api/transition", json={"effect": "HALFTIME"})
        assert resp.status_code == 200
        assert resp.json()["transition"] == "halftime"


class TestAllowlistsAreDerived:
    def test_persisted_state_allowlist_matches_the_enum(self) -> None:
        assert frozenset(fx.value for fx in TransitionFx) == TRANSITION_EFFECTS

    @pytest.mark.parametrize("command", ["play", "serve"])
    def test_cli_choices_match_the_enum(self, command: str) -> None:
        option = next(
            param for param in cli.commands[command].params if param.name == "transition_fx"
        )
        assert set(option.type.choices) == TRANSITION_EFFECT_NAMES

    def test_settings_dropdown_lists_every_effect(self) -> None:
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        select = re.search(r'<select id="transition-select".*?</select>', html, re.S)
        assert select is not None
        values = set(re.findall(r'<option value="([^"]+)"', select.group(0)))
        assert values == TRANSITION_EFFECT_NAMES

    def test_browser_engine_plays_every_real_effect(self) -> None:
        source = (_STATIC / "modules" / "audio-engine.js").read_text(encoding="utf-8")
        catalogue = re.search(r"const real = \[(.*?)\];", source, re.S)
        assert catalogue is not None
        names = set(re.findall(r'"([a-z_]+)"', catalogue.group(1)))
        assert names == TRANSITION_EFFECT_NAMES - _META_MODES
