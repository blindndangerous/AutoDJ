import { closeDialog, openDialog, otherModalOpen } from "./dialog-guard.js";
import {
  dialogReturnPlace, dialogStatusLine, focusAfterDialog, focusSteadyPlace, releaseDialogFocus,
} from "./dom-helpers.js";
import { sayIfStillPending } from "./live-region.js";
import { tableFocusTarget } from "./table-row.js";

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
// accessibility tree and NVDA returns to where it was.  It is not made
// inert or aria-hidden either, which would hide it from NVDA again, so a
// screen reader can still read the page behind the dialog.  While the
// dialog is open, nothing on the page can be used, and stray focus comes
// back to Cancel (dialog-guard.js, shared with the other dialogs since
// #631).  Opened over another modal dialog, whose page is inert, it
// opens with showModal() and lets go of focus before it closes, as other
// modal dialogs do (#385).
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
// to put back, and focus moves on in the same task.  The answer comes at
// the user's action, before the dialog closes; the close event, a task
// later, only checks focus, putting it back when it is still on the page
// body or in a closed dialog, as when the opener was gone and the browser
// had nowhere to put it (#385).
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

// Each confirmation dialog's current opening, and the focus checks waiting
// for its close events.  An opening is answered at the moment of its own
// action (confirm, Cancel, Escape, the third choice, or the dialog taken out
// of the page): it removes every listener it added and resolves before the
// dialog closes, so nothing it left can act on a later opening.  The
// browser fires close a task after the dialog closes, and another
// confirmation can open before then, as Queue anyway does when the song
// then needs a voice track choice.  Each close an opening makes queues one
// focus check, run by its own close event: close events come one per close,
// in order.  A close event with no check waiting is a close the page or the
// browser made itself, and answers No for the opening still unanswered.
// While its action runs, an opening is not answered by such a close (the
// browser closes a dialog on a second Escape however it is held); its close
// event is noted, and it answers with the action's result.
const dialogStates = new WeakMap();

function dialogState(dialog) {
  let state = dialogStates.get(dialog);
  if (!state) {
    state = { current: null, checks: [] };
    dialogStates.set(dialog, state);
    dialog.addEventListener("close", () => {
      const check = state.checks.shift();
      if (check) {
        check();
        return;
      }
      const opening = state.current;
      if (!opening || opening.settled || dialog.open) return;
      if (opening.pending) opening.closeCame = true;
      else opening.closedElsewhere();
    });
  }
  return state;
}

// station: the action changes one station (#70).  While the server has
// several, the message then ends by naming the station the page controls.
export function confirmAction(doc, options) {
  return ask(doc, options, null, (answer) => answer === "confirm");
}

