import {
  dialogReturnPlace, dialogStatusLine, focusAfterDialog, focusSteadyPlace,
} from "./dom-helpers.js";
import { sayIfStillPending } from "./live-region.js";

// Shared "are you sure" step for actions that cannot be undone: Clear
// queue, Replace profile, Delete profile, Delete liner, Sign out and Revoke
// device.
//
// A native <dialog> labelled by its title and described by its message:
// NVDA hears both when it opens, focus starts on Cancel so a stray Enter
// changes nothing, and Escape cancels.  Cancel and Escape put focus back on
// the control that opened the dialog.
//
// It opens with show() and aria-modal="true", not showModal() (#602, #627).
// NVDA reads a web dialog in a browse-mode document of its own.  Behind a
// modal dialog the browser hides the rest of the page from assistive
// technology, so NVDA's page document lost its place, and as the dialog
// closed NVDA read its first line, "Skip to main content", before the
// control focus returned to.  Shown with show(), the page stays in the
// accessibility tree and NVDA returns to where it was.  The page still
// cannot be reached while the dialog is open: a backdrop over it takes
// clicks, Tab and Shift+Tab go round the dialog's buttons, and focus that
// leaves the dialog comes back to Cancel.  Opened over another modal
// dialog, whose page is inert, it opens with showModal() as before.
//
// Cancel is the last button.  On opening, NVDA reads the dialog through
// and leaves its browse-mode cursor on the last button it read, where
// Enter acts: with Restart last, Enter restarted AutoDJ in Chrome (#627).
// The form has role="none", so it is not read as a form landmark.
//
// Focus goes straight from the dialog's button to where it belongs, never
// through the page body: a dialog control that let go of focus first left
// it on the body for a moment.  As a dialog closes with focus in it, the
// browser itself puts focus back on the control that opened it, which is
// right for Cancel and Escape.  When focus belongs somewhere else, the
// opener is made inert while the dialog closes, so the browser has nothing
// to put back, and focus moves on in the same task.  The close event comes
// a task later; it puts focus back only when it is not already on the
// opener (#385), as when the opener was gone and the browser had nowhere
// to put it.
//
// onConfirm, when given, does the action while the dialog is still open
// and returns the element that should have focus afterwards: the list is
// already updated when the dialog closes, and focus moves to the target in
// the same task.  Focus never rests on the page or on the opener in
// between, which made NVDA read a line from the top of the page and the
// opener before the new focus (D14).  While it runs, Cancel and Escape are
// held so the dialog cannot close under it, the confirm button is
// aria-busy, and an action still running after a moment says *pendingText*
// ("Removing…") in the dialog's own status line, where a screen reader
// reading the dialog finds it (#384).

// The shade over the page while the confirmation is open.
function backdropFor(doc) {
  let backdrop = doc.getElementById("confirm-backdrop");
  if (!backdrop) {
    backdrop = doc.createElement("div");
    backdrop.id = "confirm-backdrop";
    backdrop.hidden = true;
    doc.body.append(backdrop);
  }
  return backdrop;
}

// Whether a modal dialog other than *dialog* is open.  The page behind it
// is inert, so the confirmation must open in the top layer above it.
function otherModalOpen(doc, dialog) {
  for (const other of doc.querySelectorAll("dialog[open]")) {
    if (other === dialog) continue;
    try {
      if (other.matches(":modal")) return true;
    } catch (_) {
      // A browser without :modal; an open dialog is then taken as modal.
      return true;
    }
  }
  return false;
}

