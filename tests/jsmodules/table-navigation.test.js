import { describe, expect, it, vi } from "vitest";
import { installTableNavigation } from "../../src/autodj/static/modules/table-navigation.js";

describe("native table grid navigation", () => {
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
