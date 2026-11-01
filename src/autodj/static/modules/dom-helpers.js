// Pure DOM / formatting helpers shared across modules.

import { announceStatus } from "./live-region.js";
import { tableFocusTarget } from "./table-row.js";

// ----------------------------------------------------------------
// Debug logging — opt-in via `?debug=1` URL param OR
// localStorage.autodjDebug = "1".  Off by default; calls become no-ops.
// Use dbg("message", payload) for breadcrumbs at key state
// transitions (crossfade, advance, skip, prefetch, seek, repeat-window
// alerts).  Goes through console.log with a [autodj] prefix so it's
// easy to filter in DevTools.
// ----------------------------------------------------------------

// Lazy-evaluated so module import does not touch `localStorage`; the
// cost is one extra function call per debug-checked code path.
let _debugCached = null;
export function isDebug() {
  if (_debugCached !== null) return _debugCached;
  // Reading `globalThis.localStorage` throws SecurityError outright when
  // the browser blocks storage, so `?.` alone is not a guard.  The URL
  // check runs first, so `?debug=1` short-circuits before storage is
  // touched at all.
  try {
    _debugCached = new URLSearchParams(location.search).get("debug") === "1"
      || globalThis.localStorage?.getItem("autodjDebug") === "1";
  } catch (_) {
    _debugCached = false;
  }
  return _debugCached;
}

export function dbg(...args) {
  if (!isDebug()) return;
  console.log("[autodj]", ...args);
}

// ----------------------------------------------------------------
// Formatting
// ----------------------------------------------------------------

export function fmtTime(sec) {
  if (!Number.isFinite(Number(sec)) || sec <= 0) return "0:00";
  const h = Math.floor(sec / 3600);
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  const minutes = h ? `${h}:${String(m % 60).padStart(2, "0")}` : String(m);
  return `${minutes}:${String(s).padStart(2, "0")}`;
}

export function fmtTrack(t) {
  if (!t) return "—";
  if (t.artist && t.title) return `${t.artist} — ${t.title}`;
  return t.display_name || t.title || "Unknown";
}

// A lone dash that stands for "no value".  NVDA says nothing for "—"
// below its "most" punctuation level, so a screen reader gets `words`
// instead and the dash stays on screen.
export function setNoValue(el, words) {
  el.innerHTML = `<span aria-hidden="true">—</span><span class="visually-hidden">${escHtml(words)}</span>`;
}

export function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Query hotkeys answer out loud through #sr-status, and the same answer
// shows in the visible mirror.  Forced, so asking twice is answered
// twice; the region empties 300 ms later.
export function srSpeak(msg) {
  announceStatus(document.getElementById("sr-status"), msg, { force: true, dwellMs: 300 });
}

// ----------------------------------------------------------------
// Text-entry controls keep their keys. The hotkeys module separately
// preserves native activation, navigation, and dropdown typeahead while
// allowing unrelated letter shortcuts from buttons and sliders.
// ----------------------------------------------------------------

export function isTypingTarget(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === "TEXTAREA") return true;
  if (tag === "INPUT") {
    const t = (el.type || "text").toLowerCase();
    return [
      "text", "search", "email", "url", "password", "number",
      "tel", "date", "datetime-local", "month", "time", "week",
    ].includes(t);
  }
  return false;
}

// A search field whose text is selected when focus arrives by keyboard or
// is put back by the page (after a search, or on return from its results),
// so typing replaces the old search.  A click into the field still puts
// the caret where it lands.
export function selectOnFocus(input) {
  if (!input) return;
  let pointer = false;
  input.addEventListener("pointerdown", () => {
    pointer = input.ownerDocument.activeElement !== input;
  });
  input.addEventListener("focus", () => {
    if (!pointer && input.value) {
      input.setSelectionRange(0, input.value.length);
      // Selecting during focus stops WebKit scrolling the field into view.
      input.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    }
    pointer = false;
  });
}

// Focus *control*, first opening every closed <details> card around it.
// Focus cannot reach a field inside a closed card, so a refused value's
// error would be said with nothing in view and focus left where it was.
// A card's own summary is reachable while the card is closed, so focusing
// a summary leaves its card as it is.
export function focusRevealed(control, options) {
  if (!control) return;
  let card = (control.tagName === "SUMMARY" ? control.parentElement : control)
    ?.parentElement?.closest("details");
  while (card) {
    if (!card.open) card.open = true;
    card = card.parentElement?.closest("details");
  }
  control.focus(options);
}

// Keep field errors associated with their control without dropping its help.
export function clearFieldError(control, error) {
  if (Array.isArray(control)) {
    control.forEach((field) => clearFieldError(field, error));
    return;
  }
  control?.removeAttribute("aria-invalid");
  if (!control || !error?.id) return;
  const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== error.id);
  if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
  else control.removeAttribute("aria-describedby");
  error.textContent = "";
  error.hidden = true;
}

