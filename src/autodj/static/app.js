"use strict";

// ----------------------------------------------------------------
// Utilities
// ----------------------------------------------------------------

function fmtTime(sec) {
  if (!sec || isNaN(sec)) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function fmtTrack(t) {
  if (!t) return "—";
  if (t.artist && t.title) return `${t.artist} \u2014 ${t.title}`;
  return t.display_name || t.title || "Unknown";
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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
let lastLyricIndex = null; // suppress repeated lyric announcements
let lastQueueKey = "";     // skip queue re-render when unchanged
let cachedLyrics = [];     // full lyric list for the current track (for visible scroll)
let lastBadgeKey = null;   // suppress repeated badge announcements within one track
let lastNextKey  = null;   // suppress aria-live re-announce of unchanged next track

// ----------------------------------------------------------------
// State update — called on every WS push and on manual API calls
// ----------------------------------------------------------------

function applyState(s) {
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
    lastLyricIndex = null;
    cachedLyrics = [];
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
  progressFill.style.width = pct.toFixed(1) + "%";
  progressLbl.textContent  = `${fmtTime(elapsed)} / ${fmtTime(dur)}`;

  // Snapshot for first-click unlock branch in btnPause handler
  _lastBrowserPlayback = !!s.browser_playback;

  // Unified Play / Pause / Resume button.  Three states:
  //   1. No track yet \u2192 "Play", disabled
  //   2. Browser-playback mode, audio not yet unlocked \u2192 "Play", enabled
  //      (clicking unlocks AudioContext + starts deck)
  //   3. Playing or paused \u2192 "Pause" / "Resume" toggle
  // No aria-pressed \u2014 the visible label is the state.
  const hasTrack = s.current_track != null;
  if (!hasTrack) {
    btnPause.disabled = true;
    btnPause.innerHTML = '<span aria-hidden="true">\u25B6</span> Play';
  } else if (s.browser_playback && !playbackEnabled) {
    btnPause.disabled = false;
    btnPause.innerHTML = '<span aria-hidden="true">\u25B6</span> Play';
  } else {
    btnPause.disabled = false;
    btnPause.innerHTML = s.is_paused
      ? '<span aria-hidden="true">\u25B6</span> Resume'
      : '<span aria-hidden="true">\u23F8</span> Pause';
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

  transitionSelect.value = st.transition || "none";

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
  pbDaypart.checked      = !!(st.playback && st.playback.enable_daypart);
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
pbReplayGain.addEventListener("change", () => {
  postSettings("/api/playback-settings", { replaygain_enabled: pbReplayGain.checked });
});
pbDaypart.addEventListener("change", () => {
  postSettings("/api/playback-settings", { enable_daypart: pbDaypart.checked });
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
    if (phrases.length) {
      // Slight delay so the title aria-live region speaks first
      setTimeout(() => { badgesAnnounce.textContent = phrases.join(", "); }, 800);
    }
  }
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
    "cross_eq_swap", "bitcrusher", "flanger", "pitch_swell", "telephone",
    "backspin", "forward_spin", "chorus", "submerge", "vinyl_wow",
    "freeze", "glitch",
    "scratch", "beat_repeat", "sidechain_pump", "reverse_reverb", "air_horn"];
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

function _makeReverbIR(durationSec, decay) {
  const sr = _ctx.sampleRate;
  const n = Math.max(1, Math.floor(sr * durationSec));
  const buf = _ctx.createBuffer(2, n, sr);
  for (let ch = 0; ch < 2; ch++) {
    const data = buf.getChannelData(ch);
    for (let i = 0; i < n; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / n, decay);
  }
  return buf;
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

function _doSpin(ctx, outDeck, t0, fadeSec, reverse, teardowns) {
  // Real reverse / push-forward via decoded AudioBuffer + AudioBufferSource.
  // Critical detail: the buffer source routes DIRECT to ctx.destination,
  // bypassing deck.gain — otherwise the crossfade ramp silences the spin
  // before it's heard.  We mute the live HTMLMediaElement AND ramp
  // deck.gain to 0 immediately so the only audible source is our spin.
  //
  // Industry envelope:
  //   reverse: rate decays 2.0 → 0.05 (vinyl friction physics)
  //   forward: rate accelerates 1.0 → 3.0 (push-forward release)
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
    if (reverse) {
      bufSrc.playbackRate.setValueAtTime(2.0, t0);
      bufSrc.playbackRate.linearRampToValueAtTime(0.05, t0 + spinSec);
    } else {
      bufSrc.playbackRate.setValueAtTime(1.0, t0);
      bufSrc.playbackRate.linearRampToValueAtTime(3.0, t0 + spinSec);
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
      try { bufSrc.disconnect(); bufGain.disconnect(); } catch (_) {}
    }
    if (synthNoise) {
      try { synthNoise.stop(); } catch (_) {}
      try { synthNoise.disconnect(); synthBp.disconnect(); synthG.disconnect(); } catch (_) {}
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
      try { bufSrc.disconnect(); bufGain.disconnect(); } catch (_) {}
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
};
const _MAX_FX_DURATION_S = 12.0;
const _ABS_MIN_FX_DURATION_S = 1.0;

function _effectDurationFor(effect, fadeSec, outroLen) {
  // Static floor (per-effect minimum) — always honoured.
  const staticMin = _MIN_FX_DURATION_S[effect] || 0;
  // Without a known outro, fall back to the legacy "max of fade and
  // per-effect floor" behaviour.
  if (outroLen == null || !(outroLen > 0)) {
    return Math.max(fadeSec, staticMin);
  }
  const frac = _OUTRO_FRACTION[effect] != null ? _OUTRO_FRACTION[effect] : 0.5;
  const target = outroLen * frac;
  // Clamp: never below the per-effect floor (or absolute 1.0s), never
  // above 12s — keeps musically sane boundaries even on edge tracks.
  const lo = Math.max(_ABS_MIN_FX_DURATION_S, staticMin);
  return Math.min(_MAX_FX_DURATION_S, Math.max(lo, target));
}

function applyTransitionFx(effect, fadeSec, outDeck, inDeck) {
  const ctx = _ctx;
  if (!ctx || effect === "none" || !effect) return () => {};
  // Pick the per-effect duration based on the outgoing track's outro
  // length when known, otherwise extend to the static minimum.
  fadeSec = _effectDurationFor(effect, fadeSec, _currentOutroLenCache);
  const t0 = ctx.currentTime;
  const tEnd = t0 + fadeSec;
  const teardowns = [];

  function tearAll() {
    for (const fn of teardowns) { try { fn(); } catch (_) {} }
    _restoreDirect(outDeck);
    _restoreDirect(inDeck);
  }

  if (effect === "lowpass_sweep") {
    const f = ctx.createBiquadFilter();
    f.type = "lowpass";
    f.frequency.setValueAtTime(ctx.sampleRate / 2, t0);
    f.frequency.exponentialRampToValueAtTime(250, tEnd);
    _routeThrough(outDeck, f);
    f.connect(outDeck.gain);
    teardowns.push(() => f.disconnect());
  }
  else if (effect === "highpass_sweep") {
    const f = ctx.createBiquadFilter();
    f.type = "highpass";
    f.frequency.setValueAtTime(4000, t0);
    f.frequency.exponentialRampToValueAtTime(60, tEnd);
    _routeThrough(inDeck, f);
    f.connect(inDeck.gain);
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
    // Route the wet (delay) path DIRECT to destination so the echo
    // tail survives after deck.gain has ramped to silence — that's
    // what makes it sound like a tape echo throw rather than a
    // muted dry signal.
    const delay = ctx.createDelay(2.0); delay.delayTime.value = 0.375;
    const fb = ctx.createGain(); fb.gain.value = 0.6;
    const wet = ctx.createGain();
    wet.gain.setValueAtTime(_volume * 0.85, t0);
    wet.gain.setValueAtTime(_volume * 0.85, t0 + fadeSec * 0.6);
    wet.gain.exponentialRampToValueAtTime(0.001, tEnd);
    outDeck.source.connect(delay);
    delay.connect(fb); fb.connect(delay);
    delay.connect(wet); wet.connect(ctx.destination);
    teardowns.push(() => {
      try { delay.disconnect(); fb.disconnect(); wet.disconnect(); } catch (_) {}
    });
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
    teardowns.push(() => {
      try { conv.disconnect(); wet.disconnect(); send.disconnect(); } catch (_) {}
    });
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
    teardowns.push(() => {
      try { hp.disconnect(); lp.disconnect(); drive.disconnect(); shaper.disconnect(); } catch (_) {}
    });
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
      try { delay.disconnect(); fb.disconnect(); wet.disconnect(); lfoGain.disconnect(); } catch (_) {}
    });
  }
  else if (effect === "bitcrusher") {
    // AudioWorklet-based bitcrusher: sample-rate reduction + bit-depth
    // quantisation give the authentic 8-bit-console / Atari sound.
    // Worklet module loads at AudioContext boot; if a transition fires
    // before it finishes (first ~50 ms of session) we just skip the
    // effect for that one crossfade — no WaveShaper fallback.
    if (!_workletReady.bitcrusher) {
      console.warn("bitcrusher worklet not ready; skipping effect for this crossfade");
      return tearAll;
    }
    const node = new AudioWorkletNode(ctx, "bitcrusher", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [2],
    });
    const bitsParam = node.parameters.get("bits");
    const rateParam = node.parameters.get("rateReduce");
    // Peak crush in first 50 % of fade so the user actually hears the
    // 8-bit-console sound while the deck is still loud.  Crossfade
    // ramp dominates the second half regardless.
    const peakAt = t0 + fadeSec * 0.5;
    bitsParam.setValueAtTime(12, t0);
    bitsParam.linearRampToValueAtTime(3, peakAt);
    bitsParam.setValueAtTime(3, tEnd);
    rateParam.setValueAtTime(1, t0);
    rateParam.linearRampToValueAtTime(16, peakAt);
    rateParam.setValueAtTime(16, tEnd);
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
      // Accelerate gate rate from 8 → 16 Hz over the fade for a build-up feel
      rateParam.setValueAtTime(8, t0);
      rateParam.linearRampToValueAtTime(16, tEnd);
      dutyParam.setValueAtTime(0.25, t0);
      _routeThrough(outDeck, node);
      node.connect(outDeck.gain);
      teardowns.push(() => { try { node.disconnect(); } catch (_) {} });
    } else {
      const wrapper = ctx.createGain();
      wrapper.gain.setValueAtTime(1, t0);
      let t = t0;
      let rate = 8;
      while (t < tEnd) {
        const cycle = 1 / rate;
        wrapper.gain.setValueAtTime(1, t);
        wrapper.gain.setValueAtTime(0, t + cycle * 0.25);
        t += cycle;
        rate = Math.min(16, rate * 1.05);
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
    teardowns.push(() => {
      try { lp.disconnect(); conv.disconnect(); wet.disconnect(); } catch (_) {}
    });
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
      const node = new AudioWorkletNode(ctx, "freeze");
      node.parameters.get("grainMs").setValueAtTime(150, t0);
      node.parameters.get("fadeOutSec").setValueAtTime(fadeSec, t0);
      const g = ctx.createGain();
      g.gain.setValueAtTime(_volume, t0);
      // CRITICAL: do NOT set audio.muted=true.  MediaElementSource
      // respects the element's muted flag and feeds silence into the
      // worklet — so the freeze captures silence and loops nothing.
      // Just zero deck.gain (downstream of the source tap) instead.
      _routeThrough(outDeck, node);
      node.connect(g); g.connect(ctx.destination);
      outDeck.gain.gain.cancelScheduledValues(t0);
      outDeck.gain.gain.setValueAtTime(0, t0);
      teardowns.push(() => {
        try { node.disconnect(); g.disconnect(); } catch (_) {}
      });
    }
  }
  else if (effect === "glitch") {
    // Random buffer slicing + reorder.  Same pattern as freeze: bypass
    // deck.gain so the chaotic stutter is audible.  Do NOT mute the
    // <audio> element — that silences the source feeding the worklet.
    if (_workletReady.glitch) {
      const node = new AudioWorkletNode(ctx, "glitch");
      node.parameters.get("sliceMs").setValueAtTime(80, t0);
      node.parameters.get("density").setValueAtTime(0.85, t0);
      const g = ctx.createGain();
      g.gain.setValueAtTime(_volume, t0);
      g.gain.setValueAtTime(_volume, t0 + fadeSec * 0.7);
      g.gain.linearRampToValueAtTime(0, tEnd);
      _routeThrough(outDeck, node);
      node.connect(g); g.connect(ctx.destination);
      outDeck.gain.gain.cancelScheduledValues(t0);
      outDeck.gain.gain.setValueAtTime(0, t0);
      teardowns.push(() => {
        try { node.disconnect(); g.disconnect(); } catch (_) {}
      });
    }
  }
  // -------- scratch: rapid back-and-forth sweep over short slice --------
  else if (effect === "scratch") {
    const path = outDeck.path;
    const currentT = outDeck.audio.currentTime;
    const sliceSec = 0.25;       // 250 ms — classic scratch slice
    const totalSec = Math.max(fadeSec, 2.0);
    const nPasses = 4;           // forward, reverse, forward, reverse
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
        const t1 = t0 + p * passLen;
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
        try { s.src.disconnect(); s.g.disconnect(); } catch (_) {}
      }
    });
  }
  // -------- beat_repeat: capture short slice, retrigger N times --------
  else if (effect === "beat_repeat") {
    const path = outDeck.path;
    const currentT = outDeck.audio.currentTime;
    const sliceSec = 0.25;
    const nRepeats = 8;
    const totalSec = Math.max(fadeSec, 3.0);
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
      const stride = totalSec / nRepeats;
      for (let i = 0; i < nRepeats; i++) {
        const src = ctx.createBufferSource(); src.buffer = slice;
        const g = ctx.createGain();
        const t1 = t0 + i * stride;
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
        try { s.src.disconnect(); s.g.disconnect(); } catch (_) {}
      }
    });
  }
  // -------- sidechain_pump: rhythmic 4-on-the-floor amplitude duck --------
  else if (effect === "sidechain_pump") {
    // Apply a periodic gain envelope between deck.source and deck.gain.
    // Pump rate = 120 BPM → 0.5 s period.  Full duck on the beat,
    // exponential recovery between beats.
    const pump = ctx.createGain();
    pump.gain.value = 1.0;
    const period = 60 / 120;     // 120 BPM
    const depth = 0.7;
    let t = t0;
    while (t < tEnd) {
      // Beat onset: drop to 1-depth instantly, then ramp back to 1 over
      // the period.
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
    // Build a normal IR, reverse it, convolve.  Wet path bypasses
    // deck.gain so the swell crescendos all the way to the cut.
    const sr = ctx.sampleRate;
    const irLen = Math.floor(2.0 * sr);
    const buf = ctx.createBuffer(2, irLen, sr);
    for (let ch = 0; ch < 2; ch++) {
      const data = buf.getChannelData(ch);
      // Reverse-decay envelope: starts at 0, rises to 1 at the end.
      for (let i = 0; i < irLen; i++) {
        const env = (i / irLen) ** 2;
        data[i] = (Math.random() * 2 - 1) * env * 0.4;
      }
    }
    const conv = ctx.createConvolver(); conv.buffer = buf;
    const wet = ctx.createGain();
    wet.gain.setValueAtTime(0.0, t0);
    wet.gain.linearRampToValueAtTime(_volume * 1.4, tEnd - 0.1);
    wet.gain.linearRampToValueAtTime(0.0, tEnd);
    outDeck.source.connect(conv); conv.connect(wet); wet.connect(ctx.destination);
    teardowns.push(() => {
      try { conv.disconnect(); wet.disconnect(); } catch (_) {}
    });
  }
  // -------- air_horn: synth dub-siren riser layered with the music --------
  else if (effect === "air_horn") {
    const osc = ctx.createOscillator();
    osc.type = "square";
    // 220 Hz → 880 Hz pitch sweep
    osc.frequency.setValueAtTime(220, t0);
    osc.frequency.exponentialRampToValueAtTime(880, tEnd - 0.1);
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
      try { osc.disconnect(); lp.disconnect(); g.disconnect(); } catch (_) {}
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

  return tearAll;
}

function startCrossfade(nextPath, fadeSec) {
  if (!_ctx || crossfading) return;
  if (!nextPath) return;
  crossfading = true;

  const standby = deckStandby();
  setSrcOnDeck(standby, nextPath);
  standby.gain.gain.setValueAtTime(0, _ctx.currentTime);
  playOnDeck(standby);

  const active = deckActive();
  const t0 = _ctx.currentTime;

  // Resolve + apply the chosen transition effect over the fade window.
  const fxName = _resolveTransition(_lastTransitionFx);
  console.debug("autodj transition:", fxName);
  const teardownFx = applyTransitionFx(fxName, fadeSec, active, standby);

  active.gain.gain.cancelScheduledValues(t0);
  active.gain.gain.setValueAtTime(active.gain.gain.value, t0);
  active.gain.gain.linearRampToValueAtTime(0, t0 + fadeSec);

  standby.gain.gain.cancelScheduledValues(t0);
  standby.gain.gain.setValueAtTime(0, t0);
  standby.gain.gain.linearRampToValueAtTime(_volume, t0 + fadeSec);

  suppressAdvance = true;
  fetch("/api/advance", { method: "POST" }).catch(() => {});

  setTimeout(() => {
    teardownFx();
    activeIdx ^= 1;
    crossfading = false;
    suppressAdvance = false;
    try { active.audio.pause(); } catch (_) {}
    active.audio.removeAttribute("src");
    active.path = null;
    active.audio.load();
  }, fadeSec * 1000 + 100);
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
    // Auto-skip on ANY error — bad codec, missing file, network drop.
    // Aborted (code 1) is usually triggered by us tearing down a deck, so
    // skip only when the affected deck is the active one.
    const isActive = d === deckActive();
    if ((!e || e.code !== 1) || isActive) {
      msg += " — auto-skipping.";
      fetch("/api/advance", { method: "POST" }).catch(() => {});
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
    const fadeSec = _crossfadeSecondsCache;
    if (remaining > 0 && remaining < fadeSec && _nextTrackPathCache) {
      startCrossfade(_nextTrackPathCache, fadeSec);
      return;
    }
    // Silence detector — fire the crossfade EARLY when the active deck
    // has gone quiet past the halfway mark.  Eliminates the long dead
    // air at the end of fade-out tracks (or the silent run-in at the
    // start of the next track, since prefetch loaded it already).
    if (_silenceTriggerEnabled
        && d.analyser && _nextTrackPathCache
        && d.audio.currentTime > dur * 0.5) {
      const buf = new Float32Array(d.analyser.fftSize);
      d.analyser.getFloatTimeDomainData(buf);
      let sumSq = 0;
      for (let i = 0; i < buf.length; i++) sumSq += buf[i] * buf[i];
      const rms = Math.sqrt(sumSq / buf.length);
      // RMS threshold ≈ −60 dBFS — anything quieter is functionally silence.
      if (rms < 0.001) {
        d._silenceMs += 250;   // timeupdate fires ~4 Hz
        if (d._silenceMs >= 600) {
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
let _nextTrackPathCache = null;
let _prefetchEnabled = true;
let _silenceTriggerEnabled = true;

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
  // Outgoing track's outro length drives the per-effect duration table
  // in `applyTransitionFx`.  Null when the track hasn't been DJ-meta
  // analysed yet — falls back to the static minimums.
  _currentOutroLenCache = (s.current_track && typeof s.current_track.outro_len === "number")
    ? s.current_track.outro_len : null;
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
      setSrcOnDeck(active, path);
      playOnDeck(active);
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
        deckActive().audio.pause();
      }
    } else {
      if (deckActive().audio.paused && playbackEnabled) {
        playOnDeck(deckActive());
      }
      if (!crossfading) deckActive().gain.gain.value = _volume;
    }
  }
}

// ----------------------------------------------------------------
// Cover art
// ----------------------------------------------------------------

function loadCoverArt(trackPath) {
  // Probe the image first; only swap into the visible <img> on success so
  // failed loads don't display a broken-image icon.
  const probe = new Image();
  probe.onload = () => {
    coverArt.src = probe.src;
    coverArt.hidden = false;
  };
  probe.onerror = () => {
    coverArt.hidden = true;
    coverArt.removeAttribute("src");
  };
  probe.src = `/api/art?path=${encodeURIComponent(trackPath)}`;
}

// ----------------------------------------------------------------
// Lyrics
// ----------------------------------------------------------------

async function loadLyrics() {
  try {
    const res = await fetch("/api/lyrics");
    const data = await res.json();
    cachedLyrics = data.lyrics || [];
  } catch (_) {
    cachedLyrics = [];
  }
  renderLyricsList();
}

function renderLyricsList() {
  if (cachedLyrics.length === 0) {
    lyricsCard.hidden = true;
    lyricsList.innerHTML = "";
    return;
  }
  lyricsCard.hidden = false;
  lyricsList.innerHTML = cachedLyrics
    .map((ll, i) => `<li data-i="${i}">${escHtml(ll.text || "\u266b")}</li>`)
    .join("");
}

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


function applyLyricsState(s) {
  // Plain (unsynced) beets lyrics fallback — show as a single block when
  // we have no timestamped .lrc list.  Updated on every track change.
  if (!s.has_lyrics && s.lyrics_plain) {
    if (cachedLyrics.length || lyricsList.querySelector(".plain-lyrics") === null) {
      cachedLyrics = [];
      lyricsCard.hidden = false;
      lyricsList.innerHTML = `<li class="plain-lyrics" style="white-space:pre-wrap;list-style:none;padding-left:0">${escHtml(s.lyrics_plain)}</li>`;
    }
    lastLyricIndex = null;
    return;
  }
  if (!s.has_lyrics) {
    if (cachedLyrics.length || lyricsList.children.length) {
      cachedLyrics = [];
      renderLyricsList();
    }
    lastLyricIndex = null;
    return;
  }
  const idx = s.lyric_index;
  if (idx === lastLyricIndex) return;
  lastLyricIndex = idx;

  const items = lyricsList.querySelectorAll("li");
  items.forEach((li) => {
    li.classList.remove("active");
    li.removeAttribute("aria-current");
  });
  if (idx !== null && idx >= 0 && idx < items.length) {
    const li = items[idx];
    li.classList.add("active");
    li.setAttribute("aria-current", "true");
    li.scrollIntoView({ behavior: "smooth", block: "center" });
    if (s.lyric_text) {
      lyricAnnounce.textContent = s.lyric_text;
    }
  }
}

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
    btnPause.innerHTML = isPaused
      ? '<span aria-hidden="true">\u25B6</span> Resume'
      : '<span aria-hidden="true">\u23F8</span> Pause';
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
    startCrossfade(_nextTrackPathCache, _crossfadeSecondsCache);
  } else {
    await fetch("/api/skip", { method: "POST" });
  }
  setTimeout(() => { btnSkip.disabled = false; }, 800);
});

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
  clearTimeout(volAnnounceTimer);
  volAnnounceTimer = setTimeout(() => {
    if (volAnnounce) volAnnounce.textContent = `Volume ${val}%.`;
  }, 250);
});

