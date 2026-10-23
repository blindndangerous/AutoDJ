// Settings number fields: crossfade and fade-in length, the EQ duck
// cutoff, how widely similarity picks spread, phrase
// length, the beatmatch stretch limit and glide, the filter sweep floor,
// transition effect level and the ReplayGain target and peak limit, plus
// the filter sweep switch.
//
// Each number field carries the same range the server accepts
// (PlaybackSettingsBody / DjMixBody in settings_bodies.py), so a value the server
// would refuse is caught here and said in words instead of arriving as a
// 422.  A refused or failed value goes straight back to what the server
// has, so the field never shows a setting that is not in effect.

import { announceStatus } from "./live-region.js";
import { applySoundModeVisibility, saveHolds } from "./settings-panel.js";
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";

const NUMBER_FIELDS = [
  {
    key: "pbCrossfade", url: "/api/playback-settings", field: "crossfade_seconds",
    label: "Crossfade seconds", min: 0, max: 20, integer: false,
    read: (st) => st.playback?.crossfade_seconds,
  },
  {
    key: "pbFadeIn", url: "/api/playback-settings", field: "fade_in_seconds",
    label: "Fade-in seconds", min: 0, max: 20, integer: false,
    read: (st) => st.playback?.fade_in_seconds,
  },
  {
    key: "djPhraseBars", url: "/api/djmix", field: "phrase_bars",
    label: "Phrase length", min: 1, max: 64, integer: true,
    read: (st) => st.djmix?.phrase_bars,
  },
  {
    // Shown in percent, stored as a fraction.
    key: "txWetMix", url: "/api/playback-settings", field: "transition_wet_mix",
    label: "Transition effect level", min: 0, max: 100, integer: false,
    read: (st) => (typeof st.playback?.transition_wet_mix === "number"
      ? Math.round(st.playback.transition_wet_mix * 100) : undefined),
    toServer: (value) => value / 100,
  },
  {
    key: "pbRgTarget", url: "/api/playback-settings", field: "replaygain_target_db",
    label: "ReplayGain target loudness", min: -30, max: 0, integer: false,
    read: (st) => st.playback?.replaygain_target_db,
  },
  {
    key: "pbRgClip", url: "/api/playback-settings", field: "replaygain_max_clip_safe_gain",
    label: "ReplayGain peak limit", min: 0.1, max: 2, integer: false,
    read: (st) => st.playback?.replaygain_max_clip_safe_gain,
  },
  {
    key: "pbBassCutoff", url: "/api/playback-settings", field: "crossfade_bass_cutoff_hz",
    label: "EQ duck cutoff", min: 20, max: 1000, integer: false,
    read: (st) => st.playback?.crossfade_bass_cutoff_hz,
  },
  {
    key: "pbPickTopK", url: "/api/playback-settings", field: "pick_top_k",
    label: "Choose among the closest songs", min: 1, max: 100, integer: true,
    read: (st) => st.playback?.pick_top_k,
  },
  {
    key: "pbPickTemperature", url: "/api/playback-settings", field: "pick_temperature",
    label: "Pick randomness", min: 0, max: 10, integer: false,
    read: (st) => st.playback?.pick_temperature,
  },
  {
    key: "pbHourFitWindow", url: "/api/playback-settings", field: "hour_fit_window_minutes",
    label: "Minutes before to start fitting", min: 1, max: 30, integer: true,
    read: (st) => st.playback?.hour_fit_window_minutes,
  },
  {
    // Shown in percent, stored as a fraction.
    key: "djMaxStretch", url: "/api/djmix", field: "beatmatch_max_stretch",
    label: "Beatmatch stretch limit", min: 0, max: 50, integer: false,
    read: (st) => (typeof st.djmix?.beatmatch_max_stretch === "number"
      ? Math.round(st.djmix.beatmatch_max_stretch * 1000) / 10 : undefined),
    toServer: (value) => value / 100,
  },
  {
    key: "djGlideBars", url: "/api/djmix", field: "beatmatch_glide_bars",
    label: "Glide back", min: 0, max: 64, integer: true,
    read: (st) => st.djmix?.beatmatch_glide_bars,
  },
  {
    key: "djSweepFloor", url: "/api/djmix", field: "filter_sweep_floor_hz",
    label: "Filter sweep floor", min: 20, max: 5000, integer: false,
    read: (st) => st.djmix?.filter_sweep_floor_hz,
  },
  {
    key: "pbIntroLift", url: "/api/playback-settings", field: "intro_lift_max_db",
    label: "Quiet intro lift limit", min: 0, max: 6, integer: false,
    read: (st) => st.playback?.intro_lift_max_db,
  },
  {
    key: "pbStereoTrim", url: "/api/playback-settings", field: "stereotool_input_trim_db",
    label: "Stereo Tool input trim", min: -12, max: 6, integer: false,
    read: (st) => st.playback?.stereotool_input_trim_db,
  },
  {
    key: "pbStereoAgcDrive", url: "/api/playback-settings", field: "stereotool_agc_drive_db",
    label: "AGC drive boost", min: 0, max: 12, integer: false,
    read: (st) => st.playback?.stereotool_agc_drive_db,
  },
  {
    key: "pbMusicSpeed", url: "/api/playback-settings", field: "music_speed_percent",
    label: "Music speed", min: -10, max: 10, integer: false,
    read: (st) => st.playback?.music_speed_percent ?? 0,
  },
];

