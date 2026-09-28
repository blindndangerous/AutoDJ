// Transition effects must sit under the music, never over it.
//
// A fake AudioContext records the node graph the engine builds for each
// effect.  From that graph the tests check two things: every sound the
// page makes reaches the speakers through the one master gain that
// carries the page volume and mute, and no effect is louder than the
// deck it treats.  "Louder" is measured as a worst-case peak gain: the
// sum over every signal path from a source to the master of the product
// of each node's largest gain, so feedback loops count at their full
// 1 / (1 - loop gain) build-up and parallel paths are added as if in
// phase.  A reverb is counted at its RMS gain (a noise impulse response
// has no meaningful peak), using the normalisation Web Audio applies to
// a ConvolverNode.  The untouched deck signal (source straight into its
// deck gain) is the reference, at gain 1, and is left out of the sum.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SAMPLE_RATE = 48000;

const EFFECTS = [
  "echo_out", "reverb_tail", "highpass_sweep", "lowpass_sweep", "tape_stop",
  "gate_stutter", "noise_riser", "noise_drop", "cross_eq_swap", "bitcrusher",
  "flanger", "pitch_swell", "pitch_fall", "telephone", "backspin",
  "forward_spin", "chorus", "submerge", "vinyl_wow", "freeze", "glitch",
  "scratch", "beat_repeat", "sidechain_pump", "reverse_reverb", "air_horn",
  "vinyl_rewind", "transformer", "dub_siren", "stutter_build", "wow_flutter",
  "phaser", "ring_modulator", "dub_delay", "halftime",
];

class FakeParam {
  constructor(value) {
    this._value = value;
    this.events = [];
    this.inputs = new Set();
  }
  get value() { return this._value; }
  set value(v) { this._value = v; this.events.push(v); }
  setValueAtTime(v) { this._value = v; this.events.push(v); return this; }
  linearRampToValueAtTime(v) { this._value = v; this.events.push(v); return this; }
  exponentialRampToValueAtTime(v) { this._value = v; this.events.push(v); return this; }
  setTargetAtTime(v) { this._value = v; this.events.push(v); return this; }
  cancelScheduledValues() { return this; }
  // The default only counts when the engine never sets the parameter.
  get max() { return Math.max(...this.values.map(Math.abs)); }
  get highest() { return Math.max(...this.values); }
  get values() { return this.events.length ? this.events : [this._value]; }
}

class FakeNode {
  constructor(ctx, kind, props = {}) {
    this.context = ctx;
    this.kind = kind;
    this.outputs = new Set();
    Object.assign(this, props);
    ctx.nodes.push(this);
  }
  connect(target) {
    this.outputs.add(target);
    if (target instanceof FakeParam) target.inputs.add(this);
    return target;
  }
  disconnect() {
    for (const t of this.outputs) if (t instanceof FakeParam) t.inputs.delete(this);
    this.outputs.clear();
  }
  start() {}
  stop() {}
  addEventListener() {}
}

function fakeBuffer(channels, length, sampleRate, fill = 0) {
  const data = Array.from({ length: channels }, () => new Float32Array(length).fill(fill));
  return {
    numberOfChannels: channels,
    length,
    sampleRate,
    duration: length / sampleRate,
    getChannelData: (c) => data[c],
  };
}

