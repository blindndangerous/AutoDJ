// Settings panel state sync + postSettings helper.
//
// Listener wiring (`presetSelect.addEventListener`, ...) stays inline
// in app.js because each handler is a one-liner that calls
// postSettings with a single field; pulling them into the module
// would require funnelling every DOM ref + would not shorten the
// total surface area.

import { escHtml } from "./dom-helpers.js";
import { applyShowWhen } from "./show-when.js";
import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";

let _lastPresetOptionsKey = "";
const _controlDefaults = new WeakMap();
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

function rememberControlDefaults(els) {
  for (const control of Object.values(els)) {
    if (!control || _controlDefaults.has(control)) continue;
    _controlDefaults.set(control, {
      checked: control.defaultChecked,
      disabled: control.disabled,
      selectedIndex: control.selectedIndex,
      value: control.defaultValue,
    });
  }
}

export function resetSettingsState(els) {
  rememberControlDefaults(els);
  for (const control of Object.values(els)) {
    if (!control) continue;
    const defaults = _controlDefaults.get(control);
    if (control === els.presetSelect) {
      control.innerHTML = '<option value="">(none)</option>';
      control.value = "";
    } else if (control.tagName === "INPUT") {
      if (control.type === "checkbox" || control.type === "radio") {
        control.checked = defaults.checked;
      } else {
        control.value = defaults.value;
      }
    } else if (control.tagName === "SELECT") {
      control.selectedIndex = defaults.selectedIndex;
    }
    control.disabled = defaults.disabled;
  }
  _lastPresetOptionsKey = "";
  applyShowWhen();
}

export async function postSettings(url, body, { settingsStatus, control } = {}) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    await withDisabled(control, () => requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    if (control) confirmValue(control);
    return true;
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    const reverted = revertAfterFailure(control);
    if (reverted) applyShowWhen();
    // The settings card is several screens tall, so the failure has to
    // travel to the visible toast as well as the live region.
    announceStatus(settingsStatus,
      reverted ? `${reverted} ${err.message}` : `Could not save. ${err.message}`,
      { dwellMs: 6000, force: true, tone: "error" });
    return false;
  }
}

export function applySettingsState(st, els) {
  rememberControlDefaults(els);
  const {
    presetSelect, transitionSelect,
    harmonicMode,
    djBeatmatch, djPhraseAlign, djOutroIntro,
    pbEqDuck, pbPickMode, pbShowLyrics, pbAnchorSeed,
    pbReplayGain,
    pbDaypart, pbMoodArc, pbMoodArcHours, pbImportCues,
    pbBeatSyncFx, pbKeySyncFx, pbBeatmatchSkip,
    pbTransitionMode, pbPostQueueSeed, pbCrossfade, pbFadeIn,
    keyNotation, keyPreferFlats,
    bpmLo, bpmHi,
    discEnabled, discEvery,
  } = els;

  // Populate preset dropdown only when the option list changes.
  const optsKey = (st.available_presets || []).join("|");
  if (optsKey !== _lastPresetOptionsKey) {
    _lastPresetOptionsKey = optsKey;
    presetSelect.innerHTML = '<option value="">(none)</option>' +
      (st.available_presets || []).map((n) =>
        `<option value="${escHtml(n)}">${escHtml(n)}</option>`
      ).join("");
  }
  // Without the focus-check guard, every WS state echo (~1 Hz)
  // reassigns .value, which closes the dropdown and shifts focus
  // mid-selection.
  if (document.activeElement !== presetSelect) presetSelect.value = st.preset || "";

  if (document.activeElement !== transitionSelect) {
    transitionSelect.value = st.transition || "none";
  }

  if (st.djmix && document.activeElement !== harmonicMode) {
    const mode = st.djmix.harmonic_mode;
    if (harmonicMode.value !== mode) harmonicMode.value = mode;
  }
  djBeatmatch.checked   = !!(st.djmix && st.djmix.beatmatch);
  djPhraseAlign.checked = !!(st.djmix && st.djmix.phrase_align);
  djOutroIntro.checked  = !!(st.djmix && st.djmix.outro_intro_align);

  pbEqDuck.checked       = !!(st.playback && st.playback.crossfade_eq_duck);
  if (pbPickMode && document.activeElement !== pbPickMode) {
    // Project the two server-side flags back to the three-way select.
    let mode = "similarity";
    if (st.playback && st.playback.pure_shuffle) mode = "pure";
    else if (st.playback && st.playback.smart_shuffle) mode = "smart";
    pbPickMode.value = mode;
  }
  pbShowLyrics.checked   = (st.playback && st.playback.show_lyrics !== false);
  pbAnchorSeed.checked   = !!(st.playback && st.playback.anchor_to_seed);
  pbReplayGain.checked   = !!(st.playback && st.playback.replaygain_enabled);
  if (pbDaypart) pbDaypart.checked = !!(st.playback && st.playback.enable_daypart);
  if (pbMoodArc) pbMoodArc.checked = !!(st.playback && st.playback.enable_mood_arc);
  if (pbMoodArcHours && st.playback && typeof st.playback.mood_arc_hours === "number") {
    pbMoodArcHours.value = st.playback.mood_arc_hours;
  }
  if (pbImportCues) {
    pbImportCues.checked = !!(st.playback && st.playback.import_external_cues);
  }
  if (pbBeatSyncFx) pbBeatSyncFx.checked = !!(st.playback && st.playback.beat_sync_fx);
  if (pbKeySyncFx) pbKeySyncFx.checked = !!(st.playback && st.playback.key_sync_fx);
  if (pbBeatmatchSkip) {
    pbBeatmatchSkip.checked = !!(st.playback && st.playback.beatmatch_on_skip === true);
  }
  if (st.playback && st.playback.transition_mode &&
      document.activeElement !== pbTransitionMode) {
    pbTransitionMode.value = st.playback.transition_mode;
  }
  if (pbPostQueueSeed && st.playback && st.playback.post_queue_seed &&
      document.activeElement !== pbPostQueueSeed) {
    pbPostQueueSeed.value = st.playback.post_queue_seed;
  }
  if (st.playback && document.activeElement !== pbCrossfade) {
    pbCrossfade.value = st.playback.crossfade_seconds;
  }
  if (pbFadeIn && st.playback && document.activeElement !== pbFadeIn) {
    pbFadeIn.value = st.playback.fade_in_seconds;
  }
  if (keyNotation && st.playback && st.playback.key_notation &&
      document.activeElement !== keyNotation) {
    keyNotation.value = st.playback.key_notation;
  }
  if (keyPreferFlats && st.playback) {
    keyPreferFlats.checked = !!st.playback.key_prefer_flats;
  }
  if (st.bpm_range && document.activeElement !== bpmLo) {
    bpmLo.value = st.bpm_range.lo != null ? st.bpm_range.lo : "";
  }
  if (st.bpm_range && document.activeElement !== bpmHi) {
    bpmHi.value = st.bpm_range.hi != null ? st.bpm_range.hi : "";
  }
  // One concept, one answer: the checkbox tracks the same runtime flag
  // the Now Playing Discovery button toggles, so the two controls can no
  // longer disagree.
  const discOn = st.discovery_every != null && Boolean(st.discovery_enabled);
  discEnabled.checked = discOn;
  if (document.activeElement !== discEvery && discOn) {
    discEvery.value = st.discovery_every;
  }
  discEvery.disabled = !discOn;
  // A focused control may hold a change still being saved.
  for (const control of Object.values(els)) {
    if (control && control !== document.activeElement) confirmValue(control);
  }
  applyShowWhen();
}
