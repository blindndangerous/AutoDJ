// Pure helpers extracted to ./modules/dom-helpers.js.  Aliased back to
// the historical underscore-prefixed names so the rest of this file
// keeps working without a sweep.
import {
  DEBUG as _DEBUG,
  dbg as _dbg,
  fmtTime,
  fmtTrack,
  escHtml,
  isTypingTarget as _isTypingTarget,
} from "./modules/dom-helpers.js";

if (_DEBUG) {
  console.log("[autodj] debug logging ENABLED " +
    "(disable with localStorage.removeItem('autodjDebug') " +
    "or remove ?debug=1 from URL).");
}

// ----------------------------------------------------------------
// DOM refs
// ----------------------------------------------------------------

const connStatus   = document.getElementById("conn-status");
const npAnnounce   = document.getElementById("now-playing-announce");
const npMeta       = document.getElementById("now-playing-meta");
const coverArt     = document.getElementById("cover-art");
const progressFill = document.getElementById("progress-fill");
const progressLbl  = document.getElementById("progress-bar-label");
const nextText     = document.getElementById("next-track-text");
const btnPause     = document.getElementById("btn-pause");
const btnSkip      = document.getElementById("btn-skip");
const btnDiscovery = document.getElementById("btn-discovery");
const volSlider    = document.getElementById("vol");
const volPct       = document.getElementById("vol-pct");
const btnMute      = document.getElementById("btn-mute");
const searchInput  = document.getElementById("search-input");
const btnSearch    = document.getElementById("btn-search");
const searchResults= document.getElementById("search-results");
const searchCount  = document.getElementById("search-count");
const historyList  = document.getElementById("history-list");
const whyList      = document.getElementById("why-list");
const lyricsCard   = document.getElementById("lyrics-card");
const lyricsList   = document.getElementById("lyrics-list");
const lyricAnnounce= document.getElementById("lyric-announce");
const queueList    = document.getElementById("queue-list");
const queueCount   = document.getElementById("queue-count");
const queueAnnounce= document.getElementById("queue-announce");
const badgesRow    = document.getElementById("now-playing-badges");
const badgesAnnounce = document.getElementById("badges-announce");
const eqLow        = document.getElementById("eq-low");
const eqMid        = document.getElementById("eq-mid");
const eqHigh       = document.getElementById("eq-high");
const eqLowVal     = document.getElementById("eq-low-value");
const eqMidVal     = document.getElementById("eq-mid-value");
const eqHighVal    = document.getElementById("eq-high-value");
const btnEqReset   = document.getElementById("btn-eq-reset");
const eqAnnounce   = document.getElementById("eq-announce");
const audioEl      = document.getElementById("browser-player");
// (enable-playback-card was removed — Play button is unified into btn-pause)
// Settings card
const presetSelect    = document.getElementById("preset-select");
const transitionSelect= document.getElementById("transition-select");
const harmonicMode    = document.getElementById("harmonic-mode");
const djBeatmatch     = document.getElementById("dj-beatmatch");
const djPhraseAlign   = document.getElementById("dj-phrase-align");
const djOutroIntro    = document.getElementById("dj-outro-intro");
const djFilterSweep   = null;  // moved into Transition effect dropdown
const pbEqDuck        = document.getElementById("pb-eq-duck");
const pbSmartShuffle  = document.getElementById("pb-smart-shuffle");
const pbPureShuffle   = document.getElementById("pb-pure-shuffle");
const pbShowLyrics    = document.getElementById("pb-show-lyrics");
const pbAnchorSeed    = document.getElementById("pb-anchor-seed");
const pbReplayGain    = document.getElementById("pb-replaygain");
const pbDaypart       = document.getElementById("pb-daypart");
const pbMoodArc       = document.getElementById("pb-mood-arc");
const pbMoodArcHours  = document.getElementById("pb-mood-arc-hours");
const pbImportCues    = document.getElementById("pb-import-cues");
const pbBeatSyncFx    = document.getElementById("pb-beat-sync-fx");
const pbKeySyncFx     = document.getElementById("pb-key-sync-fx");
const pbBeatmatchSkip = document.getElementById("pb-beatmatch-on-skip");
const pbTransitionMode = document.getElementById("pb-transition-mode");
const pbCrossfade     = document.getElementById("pb-crossfade");
const bpmLo           = document.getElementById("bpm-lo");
const bpmHi           = document.getElementById("bpm-hi");
const bpmClear        = document.getElementById("bpm-clear");
const discEnabled     = document.getElementById("disc-enabled");
const discEvery       = document.getElementById("disc-every");
const settingsStatus  = document.getElementById("settings-status");
const volAnnounce     = document.getElementById("vol-announce");

// ----------------------------------------------------------------
// State
// ----------------------------------------------------------------

let lastTrackKey = null;   // detect track changes for aria-live announce
const historyItems = [];   // most-recent first
// lastLyricIndex + cachedLyrics moved into ./modules/lyrics.js.
let lastQueueKey = "";     // skip queue re-render when unchanged
let lastBadgeKey = null;   // suppress repeated badge announcements within one track
let lastNextKey  = null;   // suppress aria-live re-announce of unchanged next track

// ----------------------------------------------------------------
// State update — called on every WS push and on manual API calls
// ----------------------------------------------------------------

function applyState(s) {
  // Voice liner track-count bump (forward declaration of helper -- see
  // voice liner block at the bottom of this file).  Safe to call here
  // because module-init runs top-to-bottom and the helper is defined
  // before WS messages start arriving.
  if (typeof _bumpLinerTrackCount === "function") _bumpLinerTrackCount(s);

  // Now Playing
  const trackKey   = s.current_track ? s.current_track.path : null;
  const trackLabel = fmtTrack(s.current_track);

  if (trackKey !== lastTrackKey) {
    // Track changed — update aria-live region so screen readers announce it
    npAnnounce.textContent = trackLabel;
    lastTrackKey = trackKey;
    // Update browser titlebar: "AutoDJ - Artist - Title - Album"
    const t = s.current_track;
    if (t) {
      const segs = ["AutoDJ"];
      if (t.artist) segs.push(t.artist);
      if (t.title)  segs.push(t.title);
      if (t.album)  segs.push(t.album);
      document.title = segs.join(" - ");
    } else {
      document.title = "AutoDJ";
    }
    // Reset lyric tracking — new track may have different lyrics
    resetLyricState();
    // Refresh cover art and full lyric list for the new track
    if (trackKey) {
      loadCoverArt(trackKey);
      loadLyrics();
    } else {
      coverArt.hidden = true;
      coverArt.src = "";
    }

    // Push to history (skip duplicates at top)
    if (trackKey && historyItems[0] !== trackLabel) {
      historyItems.unshift(trackLabel);
      if (historyItems.length > 20) historyItems.pop();
      renderHistory();
    }
  }

  // Meta line: album · BPM
  const parts = [];
  if (s.current_track) {
    if (s.current_track.album) parts.push(s.current_track.album);
    if (s.current_track.bpm)   parts.push(`${Math.round(s.current_track.bpm)} BPM`);
  }
  npMeta.textContent = parts.join(" \u00b7 ");

  // Badges + announce on track change
  applyBadges(s);

  // Camelot wheel — decorative only.  Pull harmonic_mode from settings
  // so the highlighted "compatible" set matches what the picker uses.
  const _hm = (s.settings && s.settings.djmix && s.settings.djmix.harmonic_mode)
              || "compatible";
  const _cell = s.current_track ? s.current_track.camelot : null;
  applyCamelotWheel(_cell, _hm);

  // EQ slider state from server
  applyEqState(s.eq);

  // Progress bar — in browser-playback mode the deck is the real audio
  // clock, so read elapsed/duration from there.  Server's elapsed is
  // 0 in headless mode (deliberate — see Player._run_headless).
  let elapsed = s.elapsed || 0;
  let dur = s.duration || 0;
  if (s.browser_playback && playbackEnabled && _ctx) {
    const a = decks[activeIdx].audio;
    if (a && isFinite(a.duration) && a.duration > 0) {
      elapsed = a.currentTime;
      dur = a.duration;
    } else {
      elapsed = 0;
    }
  }
  const pct = dur > 0 ? Math.min(100, (elapsed / dur) * 100) : 0;
  // Cache for the seek slider — its keyboard / pointer handlers need
  // the current duration in seconds even when no deck is unlocked yet
  // (server-audio mode, pre-Play state).  Set before the early-return
  // path so a paused / not-yet-started session can still seek.
  _lastDuration = dur;
  // Suppress live progress updates while the user is actively
  // dragging the seek slider so the UI doesn't yo-yo between the
  // drag position and a stale server-broadcast position.
  if (_seekDragging) return;
  progressFill.style.width = pct.toFixed(1) + "%";
  const timeText = `${fmtTime(elapsed)} / ${fmtTime(dur)}`;
  progressLbl.textContent  = timeText;
  // A11y C1: expose progressbar value + valuetext so NVDA users can query
  // track position (insert+T / NVDA+Tab).  pct is 0–100 — matches valuemax=100.
  const progressTrack = document.getElementById("progress-track");
  if (progressTrack) {
    progressTrack.setAttribute("aria-valuenow", pct.toFixed(0));
    progressTrack.setAttribute(
      "aria-valuetext",
      `${fmtTime(elapsed)} of ${fmtTime(dur)}`
    );
  }

  // Snapshot for first-click unlock branch in btnPause handler
  _lastBrowserPlayback = !!s.browser_playback;

  // Unified Play / Pause / Resume button.  Three states:
  //   1. No track yet \u2192 "Play", disabled
  //   2. Browser-playback mode, audio not yet unlocked \u2192 "Play", enabled
  //      (clicking unlocks AudioContext + starts deck)
  //   3. Playing or paused \u2192 "Pause" / "Resume" toggle
  // A11y C2 (v5.4.0 audit): aria-pressed updates atomically with the
  // glyph + label so NVDA never reads stale state when one of the three
  // attributes lags.  pressed=true means "currently playing".
  const hasTrack = s.current_track != null;
  if (!hasTrack) {
    btnPause.disabled = true;
    btnPause.innerHTML = '<span aria-hidden="true">\u25B6</span> Play';
    btnPause.setAttribute("aria-pressed", "false");
  } else if (s.browser_playback && !playbackEnabled) {
    btnPause.disabled = false;
    btnPause.innerHTML = '<span aria-hidden="true">\u25B6</span> Play';
    btnPause.setAttribute("aria-pressed", "false");
  } else {
    btnPause.disabled = false;
    btnPause.innerHTML = s.is_paused
      ? '<span aria-hidden="true">\u25B6</span> Resume'
      : '<span aria-hidden="true">\u23F8</span> Pause';
    btnPause.setAttribute("aria-pressed", s.is_paused ? "false" : "true");
  }

  // Volume — server stores the perceptual *gain* (post-curve), so invert
  // the fader curve before writing it back to the slider.  Without this
  // inversion a 50 % slider sets gain ≈ 0.0316, the WS echo arrives as
  // `volume: 0.03`, and Math.round(0.03*100)=3 — the slider snaps to ~0
  // every time the user nudges it.  Skip the overwrite while the user
  // is actively dragging / arrow-keying so the in-flight POST round-trip
  // can't fight the input.
  if (Date.now() - _lastUserVolTs > 600) {
    const volInt = _gainToSlider(s.volume);
    volSlider.value = volInt;
    volPct.textContent = volInt + "%";
  }

  // Mute
  const isMuted = s.is_muted;
  btnMute.setAttribute("aria-pressed", isMuted ? "true" : "false");
  btnMute.innerHTML = isMuted
    ? '<span aria-hidden="true">\uD83D\uDD07</span> Unmute'
    : '<span aria-hidden="true">\uD83D\uDD0A</span> Mute';

  // Up Next — only mutate textContent when value actually changes so the
  // aria-live region doesn't re-announce on every per-second WS tick.
  const nextKey = s.next_track ? s.next_track.path : "";
  if (nextKey !== lastNextKey) {
    lastNextKey = nextKey;
    nextText.textContent = fmtTrack(s.next_track);
  }

  // Discovery button — show only when discovery is configured
  if (s.discovery_available) {
    btnDiscovery.style.display = "";
    const isOn = s.discovery_enabled;
    btnDiscovery.setAttribute("aria-pressed", isOn ? "true" : "false");
    btnDiscovery.innerHTML = isOn
      ? '<span aria-hidden="true">\u25c8</span> Discovery <small>ON</small>'
      : '<span aria-hidden="true">\u25c8</span> Discovery';
  } else {
    btnDiscovery.style.display = "none";
  }

  // Lyrics \u2014 visible list highlight + announce only on line change
  applyLyricsState(s);

  // Why this track? \u2014 refresh only on track change to avoid pointless WS churn
  applyWhyState(s);

  // Library job log + status
  applyLibraryJobState(s);

  // Queue \u2014 render if changed
  applyQueueState(s.queue || []);

  // Browser-side audio (when server runs headless / no_playback)
  applyBrowserPlaybackState(s);

  // OS media-keys / lock-screen integration
  updateMediaSession(s);

  // Mirror settings to the form.
  if (s.settings) applySettingsState(s.settings);
}

// ----------------------------------------------------------------
// Settings card — mirror of CLI flags
// ----------------------------------------------------------------

let lastPresetOptionsKey = "";

function applySettingsState(st) {
  // Populate preset dropdown only when the option list changes
  const optsKey = (st.available_presets || []).join("|");
  if (optsKey !== lastPresetOptionsKey) {
    lastPresetOptionsKey = optsKey;
    presetSelect.innerHTML = '<option value="">(none)</option>' +
      (st.available_presets || []).map(n =>
        `<option value="${escHtml(n)}">${escHtml(n)}</option>`
      ).join("");
  }
  presetSelect.value = st.preset || "";

  if (document.activeElement !== transitionSelect) {
    // Guard with focus check — without this, every WebSocket state echo
    // (~1 Hz) reassigns `.value`, which closes the dropdown and shifts
    // focus while the user is mid-selection.  Same pattern as
    // pbTransitionMode / pbCrossfade above.
    transitionSelect.value = st.transition || "none";
  }

  // Harmonic mode dropdown reflects both flag + mode.  The "off" option
  // implies harmonic_mixing=false; any other option enables it.
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
  pbSmartShuffle.checked = !!(st.playback && st.playback.smart_shuffle);
  pbPureShuffle.checked  = !!(st.playback && st.playback.pure_shuffle);
  // show_lyrics defaults to true on legacy state payloads.
  pbShowLyrics.checked   = (st.playback && st.playback.show_lyrics !== false);
  pbAnchorSeed.checked   = !!(st.playback && st.playback.anchor_to_seed);
  pbReplayGain.checked   = !!(st.playback && st.playback.replaygain_enabled);
  if (pbDaypart) {
    pbDaypart.checked = !!(st.playback && st.playback.enable_daypart);
  }
  if (pbMoodArc) {
    pbMoodArc.checked = !!(st.playback && st.playback.enable_mood_arc);
  }
  if (pbMoodArcHours && st.playback && typeof st.playback.mood_arc_hours === "number") {
    pbMoodArcHours.value = st.playback.mood_arc_hours;
  }
  if (pbImportCues) {
    pbImportCues.checked = !!(st.playback && st.playback.import_external_cues);
  }
  if (pbBeatSyncFx) {
    // Default ON when server hasn't sent the field yet (older deploy).
    pbBeatSyncFx.checked = !(st.playback && st.playback.beat_sync_fx === false);
  }
  // One-shot library-size sanity check — warn the user when the
  // configured no_repeat_window exceeds the library size, since that
  // forces repeats sooner than the config implies.  Logs once per
  // session so chatty WS pushes don't spam.
  if (!_libraryWarned && st.playback &&
      typeof st.playback.no_repeat_window === "number" &&
      typeof st.playback.library_size === "number" &&
      st.playback.library_size > 0) {
    _libraryWarned = true;
    _dbg("library_size =", st.playback.library_size,
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
  if (st.playback && st.playback.transition_mode && document.activeElement !== pbTransitionMode) {
    pbTransitionMode.value = st.playback.transition_mode;
  }
  if (st.playback && document.activeElement !== pbCrossfade) {
    pbCrossfade.value = st.playback.crossfade_seconds;
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

async function postSettings(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    settingsStatus.textContent = `Could not save: ${err.message}`;
    setTimeout(() => { settingsStatus.textContent = ""; }, 4000);
  }
}

presetSelect.addEventListener("change", () => {
  postSettings("/api/preset", { name: presetSelect.value || null });
});

transitionSelect.addEventListener("change", () => {
  postSettings("/api/transition", { effect: transitionSelect.value });
});

const _djToggleMap = [
  [djBeatmatch,   "beatmatch"],
  [djPhraseAlign, "phrase_align"],
  [djOutroIntro,  "outro_intro_align"],
];
for (const [el, key] of _djToggleMap) {
  el.addEventListener("change", () => {
    postSettings("/api/djmix", { [key]: el.checked });
  });
}
harmonicMode.addEventListener("change", () => {
  postSettings("/api/djmix", { harmonic_mode: harmonicMode.value });
});

pbEqDuck.addEventListener("change", () => {
  postSettings("/api/playback-settings", { crossfade_eq_duck: pbEqDuck.checked });
});
pbSmartShuffle.addEventListener("change", () => {
  postSettings("/api/playback-settings", { smart_shuffle: pbSmartShuffle.checked });
});
pbPureShuffle.addEventListener("change", () => {
  postSettings("/api/playback-settings", { pure_shuffle: pbPureShuffle.checked });
});
pbShowLyrics.addEventListener("change", () => {
  postSettings("/api/playback-settings", { show_lyrics: pbShowLyrics.checked });
});
pbAnchorSeed.addEventListener("change", () => {
  postSettings("/api/playback-settings", { anchor_to_seed: pbAnchorSeed.checked });
});
if (pbDaypart) {
  pbDaypart.addEventListener("change", () => {
    postSettings("/api/playback-settings", { enable_daypart: pbDaypart.checked });
  });
}
if (pbMoodArc) {
  pbMoodArc.addEventListener("change", () => {
    postSettings("/api/playback-settings", { enable_mood_arc: pbMoodArc.checked });
  });
}
if (pbMoodArcHours) {
  pbMoodArcHours.addEventListener("change", () => {
    const hrs = parseFloat(pbMoodArcHours.value);
    if (isFinite(hrs) && hrs > 0) {
      postSettings("/api/playback-settings", { mood_arc_hours: hrs });
    }
  });
}
if (pbImportCues) {
  pbImportCues.addEventListener("change", () => {
    postSettings("/api/playback-settings", {
      import_external_cues: pbImportCues.checked,
    });
  });
}
if (pbBeatSyncFx) {
  pbBeatSyncFx.addEventListener("change", () => {
    postSettings("/api/playback-settings", {
      beat_sync_fx: pbBeatSyncFx.checked,
    });
  });
}
if (pbKeySyncFx) {
  pbKeySyncFx.addEventListener("change", () => {
    postSettings("/api/playback-settings", {
      key_sync_fx: pbKeySyncFx.checked,
    });
  });
}
if (pbBeatmatchSkip) {
  pbBeatmatchSkip.addEventListener("change", () => {
    postSettings("/api/playback-settings", {
      beatmatch_on_skip: pbBeatmatchSkip.checked,
    });
  });
}
pbReplayGain.addEventListener("change", () => {
  postSettings("/api/playback-settings", { replaygain_enabled: pbReplayGain.checked });
});
pbTransitionMode.addEventListener("change", () => {
  postSettings("/api/playback-settings", { transition_mode: pbTransitionMode.value });
});

// ----------------------------------------------------------------
// Audio output device selector (browser-only — server-side device is
// configured separately via [playback] audio_device in config.toml).
// Uses navigator.mediaDevices.enumerateDevices() + audio.setSinkId().
// Selection persists across reloads in localStorage.
// ----------------------------------------------------------------
const audioDeviceSelect = document.getElementById("audio-device");
const audioDeviceRefresh = document.getElementById("audio-device-refresh");
const _SINK_KEY = "autodj.sinkId";

function _setSinkIdSupported() {
  // Two distinct setSinkId APIs:
  //   1. HTMLMediaElement.prototype.setSinkId — works on raw <audio>
  //      elements but is BYPASSED once a MediaElementAudioSourceNode taps
  //      the element (because Web Audio routes through AudioContext).
  //   2. AudioContext.prototype.setSinkId — Chromium / Edge 110+, the
  //      ONLY one that works when our crossfade graph is live.  Firefox
  //      hasn't shipped it (2026-05), Safari neither.
  // We treat the feature as "supported" when EITHER exists; runtime
  // routing in `_applySink` picks whichever actually applies.
  const hmeOk =
    typeof HTMLMediaElement !== "undefined"
      && typeof HTMLMediaElement.prototype.setSinkId === "function";
  const ctxOk =
    typeof AudioContext !== "undefined"
      && typeof AudioContext.prototype.setSinkId === "function";
  return hmeOk || ctxOk;
}

function _ctxSinkSupported() {
  return _ctx && typeof _ctx.setSinkId === "function";
}

async function _refreshAudioDevices() {
  if (!audioDeviceSelect) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
    audioDeviceSelect.disabled = true;
    audioDeviceSelect.title = "Browser does not support enumerateDevices.";
    return;
  }
  if (!_setSinkIdSupported()) {
    audioDeviceSelect.disabled = true;
    audioDeviceSelect.title =
      "Your browser does not support audio.setSinkId — output stays on the system default.";
    if (audioDeviceRefresh) audioDeviceRefresh.disabled = true;
    return;
  }
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const outs = devs.filter(d => d.kind === "audiooutput");
    const saved = localStorage.getItem(_SINK_KEY) || "";
    audioDeviceSelect.innerHTML = '<option value="">System default</option>';
    let hasLabels = false;
    outs.forEach((d, i) => {
      const opt = document.createElement("option");
      opt.value = d.deviceId;
      // If the label is blank (no permission yet), use a generic but
      // still-uniquely-identifying fallback.
      if (d.label) {
        hasLabels = true;
        opt.textContent = d.label;
      } else {
        opt.textContent = `Output ${i + 1}`;
      }
      audioDeviceSelect.appendChild(opt);
    });
    if (saved && [...audioDeviceSelect.options].some(o => o.value === saved)) {
      audioDeviceSelect.value = saved;
      _applySink(saved);
    }
    if (audioDeviceRefresh) {
      audioDeviceRefresh.style.display = hasLabels ? "none" : "";
    }
  } catch (err) {
    console.warn("enumerateDevices failed:", err);
  }
}

async function _applySink(sinkId) {
  // Prefer AudioContext.setSinkId — it actually routes the live crossfade
  // graph.  Element-level setSinkId is a fallback for browsers that lack
  // ctx.setSinkId (Firefox 116+) AND only works when we're playing the
  // <audio> directly without Web Audio interception (rare path).
  let lastErr = null;
  if (_ctxSinkSupported()) {
    try {
      // Chromium accepts "" or string id; null = system default.
      await _ctx.setSinkId(sinkId || "");
      return true;
    } catch (err) {
      lastErr = err;
      console.warn("AudioContext.setSinkId failed:", err);
    }
  }
  // Element fallback (Firefox).  Will be a silent no-op once Web Audio
  // is in play, but useful before the user enables playback.
  for (const d of decks) {
    if (!d || !d.audio || typeof d.audio.setSinkId !== "function") continue;
    try {
      await d.audio.setSinkId(sinkId || "");
    } catch (err) {
      lastErr = err;
      try { await d.audio.setSinkId(undefined); } catch (_) {}
    }
  }
  if (lastErr) {
    if (settingsStatus) {
      settingsStatus.textContent =
        "Could not switch audio device: " +
        (lastErr.message || lastErr.name || "unknown");
      setTimeout(() => { settingsStatus.textContent = ""; }, 5000);
    }
    return false;
  }
  return true;
}

async function _grantDeviceLabels() {
  // Briefly request the microphone, immediately stop it.  This is what
  // populates the .label field on subsequent enumerateDevices() calls
  // in every privacy-conscious browser (Chromium, Firefox).
  if (audioDeviceRefresh) {
    audioDeviceRefresh.textContent = "Requesting permission…";
    audioDeviceRefresh.disabled = true;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach(t => t.stop());
    await _refreshAudioDevices();
    if (audioDeviceRefresh) {
      audioDeviceRefresh.textContent = "Show device names";
      audioDeviceRefresh.disabled = false;
    }
  } catch (err) {
    console.warn("microphone permission denied:", err);
    // Re-enable the button so the user can retry — denial is recoverable
    // by clicking the lock icon in the address bar and resetting the
    // microphone permission.  Without re-enabling, a single mis-click on
    // the prompt would lock the user out forever.
    if (audioDeviceRefresh) {
      audioDeviceRefresh.textContent = "Permission denied — click to retry";
      audioDeviceRefresh.disabled = false;
    }
    if (settingsStatus) {
      settingsStatus.textContent =
        "Microphone permission denied.  Reset it via the lock icon in the " +
        "address bar (or your browser's site settings) and click again.  " +
        "AutoDJ never records audio; the prompt is the only way browsers " +
        "expose audio-output device names.";
      // Don't auto-clear — the user needs time to read this, and the
      // next click on the button will overwrite it anyway.
    }
  }
}

// React to permission changes pushed by the browser (e.g. user resets
// microphone permission via the address-bar lock icon).  Re-enables the
// label button and re-runs enumerateDevices once permission is granted.
if (navigator.permissions && navigator.permissions.query) {
  navigator.permissions.query({ name: "microphone" })
    .then((status) => {
      const refresh = () => {
        if (status.state === "granted" && audioDeviceRefresh) {
          audioDeviceRefresh.textContent = "Show device names";
          audioDeviceRefresh.disabled = false;
          _refreshAudioDevices();
        } else if (status.state === "denied" && audioDeviceRefresh) {
          audioDeviceRefresh.textContent = "Permission denied — click to retry";
          audioDeviceRefresh.disabled = false;
        }
      };
      status.addEventListener("change", refresh);
    })
    .catch(() => {});
}

if (audioDeviceSelect) {
  audioDeviceSelect.addEventListener("change", async () => {
    const id = audioDeviceSelect.value;
    localStorage.setItem(_SINK_KEY, id);
    // First-time selection: AudioContext must exist before ctx.setSinkId
    // works, so build the (silent) graph if it hasn't been yet.
    if (!_ctx) ensureAudioGraph();
    const ok = await _applySink(id);
    if (ok && settingsStatus) {
      const sel = audioDeviceSelect.options[audioDeviceSelect.selectedIndex];
      const label = sel ? sel.textContent : "selected device";
      settingsStatus.textContent = `Audio output: ${label}`;
      setTimeout(() => { settingsStatus.textContent = ""; }, 3000);
    }
  });
  if (audioDeviceRefresh) {
    audioDeviceRefresh.addEventListener("click", _grantDeviceLabels);
  }
  // Refresh on load + when devices change (USB plug/unplug)
  _refreshAudioDevices();
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", _refreshAudioDevices);
  }
}
pbCrossfade.addEventListener("change", () => {
  const v = parseFloat(pbCrossfade.value);
  if (!isNaN(v) && v >= 0) postSettings("/api/playback-settings", { crossfade_seconds: v });
});

