import { confirmAction } from "./confirm-dialog.js";

// Undo is an explicit property of the owning screen, never inferred from
// the action label. Keep confirmation execution and focus in its guard.
export function deleteCollectionItem(doc, { undo = false, ...options }) {
  return undo ? options.onConfirm() : confirmAction(doc, options);
}
