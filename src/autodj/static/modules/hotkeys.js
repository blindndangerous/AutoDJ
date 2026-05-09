// Keyboard shortcuts -- YouTube-style transport control.
//
// Active when focus is on the page chrome (NVDA focus mode passes keys
// through to the browser, so these work alongside screen-reader users).
// Skipped when typing in inputs / contenteditable.
//
// Window + capture-phase listener so hotkeys fire even when a focused
// element (button, slider, custom widget) would otherwise consume the
// keydown event.  YouTube uses the same pattern.
//
// Key-held latch: NVDA (and some IMEs) forward auto-repeat keydowns
// without setting KeyboardEvent.repeat, so the e.repeat guard alone
// missed bursts.  Track every physical keydown until its keyup; a
// second keydown for the same key is suppressed regardless of the
// repeat flag.
//
// Scope: transport hotkeys (Space/K/N/S/M/Arrow keys) only fire when
// the Now Playing tab is visible.  ? (open shortcuts dialog) fires
// from any tab so the rule remains discoverable.

import { isTypingTarget } from "./dom-helpers.js";

export function toggleShortcutsModal() {
  const modal = document.getElementById("hotkey-help-modal");
  if (!modal) return false;
  if (modal.open) {
    modal.close();
  } else {
    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      // Older browsers: fall back to the open attribute (no focus trap).
      modal.setAttribute("open", "");
    }
    // Focus the Close button explicitly -- browsers vary on default focus.
    const closeBtn = document.getElementById("btn-shortcuts-close");
    if (closeBtn) {
      try { closeBtn.focus(); } catch (_) {}
    }
  }
  return true;
}

const _pressed = new Set();

export function installHotkeys({ btnPause, btnSkip, btnShuffle, btnMute, volSlider, seekDelta, getBpm }) {
  window.addEventListener("keyup", (e) => {
    _pressed.delete(e.key);
    // Modifier-aware aliases -- e.g. Shift held on "?" produces "?",
    // but releasing the letter without releasing Shift drops the
    // lower-case sibling too.  Cheap to clear both.
    if (e.key && e.key.length === 1) {
      _pressed.delete(e.key.toLowerCase());
      _pressed.delete(e.key.toUpperCase());
    }
  }, true);

  // Window blur clears the latch -- otherwise alt-tabbing while a key
  // is held would leave it permanently flagged as pressed.
  window.addEventListener("blur", () => _pressed.clear());

  window.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    if (_pressed.has(e.key)) return;
    _pressed.add(e.key);
    if (isTypingTarget(e.target)) return;

    const nowPanel = document.getElementById("panel-now");
    const nowVisible = nowPanel && !nowPanel.hasAttribute("hidden");
    if (!nowVisible && e.key !== "?") return;

    const modal = document.getElementById("hotkey-help-modal");
    if (modal && modal.open && modal.contains(e.target)) {
      if (e.key === "?") {
        e.preventDefault();
        toggleShortcutsModal();
      }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    // Tablist owns its own arrow-key navigation (APG roving tabindex).
    const targetIsTab =
      e.target && e.target.getAttribute && e.target.getAttribute("role") === "tab";

    const key = e.key;
    let bumpVol = 0;
    switch (key) {
      case " ":
      case "Spacebar":
      case "k":
      case "K":
        if (btnPause) btnPause.click();
        break;
      case "n":
      case "N":
        if (btnSkip) btnSkip.click();
        break;
      case "s":
      case "S":
        if (btnShuffle) btnShuffle.click();
        break;
      case "m":
      case "M":
        if (btnMute) btnMute.click();
        break;
      case "ArrowUp":
        if (targetIsTab) return;
        bumpVol = +5;
        break;
      case "ArrowDown":
        if (targetIsTab) return;
        bumpVol = -5;
        break;
      case ",": {
        if (!seekDelta) return;
        const bpm = getBpm ? getBpm() : 0;
        const measureSec = bpm > 0 ? (4 * 60) / bpm : 5.0;
        seekDelta(-measureSec);
        break;
      }
      case ".": {
        if (!seekDelta) return;
        const bpm2 = getBpm ? getBpm() : 0;
        const measureSec2 = bpm2 > 0 ? (4 * 60) / bpm2 : 5.0;
        seekDelta(measureSec2);
        break;
      }
      case "?":
        if (!toggleShortcutsModal()) return;
        break;
      default:
        return;
    }

    if (bumpVol !== 0 && volSlider) {
      const cur = parseInt(volSlider.value, 10);
      const next = Math.max(0, Math.min(100, cur + bumpVol));
      if (next !== cur) {
        volSlider.value = String(next);
        // Synthesize an input event so the existing listener does the
        // gain ramp + server POST + announce in one place.
        volSlider.dispatchEvent(new Event("input"));
      }
    }

    // Always swallow the key when we matched one -- prevents the page
    // from scrolling on Space, etc.
    e.preventDefault();
  });
}
