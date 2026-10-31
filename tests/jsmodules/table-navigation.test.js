import { describe, expect, it, vi } from "vitest";
import axe from "axe-core";
import { installTableNavigation } from "../../src/autodj/static/modules/table-navigation.js";
import { tableFocusTarget, tableRow } from "../../src/autodj/static/modules/table-row.js";

function accessibleName(element) {
  axe.setup(document);
  try { return axe.commons.text.accessibleText(element); }
  finally { axe.teardown(); }
}

describe("native table grid navigation", () => {
  // ADR-0021-24
  it("returns actions to their cell without selecting a sibling native control", () => {
    document.body.innerHTML = '<table><tbody><tr><td role="gridcell" tabindex="0"><input id="first"><select><option>One</option></select><button>Open</button></td></tr></tbody></table>';
    const cell = document.querySelector("td");
    const input = cell.querySelector("input");
    const select = cell.querySelector("select");
    const button = cell.querySelector("button");
    expect(tableFocusTarget(button)).toBe(cell);
    expect(tableFocusTarget(cell)).toBe(cell);
    expect(tableFocusTarget(input)).toBe(input);
    expect(tableFocusTarget(select)).toBe(select);
    installTableNavigation(document.querySelector("tbody"));
    button.focus();
    expect(document.activeElement).toBe(cell);
    input.focus();
    expect(document.activeElement).toBe(input);
    select.focus();
    expect(document.activeElement).toBe(select);
  });

  // ADR-0021-23
  it("adds only the row name on vertical data-cell moves and clears it on focusout", () => {
    document.body.innerHTML = `<table><thead><tr><th>Name</th><th>Artist</th><th>Actions</th></tr></thead><tbody>
      <tr><td role="gridcell" data-name>First song</td><td role="gridcell">First artist</td><td role="gridcell"><button>Open First song</button></td></tr>
      <tr><td role="gridcell" data-name>Second song</td><td role="gridcell">Second artist</td><td role="gridcell"><button>Open Second song</button></td></tr>
      <tr><td role="gridcell" data-name>Third song</td><td role="gridcell">Third artist</td><td role="gridcell"><input aria-label="Select Third song" type="checkbox"></td></tr>
    </tbody></table><button id="outside">Outside</button>`;
    const body = document.querySelector("tbody");
    installTableNavigation(body);
    const [first, second, third] = body.children;
    const press = (key, options = {}) => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, ...options }));
    first.cells[1].focus();
    press("ArrowDown");
    expect(accessibleName(second.cells[1])).toBe("Second artist, Second song");
    press("ArrowUp");
    expect(second.cells[1].hasAttribute("aria-label")).toBe(false);
    expect(accessibleName(first.cells[1])).toBe("First artist, First song");
    press("End", { ctrlKey: true });
    expect(accessibleName(third.cells[1])).toBe("Third artist, Third song");
    document.getElementById("outside").focus();
    expect(third.cells[1].hasAttribute("aria-label")).toBe(false);
    first.cells[0].focus();
    press("ArrowDown");
    expect(accessibleName(second.cells[0])).toBe("Second song");
    expect(second.cells[0].hasAttribute("aria-label")).toBe(false);
    first.cells[2].focus();
    press("ArrowDown");
    expect(second.cells[2].hasAttribute("aria-label")).toBe(false);
    press("ArrowDown");
    expect(document.activeElement).toBe(third.querySelector("input"));
    expect(third.cells[2].hasAttribute("aria-label")).toBe(false);
  });

  // ADR-0021-16
  it("keeps a clicked button stationary while redirecting focus to its cell", () => {
    document.body.innerHTML = '<table><tbody id="rows"><tr><td role="gridcell">Show</td><td role="gridcell"><button>Open list</button></td></tr></tbody></table>';
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const [name, cell] = body.querySelectorAll("td");
    const button = cell.querySelector("button");
    const cellFocus = vi.spyOn(cell, "focus");
    const cellScroll = vi.spyOn(cell, "scrollIntoView");
    const open = vi.fn();
    button.addEventListener("click", open);

    // Native pointer focus arrives on the button before mouseup. Moving
    // its cell at this point can move the button away from the pointer.
    button.focus();
    expect(document.activeElement).toBe(cell);
    expect(cellFocus).toHaveBeenCalledWith({ preventScroll: true });
    expect(cellScroll).not.toHaveBeenCalled();
    expect([...body.querySelectorAll('[tabindex="0"]')]).toEqual([cell]);
    button.click();
    expect(open).toHaveBeenCalledTimes(1);

    // Keyboard movement still brings the complete destination cell into view.
    name.focus();
    name.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(cell);
    expect(cellScroll).toHaveBeenCalledWith({ block: "nearest", inline: "nearest" });
    cell.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(open).toHaveBeenCalledTimes(2);
  });

  it.each(['<input type="checkbox">', '<select><option>One</option></select>'])(
    "scrolls the focused native control instead of its taller cell: %s", (markup) => {
      document.body.innerHTML = `<table><tbody id="rows"><tr><td role="gridcell">Name</td><td role="gridcell">${markup}</td></tr></tbody></table>`;
      const body = document.getElementById("rows");
      installTableNavigation(body);
      const cell = body.querySelectorAll("td")[1];
      const control = cell.firstElementChild;
      const cellScroll = vi.spyOn(cell, "scrollIntoView");
      const controlScroll = vi.spyOn(control, "scrollIntoView");
      control.focus();
      expect(document.activeElement).toBe(control);
      expect(cellScroll).not.toHaveBeenCalled();
      expect(controlScroll).toHaveBeenCalledWith({ block: "nearest", inline: "nearest" });
      expect([...body.querySelectorAll('[tabindex="0"]')]).toEqual([control]);
    },
  );

  it("refreshes an empty grid and makes newly loaded controls reachable", () => {
    document.body.innerHTML = '<table><tbody id="rows"></tbody></table>';
    const body = document.getElementById("rows");
    const navigation = installTableNavigation(body);
    navigation.refresh();
    body.innerHTML = '<tr><td role="gridcell"><button>Play now</button></td></tr>';
    navigation.refresh();
    expect(body.querySelector("button").tabIndex).toBe(-1);
    expect(body.querySelector("td").tabIndex).toBe(0);
  });

  it("focuses a native select in its grid cell and preserves its arrow keys", () => {
    document.body.innerHTML = '<table><tbody id="rows"><tr><td role="gridcell">Music</td><td role="gridcell"><select><option>Basic</option><option>Similarity</option></select></td></tr></tbody></table>';
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const [name, cell] = body.querySelectorAll("td");
    name.focus();
    name.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    const select = cell.querySelector("select");
    expect(document.activeElement).toBe(select);
    const down = new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true });
    select.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(select);
  });

  it("keeps one tab stop and moves by row, action, and row edge", () => {
    document.body.innerHTML = `<table><tbody id="rows">
      <tr><td role="rowheader" tabindex="-1">One</td><td role="gridcell" tabindex="-1"><button tabindex="-1">Two</button></td></tr>
      <tr><td role="rowheader" tabindex="-1">Three</td><td role="gridcell" tabindex="-1"><button tabindex="-1">Four</button></td></tr>
    </tbody></table>`;
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const [one, two, three, four] = body.querySelectorAll("td");
    expect([one, two, three, four].map((item) => item.tabIndex)).toEqual([0, -1, -1, -1]);
    one.focus();
    one.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(two);
    two.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(four);
    four.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    expect(document.activeElement).toBe(three);
    const first = new KeyboardEvent("keydown", { key: "Home", ctrlKey: true, bubbles: true, cancelable: true });
    three.dispatchEvent(first);
    expect(first.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(one);
    const last = new KeyboardEvent("keydown", { key: "End", ctrlKey: true, bubbles: true, cancelable: true });
    one.dispatchEvent(last);
    expect(last.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(three);
    expect([one, two, three, four].filter((item) => item.tabIndex === 0)).toEqual([three]);
  });

  it("leaves native column headings out of computed cell names during navigation", () => {
    document.body.innerHTML = `<table><thead><tr><th>Title</th><th>Artist</th></tr></thead><tbody id="rows">
      <tr><td role="gridcell" data-name>Song 1</td><td role="gridcell">Band 1</td></tr>
      <tr><td role="gridcell" data-name>Song 2</td><td role="gridcell">Band 2</td></tr>
    </tbody></table>`;
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const [song1, band1, song2, band2] = body.querySelectorAll("td");
    song1.focus();
    song1.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(band1);
    expect(accessibleName(band1)).toBe("Band 1");
    band1.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(band2);
    expect(band1.hasAttribute("aria-label")).toBe(false);
    expect(accessibleName(band2)).toBe("Band 2, Song 2");
    band2.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    expect(accessibleName(song2)).toBe("Song 2");
    band2.innerHTML = '<button aria-label="Track info for Song 2">Track info</button>';
    song2.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(band2);
    expect(band2.hasAttribute("aria-label")).toBe(false);
  });

  it("keeps shared tableRow data names and Options focus on grid cells", () => {
    document.body.innerHTML = '<table><thead><tr><th>Name</th><th>Active</th><th>Options</th></tr></thead><tbody></tbody></table>';
    const body = document.querySelector("tbody");
    const button = document.createElement("button");
    button.textContent = "Options";
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-label", "Options for Evening");
    const row = tableRow(document, ["Evening", "No", button]);
    body.append(row);
    installTableNavigation(body);
    row.cells[0].focus();
    row.cells[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(accessibleName(row.cells[0])).toBe("Evening");
    expect(accessibleName(row.cells[1])).toBe("No");
    expect(accessibleName(button)).toBe("Options for Evening");
    const scroll = vi.spyOn(row.cells[2], "scrollIntoView");
    button.focus();
    expect(document.activeElement).toBe(row.cells[2]);
    expect(scroll).not.toHaveBeenCalled();
    expect(button.tabIndex).toBe(-1);
    expect([...body.querySelectorAll('[tabindex="0"]')]).toEqual([row.cells[2]]);
  });

  it("moves between data rows across browsable date headings", () => {
    document.body.innerHTML = `<table><tbody id="rows">
      <tr><td role="gridcell">Newer</td></tr>
      <tr><td colspan="4"><h3>13 Oct 2026</h3></td></tr>
      <tr><td role="gridcell">Older</td></tr>
    </tbody></table>`;
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const [newer, older] = body.querySelectorAll("td[role=gridcell]");
    newer.focus();
    newer.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(older);
    older.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
    expect(document.activeElement).toBe(newer);
    expect(body.querySelector("h3").textContent).toBe("13 Oct 2026");
  });

  it("keeps Home, End and Ctrl+Home/End after refresh with sparse rows and controls", () => {
    document.body.innerHTML = '<table><tbody id="rows"><tr><td role="gridcell">Old</td></tr></tbody></table>';
    const body = document.getElementById("rows");
    const navigation = installTableNavigation(body);
    body.innerHTML = `<tr><td role="rowheader">One</td><td role="gridcell"><button>Play</button></td></tr>
      <tr><td role="rowheader">Two</td></tr>
      <tr><td role="rowheader">Three</td><td role="gridcell"><input type="checkbox"></td></tr>`;
    navigation.refresh();
    const [one, playCell] = body.querySelectorAll("td[role]");
    const play = playCell.querySelector("button");
    const checkbox = body.querySelector("input");
    play.focus();
    const home = new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true });
    play.dispatchEvent(home);
    expect(home.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(one);
    const end = new KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true });
    one.dispatchEvent(end);
    expect(document.activeElement).toBe(playCell);
    const lastRow = new KeyboardEvent("keydown", { key: "End", ctrlKey: true, bubbles: true, cancelable: true });
    play.dispatchEvent(lastRow);
    expect(document.activeElement).toBe(checkbox);
    const firstRow = new KeyboardEvent("keydown", { key: "Home", ctrlKey: true, bubbles: true, cancelable: true });
    checkbox.dispatchEvent(firstRow);
    expect(document.activeElement).toBe(playCell);
  });

  it("leaves modifiers alone and activates the cell's native checkbox by Space", () => {
    document.body.innerHTML = `<table><tbody id="rows"><tr><td role="gridcell" tabindex="-1"><input type="checkbox" tabindex="-1"></td></tr></tbody></table>`;
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const cell = body.querySelector("td");
    const checkbox = body.querySelector("input");
    const modified = new KeyboardEvent("keydown", { key: "ArrowDown", ctrlKey: true, bubbles: true, cancelable: true });
    cell.dispatchEvent(modified);
    expect(modified.defaultPrevented).toBe(false);
    const space = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
    cell.dispatchEvent(space);
    expect(space.defaultPrevented).toBe(true);
    expect(checkbox.checked).toBe(true);
  });

  it("keeps disabled native controls out of table tab stops and activation", () => {
    document.body.innerHTML = `<table><tbody id="rows"><tr>
      <td role="gridcell"><button disabled>Unavailable</button></td>
      <td role="gridcell"><button>Available</button></td>
    </tr></tbody></table>`;
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const [unavailableCell, availableCell] = body.querySelectorAll("td");
    const unavailable = unavailableCell.querySelector("button");
    let activations = 0;
    unavailable.addEventListener("click", () => { activations += 1; });
    expect([unavailableCell.tabIndex, unavailable.tabIndex, availableCell.tabIndex]).toEqual([0, -1, -1]);

    unavailableCell.focus();
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    unavailableCell.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(false);
    expect(activations).toBe(0);
    unavailableCell.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(availableCell);
    expect([unavailableCell, availableCell].filter((cell) => cell.tabIndex === 0)).toEqual([availableCell]);
  });

  it("puts the tab stop on a cell's single native control and preserves checkbox state", () => {
    document.body.innerHTML = `<table><tbody id="rows">
      <tr><td role="rowheader" tabindex="-1">Folder</td><td role="gridcell" tabindex="-1"><label><input type="checkbox" tabindex="-1"> Include subfolders</label></td></tr>
      <tr><td role="rowheader" tabindex="-1">Other</td><td role="gridcell" tabindex="-1"><button tabindex="-1">Remove</button></td></tr>
    </tbody></table>`;
    const body = document.getElementById("rows");
    const navigation = installTableNavigation(body);
    const [folder, checkboxCell, other, buttonCell] = body.querySelectorAll("td");
    const checkbox = checkboxCell.querySelector("input");
    const button = buttonCell.querySelector("button");
    expect([folder.tabIndex, checkboxCell.tabIndex, checkbox.tabIndex, other.tabIndex, button.tabIndex]).toEqual([0, -1, -1, -1, -1]);

    checkboxCell.focus();
    expect(document.activeElement).toBe(checkbox);
    folder.focus();
    folder.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(checkbox);
    expect(checkbox.type).toBe("checkbox");
    checkbox.checked = true;
    navigation.refresh();
    expect(checkbox.checked).toBe(true);
    expect([folder.tabIndex, checkboxCell.tabIndex, checkbox.tabIndex, other.tabIndex, button.tabIndex]).toEqual([-1, -1, 0, -1, -1]);

    checkbox.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(buttonCell);
    expect([folder, checkboxCell, other, buttonCell].filter((item) => item.tabIndex === 0)).toEqual([buttonCell]);
    expect(button.tabIndex).toBe(-1);
    button.focus();
    expect(document.activeElement).toBe(buttonCell);
  });

  it("does not synthesize a second activation for a native control key event", () => {
    document.body.innerHTML = `<table><tbody id="rows"><tr><td role="gridcell" tabindex="-1"><button tabindex="-1">Remove</button></td></tr></tbody></table>`;
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const button = body.querySelector("button");
    let activations = 0;
    button.addEventListener("click", () => { activations += 1; });

    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    button.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(false);
    expect(activations).toBe(0);
    button.click();
    expect(activations).toBe(1);
  });

  it("opens Track info when Enter is pressed on a track's name", () => {
    document.body.innerHTML = `<table><tbody id="rows">
      <tr><td role="gridcell">12:00</td><td role="gridcell" data-name>Song</td>
        <td role="gridcell"><button type="button">Track info</button></td></tr>
    </tbody></table>`;
    const body = document.getElementById("rows");
    installTableNavigation(body);
    const [, name, action] = body.querySelectorAll("td");
    const open = vi.fn();
    action.querySelector("button").addEventListener("click", open);
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    name.dispatchEvent(enter);
    expect(open).toHaveBeenCalledOnce();
    expect(enter.defaultPrevented).toBe(true);
    name.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
    expect(open).toHaveBeenCalledOnce();
  });
});
