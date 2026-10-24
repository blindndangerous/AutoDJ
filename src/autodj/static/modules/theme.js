import { registerSettingsGroup } from "./settings-drafts.js";
// Settings, Display and keys, Theme: System (the default), Light or Dark.
//
// app.css gives every colour a light and a dark value with light-dark().
// With no data-theme on <html> the root's color-scheme is "light dark",
// so the page follows the reader's system preference; data-theme="light"
// or "dark" pins one.  The choice is kept per browser, like the shortcut
// switch.  theme-init.js applies the stored choice before first paint;
// this module drives the radio buttons and applies a change at once.
// Changing the theme announces nothing beyond the radio button's own
// checked state.

import { defaultStorage, readStorage } from "./safe-storage.js";

export const THEME_STORAGE_KEY = "autodj.theme";
const PINNED = new Set(["light", "dark"]);

// "system", "light" or "dark".  Anything unreadable or unknown is System.
export function readThemeChoice(storage = defaultStorage()) {
  const stored = readStorage(storage, THEME_STORAGE_KEY);
  return PINNED.has(stored) ? stored : "system";
}

export function applyThemeChoice(choice, root = globalThis.document?.documentElement) {
  if (!root) return;
  if (PINNED.has(choice)) root.setAttribute("data-theme", choice);
  else root.removeAttribute("data-theme");
}

function writeThemeChoice(storage, choice) {
  if (!storage) throw new Error("Browser storage is unavailable.");
  storage.setItem(THEME_STORAGE_KEY, choice);
}

export function installThemeSetting(doc = globalThis.document, storage = defaultStorage()) {
  const radios = [...(doc?.querySelectorAll?.('input[type="radio"][name="theme"]') || [])];
  const show = (choice) => {
    for (const radio of radios) radio.checked = radio.value === choice;
    applyThemeChoice(choice, doc.documentElement);
  };
  show(readThemeChoice(storage));
  const draft = registerSettingsGroup({ panel: radios[0]?.closest('[id^="spanel-"]'), controls: radios,
    read: () => ({ theme: radios.find((radio) => radio.checked)?.value || "system" }),
    write: (value) => show(value.theme),
    persisted: () => ({ theme: readThemeChoice(storage) }),
    commit: (value) => writeThemeChoice(storage, value.theme),
  });
  for (const radio of radios) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      const choice = PINNED.has(radio.value) ? radio.value : "system";
      applyThemeChoice(choice, doc.documentElement);
    });
  }
  // Another tab of this page changed it: follow, silently.
  doc.defaultView?.addEventListener?.("storage", (event) => {
    if (event.key === THEME_STORAGE_KEY) draft.accept({ theme: readThemeChoice(storage) });
  });
}