// station: the action changes one station (#70).  While the server has
// several, the message then ends by naming the station the page controls.
export function confirmAction(doc, {
  title, message, confirmLabel, onConfirm = null, pendingText = "Working…",
}) {
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
  const opener = doc.activeElement === doc.body ? null : doc.activeElement;
  // Where focus goes if the opener is gone when the dialog closes.
  const place = dialogReturnPlace(opener);
  const modal = otherModalOpen(doc, dialog);
  const backdrop = modal ? null : backdropFor(doc);
  return new Promise((resolve) => {
    let pending = false;
    const listeners = [
      [confirmButton, "click", accept],
      [cancelButton, "click", cancel],
      [dialog, "cancel", holdWhilePending],
      [dialog, "close", closed],
    ];
    if (!modal) {
      listeners.push(
        [doc, "keydown", keys, true],
        [doc, "focusin", keepFocusIn],
        [backdrop, "mousedown", stayPut],
        [backdrop, "click", stayPut],
      );
    }
    // Tab and Shift+Tab go round the dialog's buttons, from the page body
    // too (where a click on the dialog's text leaves focus), and Escape
    // cancels: what a modal dialog does.  A modal dialog opened over it,
    // such as sign in, has the keys to itself.
    function keys(event) {
      if (otherModalOpen(doc, dialog)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (!pending) closeOnto("cancel", null);
        return;
      }
      if (event.key !== "Tab") return;
      const stops = [...dialog.querySelectorAll("button:not([disabled])")];
      if (!stops.length) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const inside = dialog.contains(doc.activeElement);
      if (event.shiftKey && (!inside || doc.activeElement === first)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (!inside || doc.activeElement === last)) {
        event.preventDefault();
        first.focus();
      }
    }
    // Focus that leaves the open dialog for the page comes back to Cancel.
    function keepFocusIn(event) {
      if (!dialog.open || dialog.contains(event.target) || otherModalOpen(doc, dialog)) return;
      cancelButton?.focus();
    }
    // A press on the backdrop neither acts nor takes focus from the dialog.
    function stayPut(event) {
      event.preventDefault();
      event.stopPropagation();
    }
    function hideBackdrop() {
      if (backdrop) backdrop.hidden = true;
    }
    // Back on the opener, or somewhere steady near it; never the page body.
    // Focus already there, put back by the browser, stays: letting go of it
    // to take it again would rest it on the page body (#602).
    function returnFocus() {
      if (opener && doc.activeElement === opener) return;
      if (!focusAfterDialog(opener)) focusSteadyPlace(doc, place);
    }
    // Close the dialog with focus going straight to *target*, or back to the
    // opener when there is none.
    function closeOnto(value, target) {
      const elsewhere = target?.isConnected && target !== opener;
      // The browser would put focus back on the opener first; an inert
      // control cannot take it.
      const hold = elsewhere && opener?.isConnected && !opener.hasAttribute("inert");
      if (hold) opener.setAttribute("inert", "");
      try {
        dialog.close(value);
      } finally {
        if (hold) opener.removeAttribute("inert");
      }
      hideBackdrop();
      if (!(elsewhere && focusAfterDialog(target))) returnFocus();
    }
    function settle(value) {
      for (const [target, type, listener, capture] of listeners) {
        target?.removeEventListener(type, listener, capture);
      }
      hideBackdrop();
      resolve(value);
    }
    async function accept(event) {
      event.preventDefault();
      if (pending) return;
      pending = true;
      let target = null;
      if (onConfirm) {
        const status = dialogStatusLine(dialog, "confirm-status");
        confirmButton.setAttribute("aria-busy", "true");
        const stopPending = sayIfStillPending((text) => { status.textContent = text; }, pendingText);
        try {
          target = await onConfirm();
        } catch (_) {
          target = null;
        } finally {
          stopPending();
          status.textContent = "";
          confirmButton.removeAttribute("aria-busy");
        }
      }
      closeOnto("confirm", target);
      settle(true);
    }
    function cancel(event) {
      event.preventDefault();
      if (pending) return;
      closeOnto("cancel", null);
    }
    // Escape over another modal dialog: held while the action runs;
    // otherwise the browser closes the dialog and puts focus back on the
    // opener.
    function holdWhilePending(event) {
      if (pending) event.preventDefault();
    }
    // Cancel, Escape, or a browser that closed the dialog anyway.  Focus
    // still in the closed dialog, or on the page body, goes back.
    function closed() {
      if (pending) return;
      settle(false);
      const active = doc.activeElement;
      if (!active || active === doc.body || dialog.contains(active)) returnFocus();
    }
    for (const [target, type, listener, capture] of listeners) {
      target?.addEventListener(type, listener, capture);
    }
    dialog.setAttribute("aria-modal", "true");
    dialog.querySelector("form")?.setAttribute("role", "none");
    cancelButton?.parentElement?.append(cancelButton);
    if (modal) {
      dialog.showModal();
    } else {
      backdrop.hidden = false;
      dialog.show();
    }
    cancelButton?.focus();
  });
}

// Three-way choice for a server conflict. It uses the same dialog, title,
// focus handling, and button styling as ordinary confirmations, Cancel
// last.
export async function chooseAction(doc, {
  title, message, choices,
}) {
  const dialog = doc.getElementById("confirm-dialog");
  const acceptButton = doc.getElementById("confirm-accept");
  const cancelButton = doc.getElementById("confirm-cancel");
  if (!dialog || dialog.open || !acceptButton || !cancelButton
      || acceptButton.parentElement !== cancelButton.parentElement || choices.length !== 3) return null;

  const [acceptLabel, alternateLabel, cancelLabel] = choices;
  const originalCancel = cancelButton.textContent;
  const alternate = doc.createElement("button");
  alternate.type = "button";
  alternate.textContent = alternateLabel;
  const actions = acceptButton.parentElement;
  actions.append(acceptButton, alternate, cancelButton);
  cancelButton.textContent = cancelLabel;
  // Focus goes back as for Cancel, put back by the browser as it closes.
  const discard = (event) => {
    event.preventDefault();
    dialog.close("alternate");
  };
  alternate.addEventListener("click", discard);
  try {
    const accepted = await confirmAction(doc, {
      title, message, confirmLabel: acceptLabel,
    });
    return accepted ? "accept" : dialog.returnValue === "alternate" ? "alternate" : null;
  } finally {
    alternate.removeEventListener("click", discard);
    alternate.remove();
    actions.append(acceptButton, cancelButton);
    cancelButton.textContent = originalCancel;
  }
}