function postBpmRange() {
  const lo = parseFloat(bpmLo.value);
  const hi = parseFloat(bpmHi.value);
  postSettings("/api/bpm-range", {
    lo: isNaN(lo) ? null : lo,
    hi: isNaN(hi) ? null : hi,
  });
}
bpmLo.addEventListener("change", postBpmRange);
bpmHi.addEventListener("change", postBpmRange);
bpmClear.addEventListener("click", () => {
  bpmLo.value = "";
  bpmHi.value = "";
  postSettings("/api/bpm-range", { lo: null, hi: null });
});

function postDiscovery() {
  const on = discEnabled.checked;
  const v = parseInt(discEvery.value, 10);
  postSettings("/api/discovery", { every: on && !isNaN(v) && v > 0 ? v : null });
}
discEnabled.addEventListener("change", () => {
  discEvery.setAttribute("aria-disabled", discEnabled.checked ? "false" : "true");
  postDiscovery();
});
discEvery.addEventListener("change", () => {
  if (discEnabled.checked) postDiscovery();
});

// ----------------------------------------------------------------
// Badges (BPM / Camelot key / energy / beatmatch ratio)
// ----------------------------------------------------------------

function applyBadges(s) {
  const t = s.current_track;
  if (!t) {
    badgesRow.innerHTML = "";
    return;
  }
  const out = [];
  if (t.bpm)            out.push(`<span class="badge">${Math.round(t.bpm)} BPM</span>`);
  if (t.camelot && t.camelot !== "--")
                        out.push(`<span class="badge badge-key">Key ${escHtml(t.camelot)}</span>`);
  if (t.energy && t.energy > 0)
                        out.push(`<span class="badge">Energy ${(t.energy).toFixed(2)}</span>`);
  if (s.beatmatch_ratio && Math.abs(s.beatmatch_ratio - 1.0) > 0.005)
                        out.push(`<span class="badge badge-stretch">Beatmatch ${s.beatmatch_ratio.toFixed(3)}x</span>`);
  badgesRow.innerHTML = out.join("");

  // Announce key + BPM only on track change (not on every WS tick).
  // Same trigger as the title aria-live: track-id change has already updated
  // lastTrackKey above, so only fire here when WE see a fresh track AND
  // we have a key/BPM to read.  Spell "times" for beatmatch (per a11y review).
  if (s.current_track.path === lastTrackKey && lastBadgeKey !== s.current_track.path) {
    lastBadgeKey = s.current_track.path;
    const phrases = [];
    if (t.camelot && t.camelot !== "--") phrases.push(`Key ${t.camelot}`);
    if (t.bpm) phrases.push(`BPM ${Math.round(t.bpm)}`);
    if (s.beatmatch_ratio && Math.abs(s.beatmatch_ratio - 1.0) > 0.005) {
      phrases.push(`beatmatched ${s.beatmatch_ratio.toFixed(2)} times`);
    }
    // Cue-point summary intentionally NOT announced -- key + BPM is
    // what users want on track change.  Cue strip on the progress bar
    // still conveys the markers visually for sighted users.
    if (phrases.length) {
      // Slight delay so the title aria-live region speaks first
      setTimeout(() => {
        badgesAnnounce.textContent = phrases.join(", ");
        _clearLiveRegionLater(badgesAnnounce);
      }, 800);
    }
  }
  renderCueStrip(t);
}

// ----------------------------------------------------------------
// Cue strip — decorative markers on the progress bar.
// Sighted users see colored ticks; AT users get the summary above.
// ----------------------------------------------------------------

// Cue strip rendering moved to ./modules/cues.js.
import {
  renderCueStrip as _renderCueStripImpl,
  summariseCues as _summariseCues,
} from "./modules/cues.js";

const _cueStrip = document.getElementById("cue-strip");
function renderCueStrip(track) { _renderCueStripImpl(_cueStrip, track); }

// Camelot wheel rendering moved to ./modules/camelot-wheel.js.
import { applyCamelotWheel as _applyCamelotWheelImpl } from "./modules/camelot-wheel.js";

const _camelotSectors = document.getElementById("camelot-sectors");
const _camelotLabels  = document.getElementById("camelot-labels");

function applyCamelotWheel(currentCell, harmonicMode) {
  _applyCamelotWheelImpl(currentCell, harmonicMode, {
    sectorsEl: _camelotSectors,
    labelsEl:  _camelotLabels,
  });
}

// ----------------------------------------------------------------
// 3-band EQ
// ----------------------------------------------------------------

function eqValueLabel(v100) {
  // v100: 0–200 with 100 = unity.  Return human label + dB.
  if (v100 === 0) return "Kill";
  if (v100 === 100) return "Unity";
  // dB = 20 * log10(v/100)
  const db = 20 * Math.log10(v100 / 100);
  const sign = db >= 0 ? "+" : "";
  return `${sign}${db.toFixed(1)} dB`;
}

function applyEqState(eq) {
  if (!eq) return;
  // Server gives 0.0–2.0 floats; convert to 0–200 ints for the slider.
  const map = [
    [eqLow, eqLowVal, Math.round(eq.low * 100)],
    [eqMid, eqMidVal, Math.round(eq.mid * 100)],
    [eqHigh, eqHighVal, Math.round(eq.high * 100)],
  ];
  for (const [slider, span, value] of map) {
    if (parseInt(slider.value, 10) !== value) {
      slider.value = value;
    }
    const label = eqValueLabel(value);
    slider.setAttribute("aria-valuetext", label);
    span.textContent = label;
  }
}

let eqDebounceTimer = null;
function postEq() {
  clearTimeout(eqDebounceTimer);
  eqDebounceTimer = setTimeout(() => {
    fetch("/api/eq", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        low:  parseInt(eqLow.value, 10) / 100,
        mid:  parseInt(eqMid.value, 10) / 100,
        high: parseInt(eqHigh.value, 10) / 100,
      }),
    });
  }, 120);
}

[eqLow, eqMid, eqHigh].forEach((slider, i) => {
  const span = [eqLowVal, eqMidVal, eqHighVal][i];
  slider.addEventListener("input", () => {
    const label = eqValueLabel(parseInt(slider.value, 10));
    slider.setAttribute("aria-valuetext", label);
    span.textContent = label;
    postEq();
  });
});

btnEqReset.addEventListener("click", () => {
  eqLow.value = eqMid.value = eqHigh.value = "100";
  for (const [s, sp] of [[eqLow, eqLowVal], [eqMid, eqMidVal], [eqHigh, eqHighVal]]) {
    s.setAttribute("aria-valuetext", "Unity");
    sp.textContent = "Unity";
  }
  postEq();
  eqAnnounce.textContent = "EQ reset to unity.";
  _clearLiveRegionLater(eqAnnounce);
  // Per a11y review, focus stays on Reset button.
});

// ----------------------------------------------------------------
// Browser-side audio playback — Web Audio API two-deck crossfade
// ----------------------------------------------------------------
//
// Server picks tracks; browser does the actual playback + crossfade.
// Two <audio> decks (A and B) are wired through Web Audio gain nodes
// so we can ramp gains for a smooth client-side crossfade — the kind
// of transition a real DJ deck produces, not the browser's hard cut.
//
// Track-change choreography:
//   1. Active deck plays current_track.
//   2. When remaining time on active deck < crossfade_seconds AND state
//      has a next_track, load next_track on standby deck, start crossfade
//      gain ramp (active 1.0→0.0, standby 0.0→1.0), and POST /api/advance
//      so the server picks a NEW next track.
//   3. WS push arrives with new current_track (= our standby) + new
//      next_track.  We mark standby as the new active and idle the old
//      active so the cycle continues.

let playbackEnabled = false;
let suppressAdvance = false;   // gate spurious advance posts during programmatic actions
let _lastBrowserPlayback = false;  // mirror of state.browser_playback for click handlers
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent || "");

if (isIOS) {
  // iOS ignores HTMLMediaElement.volume — hide the volume control
  // rather than letting users drag something that does nothing.
  const volRow = volSlider.closest(".volume-row");
  if (volRow) volRow.style.display = "none";
}

const audioElB = document.getElementById("browser-player-b");

// Web Audio graph — built lazily on first user gesture (browsers
// require a user activation to construct an AudioContext).
let _ctx = null;
const decks = [
  { audio: audioEl,  source: null, gain: null, path: null, busy: false },
  { audio: audioElB, source: null, gain: null, path: null, busy: false },
];
let activeIdx = 0;
let crossfading = false;

// Per-worklet readiness flags so a single failed module doesn't disable
// all four effects.  Settled individually via Promise.allSettled below.
const _workletReady = {
  bitcrusher: false,
  stutter: false,
  freeze: false,
  glitch: false,
};
function _anyWorkletReady() {
  return _workletReady.bitcrusher || _workletReady.stutter
       || _workletReady.freeze || _workletReady.glitch;
}

function ensureAudioGraph() {
  if (_ctx) return _ctx;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) {
    console.warn("Web Audio API unavailable; falling back to plain <audio>.");
    return null;
  }
  _ctx = new Ctx();
  for (const d of decks) {
    d.source = _ctx.createMediaElementSource(d.audio);
    d.gain   = _ctx.createGain();
    d.gain.gain.value = 0;
    d.source.connect(d.gain);
    d.gain.connect(_ctx.destination);
    // Silent analyser tap — pulled only for silence detection.  Pre-gain
    // so we measure the actual track signal, not our crossfade ramp.
    d.analyser = _ctx.createAnalyser();
    d.analyser.fftSize = 512;
    d.source.connect(d.analyser);
    d._silenceMs = 0;
  }
  decks[0].gain.gain.value = 1;
  decks[1].gain.gain.value = 0;
  // Load AudioWorklets asynchronously.  Effects fall back to vanilla
  // Web Audio nodes (WaveShaper, GainNode automation) if a worklet
  // isn't ready in time.
  if (_ctx.audioWorklet) {
    const _load = (name, url) =>
      _ctx.audioWorklet.addModule(url).then(
        () => { _workletReady[name] = true; },
        (err) => { console.warn(`AudioWorklet load failed for ${name}:`, err); },
      );
    _load("bitcrusher", "/bitcrusher-worklet.js");
    _load("stutter",    "/stutter-worklet.js");
    _load("freeze",     "/freeze-worklet.js");
    _load("glitch",     "/glitch-worklet.js");
  }
  return _ctx;
}

function deckActive() { return decks[activeIdx]; }
function deckStandby() { return decks[activeIdx ^ 1]; }

function stopAllDecks() {
  // Hard stop — used when the server disconnects so audio doesn't keep
  // playing from buffered files after the control surface is gone.
  for (const d of decks) {
    try { d.audio.pause(); } catch (_) {}
    try { d.audio.currentTime = 0; } catch (_) {}
    if (d.gain) {
      try {
        d.gain.gain.cancelScheduledValues(_ctx ? _ctx.currentTime : 0);
        d.gain.gain.value = 0;
      } catch (_) {}
    }
  }
  crossfading = false;
}

function setSrcOnDeck(deck, path) {
  if (deck.path === path) return;
  deck.path = path;
  deck.audio.src = "/api/audio?path=" + encodeURIComponent(path);
  // Kick off background decode so spin / tape_stop / freeze effects have
  // the AudioBuffer ready when the crossfade fires.  Decoded buffers are
  // cached in _bufferCache so repeated transitions on the same track
  // don't re-fetch.
  if (typeof _decodeFor === "function") {
    _decodeFor(path).catch(() => {});
  }
}

function playOnDeck(deck) {
  return deck.audio.play().catch((err) => {
    console.warn("deck.play failed:", err);
  });
}

function setVolume(linear) {
  // Master volume rides on whichever gain node is "live".  In normal
  // play we apply the slider value directly to the active deck's gain,
  // multiplied by 1.0 (= full).  During a crossfade the per-deck gains
  // are being ramped, so we wait until that completes before touching
  // them again — the slider's effective value is captured by `_volume`.
  _volume = isIOS ? 1 : linear;
  if (!_ctx || crossfading) return;
  for (let i = 0; i < decks.length; i++) {
    const target = (i === activeIdx) ? _volume : 0;
    decks[i].gain.gain.cancelScheduledValues(_ctx.currentTime);
    decks[i].gain.gain.setValueAtTime(target, _ctx.currentTime);
  }
}
let _volume = 1;

// ----------------------------------------------------------------
// Browser-side transition effects (Web Audio API).  Each effect builds
// a small node graph between its target deck's source and gain, runs
// for fadeSec, and disconnects on teardown.
// ----------------------------------------------------------------

let _lastTransitionFx = "none";
let _rotateCursor = -1;

function _resolveTransition(name) {
  const real = ["echo_out", "reverb_tail", "highpass_sweep", "lowpass_sweep",
    "tape_stop", "gate_stutter", "noise_riser", "noise_drop",
    "cross_eq_swap", "bitcrusher", "flanger", "pitch_swell", "pitch_fall",
    "telephone",
    "backspin", "forward_spin", "chorus", "submerge", "vinyl_wow",
    "freeze", "glitch",
    "scratch", "beat_repeat", "sidechain_pump", "reverse_reverb", "air_horn",
    "vinyl_rewind", "transformer", "dub_siren", "stutter_build", "wow_flutter",
    "phaser", "ring_modulator", "dub_delay", "halftime"];
  if (name === "random") return real[Math.floor(Math.random() * real.length)];
  if (name === "rotate") {
    _rotateCursor = (_rotateCursor + 1) % real.length;
    return real[_rotateCursor];
  }
  return name || "none";
}

function _routeThrough(deck, headNode) {
  try { deck.source.disconnect(); } catch (_) {}
  deck.source.connect(headNode);
}
function _restoreDirect(deck) {
  try { deck.source.disconnect(); } catch (_) {}
  deck.source.connect(deck.gain);
}

// Impulse-response cache.  WeakMap keyed by AudioContext (lets the
// buffers GC if the context ever goes away), inner Map keyed by
// "shape:durationSec:decay" so the same reverb tail / submerge wash /
// reverse_reverb swell built every transition reuses the same fp32
// stereo buffer instead of re-allocating ~1.5 MB and re-running the
// RNG fill on each crossfade.  Pattern borrowed from chat_grid's
// client/src/audio/effects.ts (getCachedImpulseResponse).
const _irCache = new WeakMap();

function _cachedIR(key, build) {
  let ctxCache = _irCache.get(_ctx);
  if (!ctxCache) {
    ctxCache = new Map();
    _irCache.set(_ctx, ctxCache);
  }
  const fullKey = `${_ctx.sampleRate}:${key}`;
  let buf = ctxCache.get(fullKey);
  if (!buf) {
    buf = build();
    ctxCache.set(fullKey, buf);
  }
  return buf;
}

function _makeReverbIR(durationSec, decay) {
  return _cachedIR(`fwd:${durationSec}:${decay}`, () => {
    const sr = _ctx.sampleRate;
    const n = Math.max(1, Math.floor(sr * durationSec));
    const buf = _ctx.createBuffer(2, n, sr);
    for (let ch = 0; ch < 2; ch++) {
      const data = buf.getChannelData(ch);
      for (let i = 0; i < n; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / n, decay);
    }
    return buf;
  });
}

function _makeReverseReverbIR(durationSec, scale) {
  return _cachedIR(`rev:${durationSec}:${scale}`, () => {
    const sr = _ctx.sampleRate;
    const n = Math.max(1, Math.floor(sr * durationSec));
    const buf = _ctx.createBuffer(2, n, sr);
    for (let ch = 0; ch < 2; ch++) {
      const data = buf.getChannelData(ch);
      for (let i = 0; i < n; i++) {
        const env = (i / n) ** 2;
        data[i] = (Math.random() * 2 - 1) * env * scale;
      }
    }
    return buf;
  });
}

// Best-effort disconnect helper.  Effect teardown can fire after the
// AudioContext has already torn the graph down (e.g. on rapid skip),
// so disconnect() can throw "node is not connected".  Swallow per node
// instead of repeating try/catch blocks at every call site.
function _disconnectAll(...nodes) {
  for (const n of nodes) {
    if (!n) continue;
    try { n.disconnect(); } catch (_) {}
  }
}

