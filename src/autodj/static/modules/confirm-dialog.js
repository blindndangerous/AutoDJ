// Shared "are you sure" step for actions that cannot be undone: Clear
// queue, Delete profile, Sign out, Revoke device.
//
// A native modal <dialog>: NVDA hears the title and the message when it
// opens, focus starts on Cancel so a stray Enter changes nothing, and
// Escape cancels.  The caller decides where focus goes afterwards; the
// dialog never leaves it on <body>.

export function confirmAction(doc, { title, message, confirmLabel }) {
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
  // Escape closes without a submitter and keeps the old returnValue.
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "confirm");
    }, { once: true });
    dialog.showModal();
    cancelButton?.focus();
  });
}