// ----------------------------------------------------------------
// Media Session API — wires up OS media keys, lock-screen art,
// and notification-shade transport on Chromium / WebKit / Firefox.
// ----------------------------------------------------------------

function updateMediaSession(s) {
  if (!("mediaSession" in navigator)) return;
  const t = s.current_track;
  if (!t) {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
    return;
  }
  navigator.mediaSession.metadata = new MediaMetadata({
    title:  t.title || "",
    artist: t.artist || "",
    album:  t.album || "",
    artwork: [{
      src: "/api/art?path=" + encodeURIComponent(t.path),
      sizes: "512x512",
      type: "image/jpeg",
    }],
  });
  navigator.mediaSession.playbackState = s.is_paused ? "paused" : "playing";
  if (s.duration && s.elapsed != null) {
    try {
      navigator.mediaSession.setPositionState({
        duration: s.duration,
        position: Math.min(s.elapsed, s.duration),
        playbackRate: 1.0,
      });
    } catch (_) { /* not supported on every browser */ }
  }
}

if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("play", () => {
    if (!playbackEnabled && _lastBrowserPlayback) unlockAndPlay().catch(()=>{});
    else fetch("/api/pause", { method: "POST" });
  });
  navigator.mediaSession.setActionHandler("pause", () => {
    fetch("/api/pause", { method: "POST" });
  });
  navigator.mediaSession.setActionHandler("nexttrack", () => {
    fetch("/api/skip", { method: "POST" });
  });
  navigator.mediaSession.setActionHandler("previoustrack", null);
}

