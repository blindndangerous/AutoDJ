import { beforeEach, describe, expect, it, vi } from "vitest";
import { clockText, setTimeFormat } from "../../src/autodj/static/modules/time-format.js";
import { installTimeSettings } from "../../src/autodj/static/modules/time-settings.js";
import { selectStation } from "../../src/autodj/static/modules/stations.js";

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

it("edits the log label with the station options and names the station ID it replaces", async () => {
  groups.length = 0;
  selectStation("default");
  document.body.innerHTML = `
    <div id="spanel-stations"><div id="stations-status"></div>
    <select id="time-zone"></select><select id="time-format"></select>
    <select id="station-output"><option value="browser">Browser</option><option value="stream">Stream</option></select>
    <input id="station-monitor" type="checkbox"><input id="station-upload-cap" type="number">
    <label for="station-log-label">Log label</label>
    <input id="station-log-label" type="text" maxlength="16" aria-describedby="station-log-label-help">
    <p id="station-log-label-help">Shown at the start of this station's log lines. Leave empty to use the station ID, <span id="station-log-label-id">default</span>.</p>
    </div>`;
  const options = { output: "stream", server_audio_too: false, liner_upload_max_mib: 50, log_label: "nor" };
  const request = vi.fn(async (url) => {
    if (url === "/api/time-settings") return { timezone: "UTC", timezones: ["UTC"], time_format: "24" };
    return options;
  });
  await installTimeSettings({ request }).load();
  const input = document.getElementById("station-log-label");
  const id = document.getElementById("station-log-label-id");
  expect(input.value).toBe("nor");
  expect(id.textContent).toBe("default");
  const draft = groups.find((group) => group.group === "station");
  // The error for a refused label is shown on this field: its key's place in read().
  const keys = Object.keys(draft.read());
  expect(draft.controls[keys.indexOf("log_label")]).toBe(input);
  input.value = "  nor-2 ";
  expect(draft.read()).toEqual({
    output: "stream", server_audio_too: false, liner_upload_max_mib: 50, log_label: "nor-2",
  });
  input.value = "";
  expect(draft.read().log_label).toBe("");
  // A station without a label shows an empty field, not "null".
  draft.write({ ...options, log_label: null });
  expect(input.value).toBe("");
  selectStation("nightowl");
  expect(id.textContent).toBe("nightowl");
  selectStation("default");
});
