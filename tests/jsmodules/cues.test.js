// Cue summary string + cue-strip render.

import { describe, it, expect } from "vitest";
import { summariseCues, renderCueStrip, CUE_COLORS } from
  "../../src/autodj/static/modules/cues.js";

describe("summariseCues", () => {
  it("singular cue point label", () => {
    const out = summariseCues([{ type: "drop", time_s: 30 }]);
    expect(out).toContain("1 cue point");
    expect(out).toContain("drop at 30 seconds");
  });

  it("plural cue points label", () => {
    const out = summariseCues([
      { type: "drop",      time_s: 30 },
      { type: "breakdown", time_s: 90 },
    ]);
    expect(out).toContain("2 cue points");
    expect(out).toContain("breakdown at 1 minute 30");
  });

  it("filters phrase markers from interesting list", () => {
    const out = summariseCues([
      { type: "phrase", time_s: 10 },
      { type: "phrase", time_s: 20 },
      { type: "drop",   time_s: 30 },
    ]);
    expect(out).toContain("3 cue points");
    expect(out).toContain("drop at 30 seconds");
    expect(out).not.toContain("phrase at");
  });

  it("falls back to headline when only phrases exist", () => {
    const out = summariseCues([{ type: "phrase", time_s: 10 }]);
    expect(out).toBe("1 cue point");
  });

  it("caps interesting list at 3", () => {
    const cues = [
      { type: "drop",            time_s: 30 },
      { type: "breakdown",       time_s: 60 },
      { type: "first_downbeat",  time_s: 90 },
      { type: "outro_downbeat",  time_s: 120 },
    ];
    const out = summariseCues(cues);
    expect(out).toContain("first downbeat at 1 minute 30");
    // Fourth marker is dropped from the announcement.
    expect(out).not.toContain("outro downbeat at 2 minutes 0");
  });
});

describe("renderCueStrip", () => {
  it("clears strip when no cues", () => {
    const el = { innerHTML: "<span></span>" };
    renderCueStrip(el, { path: "a.flac", length: 100, cues: [] });
    expect(el.innerHTML).toBe("");
  });

  it("renders cue marks at correct percentages", () => {
    const el = { innerHTML: "" };
    renderCueStrip(el, {
      path: "b.flac",
      length: 100,
      cues: [{ type: "drop", time_s: 25 }],
    });
    expect(el.innerHTML).toContain("left:25.00%");
    expect(el.innerHTML).toContain(CUE_COLORS.drop);
  });

  it("dedupes against previous render of same key", () => {
    const el = { innerHTML: "" };
    const track = {
      path: "c.flac", length: 60,
      cues: [{ type: "drop", time_s: 30 }],
    };
    renderCueStrip(el, track);
    el.innerHTML = "tampered";
    renderCueStrip(el, track);
    // Same path + same cue count -> no rebuild.
    expect(el.innerHTML).toBe("tampered");
  });
});
