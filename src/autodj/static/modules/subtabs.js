import { settingsHaveChanges, confirmSettingsNavigation } from "./settings-drafts.js";
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
const CATEGORY_KEY = "autodj.settingsCategory";
const categoryStorageKey = (kind) => `${STORAGE_KEY}.${kind}`;
const OLD_TABS = { transitions: "mixing", djmix: "mixing", liners: "announcements", "time-checks": "announcements", breaks: "announcements", live: "stream", workers: "server", updates: "server" };

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
  const categoryRadios = [...(settings?.querySelectorAll('#settings-section input[type="radio"]') || [])];
  const innerSections = [...(settings?.querySelectorAll("[data-settings-section]") || [])];
  const profileTargets = [...(settings?.querySelectorAll(".profile-target-row") || [])];
  // Markup starts the General tabs hidden; category visibility is applied
  // below. This set tracks only explicit runtime hiding via setTabHidden.
  const runtimeHidden = new Set();
  let selected = null;
  let category = "profile";
  let pending = false;

  function visibleTabs(kind = category) {
    return tabs.filter((tab) => !runtimeHidden.has(tab.dataset.subview) &&
      (!tab.dataset.settingsKind || tab.dataset.settingsKind === kind));
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
      for (const radio of categoryRadios) radio.checked = radio.value === category;
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
    category = tab.dataset.settingsKind || category;
    for (const each of tabs) {
      if (each.dataset.settingsKind) each.hidden = runtimeHidden.has(each.dataset.subview) || each.dataset.settingsKind !== category;
    }
    for (const radio of categoryRadios) radio.checked = radio.value === category;
    markSelected(tabs, tab);
    for (const [each, panel] of panels) {
      if (panel) panel.hidden = each !== tab;
    }
    for (const row of profileTargets) row.hidden = category !== "profile";
    if (remember) {
      writeStored(storage, tab.dataset.subview);
      writeStorage(storage, categoryStorageKey(category), tab.dataset.subview);
      writeStorage(storage, CATEGORY_KEY, category);
    }
    notify();
    if (focusPanel) focusHeading(panels.get(tab));
    else if (focusTab) tab.focus();
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => select(tab, { focusPanel: true }));
    tab.addEventListener("keydown", (event) => {
      const next = tabForKey(visibleTabs(), tab, event.key);
      if (!next) return;
      event.preventDefault();
      // Select first, then focus, so focus lands on a selected tab.
      select(next, { focusTab: true });
    });
  }

  for (const radio of categoryRadios) radio.addEventListener("change", () => {
    if (!radio.checked) return;
    const nextKind = radio.value;
    const remembered = readStorage(storage, categoryStorageKey(nextKind)) || readStored(storage);
    const rememberedTab = tabs.find((tab) => tab.dataset.settingsKind === nextKind &&
      (tab.dataset.subview === remembered || OLD_TABS[remembered] === tab.dataset.subview) &&
      !runtimeHidden.has(tab.dataset.subview));
    const next = rememberedTab || visibleTabs(nextKind)[0];
    if (next) select(next);
  });

  // The section router shows and hides the Settings view by its hidden
  // attribute, so panels hear about that too.
  const Observer = doc.defaultView?.MutationObserver;
  if (view && Observer) {
    new Observer(notify).observe(view, { attributes: true, attributeFilter: ["hidden"] });
  }

  const legacySelection = readStored(storage);
  const savedCategory = readStorage(storage, CATEGORY_KEY) ||
    tabs.find((tab) => tab.dataset.subview === legacySelection || OLD_TABS[legacySelection] === tab.dataset.subview)
      ?.dataset.settingsKind || "profile";
  const remembered = readStorage(storage, categoryStorageKey(savedCategory)) || legacySelection;
  const stored = tabs.find((tab) => tab.dataset.settingsKind === savedCategory &&
    (tab.dataset.subview === remembered || OLD_TABS[remembered] === tab.dataset.subview) &&
    !runtimeHidden.has(tab.dataset.subview));
  const defaultTab = visibleTabs(savedCategory)[0] || tabs[0];
  select(stored || defaultTab, { remember: false });

  return {
    // Show or hide the tab named *name* (Stream exists only in stream
    // mode).  Hiding the selected tab selects the first one left.
    setTabHidden(name, hidden) {
      const tab = tabs.find((each) => each.dataset.subview === name);
      if (!tab || runtimeHidden.has(name) === hidden) return;
      if (hidden) runtimeHidden.add(name);
      else runtimeHidden.delete(name);
      tab.hidden = hidden || (tab.dataset.settingsKind && tab.dataset.settingsKind !== category);
      if (hidden && tab === selected) {
        select(visibleTabs()[0] || tabs.find((each) => !each.hidden) || tabs[0], { remember: false });
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
