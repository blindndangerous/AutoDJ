// Build a native table row with roving grid cells and existing DOM controls.
export function tableRow(doc, cells, { colspan = [], rowHeaderIndex = 0 } = {}) {
  const row = doc.createElement("tr");
  cells.forEach((content, index) => {
    const cell = doc.createElement("td");
    cell.setAttribute("role", index === rowHeaderIndex ? "rowheader" : "gridcell");
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
