import { describe, expect, it, vi } from "vitest";
import { createRowActionsMenu, rowActionsCell } from "../../src/autodj/static/modules/row-actions-menu.js";
import { createCollectionTable } from "../../src/autodj/static/modules/collection-table.js";
const keyEvent = (type, key, init = {}) => {
  const event = new KeyboardEvent(type, { key, bubbles: true, cancelable: true, ...init });
  document.activeElement.dispatchEvent(event);
  return event;
};
// A menu focuses its first item a frame after it is shown (#712).
const menuFocus = () => vi.waitFor(() => expect(document.activeElement.getAttribute("role")).toBe("menuitem"));
const press = (key, init = {}) => {
  keyEvent("keydown", key, init);
  if (key === " ") keyEvent("keyup", key, init);
};

function table(actions) {
  document.body.innerHTML = '<div id="rows"></div><button id="outside">Outside</button><p id="status" role="status"></p>';
  const status = document.getElementById("status");
  const grid = createCollectionTable({ host: document.getElementById("rows"), label: "Examples", status,
    columns: [{ key: "name", label: "Name", value: (item) => item }], keyOf: (item) => item, nameOf: (item) => item,
    actions: () => actions,
  });
  grid.update(["First", "Second"]);
  return { grid, status, first: grid.body.querySelector('[aria-haspopup="menu"]') };
}
describe("row action menus", () => {
  it.each(["Escape", "Tab", "Enter"])("restores the Options gridcell after %s closes the menu", (key) => {
    const run = vi.fn();
    const { grid, first } = table([{ id: "run", label: "Run now", run }]);
    first.click();
    press(key);
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(grid.target("First", "options"));
    expect(first.tabIndex).toBe(-1);
    press("ArrowLeft");
    expect(document.activeElement).toBe(grid.target("First", "name"));
    expect(run).toHaveBeenCalledTimes(key === "Enter" ? 1 : 0);
  });
  // ADR-0021-40
  it("shows the menu before it focuses the first item and routes keys that arrive first", async () => {
    const { grid, first } = table([{ id: "edit", label: "Edit", run() {} }, { id: "run", label: "Run now", run() {} }]);
    const cell = grid.target("First");
    const focused = [];
    document.addEventListener("focusin", (event) => focused.push(event.target), true);
    cell.focus();
    focused.length = 0;
    press("Enter");
    const menu = document.getElementById(first.getAttribute("aria-controls"));
    // Shown now; focused only after the browser has announced the popup.
    expect(menu.hidden).toBe(false);
    expect(document.activeElement).toBe(cell);
    await vi.waitFor(() => expect(document.activeElement.textContent).toBe("Edit"));
    expect(focused.map((item) => item.textContent)).toEqual(["Edit"]);
    press("Escape");
    expect(document.activeElement).toBe(cell);
    // A key pressed before that focus goes to the menu, not the table.
    press("Enter");
    press("ArrowDown");
    expect(document.activeElement.textContent).toBe("Run now");
    press("Escape");
    press("Enter");
    press("Escape");
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(cell);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(document.activeElement).toBe(cell);
  });
  // ADR-0021-40
  it("focuses the replacement first item when an update replaces it while focus is pending", async () => {
    document.body.innerHTML = '<p id="status"></p>';
    const run = { id: "run", label: "Run now", run() {} };
    const menu = createRowActionsMenu({ doc: document, name: "Example", actions: [{ id: "edit", label: "Edit", run() {} }, run] });
    document.body.append(menu.element);
    menu.button.focus();
    press("Enter");
    menu.update("Example", [{ id: "copy", label: "Copy", run() {} }, run]);
    await vi.waitFor(() => expect(document.activeElement.textContent).toBe("Copy"));
    expect(menu.button.getAttribute("aria-expanded")).toBe("true");
  });
  // ADR-0021-40
  it("closes quietly instead of taking focus that moved elsewhere before the deferred focus", async () => {
    const { first } = table([{ id: "edit", label: "Edit", run() {} }]);
    first.click();
    document.getElementById("outside").focus();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(document.activeElement.id).toBe("outside");
    expect(first.getAttribute("aria-expanded")).toBe("false");
  });
  // ADR-0021-40
  it("keeps one pending menu: opening a second closes the first and takes the next key", async () => {
    const run = vi.fn();
    const { grid } = table([{ id: "edit", label: "Edit", run() {} }, { id: "run", label: "Run now", run }]);
    const [a, b] = grid.body.querySelectorAll('[aria-haspopup="menu"]');
    a.click();
    const cell = grid.target("Second", "name");
    cell.focus();
    cell.dispatchEvent(new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    expect(a.getAttribute("aria-expanded")).toBe("false");
    expect(b.getAttribute("aria-expanded")).toBe("true");
    press("ArrowDown");
    expect(document.activeElement.textContent).toBe("Run now");
    expect(b.closest("tr").contains(document.activeElement)).toBe(true);
    press("Enter");
    expect(run).toHaveBeenCalledOnce();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(document.activeElement).toBe(cell);
  });
  it("closes an open menu whose actions all disappear", async () => {
    document.body.innerHTML = '<p id="status"></p><button id="before">Before</button>';
    const menu = createRowActionsMenu({ doc: document, name: "Example", actions: [{ id: "edit", label: "Edit", run() {} }] });
    document.body.append(menu.element);
    menu.button.focus();
    press("Enter");
    menu.update("Example", []);
    expect(menu.button.getAttribute("aria-expanded")).toBe("false");
    const key = new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true });
    document.getElementById("before").dispatchEvent(key);
    expect(key.defaultPrevented).toBe(false);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(document.activeElement.getAttribute("role")).not.toBe("menuitem");
  });
  it("anchors a menu opened from another cell to that cell", () => {
    const { grid, first } = table([{ id: "edit", label: "Edit", run() {} }]);
    const cell = grid.target("First", "name");
    const rect = (left, top) => () => ({ left, top, right: left + 50, bottom: top + 20, width: 50, height: 20, x: left, y: top });
    cell.getBoundingClientRect = rect(10, 100);
    first.getBoundingClientRect = rect(300, 100);
    cell.focus();
    cell.dispatchEvent(new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    const menu = document.getElementById(first.getAttribute("aria-controls"));
    expect(menu.style.left).toBe("10px");
    expect(menu.style.top).toBe("120px");
  });
  // ADR-0021-04
  it("supports menu keys and disabled reasons", async () => {
    document.body.innerHTML = '<p id="status" role="status"></p>';
    const status = document.getElementById("status");
    const run = vi.fn();
    const menu = createRowActionsMenu({ doc: document, name: "Example", status, actions: [
      { id: "run", label: "Run now", run },
      { id: "disable", label: "Disable", disabledReason: "Already disabled.", run },
      { id: "delete", label: "Delete", run },
    ] });
    document.body.append(menu.element);
    const popup = document.getElementById(menu.button.getAttribute("aria-controls"));
    expect(menu.button.getAttribute("aria-haspopup")).toBe("menu");
    menu.button.focus();
    press("ArrowUp");
    await menuFocus();
    expect(document.activeElement.textContent).toBe("Delete");
    press("Home");
    expect(document.activeElement.textContent).toBe("Run now");
    press("ArrowDown");
    expect(document.activeElement.getAttribute("aria-disabled")).toBe("true");
    press("Enter");
    expect(run).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(status.textContent).toBe("Already disabled."));
    press("End");
    press("r");
    expect(document.activeElement.textContent).toBe("Run now");
    press("Enter");
    expect(run).toHaveBeenCalledOnce();
    expect(menu.button.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(menu.button);
    for (const key of ["Enter", " ", "ArrowDown"]) {
      press(key);
      await menuFocus();
      expect(document.activeElement.textContent).toBe("Run now");
      press("Escape");
      expect(document.activeElement).toBe(menu.button);
    }
    press("Enter");
    press("Tab");
    expect(popup.hidden).toBe(true);
  });
  it("wraps the menu in a plain grid cell", () => {
    const menu = createRowActionsMenu({ doc: document, name: "Example", actions: [{ id: "edit", label: "Edit", run() {} }] });
    const cell = rowActionsCell(document, menu);
    expect(cell.tagName).toBe("TD");
    expect(cell.getAttribute("role")).toBe("gridcell");
    expect(cell.querySelector("button").tabIndex).toBe(-1);
  });
  it("opens on Space release without a duplicate native activation", async () => {
    const run = vi.fn();
    document.body.innerHTML = "";
    const menu = createRowActionsMenu({ doc: document, name: "First", actions: [{ id: "run", label: "Run now", run }] });
    document.body.append(menu.element);
    const first = menu.button;
    first.focus();
    expect(keyEvent("keydown", " ").defaultPrevented).toBe(true);
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(first);
    expect(keyEvent("keyup", " ").defaultPrevented).toBe(true);
    expect(first.getAttribute("aria-expanded")).toBe("true");
    await menuFocus();
    press(" ");
    expect(run).toHaveBeenCalledOnce();
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(first);
  });
  it("closes on an outside click and allows only one open menu", () => {
    const { grid, first } = table([{ id: "run", label: "Run now", run() {} }]);
    first.click();
    document.getElementById("outside").click();
    expect(first.getAttribute("aria-expanded")).toBe("false");
    first.click();
    const second = grid.body.querySelectorAll('[aria-haspopup="menu"]')[1];
    second.click();
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(second.getAttribute("aria-expanded")).toBe("true");
    expect(grid.table.querySelectorAll('[role="menu"]:not([hidden])')).toHaveLength(1);
  });
  it("exposes exactly one table Tab stop throughout menu round trips", () => {
    const { grid } = table([{ id: "run", label: "Run now", run() {} }]);
    const stops = () => [...grid.table.querySelectorAll('td, button, [tabindex]')]
      .filter((element) => element.tabIndex >= 0 && !element.closest("[hidden]"));
    grid.target("First", "options").focus();
    expect(stops()).toHaveLength(1);
    for (const key of ["Enter", "Escape", " ", "Tab"]) {
      press(key);
      expect(stops()).toHaveLength(1);
    }
  });
  // ADR-0021-15
  it("describes a disabled choice before activation and updates or removes its reason", () => {
    document.body.innerHTML = "";
    const disabled = (disabledReason) => [{ id: "run", label: "Run now", disabledReason, run() {} }];
    const menu = createRowActionsMenu({ doc: document, name: "Example", actions: disabled("Start the station to run this event now") });
    document.body.append(menu.element);
    const item = menu.element.querySelector('[role="menuitem"]');
    const id = item.getAttribute("aria-describedby");
    expect(id).toBeTruthy();
    expect(document.getElementById(id).hidden).toBe(true);
    expect(document.getElementById(id).textContent).toBe("Start the station to run this event now");
    menu.update("Example", disabled("Still stopped"));
    expect(item.getAttribute("aria-describedby")).toBe(id);
    expect(document.getElementById(id).textContent).toBe("Still stopped");
    menu.update("Example", disabled(""));
    expect(item.hasAttribute("aria-describedby")).toBe(false);
    expect(document.getElementById(id)).toBeNull();
    expect(item.getAttribute("aria-disabled")).toBe("false");
  });
  it.each(["mouse", "Enter", "Space", "type-ahead then Enter"])("blocks off-air activation by %s and announces why", async (method) => {
    const run = vi.fn();
    const { first, status } = table([{ id: "run", label: "Run now", disabledReason: "Start the station to run this event now", run }]);
    first.click();
    await menuFocus();
    const item = document.activeElement;
    if (method === "mouse") item.click();
    else if (method === "Space") press(" ");
    else {
      if (method === "type-ahead then Enter") press("r");
      press("Enter");
    }
    expect(run).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(status.textContent.trim()).toBe("Start the station to run this event now"));
    expect(first.getAttribute("aria-expanded")).toBe("true");
  });
});
