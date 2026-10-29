import { settingsHaveChanges, confirmSettingsNavigation } from "./settings-drafts.js";
// Settings subtabs: labeled tablists inside Settings, sharing one
// visible panel. All groups remain available while editing.
//
// Keys and selection follow the section tabs (tablist.js): arrows, Home
// and End move and select, and clicking a tab moves focus to its panel's
// heading, as clicking a section tab does.  Like the section tabs, these
// carry no aria-controls (see index.html).  Each tab names its panel in
// data-panel instead.  The selected tab is remembered in this browser.
//
// Panels that load on demand (Profiles, for example) need to know when
// they are seen, as they did as <details> cards.  So each panel's `open`
// says whether it is seen now -- its tab is selected and the Settings
// view is showing -- and a "toggle" event fires on it when that changes.

import { markSelected, tabForKey } from "./tablist.js";
import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

const STORAGE_KEY = "autodj.settingsSubtab";
const OLD_TABS = { transitions: "mixing", djmix: "mixing", liners: "announcements", "time-checks": "announcements", breaks: "announcements", live: "stream" };

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
  const settings = tablist.closest("#panel-settings");
  const tablists = tablist.matches('[role="tablist"]') ? [tablist] : [...tablist.querySelectorAll('[role="tablist"]')];
  const innerSections = [...(settings?.querySelectorAll("[data-settings-section]") || [])];
  const profileTargets = [...(settings?.querySelectorAll(".profile-target-row") || [])];
  let selected = null;
  let pending = false;

  function visibleTabs(list = tablist) {
    return tabs.filter((tab) => !tab.hidden && list.contains(tab));
  }

  function markTabs() {
    markSelected(tabs, selected);
    // Every group stays reachable with Tab, even when another group's
    // panel is selected. Arrow keys move within the focused tablist.
    for (const list of tablists) {
      const visible = visibleTabs(list);
      if (!visible.includes(selected) && visible.length) visible[0].tabIndex = 0;
    }
  }

  function notify() {
    for (const panel of panels.values()) {
      if (!panel) continue;
      const seen = !panel.hidden && !view?.hidden;
      if (Boolean(panel.open) === seen) continue;
      panel.open = seen;
      panel.dispatchEvent(new Event("toggle"));
    }
    for (const panel of innerSections) {
      const outer = panel.closest('[role="tabpanel"]:not([data-settings-section])');
      const seen = !panel.hidden && !outer?.hidden && !view?.hidden;
      if (Boolean(panel.open) === seen) continue;
      panel.open = seen;
      panel.dispatchEvent(new Event("toggle"));
    }
  }

  function select(tab, { focusPanel = false, remember = true, confirmed = false, focusTab = false } = {}) {
    if (!confirmed && selected && selected !== tab && settingsHaveChanges(doc)) {
      if (!pending) {
        pending = true;
        void confirmSettingsNavigation(doc).then((allowed) => {
          pending = false;
          if (allowed) select(tab, { focusPanel, remember, focusTab, confirmed: true });
        });
      }
      return;
    }
    selected = tab;
    markTabs();
    for (const [each, panel] of panels) {
      if (panel) panel.hidden = each !== tab;
    }
    for (const row of profileTargets) row.hidden = Boolean(tab.dataset.settingsKind) && tab.dataset.settingsKind !== "profile";
    if (remember) {
      writeStored(storage, tab.dataset.subview);
    }
    notify();
    if (focusPanel) focusHeading(panels.get(tab));
    else if (focusTab) tab.focus();
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => select(tab, { focusPanel: true }));
    tab.addEventListener("keydown", (event) => {
      const next = tabForKey(visibleTabs(tab.closest('[role="tablist"]') || tablist), tab, event.key);
      if (!next) return;
      event.preventDefault();
      // Select first, then focus, so focus lands on a selected tab.
      select(next, { focusTab: true });
    });
  }

  // The section router shows and hides the Settings view by its hidden
  // attribute, so panels hear about that too.
  const Observer = doc.defaultView?.MutationObserver;
  if (view && Observer) {
    new Observer(notify).observe(view, { attributes: true, attributeFilter: ["hidden"] });
  }

  const remembered = readStored(storage);
  const stored = tabs.find((tab) =>
    (tab.dataset.subview === remembered || OLD_TABS[remembered] === tab.dataset.subview) &&
    !tab.hidden);
  const defaultTab = visibleTabs()[0] || tabs[0];
  select(stored || defaultTab, { remember: false });

  return {
    // Show or hide the tab named *name* (Stream exists only in stream
    // mode).  Hiding the selected tab selects the first one left.
    setTabHidden(name, hidden) {
      const tab = tabs.find((each) => each.dataset.subview === name);
      if (!tab || tab.hidden === hidden) return;
      tab.hidden = hidden;
      if (hidden && tab === selected) {
        select(visibleTabs(tab.closest('[role="tablist"]') || tablist)[0] || visibleTabs()[0] || tabs[0], { remember: false });
      } else markTabs();
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