// Real reverse / fast-forward playback via decoded AudioBuffer.  HTML
// <audio> can't go negative on playbackRate, so we fetch + decode the
// audio file, slice the relevant chunk, optionally reverse it, and play
// through an AudioBufferSourceNode while muting the live deck.  Caches
// the decoded buffer per path so a Skip → Skip cycle doesn't re-decode.
const _bufferCache = new Map();   // path → AudioBuffer

async function _decodeFor(path) {
  if (_bufferCache.has(path)) return _bufferCache.get(path);
  const url = "/api/audio?path=" + encodeURIComponent(path);
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`audio fetch ${resp.status}`);
  const arr = await resp.arrayBuffer();
  const buf = await _ctx.decodeAudioData(arr);
  // Cap cache at 4 entries — these can be 100+ MB each
  if (_bufferCache.size >= 4) {
    const k = _bufferCache.keys().next().value;
    _bufferCache.delete(k);
  }
  _bufferCache.set(path, buf);
  return buf;
}

function _doSpin(ctx, outDeck, t0, fadeSec, reverse, teardowns, slow = false) {
  // Real reverse / push-forward via decoded AudioBuffer + AudioBufferSource.
  // Critical detail: the buffer source routes DIRECT to ctx.destination,
  // bypassing deck.gain — otherwise the crossfade ramp silences the spin
  // before it's heard.  We mute the live HTMLMediaElement AND ramp
  // deck.gain to 0 immediately so the only audible source is our spin.
  //
  // Industry envelope:
  //   reverse: rate decays 2.0 → 0.05 (vinyl friction physics)
  //   forward: rate accelerates 0.05 → 2.5 (mirror of backspin —
  //            record starts at a near-stop, friction-released into
  //            full forward speed at the cut)
  const path = outDeck.path;
  const currentT = outDeck.audio.currentTime;
  const spinSec = Math.max(fadeSec, 2.5);
  const windowSec = Math.max(spinSec * 1.5, 4.0);

  outDeck.audio.muted = true;
  // Force live deck silent — caller's crossfade ramp may not reach 0 fast
  // enough.  We restore on teardown.
  const prevGain = outDeck.gain.gain.value;
  outDeck.gain.gain.cancelScheduledValues(t0);
  outDeck.gain.gain.setValueAtTime(0, t0);

  let bufSrc = null;
  let bufGain = null;
  let synthNoise = null, synthBp = null, synthG = null;
  let cancelled = false;

  // Synth friction noise routes direct to destination too.  Quiet so it
  // doesn't drown the actual reversed audio.
  const noiseBuf = ctx.createBuffer(1, ctx.sampleRate * spinSec, ctx.sampleRate);
  const nd = noiseBuf.getChannelData(0);
  for (let i = 0; i < nd.length; i++) nd[i] = (Math.random() * 2 - 1) * 0.6;
  synthNoise = ctx.createBufferSource(); synthNoise.buffer = noiseBuf;
  synthBp = ctx.createBiquadFilter();
  synthBp.type = "bandpass"; synthBp.Q.value = 1.5;
  if (reverse) {
    synthBp.frequency.setValueAtTime(2500, t0);
    synthBp.frequency.exponentialRampToValueAtTime(120, t0 + spinSec);
  } else {
    synthBp.frequency.setValueAtTime(120, t0);
    synthBp.frequency.exponentialRampToValueAtTime(2500, t0 + spinSec);
  }
  synthG = ctx.createGain();
  synthG.gain.setValueAtTime(0.0, t0);
  synthG.gain.linearRampToValueAtTime(0.20, t0 + 0.05);
  synthG.gain.linearRampToValueAtTime(0.15, t0 + spinSec * 0.6);
  synthG.gain.exponentialRampToValueAtTime(0.001, t0 + spinSec);
  synthNoise.connect(synthBp); synthBp.connect(synthG); synthG.connect(ctx.destination);
  synthNoise.start();

  _decodeFor(path).then((buf) => {
    if (cancelled) return;
    const sr = buf.sampleRate;
    const startSamp = Math.max(0, Math.floor((currentT - windowSec) * sr));
    const endSamp = Math.min(buf.length, Math.floor(currentT * sr));
    const len = endSamp - startSamp;
    if (len <= 0) return;
    const chunk = ctx.createBuffer(buf.numberOfChannels, len, sr);
    for (let ch = 0; ch < buf.numberOfChannels; ch++) {
      const dst = chunk.getChannelData(ch);
      const src = buf.getChannelData(ch);
      if (reverse) {
        for (let i = 0; i < len; i++) dst[i] = src[endSamp - 1 - i];
      } else {
        for (let i = 0; i < len; i++) dst[i] = src[startSamp + i];
      }
    }
    bufSrc = ctx.createBufferSource();
    bufSrc.buffer = chunk;
    if (reverse && slow) {
      // vinyl_rewind: slow musical reverse 1.0 → 0.5 (one-octave drop)
      bufSrc.playbackRate.setValueAtTime(1.0, t0);
      bufSrc.playbackRate.linearRampToValueAtTime(0.5, t0 + spinSec);
    } else if (reverse) {
      bufSrc.playbackRate.setValueAtTime(2.0, t0);
      bufSrc.playbackRate.linearRampToValueAtTime(0.05, t0 + spinSec);
    } else {
      // True mirror of the backspin envelope — slow start, accelerating
      // INTO the cut.  Without this the forward variant just sounded
      // like a fast-forward, not a deliberate spin.
      bufSrc.playbackRate.setValueAtTime(0.05, t0);
      bufSrc.playbackRate.linearRampToValueAtTime(2.5, t0 + spinSec);
    }
    bufGain = ctx.createGain();
    bufGain.gain.setValueAtTime(_volume, t0);
    bufGain.gain.setValueAtTime(_volume, t0 + spinSec - 0.3);
    bufGain.gain.linearRampToValueAtTime(0.0, t0 + spinSec);
    // DIRECT route — bypass deck.gain so crossfade ramp doesn't silence us.
    bufSrc.connect(bufGain); bufGain.connect(ctx.destination);
    bufSrc.start();
  }).catch((err) => {
    console.warn("spin decode failed, falling back to friction-only:", err);
  });

  teardowns.push(() => {
    cancelled = true;
    outDeck.audio.muted = false;
    // Don't restore prevGain — the crossfade has handed off to the new
    // deck.  Setting it back here would cause a brief audible pop of the
    // already-finished outgoing track.
    if (bufSrc) {
      try { bufSrc.stop(); } catch (_) {}
      _disconnectAll(bufSrc, bufGain);
    }
    if (synthNoise) {
      try { synthNoise.stop(); } catch (_) {}
      _disconnectAll(synthNoise, synthBp, synthG);
    }
  });
}

function _doTapeStop(ctx, outDeck, t0, fadeSec, teardowns) {
  // Tape stop = forward playback with playbackRate ramping to 0 over
  // the full fade.  Routes DIRECT to ctx.destination (bypassing
  // deck.gain) so the slow-down is heard at full volume rather than
  // being silenced by the crossfade ramp.
  const path = outDeck.path;
  const currentT = outDeck.audio.currentTime;
  const stopSec = Math.max(fadeSec, 4.0);
  outDeck.audio.muted = true;
  outDeck.gain.gain.cancelScheduledValues(t0);
  outDeck.gain.gain.setValueAtTime(0, t0);

  let bufSrc = null, bufGain = null;
  let cancelled = false;

  _decodeFor(path).then((buf) => {
    if (cancelled) return;
    const sr = buf.sampleRate;
    const startSamp = Math.floor(currentT * sr);
    const endSamp = Math.min(buf.length, startSamp + Math.floor(stopSec * 2 * sr));
    const len = endSamp - startSamp;
    if (len <= 0) return;
    const chunk = ctx.createBuffer(buf.numberOfChannels, len, sr);
    for (let ch = 0; ch < buf.numberOfChannels; ch++) {
      chunk.getChannelData(ch).set(buf.getChannelData(ch).subarray(startSamp, endSamp));
    }
    bufSrc = ctx.createBufferSource();
    bufSrc.buffer = chunk;
    // Quadratic-ish slowdown — sounds like a real tape brake.
    bufSrc.playbackRate.setValueAtTime(1.0, t0);
    bufSrc.playbackRate.linearRampToValueAtTime(0.4, t0 + stopSec * 0.5);
    bufSrc.playbackRate.exponentialRampToValueAtTime(0.001, t0 + stopSec);
    bufGain = ctx.createGain();
    bufGain.gain.setValueAtTime(_volume, t0);
    bufGain.gain.setValueAtTime(_volume, t0 + stopSec - 0.2);
    bufGain.gain.linearRampToValueAtTime(0.0, t0 + stopSec);
    bufSrc.connect(bufGain); bufGain.connect(ctx.destination);
    bufSrc.start();
  }).catch((err) => {
    console.warn("tape_stop decode failed:", err);
    outDeck.audio.muted = false;
  });

  teardowns.push(() => {
    cancelled = true;
    outDeck.audio.muted = false;
    if (bufSrc) {
      try { bufSrc.stop(); } catch (_) {}
      _disconnectAll(bufSrc, bufGain);
    }
  });
}

// Industry-standard minimum effect lengths (seconds).  Sourced from
// commercial DJ-tool defaults (Pioneer DJM, Reloop RMX, Numark NS).
// Mirrors Player._MIN_FX_DURATION_S on the Python side so CLI and
// browser playback feel identical.
const _MIN_FX_DURATION_S = {
  tape_stop:      4.0,
  backspin:       2.5,
  forward_spin:   2.5,
  noise_riser:    4.0,
  noise_drop:     3.0,
  reverb_tail:    4.0,
  freeze:         4.0,
  glitch:         3.0,
  echo_out:       3.0,
  scratch:        2.0,
  beat_repeat:    3.0,
  sidechain_pump: 4.0,
  reverse_reverb: 3.0,
  air_horn:       3.0,
  vinyl_rewind:   3.5,
  transformer:    2.5,
  dub_siren:      3.0,
  stutter_build:  3.0,
  wow_flutter:    2.5,
  phaser:         3.0,
  ring_modulator: 2.5,
  dub_delay:      4.0,
  halftime:       3.0,
};

// Per-effect fraction of the outgoing track's *outro* the effect should
// occupy when an outro length is known.  Effects that need to "fill the
// tail" (reverb, echo throws, risers) consume more of it; effects that
// punctuate (scratch, air horn, glitch) take less.  Used by
// `_effectDurationFor` — falls back to `_MIN_FX_DURATION_S` when no
// outro length is available.
const _OUTRO_FRACTION = {
  reverb_tail:    0.85,
  reverse_reverb: 0.80,
  echo_out:       0.75,
  noise_riser:    0.90,
  noise_drop:     0.50,
  tape_stop:      0.60,
  freeze:         0.55,
  submerge:       0.70,
  lowpass_sweep:  0.80,
  highpass_sweep: 0.80,
  cross_eq_swap:  0.80,
  sidechain_pump: 0.70,
  pitch_swell:    0.65,
  pitch_fall:     0.65,
  vinyl_wow:      0.60,
  flanger:        0.70,
  chorus:         0.70,
  telephone:      0.70,
  beat_repeat:    0.45,
  gate_stutter:   0.45,
  glitch:         0.35,
  bitcrusher:     0.55,
  scratch:        0.30,
  air_horn:       0.25,
  backspin:       0.45,
  forward_spin:   0.45,
  vinyl_rewind:   0.55,
  transformer:    0.45,
  dub_siren:      0.30,
  stutter_build:  0.50,
  wow_flutter:    0.65,
  phaser:         0.70,
  ring_modulator: 0.55,
  dub_delay:      0.80,
  halftime:       0.55,
};
const _MAX_FX_DURATION_S = 12.0;
const _ABS_MIN_FX_DURATION_S = 1.0;

// --- FX bar-length table (mirrors autodj.beat_sync.FX_BAR_TABLE in Python) ---
//
// Each entry is [bars, snapToDownbeat]:
//   bars              integer bar count used by _effectDurationFor when
//                     beat-sync is enabled.  fadeSec rounds to N bars at
//                     the blended outgoing->incoming tempo.
//   snapToDownbeat    when true, the effect's first scheduled event lands
//                     on the next outgoing downbeat (≤ 1 bar of latency).
//                     Pure ambient envelope FX get false.
const _FX_BAR_TABLE = {
  beat_repeat:    [4, true],
  gate_stutter:   [4, true],
  stutter_build:  [4, true],
  sidechain_pump: [8, true],
  halftime:       [4, true],
  transformer:    [2, true],
  echo_out:       [4, true],
  dub_delay:      [8, true],
  scratch:        [2, true],
  noise_riser:    [4, true],
  noise_drop:     [4, true],
  reverse_reverb: [4, true],
  air_horn:       [2, true],
  dub_siren:      [4, true],
  highpass_sweep: [4, false],
  lowpass_sweep:  [4, false],
  cross_eq_swap:  [4, false],
  submerge:       [4, false],
  telephone:      [4, false],
  chorus:         [4, false],
  phaser:         [4, false],
  flanger:        [4, false],
  wow_flutter:    [4, false],
  vinyl_wow:      [4, false],
  ring_modulator: [4, false],
  bitcrusher:     [4, false],
  pitch_swell:    [2, true],
  pitch_fall:     [2, true],
  tape_stop:      [2, true],
  backspin:       [2, true],
  forward_spin:   [2, true],
  vinyl_rewind:   [4, true],
  freeze:         [2, true],
  glitch:         [4, true],
  reverb_tail:    [4, false],
};

// --- _BS: BeatSync helper.  Refreshed at the start of every crossfade
// from the server-emitted track payload (downbeats_outro / downbeats_intro
// / key_hz) plus the cached BPMs.  All accessors take an AudioContext
// time so the math stays sample-accurate. ---
const _BS = {
  enabled: false,
  keyEnabled: false,
  outBpm: 0,
  inBpm: 0,
  fxStartCtx: 0,
  fxDurCtx: 0,
  // Mapping audio.currentTime <-> ctx.currentTime captured at refresh.
  audioToCtxOffset: 0,    // ctx_t = audio_t + offset
  outDownbeatsCtx: [],    // outgoing downbeats already in ctx-time
  outKeyHz: null,
  inKeyHz: null,

  refresh(outDeck, fxStartCtx, fxDurCtx) {
    this.enabled = !!_beatSyncEnabled;
    this.keyEnabled = !!_keySyncEnabled;
    this.outBpm = _outBpmCache || 0;
    this.inBpm = _inBpmCache || 0;
    this.fxStartCtx = fxStartCtx;
    this.fxDurCtx = Math.max(0.001, fxDurCtx);
    this.outKeyHz = _outKeyHzCache;
    this.inKeyHz = _inKeyHzCache;

    // Build the audio<->ctx offset from the active deck's currentTime now.
    let audioT = 0;
    try { audioT = outDeck.audio.currentTime || 0; } catch (_) { audioT = 0; }
    this.audioToCtxOffset = fxStartCtx - audioT;

    // Translate outgoing downbeats (audio time) into ctx time + drop those
    // strictly in the past so callers iterate forward only.
    const tCtxNow = fxStartCtx;
    const list = (_outDownbeatsCache || [])
      .map((d) => d + this.audioToCtxOffset)
      .filter((t) => t >= tCtxNow - 0.05);
    this.outDownbeatsCtx = list;
  },

  // Linear blend across the fade; clamps frac.
  bpmAt(tCtx) {
    const f = Math.max(0, Math.min(1, (tCtx - this.fxStartCtx) / this.fxDurCtx));
    if (this.outBpm > 0 && this.inBpm > 0) {
      return this.outBpm * (1 - f) + this.inBpm * f;
    }
    return this.outBpm > 0 ? this.outBpm : (this.inBpm > 0 ? this.inBpm : 120);
  },

  // Seconds per beat at the blended tempo (1/4 note).
  beatSec(tCtx) { return 60 / Math.max(1, this.bpmAt(tCtx)); },

  // Seconds per bar at the blended tempo (4/4).
  barSec(tCtx) { return this.beatSec(tCtx) * 4; },

  // First downbeat (in ctx time) >= tCtx.  Returns tCtx itself when no
  // grid is available so callers can use the result unconditionally.
  nextDownbeat(tCtx) {
    if (!this.enabled) return tCtx;
    for (const d of this.outDownbeatsCtx) {
      if (d >= tCtx - 0.005) return d;
    }
    // Synthesize from blended BPM when grid is exhausted (fallback grid
    // anchored at fxStart).
    const bs = this.barSec(tCtx);
    if (bs <= 0) return tCtx;
    const phase = ((tCtx - this.fxStartCtx) % bs + bs) % bs;
    return tCtx + (phase < 0.005 ? 0 : (bs - phase));
  },

  // Log-space lerp from outgoing root -> incoming root.  Returns null
  // when neither side has a known key (caller falls back to its hardcoded
  // frequency) or when key-sync is disabled.
  rootHzAt(tCtx) {
    if (!this.keyEnabled) return null;
    const out = this.outKeyHz, inn = this.inKeyHz;
    if (!out && !inn) return null;
    if (out && !inn) return out;
    if (!out && inn) return inn;
    const f = Math.max(0, Math.min(1, (tCtx - this.fxStartCtx) / this.fxDurCtx));
    return Math.exp(Math.log(out) * (1 - f) + Math.log(inn) * f);
  },
};

function _effectDurationFor(effect, fadeSec, outroLen) {
  // Static floor (per-effect minimum) — always honoured.
  const staticMin = _MIN_FX_DURATION_S[effect] || 0;
  // Without a known outro, fall back to the legacy "max of fade and
  // per-effect floor" behaviour.
  if (outroLen == null || !(outroLen > 0)) {
    return Math.max(fadeSec, staticMin);
  }
  const frac = _OUTRO_FRACTION[effect] != null ? _OUTRO_FRACTION[effect] : 0.5;
  let target = outroLen * frac;
  // Clamp: never below the per-effect floor (or absolute 1.0s), never
  // above 12s — keeps musically sane boundaries even on edge tracks.
  const lo = Math.max(_ABS_MIN_FX_DURATION_S, staticMin);
  let dur = Math.min(_MAX_FX_DURATION_S, Math.max(lo, target));

  // Beat-sync rounding: when enabled and the outgoing track has a known
  // BPM, round the target up to the nearest whole bar count from the
  // FX_BAR_TABLE so rhythmic effects fit an integer number of bars.
  if (_beatSyncEnabled && _outBpmCache > 0 && _FX_BAR_TABLE[effect]) {
    const bars = _FX_BAR_TABLE[effect][0];
    const barSec = 60 * 4 / _outBpmCache;     // outgoing-track bar length
    // Pick the bar count whose total duration is closest to `dur` while
    // staying inside the [lo, _MAX_FX_DURATION_S] envelope.  Snapping to
    // the FX_BAR_TABLE default first, then halving / doubling if it
    // falls outside the clamp window.
    let candidate = bars * barSec;
    if (candidate > _MAX_FX_DURATION_S) {
      // Halve until it fits (covers very slow tempos: 60 BPM x 8 bars = 32 s).
      while (candidate > _MAX_FX_DURATION_S && candidate > lo) {
        candidate = candidate / 2;
      }
    } else if (candidate < lo) {
      // Double until it clears the floor (very fast tempos: 180 BPM x 2 bars = 2.7 s).
      while (candidate < lo && candidate < _MAX_FX_DURATION_S) {
        candidate = candidate * 2;
      }
    }
    dur = Math.min(_MAX_FX_DURATION_S, Math.max(lo, candidate));
  }
  return dur;
}

