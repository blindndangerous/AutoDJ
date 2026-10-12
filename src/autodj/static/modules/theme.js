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

export const THEME_STORAGE_KEY = "autodj.theme";
const PINNED = new Set(["light", "dark"]);

// Reading localStorage itself throws where site data is blocked.
function defaultStorage() {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

// "system", "light" or "dark".  Anything unreadable or unknown is System.
export function readThemeChoice(storage = defaultStorage()) {
  try {
    const stored = storage?.getItem(THEME_STORAGE_KEY);
    return PINNED.has(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function applyThemeChoice(choice, root = globalThis.document?.documentElement) {
  if (!root) return;
  if (PINNED.has(choice)) root.setAttribute("data-theme", choice);
  else root.removeAttribute("data-theme");
}

function writeThemeChoice(storage, choice) {
  try {
    storage?.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    // Private windows and blocked storage: used until the page reloads.
  }
}

export function installThemeSetting(doc = globalThis.document, storage = defaultStorage()) {
  const radios = [...(doc?.querySelectorAll?.('input[type="radio"][name="theme"]') || [])];
  const show = (choice) => {
    for (const radio of radios) radio.checked = radio.value === choice;
    applyThemeChoice(choice, doc.documentElement);
  };
  show(readThemeChoice(storage));
  for (const radio of radios) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      const choice = PINNED.has(radio.value) ? radio.value : "system";
      applyThemeChoice(choice, doc.documentElement);
      writeThemeChoice(storage, choice);
    });
  }
  // Another tab of this page changed it: follow, silently.
  doc.defaultView?.addEventListener?.("storage", (event) => {
    if (event.key === THEME_STORAGE_KEY) show(readThemeChoice(storage));
  });
}
