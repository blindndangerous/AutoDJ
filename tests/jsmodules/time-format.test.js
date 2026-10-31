import { describe, expect, it } from "vitest";

import { approximateDurationText, relativeTimeText } from "../../src/autodj/static/modules/time-format.js";

describe("relativeTimeText", () => {
  const reference = Date.parse("2026-01-01T00:00:00Z");
  const at = (days) => new Date(reference + days * 86400000);

  it("formats nearby dates, months, and years against the supplied clock", () => {
    expect(relativeTimeText(at(4), { reference })).toBe("in 4 days");
    expect(relativeTimeText(at(31), { reference })).toBe("next month");
    expect(relativeTimeText(at(4 * 365), { reference })).toBe("in 4 years");
    expect(relativeTimeText(reference - 20_000, { reference })).toBe("this minute");
    expect(relativeTimeText(reference - 5 * 60_000, { reference })).toBe("5 minutes ago");
  });

  it("keeps scheduler style units and handles invalid dates", () => {
    expect(relativeTimeText(at(4), { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("in 4 days");
    expect(relativeTimeText(at(1.25), { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("in 30 hours");
    expect(relativeTimeText("not a date", { reference })).toBe("");
    expect(relativeTimeText(null, { reference })).toBe("");
  });

  it("describes duration magnitude with locale-specific units", () => {
    expect(approximateDurationText(at(4), { reference })).toBe("about 4 days");
    expect(approximateDurationText(at(31), { reference })).toBe("about 1 month");
    expect(approximateDurationText(at(4 * 365), { reference })).toBe("about 4 years");
    expect(approximateDurationText(at(-4), { reference })).toBe("about 4 days");
    expect(approximateDurationText(at(4), { reference, locale: "fr-FR" })).toMatch(/4.*jours/);
    expect(approximateDurationText("not a date", { reference })).toBe("");
    expect(approximateDurationText(null, { reference })).toBe("");
  });

  it("preserves scheduler rounded-minute unit boundaries", () => {
    expect(relativeTimeText(reference - 30_000, { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("this minute");
    expect(relativeTimeText(reference, { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("this minute");
    expect(relativeTimeText(reference + 59 * 60_000 + 40_000, { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("in 1 hour");
    expect(relativeTimeText(reference + 47 * 3600_000 + 59 * 60_000 + 40_000, { reference, maxUnit: "day", dayAfterHours: 48 })).toBe("in 2 days");
  });

  it("changes units at approximate month and year boundaries", () => {
    expect(approximateDurationText(at(30), { reference })).toBe("about 30 days");
    expect(approximateDurationText(at(31), { reference })).toBe("about 1 month");
    expect(approximateDurationText(at(364), { reference })).toBe("about 12 months");
    expect(approximateDurationText(at(366), { reference })).toBe("about 1 year");
  });
});
