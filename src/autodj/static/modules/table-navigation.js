// One tab stop per native table grid; arrows move between its cells.
// A cell with a button takes focus itself, never the button: moving focus
// on to the button made NVDA read the cell and then the button.  Enter or
// Space on the cell presses the button, which is for the mouse and browse
// mode.  A checkbox takes focus itself, so its state is read.
// NVDA says the column heading itself in focus mode, after the cell, with
// Firefox and Chrome alike, so a cell's own name never repeats it.  Until
// focus leaves, a cell moved to on another row is named with its text and
// the row's name cell, such as its song.  A cell that holds a button is
// described as a button and named by its action and the row's song:
// "Remove folder, <path> button Actions".  When the column heading already
// names the action, as Play now does in Search, the cell is named by the
// song alone, so NVDA says "<song> button Play now" and not the action
// twice.  A checkbox's cell, read before the focused checkbox, is named by
// the row's song.
export function installTableNavigation(body) {
  if (!body) return { refresh() {} };
  const cells = () => [...body.querySelectorAll("tr > td[role=gridcell], tr > td[role=rowheader]")];
  const controls = (cell) => cell ? [...cell.querySelectorAll("button, input, select")] : [];
  const input = (cell) => controls(cell).find((item) => ["INPUT", "SELECT"].includes(item.tagName) && !item.disabled);
  const stop = (cell) => input(cell) || cell;
  const text = (cell) => cell?.textContent.replace(/\s+/g, " ").trim();
  const button = (cell) => controls(cell).find((item) => item.tagName === "BUTTON" && !item.disabled);
  const rowName = (cell) => !cell.hasAttribute("data-name") && text(cell.parentElement.querySelector("[data-name]"));
  const columnHeading = (cell) => {
    const row = [...cell.parentElement.children];
    const place = row.slice(0, row.indexOf(cell)).reduce((sum, item) => sum + item.colSpan, 0);
    return text(cell.closest("table")?.querySelector("thead tr")?.children[place]) || "";
  };
  // An action cell's name: its action, unless the column heading says it,
  // then the row's song.  No song leaves the button's own name.
  const actionName = (cell, control, song) => {
    if (!song) return null;
    const action = text(control);
    const heading = columnHeading(cell);
    return action && action.toLowerCase() !== heading.toLowerCase() ? `${action}, ${song}` : song;
  };
  const label = (cell, name, role = null) => {
    if (name) cell.setAttribute("aria-label", name);
    if (role) cell.setAttribute("aria-roledescription", role);
    cell.dataset.headingLabel = "";
  };
  const unlabel = (cell) => {
    if (cell?.dataset.headingLabel === undefined) return;
    cell.removeAttribute("aria-label");
    cell.removeAttribute("aria-roledescription");
    delete cell.dataset.headingLabel;
  };
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
    const song = rowName(cell);
    if (target === cell && button(cell)) label(cell, actionName(cell, button(cell), song), "button");
    else if (target !== cell && song) label(cell, song);
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
    const song = target.parentElement !== row && rowName(target);
    if (song && !controls(target).length) label(target, `${text(target)}, ${song}`);
    stop(target).focus();
  });
  body.addEventListener("focusout", (event) => {
    unlabel(event.target.closest?.("td"));
  });
  refresh();
  return { refresh };
}
