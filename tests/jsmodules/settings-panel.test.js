// Regression test: applySettingsState requires an `els` bag.
//
// Bug 2026-05-07: app.js called `applySettingsState(s.settings)` with no
// second arg; settings-panel destructures `els` immediately and threw
// "els is undefined", which the /api/status .catch then mislabelled as
// "Cannot reach server: els is undefined" even when the server was alive.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { applySettingsState, installSettingsControls } from
  "../../src/autodj/static/modules/settings-panel.js";
import { applySettingsDrafts, loadSettingsDrafts, resetSettingsDrafts, saveSettingsPanel } from
  "../../src/autodj/static/modules/settings-drafts.js";

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
    pbHourFit:       cb(),
    pbLoudness:      sel(["off", "replaygain", "smooth", "stereotool"]),
    pbRgTarget:      num(),
    pbQuietBoost:    num(),
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
    lnMatchedIntroOnlyWithoutLiner: cb(),
    pbTransitionMode: sel(["full_intro_outro", "fixed", "saved_points"]),
    pbPickMode:      sel(["similarity", "random", "least_recent", "clock"]),
    bpmLo:           num(),
    bpmHi:           num(),
    discEnabled:     cb(),
    discEvery:       num(),
    pbPrefetch:      cb(),
    pbSilenceTrigger: cb(),
    pbMusicSpeed: num(),
  };
}

