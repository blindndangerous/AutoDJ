// Browse library and Browse voice liners live in Queue & Search, after
// Search (#47).  These checks run against the real index.html.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
}

const byId = (id) => document.getElementById(id);

// Focusable controls in DOM order, as Tab reaches them.
// A control inside a hidden part of *root* (a closed form) is not in the order.
function hiddenWithin(node, root) {
  for (let element = node; element && element !== root; element = element.parentElement) {
    if (element.hidden) return true;
  }
  return false;
}

function tabOrder(root) {
  return [...root.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex]")]
    .filter((node) => !node.disabled && node.getAttribute("tabindex") !== "-1" && !hiddenWithin(node, root));
}

afterEach(() => {
  window.location.hash = "";
  document.body.replaceChildren();
});

describe("library browsing in Queue & Search", () => {
  it("puts Browse library, then Browse voice liners, after Search", () => {
    installIndex();
    const queue = byId("panel-queue");
    const sections = [...queue.children].filter((node) => node.matches("details.card, section.card"));
    expect(sections.map((node) => node.querySelector("h2").textContent.trim().split(/\s+/)[0]))
      .toEqual(["Queue", "Search", "Browse", "Browse"]);
    expect(sections[2].querySelector("h2").id).toBe("browse-library-heading");
    expect(sections[2].getAttribute("aria-labelledby")).toBe("browse-library-heading");
    expect(sections[3].querySelector("h2").textContent).toBe("Browse voice liners");
    expect(sections[3].getAttribute("aria-labelledby")).toBe("browse-liners-heading");
    for (const id of ["library-browse-folder", "library-browse-query", "library-browse",
      "library-browse-count", "library-browse-results", "library-browse-previous",
      "library-browse-next"]) {
      expect(sections[2].contains(byId(id)), id).toBe(true);
    }
    expect(sections[3].contains(byId("ln-file-list"))).toBe(true);
    expect(sections[3].contains(byId("ln-browse-status"))).toBe(true);
  });

  it("keeps the Browse library card's labels, count and paging intact", () => {
    installIndex();
    expect(document.querySelector('label[for="library-browse-folder"]').textContent).toBe("Folder");
    expect(document.querySelector('label[for="library-browse-query"]').textContent)
      .toBe("Search text (optional)");
    expect([...byId("library-browse-folder").options].map((option) => option.value))
      .toEqual(["all-music", "all-liners"]);
    expect(byId("library-browse-count").getAttribute("aria-live")).toBe("polite");
    expect(byId("library-browse-results").getAttribute("aria-label")).toBe("Library tracks");
    expect(byId("library-browse-previous").textContent).toBe("Previous");
    expect(byId("library-browse-next").textContent).toBe("Next");
  });

  it("tabs from Search to Browse library to the voice liners", () => {
    installIndex();
    const order = tabOrder(byId("panel-queue"));
    const at = (id) => order.indexOf(byId(id));
    expect(at("btn-search")).toBeGreaterThan(at("search-input"));
    expect(at("library-browse-folder")).toBe(at("btn-search") + 1);
    expect(at("library-browse-query")).toBe(at("library-browse-folder") + 1);
    expect(at("library-browse")).toBe(at("library-browse-query") + 1);
    // Liner rows come after Next, the last Browse library control.
    const list = byId("ln-file-list");
    list.innerHTML = '<li><span>id.wav</span><button type="button">Delete</button></li>';
    const withRow = tabOrder(byId("panel-queue"));
    expect(withRow.indexOf(list.querySelector("button")))
      .toBeGreaterThan(withRow.indexOf(byId("library-browse-previous")));
    expect(withRow.at(-1)).toBe(list.querySelector("button"));
  });

  it("gives the voice liner section its own polite status and a focusable list", () => {
    installIndex();
    const status = byId("ln-browse-status");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.getAttribute("aria-atomic")).toBe("true");
    const list = byId("ln-file-list");
    expect(list.getAttribute("aria-label")).toBe("Voice liner files");
    expect(list.getAttribute("role")).toBe("list");
    expect(list.getAttribute("tabindex")).toBe("-1");
  });

  it("leaves folder upkeep in Library tools with a pointer to the new place", () => {
    installIndex();
    const library = byId("panel-library");
    expect(library.querySelector("#browse-library-heading, #library-browse-results, #ln-file-list"))
      .toBeNull();
    for (const id of ["source-folder-path", "source-folder-add", "source-folder-rescan", "source-folders"]) {
      expect(library.contains(byId(id)), id).toBe(true);
    }
    const pointer = [...library.querySelectorAll("p")]
      .find((node) => node.textContent === "Browse your library and voice liners in Queue & Search.");
    expect(pointer).toBeDefined();
    // Read straight after the card's heading, where focus lands on a tab switch.
    expect(pointer.previousElementSibling.tagName).toBe("SUMMARY");
  });

  it("keeps uploading in Settings, Voice liners and says where the list went", () => {
    installIndex();
    const settings = byId("spanel-liners");
    expect(settings.querySelector("#ln-file-list, h4")).toBeNull();
    expect(settings.contains(byId("ln-upload-submit"))).toBe(true);
    expect(settings.contains(byId("ln-status"))).toBe(true);
    expect(settings.textContent).toContain("in Queue & Search, under Browse voice liners");
  });

  it("has unique ids and no skipped heading levels in either tab", () => {
    installIndex();
    const ids = [...document.querySelectorAll("[id]")].map((node) => node.id);
    expect(ids.filter((id, index) => ids.indexOf(id) !== index)).toEqual([]);
    for (const panelId of ["panel-queue", "panel-library"]) {
      const levels = [...byId(panelId).querySelectorAll("h1, h2, h3, h4, h5, h6")]
        .map((node) => Number(node.tagName[1]));
      expect(levels[0], panelId).toBe(2);
      levels.forEach((level, index) => {
        if (index) expect(level - levels[index - 1], panelId).toBeLessThanOrEqual(1);
      });
    }
    for (const node of document.querySelectorAll("[aria-labelledby]")) {
      for (const id of node.getAttribute("aria-labelledby").split(/\s+/)) {
        expect(byId(id), `${node.id || node.tagName} -> ${id}`).not.toBeNull();
      }
    }
    expect(document.querySelector('[role="tablist"][aria-controls], [role="tab"][aria-controls]')).toBeNull();
  });

  it("lands #queue on the moved sections and focuses the first card", async () => {
    installIndex();
    window.location.hash = "#now";
    vi.resetModules();
    const { initViewRouter } = await import("../../src/autodj/static/modules/tabs.js");
    initViewRouter();
    byId("tab-queue").click();
    await vi.waitFor(() => expect(byId("panel-queue").hidden).toBe(false));
    expect(window.location.hash).toBe("#queue");
    expect(byId("browse-library-heading").closest("[hidden]")).toBeNull();
    expect(byId("ln-file-list").closest("[hidden]")).toBeNull();
    expect(document.activeElement).toBe(document.querySelector("#panel-queue summary"));
  });
});
