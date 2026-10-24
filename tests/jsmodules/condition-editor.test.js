import { describe, expect, it } from "vitest";

import { describeConditions } from "../../src/autodj/static/modules/clockwheel-step.js";
import { CONDITION_FIELDS, createConditionRow, readConditionRows } from "../../src/autodj/static/modules/condition-editor.js";

function row(value = {}, options = {}) {
  return createConditionRow({
    document,
    nextId: (() => { let id = 0; return () => `condition-${++id}`; })(),
    index: 1,
    value,
    sourceChoices: [["music", "Music"]],
    genreChoices: [["Rock", "Rock"]],
    beetsChoices: [["mood", "mood"]],
    categoryChoices: [["c1", "Rock"]],
    ...options,
  });
}

describe("shared condition editor", () => {
  it("uses one field definition while limiting categories to opted-in editors", () => {
    expect(CONDITION_FIELDS).toEqual(["folder", "year", "bpm", "duration", "artist", "title", "album", "genre", "beets", "category"]);
    expect([...row().field.options].map((option) => option.value)).not.toContain("category");
    expect([...row({}, { allowCategory: true }).field.options].map((option) => option.value)).toContain("category");
    const condition = row({ field: "duration" }, { allowCategory: true });
    expect(condition.help.textContent).toBe("Enter seconds or minutes:seconds.");
    condition.field.value = "category";
    condition.field.dispatchEvent(new Event("change"));
    expect(condition.help.textContent).toBe("Choose a saved music category.");
  });

  it("returns the actual invalid named control for caller error handling", () => {
    const controls = row({ field: "year" });
    expect(() => readConditionRows([controls.row])).toThrow("Enter a value for year.");
    try { readConditionRows([controls.row]); } catch (error) { expect(error.control).toBe(controls.input); }
  });

  it("reads the folder, genre, Beets, and category controls by name", () => {
    const folder = row({ field: "folder", source: "music", folder: "Rock", recursive: true });
    expect(readConditionRows([folder.row])).toEqual([{ field: "folder", source: "music", folder: "Rock", recursive: true }]);
    const genre = row({ field: "genre", value: "Rock" });
    expect(readConditionRows([genre.row])).toEqual([{ field: "genre", operator: "is", value: "Rock" }]);
    const beets = row({ field: "beets", name: "mood", value: "upbeat" });
    expect(readConditionRows([beets.row])).toEqual([{ field: "beets", name: "mood", operator: "is", value: "upbeat" }]);
    const category = row({ field: "category", category: "c1" }, { allowCategory: true });
    expect(readConditionRows([category.row])).toEqual([{ field: "category", category: "c1" }]);
  });

  // ADR-0009-07
  it("offers within the last N days for a Beets date and reads a whole number of days", () => {
    const condition = row({ field: "beets", name: "added", operator: "within_days", value: 30 }, { beetsChoices: [["added", "added"], ["mtime", "mtime"]] });
    const option = [...condition.operator.options].find((item) => item.value === "within_days");
    expect(option.textContent).toBe("within the last N days (date)");
    expect(condition.operator.value).toBe("within_days");
    expect(condition.input.previousElementSibling.textContent).toBe("Days (1 to 3650)");
    expect(condition.input.getAttribute("inputmode")).toBe("numeric");
    expect(condition.upper.hidden).toBe(true);
    expect(condition.help.textContent).toContain("in the last N days");
    expect(readConditionRows([condition.row])).toEqual([{ field: "beets", name: "added", operator: "within_days", value: 30 }]);
    for (const bad of ["0", "3651", "1.5", "-2", "ten"]) {
      condition.input.value = bad;
      expect(() => readConditionRows([condition.row])).toThrow("Enter a whole number of days from 1 to 3650.");
      expect(condition.input.getAttribute("aria-invalid")).toBe("true");
    }
    condition.operator.value = "gt";
    condition.operator.dispatchEvent(new Event("change"));
    expect(condition.input.previousElementSibling.textContent).toBe("Value");
    expect(condition.input.hasAttribute("inputmode")).toBe(false);
    expect(condition.help.textContent).toBe("Choose a Beets field, then compare its value.");
    // Only Beets fields hold dates.
    const year = row({ field: "year" });
    expect([...year.operator.options].map((item) => item.value)).not.toContain("within_days");
  });

  it("describes a relative date condition in words", () => {
    expect(describeConditions([{ field: "beets", name: "added", operator: "within_days", value: 30 }])).toBe("Beets added within the last 30 days");
    expect(describeConditions([{ field: "beets", name: "mtime", operator: "within_days", value: 1 }])).toBe("Beets mtime within the last 1 day");
  });
});
