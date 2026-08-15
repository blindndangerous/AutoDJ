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

describe("badge announcements", () => {
  it("does not repeat BPM after the comprehensive track-change announcement", () => {
    vi.useFakeTimers();
    const badgesAnnounce = document.createElement("div");
    badges.applyBadges({
      current_track: {
        path: "no-duplicate-bpm.flac",
        bpm: 128,
        key_label: "8A",
      },
      beatmatch_ratio: 1,
    }, {
      badgesAnnounce,
      badgesRow: document.createElement("div"),
    }, {
      lastTrackKey: "no-duplicate-bpm.flac",
      renderCueStrip: () => {},
    });

    vi.advanceTimersByTime(800);

    expect(badgesAnnounce.textContent).toBe("Key 8A");
    expect(badgesAnnounce.textContent).not.toContain("BPM");
  });
});
