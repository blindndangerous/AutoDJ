// Settings panel: the controls that save one server setting each, the
// state sync from the websocket push, and the postSettings helper.
//
// FIELDS lists the plain controls once: where each saves and how it reads
// the pushed settings.  Pick mode, the BPM range and discovery carry
// their own rules and are handled by name below.

import { escHtml } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";

let _lastPresetOptionsKey = "";
// The value the server last confirmed for each control, so a failed save
// can put the control straight back instead of leaving the user's choice
// on screen until the next websocket push flips it silently.
const _confirmed = new WeakMap();

function isToggle(control) {
  return control.type === "checkbox" || control.type === "radio";
}

function confirmValue(control) {
  _confirmed.set(control, isToggle(control) ? control.checked : control.value);
}

function controlValue(control) {
  return isToggle(control) ? control.checked : control.value;
}

// The save in flight for each control, and then the value it saved until
// a push carries it.  A push that left the server before the save holds
// the old value, and writing it back unticked a checkbox the listener had
// just ticked (and spoke nothing).  So a pushed value is not written to a
// control while its save is out, nor after the reply until a push agrees
// with it; a failed save ends the hold at once (revertAfterFailure).
// ECHO_WAIT_MS only backstops an echo that never comes, for instance a
// value the server stores in another form.
const _saves = new WeakMap();   // control -> { value, answeredAt }
const ECHO_WAIT_MS = 5000;

function sameValue(pushed, saved) {
  if (typeof saved === "boolean") return Boolean(pushed) === saved;
  if (pushed == null) return saved === "";
  if (String(pushed) === saved) return true;
  return saved !== "" && Number(pushed) === Number(saved);
}

// True while a pushed value must be kept off *control*.
export function saveHolds(control, pushed) {
  const save = control && _saves.get(control);
  if (!save) return false;
  if (save.answeredAt === 0) return true;
  if (sameValue(pushed, save.value) || Date.now() - save.answeredAt > ECHO_WAIT_MS) {
    _saves.delete(control);
    return false;
  }
  return true;
}

function spokenValue(control) {
  if (isToggle(control)) return control.checked ? "on" : "off";
  if (control.tagName === "SELECT") {
    return control.selectedOptions?.[0]?.textContent.trim() || "unset";
  }
  return control.value === "" ? "blank" : control.value;
}

// "Could not save Beatmatch; it is still on."  The label is the name the
// user just heard on the control.
function revertAfterFailure(control) {
  if (!control || !_confirmed.has(control)) return "";
  const value = _confirmed.get(control);
  if (isToggle(control)) control.checked = value;
  else control.value = value;
  const name = (control.labels?.[0]?.textContent || "").replace(/\s+/g, " ").trim();
  return name ? `Could not save ${name}; it is still ${spokenValue(control)}.` : "";
}

export function resetSettingsState(els) {
  for (const control of Object.values(els)) {
    if (!control) continue;
    if (control === els.presetSelect) {
      control.innerHTML = '<option value="">(none)</option>';
      control.value = "";
    } else if (isToggle(control)) {
      control.checked = control.defaultChecked;
    } else if (control.tagName === "SELECT") {
      control.selectedIndex = Math.max(0,
        Array.from(control.options).findIndex((option) => option.defaultSelected));
    } else {
      control.value = control.defaultValue;
    }
  }
  // Every N songs is only editable while discovery is on.
  if (els.discEvery) els.discEvery.disabled = !els.discEnabled?.checked;
  _lastPresetOptionsKey = "";
}