const MANUAL_KEYS = new Set([
  "pbIntroLift", "pbRgTarget", "pbRgClip", "pbStereoTrim", "pbStereoAgcDrive",
  "pbMusicSpeed", "pbPrefetch", "pbSilenceTrigger",
]);
const LOUDNESS_FIELD = {
  key: "pbLoudness", field: "loudness_processing",
  read: (st) => st.playback?.loudness_processing ??
    (st.playback?.replaygain_enabled ? "replaygain" : "off"),
};
const _dirty = new WeakSet();
const _savedEcho = new WeakMap();
const _saveStates = new WeakMap();
const ECHO_WAIT_MS = 5000;

const _serverValue = new WeakMap();

// NVDA at punctuation level none drops a leading hyphen, so "-14" would
// be heard as "14".
function numberWords(n) {
  return n < 0 ? `minus ${-n}` : String(n);
}

function rangeWords(spec) {
  const words = numberWords;
  const kind = spec.integer ? "a whole number" : "a number";
  return `${kind} from ${words(spec.min)} to ${words(spec.max)}`;
}

function invalidText(spec) {
  return `Could not save ${spec.label}: enter ${rangeWords(spec)}.`;
}

function savedEchoHolds(input, pushed) {
  const save = _savedEcho.get(input);
  if (!save) return false;
  if ((pushed === save.value || (typeof pushed !== "boolean"
      && Number(pushed) === Number(save.value)))
      || Date.now() - save.at > ECHO_WAIT_MS) {
    _savedEcho.delete(input);
    return false;
  }
  return true;
}

// The number in *text* when it is inside spec's range, else null.
export function parseSettingValue(text, spec) {
  if (String(text).trim() === "") return null;
  const value = Number(text);
  if (!Number.isFinite(value) || value < spec.min || value > spec.max) return null;
  if (spec.integer && !Number.isInteger(value)) return null;
  return value;
}

// What a refused value says; *shown* is what the field holds again.
export function refusalText(spec, shown) {
  return `Could not save ${spec.label}: enter ${rangeWords(spec)}.  It is still ${numberWords(Number(shown))}.`;
}

