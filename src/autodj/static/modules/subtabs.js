// Settings subtabs: a second tablist inside the Settings view, one panel
// per group of settings, so a new user meets one short group at a time.
//
// Keys and selection follow the section tabs (tablist.js): arrows, Home
// and End move and select, and clicking a tab moves focus to its panel's
// heading, as clicking a section tab does.  Like the section tabs, these
// carry no aria-controls (see index.html).  Each tab names its panel in
// data-panel instead.  The selected tab is remembered in this browser.
//
// Panels that load on demand (Profiles and access) need to know when
// they are seen, as they did as <details> cards.  So each panel's `open`
// says whether it is seen now -- its tab is selected and the Settings
// view is showing -- and a "toggle" event fires on it when that changes.

import { markSelected, tabForKey } from "./tablist.js";
import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

const STORAGE_KEY = "autodj.settingsSubtab";

function readStored(storage) {
  return readStorage(storage, STORAGE_KEY);
}

function writeStored(storage, value) {
  writeStorage(storage, STORAGE_KEY, value);
}

function focusHeading(panel) {
  const heading = panel.querySelector(":scope > h3");
  if (!heading) return;
  heading.setAttribute("tabindex", "-1");
  heading.focus({ preventScroll: false });
}

export function initSubtabs(tablist, { storage = defaultStorage() } = {}) {
  const doc = tablist.ownerDocument;
  const tabs = [...tablist.querySelectorAll('[role="tab"]')];
  const panels = new Map(tabs.map((tab) => [tab, doc.getElementById(tab.dataset.panel)]));
  const view = tablist.closest("section");
  let selected = null;

  function notify() {
    for (const panel of panels.values()) {
      if (!panel) continue;
      const seen = !panel.hidden && !view?.hidden;
      if (Boolean(panel.open) === seen) continue;
      panel.open = seen;
      panel.dispatchEvent(new Event("toggle"));
    }
  }

  function select(tab, { focusPanel = false, remember = true } = {}) {
    selected = tab;
    markSelected(tabs, tab);
    for (const [each, panel] of panels) {
      if (panel) panel.hidden = each !== tab;
    }
    if (remember) writeStored(storage, tab.dataset.subview);
    notify();
    if (focusPanel) focusHeading(panels.get(tab));
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => select(tab, { focusPanel: true }));
    tab.addEventListener("keydown", (event) => {
      const next = tabForKey(tabs, tab, event.key);
      if (!next) return;
      event.preventDefault();
      // Select first, then focus, so focus lands on a selected tab.
      select(next);
      next.focus();
    });
  }

  // The section router shows and hides the Settings view by its hidden
  // attribute, so panels hear about that too.
  const Observer = doc.defaultView?.MutationObserver;
  if (view && Observer) {
    new Observer(notify).observe(view, { attributes: true, attributeFilter: ["hidden"] });
  }

  const stored = tabs.find((tab) => tab.dataset.subview === readStored(storage) && !tab.hidden);
  select(stored || tabs.find((tab) => !tab.hidden) || tabs[0], { remember: false });

  return {
    // Show or hide the tab named *name* (Stream exists only in stream
    // mode).  Hiding the selected tab selects the first one left.
    setTabHidden(name, hidden) {
      const tab = tabs.find((each) => each.dataset.subview === name);
      if (!tab || tab.hidden === hidden) return;
      tab.hidden = hidden;
      if (hidden && tab === selected) {
        select(tabs.find((each) => !each.hidden) || tabs[0], { remember: false });
      }
    },
    // Put focus on the selected tab, for when the control that had it
    // has just gone away.
    focusSelected() {
      selected?.focus();
    },
    get selected() {
      return selected?.dataset.subview ?? null;
    },
  };
}
