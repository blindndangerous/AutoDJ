import { reconcileRows } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { tableFocusTarget, tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { createRowActionsMenu, rowActionsCell } from "./row-actions-menu.js";

const hosts = new WeakMap();

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
export function createCollectionTable({ host, label, columns, keyOf, nameOf,
  actions, fallback, status, emptyText = "No items.", nameIndex = 0 }) {
  const doc = host.ownerDocument;
  const scroll = doc.createElement("div");
  scroll.className = "data-table-scroll";
  const table = doc.createElement("table");
  table.className = "collection-table";
  hosts.set(table, host);
  table.setAttribute("role", "grid");
  table.setAttribute("aria-label", label);
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
    columns.forEach((column, at) => {
      const value = String(column.value(item, index) ?? "");
      const cell = row.cells[at];
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