// ----------------------------------------------------------------
// Keyboard shortcuts (only fire when no input/select/textarea focused
// so the Settings card and Search bar remain typeable).
// ----------------------------------------------------------------

document.addEventListener("keydown", (e) => {
  const tgt = e.target;
  const tag = (tgt && tgt.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  // Tablist owns its own arrow-key navigation; don't let the global
  // shortcut handler steal Up/Down to nudge volume when focus is on
  // a tab.
  if (tgt && tgt.getAttribute && tgt.getAttribute("role") === "tab") return;

  switch (e.key) {
    case " ":
    case "Spacebar":
      e.preventDefault();
      btnPause.click();
      break;
    case "n":
    case "N":
      e.preventDefault();
      btnSkip.click();
      break;
    case "m":
    case "M":
      e.preventDefault();
      btnMute.click();
      break;
    case "ArrowUp":
      e.preventDefault();
      volSlider.value = Math.min(100, parseInt(volSlider.value, 10) + 5);
      volSlider.dispatchEvent(new Event("input"));
      break;
    case "ArrowDown":
      e.preventDefault();
      volSlider.value = Math.max(0, parseInt(volSlider.value, 10) - 5);
      volSlider.dispatchEvent(new Event("input"));
      break;
  }
});

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
    } else {
      await fetch("/api/queue/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      queueAnnounce.textContent = `Added ${name} to queue.`;
    }
  } finally {
    btn.disabled = false;
    btn.focus();
  }
});

