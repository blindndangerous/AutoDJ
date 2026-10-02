import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The browser engine plays the transition effect the server planned
// (state next_transition_fx) for the pair it is fading, and falls back to
// its own pick when there is no plan for that pair.

function jsonResponse(body) {
  return new globalThis.Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function installDom() {
  document.body.innerHTML = `
    <input id="eq-low" type="range" value="100">
    <input id="eq-mid" type="range" value="100">
    <input id="eq-high" type="range" value="100">
    <span id="eq-low-value"></span>
    <span id="eq-mid-value"></span>
    <span id="eq-high-value"></span>
    <div id="eq-announce"></div>
    <button id="btn-eq-reset"></button>
    <div class="volume-row"><input id="vol" type="range" value="100"></div>
    <button id="btn-pause"></button>
    <img id="cover-art">
    <div id="now-playing-title">Artist — Title</div>
    <div id="sr-status"></div>
    <audio id="browser-player"></audio>
    <audio id="browser-player-b"></audio>
  `;
  for (const audio of document.querySelectorAll("audio")) {
    audio.play = vi.fn().mockResolvedValue(undefined);
    audio.pause = vi.fn();
    audio.load = vi.fn();
  }
}

function param(value = 0) {
  return {
    value,
    cancelScheduledValues: vi.fn(),
    exponentialRampToValueAtTime: vi.fn(),
    linearRampToValueAtTime: vi.fn(),
    setTargetAtTime: vi.fn(),
    setValueAtTime: vi.fn(),
    setValueCurveAtTime: vi.fn(),
  };
}

// A Web Audio node that takes any effect's wiring: every unknown
// property reads as an automatable parameter.
function node() {
  const base = {
    connect: vi.fn((target) => target),
    disconnect: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
    addEventListener: vi.fn(),
  };
  return new Proxy(base, {
    get(target, prop) {
      if (!(prop in target) && typeof prop === "string") target[prop] = param();
      return target[prop];
    },
  });
}

function buffer(channels = 2, length = 8000, sampleRate = 8000) {
  const data = Array.from({ length: channels }, () => new Float32Array(length));
  return { numberOfChannels: channels, length, sampleRate, duration: length / sampleRate,
    getChannelData: (c) => data[c] };
}

function installAudioContext() {
  const context = {
    currentTime: 2,
    sampleRate: 8000,
    state: "running",
    destination: node(),
    resume: vi.fn().mockResolvedValue(undefined),
    decodeAudioData: vi.fn().mockResolvedValue(buffer()),
    createBuffer: vi.fn((channels, length, rate) => buffer(channels, length, rate)),
    createPeriodicWave: vi.fn(() => ({})),
  };
  const ctx = new Proxy(context, {
    get(target, prop) {
      if (!(prop in target) && typeof prop === "string" && prop.startsWith("create")) {
        target[prop] = vi.fn(() => node());
      }
      return target[prop];
    },
  });
  vi.stubGlobal("AudioContext", vi.fn(function AudioContextMock() {
    return ctx;
  }));
  window.AudioContext = globalThis.AudioContext;
  return ctx;
}

let engine;
let played;   // the effect of each crossfade, from the engine's debug line

async function startEngine() {
  installDom();
  installAudioContext();
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ current_track: { path: "a.mp3" } })));
  engine = await import("../../src/autodj/static/modules/audio-engine.js");
  await engine.unlockAndPlay();
  vi.useFakeTimers();
  played = [];
  vi.spyOn(console, "debug").mockImplementation((label, fx) => {
    if (label === "autodj transition:") played.push(fx);
  });
}