// One opening of the confirmation.  Its answer is "confirm", "cancel" or,
// with *alternate* (a third button chooseAction adds), "alternate"; the
// promise resolves with *as*(answer), with no promise step in between.
function ask(doc, {
  title, message, confirmLabel, onConfirm = null, pendingText = "Working…",
}, alternate, as) {
  const dialog = doc.getElementById("confirm-dialog");
  const heading = doc.getElementById("confirm-title");
  const text = doc.getElementById("confirm-message");
  const confirmButton = doc.getElementById("confirm-accept");
  const cancelButton = doc.getElementById("confirm-cancel");
  if (!dialog || !heading || !text || !confirmButton || dialog.open) {
    return Promise.resolve(as("cancel"));
  }
  const state = dialogState(dialog);
  // An opening the page or browser closed: one waiting for its action lets
  // go of the dialog and answers with the action's result when it ends; any
  // other is answered No now.  A close event not come yet is passed over
  // when it comes.
  const previous = state.current;
  if (previous && !previous.settled) {
    if (previous.pending) previous.detach();
    else previous.settle("cancel");
    if (!previous.closeCame) state.checks.push(() => {});
  }
  // This opening shows the dialog idle, whatever a replaced opening, still
  // waiting for its action, left: a busy confirm button, "Removing…".
  confirmButton.removeAttribute("aria-busy");
  const status = doc.getElementById("confirm-status");
  if (status && dialog.contains(status)) status.textContent = "";
  heading.textContent = title;
  text.textContent = message;
  confirmButton.textContent = confirmLabel;
  dialog.returnValue = "";
  const opener = doc.activeElement === doc.body ? null : tableFocusTarget(doc.activeElement);
  // Where focus goes if the opener is gone when the dialog closes.
  const place = dialogReturnPlace(opener);
  const modal = otherModalOpen(doc, dialog);
  let release = () => {};
  return new Promise((resolve) => {
    const opening = { settled: false, pending: false, closeCame: false, settle, detach, closedElsewhere };
    state.current = opening;
    const listeners = [
      [confirmButton, "click", accept],
      [cancelButton, "click", cancel],
      [alternate, "click", chooseAlternate],
      [dialog, "cancel", escape],
    ];
    // Back on the opener, or somewhere steady near it; never the page body.
    // Focus already there, put back by the browser, stays: letting go of it
    // to take it again would rest it on the page body (#602).  Over another
    // modal dialog it is taken again, so the change is reported once the
    // dialog has closed (#385).
    function returnFocus() {
      if (!modal && opener && doc.activeElement === opener) return;
      if (!focusAfterDialog(opener)) focusSteadyPlace(doc, place);
    }
    // Run by this opening's close event, a task after it closed.  Nothing
    // is done while the dialog is open again for a later opening.  Focus
    // still in a closed dialog, or on the page body, goes back; after
    // Cancel or Escape over another modal dialog, so does focus the browser
    // put back on the opener, taken again so the change is reported (#385).
    // Focus the page has moved elsewhere stays.
    function checkFocus(retake) {
      if (dialog.open) return;
      const active = doc.activeElement;
      const loose = !active || active === doc.body || dialog.contains(active)
        || Boolean(active?.closest?.("dialog:not([open])"));
      if (loose || (retake && active === opener)) returnFocus();
    }
    // Whether this opening still has the dialog; a later one may have it.
    const showing = () => state.current === opening;
    // Every listener this opening added goes.
    function detach() {
      for (const [target, type, listener, capture] of listeners) {
        target?.removeEventListener(type, listener, capture);
      }
      release();
    }
    // Answered: its listeners go, then the promise resolves.  Once only.
    function settle(answer) {
      if (opening.settled) return;
      opening.settled = true;
      detach();
      resolve(as(answer));
    }
    // Closed by the page or the browser, not by this opening's own action.
    function closedElsewhere() {
      settle("cancel");
      checkFocus(modal);
    }
    // Answer, then close the dialog with focus going straight to *target*,
    // or back to the opener when there is none.
    function finish(answer, target) {
      target = tableFocusTarget(target);
      settle(answer);
      // A later opening has the dialog now: this one only answers.
      if (!showing()) return;
      const closing = dialog.open;
      if (modal) {
        // As other modal dialogs close (#385): the dialog lets go of focus,
        // and focus is taken once the dialog has closed, by the action's
        // target now or by the close event a task later.
        releaseDialogFocus(dialog);
        dialog.close(answer);
        if (closing) state.checks.push(() => checkFocus(answer !== "confirm"));
        if (answer === "confirm" && !(target?.isConnected && focusAfterDialog(target))) returnFocus();
        return;
      }
      closeDialog(dialog, { opener, target, place, value: answer });
      if (closing) state.checks.push(() => checkFocus(false));
    }
    async function accept(event) {
      event.preventDefault();
      if (opening.pending || opening.settled) return;
      let target = null;
      if (onConfirm) {
        opening.pending = true;
        const status = dialogStatusLine(dialog, "confirm-status");
        confirmButton.setAttribute("aria-busy", "true");
        const stopPending = sayIfStillPending((text) => {
          if (showing()) status.textContent = text;
        }, pendingText);
        try {
          target = await onConfirm();
        } catch (_) {
          target = null;
        } finally {
          stopPending();
          if (showing()) {
            status.textContent = "";
            confirmButton.removeAttribute("aria-busy");
          }
          opening.pending = false;
        }
        // Taken out of the page meanwhile.
        if (opening.settled) return;
      }
      finish("confirm", target);
    }
    function cancel(event) {
      event.preventDefault();
      if (opening.pending) return;
      finish("cancel", null);
    }
    function chooseAlternate(event) {
      event.preventDefault();
      if (opening.pending) return;
      finish("alternate", null);
    }
    // Escape over another modal dialog: held while the action runs;
    // otherwise this opening answers Cancel and closes the dialog itself,
    // letting go of focus first (#385).
    function escape(event) {
      event.preventDefault();
      if (opening.pending) return;
      finish("cancel", null);
    }
    for (const [target, type, listener, capture] of listeners) {
      target?.addEventListener(type, listener, capture);
    }
    dialog.querySelector("form")?.setAttribute("role", "none");
    cancelButton?.parentElement?.append(cancelButton);
    // The browser focuses Cancel itself as the dialog opens, not the first
    // button, the confirm one, which a screen reader would read first.
    cancelButton?.setAttribute("autofocus", "");
    // Escape cancels, held while the action runs.  A dialog taken out of
    // the page fires no close event, so it is answered No and let go of.
    ({ release } = openDialog(dialog, {
      home: cancelButton,
      onEscape: () => { if (!opening.pending) finish("cancel", null); },
      onGone: () => settle("cancel"),
    }));
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
  try {
    return await ask(doc, { title, message, confirmLabel: acceptLabel }, alternate,
      (answer) => (answer === "confirm" ? "accept" : answer === "alternate" ? "alternate" : null));
  } finally {
    alternate.remove();
    actions.append(acceptButton, cancelButton);
    cancelButton.textContent = originalCancel;
  }
}
