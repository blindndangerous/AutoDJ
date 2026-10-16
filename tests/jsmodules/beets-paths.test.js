import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";
import { beetsPathsText, showBeetsPaths, showStaticText } from "../../src/autodj/static/modules/beets-paths.js";

const PROBLEM = {
  summary: "0 of 3 library songs have a beets entry",
  problem: "No song in the beets database matches a song in AutoDJ's library.",
  fix: 'Set music_dir = "/music" under [library].',
  note: null,
  mismatch: true,
};

describe("beets paths status", () => {
  it("says a mismatch with its fix, and the count only in Library tools", () => {
    expect(beetsPathsText(PROBLEM)).toBe(`Beets: ${PROBLEM.problem} ${PROBLEM.fix}`);
    const fine = { ...PROBLEM, problem: null, fix: null, mismatch: false, summary: "3 of 3 library songs have a beets entry" };
    expect(beetsPathsText(fine)).toBe("");
    expect(beetsPathsText(fine, { full: true })).toBe("Beets: 3 of 3 library songs have a beets entry.");
    const found = { ...fine, note: "Beets' relative paths are read from /music." };
    expect(beetsPathsText(found, { full: true })).toBe(`Beets: ${fine.summary}. ${found.note}`);
    expect(beetsPathsText(null, { full: true })).toBe("");
  });

  it("writes static text, marks a problem and hides when there is nothing to say", () => {
    const element = document.createElement("p");
    element.hidden = true;
    showBeetsPaths(element, PROBLEM);
    expect(element.hidden).toBe(false);
    expect(element.classList.contains("field-error")).toBe(true);
    expect(element.hasAttribute("aria-live")).toBe(false);
    expect(element.hasAttribute("role")).toBe(false);
    showStaticText(element, "");
    expect(element.hidden).toBe(true);
    expect(element.classList.contains("field-error")).toBe(false);
    expect(() => showStaticText(null, "text")).not.toThrow();
  });

  it("leaves the same words in place so a poll changes nothing", () => {
    const element = document.createElement("p");
    showBeetsPaths(element, PROBLEM);
    const node = element.firstChild;
    showBeetsPaths(element, PROBLEM);
    expect(element.firstChild).toBe(node);
  });

  it("puts the page's problem paragraphs outside every live region", () => {
    const html = readFileSync("src/autodj/static/index.html", "utf8");
    const page = document.createElement("template");
    page.innerHTML = html;
    for (const id of ["library-problem", "categories-beets-paths", "source-folders-beets"]) {
      const element = page.content.getElementById(id);
      expect(element, id).not.toBeNull();
      expect(element.closest("[aria-live], [role=status], [role=alert]"), id).toBeNull();
    }
  });
});
