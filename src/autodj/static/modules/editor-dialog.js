import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { dialogStatusLine, focusAfterDialog, releaseDialogFocus, showFieldError } from "./dom-helpers.js";
import { announceStatus, clearAnnouncedStatus, sayIfStillPending } from "./live-region.js";

const openEditors = new WeakMap();
const editorStatuses = new WeakMap();

export const showEditorError = showFieldError;

// Keep dialogs outside tab panels so hidden panels cannot hide an open editor
// from assistive technology; native modality also supports nested editors.
export function createEditorDialog({ form, heading, cancel, fallback, save, onCancel, dialog: existingDialog, submit }) {
  const doc = form.ownerDocument;
  const dialog = existingDialog || doc.createElement("dialog");
  if (!dialog.id) dialog.id = `${form.id}-dialog`;
  dialog.classList.add("editor-dialog");
  dialog.setAttribute("aria-labelledby", heading.id);
  dialog.append(form);
  doc.body.append(dialog);
  const status = dialogStatusLine(dialog, `${form.id}-status`);
  editorStatuses.set(dialog, status);
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
    clearAnnouncedStatus(status);
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
    const returnTo = onCancel?.() || opener;
    if (!focusAfterDialog(returnTo)) focusAfterDialog(fallback);
  }
  cancel.addEventListener("click", cancelEdit);
  dialog.addEventListener("cancel", cancelEdit);
  async function saveEdit(event) {
    event.preventDefault();
    if (pending || !dialog.open) return;
    pending = true;
    const mine = generation;
    const epoch = captureAuthenticatedRequestEpoch();
    const current = () => dialog.open && generation === mine && isAuthenticatedRequestCurrent(epoch);
    form.setAttribute("aria-busy", "true");
    [...form.querySelectorAll('button[type="submit"]:not(:disabled)'), ...(submit && !submit.disabled ? [submit] : [])].forEach((button) => {
      button.dataset.editorPending = "";
      button.disabled = true;
    });
    const stop = sayIfStillPending((message) => { if (current()) say(message); }, "Saving…");
    try {
      const result = save(event, current);
      if (result?.then) await result;
    }
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
  }
  form.addEventListener("submit", saveEdit);
  submit?.addEventListener("click", saveEdit);
  return {
    dialog, status, close,
    region(fallbackStatus) {
      const top = stack.findLast((item) => item.isConnected && item.open);
      return top ? editorStatuses.get(top) : fallbackStatus;
    },
    open(first, from = doc.activeElement) {
      generation += 1;
      opener = from;
      form.hidden = false;
      original = snapshot();
      clearAnnouncedStatus(status);
      dialog.showModal();
      for (let index = stack.length - 1; index >= 0; index -= 1) {
        if (stack[index] === dialog || !stack[index].isConnected) stack.splice(index, 1);
      }
      stack.push(dialog);
      first?.focus();
    },
  };
}
