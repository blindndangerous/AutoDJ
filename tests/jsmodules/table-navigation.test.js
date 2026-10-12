import { describe, expect, it } from "vitest";
import { installTableNavigation } from "../../src/autodj/static/modules/table-navigation.js";

describe("native table grid navigation", () => {
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
    expect([one, two, three, four].filter((item) => item.tabIndex === 0)).toEqual([three]);
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
});
