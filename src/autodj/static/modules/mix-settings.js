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

import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { applySoundModeVisibility } from "./settings-panel.js";
import { registerSettingsGroup, settingsDraftHolds } from "./settings-drafts.js";

const drafts = new WeakMap();
const NUMBER_FIELDS = [
  {
    key: "pbCrossfade", group: "playback", field: "crossfade_seconds",
    label: "Crossfade seconds", min: 0, max: 20, integer: false,
    read: (st) => st.playback?.crossfade_seconds,
  },
  {
    key: "pbFadeIn", group: "playback", field: "fade_in_seconds",
    label: "Fade-in seconds", min: 0, max: 20, integer: false,
    read: (st) => st.playback?.fade_in_seconds,
  },
  {
    key: "djPhraseBars", group: "djmix", field: "phrase_bars",
    label: "Phrase length", min: 1, max: 64, integer: true,
    read: (st) => st.djmix?.phrase_bars,
  },
  {
    // Shown in percent, stored as a fraction.
    key: "txWetMix", group: "playback", field: "transition_wet_mix",
    label: "Transition effect level", min: 0, max: 100, integer: false,
    read: (st) => (typeof st.playback?.transition_wet_mix === "number"
      ? Math.round(st.playback.transition_wet_mix * 100) : undefined),
    toServer: (value) => value / 100,
  },
  {
    key: "pbRgTarget", group: "playback", field: "replaygain_target_db",
    label: "ReplayGain target loudness", min: -30, max: 0, integer: false,
    read: (st) => st.playback?.replaygain_target_db,
  },
  {
    key: "pbRgClip", group: "playback", field: "replaygain_max_clip_safe_gain",
    label: "ReplayGain peak limit", min: 0.1, max: 2, integer: false,
    read: (st) => st.playback?.replaygain_max_clip_safe_gain,
  },
  {
    key: "pbBassCutoff", group: "playback", field: "crossfade_bass_cutoff_hz",
    label: "EQ duck cutoff", min: 20, max: 1000, integer: false,
    read: (st) => st.playback?.crossfade_bass_cutoff_hz,
  },
  {
    key: "pbPickTopK", group: "playback", field: "pick_top_k",
    label: "Choose among the closest songs", min: 1, max: 100, integer: true,
    read: (st) => st.playback?.pick_top_k,
  },
  {
    key: "pbPickTemperature", group: "playback", field: "pick_temperature",
    label: "Pick randomness", min: 0, max: 10, integer: false,
    read: (st) => st.playback?.pick_temperature,
  },
  {
    key: "pbHourFitWindow", group: "playback", field: "hour_fit_window_minutes",
    label: "Minutes before to start fitting", min: 1, max: 30, integer: true,
    read: (st) => st.playback?.hour_fit_window_minutes,
  },
  {
    // Shown in percent, stored as a fraction.
    key: "djMaxStretch", group: "djmix", field: "beatmatch_max_stretch",
    label: "Beatmatch stretch limit", min: 0, max: 50, integer: false,
    read: (st) => (typeof st.djmix?.beatmatch_max_stretch === "number"
      ? Math.round(st.djmix.beatmatch_max_stretch * 1000) / 10 : undefined),
    toServer: (value) => value / 100,
  },
  {
    key: "djGlideBars", group: "djmix", field: "beatmatch_glide_bars",
    label: "Glide back", min: 0, max: 64, integer: true,
    read: (st) => st.djmix?.beatmatch_glide_bars,
  },
  {
    key: "djSweepFloor", group: "djmix", field: "filter_sweep_floor_hz",
    label: "Filter sweep floor", min: 20, max: 5000, integer: false,
    read: (st) => st.djmix?.filter_sweep_floor_hz,
  },
  {
    key: "pbIntroLift", group: "playback", field: "intro_lift_max_db",
    label: "Quiet intro lift limit", min: 0, max: 6, integer: false,
    read: (st) => st.playback?.intro_lift_max_db,
  },
  {
    key: "pbStereoTrim", group: "playback", field: "stereotool_input_trim_db",
    label: "Stereo Tool input trim", min: -12, max: 6, integer: false,
    read: (st) => st.playback?.stereotool_input_trim_db,
  },
  {
    key: "pbStereoAgcDrive", group: "playback", field: "stereotool_agc_drive_db",
    label: "AGC drive boost", min: 0, max: 12, integer: false,
    read: (st) => st.playback?.stereotool_agc_drive_db,
  },
  {
    key: "pbMusicSpeed", group: "playback", field: "music_speed_percent",
    label: "Music speed", min: -10, max: 10, integer: false,
    read: (st) => st.playback?.music_speed_percent ?? 0,
  },
];

