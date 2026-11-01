import { placeRows, rowButton } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { tableFocusTarget, tableRow } from "./table-row.js";

let nextMenu = 0;
// One row menu is open at a time on a page, so only one can await its focus.
let openMenu = null;

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
  let origin = null;  // the cell a context menu opened from, if any
  let pending = null;  // the item a just-shown menu is about to focus
  let ownLabel = null;  // the name of the cell the shown menu sits in
  const reasons = new Map();
  const inside = (node) => Boolean(node) && (element.contains(node) || menu.contains(node));

  // Beside the cell the menu opened from, or its Options button.
  function position() {
    const anchor = (origin?.closest?.("td") || origin || button).getBoundingClientRect();
    const bounds = menu.getBoundingClientRect();
    const view = doc.defaultView;
    menu.style.left = `${Math.max(4, Math.min(anchor.left, view.innerWidth - bounds.width - 4))}px`;
    const below = anchor.bottom + bounds.height <= view.innerHeight;
    menu.style.top = `${Math.max(4, below ? anchor.bottom : anchor.top - bounds.height)}px`;
  }
  function outside(event) {
    if (!inside(event.target)) close();
  }
  // Focus *item*, unless focus already reached the menu some other way.
  function focusItem(item) {
    pending = null;
    doc.removeEventListener("keydown", early, true);
    if (menu.contains(doc.activeElement)) return;
    const cell = origin?.closest?.("td");
    item?.focus();
    // Inside the origin cell, the menu must not become part of its name.
    if (cell?.contains(menu) && !cell.hasAttribute("aria-label")) {
      ownLabel = cell;
      cell.dataset.menuLabel = "";
      cell.setAttribute("aria-label", [...cell.childNodes].filter((node) => node !== menu)
        .map((node) => node.textContent).join("").replace(/\s+/g, " ").trim());
    }
  }
  // The deferred focus: whatever item is first now, if focus has not moved on.
  function settle() {
    if (!pending) return;
    if (!menu.isConnected) { close(); return; }
    const active = doc.activeElement;
    const back = returnTarget();
    if (!active || active === doc.body || active === back || menu.contains(active)) focusItem(pending);
    else close();
  }
  // A key pressed before the deferred focus goes to the menu, as if focused.
  function early(event) {
    if (!pending) return;
    // A menu removed with its page content gives up its claim on keys.
    if (!menu.isConnected) { close(); return; }
    const routed = !menu.contains(event.target);
    focusItem(pending);
    if (!routed) return;
    event.stopPropagation();
    menuKey(event);
  }
  // *from* is where focus returns on close; the button's cell by default.
  // A menu opened from another cell is placed in that cell, so focus moves
  // into the menu without passing through the Actions cell's ancestry.
  function open(last = false, from = null) {
    if (!choices.length) return false;
    if (openMenu && openMenu !== close) openMenu();
    openMenu = close;
    origin = from;
    const cell = from?.closest?.("td");
    if (cell && !cell.contains(button)) cell.append(menu);
    // Its own Actions cell is named by the button alone while the shown menu
    // is inside it: NVDA read the cell as the button and the menu's names.
    if (ownLabel && !ownLabel.contains(menu)) releaseLabel();
    const home = button.closest("td");
    if (home?.contains(menu) && !home.hasAttribute("aria-label")) {
      ownLabel = home;
      home.dataset.menuLabel = "";
      home.setAttribute("aria-label", button.getAttribute("aria-label"));
    }
    menu.hidden = false;
    if (menu.showPopover && !menu.matches(":popover-open")) menu.showPopover();
    button.setAttribute("aria-expanded", "true");
    position();
    typed = "";
    doc.addEventListener("pointerdown", outside, true);
    doc.addEventListener("click", outside, true);
    doc.defaultView.addEventListener("resize", position);
    doc.addEventListener("scroll", position, true);
    // Firefox reports a shown role=menu at its next rendering update. An item
    // focused before that made NVDA move on to the menu itself, so the first
    // Escape only left focus mode (#712). Focus it a frame and a task later.
    pending = menu.children[last ? menu.children.length - 1 : 0] || null;
    doc.addEventListener("keydown", early, true);
    const view = doc.defaultView;
    view.requestAnimationFrame(() => view.setTimeout(settle));
    return true;
  }
  function returnTarget() {
    return origin?.isConnected ? origin : button.isConnected ? tableFocusTarget(button) : null;
  }
  function releaseLabel() {
    if (!ownLabel) return;
    ownLabel.removeAttribute("aria-label");
    delete ownLabel.dataset.menuLabel;
    ownLabel = null;
  }
  function close(restore = false) {
    const back = returnTarget();
    if (openMenu === close) openMenu = null;
    origin = null;
    pending = null;
    doc.removeEventListener("keydown", early, true);
    // Hidden, and its cell's name back to its content, before focus returns,
    // so the cell is read once with its own words.
    if (menu.hidePopover && menu.matches(":popover-open")) menu.hidePopover();
    menu.hidden = true;
    releaseLabel();
    if (restore) back?.focus();
    // Back to its Actions cell only once focus has left it: Chromium drops
    // focus to the page when a focused element is moved.
    if (menu.parentElement !== element) button.after(menu);
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
    // An action that moves focus, such as opening an editor, takes it
    // straight from the menu; otherwise focus returns where the menu opened.
    const back = returnTarget();
    // A pointer can activate an item before the menu takes focus (or where
    // clicks do not focus buttons); then focus returns first, as an opener.
    close(!inside(doc.activeElement));
    try { action.run(button); }
    finally {
      const active = doc.activeElement;
      if (!active || active === doc.body || inside(active)) back?.focus();
    }
  }
  function update(rowName, items) {
    choices = items;
    button.setAttribute("aria-label", `Options for ${rowName}`);
    menu.setAttribute("aria-label", `Options for ${rowName}`);
    if (ownLabel?.contains(button)) ownLabel.setAttribute("aria-label", `Options for ${rowName}`);
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
    if (pending && !pending.isConnected) pending = rows[0] || null;
    if (!rows.length && !menu.hidden) close(hadFocus);
    else if (hadFocus && !focused.isConnected) rows[0].focus();
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
  menu.addEventListener("keydown", menuKey);
  function menuKey(event) {
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
  }
  // The menu can sit in another cell, so it has its own focusout too.
  for (const owner of [element, menu]) {
    owner.addEventListener("focusout", (event) => {
      if (!inside(event.relatedTarget)) close();
    });
  }
  update(name, actions);
  return { element, button, update, close, open: (from) => open(false, from) };
}

export function rowActionsCell(doc, menu) {
  const cell = tableRow(doc, [menu.element], { nameIndex: -1 }).firstElementChild;
  cell.dataset.action = "options";
  return cell;
}
