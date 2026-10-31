import { placeRows, rowButton } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { tableRow } from "./table-row.js";

let nextMenu = 0;

// The menu is inside its table cell for ownership and focus recovery. The
// native popover top layer keeps it clear of the table's scrolling viewport.
export function createRowActionsMenu({ doc = document, name, actions = [], status }) {
  const element = doc.createElement("span");
  element.className = "row-actions";
  const button = rowButton(doc, "Options", `Options for ${name}`, () => {
    if (menu.hidden) open(); else close(true);
  });
  const menu = doc.createElement("div");
  menu.id = `row-actions-${++nextMenu}`;
  menu.className = "row-actions-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("popover", "manual");
  menu.hidden = true;
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-controls", menu.id);
  button.setAttribute("aria-expanded", "false");
  element.append(button, menu);
  let choices = actions;
  let typed = "", typedAt = 0;
  let spaceDown = false, nextReason = 0;
  const reasons = new Map();

  function position() {
    const anchor = button.getBoundingClientRect();
    const bounds = menu.getBoundingClientRect();
    const view = doc.defaultView;
    menu.style.left = `${Math.max(4, Math.min(anchor.left, view.innerWidth - bounds.width - 4))}px`;
    const below = anchor.bottom + bounds.height <= view.innerHeight;
    menu.style.top = `${Math.max(4, below ? anchor.bottom : anchor.top - bounds.height)}px`;
  }
  function outside(event) {
    if (!element.contains(event.target)) close();
  }
  function open(last = false) {
    if (!choices.length) return;
    menu.hidden = false;
    if (menu.showPopover && !menu.matches(":popover-open")) menu.showPopover();
    button.setAttribute("aria-expanded", "true");
    position();
    typed = "";
    doc.addEventListener("pointerdown", outside, true);
    doc.addEventListener("click", outside, true);
    doc.defaultView.addEventListener("resize", position);
    doc.addEventListener("scroll", position, true);
    menu.children[last ? menu.children.length - 1 : 0]?.focus();
  }
  function close(restore = false) {
    if (restore) button.focus();
    if (menu.hidePopover && menu.matches(":popover-open")) menu.hidePopover();
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    doc.removeEventListener("pointerdown", outside, true);
    doc.removeEventListener("click", outside, true);
    doc.defaultView.removeEventListener("resize", position);
    doc.removeEventListener("scroll", position, true);
  }
  function activate(id) {
    const action = choices.find((item) => item.id === id);
    if (!action) return;
    if (action.disabledReason) {
      announceStatus(status, action.disabledReason, { force: true });
      return;
    }
    close(true);
    action.run(button);
  }
  function update(rowName, items) {
    choices = items;
    button.setAttribute("aria-label", `Options for ${rowName}`);
    menu.setAttribute("aria-label", `Options for ${rowName}`);
    const existing = new Map([...menu.children].map((item) => [item.dataset.action, item]));
    const focused = doc.activeElement;
    const hadFocus = menu.contains(focused);
    const rows = items.map((action) => {
      let item = existing.get(action.id);
      if (!item) {
        item = rowButton(doc, action.label, action.label, () => activate(action.id));
        item.dataset.action = action.id;
        item.setAttribute("role", "menuitem");
        item.tabIndex = -1;
      }
      if (item.textContent !== action.label) item.textContent = action.label;
      item.setAttribute("aria-label", action.label);
      item.setAttribute("aria-disabled", String(Boolean(action.disabledReason)));
      if (action.disabledReason) {
        let reason = reasons.get(action.id);
        if (!reason) {
          reason = doc.createElement("span");
          reason.id = `${menu.id}-reason-${++nextReason}`;
          reason.hidden = true;
          element.append(reason);
          reasons.set(action.id, reason);
        }
        reason.textContent = action.disabledReason;
        item.setAttribute("aria-describedby", reason.id);
      } else {
        item.removeAttribute("aria-describedby");
      }
      return item;
    });
    for (const [id, reason] of reasons) {
      if (!items.some((action) => action.id === id && action.disabledReason)) {
        reason.remove();
        reasons.delete(id);
      }
    }
    placeRows(menu, rows);
    if (hadFocus && !focused.isConnected) {
      if (rows.length) rows[0].focus(); else close(true);
    }
  }
  button.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === " ") spaceDown = true;
      else if (!event.repeat) open(event.key === "ArrowUp");
    }
  });
  button.addEventListener("keyup", (event) => {
    if (event.key !== " ") return;
    event.preventDefault();
    event.stopPropagation();
    // Cancel the native release click, including when a menu action has just
    // returned focus here. Only a press begun on this button opens the menu.
    const shouldOpen = spaceDown;
    spaceDown = false;
    if (shouldOpen) open();
  });
  button.addEventListener("focusout", () => { spaceDown = false; });
  menu.addEventListener("keyup", (event) => {
    if (event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
    }
  });
  menu.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Tab") { close(true); return; }
    if (event.key === "Escape") { event.preventDefault(); close(true); return; }
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const items = [...menu.children];
    const index = items.indexOf(doc.activeElement);
    let next = null;
    if (event.key === "ArrowDown") next = (index + 1) % items.length;
    else if (event.key === "ArrowUp") next = (index + items.length - 1) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else if (["Enter", " "].includes(event.key)) {
      event.preventDefault();
      if (!event.repeat) activate(items[index]?.dataset.action);
      return;
    } else if (event.key.length === 1) {
      const now = Date.now();
      typed = now - typedAt > 700 ? event.key.toLowerCase() : typed + event.key.toLowerCase();
      typedAt = now;
      const prefix = [...typed].every((letter) => letter === typed[0]) ? typed[0] : typed;
      next = items.map((_, offset) => (index + offset + 1) % items.length)
        .find((at) => items[at].textContent.toLowerCase().startsWith(prefix));
    }
    if (next !== null && next !== undefined) { event.preventDefault(); items[next]?.focus(); }
  });
  element.addEventListener("focusout", (event) => {
    if (!element.contains(event.relatedTarget)) close();
  });
  update(name, actions);
  return { element, button, update, close };
}

export function rowActionsCell(doc, menu) {
  const cell = tableRow(doc, [menu.element], { nameIndex: -1 }).firstElementChild;
  cell.dataset.action = "options";
  return cell;
}
