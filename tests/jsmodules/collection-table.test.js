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
    actions: (item) => [
      { id: "edit", label: "Edit", run: () => extra.edit?.(item) },
      { id: "delete", label: "Delete", run: () => extra.remove?.(item) },
    ], ...extra,
  });
}
describe("collection tables", () => {
  // ADR-0021-02
  it("starts with a spanning empty row before the first asynchronous load", () => {
    const grid = mount({ emptyText: "No items loaded." });
    const empty = grid.body.querySelector(".no-results");
    expect(empty).not.toBeNull();
    expect(empty.colSpan).toBe(3);
    expect(empty.textContent).toBe("No items loaded.");
    expect(grid.table.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    grid.update([{ id: "a", name: "Ready", count: 1 }]);
    expect(grid.body.querySelector(".no-results")).toBeNull();
    expect(grid.body.querySelectorAll("tr[data-row-key]")).toHaveLength(1);
  });

  // ADR-0021-03
  it("names a labelled fallback field when the last focused row disappears", async () => {
    const label = document.createElement("label"); label.htmlFor = "profile"; label.textContent = "Profile name";
    const input = document.createElement("input"); input.id = "profile";
    host.after(label, input);
    const grid = mount({ fallback: input });
    grid.update([{ id: "a", name: "Morning" }]);
    grid.target("a", "options").focus();
    grid.update([]);
    expect(document.activeElement).toBe(input);
    await vi.waitFor(() => expect(status.textContent).toBe("The Examples list changed. Focus moved to Profile name."));
  });

  // ADR-0021-34
  it("puts every row action in one Actions column menu with the former primary action first", () => {
    const grid = mount();
    grid.update([{ id: "a", name: "First", count: 1 }]);
    expect([...grid.table.querySelectorAll("th")].map((cell) => cell.textContent)).toEqual(["Name", "Count", "Actions"]);
    const row = grid.body.firstElementChild;
    expect(row.cells).toHaveLength(3);
    const buttons = [...row.querySelectorAll("button")].filter((button) => !button.closest('[role="menu"]'));
    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual(["Options for First"]);
    expect(buttons[0].textContent).toBe("Options");
    expect(buttons[0].closest("td")).toBe(row.cells[2]);
    expect(grid.target("a")).toBe(row.cells[2]);
    expect(grid.target("a", "edit")).toBe(row.cells[2]);
    expect(grid.target("a", "count")).toBe(row.cells[1]);
    expect([...row.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent)).toEqual(["Edit", "Delete"]);
    const readOnly = mount({ actions: undefined, host: document.body.appendChild(document.createElement("div")) });
    readOnly.update([{ id: "a", name: "Earlier", count: 2 }]);
    expect([...readOnly.table.querySelectorAll("th")].map((cell) => cell.textContent)).toEqual(["Name", "Count"]);
    expect(readOnly.table.querySelector("button")).toBeNull();
    expect(readOnly.target("a")).toBe(readOnly.body.firstElementChild.cells[0]);
  });
  // ADR-0021-35
  it("opens a row's menu from the context menu key or right-click and returns to the originating cell", () => {
    const remove = vi.fn();
    const grid = mount({ remove });
    grid.update([{ id: "a", name: "First", count: 1 }, { id: "b", name: "Second", count: 2 }]);
    const context = (target) => {
      const event = new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      target.dispatchEvent(event);
      return event;
    };
    const count = grid.target("a", "count");
    const button = grid.target("a").querySelector('[aria-haspopup="menu"]');
    count.focus();
    expect(context(count).defaultPrevented).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement.textContent).toBe("Edit");
    press("Escape");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(count);
    press("ArrowLeft");
    expect(document.activeElement).toBe(grid.target("a", "name"));
    const name = grid.target("b", "name");
    expect(context(name).defaultPrevented).toBe(true);
    const second = grid.target("b").querySelector('[aria-haspopup="menu"]');
    expect(second.getAttribute("aria-expanded")).toBe("true");
    press("Tab");
    expect(second.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(name);
    context(name);
    press("End");
    press("Enter");
    expect(remove).toHaveBeenCalledWith({ id: "b", name: "Second", count: 2 });
    expect(document.activeElement).toBe(name);
    button.click();
    press("Escape");
    expect(document.activeElement).toBe(grid.target("a"));
    expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
  });
  // ADR-0021-25
  it("lets a menu action that moves focus take it straight from the menu", () => {
    const field = document.createElement("input");
    document.body.append(field);
    const grid = mount({ edit: () => field.focus() });
    grid.update([{ id: "a", name: "First" }]);
    grid.target("a", "name").focus();
    grid.target("a", "name").dispatchEvent(new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    const seen = [];
    const record = (event) => seen.push(event.target);
    document.addEventListener("focusin", record, true);
    press("Enter");
    document.removeEventListener("focusin", record, true);
    expect(seen).toEqual([field]);
    expect(grid.target("a").querySelector('[aria-haspopup="menu"]').getAttribute("aria-expanded")).toBe("false");
  });
  it("opens a row's Track info by its action id from the name cell and honours its disabled reason", async () => {
    const info = vi.fn();
    const grid = mount({ actions: (item) => [{ id: "info", label: "Details", disabledReason: item.reason || "", run: info }] });
    grid.update([{ id: "a", name: "Song" }, { id: "b", name: "Gone", reason: "This song is no longer in the library." }]);
    grid.target("a", "name").focus();
    press("Enter");
    expect(info).toHaveBeenCalledOnce();
    grid.target("b", "name").focus();
    press("Enter");
    expect(info).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(status.textContent).toBe("This song is no longer in the library."));
  });
  // ADR-0021-35
  it("leaves the browser menu on rows and tables without actions", () => {
    const grid = mount({ actions: (item) => item.locked ? [] : [{ id: "edit", label: "Edit", run() {} }] });
    grid.update([{ id: "a", name: "Locked", locked: true }]);
    const event = new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    grid.target("a", "name").dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    grid.update([]);
    const empty = new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    grid.body.querySelector(".no-results").dispatchEvent(empty);
    expect(empty.defaultPrevented).toBe(false);
    const readOnly = mount({ actions: undefined, host: document.body.appendChild(document.createElement("div")) });
    readOnly.update([{ id: "a", name: "Plain" }]);
    const plain = new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    readOnly.target("a").dispatchEvent(plain);
    expect(plain.defaultPrevented).toBe(false);
  });
  // ADR-0021-36
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
  // ADR-0021-36
  it("adds a changed column heading to computed data names after navigation and updates", () => {
    const grid = mount();
    grid.update([{ id: "a", name: "Evening", count: 2 }]);
    grid.target("a", "name").focus();
    press("ArrowRight");
    const names = () => {
      axe.setup(document);
      try {
        const row = grid.body.firstElementChild;
        return [row.cells[0], row.cells[1], row.querySelector('[aria-haspopup="menu"]')]
          .map((element) => axe.commons.text.accessibleText(element));
      }
      finally { axe.teardown(); }
    };
    expect(names()).toEqual(["Evening", "2, Count", "Options for Evening"]);
    grid.update([{ id: "a", name: "Later", count: 3 }]);
    expect(names()).toEqual(["Later", "3, Count", "Options for Later"]);
    press("ArrowLeft");
    expect(names()).toEqual(["Later, Name", "3", "Options for Later"]);
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
    expect(document.activeElement.textContent).toBe("Edit");
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
    expect(grid.body.children[0].cells[0].colSpan).toBe(3);
    expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
  });
  // ADR-0021-03
  it("moves focus after removal and announces the new target", async () => {
    const grid = mount();
    grid.update([{ id: "a", name: "First" }, { id: "b", name: "Second" }]);
    grid.target("a").focus();
    grid.update([{ id: "b", name: "Second" }]);
    expect(document.activeElement).toBe(grid.target("b"));
    await vi.waitFor(() => expect(status.textContent).toContain("The Examples list changed. Focus moved to Options for Second."));
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
    press("End");
    press("Enter");
    expect(remove).toHaveBeenCalledWith({ id: "a", name: "Renamed", count: 2 });
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
    expect(document.activeElement).toBe(grid.target("a", "count"));
  });
});
