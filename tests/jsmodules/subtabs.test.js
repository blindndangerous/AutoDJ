import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { installIndexMarkup as installIndex, memoryStorage } from "./helpers.js";

import { initSubtabs } from "../../src/autodj/static/modules/subtabs.js";
import { tabForKey } from "../../src/autodj/static/modules/tablist.js";


const byId = (id) => document.getElementById(id);
const press = (el, key) => el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("tabForKey", () => {
  it("wraps with the arrows, jumps with Home and End, and skips hidden tabs", () => {
    const tabs = ["a", "b", "c", "d"].map((name) => {
      const tab = document.createElement("button");
      tab.dataset.name = name;
      return tab;
    });
    const [a, b, c, d] = tabs;
    c.hidden = true;
    expect(tabForKey(tabs, b, "ArrowRight")).toBe(d);
    expect(tabForKey(tabs, d, "ArrowDown")).toBe(a);
    expect(tabForKey(tabs, a, "ArrowLeft")).toBe(d);
    expect(tabForKey(tabs, a, "End")).toBe(d);
    expect(tabForKey(tabs, d, "Home")).toBe(a);
    expect(tabForKey(tabs, a, "Enter")).toBeNull();
  });
});

describe("Settings subtabs markup", () => {
  it("keeps settings in category tabs with their editing target above the tabs", () => {
    installIndex();
    const section = byId("panel-settings");
    expect(section.querySelector(":scope > h2").textContent).toBe("Settings");
    const tabs = [...byId("settings-tabs").querySelectorAll('[role="tab"]')];
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Mixing", "Song picking", "Sound", "Announcements", "Profiles",
      "Stations", "Broadcasting", "Schedule", "Server", "Preferences",
    ]);
    for (const tab of tabs) {
      expect(tab.hasAttribute("aria-controls")).toBe(false);
      const panel = byId(tab.dataset.panel);
      expect(panel.getAttribute("role")).toBe("tabpanel");
      expect(panel.getAttribute("aria-labelledby")).toBe(tab.id);
      expect(panel.querySelector(":scope > h3").textContent).toBe(tab.textContent);
    }
    const panels = [...section.querySelectorAll('[role="tabpanel"]')];
    for (const control of section.querySelectorAll("input, select, textarea")) {
      if (control.id === "settings-profile") {
        expect(section.querySelector('label[for="settings-profile"]').textContent).toBe("Profile to edit");
        expect(panels.some((panel) => panel.contains(control))).toBe(false);
      } else if (control.type === "radio" && control.name === "settings-section") {
        expect(section.querySelector("#settings-section").contains(control)).toBe(true);
        expect(panels.some((panel) => panel.contains(control))).toBe(false);
      } else {
        expect(panels.some((panel) => panel.contains(control)), control.id).toBe(true);
      }
    }
  });

  it("labels settings and keeps their help available without requiring spoken paragraphs", () => {
    installIndex();
    for (const id of [
      "pb-bass-cutoff", "dj-max-stretch", "dj-glide-bars", "dj-sweep-floor", "pb-pick-top-k",
      "pb-pick-temperature", "pb-rg-clip", "pb-prefetch", "pb-silence-trigger",
      "stream-station-name", "tc-enabled", "tc-hour-format", "tc-rule",
    ]) {
      const control = byId(id);
      expect(document.querySelector(`label[for="${id}"]`), id).not.toBeNull();
      expect(byId(`${id}-desc`)?.textContent.trim(), id).toBeTruthy();
      const described = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      for (const ref of described) expect(byId(ref), `${id} -> ${ref}`).not.toBeNull();
    }
  });
});

