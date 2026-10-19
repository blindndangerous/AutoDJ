import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { dialogStatusLine, focusAfterDialog, focusRevealed, releaseDialogFocus } from "./dom-helpers.js";
import { announceStatus, sayIfStillPending } from "./live-region.js";

const openEditors = new WeakMap();

export function showEditorError(control, message, error, status) {
  error.textContent = message;
  error.hidden = false;
  if (!control) { announceStatus(status, message, { force: true, tone: "error" }); return; }
  control.setAttribute("aria-invalid", "true");
  const descriptions = new Set((control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
  descriptions.add(error.id);
  control.setAttribute("aria-describedby", [...descriptions].join(" "));
  const following = control.nextElementSibling;
  const label = control.closest("label") || (following?.htmlFor === control.id ? following : null);
  (label || control).after(error);
  const before = control.ownerDocument.activeElement;
  focusRevealed(control);
  if (before === control || control.ownerDocument.activeElement !== control) {
    announceStatus(status, message, { force: true, tone: "error" });
  }
}

// Keep dialogs outside tab panels so hidden panels cannot hide an open editor
// from assistive technology; native modality also supports nested editors.
export function createEditorDialog({ form, heading, cancel, fallback, save, onCancel }) {
  const doc = form.ownerDocument;
  const dialog = doc.createElement("dialog");
  dialog.id = `${form.id}-dialog`;
  dialog.className = "editor-dialog";
  dialog.setAttribute("aria-labelledby", heading.id);
  dialog.append(form);
  doc.body.append(dialog);
  const status = dialogStatusLine(dialog, `${form.id}-status`);
  const stack = openEditors.get(doc) || [];
  openEditors.set(doc, stack);
  let generation = 0;
  let pending = false;
  let confirming = false;
  let opener = null;
  let original = "";
  const snapshot = () => JSON.stringify([...form.querySelectorAll("input, select, textarea")]
    .map((control) => [control.id, control.value, control.checked,
      [...(control.files || [])].map((file) => [file.name, file.size, file.lastModified])]));
  const say = (message, tone = "info") => announceStatus(status, message, { force: true, tone });

  function close() {
    generation += 1;
    if (confirming) doc.getElementById("confirm-dialog")?.close();
    pending = false;
    form.removeAttribute("aria-busy");
    form.querySelectorAll('[data-editor-pending]').forEach((button) => {
      button.disabled = false;
      delete button.dataset.editorPending;
    });
    releaseDialogFocus(dialog);
    if (dialog.open) dialog.close();
    const index = stack.indexOf(dialog);
    if (index !== -1) stack.splice(index, 1);
    form.hidden = true;
    status.textContent = "";
  }

  async function cancelEdit(event) {
    event.preventDefault();
    if (confirming || pending) return;
    const mine = generation;
    if (snapshot() !== original) {
      confirming = true;
      const discard = await confirmAction(doc, {
        title: "Discard changes?", message: "Your changes have not been saved.", confirmLabel: "Discard changes",
      });
      confirming = false;
      if (!discard || mine !== generation) return;
    }
    close();
    onCancel?.();
    if (!focusAfterDialog(opener)) focusAfterDialog(fallback);
  }
  cancel.addEventListener("click", cancelEdit);
  dialog.addEventListener("cancel", cancelEdit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending || !dialog.open) return;
    pending = true;
    const mine = generation;
    const epoch = captureAuthenticatedRequestEpoch();
    const current = () => dialog.open && generation === mine && isAuthenticatedRequestCurrent(epoch);
    form.setAttribute("aria-busy", "true");
    form.querySelectorAll('button[type="submit"]:not(:disabled)').forEach((button) => {
      button.dataset.editorPending = "";
      button.disabled = true;
    });
    const stop = sayIfStillPending((message) => { if (current()) say(message); }, "Saving…");
    try { await save(event, current); }
    catch (error) { if (current()) say(error.message, "error"); }
    finally {
      stop();
      if (generation === mine) {
        pending = false;
        form.removeAttribute("aria-busy");
        form.querySelectorAll('[data-editor-pending]').forEach((button) => {
          button.disabled = false;
          delete button.dataset.editorPending;
        });
      }
    }
  });
  return {
    dialog, status, close,
    region(fallbackStatus) {
      const top = stack.findLast((item) => item.isConnected && item.open);
      return top ? dialogStatusLine(top, `${top.id.replace(/-dialog$/, "")}-status`) : fallbackStatus;
    },
    open(first, from = doc.activeElement) {
      generation += 1;
      opener = from;
      form.hidden = false;
      original = snapshot();
      status.textContent = "";
      dialog.showModal();
      for (let index = stack.length - 1; index >= 0; index -= 1) {
        if (stack[index] === dialog || !stack[index].isConnected) stack.splice(index, 1);
      }
      stack.push(dialog);
      first?.focus();
    },
  };
}
