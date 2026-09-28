import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { initViewRouter } from "../../src/autodj/static/modules/tabs.js";

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
}

describe("playback controls grouping", () => {
  it("uses a labelled group rather than a toolbar without arrow keys", () => {
    installIndex();
    expect(document.querySelector('[role="toolbar"]')).toBeNull();
    const group = document.querySelector("#btn-pause").closest('[role="group"]');
    expect(group?.getAttribute("aria-label")).toBe("Playback controls");
  });
});

describe("card headings", () => {
  it("gives every card a level-two heading inside its disclosure summary", () => {
    installIndex();
    const summaries = [...document.querySelectorAll("details.card > summary")];
    expect(summaries.length).toBeGreaterThan(0);
    for (const summary of summaries) {
      const headings = summary.querySelectorAll("h1, h2, h3, h4, h5, h6");
      expect(headings, summary.textContent.trim()).toHaveLength(1);
      expect(headings[0].tagName).toBe("H2");
      expect(headings[0].parentElement).toBe(summary);
    }
    // Library h3s and the voice-liner "Files" h3 now sit under an h2.
    for (const h3 of document.querySelectorAll("main h3")) {
      const card = h3.closest("details.card");
      expect(card?.querySelector(":scope > summary > h2"), h3.textContent).not.toBeNull();
    }
  });

  it("focuses the card's summary, not the heading inside it, on a tab switch", () => {
    installIndex();
    window.location.hash = "#now";
    try {
      initViewRouter();
      document.querySelector("#tab-now").click();
      const active = document.activeElement;
      expect(active.tagName).toBe("SUMMARY");
      expect(active.textContent.trim()).toBe("Now Playing");
    } finally {
      window.location.hash = "";
    }
  });
});

describe("tab arrow keys", () => {
  it("selects the next tab before focusing it, so it is spoken once", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    try {
      freshRouter();
      const now = document.querySelector("#tab-now");
      const next = document.querySelector('#view-nav [role="tab"][data-view="queue"]');
      let selectedOnFocus = null;
      next.addEventListener("focus", () => {
        selectedOnFocus = next.getAttribute("aria-selected");
      });
      now.focus();
      now.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      expect(document.activeElement).toBe(next);
      expect(selectedOnFocus).toBe("true");
    } finally {
      window.location.hash = "";
    }
  });
});
