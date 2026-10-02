import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Phrase-aligned crossfade starts in the browser engine: the boundary
// pick (_phraseBoundary) and the timeupdate trigger that times the start.

function jsonResponse(body) {
  return new globalThis.Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function gainParam(value = 0) {
  return {
    cancelScheduledValues: vi.fn(),
    exponentialRampToValueAtTime: vi.fn(),
    linearRampToValueAtTime: vi.fn(),
    setValueAtTime: vi.fn(),
    value,
  };
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

function installAudioContext() {
  const context = {
    createAnalyser: vi.fn(() => ({ fftSize: 0 })),
    createGain: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn(), gain: gainParam() })),
    createMediaElementSource: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() })),
    currentTime: 2,
    decodeAudioData: vi.fn().mockResolvedValue({}),
    destination: {},
    resume: vi.fn().mockResolvedValue(undefined),
    state: "running",
  };
  vi.stubGlobal("AudioContext", vi.fn(function AudioContextMock() {
    return context;
  }));
  window.AudioContext = globalThis.AudioContext;
  return context;
}

// 120 BPM, 180 s track: the server sends the last 32 bars (116 s to
// 178 s, one downbeat every 2 s), the first of them bar 58 of the grid.
// With 8-bar phrases the boundaries in the window are bars 64, 72, 80
// and 88: 128 s, 144 s, 160 s and 176 s.
const GRID = Array.from({ length: 32 }, (_, i) => 116 + 2 * i);
const FIRST_BAR = 58;

// The active deck's clock: `position` at the moment of the last seek,
// moving on with the (fake) wall clock while playing.
function installClock(deck) {
  const clock = { at: 0, base: Date.now(), paused: false };
  Object.defineProperty(deck.audio, "duration", { configurable: true, get: () => 180 });
  Object.defineProperty(deck.audio, "paused", { configurable: true, get: () => clock.paused });
  Object.defineProperty(deck.audio, "currentTime", {
    configurable: true,
    get: () => (clock.paused ? clock.at : clock.at + (Date.now() - clock.base) / 1000),
    set: () => {},
  });
  clock.seek = (seconds) => {
    clock.at = seconds;
    clock.base = Date.now();
  };
  return clock;
}

let engine;
let clock;
let advanceAt;   // the outgoing deck's position at each /api/advance
let audioContext;

async function startEngine() {
  installDom();
  audioContext = installAudioContext();
  advanceAt = [];
  vi.stubGlobal("fetch", vi.fn(async (url) => {
    if (url === "/api/advance") advanceAt.push(engine.decks[engine.activeIdx].audio.currentTime);
    return jsonResponse({ current_track: { path: "current.mp3" } });
  }));
  engine = await import("../../src/autodj/static/modules/audio-engine.js");
  await engine.unlockAndPlay();
  vi.useFakeTimers();
  clock = installClock(engine.decks[engine.activeIdx]);
}

function push({ phraseAlign = true, firstBar = FIRST_BAR, playback = {}, transition = "none",
  outro = {} } = {}) {
  engine.applyBrowserPlaybackState({
    browser_playback: true,
    current_track: {
      path: "current.mp3",
      bpm: 120,
      downbeats_outro: GRID,
      downbeats_outro_first_bar: firstBar,
      ...outro,
    },
    next_track: { path: "next.mp3", bpm: 120 },
    is_muted: false,
    is_paused: false,
    settings: {
      transition,
      djmix: { phrase_align: phraseAlign, phrase_bars: 8 },
      playback: { fade_in_seconds: 0, beat_sync_fx: false, ...playback },
    },
  });
}

