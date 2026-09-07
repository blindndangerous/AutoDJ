// Transient live-region helper.
//
// Many polite aria-live regions on the page (vol-announce, eq-announce,
// settings-status, ln-status, search-count, badges-announce) are
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

// ----------------------------------------------------------------
// Visible mirror of the announcement channel.
//
// Every failure the UI can report used to exist only inside a 1 px
// clipped live region, so a sighted user who clicked "Next" on a deleted
// file got no response at all.  `announceStatus` writes the message once
// into the live region that owns it and copies the SAME string into
// #status-toast, a fixed strip at the foot of the viewport.
//
// The toast carries aria-hidden="true", no role and no aria-live, so it
// is invisible to assistive tech: the region remains the single
// announcement path and NVDA still hears the message exactly once.
// ----------------------------------------------------------------

const VISIBLE_STATUS_DWELL_MS = 8000;
let _visibleStatusTimer = null;

export function showVisibleStatus(message, doc = globalThis.document, { force = false } = {}) {
  const toast = doc?.getElementById?.("status-toast");
  if (!toast) return;
  clearTimeout(_visibleStatusTimer);
  _visibleStatusTimer = null;
  if (!message) {
    toast.textContent = "";
    toast.hidden = true;
    return;
  }
  // A repeated user action has to look like it happened again, so the
  // forced path replaces the node's text even when the words match.
  if (force) toast.textContent = "";
  if (toast.textContent !== message) toast.textContent = message;
  toast.hidden = false;
  _visibleStatusTimer = setTimeout(() => {
    toast.textContent = "";
    toast.hidden = true;
    _visibleStatusTimer = null;
  }, VISIBLE_STATUS_DWELL_MS);
}

// Returns true when a screen reader will speak.
//
// Two modes, and picking the wrong one is the whole bug class:
//
//   default -- change-detection.  For anything driven by the 1 Hz
//     websocket tick.  Re-writing identical text is what makes NVDA
//     repeat itself, so an unchanged message is a no-op.
//   force   -- clear, then set on the next task, the same pattern
//     srSpeak uses.  For anything the USER just did.  Pressing "Move up"
//     twice, or searching twice and getting the same count, has to be
//     reported twice: silence would read as the button not working.
export function announceStatus(
  region,
  message,
  { dwellMs = 0, mirror = true, force = false } = {},
) {
  if (!region) return false;
  const text = message == null ? "" : String(message);
  const doc = region.ownerDocument || globalThis.document;
  if (force && text) {
    region.textContent = "";
    setTimeout(() => {
      region.textContent = text;
      if (dwellMs > 0) clearLiveRegionLater(region, dwellMs);
    }, 0);
    if (mirror) showVisibleStatus(text, doc, { force: true });
    return true;
  }
  if (region.textContent === text) return false;
  region.textContent = text;
  if (mirror) showVisibleStatus(text, doc);
  if (dwellMs > 0) clearLiveRegionLater(region, dwellMs);
  return true;
}
