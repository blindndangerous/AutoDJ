// Shared "are you sure" step for actions that cannot be undone: Clear
// queue, Delete profile, Delete liner, Sign out, Revoke device.
//
// A native modal <dialog>: NVDA hears the title and the message when it
// opens, focus starts on Cancel so a stray Enter changes nothing, and
// Escape cancels.  Cancel leaves focus on the control that opened the
// dialog; the browser puts it back there as the dialog closes.
//
// onConfirm, when given, does the action while the dialog is still open
// and returns the element that should have focus afterwards.  The page
// behind a modal dialog is inert, so focus cannot move there before the
// dialog closes; instead the list is already updated when it closes, and
// focus moves to the target in the same task.  Focus never rests on the
// page or on the opener in between, which made NVDA read a line from the
// top of the page and the opener before the new focus (D14).  While it
// runs, Cancel and Escape are held so the dialog cannot close under it.

export function confirmAction(doc, { title, message, confirmLabel, onConfirm = null }) {
  const dialog = doc.getElementById("confirm-dialog");
  const heading = doc.getElementById("confirm-title");
  const text = doc.getElementById("confirm-message");
  const confirmButton = doc.getElementById("confirm-accept");
  const cancelButton = doc.getElementById("confirm-cancel");
  if (!dialog || !heading || !text || !confirmButton || dialog.open) {
    return Promise.resolve(false);
  }
  heading.textContent = title;
  text.textContent = message;
  confirmButton.textContent = confirmLabel;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    let pending = false;
    const listeners = [
      [confirmButton, "click", accept],
      [cancelButton, "click", cancel],
      [dialog, "cancel", holdWhilePending],
      [dialog, "close", closed],
    ];
    function settle(value) {
      for (const [target, type, listener] of listeners) {
        target?.removeEventListener(type, listener);
      }
      resolve(value);
    }
    async function accept(event) {
      event.preventDefault();
      if (pending) return;
      pending = true;
      let target = null;
      if (onConfirm) {
        try {
          target = await onConfirm();
        } catch (_) {
          target = null;
        }
      }
      dialog.close("confirm");
      if (target?.isConnected) target.focus();
      settle(true);
    }
    function cancel(event) {
      event.preventDefault();
      if (!pending) dialog.close("cancel");
    }
    function holdWhilePending(event) {
      if (pending) event.preventDefault();
    }
    // Cancel, Escape, or a browser that closed the dialog anyway.
    function closed() {
      if (!pending) settle(false);
    }
    for (const [target, type, listener] of listeners) {
      target?.addEventListener(type, listener);
    }
    dialog.showModal();
    cancelButton?.focus();
  });
}
