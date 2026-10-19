import { describe, expect, it } from "vitest";

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
});
