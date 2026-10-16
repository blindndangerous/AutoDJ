// Settings, Display and keys, Theme: System (default), Light or Dark, kept
// per browser and applied before first paint.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { memoryStorage } from "./helpers.js";

import {
  THEME_STORAGE_KEY,
  applyThemeChoice,
  installThemeSetting,
  readThemeChoice,
} from "../../src/autodj/static/modules/theme.js";
import { fallbackTokens, themeTokens } from "./theme-css.js";

const initSource = readFileSync(
  join(process.cwd(), "src", "autodj", "static", "modules", "theme-init.js"), "utf8",
);

const appCss = readFileSync(
  join(process.cwd(), "src", "autodj", "static", "app.css"), "utf8",
);

const RADIOS = ["system", "light", "dark"].map((value) => (
  `<input type="radio" name="theme" id="theme-${value}" value="${value}"`
  + `${value === "system" ? " checked" : ""}>`
)).join("");


const blockedStorage = {
  getItem() { throw new Error("SecurityError"); },
  setItem() { throw new Error("SecurityError"); },
};

const root = () => document.documentElement;
const checked = () => document.querySelector('input[name="theme"]:checked')?.value;

function choose(value) {
  const radio = document.getElementById(`theme-${value}`);
  radio.checked = true;
  radio.dispatchEvent(new window.Event("change", { bubbles: true }));
}

beforeEach(() => {
  root().removeAttribute("data-theme");
  document.body.innerHTML = `<fieldset>${RADIOS}</fieldset>`;
});
afterEach(() => {
  root().removeAttribute("data-theme");
});

describe("readThemeChoice", () => {
  it("defaults to System with nothing stored", () => {
    expect(readThemeChoice(memoryStorage())).toBe("system");
  });

  it("reads Light and Dark back", () => {
    expect(readThemeChoice(memoryStorage({ [THEME_STORAGE_KEY]: "light" }))).toBe("light");
    expect(readThemeChoice(memoryStorage({ [THEME_STORAGE_KEY]: "dark" }))).toBe("dark");
  });

  it("falls back to System for an unknown value, no storage or blocked storage", () => {
    expect(readThemeChoice(memoryStorage({ [THEME_STORAGE_KEY]: "sepia" }))).toBe("system");
    expect(readThemeChoice(null)).toBe("system");
    expect(readThemeChoice(blockedStorage)).toBe("system");
  });
});

describe("applyThemeChoice", () => {
  it("pins Light or Dark on <html> and clears it for System", () => {
    applyThemeChoice("light", root());
    expect(root().getAttribute("data-theme")).toBe("light");
    applyThemeChoice("dark", root());
    expect(root().getAttribute("data-theme")).toBe("dark");
    applyThemeChoice("system", root());
    expect(root().hasAttribute("data-theme")).toBe(false);
  });
});

describe("installThemeSetting", () => {
  it("checks System by default and leaves the system preference in charge", () => {
    installThemeSetting(document, memoryStorage());
    expect(checked()).toBe("system");
    expect(root().hasAttribute("data-theme")).toBe(false);
  });

  it("checks and applies the stored choice", () => {
    installThemeSetting(document, memoryStorage({ [THEME_STORAGE_KEY]: "dark" }));
    expect(checked()).toBe("dark");
    expect(root().getAttribute("data-theme")).toBe("dark");
  });

  it("applies a change at once and keeps it for the next visit", () => {
    const storage = memoryStorage();
    installThemeSetting(document, storage);
    choose("light");
    expect(root().getAttribute("data-theme")).toBe("light");
    expect(storage.data.get(THEME_STORAGE_KEY)).toBe("light");

    // A reload: a fresh page reads it back.
    root().removeAttribute("data-theme");
    document.body.innerHTML = `<fieldset>${RADIOS}</fieldset>`;
    installThemeSetting(document, storage);
    expect(checked()).toBe("light");
    expect(root().getAttribute("data-theme")).toBe("light");

    choose("system");
    expect(root().hasAttribute("data-theme")).toBe(false);
    expect(storage.data.get(THEME_STORAGE_KEY)).toBe("system");
  });

  it("still applies a change when storage is blocked, starting from System", () => {
    installThemeSetting(document, blockedStorage);
    expect(checked()).toBe("system");
    expect(() => choose("dark")).not.toThrow();
    expect(root().getAttribute("data-theme")).toBe("dark");
  });

  it("follows a change made in another tab", () => {
    const storage = memoryStorage();
    installThemeSetting(document, storage);
    storage.setItem(THEME_STORAGE_KEY, "dark");
    window.dispatchEvent(new window.StorageEvent("storage", { key: THEME_STORAGE_KEY }));
    expect(checked()).toBe("dark");
    expect(root().getAttribute("data-theme")).toBe("dark");
  });
});

// theme-init.js is the classic <head> script: run it as the browser would.
describe("theme-init.js", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  afterEach(() => {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
  });
  function runWith(storage) {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        if (storage === blockedStorage) throw new Error("SecurityError");
        return storage;
      },
    });
    new Function(initSource)();
  }

  it("sets the stored Light or Dark before the page paints", () => {
    runWith(memoryStorage({ [THEME_STORAGE_KEY]: "light" }));
    expect(root().getAttribute("data-theme")).toBe("light");
  });

  it("leaves System, an unknown value and blocked storage to the system preference", () => {
    for (const storage of [
      memoryStorage(),
      memoryStorage({ [THEME_STORAGE_KEY]: "system" }),
      memoryStorage({ [THEME_STORAGE_KEY]: "<script>" }),
      blockedStorage,
    ]) {
      root().removeAttribute("data-theme");
      expect(() => runWith(storage)).not.toThrow();
      expect(root().hasAttribute("data-theme")).toBe(false);
    }
  });

  it("uses the same storage key as the setting", () => {
    expect(initSource).toContain(`"${THEME_STORAGE_KEY}"`);
  });
});

// Browsers without light-dark() get app.css's static dark token set.
describe("app.css light-dark() fallback", () => {
  it("sets every token to its dark value", () => {
    const dark = themeTokens(appCss, "dark");
    const fallback = fallbackTokens(appCss);
    expect([...fallback.keys()].sort()).toEqual([...dark.keys()].sort());
    for (const [name, value] of dark) expect(fallback.get(name), name).toBe(value);
  });

  it("applies only where light-dark() is unsupported, and pins the dark scheme", () => {
    expect(appCss).toMatch(
      /@supports not \(color: light-dark\(#000, #fff\)\) \{\s*:root,\s*:root\[data-theme\] \{\s*color-scheme: dark;/,
    );
  });
});
