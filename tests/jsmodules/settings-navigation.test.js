import { beforeEach, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";

const guard = vi.hoisted(() => ({ dirty: vi.fn(), confirm: vi.fn(), actualDrafts: false }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    settingsHaveChanges: (...args) => guard.actualDrafts
      ? actual.settingsHaveChanges(...args) : guard.dirty(...args),
    confirmSettingsNavigation: (...args) => guard.actualDrafts
      ? actual.confirmSettingsNavigation(...args) : guard.confirm(...args),
  };
});

beforeEach(() => {
  vi.resetModules(); guard.dirty.mockReturnValue(true); guard.confirm.mockReset(); guard.actualDrafts = false;
  installIndexMarkup();
});

it("keeps the Settings subtab after a failed save and moves only after approval", async () => {
  const { initSubtabs } = await import("../../src/autodj/static/modules/subtabs.js");
  initSubtabs(document.getElementById("settings-tabs"), { storage: { getItem: () => "transitions", setItem() {} } });
  guard.confirm.mockResolvedValue(false);
  document.getElementById("stab-sound").click();
  await vi.waitFor(() => expect(guard.confirm).toHaveBeenCalledTimes(1));
  expect(document.getElementById("spanel-mixing").hidden).toBe(false);
  guard.confirm.mockResolvedValue(true);
  document.getElementById("stab-sound").click();
  await vi.waitFor(() => expect(document.getElementById("spanel-sound").hidden).toBe(false));
});

it("guards category changes, restores the radio on refusal, and remembers tabs per category", async () => {
  guard.dirty.mockReturnValue(true);
  guard.confirm.mockResolvedValue(false);
  const { initSubtabs } = await import("../../src/autodj/static/modules/subtabs.js");
  const storage = { values: new Map(), getItem(key) { return this.values.get(key) ?? null; }, setItem(key, value) { this.values.set(key, value); } };
  const ui = initSubtabs(document.getElementById("settings-tabs"), { storage });
  const radios = document.querySelectorAll('#settings-section input[name="settings-section"]');
  radios[1].click();
  await vi.waitFor(() => expect(guard.confirm).toHaveBeenCalledTimes(1));
  expect(radios[0].checked).toBe(true);
  expect(ui.selected).toBe("mixing");
  expect(document.getElementById("spanel-mixing").hidden).toBe(false);

  guard.confirm.mockResolvedValue(true);
  radios[1].click();
  await vi.waitFor(() => expect(ui.selected).toBe("stations"));
  expect(radios[1].checked).toBe(true);
  expect([...document.querySelectorAll('#panel-settings > [role="tabpanel"]')].filter((panel) => !panel.hidden)).toHaveLength(1);
  guard.dirty.mockReturnValue(false);
  document.getElementById("stab-announcements").click();
  document.getElementById("stab-server").click();
  radios[0].click();
  expect(ui.selected).toBe("announcements");
  radios[1].click();
  expect(ui.selected).toBe("server");
});

it.each([
  ["liners", "announcements", "profile"],
  ["updates", "server", "general"],
])("maps old tab %s to %s in %s settings", async (legacy, tab, category) => {
  const { initSubtabs } = await import("../../src/autodj/static/modules/subtabs.js");
  const ui = initSubtabs(document.getElementById("settings-tabs"), {
    storage: { getItem: (key) => key === "autodj.settingsSubtab" ? legacy : null, setItem() {} },
  });
  expect(ui.selected).toBe(tab);
  expect(document.querySelector(`#settings-section [value="${category}"]`).checked).toBe(true);
});

it("keeps Settings after refused navigation and allows an approved tab change", async () => {
  window.history.replaceState(null, "", "#settings");
  const { initViewRouter } = await import("../../src/autodj/static/modules/tabs.js");
  initViewRouter();
  guard.confirm.mockResolvedValue(false);
  document.getElementById("tab-now").click();
  await vi.waitFor(() => expect(guard.confirm).toHaveBeenCalled());
  expect(document.getElementById("panel-settings").hidden).toBe(false);
  guard.confirm.mockResolvedValue(true);
  document.getElementById("tab-now").click();
  await vi.waitFor(() => expect(document.getElementById("panel-now").hidden).toBe(false));
});

it("keeps a clean profile's time-check draft clean before and after live metadata arrives", async () => {
  guard.actualDrafts = true;
  let releaseInitial;
  const runtime = {
    enabled: true, hour_format: "12", rule: "after_hour_song", timezone: "UTC", duck_db: -12,
    missing_minutes: 1000, tts_available: true, enabled_packs: [], pack_order: "rotate",
    packs: [{ name: "Studio", enabled: false, minutes_covered: 440, summary: "Live coverage." }],
  };
  const liveRequest = vi.fn(() => new Promise((resolve) => { releaseInitial = resolve; }));
  const profileValues = { time_checks: {
    enabled: false, hour_format: "24", rule: "off", timezone: "UTC", duck_db: -12,
    missing_minutes: 1439, tts_available: true, enabled_packs: ["Studio"], pack_order: "random",
  } };
  vi.stubGlobal("fetch", vi.fn(async () => new globalThis.Response(JSON.stringify({
    profile: "Evening", revision: "profile-1", values: profileValues,
  }), { headers: { "Content-Type": "application/json" } })));
  try {
    const { installTimeChecks } = await import("../../src/autodj/static/modules/time-checks.js");
    const { dirtySettingsParts, setSettingsProfile } = await import("../../src/autodj/static/modules/settings-drafts.js");
    const controls = installTimeChecks({ request: liveRequest, interval: () => {} });

    await setSettingsProfile("Evening");
    expect(dirtySettingsParts(document)).toEqual([]);
    liveRequest.mockResolvedValue(runtime);
    await controls.refresh();

    expect(dirtySettingsParts(document)).toEqual([]);
    expect(document.getElementById("tc-enabled").checked).toBe(false);
    expect(document.getElementById("tc-hour-format").value).toBe("24");
    expect(document.querySelector("#tc-pack-list input").checked).toBe(true);
    expect(document.getElementById("tc-pack-0-desc").textContent).toBe("Live coverage.");
  } finally {
    releaseInitial?.(runtime);
    await Promise.resolve();
    vi.unstubAllGlobals();
    guard.actualDrafts = false;
  }
});