export function clearFormErrors(form, error = null) {
  if (!form) return;
  const errors = error ? [error] : [...form.querySelectorAll(".field-error")];
  form.querySelectorAll("[aria-describedby]").forEach((control) => {
    let ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    for (const item of errors) ids = ids.filter((id) => id !== item.id);
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
    else control.removeAttribute("aria-describedby");
  });
  form.querySelectorAll("[aria-invalid='true']").forEach((control) => control.removeAttribute("aria-invalid"));
  errors.forEach((item) => {
    item.textContent = "";
    item.hidden = true;
  });
}

// *status* is the live region that says the error when focus does not move
// to the field (the focused field reads its own error), or a feature's
// announcer, called as status(message, "error") in that case.
export function showFieldError(control, message, error, status, { focus = true } = {}) {
  const controls = (Array.isArray(control) ? control : [control]).filter(Boolean);
  control = controls[0];
  error.textContent = message;
  error.hidden = false;
  const announce = () => {
    if (typeof status === "function") status(message, "error");
    else announceStatus(status, message, { force: true, tone: "error" });
  };
  if (!control) { announce(); return; }
  // The error comes first, so it is read before a field's longer help.
  for (const field of controls) {
    field.setAttribute("aria-invalid", "true");
    const ids = new Set([error.id, ...(field.getAttribute("aria-describedby") || "").split(/\s+/)].filter(Boolean));
    if (ids.size) field.setAttribute("aria-describedby", [...ids].join(" "));
  }
  const following = control.nextElementSibling;
  const label = control.closest("label") || (following?.htmlFor === control.id ? following : null);
  (label || control).after(error);
  const before = control.ownerDocument.activeElement;
  if (focus) focusRevealed(control);
  if (!focus || before === control || control.ownerDocument.activeElement !== control) announce();
}

// The server names its public field; each feature maps that key to its own
// controls. Unknown keys stay with the caller's ordinary failure message.
export function applyServerFieldError(error, fieldMap, errorNode, status, options) {
  if (!errorNode || !Object.hasOwn(fieldMap, error?.field)) return false;
  const mapped = fieldMap[error.field];
  const controls = (Array.isArray(mapped) ? mapped : [mapped])
    .map((field) => typeof field === "string" ? errorNode.ownerDocument.getElementById(field) : field)
    .filter(Boolean);
  if (!controls.length) return false;
  showFieldError(controls, error.message, errorNode, status, options);
  return true;
}

// ----------------------------------------------------------------
// Closing a modal <dialog> (#385).  The browser puts focus back on the
// control that opened a dialog while the dialog is still closing, when the
// page is still inert, and NVDA in Chrome then lost the focus and read the
// top of the page (the Station menu) though the control had focus.  The
// control in the dialog lets go of focus before the dialog closes, so the
// browser has nothing to put back, and the page focuses the control once
// the dialog is closed: a focus change NVDA reports.
// ----------------------------------------------------------------

// Before dialog.close(), or in its cancel event for Escape.
export function releaseDialogFocus(dialog) {
  const active = dialog?.ownerDocument?.activeElement;
  if (active && active !== dialog && dialog.contains(active)) active.blur();
}

// Once the dialog is closed.  A control the browser focused anyway lets go
// and takes focus again, so the change is still reported.
export function focusAfterDialog(control) {
  control = tableFocusTarget(control);
  // The page body takes no focus, so it never counts as a place to return to.
  if (!control?.isConnected || control.disabled || control === control.ownerDocument.body) return false;
  if (control.ownerDocument.activeElement === control) control.blur();
  control.focus();
  return control.ownerDocument.activeElement === control;
}

// A dialog's own status line, made on first use: the page's status regions
// are inert behind a modal dialog, so what is said while it is open is said
// here (#384, #332).
export function dialogStatusLine(dialog, id) {
  const doc = dialog.ownerDocument;
  let status = doc.getElementById(id);
  if (!status) {
    status = doc.createElement("p");
    status.id = id;
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.setAttribute("aria-atomic", "true");
    (dialog.querySelector("form") || dialog).append(status);
  }
  return status;
}

// The card or panel *control* is in, found as a dialog opens, so focus has
// somewhere steady to go if the control is gone when the dialog closes.
export function dialogReturnPlace(control) {
  return control?.closest?.("details, section[data-view]") || null;
}

// Where focus goes when the control it should return to is gone, turned
// off, hidden, or was the page itself: *place*'s summary, else its heading,
// else the main content.  Never the page body, where NVDA reads from the
// top of the page.
export function focusSteadyPlace(doc, place) {
  const shown = (element) => element?.isConnected && !element.closest("[hidden]");
  if (shown(place)) {
    const summary = place.tagName === "DETAILS" ? place.querySelector(":scope > summary") : null;
    if (summary && focusAfterDialog(summary)) return true;
    const heading = place.querySelector("h2, h3");
    if (shown(heading)) {
      if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
      if (focusAfterDialog(heading)) return true;
    }
  }
  return focusAfterDialog(doc.getElementById("main-content"));
}

