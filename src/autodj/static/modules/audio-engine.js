// AutoDJ audio engine: AudioContext, two-deck crossfade, EQ + filters,
// 35-effect transition library, beat-sync helper (_BS), reverb IR cache,
// cover-art probe.  Phase 2 of the app.js -> per-concern split: bundles
// the entire client-side audio pipeline behind a single import surface.
//
// Live exports: top-level `let` bindings (_ctx, decks, _outBpmCache, ...)
// are exposed as ES-module live bindings so consumers (transport
// handlers, liners scheduler, websocket reset) see the latest value
// without explicit accessors.  Reassignment must happen inside this
// module; resetTrackCaches() handles the WebSocket-reconnect reset
// that previously inlined three direct assignments.

import { dbg } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";

// DOM refs are looked up here so app.js doesn't have to inject them.
const eqLow      = document.getElementById("eq-low");
const eqMid      = document.getElementById("eq-mid");
const eqHigh     = document.getElementById("eq-high");
const eqLowVal   = document.getElementById("eq-low-value");
const eqMidVal   = document.getElementById("eq-mid-value");
const eqHighVal  = document.getElementById("eq-high-value");
const eqAnnounce = document.getElementById("eq-announce");
const btnEqReset = document.getElementById("btn-eq-reset");
const volSlider  = document.getElementById("vol");
const coverArt   = document.getElementById("cover-art");
const npAnnounce = document.getElementById("now-playing-announce");

// ----------------------------------------------------------------
// 3-band EQ
// ----------------------------------------------------------------

export function eqValueLabel(v100) {
  // v100: 0–200 with 100 = unity.  Return human label + dB.
  if (v100 === 0) return "Kill";
  if (v100 === 100) return "Unity";
  // dB = 20 * log10(v/100)
  const db = 20 * Math.log10(v100 / 100);
  const sign = db >= 0 ? "+" : "";
  return `${sign}${db.toFixed(1)} dB`;
}

