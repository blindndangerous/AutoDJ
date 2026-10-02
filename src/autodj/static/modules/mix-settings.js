// Settings number fields: crossfade and fade-in length, repeat windows,
// phrase length, transition effect level and the ReplayGain target, plus
// the filter sweep switch.
//
// Each number field carries the same range the server accepts
// (PlaybackSettingsBody / DjMixBody in settings_bodies.py), so a value the server
// would refuse is caught here and said in words instead of arriving as a
// 422.  A refused or failed value goes straight back to what the server
// has, so the field never shows a setting that is not in effect.

import { announceStatus } from "./live-region.js";
import { saveHolds } from "./settings-panel.js";

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
    key: "pbNoRepeat", url: "/api/playback-settings", field: "no_repeat_window",
    label: "Tracks before a song can repeat", min: 0, max: 100000, integer: true,
    read: (st) => st.playback?.no_repeat_window,
  },
  {
    key: "pbArtistRepeat", url: "/api/playback-settings", field: "artist_repeat_window",
    label: "Tracks before an artist can repeat", min: 0, max: 100, integer: true,
    read: (st) => st.playback?.artist_repeat_window,
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
];

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
    if (!input || typeof value !== "number") continue;
    // A save still out (or not yet echoed) keeps the field's new value.
    if (saveHolds(input, value)) continue;
    _serverValue.set(input, value);
    if (doc?.activeElement !== input && input.value !== String(value)) {
      input.value = String(value);
    }
  }
  const sweep = els.djFilterSweep;
  if (sweep && st.djmix && typeof st.djmix.filter_sweep === "boolean"
      && !saveHolds(sweep, st.djmix.filter_sweep)) {
    sweep.checked = st.djmix.filter_sweep;
  }
}

export function resetMixSettings(els) {
  for (const input of Object.values(els)) {
    if (!input) continue;
    if (input.type === "checkbox") input.checked = input.defaultChecked;
    else input.value = input.defaultValue;
    _serverValue.delete(input);
  }
}

// postSettings(url, body, control) resolves true on success and has
// already reported a failure in the settings status region.
export function installMixSettings(els, { postSettings, settingsStatus }) {
  for (const spec of NUMBER_FIELDS) {
    const input = els[spec.key];
    if (!input) continue;
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
  if (els.djFilterSweep) {
    els.djFilterSweep.addEventListener("change", async () => {
      const box = els.djFilterSweep;
      const ok = await postSettings("/api/djmix", { filter_sweep: box.checked }, box);
      if (!ok) box.checked = !box.checked;
    });
  }
}
