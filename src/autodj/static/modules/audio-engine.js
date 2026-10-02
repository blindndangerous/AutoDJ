// AutoDJ audio engine: AudioContext, two-deck crossfade, EQ + filters,
// 35-effect transition library, beat-sync helper (_BS), reverb IR cache,
// cover-art probe.  Phase 2 of the app.js -> per-concern split: bundles
// the entire client-side audio pipeline behind a single import surface.
//
// Live exports: top-level `let` bindings (_ctx, decks, _outBpmCache, ...)
// are exposed as ES-module live bindings so consumers (transport
// handlers, liners scheduler, websocket reset) see the latest value
// without explicit accessors.  Reassignment must happen inside this
// module; resetTransitionCaches() and resetTrackCaches() reset them
// after the session expires.  A dropped link resets nothing: the decks
// play on, and the first state after the reconnect refreshes the caches.

import { dbg } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  isServerUnreachable,
  makeSingleFlight,
  postJsonBestEffort,
  probeResource,
  requestBinary,
  requestJson,
  requestJsonBestEffort,
} from "./api-client.js";

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

// The EQ change this page is still saving.  A push that left the server
// before the save carries the old bands and threw the slider (and the
// arrow press after it) back; a fixed 600 ms guard still lost on a slow
// round trip.  Set by a slider or Reset, it holds pushes off while the
// save waits out its debounce and while it is in flight, and after the
// reply until a push carries the saved bands.  A failed save clears it,
// so the next push puts the sliders back where the server has them.
// ECHO_WAIT_MS is only a backstop for an echo that never comes.
let _eqSave = null;   // { sent: [low, mid, high] | null, answeredAt }
const ECHO_WAIT_MS = 5000;

function eqBands(eq) {
  return [Math.round(eq.low * 100), Math.round(eq.mid * 100), Math.round(eq.high * 100)];
}

function eqSaveHolds(eq) {
  const save = _eqSave;
  if (!save) return false;
  if (save.sent === null) return true;
  const pushed = eqBands(eq);
  if (pushed.every((value, i) => value === save.sent[i])
      || Date.now() - save.answeredAt > ECHO_WAIT_MS) {
    _eqSave = null;
    return false;
  }
  return true;
}

export function applyEqState(eq) {
  if (!eq) return;
  if (eqSaveHolds(eq)) return;
  // Server gives 0.0–2.0 floats; convert to 0–200 ints for the slider.
  const [low, mid, high] = eqBands(eq);
  const map = [
    [eqLow, eqLowVal, low],
    [eqMid, eqMidVal, mid],
    [eqHigh, eqHighVal, high],
  ];
  for (const [slider, span, value] of map) {
    if (parseInt(slider.value, 10) !== value) {
      slider.value = value;
    }
    const label = eqValueLabel(value);
    if (slider.getAttribute("aria-valuetext") !== label) {
      slider.setAttribute("aria-valuetext", label);
    }
    if (span.textContent !== label) span.textContent = label;
  }
}

let eqDebounceTimer = null;
// Engine failures go to the shared status region, never into
// #now-playing-title: that node is the visible track title, so writing
// an error there replaced the song name on screen and bypassed the
// visible status line.  `force` is for failures of something the user
// just did, which must be reported again if it fails again.
function announceEngineError(message, { force = false } = {}) {
  announceStatus(document.getElementById("sr-status"), message,
    { dwellMs: 8000, force, tone: "error" });
}

function announceRequestError(errorValue) {
  announceEngineError(errorValue.message || String(errorValue));
}

