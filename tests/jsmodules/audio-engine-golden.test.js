// Temporary: the effect refactor must build the same Web Audio graph.
//
// Runs every transition effect through the engine as it was before the
// refactor (golden/audio-engine-before.js, a verbatim copy with its
// imports repointed) and through the current engine, against a fake
// AudioContext that records every node, connection, parameter event,
// buffer and <audio> element write, and asserts the two records match.
// Deleted, with the copy, once the refactor is done.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SAMPLE_RATE = 8000;

const EFFECTS = [
  "echo_out", "reverb_tail", "highpass_sweep", "lowpass_sweep", "tape_stop",
  "gate_stutter", "noise_riser", "noise_drop", "cross_eq_swap", "bitcrusher",
  "flanger", "pitch_swell", "pitch_fall", "telephone", "backspin",
  "forward_spin", "chorus", "submerge", "vinyl_wow", "freeze", "glitch",
  "scratch", "beat_repeat", "sidechain_pump", "reverse_reverb", "air_horn",
  "vinyl_rewind", "transformer", "dub_siren", "stutter_build", "wow_flutter",
  "phaser", "ring_modulator", "dub_delay", "halftime",
];

class RecParam {
  constructor(owner, name, value) {
    this.owner = owner;
    this.name = name;
    this._value = value;
    this.log = [];
  }
  get value() { return this._value; }
  set value(v) { this._value = v; this.log.push(["value", v]); }
  setValueAtTime(v, t) { this._value = v; this.log.push(["set", v, t]); return this; }
  linearRampToValueAtTime(v, t) { this._value = v; this.log.push(["lin", v, t]); return this; }
  exponentialRampToValueAtTime(v, t) { this._value = v; this.log.push(["exp", v, t]); return this; }
  setTargetAtTime(v, t, c) { this._value = v; this.log.push(["target", v, t, c]); return this; }
  cancelScheduledValues(t) { this.log.push(["cancel", t]); return this; }
}

class RecNode {
  constructor(ctx, kind, args = []) {
    this.context = ctx;
    this.kind = kind;
    this.args = args;
    this.outputs = new Set();
    this.calls = [];
    this.params = {};
    ctx.nodes.push(this);
  }
  param(name, value) {
    const p = new RecParam(this, name, value);
    this.params[name] = p;
    this[name] = p;
    return p;
  }
  connect(target) { this.outputs.add(target); return target; }
  disconnect() { this.outputs.clear(); }
  start(...a) { this.calls.push(["start", ...a]); }
  stop(...a) { this.calls.push(["stop", ...a]); }
  addEventListener() {}
}

function digest(values) {
  if (!values) return null;
  let h = 0;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    sum += v;
    h = (h * 31 + Math.round(v * 1e6)) % 1000000007;
  }
  return [values.length, h, sum];
}

function recBuffer(channels, length, sampleRate, fill) {
  const data = Array.from({ length: channels }, (_, c) => {
    const arr = new Float32Array(length);
    if (fill) for (let i = 0; i < length; i++) arr[i] = fill(c, i);
    return arr;
  });
  return {
    numberOfChannels: channels, length, sampleRate, duration: length / sampleRate,
    getChannelData: (c) => data[c],
  };
}

