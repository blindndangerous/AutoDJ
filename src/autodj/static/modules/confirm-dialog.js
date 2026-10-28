import {
  dialogReturnPlace, dialogStatusLine, focusAfterDialog, focusSteadyPlace,
} from "./dom-helpers.js";
import { sayIfStillPending } from "./live-region.js";

// Shared "are you sure" step for actions that cannot be undone: Clear
// queue, Replace profile, Delete profile, Delete liner, Sign out and Revoke
// device.
//
// A native modal <dialog>: NVDA hears the title and the message when it
// opens, focus starts on Cancel so a stray Enter changes nothing, and
// Escape cancels.  Cancel and Escape put focus back on the control that
// opened the dialog.
//
// Focus goes straight from the dialog's button to where it belongs, never
// through the page body: a dialog control that let go of focus first left
// it on the body for a moment, and NVDA in Firefox read "Skip to main
// content" before the control (#602).  As a modal dialog closes, the
// browser itself puts focus back on the control that opened it, which is
// right for Cancel and Escape.  When focus belongs somewhere else, the
// opener is made inert while the dialog closes, so the browser has nothing
// to put back, and focus moves on in the same task.  The close event comes
// a task later; it puts focus back only when it is not already on the
// opener (#385), as when the opener was gone and the browser had nowhere
// to put it.
//
// onConfirm, when given, does the action while the dialog is still open
// and returns the element that should have focus afterwards.  The page
// behind a modal dialog is inert, so focus cannot move there before the
// dialog closes; instead the list is already updated when it closes, and
// focus moves to the target in the same task.  Focus never rests on the
// page or on the opener in between, which made NVDA read a line from the
// top of the page and the opener before the new focus (D14).  While it
// runs, Cancel and Escape are held so the dialog cannot close under it,
// the confirm button is aria-busy, and an action still running after a
// moment says *pendingText* ("Removing…") in the dialog's own status line:
// the page's status regions are inert behind the dialog (#384).

// Screen reader trial (#602, #627), chosen by ?confirm=<name> in the page
// address so each way of opening the confirmation can be heard in NVDA
// before one is kept.  Without it the confirmation opens as before.
//   cancel-last     showModal(); Cancel after the confirm button.
//   nonmodal        show() with aria-modal="true", Cancel last; the page
//                   keeps its accessibility tree, a backdrop takes clicks,
//                   and Tab and Escape are handled here.
//   nonmodal-plain  nonmodal without aria-modal.
//   group           nonmodal-plain, exposed as a group, not a dialog.
// Every trial also takes the form out of the tree (role="none"), so it is
// not read as a form landmark.
const TRIALS = {
  "cancel-last": { cancelLast: true },
  nonmodal: { cancelLast: true, nonModal: true, ariaModal: true },
  "nonmodal-plain": { cancelLast: true, nonModal: true },
  group: { cancelLast: true, nonModal: true, role: "group" },
};

function confirmTrial(doc) {
  try {
    const name = new URLSearchParams(doc.defaultView?.location?.search || "").get("confirm");
    return Object.hasOwn(TRIALS, name) ? TRIALS[name] : null;
  } catch (_) {
    return null;
  }
}

function trialBackdrop(doc) {
  let backdrop = doc.getElementById("confirm-backdrop");
  if (!backdrop) {
    backdrop = doc.createElement("div");
    backdrop.id = "confirm-backdrop";
    backdrop.hidden = true;
    doc.body.append(backdrop);
  }
  return backdrop;
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
  const trial = confirmTrial(doc);
  return new Promise((resolve) => {
    let pending = false;
    const listeners = [
      [confirmButton, "click", accept],
      [cancelButton, "click", cancel],
      [dialog, "cancel", holdWhilePending],
      [dialog, "close", closed],
    ];
    if (trial?.nonModal) {
      listeners.push([dialog, "keydown", trialKeys], [doc, "focusin", trialKeepFocus]);
    }
    // Trial: Tab and Shift+Tab go round the dialog's buttons, and Escape
    // cancels, as they do in a modal dialog.
    function trialKeys(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!pending) closeOnto("cancel", null);
        return;
      }
      if (event.key !== "Tab") return;
      const stops = [...dialog.querySelectorAll("button:not([disabled])")];
      const first = stops[0];
      const last = stops[stops.length - 1];
      if (event.shiftKey && doc.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && doc.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    // Trial: focus that leaves the open dialog comes back to Cancel.
    function trialKeepFocus(event) {
      if (dialog.open && !dialog.contains(event.target)) cancelButton?.focus();
    }
    function endTrial() {
      if (!trial) return;
      const backdrop = doc.getElementById("confirm-backdrop");
      if (backdrop) backdrop.hidden = true;
      dialog.classList.remove("confirm-nonmodal");
      dialog.removeAttribute("aria-modal");
      dialog.removeAttribute("role");
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
      endTrial();
      if (!(elsewhere && focusAfterDialog(target))) returnFocus();
    }
    function settle(value) {
      for (const [target, type, listener] of listeners) {
        target?.removeEventListener(type, listener);
      }
      endTrial();
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
    // Escape: held while the action runs; otherwise the browser closes the
    // dialog and puts focus back on the opener.
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
    for (const [target, type, listener] of listeners) {
      target?.addEventListener(type, listener);
    }
    if (trial) dialog.querySelector("form")?.setAttribute("role", "none");
    // Last among the buttons, so a screen reader that reads the dialog
    // through leaves its cursor on Cancel.
    if (trial?.cancelLast) cancelButton?.parentElement?.append(cancelButton);
    if (trial?.nonModal) {
      dialog.classList.add("confirm-nonmodal");
      if (trial.ariaModal) dialog.setAttribute("aria-modal", "true");
      if (trial.role) dialog.setAttribute("role", trial.role);
      trialBackdrop(doc).hidden = false;
      dialog.show();
    } else {
      dialog.showModal();
    }
    cancelButton?.focus();
  });
}

// Three-way choice for a server conflict. It uses the same native modal,
// title, focus handling, and button styling as ordinary confirmations.
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
    actions.append(cancelButton, acceptButton);
    cancelButton.textContent = originalCancel;
  }
}
