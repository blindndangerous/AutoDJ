
import { describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

import { initViewRouter } from "../../src/autodj/static/modules/tabs.js";

describe("playback controls grouping", () => {
  it("uses a labelled group rather than a toolbar without arrow keys", () => {
    installIndex();
    expect(document.querySelector('[role="toolbar"]')).toBeNull();
    const group = document.querySelector("#btn-pause").closest('[role="group"]');
    expect(group?.getAttribute("aria-label")).toBe("Playback controls");
  });
});

describe("card headings", () => {
  it("gives every card a heading inside its disclosure summary, one level below its context", () => {
    // Track info's, Ads', Carts' and History's cards sit under the view's own h2, so
    // theirs are h3s (#297); a card inside a Settings group sits under the
    // group's h3, so it has an h4.  The rest are h2s.
    installIndex();
    const summaries = [...document.querySelectorAll("details.card > summary")];
    expect(summaries.length).toBeGreaterThan(0);
    const expectedLevel = (summary) => {
      if (summary.closest("#panel-settings [role='tabpanel']")) return "H4";
      if (summary.closest("#panel-track-info, #panel-ads, #panel-carts, #panel-history")) return "H3";
      return "H2";
    };
    for (const summary of summaries) {
      const headings = summary.querySelectorAll("h1, h2, h3, h4, h5, h6");
      expect(headings, summary.textContent.trim()).toHaveLength(1);
      expect(headings[0].tagName, summary.textContent.trim()).toBe(expectedLevel(summary));
      expect(headings[0].parentElement).toBe(summary);
      // Native details carries the state; the name is the heading alone.
      expect(summary.hasAttribute("aria-expanded")).toBe(false);
      expect(summary.hasAttribute("aria-label")).toBe(false);
    }
    // Library h3s and the voice-liner "Files" h3 now sit under an h2.
    for (const h3 of document.querySelectorAll("main h3")) {
      if (h3.closest("#panel-track-info")) continue;
      if (h3.parentElement.tagName === "SUMMARY") continue;
      const card = h3.closest("details.card");
      expect(card?.querySelector(":scope > summary > h2"), h3.textContent).not.toBeNull();
    }
  });

  it("leaves focus on the tab when it is clicked", () => {
    installIndex();
    window.location.hash = "#now";
    try {
      initViewRouter();
      document.querySelector("#tab-now").click();
      expect(document.activeElement.id).toBe("tab-now");
    } finally {
      window.location.hash = "";
    }
  });
});

describe("tab arrow keys", () => {
  it.each(["arrow", "hash"])("pauses the cue preview on %s navigation and keeps its position", async (navigation) => {
    installIndex();
    window.location.hash = "#track-info";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    try {
      const preview = document.getElementById("points-preview");
      const pause = vi.fn();
      Object.defineProperty(preview, "pause", { configurable: true, value: pause });
      Object.defineProperty(preview, "currentTime", { configurable: true, writable: true, value: 42 });
      freshRouter();
      document.querySelector("#tab-track-info").focus();
      if (navigation === "arrow") {
        document.querySelector("#tab-track-info").dispatchEvent(
          new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      } else {
        window.location.hash = "#history";
        window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      }
      expect(pause).toHaveBeenCalledOnce();
      expect(preview.currentTime).toBe(42);
      expect(document.querySelector("#panel-history").hidden).toBe(false);
    } finally {
      window.location.hash = "";
    }
  });

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

  it("orders the tabs from one list, fixes cached markup and opens them with Alt+number", async () => {
    installIndex();
    const order = ["now", "queue", "track-info", "history", "carts", "voice", "ads", "library", "settings"];
    const ids = () => [...document.querySelectorAll('#view-nav [role="tab"]')].map((tab) => tab.dataset.view);
    expect(ids()).toEqual(order);
    // A cached page with the tabs in another order is put right.
    const list = document.querySelector('#view-nav [role="tablist"]');
    list.append(...[...list.children].reverse());
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    try {
      freshRouter();
      expect(ids()).toEqual(order);
      const alt = (code) => document.dispatchEvent(
        new KeyboardEvent("keydown", { code, altKey: true, bubbles: true, cancelable: true }));
      document.getElementById("tab-queue").focus();
      document.getElementById("tab-queue").dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      expect(document.activeElement).toBe(document.getElementById("tab-track-info"));
      alt("Digit9");
      await vi.waitFor(() => expect(document.querySelector("#panel-settings").hidden).toBe(false));
      expect(document.activeElement).toBe(document.getElementById("tab-settings"));
      // An open dialog keeps the page behind it where it is.
      document.getElementById("hotkey-help-modal").setAttribute("open", "");
      alt("Digit4");
      expect(window.location.hash).toBe("#settings");
    } finally {
      window.location.hash = "";
    }
  });

  it("leaves focus on the Voice tracks tab when it is chosen", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    freshRouter();
    document.querySelector("#tab-voice").click();
    await vi.waitFor(() => expect(document.querySelector("#panel-voice").hidden).toBe(false));
    expect(document.activeElement.id).toBe("tab-voice");
    expect(window.location.hash).toBe("#voice");
    window.location.hash = "";
  });

  it("leaves focus on the Ads tab when it is chosen", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    freshRouter();
    document.querySelector("#tab-ads").click();
    await vi.waitFor(() => expect(document.querySelector("#panel-ads").hidden).toBe(false));
    expect(document.activeElement.id).toBe("tab-ads");
    expect(window.location.hash).toBe("#ads");
    window.location.hash = "";
  });

  it("leaves focus on the Carts tab when it is chosen", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    freshRouter();
    document.querySelector("#tab-carts").click();
    await vi.waitFor(() => expect(document.querySelector("#panel-carts").hidden).toBe(false));
    expect(document.activeElement.id).toBe("tab-carts");
    expect(window.location.hash).toBe("#carts");
    window.location.hash = "";
  });

  it("routes directly to Track info and leaves focus on its tab", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    freshRouter();
    document.querySelector("#tab-track-info").click();
    await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
    expect(document.activeElement.id).toBe("tab-track-info");
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

describe("visible status mirror", () => {
  it("clears a status left by the view a user switches away from", async () => {
    installIndex();
    window.location.hash = "#library";
    vi.resetModules();
    const { initViewRouter: freshRouter } = await import(
      "../../src/autodj/static/modules/tabs.js");
    const { showVisibleStatus } = await import(
      "../../src/autodj/static/modules/live-region.js");
    try {
      freshRouter();
      showVisibleStatus("Enter a name for the category.", document, { tone: "error" });
      const toast = document.querySelector("#status-toast");
      expect(toast.hidden).toBe(false);
      // Choosing the tab already shown leaves the message alone.
      document.querySelector("#tab-library").click();
      expect(toast.hidden).toBe(false);
      document.querySelector("#tab-settings").click();
      expect(toast.hidden).toBe(true);
      expect(toast.textContent).toBe("");
    } finally {
      window.location.hash = "";
    }
  });
});
