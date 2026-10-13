// One tab stop per native table grid; arrows move between its cells.
export function installTableNavigation(body) {
  if (!body) return { refresh() {} };
  const cells = () => [...body.querySelectorAll("tr > td[role=gridcell], tr > td[role=rowheader]")];
  const controls = (cell) => cell ? [...cell.querySelectorAll("button, input")] : [];
  const cellControl = (cell) => {
    const items = controls(cell);
    return items.length === 1 && !items[0].disabled ? items[0] : null;
  };
  function setRovingFocus(active) {
    for (const cell of cells()) {
      const control = cellControl(cell);
      cell.tabIndex = active === cell && !control ? 0 : -1;
      for (const item of controls(cell)) item.tabIndex = item === active ? 0 : -1;
    }
  }
  function refresh() {
    const items = cells();
    const focused = body.ownerDocument.activeElement;
    const activeCell = focused?.closest?.("td[role=gridcell], td[role=rowheader]");
    const cell = activeCell && body.contains(activeCell) ? activeCell : items[0];
    const active = cellControl(cell) || cell;
    setRovingFocus(active);
  }
  body.addEventListener("focusin", (event) => {
    const cell = event.target.closest?.("td");
    if (!cell || !body.contains(cell)) return;
    const control = cellControl(cell);
    if (event.target === cell && control) { control.focus(); return; }
    setRovingFocus(control === event.target ? event.target : cell);
  });
  body.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const cell = event.target.closest?.("td[role=gridcell], td[role=rowheader]");
    if (!cell || !body.contains(cell)) return;
    if (event.key === "Enter" || event.key === " ") {
      if (event.target !== cell && controls(cell).includes(event.target)) return;
      if (event.repeat) { event.preventDefault(); return; }
      const control = controls(cell).find((item) => !item.disabled);
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
    if (target) (cellControl(target) || target).focus();
  });
  refresh();
  return { refresh };
}
