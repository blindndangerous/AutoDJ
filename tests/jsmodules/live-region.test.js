// Live-region clear-after-dwell behaviour.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { announceStatus, clearLiveRegionLater } from
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

describe("announceStatus", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<p id="region" role="status" aria-live="polite" aria-atomic="true"></p>'
      + '<div id="status-toast" aria-hidden="true" hidden></div>';
  });

  it("mirrors the announced text into the visible status line", () => {
    const region = document.querySelector("#region");
    const toast = document.querySelector("#status-toast");

    announceStatus(region, "Could not update queue: request refused.");

    expect(region.textContent).toBe("Could not update queue: request refused.");
    expect(toast.textContent).toBe("Could not update queue: request refused.");
    expect(toast.hidden).toBe(false);
  });

  it("keeps the visible mirror out of the accessibility tree", () => {
    const toast = document.querySelector("#status-toast");
    announceStatus(document.querySelector("#region"), "Added a track to queue.");
    // One source of truth, one announcement: the region speaks, the
    // sighted mirror is hidden from assistive tech.
    expect(toast.getAttribute("aria-hidden")).toBe("true");
    expect(toast.hasAttribute("aria-live")).toBe(false);
    expect(toast.hasAttribute("role")).toBe(false);
  });

  it("does not rewrite a region that already carries the message", () => {
    const region = document.querySelector("#region");
    region.textContent = "Saved.";
    const records = [];
    const observer = new window.MutationObserver((r) => records.push(...r));
    observer.observe(region, { childList: true, characterData: true, subtree: true });

    expect(announceStatus(region, "Saved.")).toBe(false);
    expect(records).toHaveLength(0);
    observer.disconnect();
  });

  it("clears both channels after the dwell", () => {
    vi.useFakeTimers();
    const region = document.querySelector("#region");
    const toast = document.querySelector("#status-toast");
    announceStatus(region, "Audio output: Speakers", { dwellMs: 1000 });
    vi.advanceTimersByTime(1001);
    expect(region.textContent).toBe("");
    vi.advanceTimersByTime(60000);
    expect(toast.hidden).toBe(true);
    expect(toast.textContent).toBe("");
    vi.useRealTimers();
  });

  it("no-ops without a region", () => {
    expect(() => announceStatus(null, "nothing")).not.toThrow();
  });
});
