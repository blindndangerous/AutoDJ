import { describe, it, expect, vi, afterEach } from "vitest";

function installAudioDom() {
  document.body.innerHTML = `
    <input id="eq-low" type="range" value="100">
    <input id="eq-mid" type="range" value="100">
    <input id="eq-high" type="range" value="100">
    <span id="eq-low-value"></span>
    <span id="eq-mid-value"></span>
    <span id="eq-high-value"></span>
    <div id="eq-announce"></div>
    <button id="btn-eq-reset"></button>
    <input id="vol" type="range" value="100">
    <button id="btn-pause"></button>
    <img id="cover-art">
    <div id="now-playing-announce"></div>
    <audio id="browser-player"></audio>
    <audio id="browser-player-b"></audio>
  `;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("audio-engine network usage", () => {
  it("does not fetch and decode a whole track when assigning a deck src", async () => {
    vi.resetModules();
    installAudioDom();
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const mod = await import("../../src/autodj/static/modules/audio-engine.js");

    mod.setSrcOnDeck(mod.decks[0], "/music/current.flac");

    expect(mod.decks[0].path).toBe("/music/current.flac");
    expect(mod.decks[0].audio.src).toContain(
      "/api/audio?path=%2Fmusic%2Fcurrent.flac",
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
