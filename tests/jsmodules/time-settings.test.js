import { beforeEach, describe, expect, it, vi } from "vitest";
import { clockText, setTimeFormat } from "../../src/autodj/static/modules/time-format.js";
import { installTimeSettings } from "../../src/autodj/static/modules/time-settings.js";

const { groups } = vi.hoisted(() => ({ groups: [] }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  registerSettingsGroup: (spec) => {
    groups.push(spec);
    return { accept: (body) => spec.write(body), dirty: () => false };
  },
}));

const when = new Date(2026, 9, 14, 15, 4, 30);

describe("time format", () => {
  beforeEach(() => { groups.length = 0; setTimeFormat("24"); });

  it("writes 24-hour times unless set to 12 hour", () => {
    expect(clockText(when, { locale: "en-US" })).toBe("15:04");
    setTimeFormat("12");
    expect(clockText(when, { locale: "en-US", seconds: true })).toBe("3:04:30 PM");
  });

  it("loads time settings and exposes the station time draft", async () => {
    document.body.innerHTML = `
      <div id="spanel-stations"><div id="stations-status"></div>
      <select id="time-zone"></select>
      <select id="time-format"><option value="24">24</option><option value="12">12</option></select></div>`;
    const view = (timezone, format) => ({
      timezone, running_timezone: "UTC", time_format: format, timezones: ["Pacific/Auckland", "UTC"],
    });
    const request = vi.fn(async () => view("UTC", "24"));
    const settings = installTimeSettings({ request });
    await settings.load();
    expect(document.getElementById("time-zone").options[0].textContent).toBe("Pacific/Auckland");
    document.getElementById("time-zone").value = "Pacific/Auckland";
    document.getElementById("time-format").value = "12";
    const draft = groups.find((group) => group.group === "time");
    expect(draft.read()).toEqual({ timezone: "Pacific/Auckland", time_format: "12" });
    expect(request).toHaveBeenCalledTimes(1);
    draft.write({ timezone: "Pacific/Auckland", time_format: "12" });
    expect(clockText(when, { locale: "en-US" })).toBe("3:04 PM");
  });
});


it("exposes station output and cap as a draft, clearing incompatible monitoring", async () => {
  groups.length = 0;
  document.body.innerHTML = `
    <div id="spanel-stations"><div id="stations-status"></div>
    <select id="time-zone"></select><select id="time-format"></select>
    <select id="station-output"><option value="browser">Browser</option><option value="stream">Stream</option></select>
    <input id="station-monitor" type="checkbox"><input id="station-upload-cap" type="number">
    </div>`;
  const options = { output: "stream", server_audio_too: true, liner_upload_max_mib: 80 };
  const request = vi.fn(async (url) => {
    if (url === "/api/time-settings") return { timezone: "UTC", timezones: ["UTC"], time_format: "24" };
    return options;
  });
  await installTimeSettings({ request }).load();
  const output = document.getElementById("station-output");
  const monitor = document.getElementById("station-monitor");
  expect(monitor.checked).toBe(true);
  output.value = "browser";
  output.dispatchEvent(new Event("change"));
  expect(monitor.checked).toBe(false);
  expect(monitor.disabled).toBe(true);
  const draft = groups.find((group) => group.group === "station");
  expect(draft.read()).toEqual({ output: "browser", server_audio_too: false, liner_upload_max_mib: 80 });
  expect(request).toHaveBeenCalledTimes(2);
});
