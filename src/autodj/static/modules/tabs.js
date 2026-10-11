// Section router -- single-page navigation. Audio
// graph + crossfade state survive every navigation because the
// document never reloads; only the `hidden` attribute toggles on
// each <section>.  Tablist arrow / Home / End navigation follows
// the ARIA APG roving-tabindex pattern (tablist.js).

import { markSelected, tabForKey } from "./tablist.js";

const VIEW_NAMES = ["now", "queue", "track-info", "history", "settings", "library"];

const _viewSections = new Map();
const _viewLinks = new Map();
let _initialised = false;

export function initViewRouter() {
  if (_initialised) return;
  for (const name of VIEW_NAMES) {
    const sec = document.querySelector(`section[data-view="${name}"]`);
    const lnk = document.querySelector(`#view-nav [role="tab"][data-view="${name}"]`);
    if (sec) _viewSections.set(name, sec);
    if (lnk) {
      _viewLinks.set(name, lnk);
      // Keep cached page markup in the same order as keyboard navigation.
      lnk.parentElement.append(lnk);
    }
  }
  for (const lnk of _viewLinks.values()) {
    lnk.addEventListener("click", () => {
      const target = lnk.dataset.view;
      if (location.hash !== "#" + target) location.hash = target;
      else applyView(target, true);
    });
    // Activates the focused tab on move (automatic activation) -- panel
    // swap is just a `hidden` toggle, no perf reason for manual.
    lnk.addEventListener("keydown", _onTabKeydown);
  }
  window.addEventListener("hashchange", () => {
    const view = (location.hash || "#now").replace(/^#/, "");
    applyView(VIEW_NAMES.includes(view) && _viewSections.has(view) ? view : "now", true);
  });
  // Initial paint -- no focus stealing on first load.
  const initial = (location.hash || "#now").replace(/^#/, "");
  applyView(VIEW_NAMES.includes(initial) && _viewSections.has(initial) ? initial : "now", false);
  _initialised = true;
}

function _onTabKeydown(e) {
  const order = VIEW_NAMES.filter(n => _viewLinks.has(n)).map(n => _viewLinks.get(n));
  const next = tabForKey(order, e.currentTarget, e.key);
  if (!next) return;
  const nextName = next.dataset.view;
  e.preventDefault();
  // Select first, then focus, as the APG tabs example does, so focus
  // lands on a tab that is already selected.  The hashchange that
  // follows finds the state already set and changes nothing.  The tabs
  // carry no aria-controls (see index.html): that relation to the panel
  // shown here made NVDA speak the focused tab a second time.
  applyView(nextName, false);
  const nextTab = _viewLinks.get(nextName);
  if (nextTab) nextTab.focus();
  if (location.hash !== "#" + nextName) location.hash = nextName;
}

function applyView(name, userInitiated) {
  for (const [k, sec] of _viewSections) {
    if (k === name) sec.removeAttribute("hidden");
    else sec.setAttribute("hidden", "");
  }
  markSelected([..._viewLinks.values()], _viewLinks.get(name));
  if (userInitiated) {
    const sec = _viewSections.get(name);
    const tab = _viewLinks.get(name);
    // Don't steal focus away from a focused tab during arrow-key nav
    // -- that defeats roving tabindex.
    if (sec && document.activeElement !== tab) {
      // Only a heading of the panel itself.  Card titles are <h2>s inside
      // <summary>, and focusing one of those would land inside the
      // disclosure button instead of on it.
      const heading = Array.from(sec.children).find((el) => el.tagName === "H2");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: false });
      } else {
        const firstSummary = sec.querySelector("summary");
        if (firstSummary) firstSummary.focus({ preventScroll: false });
      }
    }
  }
}
