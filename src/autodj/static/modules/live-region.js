// Transient live-region helper.
//
// Many polite aria-live regions on the page (vol-announce, eq-announce,
// settings-status, ln-status) are
// visually-hidden so sighted users never see them.  AT users running a
// Speech Viewer, or any user with a stylesheet override that reveals
// .visually-hidden, would otherwise see a growing pile of stale
// messages parked at the bottom of the page
// ("Volume 100%.  Volume 95%.  Volume 90%.").
//
// `clearLiveRegionLater` wipes the textContent a few seconds after each
// announce so the region is empty between events.  Repeated calls for
// the same element reset the timer instead of stacking, so the most
// recent announcement always controls the dwell.

// Added to a repeated message so the region text changes.
export const NBSP = String.fromCharCode(160);
const _timers = new WeakMap();

export function clearLiveRegionLater(el, dwellMs = 3000) {
  if (!el) return;
  const prev = _timers.get(el);
  if (prev) clearTimeout(prev);
  const t = setTimeout(() => {
    if (el && el.textContent) el.textContent = "";
    _timers.delete(el);
  }, dwellMs);
  _timers.set(el, t);
}

// Shown, not said: *text* goes into a live region with the region off, and
// the region is on again a second later, once the browser has passed the
// change on.  For words a reader has heard already, or that another line
// says: the connection pill's "Live" after a station switch (#327), the
// last library job's result after a reload (#329).  Words that must be
// said next go through writeLive.
const _quiet = new WeakMap();
const _pending = new WeakMap();
// How long a region turned back on waits before new words go in, so the
// browser has seen it on and says them.
const LIVE_AGAIN_MS = 100;

function cancelPending(region) {
  const pending = _pending.get(region);
  if (!pending) return;
  clearTimeout(pending.timer);
  _pending.delete(region);
}

// The words a region shows, or is about to show.
export function shownText(region) {
  return _pending.has(region) ? _pending.get(region).text : region?.textContent ?? "";
}

export function writeQuietly(region, text, { restoreMs = 1000 } = {}) {
  if (!region) return;
  cancelPending(region);
  if (region.textContent === text) return;
  const before = _quiet.get(region);
  if (before) clearTimeout(before.timer);
  const live = before ? before.live : region.getAttribute("aria-live");
  region.setAttribute("aria-live", "off");
  region.textContent = text;
  const timer = setTimeout(() => liveAgain(region), restoreMs);
  _quiet.set(region, { timer, live });
}

// Turns a region writeQuietly turned off back on at once.
export function liveAgain(region) {
  const quiet = region && _quiet.get(region);
  if (!quiet) return;
  clearTimeout(quiet.timer);
  _quiet.delete(region);
  if (quiet.live === null) region.removeAttribute("aria-live");
  else region.setAttribute("aria-live", quiet.live);
}

// Words to be said in a region writeQuietly may have turned off: the region
// is turned on first, and the words go in LIVE_AGAIN_MS later, once the
// browser has seen it on.  Written at once, they could reach the browser
// with the region still off, and nothing was said.  A later write replaces
// one still waiting.
export function writeLive(region, text) {
  if (!region) return;
  cancelPending(region);
  if (!_quiet.has(region)) {
    if (region.textContent !== text) region.textContent = text;
    return;
  }
  liveAgain(region);
  const timer = setTimeout(() => {
    _pending.delete(region);
    if (region.textContent !== text) region.textContent = text;
  }, LIVE_AGAIN_MS);
  _pending.set(region, { timer, text });
}

// A press whose request is still out after PENDING_SAY_MS says *text*
// ("Saving…") through *say*, once, so the press is heard: NVDA, JAWS and
// VoiceOver do not say aria-busy on the focused control (#384).  A result
// that comes sooner is the only thing said.  Call the returned function
// when the request ends.
const PENDING_SAY_MS = 300;
export function sayIfStillPending(say, text, delayMs = PENDING_SAY_MS) {
  const timer = setTimeout(() => say(text), delayMs);
  return () => clearTimeout(timer);
}

// ----------------------------------------------------------------
// Visible mirror of the announcement channel.
//
// Every failure the UI can report used to exist only inside a 1 px
// clipped live region, so a sighted user who clicked "Next" on a deleted
// file got no response at all.  `announceStatus` writes the message once
// into the live region that owns it and copies the SAME string into
// #status-toast, a fixed strip at the foot of the viewport.
//
// #status-toast carries aria-hidden="true", no role and no aria-live, so
// the message in it is invisible to assistive tech: the region remains
// the single announcement path and NVDA still hears the message exactly
// once.  Its frame, #status-toast-box, also holds a Dismiss button.  The
// button sits beside the message, never inside it or inside a live
// region, so it adds nothing to what is spoken, and showing the toast
// never moves focus to it.
// ----------------------------------------------------------------

