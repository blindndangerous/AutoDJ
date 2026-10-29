import { afterEach, expect, it, vi } from "vitest";
import { installTimeSettings } from "../../src/autodj/static/modules/time-settings.js";
import { applySettingsDrafts, resetSettingsDrafts, settingsHaveChanges } from
  "../../src/autodj/static/modules/settings-drafts.js";

afterEach(() => {
  resetSettingsDrafts(document);
  document.body.replaceChildren();
});

it.each(["settings first", "choices first"])("loads time zones with Settings visible: %s", async (order) => {
  document.body.innerHTML = `
    <section id="panel-settings">
      <div id="spanel-stations" role="tabpanel"><select id="time-zone"></select></div>
      <div id="spanel-server" role="tabpanel">
        <select id="time-format"><option value="24">24 hour</option><option value="12">12 hour</option></select>
      </div>
    </section>`;
  const request = vi.fn(async () => ({
    timezone: "UTC", time_format: "24", timezones: ["America/New_York", "Europe/London", "UTC"],
  }));
  const settings = installTimeSettings({ request });
  const snapshot = { profile: "", revision: "one", values: {
    time: { timezone: "America/New_York", time_format: "24" },
  } };
  if (order === "settings first") applySettingsDrafts(snapshot);
  await settings.load();
  if (order === "choices first") applySettingsDrafts(snapshot);
  const zone = document.getElementById("time-zone");
  expect([...zone.options].map((option) => option.value)).toEqual(["America/New_York", "Europe/London", "UTC"]);
  expect(zone.value).toBe("America/New_York");
  expect(settingsHaveChanges()).toBe(false);

  zone.value = "Europe/London";
  zone.dispatchEvent(new Event("change", { bubbles: true }));
  await settings.load();
  expect(zone.value).toBe("Europe/London");
  expect(settingsHaveChanges()).toBe(true);
});