function applyTransitionFx(effect, fadeSec, outDeck, inDeck) {
  const ctx = _ctx;
  if (!ctx || effect === "none" || !effect) return () => {};
  // Caller (startCrossfade) now resolves the effect-preferred duration
  // and passes it in so the gain ramp and the effect scheduling share
  // one timeline.  Direct callers (none currently, but defensive) get
  // legacy behaviour via the floor when no outro length is known.
  if (fadeSec == null || !(fadeSec > 0)) {
    fadeSec = _effectDurationFor(effect, 3.0, _currentOutroLenCache);
  }
  const t0 = ctx.currentTime;
  const tEnd = t0 + fadeSec;
  const teardowns = [];

  function tearAll() {
    for (const fn of teardowns) { try { fn(); } catch (_) {} }
    _restoreDirect(outDeck);
    _restoreDirect(inDeck);
  }

  if (effect === "lowpass_sweep") {
    // Outgoing track keeps full volume while a steep low-pass closes,
    // then drops sharply at the end.  Overriding deck.gain here is
    // safe because applyTransitionFx now runs AFTER startCrossfade has
    // already scheduled its baseline ramps — our writes win.
    const f = ctx.createBiquadFilter();
    f.type = "lowpass";
    f.Q.value = 0.9;
    const sweepEnd = t0 + Math.max(0.5, fadeSec * 0.7);
    f.frequency.setValueAtTime(ctx.sampleRate / 2, t0);
    f.frequency.exponentialRampToValueAtTime(180, sweepEnd);
    _routeThrough(outDeck, f);
    f.connect(outDeck.gain);
    // Keep outgoing loud while the filter sweeps, then a fast 200 ms
    // drop at the very end so the cut is clean.
    outDeck.gain.gain.cancelScheduledValues(t0);
    outDeck.gain.gain.setValueAtTime(_volume, t0);
    outDeck.gain.gain.setValueAtTime(_volume, Math.max(t0, tEnd - 0.2));
    outDeck.gain.gain.linearRampToValueAtTime(0, tEnd);
    teardowns.push(() => f.disconnect());
  }
  else if (effect === "highpass_sweep") {
    // Incoming track plays at FULL volume but heavily filtered, so the
    // bass-bloom is unmistakable — without overriding inDeck.gain the
    // standard 0 → _volume ramp masks the filter character (everything
    // sounds like "muffled fade-in" instead of "filter-in").
    const f = ctx.createBiquadFilter();
    f.type = "highpass";
    f.Q.value = 0.9;
    const sweepEnd = t0 + Math.max(0.5, fadeSec * 0.7);
    f.frequency.setValueAtTime(6000, t0);
    f.frequency.exponentialRampToValueAtTime(50, sweepEnd);
    _routeThrough(inDeck, f);
    f.connect(inDeck.gain);
    inDeck.gain.gain.cancelScheduledValues(t0);
    inDeck.gain.gain.setValueAtTime(_volume, t0);
    teardowns.push(() => f.disconnect());
  }
  else if (effect === "cross_eq_swap") {
    const fOut = ctx.createBiquadFilter();
    fOut.type = "highpass"; fOut.frequency.value = 250;
    _routeThrough(outDeck, fOut); fOut.connect(outDeck.gain);
    const fIn = ctx.createBiquadFilter();
    fIn.type = "lowpass";
    fIn.frequency.setValueAtTime(250, t0);
    fIn.frequency.exponentialRampToValueAtTime(ctx.sampleRate / 2, tEnd);
    _routeThrough(inDeck, fIn); fIn.connect(inDeck.gain);
    teardowns.push(() => fOut.disconnect());
    teardowns.push(() => fIn.disconnect());
  }
  else if (effect === "echo_out") {
    // Tempo-synced eighth-note echo throw.  delayTime = beatSec / 2
    // (1/8 note) so the tail subdivides the outgoing groove instead of
    // sitting at the legacy 375 ms.  Route the wet path DIRECT to
    // destination so the echo tail survives after deck.gain has ramped
    // to silence.
    const delay = ctx.createDelay(2.0);
    const eighth = _BS.beatSec(t0) / 2;
    delay.delayTime.value = Math.max(0.05, Math.min(1.5, eighth));
    const fb = ctx.createGain(); fb.gain.value = 0.6;
    const wet = ctx.createGain();
    wet.gain.setValueAtTime(_volume * 0.85, t0);
    wet.gain.setValueAtTime(_volume * 0.85, t0 + fadeSec * 0.6);
    wet.gain.exponentialRampToValueAtTime(0.001, tEnd);
    outDeck.source.connect(delay);
    delay.connect(fb); fb.connect(delay);
    delay.connect(wet); wet.connect(ctx.destination);
    teardowns.push(() => _disconnectAll(delay, fb, wet));
  }
  else if (effect === "reverb_tail") {
    // Big-hall reverb that survives the crossfade.  Wet path bypasses
    // deck.gain entirely so the tail rings even after the dry signal
    // is silenced.  IR length 4 s + decay=1.8 (slower decay = longer
    // audible tail).  Send level boosted +6 dB above unity so the wet
    // reads as loud as the dry would have been.
    const conv = ctx.createConvolver();
    conv.buffer = _makeReverbIR(4.0, 1.8);
    const send = ctx.createGain(); send.gain.value = 2.0;   // +6 dB pre-conv
    const wet = ctx.createGain();
    wet.gain.setValueAtTime(_volume * 1.2, t0);
    wet.gain.setValueAtTime(_volume * 1.2, t0 + fadeSec * 0.4);
    wet.gain.exponentialRampToValueAtTime(0.001, t0 + fadeSec + 1.0);
    outDeck.source.connect(send); send.connect(conv); conv.connect(wet);
    wet.connect(ctx.destination);
    teardowns.push(() => _disconnectAll(conv, wet, send));
  }
  else if (effect === "telephone") {
    // Real telephone band-pass + saturation + heavy compression.
    // Without saturation it just sounds slightly muffled, not phone-y.
    const hp = ctx.createBiquadFilter();
    hp.type = "highpass"; hp.frequency.value = 500; hp.Q.value = 3;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass";  lp.frequency.value = 2800; lp.Q.value = 3;
    // Saturation curve — soft-clip with mild even-harmonic bias
    const shaper = ctx.createWaveShaper();
    const n = 1024;
    const curve = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const x = (i / (n - 1)) * 2 - 1;
      curve[i] = Math.tanh(x * 4) * 0.85;
    }
    shaper.curve = curve;
    const drive = ctx.createGain(); drive.gain.value = 1.6;
    _routeThrough(outDeck, hp);
    hp.connect(lp); lp.connect(drive); drive.connect(shaper);
    shaper.connect(outDeck.gain);
    teardowns.push(() => _disconnectAll(hp, lp, drive, shaper));
  }
  else if (effect === "flanger") {
    // Classic flanger: short delay (1-10 ms) modulated by slow LFO,
    // with feedback to intensify the comb-filter resonance.
    const delay = ctx.createDelay(0.02); delay.delayTime.value = 0.005;
    const lfo = ctx.createOscillator(); lfo.frequency.value = 0.5;
    const lfoGain = ctx.createGain(); lfoGain.gain.value = 0.004;
    lfo.connect(lfoGain); lfoGain.connect(delay.delayTime);
    const fb = ctx.createGain(); fb.gain.value = 0.55;   // feedback for resonance
    delay.connect(fb); fb.connect(delay);
    const wet = ctx.createGain(); wet.gain.value = 0.55;
    outDeck.source.connect(delay); delay.connect(wet); wet.connect(outDeck.gain);
    lfo.start();
    teardowns.push(() => {
      try { lfo.stop(); } catch (_) {}
      _disconnectAll(delay, fb, wet, lfoGain);
    });
  }
  else if (effect === "bitcrusher") {
    // AudioWorklet-based bitcrusher: sample-rate reduction + bit-depth
    // quantisation give the authentic 8-bit-console / Atari sound.
    // Worklet module loads at AudioContext boot; if a transition fires
    // before it finishes (first ~50 ms of session) we just skip the
    // effect for that one crossfade — no WaveShaper fallback.
    if (!_workletReady.bitcrusher) {
      // WaveShaper fallback — quantises amplitude only (no rate-reduce
      // sample-and-hold) but still produces a recognisable crunch so
      // the effect is never silent on browsers where the worklet
      // module fails to load.
      console.warn("bitcrusher worklet not ready; falling back to WaveShaper.");
      const shaper = ctx.createWaveShaper();
      const N = 4096;
      const curve = new Float32Array(N);
      const levels = 4;  // 3-bit quantise
      for (let i = 0; i < N; i++) {
        const x = (i / (N - 1)) * 2 - 1;
        curve[i] = Math.round(x * levels) / levels;
      }
      shaper.curve = curve;
      shaper.oversample = "none";
      _routeThrough(outDeck, shaper);
      shaper.connect(outDeck.gain);
      outDeck.gain.gain.cancelScheduledValues(t0);
      outDeck.gain.gain.setValueAtTime(_volume, t0);
      outDeck.gain.gain.setValueAtTime(_volume, Math.max(t0, tEnd - 0.3));
      outDeck.gain.gain.linearRampToValueAtTime(0, tEnd);
      teardowns.push(() => shaper.disconnect());
      return tearAll;
    }
    const node = new AudioWorkletNode(ctx, "bitcrusher", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [2],
    });
    // Outgoing stays loud through the entire effect, then drops in 300 ms
    // at the very end — without this the deck-gain crossfade ramp masks
    // the lo-fi character.
    outDeck.gain.gain.cancelScheduledValues(t0);
    outDeck.gain.gain.setValueAtTime(_volume, t0);
    outDeck.gain.gain.setValueAtTime(_volume, Math.max(t0, tEnd - 0.3));
    outDeck.gain.gain.linearRampToValueAtTime(0, tEnd);
    const bitsParam = node.parameters.get("bits");
    const rateParam = node.parameters.get("rateReduce");
    // Peak crush at 25 % of fade — by the halfway mark the crossfade
    // gain ramp has already dropped the outgoing track to ~50 %, so
    // the lo-fi character has to land EARLY to be perceived.  Bottom
    // out at 2 bits / 24× rate-reduce for an unmistakable Atari sound.
    const peakAt = t0 + Math.max(0.4, fadeSec * 0.25);
    bitsParam.setValueAtTime(12, t0);
    bitsParam.linearRampToValueAtTime(2, peakAt);
    bitsParam.setValueAtTime(2, tEnd);
    rateParam.setValueAtTime(1, t0);
    rateParam.linearRampToValueAtTime(24, peakAt);
    rateParam.setValueAtTime(24, tEnd);
    _routeThrough(outDeck, node);
    node.connect(outDeck.gain);
    teardowns.push(() => { try { node.disconnect(); } catch (_) {} });
  }
  else if (effect === "gate_stutter") {
    // Sample-accurate stutter via AudioWorklet — raised-cosine fades on
    // every gate edge eliminate the clicks that GainNode setValueAtTime
    // produces at hard 1→0 transitions.  Falls back to setValueAtTime
    // scheduling when the worklet hasn't loaded yet.
    if (_workletReady.stutter) {
      const node = new AudioWorkletNode(ctx, "stutter");
      const rateParam = node.parameters.get("rate");
      const dutyParam = node.parameters.get("duty");
      // Tempo-synced gate: 1/8-note triplet accelerating to 1/16 over
      // the fade.  Hz = bpm/60 * subdivision.  Falls back to 8->16 Hz
      // when BPM is unknown (matches legacy behaviour).
      const beatHz = (_BS.outBpm > 0 ? _BS.outBpm : 120) / 60;
      const startHz = beatHz * 2;       // 1/8 notes
      const endHz = beatHz * 4;         // 1/16 notes
      rateParam.setValueAtTime(startHz, t0);
      rateParam.linearRampToValueAtTime(endHz, tEnd);
      dutyParam.setValueAtTime(0.25, t0);
      _routeThrough(outDeck, node);
      node.connect(outDeck.gain);
      teardowns.push(() => { try { node.disconnect(); } catch (_) {} });
    } else {
      const wrapper = ctx.createGain();
      wrapper.gain.setValueAtTime(1, t0);
      const beatHz = (_BS.outBpm > 0 ? _BS.outBpm : 120) / 60;
      let t = _BS.nextDownbeat(t0);
      let rate = beatHz * 2;            // 1/8 notes
      const maxRate = beatHz * 4;       // 1/16 notes
      while (t < tEnd) {
        const cycle = 1 / rate;
        wrapper.gain.setValueAtTime(1, t);
        wrapper.gain.setValueAtTime(0, t + cycle * 0.25);
        t += cycle;
        rate = Math.min(maxRate, rate * 1.05);
      }
      _routeThrough(outDeck, wrapper); wrapper.connect(outDeck.gain);
      teardowns.push(() => wrapper.disconnect());
    }
  }
  else if (effect === "noise_riser") {
    // Build over fadeSec, then quickly fade OUT in the last 0.4s so it
    // doesn't end with a hard cut.
    const dur = Math.max(fadeSec, 4);
    const buf = ctx.createBuffer(1, ctx.sampleRate * dur, ctx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
    const src = ctx.createBufferSource(); src.buffer = buf;
    const filter = ctx.createBiquadFilter(); filter.type = "lowpass";
    filter.frequency.setValueAtTime(200, t0);
    filter.frequency.exponentialRampToValueAtTime(16000, tEnd);
    const g = ctx.createGain();
    const peakAt = Math.max(t0 + 0.05, tEnd - 0.4);
    g.gain.setValueAtTime(0.0, t0);
    g.gain.linearRampToValueAtTime(0.4, peakAt);
    g.gain.exponentialRampToValueAtTime(0.001, tEnd);
    src.connect(filter); filter.connect(g); g.connect(ctx.destination);
    src.start();
    teardowns.push(() => {
      try { src.stop(); } catch (_) {}
      src.disconnect(); filter.disconnect(); g.disconnect();
    });
  }
  // -------- noise_drop: opposite of noise_riser --------
  // Starts loud + bright on the OUTGOING side, sweeps down in pitch and
  // fades out as the track flips.  Sounds like a bomb-drop or rocket fly-by.
  else if (effect === "noise_drop") {
    const dur = Math.max(fadeSec, 3);
    const buf = ctx.createBuffer(1, ctx.sampleRate * dur, ctx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
    const src = ctx.createBufferSource(); src.buffer = buf;
    const filter = ctx.createBiquadFilter(); filter.type = "lowpass";
    filter.frequency.setValueAtTime(16000, t0);
    filter.frequency.exponentialRampToValueAtTime(150, tEnd);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.4, t0);
    g.gain.exponentialRampToValueAtTime(0.001, tEnd);
    src.connect(filter); filter.connect(g); g.connect(ctx.destination);
    src.start();
    teardowns.push(() => {
      try { src.stop(); } catch (_) {}
      src.disconnect(); filter.disconnect(); g.disconnect();
    });
  }
  // -------- chorus: detuned delays (lush thick stereo-ish doubling) --------
  else if (effect === "chorus") {
    const voices = 3;
    const rates = [0.4, 0.6, 0.8];
    const depths = [0.003, 0.004, 0.005];
    const baseDelays = [0.020, 0.025, 0.030];
    const wet = ctx.createGain(); wet.gain.value = 0.45;
    const lfos = [], delays = [], gains = [];
    for (let i = 0; i < voices; i++) {
      const d = ctx.createDelay(0.1); d.delayTime.value = baseDelays[i];
      const lfo = ctx.createOscillator(); lfo.frequency.value = rates[i];
      const lg = ctx.createGain(); lg.gain.value = depths[i];
      lfo.connect(lg); lg.connect(d.delayTime);
      outDeck.source.connect(d); d.connect(wet);
      lfo.start();
      lfos.push(lfo); delays.push(d); gains.push(lg);
    }
    wet.connect(outDeck.gain);
    teardowns.push(() => {
      for (const o of lfos) { try { o.stop(); } catch (_) {} }
      for (const o of [...delays, ...gains, wet]) {
        try { o.disconnect(); } catch (_) {}
      }
    });
  }
  // -------- submerge: heavy lowpass + reverb wash (underwater) --------
  else if (effect === "submerge") {
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass"; lp.Q.value = 1.2;
    lp.frequency.setValueAtTime(ctx.sampleRate / 2, t0);
    lp.frequency.exponentialRampToValueAtTime(400, tEnd);
    const conv = ctx.createConvolver();
    conv.buffer = _makeReverbIR(2.5, 2.0);
    const wet = ctx.createGain(); wet.gain.value = 0.6;
    _routeThrough(outDeck, lp);
    lp.connect(outDeck.gain);     // dry filtered
    lp.connect(conv);              // + wet reverb
    conv.connect(wet); wet.connect(outDeck.gain);
    teardowns.push(() => _disconnectAll(lp, conv, wet));
  }
  // -------- vinyl_wow: pitch wobble (drunk turntable) on outgoing --------
  else if (effect === "vinyl_wow") {
    // HTMLMediaElement.preservesPitch defaults to TRUE — that's why
    // playbackRate changes were inaudible: the browser was time-stretching
    // to keep pitch constant.  Disable it so playbackRate modulation
    // becomes a real pitch wobble.
    const audio = outDeck.audio;
    const prevPreserve = audio.preservesPitch !== false;
    try { audio.preservesPitch = false; } catch (_) {}
    const startMs = performance.now();
    const durMs = fadeSec * 1000;
    const iv = setInterval(() => {
      const t = (performance.now() - startMs) / durMs;
      if (t >= 1) {
        clearInterval(iv);
        try { audio.playbackRate = 1.0; } catch (_) {}
        return;
      }
      // 1.2 Hz LFO, depth grows from 5 % to 25 % (a real drunk turntable)
      const depth = 0.05 + 0.20 * t;
      const phase = Math.sin(2 * Math.PI * 1.2 * t * fadeSec);
      try { audio.playbackRate = 1.0 + depth * phase; } catch (_) {}
    }, 16);
    teardowns.push(() => {
      clearInterval(iv);
      try { audio.playbackRate = 1.0; } catch (_) {}
      try { audio.preservesPitch = prevPreserve; } catch (_) {}
    });
  }
  else if (effect === "tape_stop") {
    // Real tape stop via decoded AudioBufferSourceNode — playbackRate
    // can ramp all the way to 0 (HTMLMediaElement floors at ~0.2 with
    // glitches below).  Mute the live deck while the buffered source
    // plays the same audio.
    _doTapeStop(ctx, outDeck, t0, fadeSec, teardowns);
  }
  else if (effect === "backspin") {
    // Real reverse playback via decoded AudioBuffer.  HTMLMediaElement
    // can't go negative on playbackRate, so we fetch+decode the file,
    // reverse the relevant chunk, and play it through an AudioBufferSource
    // while muting the live deck.  Falls back to a fast tape-stop dive
    // if decoding fails (slow connection, unsupported codec).
    _doSpin(ctx, outDeck, t0, fadeSec, /*reverse=*/true, teardowns);
  }
  else if (effect === "forward_spin") {
    _doSpin(ctx, outDeck, t0, fadeSec, /*reverse=*/false, teardowns);
  }
  else if (effect === "freeze") {
    // Granular freeze — capture last grainMs of input and loop with fade.
    // Worklet output goes DIRECT to ctx.destination via its own gain
    // envelope so the looped grain is heard at full volume rather than
    // being silenced by the crossfade ramp.  Live deck source is
    // muted so the loop is the only audible signal.
    if (_workletReady.freeze) {
      // Force stereo output explicitly — without outputChannelCount,
      // some browsers default the worklet output to a single channel,
      // which then upmixes to silence on certain destination
      // configurations.  Routing source → passthrough gain → worklet
      // also stabilises the input frames on Chrome where MediaElementSource
      // → AudioWorkletNode can deliver empty input quanta during the
      // first capture window.
      const node = new AudioWorkletNode(ctx, "freeze", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [2],
      });
      node.parameters.get("grainMs").setValueAtTime(150, t0);
      node.parameters.get("fadeOutSec").setValueAtTime(fadeSec, t0);
      const passthrough = ctx.createGain();
      passthrough.gain.value = 1.0;
      const g = ctx.createGain();
      g.gain.setValueAtTime(_volume * 1.2, t0);
      // CRITICAL: do NOT set audio.muted=true.  MediaElementSource
      // respects the element's muted flag and feeds silence into the
      // worklet — so the freeze captures silence and loops nothing.
      // Just zero deck.gain (downstream of the source tap) instead.
      _routeThrough(outDeck, passthrough);
      passthrough.connect(node);
      node.connect(g); g.connect(ctx.destination);
      // Schedule deck.gain mute slightly AFTER t0 so startCrossfade's
      // own cancelScheduledValues(t0) + ramp doesn't undo it.
      outDeck.gain.gain.setValueAtTime(0, t0 + 0.001);
      teardowns.push(() => _disconnectAll(node, g, passthrough));
    } else {
      // Worklet unavailable (non-secure context — http:// over LAN).
      // Capture last 150 ms of decoded audio and loop it via a
      // BufferSource so the freeze still produces sound.  Routed
      // direct to destination so the deck-gain crossfade can mute the
      // dry path independently.
      console.warn("freeze worklet unavailable; using BufferSource fallback");
      const path = outDeck.path;
      const currentT = outDeck.audio.currentTime;
      const grainSec = 0.15;
      let bufSrc = null, bufGain = null;
      let cancelled = false;
      outDeck.gain.gain.setValueAtTime(0, t0 + 0.001);
      _decodeFor(path).then((buf) => {
        if (cancelled) return;
        const sr = buf.sampleRate;
        const grainLen = Math.floor(grainSec * sr);
        const startSamp = Math.max(0, Math.floor(currentT * sr) - grainLen);
        const grain = ctx.createBuffer(buf.numberOfChannels, grainLen, sr);
        for (let ch = 0; ch < buf.numberOfChannels; ch++) {
          grain.getChannelData(ch).set(
            buf.getChannelData(ch).subarray(startSamp, startSamp + grainLen),
          );
        }
        bufSrc = ctx.createBufferSource();
        bufSrc.buffer = grain;
        bufSrc.loop = true;
        bufGain = ctx.createGain();
        bufGain.gain.setValueAtTime(_volume * 1.2, t0);
        bufGain.gain.linearRampToValueAtTime(0.0, tEnd);
        bufSrc.connect(bufGain); bufGain.connect(ctx.destination);
        bufSrc.start();
      }).catch((err) => console.warn("freeze fallback decode failed:", err));
      teardowns.push(() => {
        cancelled = true;
        if (bufSrc) { try { bufSrc.stop(); } catch (_) {} _disconnectAll(bufSrc, bufGain); }
      });
    }
  }
  else if (effect === "glitch") {
    // Random buffer slicing + reorder.  Same pattern as freeze: bypass
    // deck.gain so the chaotic stutter is audible.  Do NOT mute the
    // <audio> element — that silences the source feeding the worklet.
    if (_workletReady.glitch) {
      const node = new AudioWorkletNode(ctx, "glitch", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [2],
      });
      node.parameters.get("sliceMs").setValueAtTime(80, t0);
      node.parameters.get("density").setValueAtTime(0.85, t0);
      const passthrough = ctx.createGain();
      passthrough.gain.value = 1.0;
      const g = ctx.createGain();
      g.gain.setValueAtTime(_volume * 1.2, t0);
      g.gain.setValueAtTime(_volume * 1.2, t0 + fadeSec * 0.7);
      g.gain.linearRampToValueAtTime(0, tEnd);
      _routeThrough(outDeck, passthrough);
      passthrough.connect(node);
      node.connect(g); g.connect(ctx.destination);
      outDeck.gain.gain.setValueAtTime(0, t0 + 0.001);
      teardowns.push(() => _disconnectAll(node, g, passthrough));
    } else {
      // Non-secure-context fallback — slice the decoded buffer into
      // 80 ms chunks and schedule them in a random order via separate
      // BufferSources.  Each chunk has a 5 ms attack/release ramp so
      // the seams don't click.
      console.warn("glitch worklet unavailable; using BufferSource fallback");
      const path = outDeck.path;
      const currentT = outDeck.audio.currentTime;
      const sliceSec = 0.08;
      const totalSec = Math.max(fadeSec, 2.0);
      let cancelled = false;
      const sources = [];
      outDeck.gain.gain.setValueAtTime(0, t0 + 0.001);
      _decodeFor(path).then((buf) => {
        if (cancelled) return;
        const sr = buf.sampleRate;
        const sliceLen = Math.floor(sliceSec * sr);
        const winLen = Math.max(sliceLen * 6, Math.floor(0.5 * sr));
        const winStart = Math.max(0, Math.floor(currentT * sr) - winLen);
        const nSrcSlices = Math.max(1, Math.floor(winLen / sliceLen));
        const nSlots = Math.ceil(totalSec / sliceSec);
        const ramp = 0.005;
        for (let i = 0; i < nSlots; i++) {
          const idx = Math.floor(Math.random() * nSrcSlices);
          const sStart = winStart + idx * sliceLen;
          if (sStart + sliceLen > buf.length) continue;
          const slice = ctx.createBuffer(buf.numberOfChannels, sliceLen, sr);
          for (let ch = 0; ch < buf.numberOfChannels; ch++) {
            slice.getChannelData(ch).set(
              buf.getChannelData(ch).subarray(sStart, sStart + sliceLen),
            );
          }
          const src = ctx.createBufferSource(); src.buffer = slice;
          const g2 = ctx.createGain();
          const tStart = t0 + i * sliceSec;
          g2.gain.setValueAtTime(0, tStart);
          g2.gain.linearRampToValueAtTime(_volume * 1.2, tStart + ramp);
          g2.gain.setValueAtTime(_volume * 1.2, tStart + sliceSec - ramp);
          g2.gain.linearRampToValueAtTime(0, tStart + sliceSec);
          src.connect(g2); g2.connect(ctx.destination);
          src.start(tStart);
          src.stop(tStart + sliceSec + 0.01);
          sources.push({ src, g: g2 });
        }
      }).catch((err) => console.warn("glitch fallback decode failed:", err));
      teardowns.push(() => {
        cancelled = true;
        for (const s of sources) {
          try { s.src.stop(); } catch (_) {}
          _disconnectAll(s.src, s.g);
        }
      });
    }
  }
  // -------- scratch: rapid back-and-forth sweep over short slice --------
  else if (effect === "scratch") {
    const path = outDeck.path;
    const currentT = outDeck.audio.currentTime;
    // 1/4-note slice, scratched over a bar (4 passes ⇒ one bar of
    // forward / reverse / forward / reverse).  Aligns with the groove
    // instead of the legacy 250 ms fixed slice.
    const sliceSec = _BS.beatSec(t0);
    const totalSec = Math.max(fadeSec, 2.0);
    const nPasses = 4;
    outDeck.audio.muted = true;
    outDeck.gain.gain.cancelScheduledValues(t0);
    outDeck.gain.gain.setValueAtTime(0, t0);
    let cancelled = false;
    const sources = [];
    _decodeFor(path).then((buf) => {
      if (cancelled) return;
      const sr = buf.sampleRate;
      const sliceLen = Math.floor(sliceSec * sr);
      const startSamp = Math.max(0, Math.floor(currentT * sr) - sliceLen);
      const slice = ctx.createBuffer(buf.numberOfChannels, sliceLen, sr);
      for (let ch = 0; ch < buf.numberOfChannels; ch++) {
        slice.getChannelData(ch).set(
          buf.getChannelData(ch).subarray(startSamp, startSamp + sliceLen),
        );
      }
      const passLen = totalSec / nPasses;
      const tScratchStart = _BS.nextDownbeat(t0);
      for (let p = 0; p < nPasses; p++) {
        const reverse = (p % 2 === 1);
        const passBuf = ctx.createBuffer(slice.numberOfChannels, sliceLen, sr);
        for (let ch = 0; ch < slice.numberOfChannels; ch++) {
          const dst = passBuf.getChannelData(ch);
          const src = slice.getChannelData(ch);
          if (reverse) {
            for (let i = 0; i < sliceLen; i++) dst[i] = src[sliceLen - 1 - i];
          } else {
            dst.set(src);
          }
        }
        const src = ctx.createBufferSource(); src.buffer = passBuf;
        // Sine-shaped rate envelope — accelerate then decelerate per pass
        const t1 = tScratchStart + p * passLen;
        const t2 = t1 + passLen;
        src.playbackRate.setValueAtTime(0.4, t1);
        src.playbackRate.linearRampToValueAtTime(2.0, t1 + passLen * 0.5);
        src.playbackRate.linearRampToValueAtTime(0.4, t2);
        const g = ctx.createGain();
        g.gain.setValueAtTime(_volume * 0.95, t1);
        if (p === nPasses - 1) {
          g.gain.linearRampToValueAtTime(0, t2);
        }
        src.connect(g); g.connect(ctx.destination);
        src.start(t1);
        src.stop(t2 + 0.01);
        sources.push({ src, g });
      }
    }).catch(() => {});
    teardowns.push(() => {
      cancelled = true;
      outDeck.audio.muted = false;
      for (const s of sources) {
        try { s.src.stop(); } catch (_) {}
        _disconnectAll(s.src, s.g);
      }
    });
  }
  // -------- beat_repeat: capture short slice, retrigger N times --------
  else if (effect === "beat_repeat") {
    const path = outDeck.path;
    const currentT = outDeck.audio.currentTime;
    // Tempo-synced 1/8-note slice retriggered every 1/8 note.  Slice
    // size + stride both come from the blended BPM so the repeats land
    // on the grid; first hit lands on the next downbeat.
    const sliceSec = _BS.beatSec(t0) / 2;
    const totalSec = Math.max(fadeSec, 3.0);
    const nRepeats = Math.max(4, Math.floor(totalSec / sliceSec));
    outDeck.audio.muted = true;
    outDeck.gain.gain.cancelScheduledValues(t0);
    outDeck.gain.gain.setValueAtTime(0, t0);
    let cancelled = false;
    const sources = [];
    _decodeFor(path).then((buf) => {
      if (cancelled) return;
      const sr = buf.sampleRate;
      const sliceLen = Math.floor(sliceSec * sr);
      const startSamp = Math.max(0, Math.floor(currentT * sr) - sliceLen);
      const slice = ctx.createBuffer(buf.numberOfChannels, sliceLen, sr);
      for (let ch = 0; ch < buf.numberOfChannels; ch++) {
        slice.getChannelData(ch).set(
          buf.getChannelData(ch).subarray(startSamp, startSamp + sliceLen),
        );
      }
      const stride = sliceSec;
      const tStart = _BS.nextDownbeat(t0);
      for (let i = 0; i < nRepeats; i++) {
        const t1 = tStart + i * stride;
        if (t1 >= tEnd) break;
        const src = ctx.createBufferSource(); src.buffer = slice;
        const g = ctx.createGain();
        // Retrigger envelope — sharp attack + decay so each hit punches
        g.gain.setValueAtTime(_volume, t1);
        g.gain.linearRampToValueAtTime(0, t1 + sliceSec);
        src.connect(g); g.connect(ctx.destination);
        src.start(t1);
        src.stop(t1 + sliceSec + 0.01);
        sources.push({ src, g });
      }
    }).catch(() => {});
    teardowns.push(() => {
      cancelled = true;
      outDeck.audio.muted = false;
      for (const s of sources) {
        try { s.src.stop(); } catch (_) {}
        _disconnectAll(s.src, s.g);
      }
    });
  }
  // -------- sidechain_pump: rhythmic 4-on-the-floor amplitude duck --------
  else if (effect === "sidechain_pump") {
    // Periodic 4-on-floor gain duck synced to the OUTGOING track's tempo
    // and aligned to its next downbeat.  Each duck lands on a beat and
    // recovers across the beat; period = beatSec at the blended BPM.
    const pump = ctx.createGain();
    pump.gain.value = 1.0;
    const depth = 0.7;
    const tStart = _BS.nextDownbeat(t0);
    let t = tStart;
    while (t < tEnd) {
      const period = _BS.beatSec(t);
      pump.gain.setValueAtTime(1 - depth, t);
      pump.gain.exponentialRampToValueAtTime(1.0, t + period * 0.95);
      t += period;
    }
    _routeThrough(outDeck, pump);
    pump.connect(outDeck.gain);
    teardowns.push(() => { try { pump.disconnect(); } catch (_) {} });
  }
  // -------- reverse_reverb: swelling reverb INTO the cut --------
  else if (effect === "reverse_reverb") {
    // Build a reverse-decay IR (envelope rises 0 → 1 over duration),
    // convolve.  Wet path bypasses deck.gain so the swell crescendos
    // all the way to the cut.  IR is shared across crossfades via
    // _cachedIR — re-running the RNG fill every transition was wasteful.
    // True reverse-reverb requires playing reversed audio through a
    // forward reverb, then reversing the result.  Approximated here
    // with: dense forward-decay IR + rising wet send.  A pre-emphasis
    // band-pass on the wet path concentrates the swell in the
    // 200-2000 Hz range so it cuts through over the dry signal —
    // without this the wet was perceptually buried even at high gain.
    const conv = ctx.createConvolver();
    conv.buffer = _makeReverseReverbIR(2.5, 2.5);
    const bp = ctx.createBiquadFilter();
    bp.type = "bandpass"; bp.frequency.value = 700; bp.Q.value = 0.7;
    const wet = ctx.createGain();
    wet.gain.setValueAtTime(0.001, t0);
    wet.gain.exponentialRampToValueAtTime(Math.max(0.001, _volume * 4.0), tEnd - 0.05);
    wet.gain.linearRampToValueAtTime(0.0, tEnd);
    outDeck.source.connect(conv); conv.connect(bp); bp.connect(wet);
    wet.connect(ctx.destination);
    teardowns.push(() => _disconnectAll(conv, bp, wet));
  }
  // -------- air_horn: synth dub-siren riser layered with the music --------
  else if (effect === "air_horn") {
    const osc = ctx.createOscillator();
    osc.type = "square";
    // Pitch sweep tuned to song key when key-sync enabled.  Anchor at
    // the outgoing root one octave below middle (e.g. A2≈110), sweep
    // up two octaves toward the incoming root.  Falls back to the
    // legacy 220→880 Hz sweep when neither key is known.
    const outRoot = _BS.rootHzAt(t0);
    const inRoot = _BS.rootHzAt(tEnd);
    const startHz = outRoot ? outRoot * 0.5 : 220;
    const endHz = inRoot ? inRoot * 2.0 : 880;
    osc.frequency.setValueAtTime(startHz, t0);
    osc.frequency.exponentialRampToValueAtTime(Math.max(50, endHz), tEnd - 0.1);
    // Soft filter so it isn't pure square harshness
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass"; lp.frequency.value = 3500; lp.Q.value = 1.5;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(_volume * 0.4, t0 + 0.3);
    g.gain.setValueAtTime(_volume * 0.4, tEnd - 0.1);
    g.gain.linearRampToValueAtTime(0, tEnd);
    osc.connect(lp); lp.connect(g); g.connect(ctx.destination);
    osc.start(t0);
    osc.stop(tEnd + 0.05);
    teardowns.push(() => {
      try { osc.stop(); } catch (_) {}
      _disconnectAll(osc, lp, g);
    });
  }
  else if (effect === "pitch_swell") {
    // Real pitch swell — disable preservesPitch so playbackRate
    // upward ramp becomes pitch up, not just tempo up.  Browsers
    // default preservesPitch=true which is why the old swell sounded
    // like a tempo speed-up.
    const audio = outDeck.audio;
    const prevPreserve = audio.preservesPitch !== false;
    try { audio.preservesPitch = false; } catch (_) {}
    const startMs = performance.now();
    const durMs = fadeSec * 1000;
    const iv = setInterval(() => {
      const t = (performance.now() - startMs) / durMs;
      if (t >= 1) { clearInterval(iv); return; }
      try { audio.playbackRate = 1.0 + t; } catch (_) {}
    }, 20);
    teardowns.push(() => {
      clearInterval(iv);
      try { audio.playbackRate = 1.0; } catch (_) {}
      try { audio.preservesPitch = prevPreserve; } catch (_) {}
    });
  }
  else if (effect === "pitch_fall") {
    // Mirror of pitch_swell — pitch ramps DOWN (1.0 → 0.3) into the
    // cut, like a slowed tape but without the brake-to-zero of
    // tape_stop.  Floors at 0.3 because some browsers get glitchy
    // below ~0.25 playbackRate.
    const audio = outDeck.audio;
    const prevPreserve = audio.preservesPitch !== false;
    try { audio.preservesPitch = false; } catch (_) {}
    const startMs = performance.now();
    const durMs = fadeSec * 1000;
    const iv = setInterval(() => {
      const t = (performance.now() - startMs) / durMs;
      if (t >= 1) { clearInterval(iv); return; }
      try { audio.playbackRate = Math.max(0.3, 1.0 - 0.7 * t); } catch (_) {}
    }, 20);
    teardowns.push(() => {
      clearInterval(iv);
      try { audio.playbackRate = 1.0; } catch (_) {}
      try { audio.preservesPitch = prevPreserve; } catch (_) {}
    });
  }
  // -------- vinyl_rewind: slow musical reverse + pitch drop --------
  else if (effect === "vinyl_rewind") {
    // Distinct from backspin (fast 2.0×→0.05× friction).  Vinyl-rewind is
    // a smooth 1.0×→0.5× reverse — sounds like rewinding a Walkman tape
    // to find the previous track, not a turntablist trick.
    _doSpin(ctx, outDeck, t0, fadeSec, /*reverse=*/true, teardowns, /*slow=*/true);
  }
  // -------- transformer: rapid tempo-cut DJ fader pattern --------
  else if (effect === "transformer") {
    // Syncopated [open, cut, open, cut, cut, open, cut, open] pattern
    // at 1/16-note resolution.  cps = bpm/60 * 4.  Aligned to next
    // downbeat so the syncopation lands on-grid instead of arbitrary.
    const wrapper = ctx.createGain();
    wrapper.gain.setValueAtTime(1, t0);
    const pattern = [1, 0, 1, 0, 0, 1, 0, 1];
    const cps = (_BS.outBpm > 0 ? _BS.outBpm : 120) / 60 * 4;
    const cycle = 1 / cps;
    let i = 0;
    for (let t = _BS.nextDownbeat(t0); t < tEnd; t += cycle) {
      const open = pattern[i % pattern.length];
      // Tiny ramps avoid clicks on the gate edges.
      wrapper.gain.setValueAtTime(open ? 1 : 0, t);
      wrapper.gain.linearRampToValueAtTime(open ? 1 : 0, t + Math.min(0.005, cycle * 0.1));
      i++;
    }
    wrapper.gain.setValueAtTime(1, tEnd);
    _routeThrough(outDeck, wrapper);
    wrapper.connect(outDeck.gain);
    teardowns.push(() => {
      try { wrapper.disconnect(); } catch (_) {}
      _restoreDirect(outDeck);
    });
  }
  // -------- dub_siren: smooth sine siren with vibrato --------
  else if (effect === "dub_siren") {
    // Distinct from air_horn (square-ish 220→880 Hz fast horn).  Dub siren
    // is a sine 440 → 1760 Hz with 5 Hz vibrato and slow fade-in — sits
    // BEHIND the music rather than crashing on top.
    const osc = ctx.createOscillator();
    osc.type = "sine";
    // Tune sweep to song key when known: outgoing root -> incoming root
    // two octaves up (perceived siren rise).  Legacy 440->1760 Hz
    // (A4 -> A6) when neither side has a key.
    const outRoot = _BS.rootHzAt(t0);
    const inRoot = _BS.rootHzAt(tEnd);
    const startHz = outRoot || 440;
    const endHz = (inRoot || 440) * 4.0;
    osc.frequency.setValueAtTime(startHz, t0);
    osc.frequency.exponentialRampToValueAtTime(Math.max(80, endHz), tEnd);
    // Vibrato: second oscillator modulates frequency
    const vibLfo = ctx.createOscillator();
    vibLfo.type = "sine";
    vibLfo.frequency.value = 5;
    const vibGain = ctx.createGain();
    vibGain.gain.value = 8;  // ~15 cents at 1 kHz
    vibLfo.connect(vibGain); vibGain.connect(osc.frequency);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(_volume * 0.25, t0 + fadeSec * 0.5);
    g.gain.setValueAtTime(_volume * 0.25, tEnd - 0.2);
    g.gain.linearRampToValueAtTime(0, tEnd);
    osc.connect(g); g.connect(ctx.destination);
    osc.start(t0); vibLfo.start(t0);
    osc.stop(tEnd + 0.05); vibLfo.stop(tEnd + 0.05);
    teardowns.push(() => {
      try { osc.stop(); vibLfo.stop(); } catch (_) {}
      _disconnectAll(osc, vibLfo, vibGain, g);
    });
  }
  // -------- stutter_build: accelerating gate freq quarter -> 32nd notes --------
  else if (effect === "stutter_build") {
    // Tempo-synced build: starts at 1/4-note gate, accelerates to
    // 1/32-note gate at the cut.  At 120 BPM that's 2 Hz -> 16 Hz; at
    // 140 BPM 2.33 -> 18.7 Hz.  Aligned to next downbeat.
    const wrapper = ctx.createGain();
    wrapper.gain.setValueAtTime(1, t0);
    const beatHz = (_BS.outBpm > 0 ? _BS.outBpm : 120) / 60;
    const startRate = beatHz;            // 1/4 notes
    const endRate = beatHz * 8;          // 1/32 notes
    let t = _BS.nextDownbeat(t0);
    let elapsed = 0;
    while (t < tEnd) {
      const frac = elapsed / Math.max(0.001, fadeSec);
      const rate = startRate + (endRate - startRate) * Math.min(1, frac);
      const cycle = 1 / rate;
      // 50% duty: half open, half closed.  Tiny ramp to avoid clicks.
      wrapper.gain.setValueAtTime(1, t);
      wrapper.gain.setValueAtTime(0, t + cycle * 0.5);
      t += cycle;
      elapsed += cycle;
    }
    wrapper.gain.setValueAtTime(1, tEnd);
    _routeThrough(outDeck, wrapper);
    wrapper.connect(outDeck.gain);
    teardowns.push(() => {
      try { wrapper.disconnect(); } catch (_) {}
      _restoreDirect(outDeck);
    });
  }
  // -------- phaser: 4-stage allpass cascade (sweepy notch sound) --------
  else if (effect === "phaser") {
    // Web Audio doesn't ship a phaser primitive; cascade four BiquadFilter
    // allpass stages with a single LFO modulating each frequency.  4 stages
    // gives the classic "sweepy" 4-notch sound (vs 2-stage = subtle, 6-stage
    // = guitar-pedal-warble).  Different from flanger (DelayNode + LFO =
    // metallic comb teeth).
    const stages = [];
    for (let i = 0; i < 4; i++) {
      const ap = ctx.createBiquadFilter();
      ap.type = "allpass";
      ap.frequency.value = 400 + i * 200;  // staggered base frequencies
      ap.Q.value = 0.7;
      stages.push(ap);
    }
    const lfo = ctx.createOscillator();
    lfo.type = "sine";
    lfo.frequency.value = 0.5;
    const lfoGain = ctx.createGain();
    lfoGain.gain.value = 600;  // ±600 Hz sweep
    lfo.connect(lfoGain);
    stages.forEach(ap => lfoGain.connect(ap.frequency));
    // Chain stages: out → ap1 → ap2 → ap3 → ap4 → wet
    const wet = ctx.createGain();
    wet.gain.value = 0.5;
    const dry = ctx.createGain();
    dry.gain.value = 0.5;
    _routeThrough(outDeck, dry);
    dry.connect(outDeck.gain);
    // Parallel wet path
    const wetIn = ctx.createGain();
    outDeck.source.connect(wetIn);
    wetIn.connect(stages[0]);
    for (let i = 0; i < stages.length - 1; i++) stages[i].connect(stages[i + 1]);
    stages[stages.length - 1].connect(wet);
    wet.connect(outDeck.gain);
    lfo.start(t0);
    lfo.stop(tEnd + 0.05);
    teardowns.push(() => {
      try { lfo.stop(); } catch (_) {}
      _disconnectAll(...stages, lfo, lfoGain, wet, dry, wetIn);
      _restoreDirect(outDeck);
    });
  }
  // -------- ring_modulator: signal × sine carrier (clangy bell tone) --------
  else if (effect === "ring_modulator") {
    // True ring-mod = signal × sine.  Web Audio has no multiplier node, but
    // a GainNode whose .gain is driven by a LFO source achieves the same
    // result: gain oscillates between -1 and +1, multiplying the audio by
    // the LFO sample-by-sample.
    const wet = ctx.createGain();
    wet.gain.value = 0;  // baseline 0; LFO modulates around 0
    const carrier = ctx.createOscillator();
    carrier.type = "sine";
    // Carrier tuned to the song's root note (one octave down from C4
    // reference) so the resulting clangy sidebands sit IN-key with the
    // track instead of producing the legacy 173 Hz F3-ish dissonance.
    const carrierRoot = _BS.rootHzAt(t0);
    carrier.frequency.value = carrierRoot ? carrierRoot * 0.5 : 173;
    const carrierGain = ctx.createGain();
    carrierGain.gain.value = 1.0;
    carrier.connect(carrierGain);
    carrierGain.connect(wet.gain);
    // Mix 50/50 dry + wet so the original beat is still audible
    const dry = ctx.createGain();
    dry.gain.value = 0.5;
    const wetMix = ctx.createGain();
    wetMix.gain.value = 0.5;
    _routeThrough(outDeck, dry);
    dry.connect(outDeck.gain);
    outDeck.source.connect(wet);
    wet.connect(wetMix);
    wetMix.connect(outDeck.gain);
    carrier.start(t0);
    carrier.stop(tEnd + 0.05);
    teardowns.push(() => {
      try { carrier.stop(); } catch (_) {}
      _disconnectAll(carrier, carrierGain, wet, wetMix, dry);
      _restoreDirect(outDeck);
    });
  }
  // -------- dub_delay: long quarter-note lowpass-feedback delay --------
  else if (effect === "dub_delay") {
    // Distinct from echo_out (1/8 note feedback, full-spectrum).
    // Dub delay = full-beat (1/4 note) delay with lowpass on the
    // feedback path so each repeat darkens.  Quarter note keeps the
    // groove audible inside the feedback even at slow tempos.
    const delayNode = ctx.createDelay(2.0);
    const quarter = _BS.beatSec(t0);
    delayNode.delayTime.value = Math.max(0.2, Math.min(1.8, quarter));
    const fb = ctx.createGain();
    fb.gain.value = 0.55;
    const fbLp = ctx.createBiquadFilter();
    fbLp.type = "lowpass";
    fbLp.frequency.value = 1500;
    fbLp.Q.value = 0.7;
    const wet = ctx.createGain();
    wet.gain.value = 0.55;
    const dry = ctx.createGain();
    dry.gain.value = 0.6;
    _routeThrough(outDeck, dry);
    dry.connect(outDeck.gain);
    // Wet path: source → delay → fbLp → fb → delay (loop) AND → wet → out
    outDeck.source.connect(delayNode);
    delayNode.connect(fbLp);
    fbLp.connect(fb);
    fb.connect(delayNode);  // feedback loop
    fbLp.connect(wet);
    wet.connect(outDeck.gain);
    teardowns.push(() => {
      _disconnectAll(delayNode, fbLp, fb, wet, dry);
      _restoreDirect(outDeck);
    });
  }
  // -------- halftime: tempo to 50 % with pitch preserved --------
  else if (effect === "halftime") {
    // Distinct from pitch_fall (pitch + tempo down) and tape_stop (slow
    // to zero).  Halftime keeps musical pitch — kicks half as often,
    // melodies recognisable.  Classic trap pre-drop technique.  Browser
    // implementation: HTMLMediaElement.preservesPitch=true (default) +
    // playbackRate=0.5.  Cleanest way without granular DSP.
    const audio = outDeck.audio;
    const prevPreserve = audio.preservesPitch !== false;
    const prevRate = audio.playbackRate;
    try { audio.preservesPitch = true; } catch (_) {}
    // Smooth ramp from 1.0 → 0.5 over half the fade so the halftime drop
    // feels intentional, not a glitch
    const startMs = performance.now();
    const durMs = (fadeSec * 0.5) * 1000;
    const iv = setInterval(() => {
      const t = (performance.now() - startMs) / durMs;
      if (t >= 1) {
        clearInterval(iv);
        try { audio.playbackRate = 0.5; } catch (_) {}
        return;
      }
      try { audio.playbackRate = 1.0 - 0.5 * t; } catch (_) {}
    }, 20);
    teardowns.push(() => {
      clearInterval(iv);
      try { audio.playbackRate = prevRate; } catch (_) {}
      try { audio.preservesPitch = prevPreserve; } catch (_) {}
    });
  }
  // -------- wow_flutter: pitch wobble + amplitude tremolo --------
  else if (effect === "wow_flutter") {
    // vinyl_wow modulates pitch only.  wow_flutter adds amplitude tremolo
    // for a worn-cassette feel: slow 1.5 Hz wow + fast 8 Hz flutter trem.
    const audio = outDeck.audio;
    const prevPreserve = audio.preservesPitch !== false;
    try { audio.preservesPitch = false; } catch (_) {}
    // Amplitude tremolo via gain node in the audio path.
    const trem = ctx.createGain();
    trem.gain.setValueAtTime(1, t0);
    const tremLfo = ctx.createOscillator();
    tremLfo.type = "sine";
    tremLfo.frequency.value = 8;
    const tremDepth = ctx.createGain();
    tremDepth.gain.value = 0.15;  // ±15 % amplitude
    tremLfo.connect(tremDepth); tremDepth.connect(trem.gain);
    _routeThrough(outDeck, trem); trem.connect(outDeck.gain);
    tremLfo.start(t0); tremLfo.stop(tEnd + 0.05);
    // Pitch wobble via setInterval (matches vinyl_wow pattern).
    const startMs = performance.now();
    const durMs = fadeSec * 1000;
    const iv = setInterval(() => {
      const t = (performance.now() - startMs) / durMs;
      if (t >= 1) { clearInterval(iv); try { audio.playbackRate = 1.0; } catch (_) {} return; }
      const phase = Math.sin(2 * Math.PI * 1.5 * t * fadeSec);
      try { audio.playbackRate = 1.0 + 0.04 * phase; } catch (_) {}
    }, 16);
    teardowns.push(() => {
      clearInterval(iv);
      try { audio.playbackRate = 1.0; } catch (_) {}
      try { audio.preservesPitch = prevPreserve; } catch (_) {}
      try { tremLfo.stop(); } catch (_) {}
      _disconnectAll(trem, tremLfo, tremDepth);
      _restoreDirect(outDeck);
    });
  }

  return tearAll;
}