// ----------------------------------------------------------------
// Library tools — index / enrich / prune / stats from the web UI.
// All controls are no-ops on pages that don't include the library
// section markup, so this code is safe to run unconditionally.
// ----------------------------------------------------------------

const _libRunIndex   = document.getElementById("lib-run-index");
const _libRunEnrich  = document.getElementById("lib-run-enrich");
const _libRunPrune   = document.getElementById("lib-run-prune");
const _libRunStats   = document.getElementById("lib-run-stats");
const _libRunStop    = document.getElementById("lib-run-stop");
const _libIndexLimit = document.getElementById("lib-index-limit");
const _libStatsRefresh = document.getElementById("lib-stats-refresh");
const _libLog        = document.getElementById("library-log");
const _libJobStatus  = document.getElementById("lib-job-status");
const _libStatCount     = document.getElementById("lib-stat-count");
const _libStatAvgBpm    = document.getElementById("lib-stat-avg-bpm");
const _libStatWithKey   = document.getElementById("lib-stat-with-key");
const _libStatWithGenre = document.getElementById("lib-stat-with-genre");
const _libStatWithEnergy= document.getElementById("lib-stat-with-energy");

let _lastLibLogKey = "";

async function _libRun(name, args = []) {
  try {
    const r = await fetch("/api/library/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, args }),
    });
    if (!r.ok) {
      const txt = await r.text();
      _libJobStatus.textContent = `Could not start ${name}: ${txt}`;
      return;
    }
    _libJobStatus.textContent = `${name} started…`;
  } catch (err) {
    _libJobStatus.textContent = `Error starting ${name}: ${err.message || err}`;
  }
}

