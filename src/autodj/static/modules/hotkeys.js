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
// Scope: every shortcut works from any tab except Space and the Up and
// Down arrows, which scroll the page on the other tabs and so only act
// while Now Playing is showing.  Text fields and dropdowns keep all keys.
//
// Key conflicts: lowercase k = pause, uppercase K (Shift+K) = speak key.
// Lowercase n = skip, uppercase N (Shift+N) = speak next track.
// Shift+L speaks the current lyric line; lowercase l is unused.
// s/S and m/M are shuffle and mute in either case, so no status key may
// use them.  Status keys: T N R B K L E V Q J.
//
// WCAG 2.1.4: single-key shortcuts can be switched off with the
// Settings > Keyboard checkbox.  The choice is kept per browser in
// localStorage and defaults to on; storage that is blocked or throws
// simply means "on" and an unsaved choice.

import { fmtDurationWords, fmtTime, isTypingTarget, srSpeak } from "./dom-helpers.js";

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

const TYPEAHEAD_SELECTOR = [
  "select",
  '[role="combobox"]',
  '[role="listbox"]',
  '[role="menu"]',
  '[role="menuitem"]',
  '[role="option"]',
].join(",");

// Whether the element a key was pressed on keeps that key.  Select-like
// widgets own printable-key typeahead as well as their navigation keys;
// other controls own Space and the arrows.
function _shouldDeferToNativeKeyboard(el, key) {
  if (!el) return false;
  if (el.closest(TYPEAHEAD_SELECTOR)) return true;
  return (key === " " || key.startsWith("Arrow")) && el.closest(NATIVE_KEYBOARD_SELECTOR) !== null;
}

// "Artist, Title": a comma, because NVDA with punctuation off says
// nothing for a dash and runs the two names together.
function _spokenTrack(t) {
  if (t.artist && t.title) return `${t.artist}, ${t.title}`;
  return t.display_name || t.title || "Unknown track";
}

function _queueSummary(queue) {
  if (!queue || queue.length === 0) return "Queue empty";
  const count = queue.length === 1 ? "1 track queued" : `${queue.length} tracks queued`;
  return `${count}. First, ${_spokenTrack(queue[0])}`;
}

export function toggleShortcutsModal() {
  const modal = document.getElementById("hotkey-help-modal");
  if (!modal) return false;
  if (modal.open) {
    modal.close();
  } else {
    modal.showModal();
    // Focus the Close button explicitly -- browsers vary on default focus.
    const closeBtn = document.getElementById("btn-shortcuts-close");
    if (closeBtn) {
      try { closeBtn.focus(); } catch (_) {}
    }
  }
  return true;
}

const SHORTCUTS_STORAGE_KEY = "autodj.keyboardShortcuts";
let _shortcutsOn = null;

// Read lazily, never at import: touching localStorage at module load is
// what dom-helpers.js avoids too.
function shortcutsEnabled() {
  if (_shortcutsOn === null) {
    try {
      _shortcutsOn = globalThis.localStorage?.getItem(SHORTCUTS_STORAGE_KEY) !== "off";
    } catch (_) {
      _shortcutsOn = true;
    }
  }
  return _shortcutsOn;
}

function setShortcutsEnabled(on) {
  _shortcutsOn = Boolean(on);
  try {
    globalThis.localStorage?.setItem(SHORTCUTS_STORAGE_KEY, on ? "on" : "off");
  } catch (_) {
    // Still honoured for this page load; it just will not be remembered.
  }
}

function installShortcutToggle(checkbox) {
  if (!checkbox) return;
  checkbox.checked = shortcutsEnabled();
  checkbox.addEventListener("change", () => setShortcutsEnabled(checkbox.checked));
}

const _pressed = new Set();