// `serverLed` = the server already advanced (shuffle, queued "Now",
// media-session next, CLI-side advance arriving via WS).  Browser is
// only playing catch-up to render the visual / audible crossfade; it
// MUST NOT POST /api/advance again or the server steps forward a
// second time and a fresh state push triggers another catch-up
// crossfade -- cascading "shuffles every few seconds" bug.
function startCrossfade(nextPath, fadeSec, serverLed = false) {
  if (!_ctx || crossfading) return;
  if (!nextPath) return;
  crossfading = true;
  _dbg("crossfade ->", nextPath, "| fade=", fadeSec.toFixed(2), "s",
    "| serverLed=", serverLed);

  const standby = deckStandby();
  setSrcOnDeck(standby, nextPath);
  standby.gain.gain.setValueAtTime(0, _ctx.currentTime);
  // Mixxx-style intro alignment / leading-silence skip — when the
  // server has reported intro_end_s for the next track and the user has
  // chosen full_intro_outro or fixed_skip_silence, seek the standby
  // deck to that marker so the dry-quiet intro doesn't waste fade
  // headroom.  outro_fade + fixed leave the deck at 0 (legacy behaviour).
  const skipIntro = (_transitionMode === "full_intro_outro"
                     || _transitionMode === "fixed_skip_silence")
                    && typeof _nextTrackIntroEndCache === "number"
                    && _nextTrackIntroEndCache > 0;
  if (skipIntro) {
    // Wait for the loaded-metadata event so currentTime can be set.  Clamp
    // the seek target against the actual duration once metadata is known
    // — server-side intro_end_s can outrun the real track length when the
    // FAISS index has stale or wrong-length entries, and an out-of-range
    // assignment seeks to the end + decode tail (silent crossfade).
    const seekTarget = _nextTrackIntroEndCache;
    const seekIfReady = () => {
      try {
        const dur = standby.audio.duration;
        const safe = isFinite(dur) && dur > 1.0
          ? Math.min(seekTarget, Math.max(0, dur - 1.0))
          : seekTarget;
        standby.audio.currentTime = safe;
      } catch (_) {}
    };
    if (standby.audio.readyState >= 1) {
      seekIfReady();
    } else {
      standby.audio.addEventListener("loadedmetadata", seekIfReady, { once: true });
    }
  }
  playOnDeck(standby);

  const active = deckActive();
  const t0 = _ctx.currentTime;

  // Resolve + apply the chosen transition effect over the fade window.
  const fxName = _resolveTransition(_lastTransitionFx);
  // Resolve effect-preferred duration UP FRONT so the gain ramp, the
  // effect scheduling, and the cleanup setTimeout all use the SAME
  // timeline.  Earlier code resolved this inside applyTransitionFx
  // which left the gain ramp ending before / after the effect tail and
  // caused audible cuts (effect-shorter-than-fade) or trailing silence
  // (effect-longer-than-fade).
  const effectDur = _effectDurationFor(fxName, fadeSec, _currentOutroLenCache);
  // Refresh the beat-sync cache up front so applyTransitionFx can read
  // _BS.beatSec / barSec / nextDownbeat / rootHzAt while scheduling.
  _BS.refresh(active, t0, effectDur);
  console.debug("autodj transition:", fxName, "duration:", effectDur.toFixed(2),
    "s | bpm:", _BS.outBpm.toFixed(1), "->", _BS.inBpm.toFixed(1),
    "| keyHz:", _BS.outKeyHz, "->", _BS.inKeyHz);

  // Schedule the baseline crossfade gain ramps FIRST so that any
  // subsequent overrides issued by `applyTransitionFx` (e.g.
  // deck.gain.setValueAtTime(0, t0+0.001) for freeze / glitch /
  // bitcrusher) aren't wiped out by a later cancelScheduledValues(t0).
  active.gain.gain.cancelScheduledValues(t0);
  active.gain.gain.setValueAtTime(active.gain.gain.value, t0);
  active.gain.gain.linearRampToValueAtTime(0, t0 + effectDur);

  standby.gain.gain.cancelScheduledValues(t0);
  standby.gain.gain.setValueAtTime(0, t0);
  standby.gain.gain.linearRampToValueAtTime(_volume, t0 + effectDur);

  // Pass the resolved duration so applyTransitionFx no longer recomputes
  // it -- prevents the duration drift that caused cuts.
  const teardownFx = applyTransitionFx(fxName, effectDur, active, standby);

  suppressAdvance = true;
  if (!serverLed) {
    fetch("/api/advance", { method: "POST" }).catch(() => {});
  }

  setTimeout(() => {
    teardownFx();
    activeIdx ^= 1;
    crossfading = false;
    suppressAdvance = false;
    try { active.audio.pause(); } catch (_) {}
    active.audio.removeAttribute("src");
    active.path = null;
    active.audio.load();
  }, effectDur * 1000 + 100);
}

