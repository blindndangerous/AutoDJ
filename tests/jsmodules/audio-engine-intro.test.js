import { describe, expect, it, vi } from "vitest";

class Param {
  constructor() { this.value = 1; this.events = []; }
  cancelScheduledValues() {}
  setValueAtTime(value, at) { this.value = value; this.events.push([value, at]); }
  linearRampToValueAtTime(value, at) { this.value = value; this.events.push([value, at]); }
  exponentialRampToValueAtTime(value, at) { this.value = value; this.events.push([value, at]); }
  setTargetAtTime(value) { this.value = value; }
}

class Node {
  constructor() { this.outputs = new Set(); this.gain = new Param(); }
  connect(target) { this.outputs.add(target); }
  disconnect() { this.outputs.clear(); }
}

class Context {
  constructor() { this.currentTime = 10; this.destination = new Node(); }
  createGain() { return new Node(); }
  createMediaElementSource() { return new Node(); }
  createAnalyser() { return Object.assign(new Node(), { fftSize: 0 }); }
}

describe("browser quiet intro lift", () => {
  it("connects a lift queued before AudioContext starts", async () => {
    document.body.innerHTML = `
      <input id="eq-low"><input id="eq-mid"><input id="eq-high">
      <span id="eq-low-value"></span><span id="eq-mid-value"></span>
      <span id="eq-high-value"></span><div id="eq-announce"></div>
      <button id="btn-eq-reset"></button><input id="vol">
      <img id="cover-art"><audio id="browser-player"></audio>
      <audio id="browser-player-b"></audio>`;
    const previous = window.AudioContext;
    window.AudioContext = Context;
    vi.resetModules();
    try {
      const engine = await import("../../src/autodj/static/modules/audio-engine.js");
      engine.decks[0].liftDb = 4;
      engine.ensureAudioGraph();
      expect(engine.decks[0].gain.outputs.has(engine.decks[0].intro)).toBe(true);
      expect(engine.decks[0].intro.gain.events.some(([value]) => value > 1.5)).toBe(true);
    } finally {
      window.AudioContext = previous;
    }
  });

  it("inserts a dedicated lift after the deck gain only for a lifted track", async () => {
    document.body.innerHTML = `
      <input id="eq-low"><input id="eq-mid"><input id="eq-high">
      <span id="eq-low-value"></span><span id="eq-mid-value"></span>
      <span id="eq-high-value"></span><div id="eq-announce"></div>
      <button id="btn-eq-reset"></button><input id="vol">
      <img id="cover-art"><audio id="browser-player"></audio>
      <audio id="browser-player-b"></audio>`;
    const previous = window.AudioContext;
    window.AudioContext = Context;
    vi.resetModules();
    try {
      const engine = await import("../../src/autodj/static/modules/audio-engine.js");
      engine.ensureAudioGraph();
      const deck = engine.decks[0];
      expect(deck.source.outputs.has(deck.gain)).toBe(true);
      expect(deck.intro).toBeNull();
      engine.setSrcOnDeck(deck, "song.wav");
      engine.applyBrowserPlaybackState({ browser_playback: true,
        current_track: { path: "song.wav", intro_lift_db: 4 }, next_track: null,
        settings: { playback: {} }, is_paused: false });
      expect(deck.liftDb).toBe(4);
      expect(deck.gain.outputs.has(deck.intro)).toBe(true);
      expect(deck.intro.gain.events.some(([value]) => value > 1.5)).toBe(true);
    } finally {
      window.AudioContext = previous;
    }
  });
});
