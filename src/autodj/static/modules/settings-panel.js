// Settings panel state sync + postSettings helper.
//
// Listener wiring (`presetSelect.addEventListener`, ...) stays inline
// in app.js because each handler is a one-liner that calls
// postSettings with a single field; pulling them into the module
// would require funnelling every DOM ref + would not shorten the
// total surface area.

import { escHtml, dbg } from "./dom-helpers.js";

let _lastPresetOptionsKey = "";
let _libraryWarned = false;

export async function postSettings(url, body, { settingsStatus } = {}) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    if (settingsStatus) {
      settingsStatus.textContent = `Could not save: ${err.message}`;
      setTimeout(() => { settingsStatus.textContent = ""; }, 4000);
    }
  }
}

export function applySettingsState(st, els) {
  const {
    presetSelect, transitionSelect,
    harmonicMode,
    djBeatmatch, djPhraseAlign, djOutroIntro,
    pbEqDuck, pbPickMode, pbShowLyrics, pbAnchorSeed,
    pbReplayGain,
    pbDaypart, pbMoodArc, pbMoodArcHours, pbImportCues,
    pbBeatSyncFx, pbKeySyncFx, pbBeatmatchSkip,
    pbTransitionMode, pbCrossfade, pbFadeIn,
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
  presetSelect.value = st.preset || "";

  if (document.activeElement !== transitionSelect) {
    // Without the focus-check guard, every WS state echo (~1 Hz)
    // reassigns .value, which closes the dropdown and shifts focus
    // mid-selection.
    transitionSelect.value = st.transition || "none";
  }

  if (st.djmix) {
    const mode = st.djmix.harmonic_mixing
      ? (st.djmix.harmonic_mode || "compatible")
      : "off";
    if (harmonicMode.value !== mode) harmonicMode.value = mode;
  }
  djBeatmatch.checked   = !!(st.djmix && st.djmix.beatmatch);
  djPhraseAlign.checked = !!(st.djmix && st.djmix.phrase_align);
  djOutroIntro.checked  = !!(st.djmix && st.djmix.outro_intro_align);

  pbEqDuck.checked       = !!(st.playback && st.playback.crossfade_eq_duck);
  if (pbPickMode && document.activeElement !== pbPickMode) {
    // Project the two server-side flags back to the three-way select.
    // pure_shuffle wins over smart_shuffle when both are somehow set
    // (defensive — bridge clears the other on switch, but old saved
    // state might carry both true).
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
  if (pbBeatSyncFx) {
    // Default ON when the server hasn't sent the field yet (older deploy).
    pbBeatSyncFx.checked = !(st.playback && st.playback.beat_sync_fx === false);
  }

  // One-shot library-size sanity check -- warn the user when the
  // configured no_repeat_window exceeds the library size, since that
  // forces repeats sooner than the config implies.  Fired once per
  // session so chatty WS pushes do not spam.
  if (!_libraryWarned && st.playback &&
      typeof st.playback.no_repeat_window === "number" &&
      typeof st.playback.library_size === "number" &&
      st.playback.library_size > 0) {
    _libraryWarned = true;
    dbg("library_size =", st.playback.library_size,
      "| no_repeat_window =", st.playback.no_repeat_window);
    if (st.playback.library_size <= st.playback.no_repeat_window) {
      console.warn("[autodj] Library has", st.playback.library_size,
        "tracks but no_repeat_window is", st.playback.no_repeat_window,
        "-- repeats will start once you reach the library size. " +
        "Lower playback.no_repeat_window in config.toml to silence.");
    }
  }
  if (pbKeySyncFx) {
    pbKeySyncFx.checked = !(st.playback && st.playback.key_sync_fx === false);
  }
  if (pbBeatmatchSkip) {
    pbBeatmatchSkip.checked = !!(st.playback && st.playback.beatmatch_on_skip === true);
  }
  if (st.playback && st.playback.transition_mode &&
      document.activeElement !== pbTransitionMode) {
    pbTransitionMode.value = st.playback.transition_mode;
  }
  if (st.playback && document.activeElement !== pbCrossfade) {
    pbCrossfade.value = st.playback.crossfade_seconds;
  }
  if (pbFadeIn && st.playback && document.activeElement !== pbFadeIn) {
    pbFadeIn.value = typeof st.playback.fade_in_seconds === "number"
      ? st.playback.fade_in_seconds : 3;
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
  const discOn = st.discovery_every != null;
  discEnabled.checked = discOn;
  if (document.activeElement !== discEvery && discOn) {
    discEvery.value = st.discovery_every;
  }
  discEvery.setAttribute("aria-disabled", discOn ? "false" : "true");
}
