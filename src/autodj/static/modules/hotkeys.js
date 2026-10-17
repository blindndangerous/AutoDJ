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
// use them.  Status keys: T N R B K L E V Q J H U.
// Stations (#70): lowercase c moves focus to the station switcher while
// the server has several stations; Shift+C says which station the page
// controls.  With several stations the status keys about the station
// (T N R B K L E Q) start with its name.
//
// Reserved for the cart wall (modules/cart-wall.js), which handles them
// only while focus is on the wall: the digits, minus, equals, Page Up and
// Page Down.  No shortcut here may use them.
//
// Reserved for the voice tracker (modules/voice-tracker.js): Alt+R, only
// while the Voice tracks tab shows.  Every shortcut here ignores Alt, and
// Alt with an arrow (clockwheel steps) is the page's other Alt shortcut.
// Alt+R is free in Firefox, Chrome and Edge on Windows (no menu or
// browser command uses it; Firefox's menu bar keys are F E V S B T H),
// the page gives no element accesskey="r", and NVDA and JAWS pass it
// through to the page in browse and focus mode alike.
//
// Landmarks (#60, modules/landmarks.js): Left and Right bracket move to the
// previous and next landmark of the song -- the cue point preview's while
// Track info shows, else the song on air -- and say it; Shift+U says what is
// coming next on air.  No shortcut here or in Chrome, Edge, Firefox or
// Safari uses a bare bracket or Shift+U (Firefox's quick find keys are ' and
// /).  NVDA and JAWS have no focus-mode or forms-mode command on them; in
// browse mode they keep every printable key, as they do for the other
// shortcuts, so a key reaches the page in focus mode or after the
// pass-through key (NVDA+F2, JAWS Insert+3).  On layouts that type a
// bracket with AltGr (German, French, Nordic) the browser reports Ctrl+Alt,
// so a bracket the layout typed with AltGr is accepted: e.key is "[" or "]",
// AltGraph is down, and the physical key is not the bracket key itself.
// Ctrl+Alt on the US bracket keys stays the browser's and the screen
// reader's.  Outside Track info the brackets only say the landmark of the
// song on air unless "Bracket keys seek the song on air" (Settings,
// Keyboard; off by default, kept per browser like the toggle below) is on.
// Reserved for Talk (modules/live-talk.js, #48): Alt+M, held to talk over
// the music while the station can go live, from anywhere on the page.  It
// is hold-only: letting go always stops, so it never leaves a microphone
// on (only the Talk button latches, for 2 minutes at most).  It
// matches the M key by position (KeyM), so Option+M on a Mac works too,
// and never with Control or Meta, so AltGr+M (Control+Alt on Windows),
// which types a character on many layouts, is left alone.  Alt+M is free
// in Firefox, Chrome and Edge on Windows (Firefox's menu bar keys are
// F E V S B T H; Chrome and Edge give it no command), no element has
// accesskey="m", and NVDA and JAWS pass Alt+letter through to the page in
// browse and focus mode.  Plain m and M stay Mute: these shortcuts ignore
// Alt.  Not switched off by the Settings > Keyboard checkbox: WCAG 2.1.4
// covers single-character shortcuts, and this one needs Alt.
//
// WCAG 2.1.4: single-key shortcuts can be switched off with the
// Settings > Keyboard checkbox.  The choice is kept per browser in
// localStorage and defaults to on; storage that is blocked or throws
// simply means "on" and an unsaved choice.

import {
  dialogReturnPlace, fmtTime, focusAfterDialog, focusSteadyPlace, isTypingTarget,
  releaseDialogFocus, srSpeak,
} from "./dom-helpers.js";
import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

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
  // Table cells: arrows move between rows and cells (table-navigation.js).
  "td",
  '[role="gridcell"]',
  '[role="rowheader"]',
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

// Where focus goes back to when the shortcut list closes: the control that
// opened it, or somewhere steady near it.  The list lets go of focus before
// it closes, so the browser has nothing to put back; put back by the
// browser, focus was not reported and NVDA read the skip link instead.
let _shortcutsOpener = null;
let _shortcutsPlace = null;
let _shortcutsReturnPending = false;

function _returnShortcutsFocus(modal) {
  if (!_shortcutsReturnPending) return;
  _shortcutsReturnPending = false;
  const opener = _shortcutsOpener;
  const place = _shortcutsPlace;
  _shortcutsOpener = null;
  _shortcutsPlace = null;
  if (!focusAfterDialog(opener)) focusSteadyPlace(modal.ownerDocument, place);
}

// Escape closes the list natively: let go of focus as it starts to close,
// and return it once it has.
function _wireShortcutsModal(modal) {
  if (modal.dataset.focusReturn) return;
  modal.dataset.focusReturn = "on";
  modal.addEventListener("cancel", () => releaseDialogFocus(modal));
  modal.addEventListener("close", () => _returnShortcutsFocus(modal));
}

