// Pure DOM / formatting helpers shared across modules.

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
  try {
    const params = new URLSearchParams(location.search);
    if (params.get("debug") === "1") { _debugCached = true; return true; }
  } catch (_) {}
  try {
    if (typeof globalThis !== "undefined" &&
        globalThis.localStorage &&
        globalThis.localStorage.getItem("autodjDebug") === "1") {
      _debugCached = true;
      return true;
    }
  } catch (_) {}
  _debugCached = false;
  return false;
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
// Hotkey gate: suppress hotkeys ONLY when focus is on a text-entry
// control.  Earlier blanket-suppression on every INPUT/SELECT killed
// hotkeys whenever the user landed on the volume / EQ slider, the
// preset dropdown, etc.  Visible-button hotkeys (Space, M, N, S, ?)
// should still fire from those non-text controls.
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