class RecContext {
  constructor({ worklets }) {
    this.nodes = [];
    this.sampleRate = SAMPLE_RATE;
    this.currentTime = 2;
    this.state = "running";
    this.destination = new RecNode(this, "destination");
    if (worklets) this.audioWorklet = { addModule: () => Promise.resolve() };
  }
  resume() { return Promise.resolve(); }
  createGain() { const n = new RecNode(this, "gain"); n.param("gain", 1); return n; }
  createBiquadFilter() {
    const n = new RecNode(this, "biquad");
    n.type = "lowpass";
    n.param("frequency", 350); n.param("Q", 1); n.param("gain", 0);
    return n;
  }
  createDelay(...a) { const n = new RecNode(this, "delay", a); n.param("delayTime", 0); return n; }
  createConvolver() { const n = new RecNode(this, "convolver"); n.buffer = null; n.normalize = true; return n; }
  createWaveShaper() { const n = new RecNode(this, "shaper"); n.curve = null; n.oversample = "none"; return n; }
  createOscillator() {
    const n = new RecNode(this, "oscillator");
    n.type = "sine"; n.param("frequency", 440); n.param("detune", 0);
    return n;
  }
  createBufferSource() {
    const n = new RecNode(this, "buffer");
    n.buffer = null; n.loop = false; n.param("playbackRate", 1);
    return n;
  }
  createMediaElementSource() { return new RecNode(this, "media"); }
  createAnalyser() {
    const n = new RecNode(this, "analyser");
    n.fftSize = 0; n.getFloatTimeDomainData = () => {};
    return n;
  }
  createBuffer(channels, length, sampleRate) {
    const buf = recBuffer(channels, length, sampleRate);
    buf.args = [channels, length, sampleRate];
    return buf;
  }
  decodeAudioData() {
    return Promise.resolve(recBuffer(2, SAMPLE_RATE * 30, SAMPLE_RATE,
      (c, i) => (((i * 7 + c * 3) % 101) / 101) - 0.5));
  }
}

class RecWorkletNode extends RecNode {
  constructor(ctx, name, options) {
    super(ctx, "worklet", [name, options]);
    const params = new Map();
    this.parameters = { get: (key) => {
      if (!params.has(key)) params.set(key, this.param(key, 0));
      return params.get(key);
    } };
  }
}

function snapshot(ctx) {
  const id = new Map(ctx.nodes.map((n, i) => [n, i]));
  const ref = (t) => (t instanceof RecParam ? `${id.get(t.owner)}.${t.name}` : String(id.get(t)));
  return ctx.nodes.map((n) => {
    const props = {};
    for (const key of ["type", "loop", "oversample", "normalize", "fftSize"]) {
      if (key in n) props[key] = n[key];
    }
    if ("buffer" in n) {
      props.buffer = n.buffer && {
        args: n.buffer.args ?? null,
        data: Array.from({ length: n.buffer.numberOfChannels },
          (_, c) => digest(n.buffer.getChannelData(c))),
      };
    }
    if ("curve" in n) props.curve = digest(n.curve);
    return {
      kind: n.kind,
      args: n.args,
      props,
      params: Object.fromEntries(Object.entries(n.params).map(([k, p]) => [k, p.log])),
      outputs: [...n.outputs].map(ref).sort(),
      calls: n.calls,
    };
  });
}

