// Build a native table row with roving grid cells and existing DOM controls.
// Every cell is a plain grid cell: a row header made NVDA say "row header"
// each time focus landed on the name.  The name cell (*nameIndex*) is
// marked data-name, so Enter on it opens the row's Track info.
// Native column headers supply context. Navigation adds row context only
// when crossing into another row's plain data cell.
export function tableRow(doc, cells, { colspan = [], nameIndex = 0 } = {}) {
  const row = doc.createElement("tr");
  cells.forEach((content, index) => {
    const cell = doc.createElement("td");
    cell.setAttribute("role", "gridcell");
    if (index === nameIndex) cell.dataset.name = "";
    cell.tabIndex = -1;
    if (colspan[index]) cell.colSpan = colspan[index];
    if (Array.isArray(content)) cell.append(...content);
    else if (content?.nodeType) cell.append(content);
    else cell.textContent = String(content ?? "");
    for (const control of cell.querySelectorAll("button, input")) control.tabIndex = -1;
    row.append(cell);
  });
  return row;
}

// Return to the table's focus model after a menu or dialog. Native inputs
// keep their own focus; action buttons belong to their containing cell.
export function tableFocusTarget(element) {
  const cell = element?.closest?.('td[role="gridcell"], td[role="rowheader"]');
  if (!cell || element.disabled) return element;
  if (element.matches("input, select") && !element.closest('[role="menu"]')) return element;
  return cell;
}
