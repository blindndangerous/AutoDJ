import { withDisabled } from "./api-client.js";
import { reconcileRows } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { tableFocusTarget, tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { createRowActionsMenu, rowActionsCell } from "./row-actions-menu.js";

const hosts = new WeakMap();
const pendingToggles = new WeakSet();
let nextReason = 0;

// Consumers identify the owning collection without depending on its wrappers.
export function collectionHost(element) {
  const table = element?.closest?.(".collection-table");
  return table ? hosts.get(table) || null : null;
}

// Collections share row identity, cell navigation, popup actions, empty
// state and focus recovery. Panels supply data and action descriptors only.
// A row's actions, most used first, share one last Actions column: its
// Options menu (#712). Shift+F10, the Applications key and right-click open
// the same menu; tables without actions keep the browser's own menu.
// A native caption names the table (WCAG H39); *hideCaption* hides it from
// view only where a heading just above already says the same.
// A column with *toggle* holds a native checkbox that turns its row on or
// off in place: { checked(item), label(item), run(item, checked, control),
// available(item), note(item), disabledReason(item) }. Where available(item)
// is false the cell shows its value instead; note(item) is visible text
// beside the checkbox that describes it. Once *run* settles the checkbox
// shows checked(item) for the row's current data.
export function createCollectionTable({ host, label, columns, keyOf, nameOf,
  actions, fallback, status, emptyText = "No items.", nameIndex = 0, hideCaption = false }) {
  const doc = host.ownerDocument;
  const scroll = doc.createElement("div");
  scroll.className = "data-table-scroll";
  const table = doc.createElement("table");
  table.className = "collection-table";
  hosts.set(table, host);
  table.setAttribute("role", "grid");
  const caption = table.createCaption();
  caption.textContent = label;
  if (hideCaption) caption.className = "visually-hidden";
  const head = table.createTHead().insertRow();
  for (const title of [...columns.map((column) => column.label), ...(actions ? ["Actions"] : [])]) {
    const cell = doc.createElement("th");
    cell.scope = "col";
    cell.textContent = title;
    head.append(cell);
  }
  const body = table.createTBody();
  scroll.append(table);
  host.replaceChildren(scroll);
  const navigation = installTableNavigation(body);
  const records = new WeakMap();
  // Browsers fire contextmenu for Shift+F10, the Applications key and
  // right-click alike, on the focused or the clicked element.
  body.addEventListener("contextmenu", (event) => {
    if (event.target.closest?.('[role="menu"]')) return;
    const menu = records.get(event.target.closest?.("tr"))?.menu;
    if (menu?.open(tableFocusTarget(event.target))) event.preventDefault();
  });

  function createRow() {
    const row = tableRow(doc, columns.map(() => ""), { nameIndex });
    const record = {};
    columns.forEach((column, index) => { row.cells[index].dataset.action = column.key; });
    if (actions) {
      record.menu = createRowActionsMenu({ doc, name: "", actions: [], status });
      row.append(rowActionsCell(doc, record.menu));
    }
    records.set(row, record);
    return row;
  }
  function updateRow(row, item, index) {
    const record = records.get(row);
    const name = nameOf(item, index);
    row.dataset.rowName = name;
    record.item = item;
    columns.forEach((column, at) => {
      const value = String(column.value(item, index) ?? "");
      const cell = row.cells[at];
      if (column.toggle && updateToggle(cell, column.toggle, record, item)) return;
      // A context menu opened from this cell sits after its text; keep it.
      const menu = cell.querySelector(":scope > .row-actions-menu");
      if (!menu) {
        if (cell.textContent !== value) cell.textContent = value;
      } else if (cell.firstChild?.nodeType === 3) {
        if (cell.firstChild.data !== value) cell.firstChild.data = value;
      } else cell.prepend(value);
      if (menu && "menuLabel" in cell.dataset && cell.getAttribute("aria-label") !== value) cell.setAttribute("aria-label", value);
    });
    record.menu?.update(name, actions(item, index));
  }
  // The checkbox is kept while its row can be changed, so focus stays on it.
  // A row menu opened in this cell stays put as the checkbox comes and goes.
  function updateToggle(cell, toggle, record, item) {
    record.toggles ||= new Map();
    let control = record.toggles.get(cell);
    const own = () => [...cell.childNodes].filter((node) => !node.matches?.(".row-actions-menu"));
    if (toggle.available && !toggle.available(item)) {
      if (control) {
        const focused = doc.activeElement === control;
        record.toggles.delete(cell);
        for (const node of own()) node.remove();
        if (focused) cell.focus();
      }
      return false;
    }
    if (!control) {
      control = doc.createElement("input");
      control.type = "checkbox";
      control.tabIndex = -1;
      control.addEventListener("click", (event) => toggled(event, control, toggle, record));
      for (const node of own()) node.remove();
      cell.prepend(control);
      record.toggles.set(cell, control);
    }
    if (!pendingToggles.has(control)) control.checked = Boolean(toggle.checked(item));
    control.setAttribute("aria-label", toggle.label(item));
    // A visible note beside the checkbox, such as an ended event's, and, like
    // a menu item, an unavailable one's hidden reason describe it.
    const described = [
      describe(cell, control, "toggle-note", toggle.note?.(item) || "", false),
      describe(cell, control, "toggle-reason", toggle.disabledReason?.(item) || "", true),
    ].filter(Boolean);
    if (described.length) control.setAttribute("aria-describedby", described.join(" "));
    else control.removeAttribute("aria-describedby");
    if (cell.querySelector(":scope > .toggle-reason")) control.setAttribute("aria-disabled", "true");
    else control.removeAttribute("aria-disabled");
    return true;
  }
  // The id of *cell*'s *kind* text after *control*, made or removed as *text* needs.
  function describe(cell, control, kind, text, hidden) {
    let node = cell.querySelector(`:scope > .${kind}`);
    if (!text) { node?.remove(); return ""; }
    if (!node) {
      node = doc.createElement("span");
      node.className = kind;
      node.id = `collection-${kind}-${++nextReason}`;
      node.hidden = hidden;
      (kind === "toggle-note" ? control : cell.querySelector(":scope > .toggle-note") || control).after(node);
    }
    if (node.textContent !== text) node.textContent = text;
    return node.id;
  }
  // One change at a time per checkbox: a press while it saves does nothing.
  // The checkbox stays enabled and says it is busy (withDisabled keepEnabled),
  // so NVDA never calls it unavailable, focused or not. Once settled it shows
  // the row's current state: a reload's, or the old one after a failure.
  async function toggled(event, control, toggle, record) {
    if (pendingToggles.has(control)) { event.preventDefault(); return; }
    if (control.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      announceStatus(status, control.parentElement.querySelector(":scope > .toggle-reason").textContent, { force: true });
      return;
    }
    const checked = control.checked;
    pendingToggles.add(control);
    try {
      await withDisabled(control, () => toggle.run(record.item, checked, control), { keepEnabled: true });
    } catch {
      // The panel says what failed; the state below is the server's.
    } finally {
      pendingToggles.delete(control);
    }
    if (control.isConnected) control.checked = Boolean(toggle.checked(record.item));
  }
  function removedMenus(items) {
    const keys = new Set(items.map((item) => String(keyOf(item))));
    return [...body.children].filter((row) => !keys.has(row.dataset.rowKey))
      .map((row) => records.get(row)?.menu).filter(Boolean);
  }
  function update(items, { announceFocus = true } = {}) {
    if (!items.length && body.querySelector(".no-results")) {
      navigation.refresh();
      return { rows: [], focusLost: false, target: null };
    }
    const removed = removedMenus(items);
    const result = reconcileRows(body, items, keyOf, createRow, updateRow, {
      fallback, rowSelector: "tr", controlSelector: ":scope > td", focusSelector: ":scope > td",
      focusControl: (active) => active?.closest?.("td"),
      onFocusLost: ({ target }) => {
        if (!announceFocus) return;
        // Not a menu item of a menu open in that cell.
        const control = [...(target?.querySelectorAll?.("button") || [])].find((item) => !item.closest('[role="menu"]'));
        const fieldLabel = [...(target?.labels || [])].map((item) => item.textContent.trim()).join(" ");
        const name = control?.getAttribute("aria-label") || target?.getAttribute("aria-label") || fieldLabel || target?.textContent.trim() || label;
        announceStatus(status, `The ${label} list changed. Focus moved to ${name}.`, { force: true });
      },
    });
    for (const menu of removed) menu.close();
    if (!items.length) {
      const row = tableRow(doc, [emptyText], { colspan: [head.cells.length], nameIndex: -1 });
      row.cells[0].className = "no-results";
      body.append(row);
    }
    navigation.refresh();
    return result;
  }
  // A data column's cell by its key; any action belongs to the Actions cell.
  function target(key, column) {
    const row = [...body.children].find((item) => item.dataset.rowKey === String(key));
    if (!row) return null;
    return [...row.cells].find((cell) => cell.dataset.action === column)
      || row.cells[actions ? columns.length : nameIndex];
  }
  function clear() {
    const removed = removedMenus([]);
    reconcileRows(body, [], keyOf, createRow, updateRow, { fallback, rowSelector: "tr", controlSelector: ":scope > td" });
    for (const menu of removed) menu.close();
    navigation.refresh();
  }
  update([], { announceFocus: false });
  return { table, body, update, target, clear };
}