const VISIBLE_STATUS_DWELL_MS = 8000;
// After the pointer or focus leaves a paused toast it stays at least this
// long, so it does not vanish the instant the pointer slips off it.
const VISIBLE_STATUS_RESUME_MS = 2000;
// Matches the 1rem inset in app.css.
const VISIBLE_STATUS_GAP_PX = 16;
let _visibleStatusTimer = null;
let _hideAt = 0;
let _remainingMs = 0;
let _hovered = false;
let _focusWithin = false;
let _lastFocusOutside = null;

// The positioned box: #status-toast-box on the page, or the message
// element itself where there is no frame.
function toastFrame(toast) {
  return toast.closest?.("#status-toast-box") || toast;
}

// WCAG 2.4.11: the toast is fixed to the foot of the viewport, so it
// could sit on top of the control that has focus (a Save button, the
// last queue row, the Refresh stats button).  When the focused element
// reaches into the strip the toast would cover, the toast moves to the
// top of the viewport instead.  Focus on the toast's own button leaves
// it where it is.
function placeVisibleStatus(frame, doc) {
  const view = doc.defaultView;
  const active = doc.activeElement;
  if (active && frame.contains(active)) return;
  let atTop = false;
  if (view && active && active !== doc.body
      && typeof active.getBoundingClientRect === "function") {
    const focused = active.getBoundingClientRect();
    const strip = view.innerHeight - frame.offsetHeight - 2 * VISIBLE_STATUS_GAP_PX;
    atTop = focused.bottom > strip && focused.top < view.innerHeight;
  }
  frame.classList.toggle("at-top", atTop);
}

function stopHideTimer() {
  clearTimeout(_visibleStatusTimer);
  _visibleStatusTimer = null;
}

// WCAG 1.4.13: while the pointer is over the toast or focus is in it, it
// stays; the remaining time runs once both have left.
function startHideTimer(doc, ms) {
  stopHideTimer();
  _remainingMs = ms;
  if (_hovered || _focusWithin) return;
  _hideAt = Date.now() + ms;
  _visibleStatusTimer = setTimeout(() => {
    _visibleStatusTimer = null;
    showVisibleStatus("", doc);
  }, ms);
}

function pauseVisibleStatus() {
  if (!_visibleStatusTimer) return;
  _remainingMs = Math.max(0, _hideAt - Date.now());
  stopHideTimer();
}

function resumeVisibleStatus(doc) {
  if (_hovered || _focusWithin || _visibleStatusTimer) return;
  const toast = doc.getElementById?.("status-toast");
  if (!toast || toast.hidden) return;
  startHideTimer(doc, Math.max(_remainingMs, VISIBLE_STATUS_RESUME_MS));
}

// Hiding the toast must not drop focus that is on its own button onto
// the page body: it goes back where it was before.
function restoreFocusFrom(frame, doc) {
  const active = doc.activeElement;
  if (!active || !frame.contains(active)) return;
  const back = _lastFocusOutside;
  if (back && back.isConnected && !frame.contains(back)) back.focus?.();
  if (frame.contains(doc.activeElement)) doc.getElementById("main-content")?.focus?.();
}

export function showVisibleStatus(
  message,
  doc = globalThis.document,
  { force = false, tone = "info" } = {},
) {
  const toast = doc?.getElementById?.("status-toast");
  if (!toast) return;
  const frame = toastFrame(toast);
  stopHideTimer();
  if (!message) {
    restoreFocusFrom(frame, doc);
    toast.textContent = "";
    frame.classList.remove("is-error");
    toast.hidden = true;
    frame.hidden = true;
    // A hidden toast is neither hovered nor focused, and its leave events
    // may never come.
    _hovered = false;
    _focusWithin = false;
    return;
  }
  // A repeated user action has to look like it happened again, so the
  // forced path replaces the node's text even when the words match.
  if (force) toast.textContent = "";
  if (toast.textContent !== message) toast.textContent = message;
  // A failure and a confirmation must not be the same bordered box.
  frame.classList.toggle("is-error", tone === "error");
  toast.hidden = false;
  frame.hidden = false;
  placeVisibleStatus(frame, doc);
  startHideTimer(doc, VISIBLE_STATUS_DWELL_MS);
}

