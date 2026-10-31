import { describe, expect, it, vi } from "vitest";
import { deleteCollectionItem } from "../../src/autodj/static/modules/collection-delete.js";

function mount() {
  document.body.innerHTML = `<table><tbody><tr>
    <td role="gridcell" tabindex="0"><button id="first">Options for First</button></td>
    <td role="gridcell" tabindex="-1"><button id="second">Options for Second</button></td>
  </tr></tbody></table><dialog id="confirm-dialog"><h2 id="confirm-title"></h2>
    <p id="confirm-message"></p><button id="confirm-accept">Delete</button><button id="confirm-cancel">Cancel</button></dialog>`;
  const dialog = document.getElementById("confirm-dialog");
  dialog.show = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event("close")); };
  document.getElementById("first").focus();
  return { first: document.getElementById("first"), second: document.getElementById("second") };
}
describe("collection removal", () => {
  it("leaves focus handoff to an undoable removal without a preliminary cell focus", () => {
    const { first, second } = mount();
    const cellFocus = vi.fn();
    first.closest("td").addEventListener("focus", cellFocus);
    deleteCollectionItem(document, { undo: true, onConfirm: () => {
      second.closest("td").focus();
      first.closest("td").remove();
    } });
    expect(cellFocus).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(second.closest("td"));
  });

  // ADR-0021-05
  it("confirms removal unless undo is available", async () => {
    mount();
    const onConfirm = vi.fn(() => "removed");
    const pending = deleteCollectionItem(document, { title: "Delete Example?", onConfirm });
    expect(document.getElementById("confirm-dialog").open).toBe(true);
    document.getElementById("confirm-cancel").click();
    expect(await pending).toBe(false);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(await deleteCollectionItem(document, { undo: true, onConfirm })).toBe("removed");
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(document.getElementById("confirm-dialog").open).toBe(false);
  });

  // ADR-0021-18
  it("returns cancelled removal to the owning gridcell without focusing its button", async () => {
    const { first } = mount();
    const pending = deleteCollectionItem(document, { title: "Delete First?" });
    const buttonFocus = vi.fn();
    first.addEventListener("focus", buttonFocus);
    document.getElementById("confirm-cancel").click();
    expect(await pending).toBe(false);
    expect(document.activeElement).toBe(first.closest("td"));
    expect(buttonFocus).not.toHaveBeenCalled();
  });

  it("returns completed removal to the replacement gridcell without focusing its button", async () => {
    const { first, second } = mount();
    const buttonFocus = vi.fn();
    second.addEventListener("focus", buttonFocus);
    const pending = deleteCollectionItem(document, { title: "Delete First?", onConfirm: () => {
      first.closest("td").remove();
      return second;
    } });
    document.getElementById("confirm-accept").click();
    expect(await pending).toBe(true);
    expect(document.activeElement).toBe(second.closest("td"));
    expect(buttonFocus).not.toHaveBeenCalled();
  });
});