class FakeContext {
  constructor({ worklets }) {
    this.nodes = [];
    this.sampleRate = SAMPLE_RATE;
    this.currentTime = 2;
    this.state = "running";
    this.destination = new FakeNode(this, "destination");
    if (worklets) this.audioWorklet = { addModule: () => Promise.resolve() };
  }
  resume() { return Promise.resolve(); }
  createGain() { return new FakeNode(this, "gain", { gain: new FakeParam(1) }); }
  createBiquadFilter() {
    return new FakeNode(this, "biquad", {
      type: "lowpass", frequency: new FakeParam(350), Q: new FakeParam(1),
      gain: new FakeParam(0),
    });
  }
  createDelay() { return new FakeNode(this, "delay", { delayTime: new FakeParam(0) }); }
  createConvolver() { return new FakeNode(this, "convolver", { buffer: null, normalize: true }); }
  createWaveShaper() { return new FakeNode(this, "shaper", { curve: null, oversample: "none" }); }
  createOscillator() {
    return new FakeNode(this, "oscillator", {
      type: "sine", frequency: new FakeParam(440), detune: new FakeParam(0),
    });
  }
  createBufferSource() {
    return new FakeNode(this, "buffer", { buffer: null, loop: false, playbackRate: new FakeParam(1) });
  }
  createMediaElementSource() { return new FakeNode(this, "media"); }
  createAnalyser() {
    return new FakeNode(this, "analyser", { fftSize: 0, getFloatTimeDomainData() {} });
  }
  createBuffer(channels, length, sampleRate) { return fakeBuffer(channels, length, sampleRate); }
  // Decoded music at full scale: the loudest a deck can play.
  decodeAudioData() { return Promise.resolve(fakeBuffer(2, SAMPLE_RATE * 30, SAMPLE_RATE, 1)); }
}

class FakeWorkletNode extends FakeNode {
  constructor(ctx, name) {
    const params = new Map();
    super(ctx, "worklet", {
      name,
      parameters: { get: (key) => {
        if (!params.has(key)) params.set(key, new FakeParam(0));
        return params.get(key);
      } },
    });
  }
}

// Largest gain of a Web Audio biquad.  Lowpass and highpass read Q in dB.
function biquadPeak(node) {
  if (node.type === "lowpass" || node.type === "highpass") {
    const q = 10 ** (node.Q.highest / 20);
    return q <= Math.SQRT1_2 ? 1 : q / Math.sqrt(1 - 1 / (4 * q * q));
  }
  return 1;
}

// RMS gain of a ConvolverNode after Web Audio's impulse normalisation.
function convolverGain(node) {
  const buf = node.buffer;
  if (!buf) return 0;
  let power = 0;
  const perChannel = [];
  for (let c = 0; c < buf.numberOfChannels; c++) {
    let sum = 0;
    for (const v of buf.getChannelData(c)) sum += v * v;
    perChannel.push(sum);
    power += sum;
  }
  let scale = 1;
  if (node.normalize) {
    const rms = Math.max(Math.sqrt(power / (buf.numberOfChannels * buf.length)), 0.000125);
    scale = (0.00125 / rms) * (44100 / buf.sampleRate);
  }
  return Math.max(...perChannel.map((sum) => scale * Math.sqrt(sum)));
}

function bufferPeak(node) {
  const buf = node.buffer;
  if (!buf) return 0;
  let peak = 0;
  for (let c = 0; c < buf.numberOfChannels; c++) {
    for (const v of buf.getChannelData(c)) peak = Math.max(peak, Math.abs(v));
  }
  return peak;
}

// Worst-case amplitude every node puts out when only `sources` play at
// their own level.  Iterates to a fixed point so feedback loops settle
// at their full build-up; a loop gain of 1 or more never settles and
// comes back as Infinity.
function propagate(ctx, sources, { skipEdge = () => false, modulation = null } = {}) {
  const out = new Map(ctx.nodes.map((n) => [n, 0]));
  const gainOf = (n) => {
    switch (n.kind) {
      case "gain": {
        let g = n.gain.max;
        if (modulation) for (const m of n.gain.inputs) g += modulation.get(m) || 0;
        return g;
      }
      case "biquad": return biquadPeak(n);
      case "convolver": return convolverGain(n);
      default: return 1;
    }
  };
  for (let iter = 0; iter < 5000; iter++) {
    const input = new Map(ctx.nodes.map((n) => [n, 0]));
    for (const n of ctx.nodes) {
      for (const t of n.outputs) {
        if (t instanceof FakeParam || skipEdge(n, t)) continue;
        input.set(t, input.get(t) + out.get(n));
      }
    }
    let change = 0;
    for (const n of ctx.nodes) {
      let v;
      if (sources.has(n)) v = sources.get(n);
      else if (n.kind === "shaper") v = input.get(n) > 0 ? Math.max(...n.curve.map(Math.abs)) : 0;
      else v = gainOf(n) * input.get(n);
      change = Math.max(change, Math.abs(v - out.get(n)));
      out.set(n, v);
    }
    if (change < 1e-12) return { out, input };
    if (!Number.isFinite(change) || change > 1e6) break;
  }
  return { out: new Map(ctx.nodes.map((n) => [n, Infinity])), input: new Map() };
}

