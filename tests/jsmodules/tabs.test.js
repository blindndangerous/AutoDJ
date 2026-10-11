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
  it("focuses the destination heading if a Return target disconnects before hashchange", async () => {
    installIndex();
    window.location.hash = "#track-info";
    vi.resetModules();
    const { initViewRouter: freshRouter, returnToView } = await import(
      "../../src/autodj/static/modules/tabs.js");
    try {
      freshRouter();
      const source = document.createElement("button");
      document.querySelector("#panel-queue").append(source);
      expect(returnToView("queue", source)).toBe(true);
      source.remove();
      await vi.waitFor(() => expect(document.querySelector("#panel-queue").hidden).toBe(false));
      expect(document.activeElement).toBe(document.querySelector("#panel-queue summary"));
    } finally {
      window.location.hash = "";
    }
  });

  it("places Carts, then Track info, after Queue & Search in cached markup and keyboard order", async () => {
    installIndex();
    const carts = document.getElementById("tab-carts");
    const info = document.getElementById("tab-track-info");
    expect(document.getElementById("tab-queue").nextElementSibling).toBe(carts);
    expect(carts.nextElementSibling).toBe(info);
    // A cached page with the tabs in another order is put right.
    info.parentElement.append(carts, info);
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    freshRouter();
    const queue = document.getElementById("tab-queue");
    expect(queue.nextElementSibling).toBe(carts);
    expect(carts.nextElementSibling).toBe(info);
    queue.focus();
    queue.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(carts);
    expect(document.getElementById("panel-carts").hidden).toBe(false);
    carts.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(info);
    window.location.hash = "";
  });

  it("focuses the Carts heading when the tab is chosen", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    freshRouter();
    document.querySelector("#tab-carts").click();
    await vi.waitFor(() => expect(document.querySelector("#panel-carts").hidden).toBe(false));
    expect(document.activeElement.id).toBe("carts-heading");
    expect(window.location.hash).toBe("#carts");
    window.location.hash = "";
  });

  it("routes directly to Track info and focuses its heading", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    freshRouter();
    document.querySelector("#tab-track-info").click();
    await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
    expect(document.activeElement.id).toBe("track-info-heading");
    expect(window.location.hash).toBe("#track-info");
    window.location.hash = "";
  });

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

  it("moves focus once per arrow, writes no live region and ties no panel to the tab", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    try {
      freshRouter();
      const now = document.querySelector("#tab-now");
      now.focus();
      const focusEvents = [];
      document.addEventListener("focusin", (event) => focusEvents.push(event.target.id));
      const liveWrites = [];
      const observer = new window.MutationObserver((records) => {
        for (const record of records) {
          const node = record.target.nodeType === 1 ? record.target : record.target.parentElement;
          if (node?.closest('[aria-live], [role="status"], [role="alert"]')) liveWrites.push(node);
        }
      });
      observer.observe(document.body, { childList: true, characterData: true, subtree: true });

      now.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 20));
      observer.disconnect();

      expect(focusEvents).toEqual(["tab-queue"]);
      expect(liveWrites).toEqual([]);
      // Showing a panel that the focused tab points at with aria-controls
      // made Chrome and NVDA speak the tab twice.
      for (const tab of document.querySelectorAll('[role="tab"]')) {
        expect(tab.hasAttribute("aria-controls"), tab.id).toBe(false);
      }
    } finally {
      window.location.hash = "";
    }
  });
});