export function applyMixSettings(st, els) {
  if (!st) return;
  const doc = Object.values(els).find(Boolean)?.ownerDocument;
  for (const spec of NUMBER_FIELDS) {
    const input = els[spec.key];
    const value = spec.read(st);
    if (!input || typeof value !== "number" || _dirty.has(input)
        || savedEchoHolds(input, value)) continue;
    // A save still out (or not yet echoed) keeps the field's new value.
    if (saveHolds(input, value)) continue;
    _serverValue.set(input, value);
    if (doc?.activeElement !== input && input.value !== String(value)) {
      input.value = String(value);
    }
  }
  const loudness = els.pbLoudness;
  const loudnessValue = LOUDNESS_FIELD.read(st);
  if (loudness && typeof loudnessValue === "string" && !_dirty.has(loudness)
      && !savedEchoHolds(loudness, loudnessValue)
      && !saveHolds(loudness, loudnessValue)) {
    loudness.value = loudnessValue;
  }
  if (els.stereoPreset && typeof st.playback?.stereotool_preset === "string"
      && !_dirty.has(els.stereoPreset) && !savedEchoHolds(els.stereoPreset, st.playback.stereotool_preset)) {
    els.stereoPreset.value = st.playback.stereotool_preset;
  }
  for (const [key, field] of [["pbPrefetch", "prefetch_next_track"],
    ["pbSilenceTrigger", "silence_trigger_crossfade"]]) {
    const input = els[key];
    if (input && typeof st.playback?.[field] === "boolean" && !_dirty.has(input)
        && !savedEchoHolds(input, st.playback[field])) {
      input.checked = st.playback[field];
    }
  }
  applySoundModeVisibility(els, st, doc);
  const sweep = els.djFilterSweep;
  if (sweep && st.djmix && typeof st.djmix.filter_sweep === "boolean"
      && !saveHolds(sweep, st.djmix.filter_sweep)) {
    sweep.checked = st.djmix.filter_sweep;
  }
}

export function resetMixSettings(els) {
  for (const input of Object.values(els)) {
    if (!input) continue;
    _dirty.delete(input);
    _savedEcho.delete(input);
    if (input === els.masteringSave) continue;
    if (input.type === "checkbox") input.checked = input.defaultChecked;
    else if (input.tagName === "SELECT") {
      input.selectedIndex = Math.max(0,
        Array.from(input.options).findIndex((option) => option.defaultSelected));
    }
    else input.value = input.defaultValue;
    _serverValue.delete(input);
  }
  if (els.masteringSave) els.masteringSave.disabled = true;
  const state = _saveStates.get(els.masteringSave);
  if (state) { state.generation += 1; state.saving = false; }
}