function reaches(node, target, seen = new Set()) {
  if (node === target) return true;
  if (seen.has(node)) return false;
  seen.add(node);
  for (const t of node.outputs) {
    if (!(t instanceof FakeParam) && reaches(t, target, seen)) return true;
  }
  return false;
}

// An LFO: every path out of it ends on an AudioParam, never at a speaker.
function drivesOnlyParams(node, seen = new Set()) {
  if (seen.has(node)) return true;
  seen.add(node);
  return [...node.outputs].every((t) => t instanceof FakeParam || drivesOnlyParams(t, seen));
}

function jsonResponse(body) {
  return new globalThis.Response(JSON.stringify(body), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
}

function installDom() {
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
  for (const audio of document.querySelectorAll("audio")) {
    audio.play = vi.fn().mockResolvedValue(undefined);
    audio.pause = vi.fn();
    audio.load = vi.fn();
    Object.defineProperty(audio, "currentTime", { configurable: true, writable: true, value: 20 });
  }
}

async function settle() {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

// Run one transition and return the engine and the graph it built.
async function runEffect(effect, { volume = 0.5, wetMix = 1, worklets = true, muted = false } = {}) {
  installDom();
  let ctx = null;
  vi.stubGlobal("AudioContext", vi.fn(function AudioContextMock() {
    ctx = new FakeContext({ worklets });
    return ctx;
  }));
  window.AudioContext = globalThis.AudioContext;
  vi.stubGlobal("AudioWorkletNode", FakeWorkletNode);
  vi.stubGlobal("fetch", vi.fn(async (url) => (String(url).startsWith("/api/audio")
    ? new globalThis.Response(new Uint8Array([1, 2, 3]), {
      status: 200, headers: { "Content-Type": "audio/mpeg" },
    })
    : jsonResponse({ ok: true }))));
  const engine = await import("../../src/autodj/static/modules/audio-engine.js");
  engine.setVolume(volume);
  engine.applyBrowserPlaybackState({
    browser_playback: true,
    current_track: { path: "current.mp3", bpm: 124, key_hz: 220 },
    next_track: { path: "next.mp3", bpm: 124, key_hz: 220 },
    is_muted: muted,
    is_paused: false,
    settings: {
      transition: effect,
      playback: { fade_in_seconds: 0, transition_wet_mix: wetMix },
    },
  });
  engine.ensureAudioGraph();
  await settle();
  engine.setSrcOnDeck(engine.decks[0], "current.mp3");
  void engine.startCrossfade("next.mp3", 6, true);
  await settle();
  return { ctx, engine };
}

// Worst-case gain from each source to the master when that source alone
// plays at full scale, leaving out the untouched deck path.
function effectGains(ctx, engine) {
  const master = engine._master;
  const plain = new Map(engine.decks.map((d) => [d.source, d.gain]));
  const skipEdge = (from, to) => plain.get(from) === to;
  const effectSources = new Map(ctx.nodes
    .filter((n) => n.kind === "oscillator" || n.kind === "buffer")
    .map((n) => [n, n.kind === "buffer" ? bufferPeak(n) : 1]));
  const modulation = propagate(ctx, effectSources).out;
  const gains = [];
  for (const [source, level] of [
    ...engine.decks.map((d) => [d.source, 1]),
    ...effectSources,
  ]) {
    if (!reaches(source, master)) continue;
    const { input } = propagate(ctx, new Map([[source, level]]), { skipEdge, modulation });
    gains.push({ source: source.kind, gain: input.get(master) ?? Infinity });
  }
  return gains;
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("transition effect levels", () => {
  for (const worklets of [true, false]) {
    const mode = worklets ? "with worklets" : "without worklets";
    for (const effect of EFFECTS) {
      it(`${effect} ${mode}: every sound goes through the master volume`, async () => {
        const { ctx, engine } = await runEffect(effect, { worklets });
        const master = engine._master;
        expect(master.kind).toBe("gain");
        expect(master.gain.value).toBe(0.5);
        const intoSpeakers = ctx.nodes.filter((n) => n.outputs.has(ctx.destination));
        expect(intoSpeakers).toEqual([master]);
        for (const node of ctx.nodes) {
          if (node === master || node === ctx.destination || node.kind === "analyser") continue;
          if (node.outputs.size === 0 || drivesOnlyParams(node)) continue;
          expect(reaches(node, master), `${node.kind} in ${effect}`).toBe(true);
        }
        engine.stopAllDecks();
      });

      it(`${effect} ${mode}: no louder than the deck it treats`, async () => {
        const { ctx, engine } = await runEffect(effect, { worklets });
        const gains = effectGains(ctx, engine);
        for (const { source, gain } of gains) {
          expect(gain, `${source} in ${effect}`).toBeLessThanOrEqual(1 + 1e-9);
        }
        engine.stopAllDecks();
      });
    }
  }

  it("scales effect sound by the wet mix", async () => {
    const full = await runEffect("echo_out", { wetMix: 1 });
    const fullGain = Math.max(...effectGains(full.ctx, full.engine).map((g) => g.gain));
    full.engine.stopAllDecks();
    vi.resetModules();
    const half = await runEffect("echo_out", { wetMix: 0.5 });
    const halfGain = Math.max(...effectGains(half.ctx, half.engine).map((g) => g.gain));
    half.engine.stopAllDecks();
    expect(fullGain).toBeGreaterThan(0);
    expect(halfGain).toBeCloseTo(fullGain / 2, 9);
  });

  // The page volume now sits on the master alone, so an effect's own
  // envelope is the same at any volume.  At 5 % (the 2026-09-28 check)
  // the reverse reverb swell read 3.6 times the deck, and the noise and
  // spin tails ramped towards a fixed 0.001 that was above where they
  // started, so they rose instead of fading.
  const quiet = 10 ** (-57 / 20);

  it("keeps the reverse reverb swell under the deck at a low volume", async () => {
    const { ctx, engine } = await runEffect("reverse_reverb", { volume: quiet });
    expect(engine._master.gain.value).toBe(quiet);
    const gains = effectGains(ctx, engine).filter(({ source }) => source === "media");
    expect(gains.length).toBeGreaterThan(0);
    for (const { gain } of gains) expect(gain).toBeLessThanOrEqual(1);
    engine.stopAllDecks();
  });

  for (const effect of ["noise_riser", "noise_drop", "backspin", "forward_spin", "vinyl_rewind"]) {
    it(`${effect}: its tails fade out at a low volume instead of rising`, async () => {
      const { ctx, engine } = await runEffect(effect, { volume: quiet });
      const master = engine._master;
      const deckGains = new Set(engine.decks.map((d) => d.gain));
      const bus = ctx.nodes.find((n) => n.kind === "gain" && n !== master
        && !deckGains.has(n) && n.outputs.has(master));
      const layers = ctx.nodes.filter((n) => n.kind === "gain" && n.outputs.has(bus));
      expect(layers.length).toBeGreaterThan(0);
      for (const layer of layers) {
        const events = layer.gain.events;
        const peakAt = events.indexOf(Math.max(...events));
        const tail = events.slice(peakAt);
        for (let i = 1; i < tail.length; i++) {
          expect(tail[i], `${effect} step ${i}`).toBeLessThanOrEqual(tail[i - 1]);
        }
        expect(events.at(-1)).toBeLessThanOrEqual(events[peakAt] / 100);
      }
      engine.stopAllDecks();
    });
  }

  it("silences effects with the music when muted", async () => {
    const { engine } = await runEffect("echo_out", { muted: true });
    expect(engine._master.gain.value).toBe(0);
    engine.stopAllDecks();
  });
});
