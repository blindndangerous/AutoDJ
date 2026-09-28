import { afterEach, describe, expect, it, vi } from "vitest";

import * as badges from "../../src/autodj/static/modules/badges.js";

afterEach(() => {
  vi.useRealTimers();
});

describe("persistent playback metadata", () => {
  it("formats every field in a stable order", () => {
    expect(badges.formatPersistentMetadata?.({
      album: "Night Drive",
      bpm: 127.6,
      key_label: "8A",
      energy: 0.734,
    })).toBe("Album Night Drive · BPM 128 · Key 8A · Energy 0.73");
  });

  it("uses explicit unknowns for missing and non-finite values", () => {
    expect(badges.formatPersistentMetadata?.({
      album: "",
      bpm: Number.POSITIVE_INFINITY,
      key_label: "--",
      energy: Number.NaN,
    })).toBe("Album unknown · BPM unknown · Key unknown · Energy unknown");
  });

  it("returns empty text when there is no track", () => {
    expect(badges.formatPersistentMetadata?.(null)).toBe("");
    expect(badges.formatPersistentMetadata?.(undefined)).toBe("");
  });

  it("treats zero energy as unknown", () => {
    expect(badges.formatPersistentMetadata?.({ energy: 0 })).toContain(
      "Energy unknown",
    );
  });

  it("treats a whitespace-padded key sentinel as unknown", () => {
    expect(badges.formatPersistentMetadata?.({ key_label: " -- " })).toContain(
      "Key unknown",
    );
  });
});

describe("track-change details", () => {
  it("speaks BPM, key and beatmatch once, as the tail of the title", () => {
    expect(badges.trackChangeDetails({
      current_track: {
        path: "a.flac", bpm: 127.6, key_label: "F#m", key_spoken: "F sharp minor",
      },
      beatmatch_ratio: 1.02,
    })).toBe(", 128 BPM, key F sharp minor, beatmatched 1.02 times");
  });

  it("omits unknown key, missing BPM and a unity beatmatch", () => {
    expect(badges.trackChangeDetails({
      current_track: { path: "a.flac", key_label: "--", key_spoken: "" },
      beatmatch_ratio: 1,
    })).toBe("");
    expect(badges.trackChangeDetails({ current_track: null })).toBe("");
  });

  it("no longer writes a delayed second announcement", () => {
    vi.useFakeTimers();
    const badgesRow = document.createElement("div");
    const spy = vi.spyOn(globalThis, "setTimeout");
    badges.applyBadges({
      current_track: { path: "a.flac", bpm: 128, key_label: "8A" },
    }, { badgesRow }, { renderCueStrip: () => {} });
    expect(spy).not.toHaveBeenCalled();
    expect(badgesRow.textContent).toContain("Key 8A");
    spy.mockRestore();
    vi.useRealTimers();
  });
});
