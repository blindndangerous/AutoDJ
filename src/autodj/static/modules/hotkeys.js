// Hotkeys module — global keydown listener wiring.
//
// Single window-level capture-phase listener.  Earlier code attached a
// duplicate document-level listener too, which made every press fire
// twice (Space pause-toggled twice; M cycled mute on/off the same
// instant; etc).  Only one listener now, on `window`, in the capture
// phase.  Lets the modal block its own bubble before the rest fires.
//
// Key-held latch: NVDA (and some IMEs) forward auto-repeat keydowns
// without setting KeyboardEvent.repeat, so the e.repeat guard alone
// missed bursts.  Track every physical keydown until its keyup; a
// second keydown for the same key is suppressed regardless of the
// repeat flag.
//
// Scope: transport hotkeys only fire when the Now Playing tab is
// visible.  Status keys and ? work from any tab.
//
// Key conflicts: lowercase k = pause, uppercase K (Shift+K) = speak key.
// Lowercase n = skip, uppercase N (Shift+N) = speak next track.

import { isTypingTarget, srSpeak } from "./dom-helpers.js";

const NATIVE_KEYBOARD_SELECTOR = [
  "button",
  "input",
  "select",
  "textarea",
  "a[href]",
  "summary",
  '[contenteditable="true"]',
  '[role="button"]',
  '[role="slider"]',
  '[role="spinbutton"]',
  '[role="combobox"]',
  '[role="listbox"]',
  '[role="menuitem"]',
  '[role="option"]',
  '[role="switch"]',
  '[role="tab"]',
].join(",");

export function ownsNativeKeyboardBehavior(target) {
  if (!target || target.nodeType !== 1 || typeof target.closest !== "function") {
    return false;
  }
  try {
    return target.closest(NATIVE_KEYBOARD_SELECTOR) !== null;
  } catch (_) {
    return false;
  }
}

const TYPEAHEAD_SELECTOR = [
  "select",
  '[role="combobox"]',
  '[role="listbox"]',
  '[role="menu"]',
  '[role="menuitem"]',
  '[role="option"]',
].join(",");

function _eventPath(event) {
  const path = event.target ? [event.target] : [];
  if (typeof event.composedPath !== "function") return path;
  try {
    const composedPath = event.composedPath();
    if (Array.isArray(composedPath)) {
      for (const target of composedPath) {
        if (!path.includes(target)) path.push(target);
      }
    }
  } catch (_) {
    // The event target is still useful when a host rejects composedPath().
  }
  return path;
}

function _eventPathMatches(event, predicate) {
  return _eventPath(event).some(predicate);
}

function _eventTargetIsTyping(event) {
  return _eventPathMatches(event, isTypingTarget);
}

function _eventTargetOwnsTypeahead(event) {
  return _eventPathMatches(event, (target) => {
    if (!target || target.nodeType !== 1 || typeof target.closest !== "function") {
      return false;
    }
    try {
      return target.closest(TYPEAHEAD_SELECTOR) !== null;
    } catch (_) {
      return false;
    }
  });
}

function _shouldDeferToNativeKeyboard(event) {
  // Select-like widgets own printable-key typeahead as well as their
  // navigation keys.  Do not steal any of those events for app shortcuts.
  if (_eventTargetOwnsTypeahead(event)) return true;

  const key = event.key || "";
  const nativeKey = key === " " || key === "Spacebar" || key.startsWith("Arrow");
  return nativeKey
    && _eventPathMatches(event, ownsNativeKeyboardBehavior);
}

function _eventIsWithin(event, element) {
  if (event.target && element.contains(event.target)) return true;
  return _eventPath(event).includes(element);
}

function _fmtRemaining(sec) {
  const s = Math.max(0, Math.round(sec));
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  if (mins === 0) return `${secs} second${secs === 1 ? "" : "s"}`;
  const minStr = `${mins} minute${mins === 1 ? "" : "s"}`;
  if (secs === 0) return minStr;
  return `${minStr} ${secs} second${secs === 1 ? "" : "s"}`;
}

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

export function installHotkeys({
  btnPause, btnSkip, btnShuffle, btnMute, volSlider,
  seekDelta, getBpm,
  getTrack, getNextTrack, getRemaining,
  isEnabled = () => true,
}) {
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

    // Do not latch keys pressed inside the help dialog.  If a keyup is
    // missed as the dialog closes, it must not suppress the next page key.
    if (_eventTargetIsTyping(e)) return;
    const modal = document.getElementById("hotkey-help-modal");
    if (modal && modal.open && _eventIsWithin(e, modal)) {
      if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey
          && !_pressed.has(e.key) && isEnabled()) {
        _pressed.add(e.key);
        e.preventDefault();
        toggleShortcutsModal();
      }
      return;
    }

    if (_shouldDeferToNativeKeyboard(e)) return;
    if (_pressed.has(e.key)) return;
    _pressed.add(e.key);
    if (!isEnabled()) return;

    const nowPanel = document.getElementById("panel-now");
    const nowVisible = nowPanel && !nowPanel.hasAttribute("hidden");
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    const key = e.key;
    const statusKey = ["T", "N", "R", "B", "K"].includes(key);
    if (!nowVisible && !statusKey && key !== "?") return;

    let bumpVol = 0;
    switch (key) {
      case " ":
      case "Spacebar":
      case "k":
        if (btnPause) btnPause.click();
        break;
      case "n":
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
        bumpVol = +5;
        break;
      case "ArrowDown":
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
      // Status keys -- Shift+letter, speak via aria live region.
      case "T": {
        const t = getTrack && getTrack();
        srSpeak(t ? `${t.artist || ""} - ${t.title || ""}`.trim() : "Nothing playing");
        break;
      }
      case "N": {
        const nx = getNextTrack && getNextTrack();
        if (nx) {
          const bpm = nx.bpm ? `, ${Math.round(nx.bpm)} BPM` : "";
          srSpeak(`${nx.artist || ""}, ${nx.title || ""}${bpm}`.trim());
        } else {
          srSpeak("No next track");
        }
        break;
      }
      case "R": {
        const rem = getRemaining && getRemaining();
        srSpeak(rem != null ? _fmtRemaining(rem) : "Position unknown");
        break;
      }
      case "B": {
        const bpm = getBpm && getBpm();
        srSpeak(bpm > 0 ? `${Math.round(bpm)} BPM` : "BPM unknown");
        break;
      }
      case "K": {
        const tk = getTrack && getTrack();
        srSpeak(tk && tk.key_label ? tk.key_label : "Key unknown");
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
  }, true);
}
