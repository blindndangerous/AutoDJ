import { describe, expect, it } from "vitest";

import { checkStationName, isStationNameError, STATION_NAME_MAX } from "../../src/autodj/static/modules/station-name-field.js";

describe("station name while active", () => {
  // ADR-0009-08
  it("allows a blank name, trims it and refuses what the server refuses", () => {
    expect(checkStationName("")).toEqual({ name: "" });
    expect(checkStationName(undefined)).toEqual({ name: "" });
    expect(checkStationName("  Example FM Night Shift ")).toEqual({ name: "Example FM Night Shift" });
    expect(checkStationName("Café Radio")).toEqual({ name: "Café Radio" });
    expect(STATION_NAME_MAX).toBe(100);
    // Characters, as Python counts them, not UTF-16 units.
    expect(checkStationName("🎵".repeat(100)).name).toBe("🎵".repeat(100));
    expect(checkStationName("x".repeat(101)).error).toBe("Station name while active can be at most 100 characters.");
    for (const bad of ["Night\tShift", "Night\u0000Shift", "Night Shift", "Night Shift"]) {
      expect(checkStationName(bad).error).toBe("Station name while active cannot contain tabs, line breaks or other control characters.");
    }
    expect(isStationNameError("Station name while active can be at most 100 characters")).toBe(true);
    expect(isStationNameError("Step 2: its category is not saved")).toBe(false);
  });
});