// ----------------------------------------------------------------
// Settings lists: saved profiles, paired devices, voice liners.
// ----------------------------------------------------------------

// A button for one row, named for that row: "Delete" on screen, "Delete
// profile Early" to a screen reader.
export function rowButton(doc, label, name, onClick) {
  const button = doc.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("aria-label", name);
  button.addEventListener("click", () => onClick(button));
  return button;
}

// One row: the text in a span, then its buttons.
export function listRow(doc, text, textClass, ...buttons) {
  const row = doc.createElement("li");
  const span = doc.createElement("span");
  if (textClass) span.className = textClass;
  span.textContent = text;
  row.append(span);
  for (const button of buttons) row.append(" ", button);
  return row;
}

// After a row's delete, the control that gets focus: the same control in
// the row that took its place, else the last row's, else *fallback*.
export function nextRowControl(list, selector, index, fallback) {
  const controls = list.querySelectorAll(selector);
  return controls[Math.min(index, controls.length - 1)] || fallback;
}

// Puts *rows* in *list* in this order, in place, and removes everything
// else in it.  A row already there is moved, not drawn again, and the row
// that holds focus is never moved: the rows around it are.  A list drawn
// again from scratch destroyed the focused button, and NVDA then read a
// cut-off line of the list before the status message (#328).  The caller
// moves focus off a row that goes before calling this.
export function placeRows(list, rows) {
  const wanted = new Set(rows);
  for (const node of [...list.childNodes]) if (!wanted.has(node)) node.remove();
  const active = list.ownerDocument.activeElement;
  const pivot = rows.findIndex((row) => row.contains(active));
  if (pivot < 0) {
    let next = list.firstChild;
    for (const row of rows) {
      if (row !== next) list.insertBefore(row, next);
      next = row.nextSibling;
    }
    return;
  }
  // Back to front before the focused row, front to back after it.
  let anchor = rows[pivot];
  for (let i = pivot - 1; i >= 0; i -= 1) {
    if (rows[i].nextSibling !== anchor) list.insertBefore(rows[i], anchor);
    anchor = rows[i];
  }
  let previous = rows[pivot];
  for (let i = pivot + 1; i < rows.length; i += 1) {
    if (previous.nextSibling !== rows[i]) list.insertBefore(rows[i], previous.nextSibling);
    previous = rows[i];
  }
}

// Reuse direct-child rows by stable item key, update them in place, then
// order them without moving the row that currently contains focus.
export function reconcileRows(list, items, keyOf, createRow, updateRow, {
  fallback = null, onFocusLost = null, rowSelector = "li",
  controlSelector = ":scope > button:not(:disabled)", focusSelector = "button", focusControl = (active) => active,
} = {}) {
  const active = list.ownerDocument.activeElement;
  const focusedRow = active?.closest?.(rowSelector);
  const focusedIndex = focusedRow && focusedRow.parentElement === list ? [...list.children].indexOf(focusedRow) : -1;
  const focusedKey = focusedRow?.dataset.rowKey;
  const control = focusControl(active);
  const focusedAction = control?.dataset?.action || control?.dataset?.role;
  const focusedColumn = focusedRow ? [...focusedRow.querySelectorAll(focusSelector)].indexOf(control) : -1;
  const existing = new Map([...list.children]
    .filter((row) => row.dataset.rowKey !== undefined)
    .map((row) => [row.dataset.rowKey, row]));
  const rows = items.map((item, index) => {
    const key = String(keyOf(item));
    let row = existing.get(key);
    if (!row) row = createRow(item);
    if (row.dataset.rowKey !== key) row.dataset.rowKey = key;
    updateRow(row, item, index);
    return row;
  });
  placeRows(list, rows);
  const focused = focusedIndex >= 0 && (!active.isConnected || active.disabled);
  let target = null;
  if (focused) {
    const candidates = (row) => [...row.querySelectorAll(controlSelector)];
    const choose = (row) => {
      const buttons = candidates(row);
      return buttons.find((button) => (button.dataset.action || button.dataset.role) === focusedAction)
        || buttons[Math.min(Math.max(focusedColumn, 0), buttons.length - 1)] || buttons[0] || null;
    };
    const same = rows.find((row) => row.dataset.rowKey === focusedKey);
    target = same && choose(same);
    if (!target && rows.length) target = choose(rows[Math.min(focusedIndex, rows.length - 1)]);
    if (!target) target = typeof fallback === "function" ? fallback() : fallback;
    target?.focus?.();
    onFocusLost?.({ key: focusedKey, action: focusedAction, target });
  }
  return { rows, focusLost: focused, target };
}

// The station's calendar day at *now*, as YYYY-MM-DD, or "" when the page
// does not know the station time zone.  Pages left open past midnight use it
// to see that the station's day has changed (liners, carts).
export function stationDay(timeZone, now = new Date()) {
  if (!timeZone) return "";
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
      timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(now).map(({ type, value }) => [type, value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  } catch {
    return "";
  }
}
