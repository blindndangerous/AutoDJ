import { describe, expect, it } from "vitest";

import { approximateDurationText, relativeTimeText } from "../../src/autodj/static/modules/time-format.js";

describe("relativeTimeText", () => {
  const reference = Date.parse("2026-01-01T00:00:00Z");
  const at = (days) => new Date(reference + days * 86400000);

  it("formats short intervals, nearby dates, months, and years against the supplied clock", () => {
    expect(relativeTimeText(at(4), { reference })).toBe("in 4 days");
    expect(relativeTimeText(at(31), { reference })).toBe("next month");
    expect(relativeTimeText(at(4 * 365), { reference })).toBe("in 4 years");
    expect(relativeTimeText(reference - 20_000, { reference })).toBe("now");
    expect(relativeTimeText(reference - 5 * 60_000, { reference })).toBe("5 minutes ago");
  });

  it("keeps scheduler style units and handles invalid dates", () => {
    expect(relativeTimeText(at(4), { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("in 4 days");
    expect(relativeTimeText(at(1.25), { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("in 30 hours");
    expect(relativeTimeText("not a date", { reference })).toBe("");
  });

  it("describes approximate durations in either direction", () => {
    expect(approximateDurationText(at(4), { reference })).toBe("in about 4 days");
    expect(approximateDurationText(at(31), { reference })).toBe("in about 1 month");
    expect(approximateDurationText(at(4 * 365), { reference })).toBe("in about 4 years");
    expect(approximateDurationText(at(-4), { reference })).toBe("about 4 days ago");
    expect(approximateDurationText("not a date", { reference })).toBe("");
  });
});