if (_libRunIndex) {
  _libRunIndex.addEventListener("click", () => {
    const limit = parseInt(_libIndexLimit.value, 10);
    const args = !isNaN(limit) && limit > 0 ? ["--limit", String(limit)] : [];
    _libRun("index", args);
  });
}
if (_libRunEnrich) _libRunEnrich.addEventListener("click", () => _libRun("enrich"));
if (_libRunPrune)  _libRunPrune.addEventListener("click", () => _libRun("prune"));
if (_libRunStats)  _libRunStats.addEventListener("click", () => _libRun("stats"));
if (_libRunStop)   _libRunStop.addEventListener("click", async () => {
  try { await fetch("/api/library/stop", { method: "POST" }); } catch (_) {}
});

async function _refreshLibStats() {
  if (!_libStatCount) return;
  try {
    const r = await fetch("/api/library/stats");
    if (!r.ok) return;
    const s = await r.json();
    _libStatCount.textContent     = s.track_count;
    _libStatAvgBpm.textContent    = s.average_bpm
      ? `${s.average_bpm} (${s.tracks_with_bpm} tracks)` : "—";
    _libStatWithKey.textContent    = s.tracks_with_key;
    _libStatWithGenre.textContent  = s.tracks_with_genre;
    _libStatWithEnergy.textContent = s.tracks_with_energy;
  } catch (_) {}
}
if (_libStatsRefresh) _libStatsRefresh.addEventListener("click", _refreshLibStats);
if (_libStatCount) _refreshLibStats();

