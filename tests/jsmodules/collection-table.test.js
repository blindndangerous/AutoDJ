import { beforeEach, describe, expect, it, vi } from "vitest";
import axe from "axe-core";
import * as collections from "../../src/autodj/static/modules/collection-table.js";

const { createCollectionTable } = collections;

const press = (key) => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
let host, status, fallback;
beforeEach(() => {
  document.body.innerHTML = '<div id="host"></div><button id="add">Add item</button><p id="status" role="status"></p>';
  host = document.getElementById("host");
  status = document.getElementById("status");
  fallback = document.getElementById("add");
});
function mount(extra = {}) {
  return createCollectionTable({ host, label: "Examples", status, fallback,
    columns: [{ key: "name", label: "Name", value: (item) => item.name }, { key: "count", label: "Count", value: (item) => item.count }],
    keyOf: (item) => item.id, nameOf: (item) => item.name,
    primary: (item) => ({ id: "edit", label: "Edit", run: () => extra.edit?.(item) }),
    actions: (item) => [{ id: "delete", label: "Delete", run: () => extra.remove?.(item) }], ...extra,
  });
}
describe("collection tables", () => {
  // ADR-0021-23
  it("refreshes the focused row context after keyed data changes", () => {
    const grid = mount();
    grid.update([{ id: "a", name: "First", count: 1 }, { id: "b", name: "Second", count: 2 }]);
    grid.target("a", "count").focus();
    press("ArrowDown");
    const count = grid.target("b", "count");
    expect(count.getAttribute("aria-label")).toBe("2, Second");
    grid.update([{ id: "a", name: "First", count: 1 }, { id: "b", name: "Renamed", count: 3 }]);
    expect(document.activeElement).toBe(count);
    expect(count.getAttribute("aria-label")).toBe("3, Renamed");
    press("ArrowLeft");
    expect(count.hasAttribute("aria-label")).toBe(false);
  });
  // ADR-0021-17
  it("keeps computed data names free of native headers after navigation and updates", () => {
    const grid = mount();
    grid.update([{ id: "a", name: "Evening", count: 2 }]);
    grid.target("a", "name").focus();
    press("ArrowRight");
    const names = () => {
      axe.setup(document);
      try {
        const row = grid.body.firstElementChild;
        return [row.cells[0], row.cells[1], row.querySelector('button[data-action="edit"]'), row.querySelector('[aria-haspopup="menu"]')]
          .map((element) => axe.commons.text.accessibleText(element));
      }
      finally { axe.teardown(); }
    };
    expect(names()).toEqual(["Evening", "2", "Edit Evening", "Options for Evening"]);
    grid.update([{ id: "a", name: "Later", count: 3 }]);
    expect(names()).toEqual(["Later", "3", "Edit Later", "Options for Later"]);
  });
  it("finds a collection's owning host independently of internal wrappers", () => {
    const grid = mount();
    grid.update([{ id: "a", name: "First" }]);
    const cell = grid.target("a");
    const wrapper = document.createElement("div");
    grid.table.before(wrapper);
    wrapper.append(grid.table);
    expect(collections.collectionHost(cell)).toBe(host);
    expect(collections.collectionHost(cell.querySelector("button"))).toBe(host);
    expect(collections.collectionHost(grid.body.querySelector('[role="menuitem"]'))).toBe(host);
    expect(collections.collectionHost(fallback)).toBeNull();
    expect(collections.collectionHost(null)).toBeNull();
    grid.clear();
    expect(collections.collectionHost(grid.table)).toBe(host);
  });
  // ADR-0021-01
  it("preserves keyed rows and focus while updating data", () => {
    const edit = vi.fn();
    const grid = mount({ edit });
    grid.update([{ id: "a", name: "First", count: 1 }, { id: "b", name: "Second", count: 2 }]);
    const first = grid.body.firstElementChild;
    const target = grid.target("a");
    target.focus();
    grid.update([{ id: "b", name: "Second", count: 3 }, { id: "a", name: "Renamed", count: 4 }]);
    expect(grid.body.lastElementChild).toBe(first);
    expect(document.activeElement).toBe(target);
    expect(first.cells[0].textContent).toBe("Renamed");
    press("Enter");
    expect(edit).toHaveBeenCalledWith({ id: "a", name: "Renamed", count: 4 });
    expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    expect(host.querySelector('[role="rowheader"]')).toBeNull();
  });
  // ADR-0021-02
  it("renders one empty row across all columns", () => {
    const grid = mount({ emptyText: "No examples." });
    grid.update([]);
    expect(grid.body.children).toHaveLength(1);
    expect(grid.body.querySelector(".no-results").textContent).toBe("No examples.");
    expect(grid.body.children[0].cells[0].colSpan).toBe(4);
    expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
  });
  // ADR-0021-03
  it("moves focus after removal and announces the new target", async () => {
    const grid = mount();
    grid.update([{ id: "a", name: "First" }, { id: "b", name: "Second" }]);
    grid.target("a").focus();
    grid.update([{ id: "b", name: "Second" }]);
    expect(document.activeElement).toBe(grid.target("b"));
    await vi.waitFor(() => expect(status.textContent).toContain("The Examples list changed. Focus moved to Edit Second."));
    grid.update([]);
    expect(document.activeElement).toBe(fallback);
  });
  it("keeps popup focus through refresh and returns Escape to the sole Options tab stop", () => {
    const remove = vi.fn();
    const grid = mount({ remove });
    grid.update([{ id: "a", name: "First", count: 1 }]);
    grid.target("a", "options").focus();
    press("Enter");
    const item = document.activeElement;
    expect(item.getAttribute("role")).toBe("menuitem");
    grid.update([{ id: "a", name: "Renamed", count: 2 }]);
    expect(document.activeElement).toBe(item);
    press("Escape");
    expect(document.activeElement).toBe(grid.target("a", "options"));
    expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    press("Enter");
    press("Enter");
    expect(remove).toHaveBeenCalledWith({ id: "a", name: "Renamed", count: 2 });
  });
  it("keeps disabled primary actions focusable and explains them", async () => {
    const run = vi.fn();
    const grid = mount({ primary: () => ({ id: "edit", label: "Edit", disabledReason: "Save first.", run }) });
    grid.update([{ id: "a", name: "First" }]);
    grid.target("a").focus();
    press("Enter");
    expect(run).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(status.textContent).toBe("Save first."));
    expect(grid.target("a").querySelector("button").disabled).toBe(false);
  });
  // ADR-0021-15
  it("associates the current disabled primary reason before activation", () => {
    const grid = mount({ primary: (item) => ({ id: "edit", label: "Edit", disabledReason: item.reason, run() {} }) });
    grid.update([{ id: "a", name: "First", reason: "Save first." }]);
    const button = grid.target("a").querySelector("button");
    const id = button.getAttribute("aria-describedby");
    expect(id).toBeTruthy();
    expect(document.getElementById(id).textContent).toBe("Save first.");
    grid.update([{ id: "a", name: "First", reason: "Still saving." }]);
    expect(button.getAttribute("aria-describedby")).toBe(id);
    expect(document.getElementById(id).textContent).toBe("Still saving.");
    grid.update([{ id: "a", name: "First" }]);
    expect(button.hasAttribute("aria-describedby")).toBe(false);
  });
  it("preserves focus on an unchanged empty state", () => {
    const grid = mount();
    grid.update([]);
    const empty = grid.body.querySelector("td");
    empty.focus();
    grid.update([]);
    expect(document.activeElement).toBe(empty);
  });
  it("recovers focus when an open menu row disappears", async () => {
    const grid = mount();
    grid.update([{ id: "a", name: "First" }, { id: "b", name: "Second" }]);
    grid.target("a", "options").focus();
    press("Enter");
    expect(document.activeElement.getAttribute("role")).toBe("menuitem");
    grid.update([{ id: "b", name: "Second" }]);
    expect(document.activeElement).toBe(grid.target("b", "options"));
    expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    await vi.waitFor(() => expect(status.textContent).toContain("Options for Second"));
  });
  // ADR-0021-18
  it("resumes grid arrows from the Options cell after Escape", () => {
    const grid = mount();
    grid.update([{ id: "a", name: "First" }]);
    grid.target("a", "options").focus();
    press("Enter");
    press("Escape");
    expect(document.activeElement).toBe(grid.target("a", "options"));
    press("ArrowLeft");
    expect(document.activeElement).toBe(grid.target("a"));
  });
});
