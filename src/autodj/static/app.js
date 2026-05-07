// Pure helpers extracted to ./modules/dom-helpers.js.  Aliased back to
// the historical underscore-prefixed names so the rest of this file
// keeps working without a sweep.
import {
  isDebug,
  dbg,
  fmtTime,
  fmtTrack,
  escHtml,
  isTypingTarget,
} from "./modules/dom-helpers.js";

if (isDebug()) {
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
  if (typeof _bumpLinerTrackCount === "function") bumpLinerTrackCount(s);

  // Now Playing
  const trackKey   = s.current_track ? s.current_track.path : null;
  const trackLabel = fmtTrack(s.current_track);

  if (trackKey !== lastTrackKey) {
    // Track changed -- update aria-live region so screen readers
    // announce it.  Include BPM in the same announcement as the title
    // so NVDA reads "<artist> -- <title>, 128 BPM" in one breath.  Key
    // is already announced via #badges-announce 800 ms later
    // (badges.js); doing both here would double-speak it.
    const _bpmTail = s.current_track && s.current_track.bpm
      ? `, ${Math.round(s.current_track.bpm)} BPM`
      : "";
    npAnnounce.textContent = trackLabel + _bpmTail;
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
      loadLyrics(_lyricEls);
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

  // Meta line: album · Key · BPM.  Key + BPM render unconditionally
  // (with "unknown" placeholder when the track has no detected value)
  // so the line is never empty and users always know where to look
  // for those fields.  Album is omitted entirely when absent because
  // an "album: unknown" placeholder is noisier than useful.
  const parts = [];
  if (s.current_track) {
    const t = s.current_track;
    if (t.album) parts.push(t.album);
    parts.push(`Key ${t.camelot && t.camelot !== "--" ? t.camelot : "unknown"}`);
    parts.push(t.bpm ? `${Math.round(t.bpm)} BPM` : "BPM unknown");
  }
  // npMeta is the screen-reader-reachable static text for the
  // now-playing card -- the visual badges row is aria-hidden, and the
  // live-region announce only fires on track change.  Including key +
  // BPM here lets NVDA users query the current values by re-reading
  // the section any time, not only when the track flips.
  npMeta.textContent = parts.join(" \u00b7 ");

  // Badges + announce on track change.  Module needs the els bag and
  // a couple of dispatcher-owned values (lastTrackKey, renderCueStrip).
  applyBadges(s, { badgesRow, badgesAnnounce }, { lastTrackKey, renderCueStrip });

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
  setLastBrowserPlayback(s.browser_playback);

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
  applyLyricsState(s, _lyricEls);

  // Why this track? \u2014 refresh only on track change to avoid pointless WS churn
  applyWhyState(s);

  // Library job log + status
  applyLibraryJobState(s, _libEls);

  // Queue \u2014 render if changed
  applyQueueState(s.queue || [], _queueEls);

  // Browser-side audio (when server runs headless / no_playback)
  applyBrowserPlaybackState(s);

  // OS media-keys / lock-screen integration
  updateMediaSession(s);

  // Mirror settings to the form.
  if (s.settings) applySettingsState(s.settings, _settingsEls());
}

// ----------------------------------------------------------------
// Settings card — mirror of CLI flags
// ----------------------------------------------------------------

// Settings panel sync moved to ./modules/settings-panel.js.
import {
  applySettingsState,
  postSettings as _postSettingsModule,
} from "./modules/settings-panel.js";

const _settingsEls = () => ({
  presetSelect, transitionSelect, harmonicMode,
  djBeatmatch, djPhraseAlign, djOutroIntro,
  pbEqDuck, pbSmartShuffle, pbPureShuffle, pbShowLyrics, pbAnchorSeed,
  pbReplayGain,
  pbDaypart, pbMoodArc, pbMoodArcHours, pbImportCues,
  pbBeatSyncFx, pbKeySyncFx, pbBeatmatchSkip,
  pbTransitionMode, pbCrossfade,
  bpmLo, bpmHi,
  discEnabled, discEvery,
});

function postSettings(url, body) {
  return _postSettingsModule(url, body, { settingsStatus });
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

// Badges moved to ./modules/badges.js.
import { applyBadges } from "./modules/badges.js";

// ----------------------------------------------------------------
// Cue strip — decorative markers on the progress bar.
// Sighted users see colored ticks; AT users get the summary above.
// ----------------------------------------------------------------

// Cue strip rendering moved to ./modules/cues.js.
import {
  renderCueStrip as _renderCueStripModule,
  summariseCues,
} from "./modules/cues.js";

const _cueStrip = document.getElementById("cue-strip");
function renderCueStrip(track) { _renderCueStripModule(_cueStrip, track); }

// Camelot wheel rendering moved to ./modules/camelot-wheel.js.
import { applyCamelotWheel as _applyCamelotWheelModule } from "./modules/camelot-wheel.js";

const _camelotSectors = document.getElementById("camelot-sectors");
const _camelotLabels  = document.getElementById("camelot-labels");

function applyCamelotWheel(currentCell, harmonicMode) {
  _applyCamelotWheelModule(currentCell, harmonicMode, {
    sectorsEl: _camelotSectors,
    labelsEl:  _camelotLabels,
  });
}

// Audio engine extracted to ./modules/audio-engine.js.  Named imports
// for the bindings (`_ctx`, `decks`, ...) keep ES module live-binding
// semantics so closures inside this file see updates without explicit
// accessors.
import {
  setVolume, applyBrowserPlaybackState, startCrossfade, stopAllDecks,
  applyEqState, loadCoverArt, applyTransitionFx, resetTrackCaches,
  ensureAudioGraph, unlockAndPlay,
  deckActive, deckStandby, setSrcOnDeck, playOnDeck,
  postEq, eqValueLabel,
  _ctx, decks, activeIdx, _volume, _lastBrowserPlayback, playbackEnabled,
  _outBpmCache, _inBpmCache,
  _crossfadeSecondsCache, _nextTrackPathCache,
  _beatmatchOnSkip, crossfading,
  setLastBrowserPlayback,
  setApplyState,
} from "./modules/audio-engine.js";

// Register applyState with the audio engine so /api/repick-next +
// unlockAndPlay can refresh the UI without the engine reaching back
// into app.js's scope.  Module-top so it lands before any user gesture
// can invoke unlockAndPlay.
setApplyState(applyState);



// Lyrics rendering moved to ./modules/lyrics.js.
import {
  loadLyrics, applyLyricsState, renderLyricsList,
  resetLyricState,
  getCachedLyrics,
} from "./modules/lyrics.js";

const _lyricEls = { lyricsCard, lyricsList, lyricAnnounce };

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




// Queue (render + Up/Down/Remove buttons) moved to ./modules/queue.js.
import {
  applyQueueState, installQueueButtons,
} from "./modules/queue.js";

const _queueEls = { queueList, queueCount, queueAnnounce };
installQueueButtons(_queueEls);

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
    resetTrackCaches();
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
      dbg("beatmatch-on-skip: ratio=", clamped.toFixed(3),
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
  dbg("seek ->", (frac * 100).toFixed(1) + "%", "of", dur.toFixed(1), "s");
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
  btnShortcuts.addEventListener("click", () => toggleShortcutsModal());
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
import { clearLiveRegionLater } from "./modules/live-region.js";
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
      clearLiveRegionLater(volAnnounce);
    }
  }, 250);
});