export function installHotkeys({
  btnSkip, btnShuffle, btnMute, volSlider,
  seekDelta, getBpm,
  getTrack, getNextTrack, getRemaining, getLyricLine,
  getPosition, getQueue, getJobStatus,
  isEnabled = () => true,
  shortcutToggle = null,
  // What Space / k do: Play / Pause, or in stream mode start or stop
  // listening on this page.
  togglePlay,
}) {
  installShortcutToggle(shortcutToggle);
  const active = () => isEnabled() && shortcutsEnabled();
  // The latch only has to stop a held key from firing again before it
  // is released, so any keyup clears all of it.  Keys NVDA passes
  // through arrive with keyup.key "Unidentified", so clearing only the
  // released key left m latched after its first press.
  window.addEventListener("keyup", () => _pressed.clear(), true);

  // Window blur and hiding the tab clear the latch too -- otherwise
  // alt-tabbing while a key is held would leave it flagged as pressed.
  window.addEventListener("blur", () => _pressed.clear());
  document.addEventListener("visibilitychange", () => _pressed.clear());

  window.addEventListener("keydown", (e) => {
    if (e.repeat) return;

    // Do not latch keys pressed while a dialog is open.  If a keyup is
    // missed as the dialog closes, it must not suppress the next page key.
    // The page has no shadow DOM, so the target is where the key went.
    const el = e.target instanceof Element ? e.target : null;
    if (isTypingTarget(el)) return;
    // Every dialog here opens with showModal(), so the page behind it is
    // inert: while one shows (the shortcut list, a confirmation such as
    // Clear queue or Delete liner, sign in) no shortcut acts on the page.
    // Only ? works, to close the shortcut list.
    const modal = document.getElementById("hotkey-help-modal");
    if (document.querySelector("dialog[open]")) {
      if (modal && modal.open && el && modal.contains(el)
          && e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey
          && !_pressed.has(e.key) && active()) {
        _pressed.add(e.key);
        e.preventDefault();
        toggleShortcutsModal();
      }
      return;
    }

    if (_shouldDeferToNativeKeyboard(el, e.key || "")) return;
    if (_pressed.has(e.key)) return;
    _pressed.add(e.key);
    if (!active()) return;

    const nowPanel = document.getElementById("panel-now");
    const nowVisible = nowPanel && !nowPanel.hasAttribute("hidden");
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    const key = e.key;
    const scrollsPage = key === " " || key === "ArrowUp" || key === "ArrowDown";
    if (!nowVisible && scrollsPage) return;

    let bumpVol = 0;
    switch (key) {
      case " ":
      case "k":
        togglePlay();
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
        srSpeak(rem != null ? fmtDurationWords(rem) : "Position unknown");
        break;
      }
      case "B": {
        const bpm = getBpm && getBpm();
        srSpeak(bpm > 0 ? `${Math.round(bpm)} BPM` : "BPM unknown");
        break;
      }
      case "K": {
        const tk = getTrack && getTrack();
        // key_spoken is the key in words ("F sharp minor"); the display
        // label ("F#m") reads as "F number m" to a screen reader.
        srSpeak(tk && tk.key_spoken ? tk.key_spoken : "Key unknown");
        break;
      }
      case "L":
        srSpeak(getLyricLine ? getLyricLine() : "No lyrics for this track.");
        break;
      case "E": {
        const pos = getPosition && getPosition();
        // The seek slider's own wording, so the two never disagree.
        srSpeak(pos ? `${fmtTime(pos.elapsed)} of ${fmtTime(pos.duration)}` : "Position unknown");
        break;
      }
      case "V": {
        if (!volSlider) return;
        const muted = btnMute && btnMute.getAttribute("aria-pressed") === "true";
        srSpeak(`Volume ${volSlider.value} percent${muted ? ", muted" : ""}`);
        break;
      }
      case "Q":
        srSpeak(_queueSummary(getQueue && getQueue()));
        break;
      case "J":
        srSpeak(getJobStatus ? getJobStatus() : "No library job has run.");
        break;
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