function applyLibraryJobState(s) {
  const job = s && s.library_job;
  if (!job || !_libLog) return;
  // Status line
  if (_libJobStatus) {
    if (job.running) {
      _libJobStatus.textContent =
        `${job.name} running for ${job.elapsed_seconds}s…`;
    } else if (job.exit_code != null) {
      const ok = job.exit_code === 0;
      _libJobStatus.textContent = ok
        ? `${job.name} finished cleanly in ${job.elapsed_seconds}s.`
        : `${job.name} exited with code ${job.exit_code} after ${job.elapsed_seconds}s.`;
    } else if (!job.name) {
      _libJobStatus.textContent = "Idle.";
    }
  }
  // Append-only log render — only re-render when payload changed.
  const lines = job.lines || [];
  const key = lines.length + "@" + (lines[lines.length - 1] || "");
  if (key === _lastLibLogKey) return;
  _lastLibLogKey = key;
  if (lines.length === 0) {
    _libLog.innerHTML = '<em style="color:var(--text-dim)">No job has run yet.</em>';
  } else {
    _libLog.textContent = lines.join("\n");
    _libLog.scrollTop = _libLog.scrollHeight;
  }
}

// ----------------------------------------------------------------
// Section router — single-page nav across the four views.  Audio
// graph + crossfade state survive every navigation because we never
// reload the document; only `hidden` toggles on each <section>.
// ----------------------------------------------------------------

