import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, withDisabled } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { closeDialog, openDialog } from "./dialog-guard.js";
import { dialogStatusLine, focusAfterDialog } from "./dom-helpers.js";
import { clearAnnouncedStatus, makeSay } from "./live-region.js";
import { tableFocusTarget } from "./table-row.js";

const openEditors = new WeakMap();
const closeEditors = new WeakMap();
const editorStatuses = new WeakMap();


// Keep dialogs outside tab panels so hidden panels cannot hide an open editor
// from assistive technology.  An editor opens with show() and aria-modal,
// not showModal(), so NVDA keeps its place on the page (#631, see
// dialog-guard.js); editors and the dialogs opened from them stack.
export function createEditorDialog({ form, heading, cancel, fallback, save, onCancel, dialog: existingDialog, submit, pendingText = "Saving…" }) {
  const doc = form.ownerDocument;
  const dialog = existingDialog || doc.createElement("dialog");
  if (!dialog.id) dialog.id = `${form.id}-dialog`;
  dialog.classList.add("editor-dialog");
  dialog.setAttribute("aria-labelledby", heading.id);
  // Without explicit help, NVDA synthesizes a description from the entire
  // form. Keep caller-provided short help, such as the clockwheel step's.
  if (!dialog.getAttribute("aria-describedby")) {
    const help = doc.createElement("p");
    help.id = `${dialog.id}-help`;
    help.hidden = true;
    help.textContent = "Use Tab to move between fields and actions.";
    dialog.append(help);
    dialog.setAttribute("aria-describedby", help.id);
  }
  // The heading names the dialog from inside its form's legend, so the
  // fieldset would be a second group with the same name: NVDA said "Add
  // category dialog" and then "Add category grouping".  The fieldset keeps
  // its layout and loses its role.
  const legendGroup = heading.closest("legend")?.parentElement;
  if (legendGroup?.tagName === "FIELDSET" && form.contains(legendGroup)) legendGroup.setAttribute("role", "none");
  dialog.append(form);
  doc.body.append(dialog);
  const status = dialogStatusLine(dialog, `${form.id}-status`);
  editorStatuses.set(dialog, status);
  const stack = openEditors.get(doc) || [];
  openEditors.set(doc, stack);
  let generation = 0;
  let pending = false;
  let busyController = null;
  let confirming = false;
  let opener = null;
  let original = "";
  const snapshot = () => JSON.stringify([...form.querySelectorAll("input, select, textarea")]
    .map((control) => [control.id, control.value, control.checked,
      [...(control.files || [])].map((file) => [file.name, file.size, file.lastModified])]));
  const say = makeSay(status);

  // *restore*: focus goes back to the opener, which the browser does as the
  // dialog closes.  Otherwise the caller moves focus on in the same task,
  // and the opener is held inert so the browser does not take it first.
  function close({ restore = false } = {}) {
    const position = stack.indexOf(dialog);
    if (position !== -1) {
      for (const child of stack.slice(position + 1).reverse()) closeEditors.get(child)?.();
    }
    generation += 1;
    if (confirming) doc.getElementById("confirm-dialog")?.close();
    pending = false;
    busyController?.abort();
    closeDialog(dialog, restore ? { opener: opener?.isConnected ? opener : null } : { opener, handOff: true });
    const index = stack.indexOf(dialog);
    if (index !== -1) stack.splice(index, 1);
    form.hidden = true;
    clearAnnouncedStatus(status);
  }
  closeEditors.set(dialog, close);

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
    close({ restore: true });
    const returnTo = onCancel?.() || opener;
    if (doc.activeElement !== returnTo && !focusAfterDialog(returnTo)) focusAfterDialog(fallback);
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
    const controller = new AbortController();
    busyController = controller;
    const signal = controller.signal;
    const buttons = new Set([...form.querySelectorAll('button[type="submit"]:not(:disabled)'), ...(submit && !submit.disabled ? [submit] : [])]);
    const finish = () => {
      if (generation === mine && busyController === controller) {
        pending = false;
        controller.abort();
      }
    };
    let run = () => withDisabled(form, () => {
      const result = save(event, current);
      if (result?.then) return Promise.resolve(result).finally(finish);
      finish();
      return result;
    }, {
      signal, pendingText, say: (message) => { if (current()) say(message); },
    });
    for (const button of buttons) {
      const operation = run;
      run = () => withDisabled(button, operation, { signal });
    }
    try {
      await run();
    }
    catch (error) { if (current()) say(error.message, "error"); }
    finally {
      finish();
    }
  }
  form.addEventListener("submit", saveEdit);
  submit?.addEventListener("click", saveEdit);
  return {
    dialog, status, close,
    // The owning panel is going away: close the editor and remove every
    // listener added here, so none outlives the panel.
    destroy() {
      close();
      cancel.removeEventListener("click", cancelEdit);
      dialog.removeEventListener("cancel", cancelEdit);
      form.removeEventListener("submit", saveEdit);
      submit?.removeEventListener("click", saveEdit);
      closeEditors.delete(dialog);
      editorStatuses.delete(dialog);
    },
    region(fallbackStatus) {
      const top = stack.findLast((item) => item.isConnected && item.open);
      return top ? editorStatuses.get(top) : fallbackStatus;
    },
    open(first, from = doc.activeElement) {
      generation += 1;
      opener = tableFocusTarget(from);
      form.hidden = false;
      original = snapshot();
      clearAnnouncedStatus(status);
      // Native opening must choose the requested field itself, otherwise it
      // focuses the first descendant before our requested field gets focus.
      const hadAutofocus = first?.hasAttribute("autofocus");
      first?.setAttribute("autofocus", "");
      try {
        openDialog(dialog, { home: first });
      } finally {
        if (!hadAutofocus) first?.removeAttribute("autofocus");
      }
      for (let index = stack.length - 1; index >= 0; index -= 1) {
        if (stack[index] === dialog || !stack[index].isConnected) stack.splice(index, 1);
      }
      stack.push(dialog);
      if (doc.activeElement !== first) first?.focus();
    },
  };
}
