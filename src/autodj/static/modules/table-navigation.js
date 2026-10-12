// One tab stop per native table grid; arrows move between its cells.
export function installTableNavigation(body) {
  if (!body) return { refresh() {} };
  const cells = () => [...body.querySelectorAll("tr > td[role=gridcell], tr > td[role=rowheader]")];
  const controls = (cell) => [...cell.querySelectorAll("button:not(:disabled), input:not(:disabled)")];
  function refresh() {
    const items = cells();
    const active = body.ownerDocument.activeElement?.closest?.("td");
    items.forEach((cell, index) => { cell.tabIndex = cell === active ? 0 : index === 0 && !body.contains(active) ? 0 : -1; });
    body.querySelectorAll("button, input").forEach((item) => { item.tabIndex = -1; });
  }
  body.addEventListener("focusin", (event) => {
    const cell = event.target.closest?.("td");
    if (!cell || !body.contains(cell)) return;
    for (const item of cells()) item.tabIndex = item === cell ? 0 : -1;
  });
  body.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const cell = event.target.closest?.("td[role=gridcell], td[role=rowheader]");
    if (!cell || !body.contains(cell)) return;
    if (event.key === "Enter" || event.key === " ") {
      if (event.repeat) { event.preventDefault(); return; }
      const control = controls(cell)[0];
      if (control) { event.preventDefault(); control.click(); }
      return;
    }
    const row = cell.closest("tr");
    const rows = [...body.querySelectorAll("tr")];
    const rowIndex = rows.indexOf(row);
    const column = [...row.querySelectorAll("td[role=gridcell], td[role=rowheader]")].indexOf(cell);
    let target = null;
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      const next = rowIndex + (event.key === "ArrowUp" ? -1 : 1);
      target = rows[next]?.querySelectorAll("td[role=gridcell], td[role=rowheader]")[column];
    } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      target = row.querySelectorAll("td[role=gridcell], td[role=rowheader]")[column + (event.key === "ArrowLeft" ? -1 : 1)];
    } else if (event.key === "Home" || event.key === "End") {
      const rowCells = row.querySelectorAll("td[role=gridcell], td[role=rowheader]");
      target = rowCells[event.key === "Home" ? 0 : rowCells.length - 1];
    }
    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) event.preventDefault();
    if (target) target.focus();
  });
  refresh();
  return { refresh };
}
