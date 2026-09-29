// Regression test: applySettingsState requires an `els` bag.
//
// Bug 2026-05-07: app.js called `applySettingsState(s.settings)` with no
// second arg; settings-panel destructures `els` immediately and threw
// "els is undefined", which the /api/status .catch then mislabelled as
// "Cannot reach server: els is undefined" even when the server was alive.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { applySettingsState, installSettingsControls, postSettings } from
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
    pbBeatSyncFx:    cb(),
    pbKeySyncFx:     cb(),
    pbBeatmatchSkip: cb(),
    pbTransitionMode: sel(["full_intro_outro", "fixed"]),
    pbPickMode:      sel(["similarity", "pure"]),
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
        harmonic_mode: "compatible",
        beatmatch: true,
        phrase_align: false,
        outro_intro_align: true,
      },
      playback: {
        crossfade_eq_duck: true,
        pure_shuffle: false,
        show_lyrics: true,
        anchor_to_seed: false,
        replaygain_enabled: false,
        beat_sync_fx: true,
        key_sync_fx: false,
        beatmatch_on_skip: false,
        transition_mode: "full_intro_outro",
      },
      bpm_range: { lo: 90, hi: 130 },
      discovery_every: 5,
      discovery_enabled: true,
    };
    expect(() => applySettingsState(state, els)).not.toThrow();
    expect(els.presetSelect.value).toBe("chill");
    expect(els.transitionSelect.value).toBe("echo_out");
    expect(els.harmonicMode.value).toBe("compatible");
    expect(els.djBeatmatch.checked).toBe(true);
    expect(els.discEnabled.checked).toBe(true);
    expect(els.discEvery.value).toBe("5");
  });

  it("keeps the discovery checkbox in step with the runtime toggle", () => {
    const els = makeEls();

    // Configured and running: both controls say on.
    applySettingsState({ discovery_every: 20, discovery_enabled: true }, els);
    expect(els.discEnabled.checked).toBe(true);

    // Turned off from the Now Playing button: the checkbox follows rather
    // than contradicting it.
    applySettingsState({ discovery_every: 20, discovery_enabled: false }, els);
    expect(els.discEnabled.checked).toBe(false);
  });

  it("leaves a focused Preset or Harmonic mixing choice alone", () => {
    const els = makeEls();
    document.body.append(els.harmonicMode);
    applySettingsState({ djmix: { harmonic_mode: "off" } }, els);
    els.harmonicMode.focus();
    els.harmonicMode.value = "compatible";
    applySettingsState({ djmix: { harmonic_mode: "off" } }, els);
    expect(els.harmonicMode.value).toBe("compatible");
  });
});

describe("installSettingsControls", () => {
  it("saves each control to its own endpoint and field", () => {
    const els = makeEls();
    const save = vi.fn();
    installSettingsControls(els, save);
    const change = (control, value) => {
      if (control.type === "checkbox") control.checked = value;
      else control.value = value;
      control.dispatchEvent(new Event("change"));
      return save.mock.calls.at(-1)?.slice(0, 2);
    };

    expect(change(els.djPhraseAlign, true)).toEqual(["/api/djmix", { phrase_align: true }]);
    expect(change(els.pbReplayGain, true))
      .toEqual(["/api/playback-settings", { replaygain_enabled: true }]);
    expect(change(els.transitionSelect, "reverb_tail"))
      .toEqual(["/api/transition", { effect: "reverb_tail" }]);
    expect(change(els.pbPickMode, "pure"))
      .toEqual(["/api/playback-settings", { pure_shuffle: true }]);
    expect(change(els.bpmLo, "90")).toEqual(["/api/bpm-range", { lo: 90, hi: null }]);
    els.discEvery.value = "7";
    expect(change(els.discEnabled, true)).toEqual(["/api/discovery", { every: 7 }]);
    expect(els.discEvery.disabled).toBe(false);
  });
});

describe("postSettings", () => {
  it("puts a checkbox back at once and says what it still is", async () => {
    const els = makeEls();
    document.body.innerHTML = '<label for="dj-beatmatch">Beatmatch</label>';
    els.djBeatmatch.id = "dj-beatmatch";
    document.body.append(els.djBeatmatch);
    applySettingsState({ djmix: { beatmatch: false } }, els);
    els.djBeatmatch.checked = true;
    const settingsStatus = document.createElement("p");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ detail: "Settings locked" }),
      { status: 423, headers: { "Content-Type": "application/json" } },
    )));

    await postSettings("/api/djmix", { beatmatch: true }, {
      settingsStatus, control: els.djBeatmatch,
    });
    expect(els.djBeatmatch.checked).toBe(false);
    await vi.waitFor(() => expect(settingsStatus.textContent)
      .toBe("Could not save Beatmatch; it is still off. Settings locked"));
    vi.unstubAllGlobals();
  });

  it("returns false, announces failure, and restores the initiating control", async () => {
    const settingsStatus = document.createElement("p");
    const control = document.createElement("select");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ detail: "Settings locked" }),
      { status: 423, headers: { "Content-Type": "application/json" } },
    )));

    await expect(postSettings("/api/preset", { name: "party" }, {
      settingsStatus,
      control,
    })).resolves.toBe(false);
    // The failure is announced with force, which clears the region and
    // sets it on the next task so a repeated save failure speaks again.
    await vi.waitFor(() =>
      expect(settingsStatus.textContent).toContain("Could not save"));
    expect(settingsStatus.textContent).toContain("Settings locked");
    expect(control.disabled).toBe(false);
    vi.unstubAllGlobals();
  });
});
