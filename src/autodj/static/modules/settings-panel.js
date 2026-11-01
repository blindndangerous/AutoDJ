import { registerSettingsGroup, settingsDraftHolds } from "./settings-drafts.js";
// Settings controls register their groups with the shared tab draft controller.
//
// FIELDS lists the plain controls once: where each saves and how it reads
// the pushed settings.  Pick mode, the BPM range and discovery carry
// their own rules and are handled by name below.

import { escHtml } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { failureText,
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";

let _lastPresetOptionsKey = "";
const drafts = new WeakMap();
function isToggle(control) {
  return control.type === "checkbox" || control.type === "radio";
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

// Individual record editors keep their explicit submission and field errors.
export async function postSettings(url, body, { settingsStatus, control, onError } = {}) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    await withDisabled(control, () => requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
    return isAuthenticatedRequestCurrent(epoch);
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    if (onError) onError(err);
    else announceStatus(settingsStatus, failureText("save.", err),
      { dwellMs: 6000, force: true, tone: "error" });
    return false;
  }
}

// key: the control in els. group: playback when absent; field: the body key.
// A checkbox saves its checked state;
// any other control saves send(value), its value when there is no send,
// and nothing when send gives undefined.  read gives the pushed value, a
// playback field of the same name when absent; null or undefined leaves
// the control alone.
const FIELDS = [
  { key: "presetSelect", group: "preset", field: "name",
    read: (st) => st.preset || "", send: (v) => v || null },
  { key: "transitionSelect", group: "transition", field: "effect",
    read: (st) => st.transition || "none" },
  { key: "harmonicMode", group: "djmix", field: "harmonic_mode", read: (st) => st.djmix?.harmonic_mode },
  { key: "djBeatmatch", group: "djmix", field: "beatmatch", read: (st) => Boolean(st.djmix?.beatmatch) },
  { key: "djDropMix", group: "djmix", field: "drop_mix", read: (st) => Boolean(st.djmix?.drop_mix) },
  { key: "djKeyShift", group: "djmix", field: "key_shift", read: (st) => Boolean(st.djmix?.key_shift) },
  { key: "djPhraseAlign", group: "djmix", field: "phrase_align",
    read: (st) => Boolean(st.djmix?.phrase_align) },
  { key: "djOutroIntro", group: "djmix", field: "outro_intro_align",
    read: (st) => Boolean(st.djmix?.outro_intro_align) },
  // On unless the server says otherwise.
  { key: "djVocalGuard", group: "djmix", field: "vocal_guard",
    read: (st) => Boolean(st.djmix) && st.djmix.vocal_guard !== false },
  { key: "pbEqDuck", field: "crossfade_eq_duck" },
  { key: "pbShowLyrics", field: "show_lyrics",
    read: (st) => Boolean(st.playback) && st.playback.show_lyrics !== false },
  { key: "pbAnchorSeed", field: "anchor_to_seed" },
  { key: "pbHourFit", field: "hour_fit" },
  {
    key: "pbLoudness", field: "loudness_processing",
    read: (st) => st.playback?.loudness_processing ??
      (st.playback?.replaygain_enabled ? "replaygain" : "off"),
  },
  { key: "pbBeatSyncFx", field: "beat_sync_fx" },
  { key: "pbKeySyncFx", field: "key_sync_fx" },
  { key: "pbBeatmatchSkip", field: "beatmatch_on_skip",
    read: (st) => st.playback?.beatmatch_on_skip === true },
  { key: "pbSkipStyle", field: "skip_style" },
  { key: "lnTalkUp", field: "liners_talk_up" },
  { key: "lnMatchedIntroOnlyWithoutLiner", field: "matched_intro_only_without_liner" },
  { key: "pbTransitionMode", field: "transition_mode" },
  { key: "pbPostQueueSeed", field: "post_queue_seed" },
  // No extra announcement for these two: the select and checkbox already
  // speak their new value.
  { key: "keyNotation", field: "key_notation" },
  { key: "keyPreferFlats", field: "key_prefer_flats" },
  // On unless the server says otherwise, as the browser deck reads them.
  { key: "pbPrefetch", field: "prefetch_next_track",
    read: (st) => Boolean(st.playback) && st.playback.prefetch_next_track !== false },
  { key: "pbSilenceTrigger", field: "silence_trigger_crossfade",
    read: (st) => Boolean(st.playback) && st.playback.silence_trigger_crossfade !== false },
  { key: "pbMusicSpeed", field: "music_speed_percent",
    read: (st) => st.playback?.music_speed_percent ?? 0 },
];

function pushedValue(spec, control, st) {
  if (spec.read) return spec.read(st);
  const value = st.playback?.[spec.field];
  return isToggle(control) ? Boolean(value) : value;
}

function setControlVisible(control, visible) {
  if (!control) return;
  const row = control.closest(".settings-row, .settings-check");
  if (row) row.hidden = !visible;
  for (const id of (control.getAttribute("data-help") || "").split(/\s+/)) {
    if (id) {
      const description = control.ownerDocument.getElementById(id);
      if (description) description.hidden = !visible;
    }
  }
}

export function applySoundModeVisibility(els, st = {}, doc = Object.values(els).find(Boolean)?.ownerDocument || globalThis.document) {
  const loudness = els.pbLoudness?.value || doc.getElementById("pb-replaygain")?.value
    || st.playback?.loudness_processing || (st.playback?.replaygain_enabled ? "replaygain" : "off");
  setControlVisible(els.pbRgTarget || doc.getElementById("pb-rg-target"),
    loudness === "replaygain");
  setControlVisible(els.pbQuietBoost || doc.getElementById("pb-quiet-boost"),
    loudness === "smooth" || loudness === "stereotool");
}

function applyModeVisibility(st, els, doc) {
  const savedPoints = (els.pbTransitionMode?.value || st.playback?.transition_mode)
    === "saved_points";
  for (const key of [
    "transitionSelect", "pbEqDuck", "djBeatmatch", "djDropMix", "djKeyShift",
    "djPhraseAlign", "djOutroIntro", "djVocalGuard",
    "pbBeatSyncFx", "pbKeySyncFx",
  ]) {
    setControlVisible(els[key], !savedPoints);
  }
  setControlVisible(els.djFilterSweep || doc.getElementById("dj-filter-sweep"), !savedPoints);
  setControlVisible(els.pbFadeIn || doc.getElementById("pb-fade-in"), !savedPoints);
  setControlVisible(els.djPhraseBars || doc.getElementById("dj-phrase-bars"), !savedPoints);
  setControlVisible(els.txWetMix || doc.getElementById("tx-wet-mix"), !savedPoints);
  for (const id of ["dj-max-stretch", "dj-glide-bars", "dj-sweep-floor", "pb-bass-cutoff"]) {
    setControlVisible(doc.getElementById(id), !savedPoints);
  }
  // Whole groups that do nothing with saved segue points (the effect and
  // DJ-mix fieldsets), and the notes that say so in their place.
  for (const group of doc.querySelectorAll('[data-saved-points="hide"]')) {
    group.hidden = savedPoints;
  }
  for (const note of doc.querySelectorAll('[data-saved-points="note"]')) {
    note.hidden = !savedPoints;
  }
  applySoundModeVisibility(els, st, doc);
  const note = els.pbSavedPointsNote || doc.getElementById("pb-saved-points-note");
  if (note) note.hidden = !savedPoints;
}

export function installSettingsControls(els) {
  const bind = (group, controls, read, write, fields) => {
    const draft = registerSettingsGroup({ panel: controls.find(Boolean)?.closest('[id^="spanel-"]'), group,
      controls: controls.filter(Boolean), read, write, fields });
    for (const control of controls.filter(Boolean)) drafts.set(control, draft);
    return draft;
  };
  for (const spec of FIELDS) {
    const control = els[spec.key];
    if (!control) continue;
    const group = spec.group || "playback";
    bind(group, [control], () => ({ [spec.field]: isToggle(control) ? control.checked
      : spec.send ? spec.send(control.value) : control.value }), (value) => {
      if (!(spec.field in value)) return;
      if (isToggle(control)) control.checked = Boolean(value[spec.field]);
      else control.value = value[spec.field] ?? "";
      applyModeVisibility({}, els, control.ownerDocument);
    });
    control.addEventListener("change", () => applyModeVisibility({}, els, control.ownerDocument));
  }
  const { pbPickMode, bpmLo, bpmHi, discEnabled, discEvery } = els;
  if (pbPickMode) bind("playback", [pbPickMode], () => ({ pick_mode: pbPickMode.value }),
    (value) => { if (value.pick_mode != null) pbPickMode.value = value.pick_mode; });
  if (bpmLo && bpmHi) bind("bpm_range", [bpmLo, bpmHi],
    () => ({ lo: bpmLo.value === "" ? null : Number(bpmLo.value), hi: bpmHi.value === "" ? null : Number(bpmHi.value) }),
    (value) => { bpmLo.value = value.lo ?? ""; bpmHi.value = value.hi ?? ""; }, { lo: bpmLo, hi: bpmHi });
  if (discEnabled && discEvery) {
    bind("discovery", [discEnabled, discEvery], () => ({ every: discEnabled.checked ? Number(discEvery.value) : null }),
      (value) => {
        discEnabled.checked = value.every != null;
        if (value.every != null) discEvery.value = value.every;
        discEvery.disabled = !discEnabled.checked;
      }, { every: discEvery });
    discEnabled.addEventListener("change", () => { discEvery.disabled = !discEnabled.checked; });
  }
}

export function applySettingsState(st, els) {
  const doc = Object.values(els).find(Boolean)?.ownerDocument || globalThis.document;
  const { presetSelect, pbPickMode, bpmLo, bpmHi, discEnabled, discEvery } = els;

  // Populate preset dropdown only when the option list changes.
  const optsKey = (st.available_presets || []).join("|");
  if (presetSelect && optsKey !== _lastPresetOptionsKey) {
    const selected = presetSelect.value;
    _lastPresetOptionsKey = optsKey;
    presetSelect.innerHTML = '<option value="">(none)</option>' +
      (st.available_presets || []).map((n) =>
        `<option value="${escHtml(n)}">${escHtml(n)}</option>`
      ).join("");
    presetSelect.value = selected;
  }
  // A checkbox follows every push.  A select or field the user is on is
  // left alone: reassigning .value on each ~1 Hz echo closed an open
  // dropdown and overwrote typing.
  for (const spec of FIELDS) {
    const control = els[spec.key];
    if (!control) continue;
    const value = pushedValue(spec, control, st);
    if (value != null) drafts.get(control)?.accept({ [spec.field]: value });
    if (value == null || settingsDraftHolds(control)) continue;
    if (isToggle(control)) control.checked = value;
    else if (doc.activeElement !== control && control.value !== String(value)) control.value = value;
  }
  const pickMode = st.playback?.pick_mode ?? "similarity";
  drafts.get(pbPickMode)?.accept({ pick_mode: pickMode });
  if (st.bpm_range) drafts.get(bpmLo)?.accept(st.bpm_range);
  if (st.discovery_every !== undefined) drafts.get(discEnabled)?.accept({ every: st.discovery_enabled ? st.discovery_every : null });
  if (pbPickMode && doc.activeElement !== pbPickMode && !settingsDraftHolds(pbPickMode)) {
    pbPickMode.value = pickMode;
  }
  for (const [input, bound] of [[bpmLo, "lo"], [bpmHi, "hi"]]) {
    if (!st.bpm_range || !input || doc.activeElement === input) continue;
    const value = st.bpm_range[bound] ?? "";
    if (!settingsDraftHolds(input)) input.value = value;
  }
  // One concept, one answer: the checkbox tracks the same runtime flag
  // the Now Playing Discovery button toggles, so the two controls can no
  // longer disagree.
  const discOn = st.discovery_every != null && Boolean(st.discovery_enabled);
  if (!settingsDraftHolds(discEnabled)) {
    discEnabled.checked = discOn;
    discEvery.disabled = !discOn;
  }
  if (doc.activeElement !== discEvery && discOn && !settingsDraftHolds(discEvery)) {
    discEvery.value = st.discovery_every;
  }
  applyModeVisibility(st, els, doc);
}
