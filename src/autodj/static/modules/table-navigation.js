// One tab stop per native table grid; arrows move between its cells.
// A cell with a button takes focus itself, never the button: moving focus
// on to the button made NVDA read the cell and then the button.  Enter or
// Space on the cell presses the button, which is for the mouse and browse
// mode.  A checkbox takes focus itself, so its state is read.
// NVDA says no table headings in focus mode, so until focus leaves, a
// moved-to cell's name is its text, then the column heading (in another
// column) and the row's name cell, such as its song (on another row).
// A button or checkbox already names itself, so its cell gets nothing.
export function installTableNavigation(body) {
  if (!body) return { refresh() {} };
  const cells = () => [...body.querySelectorAll("tr > td[role=gridcell], tr > td[role=rowheader]")];
  const controls = (cell) => cell ? [...cell.querySelectorAll("button, input, select")] : [];
  const input = (cell) => controls(cell).find((item) => ["INPUT", "SELECT"].includes(item.tagName) && !item.disabled);
  const stop = (cell) => input(cell) || cell;
  const place = (cell) => {
    const row = [...cell.parentElement.children];
    return row.slice(0, row.indexOf(cell)).reduce((sum, item) => sum + item.colSpan, 0);
  };
  const text = (cell) => cell?.textContent.replace(/\s+/g, " ").trim();
  const heading = (cell) => text(cell.closest("table")?.querySelector("thead tr")?.children[place(cell)]);
  function setRovingFocus(active) {
    for (const cell of cells()) {
      cell.tabIndex = active === cell ? 0 : -1;
      for (const item of controls(cell)) item.tabIndex = item === active ? 0 : -1;
    }
  }
  function refresh() {
    const items = cells();
    const focused = body.ownerDocument.activeElement;
    const activeCell = focused?.closest?.("td[role=gridcell], td[role=rowheader]");
    setRovingFocus(stop(activeCell && body.contains(activeCell) ? activeCell : items[0]));
  }
  body.addEventListener("focusin", (event) => {
    const cell = event.target.closest?.("td");
    if (!cell || !body.contains(cell)) return;
    // Firefox can focus a tall cell while leaving its bottom below the viewport.
    cell.scrollIntoView({ block: "nearest", inline: "nearest" });
    const target = stop(cell);
    // Focus that lands on a cell's button, such as Track info handing it
    // back, moves to the cell.
    if (event.target !== target) { target.focus(); return; }
    setRovingFocus(target);
  });
  body.addEventListener("keydown", (event) => {
    if (event.target.matches("select")) return;
    const rowEdge = event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey
      && (event.key === "Home" || event.key === "End");
    if ((event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) && !rowEdge) return;
    const cell = event.target.closest?.("td[role=gridcell], td[role=rowheader]");
    if (!cell || !body.contains(cell)) return;
    if (event.key === "Enter" || event.key === " ") {
      if (event.target !== cell && controls(cell).includes(event.target)) return;
      if (event.repeat) { event.preventDefault(); return; }
      const control = controls(cell).find((item) => !item.disabled);
      if (control) { event.preventDefault(); control.click(); return; }
      // Enter on a track's name opens its Track info.
      const info = event.key === "Enter" && cell.hasAttribute("data-name")
        && [...cell.closest("tr").querySelectorAll("button")]
          .find((item) => item.textContent.trim() === "Track info" && !item.disabled);
      if (info) { event.preventDefault(); info.click(); }
      return;
    }
    const row = cell.closest("tr");
    const rows = [...body.querySelectorAll("tr")]
      .filter((item) => item.querySelector("td[role=gridcell], td[role=rowheader]"));
    const rowIndex = rows.indexOf(row);
    const column = [...row.querySelectorAll("td[role=gridcell], td[role=rowheader]")].indexOf(cell);
    let target = null;
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      const next = rowIndex + (event.key === "ArrowUp" ? -1 : 1);
      target = rows[next]?.querySelectorAll("td[role=gridcell], td[role=rowheader]")[column];
    } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      target = row.querySelectorAll("td[role=gridcell], td[role=rowheader]")[column + (event.key === "ArrowLeft" ? -1 : 1)];
    } else if (event.key === "Home" || event.key === "End") {
      const destination = rowEdge ? rows[event.key === "Home" ? 0 : rows.length - 1] : row;
      const rowCells = destination.querySelectorAll("td[role=gridcell], td[role=rowheader]");
      target = rowCells[rowEdge ? Math.min(column, rowCells.length - 1) : event.key === "Home" ? 0 : rowCells.length - 1];
    }
    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) event.preventDefault();
    if (!target) return;
    const song = target.parentElement !== row && !target.hasAttribute("data-name")
      && text(target.parentElement.querySelector("[data-name]"));
    const header = place(target) !== place(cell) && heading(target);
    if ((song || header) && !controls(target).length) {
      target.setAttribute("aria-label", [text(target), header, song].filter(Boolean).join(", "));
      target.dataset.headingLabel = "";
    }
    stop(target).focus();
  });
  body.addEventListener("focusout", (event) => {
    if (event.target.dataset?.headingLabel === undefined) return;
    event.target.removeAttribute("aria-label");
    delete event.target.dataset.headingLabel;
  });
  refresh();
  return { refresh };
}