function tick(seconds) {
  clock.seek(seconds);
  engine.decks[engine.activeIdx].audio.dispatchEvent(new Event("timeupdate"));
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("phrase boundary pick", () => {
  let pick;
  beforeEach(async () => {
    installDom();
    installAudioContext();
    ({ _phraseBoundary: pick } = await import("../../src/autodj/static/modules/audio-engine.js"));
  });

  it("picks the nearest boundary, counting phrases from the grid's first bar", () => {
    expect(pick(GRID, FIRST_BAR, 8, 150, 4, 180)).toBe(144);
    expect(pick(GRID, FIRST_BAR, 8, 166.5, 4, 180)).toBe(160);
    // Same window, counted from bar 0: boundaries fall on 116, 132, ...
    expect(pick(GRID, 0, 8, 150, 4, 180)).toBe(148);
    // Halfway between two boundaries: the earlier, as the server's min().
    expect(pick(GRID, FIRST_BAR, 8, 152, 4, 180)).toBe(144);
  });

  it("leaves the start alone with no boundary within half a phrase", () => {
    // The window's first boundary is 128 s, 28 s (more than 8 s) away.
    expect(pick(GRID, FIRST_BAR, 8, 100, 4, 180)).toBeNull();
  });

  it("never picks a boundary whose fade would run past the end", () => {
    expect(pick(GRID, FIRST_BAR, 8, 172, 4, 180)).toBe(176);
    expect(pick(GRID, FIRST_BAR, 8, 172, 6, 180)).toBeNull();
  });

  it("falls back without a grid, a bar number, or enough bars", () => {
    expect(pick([], FIRST_BAR, 8, 150, 4, 180)).toBeNull();
    expect(pick(undefined, FIRST_BAR, 8, 150, 4, 180)).toBeNull();
    expect(pick(GRID, null, 8, 150, 4, 180)).toBeNull();
    // Four bars in all, fewer than one 8-bar phrase.
    expect(pick([0, 2, 4, 6], 0, 8, 4, 1, 180)).toBeNull();
  });
});

describe("phrase-aligned crossfade trigger", () => {
  // outro_fade with the outro at 142 s and 8 s long: today the fade
  // starts at 142 s.  The nearest phrase boundary is 144 s, and the
  // plain crossfade's 4 s effect ends well before the track does.
  const outroFade = { playback: { transition_mode: "outro_fade" },
    outro: { outro_start_s: 142, outro_len: 8 } };

  it("starts the fade on the boundary, timed to it rather than to a timeupdate", async () => {
    await startEngine();
    push(outroFade);
    tick(142.1);
    expect(engine.crossfading).toBe(false);
    tick(143.3);
    expect(engine.crossfading).toBe(false);
    await vi.advanceTimersByTimeAsync(690);
    expect(engine.crossfading).toBe(false);
    await vi.advanceTimersByTimeAsync(15);
    expect(engine.crossfading).toBe(true);
    expect(advanceAt).toHaveLength(1);
    expect(advanceAt[0]).toBeCloseTo(144, 2);
  });

  it.each([
    ["without a grid bar number", { firstBar: null }],
    ["with phrase align off", { phraseAlign: false }],
  ])("starts where it does today %s", async (_label, over) => {
    await startEngine();
    push({ ...outroFade, ...over });
    tick(142.1);
    expect(engine.crossfading).toBe(true);
  });

  it("keeps today's start when the boundary's fade would run past the end", async () => {
    // Fixed 10 s fade: today it starts at 170 s.  The nearest boundary,
    // 176 s, leaves only 4 s, so the start stays at 170 s.
    await startEngine();
    push({ playback: { transition_mode: "fixed", crossfade_seconds: 10 } });
    tick(169.9);
    expect(engine.crossfading).toBe(false);
    tick(170.1);
    expect(engine.crossfading).toBe(true);
  });

  it("anchors a late phrase start's beat-synced effect on the boundary", async () => {
    await startEngine();
    push({ ...outroFade, transition: "sidechain_pump",
      playback: { transition_mode: "outro_fade", beat_sync_fx: true } });
    tick(143.3);         // arms the timer for the 144 s boundary
    clock.at += 0.03;    // the deck runs 30 ms ahead: the timer fires 30 ms late
    await vi.advanceTimersByTimeAsync(700);
    expect(engine.crossfading).toBe(true);
    // The context reads 2 s when the fade starts and the deck is 30 ms
    // past the boundary, so the boundary is context time 1.97.  The pump's
    // first duck lands there, not a bar later on 146 s (context 3.97).
    const ducks = audioContext.createGain.mock.results
      .flatMap(({ value }) => value.gain.setValueAtTime.mock.calls)
      .filter(([level]) => Math.abs(level - 0.3) < 1e-9);
    expect(ducks.length).toBeGreaterThan(0);
    // (Within the fake timer's millisecond rounding.)
    expect(ducks[0][1]).toBeCloseTo(1.97, 2);
  });

  it("does not start on the boundary after a pause since the timer was armed", async () => {
    await startEngine();
    push(outroFade);
    tick(143.5);
    clock.paused = true;
    await vi.advanceTimersByTimeAsync(1000);
    expect(engine.crossfading).toBe(false);
  });
});
