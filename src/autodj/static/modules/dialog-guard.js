import { focusAfterDialog, focusSteadyPlace, releaseDialogFocus } from "./dom-helpers.js";

// A <dialog> that acts modal without showModal() (#602, #627, #631).
//
// NVDA reads a web dialog in a browse-mode document of its own.  Behind a
// showModal() dialog the browser hides the rest of the page from assistive
// technology, so NVDA's page document lost its place, and as the dialog
// closed NVDA read its first line, "Skip to main content", before the
// control focus returned to.  Opened with show() and aria-modal="true", the
// page stays in the accessibility tree and NVDA returns to where it was.
// The page is not made inert or aria-hidden either, which would hide it
// from NVDA again, so a screen reader can still read it behind the dialog.
// While the dialog is open, nothing on the page can be used:
//   - a backdrop over the page takes pointer presses and does nothing;
//   - Tab and Shift+Tab go round the dialog's controls;
//   - focus that moves to the page comes back to the dialog's home control;
//   - click, auxclick, pointerdown, mousedown, Enter and Space, submit and
//     typed input aimed at the page are stopped before the page sees them,
//     and focus goes back to the home control.  This covers a screen reader
//     that ignores aria-modal and activates a page control (TalkBack,
//     Narrator), and Enter on a link found with find in page.  Input events
//     a script sends the page itself still reach it;
//   - Escape fires the dialog's cancel event, as the browser does for a
//     modal dialog, and closes it unless a listener prevents that.
// Keyboard shortcuts already stand down while a dialog is open
// (hotkeys.js).
//
// Dialogs opened this way stack: one opened from another (a Browse… dialog
// from an editor, a confirmation over an editor) sits above it, and only
// the top one guards; the dialog under it is page to it.  Opened over a
// dialog that is modal, whose page is inert, a dialog opens with
// showModal() instead.
//
// Each dialog puts its safe button (Cancel, Keep editing) last.  On
// opening, NVDA reads the dialog through and leaves its browse-mode cursor
// on the last button it read, where Enter acts (#627).

// Guarded dialogs per document, bottom first.
const stacks = new WeakMap();
// Dialogs opened with showModal() by openDialog.
const modals = new WeakSet();

// What a press, key or submission aimed at the page sends while a guarded
// dialog is open.
const PAGE_EVENTS = ["click", "auxclick", "pointerdown", "mousedown", "keydown", "submit", "input"];

// Controls Tab stops on.
const TABBABLE = [
  "a[href]", "area[href]", "button", "input:not([type='hidden'])", "select", "textarea",
  "summary", "iframe", "audio[controls]", "video[controls]", "[contenteditable]", "[tabindex]",
].join(", ");

function stackFor(doc) {
  let stack = stacks.get(doc);
  if (!stack) {
    stack = [];
    stacks.set(doc, stack);
  }
  return stack;
}

// Whether a modal dialog other than *dialog* is open.  The page behind it
// is inert, so *dialog* must open in the top layer above it.  A dialog this
// module opened with show() is not modal.
export function otherModalOpen(doc, dialog) {
  const guarded = new Set(stackFor(doc).map((entry) => entry.dialog));
  for (const other of doc.querySelectorAll("dialog[open]")) {
    if (other === dialog || guarded.has(other)) continue;
    try {
      if (other.matches(":modal")) return true;
    } catch (_) {
      // A browser without :modal; an open dialog is then taken as modal.
      return true;
    }
  }
  return false;
}

// The shade over the page while a guarded dialog is open.
function backdropFor(doc) {
  let backdrop = doc.getElementById("dialog-backdrop");
  if (!backdrop) {
    backdrop = doc.createElement("div");
    backdrop.id = "dialog-backdrop";
    backdrop.hidden = true;
    doc.body.append(backdrop);
  }
  return backdrop;
}

// The backdrop sits just under the top dialog, and each dialog over the
// one it was opened from.
function layout(doc) {
  const shown = stackFor(doc).filter((entry) => entry.dialog.isConnected && entry.dialog.open);
  const backdrop = doc.getElementById("dialog-backdrop");
  shown.forEach((entry, index) => { entry.dialog.style.zIndex = String(10001 + 2 * index); });
  if (!backdrop) return;
  backdrop.hidden = shown.length === 0;
  backdrop.style.zIndex = shown.length ? String(10000 + 2 * (shown.length - 1)) : "";
}

function tabbable(dialog) {
  return [...dialog.querySelectorAll(TABBABLE)].filter((control) => !control.disabled
    && control.getAttribute("tabindex") !== "-1"
    && !control.closest("[hidden], [inert]")
    && !(control.closest("details:not([open])") && control.tagName !== "SUMMARY")
    && (typeof control.checkVisibility !== "function" || control.checkVisibility()));
}

