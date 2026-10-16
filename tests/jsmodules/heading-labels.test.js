import { afterEach, expect, it } from "vitest";
import { installIndexMarkup } from "./helpers.js";

afterEach(() => document.body.replaceChildren());

it("uses headings without repeating them as names of enclosing regions or forms", () => {
  installIndexMarkup();
  for (const container of document.querySelectorAll("section[aria-labelledby], form[aria-labelledby]")) {
    for (const id of container.getAttribute("aria-labelledby").split(/\s+/)) {
      const label = document.getElementById(id);
      expect(label, id).not.toBeNull();
      expect(label.matches("h1, h2, h3, h4, h5, h6") && container.contains(label), id).toBe(false);
    }
  }
  for (const id of ["rotation-results-heading", "brk-form-heading", "listener-control-title",
    "listener-bans-title", "requests-settings-heading", "vt-slot-heading", "vt-record-heading",
    "vt-booked-heading", "ads-spots-heading", "ads-breaks-heading", "source-folders-heading",
    "shows-editor-heading"]) {
    expect(document.getElementById(id)?.matches("h1, h2, h3, h4, h5, h6"), id).toBe(true);
  }
  // These names identify interactive surfaces and are not redundant region labels.
  for (const surface of document.querySelectorAll('[role="tabpanel"], dialog')) {
    const id = surface.getAttribute("aria-labelledby");
    expect(document.getElementById(id), surface.id).not.toBeNull();
  }
});
