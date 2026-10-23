
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

import {
  DUCK_ERROR,
  MIN_INTRO_ERROR,
  OFF_REASON,
  installBreakSettings,
  parseNumber,
  savedText,
} from "../../src/autodj/static/modules/breaks-settings.js";
import { initSubtabs } from "../../src/autodj/static/modules/subtabs.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.

const byId = (id) => document.getElementById(id);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const said = () => vi.waitFor(() => {
  const text = byId("brk-status").textContent;
  if (!text) throw new Error("nothing said yet");
  return text;
});
const submit = () => byId("brk-form").dispatchEvent(new Event("submit", { cancelable: true }));

const SAVED = {
  enabled: true, dj_placement: "auto", min_intro_seconds: 2, duck_db: null,
  liners_duck_db: -12, reason: null,
};

function server(overrides = {}) {
  let settings = { ...SAVED, ...overrides };
  return vi.fn(async (url, options = {}) => {
    expect(url).toBe("/api/breaks/settings");
    if (options.method === "POST") {
      const body = JSON.parse(options.body);
      const dropped = settings.enabled && body.enabled === false ? 2 : 0;
      settings = { ...settings, ...body };
      return { ...settings, dropped };
    }
    return settings;
  });
}

async function install(request) {
  const controls = installBreakSettings({ doc: document, request });
  await controls.refresh();
  return controls;
}

beforeEach(installIndex);

