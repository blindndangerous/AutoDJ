// One tab stop per native table grid; arrows move between its cells.
// A cell with a button takes focus itself, never the button: moving focus
// on to the button made NVDA read the cell and then the button.  Enter or
// Space on the cell presses the button, which is for the mouse and browse
// mode.  A checkbox takes focus itself, so its state is read.
// NVDA reports native column headers in focus mode. It does not report the
// row name because these tables deliberately avoid row headers. Add only
// that name when crossing rows into a plain data cell, until focus leaves.
// Name cells and controls already identify themselves and get no extra label.
import { tableFocusTarget } from "./table-row.js";

export function installTableNavigation(body) {
  if (!body) return { refresh() {} };
  const cells = () => [...body.querySelectorAll("tr > td[role=gridcell], tr > td[role=rowheader]")];
  const controls = (cell) => cell ? [...cell.querySelectorAll("button, input, select")]
    .filter((item) => !item.closest('[role="menu"]')) : [];
  // Enter control-only cells on their native input. A mixed action/input
  // cell remains a cell, so returning from an action cannot select its sibling.
  const stop = (cell) => controls(cell).some((item) => item.tagName === "BUTTON") ? cell
    : controls(cell).find((item) => !item.disabled && item.matches("input, select")) || cell;
  const text = (cell) => cell?.textContent.replace(/\s+/g, " ").trim();
  let labelledCell = null;
  function clearRowLabel() {
    labelledCell?.removeAttribute("aria-label");
    labelledCell = null;
  }
  function labelRow(cell) {
    const name = !cell.hasAttribute("data-name") && !controls(cell).length
      && text(cell.parentElement.querySelector("[data-name]"));
    if (!name) { clearRowLabel(); return; }
    const label = `${text(cell)}, ${name}`;
    if (cell.getAttribute("aria-label") !== label) cell.setAttribute("aria-label", label);
    labelledCell = cell;
  }
  function setRovingFocus(active) {
    for (const cell of cells()) {
      cell.tabIndex = active === cell ? 0 : -1;
      for (const item of controls(cell)) item.tabIndex = item === active ? 0 : -1;
    }
  }
  function refresh() {
    const items = cells();
    const focused = body.ownerDocument.activeElement;
    if (labelledCell) {
      if (focused === labelledCell && body.contains(labelledCell)) labelRow(labelledCell);
      else clearRowLabel();
    }
    // A row menu owns its focus until it closes; refreshing data must not
    // pull its active item back into the grid.
    if (body.contains(focused) && focused.closest?.('[role="menu"]')) return;
    const activeCell = focused?.closest?.("td[role=gridcell], td[role=rowheader]");
    setRovingFocus(activeCell && body.contains(activeCell)
      ? (focused === activeCell ? stop(activeCell) : tableFocusTarget(focused)) : stop(items[0]));
  }
  let redirectingControlFocus = false;
  body.addEventListener("focusin", (event) => {
    if (event.target.closest?.('[role="menu"]')) return;
    const cell = event.target.closest?.("td");
    if (!cell || !body.contains(cell)) return;
    const target = event.target === cell ? stop(cell) : tableFocusTarget(event.target);
    // Focus that lands on a cell's button, such as Track info handing it
    // back, moves to the cell.
    if (event.target !== target) {
      // A pointer focuses the button before mouseup. Keep it stationary
      // while transferring focus, or the eventual click can miss it.
      const previous = redirectingControlFocus;
      redirectingControlFocus = target === cell;
      try { target.focus({ preventScroll: redirectingControlFocus }); }
      finally { redirectingControlFocus = previous; }
      return;
    }
    // Firefox can focus a tall cell while leaving its bottom below the viewport.
    // Direct cell navigation still scrolls it; native controls scroll only
    // themselves, and a redirected button has already been brought into view.
    if (!redirectingControlFocus) target.scrollIntoView({ block: "nearest", inline: "nearest" });
    setRovingFocus(target);
  });
  body.addEventListener("keydown", (event) => {
    if (event.target.closest?.('[role="menu"]')) return;
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
    // Clear the old temporary label before focusing: focusout must not
    // clear the destination's label when focus moves between named rows.
    clearRowLabel();
    if (target.parentElement !== row) labelRow(target);
    stop(target).focus();
  });
  body.addEventListener("focusout", (event) => {
    if (event.target === labelledCell) clearRowLabel();
  });
  refresh();
  return { refresh };
}