function mulberry32(seed) {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function jsonResponse(body) {
  return new globalThis.Response(JSON.stringify(body), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
}

function installDom(audioLog) {
  document.body.innerHTML = `
    <input id="eq-low" type="range" value="100">
    <input id="eq-mid" type="range" value="100">
    <input id="eq-high" type="range" value="100">
    <span id="eq-low-value"></span><span id="eq-mid-value"></span>
    <span id="eq-high-value"></span><div id="eq-announce"></div>
    <button id="btn-eq-reset"></button>
    <div class="volume-row"><input id="vol" type="range" value="100"></div>
    <img id="cover-art"><div id="sr-status"></div>
    <audio id="browser-player"></audio><audio id="browser-player-b"></audio>
  `;
  for (const [index, audio] of [...document.querySelectorAll("audio")].entries()) {
    audio.play = vi.fn().mockResolvedValue(undefined);
    audio.pause = vi.fn();
    audio.load = vi.fn();
    Object.defineProperty(audio, "currentTime", { configurable: true, writable: true, value: 20 });
    for (const key of ["playbackRate", "preservesPitch"]) {
      let value = key === "playbackRate" ? 1 : true;
      Object.defineProperty(audio, key, {
        configurable: true,
        get: () => value,
        set: (v) => { value = v; audioLog.push([index, key, v]); },
      });
    }
  }
}

async function settle() {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

const SCENARIOS = [
  {
    name: "tempo and key known, wet 1",
    worklets: true,
    wetMix: 1,
    current: { bpm: 124, key_hz: 220, downbeats_outro: [20.4, 22.3, 24.2, 26.1] },
    next: { bpm: 128, key_hz: 247 },
  },
  {
    name: "tempo and key known, no worklets, wet 0.6, outro known",
    worklets: false,
    wetMix: 0.6,
    current: { bpm: 96, key_hz: 196, outro_len: 9 },
    next: { bpm: 100, key_hz: 262 },
  },
  {
    name: "nothing known, worklets",
    worklets: true,
    wetMix: 0.8,
    current: {},
    next: {},
  },
  {
    name: "nothing known, no worklets, wet 1",
    worklets: false,
    wetMix: 1,
    current: {},
    next: {},
  },
];

async function record(load, effect, scenario) {
  vi.resetModules();
  const audioLog = [];
  installDom(audioLog);
  let ctx = null;
  let now = 1000;
  const intervals = [];
  const rng = mulberry32(12345);
  vi.spyOn(Math, "random").mockImplementation(rng);
  vi.spyOn(globalThis.performance, "now").mockImplementation(() => now);
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "debug").mockImplementation(() => {});
  vi.stubGlobal("setInterval", (fn, ms) => { intervals.push({ fn, ms, live: true }); return intervals.length; });
  vi.stubGlobal("clearInterval", (handle) => { if (intervals[handle - 1]) intervals[handle - 1].live = false; });
  vi.stubGlobal("AudioContext", vi.fn(function AudioContextMock() {
    ctx = new RecContext({ worklets: scenario.worklets });
    return ctx;
  }));
  window.AudioContext = globalThis.AudioContext;
  vi.stubGlobal("AudioWorkletNode", RecWorkletNode);
  vi.stubGlobal("fetch", vi.fn(async (url) => (String(url).startsWith("/api/audio")
    ? new globalThis.Response(new Uint8Array([1, 2, 3]), {
      status: 200, headers: { "Content-Type": "audio/mpeg" },
    })
    : jsonResponse({ ok: true }))));
  const engine = await load();
  engine.setVolume(0.5);
  engine.applyBrowserPlaybackState({
    browser_playback: true,
    current_track: { path: "current.mp3", ...scenario.current },
    next_track: { path: "next.mp3", ...scenario.next },
    is_muted: false,
    is_paused: false,
    settings: {
      transition: effect,
      playback: { fade_in_seconds: 0, transition_wet_mix: scenario.wetMix },
    },
  });
  engine.ensureAudioGraph();
  await settle();
  engine.setSrcOnDeck(engine.decks[0], "current.mp3");
  void engine.startCrossfade("next.mp3", 6, true);
  await settle();
  const built = snapshot(ctx);
  const ticks = [];
  for (let step = 0; step < 400; step++) {
    now = 1000 + step * 37;
    for (const [i, iv] of intervals.entries()) {
      if (iv.live) { iv.fn(); ticks.push([step, i, audioLog.length]); }
    }
  }
  const driven = audioLog.slice();
  engine.stopAllDecks();
  const tornDown = snapshot(ctx);
  vi.restoreAllMocks();
  return {
    built, tornDown, driven, finalAudio: audioLog.slice(driven.length),
    intervals: intervals.map((iv) => [iv.ms, iv.live]), ticks,
  };
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("effect refactor keeps every effect's graph", () => {
  for (const scenario of SCENARIOS) {
    for (const effect of EFFECTS) {
      it(`${effect}: ${scenario.name}`, async () => {
        const before = await record(() => import("./golden/audio-engine-before.js"), effect, scenario);
        const after = await record(
          () => import("../../src/autodj/static/modules/audio-engine.js"), effect, scenario);
        // Every effect builds nodes or drives a deck's playback rate.
        expect(before.built.length > 10 || before.driven.length > 0).toBe(true);
        expect(after).toEqual(before);
      });
    }
  }
});
