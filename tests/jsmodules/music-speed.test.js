import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("audio engine music speed", () => {
  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = `
      <input id="eq-low" value="100"><input id="eq-mid" value="100"><input id="eq-high" value="100">
      <span id="eq-low-value"></span><span id="eq-mid-value"></span><span id="eq-high-value"></span>
      <div id="eq-announce"></div><button id="btn-eq-reset"></button><input id="vol" value="100">
      <img id="cover-art"><div id="sr-status"></div>
      <audio id="browser-player"></audio><audio id="browser-player-b"></audio>`;
    for (const audio of document.querySelectorAll("audio")) {
      audio.play = vi.fn().mockResolvedValue(undefined);
      audio.pause = vi.fn();
      // Match the HTML media load algorithm: .load() restores playbackRate
      // from defaultPlaybackRate.
      audio.load = vi.fn(() => { audio.playbackRate = audio.defaultPlaybackRate; });
    }
    const context = {
      createAnalyser: () => ({ fftSize: 0 }),
      createGain: () => ({ connect: vi.fn(), disconnect: vi.fn(), gain: {
        value: 1, cancelScheduledValues: vi.fn(), setValueAtTime: vi.fn(),
        linearRampToValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn(),
      } }),
      createMediaElementSource: () => ({ connect: vi.fn(), disconnect: vi.fn() }),
      currentTime: 0, destination: {}, resume: vi.fn().mockResolvedValue(undefined), state: "running",
    };
    vi.stubGlobal("AudioContext", vi.fn(function AudioContextMock() { return context; }));
    window.AudioContext = globalThis.AudioContext;
  });
  afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren(); });

  it("applies speed to music decks and leaves liner decks at native rate", async () => {
    const engine = await import("../../src/autodj/static/modules/audio-engine.js");
    engine.applyBrowserPlaybackState({ browser_playback: true, settings: { playback: { music_speed_percent: 1.23 } } });
    const music = engine.decks[0];
    const liner = engine.decks[1];
    engine.setSrcOnDeck(music, "song.mp3", "music");
    engine.setSrcOnDeck(liner, "liner.mp3", "liner");
    expect(music.audio.playbackRate).toBeCloseTo(1.0123);
    expect(music.audio.defaultPlaybackRate).toBeCloseTo(1.0123);
    expect(music.audio.preservesPitch).toBe(false);
    expect(liner.audio.playbackRate).toBe(1);
    expect(liner.audio.defaultPlaybackRate).toBe(1);
    expect(liner.audio.preservesPitch).toBe(true);
  });

  it("keeps effective music and native voice rates after media reload", async () => {
    const engine = await import("../../src/autodj/static/modules/audio-engine.js");
    engine.applyBrowserPlaybackState({ browser_playback: true,
      current_track: { path: "song.mp3", kind: "music" },
      next_track: { path: "next.mp3", kind: "music" },
      settings: { playback: { music_speed_percent: 1.23 } } });
    engine.ensureAudioGraph();
    engine.setSrcOnDeck(engine.decks[0], "song.mp3", "music");

    // Exercise the same write that applies the incoming BPM multiplier.
    void engine.startCrossfade("next.mp3", 1, false, null, 1.5, true, "music");
    const incoming = engine.decks[1].audio;
    expect(incoming.defaultPlaybackRate).toBeCloseTo(1.0123 * 1.5);
    incoming.load();
    expect(incoming.playbackRate).toBeCloseTo(1.0123 * 1.5);

    const voiceDeck = engine.decks[0];
    engine.setSrcOnDeck(voiceDeck, "liner.mp3", "liner");
    voiceDeck.audio.load();
    expect(voiceDeck.audio.playbackRate).toBe(1);
    expect(voiceDeck.audio.defaultPlaybackRate).toBe(1);
    engine.stopAllDecks();
  });

  it("uses a plain fade for a sped-up music and liner overlap", async () => {
    const engine = await import("../../src/autodj/static/modules/audio-engine.js");
    engine.applyBrowserPlaybackState({ browser_playback: true,
      current_track: { path: "song.mp3", kind: "music" },
      next_track: { path: "liner.mp3", kind: "liner" },
      settings: { transition: "tape_stop", playback: { music_speed_percent: 1.23 } } });
    engine.ensureAudioGraph();
    engine.setSrcOnDeck(engine.decks[0], "song.mp3", "music");
    const debug = vi.spyOn(console, "debug").mockImplementation(() => {});
    void engine.startCrossfade("liner.mp3", 1, true);
    expect(debug.mock.calls[0][1]).toBe("none");
    expect(engine.decks[1].audio.playbackRate).toBe(1);
    debug.mockRestore();
    engine.stopAllDecks();
  });
});
