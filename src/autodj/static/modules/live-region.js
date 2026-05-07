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