export function applyEqState(eq) {
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
export function postEq() {
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
  clearLiveRegionLater(eqAnnounce);
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

export let playbackEnabled = false;
export let suppressAdvance = false;   // gate spurious advance posts during programmatic actions
export let _lastBrowserPlayback = false;  // mirror of state.browser_playback for click handlers

// Dependency-injected state applier.  app.js defines applyState (it
// orchestrates badges / lyrics / queue / settings panels which live in
// app.js's closure) and registers it via setApplyState() at startup.
// Audio engine never imports app.js (would be a circular import) and
// never references the bare identifier (would be a ReferenceError on
// the /api/repick-next + unlockAndPlay paths).
let _applyState = null;
export function setApplyState(fn) { _applyState = fn; }
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent || "");

if (isIOS) {
  // iOS ignores HTMLMediaElement.volume — hide the volume control
  // rather than letting users drag something that does nothing.
  const volRow = volSlider.closest(".volume-row");
  if (volRow) volRow.style.display = "none";
}

const audioEl  = document.getElementById("browser-player");
const audioElB = document.getElementById("browser-player-b");

// Web Audio graph — built lazily on first user gesture (browsers
// require a user activation to construct an AudioContext).
export let _ctx = null;
export const decks = [
  { audio: audioEl,  source: null, gain: null, path: null, busy: false },
  { audio: audioElB, source: null, gain: null, path: null, busy: false },
];
export let activeIdx = 0;
export let crossfading = false;

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

export function ensureAudioGraph() {
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

export function deckActive() { return decks[activeIdx]; }
export function deckStandby() { return decks[activeIdx ^ 1]; }

export function stopAllDecks() {
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

export function setSrcOnDeck(deck, path) {
  if (deck.path === path) return;
  deck.path = path;
  deck.audio.src = "/api/audio?path=" + encodeURIComponent(path);
}

export function playOnDeck(deck) {
  return deck.audio.play().catch((err) => {
    console.warn("deck.play failed:", err);
  });
}

export function setVolume(linear) {
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
export let _volume = 1;

// ----------------------------------------------------------------
// Browser-side transition effects (Web Audio API).  Each effect builds
// a small node graph between its target deck's source and gain, runs
// for fadeSec, and disconnects on teardown.
// ----------------------------------------------------------------

export let _lastTransitionFx = "none";
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
const DECODE_FETCH_TIMEOUT_MS = 8000;

async function _decodeFor(path) {
  if (_bufferCache.has(path)) return _bufferCache.get(path);
  const url = "/api/audio?path=" + encodeURIComponent(path);
  const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), DECODE_FETCH_TIMEOUT_MS) : null;
  let arr;
  try {
    const opts = ctrl ? { signal: ctrl.signal } : undefined;
    const resp = await fetch(url, opts);
    if (!resp.ok) throw new Error(`audio fetch ${resp.status}`);
    arr = await resp.arrayBuffer();
  } finally {
    if (timer) clearTimeout(timer);
  }
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
export const _FX_BAR_TABLE = {
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
export const _BS = {
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
  const target = outroLen * frac;
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

export function applyTransitionFx(effect, fadeSec, outDeck, inDeck) {
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
export function startCrossfade(nextPath, fadeSec, serverLed = false) {
  if (!_ctx || crossfading) return;
  if (!nextPath) return;
  crossfading = true;
  dbg("crossfade ->", nextPath, "| fade=", fadeSec.toFixed(2), "s",
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
  if (_fadeInSecondsCache <= 0) {
    standby.gain.gain.setValueAtTime(_volume, t0);
  } else {
    const fadeInDur = Math.min(_fadeInSecondsCache, effectDur);
    standby.gain.gain.linearRampToValueAtTime(_volume, t0 + fadeInDur);
  }

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
        if (state && _applyState) _applyState(state);
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
export let _crossfadeSecondsCache = 3.0;
export let _fadeInSecondsCache = 3.0;
export let _currentOutroLenCache = null;
export let _currentOutroStartCache = null;   // active deck's outro_start_s
export let _nextTrackIntroEndCache = null;   // incoming track's intro_end_s
export let _nextTrackPathCache = null;
let _transitionMode = "full_intro_outro";
export let _prefetchEnabled = true;
export let _silenceTriggerEnabled = true;

// --- Beat- + key-sync transition FX caches.  Populated from
// applyBrowserPlaybackState whenever a state push lands; consumed by
// _BS.refresh() at the start of every crossfade so per-effect timing
// can snap to downbeats and oscillator-FX can tune to root notes. ---
export let _beatSyncEnabled = true;
export let _keySyncEnabled = true;
export let _beatmatchOnSkip = false;
export let _outBpmCache = 0;
export let _inBpmCache = 0;
export let _outDownbeatsCache = [];
export let _inDownbeatsCache = [];
export let _outKeyHzCache = null;
export let _inKeyHzCache = null;

// _libraryWarned moved into ./modules/settings-panel.js.

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
export async function unlockAndPlay() {
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
  if (_applyState) _applyState(state);    // refresh UI from /api/status
}

export function applyBrowserPlaybackState(s) {
  // When the server has its own audio output, the browser stays out of
  // the way (no decks fired up, no crossfade, no advance posts).
  if (!s.browser_playback) return;

  _crossfadeSecondsCache = (s.settings && s.settings.playback &&
    s.settings.playback.crossfade_seconds) || 3.0;
  _fadeInSecondsCache = (s.settings && s.settings.playback &&
    typeof s.settings.playback.fade_in_seconds === "number")
    ? s.settings.playback.fade_in_seconds : 3.0;
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

export function loadCoverArt(trackPath) {
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


// Reset cached track markers + clear deck audio.  Called by the
// WebSocket onclose handler to wipe stale intro / outro markers so
// the next reconnect doesn't trigger a crossfade on a marker that
// no longer matches the currently-loaded track.
export function resetTrackCaches() {
  _currentOutroLenCache    = null;
  _currentOutroStartCache  = null;
  _nextTrackIntroEndCache  = null;
}

// Setter for `_lastBrowserPlayback` so app.js (which mirrors this from
// the WS state push) can update the binding without violating the ES
// module import-reassignment rule.
export function setLastBrowserPlayback(v) {
  _lastBrowserPlayback = !!v;
}