export function postEq() {
  const save = _eqSave = { sent: null, answeredAt: 0 };
  clearTimeout(eqDebounceTimer);
  eqDebounceTimer = setTimeout(() => {
    const sent = [eqLow, eqMid, eqHigh].map((slider) => parseInt(slider.value, 10));
    void postJsonBestEffort("/api/eq", {
        low:  sent[0] / 100,
        mid:  sent[1] / 100,
        high: sent[2] / 100,
    }, announceRequestError).then((reply) => {
      if (_eqSave !== save) return;   // a newer change owns the sliders
      if (reply === null) {
        _eqSave = null;
        return;
      }
      save.sent = sent;
      save.answeredAt = Date.now();
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
  // Forced: a second Reset within the dwell must be heard again.
  announceStatus(eqAnnounce, "EQ reset to unity.", { dwellMs: 3000, force: true, mirror: false });
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
let suppressAdvance = false;   // gate spurious advance posts during programmatic actions
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
let _playbackGeneration = 0;
let _pendingCrossfade = null;

// The effects that run in an AudioWorklet, and the module each needs.
// AudioWorklet exists only on an HTTPS or localhost page; where it is
// missing, or a module fails to load, the effect is skipped and the
// transition is a plain crossfade.
const _WORKLET_OF = {
  gate_stutter: "stutter",
  bitcrusher: "bitcrusher",
  freeze: "freeze",
  glitch: "glitch",
};
const _workletReady = {};

export function ensureAudioGraph() {
  if (_ctx) return _ctx;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) {
    console.warn("Web Audio API unavailable; falling back to plain <audio>.");
    return null;
  }
  _ctx = new Ctx();
  // Every sound the page makes reaches the speakers through this one
  // gain, which carries the page volume and drops to 0 while muted: both
  // decks, every transition effect and voice liners.
  _master = _ctx.createGain();
  _masterTarget = _muted ? 0 : _volume;
  _master.gain.value = _masterTarget;
  _master.connect(_ctx.destination);
  // The music and its effects meet here before the master, and drop to 0
  // while paused, so an effect tail stops with the decks.  Voice liners
  // skip it: Test previews a liner while paused.
  _program = _ctx.createGain();
  _programTarget = _paused ? 0 : 1;
  _program.gain.value = _programTarget;
  _program.connect(_master);
  // Effect sounds that play past the deck gains (echo and reverb tails,
  // noise, tones, decoded replays) meet here, scaled by the wet mix.
  _fxBus = _ctx.createGain();
  _fxBus.gain.value = _wetMixCache;
  _fxBus.connect(_program);
  for (const d of decks) {
    d.source = _ctx.createMediaElementSource(d.audio);
    d.gain   = _ctx.createGain();
    d.gain.gain.value = 0;
    d.source.connect(d.gain);
    d.gain.connect(_program);
    // Silent analyser tap — pulled only for silence detection.  Pre-gain
    // so we measure the actual track signal, not our crossfade ramp.
    d.analyser = _ctx.createAnalyser();
    d.analyser.fftSize = 512;
    d.source.connect(d.analyser);
    d._silenceMs = 0;
  }
  // Deck gains run from 0 to 1 (the crossfade and liner ducking); the
  // master above puts the page volume on them, so the first sound after
  // a page load already matches the slider.
  decks[0].gain.gain.value = 1;
  decks[1].gain.gain.value = 0;
  // Load the AudioWorklet modules in the background.  An effect whose
  // module isn't ready yet is skipped (see _WORKLET_OF).
  if (_ctx.audioWorklet) {
    for (const name of Object.values(_WORKLET_OF)) {
      _ctx.audioWorklet.addModule(`/${name}-worklet.js`).then(
        () => { _workletReady[name] = true; },
        (err) => { console.warn(`AudioWorklet load failed for ${name}:`, err); },
      );
    }
  }
  return _ctx;
}

function deckActive() { return decks[activeIdx]; }
function deckStandby() { return decks[activeIdx ^ 1]; }

export function stopAllDecks() {
  // Hard stop -- used when the session ends (expired or revoked), so
  // protected audio does not keep playing from buffered files.  A link
  // that only drops does not stop the decks: they play on from what they
  // have buffered.  Nothing plays again until the listener presses Play:
  // a server that comes back must not restart the music on its own (D15).
  _playbackGeneration += 1;
  playbackEnabled = false;
  clearAdvance();
  for (const retry of _mediaRetry.values()) clearTimeout(retry.timer);
  _mediaRetry.clear();
  if (_pendingCrossfade) {
    const pending = _pendingCrossfade;
    _pendingCrossfade = null;
    clearTimeout(pending.timer);
    try { pending.cleanupMetadata(); } catch (_) {}
    try { pending.teardownFx(); } catch (errorValue) { announceRequestError(errorValue); }
    pending.resolve(false);
  }
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
  if (_duck) {
    try {
      _duck.gain.cancelScheduledValues(_ctx.currentTime);
      _duck.gain.value = 1;
    } catch (_) {}
  }
  crossfading = false;
  suppressAdvance = false;
}

export function setSrcOnDeck(deck, path) {
  if (deck.path === path) return;
  deck.path = path;
  deck.audio.src = "/api/audio?path=" + encodeURIComponent(path);
}

function playOnDeck(deck) {
  return deck.audio.play().catch((err) => {
    console.warn("deck.play failed:", err);
  });
}

// The page's volume as a linear gain, whether mute holds the master at
// 0, and whether pause holds the music (not the liners) at 0.  The volume
// starts silent until the first server state says otherwise, so nothing
// plays at full gain before the page has read the volume the slider
// shows.
let _volume = 0;
let _muted = false;
let _paused = false;
export let _master = null;
let _masterTarget = null;
let _program = null;
let _programTarget = null;
let _fxBus = null;
// The server's transition wet mix (0 to 1): how much of each effect is
// heard against the untouched music.
let _wetMixCache = 1;

export function setVolume(linear) {
  const next = isIOS ? 1 : linear;
  if (next === _volume) return;
  _volume = next;
  applyVolume();
}

export function setMuted(muted) {
  const next = Boolean(muted);
  if (next === _muted) return;
  _muted = next;
  applyVolume();
}

// Put the volume, or 0 while muted, on the master now, and 0 on the
// music while paused.  Both sit after the deck gains, so a change never
// disturbs a crossfade or a liner duck.  Without Web Audio the <audio>
// elements play directly, so their own volume carries it.
function applyVolume() {
  if (!_ctx) {
    if (!(window.AudioContext || window.webkitAudioContext)) {
      for (const d of decks) d.audio.volume = _muted ? 0 : _volume;
    }
    return;
  }
  const now = _ctx.currentTime;
  const program = _paused ? 0 : 1;
  if (program !== _programTarget) {
    _programTarget = program;
    _program.gain.cancelScheduledValues(now);
    _program.gain.setValueAtTime(program, now);
  }
  const target = _muted ? 0 : _volume;
  if (target === _masterTarget) return;
  _masterTarget = target;
  _master.gain.cancelScheduledValues(now);
  _master.gain.setValueAtTime(target, now);
}

// Voice liners duck the music here, on a gain of its own between the
// music (decks and effects) and the master, so a duck never touches the
// deck gains a crossfade is ramping, and a crossfade never cuts a duck
// short.  Made the first time a liner ducks.
let _duck = null;

export function duckMusic(level, seconds) {
  if (!_ctx) return;
  if (!_duck) {
    _duck = _ctx.createGain();
    _duck.gain.value = 1;
    _program.disconnect();
    _program.connect(_duck);
    _duck.connect(_master);
  }
  const t0 = _ctx.currentTime;
  const gain = _duck.gain;
  gain.cancelScheduledValues(t0);
  gain.setValueAtTime(gain.value, t0);
  gain.linearRampToValueAtTime(level, t0 + 0.2);
  gain.setValueAtTime(level, t0 + Math.max(0.2, seconds - 0.2));
  gain.linearRampToValueAtTime(1, t0 + seconds + 0.2);
}

// Live deck at full, standby silent.  stopAllDecks leaves both at 0, and
// only Play (unlockAndPlay) puts them back: rewriting them on every state
// push would cut short a liner duck.
function restoreDeckGains() {
  if (!_ctx || crossfading) return;
  for (let i = 0; i < decks.length; i++) {
    decks[i].gain.gain.cancelScheduledValues(_ctx.currentTime);
    decks[i].gain.gain.setValueAtTime(i === activeIdx ? 1 : 0, _ctx.currentTime);
  }
}

// ----------------------------------------------------------------
// Browser-side transition effects (Web Audio API).  Each effect builds
// a small node graph between its target deck's source and gain, runs
// for fadeSec, and disconnects on teardown.
// ----------------------------------------------------------------

let _lastTransitionFx = "none";
let _rotateCursor = -1;

// Random and rotate pick only effects that can run; a chosen effect
// that can't is played as a plain crossfade.
const _canRun = (name) => !Object.hasOwn(_WORKLET_OF, name) || _workletReady[_WORKLET_OF[name]] === true;

function _resolveTransition(name) {
  const real = Object.keys(_EFFECTS).filter(_canRun);
  if (name === "random") return real[Math.floor(Math.random() * real.length)];
  if (name === "rotate") {
    _rotateCursor = (_rotateCursor + 1) % real.length;
    return real[_rotateCursor];
  }
  return name && _canRun(name) ? name : "none";
}

// disconnect() drops every output of the deck's source, the silence
// detector's analyser tap included, so both put the tap back.  Without
// it the first effect transition left the silence-triggered crossfade
// dead for the rest of the session.
function _reconnectSource(deck, target) {
  try { deck.source.disconnect(); } catch (_) {}
  deck.source.connect(target);
  if (deck.analyser) deck.source.connect(deck.analyser);
}
function _routeThrough(deck, headNode) {
  _reconnectSource(deck, headNode);
}
function _restoreDirect(deck) {
  _reconnectSource(deck, deck.gain);
}

// Web Audio reads a lowpass or highpass Q in dB.  -3.01 dB is the
// Butterworth 1/sqrt(2): the passband stays at unity with no resonant
// bump above the deck's own level.
const FLAT_Q = -3.0103;

// Levels below are relative to the deck at full (gain 1), which the
// master then sets to the page volume.  Every effect path peaks at or
// under that, so no effect is louder than the music it treats.  The
// browser decks play the files as they are, without ReplayGain, so
// effects built from a deck follow that track's own loudness; the
// synthesised layers (noise, tones) are set against a mastered track
// peaking at full scale.

// The deck's untouched signal, at (1 - wet mix), is all that reaches its
// gain while the effect runs.  Returns nothing: effects that replace the
// deck's sound with a decoded replay send it to the effect bus.
function _keepDry(ctx, deck, wet, teardowns) {
  const dry = ctx.createGain();
  dry.gain.value = 1 - wet;
  _routeThrough(deck, dry);
  dry.connect(deck.gain);
  teardowns.push(() => _disconnectAll(dry));
}

// For an effect put inline on a deck (filters, gates, crushers): the
// returned node takes the effect's output at the wet mix, beside the
// untouched signal at (1 - wet mix).  Connect deck.source to the
// effect's first node after calling this.
function _replaceOn(ctx, deck, wet, teardowns) {
  _keepDry(ctx, deck, wet, teardowns);
  const out = ctx.createGain();
  out.gain.value = wet;
  out.connect(deck.gain);
  teardowns.push(() => _disconnectAll(out));
  return out;
}

// For an effect added to a deck's sound that fades out with it (flanger,
// chorus, dub delay): the untouched signal keeps playing, and the
// returned node takes the added signal at the wet mix.
function _addTo(ctx, deck, wet, teardowns) {
  const out = ctx.createGain();
  out.gain.value = wet;
  out.connect(deck.gain);
  teardowns.push(() => _disconnectAll(out));
  return out;
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

// Decoded copies of deck files for the effects that replay them (see
// _decodedReplays), cached per path so a Skip → Skip cycle doesn't
// re-decode.
const _bufferCache = new Map();   // path → AudioBuffer
const _bufferPending = new Map();
let _bufferGeneration = 0;

async function _decodeFor(path) {
  if (_bufferCache.has(path)) return _bufferCache.get(path);
  if (_bufferPending.has(path)) return _bufferPending.get(path);
  const url = "/api/audio?path=" + encodeURIComponent(path);
  const generation = _bufferGeneration;
  const pending = requestBinary(url)
    .then((arr) => _ctx.decodeAudioData(arr))
    .then((buf) => {
      if (generation !== _bufferGeneration) return buf;
      // Cap cache at 4 entries — these can be 100+ MB each
      if (_bufferCache.size >= 4) {
        const k = _bufferCache.keys().next().value;
        _bufferCache.delete(k);
      }
      _bufferCache.set(path, buf);
      return buf;
    })
    .catch((errorValue) => {
      // Decoded-audio effects are invoked on demand at transition time.
      // Surface a failed audio request just as other client requests do,
      // while preserving the rejection for the effect's own error handler.
      announceRequestError(errorValue);
      throw errorValue;
    })
    .finally(() => {
      if (_bufferPending.get(path) === pending) {
        _bufferPending.delete(path);
      }
    });
  _bufferPending.set(path, pending);
  return pending;
}
// Industry-standard minimum effect lengths (seconds).  Sourced from
// commercial DJ-tool defaults (Pioneer DJM, Reloop RMX, Numark NS).
// Player._MIN_FX_DURATION_S on the Python side holds the same values
// for the server mix; the two are kept in step by hand.
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

// --- FX bar-length table (the browser owns this; there is no Python copy) ---
//
// Each entry is the bar count _effectDurationFor rounds the effect's
// length to when beat-sync is on and the outgoing BPM and outro are known.
// Which effects start on the next downbeat is decided in each effect's
// builder below (scratch, beat_repeat, sidechain_pump, transformer and
// stutter_build call _BS.nextDownbeat), not here.
const _FX_BAR_TABLE = {
  beat_repeat:    4,
  gate_stutter:   4,
  stutter_build:  4,
  sidechain_pump: 8,
  halftime:       4,
  transformer:    2,
  echo_out:       4,
  dub_delay:      8,
  scratch:        2,
  noise_riser:    4,
  noise_drop:     4,
  reverse_reverb: 4,
  air_horn:       2,
  dub_siren:      4,
  highpass_sweep: 4,
  lowpass_sweep:  4,
  cross_eq_swap:  4,
  submerge:       4,
  telephone:      4,
  chorus:         4,
  phaser:         4,
  flanger:        4,
  wow_flutter:    4,
  vinyl_wow:      4,
  ring_modulator: 4,
  bitcrusher:     4,
  pitch_swell:    2,
  pitch_fall:     2,
  tape_stop:      2,
  backspin:       2,
  forward_spin:   2,
  vinyl_rewind:   4,
  freeze:         2,
  glitch:         4,
  reverb_tail:    4,
};

// --- _BS: BeatSync helper.  Refreshed at the start of every crossfade
// from the server-emitted track payload (downbeats_outro and key_hz)
// plus the cached BPMs.  All accessors take an AudioContext
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
  const target = outroLen * frac;
  // Clamp: never below the per-effect floor (or absolute 1.0s), never
  // above 12s — keeps musically sane boundaries even on edge tracks.
  const lo = Math.max(_ABS_MIN_FX_DURATION_S, staticMin);
  let dur = Math.min(_MAX_FX_DURATION_S, Math.max(lo, target));

  // Beat-sync rounding: when enabled and the outgoing track has a known
  // BPM, round the target up to the nearest whole bar count from the
  // FX_BAR_TABLE so rhythmic effects fit an integer number of bars.
  if (_beatSyncEnabled && _outBpmCache > 0 && _FX_BAR_TABLE[effect]) {
    const bars = _FX_BAR_TABLE[effect];
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

// ---- Effect builders ------------------------------------------------------
//
// Each effect in _EFFECTS below gets `fx`: the context, its start and end
// (t0, tEnd) and length (fadeSec), both decks, the teardown list, and
// the routing helpers replaceOn / addTo / keepDry bound to the wet mix.
// Effects that differ only in their settings share a builder here.

// Put the chain `first` ... `last` inline on `deck`, its output at the
// wet mix beside the untouched signal (see _replaceOn).
function _inline(fx, deck, first, last = first) {
  const out = fx.replaceOn(deck);
  deck.source.connect(first);
  last.connect(out);
}

// Keep the outgoing deck at full level through the effect, then drop it
// to silence over the last `dropSec`: left to the crossfade ramp, the
// deck fades before the effect's character is heard.
function _holdThenDrop(fx, dropSec) {
  const { t0, tEnd } = fx;
  const gain = fx.outDeck.gain.gain;
  gain.cancelScheduledValues(t0);
  gain.setValueAtTime(1, t0);
  gain.setValueAtTime(1, Math.max(t0, tEnd - dropSec));
  gain.linearRampToValueAtTime(0, tEnd);
}

// Filter sweep put inline on `deck`: the cutoff glides from `fromHz` to
// `toHz` over the first 70 % of the effect (at least 0.5 s).
function _filterSweep(fx, deck, type, fromHz, toHz) {
  const f = fx.ctx.createBiquadFilter();
  f.type = type;
  f.Q.value = FLAT_Q;
  f.frequency.setValueAtTime(fromHz, fx.t0);
  f.frequency.exponentialRampToValueAtTime(toHz, fx.t0 + Math.max(0.5, fx.fadeSec * 0.7));
  _inline(fx, deck, f);
  fx.teardowns.push(() => _disconnectAll(f));
}

// A delay line whose time a sine LFO sweeps by ±depthSec around baseSec.
function _sweptDelay(ctx, maxSec, baseSec, hz, depthSec) {
  const delay = ctx.createDelay(maxSec); delay.delayTime.value = baseSec;
  const lfo = ctx.createOscillator(); lfo.frequency.value = hz;
  const lfoGain = ctx.createGain(); lfoGain.gain.value = depthSec;
  lfo.connect(lfoGain); lfoGain.connect(delay.delayTime);
  return { delay, lfo, lfoGain };
}

// Feedback delay on the outgoing deck (echo_out, flanger, dub_delay).
// Its repeats feed back at `feedback`, darkened by a lowpass at
// `dampingHz` when given, and with the delay time swept when `sweep` is
// given.  Returns the node carrying the repeats at `level` (a number, or
// a function that automates the gain); the caller routes it.  Repeats
// that line up in phase build to level / (1 - feedback), so keeping that
// at or under 1 keeps the effect no louder than the deck.
function _feedbackDelay(fx, { maxSec, delaySec, sweep = null, feedback, dampingHz = null, level }) {
  const { ctx, outDeck } = fx;
  let delay, lfo = null, lfoGain = null;
  if (sweep) {
    ({ delay, lfo, lfoGain } = _sweptDelay(ctx, maxSec, delaySec, sweep.hz, sweep.depthSec));
  } else {
    delay = ctx.createDelay(maxSec);
    delay.delayTime.value = delaySec;
  }
  const fb = ctx.createGain(); fb.gain.value = feedback;
  let damping = null;
  if (dampingHz) {
    damping = ctx.createBiquadFilter();
    damping.type = "lowpass"; damping.frequency.value = dampingHz; damping.Q.value = FLAT_Q;
    delay.connect(damping);
  }
  const tap = damping || delay;
  const out = ctx.createGain();
  if (typeof level === "number") out.gain.value = level; else level(out.gain);
  outDeck.source.connect(delay);
  tap.connect(fb); fb.connect(delay);   // feedback loop
  tap.connect(out);
  if (lfo) lfo.start();
  fx.teardowns.push(() => {
    if (lfo) { try { lfo.stop(); } catch (_) {} }
    _disconnectAll(delay, fb, damping, out, lfoGain);
  });
  return out;
}

// Mono white noise at `level`, `sec` long.
function _noiseBuffer(ctx, sec, level) {
  const buf = ctx.createBuffer(1, ctx.sampleRate * sec, ctx.sampleRate);
  const data = buf.getChannelData(0);
  for (let i = 0; i < data.length; i++) data[i] = (Math.random() * 2 - 1) * level;
  return buf;
}

// Noise layer on the effect bus (noise_riser, noise_drop): white noise
// through a lowpass sweeping `fromHz` -> `toHz`, its level shaped by
// `envelope(gainParam)`.  At least `minSec` long.
function _noiseLayer(fx, minSec, fromHz, toHz, envelope) {
  const { ctx, t0, tEnd } = fx;
  const src = ctx.createBufferSource();
  src.buffer = _noiseBuffer(ctx, Math.max(fx.fadeSec, minSec), 1);
  const filter = ctx.createBiquadFilter(); filter.type = "lowpass";
  filter.frequency.setValueAtTime(fromHz, t0);
  filter.frequency.exponentialRampToValueAtTime(toHz, tEnd);
  const g = ctx.createGain();
  envelope(g.gain);
  src.connect(filter); filter.connect(g); g.connect(_fxBus);
  src.start();
  fx.teardowns.push(() => {
    try { src.stop(); } catch (_) {}
    _disconnectAll(src, filter, g);
  });
}

// An AudioWorklet with explicit stereo output: without outputChannelCount
// some browsers give the worklet a single channel, which then upmixes to
// silence on certain destinations.
function _stereoWorklet(ctx, name) {
  return new AudioWorkletNode(ctx, name, {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [2],
  });
}

// Worklet fed from the outgoing deck and sent to the effect bus through
// a gain shaped by `envelope` (freeze, glitch), so it is heard at the
// deck's level instead of fading with the crossfade.  The deck keeps
// only its dry share.  Routing source -> passthrough gain -> worklet
// steadies the input on Chrome, where MediaElementSource ->
// AudioWorkletNode can deliver empty quanta during the first capture
// window.  Do NOT mute the <audio> element: MediaElementSource respects
// the muted flag and would feed the worklet silence.
function _workletToBus(fx, name, params, envelope) {
  const { ctx, t0, outDeck } = fx;
  const node = _stereoWorklet(ctx, name);
  for (const [key, value] of Object.entries(params)) {
    node.parameters.get(key).setValueAtTime(value, t0);
  }
  const passthrough = ctx.createGain();
  passthrough.gain.value = 1.0;
  const g = ctx.createGain();
  envelope(g.gain);
  fx.keepDry(outDeck);
  outDeck.source.connect(passthrough);
  passthrough.connect(node);
  node.connect(g); g.connect(_fxBus);
  fx.teardowns.push(() => _disconnectAll(node, g, passthrough));
}

// `len` frames of `buf` from `start` as a new buffer, back to front when
// `reverse`.
function _copyFrames(ctx, buf, start, len, reverse = false) {
  const out = ctx.createBuffer(buf.numberOfChannels, len, buf.sampleRate);
  for (let ch = 0; ch < buf.numberOfChannels; ch++) {
    const dst = out.getChannelData(ch);
    const src = buf.getChannelData(ch);
    if (reverse) {
      for (let i = 0; i < len; i++) dst[i] = src[start + len - 1 - i];
    } else {
      dst.set(src.subarray(start, start + len));
    }
  }
  return out;
}

// Replays cut from the decoded outgoing track (spins, tape stop, scratch,
// beat repeat).  They go to the effect bus, not deck.gain, or the
// crossfade ramp would silence them; the deck keeps only its dry share,
// so at a full wet mix the replay is what is heard.  `build(buf,
// currentT)` schedules them once the file is decoded and returns
// [{ src, g }], which stop on teardown.  HTML <audio> can't play
// backwards or slow to a stop, which is why these decode the file.
function _decodedReplays(fx, build, onError) {
  const path = fx.outDeck.path;
  const currentT = fx.outDeck.audio.currentTime;
  fx.keepDry(fx.outDeck);
  let cancelled = false;
  const sources = [];
  _decodeFor(path).then((buf) => {
    if (cancelled) return;
    sources.push(...build(buf, currentT));
  }).catch(onError);
  fx.teardowns.push(() => {
    cancelled = true;
    for (const s of sources) {
      try { s.src.stop(); } catch (_) {}
      _disconnectAll(s.src, s.g);
    }
  });
}

// _decodeFor has already announced a failed request, so an effect whose
// replay cannot be decoded only logs it: one announcement per failure.
const _warnReplayFailed = (err) => console.warn("effect replay decode failed:", err);

// Playback-rate glide from `from` to `to` over `sec`.
const _glide = (from, to) => (rate, t0, sec) => {
  rate.setValueAtTime(from, t0);
  rate.linearRampToValueAtTime(to, t0 + sec);
};

// The outgoing track replayed at a changing speed (tape_stop and the
// spins), at least `minSec` long.  A spin reads the window behind the
// playhead, backwards when `reverse`; tape stop reads the audio still to
// come.  `rate(param, t0, sec)` shapes the speed, the last `release`
// seconds fade out, and `friction` [fromHz, toHz] adds a quiet swept
// noise, the sound of the record under the hand.
function _spin(fx, { minSec, ahead = false, reverse = false, rate, release, friction = null }) {
  const { ctx, t0 } = fx;
  const sec = Math.max(fx.fadeSec, minSec);
  _decodedReplays(fx, (buf, currentT) => {
    const sr = buf.sampleRate;
    let startSamp, endSamp;
    if (ahead) {
      startSamp = Math.floor(currentT * sr);
      endSamp = Math.min(buf.length, startSamp + Math.floor(sec * 2 * sr));
    } else {
      const windowSec = Math.max(sec * 1.5, 4.0);
      startSamp = Math.max(0, Math.floor((currentT - windowSec) * sr));
      endSamp = Math.min(buf.length, Math.floor(currentT * sr));
    }
    const len = endSamp - startSamp;
    if (len <= 0) return [];
    const src = ctx.createBufferSource();
    src.buffer = _copyFrames(ctx, buf, startSamp, len, reverse);
    rate(src.playbackRate, t0, sec);
    const g = ctx.createGain();
    g.gain.setValueAtTime(1, t0);
    g.gain.setValueAtTime(1, t0 + sec - release);
    g.gain.linearRampToValueAtTime(0.0, t0 + sec);
    src.connect(g); g.connect(_fxBus);
    src.start();
    return [{ src, g }];
  }, _warnReplayFailed);
  if (!friction) return;
  const noise = ctx.createBufferSource();
  noise.buffer = _noiseBuffer(ctx, sec, 0.6);
  const bp = ctx.createBiquadFilter();
  bp.type = "bandpass"; bp.Q.value = 1.5;
  bp.frequency.setValueAtTime(friction[0], t0);
  bp.frequency.exponentialRampToValueAtTime(friction[1], t0 + sec);
  const g = ctx.createGain();
  g.gain.setValueAtTime(0.0, t0);
  g.gain.linearRampToValueAtTime(0.20, t0 + 0.05);
  g.gain.linearRampToValueAtTime(0.15, t0 + sec * 0.6);
  g.gain.exponentialRampToValueAtTime(0.001, t0 + sec);
  noise.connect(bp); bp.connect(g); g.connect(_fxBus);
  noise.start();
  fx.teardowns.push(() => {
    try { noise.stop(); } catch (_) {}
    _disconnectAll(noise, bp, g);
  });
}

// Bend the outgoing <audio> element's playbackRate along `rateAt(t)`, t
// running 0 -> 1 over `sec`, every `everyMs`.  preservesPitch defaults to
// true in browsers, which turns a rate change into a tempo change only;
// switching it off makes the bend a real pitch bend.  When the bend ends
// the rate goes to `endRate` (if given); teardown puts it back to
// `restoreRate` (the rate before the effect when null).
function _bendRate(fx, { sec = fx.fadeSec, everyMs, rateAt, preservesPitch = false,
  endRate = null, restoreRate = 1.0 }) {
  const audio = fx.outDeck.audio;
  const prevPreserve = audio.preservesPitch !== false;
  const prevRate = audio.playbackRate;
  try { audio.preservesPitch = preservesPitch; } catch (_) {}
  const startMs = performance.now();
  const durMs = sec * 1000;
  const iv = setInterval(() => {
    const t = (performance.now() - startMs) / durMs;
    if (t >= 1) {
      clearInterval(iv);
      if (endRate !== null) { try { audio.playbackRate = endRate; } catch (_) {} }
      return;
    }
    try { audio.playbackRate = rateAt(t); } catch (_) {}
  }, everyMs);
  fx.teardowns.push(() => {
    clearInterval(iv);
    try { audio.playbackRate = restoreRate === null ? prevRate : restoreRate; } catch (_) {}
    try { audio.preservesPitch = prevPreserve; } catch (_) {}
  });
}

// A gain inline on the outgoing deck, automated by `schedule(gainParam)`
// (the rhythmic gates and the sidechain pump).
function _gainGate(fx, schedule) {
  const gate = fx.ctx.createGain();
  schedule(gate.gain);
  _inline(fx, fx.outDeck, gate);
  fx.teardowns.push(() => _disconnectAll(gate));
}

// ---- The effects ----------------------------------------------------------
//
// Keyed by effect name, in the order ROTATE cycles through them.
const _EFFECTS = {
  echo_out(fx) {
    // Tempo-synced eighth-note echo throw, sent to the effect bus so the
    // tail survives after deck.gain has ramped to silence.  Level 0.4
    // with feedback 0.6 builds to at most 0.4 / (1 - 0.6) = 1.
    const echo = _feedbackDelay(fx, {
      maxSec: 2.0,
      delaySec: Math.max(0.05, Math.min(1.5, _BS.beatSec(fx.t0) / 2)),
      feedback: 0.6,
      level: (gain) => {
        gain.setValueAtTime(0.4, fx.t0);
        gain.setValueAtTime(0.4, fx.t0 + fx.fadeSec * 0.6);
        gain.exponentialRampToValueAtTime(0.001, fx.tEnd);
      },
    });
    echo.connect(_fxBus);
  },

  reverb_tail(fx) {
    // Big-hall reverb on the effect bus, so it rings on after the dry
    // signal is silenced.  4 s IR, slow decay.  The normalised IR passes
    // about half the level (RMS); at 1.4 the tail sits about 3 dB under
    // the dry.
    const { ctx, t0, fadeSec } = fx;
    const conv = ctx.createConvolver();
    conv.buffer = _makeReverbIR(4.0, 1.8);
    const tail = ctx.createGain();
    tail.gain.setValueAtTime(1.4, t0);
    tail.gain.setValueAtTime(1.4, t0 + fadeSec * 0.4);
    tail.gain.exponentialRampToValueAtTime(0.001, t0 + fadeSec + 1.0);
    fx.outDeck.source.connect(conv); conv.connect(tail);
    tail.connect(_fxBus);
    fx.teardowns.push(() => _disconnectAll(conv, tail));
  },

  highpass_sweep(fx) {
    // Filter-in: the incoming track at full level, heavily filtered, so
    // the bass bloom is unmistakable.  Left to its 0 -> 1 ramp it would
    // sound like a muffled fade-in.
    _filterSweep(fx, fx.inDeck, "highpass", 6000, 50);
    fx.inDeck.gain.gain.cancelScheduledValues(fx.t0);
    fx.inDeck.gain.gain.setValueAtTime(1, fx.t0);
  },

  lowpass_sweep(fx) {
    // Filter-out: the outgoing track stays loud while a steep lowpass
    // closes, then drops in the last 200 ms.  applyTransitionFx runs
    // after startCrossfade has scheduled its ramps, so these writes win.
    _filterSweep(fx, fx.outDeck, "lowpass", fx.ctx.sampleRate / 2, 180);
    _holdThenDrop(fx, 0.2);
  },

  tape_stop(fx) {
    _spin(fx, {
      minSec: 4.0,
      ahead: true,
      release: 0.2,
      // Quadratic-ish slowdown to a stop, like a real tape brake.
      rate: (rate, t0, sec) => {
        rate.setValueAtTime(1.0, t0);
        rate.linearRampToValueAtTime(0.4, t0 + sec * 0.5);
        rate.exponentialRampToValueAtTime(0.001, t0 + sec);
      },
    });
  },

  gate_stutter(fx) {
    // Tempo-synced gate accelerating from 1/8 to 1/16 notes (8 -> 16 Hz
    // when the BPM is unknown), open a quarter of each cycle.  The
    // worklet puts raised-cosine fades on every edge.
    const { ctx, t0, tEnd } = fx;
    const beatHz = (_BS.outBpm > 0 ? _BS.outBpm : 120) / 60;
    const node = new AudioWorkletNode(ctx, "stutter");
    const rateParam = node.parameters.get("rate");
    rateParam.setValueAtTime(beatHz * 2, t0);
    rateParam.linearRampToValueAtTime(beatHz * 4, tEnd);
    node.parameters.get("duty").setValueAtTime(0.25, t0);
    _inline(fx, fx.outDeck, node);
    fx.teardowns.push(() => _disconnectAll(node));
  },

  noise_riser(fx) {
    // Builds over the effect, then fades out in the last 0.4 s instead
    // of ending on a hard cut.  Crests at 0.25: white noise that bright
    // reads far louder than its level, so it sits about 8 dB under a
    // mastered track's RMS.
    const { t0, tEnd } = fx;
    _noiseLayer(fx, 4, 200, 16000, (gain) => {
      gain.setValueAtTime(0.0, t0);
      gain.linearRampToValueAtTime(0.25, Math.max(t0 + 0.05, tEnd - 0.4));
      gain.exponentialRampToValueAtTime(0.001, tEnd);
    });
  },

  noise_drop(fx) {
    // The riser's opposite: starts bright and sweeps down in pitch as it
    // fades, like a bomb drop or a rocket fly-by.
    const { t0, tEnd } = fx;
    _noiseLayer(fx, 3, 16000, 150, (gain) => {
      gain.setValueAtTime(0.25, t0);
      gain.exponentialRampToValueAtTime(0.001, tEnd);
    });
  },

  cross_eq_swap(fx) {
    // Outgoing keeps its highs; incoming starts as bass only and opens
    // up, so the two hand the bass over instead of clashing.
    const { ctx, t0, tEnd } = fx;
    const fOut = ctx.createBiquadFilter();
    fOut.type = "highpass"; fOut.frequency.value = 250; fOut.Q.value = FLAT_Q;
    _inline(fx, fx.outDeck, fOut);
    const fIn = ctx.createBiquadFilter();
    fIn.type = "lowpass"; fIn.Q.value = FLAT_Q;
    fIn.frequency.setValueAtTime(250, t0);
    fIn.frequency.exponentialRampToValueAtTime(ctx.sampleRate / 2, tEnd);
    _inline(fx, fx.inDeck, fIn);
    fx.teardowns.push(() => _disconnectAll(fOut, fIn));
  },

  bitcrusher(fx) {
    // Sample-rate reduction plus bit-depth quantisation in a worklet:
    // the 8-bit console sound.  The deck holds full level so the lo-fi
    // character isn't masked by the crossfade ramp.
    const { ctx, t0, tEnd, fadeSec } = fx;
    const node = _stereoWorklet(ctx, "bitcrusher");
    _holdThenDrop(fx, 0.3);
    // Peak crush at 25 % of the fade: by halfway the crossfade has the
    // outgoing track at about 50 %, so the crush has to land early to be
    // heard.  Bottoms out at 2 bits / 24x rate reduction.
    const peakAt = t0 + Math.max(0.4, fadeSec * 0.25);
    const bitsParam = node.parameters.get("bits");
    const rateParam = node.parameters.get("rateReduce");
    bitsParam.setValueAtTime(12, t0);
    bitsParam.linearRampToValueAtTime(2, peakAt);
    bitsParam.setValueAtTime(2, tEnd);
    rateParam.setValueAtTime(1, t0);
    rateParam.linearRampToValueAtTime(24, peakAt);
    rateParam.setValueAtTime(24, tEnd);
    _inline(fx, fx.outDeck, node);
    fx.teardowns.push(() => _disconnectAll(node));
  },

  flanger(fx) {
    // 1-9 ms delay swept by a 0.5 Hz LFO, with feedback for the comb
    // resonance.  Level 0.45 with feedback 0.55 keeps the comb peaks at
    // 0.45 / (1 - 0.55) = 1.
    const flanged = _feedbackDelay(fx, {
      maxSec: 0.02,
      delaySec: 0.005,
      sweep: { hz: 0.5, depthSec: 0.004 },
      feedback: 0.55,
      level: 0.45,
    });
    flanged.connect(fx.addTo(fx.outDeck));
  },

  pitch_swell(fx) {
    // Speed and pitch rising 1x -> 2x into the cut.
    _bendRate(fx, { everyMs: 20, rateAt: (t) => 1.0 + t });
  },

  pitch_fall(fx) {
    // Pitch sagging 1x -> 0.3x without tape_stop's brake to zero.  Floors
    // at 0.3 because some browsers glitch below about 0.25.
    _bendRate(fx, { everyMs: 20, rateAt: (t) => Math.max(0.3, 1.0 - 0.7 * t) });
  },

  telephone(fx) {
    // Telephone band-pass, then drive into a soft-clip curve: without the
    // saturation it only sounds muffled, not phone-like.  The drive
    // squashes the band to a near-constant level, so the curve's ceiling
    // sets how loud the phone is: 0.35 lands it just under the music's
    // RMS.
    const { ctx } = fx;
    const hp = ctx.createBiquadFilter();
    hp.type = "highpass"; hp.frequency.value = 500; hp.Q.value = 3;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass";  lp.frequency.value = 2800; lp.Q.value = 3;
    const shaper = ctx.createWaveShaper();
    const n = 1024;
    const curve = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const x = (i / (n - 1)) * 2 - 1;
      curve[i] = Math.tanh(x * 4) * 0.35;
    }
    shaper.curve = curve;
    const drive = ctx.createGain(); drive.gain.value = 1.6;
    _inline(fx, fx.outDeck, hp, shaper);
    hp.connect(lp); lp.connect(drive); drive.connect(shaper);
    fx.teardowns.push(() => _disconnectAll(hp, lp, drive, shaper));
  },

  backspin(fx) {
    // Thrown back at 2x, friction slowing it to 0.05x.
    _spin(fx, {
      minSec: 2.5, reverse: true, rate: _glide(2.0, 0.05), release: 0.3, friction: [2500, 120],
    });
  },

  forward_spin(fx) {
    // The backspin's mirror: from a near stop, released into 2.5x at the cut.
    _spin(fx, {
      minSec: 2.5, rate: _glide(0.05, 2.5), release: 0.3, friction: [120, 2500],
    });
  },

  chorus(fx) {
    // Three detuned voices; at 0.33 each they add up, even in phase, to
    // the deck's own level.
    const voiced = fx.ctx.createGain(); voiced.gain.value = 0.33;
    const voices = [[0.020, 0.4, 0.003], [0.025, 0.6, 0.004], [0.030, 0.8, 0.005]]
      .map(([baseSec, hz, depthSec]) => {
        const voice = _sweptDelay(fx.ctx, 0.1, baseSec, hz, depthSec);
        fx.outDeck.source.connect(voice.delay); voice.delay.connect(voiced);
        voice.lfo.start();
        return voice;
      });
    voiced.connect(fx.addTo(fx.outDeck));
    fx.teardowns.push(() => {
      for (const voice of voices) { try { voice.lfo.stop(); } catch (_) {} }
      _disconnectAll(...voices.map((v) => v.delay), ...voices.map((v) => v.lfoGain), voiced);
    });
  },

  submerge(fx) {
    // Underwater: a lowpass closing to 400 Hz plus a reverb wash.  The
    // filtered signal at 0.8 plus the wash (the 2.5 s IR passes about
    // 0.4, times 0.45) stays at the deck's level.
    const { ctx, t0, tEnd } = fx;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass"; lp.Q.value = FLAT_Q;
    lp.frequency.setValueAtTime(ctx.sampleRate / 2, t0);
    lp.frequency.exponentialRampToValueAtTime(400, tEnd);
    const conv = ctx.createConvolver();
    conv.buffer = _makeReverbIR(2.5, 2.0);
    const filtered = ctx.createGain(); filtered.gain.value = 0.8;
    const wash = ctx.createGain(); wash.gain.value = 0.45;
    const out = fx.replaceOn(fx.outDeck);
    fx.outDeck.source.connect(lp);
    lp.connect(filtered); filtered.connect(out);   // dry filtered
    lp.connect(conv);                              // + wet reverb
    conv.connect(wash); wash.connect(out);
    fx.teardowns.push(() => _disconnectAll(lp, conv, filtered, wash));
  },

  vinyl_wow(fx) {
    // Drunk turntable: a 1.2 Hz pitch wobble whose depth grows 5 % -> 25 %.
    _bendRate(fx, {
      everyMs: 16,
      endRate: 1.0,
      rateAt: (t) => {
        const depth = 0.05 + 0.20 * t;
        return 1.0 + depth * Math.sin(2 * Math.PI * 1.2 * t * fx.fadeSec);
      },
    });
  },

  freeze(fx) {
    // Capture the last 150 ms and loop it, fading out over the effect.
    _workletToBus(fx, "freeze", { grainMs: 150, fadeOutSec: fx.fadeSec },
      (gain) => gain.setValueAtTime(1, fx.t0));
  },

  glitch(fx) {
    // 80 ms slices of the recent audio replayed in random order.
    const { t0, tEnd, fadeSec } = fx;
    _workletToBus(fx, "glitch", { sliceMs: 80, density: 0.85 }, (gain) => {
      gain.setValueAtTime(1, t0);
      gain.setValueAtTime(1, t0 + fadeSec * 0.7);
      gain.linearRampToValueAtTime(0, tEnd);
    });
  },

  scratch(fx) {
    // A 1/4-note slice scratched over the effect: forward, back, forward,
    // back, each pass speeding up then slowing down, from the next
    // downbeat.
    const { ctx, t0, fadeSec } = fx;
    const sliceSec = _BS.beatSec(t0);
    const totalSec = Math.max(fadeSec, 2.0);
    const nPasses = 4;
    _decodedReplays(fx, (buf, currentT) => {
      const sr = buf.sampleRate;
      const sliceLen = Math.floor(sliceSec * sr);
      const startSamp = Math.max(0, Math.floor(currentT * sr) - sliceLen);
      const slice = _copyFrames(ctx, buf, startSamp, sliceLen);
      const passLen = totalSec / nPasses;
      const tScratchStart = _BS.nextDownbeat(t0);
      const sources = [];
      for (let p = 0; p < nPasses; p++) {
        const src = ctx.createBufferSource();
        src.buffer = _copyFrames(ctx, slice, 0, sliceLen, p % 2 === 1);
        const t1 = tScratchStart + p * passLen;
        const t2 = t1 + passLen;
        src.playbackRate.setValueAtTime(0.4, t1);
        src.playbackRate.linearRampToValueAtTime(2.0, t1 + passLen * 0.5);
        src.playbackRate.linearRampToValueAtTime(0.4, t2);
        const g = ctx.createGain();
        g.gain.setValueAtTime(0.95, t1);
        if (p === nPasses - 1) {
          g.gain.linearRampToValueAtTime(0, t2);
        }
        src.connect(g); g.connect(_fxBus);
        src.start(t1);
        src.stop(t2 + 0.01);
        sources.push({ src, g });
      }
      return sources;
    }, _warnReplayFailed);
  },

  beat_repeat(fx) {
    // A 1/8-note slice retriggered every 1/8 note from the next downbeat,
    // each hit a sharp attack and decay.
    const { ctx, t0, tEnd, fadeSec } = fx;
    const sliceSec = _BS.beatSec(t0) / 2;
    const nRepeats = Math.max(4, Math.floor(Math.max(fadeSec, 3.0) / sliceSec));
    _decodedReplays(fx, (buf, currentT) => {
      const sr = buf.sampleRate;
      const sliceLen = Math.floor(sliceSec * sr);
      const startSamp = Math.max(0, Math.floor(currentT * sr) - sliceLen);
      const slice = _copyFrames(ctx, buf, startSamp, sliceLen);
      const tStart = _BS.nextDownbeat(t0);
      const sources = [];
      for (let i = 0; i < nRepeats; i++) {
        const t1 = tStart + i * sliceSec;
        if (t1 >= tEnd) break;
        const src = ctx.createBufferSource(); src.buffer = slice;
        const g = ctx.createGain();
        g.gain.setValueAtTime(1, t1);
        g.gain.linearRampToValueAtTime(0, t1 + sliceSec);
        src.connect(g); g.connect(_fxBus);
        src.start(t1);
        src.stop(t1 + sliceSec + 0.01);
        sources.push({ src, g });
      }
      return sources;
    }, _warnReplayFailed);
  },

  sidechain_pump(fx) {
    // Four-on-the-floor duck to 0.3 on every beat of the blended tempo
    // from the next downbeat, recovering across the beat.
    const { t0, tEnd } = fx;
    _gainGate(fx, (gain) => {
      gain.value = 1.0;
      let t = _BS.nextDownbeat(t0);
      while (t < tEnd) {
        const period = _BS.beatSec(t);
        gain.setValueAtTime(1 - 0.7, t);
        gain.exponentialRampToValueAtTime(1.0, t + period * 0.95);
        t += period;
      }
    });
  },

  reverse_reverb(fx) {
    // A swell rising into the cut: a dense reverb whose send rises, on
    // the effect bus so it crescendos past the deck fade.  A band-pass
    // centres it on 200-2000 Hz so it isn't buried under the dry signal.
    // The normalised 2.5 s IR passes about 0.4 (RMS), so the swell
    // crests at 2.0 x 0.4, just under the deck's level.
    const { ctx, t0, tEnd } = fx;
    const conv = ctx.createConvolver();
    conv.buffer = _makeReverseReverbIR(2.5, 2.5);
    const bp = ctx.createBiquadFilter();
    bp.type = "bandpass"; bp.frequency.value = 700; bp.Q.value = 0.7;
    const swell = ctx.createGain();
    swell.gain.setValueAtTime(0.001, t0);
    swell.gain.exponentialRampToValueAtTime(2.0, tEnd - 0.05);
    swell.gain.linearRampToValueAtTime(0.0, tEnd);
    fx.outDeck.source.connect(conv); conv.connect(bp); bp.connect(swell);
    swell.connect(_fxBus);
    fx.teardowns.push(() => _disconnectAll(conv, bp, swell));
  },

  air_horn(fx) {
    // Square-wave horn sweeping up two octaves: from half the outgoing
    // root to twice the incoming one when key-sync knows them, else
    // 220 -> 880 Hz.  Softened by a lowpass; a square wave is as loud as
    // its peak, so 0.2 sits about 5 dB under a mastered track's RMS.
    const { ctx, t0, tEnd } = fx;
    const osc = ctx.createOscillator();
    osc.type = "square";
    const outRoot = _BS.rootHzAt(t0);
    const inRoot = _BS.rootHzAt(tEnd);
    osc.frequency.setValueAtTime(outRoot ? outRoot * 0.5 : 220, t0);
    osc.frequency.exponentialRampToValueAtTime(Math.max(50, inRoot ? inRoot * 2.0 : 880), tEnd - 0.1);
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass"; lp.frequency.value = 3500; lp.Q.value = 1.5;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(0.2, t0 + 0.3);
    g.gain.setValueAtTime(0.2, tEnd - 0.1);
    g.gain.linearRampToValueAtTime(0, tEnd);
    osc.connect(lp); lp.connect(g); g.connect(_fxBus);
    osc.start(t0);
    osc.stop(tEnd + 0.05);
    fx.teardowns.push(() => {
      try { osc.stop(); } catch (_) {}
      _disconnectAll(osc, lp, g);
    });
  },

  vinyl_rewind(fx) {
    // Walkman rewind: a smooth reverse slowing 1x -> 0.5x, a musical
    // sister to the backspin's turntablist throw.
    _spin(fx, {
      minSec: 2.5, reverse: true, rate: _glide(1.0, 0.5), release: 0.3, friction: [2500, 120],
    });
  },

  transformer(fx) {
    // Syncopated open/cut pattern at 1/16-note steps from the next
    // downbeat, with 5 ms ramps on the edges.
    const { t0, tEnd } = fx;
    _gainGate(fx, (gain) => {
      gain.setValueAtTime(1, t0);
      const pattern = [1, 0, 1, 0, 0, 1, 0, 1];
      const cycle = 1 / ((_BS.outBpm > 0 ? _BS.outBpm : 120) / 60 * 4);
      let i = 0;
      for (let t = _BS.nextDownbeat(t0); t < tEnd; t += cycle) {
        const open = pattern[i % pattern.length];
        gain.setValueAtTime(open ? 1 : 0, t);
        gain.linearRampToValueAtTime(open ? 1 : 0, t + Math.min(0.005, cycle * 0.1));
        i++;
      }
      gain.setValueAtTime(1, tEnd);
    });
  },

  dub_siren(fx) {
    // Sine siren two octaves up from the outgoing root (440 -> 1760 Hz
    // when no key is known) with a 5 Hz vibrato and a slow fade-in: it
    // sits behind the music where the air horn lands on top.
    const { ctx, t0, tEnd, fadeSec } = fx;
    const osc = ctx.createOscillator();
    osc.type = "sine";
    const outRoot = _BS.rootHzAt(t0);
    const inRoot = _BS.rootHzAt(tEnd);
    osc.frequency.setValueAtTime(outRoot || 440, t0);
    osc.frequency.exponentialRampToValueAtTime(Math.max(80, (inRoot || 440) * 4.0), tEnd);
    const vibLfo = ctx.createOscillator();
    vibLfo.type = "sine";
    vibLfo.frequency.value = 5;
    const vibGain = ctx.createGain();
    vibGain.gain.value = 8;  // ~15 cents at 1 kHz
    vibLfo.connect(vibGain); vibGain.connect(osc.frequency);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(0.2, t0 + fadeSec * 0.5);
    g.gain.setValueAtTime(0.2, tEnd - 0.2);
    g.gain.linearRampToValueAtTime(0, tEnd);
    osc.connect(g); g.connect(_fxBus);
    osc.start(t0); vibLfo.start(t0);
    osc.stop(tEnd + 0.05); vibLfo.stop(tEnd + 0.05);
    fx.teardowns.push(() => {
      try { osc.stop(); vibLfo.stop(); } catch (_) {}
      _disconnectAll(osc, vibLfo, vibGain, g);
    });
  },

  stutter_build(fx) {
    // A 50 % gate accelerating from 1/4 to 1/32 notes over the effect,
    // from the next downbeat.
    const { t0, tEnd, fadeSec } = fx;
    _gainGate(fx, (gain) => {
      gain.setValueAtTime(1, t0);
      const beatHz = (_BS.outBpm > 0 ? _BS.outBpm : 120) / 60;
      const startRate = beatHz;
      const endRate = beatHz * 8;
      let t = _BS.nextDownbeat(t0);
      let elapsed = 0;
      while (t < tEnd) {
        const frac = elapsed / Math.max(0.001, fadeSec);
        const cycle = 1 / (startRate + (endRate - startRate) * Math.min(1, frac));
        gain.setValueAtTime(1, t);
        gain.setValueAtTime(0, t + cycle * 0.5);
        t += cycle;
        elapsed += cycle;
      }
      gain.setValueAtTime(1, tEnd);
    });
  },

  wow_flutter(fx) {
    // Worn cassette: vinyl_wow's pitch wobble (1.5 Hz, ±4 %) plus an
    // 8 Hz tremolo of 0.85 ± 0.15, so its crests reach the deck's level
    // and never pass it.
    const { ctx, t0, tEnd } = fx;
    const trem = ctx.createGain();
    trem.gain.setValueAtTime(0.85, t0);
    const tremLfo = ctx.createOscillator();
    tremLfo.type = "sine";
    tremLfo.frequency.value = 8;
    const tremDepth = ctx.createGain();
    tremDepth.gain.value = 0.15;
    tremLfo.connect(tremDepth); tremDepth.connect(trem.gain);
    _inline(fx, fx.outDeck, trem);
    tremLfo.start(t0); tremLfo.stop(tEnd + 0.05);
    fx.teardowns.push(() => {
      try { tremLfo.stop(); } catch (_) {}
      _disconnectAll(trem, tremLfo, tremDepth);
    });
    _bendRate(fx, {
      everyMs: 16,
      endRate: 1.0,
      rateAt: (t) => 1.0 + 0.04 * Math.sin(2 * Math.PI * 1.5 * t * fx.fadeSec),
    });
  },

  phaser(fx) {
    // Four allpass stages (staggered 400-1000 Hz) swept ±600 Hz by one
    // 0.5 Hz LFO: four moving notches, where the flanger's delay gives
    // metallic comb teeth.  Mixed 50/50 with the plain signal; allpasses
    // keep the level, so the sum never passes the deck's.
    const { ctx, t0, tEnd } = fx;
    const stages = [];
    for (let i = 0; i < 4; i++) {
      const ap = ctx.createBiquadFilter();
      ap.type = "allpass";
      ap.frequency.value = 400 + i * 200;
      ap.Q.value = 0.7;
      stages.push(ap);
    }
    const lfo = ctx.createOscillator();
    lfo.type = "sine";
    lfo.frequency.value = 0.5;
    const lfoGain = ctx.createGain();
    lfoGain.gain.value = 600;
    lfo.connect(lfoGain);
    stages.forEach((ap) => lfoGain.connect(ap.frequency));
    const out = fx.replaceOn(fx.outDeck);
    const shifted = ctx.createGain();
    shifted.gain.value = 0.5;
    const plain = ctx.createGain();
    plain.gain.value = 0.5;
    fx.outDeck.source.connect(plain);
    plain.connect(out);
    fx.outDeck.source.connect(stages[0]);
    for (let i = 0; i < stages.length - 1; i++) stages[i].connect(stages[i + 1]);
    stages[stages.length - 1].connect(shifted);
    shifted.connect(out);
    lfo.start(t0);
    lfo.stop(tEnd + 0.05);
    fx.teardowns.push(() => {
      try { lfo.stop(); } catch (_) {}
      _disconnectAll(...stages, lfo, lfoGain, shifted, plain);
    });
  },

  ring_modulator(fx) {
    // Signal times a sine carrier (clangy bell sidebands), 50/50 with the
    // plain signal.  A GainNode at 0 whose gain the carrier drives
    // multiplies the audio by the sine.  The carrier sits an octave under
    // the song's root so the sidebands stay in key (173 Hz, about F3,
    // without key-sync).
    const { ctx, t0, tEnd } = fx;
    const ring = ctx.createGain();
    ring.gain.value = 0;
    const carrier = ctx.createOscillator();
    carrier.type = "sine";
    const carrierRoot = _BS.rootHzAt(t0);
    carrier.frequency.value = carrierRoot ? carrierRoot * 0.5 : 173;
    const carrierGain = ctx.createGain();
    carrierGain.gain.value = 1.0;
    carrier.connect(carrierGain);
    carrierGain.connect(ring.gain);
    const out = fx.replaceOn(fx.outDeck);
    const plain = ctx.createGain();
    plain.gain.value = 0.5;
    const ringMix = ctx.createGain();
    ringMix.gain.value = 0.5;
    fx.outDeck.source.connect(plain);
    plain.connect(out);
    fx.outDeck.source.connect(ring);
    ring.connect(ringMix);
    ringMix.connect(out);
    carrier.start(t0);
    carrier.stop(tEnd + 0.05);
    fx.teardowns.push(() => {
      try { carrier.stop(); } catch (_) {}
      _disconnectAll(carrier, carrierGain, ring, ringMix, plain);
    });
  },

  dub_delay(fx) {
    // Quarter-note delay with a lowpass in the loop, so every repeat is
    // darker; the track plays on untouched beside it.  0.45 with feedback
    // 0.55 builds to at most 0.45 / (1 - 0.55) = 1.
    const repeats = _feedbackDelay(fx, {
      maxSec: 2.0,
      delaySec: Math.max(0.2, Math.min(1.8, _BS.beatSec(fx.t0))),
      feedback: 0.55,
      dampingHz: 1500,
      level: 0.45,
    });
    repeats.connect(fx.addTo(fx.outDeck));
  },

  halftime(fx) {
    // Half tempo at the same pitch (preservesPitch on), easing 1x -> 0.5x
    // over the first half of the effect: the trap pre-drop.
    _bendRate(fx, {
      sec: fx.fadeSec * 0.5,
      everyMs: 20,
      preservesPitch: true,
      rateAt: (t) => 1.0 - 0.5 * t,
      endRate: 0.5,
      restoreRate: null,
    });
  },
};

function applyTransitionFx(effect, fadeSec, outDeck, inDeck) {
  const ctx = _ctx;
  if (!ctx || effect === "none" || !effect) return () => {};
  // Caller (startCrossfade) resolves the effect-preferred duration and
  // passes it in so the gain ramp and the effect share one timeline.
  const t0 = ctx.currentTime;
  const wet = _wetMixCache;
  const teardowns = [];
  _fxBus.gain.cancelScheduledValues(t0);
  _fxBus.gain.setValueAtTime(wet, t0);
  const fx = {
    ctx, t0, tEnd: t0 + fadeSec, fadeSec, outDeck, inDeck, teardowns,
    replaceOn: (deck) => _replaceOn(ctx, deck, wet, teardowns),
    addTo: (deck) => _addTo(ctx, deck, wet, teardowns),
    keepDry: (deck) => _keepDry(ctx, deck, wet, teardowns),
  };
  if (Object.hasOwn(_EFFECTS, effect)) _EFFECTS[effect](fx);
  return () => {
    for (const fn of teardowns) { try { fn(); } catch (_) {} }
    _restoreDirect(outDeck);
    _restoreDirect(inDeck);
  };
}

// `serverLed` = the server already advanced (shuffle, queued "Now",
// media-session next, CLI-side advance arriving via WS).  Browser is
// only playing catch-up to render the visual / audible crossfade; it
// MUST NOT POST /api/advance again or the server steps forward a
// second time and a fresh state push triggers another catch-up
// crossfade -- cascading "shuffles every few seconds" bug.
//
// The advance this page has asked for (the end of a track, or Skip while
// this page plays the music), from the request until the server has
// moved on.  A state push can leave the server before it handles the
// request and arrive after the page has already crossfaded into the new
// track; applied, it started a crossfade straight back to the old one.
// While the advance is pending, a state that still names the track the
// page left (fromPath) is stale and is not applied (isStaleAdvanceState).
// It stays pending after the answer until a push names another track,
// because a push sent before the advance can still land after it.
//
// The request carries from_path, the track this page believes is
// playing, so a server that has already moved on does not advance twice.
// A request that never reached the server (the link was down) is sent
// again once the link is back.  The time limits are backstops only, so
// a lost answer or echo never leaves the page deaf to the server.
let _advance = null;
let _applyingAdvanceAnswer = false;
const ADVANCE_ANSWER_WAIT_MS = 15000;
const ADVANCE_ECHO_WAIT_MS = 5000;
const ADVANCE_RETRY_MS = 5000;
const ADVANCE_MAX_SENDS = 4;
const ADVANCE_WAITING_TEXT =
  "The music stopped: AutoDJ could not be reached for the next track. "
  + "It carries on when the connection is back.";

// Whether the page's link to the server (its websocket) is up.  app.js
// reports it; requests and media loads that fail while it is down wait
// for it instead of being treated as failures.
let _linkUp = true;

function clearAdvance() {
  if (_advance && _advance.timer !== null) clearTimeout(_advance.timer);
  _advance = null;
}

function advanceIsCurrent(advance) {
  return _advance === advance
    && isAuthenticatedRequestCurrent(advance.epoch)
    && advance.generation === _playbackGeneration;
}

function retryAdvanceLater(advance) {
  if (!_linkUp) {
    advance.waitingForLink = true;
    // A deck that has run out stays silent until then: say so once.
    if (!advance.waitingSpoken && deckActive().audio.ended) {
      advance.waitingSpoken = true;
      announceEngineError(ADVANCE_WAITING_TEXT);
    }
    return;
  }
  advance.timer = setTimeout(() => {
    advance.timer = null;
    if (!advanceIsCurrent(advance)) return;
    if (_linkUp) void sendAdvance(advance);
    else retryAdvanceLater(advance);
  }, ADVANCE_RETRY_MS);
}

async function sendAdvance(advance) {
  if (!advanceIsCurrent(advance)) return null;
  advance.sends += 1;
  advance.waitingForLink = false;
  advance.until = Date.now() + ADVANCE_ANSWER_WAIT_MS;
  let state;
  try {
    state = await requestJson("/api/advance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(advance.fromPath ? { from_path: advance.fromPath } : {}),
    });
  } catch (errorValue) {
    if (!advanceIsCurrent(advance)) return null;
    if (isServerUnreachable(errorValue) && advance.sends < ADVANCE_MAX_SENDS) {
      retryAdvanceLater(advance);
      return null;
    }
    clearAdvance();
    announceRequestError(errorValue);
    return null;
  }
  if (!advanceIsCurrent(advance)) return null;
  const path = state && state.current_track ? state.current_track.path : null;
  if (path === advance.fromPath) {
    // The server stayed on that track: follow it.
    clearAdvance();
  } else {
    advance.answered = true;
    advance.until = Date.now() + ADVANCE_ECHO_WAIT_MS;
  }
  if (_applyState) {
    _applyingAdvanceAnswer = true;
    try { _applyState(state); } finally { _applyingAdvanceAnswer = false; }
  }
  return state;
}

function requestAdvance(fromPath = deckActive().path) {
  if (_advance && _advance.fromPath === fromPath && !_advance.answered) {
    return _advance.promise;
  }
  clearAdvance();
  const advance = {
    fromPath,
    epoch: captureAuthenticatedRequestEpoch(),
    generation: _playbackGeneration,
    answered: false,
    until: 0,
    sends: 0,
    timer: null,
    waitingForLink: false,
    waitingSpoken: false,
    promise: null,
  };
  _advance = advance;
  advance.promise = sendAdvance(advance);
  return advance.promise;
}

// True for a state the page must not apply because it predates the
// advance this page is waiting on (see _advance above).  A state that
// names another track ends the wait once the server has answered.
export function isStaleAdvanceState(s) {
  const advance = _advance;
  if (!advance || _applyingAdvanceAnswer) return false;
  const path = s && s.current_track ? s.current_track.path : null;
  if (path !== advance.fromPath) {
    if (advance.answered) clearAdvance();
    return false;
  }
  if (!advance.waitingForLink && advance.timer === null && Date.now() > advance.until) {
    clearAdvance();
    return false;
  }
  return true;
}

// Called by app.js when the websocket opens (true) or drops (false).
export function setLinkUp(up) {
  _linkUp = Boolean(up);
  if (!_linkUp) return;
  if (_advance && _advance.waitingForLink) void sendAdvance(_advance);
  for (const deck of Array.from(_mediaRetry.keys())) {
    const retry = _mediaRetry.get(deck);
    if (retry.waitingForLink) reloadDeck(deck, retry);
  }
}

// The repick in flight: a newer one makes its answer stale.
let repickRequest = null;
const runRepick = makeSingleFlight(async (blacklist, epoch, playbackGeneration) => {
  const isCurrent = () => isAuthenticatedRequestCurrent(epoch)
    && playbackGeneration === _playbackGeneration;
  if (!isCurrent()) return null;
  repickRequest?.abort();
  const request = repickRequest = new AbortController();
  const state = await requestJsonBestEffort("/api/repick-next", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(blacklist ? { blacklist } : {}),
  }, (errorValue) => {
    if (!request.signal.aborted && isCurrent()) announceRequestError(errorValue);
  });
  if (state && _applyState && !request.signal.aborted && isCurrent()) _applyState(state);
  return state;
});
function requestRepick(blacklist) {
  return runRepick(
    blacklist, captureAuthenticatedRequestEpoch(), _playbackGeneration,
  );
}

export function startCrossfade(nextPath, fadeSec, serverLed = false) {
  if (!_ctx || crossfading || !nextPath) return Promise.resolve(false);
  crossfading = true;
  const operationGeneration = _playbackGeneration;
  dbg("crossfade ->", nextPath, "| fade=", fadeSec.toFixed(2), "s",
    "| serverLed=", serverLed);

  const standby = deckStandby();
  let cleanupMetadata = () => {};
  setSrcOnDeck(standby, nextPath);
  standby.gain.gain.setValueAtTime(0, _ctx.currentTime);
  // Mixxx-style leading-silence skip — when the server has reported
  // intro_start_s (the first sound) for the next track and the user has
  // chosen full_intro_outro or fixed_skip_silence, seek the standby deck
  // to it.  The intro itself plays under the outgoing outro: skipping to
  // intro_end_s cut into verses sung over a quiet intro.  outro_fade +
  // fixed leave the deck at 0.
  const skipSilence = (_transitionMode === "full_intro_outro"
                       || _transitionMode === "fixed_skip_silence")
                      && typeof _nextTrackIntroStartCache === "number"
                      && _nextTrackIntroStartCache > 0;
  if (skipSilence) {
    // Wait for the loaded-metadata event so currentTime can be set.  Clamp
    // the seek target against the actual duration once metadata is known
    // — a server-side marker can outrun the real track length when the
    // FAISS index has stale or wrong-length entries, and an out-of-range
    // assignment seeks to the end + decode tail (silent crossfade).
    const seekTarget = _nextTrackIntroStartCache;
    const seekIfReady = () => {
      if (operationGeneration !== _playbackGeneration) return;
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
      cleanupMetadata = () => standby.audio.removeEventListener("loadedmetadata", seekIfReady);
    }
  }
  playOnDeck(standby);

  const active = deckActive();
  const t0 = _ctx.currentTime;
  // A fade of 0 (crossfade seconds 0) is a cut, as in the server mix:
  // no transition effect, the incoming deck at full at once.
  const cut = !(fadeSec > 0);

  // Resolve + apply the chosen transition effect over the fade window.
  const fxName = cut ? "none" : _resolveTransition(_lastTransitionFx);
  // Resolve effect-preferred duration UP FRONT so the gain ramp, the
  // effect scheduling, and the cleanup setTimeout all use the SAME
  // timeline.  Earlier code resolved this inside applyTransitionFx
  // which left the gain ramp ending before / after the effect tail and
  // caused audible cuts (effect-shorter-than-fade) or trailing silence
  // (effect-longer-than-fade).
  const effectDur = cut ? 0 : _effectDurationFor(fxName, fadeSec, _currentOutroLenCache);
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
  standby.gain.gain.cancelScheduledValues(t0);
  if (cut) {
    active.gain.gain.setValueAtTime(0, t0);
    standby.gain.gain.setValueAtTime(1, t0);
  } else {
    active.gain.gain.setValueAtTime(active.gain.gain.value, t0);
    active.gain.gain.linearRampToValueAtTime(0, t0 + effectDur);
    standby.gain.gain.setValueAtTime(0, t0);
    if (_fadeInSecondsCache <= 0) {
      standby.gain.gain.setValueAtTime(1, t0);
    } else {
      const fadeInDur = Math.min(_fadeInSecondsCache, effectDur);
      standby.gain.gain.linearRampToValueAtTime(1, t0 + fadeInDur);
    }
  }

  // Pass the resolved duration so applyTransitionFx no longer recomputes
  // it -- prevents the duration drift that caused cuts.
  const teardownFx = applyTransitionFx(fxName, effectDur, active, standby);

  suppressAdvance = true;
  // The next track is spoken for: until a state names a newer one, it
  // must not be faded into a second time (a long outage sends no state).
  if (_nextTrackPathCache === nextPath) _nextTrackPathCache = null;
  if (!serverLed) {
    void requestAdvance(active.path);
  }

  return new Promise((resolve) => {
    const pending = {
      cleanupMetadata,
      generation: operationGeneration,
      resolve,
      teardownFx,
      timer: null,
    };
    _pendingCrossfade = pending;
    pending.timer = setTimeout(() => {
      if (_pendingCrossfade !== pending || operationGeneration !== _playbackGeneration) {
        resolve(false);
        return;
      }
      _pendingCrossfade = null;
      cleanupMetadata();
      try { teardownFx(); } catch (errorValue) { announceRequestError(errorValue); }
      activeIdx ^= 1;
      crossfading = false;
      suppressAdvance = false;
      try { active.audio.pause(); } catch (_) {}
      try { active.audio.removeAttribute("src"); } catch (_) {}
      active.path = null;
      try { active.audio.load(); } catch (_) {}
      resolve(true);
    }, effectDur * 1000 + 100);
  });
}

// A deck whose file stopped loading with a network error, and how it is
// being retried.  A network error says nothing about the file: during an
// outage every load fails.  So the load is tried again, once the link is
// back if it is down, and only a second network error with the link up
// counts the file as bad (skipped, or replaced as the next track).
const _mediaRetry = new Map();   // deck -> { path, position, tried, waitingForLink, spoken, timer }
const MEDIA_RETRY_MS = 2000;
const MEDIA_WAITING_TEXT =
  "The music stopped: the connection to AutoDJ dropped. "
  + "It carries on when the connection is back.";

function reloadDeck(deck, retry) {
  retry.waitingForLink = false;
  if (!playbackEnabled || deck.path !== retry.path) {
    _mediaRetry.delete(deck);
    return;
  }
  // A new src, even for the same file, so the element fetches again.
  deck.path = null;
  setSrcOnDeck(deck, retry.path);
  if (deck !== deckActive() && !crossfading) {
    try { deck.audio.load(); } catch (_) {}
    return;
  }
  const resumeAt = () => {
    try { deck.audio.currentTime = retry.position; } catch (_) {}
  };
  if (retry.position > 0) deck.audio.addEventListener("loadedmetadata", resumeAt, { once: true });
  if (!_paused) playOnDeck(deck);
}

// Returns true when the error is being retried, false when the file
// should be treated as bad.
function retryAfterNetworkError(deck) {
  const known = _mediaRetry.get(deck);
  if (known && known.path === deck.path && known.tried && _linkUp) {
    _mediaRetry.delete(deck);
    return false;
  }
  const retry = known && known.path === deck.path
    ? known
    : {
      path: deck.path, position: 0, tried: false, waitingForLink: false, spoken: false, timer: null,
    };
  _mediaRetry.set(deck, retry);
  try {
    const position = deck.audio.currentTime;
    if (Number.isFinite(position) && position > 0) retry.position = position;
  } catch (_) {}
  clearTimeout(retry.timer);
  retry.timer = null;
  if (!_linkUp) {
    retry.waitingForLink = true;
    if (deck === deckActive() && !retry.spoken) {
      retry.spoken = true;
      announceEngineError(MEDIA_WAITING_TEXT);
    }
    return true;
  }
  retry.tried = true;
  retry.timer = setTimeout(() => {
    retry.timer = null;
    if (_mediaRetry.get(deck) !== retry) return;
    if (_linkUp) reloadDeck(deck, retry);
    else retry.waitingForLink = true;
  }, MEDIA_RETRY_MS);
  return true;
}

// "ended" on either deck = unconditional advance to next track when not
// already mid-crossfade (no next_track queued, or fade window missed).
for (const d of decks) {
  d.audio.addEventListener("ended", () => {
    if (suppressAdvance || crossfading) return;
    void requestAdvance();
  });
  // A deck that plays again has recovered from any network error.
  d.audio.addEventListener("playing", () => {
    const retry = _mediaRetry.get(d);
    if (retry && retry.path === d.path) {
      clearTimeout(retry.timer);
      _mediaRetry.delete(d);
    }
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
    if (e && e.code === 1) return;
    // A stopped page has nothing to skip: the error belongs to a deck the
    // stop tore down, and skipping would move the server for every page.
    if (!playbackEnabled) return;
    if (e && e.code === 2 && d.path && retryAfterNetworkError(d)) return;
    const isActive = d === deckActive();
    if (isActive) {
      // Active deck failed mid-playback — auto-advance.
      msg += " — auto-skipping.";
      void requestAdvance();
    } else {
      // Standby deck (the prefetched next track) failed to load.  Don't
      // advance the live track — just ask the server for a different next
      // track and let the live one keep playing.  Blacklist the bad path
      // so similarity won't immediately re-pick it.
      msg += " — picking a different next track.";
      void requestRepick(path);
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
    announceEngineError(msg);
  });
  // Watch active deck's currentTime for crossfade trigger.
  d.audio.addEventListener("timeupdate", () => {
    if (d !== deckActive()) return;
    if (crossfading || !playbackEnabled) return;
    const dur = d.audio.duration;
    if (!isFinite(dur) || dur <= 0) return;
    const remaining = dur - d.audio.currentTime;
    const fadeSec = _fadeSecNow();
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
    // Phrase align: start on the phrase boundary nearest the time one of
    // the two triggers below would fire.  timeupdate comes only about 4
    // times a second, so the start is timed with a timer armed shortly
    // before the boundary.  A boundary missed by more than a moment (a
    // seek past it, a grid that arrived late) leaves today's triggers.
    const phraseStart = _nextTrackPathCache ? _phraseStartFor(dur, fadeSec, useMarker) : null;
    const ahead = phraseStart === null ? -Infinity : phraseStart - d.audio.currentTime;
    if (ahead >= -_PHRASE_LATE_S) {
      if (ahead <= 0) {
        _cancelPhraseStart();
        startCrossfade(_nextTrackPathCache, fadeSec);
        return;
      }
      if (ahead <= _PHRASE_LOOKAHEAD_S) _armPhraseStart(d, phraseStart);
    } else {
      _cancelPhraseStart();
      if (useMarker && d.audio.currentTime >= _currentOutroStartCache) {
        startCrossfade(_nextTrackPathCache, fadeSec);
        return;
      }
      if (remaining > 0 && remaining < fadeSec && _nextTrackPathCache) {
        startCrossfade(_nextTrackPathCache, fadeSec);
        return;
      }
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
let _fadeInSecondsCache = 3.0;
export let _currentOutroLenCache = null;
export let _currentOutroStartCache = null;   // active deck's outro_start_s
export let _nextTrackIntroEndCache = null;   // incoming track's intro_end_s
export let _nextTrackIntroStartCache = null; // incoming track's intro_start_s
export let _nextTrackPathCache = null;
let _transitionMode = "full_intro_outro";
let _prefetchEnabled = true;
let _silenceTriggerEnabled = true;

// --- Beat- + key-sync transition FX caches.  Populated from
// applyBrowserPlaybackState whenever a state push lands; consumed by
// _BS.refresh() at the start of every crossfade so per-effect timing
// can snap to downbeats and oscillator-FX can tune to root notes. ---
let _beatSyncEnabled = true;
let _keySyncEnabled = true;
export let _beatmatchOnSkip = false;
export let _outBpmCache = 0;
export let _inBpmCache = 0;
let _outDownbeatsCache = [];
let _outKeyHzCache = null;
let _inKeyHzCache = null;
// Phrase alignment ([djmix] phrase_align / phrase_bars) and the bar number
// of _outDownbeatsCache[0] in the server's grid (null when synthesised).
let _phraseAlignCache = false;
let _phraseBarsCache = 8;
let _outFirstBarCache = null;

// Mixxx-style fade-length picker.  Mirrors AutoDJProcessor's
// TransitionMode enum -- see CHANGELOG entry for 0.12.3.
//
// - full_intro_outro: align outgoing outro start with incoming intro
//   start; fade length = min(outroLen, intro length) clamped 1.0-12.0 s,
//   where the intro runs from nextIntroStart (0 when unknown) to
//   nextIntroEnd.
// - outro_fade: fade length = outroLen (clamped); ignore the intro.
// - fixed_skip_silence: baseFade as-is; the leading-silence skip is
//   applied to the standby deck in startCrossfade().
// - fixed (and fallback): plain fixed-length crossfade.
export function _resolveFadeSec(mode, baseFade, outroLen, nextIntroEnd, nextIntroStart = null) {
  const clamp = (v) => Math.max(1.0, Math.min(12.0, v));
  if (mode === "full_intro_outro"
      && typeof outroLen === "number" && outroLen > 0
      && typeof nextIntroEnd === "number" && nextIntroEnd > 0) {
    const introStart = typeof nextIntroStart === "number" ? nextIntroStart : 0;
    return clamp(Math.min(outroLen, nextIntroEnd - introStart));
  }
  if (mode === "outro_fade" && typeof outroLen === "number" && outroLen > 0) {
    return clamp(outroLen);
  }
  return baseFade;
}

// The fade length the timeupdate trigger uses (crossfade seconds, or the
// outro and intro markers in the marker modes).
function _fadeSecNow() {
  return _resolveFadeSec(
    _transitionMode, _crossfadeSecondsCache,
    _currentOutroLenCache, _nextTrackIntroEndCache, _nextTrackIntroStartCache,
  );
}

// --- Phrase-aligned crossfade start ([djmix] phrase_align, phrase_bars).
//
// The same rule as the server mix (Player._crossfade_start_in_a with
// dj_meta.nearest_phrase_boundary): a phrase boundary is every
// phrase_bars-th bar of the beat grid, counted from its first beat.  The
// boundary nearest the time the fade would start anyway is used when it
// lies within half a phrase of that time and the fade still ends by the
// end of the track; otherwise the start is left as it was.  Where the
// page differs from the server:
//   - It has only the outro part of the grid (downbeats_outro, the last
//     32 bars) and the bar number of its first downbeat
//     (downbeats_outro_first_bar), so a fade that would start earlier
//     than about 32 bars from the end is not moved.  Boundaries are a
//     phrase apart, so a boundary within half a phrase is the only one;
//     when the window holds it, it is the boundary the server picks.
//   - A phrase's length in seconds comes from the window's bar spacing;
//     the server averages the beat spacing over the whole track.
//   - The fade checked against the track end is the effect length this
//     page will play (_effectDurationFor), which can be longer than the
//     crossfade seconds the server checks.
//   - "Too short" counts bars (at least phrase_bars of them), where the
//     server counts beats (at least 4 x phrase_bars).
//   - A grid synthesised from the BPM (no detected beats) has no bar
//     number, so it is not used, as the server has no grid to use either.
const _PHRASE_LOOKAHEAD_S = 1.0;  // arm the start timer this long before the boundary
const _PHRASE_LATE_S = 0.5;       // a boundary passed by more than this is missed

// Pure boundary pick: the phrase boundary in `downbeats` (bar `firstBar`
// of the grid onward) nearest `target`, or null when there is none within
// half a phrase, the grid is too short, or a fade of `fadeDur` seconds
// from the boundary would run past `trackEnd`.
export function _phraseBoundary(downbeats, firstBar, bars, target, fadeDur, trackEnd) {
  if (!Array.isArray(downbeats) || downbeats.length < 2) return null;
  if (!Number.isInteger(firstBar) || firstBar < 0) return null;
  if (!Number.isInteger(bars) || bars < 1) return null;
  if (firstBar + downbeats.length < bars || !Number.isFinite(target)) return null;
  const barSec = (downbeats[downbeats.length - 1] - downbeats[0]) / (downbeats.length - 1);
  if (!(barSec > 0)) return null;
  let best = null;
  // Ties go to the earlier boundary, as Python's min() does.
  for (let i = (bars - (firstBar % bars)) % bars; i < downbeats.length; i += bars) {
    if (best === null || Math.abs(downbeats[i] - target) < Math.abs(best - target)) {
      best = downbeats[i];
    }
  }
  if (best === null || Math.abs(best - target) > (bars * barSec) / 2) return null;
  if (best < 0 || best + fadeDur > trackEnd) return null;
  return best;
}

// Seconds the next crossfade's effect will run, as startCrossfade works it
// out.  Random and rotate are not chosen until the fade starts, so they
// count as their longest effect.
function _plannedEffectDur(fadeSec) {
  if (!(fadeSec > 0)) return 0;
  const name = _lastTransitionFx;
  const names = name === "random" || name === "rotate"
    ? Object.keys(_EFFECTS).filter(_canRun)
    : [_resolveTransition(name)];
  return Math.max(0, ...names.map((n) => _effectDurationFor(n, fadeSec, _currentOutroLenCache)));
}

// Where the timeupdate trigger should start the fade with phrase align
// on, or null to leave it alone.  The unaligned start is the outro start
// in the marker modes, or fadeSec before the end, whichever comes first.
function _phraseStartFor(dur, fadeSec, useMarker) {
  if (!_phraseAlignCache) return null;
  const today = useMarker ? Math.min(_currentOutroStartCache, dur - fadeSec) : dur - fadeSec;
  return _phraseBoundary(
    _outDownbeatsCache, _outFirstBarCache, _phraseBarsCache,
    today, _plannedEffectDur(fadeSec), dur,
  );
}

// The armed start: { deck, at, id }, or null.
let _phraseTimer = null;

function _cancelPhraseStart() {
  if (_phraseTimer) clearTimeout(_phraseTimer.id);
  _phraseTimer = null;
}

// Start the crossfade when `deck` reaches `at` (its audio time).  The
// callback checks again that the fade is still wanted: a pause, a skip
// or a seek back since arming leaves it to the next timeupdate.
function _armPhraseStart(deck, at) {
  if (_phraseTimer && _phraseTimer.deck === deck && _phraseTimer.at === at) return;
  _cancelPhraseStart();
  const generation = _playbackGeneration;
  const audio = deck.audio;
  const wait = (at - audio.currentTime) / (audio.playbackRate || 1);
  const timer = { deck, at, id: 0 };
  timer.id = setTimeout(() => {
    _phraseTimer = null;
    if (generation !== _playbackGeneration || deck !== deckActive() || crossfading
        || !playbackEnabled || audio.paused || !_nextTrackPathCache) return;
    const early = at - audio.currentTime;
    if (early > 0.005) {
      // Woke before the boundary (timer granularity, a stalled deck):
      // wait out the rest.  Further off means a seek back.
      if (early <= _PHRASE_LOOKAHEAD_S) _armPhraseStart(deck, at);
      return;
    }
    startCrossfade(_nextTrackPathCache, _fadeSecNow());
  }, Math.max(0, wait * 1000));
  _phraseTimer = timer;
}

const BLOCKED_PLAY_TEXT = "The browser did not start playback. Press Play again.";

// Play from the Play button, the first time and after every hard stop.
// Resolves true only once the deck is really playing, so the caller's
// "Playing" is never said over silence; otherwise it says why and
// resolves false.
//
// beforePlay, when given, is the request that picks what plays (Play now
// on a search result).  It runs after the unlock, which has to happen in
// the click, and before the current track is read, so the deck starts on
// the chosen track.  Its failure is thrown to the caller unannounced.
export async function unlockAndPlay(beforePlay = null) {
  const epoch = captureAuthenticatedRequestEpoch();
  // Both calls run synchronously inside the click: Firefox only lets a
  // context resume, and a deck play, while the user gesture is current.
  ensureAudioGraph();
  const resuming = _ctx && _ctx.state !== "running" ? _ctx.resume() : null;
  // Start a silent play() on the active deck to satisfy iOS gesture rule.
  playOnDeck(deckActive());

  if (beforePlay) {
    try {
      await beforePlay();
    } catch (err) {
      if (!playbackEnabled) {
        try { deckActive().audio.pause(); } catch (_) {}
      }
      throw err;
    }
  }
  // Pull current state and load the active deck with the current track.
  if (resuming) {
    try { await resuming; } catch (_) { /* checked below, before Playing */ }
  }
  let state;
  try {
    state = await requestJson("/api/status");
    // A paused server pauses the deck again on its next push: the first
    // Play after pairing said "Playing" and nothing played (D17).  Play
    // means play, so the server is unpaused before the deck starts.
    if (state.is_paused && isAuthenticatedRequestCurrent(epoch)) {
      const reply = await requestJson("/api/pause", { method: "POST" });
      if (reply.paused) throw new Error("the server stayed paused");
      state = { ...state, is_paused: false };
    }
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    announceEngineError("Cannot reach server: " + (err.message || err), { force: true });
    throw err;
  }
  if (!isAuthenticatedRequestCurrent(epoch)) return false;
  const path = state.current_track ? state.current_track.path : null;
  if (!path) {
    announceEngineError("No current track on server.", { force: true });
    throw new Error("no current track");
  }
  const deck = deckActive();
  setSrcOnDeck(deck, path);
  let started = true;
  try {
    await deck.audio.play();
  } catch (err) {
    console.warn("deck.play failed:", err);
    started = false;
  }
  if (!isAuthenticatedRequestCurrent(epoch)) {
    stopAllDecks();
    return false;
  }
  if (!started || (_ctx && _ctx.state !== "running")) {
    try { deck.audio.pause(); } catch (_) {}
    announceEngineError(BLOCKED_PLAY_TEXT, { force: true });
    return false;
  }
  playbackEnabled = true;
  restoreDeckGains();   // a hard stop left them at 0
  applyVolume();
  if (_applyState) _applyState(state);    // refresh UI from /api/status
  return true;
}

export function applyBrowserPlaybackState(s) {
  // When the server has its own audio output, the browser stays out of
  // the way (no decks fired up, no crossfade, no advance posts).
  if (!s.browser_playback) return;
  // A push from before the server took this page's advance: applying it
  // would fade back to the track the page has just left.
  if (isStaleAdvanceState(s)) return;

  // 0 is a real setting (cut between tracks), not a missing one.
  const crossfade = s.settings && s.settings.playback
    && s.settings.playback.crossfade_seconds;
  _crossfadeSecondsCache = typeof crossfade === "number" && crossfade >= 0 ? crossfade : 3.0;
  _fadeInSecondsCache = (s.settings && s.settings.playback &&
    typeof s.settings.playback.fade_in_seconds === "number")
    ? s.settings.playback.fade_in_seconds : 3.0;
  _nextTrackPathCache = s.next_track ? s.next_track.path : null;
  _lastTransitionFx = (s.settings && s.settings.transition) || "none";
  _transitionMode = (s.settings && s.settings.playback &&
    s.settings.playback.transition_mode) || "full_intro_outro";
  const wetMix = s.settings && s.settings.playback
    && s.settings.playback.transition_wet_mix;
  _wetMixCache = typeof wetMix === "number" ? Math.min(1, Math.max(0, wetMix)) : 1;
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
  _nextTrackIntroStartCache = (s.next_track
      && typeof s.next_track.intro_start_s === "number")
    ? s.next_track.intro_start_s : null;
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
  _outFirstBarCache = (s.current_track
      && Number.isInteger(s.current_track.downbeats_outro_first_bar))
    ? s.current_track.downbeats_outro_first_bar : null;
  const djmix = s.settings && s.settings.djmix;
  _phraseAlignCache = Boolean(djmix && djmix.phrase_align);
  _phraseBarsCache = djmix && Number.isInteger(djmix.phrase_bars) ? djmix.phrase_bars : 8;
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

  // Sync server-driven pause / mute with the browser deck.  Mute puts
  // the master at 0, which silences effects and liners too; pause puts
  // the music and its effects at 0 and stops the decks, while a Test
  // liner can still be heard.  Neither touches the deck gains (a
  // crossfade in progress).  Muted music keeps playing, silently.
  _muted = Boolean(s.is_muted);
  _paused = Boolean(s.is_paused);
  if (_ctx) {
    applyVolume();
    if (s.is_paused) {
      suppressAdvance = true;
      // Pause BOTH decks during a crossfade — pausing only the active
      // (outgoing) deck would leave the incoming standby deck audible
      // and the user's pause click would feel like a duck rather than
      // a stop.  Off-crossfade, only the active deck is playing.
      for (const d of decks) {
        try { d.audio.pause(); } catch (_) {}
      }
    } else {
      // Pause set suppressAdvance; only a crossfade still running may keep
      // it.  Left set, the "ended" fallback stayed off after any pause, and
      // with no crossfade to start the next track the music stopped dead
      // at the end of the track.
      if (!crossfading) suppressAdvance = false;
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
    }
  }
}

// ----------------------------------------------------------------
// Cover art
// ----------------------------------------------------------------

// The cover art probe in flight: a newer track aborts it.
let coverRequest = null;

// Keep the 96 px box in the layout when there is no art.  Hiding the
// <img> moved the title, badges and Camelot wheel 111 px sideways on
// every track change, and a library where most tracks carry no embedded
// art spends most of its time in the "no art" case.
function showArtPlaceholder(element) {
  element.removeAttribute("src");
  element.classList.add("no-art");
  element.hidden = false;
}

export function loadCoverArt(trackPath) {
  // A request probe avoids the console noise caused by an image 404.
  // but <img>.onerror logs a console error for every 404, which spams
  // DevTools on tracks without embedded art.  fetch returns ok=false on
  // 404 without logging.  Set <img>.src only after we know the response
  // is a real image.
  coverRequest?.abort();
  if (!trackPath) {
    showArtPlaceholder(coverArt);
    return;
  }
  const request = coverRequest = new AbortController();
  const url = `/api/art?path=${encodeURIComponent(trackPath)}`;
  void probeResource(url, { method: "GET", signal: request.signal }).then((exists) => {
    if (request.signal.aborted) return;
    if (!exists) {
      showArtPlaceholder(coverArt);
      return;
    }
    coverArt.src = url;
    coverArt.classList.remove("no-art");
    coverArt.hidden = false;
  }).catch((errorValue) => {
    if (request.signal.aborted) return;
    showArtPlaceholder(coverArt);
    announceRequestError(errorValue);
  });
}


// Reset only the transition markers, without canceling art or decode
// ownership.  Part of the hard stop after the session ends.
export function resetTransitionCaches() {
  _currentOutroLenCache    = null;
  _currentOutroStartCache  = null;
  _nextTrackIntroEndCache  = null;
  _nextTrackIntroStartCache = null;
  _nextTrackPathCache      = null;
}

// Full protected-session reset used only after confirmed auth expiry.
export function resetTrackCaches() {
  resetTransitionCaches();
  coverRequest?.abort();
  _bufferGeneration += 1;
  _bufferCache.clear();
  _bufferPending.clear();
}

// Setter for `_lastBrowserPlayback` so app.js (which mirrors this from
// the WS state push) can update the binding without violating the ES
// module import-reassignment rule.
export function setLastBrowserPlayback(v) {
  _lastBrowserPlayback = !!v;
}