describe("Breaks panel markup", () => {
  it("is a Settings subtab between Time checks and Schedule, with no aria-controls", () => {
    const tab = byId("stab-breaks");
    expect(tab.getAttribute("role")).toBe("tab");
    expect(tab.textContent).toBe("Breaks");
    expect(tab.hasAttribute("aria-controls")).toBe(false);
    expect(tab.previousElementSibling.id).toBe("stab-time-checks");
    expect(tab.nextElementSibling.id).toBe("stab-schedule");
    const panel = byId(tab.dataset.panel);
    expect(panel.id).toBe("spanel-breaks");
    expect(panel.getAttribute("role")).toBe("tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe("stab-breaks");
    expect(panel.querySelector(":scope > h3").textContent).toBe("Breaks");
  });

  it("labels every control, groups the placement and keeps one status region", () => {
    const panel = byId("spanel-breaks");
    for (const id of ["brk-enabled", "brk-placement-auto", "brk-placement-talk-up",
      "brk-placement-standalone", "brk-min-intro", "brk-duck"]) {
      const label = panel.querySelector(`label[for="${id}"]`);
      expect(label?.textContent.trim(), id).toBeTruthy();
      const help = byId(byId(id).getAttribute("aria-describedby").split(" ")[0]);
      expect(help?.textContent.trim(), id).toBeTruthy();
    }
    expect(panel.querySelector('label[for="brk-enabled"]').textContent.trim())
      .toBe("Accept announcer uploads");
    expect(byId("brk-placement").tagName).toBe("FIELDSET");
    expect(byId("brk-placement").querySelector("legend").textContent).toBe("Where a DJ break goes");
    // The group's help is read as part of the group, not through aria-describedby,
    // which NVDA does not read reliably on a fieldset.
    expect(byId("brk-placement").hasAttribute("aria-describedby")).toBe(false);
    expect(byId("brk-placement").querySelector("legend + #brk-placement-help")).not.toBeNull();
    expect(byId("brk-form").hasAttribute("aria-labelledby")).toBe(false);
    expect(byId("brk-form-heading").textContent).toBe("Break settings");
    expect(panel.querySelectorAll("[aria-live]")).toHaveLength(1);
    expect(byId("brk-status").getAttribute("role")).toBe("status");
    expect(byId("brk-reason").hasAttribute("aria-live")).toBe(false);
    expect(byId("brk-save").textContent).toBe("Save break settings");
    // The placement help is the operations guide's, in words.
    expect(byId("brk-placement-auto-help").textContent).toMatch(/at least as long as the shortest intro/);
    expect(byId("brk-placement-talk-up-help").textContent).toMatch(/With no intro point the break still plays between the songs/);
    expect(byId("brk-placement-help").textContent).toMatch(/News, weather and music news always play between the songs/);
  });

  it("no longer sends the operator to config.toml to turn breaks on", () => {
    const workers = byId("spanel-workers").textContent;
    expect(workers).not.toMatch(/\[breaks\]|config\.toml/);
    expect(workers).toMatch(/Turn breaks on for the station in Settings, Breaks\./);
  });

  it("asks the station for its settings when the tab is selected", async () => {
    const request = server();
    byId("panel-settings").hidden = false;
    installBreakSettings({ doc: document, request });
    initSubtabs(byId("settings-tabs"), { storage: null });
    const before = request.mock.calls.length;
    byId("stab-breaks").click();
    await flush();
    expect(request.mock.calls.length).toBeGreaterThan(before);
    expect(byId("brk-min-intro").value).toBe("2");
  });
});

describe("Break settings words", () => {
  it("reads numbers typed with a dot, a comma or a minus sign", () => {
    expect(parseNumber(" 1.5 ", 0, 60)).toBe(1.5);
    expect(parseNumber("2,5", 0, 60)).toBe(2.5);
    expect(parseNumber("-12", -30, 0)).toBe(-12);
    expect(parseNumber("−12", -30, 0)).toBe(-12);
    for (const bad of ["", "61", "-1", "abc", "1e1", "NaN"]) expect(parseNumber(bad, 0, 60)).toBeNull();
    expect(parseNumber("3", -30, 0)).toBeNull();
  });

  it("says whether a save turned breaks on or off", () => {
    expect(savedText({ enabled: false }, { enabled: true, reason: null })).toBe("Breaks are on.");
    expect(savedText({ enabled: false }, { enabled: true, reason: "Breaks need saved-point playback" }))
      .toBe("Breaks are on. Breaks need saved-point playback.");
    expect(savedText({ enabled: true }, { enabled: false, dropped: 0 }))
      .toBe("Breaks are off. New announcer uploads are refused.");
    expect(savedText({ enabled: true }, { enabled: false, dropped: 1 }))
      .toBe("Breaks are off. New announcer uploads are refused. 1 waiting break was dropped.");
    expect(savedText({ enabled: true }, { enabled: true })).toBe("Break settings saved.");
  });
});

describe("Break settings panel", () => {
  it("shows the saved settings, with the liner duck the blank field uses", async () => {
    await install(server({ dj_placement: "standalone", min_intro_seconds: 3.5, duck_db: -9 }));
    expect(byId("brk-enabled").checked).toBe(true);
    expect(byId("brk-placement-standalone").checked).toBe(true);
    expect(byId("brk-min-intro").value).toBe("3.5");
    expect(byId("brk-duck").value).toBe("-9");
    expect(byId("brk-duck-help").textContent).toMatch(/voice liner duck depth, now minus 12\./);
    expect(byId("brk-reason").hidden).toBe(true);
  });

  it("says in plain text why breaks cannot be taken now", async () => {
    await install(server({ reason: "Breaks need saved-point playback (transition mode: use saved segue points)" }));
    expect(byId("brk-reason").hidden).toBe(false);
    expect(byId("brk-reason").textContent)
      .toBe("Breaks need saved-point playback (transition mode: use saved segue points).");
    installIndex();
    await install(server({ enabled: false }));
    expect(byId("brk-enabled").checked).toBe(false);
    expect(byId("brk-reason").textContent).toBe(OFF_REASON);
    expect(byId("brk-duck").value).toBe("");
  });

  it("saves every setting, busy but never disabled, and says so once", async () => {
    const request = server();
    await install(request);
    byId("brk-placement-talk-up").checked = true;
    byId("brk-min-intro").value = "1,5";
    byId("brk-duck").value = "-8";
    byId("brk-form").dispatchEvent(new Event("change"));
    let answer;
    request.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
    const save = byId("brk-save");
    submit();
    expect(save.getAttribute("aria-busy")).toBe("true");
    expect(save.classList.contains("is-pending")).toBe(true);
    expect(save.hasAttribute("aria-disabled")).toBe(false);
    expect(save.disabled).toBe(false);
    const calls = request.mock.calls.length;
    submit();  // a second press waits for the first
    expect(request.mock.calls.length).toBe(calls);
    const [, options] = request.mock.calls.at(-1);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      enabled: true, dj_placement: "talk_up", min_intro_seconds: 1.5, duck_db: -8,
    });
    answer({ ...SAVED, dj_placement: "talk_up", min_intro_seconds: 1.5, duck_db: -8, dropped: 0 });
    expect(await said()).toBe("Break settings saved.");
    expect(save.hasAttribute("aria-busy")).toBe(false);
    expect(save.classList.contains("is-pending")).toBe(false);
  });

  it("sends a blank duck as null: the voice liner duck", async () => {
    const request = server({ duck_db: -6 });
    await install(request);
    byId("brk-duck").value = " ";
    submit();
    await said();
    expect(JSON.parse(request.mock.calls.at(-1)[1].body).duck_db).toBeNull();
  });

  it("turning breaks off says announcer uploads are refused and how many were dropped", async () => {
    const request = server();
    await install(request);
    byId("brk-enabled").checked = false;
    byId("brk-form").dispatchEvent(new Event("change"));
    submit();
    expect(await said())
      .toBe("Breaks are off. New announcer uploads are refused. 2 waiting breaks were dropped.");
    expect(JSON.parse(request.mock.calls.at(-1)[1].body).enabled).toBe(false);
    expect(byId("brk-reason").textContent).toBe(OFF_REASON);
  });

  it("refuses a value out of range: marked, linked, shown and focused, nothing sent", async () => {
    const request = server();
    await install(request);
    const calls = request.mock.calls.length;
    const field = byId("brk-min-intro");
    field.value = "90";
    submit();
    await flush();
    expect(request.mock.calls.length).toBe(calls);
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(field.getAttribute("aria-describedby")).toBe("brk-min-intro-help brk-min-intro-error");
    expect(byId("brk-min-intro-error").hidden).toBe(false);
    expect(byId("brk-min-intro-error").textContent).toBe(MIN_INTRO_ERROR);
    expect(document.activeElement).toBe(field);
    expect(await said()).toBe(MIN_INTRO_ERROR);

    field.value = "2";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    expect(field.hasAttribute("aria-invalid")).toBe(false);
    // Typing clears the error text with the mark.
    expect(byId("brk-min-intro-error").hidden).toBe(true);
    expect(byId("brk-min-intro-error").textContent).toBe("");
    expect(field.getAttribute("aria-describedby")).toBe("brk-min-intro-help");
    const duck = byId("brk-duck");
    duck.value = "6";
    submit();
    await flush();
    expect(duck.getAttribute("aria-invalid")).toBe("true");
    expect(byId("brk-duck-error").textContent).toBe(DUCK_ERROR);
    expect(document.activeElement).toBe(duck);
    expect(field.getAttribute("aria-describedby")).toBe("brk-min-intro-help");
    expect(request.mock.calls.length).toBe(calls);
  });

  it("says why a save failed and leaves the button usable", async () => {
    const request = server();
    await install(request);
    request.mockRejectedValueOnce(new Error("Request failed (500)"));
    submit();
    expect(await said()).toBe("Could not save the break settings: Request failed (500)");
    expect(byId("brk-save").hasAttribute("aria-busy")).toBe(false);
  });

  it("does not overwrite settings being changed when it refreshes", async () => {
    const request = server();
    const controls = await install(request);
    byId("brk-min-intro").value = "7";
    byId("brk-form").dispatchEvent(new Event("input"));
    await controls.refresh();
    expect(byId("brk-min-intro").value).toBe("7");
  });

  it("forgets the old station's words on a switch", async () => {
    const controls = await install(server({ enabled: false }));
    expect(byId("brk-reason").hidden).toBe(false);
    controls.clear();
    expect(byId("brk-reason").hidden).toBe(true);
    expect(byId("brk-duck-liners").textContent).toBe("");
  });
});
