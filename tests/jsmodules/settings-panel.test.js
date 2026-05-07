// Regression test: applySettingsState requires an `els` bag.
//
// Bug 2026-05-07: app.js called `applySettingsState(s.settings)` with no
// second arg; settings-panel destructures `els` immediately and threw
// "els is undefined", which the /api/status .catch then mislabelled as
// "Cannot reach server: els is undefined" even when the server was alive.

import { describe, it, expect, beforeEach } from "vitest";
import { applySettingsState } from
  "../../src/autodj/static/modules/settings-panel.js";

function makeEls() {
  // Minimal stand-ins for every field settings-panel touches.
  const sel = (opts = []) => {
    const s = document.createElement("select");
    for (const o of opts) {
      const op = document.createElement("option");
      op.value = o; op.textContent = o; s.appendChild(op);
    }
    return s;
  };
  const cb = () => {
    const c = document.createElement("input");
    c.type = "checkbox";
    return c;
  };
  const num = () => {
    const n = document.createElement("input");
    n.type = "number";
    return n;
  };
  return {
    presetSelect:    sel(),
    transitionSelect: sel(["echo_out", "reverb_tail"]),
    harmonicMode:    sel(["off", "compatible"]),
    djBeatmatch:     cb(),
    djPhraseAlign:   cb(),
    djOutroIntro:    cb(),
    pbEqDuck:        cb(),
    pbSmartShuffle:  cb(),
    pbPureShuffle:   cb(),
    pbShowLyrics:    cb(),
    pbAnchorSeed:    cb(),
    pbReplayGain:    cb(),
    pbDaypart:       cb(),
    pbMoodArc:       cb(),
    pbMoodArcHours:  num(),
    pbImportCues:    cb(),
    pbBeatSyncFx:    cb(),
    pbKeySyncFx:     cb(),
    pbBeatmatchSkip: cb(),
    pbTransitionMode: sel(["full_intro_outro", "fixed"]),
    pbCrossfade:     num(),
    bpmLo:           num(),
    bpmHi:           num(),
    discEnabled:     cb(),
    discEvery:       num(),
  };
}

describe("applySettingsState", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("throws a clear error when called with no els bag (regression)", () => {
    // The bug: caller forgot the els arg.  Surface a TypeError
    // immediately so the regression is obvious in a stack trace
    // instead of being swallowed by the /api/status .catch as
    // "Cannot reach server".
    expect(() => applySettingsState({}, undefined)).toThrow();
  });

  it("applies a typical state without throwing when els is provided", () => {
    const els = makeEls();
    const state = {
      available_presets: ["chill", "party"],
      preset: "chill",
      transition: "echo_out",
      djmix: {
        harmonic_mixing: true,
        harmonic_mode: "compatible",
        beatmatch: true,
        phrase_align: false,
        outro_intro_align: true,
      },
      playback: {
        crossfade_eq_duck: true,
        smart_shuffle: false,
        pure_shuffle: false,
        show_lyrics: true,
        anchor_to_seed: false,
        replaygain_enabled: false,
        enable_daypart: false,
        enable_mood_arc: false,
        mood_arc_hours: 4,
        import_external_cues: true,
        beat_sync_fx: true,
        key_sync_fx: false,
        beatmatch_on_skip: false,
        transition_mode: "full_intro_outro",
        crossfade_seconds: 6,
      },
      bpm_range: { lo: 90, hi: 130 },
      discovery_every: 5,
    };
    expect(() => applySettingsState(state, els)).not.toThrow();
    expect(els.presetSelect.value).toBe("chill");
    expect(els.transitionSelect.value).toBe("echo_out");
    expect(els.harmonicMode.value).toBe("compatible");
    expect(els.djBeatmatch.checked).toBe(true);
    expect(els.pbCrossfade.value).toBe("6");
    expect(els.discEnabled.checked).toBe(true);
    expect(els.discEvery.value).toBe("5");
  });
});
