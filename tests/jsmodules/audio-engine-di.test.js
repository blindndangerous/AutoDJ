// Regression: audio-engine must not reach back into app.js scope for
// applyState.  Earlier audio-engine.js called the bare identifier
// `applyState` from /api/repick-next + unlockAndPlay, which threw
// ReferenceError under raw ES modules.  app.js now registers the
// applier via setApplyState().

import { describe, it, expect } from "vitest";

describe("audio-engine setApplyState dependency injection", () => {
  it("exports setApplyState and the engine accepts the registered fn", async () => {
    // Import after the DOM has at least the elements audio-engine queries
    // at module load.  happy-dom gives us a working document; we just
    // need the IDs to exist so the top-level getElementById calls don't
    // crash.
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
    const mod = await import(
      "../../src/autodj/static/modules/audio-engine.js"
    );
    expect(typeof mod.setApplyState).toBe("function");

    mod.setApplyState(() => {});
    // Indirect verification: there is no public function that calls
    // _applyState synchronously, but the setter must at minimum accept
    // a function without throwing and remember it.  A direct call via
    // setter+symbol is not exposed; the contract under test is that
    // app.js can wire the dependency without the engine throwing.
    expect(() => mod.setApplyState(() => {})).not.toThrow();
  });
});
