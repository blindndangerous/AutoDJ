import { reconcileRows, rowButton } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { createRowActionsMenu, rowActionsCell } from "./row-actions-menu.js";

// Collections share row identity, cell navigation, popup actions, empty
// state and focus recovery. Panels supply data and action descriptors only.
export function createCollectionTable({ host, label, columns, keyOf, nameOf,
  primary, actions, fallback, status, emptyText = "No items.", nameIndex = 0 }) {
  const doc = host.ownerDocument;
  const scroll = doc.createElement("div");
  scroll.className = "data-table-scroll";
  const table = doc.createElement("table");
  table.className = "collection-table";
  table.setAttribute("role", "grid");
  table.setAttribute("aria-label", label);
  const head = table.createTHead().insertRow();
  for (const title of [...columns.map((column) => column.label), ...(primary ? ["Actions"] : []), ...(actions ? ["Options"] : [])]) {
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

  function createRow() {
    const row = tableRow(doc, columns.map(() => ""), { nameIndex });
    const record = {};
    columns.forEach((column, index) => { row.cells[index].dataset.action = column.key; });
    if (primary) {
      const button = rowButton(doc, "", "", () => {
        if (record.primary.disabledReason) {
          announceStatus(status, record.primary.disabledReason, { force: true });
        } else record.primary.run(button);
      });
      record.button = button;
      const cell = tableRow(doc, [button], { nameIndex: -1 }).firstElementChild;
      row.append(cell);
    }
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
      if (cell.textContent !== value) cell.textContent = value;
      // A label supplied for grid movement must not retain old values.
      if (cell.dataset.headingLabel !== undefined) {
        cell.setAttribute("aria-label", `${value}, ${column.label}, ${name}`);
      }
    });
    if (primary) {
      record.primary = primary(item, index);
      const action = record.primary;
      const button = record.button;
      if (button.textContent !== action.label) button.textContent = action.label;
      button.dataset.action = action.id;
      button.setAttribute("aria-label", `${action.label} ${name}`);
      button.setAttribute("aria-disabled", String(Boolean(action.disabledReason)));
      button.parentElement.dataset.action = action.id;
    }
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
        const control = target?.querySelector?.("button");
        const name = control?.getAttribute("aria-label") || target?.getAttribute("aria-label") || target?.textContent.trim() || label;
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
  function target(key, action = "primary") {
    const row = [...body.children].find((item) => item.dataset.rowKey === String(key));
    if (!row) return null;
    if (action === "primary") return row.cells[primary ? columns.length : nameIndex];
    return [...row.cells].find((cell) => cell.dataset.action === action) || null;
  }
  function clear() {
    const removed = removedMenus([]);
    reconcileRows(body, [], keyOf, createRow, updateRow, { fallback, rowSelector: "tr", controlSelector: ":scope > td" });
    for (const menu of removed) menu.close();
    navigation.refresh();
  }
  return { table, body, update, target, clear };
}
