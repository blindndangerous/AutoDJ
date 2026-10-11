import { describe, expect, it, vi } from "vitest";

import { introMultiplier, scheduleIntroGain } from "../../src/autodj/static/modules/intro-gain.js";

describe("quiet intro gain", () => {
  it("holds the lift briefly and reaches unity at eight seconds", () => {
    expect(introMultiplier(0, 4)).toBeCloseTo(10 ** (4 / 20));
    expect(introMultiplier(1, 4)).toBeCloseTo(10 ** (4 / 20));
    expect(introMultiplier(4.5, 4)).toBeCloseTo(10 ** (2 / 20));
    expect(introMultiplier(8, 4)).toBe(1);
    expect(introMultiplier(12, 4)).toBe(1);
  });

  it("never exceeds the 6 dB the setting allows, and ignores bad values", () => {
    expect(introMultiplier(0, 20)).toBeCloseTo(10 ** (6 / 20));
    expect(introMultiplier(0, -3)).toBe(1);
    expect(introMultiplier(0, Number.NaN)).toBe(1);
    expect(introMultiplier(0, 0)).toBe(1);
  });

  it("reschedules the remaining curve after a seek", () => {
    const param = { cancelScheduledValues: vi.fn(), setValueAtTime: vi.fn(),
      exponentialRampToValueAtTime: vi.fn() };
    scheduleIntroGain(param, 20, 5, 4);
    expect(param.cancelScheduledValues).toHaveBeenCalledWith(20);
    expect(param.setValueAtTime).toHaveBeenCalledWith(introMultiplier(5, 4), 20);
    expect(param.exponentialRampToValueAtTime).toHaveBeenCalledWith(1, 23);
  });
});
