import { afterEach, describe, expect, it } from "vitest";
import {
  historyDateRange,
  refreshHistoryRange,
  setHistoryRangePreset,
} from "../../src/autodj/static/modules/history-range.js";

describe("History date ranges", () => {
  afterEach(() => setHistoryRangePreset("custom"));

  it.each([
    ["last-day", 24],
    ["last-7-days", 7 * 24],
    ["last-30-days", 30 * 24],
    ["last-90-days", 90 * 24],
  ])("uses an exact rolling duration for %s", (preset, hours) => {
    const now = new Date("2026-11-15T12:34:56.789Z");
    const range = setHistoryRangePreset(preset, now);
    expect(range).toEqual({
      from: new Date(now.getTime() - hours * 60 * 60 * 1000).toISOString(),
      to: now.toISOString(),
    });
    expect(historyDateRange("custom-from", "custom-to")).toEqual(range);
  });

  it("uses the chosen custom dates without converting the inclusive end date", () => {
    setHistoryRangePreset("custom");
    expect(historyDateRange("2026-11-01", "2026-11-15")).toEqual({
      from: "2026-11-01",
      to: "2026-11-15",
    });
  });

  it("refreshes a preset snapshot for a new first page and keeps it stable until then", () => {
    const firstNow = new Date("2026-11-15T12:00:00.000Z");
    const nextNow = new Date("2026-11-15T12:05:00.000Z");
    setHistoryRangePreset("last-day", firstNow);
    const first = historyDateRange("custom-from", "custom-to");

    expect(historyDateRange("custom-from", "custom-to")).toEqual(first);
    const refreshed = refreshHistoryRange(nextNow);
    expect(refreshed).toEqual({
      from: "2026-11-14T12:05:00.000Z",
      to: "2026-11-15T12:05:00.000Z",
    });
    // The same snapshot is used by subsequent pages and export until the
    // next first-page refresh.
    expect(historyDateRange("custom-from", "custom-to")).toBe(refreshed);
    expect(historyDateRange("custom-from", "custom-to")).not.toEqual(first);
  });

  it("does not replace custom dates during a refresh", () => {
    setHistoryRangePreset("custom");
    expect(refreshHistoryRange(new Date("2026-11-15T12:00:00.000Z"))).toBeNull();
    expect(historyDateRange("2026-11-01", "2026-11-15")).toEqual({
      from: "2026-11-01",
      to: "2026-11-15",
    });
  });
});