describe("applySettingsState", () => {
  beforeEach(() => {
    resetSettingsDrafts(document);
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

  it("keeps the music speed value while the number field is being edited", () => {
    const els = makeEls();
    els.pbMusicSpeed.value = "1.23";
    document.body.append(els.pbMusicSpeed);
    els.pbMusicSpeed.focus();
    applySettingsState({ playback: { music_speed_percent: 0 } }, els);
    expect(els.pbMusicSpeed.value).toBe("1.23");
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

  it("shows the matched intro liner precedence setting", () => {
    const els = makeEls();
    applySettingsState({ playback: { matched_intro_only_without_liner: true } }, els);
    expect(els.lnMatchedIntroOnlyWithoutLiner.checked).toBe(true);
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

    applySettingsState({ playback: { pick_mode: "random", replaygain_enabled: true } }, els);
    expect(els.pbPickMode.value).toBe("random");
    expect(els.pbLoudness.value).toBe("replaygain");
  });

  it("matches the real Random walk option", () => {
    const template = document.createElement("template");
    template.innerHTML = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
    const actual = template.content.querySelector("#pb-pick-mode");
    expect(actual.querySelector('option[value="random"]')).not.toBeNull();
    const els = makeEls();
    els.pbPickMode = actual;
    applySettingsState({ playback: { pick_mode: "random" } }, els);
    expect(actual.value).toBe("random");
  });

  it("stages Stereo Tool processing until the tab Save is pressed", async () => {
    const els = makeEls();
    const panel = document.createElement("div");
    panel.id = "spanel-sound";
    panel.append(els.pbLoudness);
    document.body.append(panel);
    const api = settingsApi({ playback: { loudness_processing: "off" } });
    applySettingsState({ playback: { loudness_processing: "stereotool" } }, els);
    expect(els.pbLoudness.value).toBe("stereotool");
    installSettingsControls(els);
    applySettingsDrafts({ profile: "", revision: "one", values: { playback: { loudness_processing: "off" } } }, document);
    els.pbLoudness.value = "stereotool";
    els.pbLoudness.dispatchEvent(new Event("change", { bubbles: true }));
    expect(api.requests).toHaveLength(0);
    await saveSettingsPanel(panel);
    expect(api.requests).toEqual([{ profile: "", revision: "one", changes: { playback: { loudness_processing: "stereotool" } } }]);
  });

  it("hides settings that do not apply to saved-point transitions", () => {
    const els = makeEls();
    const row = (control, descriptionId) => {
      const parent = document.createElement("div");
      parent.className = "settings-row";
      parent.append(control);
      const description = document.createElement("span");
      description.id = descriptionId;
      control.setAttribute("data-help", descriptionId);
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

  it("hides settings help without hiding aria-describedby errors", () => {
    const els = makeEls();
    const parent = document.createElement("div");
    parent.className = "settings-row";
    parent.append(els.pbFadeIn);
    const first = document.createElement("span");
    first.id = "fade-desc";
    const second = document.createElement("span");
    second.id = "fade-note";
    const error = document.createElement("span");
    error.id = "fade-error";
    els.pbFadeIn.setAttribute("aria-describedby", "fade-error");
    els.pbFadeIn.setAttribute("data-help", "fade-desc  fade-note");
    document.body.append(parent, first, second, error);
    applySettingsState({ playback: { transition_mode: "saved_points" } }, els);
    expect(first.hidden).toBe(true);
    expect(second.hidden).toBe(true);
    expect(error.hidden).toBe(false);
    expect(els.pbFadeIn.getAttribute("aria-describedby")).toBe("fade-error");
    applySettingsState({ playback: { transition_mode: "fixed" } }, els);
    expect(first.hidden).toBe(false);
    expect(second.hidden).toBe(false);
  });

  it("hides the new mix numbers with the rest for saved-point transitions", () => {
    const els = makeEls();
    const rows = ["dj-max-stretch", "dj-glide-bars", "dj-sweep-floor", "pb-bass-cutoff"]
      .map((id) => {
        const row = document.createElement("div");
        row.className = "settings-row";
        const input = document.createElement("input");
        input.id = id;
        row.append(input);
        document.body.append(row);
        return row;
      });
    applySettingsState({ playback: { transition_mode: "saved_points" } }, els);
    expect(rows.every((row) => row.hidden)).toBe(true);
    applySettingsState({ playback: { transition_mode: "fixed" } }, els);
    expect(rows.some((row) => row.hidden)).toBe(false);
  });

  it("shows early loading and the silence trigger on unless the server says off", () => {
    const els = makeEls();
    applySettingsState({ playback: {} }, els);
    expect(els.pbPrefetch.checked).toBe(true);
    expect(els.pbSilenceTrigger.checked).toBe(true);
    applySettingsState({
      playback: { prefetch_next_track: false, silence_trigger_crossfade: false },
    }, els);
    expect(els.pbPrefetch.checked).toBe(false);
    expect(els.pbSilenceTrigger.checked).toBe(false);
  });

  it("shows quiet boost only for Smooth and Stereo Tool", () => {
    const els = makeEls();
    const parent = document.createElement("div");
    parent.className = "settings-row";
    const description = document.createElement("span");
    description.id = "quiet-boost-desc";
    els.pbQuietBoost.setAttribute("data-help", description.id);
    parent.append(els.pbQuietBoost);
    document.body.append(parent, description);

    applySettingsState({ playback: { loudness_processing: "off" } }, els);
    expect(parent.hidden).toBe(true);
    expect(description.hidden).toBe(true);
    applySettingsState({ playback: { loudness_processing: "smooth" } }, els);
    expect(parent.hidden).toBe(false);
    expect(description.hidden).toBe(false);
    applySettingsState({ playback: { loudness_processing: "stereotool" } }, els);
    expect(parent.hidden).toBe(false);
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

function settingsApi(initial, failPatch = false) {
  let revision = "revision-1";
  let values = JSON.parse(JSON.stringify(initial));
  const requests = [];
  const fetch = vi.fn(async (url, options = {}) => {
    if (url !== "/api/settings" && url !== "/api/settings?profile=") throw new Error(`Unexpected request: ${url}`);
    if (options.method === "PATCH") {
      const request = JSON.parse(options.body);
      requests.push(request);
      if (failPatch) return new globalThis.Response(JSON.stringify({ detail: "The settings service is busy." }), {
        status: 503, headers: { "Content-Type": "application/json" },
      });
      for (const [group, patch] of Object.entries(request.changes)) {
        values[group] = { ...values[group], ...patch };
      }
      revision = "revision-2";
      return new globalThis.Response(JSON.stringify({
        revision,
        settings: { revision, values },
        applied: Object.keys(request.changes),
        failed: {},
        message: "",
      }), { headers: { "Content-Type": "application/json" } });
    }
    return new globalThis.Response(JSON.stringify({ revision, profile: "", values }), {
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetch);
  return { fetch, requests };
}

describe("settings drafts from settings controls", () => {
  function mount() {
    const panel = document.createElement("div");
    panel.id = "spanel-djmix";
    panel.hidden = false;
    const els = makeEls();
    for (const control of [els.djBeatmatch, els.djDropMix, els.pbPrefetch]) panel.append(control);
    document.body.append(panel);
    installSettingsControls(els);
    return { panel, els };
  }

  const initial = {
    djmix: { beatmatch: false, drop_mix: false },
    playback: { prefetch_next_track: true },
  };

  it("loads server values and patches only changed fields after Save", async () => {
    const { panel, els } = mount();
    const api = settingsApi(initial);
    await loadSettingsDrafts(document);
    expect(els.djBeatmatch.checked).toBe(false);
    expect(els.pbPrefetch.checked).toBe(true);
    els.djBeatmatch.checked = true;
    els.djBeatmatch.dispatchEvent(new Event("change", { bubbles: true }));
    expect(api.requests).toHaveLength(0);
    expect(api.fetch).toHaveBeenCalledTimes(1);

    await saveSettingsPanel(panel);
    expect(api.requests).toEqual([{ profile: "", revision: "revision-1", changes: { djmix: { beatmatch: true } } }]);
    expect(els.djBeatmatch.checked).toBe(true);
    expect(els.djDropMix.checked).toBe(false);
  });

  it("keeps a failed patch as a draft and ignores the old pushed value", async () => {
    const { panel, els } = mount();
    const api = settingsApi(initial, true);
    applySettingsDrafts({ profile: "", revision: "revision-1", values: initial }, document);
    els.djBeatmatch.checked = true;
    els.djBeatmatch.dispatchEvent(new Event("change", { bubbles: true }));
    applySettingsDrafts({ profile: "", revision: "revision-old", values: initial }, document);
    expect(els.djBeatmatch.checked).toBe(true);

    await expect(saveSettingsPanel(panel)).resolves.toBe(false);
    expect(api.requests).toEqual([{ profile: "", revision: "revision-1", changes: { djmix: { beatmatch: true } } }]);
    expect(els.djBeatmatch.checked).toBe(true);
    expect(panel.querySelector("[data-settings-save]").disabled).toBe(false);
    await vi.waitFor(() => expect(panel.querySelector(".settings-save-status").textContent)
      .toContain("The settings service is busy."));
  });
});
describe("saved segue points", () => {
  it("hides the effect and DJ-mix groups and shows the note in their place", () => {
    document.body.innerHTML = `
      <p data-saved-points="note" hidden>Not used</p>
      <fieldset data-saved-points="hide"><legend>Tempo and key</legend></fieldset>
      <fieldset data-saved-points="hide"><legend>Transition effect</legend></fieldset>`;
    const els = makeEls();
    const groups = [...document.querySelectorAll('[data-saved-points="hide"]')];
    const note = document.querySelector('[data-saved-points="note"]');
    applySettingsState({ playback: { transition_mode: "saved_points" } }, els);
    expect(groups.every((group) => group.hidden)).toBe(true);
    expect(note.hidden).toBe(false);
    applySettingsState({ playback: { transition_mode: "full_intro_outro" } }, els);
    expect(groups.some((group) => group.hidden)).toBe(false);
    expect(note.hidden).toBe(true);
  });
});
