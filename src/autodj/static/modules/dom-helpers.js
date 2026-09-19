// Pure DOM / formatting helpers shared across modules.

import { showVisibleStatus } from "./live-region.js";

// ----------------------------------------------------------------
// Debug logging — opt-in via `?debug=1` URL param OR
// localStorage.autodjDebug = "1".  Off by default; calls become no-ops.
// Use dbg("message", payload) for breadcrumbs at key state
// transitions (crossfade, advance, skip, prefetch, seek, repeat-window
// alerts).  Goes through console.log with a [autodj] prefix so it's
// easy to filter in DevTools.
// ----------------------------------------------------------------

// Lazy-evaluated so module import does not touch `localStorage`.
// Node's native webstorage runtime (used under jsdom and accessed by
// vitest's environment shim) emits a noisy
// "--localstorage-file was provided without a valid path" warning the
// first time anything reads localStorage, regardless of try/catch.
// Module init now stays clean; the cost is one extra function call
// per debug-checked code path (negligible).
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
  if (!sec || isNaN(sec)) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function fmtTrack(t) {
  if (!t) return "—";
  if (t.artist && t.title) return `${t.artist} — ${t.title}`;
  return t.display_name || t.title || "Unknown";
}

export function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ----------------------------------------------------------------
// Text-entry controls keep their keys. The hotkeys module separately
// preserves native activation, navigation, and dropdown typeahead while
// allowing unrelated letter shortcuts from buttons and sliders.
// ----------------------------------------------------------------

// ----------------------------------------------------------------
// Screen-reader announcements via ARIA live region #sr-status.
// Clear-then-set pattern lets the same message be re-announced on
// repeated key press.  300 ms window is long enough for polite
// announcement to fire; short enough that a second keypress clears
// and re-sets the text.
// ----------------------------------------------------------------

let _srTimer = null;

export function srSpeak(msg) {
  const el = document.getElementById("sr-status");
  if (!el) return;
  // Query hotkeys answer out loud; show the same answer so a sighted
  // keyboard user gets the reply too.  The mirror is aria-hidden, so
  // this is still one announcement.
  showVisibleStatus(msg);
  clearTimeout(_srTimer);
  el.textContent = "";
  setTimeout(() => {
    el.textContent = msg;
    _srTimer = setTimeout(() => { el.textContent = ""; }, 300);
  }, 0);
}

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