// ----------------------------------------------------------------
// Keyboard shortcuts moved to ./modules/hotkeys.js.  Wired below
// after the relevant DOM refs are populated -- see _hotkeysReady().
import {
  installHotkeys,
  toggleShortcutsModal,
} from "./modules/hotkeys.js";

function _wireHotkeysWhenReady() {
  // btnShuffle is declared further down the file; defer wiring to the
  // next tick so its const initialiser has run.
  setTimeout(() => {
    installHotkeys({
      btnPause:   typeof btnPause   !== "undefined" ? btnPause   : null,
      btnSkip:    typeof btnSkip    !== "undefined" ? btnSkip    : null,
      btnShuffle: typeof btnShuffle !== "undefined" ? btnShuffle : null,
      btnMute:    typeof btnMute    !== "undefined" ? btnMute    : null,
      volSlider:  typeof volSlider  !== "undefined" ? volSlider  : null,
    });
  }, 0);
}
_wireHotkeysWhenReady();

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

// Search moved to ./modules/search.js.
import { installSearch } from "./modules/search.js";
installSearch({
  searchInput,
  btnSearch,
  searchResults,
  searchCount,
  queueAnnounce,
});

// ----------------------------------------------------------------
// Library tools panel moved to ./modules/library-jobs.js.
import {
  installLibraryJobs,
  applyLibraryJobState,
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

// Tab router moved to ./modules/tabs.js.
import { initViewRouter } from "./modules/tabs.js";

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initViewRouter, { once: true });
} else {
  initViewRouter();
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
  applyShowWhen,
  installShowWhenListener,
} from "./modules/show-when.js";
installShowWhenListener();
// Apply once at load + after every state push (server may flip a
// checkbox via WS without the user touching it).
applyShowWhen();

// Voice liners moved to ./modules/liners.js.
import { installLiners, bumpLinerTrackCount } from "./modules/liners.js";

const _linerEls = {
  lnEnabled:       document.getElementById("ln-enabled"),
  lnEveryN:        document.getElementById("ln-every-n"),
  lnEveryMin:      document.getElementById("ln-every-min"),
  lnRandMin:       document.getElementById("ln-rand-min"),
  lnRandMax:       document.getElementById("ln-rand-max"),
  lnPickMode:      document.getElementById("ln-pick-mode"),
  lnDuckDb:        document.getElementById("ln-duck-db"),
  lnTestBtn:       document.getElementById("ln-test"),
  lnFolderDisplay: document.getElementById("ln-folder-display"),
  lnFileList:      document.getElementById("ln-file-list"),
  lnUpload:        document.getElementById("ln-upload"),
  lnUploadSubmit:  document.getElementById("ln-upload-submit"),
  lnStatus:        document.getElementById("ln-status"),
};

// Audio dependencies are still owned by the unmigrated audio engine
// further down this file -- inject closures that capture the current
// values at call time so the module never holds a stale ref.
installLiners(_linerEls, {
  postSettings: (url, body) => postSettings(url, body),
  canPlay:      () => !!_ctx && !!_lastBrowserPlayback,
  playLiner: async (arrayBuf, duckDb) => {
    if (!_ctx) return false;
    const audioBuf = await _ctx.decodeAudioData(arrayBuf);
    const src  = _ctx.createBufferSource();
    src.buffer = audioBuf;
    const gain = _ctx.createGain();
    gain.gain.value = 1.0;
    src.connect(gain);
    gain.connect(_ctx.destination);
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
    return true;
  },
});