const LOUDNESS_FIELD = {
  read: (st) => st.playback?.loudness_processing ?? (st.playback?.replaygain_enabled ? "replaygain" : "off"),
};
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
  for (const input of Object.values(els).filter(Boolean)) {
    const binding = drafts.get(input);
    if (binding && st[binding.group]) binding.draft.accept(st[binding.group]);
  }
  for (const spec of NUMBER_FIELDS) {
    const input = els[spec.key];
    const value = spec.read(st);
    if (!input || typeof value !== "number" || settingsDraftHolds(input)) continue;
    if (doc?.activeElement !== input && input.value !== String(value)) {
      input.value = String(value);
    }
  }
  const loudness = els.pbLoudness;
  const loudnessValue = LOUDNESS_FIELD.read(st);
  if (loudness && typeof loudnessValue === "string" && !settingsDraftHolds(loudness)) {
    loudness.value = loudnessValue;
  }
  if (els.stereoPreset && typeof st.playback?.stereotool_preset === "string"
      && !settingsDraftHolds(els.stereoPreset)) {
    els.stereoPreset.value = st.playback.stereotool_preset;
  }
  for (const [key, field] of [["pbPrefetch", "prefetch_next_track"],
    ["pbSilenceTrigger", "silence_trigger_crossfade"]]) {
    const input = els[key];
    if (input && typeof st.playback?.[field] === "boolean" && !settingsDraftHolds(input)) {
      input.checked = st.playback[field];
    }
  }
  applySoundModeVisibility(els, st, doc);
  const sweep = els.djFilterSweep;
  if (sweep && st.djmix && typeof st.djmix.filter_sweep === "boolean"
      && !settingsDraftHolds(sweep)) {
    sweep.checked = st.djmix.filter_sweep;
  }
}

export function resetMixSettings(els) {
  for (const input of Object.values(els)) {
    if (!input || input.tagName === "BUTTON") continue;
    if (input.type === "checkbox") input.checked = input.defaultChecked;
    else if (input.tagName !== "SELECT") input.value = input.defaultValue;
  }
}

export function installMixSettings(els, { settingsStatus }) {
  for (const spec of NUMBER_FIELDS) {
    const input = els[spec.key];
    if (!input) continue;
    const error = input.ownerDocument.createElement("p");
    error.id = `${input.id}-error`;
    error.className = "field-error";
    error.hidden = true;
    input.after(error);
    const clearError = () => clearFieldError(input, error);
    input.addEventListener("input", clearError);
    const group = spec.group;
    const draft = registerSettingsGroup({
      panel: input.closest('[id^="spanel-"]'), group, controls: [input],
      read: () => ({ [spec.field]: spec.toServer ? spec.toServer(Number(input.value)) : Number(input.value) }),
      write(value) {
        const shown = spec.read({ [spec.group]: value });
        if (shown != null) input.value = shown;
        clearError();
      },
      validate() {
        if (parseSettingValue(input.value, spec) !== null) { clearError(); return true; }
        showFieldError(input, invalidText(spec), error, settingsStatus);
        return false;
      },
    });
    drafts.set(input, { draft, group });
  }
  for (const [key, field, group] of [
    ["pbLoudness", "loudness_processing", "playback"], ["stereoPreset", "stereotool_preset", "playback"],
    ["pbPrefetch", "prefetch_next_track", "playback"], ["pbSilenceTrigger", "silence_trigger_crossfade", "playback"],
    ["djFilterSweep", "filter_sweep", "djmix"],
  ]) {
    const input = els[key];
    if (!input) continue;
    const draft = registerSettingsGroup({ panel: input.closest('[id^="spanel-"]'), group, controls: [input],
      read: () => ({ [field]: input.type === "checkbox" ? input.checked : input.value }),
      write(value) {
        if (!(field in value)) return;
        if (input.type === "checkbox") input.checked = value[field]; else input.value = value[field];
        applySoundModeVisibility(els);
      },
    });
    drafts.set(input, { draft, group });
    input.addEventListener("change", () => applySoundModeVisibility(els));
  }
}