// Returns true when a screen reader will speak.
//
// Two modes, and picking the wrong one is the whole bug class:
//
//   default -- change-detection.  For anything driven by the 1 Hz
//     websocket tick.  Re-writing identical text is what makes NVDA
//     repeat itself, so an unchanged message is a no-op.
//   force   -- clear, then set on the next task.  For anything the
//     USER just did.  Pressing "Move up"
//     twice, or searching twice and getting the same count, has to be
//     reported twice: silence would read as the button not working.
export function announceStatus(
  region,
  message,
  { dwellMs = 0, mirror = true, force = false, tone = "info" } = {},
) {
  if (!region) return false;
  const text = message == null ? "" : String(message);
  const doc = region.ownerDocument || globalThis.document;
  if (force && text) {
    // Chrome can merge the clear and the new text into one change, so the
    // same message again is not spoken.  A trailing no-break space on every
    // other repeat makes the text itself change.
    const repeat = region.textContent === text ? text + NBSP : text;
    region.textContent = "";
    setTimeout(() => {
      region.textContent = repeat;
      if (dwellMs > 0) clearLiveRegionLater(region, dwellMs);
    }, 0);
    if (mirror) showVisibleStatus(text, doc, { force: true, tone });
    return true;
  }
  if (region.textContent === text) return false;
  region.textContent = text;
  if (mirror) showVisibleStatus(text, doc, { tone });
  if (dwellMs > 0) clearLiveRegionLater(region, dwellMs);
  return true;
}


// Escape, or the Dismiss button, dismisses the visible mirror.  Escape
// never moves focus, so a keyboard user does not lose their place, and it
// defers to an open <dialog>, whose own Escape handling must win.  Every
// focus change while the mirror is showing re-checks that it is not
// covering the new focus.  The pointer over the toast, or focus inside
// it, holds it on screen until both leave.  The listeners are delegated
// from the document, so they find a toast added later, and installing
// twice on one document is a no-op.
const _installed = new WeakSet();

export function installVisibleStatusBehaviour(doc = globalThis.document) {
  if (!doc?.addEventListener || _installed.has(doc)) return;
  _installed.add(doc);
  const frameOf = () => {
    const toast = doc.getElementById("status-toast");
    return toast ? toastFrame(toast) : null;
  };
  doc.addEventListener("focusin", (event) => {
    const frame = frameOf();
    if (frame && frame.contains(event.target)) {
      _focusWithin = true;
      pauseVisibleStatus();
      return;
    }
    _lastFocusOutside = event.target;
    if (frame && !frame.hidden) placeVisibleStatus(frame, doc);
  });
  doc.addEventListener("focusout", (event) => {
    const frame = frameOf();
    if (!frame || !frame.contains(event.target)) return;
    if (event.relatedTarget && frame.contains(event.relatedTarget)) return;
    _focusWithin = false;
    resumeVisibleStatus(doc);
  });
  doc.addEventListener("pointerover", (event) => {
    const frame = frameOf();
    if (!frame || frame.hidden || !frame.contains(event.target)) return;
    _hovered = true;
    pauseVisibleStatus();
  });
  doc.addEventListener("pointerout", (event) => {
    const frame = frameOf();
    if (!frame || !frame.contains(event.target)) return;
    if (event.relatedTarget && frame.contains(event.relatedTarget)) return;
    _hovered = false;
    resumeVisibleStatus(doc);
  });
  // A press of Dismiss with the mouse leaves focus where it is.  WebKit
  // (Safari) does not focus a button on a click: it takes focus off the
  // control that had it and leaves it on the page body, so the keyboard
  // user's place was lost (WCAG 2.4.3).  Holding back the press's default
  // keeps focus on that control in every browser; focus that is already on
  // Dismiss is put back by restoreFocusFrom when the toast hides.
  doc.addEventListener("mousedown", (event) => {
    if (event.button !== 0 || !event.target?.closest?.("#status-toast-dismiss")) return;
    event.preventDefault();
  });
  doc.addEventListener("click", (event) => {
    if (!event.target?.closest?.("#status-toast-dismiss")) return;
    showVisibleStatus("", doc);
  });
  doc.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    const toast = doc.getElementById("status-toast");
    if (!toast || toast.hidden) return;
    if (doc.querySelector("dialog[open]")) return;
    showVisibleStatus("", doc);
  });
}
