import { describe, expect, it, vi } from "vitest";

import { copyByCommand } from "../../src/autodj/static/modules/clipboard.js";

describe("clipboard fallback", () => {
  it.each(["success", "failure", "exception"])("removes its listener after %s", (outcome) => {
    const doc = new globalThis.EventTarget();
    const setData = vi.fn();
    const copy = () => {
      const event = new Event("copy", { cancelable: true });
      event.clipboardData = { setData };
      doc.dispatchEvent(event);
    };
    doc.execCommand = () => {
      copy();
      if (outcome === "exception") throw new Error("Copy unavailable");
      return outcome === "success";
    };

    expect(copyByCommand("address", doc)).toBe(outcome === "success");
    expect(setData).toHaveBeenCalledWith("text/plain", "address");
    setData.mockClear();
    copy();
    expect(setData).not.toHaveBeenCalled();
  });
});
