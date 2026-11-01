// One tab stop per native table grid; arrows move between its cells.
// A cell with a button takes focus itself, never the button: moving focus
// on to the button made NVDA read the cell and then the button.  Enter or
// Space on the cell presses the button, which is for the mouse and browse
// mode.  A checkbox takes focus itself, so its state is read.
// NVDA says no table headings in focus mode, so until focus leaves, a
// moved-to cell's name is its text, then the column heading (in another
// column) and the row's name cell, such as its song (on another row) (#712).
// A button, input or select already names itself, so its cell gets nothing.
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
  // A cell's words, without those of a row menu opened inside it.
  const text = (cell) => {
    if (!cell) return cell;
    const words = (node) => [...node.childNodes].map((child) => child.nodeType === 3 ? child.data
      : child.nodeType === 1 && !child.matches('[role="menu"]') ? words(child) : "").join("");
    return words(cell).replace(/\s+/g, " ").trim();
  };
  const place = (cell) => {
    const row = [...cell.parentElement.children];
    return row.slice(0, row.indexOf(cell)).reduce((sum, item) => sum + item.colSpan, 0);
  };
  const heading = (cell) => text(cell.closest("table")?.querySelector("thead tr")?.children[place(cell)]);
  let labelled = null;
  function clearRowLabel() {
    labelled?.cell.removeAttribute("aria-label");
    labelled = null;
  }
  // *column* and *row* say what the move changed.
  function labelCell(cell, { column, row }) {
    const header = column && heading(cell);
    const name = row && !cell.hasAttribute("data-name")
      && text(cell.parentElement.querySelector("[data-name]"));
    if (controls(cell).length || (!header && !name)) { clearRowLabel(); return; }
    const label = [text(cell), header, name].filter(Boolean).join(", ");
    if (cell.getAttribute("aria-label") !== label) cell.setAttribute("aria-label", label);
    labelled = { cell, column, row };
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
    if (labelled) {
      if (focused === labelled.cell && body.contains(focused)) labelCell(focused, labelled);
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
      // Enter on a track's name opens its Track info, found by its action
      // id. A menu item's own activation announces a disabled reason.
      const info = event.key === "Enter" && cell.hasAttribute("data-name")
        && cell.closest("tr").querySelector('button[data-action="info"]');
      if (info && !info.disabled) { event.preventDefault(); info.click(); }
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
    labelCell(target, { column: place(target) !== place(cell), row: target.parentElement !== row });
    stop(target).focus();
  });
  body.addEventListener("focusout", (event) => {
    if (event.target === labelled?.cell) clearRowLabel();
  });
  refresh();
  return { refresh };
}