describe("initSubtabs", () => {
  function setup(storage = memoryStorage()) {
    installIndex();
    byId("panel-settings").hidden = false;
    return { ui: initSubtabs(byId("settings-tabs"), { storage }), storage };
  }

  it("starts on the first tab and moves with the arrow keys, Home and End", () => {
    const { ui } = setup();
    const first = byId("stab-mixing");
    expect(ui.selected).toBe("mixing");
    expect(first.getAttribute("aria-selected")).toBe("true");
    expect(first.tabIndex).toBe(0);
    expect(byId("spanel-mixing").hidden).toBe(false);
    expect(byId("spanel-picking").hidden).toBe(true);

    first.focus();
    press(first, "ArrowRight");
    expect(document.activeElement).toBe(byId("stab-picking"));
    expect(byId("stab-picking").getAttribute("aria-selected")).toBe("true");
    expect(first.tabIndex).toBe(-1);
    expect(byId("spanel-picking").hidden).toBe(false);
    expect(byId("spanel-mixing").hidden).toBe(true);

    // Public player settings keep Stream available outside stream mode.
    press(document.activeElement, "End");
    expect(document.activeElement).toBe(byId("stab-access"));
    press(document.activeElement, "ArrowLeft");
    expect(document.activeElement).toBe(byId("stab-announcements"));
    press(document.activeElement, "ArrowLeft");
    expect(document.activeElement).toBe(byId("stab-sound"));
    byId("settings-section").querySelector('[value="general"]').click();
    expect(ui.selected).toBe("stations");
    expect(document.activeElement).toBe(byId("stab-sound"));
    press(byId("stab-stations"), "ArrowRight");
    expect(document.activeElement).toBe(byId("stab-stream"));
    press(document.activeElement, "End");
    expect(document.activeElement).toBe(byId("stab-display"));
    press(document.activeElement, "ArrowLeft");
    expect(document.activeElement).toBe(byId("stab-server"));
    press(document.activeElement, "ArrowLeft");
    expect(document.activeElement).toBe(byId("stab-schedule"));
    press(document.activeElement, "ArrowLeft");
    expect(document.activeElement).toBe(byId("stab-stream"));
    press(document.activeElement, "ArrowLeft");
    expect(document.activeElement).toBe(byId("stab-stations"));
    press(document.activeElement, "Home");
    expect(document.activeElement).toBe(byId("stab-stations"));
  });

  it("moves focus to the panel heading on click, as the section tabs do", () => {
    setup();
    byId("stab-sound").click();
    expect(document.activeElement).toBe(byId("spanel-sound").querySelector("h3"));
  });

  it("remembers the chosen tab in this browser", () => {
    const { storage } = setup();
    byId("stab-picking").click();
    expect(storage.data.get("autodj.settingsSubtab")).toBe("picking");
    const again = setup(storage);
    expect(again.ui.selected).toBe("picking");
  });

  it("works when the browser refuses storage", () => {
    const broken = {
      getItem() { throw new Error("blocked"); },
      setItem() { throw new Error("blocked"); },
    };
    const { ui } = setup(broken);
    byId("stab-sound").click();
    expect(ui.selected).toBe("sound");
  });

  it("shows Stream only when asked and leaves it when it goes", () => {
    const { ui } = setup();
    byId("settings-section").querySelector('[value="general"]').click();
    ui.setTabHidden("stream", false);
    byId("stab-stream").click();
    expect(byId("spanel-stream").hidden).toBe(false);
    ui.setTabHidden("stream", true);
    expect(byId("stab-stream").hidden).toBe(true);
    expect(ui.selected).toBe("stations");
    expect(byId("spanel-stream").hidden).toBe(true);
    ui.focusSelected();
    expect(document.activeElement).toBe(byId("stab-stations"));
  });

  it("tells outer and inner sections when they are seen, including when Settings hides", async () => {
    const { ui } = setup();
    const panel = byId("spanel-access");
    const seen = [];
    panel.addEventListener("toggle", () => seen.push(panel.open));
    byId("stab-access").click();
    expect(seen).toEqual([true]);
    byId("panel-settings").hidden = true;
    await flush();
    expect(seen).toEqual([true, false]);
    byId("panel-settings").hidden = false;
    await flush();
    expect(seen).toEqual([true, false, true]);
    byId("stab-sound").click();
    expect(seen).toEqual([true, false, true, false]);
    expect(ui.selected).toBe("sound");
  });

  it("opens inner consolidated sections only while their outer panel is visible", () => {
    setup();
    const inner = byId("spanel-transitions");
    const seen = [];
    inner.addEventListener("toggle", () => seen.push(inner.open));
    byId("stab-picking").click();
    // Clear the initial close event caused by leaving the default Mixing tab.
    seen.length = 0;
    byId("stab-mixing").click();
    expect(inner.open).toBe(true);
    byId("stab-picking").click();
    expect(inner.open).toBe(false);
    expect(seen).toEqual([true, false]);
  });
});

describe("markSelected", () => {
  it("leaves the focused tab's attributes alone when its view is applied again", async () => {
    const { markSelected } = await import("../../src/autodj/static/modules/tablist.js");
    const tabs = [document.createElement("button"), document.createElement("button")];
    document.body.replaceChildren(...tabs);
    markSelected(tabs, tabs[0]);
    const records = [];
    const observer = new window.MutationObserver((list) => records.push(...list));
    for (const tab of tabs) observer.observe(tab, { attributes: true });
    markSelected(tabs, tabs[0]);
    await flush();
    observer.disconnect();
    expect(records).toEqual([]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(tabs[1].tabIndex).toBe(-1);
  });
});

describe("dependent settings", () => {
  it("keeps every show-when rule and the markup that uses it in step", () => {
    installIndex();
    const css = readFileSync(join(process.cwd(), "src/autodj/static/app.css"), "utf8");
    const used = new Set([...document.querySelectorAll("[data-show-when]")]
      .map((el) => el.dataset.showWhen));
    const ruled = new Set([...css.matchAll(/\[data-show-when="([^"]+)"\]/g)].map((m) => m[1]));
    expect([...used].sort()).toEqual([...ruled].sort());
    // "a|b", "id=value" and "id!=value" each name controls by id.
    for (const value of used) {
      for (const part of value.split("|")) {
        const id = part.split(/!?=/)[0];
        expect(byId(id), `${value} -> ${id}`).not.toBeNull();
      }
    }
  });
});
