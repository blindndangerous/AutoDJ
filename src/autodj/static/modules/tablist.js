// Keyboard and selection model shared by the page's tablists: the
// section tabs (tabs.js) and the Settings subtabs (subtabs.js).  It
// follows the ARIA APG tabs pattern with automatic activation: arrow
// keys wrap, Home and End jump to the ends, and only the selected tab
// is in the tab order (roving tabindex).  Hidden tabs are skipped.

// NVDA browse-mode Tab can focus tabindex=-1 tabs. Enter each group at
// its selected tab (or its first visible tab when another group owns the
// selection). Internal keyboard movement and pointer focus remain free.
export function installTabEntry(tablist) {
  let pressed = null;
  const tabAt = (element) => {
    const tab = element?.closest?.('[role="tab"]');
    return tab?.closest('[role="tablist"]') === tablist ? tab : null;
  };
  tablist.addEventListener("pointerdown", (event) => { pressed = tabAt(event.target); });
  const release = () => { pressed = null; };
  tablist.ownerDocument.addEventListener("pointerup", release);
  tablist.ownerDocument.addEventListener("pointercancel", release);
  tablist.addEventListener("focusin", (event) => {
    const tab = tabAt(event.target);
    if (!tab || tab === pressed || tablist.contains(event.relatedTarget)) return;
    const visible = [...tablist.querySelectorAll('[role="tab"]')].filter((each) => !each.hidden);
    const entry = visible.find((each) => each.getAttribute("aria-selected") === "true") || visible[0];
    if (entry && entry !== tab) entry.focus();
  });
  // Do not intercept click: browse-mode Enter focuses, then clicks the
  // original tab even when the focus-entry guard redirected that focus.
}

// The tab an arrow, Home or End key pressed on *current* moves to, or
// null for any other key.
export function tabForKey(tabs, current, key) {
  const order = tabs.filter((tab) => !tab.hidden);
  const idx = order.indexOf(current);
  if (idx === -1) return null;
  switch (key) {
    case "ArrowRight":
    case "ArrowDown":
      return order[(idx + 1) % order.length];
    case "ArrowLeft":
    case "ArrowUp":
      return order[(idx - 1 + order.length) % order.length];
    case "Home":
      return order[0];
    case "End":
      return order[order.length - 1];
    default:
      return null;
  }
}

// Mark *selected* as the selected tab of *tabs*, and make it the only
// one reachable with Tab; the others are reached with the arrow keys.
//
// Attributes are written only when they change.  An arrow key selects
// and focuses the next tab, and the hashchange that follows applies the
// same view again; writing aria-selected="true" a second time on the
// focused tab made screen readers say "selected selected".
export function markSelected(tabs, selected) {
  for (const tab of tabs) {
    const isSelected = tab === selected;
    const value = isSelected ? "true" : "false";
    if (tab.getAttribute("aria-selected") !== value) tab.setAttribute("aria-selected", value);
    const index = isSelected ? 0 : -1;
    if (tab.tabIndex !== index || !tab.hasAttribute("tabindex")) tab.tabIndex = index;
  }
}
