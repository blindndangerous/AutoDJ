// Queue & Search holds one library results table; liner file maintenance stays in Settings.
import { afterEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

const byId = (id) => document.getElementById(id);
function tabOrder(root) {
  return [...root.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex]")]
    .filter((node) => {
      if (node.disabled || node.getAttribute("tabindex") === "-1") return false;
      for (let parent = node; parent && parent !== root; parent = parent.parentElement) if (parent.hidden) return false;
      return true;
    });
}
afterEach(() => { window.location.hash = ""; document.body.replaceChildren(); });

describe("unified library search in Queue & Search", () => {
  it("has one result panel with independent type, folder, and text controls", () => {
    installIndex();
    const queue = byId("panel-queue");
    expect(queue.querySelectorAll("#search-results, #library-results-table")).toHaveLength(2);
    expect(queue.querySelector("#library-kind").value).toBe("music");
    expect(queue.querySelector("#library-kind").options[1].value).toBe("liner");
    expect(queue.querySelector('label[for="library-folder"]').textContent).toBe("Folder");
    expect(queue.querySelector('label[for="search-input"]').textContent).toBe("Search text (optional)");
    expect(queue.querySelector("#search-results").tagName).toBe("TBODY");
    expect(queue.querySelector("#library-browse-query, #library-browse-results, #ln-file-list")).toBeNull();
    expect(queue.querySelectorAll("[data-view]")).toHaveLength(0);
  });

  it("keeps existing search IDs and puts the type and folder controls before the query", () => {
    installIndex();
    const queue = byId("panel-queue");
    const order = tabOrder(queue);
    const at = (id) => order.indexOf(byId(id));
    expect(byId("search-input")).not.toBeNull();
    expect(byId("btn-search")).not.toBeNull();
    expect(at("library-kind")).toBeLessThan(at("library-folder"));
    expect(at("library-folder")).toBeLessThan(at("search-input"));
    expect(at("search-input")).toBeLessThan(at("btn-search"));
  });

  it("keeps folder maintenance in Library tools as a native table", () => {
    installIndex();
    const library = byId("panel-library");
    for (const id of ["source-folder-path", "source-folder-add", "source-folder-rescan", "source-folders-table", "source-folders"]) {
      expect(library.contains(byId(id)), id).toBe(true);
    }
    expect(byId("source-folders-table").querySelectorAll("th")).toHaveLength(5);
  });

  it("keeps liner upload, delete, and play-date maintenance in Settings", () => {
    installIndex();
    const settings = byId("spanel-liners");
    expect(settings.contains(byId("ln-upload-submit"))).toBe(true);
    expect(settings.contains(byId("ln-file-list"))).toBe(true);
    expect(settings.contains(byId("ln-dates-form"))).toBe(true);
    expect(byId("panel-queue").contains(byId("ln-file-list"))).toBe(false);
    expect(settings.textContent).toContain("Voice liner files");
  });

  it("lands #queue on the queue tab and leaves focus on the tab", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter } = await import("../../src/autodj/static/modules/tabs.js");
    initViewRouter();
    byId("tab-queue").click();
    await vi.waitFor(() => expect(byId("panel-queue").hidden).toBe(false));
    expect(window.location.hash).toBe("#queue");
    expect(byId("search-library-heading").closest("[hidden]")).toBeNull();
    expect(document.activeElement).toBe(byId("tab-queue"));
  });
});
