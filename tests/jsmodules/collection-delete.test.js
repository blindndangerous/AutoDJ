import { describe, expect, it, vi } from "vitest";
import { deleteCollectionItem } from "../../src/autodj/static/modules/collection-delete.js";
import { confirmAction } from "../../src/autodj/static/modules/confirm-dialog.js";
vi.mock("../../src/autodj/static/modules/confirm-dialog.js", () => ({ confirmAction: vi.fn(async () => false) }));
describe("collection removal", () => {
  // ADR-0021-05
  it("confirms removal unless undo is available", async () => {
    const onConfirm = vi.fn(() => "removed");
    expect(await deleteCollectionItem(document, { title: "Delete Example?", onConfirm })).toBe(false);
    expect(confirmAction).toHaveBeenCalledWith(document, { title: "Delete Example?", onConfirm });
    expect(onConfirm).not.toHaveBeenCalled();
    expect(await deleteCollectionItem(document, { undo: true, onConfirm })).toBe("removed");
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(confirmAction).toHaveBeenCalledOnce();
  });
});
