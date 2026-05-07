// Live-region clear-after-dwell behaviour.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { clearLiveRegionLater } from
  "../../src/autodj/static/modules/live-region.js";

describe("clearLiveRegionLater", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(()  => { vi.useRealTimers(); });

  it("wipes textContent after the dwell elapses", () => {
    const el = { textContent: "Volume 90%." };
    clearLiveRegionLater(el, 1000);
    expect(el.textContent).toBe("Volume 90%.");
    vi.advanceTimersByTime(999);
    expect(el.textContent).toBe("Volume 90%.");
    vi.advanceTimersByTime(1);
    expect(el.textContent).toBe("");
  });

  it("resets the timer on a second call (most recent wins)", () => {
    const el = { textContent: "first" };
    clearLiveRegionLater(el, 1000);
    vi.advanceTimersByTime(900);
    el.textContent = "second";
    clearLiveRegionLater(el, 1000);  // reset
    vi.advanceTimersByTime(900);
    expect(el.textContent).toBe("second");
    vi.advanceTimersByTime(100);
    expect(el.textContent).toBe("");
  });

  it("no-ops on null", () => {
    expect(() => clearLiveRegionLater(null)).not.toThrow();
  });

  it("does not blank already-empty regions", () => {
    const el = { textContent: "" };
    clearLiveRegionLater(el, 100);
    vi.advanceTimersByTime(200);
    expect(el.textContent).toBe("");
  });
});