// Opens *dialog* with show() and keeps the page out of reach while it is
// open, or with showModal() over a modal dialog.  *home* is where stray
// focus goes back to; *onEscape*, when given, replaces the cancel event
// Escape fires; *onGone* is called when the dialog is found taken out of
// the page, which fires no close event.  Returns { modal, release }:
// release() lets go of the page, as closeDialog does.
export function openDialog(dialog, { home = null, onEscape = null, onGone = null } = {}) {
  const doc = dialog.ownerDocument;
  dialog.setAttribute("aria-modal", "true");
  if (otherModalOpen(doc, dialog)) {
    modals.add(dialog);
    dialog.showModal();
    return { modal: true, release() {} };
  }
  modals.delete(dialog);
  const stack = stackFor(doc);
  // A dialog opened again replaces its last opening.
  for (const entry of stack.filter((item) => item.dialog === dialog)) entry.release();
  const backdrop = backdropFor(doc);
  let tabBack = null;
  const entry = { dialog, release };
  const listeners = [
    [doc, "keydown", keys, true],
    [doc, "focusin", keepFocusIn],
    [backdrop, "mousedown", stayPut],
    [backdrop, "click", stayPut],
    [dialog, "close", closed],
    ...PAGE_EVENTS.map((type) => [doc, type, keepPageOut, true]),
  ];
  const goHome = () => (home?.isConnected && !home.disabled ? home : tabbable(dialog)[0])?.focus();
  // Whether this dialog keeps the page out of reach: it is open, in the
  // page and on top, and no modal dialog has opened over it.
  function guarding() {
    if (!dialog.isConnected) {
      release();
      onGone?.();
      return false;
    }
    if (!dialog.open) {
      release();
      return false;
    }
    const top = stack.findLast((item) => item.dialog.isConnected && item.dialog.open);
    return top === entry && !otherModalOpen(doc, dialog);
  }
  function keepPageOut(event) {
    if (!guarding()) return;
    const target = event.target;
    if (target === backdrop || (target?.nodeType && dialog.contains(target))) return;
    if (event.type === "keydown" && event.key !== "Enter" && event.key !== " ") return;
    if (event.type === "input" && !event.isTrusted) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    goHome();
  }
  function keys(event) {
    if (!guarding()) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (onEscape) {
        onEscape();
        return;
      }
      const cancel = new Event("cancel", { cancelable: true });
      if (dialog.dispatchEvent(cancel)) closeDialog(dialog);
      return;
    }
    if (event.key !== "Tab") return;
    tabBack = event.shiftKey;
    const stops = tabbable(dialog);
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
  // Focus that leaves the dialog for the page comes back: round to the
  // other end after Tab or Shift+Tab, else to the home control.
  function keepFocusIn(event) {
    if (!guarding()) return;
    if (dialog.contains(event.target)) {
      tabBack = null;
      return;
    }
    const stops = tabBack === null ? [] : tabbable(dialog);
    const wrap = tabBack ? stops[stops.length - 1] : stops[0];
    tabBack = null;
    if (wrap) wrap.focus();
    else goHome();
  }
  // Closed some other way than closeDialog: the page is let go of.
  function closed() {
    if (!dialog.open) release();
  }
  function stayPut(event) {
    event.preventDefault();
    event.stopPropagation();
  }
  function release() {
    const index = stack.indexOf(entry);
    if (index === -1) return;
    stack.splice(index, 1);
    for (const [target, type, listener, capture] of listeners) target.removeEventListener(type, listener, capture);
    layout(doc);
  }
  for (const [target, type, listener, capture] of listeners) target.addEventListener(type, listener, capture);
  stack.push(entry);
  dialog.show();
  layout(doc);
  return { modal: false, release };
}

// Closes *dialog* with focus going straight from it to *target*, or back to
// *opener*, or somewhere steady in *place* when the opener is gone; never
// through the page body.  As a dialog closes with focus in it, the browser
// itself puts focus back on the control that opened it, which is right for
// Cancel and Escape, and focus already there stays.  When focus belongs
// somewhere else, the opener is made inert while the dialog closes, so the
// browser has nothing to put back.  A dialog opened with showModal() lets
// go of focus first, as other modal dialogs do (#385).  With no opener,
// focus the browser put somewhere stays.  *handOff*: the caller moves focus
// on in the same task, so the opener is held inert and focus is not moved
// here.
export function closeDialog(dialog, {
  opener = null, target = null, place = null, value, handOff = false,
} = {}) {
  const doc = dialog.ownerDocument;
  for (const entry of stackFor(doc).filter((item) => item.dialog === dialog)) entry.release();
  const modal = modals.has(dialog);
  const elsewhere = handOff || (Boolean(target?.isConnected) && target !== opener);
  const hold = !modal && elsewhere && opener?.isConnected && !opener.hasAttribute("inert");
  if (modal) releaseDialogFocus(dialog);
  if (hold) opener.setAttribute("inert", "");
  try {
    if (dialog.open) dialog.close(value);
  } finally {
    if (hold) opener.removeAttribute("inert");
  }
  if (handOff || (elsewhere && focusAfterDialog(target))) return;
  const active = doc.activeElement;
  if (!opener) {
    if (!active || active === doc.body || dialog.contains(active)) focusSteadyPlace(doc, place);
    return;
  }
  if (!modal && active === opener) return;
  if (!focusAfterDialog(opener)) focusSteadyPlace(doc, place);
}