// "ended" on either deck = unconditional advance to next track when not
// already mid-crossfade (no next_track queued, or fade window missed).
for (const d of decks) {
  d.audio.addEventListener("ended", () => {
    if (suppressAdvance || crossfading) return;
    fetch("/api/advance", { method: "POST" }).catch(() => {});
  });
  d.audio.addEventListener("error", () => {
    const e = d.audio.error;
    let msg = "Playback error.";
    if (e) {
      const codes = { 1: "aborted", 2: "network", 3: "decode", 4: "src not supported" };
      msg = `Playback error: ${codes[e.code] || "unknown"}.`;
    }
    // Surface filename so user knows which track choked.
    const path = d.path || "";
    const name = path.split(/[\\/]/).pop();
    if (name) msg += ` (${name})`;
    // Aborted (code 1) is usually triggered by us tearing down a deck, so
    // ignore those entirely — they don't represent a real playback failure.
    if (e && e.code === 1) {
      npAnnounce.textContent = msg;
      return;
    }
    const isActive = d === deckActive();
    if (isActive) {
      // Active deck failed mid-playback — auto-advance.
      msg += " — auto-skipping.";
      fetch("/api/advance", { method: "POST" }).catch(() => {});
    } else {
      // Standby deck (the prefetched next track) failed to load.  Don't
      // advance the live track — just ask the server for a different next
      // track and let the live one keep playing.  Blacklist the bad path
      // so similarity won't immediately re-pick it.
      msg += " — picking a different next track.";
      const body = path ? JSON.stringify({ blacklist: path }) : "{}";
      fetch("/api/repick-next", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      }).then(r => r.ok ? r.json() : null).then(state => {
        if (state) applyState(state);
      }).catch(() => {});
      // Clear cached prefetch path so timeupdate doesn't try to crossfade
      // into the broken file again before the next WS state push.
      _nextTrackPathCache = null;
      // Clear the failing standby deck's source so it stops retrying.
      try {
        d.audio.removeAttribute("src");
        d.audio.load();
      } catch (_) {}
      d.path = null;
    }
    npAnnounce.textContent = msg;
  });
  // Watch active deck's currentTime for crossfade trigger.
  d.audio.addEventListener("timeupdate", () => {
    if (d !== deckActive()) return;
    if (crossfading || !playbackEnabled) return;
    const dur = d.audio.duration;
    if (!isFinite(dur) || dur <= 0) return;
    const remaining = dur - d.audio.currentTime;
    const baseFade = _crossfadeSecondsCache;
    const fadeSec = _resolveFadeSec(
      _transitionMode, baseFade,
      _currentOutroLenCache, _nextTrackIntroEndCache,
    );
    // For outro_fade + full_intro_outro, the fade should begin AT the
    // outgoing outro_start (when known) rather than just "fadeSec from
    // the end".  Triggers as soon as currentTime crosses outro_start.
    // Outro_start_s > duration would never trigger and the fade-by-
    // remaining-time fallback below catches it; reject obviously broken
    // markers (negative / past-end) so we don't fall through to bad math.
    const outroValid = typeof _currentOutroStartCache === "number"
                       && _currentOutroStartCache > 0
                       && _currentOutroStartCache < dur;
    const useMarker = (_transitionMode === "outro_fade"
                       || _transitionMode === "full_intro_outro")
                      && outroValid
                      && _nextTrackPathCache;
    if (useMarker && d.audio.currentTime >= _currentOutroStartCache) {
      startCrossfade(_nextTrackPathCache, fadeSec);
      return;
    }
    if (remaining > 0 && remaining < fadeSec && _nextTrackPathCache) {
      startCrossfade(_nextTrackPathCache, fadeSec);
      return;
    }
    // Silence detector — fire the crossfade EARLY when the active deck
    // has gone quiet at the very end of a fade-out tail.  Eliminates the
    // long dead air at the end of some tracks.
    //
    // Tuned conservative (95 % of duration + 2 s continuous silence) to
    // avoid cutting tracks short on intentional mid-song breakdowns or
    // sparse passages.  Earlier 50 % + 0.6 s caused atmospheric / minimal
    // tracks with quiet middles to crossfade prematurely.
    if (_silenceTriggerEnabled
        && d.analyser && _nextTrackPathCache
        && d.audio.currentTime > dur * 0.95) {
      const buf = new Float32Array(d.analyser.fftSize);
      d.analyser.getFloatTimeDomainData(buf);
      let sumSq = 0;
      for (let i = 0; i < buf.length; i++) sumSq += buf[i] * buf[i];
      const rms = Math.sqrt(sumSq / buf.length);
      // RMS threshold ≈ −60 dBFS — anything quieter is functionally silence.
      if (rms < 0.001) {
        d._silenceMs += 250;   // timeupdate fires ~4 Hz
        if (d._silenceMs >= 2000) {
          d._silenceMs = 0;
          startCrossfade(_nextTrackPathCache, fadeSec);
        }
      } else {
        d._silenceMs = 0;
      }
    }
  });
}

// Latest server hints (cached so timeupdate doesn't have to peek into state)
let _crossfadeSecondsCache = 3.0;
let _currentOutroLenCache = null;
let _currentOutroStartCache = null;   // active deck's outro_start_s
let _nextTrackIntroEndCache = null;   // incoming track's intro_end_s
let _nextTrackPathCache = null;
let _transitionMode = "full_intro_outro";
let _prefetchEnabled = true;
let _silenceTriggerEnabled = true;

// --- Beat- + key-sync transition FX caches.  Populated from
// applyBrowserPlaybackState whenever a state push lands; consumed by
// _BS.refresh() at the start of every crossfade so per-effect timing
// can snap to downbeats and oscillator-FX can tune to root notes. ---
let _beatSyncEnabled = true;
let _keySyncEnabled = true;
let _beatmatchOnSkip = false;
let _outBpmCache = 0;
let _inBpmCache = 0;
let _outDownbeatsCache = [];
let _inDownbeatsCache = [];
let _outKeyHzCache = null;
let _inKeyHzCache = null;

// One-shot guard so the small-library repeat warning only logs once
// per session even though /api/state pushes every second.
let _libraryWarned = false;

// Mixxx-style fade-length picker.  Mirrors AutoDJProcessor's
// TransitionMode enum -- see CHANGELOG entry for 0.12.3.
//
// - full_intro_outro: align outgoing outro start with incoming intro
//   start; fade length = min(outroLen, nextIntroEnd) clamped 1.0-12.0 s.
// - outro_fade: fade length = outroLen (clamped); ignore nextIntroEnd.
// - fixed_skip_silence: baseFade as-is; the leading-silence skip is
//   applied to the standby deck in startCrossfade().
// - fixed (and fallback): legacy fixed-length crossfade.
function _resolveFadeSec(mode, baseFade, outroLen, nextIntroEnd) {
  const clamp = (v) => Math.max(1.0, Math.min(12.0, v));
  if (mode === "full_intro_outro"
      && typeof outroLen === "number" && outroLen > 0
      && typeof nextIntroEnd === "number" && nextIntroEnd > 0) {
    return clamp(Math.min(outroLen, nextIntroEnd));
  }
  if (mode === "outro_fade" && typeof outroLen === "number" && outroLen > 0) {
    return clamp(outroLen);
  }
  return baseFade;
}

// First-click unlock — used by the unified Play button (btnPause).
async function unlockAndPlay() {
  ensureAudioGraph();
  if (_ctx && _ctx.state === "suspended") await _ctx.resume();
  // Start a silent play() on the active deck to satisfy iOS gesture rule.
  playOnDeck(deckActive());

  // Pull current state and load the active deck with the current track.
  let state;
  try {
    const r = await fetch("/api/status");
    if (!r.ok) throw new Error(`/api/status returned ${r.status}`);
    state = await r.json();
  } catch (err) {
    npAnnounce.textContent = "Cannot reach server: " + (err.message || err);
    throw err;
  }
  const path = state.current_track ? state.current_track.path : null;
  if (!path) {
    npAnnounce.textContent = "No current track on server.";
    throw new Error("no current track");
  }
  setSrcOnDeck(deckActive(), path);
  await playOnDeck(deckActive());
  playbackEnabled = true;
  setVolume(_volume);   // apply current slider value
  applyState(state);    // refresh UI from /api/status
}

function applyBrowserPlaybackState(s) {
  // When the server has its own audio output, the browser stays out of
  // the way (no decks fired up, no crossfade, no advance posts).
  if (!s.browser_playback) return;

  _crossfadeSecondsCache = (s.settings && s.settings.playback &&
    s.settings.playback.crossfade_seconds) || 3.0;
  _nextTrackPathCache = s.next_track ? s.next_track.path : null;
  _lastTransitionFx = (s.settings && s.settings.transition) || "none";
  _transitionMode = (s.settings && s.settings.playback &&
    s.settings.playback.transition_mode) || "full_intro_outro";
  // Outgoing track's outro length drives the per-effect duration table
  // in `applyTransitionFx`.  Null when the track hasn't been DJ-meta
  // analysed yet — falls back to the static minimums.
  _currentOutroLenCache = (s.current_track && typeof s.current_track.outro_len === "number")
    ? s.current_track.outro_len : null;
  _currentOutroStartCache = (s.current_track
      && typeof s.current_track.outro_start_s === "number")
    ? s.current_track.outro_start_s : null;
  _nextTrackIntroEndCache = (s.next_track
      && typeof s.next_track.intro_end_s === "number")
    ? s.next_track.intro_end_s : null;
  // Beat- and key-sync metadata for transition FX scheduling.  Server
  // emits per-track downbeat windows + key_hz; we cache them here so
  // _BS.refresh() (called in startCrossfade) has fresh data without
  // having to re-walk the WS payload.
  _beatSyncEnabled = !(s.settings && s.settings.playback &&
    s.settings.playback.beat_sync_fx === false);
  _keySyncEnabled = !(s.settings && s.settings.playback &&
    s.settings.playback.key_sync_fx === false);
  _beatmatchOnSkip = !!(s.settings && s.settings.playback &&
    s.settings.playback.beatmatch_on_skip === true);
  _outBpmCache = (s.current_track && typeof s.current_track.bpm === "number")
    ? s.current_track.bpm : 0;
  _inBpmCache = (s.next_track && typeof s.next_track.bpm === "number")
    ? s.next_track.bpm : 0;
  _outDownbeatsCache = (s.current_track && Array.isArray(s.current_track.downbeats_outro))
    ? s.current_track.downbeats_outro : [];
  _inDownbeatsCache = (s.next_track && Array.isArray(s.next_track.downbeats_intro))
    ? s.next_track.downbeats_intro : [];
  _outKeyHzCache = (s.current_track && typeof s.current_track.key_hz === "number")
    ? s.current_track.key_hz : null;
  _inKeyHzCache = (s.next_track && typeof s.next_track.key_hz === "number")
    ? s.next_track.key_hz : null;
  // Honour user-controlled gapless flags from config.toml / web settings.
  _prefetchEnabled = !(s.settings && s.settings.playback &&
    s.settings.playback.prefetch_next_track === false);
  _silenceTriggerEnabled = !(s.settings && s.settings.playback &&
    s.settings.playback.silence_trigger_crossfade === false);

  // If playback is enabled, make sure the active deck is playing the
  // current track (no-op if already loaded).
  if (playbackEnabled) {
    const active = deckActive();
    const path = s.current_track ? s.current_track.path : null;
    if (path && active.path !== path && !crossfading) {
      // Four cases:
      //   1. Paused — keep pause frozen.  Hard-cut to new track at
      //      currentTime=0 with the deck still paused so the user
      //      stays in control of when audio resumes (Shuffle while
      //      paused must NOT auto-resume playback).
      //   2. Initial load — active.path is null, just set + play.
      //   3. Mid-playback w/ AudioContext — server changed current_track
      //      unexpectedly (Shuffle button, media-session next, Up Next
      //      "Now", server-side CLI advance).  Without a crossfade the
      //      live track would HARD-CUT to the new one — jarring.  Run
      //      the same client-side crossfade the regular skip path uses.
      //   4. Mid-playback w/o AudioContext — first-click unlock hasn't
      //      happened yet; can't crossfade, just set + play.
      if (s.is_paused) {
        setSrcOnDeck(active, path);
        try { active.audio.pause(); } catch (_) {}
        try { active.audio.currentTime = 0; } catch (_) {}
      } else if (active.path && _ctx) {
        startCrossfade(path, _crossfadeSecondsCache, /* serverLed = */ true);
      } else {
        setSrcOnDeck(active, path);
        playOnDeck(active);
      }
    }
    // Gapless: pre-load next track on the standby deck as soon as the
    // server picks it.  By the time the crossfade fires, the browser
    // has already fetched + decoded enough to start playback instantly
    // — no stall, no silence.
    if (_prefetchEnabled && _nextTrackPathCache && !crossfading) {
      const standby = deckStandby();
      if (standby.path !== _nextTrackPathCache) {
        setSrcOnDeck(standby, _nextTrackPathCache);
        // Force the browser to start buffering NOW (preload="metadata"
        // alone won't fetch audio bytes until play()).  We can't actually
        // play() the standby — it'd be audible — but loading the source
        // and calling .load() kicks off the byte fetch on most browsers.
        try { standby.audio.load(); } catch (_) {}
      }
    }
  }

  // Sync server-driven pause / mute with the browser deck.
  if (_ctx) {
    const wantMuted = s.is_muted || s.is_paused;
    if (wantMuted) {
      // Mute the live deck without stomping the crossfade ramp
      if (!crossfading) deckActive().gain.gain.value = 0;
      if (s.is_paused) {
        suppressAdvance = true;
        // Pause BOTH decks during a crossfade — pausing only the active
        // (outgoing) deck would leave the incoming standby deck audible
        // and the user's pause click would feel like a duck rather than
        // a stop.  Off-crossfade, only the active deck is playing.
        for (const d of decks) {
          try { d.audio.pause(); } catch (_) {}
        }
      }
    } else {
      // Resume.  Active deck must always start playing again; standby is
      // a no-op resume when not crossfading (paused but with no src in
      // the steady state).  During a crossfade, both decks were paused
      // so both must be unpaused or the incoming track stays silent.
      if (deckActive().audio.paused && playbackEnabled) {
        playOnDeck(deckActive());
      }
      if (crossfading && deckStandby().audio.paused && playbackEnabled) {
        playOnDeck(deckStandby());
      }
      if (!crossfading) deckActive().gain.gain.value = _volume;
    }
  }
}