export async function postSettings(url, body, { settingsStatus, control } = {}) {
  const epoch = captureAuthenticatedRequestEpoch();
  const save = control ? { value: controlValue(control), answeredAt: 0 } : null;
  if (save) _saves.set(control, save);
  const ownSave = () => save !== null && _saves.get(control) === save;
  try {
    await withDisabled(control, () => requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
    if (!isAuthenticatedRequestCurrent(epoch)) {
      if (ownSave()) _saves.delete(control);
      return false;
    }
    if (ownSave()) save.answeredAt = Date.now();
    if (control) confirmValue(control);
    return true;
  } catch (err) {
    if (ownSave()) _saves.delete(control);
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    const reverted = revertAfterFailure(control);
    // The settings card is several screens tall, so the failure has to
    // travel to the visible toast as well as the live region.
    announceStatus(settingsStatus,
      reverted ? `${reverted} ${err.message}` : `Could not save. ${err.message}`,
      { dwellMs: 6000, force: true, tone: "error" });
    return false;
  }
}

const PLAYBACK = "/api/playback-settings";
const DJMIX = "/api/djmix";

// key: the control in els.  url: where it saves (playback settings when
// absent) and field: the body key.  A checkbox saves its checked state;
// any other control saves send(value), its value when there is no send,
// and nothing when send gives undefined.  read gives the pushed value, a
// playback field of the same name when absent; null or undefined leaves
// the control alone.
const FIELDS = [
  { key: "presetSelect", url: "/api/preset", field: "name",
    read: (st) => st.preset || "", send: (v) => v || null },
  { key: "transitionSelect", url: "/api/transition", field: "effect",
    read: (st) => st.transition || "none" },
  { key: "harmonicMode", url: DJMIX, field: "harmonic_mode", read: (st) => st.djmix?.harmonic_mode },
  { key: "djBeatmatch", url: DJMIX, field: "beatmatch", read: (st) => Boolean(st.djmix?.beatmatch) },
  { key: "djDropMix", url: DJMIX, field: "drop_mix", read: (st) => Boolean(st.djmix?.drop_mix) },
  { key: "djKeyShift", url: DJMIX, field: "key_shift", read: (st) => Boolean(st.djmix?.key_shift) },
  { key: "djPhraseAlign", url: DJMIX, field: "phrase_align",
    read: (st) => Boolean(st.djmix?.phrase_align) },
  { key: "djOutroIntro", url: DJMIX, field: "outro_intro_align",
    read: (st) => Boolean(st.djmix?.outro_intro_align) },
  { key: "pbEqDuck", field: "crossfade_eq_duck" },
  { key: "pbShowLyrics", field: "show_lyrics",
    read: (st) => Boolean(st.playback) && st.playback.show_lyrics !== false },
  { key: "pbAnchorSeed", field: "anchor_to_seed" },
  { key: "pbReplayGain", field: "replaygain_enabled" },
  { key: "pbBeatSyncFx", field: "beat_sync_fx" },
  { key: "pbKeySyncFx", field: "key_sync_fx" },
  { key: "pbBeatmatchSkip", field: "beatmatch_on_skip",
    read: (st) => st.playback?.beatmatch_on_skip === true },
  { key: "pbTransitionMode", field: "transition_mode" },
  { key: "pbPostQueueSeed", field: "post_queue_seed" },
  // No extra announcement for these two: the select and checkbox already
  // speak their new value.
  { key: "keyNotation", field: "key_notation" },
  { key: "keyPreferFlats", field: "key_prefer_flats" },
];

function pushedValue(spec, control, st) {
  if (spec.read) return spec.read(st);
  const value = st.playback?.[spec.field];
  return isToggle(control) ? Boolean(value) : value;
}

// save(url, body, control) is postSettings with the page's status region.
export function installSettingsControls(els, save) {
  for (const spec of FIELDS) {
    const control = els[spec.key];
    if (!control) continue;
    control.addEventListener("change", () => {
      let value = control.checked;
      if (!isToggle(control)) value = spec.send ? spec.send(control.value) : control.value;
      if (value !== undefined) void save(spec.url || PLAYBACK, { [spec.field]: value }, control);
    });
  }
  const { pbPickMode, bpmLo, bpmHi, discEnabled, discEvery } = els;
  // The pick mode select is the server's pure_shuffle flag.
  pbPickMode?.addEventListener("change", () => {
    void save(PLAYBACK, { pure_shuffle: pbPickMode.value === "pure" }, pbPickMode);
  });
  const saveBpmRange = (control) => {
    const lo = parseFloat(bpmLo.value);
    const hi = parseFloat(bpmHi.value);
    void save("/api/bpm-range", { lo: isNaN(lo) ? null : lo, hi: isNaN(hi) ? null : hi }, control);
  };
  bpmLo?.addEventListener("change", () => saveBpmRange(bpmLo));
  bpmHi?.addEventListener("change", () => saveBpmRange(bpmHi));
  const saveDiscovery = (control) => {
    const v = parseInt(discEvery.value, 10);
    void save("/api/discovery", { every: discEnabled.checked && v > 0 ? v : null }, control);
  };
  discEnabled?.addEventListener("change", () => {
    discEvery.disabled = !discEnabled.checked;
    saveDiscovery(discEnabled);
  });
  discEvery?.addEventListener("change", () => {
    if (discEnabled.checked) saveDiscovery(discEvery);
  });
}

export function applySettingsState(st, els) {
  const doc = Object.values(els).find(Boolean)?.ownerDocument || globalThis.document;
  const { presetSelect, pbPickMode, bpmLo, bpmHi, discEnabled, discEvery } = els;

  // Populate preset dropdown only when the option list changes.
  const optsKey = (st.available_presets || []).join("|");
  if (presetSelect && optsKey !== _lastPresetOptionsKey) {
    _lastPresetOptionsKey = optsKey;
    presetSelect.innerHTML = '<option value="">(none)</option>' +
      (st.available_presets || []).map((n) =>
        `<option value="${escHtml(n)}">${escHtml(n)}</option>`
      ).join("");
  }
  // A checkbox follows every push.  A select or field the user is on is
  // left alone: reassigning .value on each ~1 Hz echo closed an open
  // dropdown and overwrote typing.
  for (const spec of FIELDS) {
    const control = els[spec.key];
    if (!control) continue;
    const value = pushedValue(spec, control, st);
    if (value == null || saveHolds(control, value)) continue;
    if (isToggle(control)) control.checked = value;
    else if (doc.activeElement !== control && control.value !== String(value)) control.value = value;
  }
  const pickMode = st.playback?.pure_shuffle ? "pure" : "similarity";
  if (pbPickMode && doc.activeElement !== pbPickMode && !saveHolds(pbPickMode, pickMode)) {
    pbPickMode.value = pickMode;
  }
  for (const [input, bound] of [[bpmLo, "lo"], [bpmHi, "hi"]]) {
    if (!st.bpm_range || !input || doc.activeElement === input) continue;
    const value = st.bpm_range[bound] ?? "";
    if (!saveHolds(input, value)) input.value = value;
  }
  // One concept, one answer: the checkbox tracks the same runtime flag
  // the Now Playing Discovery button toggles, so the two controls can no
  // longer disagree.
  const discOn = st.discovery_every != null && Boolean(st.discovery_enabled);
  if (!saveHolds(discEnabled, discOn)) {
    discEnabled.checked = discOn;
    discEvery.disabled = !discOn;
  }
  if (doc.activeElement !== discEvery && discOn && !saveHolds(discEvery, st.discovery_every)) {
    discEvery.value = st.discovery_every;
  }
  // A focused control may hold a change still being saved, and so may
  // one whose save is out: neither shows a confirmed value.
  for (const control of Object.values(els)) {
    if (control && control !== doc.activeElement && !_saves.has(control)) confirmValue(control);
  }
}