// postSettings(url, body, control) resolves true on success and has
// already reported a failure in the settings status region.
export function installMixSettings(els, {
  postSettings, settingsStatus, getStation = () => null, onSaved = () => {},
}) {
  const saveState = { generation: 0, saving: false };
  if (els.masteringSave) _saveStates.set(els.masteringSave, saveState);
  for (const spec of NUMBER_FIELDS) {
    const input = els[spec.key];
    if (!input) continue;
    if (MANUAL_KEYS.has(spec.key)) continue;
    const revert = () => {
      const saved = _serverValue.get(input);
      input.value = saved === undefined ? input.defaultValue : String(saved);
    };
    input.addEventListener("change", async () => {
      const value = parseSettingValue(input.value, spec);
      if (value === null) {
        revert();
        announceStatus(settingsStatus, refusalText(spec, input.value),
          { dwellMs: 6000, force: true, tone: "error" });
        return;
      }
      const sent = spec.toServer ? spec.toServer(value) : value;
      const ok = await postSettings(spec.url, { [spec.field]: sent }, input);
      if (ok) _serverValue.set(input, value);
      else revert();
    });
  }
  const markDirty = (control) => {
    _dirty.add(control);
    if (els.masteringSave && !saveState.saving) els.masteringSave.disabled = false;
  };
  for (const spec of NUMBER_FIELDS.filter(({ key }) => MANUAL_KEYS.has(key))) {
    const input = els[spec.key];
    if (!input) continue;
    for (const event of ["input", "change"]) input.addEventListener(event, () => markDirty(input));
  }
  if (els.pbLoudness) {
    for (const event of ["input", "change"]) els.pbLoudness.addEventListener(event, () => {
      markDirty(els.pbLoudness);
      applySoundModeVisibility(els);
    });
  }
  if (els.stereoPreset) {
    for (const event of ["input", "change"]) els.stereoPreset.addEventListener(event, () => markDirty(els.stereoPreset));
  }
  for (const key of ["pbPrefetch", "pbSilenceTrigger"]) {
    const input = els[key];
    if (input) input.addEventListener("change", () => markDirty(input));
  }
  if (els.masteringSave) els.masteringSave.disabled = true;
  els.masteringSave?.addEventListener("click", async () => {
    if (saveState.saving) return;
    const values = {};
    for (const spec of NUMBER_FIELDS.filter(({ key }) => MANUAL_KEYS.has(key))) {
      const input = els[spec.key];
      if (!input || !_dirty.has(input)) continue;
      const value = parseSettingValue(input.value, spec);
      if (value === null) {
        input.setAttribute("aria-invalid", "true");
        input.focus();
        announceStatus(settingsStatus, invalidText(spec),
          { dwellMs: 6000, force: true, tone: "error" });
        return;
      }
      input.removeAttribute("aria-invalid");
      values[spec.field] = value;
    }
    if (els.pbLoudness && _dirty.has(els.pbLoudness)) {
      if (!["off", "replaygain", "smooth", "stereotool"].includes(els.pbLoudness.value)) {
        els.pbLoudness.focus();
        announceStatus(settingsStatus, "Choose a valid output processing mode.",
          { dwellMs: 6000, force: true, tone: "error" });
        return;
      }
      values[LOUDNESS_FIELD.field] = els.pbLoudness.value;
    }
    if (els.stereoPreset && _dirty.has(els.stereoPreset)) values.stereotool_preset = els.stereoPreset.value;
    if (els.pbPrefetch && _dirty.has(els.pbPrefetch)) values.prefetch_next_track = els.pbPrefetch.checked;
    if (els.pbSilenceTrigger && _dirty.has(els.pbSilenceTrigger)) {
      values.silence_trigger_crossfade = els.pbSilenceTrigger.checked;
    }
    if (!Object.keys(values).length) return;
    const submitted = new Map();
    for (const spec of NUMBER_FIELDS.filter(({ key }) => MANUAL_KEYS.has(key))) {
      const input = els[spec.key];
      if (input && Object.hasOwn(values, spec.field)) submitted.set(input, input.value);
    }
    for (const input of [els.pbLoudness, els.stereoPreset, els.pbPrefetch, els.pbSilenceTrigger]) {
      if (input && _dirty.has(input)) submitted.set(input,
        input.type === "checkbox" ? input.checked : input.value);
    }
    const epoch = captureAuthenticatedRequestEpoch();
    const station = getStation();
    const generation = saveState.generation;
    saveState.saving = true;
    els.masteringSave.disabled = true;
    let failure;
    const saved = await postSettings("/api/playback-settings", values, els.masteringSave,
      { onError: (error) => { failure = error; } });
    if (saveState.generation !== generation || !isAuthenticatedRequestCurrent(epoch)
        || getStation() !== station) return;
    saveState.saving = false;
    if (!saved) {
      if (failure) announceStatus(settingsStatus,
        `Could not save sound settings: ${failure.message} Your changes are still in the form.`,
        { dwellMs: 6000, force: true, tone: "error" });
      els.masteringSave.disabled = false;
      return;
    }
    for (const [input, value] of submitted) {
      const savedValue = input.type === "checkbox" ? value : String(value);
      const currentValue = input.type === "checkbox" ? input.checked : input.value;
      if (String(currentValue) === String(savedValue)) {
        _dirty.delete(input);
        _savedEcho.set(input, { value: savedValue, at: Date.now() });
        if (input.type !== "checkbox" && input !== els.stereoPreset && input !== els.pbLoudness) {
          _serverValue.set(input, Number(value));
        }
      }
    }
    const controls = [
      ...NUMBER_FIELDS.filter(({ key }) => MANUAL_KEYS.has(key)).map(({ key }) => els[key]),
      els.pbLoudness, els.stereoPreset, els.pbPrefetch, els.pbSilenceTrigger,
    ].filter(Boolean);
    if (els.masteringSave) els.masteringSave.disabled = !controls.some((control) => _dirty.has(control));
    if (Object.hasOwn(values, "stereotool_preset")) onSaved(values);
    announceStatus(settingsStatus, "Sound settings saved.",
      { dwellMs: 5000, force: true });
  });
  if (els.djFilterSweep) {
    els.djFilterSweep.addEventListener("change", async () => {
      const box = els.djFilterSweep;
      const ok = await postSettings("/api/djmix", { filter_sweep: box.checked }, box);
      if (!ok) box.checked = !box.checked;
    });
  }
}
