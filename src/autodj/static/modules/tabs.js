// Section router -- single-page nav across the four views.  Audio
// graph + crossfade state survive every navigation because the
// document never reloads; only the `hidden` attribute toggles on
// each <section>.  Tablist arrow / Home / End navigation follows
// the ARIA APG roving-tabindex pattern.

export const VIEW_NAMES = ["now", "queue", "settings", "library"];

const _viewSections = new Map();
const _viewLinks = new Map();
let _initialised = false;

export function initViewRouter() {
  if (_initialised) return;
  for (const name of VIEW_NAMES) {
    const sec = document.querySelector(`section[data-view="${name}"]`);
    const lnk = document.querySelector(`#view-nav [role="tab"][data-view="${name}"]`);
    if (sec) _viewSections.set(name, sec);
    if (lnk) _viewLinks.set(name, lnk);
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
    applyView(VIEW_NAMES.includes(view) ? view : "now", true);
  });
  // Initial paint -- no focus stealing on first load.
  const initial = (location.hash || "#now").replace(/^#/, "");
  applyView(VIEW_NAMES.includes(initial) ? initial : "now", false);
  _initialised = true;
}

function _onTabKeydown(e) {
  const order = VIEW_NAMES.filter(n => _viewLinks.has(n));
  const cur = e.currentTarget.dataset.view;
  const idx = order.indexOf(cur);
  let nextName = null;
  switch (e.key) {
    case "ArrowRight":
    case "ArrowDown":
      nextName = order[(idx + 1) % order.length];
      break;
    case "ArrowLeft":
    case "ArrowUp":
      nextName = order[(idx - 1 + order.length) % order.length];
      break;
    case "Home":
      nextName = order[0];
      break;
    case "End":
      nextName = order[order.length - 1];
      break;
    default:
      return;
  }
  e.preventDefault();
  if (location.hash !== "#" + nextName) location.hash = nextName;
  else applyView(nextName, true);
  const nextTab = _viewLinks.get(nextName);
  if (nextTab) nextTab.focus();
}

export function applyView(name, userInitiated) {
  for (const [k, sec] of _viewSections) {
    if (k === name) sec.removeAttribute("hidden");
    else sec.setAttribute("hidden", "");
  }
  for (const [k, lnk] of _viewLinks) {
    const selected = k === name;
    lnk.setAttribute("aria-selected", selected ? "true" : "false");
    // Roving tabindex -- only the active tab is in the document tab
    // order; the others are reachable via arrow keys.
    lnk.tabIndex = selected ? 0 : -1;
  }
  if (userInitiated) {
    const sec = _viewSections.get(name);
    const tab = _viewLinks.get(name);
    // Don't steal focus away from a focused tab during arrow-key nav
    // -- that defeats roving tabindex.
    if (sec && document.activeElement !== tab) {
      const heading = sec.querySelector("h2");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: false });
      }
    }
  }
}
