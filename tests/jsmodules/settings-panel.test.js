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
    djDropMix:       cb(),
    djKeyShift:      cb(),
    djPhraseAlign:   cb(),
    djOutroIntro:    cb(),
    djVocalGuard:    cb(),
    pbEqDuck:        cb(),
    pbSmartShuffle:  cb(),
    pbPureShuffle:   cb(),
    pbShowLyrics:    cb(),
    pbAnchorSeed:    cb(),
    pbLoudness:      sel(["off", "replaygain", "smooth", "stereotool"]),
    pbRgTarget:      num(),
    pbFadeIn:        num(),
    djPhraseBars:    num(),
    txWetMix:        num(),
    djFilterSweep:   cb(),
    pbSavedPointsNote: document.createElement("p"),
    pbBeatSyncFx:    cb(),
    pbKeySyncFx:     cb(),
    pbBeatmatchSkip: cb(),
    pbSkipStyle:     sel(["fade", "echo_out", "backspin", "loop_roll"]),
    lnTalkUp:        cb(),
    pbTransitionMode: sel(["full_intro_outro", "fixed", "saved_points"]),
    pbPickMode:      sel(["similarity", "random", "least_recent", "clock"]),
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
        drop_mix: true,
        key_shift: false,
        phrase_align: false,
        outro_intro_align: true,
      },
      playback: {
        crossfade_eq_duck: true,
        pure_shuffle: false,
        pick_mode: "similarity",
        show_lyrics: true,
        anchor_to_seed: false,
        replaygain_enabled: false,
        loudness_processing: "off",
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
    expect(els.djDropMix.checked).toBe(true);
    expect(els.djKeyShift.checked).toBe(false);
    expect(els.discEnabled.checked).toBe(true);
    expect(els.discEvery.value).toBe("5");
  });

  it("shows the skip style, talk-up and vocal guard the server reports", () => {
    const els = makeEls();
    applySettingsState({
      djmix: { vocal_guard: false },
      playback: { skip_style: "loop_roll", liners_talk_up: true },
    }, els);
    expect(els.pbSkipStyle.value).toBe("loop_roll");
    expect(els.lnTalkUp.checked).toBe(true);
    expect(els.djVocalGuard.checked).toBe(false);
    // The vocal guard is on unless the server says it is off.
    applySettingsState({ djmix: { harmonic_mode: "off" }, playback: {} }, els);
    expect(els.djVocalGuard.checked).toBe(true);
  });

  it("prefers explicit pick and loudness modes, while reading legacy flags", () => {
    const els = makeEls();
    applySettingsState({
      playback: {
        pick_mode: "least_recent", pure_shuffle: true,
        loudness_processing: "smooth", replaygain_enabled: true,
      },
    }, els);
    expect(els.pbPickMode.value).toBe("least_recent");
    expect(els.pbLoudness.value).toBe("smooth");

    applySettingsState({ playback: { pure_shuffle: true, replaygain_enabled: true } }, els);
    expect(els.pbPickMode.value).toBe("random");
    expect(els.pbLoudness.value).toBe("replaygain");
  });

  it("selects and saves Stereo Tool processing", () => {
    const els = makeEls();
    applySettingsState({ playback: { loudness_processing: "stereotool" } }, els);
    expect(els.pbLoudness.value).toBe("stereotool");
    const save = vi.fn();
    installSettingsControls(els, save);
    els.pbLoudness.value = "stereotool";
    els.pbLoudness.dispatchEvent(new Event("change"));
    expect(save).toHaveBeenCalledWith(
      "/api/playback-settings", { loudness_processing: "stereotool" }, els.pbLoudness,
    );
  });

  it("hides settings that do not apply to saved-point transitions", () => {
    const els = makeEls();
    const row = (control, descriptionId) => {
      const parent = document.createElement("div");
      parent.className = "settings-row";
      parent.append(control);
      const description = document.createElement("span");
      description.id = descriptionId;
      control.setAttribute("aria-describedby", descriptionId);
      document.body.append(parent, description);
      return { parent, description };
    };
    const fade = row(els.pbFadeIn, "fade-desc");
    const effect = row(els.transitionSelect, "effect-desc");
    document.body.append(els.pbSavedPointsNote);
    applySettingsState({ playback: { transition_mode: "saved_points" } }, els);
    expect(fade.parent.hidden).toBe(true);
    expect(fade.description.hidden).toBe(true);
    expect(effect.parent.hidden).toBe(true);
    expect(els.pbSavedPointsNote.hidden).toBe(false);
    applySettingsState({ playback: { transition_mode: "fixed" } }, els);
    expect(fade.parent.hidden).toBe(false);
    expect(els.pbSavedPointsNote.hidden).toBe(true);
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
    expect(change(els.djDropMix, true)).toEqual(["/api/djmix", { drop_mix: true }]);
    expect(change(els.djKeyShift, true)).toEqual(["/api/djmix", { key_shift: true }]);
    expect(change(els.pbLoudness, "smooth"))
      .toEqual(["/api/playback-settings", { loudness_processing: "smooth" }]);
    expect(change(els.transitionSelect, "reverb_tail"))
      .toEqual(["/api/transition", { effect: "reverb_tail" }]);
    expect(change(els.pbPickMode, "random"))
      .toEqual(["/api/playback-settings", { pick_mode: "random" }]);
    expect(change(els.bpmLo, "90")).toEqual(["/api/bpm-range", { lo: 90, hi: null }]);
    expect(change(els.pbSkipStyle, "backspin"))
      .toEqual(["/api/playback-settings", { skip_style: "backspin" }]);
    expect(change(els.lnTalkUp, true))
      .toEqual(["/api/playback-settings", { liners_talk_up: true }]);
    expect(change(els.djVocalGuard, false)).toEqual(["/api/djmix", { vocal_guard: false }]);
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

describe("a setting being saved", () => {
  const state = (beatmatch) => ({
    available_presets: [], preset: "", transition: "echo_out",
    djmix: { harmonic_mode: "off", beatmatch, phrase_align: false, outro_intro_align: false },
    playback: {}, bpm_range: { lo: null, hi: null },
    discovery_every: null, discovery_enabled: false,
  });

  it("keeps a ticked checkbox while its save is out, until the server echoes it", async () => {
    document.body.innerHTML = "";
    const els = makeEls();
    for (const control of Object.values(els)) document.body.append(control);
    applySettingsState(state(false), els);
    let answer;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      answer = () => resolve(new globalThis.Response('{"ok":true}', {
        headers: { "Content-Type": "application/json" },
      }));
    })));
    const status = document.createElement("p");
    installSettingsControls(els, (url, body, control) =>
      postSettings(url, body, { settingsStatus: status, control }));

    els.djBeatmatch.focus();
    els.djBeatmatch.checked = true;
    els.djBeatmatch.dispatchEvent(new Event("change"));
    // The push from before the save would untick it under the listener.
    applySettingsState(state(false), els);
    expect(els.djBeatmatch.checked).toBe(true);

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    answer();
    await new Promise((resolve) => setTimeout(resolve, 0));
    applySettingsState(state(false), els);
    expect(els.djBeatmatch.checked).toBe(true);
    // Echoed: from here the box follows the server again.
    applySettingsState(state(true), els);
    applySettingsState(state(false), els);
    expect(els.djBeatmatch.checked).toBe(false);
    vi.unstubAllGlobals();
  });

  it("puts a checkbox back when its save fails", async () => {
    document.body.innerHTML = "";
    const els = makeEls();
    for (const control of Object.values(els)) document.body.append(control);
    applySettingsState(state(false), els);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      '{"detail":"Settings locked"}',
      { status: 423, headers: { "Content-Type": "application/json" } },
    )));
    const status = document.createElement("p");
    installSettingsControls(els, (url, body, control) =>
      postSettings(url, body, { settingsStatus: status, control }));

    els.djBeatmatch.checked = true;
    els.djBeatmatch.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(status.textContent).toContain("Settings locked"));
    expect(els.djBeatmatch.checked).toBe(false);
    applySettingsState(state(false), els);
    expect(els.djBeatmatch.checked).toBe(false);
    vi.unstubAllGlobals();
  });
});
