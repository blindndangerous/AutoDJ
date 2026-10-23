import { beforeEach, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";

const guard = vi.hoisted(() => ({ dirty: vi.fn(), confirm: vi.fn() }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  settingsHaveChanges: guard.dirty, confirmSettingsNavigation: guard.confirm,
}));

beforeEach(() => {
  vi.resetModules(); guard.dirty.mockReturnValue(true); guard.confirm.mockReset();
  installIndexMarkup();
});

it("keeps the Settings subtab after a failed save and moves only after approval", async () => {
  const { initSubtabs } = await import("../../src/autodj/static/modules/subtabs.js");
  initSubtabs(document.getElementById("settings-tabs"), { storage: { getItem: () => "transitions", setItem() {} } });
  guard.confirm.mockResolvedValue(false);
  document.getElementById("stab-sound").click();
  await vi.waitFor(() => expect(guard.confirm).toHaveBeenCalledTimes(1));
  expect(document.getElementById("spanel-transitions").hidden).toBe(false);
  guard.confirm.mockResolvedValue(true);
  document.getElementById("stab-sound").click();
  await vi.waitFor(() => expect(document.getElementById("spanel-sound").hidden).toBe(false));
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