const _VIEW_NAMES = ["now", "queue", "settings", "library"];
const _viewSections = new Map();
const _viewLinks = new Map();
let _viewInitialised = false;

function _initViewRouter() {
  for (const name of _VIEW_NAMES) {
    const sec = document.querySelector(`section[data-view="${name}"]`);
    const lnk = document.querySelector(`#view-nav [role="tab"][data-view="${name}"]`);
    if (sec) _viewSections.set(name, sec);
    if (lnk) _viewLinks.set(name, lnk);
  }
  for (const lnk of _viewLinks.values()) {
    lnk.addEventListener("click", () => {
      const target = lnk.dataset.view;
      if (location.hash !== "#" + target) location.hash = target;
      else _applyView(target, /*userInitiated=*/true);
    });
    // Tablist arrow / Home / End navigation per ARIA APG.  Activates
    // the focused tab on move (automatic activation) — panel swap is
    // just a `hidden` toggle, so no perf reason to use manual.
    lnk.addEventListener("keydown", _onTabKeydown);
  }
  window.addEventListener("hashchange", () => {
    const view = (location.hash || "#now").replace(/^#/, "");
    _applyView(_VIEW_NAMES.includes(view) ? view : "now",
               /*userInitiated=*/true);
  });
  // Initial paint — no focus stealing on first load.
  const initial = (location.hash || "#now").replace(/^#/, "");
  _applyView(_VIEW_NAMES.includes(initial) ? initial : "now",
             /*userInitiated=*/false);
  _viewInitialised = true;
}

function _onTabKeydown(e) {
  const order = _VIEW_NAMES.filter(n => _viewLinks.has(n));
  const cur = e.currentTarget.dataset.view;
  let idx = order.indexOf(cur);
  let nextName = null;
  switch (e.key) {
    case "ArrowRight":
    case "ArrowDown":
      nextName = order[(idx + 1) % order.length];
      break;
    case "ArrowLeft":
    case "ArrowUp":
      nextName = order[(idx - 1 + order.length) % order.length];
      break;
    case "Home":
      nextName = order[0];
      break;
    case "End":
      nextName = order[order.length - 1];
      break;
    default:
      return;
  }
  e.preventDefault();
  if (location.hash !== "#" + nextName) location.hash = nextName;
  else _applyView(nextName, /*userInitiated=*/true);
  const nextTab = _viewLinks.get(nextName);
  if (nextTab) nextTab.focus();
}

function _applyView(name, userInitiated) {
  for (const [k, sec] of _viewSections) {
    if (k === name) sec.removeAttribute("hidden");
    else sec.setAttribute("hidden", "");
  }
  for (const [k, lnk] of _viewLinks) {
    const selected = k === name;
    lnk.setAttribute("aria-selected", selected ? "true" : "false");
    // Roving tabindex — only the active tab is in the document tab
    // order; the others are reachable via arrow keys.
    lnk.tabIndex = selected ? 0 : -1;
  }
  // SR re-announce: focus the heading of the freshly-revealed section
  // so AT users hear "Now Playing, heading level 2" on every switch
  // initiated by activation (click / Enter).  Arrow-key navigation
  // moves focus to the new tab itself instead — handled by the caller.
  if (userInitiated) {
    const sec = _viewSections.get(name);
    const tab = _viewLinks.get(name);
    // If a tab is currently focused (arrow-key nav), don't steal focus
    // away to the heading — that would defeat roving tabindex.
    if (sec && document.activeElement !== tab) {
      const heading = sec.querySelector("h2");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: false });
      }
    }
  }
}

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
