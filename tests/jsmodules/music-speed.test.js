import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installMusicSpeedSlider, syncMusicSpeedSlider } from
  "../../src/autodj/static/modules/music-speed.js";

describe("music speed control", () => {
  beforeEach(() => { document.body.innerHTML = '<input id="n" type="number"><input id="s" type="range"><output id="o"></output>'; });
  afterEach(() => { document.body.replaceChildren(); });

  it("hydrates exact hundredths and formats the displayed percentage", () => {
    const number = document.querySelector("#n");
    const slider = document.querySelector("#s");
    const output = document.querySelector("#o");
    number.value = "1.23";
    syncMusicSpeedSlider(number, slider, output);
    expect(slider.value).toBe("1.23");
    expect(output.value).toBe("+1.23%");
  });

  it("moves by one percentage point with arrow keys and emits the save change", () => {
    const number = document.querySelector("#n");
    const slider = document.querySelector("#s");
    const output = document.querySelector("#o");
    number.value = "1.23";
    installMusicSpeedSlider({ number, slider, output });
    syncMusicSpeedSlider(number, slider, output);
    const changed = vi.fn();
    number.addEventListener("change", changed);
    slider.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
    expect(number.value).toBe("2.23");
    expect(slider.value).toBe("2.23");
    expect(output.value).toBe("+2.23%");
    expect(changed).toHaveBeenCalledOnce();
  });
});

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
    expect(music.audio.preservesPitch).toBe(false);
    expect(liner.audio.playbackRate).toBe(1);
    expect(liner.audio.preservesPitch).toBe(true);
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