function push({ current = "a.mp3", next = "b.mp3", transition = "none", fx, outro = {},
  playback = {}, djmix = {} } = {}) {
  engine.applyBrowserPlaybackState({
    browser_playback: true,
    current_track: current ? { path: current, ...outro } : null,
    next_track: next ? { path: next } : null,
    next_transition_fx: fx,
    is_muted: false,
    is_paused: false,
    settings: {
      transition,
      djmix,
      playback: { fade_in_seconds: 0, beat_sync_fx: false, prefetch_next_track: false,
        ...playback },
    },
  });
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("planned transition effect", () => {
  it.each(["auto", "random", "rotate", "none", "reverb_tail"])(
    "plays the server's plan with the %s setting",
    async (transition) => {
      await startEngine();
      const random = vi.spyOn(Math, "random");
      push({ transition, fx: "dub_delay" });
      void engine.startCrossfade("b.mp3", 3);
      expect(played).toEqual(["dub_delay"]);
      expect(random).not.toHaveBeenCalled();
    },
  );

  it("plays a plain crossfade when the server planned none", async () => {
    await startEngine();
    push({ transition: "auto", fx: "none" });
    void engine.startCrossfade("b.mp3", 3);
    expect(played).toEqual(["none"]);
  });

  it("plays the previous state's plan after a skip made the server advance first", async () => {
    await startEngine();
    push({ transition: "random", fx: "tape_stop" });
    // Skip: the server is on b.mp3 already and plans b -> c; the page
    // fades a.mp3 into b.mp3, the pair the earlier state planned.
    push({ transition: "random", current: "b.mp3", next: "c.mp3", fx: "echo_out" });
    expect(engine.crossfading).toBe(true);
    expect(played).toEqual(["tape_stop"]);
  });

  describe("falls back to the page's own pick", () => {
    it("with no plan", async () => {
      await startEngine();
      push({ transition: "reverb_tail", fx: null });
      void engine.startCrossfade("b.mp3", 3);
      expect(played).toEqual(["reverb_tail"]);
    });

    it("with no plan and the auto setting, as a plain crossfade", async () => {
      await startEngine();
      push({ transition: "auto", fx: null });
      void engine.startCrossfade("b.mp3", 3);
      expect(played).toEqual(["none"]);
    });

    it("for a plan naming another next track", async () => {
      await startEngine();
      push({ transition: "reverb_tail", next: "other.mp3", fx: "echo_out" });
      void engine.startCrossfade("b.mp3", 3);
      expect(played).toEqual(["reverb_tail"]);
    });

    it("for a plan from another current track (a stale push)", async () => {
      await startEngine();
      push({ transition: "reverb_tail", current: "elsewhere.mp3", fx: "echo_out" });
      // The state named another current track, so the page faded into
      // it; the plan was for elsewhere.mp3 -> b.mp3, not this fade.
      expect(played).toEqual(["reverb_tail"]);
    });

    it("for a plan that is not an effect this page has", async () => {
      await startEngine();
      push({ transition: "reverb_tail", fx: "laser_zap" });
      void engine.startCrossfade("b.mp3", 3);
      expect(played).toEqual(["reverb_tail"]);
    });

    it("for a planned effect this page cannot run (no AudioWorklet)", async () => {
      await startEngine();
      vi.spyOn(Math, "random").mockReturnValue(0);
      push({ transition: "random", fx: "gate_stutter" });
      void engine.startCrossfade("b.mp3", 3);
      expect(played).toHaveLength(1);
      expect(played[0]).not.toBe("gate_stutter");
      expect(played[0]).not.toBe("none");
    });

    it("once a newer state has no next track", async () => {
      await startEngine();
      push({ transition: "reverb_tail", fx: "echo_out" });
      push({ transition: "reverb_tail", next: null, fx: null });
      push({ transition: "reverb_tail", fx: null });
      void engine.startCrossfade("b.mp3", 3);
      expect(played).toEqual(["reverb_tail"]);
    });
  });
});

describe("phrase align with a planned effect", () => {
  // 120 BPM, 180 s track: the last 32 bars, one downbeat every 2 s from
  // 116 s, the first of them bar 58, so 8-bar phrase boundaries fall on
  // 128, 144, 160 and 176 s.  A fixed 3 s fade starts at 177 s today; the
  // nearest boundary is 176 s.  With an 8 s outro, random's longest
  // effect (noise riser, 7.2 s) would run past the end from 176 s, while
  // a planned scratch (2.4 s) fits.
  const GRID = Array.from({ length: 32 }, (_, i) => 116 + 2 * i);
  const phrase = {
    transition: "random",
    outro: { bpm: 120, outro_len: 8, downbeats_outro: GRID, downbeats_outro_first_bar: 58 },
    playback: { transition_mode: "fixed", crossfade_seconds: 3, silence_trigger_crossfade: false },
    djmix: { phrase_align: true, phrase_bars: 8 },
  };

  function tick(seconds) {
    const deck = engine.decks[engine.activeIdx];
    Object.defineProperty(deck.audio, "duration", { configurable: true, get: () => 180 });
    Object.defineProperty(deck.audio, "currentTime", { configurable: true, get: () => seconds,
      set: () => {} });
    deck.audio.dispatchEvent(new Event("timeupdate"));
  }

  it("uses the planned effect's length to fit the fade on the boundary", async () => {
    await startEngine();
    push({ ...phrase, fx: "scratch" });
    tick(176.05);
    expect(engine.crossfading).toBe(true);
    expect(played).toEqual(["scratch"]);
  });

  it("counts random as its longest effect without a plan", async () => {
    await startEngine();
    push({ ...phrase, fx: null });
    tick(176.05);
    expect(engine.crossfading).toBe(false);
  });
});