// ----------------------------------------------------------------
// Cover art
// ----------------------------------------------------------------

function loadCoverArt(trackPath) {
  // fetch() probe instead of <img> probe — both succeed silently on 200,
  // but <img>.onerror logs a console error for every 404, which spams
  // DevTools on tracks without embedded art.  fetch returns ok=false on
  // 404 without logging.  Set <img>.src only after we know the response
  // is a real image.
  const url = `/api/art?path=${encodeURIComponent(trackPath)}`;
  fetch(url, { method: "GET" }).then((res) => {
    if (!res.ok) {
      coverArt.hidden = true;
      coverArt.removeAttribute("src");
      return;
    }
    coverArt.src = url;
    coverArt.hidden = false;
  }).catch(() => {
    coverArt.hidden = true;
    coverArt.removeAttribute("src");
  });
}

// Lyrics rendering moved to ./modules/lyrics.js.
import {
  loadLyrics as _loadLyricsImpl,
  applyLyricsState as _applyLyricsStateImpl,
  renderLyricsList as _renderLyricsListImpl,
  resetLyricState,
  getCachedLyrics,
} from "./modules/lyrics.js";

const _lyricEls = () => ({
  lyricsCard,
  lyricsList,
  lyricAnnounce,
});

async function loadLyrics() { return _loadLyricsImpl(_lyricEls()); }
function renderLyricsList() { _renderLyricsListImpl(_lyricEls()); }

// ----------------------------------------------------------------
// Why this track? — plain-English explanation of the pick
// ----------------------------------------------------------------

let _lastWhyKey = "";

function applyWhyState(s) {
  const reasons = (s && s.why_this_track) || [];
  // Re-render only when the sentence list actually changes — keeps the
  // aria-live region from re-announcing on every per-second WS tick.
  const key = reasons.join("|") + "@" + (s.current_track ? s.current_track.path : "");
  if (key === _lastWhyKey) return;
  _lastWhyKey = key;
  if (!whyList) return;
  if (reasons.length === 0) {
    whyList.innerHTML = `<li class="no-results"
      style="color:var(--text-dim);font-style:italic;list-style:none;padding-left:0">
      No reasons yet — start playback to see why each track was picked.</li>`;
    return;
  }
  whyList.innerHTML = reasons
    .map((r) => `<li>${escHtml(r)}</li>`)
    .join("");
}


function applyLyricsState(s) { _applyLyricsStateImpl(s, _lyricEls()); }

// ----------------------------------------------------------------
// Queue
// ----------------------------------------------------------------

function queueKey(queue) {
  return queue.map(t => t.path).join("|");
}

function applyQueueState(queue) {
  const key = queueKey(queue);
  if (key === lastQueueKey) return;
  lastQueueKey = key;
  renderQueue(queue);
}

function renderQueue(queue) {
  queueCount.textContent = queue.length ? `(${queue.length})` : "";
  if (queue.length === 0) {
    queueList.innerHTML = `
      <li class="no-results"
          style="color:var(--text-dim);font-style:italic;list-style:none;padding-left:0">
        Queue is empty.  Search and use "Next" to add a track.
      </li>`;
    return;
  }
  queueList.innerHTML = queue.map((t, i) => {
    const name = escHtml(fmtTrack(t));
    const path = escHtml(t.path);
    const isFirst = i === 0;
    const isLast  = i === queue.length - 1;
    return `<li data-path="${path}">
      <span class="queue-name" title="${name}">${i + 1}. ${name}</span>
      <button class="queue-btn" data-action="up"     data-path="${path}"
              aria-label="Move ${name} up in queue"     ${isFirst ? "disabled" : ""}>
        <span aria-hidden="true">\u25b2</span> Up
      </button>
      <button class="queue-btn" data-action="down"   data-path="${path}"
              aria-label="Move ${name} down in queue"   ${isLast  ? "disabled" : ""}>
        <span aria-hidden="true">\u25bc</span> Down
      </button>
      <button class="queue-btn" data-action="remove" data-path="${path}"
              aria-label="Remove ${name} from queue">
        <span aria-hidden="true">\u2715</span> Remove
      </button>
    </li>`;
  }).join("");
}

// Event delegation for queue buttons.  Captures focus target before mutation
// so we can restore focus to the equivalent button after re-render.
queueList.addEventListener("click", async (e) => {
  const btn = e.target.closest(".queue-btn");
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action;
  const path   = btn.dataset.path;

  const items = Array.from(queueList.querySelectorAll("li[data-path]"));
  const paths = items.map(li => li.dataset.path);
  const idx   = paths.indexOf(path);
  if (idx < 0) return;

  let newPaths = paths.slice();
  let focusAction = action;
  let focusPath = path;
  let announceMsg = "";

  const niceName = items[idx]
    ? items[idx].querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, "")
    : path;

  if (action === "up" && idx > 0) {
    [newPaths[idx - 1], newPaths[idx]] = [newPaths[idx], newPaths[idx - 1]];
    announceMsg = `Moved ${niceName} up.`;
    if (idx - 1 === 0) focusAction = "down";
  } else if (action === "down" && idx < newPaths.length - 1) {
    [newPaths[idx + 1], newPaths[idx]] = [newPaths[idx], newPaths[idx + 1]];
    announceMsg = `Moved ${niceName} down.`;
    if (idx + 1 === newPaths.length - 1) focusAction = "up";
  } else if (action === "remove") {
    newPaths.splice(idx, 1);
    announceMsg = `Removed ${niceName} from queue.`;
    if (newPaths.length === 0) {
      focusPath = null;
    } else {
      focusPath = newPaths[Math.min(idx, newPaths.length - 1)];
      focusAction = "remove";
    }
  } else {
    return;
  }

  // Optimistic local render so user sees instant feedback
  renderQueue(newPaths.map(p => {
    const li = items.find(i => i.dataset.path === p);
    return {
      path: p,
      display_name: li
        ? li.querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, "")
        : p,
    };
  }));
  lastQueueKey = queueKey(newPaths.map(p => ({ path: p })));

  if (action === "remove") {
    await fetch("/api/queue/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  } else {
    await fetch("/api/queue/reorder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: newPaths }),
    });
  }

  queueAnnounce.textContent = announceMsg;
  _clearLiveRegionLater(queueAnnounce);

  if (focusPath) {
    const target = queueList.querySelector(
      `li[data-path="${CSS.escape(focusPath)}"] .queue-btn[data-action="${focusAction}"]`
    );
    if (target && !target.disabled) target.focus();
  }
});

function renderHistory() {
  if (historyItems.length === 0) return;
  historyList.innerHTML = historyItems
    .map(name => `<li><span class="history-title">${escHtml(name)}</span></li>`)
    .join("");
}

// ----------------------------------------------------------------
// WebSocket
// ----------------------------------------------------------------

// Module-scope WebSocket reference — must be declared BEFORE connectWS()
// runs, otherwise the assignment inside connectWS hits the TDZ and throws,
// leaving the page stuck on "Connecting…".
let _ws = null;

function setConnStatus(state, label) {
  connStatus.className  = state;
  connStatus.textContent = label;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  _ws = ws;

  setConnStatus("connecting", "Connecting\u2026");

  ws.onopen  = () => setConnStatus("connected", "Live");

  ws.onmessage = (ev) => {
    try { applyState(JSON.parse(ev.data)); } catch (_) {}
  };

  ws.onclose = () => {
    _ws = null;
    setConnStatus("error", "Disconnected");
    // Server gone — stop both decks immediately so audio doesn't keep
    // playing from the buffered <audio> elements after Ctrl+C on serve.
    stopAllDecks();
    // Clear stale intro / outro markers so the next reconnect doesn't
    // trigger a crossfade on a marker that no longer matches the
    // currently-loaded track (race window during reconnect).
    _currentOutroLenCache = null;
    _currentOutroStartCache = null;
    _nextTrackIntroEndCache = null;
    setTimeout(connectWS, 3000);
  };

  ws.onerror = () => setConnStatus("error", "Error");
}

connectWS();

// ----------------------------------------------------------------
// Button handlers
// ----------------------------------------------------------------

// Unified Play / Pause / Resume button.  In browser-playback mode the
// FIRST click also unlocks the AudioContext (browsers require a user
// gesture).  Subsequent clicks toggle pause via the server, which the
// state-applier mirrors to the active deck.  No `aria-pressed` is used \u2014
// the visible label honestly conveys the next action ("Play" / "Pause"
// / "Resume"), so a toggle role would be redundant.
btnPause.addEventListener("click", async () => {
  // First-click unlock path \u2014 synchronous play() inside the click
  // handler is required for iOS autoplay grants.
  if (!playbackEnabled && _lastBrowserPlayback) {
    try {
      await unlockAndPlay();
    } catch (_) { /* unlockAndPlay already announced the error */ }
    return;
  }
  // Standard transport: toggle on the server, browser deck mirrors via
  // applyBrowserPlaybackState on the next state push.
  try {
    const res  = await fetch("/api/pause", { method: "POST" });
    const data = await res.json();
    const isPaused = data.paused;
    // A11y C2: glyph + label + aria-pressed updated together.
    btnPause.innerHTML = isPaused
      ? '<span aria-hidden="true">\u25B6</span> Resume'
      : '<span aria-hidden="true">\u23F8</span> Pause';
    btnPause.setAttribute("aria-pressed", isPaused ? "false" : "true");
  } catch (_) { /* ignore \u2014 next WS state push will reconcile */ }
});

// (`_lastBrowserPlayback` is declared up in the audio playback module so
// the click handler can safely reference it before the first WS push.)

btnSkip.addEventListener("click", async () => {
  btnSkip.disabled = true;
  // In browser-playback mode, run a client-side crossfade with the
  // current transition effect.  Falls back to plain server skip if the
  // audio context isn't running yet (user hasn't clicked Play).
  if (_lastBrowserPlayback && playbackEnabled && _ctx && _nextTrackPathCache && !crossfading) {
    // Beatmatch-on-skip: when the user opted in AND both BPMs are
    // known, pitch-shift the standby deck so the new track joins the
    // existing groove instead of cold-cutting.  preservesPitch=true
    // gives a tempo-only stretch (proper beatmatch).  Reverted at
    // crossfade teardown by the timeout below.
    let bmRevert = null;
    if (_beatmatchOnSkip && _outBpmCache > 0 && _inBpmCache > 0) {
      const ratio = _outBpmCache / _inBpmCache;
      // Clamp ±15% so wildly mismatched tempos don't sound silly.
      const clamped = Math.max(0.85, Math.min(1.15, ratio));
      const standby = decks[activeIdx ^ 1];
      const audio = standby.audio;
      const prevPitch = audio.preservesPitch;
      const prevRate = audio.playbackRate;
      try { audio.preservesPitch = true; } catch (_) {}
      try { audio.playbackRate = clamped; } catch (_) {}
      _dbg("beatmatch-on-skip: ratio=", clamped.toFixed(3),
        "(", _outBpmCache.toFixed(1), "/", _inBpmCache.toFixed(1), ")");
      bmRevert = setTimeout(() => {
        try { audio.playbackRate = prevRate; } catch (_) {}
        try { audio.preservesPitch = prevPitch; } catch (_) {}
      }, _crossfadeSecondsCache * 1000 + 200);
    }
    startCrossfade(_nextTrackPathCache, _crossfadeSecondsCache);
  } else {
    await fetch("/api/skip", { method: "POST" });
  }
  setTimeout(() => { btnSkip.disabled = false; }, 800);
});

// ----------------------------------------------------------------
// Seek slider — drag/click + keyboard.  Updates server position via
// /api/seek, and (in browser-playback mode) jumps the active deck's
// HTMLMediaElement currentTime so the audio actually skips.
// ----------------------------------------------------------------

const _seekTrack = document.getElementById("progress-track");
let _seekDragging = false;
let _seekLastAriaUpdate = 0;
let _lastDuration = 0;

function _seekTrackDuration() {
  // Active deck's duration when known (browser-playback mode); falls
  // back to the cached duration computed in renderState.  Both are
  // expressed in seconds.
  if (_lastBrowserPlayback) {
    try {
      const d = decks[activeIdx].audio.duration;
      if (isFinite(d) && d > 0) return d;
    } catch (_) {}
  }
  return _lastDuration;
}

function _seekToFrac(frac, opts) {
  const dur = _seekTrackDuration();
  if (!(dur > 0)) return;
  _dbg("seek ->", (frac * 100).toFixed(1) + "%", "of", dur.toFixed(1), "s");
  const f = Math.max(0, Math.min(1, frac));
  const seconds = f * dur;
  // Local audio jump in browser-playback mode so the user hears the
  // change immediately; server is informed in parallel for state sync
  // (CLI listeners + reconnect-time replay).
  if (_lastBrowserPlayback) {
    try {
      decks[activeIdx].audio.currentTime = Math.max(0, Math.min(dur - 0.1, seconds));
    } catch (_) {}
  }
  // Throttle aria updates while dragging to avoid flooding NVDA.
  const now = performance.now();
  const force = !!(opts && opts.force);
  if (force || now - _seekLastAriaUpdate > 150) {
    _seekLastAriaUpdate = now;
    if (_seekTrack) {
      const pct = (f * 100).toFixed(0);
      _seekTrack.setAttribute("aria-valuenow", pct);
      _seekTrack.setAttribute("aria-valuetext",
        `${fmtTime(seconds)} of ${fmtTime(dur)}`);
    }
    if (progressFill) progressFill.style.width = (f * 100).toFixed(1) + "%";
    if (progressLbl) progressLbl.textContent = `${fmtTime(seconds)} / ${fmtTime(dur)}`;
  }
  // Always inform the server, even mid-drag.  The endpoint is cheap
  // and idempotent; final position wins.
  fetch("/api/seek", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds }),
  }).catch(() => {});
}

if (_seekTrack) {
  _seekTrack.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    _seekDragging = true;
    try { _seekTrack.setPointerCapture(e.pointerId); } catch (_) {}
    const rect = _seekTrack.getBoundingClientRect();
    _seekToFrac((e.clientX - rect.left) / rect.width, { force: true });
    e.preventDefault();
  });
  _seekTrack.addEventListener("pointermove", (e) => {
    if (!_seekDragging) return;
    const rect = _seekTrack.getBoundingClientRect();
    _seekToFrac((e.clientX - rect.left) / rect.width);
  });
  _seekTrack.addEventListener("pointerup", (e) => {
    if (!_seekDragging) return;
    _seekDragging = false;
    try { _seekTrack.releasePointerCapture(e.pointerId); } catch (_) {}
    const rect = _seekTrack.getBoundingClientRect();
    _seekToFrac((e.clientX - rect.left) / rect.width, { force: true });
  });
  _seekTrack.addEventListener("keydown", (e) => {
    const dur = _seekTrackDuration();
    if (!(dur > 0)) return;
    const cur = (parseFloat(_seekTrack.getAttribute("aria-valuenow")) || 0) / 100 * dur;
    let next = cur;
    let handled = true;
    switch (e.key) {
      case "ArrowLeft":  next = cur - (e.shiftKey ? 15 : 5); break;
      case "ArrowRight": next = cur + (e.shiftKey ? 15 : 5); break;
      case "PageDown":   next = cur - 15; break;
      case "PageUp":     next = cur + 15; break;
      case "Home":       next = 0; break;
      case "End":        next = Math.max(0, dur - 1); break;
      default: handled = false;
    }
    if (handled) {
      e.preventDefault();
      _seekToFrac(Math.max(0, Math.min(dur, next)) / dur, { force: true });
    }
  });
}

const btnShuffle = document.getElementById("btn-shuffle");
if (btnShuffle) {
  btnShuffle.addEventListener("click", async () => {
    btnShuffle.disabled = true;
    await fetch("/api/random-track", { method: "POST" });
    setTimeout(() => { btnShuffle.disabled = false; }, 800);
  });
}

btnMute.addEventListener("click", async () => {
  const res   = await fetch("/api/mute", { method: "POST" });
  const data  = await res.json();
  const muted = data.muted;
  btnMute.setAttribute("aria-pressed", muted ? "true" : "false");
  btnMute.innerHTML = muted
    ? '<span aria-hidden="true">\uD83D\uDD07</span> Unmute'
    : '<span aria-hidden="true">\uD83D\uDD0A</span> Mute';
});

// Shortcuts modal: toolbar button + Close button inside the modal.
const btnShortcuts = document.getElementById("btn-shortcuts");
const btnShortcutsClose = document.getElementById("btn-shortcuts-close");
if (btnShortcuts) {
  btnShortcuts.addEventListener("click", () => _toggleShortcutsModal());
}
if (btnShortcutsClose) {
  btnShortcutsClose.addEventListener("click", () => {
    const modal = document.getElementById("hotkey-help-modal");
    if (modal && modal.open) modal.close();
  });
}

// Discovery toggle — sent via WebSocket so the server can push updated state
btnDiscovery.addEventListener("click", () => {
  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({ type: "toggle_discovery" }));
  }
});

// Volume slider — debounced to avoid flooding server + announce only on
// user-initiated change (separate live region so the WS state echo
// doesn't re-announce every second).
let volTimer = null;
let volAnnounceTimer = null;

// Live-region clear helper extracted to ./modules/live-region.js.
// Re-bound under the legacy underscore name so existing call sites
// keep working without a sweep.
import { clearLiveRegionLater as _clearLiveRegionLater } from "./modules/live-region.js";
// Logarithmic (perceptual) volume curve — humans hear loudness as
// log of amplitude, so a linear slider feels backwards: 0-50 % barely
// changes, 80-100 % feels too loud.  Map slider 0-100 → gain via
// `(10 ** (slider/50 - 2))` so −60 dB at 0, −20 dB at 50, 0 dB at 100.
function _sliderToGain(pct) {
  if (pct <= 0) return 0;
  // Standard "audio fader" curve — exponentially-spaced dB to linear
  const db = (pct / 50.0 - 2.0) * 30.0;   // 0%→−60 dB, 50%→−30 dB, 100%→0 dB
  return Math.pow(10, db / 20.0);
}

// Inverse of _sliderToGain — used when writing the server-broadcast
// gain back into the slider (so the WS echo doesn't snap the fader).
function _gainToSlider(gain) {
  if (!gain || gain <= 0) return 0;
  if (gain >= 1) return 100;
  const db = 20 * Math.log10(gain);
  const pct = (db / 30.0 + 2.0) * 50.0;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

// Last user-initiated volume change (ms epoch).  WS state echoes that
// arrive within ~600 ms of a local change are ignored so the slider
// can't fight the in-flight POST.
let _lastUserVolTs = 0;

volSlider.addEventListener("input", () => {
  const val = parseInt(volSlider.value, 10);
  volPct.textContent = val + "%";
  _lastUserVolTs = Date.now();
  // Drive the Web Audio gain immediately so the change is audible
  // without waiting on the server round-trip.
  setVolume(_sliderToGain(val));
  clearTimeout(volTimer);
  volTimer = setTimeout(() => {
    // Send the perceptual gain (matches what we drive locally) so the
    // server-side player + WebSocket echo stay in sync with the slider.
    fetch("/api/volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: _sliderToGain(val) }),
    });
  }, 120);
  // Polite announce, debounced — fires only after user stops moving
  // the slider so screen readers don't read every intermediate step.
  // Cleared 3 s after the announcement so AT users running with a
  // Speech Viewer (or sighted users with a CSS override that reveals
  // visually-hidden regions) don't see a stale "Volume 90%" parked at
  // the bottom of the page after the announcer has already spoken it.
  clearTimeout(volAnnounceTimer);
  volAnnounceTimer = setTimeout(() => {
    if (volAnnounce) {
      volAnnounce.textContent = `Volume ${val}%.`;
      _clearLiveRegionLater(volAnnounce);
    }
  }, 250);
});