export function toggleShortcutsModal() {
  const modal = document.getElementById("hotkey-help-modal");
  if (!modal) return false;
  _wireShortcutsModal(modal);
  if (modal.open) {
    releaseDialogFocus(modal);
    modal.close();
    _returnShortcutsFocus(modal);
  } else {
    const active = document.activeElement;
    _shortcutsOpener = active && active !== document.body ? active : null;
    _shortcutsPlace = dialogReturnPlace(_shortcutsOpener);
    _shortcutsReturnPending = true;
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
export function shortcutsEnabled() {
  if (_shortcutsOn === null) {
    _shortcutsOn = readStorage(defaultStorage(), SHORTCUTS_STORAGE_KEY) !== "off";
  }
  return _shortcutsOn;
}

function setShortcutsEnabled(on) {
  _shortcutsOn = Boolean(on);
  writeStorage(defaultStorage(), SHORTCUTS_STORAGE_KEY, on ? "on" : "off");
}

function installShortcutToggle(checkbox) {
  if (!checkbox) return;
  checkbox.checked = shortcutsEnabled();
  checkbox.addEventListener("change", () => setShortcutsEnabled(checkbox.checked));
}

// "Bracket keys seek the song on air" (#60): off unless chosen, since a
// stray bracket would otherwise jump what listeners hear.
const BRACKET_SEEK_STORAGE_KEY = "autodj.bracketSeek";
let _bracketSeekOn = null;

export function bracketSeekEnabled() {
  if (_bracketSeekOn === null) {
    _bracketSeekOn = readStorage(defaultStorage(), BRACKET_SEEK_STORAGE_KEY) === "on";
  }
  return _bracketSeekOn;
}

function installBracketSeekToggle(checkbox) {
  if (!checkbox) return;
  checkbox.checked = bracketSeekEnabled();
  checkbox.addEventListener("change", () => {
    _bracketSeekOn = checkbox.checked;
    writeStorage(defaultStorage(), BRACKET_SEEK_STORAGE_KEY, checkbox.checked ? "on" : "off");
  });
}

const _pressed = new Set();

export function installHotkeys({
  btnSkip, btnShuffle, btnMute, volSlider,
  seekDelta, getBpm,
  getTrack, getNextTrack, getRemaining, getLyricLine,
  getPosition, getQueue, getJobStatus,
  // Shift+H: says how the hour lands, from the hour preview (#126).
  speakHour = null,
  isEnabled = () => true,
  shortcutToggle = null,
  // Stations (#70): focus the switcher (false when there is none), the
  // sentence Shift+C says, and the "Night Owl Radio: " the status keys
  // start with while there are several stations.
  focusStationSwitcher = () => false,
  getStationText = null,
  stepStation = () => false,
  // What Space / k do: Play / Pause, or in stream mode start or stop
  // listening on this page.
  togglePlay,
  // Landmarks (#60): the brackets move by one (+1 or -1) and say it;
  // Shift+U resolves to the sentence about what is coming next on air.
  jumpLandmark = null,
  getUpcomingLandmark = null,
  // Settings, Keyboard: "Bracket keys seek the song on air".
  bracketSeekToggle = null,
}) {
  installShortcutToggle(shortcutToggle);
  installBracketSeekToggle(bracketSeekToggle);
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
    const key = e.key;
    const altGraphBracket = (key === "[" || key === "]")
      && typeof e.getModifierState === "function" && e.getModifierState("AltGraph")
      && e.code !== "BracketLeft" && e.code !== "BracketRight";
    if (e.metaKey || ((e.ctrlKey || e.altKey) && !altGraphBracket)) return;

    const scrollsPage = key === " " || key === "ArrowUp" || key === "ArrowDown";
    if (!nowVisible && scrollsPage) return;

    let bumpVol = 0;
    // Shift+[ and Shift+] switch to the previous and next station, by the
    // physical key, whatever character the layout gives with Shift.
    if (e.shiftKey && (e.code === "BracketLeft" || e.code === "BracketRight")) {
      if (!stepStation(e.code === "BracketRight" ? 1 : -1)) return;
      e.preventDefault();
      return;
    }
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
        srSpeak(rem != null ? fmtTime(rem) : "Position unknown");
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
      case "c":
        if (!focusStationSwitcher()) return;
        break;
      case "C":
        if (!getStationText) return;
        srSpeak(getStationText());
        break;
      case "H":
        if (speakHour) void speakHour();
        else srSpeak("No hour preview yet.");
        break;
      case "[":
      case "]":
        if (!jumpLandmark) return;
        void jumpLandmark(key === "]" ? 1 : -1);
        break;
      case "U":
        if (!getUpcomingLandmark) return;
        void Promise.resolve()
          .then(getUpcomingLandmark)
          .then((text) => { if (text) srSpeak(text); })
          .catch((error) => srSpeak(`Could not load the landmarks: ${error.message}`));
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