// ----------------------------------------------------------------
// Keyboard shortcuts — YouTube-style transport control.
// Active when focus is on the page chrome (NVDA focus mode passes
// keys through to the browser, so these work alongside screen-reader
// users).  Skipped when typing in inputs / contenteditable.
// ----------------------------------------------------------------

// _isTypingTarget moved to ./modules/dom-helpers.js (imported above).

function _toggleShortcutsModal() {
  const modal = document.getElementById("hotkey-help-modal");
  if (!modal) return false;
  if (modal.open) {
    modal.close();
  } else {
    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      // Older browsers: fall back to the open attribute (no focus trap).
      modal.setAttribute("open", "");
    }
    // Focus the Close button explicitly — browsers vary on default focus.
    const closeBtn = document.getElementById("btn-shortcuts-close");
    if (closeBtn) {
      try { closeBtn.focus(); } catch (_) {}
    }
  }
  return true;
}

// Use window + capture-phase so hotkeys fire even when a focused element
// (button, slider, custom widget) would otherwise consume / stop the
// keydown event.  YouTube uses the same pattern.
//
// Key-held latch: NVDA (and some IMEs) forward auto-repeat keydowns
// without setting KeyboardEvent.repeat, so the e.repeat guard alone
// missed bursts.  Track every physical keydown until its keyup; second
// keydown for the same key is suppressed regardless of repeat flag.
const _pressedHotkeys = new Set();
window.addEventListener("keyup", (e) => {
  _pressedHotkeys.delete(e.key);
  // Modifier-aware aliases — e.g. Shift held on "?" produces "?", but
  // releasing the letter without releasing Shift drops the lower-case
  // sibling too.  Cheap to clear both.
  if (e.key && e.key.length === 1) {
    _pressedHotkeys.delete(e.key.toLowerCase());
    _pressedHotkeys.delete(e.key.toUpperCase());
  }
}, true);
// Window blur clears the latch — otherwise alt-tabbing while a key is
// held would leave it permanently flagged as pressed.
window.addEventListener("blur", () => _pressedHotkeys.clear());

window.addEventListener("keydown", (e) => {
  // Auto-repeat (key held) flooded shuffle/skip/mute clicks.  Two
  // belt-and-braces guards because some screen readers / IMEs forward
  // repeats without setting e.repeat:
  //   1. Native repeat flag.
  //   2. Press-latch -- second keydown w/o intervening keyup is dropped.
  if (e.repeat) return;
  if (_pressedHotkeys.has(e.key)) return;
  _pressedHotkeys.add(e.key);
  // Don't hijack keys when the user is typing in a form field.
  if (_isTypingTarget(e.target)) return;
  // Scope transport hotkeys to the Now Playing tab.  On Settings / Queue /
  // Library tabs the page is full of selects, sliders, and other widgets
  // whose own arrow-key / space behaviour must not be hijacked by the
  // volume / play-pause shortcuts.  Shortcuts dialog ("?") is allowed
  // anywhere so users can discover the rule.
  const _nowPanel = document.getElementById("panel-now");
  const _nowVisible = _nowPanel && !_nowPanel.hasAttribute("hidden");
  if (!_nowVisible && e.key !== "?" && e.key !== "/") return;
  // Don't hijack keys inside the shortcuts dialog itself — Tab/Space
  // there should be handled by the dialog (focus trap, button activation).
  const modal = document.getElementById("hotkey-help-modal");
  if (modal && modal.open && modal.contains(e.target)) {
    // Still allow "?" / Esc to close the modal from inside.
    if (e.key === "?" || e.key === "/") {
      e.preventDefault();
      _toggleShortcutsModal();
    }
    return;
  }
  // Modifiers are usually the user invoking browser / NVDA shortcuts.
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  // Tablist owns its own arrow-key navigation (APG roving tabindex).
  // Don't steal Up/Down when focus is on a tab.
  const targetIsTab = e.target && e.target.getAttribute
                      && e.target.getAttribute("role") === "tab";

  const key = e.key;
  let bumpVol = 0;

  switch (key) {
    case " ":          // Space (YouTube default)
    case "Spacebar":
    case "k":
    case "K":
      btnPause.click();
      break;
    case "n":
    case "N":
      btnSkip.click();
      break;
    case "s":
    case "S":
      // btnShuffle ref defined later in this file; guard with typeof.
      if (typeof btnShuffle !== "undefined" && btnShuffle) btnShuffle.click();
      break;
    case "m":
    case "M":
      btnMute.click();
      break;
    case "ArrowUp":
      if (targetIsTab) return;
      bumpVol = +5;
      break;
    case "ArrowDown":
      if (targetIsTab) return;
      bumpVol = -5;
      break;
    case "?":
    case "/":
      // "/" passes through both NVDA browse + focus modes; "?" is Shift+/.
      if (!_toggleShortcutsModal()) return;
      break;
    default:
      return;  // not a hotkey, let the browser handle it
  }

  if (bumpVol !== 0) {
    const cur = parseInt(volSlider.value, 10);
    const next = Math.max(0, Math.min(100, cur + bumpVol));
    if (next !== cur) {
      volSlider.value = String(next);
      // Synthesize an input event so the existing listener does the
      // gain ramp + server POST + announce in one place.
      volSlider.dispatchEvent(new Event("input"));
    }
  }

  // Always swallow the key when we matched one — prevents the page
  // from scrolling on Space, etc.
  e.preventDefault();
});

// ----------------------------------------------------------------
// Media Session API — wires up OS media keys, lock-screen art,
// and notification-shade transport on Chromium / WebKit / Firefox.
// ----------------------------------------------------------------

// Media Session API moved to ./modules/media-session.js.
import {
  updateMediaSession,
  installMediaActionHandlers,
} from "./modules/media-session.js";

installMediaActionHandlers({
  onPlay: () => {
    if (!playbackEnabled && _lastBrowserPlayback) unlockAndPlay().catch(() => {});
    else fetch("/api/pause", { method: "POST" });
  },
});

// (Legacy duplicate keydown handler removed -- the canonical hotkey
//  handler is the window-capture-phase listener defined earlier.  Two
//  handlers fired every key twice + the legacy version's blanket
//  suppression on every INPUT killed Space/M when focus was on the
//  volume slider.)

// ----------------------------------------------------------------
// Search
// ----------------------------------------------------------------

async function doSearch() {
  const q = searchInput.value.trim();
  if (!q) {
    searchResults.innerHTML = "";
    searchInput.setAttribute("aria-expanded", "false");
    searchCount.textContent = "";
    return;
  }

  const res  = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  const data = await res.json();
  const results = data.results || [];

  if (results.length === 0) {
    searchResults.innerHTML = `<li><span class="no-results">No results for \u201c${escHtml(q)}\u201d.</span></li>`;
    searchInput.setAttribute("aria-expanded", "true");
    searchCount.textContent = "No results found.";
    _clearLiveRegionLater(searchCount);
    return;
  }

  searchResults.innerHTML = results.map(t => {
    const name = escHtml(fmtTrack(t));
    const path = escHtml(t.path);
    return `<li>
      <span class="result-name" title="${name}">${name}</span>
      <button class="result-btn"
              aria-label="Play ${name} now"
              data-path="${path}"
              data-now="true"><span aria-hidden="true">&#9654;</span> Now</button>
      <button class="result-btn"
              aria-label="Queue ${name} as next track"
              data-path="${path}"
              data-now="false"><span aria-hidden="true">&#9197;</span> Next</button>
    </li>`;
  }).join("");
  searchInput.setAttribute("aria-expanded", "true");
  // Announce count separately (not the full list) per advisory
  searchCount.textContent = `${results.length} result${results.length === 1 ? "" : "s"} found.`;
  _clearLiveRegionLater(searchCount);
}

btnSearch.addEventListener("click", doSearch);
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch();
});

// Collapse results when input is cleared
searchInput.addEventListener("input", () => {
  if (!searchInput.value.trim()) {
    searchResults.innerHTML = "";
    searchInput.setAttribute("aria-expanded", "false");
    searchCount.textContent = "";
  }
});

// Play-now / queue-add buttons (event delegation on the results list).
// "Now" interrupts current track; "Next" appends to the user-managed queue
// (which is rendered in the Queue section with reorder controls).
searchResults.addEventListener("click", async (e) => {
  const btn = e.target.closest(".result-btn");
  if (!btn) return;
  const path = btn.dataset.path;
  const now  = btn.dataset.now === "true";
  const name = btn.closest("li").querySelector(".result-name").textContent;
  btn.disabled = true;
  try {
    if (now) {
      await fetch("/api/play-next", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, now: true }),
      });
      queueAnnounce.textContent = `Playing ${name} now.`;
      _clearLiveRegionLater(queueAnnounce);
    } else {
      await fetch("/api/queue/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      queueAnnounce.textContent = `Added ${name} to queue.`;
      _clearLiveRegionLater(queueAnnounce);
    }
  } finally {
    btn.disabled = false;
    btn.focus();
  }
});

// ----------------------------------------------------------------
// Library tools panel moved to ./modules/library-jobs.js.
import {
  installLibraryJobs,
  applyLibraryJobState as _applyLibraryJobStateImpl,
} from "./modules/library-jobs.js";

const _libEls = {
  runIndex:        document.getElementById("lib-run-index"),
  runEnrich:       document.getElementById("lib-run-enrich"),
  runPrune:        document.getElementById("lib-run-prune"),
  runStats:        document.getElementById("lib-run-stats"),
  runStop:         document.getElementById("lib-run-stop"),
  indexLimit:      document.getElementById("lib-index-limit"),
  statsRefresh:    document.getElementById("lib-stats-refresh"),
  libLog:          document.getElementById("library-log"),
  jobStatus:       document.getElementById("lib-job-status"),
  statCount:       document.getElementById("lib-stat-count"),
  statAvgBpm:      document.getElementById("lib-stat-avg-bpm"),
  statWithKey:     document.getElementById("lib-stat-with-key"),
  statWithGenre:   document.getElementById("lib-stat-with-genre"),
  statWithEnergy:  document.getElementById("lib-stat-with-energy"),
};
installLibraryJobs(_libEls);
function applyLibraryJobState(s) { _applyLibraryJobStateImpl(s, _libEls); }

// Tab router moved to ./modules/tabs.js.
import { initViewRouter as _initViewRouter } from "./modules/tabs.js";

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _initViewRouter, { once: true });
} else {
  _initViewRouter();
}

// ----------------------------------------------------------------
// Initial state fetch
// ----------------------------------------------------------------

fetch("/api/status")
  .then(r => {
    if (!r.ok) throw new Error(`/api/status returned ${r.status}`);
    return r.json();
  })
  .then(applyState)
  .catch((err) => {
    setConnStatus("error", `Cannot reach server: ${err.message}`);
    npAnnounce.textContent = `Cannot reach server: ${err.message}`;
  });

// data-show-when mechanism moved to ./modules/show-when.js.
import {
  applyShowWhen as _applyShowWhen,
  installShowWhenListener,
} from "./modules/show-when.js";
installShowWhenListener();
// Apply once at load + after every state push (server may flip a
// checkbox via WS without the user touching it).
_applyShowWhen();

// ----------------------------------------------------------------
// Voice liners — Settings panel, file list, upload/delete, test.
// ----------------------------------------------------------------

const lnEnabled       = document.getElementById("ln-enabled");
const lnEveryN        = document.getElementById("ln-every-n");
const lnEveryMin      = document.getElementById("ln-every-min");
const lnRandMin       = document.getElementById("ln-rand-min");
const lnRandMax       = document.getElementById("ln-rand-max");
const lnPickMode      = document.getElementById("ln-pick-mode");
const lnDuckDb        = document.getElementById("ln-duck-db");
const lnTestBtn       = document.getElementById("ln-test");
const lnFolderDisplay = document.getElementById("ln-folder-display");
const lnFileList      = document.getElementById("ln-file-list");
const lnUpload        = document.getElementById("ln-upload");
const lnUploadSubmit  = document.getElementById("ln-upload-submit");
const lnStatus        = document.getElementById("ln-status");

let _linerLib = { folder: "", files: [], config: {} };
let _linerLastFireAt = performance.now();
let _linerTrackCount = 0;
let _linerRandomTarget = null;

function _setLinerStatus(msg) {
  if (lnStatus) {
    lnStatus.classList.remove("visually-hidden");
    lnStatus.textContent = msg;
    _clearLiveRegionLater(lnStatus, 4000);
  }
}

async function _refreshLinerLibrary() {
  try {
    const resp = await fetch("/api/liners");
    if (!resp.ok) return;
    const body = await resp.json();
    _linerLib = body;
    if (lnFolderDisplay) {
      lnFolderDisplay.textContent = "Folder: " + (body.folder || "—");
    }
    if (lnFileList) {
      lnFileList.innerHTML = "";
      for (const name of body.files || []) {
        const li = document.createElement("li");
        const text = document.createElement("span");
        text.textContent = name;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.innerHTML = '<span aria-hidden="true">Delete</span>' +
          `<span class="visually-hidden"> ${escHtml(name)}</span>`;
        btn.addEventListener("click", () => _deleteLiner(name));
        li.appendChild(text);
        li.appendChild(document.createTextNode(" "));
        li.appendChild(btn);
        lnFileList.appendChild(li);
      }
    }
    // Sync config inputs from server payload (don't trample user-edited fields).
    const c = body.config || {};
    if (lnEnabled && document.activeElement !== lnEnabled) {
      lnEnabled.checked = !!c.enabled;
    }
    if (lnEveryN && document.activeElement !== lnEveryN) {
      lnEveryN.value = c.every_n_songs != null ? c.every_n_songs : "";
    }
    if (lnEveryMin && document.activeElement !== lnEveryMin) {
      lnEveryMin.value = c.every_minutes != null ? c.every_minutes : "";
    }
    if (lnRandMin && document.activeElement !== lnRandMin) {
      lnRandMin.value = c.random_min_minutes != null ? c.random_min_minutes : "";
    }
    if (lnRandMax && document.activeElement !== lnRandMax) {
      lnRandMax.value = c.random_max_minutes != null ? c.random_max_minutes : "";
    }
    if (lnPickMode && document.activeElement !== lnPickMode) {
      lnPickMode.value = c.pick_mode || "random";
    }
    if (lnDuckDb && document.activeElement !== lnDuckDb) {
      lnDuckDb.value = c.duck_db != null ? c.duck_db : -12;
    }
    _applyShowWhen();
  } catch (err) {
    _dbg("liner refresh failed:", err);
  }
}

async function _deleteLiner(name) {
  if (!confirm(`Delete liner "${name}"?`)) return;
  try {
    const resp = await fetch(
      `/api/liners/file/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    if (!resp.ok) {
      _setLinerStatus(`Delete failed: HTTP ${resp.status}`);
      return;
    }
    _setLinerStatus(`Deleted ${name}`);
    await _refreshLinerLibrary();
  } catch (err) {
    _setLinerStatus(`Delete failed: ${err.message}`);
  }
}

if (lnUploadSubmit) {
  lnUploadSubmit.addEventListener("click", async () => {
    if (!lnUpload || !lnUpload.files || lnUpload.files.length === 0) {
      _setLinerStatus("Pick a file first.");
      return;
    }
    const f = lnUpload.files[0];
    const fd = new FormData();
    fd.append("file", f, f.name);
    _setLinerStatus(`Uploading ${f.name}...`);
    try {
      const resp = await fetch("/api/liners/upload", { method: "POST", body: fd });
      if (!resp.ok) {
        const detail = await resp.text();
        _setLinerStatus(`Upload failed: ${detail}`);
        return;
      }
      _setLinerStatus(`Uploaded ${f.name}`);
      lnUpload.value = "";
      await _refreshLinerLibrary();
    } catch (err) {
      _setLinerStatus(`Upload failed: ${err.message}`);
    }
  });
}

function _postLinerConfig() {
  const body = {
    liners_enabled: !!(lnEnabled && lnEnabled.checked),
    liners_every_n_songs: _intOrNull(lnEveryN),
    liners_every_minutes: _floatOrNull(lnEveryMin),
    liners_random_min_minutes: _floatOrNull(lnRandMin),
    liners_random_max_minutes: _floatOrNull(lnRandMax),
    liners_pick_mode: lnPickMode ? lnPickMode.value : "random",
    liners_duck_db: _floatOrNull(lnDuckDb),
  };
  postSettings("/api/playback-settings", body);
}
function _intOrNull(el) {
  if (!el || el.value === "" || el.value == null) return null;
  const n = parseInt(el.value, 10);
  return isNaN(n) ? null : n;
}
function _floatOrNull(el) {
  if (!el || el.value === "" || el.value == null) return null;
  const n = parseFloat(el.value);
  return isNaN(n) ? null : n;
}

for (const el of [
  lnEnabled, lnEveryN, lnEveryMin, lnRandMin, lnRandMax, lnPickMode, lnDuckDb,
]) {
  if (!el) continue;
  el.addEventListener("change", _postLinerConfig);
}

async function _playLinerByName(name) {
  if (!_ctx) return;
  try {
    const resp = await fetch(`/api/liners/file/${encodeURIComponent(name)}`);
    if (!resp.ok) {
      _setLinerStatus(`Liner fetch failed: HTTP ${resp.status}`);
      return;
    }
    const buf = await resp.arrayBuffer();
    const audioBuf = await _ctx.decodeAudioData(buf);
    const src = _ctx.createBufferSource();
    src.buffer = audioBuf;
    const gain = _ctx.createGain();
    gain.gain.value = 1.0;
    src.connect(gain);
    gain.connect(_ctx.destination);
    // Duck the active deck for the duration of the liner + 200 ms tail.
    const duckDb = (_linerLib.config && _linerLib.config.duck_db) || -12;
    const duckLin = Math.pow(10, duckDb / 20);
    const dur = audioBuf.duration;
    const t0 = _ctx.currentTime;
    const active = decks[activeIdx];
    active.gain.gain.cancelScheduledValues(t0);
    active.gain.gain.setValueAtTime(active.gain.gain.value, t0);
    active.gain.gain.linearRampToValueAtTime(_volume * duckLin, t0 + 0.2);
    active.gain.gain.setValueAtTime(_volume * duckLin, t0 + dur - 0.2);
    active.gain.gain.linearRampToValueAtTime(_volume, t0 + dur + 0.2);
    src.start(t0);
    _linerLastFireAt = performance.now();
    _linerTrackCount = 0;
    _linerRandomTarget = _rollLinerRandomTarget();
    _setLinerStatus(`Liner playing: ${name}`);
  } catch (err) {
    _setLinerStatus(`Liner playback failed: ${err.message}`);
  }
}

function _pickLiner() {
  if (!_linerLib.files || _linerLib.files.length === 0) return null;
  const mode = (_linerLib.config && _linerLib.config.pick_mode) || "random";
  if (mode === "sequential") {
    const i = (_linerSeqCursor++) % _linerLib.files.length;
    return _linerLib.files[i];
  }
  // weighted falls back to random in the browser since weights aren't
  // persisted yet; matches LinerLibrary.pick fallback behaviour.
  const i = Math.floor(Math.random() * _linerLib.files.length);
  return _linerLib.files[i];
}
let _linerSeqCursor = 0;

function _rollLinerRandomTarget() {
  const c = _linerLib.config || {};
  const lo = c.random_min_minutes;
  const hi = c.random_max_minutes;
  if (lo == null || hi == null || lo > hi || hi <= 0) return null;
  return lo + Math.random() * (hi - lo);
}

if (lnTestBtn) {
  lnTestBtn.addEventListener("click", async () => {
    const name = _pickLiner();
    if (!name) {
      _setLinerStatus("No liner files in folder.");
      return;
    }
    await _playLinerByName(name);
  });
}

// Periodic liner trigger evaluation -- once per second.  When enabled
// and any trigger condition is met, fire a clip.
setInterval(() => {
  if (!_linerLib.config || !_linerLib.config.enabled) return;
  if (!_ctx || !_lastBrowserPlayback) return;
  const c = _linerLib.config;
  const minsSince = (performance.now() - _linerLastFireAt) / 60000;
  let fire = false;
  if (c.every_n_songs && _linerTrackCount >= c.every_n_songs) fire = true;
  if (c.every_minutes && minsSince >= c.every_minutes) fire = true;
  if (_linerRandomTarget != null && minsSince >= _linerRandomTarget) fire = true;
  if (fire) {
    const name = _pickLiner();
    if (name) _playLinerByName(name);
  }
}, 1000);

// Track-advance counter for the every_n_songs trigger.  Bumps every
// time the WS state push surfaces a new current_track path -- one
// hook covers every advance route (natural end, skip, queue Now,
// shuffle, server-led random, CLI advance).
let _lastLinerSeenPath = null;
function _bumpLinerTrackCount(s) {
  const cur = (s && s.current_track && s.current_track.path) || null;
  if (cur && cur !== _lastLinerSeenPath) {
    if (_lastLinerSeenPath !== null) _linerTrackCount += 1;
    _lastLinerSeenPath = cur;
  }
}

// Initial fetch + reapply hidden state on load.
_refreshLinerLibrary();
